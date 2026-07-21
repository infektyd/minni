// P1 honest health: afm_ok requires a verified 1-token completion, not a
// reachable /health endpoint. Covers both former health lies:
//   1. afm.ts bridge mode returned available:true unconditionally
//   2. sovereign.ts afmHealth treated any HTTP<400 parseable JSON as ok

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  callAfmJson,
  daemonAfmToProviderHealth,
  generationProbeTimeoutMs,
  getAfmProviderHealth,
  noteAfmGenerationFailure,
  resetAfmGenerationProbeCache,
  resolveAfmProvider,
} from "../dist/afm.js";
import { afmHealth, buildStatusReport } from "../dist/sovereign.js";

const HEALTH_UP = { ok: true, data: { status: "ok", adapter: null } };
const GENERATION_ALIVE = { ok: true, data: { choices: [{ message: { content: "y" } }] } };
const GENERATION_DEAD = { ok: false, error: "HTTP 503" };

// Shape of daemon-side afm_runtime_status() (src/minni/afm_provider.py:376-421),
// embedded as socket.data.afm by the daemon's "status" RPC (health.py:156-189).
const DAEMON_AFM_FRESH_BRIDGE = {
  mode: "bridge",
  status: "bridge",
  ok: true,
  generation_verified: true,
  reachable: true,
  probe_age_ms: 1200,
  native_available: false,
  native_helper_configured: false,
  adapter_configured: false,
  // Review r4 (P2): a bridge-effective daemon verdict must declare the probe
  // target so the plugin can confirm it probed the endpoint the plugin will
  // actually call. Defaults here match the plugin's own default
  // AFM_PREPARE_TASK_URL / AFM_PREPARE_TASK_MODEL.
  probe_url: "http://127.0.0.1:11437/v1/chat/completions",
  probe_model: "apple-foundation-models",
};

function transportStub(result) {
  const calls = [];
  const fn = async (url, payload) => {
    calls.push({ url, payload });
    return typeof result === "function" ? result(calls.length) : result;
  };
  fn.calls = calls;
  return fn;
}

test.beforeEach(() => {
  resetAfmGenerationProbeCache();
});

// --- the P1 gate -------------------------------------------------------------

test("GATE: /health up but generation dead => afm_ok=false", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const report = await buildStatusReport({
      vaultPath: root,
      socket: { ok: true, data: { status: "ok" } },
      afm: HEALTH_UP,
      afmGenerationTransport: transportStub(GENERATION_DEAD),
    });
    assert.equal(report.afm.ok, false, "afm_ok must be false when generation is dead");
    assert.equal(report.afm.data.reachable, true);
    assert.equal(report.afm.data.generationVerified, false);
    assert.equal(report.extractor.provider, "bridge");
    assert.equal(report.extractor.tier, "local");
    assert.equal(report.extractor.generationVerified, false);
    assert.equal(typeof report.extractor.probeAgeMs, "number");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("GATE: working generation => afm_ok=true", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const report = await buildStatusReport({
      vaultPath: root,
      socket: { ok: true, data: { status: "ok" } },
      afm: HEALTH_UP,
      afmGenerationTransport: transportStub(GENERATION_ALIVE),
    });
    assert.equal(report.afm.ok, true);
    assert.equal(report.afm.data.generationVerified, true);
    assert.equal(report.extractor.generationVerified, true);
    assert.ok(report.extractor.probeAgeMs >= 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("buildStatusReport skips the generation probe when /health is already down", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = transportStub(GENERATION_ALIVE);
    const report = await buildStatusReport({
      vaultPath: root,
      socket: { ok: true, data: { status: "ok" } },
      afm: { ok: false, error: "connect ECONNREFUSED 127.0.0.1:11437" },
      afmGenerationTransport: transport,
    });
    assert.equal(report.afm.ok, false);
    assert.equal(report.afm.data.reachable, false);
    assert.equal(transport.calls.length, 0, "no generation call when health is down");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// --- ProviderHealth probe cache ------------------------------------------------

test("getAfmProviderHealth caches verified probes within the TTL", async () => {
  const transport = transportStub(GENERATION_ALIVE);
  let clock = 1_000_000;
  const now = () => clock;
  const opts = { mode: "bridge", health: HEALTH_UP, transport, ttlMs: 300_000, now };

  const first = await getAfmProviderHealth(opts);
  clock += 60_000;
  const second = await getAfmProviderHealth(opts);

  assert.equal(first.ok, true);
  assert.equal(first.generationVerified, true);
  assert.equal(first.probeAgeMs, 0);
  assert.equal(second.ok, true);
  assert.equal(second.probeAgeMs, 60_000);
  assert.equal(transport.calls.length, 1, "second read served from cache");
});

test("getAfmProviderHealth serves stale and revalidates in the background", async () => {
  const transport = transportStub(GENERATION_ALIVE);
  let clock = 1_000_000;
  const now = () => clock;
  const opts = { mode: "bridge", health: HEALTH_UP, transport, ttlMs: 300_000, now };

  await getAfmProviderHealth(opts);
  clock += 600_000; // past TTL
  const stale = await getAfmProviderHealth(opts);
  assert.equal(stale.ok, true, "stale-while-revalidate keeps serving the last value");
  assert.equal(stale.probeAgeMs, 600_000);

  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(transport.calls.length, 2, "background refresh re-probed");
  const refreshed = await getAfmProviderHealth(opts);
  assert.equal(refreshed.probeAgeMs, 0);
  assert.equal(transport.calls.length, 2);
});

test("noteAfmGenerationFailure invalidates the cached probe", async () => {
  const chatUrl = "http://127.0.0.1:11437/v1/chat/completions";
  const transport = transportStub(GENERATION_ALIVE);
  const opts = { mode: "bridge", chatUrl, health: HEALTH_UP, transport };

  await getAfmProviderHealth(opts);
  noteAfmGenerationFailure(chatUrl);
  await getAfmProviderHealth(opts);

  assert.equal(transport.calls.length, 2, "invalidated entry must re-probe");
});

test("a failed live callAfmJson invalidates the cached generation probe", async () => {
  const chatUrl = "http://127.0.0.1:11437/v1/chat/completions";
  const transport = transportStub(GENERATION_ALIVE);
  await getAfmProviderHealth({ mode: "bridge", chatUrl, health: HEALTH_UP, transport });
  assert.equal(transport.calls.length, 1);

  const failed = await callAfmJson(chatUrl, { messages: [] }, {
    mode: "bridge",
    transport: async () => GENERATION_DEAD,
  });
  assert.equal(failed.ok, false);

  await getAfmProviderHealth({ mode: "bridge", chatUrl, health: HEALTH_UP, transport });
  assert.equal(transport.calls.length, 2, "call failure must force a fresh probe");
});

test("a successful live callAfmJson refreshes a negative cached probe (symmetric positive signal)", async () => {
  const chatUrl = "http://127.0.0.1:11437/v1/chat/completions";
  const probeTransport = transportStub(GENERATION_DEAD);

  const before = await getAfmProviderHealth({ mode: "bridge", chatUrl, health: HEALTH_UP, transport: probeTransport });
  assert.equal(before.ok, false, "dead probe caches a negative entry");

  const live = await callAfmJson(chatUrl, { messages: [] }, {
    mode: "bridge",
    transport: async () => GENERATION_ALIVE,
  });
  assert.equal(live.ok, true);

  const after = await getAfmProviderHealth({ mode: "bridge", chatUrl, health: HEALTH_UP, transport: probeTransport });
  assert.equal(after.ok, true, "live success is a generation proof; recovery must not wait out the TTL");
  assert.equal(after.generationVerified, true);
  assert.equal(probeTransport.calls.length, 1, "no extra probe needed after the live success");
});

// --- finding 1: hot-cache poisoning guard ----------------------------------------

test("hollow 200 live call (no choices) must NOT poison the probe cache", async () => {
  const chatUrl = "http://127.0.0.1:11437/v1/chat/completions";
  // A bridge answering 200 {"status":"ok"} without completion content.
  const hollow = await callAfmJson(chatUrl, { messages: [] }, {
    mode: "bridge",
    transport: async () => ({ ok: true, data: { status: "ok" } }),
  });
  assert.equal(hollow.ok, true);

  // Health must still run (and trust) the real content-checking probe.
  const probeTransport = transportStub(GENERATION_DEAD);
  const health = await getAfmProviderHealth({ mode: "bridge", chatUrl, health: HEALTH_UP, transport: probeTransport });
  assert.equal(probeTransport.calls.length, 1, "hollow 200 must not satisfy the generation probe");
  assert.equal(health.ok, false, "afm_ok must come from the real probe, not the hollow live call");
  assert.equal(health.generationVerified, false);
});

test("hollow 200 live call is neutral: it does not invalidate a verified probe either", async () => {
  const chatUrl = "http://127.0.0.1:11437/v1/chat/completions";
  const probeTransport = transportStub(GENERATION_ALIVE);
  await getAfmProviderHealth({ mode: "bridge", chatUrl, health: HEALTH_UP, transport: probeTransport });

  await callAfmJson(chatUrl, { messages: [] }, {
    mode: "bridge",
    transport: async () => ({ ok: true, data: { status: "ok" } }),
  });

  const after = await getAfmProviderHealth({ mode: "bridge", chatUrl, health: HEALTH_UP, transport: probeTransport });
  assert.equal(after.ok, true);
  assert.equal(probeTransport.calls.length, 1, "neutral signal must not force a re-probe");
});

// --- finding 3: cross-process persistent probe cache ------------------------------

async function withProbeCacheFile(run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-probe-cache-"));
  const cacheFile = path.join(root, "afm-probe-cache.json");
  const previous = process.env.MINNI_AFM_PROBE_CACHE;
  process.env.MINNI_AFM_PROBE_CACHE = cacheFile;
  resetAfmGenerationProbeCache(); // clears L1 and the (now redirected) file
  try {
    return await run(cacheFile);
  } finally {
    if (previous === undefined) delete process.env.MINNI_AFM_PROBE_CACHE;
    else process.env.MINNI_AFM_PROBE_CACHE = previous;
    resetAfmGenerationProbeCache();
    await rm(root, { recursive: true, force: true });
  }
}

const PROBE_CACHE_KEY = "bridge|http://127.0.0.1:11437/v1/chat/completions";

function persistedCacheBody(probedAtMs, generationVerified = true) {
  return JSON.stringify({
    version: 1,
    entries: {
      [PROBE_CACHE_KEY]: {
        reachable: true,
        generation_verified: generationVerified,
        detail: null,
        probed_at_ms: probedAtMs,
      },
    },
  });
}

test("persistent cache: a fresh process reuses a warm file entry without a live probe", async () => {
  await withProbeCacheFile(async (cacheFile) => {
    // Simulates a probe persisted by another process moments ago.
    await writeFile(cacheFile, persistedCacheBody(Date.now() - 1_000), "utf8");
    const transport = transportStub(GENERATION_ALIVE);
    const health = await getAfmProviderHealth({
      mode: "bridge",
      chatUrl: "http://127.0.0.1:11437/v1/chat/completions",
      health: HEALTH_UP,
      transport,
    });
    assert.equal(transport.calls.length, 0, "warm file entry under TTL must skip the live probe");
    assert.equal(health.ok, true);
    assert.equal(health.generationVerified, true);
  });
});

test("persistent cache: a stale file entry forces a normal probe", async () => {
  await withProbeCacheFile(async (cacheFile) => {
    await writeFile(cacheFile, persistedCacheBody(Date.now() - 10 * 60 * 1000), "utf8");
    const transport = transportStub(GENERATION_ALIVE);
    const health = await getAfmProviderHealth({
      mode: "bridge",
      chatUrl: "http://127.0.0.1:11437/v1/chat/completions",
      health: HEALTH_UP,
      transport,
    });
    assert.equal(transport.calls.length, 1, "stale file entry must re-probe");
    assert.equal(health.ok, true);
    assert.equal(health.probeAgeMs < 60_000, true, "served entry comes from the fresh probe");
  });
});

test("persistent cache: a corrupt file is ignored gracefully", async () => {
  await withProbeCacheFile(async (cacheFile) => {
    await writeFile(cacheFile, "{not json", "utf8");
    const transport = transportStub(GENERATION_ALIVE);
    const health = await getAfmProviderHealth({
      mode: "bridge",
      chatUrl: "http://127.0.0.1:11437/v1/chat/completions",
      health: HEALTH_UP,
      transport,
    });
    assert.equal(transport.calls.length, 1, "corrupt file degrades to a normal probe");
    assert.equal(health.ok, true);
  });
});

test("persistent cache: a probe persists its result for the next process", async () => {
  await withProbeCacheFile(async (cacheFile) => {
    const transport = transportStub(GENERATION_ALIVE);
    await getAfmProviderHealth({
      mode: "bridge",
      chatUrl: "http://127.0.0.1:11437/v1/chat/completions",
      health: HEALTH_UP,
      transport,
    });
    const persisted = JSON.parse(await readFile(cacheFile, "utf8"));
    assert.equal(persisted.version, 1);
    assert.equal(persisted.entries[PROBE_CACHE_KEY].generation_verified, true);
    assert.equal(typeof persisted.entries[PROBE_CACHE_KEY].probed_at_ms, "number");
  });
});

test("persistent cache: a failed live call invalidates the file entry too", async () => {
  await withProbeCacheFile(async (cacheFile) => {
    const chatUrl = "http://127.0.0.1:11437/v1/chat/completions";
    await writeFile(cacheFile, persistedCacheBody(Date.now() - 1_000), "utf8");
    await callAfmJson(chatUrl, { messages: [] }, {
      mode: "bridge",
      transport: async () => GENERATION_DEAD,
    });
    const persisted = JSON.parse(await readFile(cacheFile, "utf8"));
    assert.equal(persisted.entries[PROBE_CACHE_KEY], undefined, "poisoned/stale entries must not survive a live failure");
  });
});

// --- native-mode generation health ----------------------------------------------

async function withProbeHelper(responseJson, run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-native-health-"));
  const helper = path.join(root, "helper.mjs");
  await writeFile(
    helper,
    ["#!/usr/bin/env node", `process.stdout.write(${JSON.stringify(JSON.stringify(responseJson))});`].join("\n"),
    "utf8",
  );
  await chmod(helper, 0o755);
  try {
    return await run(helper);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("native mode: helper completion with content => afm_ok=true", async () => {
  await withProbeHelper({ ok: true, data: { answer: "y" } }, async (helper) => {
    const health = await getAfmProviderHealth({ mode: "native", nativeHelperPath: helper, timeoutMs: 10_000 });
    assert.equal(health.ok, true);
    assert.equal(health.generationVerified, true);
  });
});

test("native mode: ok response without completion content is NOT verified", async () => {
  await withProbeHelper({ ok: true, data: {} }, async (helper) => {
    const health = await getAfmProviderHealth({ mode: "native", nativeHelperPath: helper, timeoutMs: 10_000 });
    assert.equal(health.ok, false, "ok with empty output proves nothing about generation");
    assert.equal(health.generationVerified, false);
  });
});

test("native mode: helper rejection => afm_ok=false", async () => {
  await withProbeHelper({ ok: false, error: "model unavailable" }, async (helper) => {
    const health = await getAfmProviderHealth({ mode: "native", nativeHelperPath: helper, timeoutMs: 10_000 });
    assert.equal(health.ok, false);
    assert.equal(health.generationVerified, false);
  });
});

test("native mode: missing helper => afm_ok=false", async () => {
  const health = await getAfmProviderHealth({ mode: "native", nativeHelperPath: "/tmp/missing-native-helper" });
  assert.equal(health.ok, false);
  assert.equal(health.generationVerified, false);
});

test("auto mode without a helper probes the bridge with the bare chat body", async () => {
  const transport = transportStub(GENERATION_ALIVE);
  const health = await getAfmProviderHealth({
    mode: "auto",
    health: HEALTH_UP,
    transport,
    nativeHelperPath: undefined,
  });
  assert.equal(health.ok, true);
  assert.equal(transport.calls.length, 1);
  // Auto resolved to bridge: the probe body is the chat payload, not the
  // wrapped native envelope.
  assert.equal(transport.calls[0].payload.max_tokens, 1);
  assert.equal(transport.calls[0].payload.payload, undefined);
});

test("getAfmProviderHealth sends a 1-token completion probe", async () => {
  const transport = transportStub(GENERATION_ALIVE);
  await getAfmProviderHealth({ mode: "bridge", health: HEALTH_UP, transport });
  assert.equal(transport.calls.length, 1);
  const payload = transport.calls[0].payload;
  assert.equal(payload.max_tokens, 1);
  assert.equal(payload.temperature, 0);
  assert.deepEqual(payload.messages, [{ role: "user", content: "ok" }]);
});

test("getAfmProviderHealth off mode never probes", async () => {
  const transport = transportStub(GENERATION_ALIVE);
  const health = await getAfmProviderHealth({ mode: "off", transport });
  assert.equal(health.ok, false);
  assert.equal(health.generationVerified, false);
  assert.equal(transport.calls.length, 0);
});

test("ProviderHealth detail is sanitized", async () => {
  const transport = transportStub({
    ok: false,
    error: "spawn /Users/alice/private/helper failed for extractor.fmadapter",
  });
  const health = await getAfmProviderHealth({ mode: "bridge", health: HEALTH_UP, transport });
  assert.equal(health.ok, false);
  assert.doesNotMatch(health.detail ?? "", /\/Users\/alice/);
});

// --- fixed lie 1: bridge availability ------------------------------------------

test("resolveAfmProvider bridge mode is unavailable when health probe failed", () => {
  const provider = resolveAfmProvider("bridge", {
    nativeHelperPath: undefined,
    health: { ok: false, error: "connect ECONNREFUSED 127.0.0.1:11437" },
  });
  assert.equal(provider.provider, "bridge");
  assert.equal(provider.available, false);
  assert.match(provider.reason ?? "", /ECONNREFUSED|unavailable/);
});

test("resolveAfmProvider bridge mode stays available with healthy probe", () => {
  const provider = resolveAfmProvider("bridge", {
    nativeHelperPath: undefined,
    health: HEALTH_UP,
  });
  assert.equal(provider.available, true);
});

// --- fixed lie 2: afmHealth inspects the body -----------------------------------

async function withHealthServer(body, run, status = 200) {
  const server = createServer((req, res) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    return await run(`http://127.0.0.1:${server.address().port}/health`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("afmHealth flags degraded status values instead of trusting HTTP 200", async () => {
  await withHealthServer({ status: "error", availability: "unavailable" }, async (url) => {
    const result = await afmHealth(url);
    assert.equal(result.ok, false);
    assert.match(result.error ?? "", /status=error/);
  });
});

test("afmHealth flags availability=unavailable", async () => {
  await withHealthServer({ status: "ok", availability: "unavailable" }, async (url) => {
    const result = await afmHealth(url);
    assert.equal(result.ok, false);
    assert.match(result.error ?? "", /availability=unavailable/);
  });
});

test("afmHealth accepts a healthy body (adapter:null is fine)", async () => {
  await withHealthServer({ status: "ok", adapter: null }, async (url) => {
    const result = await afmHealth(url);
    assert.equal(result.ok, true);
  });
});

// --- finding 2: unknown status strings must not veto the generation probe --------

test("afmHealth lets unknown status strings through (the generation probe decides)", async () => {
  await withHealthServer({ status: "initializing" }, async (url) => {
    const result = await afmHealth(url);
    assert.equal(result.ok, true, "unknown status must not hard-fail health");
  });
  await withHealthServer({ status: "degraded-but-serving" }, async (url) => {
    const result = await afmHealth(url);
    assert.equal(result.ok, true);
  });
});

test("unknown /health status + working generation => afm_ok=true", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = transportStub(GENERATION_ALIVE);
    await withHealthServer({ status: "initializing" }, async (url) => {
      const health = await afmHealth(url);
      const report = await buildStatusReport({
        vaultPath: root,
        socket: { ok: true, data: { status: "ok" } },
        afm: health,
        afmGenerationTransport: transport,
      });
      assert.equal(report.afm.ok, true, "the probe, not the unknown status string, decides afm_ok");
      assert.equal(report.afm.data.generationVerified, true);
      assert.equal(transport.calls.length, 1, "the generation probe must run");
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("availability=unavailable is a definitive negative: probe skipped, afm_ok=false", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = transportStub(GENERATION_ALIVE);
    await withHealthServer({ status: "ok", availability: "unavailable" }, async (url) => {
      const health = await afmHealth(url);
      assert.equal(health.ok, false);
      const report = await buildStatusReport({
        vaultPath: root,
        socket: { ok: true, data: { status: "ok" } },
        afm: health,
        afmGenerationTransport: transport,
      });
      assert.equal(report.afm.ok, false);
      assert.equal(transport.calls.length, 0, "definitive negative must skip the probe");
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("afmHealth still fails on HTTP errors", async () => {
  await withHealthServer({ status: "ok" }, async (url) => {
    const result = await afmHealth(url);
    assert.equal(result.ok, false);
    assert.equal(result.error, "HTTP 503");
  }, 503);
});

test("native mode: dead bridge /health must not veto a working native helper", async () => {
  // The flaky-bridge scenario: hermes /health is down, but the native helper
  // generates fine. buildStatusReport must probe the helper directly instead
  // of short-circuiting on the bridge health result.
  await withProbeHelper({ ok: true, data: { answer: "y" } }, async (helper) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
    const previousHelper = process.env.MINNI_AFM_NATIVE_HELPER;
    process.env.MINNI_AFM_NATIVE_HELPER = helper;
    try {
      const report = await buildStatusReport({
        vaultPath: root,
        socket: { ok: true, data: { status: "ok" } },
        afmProviderMode: "native",
        afm: { ok: false, error: "connect ECONNREFUSED 127.0.0.1:11437" },
      });
      assert.equal(report.afm.ok, true, "native generation works; the dead bridge must not invert afm_ok");
      assert.equal(report.afm.data.generationVerified, true);
      assert.equal(report.afm.error, undefined);
      assert.equal(report.extractor.provider, "native");
      assert.equal(report.afmProvider.status, "native_available");
      assert.doesNotMatch(JSON.stringify(report), /11437/);
    } finally {
      if (previousHelper === undefined) delete process.env.MINNI_AFM_NATIVE_HELPER;
      else process.env.MINNI_AFM_NATIVE_HELPER = previousHelper;
      await rm(root, { recursive: true, force: true });
    }
  });
});

// --- W4 (punch-list §3): single-authority AFM verdict ---------------------------
//
// ~9h status simultaneously said daemon generation_verified:true (10s budget)
// and plugin afm.ok:false "native AFM helper timed out" (1.5s budget) while
// measured AFM p95 was 10-15s -> structurally guaranteed false negatives.
// Fix: the plugin probe budget mirrors the daemon's (generationProbeTimeoutMs),
// and when the daemon socket already ran a fresh matching-mode probe,
// buildStatusReport reuses it instead of spawning a second, cold, redundant
// probe (Health Endpoint Aggregation: consume the dependency's fresh summary).

async function withEnvTimeout(value, run) {
  const previous = process.env.MINNI_AFM_PROBE_TIMEOUT;
  try {
    if (value === undefined) delete process.env.MINNI_AFM_PROBE_TIMEOUT;
    else process.env.MINNI_AFM_PROBE_TIMEOUT = value;
    return await run();
  } finally {
    if (previous === undefined) delete process.env.MINNI_AFM_PROBE_TIMEOUT;
    else process.env.MINNI_AFM_PROBE_TIMEOUT = previous;
  }
}

test("generationProbeTimeoutMs mirrors MINNI_AFM_PROBE_TIMEOUT (matches the daemon's default, not the old 1.5s)", async () => {
  await withEnvTimeout(undefined, async () => {
    assert.equal(generationProbeTimeoutMs(), 10000, "unset env must match the daemon's 10s default");
  });
  await withEnvTimeout("not-a-float", async () => {
    assert.equal(generationProbeTimeoutMs(), 10000, "invalid value falls back to 10s (mirrors afm_provider.py:99-106)");
  });
  await withEnvTimeout("-1", async () => {
    assert.equal(generationProbeTimeoutMs(), 10000, "non-positive value falls back to 10s");
  });
  await withEnvTimeout("2.5", async () => {
    assert.equal(generationProbeTimeoutMs(), 2500, "a valid float is honored, converted to milliseconds");
  });
});

async function withDelayedProbeHelper(delayMs, responseJson, run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-native-timeout-"));
  const helper = path.join(root, "helper.mjs");
  await writeFile(
    helper,
    [
      "#!/usr/bin/env node",
      `setTimeout(() => { process.stdout.write(${JSON.stringify(JSON.stringify(responseJson))}); }, ${delayMs});`,
    ].join("\n"),
    "utf8",
  );
  await chmod(helper, 0o755);
  try {
    return await run(helper);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test("getAfmProviderHealth actually uses the env-derived timeout, not a hardcoded 1500ms", async () => {
  // A 1.7s-slow probe would have been killed by the old hardcoded
  // GENERATION_PROBE_TIMEOUT_MS = 1500; the new default (10s, mirroring the
  // daemon) must let it complete.
  await withDelayedProbeHelper(1700, { ok: true, data: { answer: "y" } }, async (helper) => {
    await withEnvTimeout(undefined, async () => {
      resetAfmGenerationProbeCache();
      const health = await getAfmProviderHealth({ mode: "native", nativeHelperPath: helper });
      assert.equal(health.ok, true, "a 1.7s-slow probe must survive the 10s default budget");
    });
  });
});

test("getAfmProviderHealth honors a live MINNI_AFM_PROBE_TIMEOUT override, read at call time", async () => {
  // The env var is set AFTER the module was already imported at the top of
  // this file — proves the timeout is read at call time (PR84-5-class fix),
  // not bound once at import like the old GENERATION_PROBE_TIMEOUT_MS constant.
  await withDelayedProbeHelper(600, { ok: true, data: { answer: "y" } }, async (helper) => {
    await withEnvTimeout("0.2", async () => {
      resetAfmGenerationProbeCache();
      const health = await getAfmProviderHealth({ mode: "native", nativeHelperPath: helper });
      assert.equal(health.ok, false, "a live 200ms override must be honored, killing a 600ms-slow probe");
      assert.match(health.detail ?? "", /timed out/);
    });
  });
});

test("buildStatusReport reuses a fresh daemon-sourced AFM verdict instead of spawning its own probe", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = async () => {
      assert.fail("must not probe — a fresh matching-mode daemon result should be reused");
    };
    const report = await buildStatusReport({
      vaultPath: root,
      afmProviderMode: "bridge",
      socket: { ok: true, data: { status: "ok", afm: DAEMON_AFM_FRESH_BRIDGE } },
      afm: HEALTH_UP,
      afmGenerationTransport: transport,
    });
    assert.equal(report.afm.ok, true);
    assert.equal(report.afm.data.generationVerified, true);
    assert.equal(report.afm.data.source, "daemon", "authority tag must say the daemon produced this verdict");
    assert.equal(report.extractor.probeAgeMs, 1200, "probe age must come straight from the daemon's own probe_age_ms");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("buildStatusReport falls back to its own probe when the daemon status has no afm block (back-compat)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = transportStub(GENERATION_ALIVE);
    // Exact existing fixture used throughout this file (no .afm key) — "afm
    // field ABSENT in socket.data" must behave identically to "socket
    // unreachable": fall through to today's probe, unchanged.
    const report = await buildStatusReport({
      vaultPath: root,
      socket: { ok: true, data: { status: "ok" } },
      afm: HEALTH_UP,
      afmGenerationTransport: transport,
    });
    assert.equal(report.afm.ok, true);
    assert.equal(report.afm.data.source, "plugin-probe");
    assert.equal(transport.calls.length, 1, "no daemon afm block => the plugin probe must still run");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("buildStatusReport falls back to its own probe when the daemon's afm mode doesn't match the plugin's resolved provider", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = transportStub(GENERATION_ALIVE);
    const mismatched = { ...DAEMON_AFM_FRESH_BRIDGE, mode: "native" };
    const report = await buildStatusReport({
      vaultPath: root,
      afmProviderMode: "bridge", // plugin resolves "bridge"; daemon claims "native"
      socket: { ok: true, data: { status: "ok", afm: mismatched } },
      afm: HEALTH_UP,
      afmGenerationTransport: transport,
    });
    assert.equal(transport.calls.length, 1, "a mode mismatch must not be trusted; the plugin probe must run");
    assert.equal(report.afm.data.source, "plugin-probe");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("buildStatusReport falls back to its own probe when the daemon's afm probe is stale (probe_age_ms >= TTL)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = transportStub(GENERATION_ALIVE);
    const stale = { ...DAEMON_AFM_FRESH_BRIDGE, probe_age_ms: 6 * 60 * 1000 };
    const report = await buildStatusReport({
      vaultPath: root,
      afmProviderMode: "bridge",
      socket: { ok: true, data: { status: "ok", afm: stale } },
      afm: HEALTH_UP,
      afmGenerationTransport: transport,
    });
    assert.equal(transport.calls.length, 1, "a stale (>=5min) daemon probe must not be trusted; the plugin re-probes");
    assert.equal(report.afm.data.source, "plugin-probe");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("buildStatusReport falls back to its own probe when the daemon's bridge probe targeted a DIFFERENT url than this plugin will call", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = transportStub(GENERATION_ALIVE);
    // Daemon verified a bridge endpoint on a different port; this plugin's
    // configured AFM_PREPARE_TASK_URL is the default (:11437). The daemon's
    // afm.ok says nothing about the endpoint THIS process uses, so the verdict
    // must not be reused — the plugin re-probes its own target.
    const otherTarget = { ...DAEMON_AFM_FRESH_BRIDGE, probe_url: "http://127.0.0.1:9999/v1/chat/completions" };
    const report = await buildStatusReport({
      vaultPath: root,
      afmProviderMode: "bridge",
      socket: { ok: true, data: { status: "ok", afm: otherTarget } },
      afm: HEALTH_UP,
      afmGenerationTransport: transport,
    });
    assert.equal(transport.calls.length, 1, "a target-url mismatch must not be trusted; the plugin probe must run");
    assert.equal(report.afm.data.source, "plugin-probe");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("buildStatusReport falls back to its own probe when a bridge daemon verdict omits its probe target (not comparable)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    const transport = transportStub(GENERATION_ALIVE);
    // Older daemon build: a bridge verdict with no probe_url/probe_model. The
    // plugin cannot confirm the targets match, so it falls back rather than
    // adopting an unverifiable verdict.
    const { probe_url, probe_model, ...noTarget } = DAEMON_AFM_FRESH_BRIDGE;
    void probe_url;
    void probe_model;
    const report = await buildStatusReport({
      vaultPath: root,
      afmProviderMode: "bridge",
      socket: { ok: true, data: { status: "ok", afm: noTarget } },
      afm: HEALTH_UP,
      afmGenerationTransport: transport,
    });
    assert.equal(transport.calls.length, 1, "an unstated target is not comparable; the plugin probe must run");
    assert.equal(report.afm.data.source, "plugin-probe");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("daemonAfmToProviderHealth gates a bridge verdict on the probe target (explicit expected url/model)", () => {
  const now = () => 2_000_000;
  const ttlMs = 300_000;
  const base = {
    mode: "bridge",
    status: "bridge",
    generation_verified: true,
    reachable: true,
    probe_age_ms: 100,
    probe_url: "http://127.0.0.1:11437/v1/chat/completions",
    probe_model: "apple-foundation-models",
  };
  const url = "http://127.0.0.1:11437/v1/chat/completions";
  const model = "apple-foundation-models";
  // Matching target → reused.
  assert.ok(
    daemonAfmToProviderHealth(base, "bridge", ttlMs, now, undefined, url, model),
    "matching probe target must be reused",
  );
  // Mismatched url → undefined (fall back).
  assert.equal(
    daemonAfmToProviderHealth({ ...base, probe_url: "http://127.0.0.1:9999/v1/chat/completions" }, "bridge", ttlMs, now, undefined, url, model),
    undefined,
    "a different probe_url must not be reused",
  );
  // Mismatched model → undefined (fall back).
  assert.equal(
    daemonAfmToProviderHealth({ ...base, probe_model: "some-other-model" }, "bridge", ttlMs, now, undefined, url, model),
    undefined,
    "a different probe_model must not be reused",
  );
  // Absent target → undefined (not comparable).
  assert.equal(
    daemonAfmToProviderHealth({ ...base, probe_url: undefined, probe_model: undefined }, "bridge", ttlMs, now, undefined, url, model),
    undefined,
    "an absent probe target is not comparable and must not be reused",
  );
});

test("buildStatusReport never reports contradicting socket.data.afm and top-level afm (the punch-list §3 scenario)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-honest-health-"));
  try {
    // What the OLD budget-mismatched (1.5s) plugin probe would have said.
    const transport = transportStub(GENERATION_DEAD);
    const report = await buildStatusReport({
      vaultPath: root,
      afmProviderMode: "bridge",
      socket: { ok: true, data: { status: "ok", afm: DAEMON_AFM_FRESH_BRIDGE } }, // daemon: fresh, generation_verified:true
      afm: HEALTH_UP,
      afmGenerationTransport: transport,
    });
    assert.equal(report.afm.ok, true, "the daemon's fresh verdict must win; both legs can no longer disagree");
    assert.equal(transport.calls.length, 0, "no independent, budget-mismatched probe may run when a fresh daemon verdict exists");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("daemonAfmToProviderHealth rejects malformed daemon payloads defensively", () => {
  const now = () => 2_000_000;
  const ttlMs = 300_000;
  const cases = [
    ["missing generation_verified", { mode: "bridge", reachable: true, probe_age_ms: 100 }],
    ["wrong type for generation_verified", { mode: "bridge", generation_verified: "yes", reachable: true, probe_age_ms: 100 }],
    ["missing reachable", { mode: "bridge", generation_verified: true, probe_age_ms: 100 }],
    ["missing probe_age_ms", { mode: "bridge", generation_verified: true, reachable: true }],
    ["afm block absent entirely", undefined],
    ["non-object payload", "not-an-object"],
    ["truthy numeric fields must not be coerced", { mode: "bridge", generation_verified: 1, reachable: 1, probe_age_ms: 100 }],
  ];
  for (const [label, payload] of cases) {
    assert.equal(daemonAfmToProviderHealth(payload, "bridge", ttlMs, now), undefined, label);
  }
});

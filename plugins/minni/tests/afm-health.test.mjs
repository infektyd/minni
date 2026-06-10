// P1 honest health: afm_ok requires a verified 1-token completion, not a
// reachable /health endpoint. Covers both former health lies:
//   1. afm.ts bridge mode returned available:true unconditionally
//   2. sovereign.ts afmHealth treated any HTTP<400 parseable JSON as ok

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  callAfmJson,
  getAfmProviderHealth,
  noteAfmGenerationFailure,
  resetAfmGenerationProbeCache,
  resolveAfmProvider,
} from "../dist/afm.js";
import { afmHealth, buildStatusReport } from "../dist/sovereign.js";

const HEALTH_UP = { ok: true, data: { status: "ok", adapter: null } };
const GENERATION_ALIVE = { ok: true, data: { choices: [{ message: { content: "y" } }] } };
const GENERATION_DEAD = { ok: false, error: "HTTP 503" };

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

test("afmHealth still fails on HTTP errors", async () => {
  await withHealthServer({ status: "ok" }, async (url) => {
    const result = await afmHealth(url);
    assert.equal(result.ok, false);
    assert.equal(result.error, "HTTP 503");
  }, 503);
});

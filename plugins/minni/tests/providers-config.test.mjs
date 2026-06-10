// P3 config surface: ~/.minni/providers.json loader, secret handling
// (apiKeyEnv / 0600 apiKeyFile under ~/.minni/secrets/), and the G13 model
// target allowlist (MINNI_MODEL_ALLOWED_TARGETS alias + HTTPS-required for
// non-loopback). Includes the negative-path proof that the cloud key string
// never appears in status/audit/error output.

import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { callAfmJson, checkModelTarget, resetAfmGenerationProbeCache, sanitizeAfmHealth } from "../dist/afm.js";
import { loadProvidersConfig, modelAllowedTargets, resolveCloudApiKey } from "../dist/config.js";
import { buildStatusReport } from "../dist/sovereign.js";

const SECRET_KEY = "sk-test-vErYsEcReT-cLoUdKeY-12345";

function withEnv(overrides, run) {
  const saved = {};
  for (const [key, value] of Object.entries(overrides)) {
    saved[key] = process.env[key];
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  const restore = () => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  };
  return run().finally(restore);
}

// --- providers.json loader ----------------------------------------------------

test("loadProvidersConfig returns the AFM-only default when the file is missing", () => {
  const config = loadProvidersConfig("/tmp/definitely-missing-minni-providers.json");
  assert.deepEqual(config.chain, ["afm"]);
  assert.deepEqual(config.operations, { retrieval: { localOnly: true } });
  assert.deepEqual(config.providers, {});
});

test("loadProvidersConfig parses the documented shape", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-providers-"));
  try {
    const file = path.join(root, "providers.json");
    await writeFile(
      file,
      JSON.stringify({
        chain: ["afm", "mlx", "ollama"],
        operations: { retrieval: { localOnly: true }, prepare: { localOnly: false } },
        providers: {
          mlx: { baseUrl: "http://127.0.0.1:8080", model: "mlx-community/some-model" },
          ollama: { baseUrl: "http://127.0.0.1:11434", model: "qwen3" },
          cloud: { enabled: false, vendor: "anthropic", model: "claude-haiku", apiKeyEnv: "MINNI_CLOUD_KEY", privacyMax: true },
        },
      }),
      "utf8",
    );
    const config = loadProvidersConfig(file);
    assert.deepEqual(config.chain, ["afm", "mlx", "ollama"]);
    assert.equal(config.operations.retrieval.localOnly, true);
    assert.equal(config.providers.mlx.model, "mlx-community/some-model");
    assert.equal(config.providers.cloud.vendor, "anthropic");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("loadProvidersConfig rejects inline cloud apiKey and disables the provider", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-providers-"));
  try {
    const file = path.join(root, "providers.json");
    await writeFile(
      file,
      JSON.stringify({
        chain: ["afm"],
        providers: { cloud: { enabled: true, vendor: "openai", apiKey: SECRET_KEY } },
      }),
      "utf8",
    );
    const config = loadProvidersConfig(file);
    assert.equal(config.providers.cloud.enabled, false);
    assert.equal("apiKey" in config.providers.cloud, false);
    assert.doesNotMatch(JSON.stringify(config), new RegExp(SECRET_KEY));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("loadProvidersConfig degrades to defaults on invalid JSON", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-providers-"));
  try {
    const file = path.join(root, "providers.json");
    await writeFile(file, "{not json", "utf8");
    const config = loadProvidersConfig(file);
    assert.deepEqual(config.chain, ["afm"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// --- secret resolution ----------------------------------------------------------

test("resolveCloudApiKey reads apiKeyEnv", async () => {
  await withEnv({ MINNI_TEST_CLOUD_KEY: SECRET_KEY }, async () => {
    const result = resolveCloudApiKey({ enabled: true, apiKeyEnv: "MINNI_TEST_CLOUD_KEY" });
    assert.equal(result.key, SECRET_KEY);
    assert.equal(result.error, undefined);
  });
});

test("resolveCloudApiKey reports a key-free error when the env var is unset", async () => {
  await withEnv({ MINNI_TEST_CLOUD_KEY: undefined }, async () => {
    const result = resolveCloudApiKey({ enabled: true, apiKeyEnv: "MINNI_TEST_CLOUD_KEY" });
    assert.equal(result.key, undefined);
    assert.match(result.error ?? "", /cloud_key_unavailable/);
  });
});

test("resolveCloudApiKey accepts a 0600 apiKeyFile under the secrets dir", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-secrets-"));
  try {
    const secrets = path.join(root, "secrets");
    await mkdir(secrets, { recursive: true });
    const file = path.join(secrets, "cloud.key");
    await writeFile(file, `${SECRET_KEY}\n`, "utf8");
    await chmod(file, 0o600);
    const result = resolveCloudApiKey({ enabled: true, apiKeyFile: file }, secrets);
    assert.equal(result.key, SECRET_KEY);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveCloudApiKey denies group/other-readable key files", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-secrets-"));
  try {
    const secrets = path.join(root, "secrets");
    await mkdir(secrets, { recursive: true });
    const file = path.join(secrets, "cloud.key");
    await writeFile(file, SECRET_KEY, "utf8");
    await chmod(file, 0o644);
    const result = resolveCloudApiKey({ enabled: true, apiKeyFile: file }, secrets);
    assert.equal(result.key, undefined);
    assert.match(result.error ?? "", /cloud_key_denied.*0600/);
    assert.doesNotMatch(result.error ?? "", new RegExp(SECRET_KEY));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveCloudApiKey denies key files outside the secrets dir", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-secrets-"));
  try {
    const secrets = path.join(root, "secrets");
    await mkdir(secrets, { recursive: true });
    const outside = path.join(root, "cloud.key");
    await writeFile(outside, SECRET_KEY, "utf8");
    await chmod(outside, 0o600);
    const result = resolveCloudApiKey({ enabled: true, apiKeyFile: outside }, secrets);
    assert.equal(result.key, undefined);
    assert.match(result.error ?? "", /cloud_key_denied.*secrets/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveCloudApiKey requires apiKeyEnv or apiKeyFile when enabled", () => {
  const result = resolveCloudApiKey({ enabled: true });
  assert.match(result.error ?? "", /cloud_key_unavailable/);
  assert.deepEqual(resolveCloudApiKey({ enabled: false, apiKeyEnv: "X" }), {});
});

// --- G13 model target allowlist --------------------------------------------------

test("checkModelTarget: loopback always allowed, others denied by default", async () => {
  await withEnv({ MINNI_AFM_ALLOWED_TARGETS: undefined, MINNI_MODEL_ALLOWED_TARGETS: undefined }, async () => {
    assert.deepEqual(checkModelTarget("http://127.0.0.1:11437/v1/chat/completions"), { allowed: true });
    assert.deepEqual(checkModelTarget("http://localhost:11434/api"), { allowed: true });
    assert.deepEqual(checkModelTarget("https://api.example.com/v1"), { allowed: false, reason: "not_allowlisted" });
  });
});

test("checkModelTarget honors the MINNI_MODEL_ALLOWED_TARGETS alias and requires https", async () => {
  await withEnv(
    { MINNI_AFM_ALLOWED_TARGETS: undefined, MINNI_MODEL_ALLOWED_TARGETS: "api.example.com" },
    async () => {
      assert.deepEqual(checkModelTarget("http://api.example.com/v1"), { allowed: false, reason: "https_required" });
      assert.deepEqual(checkModelTarget("https://api.example.com/v1"), { allowed: true });
      assert.equal(modelAllowedTargets().includes("api.example.com"), true);
    },
  );
});

test("GATE: non-allowlisted cloud host gets a structured denial from callAfmJson", async () => {
  await withEnv({ MINNI_AFM_ALLOWED_TARGETS: undefined, MINNI_MODEL_ALLOWED_TARGETS: undefined }, async () => {
    const result = await callAfmJson("https://api.openai.com/v1/chat/completions", { messages: [] }, { mode: "bridge" });
    assert.equal(result.ok, false);
    assert.match(result.error ?? "", /^afm_target_denied:/);
    assert.doesNotMatch(result.error ?? "", /api\.openai\.com/, "denial must not echo the target URL");
  });
});

test("callAfmJson requires https for allowlisted non-loopback targets", async () => {
  await withEnv({ MINNI_MODEL_ALLOWED_TARGETS: "api.example.com" }, async () => {
    const result = await callAfmJson("http://api.example.com/v1/chat/completions", { messages: [] }, { mode: "bridge" });
    assert.equal(result.ok, false);
    assert.match(result.error ?? "", /afm_target_denied: non-loopback model targets require https/);
  });
});

// --- negative path: the key never leaks into status/audit/error output ------------

test("GATE: cloud key string never appears in status, sanitized health, or denial errors", async () => {
  resetAfmGenerationProbeCache();
  const root = await mkdtemp(path.join(tmpdir(), "minni-keyleak-"));
  try {
    await withEnv({ MINNI_TEST_CLOUD_KEY: SECRET_KEY }, async () => {
      // 1. Structured denial for a cloud host carrying auth material.
      const denial = await callAfmJson(
        "https://api.openai.com/v1/chat/completions",
        { messages: [], metadata: { authorization: `Bearer ${SECRET_KEY}` } },
        { mode: "bridge" },
      );
      assert.equal(denial.ok, false);
      assert.doesNotMatch(JSON.stringify(denial), new RegExp(SECRET_KEY));

      // 2. sanitizeAfmHealth strips auth headers and bearer tokens from errors.
      const sanitized = sanitizeAfmHealth({
        ok: false,
        data: { status: "error" },
        error: `HTTP 401 authorization: Bearer ${SECRET_KEY} api_key=${SECRET_KEY}`,
      });
      const sanitizedBody = JSON.stringify(sanitized);
      assert.doesNotMatch(sanitizedBody, new RegExp(SECRET_KEY));
      assert.match(sanitizedBody, /\[redacted\]/);

      // 3. Full status report (audited surface) with a failing generation probe
      //    whose transport error embeds the key.
      const report = await buildStatusReport({
        vaultPath: root,
        socket: { ok: true, data: { status: "ok" } },
        afm: { ok: true, data: { status: "ok" } },
        afmGenerationTransport: async () => ({
          ok: false,
          error: `HTTP 401 from upstream; authorization: Bearer ${SECRET_KEY}`,
        }),
      });
      const reportBody = JSON.stringify(report);
      assert.equal(report.afm.ok, false);
      assert.doesNotMatch(reportBody, new RegExp(SECRET_KEY), "status/audit output must never contain the key");
    });
  } finally {
    resetAfmGenerationProbeCache();
    await rm(root, { recursive: true, force: true });
  }
});

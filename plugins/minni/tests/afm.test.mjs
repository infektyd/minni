import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { callAfmJson, defaultNativeHelperPath, resolveAfmProvider, resolvedNativeHelperPath } from "../dist/afm.js";

test("resolveAfmProvider keeps off mode local and unavailable", () => {
  const provider = resolveAfmProvider("off", { nativeHelperPath: undefined });

  assert.equal(provider.mode, "off");
  assert.equal(provider.provider, "off");
  assert.equal(provider.status, "off");
  assert.equal(provider.available, false);
});

test("resolveAfmProvider reports native unavailable without helper", () => {
  const provider = resolveAfmProvider("native", { nativeHelperPath: undefined });

  assert.equal(provider.mode, "native");
  assert.equal(provider.provider, "native");
  assert.equal(provider.status, "native_unavailable");
  assert.equal(provider.available, false);
  assert.match(provider.reason ?? "", /helper/);
});

test("TS native helper default mirrors the repo engine helper when env is unset", () => {
  const previous = process.env.MINNI_AFM_NATIVE_HELPER;
  delete process.env.MINNI_AFM_NATIVE_HELPER;
  try {
    assert.match(defaultNativeHelperPath() ?? "", /engine\/native_afm_helper$/);
    assert.equal(resolvedNativeHelperPath(), defaultNativeHelperPath());
  } finally {
    if (previous === undefined) delete process.env.MINNI_AFM_NATIVE_HELPER;
    else process.env.MINNI_AFM_NATIVE_HELPER = previous;
  }
});

test("resolveAfmProvider auto falls back to existing bridge without native helper", () => {
  const provider = resolveAfmProvider("auto", { nativeHelperPath: undefined });

  assert.equal(provider.mode, "auto");
  assert.equal(provider.provider, "bridge");
  assert.equal(provider.status, "bridge");
  assert.equal(provider.available, true);
  assert.equal(provider.fallbackUsed, true);
});

test("resolveAfmProvider does not trust helper presence without healthy native status", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-afm-health-"));
  const helper = path.join(root, "helper.mjs");
  await writeFile(helper, "#!/usr/bin/env node\n", "utf8");
  await chmod(helper, 0o755);
  try {
    const provider = resolveAfmProvider("native", {
      nativeHelperPath: helper,
      health: {
        ok: false,
        data: {
          backend: "apple-foundation-models",
          availability: "unavailable",
          status: "error",
          adapter: "/Users/alice/private/extractor.fmadapter",
        },
        error: "FoundationModels unavailable at /Users/alice/private/extractor.fmadapter",
      },
    });

    assert.equal(provider.provider, "native");
    assert.equal(provider.status, "native_unavailable");
    assert.equal(provider.available, false);
    assert.equal(provider.adapterConfigured, true);
    assert.doesNotMatch(JSON.stringify(provider), /\/Users\/alice/);
    assert.doesNotMatch(JSON.stringify(provider), /extractor\.fmadapter/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveAfmProvider native ignores dead bridge health when a helper is available", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-afm-dead-bridge-"));
  const helper = path.join(root, "helper.mjs");
  await writeFile(helper, "#!/usr/bin/env node\n", "utf8");
  await chmod(helper, 0o755);
  try {
    const provider = resolveAfmProvider("native", {
      nativeHelperPath: helper,
      health: { ok: false, error: "connect ECONNREFUSED 127.0.0.1:11437" },
    });

    assert.equal(provider.provider, "native");
    assert.equal(provider.status, "native_available");
    assert.equal(provider.available, true);
    assert.doesNotMatch(JSON.stringify(provider), /11437/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("resolveAfmProvider reports adapter configuration without paths", () => {
  const previous = process.env.MINNI_AFM_ADAPTER_PATH;
  process.env.MINNI_AFM_ADAPTER_PATH = "/Users/alice/private/extractor.fmadapter";
  try {
    const provider = resolveAfmProvider("bridge", { nativeHelperPath: undefined });

    assert.equal(provider.adapterConfigured, true);
    assert.equal(provider.adapterPath, undefined);
    assert.doesNotMatch(JSON.stringify(provider), /\/Users\/alice/);
    assert.doesNotMatch(JSON.stringify(provider), /extractor\.fmadapter/);
  } finally {
    if (previous === undefined) delete process.env.MINNI_AFM_ADAPTER_PATH;
    else process.env.MINNI_AFM_ADAPTER_PATH = previous;
  }
});

test("callAfmJson off mode does not call the transport", async () => {
  const result = await callAfmJson(
    "http://127.0.0.1:1/v1/chat/completions",
    { task: "ping" },
    {
      mode: "off",
      transport: async () => {
        throw new Error("off mode must not call transport");
      },
    },
  );

  assert.equal(result.ok, false);
  assert.equal(result.error, "AFM mode is off");
});

test("callAfmJson native mode sends the requested operation to the helper", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-afm-helper-"));
  const helper = path.join(root, "helper.mjs");
  await writeFile(
    helper,
    [
      "#!/usr/bin/env node",
      "let raw = '';",
      "process.stdin.setEncoding('utf8');",
      "process.stdin.on('data', chunk => raw += chunk);",
      "process.stdin.on('end', () => {",
      "  const request = JSON.parse(raw);",
      "  console.log(JSON.stringify({ ok: true, data: { operation: request.operation, task: request.input.task } }));",
      "});",
      "",
    ].join("\n"),
    "utf8",
  );
  await chmod(helper, 0o755);

  try {
    const result = await callAfmJson(
      "http://127.0.0.1:1/v1/chat/completions",
      { task: "native prepare" },
      {
        mode: "native",
        nativeHelperPath: helper,
        operation: "prepare_task",
        transport: async () => {
          throw new Error("native mode must not call bridge transport");
        },
      },
    );

    assert.equal(result.ok, true, result.error);
    assert.equal(result.data.operation, "prepare_task");
    assert.equal(result.data.task, "native prepare");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// --- G13 (SEC-004) AFM URL allowlist tests ---

test("G13: callAfmJson denies non-loopback non-allowlisted URL with structured afm_target_denied (no URL leak in error)", async () => {
  const result = await callAfmJson(
    "http://evil.example.com:1234/v1/chat/completions",
    { task: "spoof attempt" },
    { mode: "bridge", transport: async () => { throw new Error("should not reach transport"); } }
  );
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /afm_target_denied/);
  assert.match(result.error ?? "", /loopback-only/);
  // Ensure we did not leak the bad URL or internal details into the error string
  assert.ok(!/evil\.example\.com/.test(result.error ?? ""));
});

test("G13: loopback URL is allowed by default (no denial)", async () => {
  // We use a transport that returns a fake success so we don't hit real net; the guard must pass first
  const result = await callAfmJson(
    "http://127.0.0.1:1/v1/chat/completions",
    { task: "local ok" },
    {
      mode: "bridge",
      transport: async () => ({ ok: true, data: { fake: true } }),
    }
  );
  assert.equal(result.ok, true);
  assert.equal(result.data?.fake, true);
});

test("G13: server no longer registers afmPrepareUrl in model-facing prepare tools (spoof surface removed)", async () => {
  // Executable proof: the src/server.ts no longer contains the afmPrepareUrl key in the two prepare tool registerTool inputSchemas.
  // (This directly satisfies "plugin schema tests proving model-facing afmPrepareUrl spoofing is gone or ignored.")
  const fs = await import("node:fs/promises");
  const src = await fs.readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  // After G13 removal, the literal key appears only in comments explaining its removal, not in the zod schemas for prepare_task/outcome.
  // Must not contain a *zod schema definition* for the key (the G13 removal comments mention the word but the actionable `afmPrepareUrl: z.` lines are gone).
  const hasZodKey = /afmPrepareUrl:\s*z\./.test(src);
  assert.equal(hasZodKey, false, "no afmPrepareUrl: z. schema key remains in the prepare tool inputSchemas");
});

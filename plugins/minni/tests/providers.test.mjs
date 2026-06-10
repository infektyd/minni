// P2 provider protocol: ModelProvider / ProviderChain semantics. The wire
// behavior of the default AFM-only chain is frozen separately by the P0
// goldens (afm-contract-golden.test.mjs); these tests cover the chain itself.

import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { AfmProvider, ProviderChain, defaultProviderChain } from "../dist/providers.js";

async function withNativeHelper(responseJson, run) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-providers-native-"));
  const captureFile = path.join(root, "capture.json");
  const helper = path.join(root, "helper.mjs");
  await writeFile(
    helper,
    [
      "#!/usr/bin/env node",
      'import { readFileSync, writeFileSync } from "node:fs";',
      'const input = readFileSync(0, "utf8");',
      `writeFileSync(${JSON.stringify(captureFile)}, input);`,
      `process.stdout.write(${JSON.stringify(JSON.stringify(responseJson))});`,
    ].join("\n"),
    "utf8",
  );
  await chmod(helper, 0o755);
  const previousHelper = process.env.MINNI_AFM_NATIVE_HELPER;
  process.env.MINNI_AFM_NATIVE_HELPER = helper;
  try {
    return await run(async () => JSON.parse(await readFile(captureFile, "utf8")));
  } finally {
    if (previousHelper === undefined) delete process.env.MINNI_AFM_NATIVE_HELPER;
    else process.env.MINNI_AFM_NATIVE_HELPER = previousHelper;
    await rm(root, { recursive: true, force: true });
  }
}

function fakeProvider({ name, tier = "local", result }) {
  const calls = [];
  return {
    name,
    tier,
    calls,
    supports: () => true,
    chat: async (request) => {
      calls.push(request);
      return { provider: name, ...(typeof result === "function" ? result() : result) };
    },
  };
}

test("defaultProviderChain is AFM-only until P4-P6 backends land", () => {
  const chain = defaultProviderChain();
  const providers = chain.providersFor("prepare");
  assert.equal(providers.length, 1);
  assert.equal(providers[0].name, "afm");
  assert.equal(providers[0].tier, "local");
});

// Mirror of engine/test_providers_config.py::test_default_provider_chain_respects_providers_json.
test("defaultProviderChain maps providers.json config and skips unimplemented chain entries", () => {
  const chain = defaultProviderChain({
    chain: ["afm", "mlx"],
    operations: { prepare: { localOnly: true } },
    providers: {},
  });
  // mlx is parsed but not implemented until P4 — only afm is instantiated.
  assert.deepEqual(chain.providers.map((p) => p.name), ["afm"]);
  assert.equal(chain.operations.prepare.localOnly, true);
  assert.equal(chain.operations.retrieval.localOnly, true);
  assert.deepEqual(chain.providersFor("prepare").map((p) => p.name), ["afm"]);
});

test("defaultProviderChain prepare localOnly policy excludes cloud-tier providers", () => {
  const chain = defaultProviderChain({
    chain: ["afm"],
    operations: { prepare: { localOnly: true } },
    providers: {},
  });
  const cloud = { name: "cloud-x", tier: "cloud", supports: () => true, chat: async () => ({ ok: true, provider: "cloud-x" }) };
  const policyChain = new ProviderChain([...chain.providers, cloud], chain.operations);
  assert.deepEqual(policyChain.providersFor("prepare").map((p) => p.name), ["afm"]);
});

test("defaultProviderChain falls back to AfmProvider when no configured backend is implemented", () => {
  const chain = defaultProviderChain({ chain: ["mlx"], operations: {}, providers: {} });
  assert.deepEqual(chain.providers.map((p) => p.name), ["afm"]);
  assert.ok(chain.providers[0] instanceof AfmProvider);
});

test("retrieval operations are local-only by default", () => {
  const cloud = fakeProvider({ name: "cloud-x", tier: "cloud", result: { ok: true } });
  const local = fakeProvider({ name: "afm", tier: "local", result: { ok: true } });
  const chain = new ProviderChain([cloud, local], { retrieval: { localOnly: true } });

  const eligible = chain.providersFor("retrieval");
  assert.deepEqual(eligible.map((p) => p.name), ["afm"]);
});

test("chain returns the first ok result and stops", async () => {
  const first = fakeProvider({ name: "one", result: { ok: true, data: { winner: true } } });
  const second = fakeProvider({ name: "two", result: { ok: true } });
  const chain = new ProviderChain([first, second]);

  const result = await chain.chat({ payload: {}, operation: "prepare" });
  assert.equal(result.ok, true);
  assert.equal(result.provider, "one");
  assert.equal(second.calls.length, 0);
});

test("chain falls through failed providers and returns the last failure", async () => {
  const first = fakeProvider({ name: "one", result: { ok: false, error: "boom-one" } });
  const second = fakeProvider({ name: "two", result: { ok: false, error: "boom-two" } });
  const chain = new ProviderChain([first, second]);

  const result = await chain.chat({ payload: {}, operation: "prepare" });
  assert.equal(result.ok, false);
  assert.equal(result.provider, "two");
  assert.equal(result.error, "boom-two");
  assert.equal(first.calls.length, 1);
  assert.equal(second.calls.length, 1);
});

test("chain reports a structured error when no provider is eligible", async () => {
  const cloud = fakeProvider({ name: "cloud-x", tier: "cloud", result: { ok: true } });
  const chain = new ProviderChain([cloud], { retrieval: { localOnly: true } });

  const result = await chain.chat({ payload: {}, operation: "retrieval" });
  assert.equal(result.ok, false);
  assert.equal(result.provider, "none");
  assert.match(result.error ?? "", /no provider eligible/);
});

test("AfmProvider off mode never reaches a transport", async () => {
  const provider = new AfmProvider();
  const result = await provider.chat({
    payload: { messages: [] },
    operation: "prepare",
    mode: "off",
    transport: async () => {
      throw new Error("off mode must not call a transport");
    },
  });
  assert.equal(result.ok, false);
  assert.equal(result.provider, "afm");
  assert.match(result.error ?? "", /off/);
});

test("AfmProvider bridge mode delegates to the injected transport", async () => {
  const provider = new AfmProvider();
  const seen = [];
  const result = await provider.chat({
    payload: { model: "m", messages: [{ role: "user", content: "hi" }] },
    operation: "extraction",
    url: "http://127.0.0.1:11437/v1/chat/completions",
    mode: "bridge",
    transport: async (url, payload) => {
      seen.push({ url, payload });
      return { ok: true, data: { choices: [] } };
    },
  });
  assert.equal(result.ok, true);
  assert.equal(result.provider, "afm");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, "http://127.0.0.1:11437/v1/chat/completions");
  assert.deepEqual(seen[0].payload, { model: "m", messages: [{ role: "user", content: "hi" }] });
});

// Mirror contract with engine/model_provider.py AfmProvider: native operation
// defaults to chat_completion, the bare chat body is wrapped as {payload: ...}
// for that default, and auto resolves the mode BEFORE choosing the payload.

test("AfmProvider native mode defaults the operation to chat_completion and wraps the chat body", async () => {
  await withNativeHelper({ ok: true, data: { choices: [] } }, async (readCapture) => {
    const payload = { model: "m", messages: [{ role: "user", content: "hi" }] };
    const result = await new AfmProvider().chat({ payload, operation: "prepare", mode: "native" });
    assert.equal(result.ok, true);
    assert.equal(result.provider, "afm");
    const envelope = await readCapture();
    assert.deepEqual(envelope, {
      schema_version: 1,
      operation: "chat_completion",
      input: { payload },
    });
  });
});

test("AfmProvider auto mode sends the native envelope when the helper answers", async () => {
  await withNativeHelper({ ok: true, data: { choices: [] } }, async (readCapture) => {
    const payload = { model: "m", messages: [{ role: "user", content: "hi" }] };
    const result = await new AfmProvider().chat({
      payload,
      operation: "prepare",
      mode: "auto",
      transport: async () => {
        throw new Error("auto mode with a healthy helper must not hit the bridge");
      },
    });
    assert.equal(result.ok, true);
    const envelope = await readCapture();
    assert.deepEqual(envelope, {
      schema_version: 1,
      operation: "chat_completion",
      input: { payload },
    });
  });
});

test("AfmProvider auto mode falls back to the bridge when the native helper fails", async () => {
  await withNativeHelper({ ok: false, error: "model unavailable" }, async () => {
    const payload = { model: "m", messages: [{ role: "user", content: "hi" }] };
    const seen = [];
    const result = await new AfmProvider().chat({
      payload,
      operation: "prepare",
      url: "http://127.0.0.1:11437/v1/chat/completions",
      mode: "auto",
      transport: async (url, body) => {
        seen.push({ url, body });
        return { ok: true, data: { choices: [] } };
      },
    });
    assert.equal(result.ok, true);
    assert.equal(seen.length, 1);
    // The bridge fallback sends the bare chat body, never the native envelope.
    assert.deepEqual(seen[0].body, payload);
  });
});

test("AfmProvider explicit nativeOperation sends nativePayload verbatim", async () => {
  await withNativeHelper({ ok: true, data: { answer: "ok" } }, async (readCapture) => {
    const result = await new AfmProvider().chat({
      payload: { model: "m", messages: [] },
      operation: "prepare",
      mode: "native",
      nativeOperation: "hyde_generation",
      nativePayload: { query: "cold query" },
    });
    assert.equal(result.ok, true);
    const envelope = await readCapture();
    assert.deepEqual(envelope, {
      schema_version: 1,
      operation: "hyde_generation",
      input: { query: "cold query" },
    });
  });
});

test("AfmProvider keeps the G13 loopback denial for non-allowlisted bridge hosts", async () => {
  const provider = new AfmProvider();
  const result = await provider.chat({
    payload: { messages: [] },
    operation: "prepare",
    url: "http://evil.example.com/v1/chat/completions",
    mode: "bridge",
  });
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /afm_target_denied/);
});

test("AfmProvider unset mode consults MINNI_AFM_PROVIDER_MODE (mirror of resolve_afm_mode)", async () => {
  await withNativeHelper({ ok: true, data: { answer: "native answer" } }, async (readCapture) => {
    const previous = process.env.MINNI_AFM_PROVIDER_MODE;
    process.env.MINNI_AFM_PROVIDER_MODE = "native";
    try {
      const payload = { model: "m", messages: [{ role: "user", content: "hi" }] };
      const result = await new AfmProvider().chat({
        payload,
        operation: "prepare",
        transport: async () => {
          throw new Error("env-resolved native mode must not call the bridge");
        },
      });
      assert.equal(result.ok, true);
      const envelope = await readCapture();
      assert.deepEqual(envelope, {
        schema_version: 1,
        operation: "chat_completion",
        input: { payload },
      });
    } finally {
      if (previous === undefined) delete process.env.MINNI_AFM_PROVIDER_MODE;
      else process.env.MINNI_AFM_PROVIDER_MODE = previous;
    }
  });
});

test("ProviderChain sanitizes provider errors structurally (no key in chain output)", async () => {
  const secret = "sk-test-vErYsEcReT-cLoUdKeY-12345";
  const leaky = fakeProvider({
    name: "leaky",
    result: { ok: false, error: `HTTP 401 authorization: Bearer ${secret} x-api-key=${secret}` },
  });
  const chain = new ProviderChain([leaky]);
  const result = await chain.chat({ payload: { messages: [] }, operation: "prepare" });
  assert.equal(result.ok, false);
  assert.doesNotMatch(result.error ?? "", new RegExp(secret));
  assert.match(result.error ?? "", /\[redacted/);
});

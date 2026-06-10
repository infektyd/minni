// P2 provider protocol: ModelProvider / ProviderChain semantics. The wire
// behavior of the default AFM-only chain is frozen separately by the P0
// goldens (afm-contract-golden.test.mjs); these tests cover the chain itself.

import assert from "node:assert/strict";
import test from "node:test";

import { AfmProvider, ProviderChain, defaultProviderChain } from "../dist/providers.js";

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

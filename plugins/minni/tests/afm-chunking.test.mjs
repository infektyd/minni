// Unit tests for src/afm-chunking.ts — the TypeScript mirror of
// engine/afm_chunking.py. Same shared-primitive responsibility: is this
// payload too big for the ~4096-token AFM context window, and if so, how do
// we split it (list-shaped only here — relevantSources in task.ts is
// already a list, not raw text, so no text splitter is needed on this side).

import assert from "node:assert/strict";
import test from "node:test";

import {
  AFM_INPUT_BUDGET_TOKENS,
  MIN_CHUNK_TOKENS,
  callNativeOpChunked,
  estimateNativePayloadTokens,
  reduceViaSameOp,
  splitListByTokenBudget,
} from "../dist/afm-chunking.js";

test("estimateNativePayloadTokens grows with payload size", () => {
  const small = estimateNativePayloadTokens({ text: "hello" });
  const large = estimateNativePayloadTokens({ text: "hello ".repeat(500) });
  assert.ok(small > 0);
  assert.ok(large > small);
});

test("splitListByTokenBudget returns one group when small", () => {
  const items = [{ title: "a" }, { title: "b" }];
  const groups = splitListByTokenBudget(items, 1000);
  assert.deepEqual(groups, [items]);
});

test("splitListByTokenBudget splits when over budget, preserving every item once", () => {
  const items = Array.from({ length: 20 }, (_, i) => ({ title: `item-${i}`, body: "x".repeat(200) }));
  const groups = splitListByTokenBudget(items, 100);
  assert.ok(groups.length > 1);
  const flattened = groups.flat();
  assert.deepEqual(flattened, items);
});

test("splitListByTokenBudget never returns an empty group list", () => {
  const groups = splitListByTokenBudget([], 100);
  assert.deepEqual(groups, [[]]);
});

test("callNativeOpChunked passes through unchanged when under budget", async () => {
  const calls = [];
  const callOp = async (payload) => {
    calls.push(payload);
    return { ok: true, data: { brief: "ok" } };
  };
  const { results, wasChunked } = await callNativeOpChunked(
    callOp, { task: "x", relevantSources: [{ a: 1 }] }, "relevantSources",
  );
  assert.equal(wasChunked, false);
  assert.equal(results.length, 1);
  assert.equal(calls.length, 1);
});

test("callNativeOpChunked splits relevantSources when over budget", async () => {
  const bigSources = Array.from({ length: 50 }, (_, i) => ({
    relativePath: `note-${i}.md`,
    evidenceEnvelope: "x".repeat(300),
  }));
  const calls = [];
  const callOp = async (payload) => {
    calls.push(payload);
    return { ok: true, data: { brief: "partial" } };
  };
  const { results, wasChunked } = await callNativeOpChunked(
    callOp, { task: "x", relevantSources: bigSources }, "relevantSources",
  );
  assert.equal(wasChunked, true);
  assert.ok(results.length > 1);
  assert.equal(calls.length, results.length);
  for (const call of calls) {
    assert.ok(call.relevantSources.length < bigSources.length);
  }
});

test("callNativeOpChunked reactive fallback chunks after a surprise context_overflow", async () => {
  let callCount = 0;
  const bigSources = Array.from({ length: 50 }, (_, i) => ({ relativePath: `note-${i}.md` }));
  const callOp = async () => {
    callCount += 1;
    if (callCount === 1) return { ok: false, data: { error_kind: "context_overflow" } };
    return { ok: true, data: { brief: "recovered" } };
  };
  const { results, wasChunked } = await callNativeOpChunked(
    callOp, { task: "x", relevantSources: bigSources }, "relevantSources", 999999,
  );
  assert.equal(wasChunked, true);
  assert.ok(results.some((r) => r.ok));
});

test("reduceViaSameOp returns undefined when nothing succeeded", async () => {
  const callOp = async () => ({ ok: false });
  const reduced = await reduceViaSameOp(
    callOp, [{ ok: false }, { ok: false }], () => ({ relevantSources: [] }), "relevantSources",
  );
  assert.equal(reduced, undefined);
});

test("reduceViaSameOp returns the sole result unreduced", async () => {
  let called = false;
  const callOp = async () => {
    called = true;
    return { ok: true, data: { brief: "should not be called" } };
  };
  const only = { ok: true, data: { brief: "the one answer" } };
  const reduced = await reduceViaSameOp(callOp, [only], () => ({ relevantSources: [] }), "relevantSources");
  assert.equal(reduced, only);
  assert.equal(called, false);
});

test("reduceViaSameOp synthesizes a final result from partials", async () => {
  const callOp = async (payload) => {
    assert.ok(payload.partialBriefs.length === 2);
    return { ok: true, data: { brief: "synthesized" } };
  };
  const chunkResults = [
    { ok: true, data: { brief: "partial 1" } },
    { ok: true, data: { brief: "partial 2" } },
  ];
  const reduced = await reduceViaSameOp(
    callOp, chunkResults,
    (partials) => ({ partialBriefs: partials.map((p) => p.brief) }),
    "relevantSources",
  );
  assert.equal(reduced.data.brief, "synthesized");
});

test("AFM_INPUT_BUDGET_TOKENS and MIN_CHUNK_TOKENS are exported with expected defaults", () => {
  assert.equal(AFM_INPUT_BUDGET_TOKENS, 3200);
  assert.equal(MIN_CHUNK_TOKENS, 200);
});

test("resolveAfmInputBudgetTokens honors MINNI_AFM_INPUT_BUDGET_TOKENS", async () => {
  const { resolveAfmInputBudgetTokens } = await import("../dist/afm-chunking.js");
  const prev = process.env.MINNI_AFM_INPUT_BUDGET_TOKENS;
  try {
    delete process.env.MINNI_AFM_INPUT_BUDGET_TOKENS;
    assert.equal(resolveAfmInputBudgetTokens(), AFM_INPUT_BUDGET_TOKENS);
    process.env.MINNI_AFM_INPUT_BUDGET_TOKENS = "1234";
    assert.equal(resolveAfmInputBudgetTokens(), 1234);
    process.env.MINNI_AFM_INPUT_BUDGET_TOKENS = "garbage";
    assert.equal(resolveAfmInputBudgetTokens(), AFM_INPUT_BUDGET_TOKENS);
  } finally {
    if (prev === undefined) delete process.env.MINNI_AFM_INPUT_BUDGET_TOKENS;
    else process.env.MINNI_AFM_INPUT_BUDGET_TOKENS = prev;
  }
});

test("callNativeOpChunked chunk trigger respects env budget override", async () => {
  const prev = process.env.MINNI_AFM_INPUT_BUDGET_TOKENS;
  try {
    // Payload ~500 tokens: under the 3200 default, over a 100 override.
    const payload = { relevantSources: Array.from({ length: 10 }, (_, i) => ({ id: i, body: "x".repeat(200) })) };
    const callsDefault = [];
    delete process.env.MINNI_AFM_INPUT_BUDGET_TOKENS;
    const resDefault = await callNativeOpChunked(async (p) => {
      callsDefault.push(p);
      return { ok: true, data: {} };
    }, payload, "relevantSources");
    assert.equal(resDefault.wasChunked, false);

    process.env.MINNI_AFM_INPUT_BUDGET_TOKENS = "100";
    const callsLow = [];
    const resLow = await callNativeOpChunked(async (p) => {
      callsLow.push(p);
      return { ok: true, data: {} };
    }, payload, "relevantSources");
    assert.equal(resLow.wasChunked, true);
    assert.ok(callsLow.length > 1);
  } finally {
    if (prev === undefined) delete process.env.MINNI_AFM_INPUT_BUDGET_TOKENS;
    else process.env.MINNI_AFM_INPUT_BUDGET_TOKENS = prev;
  }
});

test("callNativeOpChunked tags failures it cannot recover by splitting", async () => {
  // Single huge item: over budget, but nothing to split.
  const payload = { relevantSources: [{ body: "x".repeat(20000) }] };
  const res = await callNativeOpChunked(
    async () => ({ ok: false, data: { error_kind: "context_overflow" }, error: "context window exceeded" }),
    payload,
    "relevantSources",
  );
  assert.equal(res.wasChunked, false);
  assert.equal(res.results.length, 1);
  assert.equal(res.results[0].ok, false);
  assert.match(res.results[0].error, /chunking cannot help/);
  assert.match(res.results[0].error, /context window exceeded/);
});

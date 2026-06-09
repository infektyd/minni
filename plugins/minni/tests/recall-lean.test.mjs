import assert from "node:assert/strict";
import test from "node:test";

import { formatRecallLean } from "../dist/sovereign.js";

const sampleResponse = {
  backend: "faiss-disk",
  agent_id: "claudecode",
  layer: "mixed",
  results: [
    {
      wikilink: "[[GROK-BUILD_HOSTED_AGENT_ENVELOPE]]",
      filename: "GROK-BUILD_HOSTED_AGENT_ENVELOPE.md",
      layer: "identity",
      score: 2.64,
      headline: "boot identity envelope",
      provenance: { semantic_rank: 46, fts_rank: 1, rrf_score: 0.0118, cross_encoder_score: 2.6, decay_factor: 1 },
      query_variants: ["boot identity for workspace"],
      trace_id: "tabc",
      source: "/Users/x/.minni/identities/grok-build/ENV.md",
    },
    {
      wikilink: "[[wiki/sessions/20260608-aetherkernel-v63]]",
      layer: "episodic",
      score: 33.1234,
      snippet: "  v63   xHCI  MMIO   0xDEADDEAD is device-side, not the window.  ",
      provenance: { semantic_rank: 1, rrf_score: 0.5, cross_encoder_score: 9.1 },
      query_variants: ["v63 mmio"],
      trace_id: "tdef",
    },
  ],
};

test("formatRecallLean drops identity-layer shelf hits", () => {
  const out = formatRecallLean("v63 mmio", sampleResponse, []);
  assert.ok(!out.includes("GROK-BUILD_HOSTED_AGENT_ENVELOPE"), "identity-layer hit must be omitted");
  assert.ok(out.includes("aetherkernel-v63"), "non-identity hit must be kept");
  assert.ok(out.includes("identity-shelf"), "should note that shelf hits were omitted");
});

test("formatRecallLean strips verbose provenance and keeps wikilink + score + headline", () => {
  const out = formatRecallLean("v63 mmio", sampleResponse, []);
  assert.ok(!out.includes("cross_encoder_score"), "provenance must be stripped");
  assert.ok(!out.includes("query_variants"), "query_variants must be stripped");
  assert.ok(!out.includes("trace_id"), "trace_id must be stripped");
  assert.ok(out.includes("33.12"), "score should be rounded to 2dp and kept");
  // snippet whitespace collapsed into the headline
  assert.ok(out.includes("v63 xHCI MMIO 0xDEADDEAD is device-side"), "headline/snippet kept and whitespace-collapsed");
});

test("formatRecallLean is dramatically smaller than the raw results blob", () => {
  const lean = formatRecallLean("v63 mmio", sampleResponse, []);
  const raw = JSON.stringify(sampleResponse.results, null, 2);
  assert.ok(lean.length < raw.length, "lean output must be smaller than the raw results JSON");
});

test("formatRecallLean handles empty / non-array results without throwing", () => {
  assert.doesNotThrow(() => formatRecallLean("q", { results: undefined }, []));
  assert.doesNotThrow(() => formatRecallLean("q", { results: "No recall results." }, []));
  const out = formatRecallLean("q", { results: [] }, []);
  assert.ok(out.includes("No non-identity daemon recall results."));
});

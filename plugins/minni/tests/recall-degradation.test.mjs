// F1 (#226 R4/R5): the daemon has written a per-corpus `degradation` report on
// every search response since R8, and NOTHING on this side read it — the
// RecallResponse type did not declare it and no renderer emitted it. A
// lexical-only answer, a dead personal vault, or a failed HyDE leg therefore
// reached the agent formatted exactly like a healthy hybrid recall.
//
// These tests are the ones the audit says would have caught that: they stub a
// daemon response in the shape recall.py actually emits and assert the
// rendered text, so a renderer that drops the field fails here.
import assert from "node:assert/strict";
import test from "node:test";

import { formatDegradation, formatRecall, formatRecallLean } from "../dist/sovereign.js";

/** The shape recall.py's response_payload puts on the wire (recall.py). */
const degradedResponse = {
  backend: "faiss-disk",
  agent_id: "claudecode",
  count: 1,
  results: [{ wikilink: "[[wiki/a]]", layer: "knowledge", score: 1.5, snippet: "a note" }],
  degraded: true,
  degradation: [
    {
      src: "c",
      vector_model: "all-MiniLM-L6-v2",
      vector_degraded: true,
      degraded: true,
    },
    {
      src: "p",
      vector_model: "all-MiniLM-L6-v2",
      vector_degraded: false,
      degraded: true,
      personal_index_failed: "personal vault index failed: database is locked",
      reason: "personal vault index failed: database is locked",
    },
  ],
};

const healthyResponse = {
  backend: "faiss-disk",
  count: 1,
  results: [{ wikilink: "[[wiki/a]]", layer: "knowledge", score: 1.5, snippet: "a note" }],
  degraded: false,
  degradation: [
    { src: "c", vector_model: "all-MiniLM-L6-v2", vector_degraded: false, degraded: false },
  ],
};

test("formatRecall flags a degraded corpus to the agent", () => {
  const out = formatRecall("v63 mmio", degradedResponse, []);
  assert.match(out, /⚠ degraded: c: vector_degraded/, "the FTS-only leg must be named");
  assert.match(out, /⚠ degraded: p: personal_index_failed/, "the dead vault must be named");
  assert.match(out, /database is locked/, "the failure detail tells the agent whether to retry");
});

test("formatRecallLean flags a degraded corpus too (per-turn recall)", () => {
  const out = formatRecallLean("v63 mmio", degradedResponse, []);
  assert.match(out, /⚠ degraded: c: vector_degraded/);
  assert.match(out, /⚠ degraded: p: personal_index_failed/);
});

test("a healthy recall carries no degradation line", () => {
  // The daemon reports healthy corpora too; echoing "all fine" every turn is
  // pure context tax, and it would train the reader to skip the marker.
  assert.equal(formatDegradation(healthyResponse), undefined);
  assert.doesNotMatch(formatRecall("q", healthyResponse, []), /⚠/);
  assert.doesNotMatch(formatRecallLean("q", healthyResponse, []), /⚠/);
});

test("auth suppression is rendered, not just counted by isDaemonResultEmpty", () => {
  // Pre-fix this field existed on RecallResponse solely to keep
  // isDaemonResultEmpty honest — a corpus blacked out by the read gate looked
  // to the agent exactly like a corpus that had nothing.
  const out = formatRecall("q", {
    count: 0,
    results: [],
    auth_suppression: [{ src: "p", pre_gate: 4, suppressed: 4, reason: "suppressed by scope" }],
  }, []);
  assert.match(out, /⚠ auth-suppressed: p: 4 candidate\(s\) withheld/);
});

test("an unknown or malformed degradation entry cannot throw the renderer", () => {
  assert.doesNotThrow(() => formatRecall("q", { degradation: [null, {}, { degraded: true }] }, []));
  assert.equal(formatDegradation({}), undefined);
  assert.equal(formatDegradation({ degradation: "not-an-array" }), undefined);
  // A degraded entry with no recognized kind must still be reported, not
  // silently dropped because the daemon named the field something new.
  assert.match(formatDegradation({ degradation: [{ src: "c", degraded: true }] }), /unspecified/);
});

import assert from "node:assert/strict";
import test from "node:test";

import { buildBootRecallSlice } from "../dist/daemon_transport.js";

test("buildBootRecallSlice projects result fields, drops duplicate evidence_envelope/provenance/rationale, and keeps score-only results", () => {
  const response = {
    agent_id: "claudecode",
    count: 7,
    results: [
      {
        wikilink: "[[identity/self]]",
        title: "Self Identity",
        text: "<EVIDENCE>identity layer row</EVIDENCE>",
        layer: "identity",
        score: 3.0,
        confidence: 0.99,
      },
      {
        wikilink: "[[notes/one]]",
        title: "Note One",
        text: '<EVIDENCE src="shared" id="1">First note body</EVIDENCE>',
        snippet: "First note snippet",
        score: 1.42,
        // Deliberately no confidence field: must NOT be dropped by a 0.55 gate
        layer: "project",
        review_state: "approved",
        age_days: 3,
        doc_id: 1,
        chunk_id: 0,
        source: "/vault/notes/one.md",
        src: "shared",
        filename: "one.md",
        agent_origin: "claudecode",
        visibility: "global",
        rationale: "matched bm25",
        recommended_action: "cite",
        recommended_scope: "repo",
        attribution: ["claudecode"],
        attribution_score: 0.9,
        provenance: { agent_id: "claudecode" },
        evidence_envelope: { raw: "<EVIDENCE>dup</EVIDENCE>" },
      },
      {
        wikilink: "[[notes/two]]",
        title: "Note Two",
        snippet: "Fallback snippet when text is absent",
        confidence: 0.41,
        score: 1.1,
        layer: "knowledge",
        review_state: "candidate",
        age_days: 5,
        evidence_envelope: { raw: "dup2" },
        provenance: { agent_id: "codex" },
      },
      {
        wikilink: "[[notes/three]]",
        title: "Note Three",
        text: "<EVIDENCE>Third note body</EVIDENCE>",
        confidence: 0.77,
        score: 0.95,
        layer: "decisions",
        review_state: "approved",
        age_days: 10,
      },
      {
        wikilink: "[[notes/four-dropped]]",
        title: "Note Four (beyond cap of 3)",
        text: "<EVIDENCE>Fourth note body</EVIDENCE>",
        confidence: 0.7,
        score: 0.8,
        layer: "project",
        review_state: "approved",
        age_days: 12,
      },
      {
        wikilink: "[[corrections/five-kept]]",
        title: "Correction Five (kept via +2 correction allowance)",
        text: "<EVIDENCE>Never use git stash</EVIDENCE>",
        confidence: 0.95,
        score: 0.75,
        layer: "corrections",
        review_state: "approved",
        age_days: 1,
        correction: true,
      },
      {
        wikilink: "[[corrections/six-kept]]",
        title: "Correction Six (kept via +2 correction allowance)",
        text: "<EVIDENCE>Always check df -h / first</EVIDENCE>",
        score: 0.7,
        layer: "corrections",
        review_state: "correction",
        age_days: 2,
      },
      {
        wikilink: "[[corrections/seven-dropped]]",
        title: "Correction Seven (3rd extra correction beyond +2 cap)",
        text: "<EVIDENCE>Dropped extra correction</EVIDENCE>",
        score: 0.65,
        layer: "corrections",
        correction: true,
      },
    ],
    episodic: [
      { id: "e1", content: "ep 1" },
      { id: "e2", content: "ep 2" },
      { id: "e3", content: "ep 3 (dropped beyond cap of 2)" },
    ],
    episodic_count: 3,
  };

  const slice = buildBootRecallSlice(response, "claudecode");

  assert.equal(slice.ok, true);
  assert.equal(slice.agent_origin, "claudecode");
  // 3 non-identity + 2 extra corrections = 5
  assert.equal(slice.results.length, 5);
  assert.equal(slice.count, 5);
  assert.deepEqual(
    slice.results.map((r) => r.wikilink),
    [
      "[[notes/one]]",
      "[[notes/two]]",
      "[[notes/three]]",
      "[[corrections/five-kept]]",
      "[[corrections/six-kept]]",
    ],
  );

  // First item: score-only result (no confidence) survives and keeps <EVIDENCE> text
  const first = slice.results[0];
  assert.deepEqual(Object.keys(first).sort(), [
    "age_days",
    "layer",
    "review_state",
    "score",
    "text",
    "title",
    "wikilink",
  ]);
  assert.equal(first.text, '<EVIDENCE src="shared" id="1">First note body</EVIDENCE>');
  assert.equal(first.score, 1.42);
  assert.equal(first.confidence, undefined);
  assert.equal(first.evidence_envelope, undefined);
  assert.equal(first.provenance, undefined);
  assert.equal(first.rationale, undefined);
  assert.equal(first.recommended_action, undefined);
  assert.equal(first.recommended_scope, undefined);
  assert.equal(first.attribution, undefined);
  assert.equal(first.attribution_score, undefined);

  // Second item: low confidence (0.41) survives at boot (no 0.55 gate) and falls back to snippet for text
  const second = slice.results[1];
  assert.equal(second.confidence, 0.41);
  assert.equal(second.text, "Fallback snippet when text is absent");

  // Corrections carry correction: true
  assert.equal(slice.results[3].correction, true);
  assert.equal(slice.results[4].correction, true);

  // Episodic capped at 2
  assert.equal(slice.episodic.length, 2);
  assert.equal(slice.episodic_count, 2);
  assert.equal(slice.note, undefined);
});

test("buildBootRecallSlice and fitEnvelope Step 2 detect correction salience from provenance.salience_boost, category, and text, and cap all-correction results to 3 at floor", async () => {
  const { fitEnvelope } = await import("../dist/agent_envelope.js");
  const response = {
    agent_id: "claudecode",
    results: [
      {
        wikilink: "[[c/one]]",
        title: "First correction",
        text: "Correction: service X moved to port 9090",
        layer: "knowledge",
        score: 1.5,
      },
      {
        wikilink: "[[c/two]]",
        title: "Second correction",
        text: "Body 2",
        layer: "knowledge",
        category: "fix",
        score: 1.4,
      },
      {
        wikilink: "[[c/three]]",
        title: "Third correction",
        text: "Body 3",
        layer: "knowledge",
        provenance: { salience_boost: 0.25 },
        score: 1.3,
      },
      {
        wikilink: "[[c/four]]",
        title: "Fourth correction (extra #1)",
        text: "Body 4",
        layer: "knowledge",
        is_correction: true,
        score: 1.2,
      },
      {
        wikilink: "[[c/five]]",
        title: "Fifth correction (extra #2)",
        text: "Body 5",
        layer: "knowledge",
        type: "contradiction",
        score: 1.1,
      },
    ],
    episodic: [],
    episodic_count: 0,
  };

  const slice = buildBootRecallSlice(response, "claudecode");
  assert.equal(slice.results.length, 5);
  for (const r of slice.results) {
    assert.equal(r.correction, true, `expected correction:true on ${r.wikilink}`);
  }

  // When fitEnvelope trims recall (Step 2), floor keeps top 1 + up to 2 corrections = 3
  const fitted = fitEnvelope(
    {
      contract: "C",
      recall: slice,
    },
    { tokenLimit: 100, charLimit: 400 },
  );
  assert.equal(fitted.recall.results.length, 3);
  assert.deepEqual(
    fitted.recall.results.map((r) => r.wikilink),
    ["[[c/one]]", "[[c/two]]", "[[c/three]]"],
  );
});

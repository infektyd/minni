import assert from "node:assert/strict";
import test from "node:test";

import {
  buildRecallPointer,
  docHandleFor,
  RECALL_POINTER_MAX_CHARS,
  vaultHandleForPath,
} from "../dist/recall-state.js";

const estTokens = (s) => Math.ceil(s.length / 4);

test("buildRecallPointer: 3 gated hits with maximal 100-char titles and 90-150-char paths fit within 120 tokens with unclipped handles and no wikilinks", () => {
  assert.equal(RECALL_POINTER_MAX_CHARS, 480);

  const rel1 = `wiki/decisions/${"a".repeat(85)}-auth-token-rotation.md`; // > 100 chars
  const rel2 = `wiki/concepts/${"b".repeat(105)}-lease-fencing-quorum.md`; // > 120 chars
  const rel3 = `wiki/sessions/${"c".repeat(105)}-socket-retry-backoff.md`; // 143 chars (90..150)
  assert.ok(rel1.length >= 90 && rel1.length <= 150);
  assert.ok(rel2.length >= 90 && rel2.length <= 150);
  assert.ok(rel3.length >= 90 && rel3.length <= 150);

  const title1 = `Decision_${"X".repeat(91)}`; // 100 chars
  const title2 = `Concept_${"Y".repeat(92)}`; // 100 chars
  const title3 = `Session_${"Z".repeat(92)}`; // 100 chars
  assert.equal(title1.length, 100);
  assert.equal(title2.length, 100);
  assert.equal(title3.length, 100);

  const h1 = vaultHandleForPath(rel1);
  const h2 = vaultHandleForPath(rel2);
  const h3 = docHandleFor("episodic", 4412);

  const pointer = buildRecallPointer({
    topScore: 0.92,
    topHits: [
      {
        title: title1,
        wikilink: `[[${rel1.replace(/\.md$/, "")}]]`,
        relativePath: rel1,
        route: "vault",
        score: 0.92,
      },
      {
        title: title2,
        wikilink: `[[${rel2.replace(/\.md$/, "")}]]`,
        relativePath: rel2,
        route: "vault",
        score: 0.86,
      },
      {
        title: title3,
        wikilink: `[[${rel3.replace(/\.md$/, "")}]]`,
        scope: "episodic",
        docId: 4412,
        route: "daemon",
        score: 0.88,
      },
    ],
  });

  assert.equal(pointer.includes("\n"), false, "pointer must be a single line");
  assert.ok(
    estTokens(pointer) <= 120,
    `pointer must stay within 120 estimated tokens (got ${estTokens(pointer)} tokens, ${pointer.length} chars)`,
  );
  assert.ok(pointer.length <= RECALL_POINTER_MAX_CHARS);
  assert.ok(!pointer.includes("[["), "pointer must never print wikilinks");
  assert.ok(!pointer.includes("]]"), "pointer must never print wikilinks");

  // Unclipped handles and comma-separated multi-hit format.
  assert.match(pointer, /^📓 3 relevant memories: /);
  assert.ok(pointer.includes(`(${h3}) 0.88`), "daemon route leader handle and score must be intact");
  assert.ok(pointer.includes(`(${h1}) 0.92`), "vault route leader handle and score must be intact");
  assert.ok(pointer.includes(`(${h2}) 0.86`), "second vault hit handle and score must be intact");
  assert.ok(pointer.includes(`"${title1}"`), "100-char title1 must not be clipped below 100 chars");
  assert.ok(pointer.includes(`"${title2}"`), "100-char title2 must not be clipped below 100 chars");
  assert.ok(pointer.includes(`"${title3}"`), "100-char title3 must not be clipped below 100 chars");
  assert.match(pointer, /\. Consult minni_recall or minni_drill\.$/);
});

test("buildRecallPointer drops trailing hits rather than clipping handles when budget is constrained", () => {
  const hits = [
    {
      title: "First memory with a moderately long descriptive title for budget testing",
      wikilink: "[[wiki/decisions/first]]",
      relativePath: "wiki/decisions/first.md",
      route: "vault",
      score: 0.91,
    },
    {
      title: "Second memory with another descriptive title that fits inside two-hit budget",
      wikilink: "[[wiki/decisions/second]]",
      relativePath: "wiki/decisions/second.md",
      route: "vault",
      score: 0.87,
    },
    {
      title: "Third memory whose addition would push the pointer past a tight 70-token cap",
      wikilink: "[[wiki/decisions/third]]",
      relativePath: "wiki/decisions/third.md",
      route: "vault",
      score: 0.83,
    },
  ];

  const pointer = buildRecallPointer({ topScore: 0.91, topHits: hits }, { maxTokens: 70 });
  assert.ok(estTokens(pointer) <= 70, `expected <= 70 tokens, got ${estTokens(pointer)}`);
  assert.match(pointer, /^📓 2 relevant memories: /);
  assert.ok(pointer.includes(`(${vaultHandleForPath("wiki/decisions/first.md")}) 0.91`));
  assert.ok(pointer.includes(`(${vaultHandleForPath("wiki/decisions/second.md")}) 0.87`));
  assert.ok(!pointer.includes("Third memory"), "third hit must be dropped cleanly");
});

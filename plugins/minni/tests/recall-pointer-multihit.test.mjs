import assert from "node:assert/strict";
import test from "node:test";

import {
  buildRecallPointer,
  RECALL_POINTER_MAX_CHARS,
  selectPointerHits,
  vaultHandleForPath,
} from "../dist/recall-state.js";

test("buildRecallPointer formats 1, 2, and 3 hits on a single line <= 300 chars with handles and minni_drill guidance", () => {
  const h1 = vaultHandleForPath("wiki/decisions/alpha.md");
  const h2 = vaultHandleForPath("wiki/decisions/beta.md");
  const h3 = "d:personal:42";

  assert.match(h1, /^m:[0-9a-f]{8}$/);
  assert.match(h2, /^m:[0-9a-f]{8}$/);

  const single = buildRecallPointer({
    topScore: 0.91,
    topHits: [
      { title: "Alpha decision", wikilink: "[[wiki/decisions/alpha]]", score: 0.91, handle: h1 },
    ],
  });
  assert.ok(!single.includes("\n"), "pointer must be single-line");
  assert.ok(single.length <= RECALL_POINTER_MAX_CHARS);
  assert.match(single, /^📓 1 relevant memory:/);
  assert.ok(single.includes(`"Alpha decision" (${h1}) 0.91`), `got: ${single}`);
  assert.ok(single.includes(`minni_drill(${h1})`), `got: ${single}`);

  const two = buildRecallPointer({
    topScore: 0.91,
    topHits: [
      { title: "Alpha decision", wikilink: "[[wiki/decisions/alpha]]", score: 0.91, handle: h1 },
      { title: "Beta runbook", wikilink: "[[wiki/decisions/beta]]", score: 0.84, handle: h2 },
    ],
  });
  assert.ok(!two.includes("\n"));
  assert.ok(two.length <= RECALL_POINTER_MAX_CHARS);
  assert.match(two, /^📓 2 relevant memories:/);
  assert.ok(two.includes(`"Alpha decision" (${h1}) 0.91`), `got: ${two}`);
  assert.ok(two.includes(`"Beta runbook" (${h2}) 0.84`), `got: ${two}`);
  assert.ok(two.includes("minni_drill("), `got: ${two}`);

  const three = buildRecallPointer({
    topScore: 0.91,
    topHits: [
      { title: "Alpha decision", wikilink: "[[wiki/decisions/alpha]]", score: 0.91, handle: h1 },
      { title: "Beta runbook", wikilink: "[[wiki/decisions/beta]]", score: 0.84, handle: h2 },
      { title: "Gamma daemon doc", wikilink: "[[wiki/decisions/gamma]]", score: 0.79, handle: h3 },
    ],
  });
  assert.ok(!three.includes("\n"));
  assert.ok(three.length <= RECALL_POINTER_MAX_CHARS);
  assert.match(three, /^📓 3 relevant memories:/);
  assert.ok(three.includes(`"Alpha decision" (${h1}) 0.91`), `got: ${three}`);
  assert.ok(three.includes(`"Beta runbook" (${h2}) 0.84`), `got: ${three}`);
  assert.ok(three.includes(`"Gamma daemon doc" (${h3}) 0.79`), `got: ${three}`);
});

test("selectPointerHits excludes hits outside the 0.15 score band and caps at 3 hits", () => {
  const hits = [
    { title: "Hit 1", wikilink: "[[a]]", score: 0.92, handle: "m:11111111" },
    { title: "Hit 2", wikilink: "[[b]]", score: 0.83, handle: "m:22222222" },
    { title: "Hit 3", wikilink: "[[c]]", score: 0.79, handle: "m:33333333" },
    { title: "Hit 4 (in band but >3)", wikilink: "[[d]]", score: 0.78, handle: "m:44444444" },
    { title: "Hit 5 (out of band)", wikilink: "[[e]]", score: 0.70, handle: "m:55555555" },
  ];
  const selected = selectPointerHits(hits, { maxHits: 3, scoreBand: 0.15 });
  assert.equal(selected.length, 3);
  assert.deepEqual(
    selected.map((h) => h.title),
    ["Hit 1", "Hit 2", "Hit 3"],
  );

  const wideSpread = [
    { title: "Top quote", wikilink: "[[a]]", score: 0.95, handle: "m:11111111" },
    { title: "Far second", wikilink: "[[b]]", score: 0.72, handle: "m:22222222" },
  ];
  const singleSelected = selectPointerHits(wideSpread, { maxHits: 3, scoreBand: 0.15 });
  assert.equal(singleSelected.length, 1);
  assert.equal(singleSelected[0].title, "Top quote");
});

test("buildRecallPointer enforces RECALL_POINTER_MAX_CHARS = 300 even with three 100-char titles", () => {
  const longA = "A".repeat(100);
  const longB = "B".repeat(100);
  const longC = "C".repeat(100);
  const pointer = buildRecallPointer({
    topScore: 0.9,
    topHits: [
      { title: longA, wikilink: "[[wiki/a]]", score: 0.9, handle: "m:aaaaaaaa" },
      { title: longB, wikilink: "[[wiki/b]]", score: 0.85, handle: "m:bbbbbbbb" },
      { title: longC, wikilink: "[[wiki/c]]", score: 0.82, handle: "m:cccccccc" },
    ],
  });
  assert.ok(!pointer.includes("\n"), "pointer must remain single-line");
  assert.ok(
    pointer.length <= RECALL_POINTER_MAX_CHARS,
    `expected pointer length <= ${RECALL_POINTER_MAX_CHARS}, got ${pointer.length}: ${pointer}`,
  );
  assert.match(pointer, /minni_drill\(/);
});

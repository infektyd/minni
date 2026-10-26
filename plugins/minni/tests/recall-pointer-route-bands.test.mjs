import assert from "node:assert/strict";
import test from "node:test";

import { buildRecallPointer, selectPointerHits } from "../dist/recall-state.js";

test("Route (a) hit (1.00) + Route (c) hit (0.60): 0.15 band excludes the Route (c) hit", () => {
  const hits = [
    {
      title: "Exact quote protocol",
      wikilink: "[[wiki/decisions/quote]]",
      score: 1.0,
      gateRoute: "a",
      handle: "m:aaaa1111",
    },
    {
      title: "Dense body mention",
      wikilink: "[[wiki/concepts/body]]",
      score: 0.6,
      gateRoute: "c",
      handle: "m:cccc2222",
    },
  ];

  const selected = selectPointerHits(hits);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].title, "Exact quote protocol");

  const pointer = buildRecallPointer({ topScore: 1.0, topHits: hits });
  assert.ok(pointer.includes('"Exact quote protocol" (m:aaaa1111) 1.00'), `got: ${pointer}`);
  assert.ok(!pointer.includes("Dense body mention"), `Route (c) hit must not appear: ${pointer}`);
});

test("Two Route (b) hits (0.80 and 0.72) are within 0.15 and both appear in the pointer", () => {
  const hits = [
    {
      title: "Primary title match",
      wikilink: "[[wiki/decisions/primary]]",
      score: 0.8,
      gateRoute: "b",
      handle: "m:bbbb1111",
    },
    {
      title: "Secondary title match",
      wikilink: "[[wiki/decisions/secondary]]",
      score: 0.72,
      gateRoute: "b",
      handle: "m:bbbb2222",
    },
  ];

  const selected = selectPointerHits(hits);
  assert.equal(selected.length, 2);

  const pointer = buildRecallPointer({ topScore: 0.8, topHits: hits });
  assert.match(pointer, /^📓 2 relevant memories:/);
  assert.ok(pointer.includes('"Primary title match" (m:bbbb1111) 0.80'), `got: ${pointer}`);
  assert.ok(pointer.includes('"Secondary title match" (m:bbbb2222) 0.72'), `got: ${pointer}`);
});

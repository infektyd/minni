// Recall-to-use S1a (§4.3): what the model could actually see.
//
// Claude Code inlines an additionalContext string up to 10,000 chars. Past that
// cap the string is spilled and only a 2,000-char preview stays in context, so
// a memory printed at char 5,000 of an oversized boot envelope was never in
// front of the model, however well it ranked. `locateItems` runs on the exact
// emitted strings and says, per item, whether it was visible.
import assert from "node:assert/strict";
import test from "node:test";

import { HOST_PREVIEW_CHARS, locateItems } from "../dist/use-ledger.js";

const CAP = 10_000;

/** A string of exactly `length` chars with `needle` placed at `at`. */
function placed(length, needle, at) {
  const filler = "·".repeat(length);
  return filler.slice(0, at) + needle + filler.slice(at + needle.length, length);
}

test("an item wholly inside an emitted string within the cap is visible", () => {
  const emitted = placed(9_000, "ITEM-ALPHA", 8_500);
  const [loc] = locateItems(emitted, ["ITEM-ALPHA"], CAP);
  assert.equal(loc.visible, true);
  assert.equal(loc.cap_unknown, undefined);
  assert.equal(loc.start, 8_500);
  assert.equal(loc.end, 8_510);
});

test("a string exactly at the cap is still inline", () => {
  const emitted = placed(CAP, "ITEM-EDGE", CAP - 9);
  const [loc] = locateItems(emitted, ["ITEM-EDGE"], CAP);
  assert.equal(loc.visible, true);
});

test("over the cap, an item that ends before char 2,000 is visible", () => {
  assert.equal(HOST_PREVIEW_CHARS, 2_000);
  const emitted = placed(12_000, "ITEM-EARLY", 100);
  const atEdge = placed(12_000, "ITEM-EDGE", 2_000 - 9); // ends exactly at char 2,000
  assert.equal(locateItems(emitted, ["ITEM-EARLY"], CAP)[0].visible, true);
  assert.equal(locateItems(atEdge, ["ITEM-EDGE"], CAP)[0].visible, true);
});

test("over the cap, an item past or straddling char 2,000 is not visible", () => {
  const late = placed(12_000, "ITEM-LATE", 5_000);
  const straddle = placed(12_000, "ITEM-STRADDLE", 1_995);
  assert.equal(locateItems(late, ["ITEM-LATE"], CAP)[0].visible, false);
  assert.equal(locateItems(straddle, ["ITEM-STRADDLE"], CAP)[0].visible, false);
});

test("an item counts only where it lies wholly inside ONE emitted string", () => {
  // "ITEM-SPLIT" exists only in the concatenation of the first two strings.
  const strings = ["head ITEM-SP", "LIT tail", placed(12_000, "ITEM-SECOND", 3_000), "short ITEM-THIRD ok"];
  const locs = locateItems(strings, ["ITEM-SPLIT", "ITEM-SECOND", "ITEM-THIRD", { text: "ITEM-ABSENT" }], CAP);
  assert.equal(locs[0].visible, false, "an item split across strings is not visible");
  assert.equal(locs[1].visible, false, "an over-cap string past the preview hides it");
  assert.equal(locs[2].visible, true);
  assert.equal(locs[2].emitted_index, 3);
  assert.equal(locs[3].visible, false);
  assert.equal(locs[3].found, false);
});

test("the earliest qualifying occurrence wins in an over-cap string", () => {
  const emitted = placed(12_000, "ITEM-TWICE", 9_000).replace(/^·{10}/, "ITEM-TWICE");
  const [loc] = locateItems(emitted, ["ITEM-TWICE"], CAP);
  assert.equal(loc.visible, true);
  assert.equal(loc.start, 0);
});

test("an empty item is never located on a wire with a known cap", () => {
  const [loc] = locateItems("anything", [""], CAP);
  assert.equal(loc.visible, false);
});

test("no known cap: visible true, cap_unknown true", () => {
  for (const cap of [undefined, null, 0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
    const locs = locateItems(placed(50_000, "ITEM-X", 40_000), ["ITEM-X", "ITEM-NOT-THERE"], cap);
    for (const loc of locs) {
      assert.equal(loc.visible, true, `cap ${cap}`);
      assert.equal(loc.cap_unknown, true, `cap ${cap}`);
    }
  }
});

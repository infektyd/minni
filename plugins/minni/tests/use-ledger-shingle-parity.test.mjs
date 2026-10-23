// Recall-to-use S1a-2: `shingleHashes` now hashes only the shingles it keeps
// (SessionStart shingles every boot learning, so hashing all ~600 positions of
// a 4,000-char body was the boot's largest capture cost). The output must not
// change at all. This pins it against the original algorithm, kept verbatim
// below as the reference, on bodies built to hit every ordering rule:
// resolution sections, nested headings, lead lines, a shingle seen first
// outside a resolution section and again inside one, a DF map, and `max`.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { MAX_SHINGLES, shingleHashes } from "../dist/use-ledger.js";

// ---- reference: the algorithm as it shipped in S1a-1 ----------------------
const WORD_RE = /[\p{L}\p{N}]+/gu;
const RESOLUTION_HEADING_RE = /^(?:resolution|fix|decision|root cause)\b/i;
const RESOLUTION_LEAD_RE = /^(?:[-*]\s+)?(?:resolution|fix|decision|root cause)\s*:/i;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const wordsOf = (text) => text.normalize("NFKC").toLowerCase().match(WORD_RE) ?? [];
const hashOf = (words) => createHash("sha256").update(words.join(" "), "utf8").digest("hex").slice(0, 16);
function clampMax(max) {
  if (typeof max !== "number" || !Number.isFinite(max)) return MAX_SHINGLES;
  return Math.max(0, Math.min(MAX_SHINGLES, Math.floor(max)));
}
function referenceShingleHashes(body, options = {}) {
  const max = clampMax(options.max);
  if (typeof body !== "string" || max === 0) return [];
  const words = [];
  const inResolution = [];
  let section = false;
  let sectionLevel = 0;
  for (const rawLine of body.slice(0, 200_000).split(/\r?\n/)) {
    const line = rawLine.trim();
    const heading = HEADING_RE.exec(line);
    if (heading) {
      const level = heading[1].length;
      if (section && level <= sectionLevel) section = false;
      if (RESOLUTION_HEADING_RE.test(heading[2].trim())) {
        section = true;
        sectionLevel = level;
      }
    }
    const marked = section || RESOLUTION_LEAD_RE.test(line);
    for (const word of wordsOf(line)) {
      words.push(word);
      inResolution.push(marked);
    }
  }
  const seen = new Set();
  const resolution = [];
  const rest = [];
  for (let i = 0; i + 8 <= words.length; i += 1) {
    const hash = hashOf(words.slice(i, i + 8));
    if (seen.has(hash)) continue;
    seen.add(hash);
    (inResolution[i] ? resolution : rest).push({ hash, pos: i });
  }
  const df = options.df;
  if (df) {
    const rarity = (entry) => {
      const count = df.get(entry.hash);
      return typeof count === "number" && Number.isFinite(count) ? count : 0;
    };
    const byRarity = (a, b) => rarity(a) - rarity(b) || a.pos - b.pos;
    resolution.sort(byRarity);
    rest.sort(byRarity);
  }
  return [...resolution, ...rest].slice(0, max).map((entry) => entry.hash);
}
// ---------------------------------------------------------------------------

const REPEATED = "the deploy uses make and never a bare tsc build step";
const BODIES = {
  empty: "",
  short: "too few words here",
  plain: Array.from({ length: 120 }, (_, i) => `word${i % 37}`).join(" "),
  sections: [
    "# Note",
    REPEATED,
    "Some context that is not a resolution at all, just background prose for the reader.",
    "## Resolution",
    `We fixed it. ${REPEATED}. Then we verified the gate twice over.`,
    "### Detail inside the section",
    "Nested detail stays inside the resolution section until a heading at its level.",
    "## Background",
    "Back outside the section with more ordinary prose about the system and its history.",
    "- Fix: restart the daemon after the model cache is rebuilt from scratch",
    "Root cause: the socket path pointed at a stale directory left by an old install",
  ].join("\n"),
  unicode: "Ärger über die Brücke: naïve café ﬁx — décision prise après réflexion approfondie du comité technique",
  long: Array.from({ length: 700 }, (_, i) => `term${(i * 7919) % 263}`).join(" "),
};

test("shingleHashes matches the reference algorithm on every body, with and without max", () => {
  for (const [name, body] of Object.entries(BODIES)) {
    for (const max of [undefined, 0, 1, 5, 64, 1000, Number.NaN]) {
      const options = max === undefined ? {} : { max };
      assert.deepEqual(shingleHashes(body, options), referenceShingleHashes(body, options), `${name} max=${max}`);
    }
  }
});

test("shingleHashes matches the reference algorithm with a DF map", () => {
  for (const [name, body] of Object.entries(BODIES)) {
    const all = referenceShingleHashes(body, { max: 64 });
    const df = new Map(all.map((hash, index) => [hash, (index * 13) % 7]));
    for (const max of [undefined, 3, 64]) {
      const options = { df, ...(max === undefined ? {} : { max }) };
      assert.deepEqual(shingleHashes(body, options), referenceShingleHashes(body, options), `${name} df max=${max}`);
    }
  }
});

test("a shingle seen first outside a resolution section keeps its first category", () => {
  // REPEATED occurs in background prose first, then inside ## Resolution.
  const out = shingleHashes(BODIES.sections);
  assert.deepEqual(out, referenceShingleHashes(BODIES.sections));
  assert.equal(new Set(out).size, out.length, "no duplicates");
});

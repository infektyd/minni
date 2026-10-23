// The scan candidate path (VaultSearchCandidates in src/vault.ts) reads every
// note it has not yet admitted once per lookup key, and the number of keys is
// the number of distinct query terms, which nothing caps. Without a bound, a
// prompt of many terms that match nothing (a pasted hex dump, an id list)
// costs one pass over the vault per term: 400 such terms made a hook slower
// than the index build the scan exists to avoid. SCAN_BUDGET_PASSES bounds it:
// past the budget the scan hands the query to the index path.
//
// This pins the bound end to end, in time, against the index path's own cold
// cost on an identical vault in the same process (so machine load moves both
// sides). The limit is deliberately generous; the unbounded scan misses it by
// a wide margin.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import test from "node:test";

import { searchVaultNotes, vaultSearchCacheState } from "../dist/vault.js";

process.env.MINNI_HOME = await mkdtemp(path.join(tmpdir(), "sm-scan-bound-home-"));

const NOTES = 200;
const NOTE_CHARS = 40_000;
// Prose from a small vocabulary, with em dashes (two-byte text, like real
// notes). No word starts with "eq", so no missing key below can match.
const WORDS = (
  "the cache keeps every note warm while the daemon answers recall and the hook " +
  "reads the vault before each prompt then scores terms ranks results and writes " +
  "a pointer for the agent so memory changes behaviour across sessions and tools"
).split(" ");

async function writeVault(label) {
  const root = await mkdtemp(path.join(tmpdir(), `sm-scan-bound-${label}-`));
  const dir = path.join(root, "wiki", "concepts");
  await mkdir(dir, { recursive: true });
  let a = 0x9e3779b9;
  const next = () => {
    a = (Math.imul(a ^ (a >>> 15), 0x2c1b3c6d) + 0x6d2b79f5) >>> 0;
    return a;
  };
  for (let n = 0; n < NOTES; n++) {
    let body = "";
    while (body.length < NOTE_CHARS) {
      body += WORDS[next() % WORDS.length];
      body += next() % 9 === 0 ? " — " : " ";
    }
    await writeFile(path.join(dir, `note-${n}.md`), `---\ntitle: Note ${n}\nstatus: accepted\n---\n\n# Note ${n}\n\n${body}\n`);
  }
  return root;
}

// 400 distinct short keys (short patterns are the slow case for a substring
// search) that match no token in the vault.
const MISSING = Array.from({ length: 400 }, (_, i) => `eq${i.toString(36).padStart(2, "0")}`).join(" ");

test("scan: 400 terms that match nothing cost about one index build, not one pass each", async (t) => {
  const roots = [];
  t.after(async () => {
    for (const root of roots) await rm(root, { recursive: true, force: true });
  });
  const warm = await writeVault("warm");
  const indexVault = await writeVault("index");
  const scanVault = await writeVault("scan");
  roots.push(warm, indexVault, scanVault);

  // Warm the read/parse path and the JIT on a third copy, on both paths.
  await searchVaultNotes(warm, "cache daemon", 18, undefined, { candidates: "index" });
  await searchVaultNotes(warm, "cache hook", 18, undefined, { candidates: "scan" });

  let start = performance.now();
  const viaIndex = await searchVaultNotes(indexVault, MISSING, 18, undefined, { candidates: "index" });
  const indexMs = performance.now() - start;

  start = performance.now();
  const viaScan = await searchVaultNotes(scanVault, MISSING, 18, undefined, { candidates: "scan" });
  const scanMs = performance.now() - start;

  assert.deepEqual(viaScan, viaIndex);
  assert.deepEqual(viaScan, [], "nothing matches");
  const limitMs = 2 * indexMs + 250;
  t.diagnostic(`index path ${indexMs.toFixed(0)} ms, scan path ${scanMs.toFixed(0)} ms, limit ${limitMs.toFixed(0)} ms`);
  assert.ok(
    scanMs <= limitMs,
    `scan took ${scanMs.toFixed(0)} ms for 400 missing terms; the bound is ${limitMs.toFixed(0)} ms ` +
      `(2 x the index path's ${indexMs.toFixed(0)} ms + 250)`,
  );
  assert.equal(vaultSearchCacheState(scanVault).prefixIndex, true, "the scan handed over to the index");
});

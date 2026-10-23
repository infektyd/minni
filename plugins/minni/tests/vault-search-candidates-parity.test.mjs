// searchVaultNotes has two candidate paths (VaultSearchCandidates in
// src/vault.ts): "index" builds an inverted index of every lookup key of every
// note and unions its buckets; "scan" tests each lookup key against each
// note's text directly, for one-shot hook processes that would otherwise pay
// the whole index build per prompt. Parity is the entire contract: for every
// vault and query, both paths must select the same candidates in the same
// order, and return the same results — same notes, same scores, same order,
// same fields.
//
// Every assertion here runs BOTH paths in one process over one cache, scan
// first (the cold-hook order: the cache is built with no index), and compares:
//   - the full scored result list (limit far above the vault size),
//   - the UPS-sized top 18,
//   - the candidate list itself, in order (vaultSearchCandidatePaths), so a
//     candidate that scores 0 cannot hide a divergence.
// The index path is the oracle; the fixture-shape assertions below only pin
// that the fixture really reaches the key rules it is meant to stress, so
// parity cannot pass vacuously.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  searchVaultNotes,
  vaultSearchCacheState,
  vaultSearchCandidatePaths,
} from "../dist/vault.js";

// Hermetic: ensureVault and anything it touches stay out of the real home.
process.env.MINNI_HOME = await mkdtemp(path.join(tmpdir(), "sm-parity-home-"));

const ALL = 100_000;
const roots = [];

test.after(async () => {
  for (const root of roots) await rm(root, { recursive: true, force: true });
});

function noteText({ frontmatter = {}, body }) {
  const lines = Object.entries(frontmatter).map(([key, value]) => `${key}: ${value}`);
  return lines.length > 0 ? `---\n${lines.join("\n")}\n---\n\n${body}\n` : `${body}\n`;
}

async function writeVault(label, notes) {
  const root = await mkdtemp(path.join(tmpdir(), `sm-parity-${label}-`));
  roots.push(root);
  for (const note of notes) {
    const full = path.join(root, note.rel);
    await mkdir(path.dirname(full), { recursive: true });
    await writeFile(full, noteText(note), "utf8");
  }
  return root;
}

async function assertParity(vault, query, label) {
  const scan = await searchVaultNotes(vault, query, ALL, undefined, { candidates: "scan" });
  const index = await searchVaultNotes(vault, query, ALL, undefined, { candidates: "index" });
  assert.deepStrictEqual(scan, index, `${label}: full results differ for ${JSON.stringify(query)}`);
  const scanTop = await searchVaultNotes(vault, query, 18, undefined, { candidates: "scan" });
  const indexTop = await searchVaultNotes(vault, query, 18, undefined, { candidates: "index" });
  assert.deepStrictEqual(scanTop, indexTop, `${label}: top-18 results differ for ${JSON.stringify(query)}`);
  const scanCandidates = vaultSearchCandidatePaths(vault, query, "scan");
  const indexCandidates = vaultSearchCandidatePaths(vault, query, "index");
  assert.deepStrictEqual(
    scanCandidates,
    indexCandidates,
    `${label}: candidates differ for ${JSON.stringify(query)}`,
  );
  return { results: index, candidates: indexCandidates ?? [] };
}

// ---------------------------------------------------------------------------
// Fixed fixture: one note per key rule, each the ONLY emitter of its key, so a
// scan that drops the rule drops exactly that note.
// ---------------------------------------------------------------------------

const C = "wiki/concepts";
const FIXTURE = [
  // plurals and the s / es / ed / ing stems, both sides of each length gate
  { rel: `${C}/plural-tests.md`, frontmatter: { title: "Plural tests" }, body: "The suite runs tests nightly." },
  { rel: `${C}/singular-test.md`, frontmatter: { title: "Singular" }, body: "One test only." },
  { rel: `${C}/gerund-testing.md`, frontmatter: { title: "Gerund" }, body: "We are testing again." },
  { rel: `${C}/past-tested.md`, frontmatter: { title: "Past" }, body: "Everything was tested." },
  { rel: `${C}/boxes-short-es.md`, frontmatter: { title: "Short es" }, body: "Stack the boxes and the bus." },
  { rel: `${C}/boxing-short-ing.md`, frontmatter: { title: "Short ing" }, body: "A boxing match, sizes vary." },
  { rel: `${C}/long-ing.md`, frontmatter: { title: "Long ing" }, body: "The socket was preconditioning frames." },
  { rel: `${C}/long-ed.md`, frontmatter: { title: "Long ed" }, body: "The socket was preconditioned first." },
  { rel: `${C}/long-es.md`, frontmatter: { title: "Long es" }, body: "Workers spawn multiprocesses here." },
  { rel: `${C}/long-s.md`, frontmatter: { title: "Long s" }, body: "Several configurations exist." },
  { rel: `${C}/long-non-stem.md`, frontmatter: { title: "Long non stem" }, body: "Speaking configurationally, and abcdefghijxyz too." },
  // an 11+-character key plus 1, 2 or 3 characters that are not a stem
  // suffix: none of these may admit `precondition`
  { rel: `${C}/long-rest-1.md`, frontmatter: { title: "Rest one" }, body: "Logs say preconditionx once." },
  { rel: `${C}/long-rest-2.md`, frontmatter: { title: "Rest two" }, body: "Logs say preconditionxy once." },
  { rel: `${C}/long-rest-3.md`, frontmatter: { title: "Rest three" }, body: "Logs say preconditionxyz once." },
  // prefixes at the 3- and 10-character edges
  { rel: `${C}/prefix-3.md`, frontmatter: { title: "Prefix three" }, body: "The cfgloader reads files." },
  { rel: `${C}/prefix-10.md`, frontmatter: { title: "Prefix ten" }, body: "Token abcdefghijk is eleven long." },
  { rel: `${C}/boundary.md`, frontmatter: { title: "Boundary" }, body: "Something about a thing, nothing more." },
  // `_`-joined tokens, digits
  { rel: `${C}/underscore.md`, frontmatter: { title: "Underscore" }, body: "Call foo_bar_baz then ab_cd and _leading with trailing_ and double__under." },
  { rel: `${C}/digits.md`, frontmatter: { title: "Digits" }, body: "Use v2 over http2 on x86_64 with utf8 and sha256 since 2026." },
  // status-skipped (never candidates, never results) and privacy-blocked
  { rel: `${C}/superseded.md`, frontmatter: { title: "Cache superseded", status: "superseded" }, body: "cache tests foo_bar cfgloader preconditioning" },
  { rel: `${C}/draft.md`, frontmatter: { title: "Cache draft", status: "draft" }, body: "cache tests foo_bar cfgloader" },
  { rel: `${C}/rejected.md`, frontmatter: { title: "Cache rejected", status: "rejected" }, body: "cache tests" },
  { rel: `${C}/expired.md`, frontmatter: { title: "Cache expired", status: "expired" }, body: "cache tests" },
  { rel: `${C}/superseded-by.md`, frontmatter: { title: "Cache old", superseded_by: "wiki/concepts/cache-policy" }, body: "cache tests" },
  { rel: `${C}/blocked.md`, frontmatter: { title: "Cache blocked", privacy: "blocked" }, body: "cache tests foo_bar" },
  // near-duplicate titles (score ties broken by path), session notes,
  // learnings, correction boosts
  { rel: `${C}/cache-policy.md`, frontmatter: { title: "Cache Policy", status: "accepted" }, body: "The cache policy caches cached entries." },
  { rel: `${C}/cache-policy-2.md`, frontmatter: { title: "Cache Policy", status: "accepted" }, body: "The cache policy caches cached entries." },
  { rel: `${C}/cache-policy-v2.md`, frontmatter: { title: "Cache Policy v2" }, body: "The cache policy caches cached entries." },
  { rel: "wiki/sessions/20260101-cache-session.md", frontmatter: { title: "Cache session", type: "session" }, body: "Session about cache tests." },
  { rel: "wiki/sessions/20260102-cache-session.md", frontmatter: { title: "Cache session" }, body: "Session about cache tests." },
  { rel: `${C}/learning.md`, frontmatter: { title: "Cache learning", minni_learning: "true" }, body: "Learned: cache tests matter." },
  { rel: "wiki/decisions/correction.md", frontmatter: { title: "Cache correction", type: "correction" }, body: "Correction: cache tests moved." },
  { rel: "wiki/decisions/decision.md", frontmatter: { title: "Cache decision", type: "decision" }, body: "Decision on cache." },
  // non-ASCII text and case folding
  { rel: `${C}/swedish.md`, frontmatter: { title: "Sökning" }, body: "Sökning och återställ, äpple och öl. Cachen är varm." },
  { rel: `${C}/cjk-emoji.md`, frontmatter: { title: "設定" }, body: "設定ファイルcache は🚀deploy で、naïve café ﬁle." },
  { rel: `${C}/case-fold.md`, frontmatter: { title: "UPPER" }, body: "CACHE TESTS İstanbul Kelvin ÅÄÖ." },
  { rel: `${C}/plain-1.md`, frontmatter: { title: "Plain one" }, body: "Notes about the deploy pipeline and ci." },
  { rel: `${C}/plain-2.md`, frontmatter: { title: "Plain two" }, body: "The db migration runs before deploy." },
];

const LONG_WORDS = [
  "the", "cache", "policy", "tests", "testing", "deploy", "pipeline", "socket",
  "frames", "configuration", "multiprocess", "precondition", "boxes", "bus",
  "foo_bar", "cfg", "abcdefghij", "v2", "http2", "x86_64", "notes", "session",
  "correction", "decision", "learning", "migration", "runs", "before", "nightly",
];
function longQuery(words, count) {
  const out = [];
  for (let i = 0; i < count; i++) out.push(words[(i * 7 + 3) % words.length]);
  return out.join(" ");
}

const QUERIES = [
  // short
  "cache", "tests", "test", "ci", "db", "fix", "deploy",
  // stems and plurals
  "testing", "tested", "boxes", "box", "bus", "runs", "sizes", "boxing", "caches", "cached",
  // long-key stem edges (11+ characters)
  "precondition", "preconditions", "preconditioned", "preconditioning", "multiprocess",
  "preconditionx", "preconditionxy", "preconditionxyz",
  "multiprocesses", "configuration", "configurations", "configurationally",
  // prefix edges: 3, 10, 11 and 12 characters
  "cfg", "cfgl", "abcdefghij", "abcdefghijk", "abcdefghijks", "abcdefghijx", "abc",
  // boundaries
  "ing", "thing", "nothing", "hing",
  // `_` subtokens and digits
  "bar", "baz", "foo_bar", "bar_baz", "foo_bar_baz", "ab", "cd", "ab_cd", "leading", "_leading",
  "trailing_", "under", "double__under", "x86", "64", "86_64", "x86_64", "v2", "http2", "utf8",
  "2026", "sha256", "__/ab",
  // heavy punctuation
  "cache!!! ??? (tests) [fix] {box} <run>", "--verbose", "foo/bar", "a-b-c", "c++ c# .net",
  "//", "-", "--", "cache/tests", "cache-tests", "tests/", "...cache...", "\"cache tests\"",
  // non-ASCII: Swedish, CJK, emoji, folding
  "sökning", "återställ", "äpple öl", "cachen", "設定ファイル", "設定", "🚀 deploy", "🚀",
  "İstanbul", "istanbul", "stanbul", "kelvin", "naïve café", "ﬁle", "ÅÄÖ",
  // phrases
  "cache policy", "the cache policy caches cached entries", "cache tests",
  // hits nothing
  "zzzqqq", "nonexistentterm", "qq",
  // stopword-only (falls back to raw tokens)
  "how do we do it?", "why",
  // long: hundreds of words, all indexable, then one with a non-indexable term
  longQuery(LONG_WORDS, 300),
  `${longQuery(LONG_WORDS, 200)} --verbose`,
  longQuery(["zzz", "qqq", "xyzzy", "plugh", "cfgx", "abcdefghijz"], 250),
];

test("parity: the fixed fixture, every query shape, candidates and results identical", async () => {
  const vault = await writeVault("fixed", FIXTURE);

  // Scan first, on a fresh cache: the cold-hook order. It must not build the
  // index (that is the whole point of the path).
  const first = await searchVaultNotes(vault, "cache tests", ALL, undefined, { candidates: "scan" });
  assert.ok(first.length > 0, "the fixture answers a basic query");
  assert.deepEqual(vaultSearchCacheState(vault), { notes: FIXTURE.length - 1, prefixIndex: false });

  const seen = new Map();
  for (const query of QUERIES) {
    seen.set(query, await assertParity(vault, query, "fixed"));
  }
  assert.equal(vaultSearchCacheState(vault).prefixIndex, true, "the index path ran on the shared cache");

  // Fixture shape: the index lookup really is engaged (a strict, non-empty
  // subset), and each rule's note is selected or rejected as the rule says.
  const candidatesOf = (query) => seen.get(query).candidates;
  const total = FIXTURE.length - 1;
  assert.ok(candidatesOf("cache").length > 0 && candidatesOf("cache").length < total);
  const has = (query, rel) => candidatesOf(query).includes(rel);
  const cases = [
    ["precondition", `${C}/long-ing.md`, true],
    ["precondition", `${C}/long-ed.md`, true],
    ["multiprocess", `${C}/long-es.md`, true],
    ["configuration", `${C}/long-s.md`, true],
    ["configuration", `${C}/long-non-stem.md`, false],
    ["precondition", `${C}/long-rest-1.md`, false],
    ["precondition", `${C}/long-rest-2.md`, false],
    ["precondition", `${C}/long-rest-3.md`, false],
    ["preconditionx", `${C}/long-rest-1.md`, true],
    ["preconditionxyz", `${C}/long-rest-3.md`, true],
    ["abcdefghij", `${C}/long-non-stem.md`, true],
    ["abcdefghijx", `${C}/long-non-stem.md`, false],
    ["abcdefghij", `${C}/prefix-10.md`, true],
    ["cfg", `${C}/prefix-3.md`, true],
    ["ing", `${C}/boundary.md`, false],
    ["hing", `${C}/boundary.md`, false],
    ["thing", `${C}/boundary.md`, true],
    ["bar", `${C}/underscore.md`, true],
    ["ab", `${C}/underscore.md`, true],
    ["bar_baz", `${C}/underscore.md`, false],
    ["foo_bar", `${C}/underscore.md`, true],
    ["under", `${C}/underscore.md`, true],
    ["x86", `${C}/digits.md`, true],
    ["86_64", `${C}/digits.md`, false],
    ["boxes", `${C}/boxes-short-es.md`, true],
    ["kelvin", `${C}/case-fold.md`, true],
    ["stanbul", `${C}/case-fold.md`, true],
  ];
  for (const [query, rel, expected] of cases) {
    assert.equal(has(query, rel), expected, `fixture shape: ${query} -> ${rel} should be ${expected}`);
  }
  // Status-skipped notes are never results; through the key lookup they are
  // not even candidates (the every-note fallback hands them to the scorer,
  // which zeroes them). Blocked notes are never cached at all.
  const skipped = /superseded|draft|rejected|expired|blocked/;
  for (const query of QUERIES) {
    const { results, candidates } = seen.get(query);
    for (const r of results) {
      assert.ok(!skipped.test(r.relativePath), `${r.relativePath} must never be a result (${JSON.stringify(query)})`);
    }
    for (const rel of candidates) {
      assert.ok(!rel.includes("blocked"), `blocked note ${rel} must never be cached (${JSON.stringify(query)})`);
      if (candidates.length < total) {
        assert.ok(!skipped.test(rel), `${rel} must not be a lookup candidate (${JSON.stringify(query)})`);
      }
    }
  }
  // Ranking features the fixture carries actually reach the results.
  const cacheResults = seen.get("cache tests").results.map((r) => r.relativePath);
  assert.ok(cacheResults.includes("wiki/decisions/correction.md"));
  assert.ok(cacheResults.includes(`${C}/learning.md`));
  assert.ok(cacheResults.includes("wiki/sessions/20260101-cache-session.md"));
  const ties = seen.get("cache policy").results.filter((r) => r.title === "Cache Policy");
  assert.equal(ties.length, 2, "near-duplicate titles both surface");
});

test("parity: an empty or whitespace-only query returns nothing and builds nothing, on both paths", async () => {
  for (const candidates of ["scan", "index"]) {
    const vault = await writeVault(`empty-${candidates}`, FIXTURE);
    for (const query of ["", "   ", "\n\t  \n"]) {
      assert.deepEqual(await searchVaultNotes(vault, query, ALL, undefined, { candidates }), []);
    }
    assert.equal(vaultSearchCacheState(vault), undefined, `${candidates}: no cache was built`);
  }
});

test("parity: below the 20-note gate every note is a candidate on both paths", async () => {
  const small = FIXTURE.filter((note) => !note.rel.includes("blocked")).slice(0, 19);
  const vault = await writeVault("small", small);
  for (const query of ["cache", "tests", "bar", "zzzqqq", "precondition"]) {
    const { candidates } = await assertParity(vault, query, "small");
    assert.equal(candidates.length, 19, `${query}: the gate admits every note`);
  }
});

// ---------------------------------------------------------------------------
// Property pass: random notes and queries over a small vocabulary that holds
// the edge shapes, with a fixed seed. Override with MINNI_PARITY_SEED to
// explore; a failure names the seed, vault and query.
// ---------------------------------------------------------------------------

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const VOCAB = [
  "cache", "caches", "cached", "caching", "test", "tests", "testing", "tested",
  "box", "boxes", "boxing", "bus", "run", "runs", "running", "ab", "cd", "ab_cd",
  "foo_bar", "_lead", "trail_", "x86_64", "v2", "abcdefghij", "abcdefghijk",
  "abcdefghijks", "abcdefghijkl", "abcdefghijking", "precondition", "preconditioned",
  "preconditions", "preconditioning", "preconditiones", "configurationally", "ss", "sss",
  "thing", "ing", "sökning", "återställ", "設定", "🚀", "İ", "é", "K", "CACHE", "Tests",
  "deploy", "note", "2026", "a", "s", "es", "ed", "q",
];
const SEPARATORS = [" ", " ", " ", "_", "-", "/", ".", "\n", "", "é", "ö", ", ", "__", "?"];
const SECTIONS = ["concepts", "concepts", "sessions", "decisions", "entities"];
const STATUSES = [undefined, undefined, undefined, "accepted", "candidate", "superseded", "draft"];
const TITLES = ["Cache", "Cache", "Tests", "Deploy notes", "設定", "Sökning", "x86_64 box"];

function pick(rand, list) {
  return list[Math.floor(rand() * list.length)];
}

function randomPhrase(rand, minWords, maxWords) {
  const count = minWords + Math.floor(rand() * (maxWords - minWords + 1));
  let out = "";
  for (let i = 0; i < count; i++) {
    out += pick(rand, VOCAB);
    if (i < count - 1) out += pick(rand, SEPARATORS);
  }
  return out;
}

function randomNote(rand, index) {
  const frontmatter = {};
  if (rand() < 0.7) frontmatter.title = pick(rand, TITLES);
  const status = pick(rand, STATUSES);
  if (status) frontmatter.status = status;
  if (rand() < 0.05) frontmatter.superseded_by = "wiki/concepts/x";
  if (rand() < 0.15) frontmatter.type = pick(rand, ["correction", "decision", "fix", "concept"]);
  if (rand() < 0.15) frontmatter.minni_learning = "true";
  if (rand() < 0.04) frontmatter.privacy = "blocked";
  else if (rand() < 0.15) frontmatter.privacy = pick(rand, ["private", "local-only", "safe"]);
  const section = pick(rand, SECTIONS);
  const stem = randomPhrase(rand, 1, 2).replace(/[^a-z0-9_-]+/gi, "-").slice(0, 24) || "n";
  return {
    rel: `wiki/${section}/${String(index).padStart(2, "0")}-${stem}.md`,
    frontmatter,
    body: randomPhrase(rand, 3, 40),
  };
}

function randomQuery(rand) {
  const roll = rand();
  if (roll < 0.03) return pick(rand, ["", "  ", "?!", "--", "-"]);
  if (roll < 0.1) return randomPhrase(rand, 40, 120);
  return randomPhrase(rand, 1, 6);
}

test("parity: property pass over random vaults and queries (fixed seed)", async () => {
  const seed = Number(process.env.MINNI_PARITY_SEED ?? 0x5ea2c4);
  const rand = mulberry32(seed);
  const VAULTS = 30;
  const QUERIES_PER_VAULT = 12;
  let iterations = 0;
  let indexEngaged = 0;
  for (let v = 0; v < VAULTS; v++) {
    const noteCount = 22 + Math.floor(rand() * 10);
    const notes = Array.from({ length: noteCount }, (_, i) => randomNote(rand, i));
    const vault = await writeVault(`prop-${v}`, notes);
    for (let q = 0; q < QUERIES_PER_VAULT; q++) {
      const query = randomQuery(rand);
      const label = `seed=${seed} vault=${v} query=${q}`;
      const { candidates } = await assertParity(vault, query, label);
      const state = vaultSearchCacheState(vault);
      if (state && state.notes >= 20 && candidates.length < state.notes) indexEngaged++;
      iterations++;
    }
  }
  assert.equal(iterations, VAULTS * QUERIES_PER_VAULT);
  // Non-vacuity: most iterations went through the key lookup, not the
  // every-note fallback.
  assert.ok(indexEngaged > iterations / 3, `seed=${seed}: only ${indexEngaged}/${iterations} iterations engaged the lookup`);
});

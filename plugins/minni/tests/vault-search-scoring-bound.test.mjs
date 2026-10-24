// Long-prompt scoring cost & multi-term Aho-Corasick parity test
// (Backlog rows l & p).
//
// Row l: a pasted ~300-word prompt (~200 query terms) spends the bulk of its
// time scoring candidate notes against ~200 terms rather than selecting
// candidates.
// Row p: on a 405-term query whose terms DO appear across a 783-note, ~12.6 MB
// vault, both `candidates: "scan"` and `candidates: "index"` admit almost the
// entire vault on the first few keys and spend ~700 ms in `scoreVaultNoteCached`
// doing 782 x 405 per-(note, term) substring/boundary passes.
//
// Replacing the per-term loop with a single-pass Aho-Corasick automaton over
// the query's pure-lower-ASCII terms cuts the per-note scoring pass to one
// character scan while preserving 100% score, ordering, and snippet parity
// with the reference per-term word-boundary matcher.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import test from "node:test";

import {
  CORRECTION_CLASS_TYPES,
  CORRECTION_SALIENCE_BOOST,
  searchVaultNotes,
  vaultSearchCacheState,
  vaultSearchCandidatePaths,
} from "../dist/vault.js";

process.env.MINNI_HOME = await mkdtemp(
  path.join(tmpdir(), "sm-scoring-bound-home-"),
);

const ALL = 100_000;
const roots = [];

test.after(async () => {
  for (const root of roots) await rm(root, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// Reference oracle: verbatim pre-Aho-Corasick regex / per-term specification
// ---------------------------------------------------------------------------

const FRONTMATTER_RE = /^\uFEFF?\s*---\r?\n([\s\S]*?)\r?\n---/;
const WORD_EDGE_CHAR = /[\p{L}\p{N}_]/u;
const NO_SEPARATOR_SCRIPTS =
  "\\p{sc=Han}\\p{sc=Hiragana}\\p{sc=Katakana}\\p{sc=Hangul}";
const NOT_WORD_BEFORE = `(?:(?<![\\p{L}\\p{N}])|(?<=[${NO_SEPARATOR_SCRIPTS}]))`;
const NOT_WORD_AFTER = `(?:(?![\\p{L}\\p{N}])|(?=[${NO_SEPARATOR_SCRIPTS}]))`;
const MORPHOLOGICAL_SUFFIX = "(?:s|es|ing|ed)?";
const MIN_MORPHOLOGICAL_LENGTH = 4;
const SINGLE_NO_SEPARATOR_CHAR = new RegExp(
  `^[${NO_SEPARATOR_SCRIPTS}]$`,
  "u",
);
const LEAD_STICKY_RE = new RegExp(NOT_WORD_BEFORE, "yu");
const TAIL_EXACT_STICKY_RE = new RegExp(NOT_WORD_AFTER, "yu");
const TAIL_INFLECT_STICKY_RE = new RegExp(
  `${MORPHOLOGICAL_SUFFIX}${NOT_WORD_AFTER}`,
  "yu",
);

const STOP_WORDS = new Set([
  "a",
  "an",
  "and",
  "are",
  "as",
  "at",
  "be",
  "by",
  "do",
  "for",
  "if",
  "in",
  "is",
  "it",
  "no",
  "of",
  "on",
  "or",
  "so",
  "the",
  "to",
  "up",
  "we",
  "with",
  "you",
  "can",
  "how",
  "what",
  "where",
  "which",
  "who",
  "why",
]);

function oracleQueryTerms(query) {
  const tokens = [
    ...new Set(query.toLowerCase().match(/[a-z0-9_/-]{2,}/g) ?? []),
  ].filter((token) => /[a-z0-9]/.test(token));
  const content = tokens.filter((token) => !STOP_WORDS.has(token));
  return content.length > 0 ? content : tokens;
}

function oracleIsPhrase(candidate) {
  if (!/[\p{L}\p{N}]/u.test(candidate)) return false;
  return (
    candidate.length >= 2 || SINGLE_NO_SEPARATOR_CHAR.test(candidate)
  );
}

function oraclePhraseForms(query) {
  const raw = query.toLowerCase().trim();
  const trimmed = raw.replace(/^[^\p{L}\p{N}_/-]+|[^\p{L}\p{N}_/-]+$/gu, "");
  return [raw, trimmed].filter(
    (form, index, all) => oracleIsPhrase(form) && all.indexOf(form) === index,
  );
}

function oracleWordRegex(term, inflect) {
  if (!term) return undefined;
  const last = term[term.length - 1];
  const lead = WORD_EDGE_CHAR.test(term[0]) ? NOT_WORD_BEFORE : "";
  const tail = WORD_EDGE_CHAR.test(last) ? NOT_WORD_AFTER : "";
  if (!lead && !tail && !/[\p{L}\p{N}]/u.test(term)) return undefined;
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const suffix =
    inflect && term.length >= MIN_MORPHOLOGICAL_LENGTH && /\p{L}/u.test(last)
      ? MORPHOLOGICAL_SUFFIX
      : "";
  return new RegExp(`${lead}${escaped}${suffix}${tail}`, "giu");
}

function oracleCountWordOccurrences(text, term, inflect = true, maxCount = 5) {
  if (!term) return 0;
  const textLower = text.toLowerCase();
  const termLower = term.toLowerCase();
  let idx = textLower.indexOf(termLower);
  if (idx === -1) return 0;
  if (termLower.length <= 256) {
    try {
      const regex = oracleWordRegex(term, inflect);
      if (!regex) return 0;
      const matches = text.match(regex);
      return matches ? Math.min(matches.length, maxCount) : 0;
    } catch {
      // Fall through to sticky boundary scan on engines with strict RegExp size caps.
    }
  }
  const last = termLower[termLower.length - 1];
  const leadRequired = WORD_EDGE_CHAR.test(termLower[0]);
  const tailRequired = WORD_EDGE_CHAR.test(last);
  if (!leadRequired && !tailRequired && !/[\p{L}\p{N}]/u.test(termLower)) {
    return 0;
  }
  const allowSuffix =
    inflect &&
    termLower.length >= MIN_MORPHOLOGICAL_LENGTH &&
    /\p{L}/u.test(last);
  const tailRe = allowSuffix ? TAIL_INFLECT_STICKY_RE : TAIL_EXACT_STICKY_RE;
  let count = 0;
  while (idx !== -1) {
    if (leadRequired) {
      LEAD_STICKY_RE.lastIndex = idx;
      if (!LEAD_STICKY_RE.test(textLower)) {
        idx = textLower.indexOf(termLower, idx + 1);
        continue;
      }
    }
    const afterIdx = idx + termLower.length;
    if (!tailRequired) {
      count++;
      if (count >= maxCount) return count;
      idx = textLower.indexOf(termLower, afterIdx);
      continue;
    }
    tailRe.lastIndex = afterIdx;
    if (tailRe.test(textLower)) {
      count++;
      if (count >= maxCount) return count;
      idx = textLower.indexOf(termLower, tailRe.lastIndex);
    } else {
      idx = textLower.indexOf(termLower, idx + 1);
    }
  }
  return count;
}

function oracleFirstWordIndex(text, term) {
  if (!term) return -1;
  const textLower = text.toLowerCase();
  const termLower = term.toLowerCase();
  let idx = textLower.indexOf(termLower);
  if (idx === -1) return -1;
  if (termLower.length <= 256) {
    try {
      const regex = oracleWordRegex(term, true);
      if (!regex) return -1;
      regex.lastIndex = 0;
      return regex.exec(text)?.index ?? -1;
    } catch {
      // Fall through to sticky boundary scan.
    }
  }
  const last = termLower[termLower.length - 1];
  const leadRequired = WORD_EDGE_CHAR.test(termLower[0]);
  const tailRequired = WORD_EDGE_CHAR.test(last);
  if (!leadRequired && !tailRequired && !/[\p{L}\p{N}]/u.test(termLower)) {
    return -1;
  }
  const allowSuffix =
    termLower.length >= MIN_MORPHOLOGICAL_LENGTH && /\p{L}/u.test(last);
  const tailRe = allowSuffix ? TAIL_INFLECT_STICKY_RE : TAIL_EXACT_STICKY_RE;
  while (idx !== -1) {
    if (leadRequired) {
      LEAD_STICKY_RE.lastIndex = idx;
      if (!LEAD_STICKY_RE.test(textLower)) {
        idx = textLower.indexOf(termLower, idx + 1);
        continue;
      }
    }
    const afterIdx = idx + termLower.length;
    if (!tailRequired) return idx;
    tailRe.lastIndex = afterIdx;
    if (tailRe.test(textLower)) return idx;
    idx = textLower.indexOf(termLower, idx + 1);
  }
  return -1;
}

function oracleSnippetFor(markdown, terms, maxLength = 280) {
  const plain = markdown
    .replace(FRONTMATTER_RE, "")
    .replace(/^#+\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  const earliest = (indexes) =>
    indexes.filter((index) => index >= 0).sort((a, b) => a - b)[0] ?? -1;
  let firstHit = earliest(terms.map((term) => oracleFirstWordIndex(plain, term)));
  if (firstHit < 0) {
    const lower = plain.toLowerCase();
    firstHit = earliest(terms.map((term) => lower.indexOf(term)));
  }
  const start = Math.max(0, Math.max(firstHit, 0) - 80);
  const end = Math.min(plain.length, start + maxLength);
  const prefix = start > 0 ? "..." : "";
  const suffix = end < plain.length ? "..." : "";
  return `${prefix}${plain.slice(start, end).trim()}${suffix}`;
}

function oracleFrontmatterField(markdown, key) {
  const fm = markdown.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!fm) return undefined;
  const safeKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const line = fm[1].match(new RegExp(`^${safeKey}:[ \\t]*(.+)$`, "m"));
  const value = line?.[1]?.trim().replace(/^["']|["']$/g, "");
  return value || undefined;
}

function oracleTitleFromMarkdown(relativePath, markdown) {
  const fmTitle = markdown.match(/^title:\s*(.+)$/m)?.[1]?.trim();
  if (fmTitle) return fmTitle.replace(/^["']|["']$/g, "");
  const heading = markdown.match(/^#\s+(.+)$/m)?.[1]?.trim();
  if (heading) return heading;
  return path.basename(relativePath, ".md");
}

function oracleScoreNote(query, terms, relativePath, title, markdown) {
  const status = oracleFrontmatterField(markdown, "status")?.toLowerCase();
  if (
    status === "superseded" ||
    status === "rejected" ||
    status === "expired" ||
    status === "draft"
  ) {
    return 0;
  }
  if (oracleFrontmatterField(markdown, "superseded_by")) return 0;

  const haystack = `${title}\n${relativePath}\n${markdown}`.toLowerCase();
  const titleLower = title.toLowerCase();
  let termScore = 0;
  for (const phrase of oraclePhraseForms(query)) {
    if (oracleCountWordOccurrences(haystack, phrase, false, 1) > 0) {
      termScore += 50;
      break;
    }
  }
  for (const term of terms) {
    const count = oracleCountWordOccurrences(haystack, term, true, 5);
    if (count > 0) {
      termScore +=
        Math.min(count, 5) +
        (oracleCountWordOccurrences(titleLower, term, true, 1) > 0 ? 3 : 0);
    }
  }
  if (termScore < 1) return 0;

  let score = termScore;
  if (relativePath.startsWith("wiki/sessions/")) score += 1;
  if (/minni_learning:\s*true/i.test(markdown)) score += 2;
  const pageType = oracleFrontmatterField(markdown, "type")?.toLowerCase();
  if (pageType && CORRECTION_CLASS_TYPES.has(pageType)) {
    score *= 1 + CORRECTION_SALIENCE_BOOST;
  }
  return score;
}

function noteText({ frontmatter = {}, body }) {
  const lines = Object.entries(frontmatter).map(
    ([key, value]) => `${key}: ${value}`,
  );
  return lines.length > 0
    ? `---\n${lines.join("\n")}\n---\n\n${body}\n`
    : `${body}\n`;
}

async function writeNotesVault(label, notes) {
  const root = await mkdtemp(path.join(tmpdir(), `sm-scoring-${label}-`));
  roots.push(root);
  for (const note of notes) {
    const full = path.join(root, note.rel);
    await mkdir(path.dirname(full), { recursive: true });
    await writeFile(full, noteText(note), "utf8");
  }
  return root;
}

// ---------------------------------------------------------------------------
// 1. Parity oracle tests: diverse notes, overlapping & morphological terms,
//    paths, flags, Unicode/non-ASCII boundaries, 300-word & 405-term prompts
// ---------------------------------------------------------------------------

const PARITY_NOTES = [
  {
    rel: "wiki/concepts/morphology-work.md",
    frontmatter: { title: "Work Pipeline Overview", status: "accepted" },
    body:
      "The worker works on working tasks and worked yesterday. " +
      "Testing tests tested test cases in process and processes. " +
      "Repeat token alpha alpha alpha alpha alpha alpha alpha seven times.",
  },
  {
    rel: "wiki/concepts/overlapping-patterns.md",
    frontmatter: { title: "Project Pro and Suffixes", status: "accepted" },
    body:
      "A pro builds a project with proper tooling. " +
      "Here es and eses appear alongside ing and testing and testings. " +
      "Overlap chains: abcabd abc abcd bcde cdef defg.",
  },
  {
    rel: "wiki/decisions/paths-and-flags.md",
    frontmatter: {
      title: "API Routes and CLI Flags",
      status: "accepted",
      type: "decision",
    },
    body:
      "Call /api/v2/status or /api/v2/health with --verbose and --dry-run. " +
      "Identifiers like minni_learning_score and search_learnings_v2 use underscores. " +
      "minni_learning: true",
  },
  {
    rel: "wiki/sessions/unicode-boundaries.md",
    frontmatter: { title: "Unicode Café & Résumé Notes", status: "accepted" },
    body:
      "In the café we reviewed a résumé and naïve assumptions — test and testing! " +
      "Accented neighbours like über, caf\u00e9s, na\u00efvet\u00e9, and \u65e5\u672c\u8a9eterm\u8a9e must respect Unicode word boundaries.",
  },
  {
    rel: "wiki/concepts/long-body-snippet.md",
    frontmatter: { title: "Long Prose Window Positioning", status: "accepted" },
    body:
      "Opening boilerplate paragraph that does not mention the target tokens at all and stretches past eighty characters easily so the snippet window must shift forward when a match occurs later in the document. ".repeat(
        3,
      ) +
      "Midway through the document we discuss authentication and scorecard metricsbefore hitting a whole-word needle token right here: zephyr_needle_token followed by trailing context that also extends well beyond two hundred and eighty characters in total length so both prefix and suffix ellipses are exercised.",
  },
  {
    rel: "wiki/concepts/tier2-substring-snippet.md",
    frontmatter: { title: "Auth Path Admission", status: "accepted" },
    body:
      "Opening boilerplate text that is more than one hundred characters long so that a Tier 2 substring match later in the body centres the window properly. " +
      "The service performs authentication and authorization before granting access to the scorecard.",
  },
  {
    rel: "wiki/concepts/correction-boost.md",
    frontmatter: {
      title: "Latency Correction Note",
      status: "accepted",
      type: "correction",
    },
    body:
      "Daemon recall latency dropped after bounding candidate scans and multi-term Aho-Corasick scoring across large vaults.",
  },
  {
    rel: "wiki/concepts/superseded-note.md",
    frontmatter: { title: "Old Superseded Note", status: "superseded" },
    body:
      "This superseded note mentions work, test, project, and /api/v2/status but must always score zero.",
  },
  ...Array.from({ length: 16 }, (_, i) => ({
    rel: `wiki/concepts/synthetic-${i}.md`,
    frontmatter: {
      title: `Synthetic Concept ${i}`,
      status: "accepted",
      ...(i % 5 === 0 ? { type: "fix" } : {}),
    },
    body:
      `Synthetic note ${i} discusses module_${i}, handler_${i}, and BatchWorker_${i}. ` +
      `Common terms: daemon recall vault cache index scan latency scoring. ` +
      `Specific tokens: tok_${i}_a tok_${i}_b tok_${i}_c word${100 + i} worked working tests.`,
  })),
];

// Build a ~300-word natural-language prompt (~200 distinct query terms)
const NATURAL_300_WORD_PROMPT = [
  "When we paste a long three hundred word engineering prompt into the session hook,",
  "the recall plugin tokenises every distinct word and scores each candidate note in the vault.",
  "Previously scoreVaultNoteCached ran countWordOccurrences in a nested loop over every note and every term,",
  "causing latency to scale with candidate_notes times query_terms.",
  "Wait until the daemon socket responds, check /api/v2/status with --verbose and --dry-run,",
  "verify that work works worked working and test tests tested testing all inflect properly,",
  "confirm pro does not bleed into project while es eses ing and testing keep their exact boundaries,",
  "and ensure café résumé naïve — test preserves Unicode lookaround semantics.",
  ...Array.from(
    { length: 165 },
    (_, i) =>
      ` prompt_term_${i} analysis_step_${i % 40} verification_gate_${i} tok_${i % 16}_a`,
  ),
].join(" ");

PARITY_NOTES.push({
  rel: "wiki/concepts/exact-300-word-phrase.md",
  frontmatter: { title: "Exact Long Non-ASCII Phrase Note", status: "accepted" },
  body: `Prefix context before exact prompt match: ${NATURAL_300_WORD_PROMPT} — trailing context after match.`,
});

// Build a 405-term query whose terms appear across the parity vault
const MATCHING_405_TERM_PARITY_QUERY = [
  "work",
  "test",
  "process",
  "pro",
  "project",
  "es",
  "eses",
  "ing",
  "testing",
  "/api/v2/status",
  "--verbose",
  "minni_learning_score",
  "zephyr_needle_token",
  "daemon",
  "recall",
  "latency",
  ...Array.from({ length: 390 }, (_, i) => `word${100 + i}`),
].join(" ");

const EDGE_CASE_QUERIES = [
  "work",
  "work works worked working",
  "test tests tested testing process processes",
  "pro project es eses ing testing",
  "/api/v2/status --verbose --dry-run minni_learning_score",
  "café résumé naïve — test",
  "zephyr_needle_token",
  "auth", // Tier 2 substring hit inside "authentication" when combined with matching terms
  "auth daemon recall latency",
  "abcabd abc abcd bcde cdef defg",
  NATURAL_300_WORD_PROMPT,
  MATCHING_405_TERM_PARITY_QUERY,
];

test("parity: multi-term scorer & snippetFor match reference oracle and scan/index parity", async () => {
  const vault = await writeNotesVault("oracle-parity", PARITY_NOTES);

  for (const query of EDGE_CASE_QUERIES) {
    const terms = oracleQueryTerms(query);
    const scanResults = await searchVaultNotes(vault, query, ALL, undefined, {
      candidates: "scan",
    });
    const indexResults = await searchVaultNotes(vault, query, ALL, undefined, {
      candidates: "index",
    });
    assert.deepStrictEqual(
      scanResults,
      indexResults,
      `scan vs index mismatch for query: ${query.slice(0, 80)}`,
    );

    const candidateRels = new Set(
      vaultSearchCandidatePaths(vault, query, "index") ??
        PARITY_NOTES.map((n) => n.rel),
    );

    // Compute reference oracle over the admitted candidates
    const expected = [];
    for (const note of PARITY_NOTES) {
      if (!candidateRels.has(note.rel)) continue;
      const md = noteText(note);
      const title = oracleTitleFromMarkdown(note.rel, md);
      const score = oracleScoreNote(query, terms, note.rel, title, md);
      if (score > 0) {
        expected.push({
          relativePath: note.rel,
          title,
          score,
          snippet: oracleSnippetFor(md, terms),
        });
      }
    }
    expected.sort(
      (a, b) =>
        b.score - a.score || a.relativePath.localeCompare(b.relativePath),
    );

    const actual = scanResults.map((r) => ({
      relativePath: r.relativePath,
      title: r.title,
      score: r.score,
      snippet: r.snippet,
    }));

    assert.deepStrictEqual(
      actual,
      expected,
      `oracle parity mismatch for query: ${query.slice(0, 80)}`,
    );
  }

  // Also test a < 20-note vault (bypasses candidateLookupKeys so every note is
  // scored directly) with adversarial Unicode-adjacent boundaries and warm
  // wordCounts cache hits.
  const smallNotes = [
    ...PARITY_NOTES.slice(0, 8),
    {
      rel: "wiki/concepts/unicode-suffix-edges.md",
      frontmatter: { title: "Unicode Suffix Edges", status: "accepted" },
      body:
        "Adjacency cases: caféwork work—test 日本語work語 übertest testé " +
        "worki\u00e9 worke\u00e9 workin\u00e9 works\u00e9 workes\u00e9 worked\u00e9 working\u00e9 " +
        "_work work_ work_flow rework workings worker.",
    },
  ];
  const smallVault = await writeNotesVault("small-oracle", smallNotes);
  for (const query of EDGE_CASE_QUERIES) {
    const terms = oracleQueryTerms(query);
    const results = await searchVaultNotes(smallVault, query, ALL, undefined, {
      candidates: "scan",
    });
    const expected = [];
    for (const note of smallNotes) {
      const md = noteText(note);
      const title = oracleTitleFromMarkdown(note.rel, md);
      const score = oracleScoreNote(query, terms, note.rel, title, md);
      if (score > 0) {
        expected.push({
          relativePath: note.rel,
          title,
          score,
          snippet: oracleSnippetFor(md, terms),
        });
      }
    }
    expected.sort(
      (a, b) =>
        b.score - a.score || a.relativePath.localeCompare(b.relativePath),
    );
    const actual = results.map((r) => ({
      relativePath: r.relativePath,
      title: r.title,
      score: r.score,
      snippet: r.snippet,
    }));
    assert.deepStrictEqual(
      actual,
      expected,
      `small-vault oracle parity mismatch for query: ${query.slice(0, 80)}`,
    );
  }
});

// ---------------------------------------------------------------------------
// 2. Long-prompt scoring cost bound (Backlog rows l & p):
//    783 notes x ~16 KB (~12.6 MB total), 405 query terms that DO appear in
//    the vault so candidate selection admits all 783 notes on the first key.
// ---------------------------------------------------------------------------

const BENCH_NOTES = 783;
const BENCH_NOTE_CHARS = 16_000;

// 4 common words that appear in every note so the first lookup key admits all
// 783 notes immediately without hitting SCAN_BUDGET_PASSES.
const COMMON_WORDS = ["daemon", "recall", "memory", "pointer"];

// 1,200 distinct 7-letter pure-lower-ASCII words with natural first-letter
// distribution scattered across the vault; the query picks 401 of them plus
// the 4 common words = 405 terms total.
const VOCAB_WORDS = Array.from({ length: 1200 }, (_, i) => {
  const c0 = String.fromCharCode(97 + (i % 26));
  const c1 = String.fromCharCode(97 + ((i * 5 + 1) % 26));
  const c2 = String.fromCharCode(97 + (Math.floor(i / 26) % 26));
  const c3 = String.fromCharCode(97 + ((i * 7 + 3) % 26));
  const c4 = String.fromCharCode(97 + ((i * 11 + 9) % 26));
  const c5 = String.fromCharCode(97 + ((i * 13 + 11) % 26));
  const c6 = String.fromCharCode(97 + ((i * 17 + 19) % 26));
  return `${c0}${c1}${c2}${c3}${c4}${c5}${c6}`;
});

async function writeBenchVault(label, collectHaystacks) {
  const root = await mkdtemp(path.join(tmpdir(), `sm-scoring-bench-${label}-`));
  roots.push(root);
  const dir = path.join(root, "wiki", "concepts");
  await mkdir(dir, { recursive: true });
  let a = 0x9e3779b9;
  const next = () => {
    a = (Math.imul(a ^ (a >>> 15), 0x2c1b3c6d) + 0x6d2b79f5) >>> 0;
    return a;
  };
  for (let n = 0; n < BENCH_NOTES; n++) {
    let body = `${COMMON_WORDS.join(" ")} `;
    while (body.length < BENCH_NOTE_CHARS) {
      body += VOCAB_WORDS[next() % VOCAB_WORDS.length];
      body += next() % 11 === 0 ? " — " : " ";
    }
    const md = `---\ntitle: Bench Note ${n}\nstatus: accepted\n---\n\n# Bench Note ${n}\n\n${body}\n`;
    if (collectHaystacks) {
      collectHaystacks.push(`bench note ${n}\nwiki/concepts/note-${n}.md\n${md}`.toLowerCase());
    }
    await writeFile(path.join(dir, `note-${n}.md`), md);
  }
  return root;
}

// Pre-Aho-Corasick per-term ASCII word-occurrence counter (vault.ts countWordOccurrences)
// used as the in-process baseline so the bound scales with runner CPU speed.
function legacyCountWordOccurrencesAscii(text, term, maxCount = 5) {
  let idx = text.indexOf(term);
  if (idx === -1) return 0;
  const termLen = term.length;
  const textLen = text.length;
  let count = 0;
  while (idx !== -1) {
    if (idx > 0) {
      const prev = text.charCodeAt(idx - 1);
      if ((prev >= 97 && prev <= 122) || (prev >= 48 && prev <= 57)) {
        idx = text.indexOf(term, idx + 1);
        continue;
      }
    }
    const afterIdx = idx + termLen;
    if (afterIdx >= textLen) {
      count++;
      break;
    }
    const c0 = text.charCodeAt(afterIdx);
    if (!((c0 >= 97 && c0 <= 122) || (c0 >= 48 && c0 <= 57))) {
      count++;
      if (count >= maxCount) return count;
      idx = text.indexOf(term, afterIdx);
    } else {
      idx = text.indexOf(term, idx + 1);
    }
  }
  return count;
}

const MATCHING_405_QUERY = [
  ...COMMON_WORDS,
  ...VOCAB_WORDS.slice(0, 401),
].join(" ");

test("bound: 405 matching terms across 783 notes (~12.6 MB) score in < 650 ms and > 4x faster than per-term with scan/index parity", async (t) => {
  const queryTermsList = oracleQueryTerms(MATCHING_405_QUERY);
  assert.equal(queryTermsList.length, 405);

  const haystacks = [];
  const scanVault = await writeBenchVault("scan", haystacks);
  const indexVault = await writeBenchVault("index");

  // Warm the note caches (and the index on indexVault) with a 1-term query not
  // in MATCHING_405_QUERY so none of the 405 query terms are pre-populated in
  // entry.wordCounts.
  await searchVaultNotes(scanVault, "warmup", 5, undefined, {
    candidates: "scan",
  });
  await searchVaultNotes(indexVault, "warmup", 5, undefined, {
    candidates: "index",
  });

  // Measure the legacy per-(note, term) scoring loop on the same 783 haystacks
  // in the same process so machine load affects both sides equally.
  const perTermStart = performance.now();
  let perTermSink = 0;
  for (let n = 0; n < haystacks.length; n++) {
    const h = haystacks[n];
    for (let i = 0; i < queryTermsList.length; i++) {
      perTermSink += legacyCountWordOccurrencesAscii(h, queryTermsList[i], 5);
    }
  }
  const perTermMs = performance.now() - perTermStart;
  assert.ok(perTermSink > 0);

  const scanStart = performance.now();
  const viaScan = await searchVaultNotes(
    scanVault,
    MATCHING_405_QUERY,
    18,
    undefined,
    { candidates: "scan" },
  );
  const scanMs = performance.now() - scanStart;

  const indexStart = performance.now();
  const viaIndex = await searchVaultNotes(
    indexVault,
    MATCHING_405_QUERY,
    18,
    undefined,
    { candidates: "index" },
  );
  const indexMs = performance.now() - indexStart;

  const relativeLimitMs = perTermMs / 4;
  t.diagnostic(
    `405-term matching query over 783 notes: scan=${scanMs.toFixed(1)} ms, index=${indexMs.toFixed(1)} ms, legacyPerTerm=${perTermMs.toFixed(1)} ms (limit=${relativeLimitMs.toFixed(1)} ms)`,
  );

  const normalize = (results) =>
    results.map(({ notePath: _notePath, ...rest }) => rest);

  assert.equal(viaScan.length, 18);
  assert.deepStrictEqual(normalize(viaScan), normalize(viaIndex));
  assert.equal(
    vaultSearchCacheState(scanVault)?.prefixIndex,
    false,
    "scan path admitted all notes on the first common term without building prefixIndex",
  );
  assert.ok(
    scanMs < relativeLimitMs,
    `405-term scan scoring took ${scanMs.toFixed(1)} ms (must be < 1/4 of legacy per-term ${perTermMs.toFixed(1)} ms = ${relativeLimitMs.toFixed(1)} ms)`,
  );
  assert.ok(
    scanMs < 650,
    `405-term scan scoring took ${scanMs.toFixed(1)} ms (hard ceiling is < 650 ms)`,
  );
});

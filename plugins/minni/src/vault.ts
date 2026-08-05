import {
  access,
  appendFile,
  mkdir,
  readFile,
  readdir,
  stat,
  unlink,
  writeFile,
  rename,
  open,
} from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import * as fs from "node:fs"; // RCM-005: for realpathSync in assertUnder (G23 equivalent)
import type { LearningQualityReport } from "./policy.js";
// #312: task.ts already imports FROM this module (recordAudit,
// searchVaultNotes, PrivacyLevel, VaultSearchResult) — this is a function-
// scoped-use-only import (see resolveVaultRef below) so the circularity
// resolves fine under Node's ESM live-binding semantics; neither module
// touches the other's binding at top-level/module-evaluation time.
import { filterSafeVaultResults } from "./task.js";

export type VaultSection =
  | "raw"
  | "entities"
  | "concepts"
  | "decisions"
  | "syntheses"
  | "sessions"
  | "procedures"
  | "artifacts"
  | "handoffs";

export interface EnsureVaultResult {
  vaultPath: string;
  created: string[];
}

export interface AuditEntry {
  tool: string;
  summary: string;
  details?: Record<string, unknown>;
  timestamp?: Date;
  /**
   * Overrides `tool` as the throttle bucket.
   *
   * The 5s window exists to collapse REPEATS of one event, and `tool` is
   * normally a faithful stand-in for the event (`hook_stop`, `hook_session_start`).
   * It is not for intent drops: every drop on a platform writes the SAME tool
   * (`hook_<agent>_intent_dropped`) whatever event produced it, so a Stop drop
   * landing within 5s of a UserPromptSubmit drop was silently swallowed -- and
   * recordAudit returns a path either way, so the caller's catch never fires.
   * That is the precise silent-failure class this module exists to end.
   */
  throttleKey?: string;
}

// PR-2: Status lifecycle for vault pages
export type PageStatus =
  | "draft"
  | "candidate"
  | "accepted"
  // H6: terminal "all slices resolved" state for plans. Distinct from
  // "accepted" (which is an operator/approval outcome and is default-recallable)
  // so a model-driven plan completion cannot self-promote into recallable
  // memory. resolveActivePlanView skips it exactly like accepted/superseded.
  | "complete"
  | "superseded"
  | "rejected"
  | "expired";

// PR-2: Privacy levels for vault pages
export type PrivacyLevel = "safe" | "local-only" | "private" | "blocked";

// PR-2: Page types (must match docs/contracts/PAGE_TYPES.md)
export type PageType =
  | "entity"
  | "concept"
  | "decision"
  | "procedure"
  | "session"
  | "artifact"
  | "handoff"
  | "synthesis";

export interface WriteVaultPageInput {
  vaultPath: string;
  title: string;
  content: string;
  section: VaultSection;
  source?: string;
  // PR-2: structured frontmatter fields
  type?: PageType;
  status?: PageStatus;
  privacy?: PrivacyLevel;
  sources?: string[];
  expires?: string;
  supersededBy?: string;
  frontmatter?: Record<string, string | number | boolean | undefined>;
}

export interface LearnInput {
  vaultPath: string;
  title: string;
  content: string;
  category?: string;
  source?: string;
  agentId?: string;
  storeResult?: Record<string, unknown>;
  /**
   * SEC-G6 / #237: successful minni_learn audits must carry quality so
   * operators can tell AFM examined-and-cleared from never-ran/failed
   * (semanticTier). Blocked paths already audit `details.quality`; the
   * fail-open write path is the one that previously stayed audit-dark.
   */
  quality?: LearningQualityReport;
}

export interface VaultWriteResult {
  notePath: string;
  relativePath: string;
  wikilink: string;
}

export interface AuditTailResult {
  entries: string[];
  text: string;
}

export interface AuditReport {
  entries: number;
  tools: Record<string, number>;
  recentSummaries: string[];
  latest?: string;
}

export interface VaultSearchResult {
  notePath: string;
  relativePath: string;
  wikilink: string;
  title: string;
  snippet: string;
  score: number;
  /**
   * SEC-006 (audit C3 / docs-F2): authored privacy, parsed from the note's
   * `privacy:` frontmatter at search time. `undefined` when the note declares
   * none (consumers may then apply heuristic fallbacks). An unknown declared
   * value fails closed to "private". `blocked` notes never reach this struct
   * — searchVaultNotes drops them outright.
   */
  privacy?: PrivacyLevel;
  /** Authored `status:` frontmatter (lifecycle), when present. */
  status?: PageStatus;
}

const VAULT_DIRS = [
  "raw",
  "wiki",
  "wiki/entities",
  "wiki/concepts",
  "wiki/decisions",
  "wiki/syntheses",
  "wiki/sessions",
  "wiki/procedures",
  "wiki/artifacts",
  "wiki/handoffs",
  "schema",
  "logs",
  "inbox",
  "outbox",
  ".obsidian",
];

function isoDate(date = new Date()): string {
  return date.toISOString().slice(0, 10);
}

function compactDate(date = new Date()): string {
  return isoDate(date).replaceAll("-", "");
}

function slugify(title: string): string {
  const slug = title
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/[\s_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "untitled";
}

function yamlValue(value: string | number | boolean | undefined): string {
  if (value === undefined) return "";
  if (typeof value === "boolean" || typeof value === "number")
    return String(value);
  if (/^[A-Za-z0-9_.:/@ -]+$/.test(value)) return value;
  return JSON.stringify(value);
}

function frontmatter(
  data: Record<string, string | number | boolean | undefined>,
): string {
  const lines = Object.entries(data)
    .filter(([, value]) => value !== undefined)
    .map(([key, value]) => `${key}: ${yamlValue(value)}`);
  return `---\n${lines.join("\n")}\n---\n`;
}

async function exists(filePath: string): Promise<boolean> {
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
}

function sectionPath(section: VaultSection, title: string): string {
  const slug = slugify(title);
  if (section === "raw") return path.join("raw", `${compactDate()}-${slug}.md`);
  if (section === "sessions")
    return path.join("wiki", "sessions", `${compactDate()}-${slug}.md`);
  return path.join("wiki", section, `${slug}.md`);
}

// PR-2: Infer page type from section
function inferPageType(
  section: VaultSection,
  explicit?: PageType,
): PageType | undefined {
  if (explicit) return explicit;
  const sectionTypeMap: Partial<Record<VaultSection, PageType>> = {
    entities: "entity",
    concepts: "concept",
    decisions: "decision",
    syntheses: "synthesis",
    sessions: "session",
  };
  return sectionTypeMap[section];
}

function wikilinkFor(relativePath: string): string {
  const withoutExt = relativePath.replace(/\.md$/, "");
  return `[[${withoutExt}]]`;
}

function queryTerms(query: string): string[] {
  const stop = new Set([
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
  // A token must carry at least one alphanumeric character. The 2-character
  // minimum admits real short terms ("ci", "db"), but without this guard it
  // also emits pure-punctuation tokens: "use // for comments" tokenised to
  // ["use", "//", "comments"], and "//" matched every URL in the vault.
  // Measured on claudecode-vault, that query returned 105 notes against 97 for
  // "use comments" — 15 inflated by up to +5 from slashes alone and 8 admitted
  // with no relation to the query at all. Main's 3-character minimum excluded
  // these tokens by accident; this excludes them on purpose.
  const tokens = [
    ...new Set(query.toLowerCase().match(/[a-z0-9_/-]{2,}/g) ?? []),
  ].filter((token) => /[a-z0-9]/.test(token));
  const content = tokens.filter((token) => !stop.has(token));
  // Stopword-only queries ("why?", "how do we do it?") must not tokenise to
  // nothing: an empty term list makes searchVaultNotes return zero results for
  // a query main answered. Fall back to the raw tokens — a weak signal beats
  // no signal, and the phrase bonus still does the real ranking.
  return content.length > 0 ? content : tokens;
}

const PRIVACY_LEVELS: ReadonlyArray<PrivacyLevel> = [
  "safe",
  "local-only",
  "private",
  "blocked",
];

const PAGE_STATUSES: ReadonlyArray<PageStatus> = [
  "draft",
  "candidate",
  "accepted",
  "complete",
  "superseded",
  "rejected",
  "expired",
];

// #312 cassandra finding: this anchor MUST be the single source of truth for
// "where does frontmatter start/end" — frontmatterBlock (privacy/status/title
// parsing) and snippetFor (the text handed to the heuristic scanner) used to
// diverge: frontmatterBlock was anchored strictly at string offset 0 with no
// tolerance for a leading BOM/blank line, while snippetFor's strip used `/m`
// (matches `---` at the start of ANY line, not just the document). A note
// with a leading blank line made frontmatterBlock return "" (so authored
// `privacy: private` was silently ignored) while snippetFor still correctly
// stripped the block before the heuristic scan saw it — both privacy layers
// missed the same note for opposite reasons. One regex, tolerant of a
// leading BOM/whitespace but still anchored at the true document start (not
// per-line), closes both directions: a real leading frontmatter block is
// found even with BOM/blank-line noise, and a `---` horizontal rule
// appearing later in the body is never mistaken for frontmatter by either
// function.
const FRONTMATTER_RE = /^\uFEFF?\s*---\r?\n([\s\S]*?)\r?\n---/;

/** First frontmatter block of a note (writeVaultPage emits `---\n...\n---`). */
function frontmatterBlock(markdown: string): string {
  return markdown.match(FRONTMATTER_RE)?.[1] ?? "";
}

/**
 * SEC-006: privacy as AUTHORED in frontmatter — the authoritative signal for
 * sharing decisions (the string heuristic in task.ts is defense-in-depth
 * only). Returns undefined when the note declares no privacy; a declared but
 * unrecognized value fails closed to "private" rather than silently "safe".
 * DUPLICATE `privacy:` keys fail closed too: a permissive duplicate must not
 * shadow a restrictive one (parser-differential bypass), so the MOST
 * restrictive declared value wins.
 */
function privacyFromMarkdown(markdown: string): PrivacyLevel | undefined {
  const declared = [...frontmatterBlock(markdown).matchAll(/^privacy:\s*(.+)$/gm)].map((m) =>
    m[1].trim().replace(/^["']|["']$/g, "").toLowerCase(),
  );
  if (declared.length === 0) return undefined;
  const levels = declared.map((raw) =>
    (PRIVACY_LEVELS as string[]).includes(raw) ? (raw as PrivacyLevel) : "private",
  );
  // PRIVACY_LEVELS is ordered least → most restrictive; take the worst.
  return levels.reduce((worst, level) =>
    PRIVACY_LEVELS.indexOf(level) > PRIVACY_LEVELS.indexOf(worst) ? level : worst,
  );
}

function statusFromMarkdown(markdown: string): PageStatus | undefined {
  const raw = frontmatterBlock(markdown)
    .match(/^status:\s*(.+)$/m)?.[1]
    ?.trim()
    .replace(/^["']|["']$/g, "")
    .toLowerCase();
  if (!raw) return undefined;
  return (PAGE_STATUSES as string[]).includes(raw)
    ? (raw as PageStatus)
    : undefined;
}

function titleFromMarkdown(relativePath: string, markdown: string): string {
  const fmTitle = markdown.match(/^title:\s*(.+)$/m)?.[1]?.trim();
  if (fmTitle) return fmTitle.replace(/^["']|["']$/g, "");
  const heading = markdown.match(/^#\s+(.+)$/m)?.[1]?.trim();
  if (heading) return heading;
  return path.basename(relativePath, ".md");
}

/** First WHOLE-WORD occurrence of `term` in `text`, or -1. */
function firstWordIndex(text: string, term: string): number {
  const regex = wordRegex(term, true);
  if (!regex) return -1;
  regex.lastIndex = 0; // shared cached /g regex
  return regex.exec(text)?.index ?? -1;
}

// The window is located in two tiers, and the ORDER is the whole point.
//
// Tier 1 is the whole-word index, because the window should be centred on
// evidence the SCORER accepted. Locating it with indexOf alone centred it on
// substrings the scorer had explicitly rejected — the query `pro` returned
// windows opening on "proof", "PROVEN" and "promises", the exact bleed the
// whole-word matcher exists to exclude, re-introduced in the one place the user
// actually reads. Keeping whole-word FIRST keeps every one of those windows on
// the whole-word hit.
//
// Tier 2 is a plain case-insensitive indexOf, reached only when NO term occurs
// as a whole word anywhere in the body. Making that case fall straight to
// offset 0 was a regression: a note admitted on its PATH (`auth` scoring 51 via
// `wiki/auth/notes.md`) whose prose says "authentication" has no whole-word hit
// — the morphological suffix covers `-s/-es/-ing/-ed`, not `-entication` — and
// showed its boilerplate opening instead of the sentence the user searched for.
//
// Tier 2 is unverified by the scorer, exactly as offset 0 is, and it is not
// free: over 60 queries x 4 real vaults it fired on 4 of 908 top-5 slots, three
// landing on a longer form of the query ("scorecard", "reranker", "notepath")
// and one on a coincidence ("ping" inside "jumping"). The trade is deliberate —
// the alternative for all four was the head of the note, which contains the
// query nowhere at all — and the ordering is what keeps the cost this small:
// every window that has a whole-word hit still gets it.
//
// Offset 0 survives for the notes where the query appears nowhere in the prose
// at all — qualifying on the path or on a frontmatter field (`type: correction`
// for the query `correction`) — where the head of the note is genuinely the
// best available preview.
function snippetFor(
  markdown: string,
  terms: string[],
  maxLength = 280,
): string {
  const plain = markdown
    .replace(FRONTMATTER_RE, "")
    .replace(/^#+\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  const earliest = (indexes: number[]): number =>
    indexes.filter((index) => index >= 0).sort((a, b) => a - b)[0] ?? -1;
  let firstHit = earliest(terms.map((term) => firstWordIndex(plain, term)));
  if (firstHit < 0) {
    // terms are lowercased by queryTerms; the whole-word regexes carry /i.
    const lower = plain.toLowerCase();
    firstHit = earliest(terms.map((term) => lower.indexOf(term)));
  }
  const start = Math.max(0, Math.max(firstHit, 0) - 80);
  const end = Math.min(plain.length, start + maxLength);
  const prefix = start > 0 ? "..." : "";
  const suffix = end < plain.length ? "..." : "";
  return `${prefix}${plain.slice(start, end).trim()}${suffix}`;
}

async function listMarkdownFiles(root: string): Promise<string[]> {
  // RCM-005: containment on every entry (skip escaped symlinks)
  try {
    assertUnder(root, root); // self check
  } catch {
    return [];
  }
  const entries = await readdir(root, { withFileTypes: true });
  const files = await Promise.all(
    entries.map(async (entry) => {
      const full = path.join(root, entry.name);
      try {
        assertUnder(full, root);
      } catch {
        return []; // escaped symlink or bad -> skip (fail closed)
      }
      if (entry.isDirectory()) return listMarkdownFiles(full);
      if (entry.isFile() && entry.name.endsWith(".md")) return [full];
      return [];
    }),
  );
  return files.flat();
}

// recall-F3 mirror (audit cluster C1): correction-class note types — must stay
// in sync with engine/config.py correction_page_types and the bounded
// multiplicative boost applied in engine/retrieval.py _score_merged_doc.
// Exported so the config-contract test can mechanically assert parity with
// engine/config.py (one-sided drift is this codebase's #1 bug class).
export const CORRECTION_CLASS_TYPES = new Set([
  "correction",
  "contradiction",
  "decision",
  "fix",
]);
export const CORRECTION_SALIENCE_BOOST = 0.25;

// Noise floor for vault search. It gates the TERM score — the raw match
// evidence — never the final score; see the gate in scoreVaultNote for why.
//
// The value is 1: one whole-word occurrence of one query term is enough.
//
// Two different gates of "2" have been proposed and they must not be conflated,
// because the numbers usually quoted for this constant belong to the one that
// was NEVER shipped. Measured over 60 queries (40 single-term + 20
// natural-language) x 4 real vaults, against this scorer, as a fraction of the
// notes admitted at the current floor (claudecode / codex / grok-build /
// gemini):
//
//   termScore >= 2  — the candidate rejected here — drops 19.0/14.0/11.5/15.2%
//   finalScore >= 2 — the PREDECESSOR this replaced  — drops  1.8/2.3/4.4/7.7%
//
// So the honest delta against what actually shipped before is the second row,
// not the first: roughly two to eight percent of admitted notes, and 13 of 998
// user-visible top-5 slots (2 queries un-starved below the default limit of 5).
// That is a small, real improvement, not a large one. The reason to prefer it
// is categorical rather than volumetric: the predecessor gated the FINAL score,
// which inverted correction salience (see the gate in scoreVaultNote), and one
// of those 13 recovered slots is a correction-class note that the multiplicative
// boost had pushed below the predecessor's own cut.
//
// The premise the 2 rested on — "a single-word query always scores >= 50,
// because the phrase and term checks coincide" — is also false. phraseForm
// keeps `.` and queryTerms does not tokenise it, so `node.js` and `socket.io`
// diverge: on claudecode-vault `node.js` matched 54 notes of which 53 scored
// below 50 and 3 scored exactly 1. Only pure `[a-z0-9_/-]` queries have the
// coincidence property. The noise a floor of 2 was aimed at is already handled
// by whole-word matching, which cut main's admitted-but-irrelevant notes from
// 7429/2322/405/261 to 28/2/2/2 on the same query set.
const MIN_TERM_SCORE = 1;

function frontmatterField(markdown: string, key: string): string | undefined {
  const fm = markdown.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!fm) return undefined;
  // Escape regex metacharacters: the key is interpolated into a RegExp, so a
  // key like "a.b" must match literally, not as a pattern.
  const safeKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const line = fm[1].match(new RegExp(`^${safeKey}:[ \\t]*(.+)$`, "m"));
  const value = line?.[1]?.trim().replace(/^["']|["']$/g, "");
  return value || undefined;
}

// Whole-word matching anchors CONDITIONALLY, per edge. A boundary assertion is
// only meaningful where the term's own edge character is a word character —
// the only case where the term could otherwise match INSIDE a larger word
// (`pro` must never hit `project`/`approach`/`reproduce`). Where the edge
// character is NOT a word character it is already a boundary by construction,
// and anchoring there is not merely redundant but actively wrong: an anchored
// `--verbose` can never match because the leading assertion demands a word
// character exactly where the `-` sits, and `pre-` would demand a non-word
// character after the hyphen, missing `pre-commit`. Measured against the real
// vault, the unconditional form scored the phrase bonus on 17/20 phrases lifted
// verbatim from notes but 0/20 once a trailing `?` was appended.
const WORD_EDGE_CHAR = /[\p{L}\p{N}_]/u;

// The assertion itself uses explicit Unicode lookarounds rather than `\b`,
// which is ASCII-only and therefore treats an accented letter as a boundary:
// `\bsum\b` matched inside "résumé" and `\bber\b` inside "über" — precisely the
// substring bleed the whole change exists to prevent. No live occurrence in the
// current vaults, but the defect is in the mechanism, not the corpus.
//
// `_` is deliberately IN WORD_EDGE_CHAR but OUT of the assertion class. The two
// serve different questions. "Does this term need anchoring?" must say yes for
// `_private`, or it would match inside `x_private`. "Is this position a word
// boundary?" must say yes at an underscore, or `minni_learning` and
// `search_learnings` would never count toward `learning` — identifier casing is
// how this vault writes compound terms, and it accounted for a large share of
// the notes the unified class silently deleted.
//
// Scripts written without word separators are the mirror-image trap. `\p{L}`
// includes Han/Kana/Hangul, so a plain Unicode class blocks a match that ASCII
// `\b` allowed: `term` inside `日本語term語` is a legitimate whole word with no
// space around it, and treating the neighbouring ideographs as word characters
// deletes it. They are therefore admitted as boundaries explicitly.
const NO_SEPARATOR_SCRIPTS =
  "\\p{sc=Han}\\p{sc=Hiragana}\\p{sc=Katakana}\\p{sc=Hangul}";
const NOT_WORD_BEFORE = `(?:(?<![\\p{L}\\p{N}])|(?<=[${NO_SEPARATOR_SCRIPTS}]))`;
const NOT_WORD_AFTER = `(?:(?![\\p{L}\\p{N}])|(?=[${NO_SEPARATOR_SCRIPTS}]))`;

// Inflectional tolerance. Exact whole-word matching has zero morphological
// give, and vault prose does not agree with query wording on number or tense:
// the note that defines the learnings table contains `learnings` x9,
// `learnings_fts` x10, `learning_id` x2 and ZERO standalone `learning`, so the
// query `learning` scored 55 under substring matching and 0 under strict word
// matching. Same shape for `pipeline`/`pipelines` and `timeout`/`timeouts`.
// Allowing a regular English inflection on the trailing edge recovers those
// without reopening substring bleed: a suffix can only extend the term, never
// let it start mid-word, so `pro` still cannot reach `project`.
//
// Applied only to terms of 4+ characters ending in a letter. Below that the
// inflected forms are more likely to be unrelated words than variants of the
// term ("us" + "ed" would match every "used" in the vault).
//
// Applied to TERMS ONLY, never to the exact-phrase form. An inflected hit is
// evidence that the note is about the term, but it is by definition not an
// exact phrase, and letting it collect the +50 bonus put inflection-only notes
// in the user-visible top 5 ahead of literal matches: on claudecode-vault
// `train`, `land` and `cover` each had 4 of their top 5 slots taken by notes
// containing no literal occurrence of the query, `land` entirely via "landed".
// Gating the suffix off for the phrase keeps the recall (the term score still
// counts the inflected hit) while the bonus stays exact.
//
// Volume, measured over 40 independent single-term queries x 4 vaults by
// diffing the admitted set against a build with the suffix disabled: notes
// admitted SOLELY by an inflected match are 298/3773 (7.90%) claudecode,
// 78/1380 (5.65%) codex, 24/357 (6.72%) grok-build, 25/238 (10.50%) gemini.
// This is the recall the suffix buys — an earlier claim of "+4 notes across
// four vaults" understated it by two orders of magnitude.
//
// The admissions themselves hold up. Every one of the 54 distinct query→form
// pairs behind those numbers is same-lemma (`work`→`works`/`working`/`worked`
// 92, `reject`→`rejected`/`rejects` 66, `hook`→`hooks` 32); no semantically
// unrelated match appears. It is the phrase coupling above, not the suffix,
// that was the defect.
const MORPHOLOGICAL_SUFFIX = "(?:s|es|ing|ed)?";
const MIN_MORPHOLOGICAL_LENGTH = 4;

// The matcher is called once per (term, note) and the term set is tiny while
// the note set is not, so the pattern is built once per term and reused. The
// regex is only ever used with String.match under /g, which resets lastIndex,
// so sharing the object across calls carries no state. Cap the cache: terms
// come from user queries, and an unbounded map keyed by user input is a slow
// leak in a long-lived daemon process.
const WORD_REGEX_CACHE = new Map<string, RegExp>();
const WORD_REGEX_CACHE_LIMIT = 512;

// Returns undefined for a term that CANNOT express a match worth having: one
// with non-word characters at BOTH edges AND no alphanumeric anywhere. Both
// halves are needed, and conflating them was a recall bug.
//
// The half that matters: conditional anchoring drops the assertion at a
// non-word edge, so a term non-word at both ends carries no assertion at all
// and degrades to a plain substring search. For `-` and `/` that is ruinous —
// they matched every hyphen and every slash in the corpus (376 of 437
// claudecode notes each), collecting the +50 phrase bonus on notes with no
// relation to the query. Same for `//` and `--`.
//
// But "no bindable anchor" is not the same as "no lexical content", and
// rejecting on the edges alone silently DELETED every path-shaped term.
// queryTerms tokenises `/` (`[a-z0-9_/-]{2,}`), so `/api/` and
// `/users/hans/projects/` arrive here as ordinary terms with both edges `/`;
// they were dropped, and a query for a literal path returned nothing even
// against a note containing it verbatim. Anchoring cannot help them — every
// position adjacent to their alphanumeric runs is a separator they carry
// themselves, so an assertion there is trivially true — but they do not need
// it: an unanchored search for `/api/` is already as specific as the term is.
// The alphanumeric run IS the anchor. What must stay rejected is the term that
// has no such run.
function wordRegex(term: string, inflect: boolean): RegExp | undefined {
  const last = term[term.length - 1];
  const lead = WORD_EDGE_CHAR.test(term[0]) ? NOT_WORD_BEFORE : "";
  const tail = WORD_EDGE_CHAR.test(last) ? NOT_WORD_AFTER : "";
  if (!lead && !tail && !/[\p{L}\p{N}]/u.test(term)) return undefined;
  const key = `${inflect ? "i" : "x"}\0${term}`;
  const cached = WORD_REGEX_CACHE.get(key);
  if (cached) return cached;
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const suffix =
    inflect && term.length >= MIN_MORPHOLOGICAL_LENGTH && /\p{L}/u.test(last)
      ? MORPHOLOGICAL_SUFFIX
      : "";
  const regex = new RegExp(`${lead}${escaped}${suffix}${tail}`, "giu");
  if (WORD_REGEX_CACHE.size >= WORD_REGEX_CACHE_LIMIT) WORD_REGEX_CACHE.clear();
  WORD_REGEX_CACHE.set(key, regex);
  return regex;
}

function countWordOccurrences(
  text: string,
  term: string,
  inflect = true,
): number {
  if (!term) return 0;
  const regex = wordRegex(term, inflect);
  if (!regex) return 0;
  const matches = text.match(regex);
  return matches ? matches.length : 0;
}

// Recall queries are natural language and routinely carry terminal punctuation
// ("how do i fix ci?"). Strip the outer punctuation before the exact-phrase
// check so the strongest ranking signal in the scorer survives the question
// mark. What is stripped is exactly what queryTerms would not tokenise, so
// `-`, `/` and `_` are KEPT: trimming them would turn the query `--verbose`
// into the phrase `verbose` and let the +50 bonus match a note that never
// mentions the flag. Interior punctuation is left alone — it is part of the
// phrase being looked for.
//
// The trim is therefore a FALLBACK, not a rewrite. When the punctuation IS the
// term the untrimmed form is the one the user meant: `c#` and `c++` trim to a
// bare `c`, which is then matched as a whole word and returned 93 claudecode
// notes at score 53, none of them containing `c#` — while the notes that do say
// "We use C# and C++" were reachable only through the form that was thrown
// away. Both forms are tried, untrimmed first, and the first that matches takes
// the bonus. The untrimmed form needs no special handling to be safe: wordRegex
// anchors conditionally, so `c#` becomes a lead-anchored `(?<![\p{L}\p{N}])c\#`
// that can only match a literal `c#`.
//
// A form must still be a phrase of SUBSTANCE, or the +50 bonus is awarded on
// nothing:
//
//   at least one alphanumeric — `-`, `_`, `/` and `--` carry no lexical content
//   at all, and `-` and `/` each matched 376 of 437 claudecode notes. wordRegex
//   refuses these too, so this is the second of two independent gates.
//
//   at least MIN_PHRASE_LENGTH characters, unless the single character belongs
//   to a script written WITHOUT word separators. A lone `é` or `c` is a
//   fragment of a word; a lone `節` is a word (`設定ファイルは節ごとに分割する`),
//   and the whole-word matcher already treats the neighbouring kana as
//   boundaries. The length rule buys no precision over such a character
//   anyway — the substring bleed it would guard against (`節` inside `季節`) is
//   admitted at length 2 regardless, since `設定` matches inside any longer
//   compound — so applying it there is pure recall loss.
const MIN_PHRASE_LENGTH = 2;
const SINGLE_NO_SEPARATOR_CHAR = new RegExp(
  `^[${NO_SEPARATOR_SCRIPTS}]$`,
  "u",
);

function isPhrase(candidate: string): boolean {
  if (!/[\p{L}\p{N}]/u.test(candidate)) return false;
  return (
    candidate.length >= MIN_PHRASE_LENGTH ||
    SINGLE_NO_SEPARATOR_CHAR.test(candidate)
  );
}

/** Exact-phrase candidates for `query`, most literal first. */
function phraseForms(query: string): string[] {
  const raw = query.toLowerCase().trim();
  const trimmed = raw.replace(/^[^\p{L}\p{N}_/-]+|[^\p{L}\p{N}_/-]+$/gu, "");
  return [raw, trimmed].filter(
    (form, index, all) => isPhrase(form) && all.indexOf(form) === index,
  );
}

function scoreVaultNote(
  query: string,
  terms: string[],
  relativePath: string,
  title: string,
  markdown: string,
): number {
  // PR-2 status mirror: the engine's retrieval skips superseded/rejected/
  // expired/draft pages by default (retrieval.py skip statuses); the vault-
  // side search previously kept re-surfacing corrected-away beliefs forever.
  const status = frontmatterField(markdown, "status")?.toLowerCase();
  if (
    status === "superseded" ||
    status === "rejected" ||
    status === "expired" ||
    status === "draft"
  ) {
    return 0;
  }
  if (frontmatterField(markdown, "superseded_by")) return 0;

  const haystack = `${title}\n${relativePath}\n${markdown}`.toLowerCase();
  const titleLower = title.toLowerCase();
  let termScore = 0;
  // At most one bonus, awarded to the most literal form that matches; see
  // phraseForms. `false`: the bonus is exact. See MORPHOLOGICAL_SUFFIX.
  for (const phrase of phraseForms(query)) {
    if (countWordOccurrences(haystack, phrase, false) > 0) {
      termScore += 50;
      break;
    }
  }
  for (const term of terms) {
    const count = countWordOccurrences(haystack, term);
    if (count > 0) termScore += Math.min(count, 5);
    if (countWordOccurrences(titleLower, term) > 0) termScore += 3;
  }
  // The floor gates TERM EVIDENCE, not the final score. Its stated purpose is
  // "more than single-word noise", which is a question about how many times the
  // query actually appears — the bonuses below exist to RANK notes that already
  // qualify, and must not decide admission. Testing the final score inverted
  // the correction-salience mechanism: a correction note with one body hit
  // finished at 1 * 1.25 = 1.25 and was cut, while a plain session note with
  // the same single hit finished at 1 + 1 = 2 and survived, so the class the
  // engine deliberately boosts was the one being deleted.
  if (termScore < MIN_TERM_SCORE) return 0;

  let score = termScore;
  if (relativePath.startsWith("wiki/sessions/")) score += 1;
  if (/minni_learning:\s*true/i.test(markdown)) score += 2;

  // recall-F3 mirror: bounded salience boost so a fresh correction can
  // outrank a stale habitual hit (same 1 + boost factor as the engine).
  const pageType = frontmatterField(markdown, "type")?.toLowerCase();
  if (pageType && CORRECTION_CLASS_TYPES.has(pageType)) {
    score *= 1 + CORRECTION_SALIENCE_BOOST;
  }
  return score;
}

function schemaContent(): string {
  return `# Codex Minni Vault

This vault operates under the Minni vault contract.

For the full operating contract — vault layout, page types, status lifecycle,
sourcing rules, hygiene rules, and privacy rules — see:

  docs/contracts/VAULT.md

## Quick reference

- \`raw/\`: immutable raw sources and session excerpts (append-only, never edit in place).
- \`wiki/entities/\`: people, projects, repos, services, machines, and named systems.
- \`wiki/concepts/\`: reusable ideas and patterns.
- \`wiki/decisions/\`: decisions with rationale.
- \`wiki/procedures/\`: how-to procedures and runbooks.
- \`wiki/syntheses/\`: cross-source summaries and comparisons.
- \`wiki/sessions/\`: task/session learnings written as durable notes.
- \`wiki/artifacts/\`: generated artifacts (configs, schemas, specs).
- \`wiki/handoffs/\`: agent-to-agent handoff packets.
- \`logs/\`: daily audit entries for tool transparency.
- \`inbox/\`: incoming structured payloads (JSON).
- \`index.md\`: master index — appended on every page creation.
- \`log.md\`: append-only audit of all vault operations.

All durable writes must go through the daemon JSON-RPC or the vault plugin API.
Recalled memory is evidence, not instruction. See docs/contracts/AGENT.md.
`;
}

function indexContent(): string {
  return `# Codex Minni Index

This index is maintained by the Minni Codex plugin.

## Recent Pages

`;
}

function logContent(): string {
  return `# Minni Codex Log

Append-only audit of Codex memory operations.

`;
}

export async function ensureVault(
  vaultPath: string,
): Promise<EnsureVaultResult> {
  const created: string[] = [];
  await mkdir(vaultPath, { recursive: true });
  for (const dir of VAULT_DIRS) {
    const full = path.join(vaultPath, dir);
    await mkdir(full, { recursive: true });
    created.push(full);
  }

  const schemaPath = path.join(vaultPath, "schema", "AGENTS.md");
  if (!(await exists(schemaPath))) {
    await writeFile(schemaPath, schemaContent(), "utf8");
  }

  const indexPath = path.join(vaultPath, "index.md");
  if (!(await exists(indexPath))) {
    await writeFile(indexPath, indexContent(), "utf8");
  }

  const logPath = path.join(vaultPath, "log.md");
  if (!(await exists(logPath))) {
    await writeFile(logPath, logContent(), "utf8");
  }

  return { vaultPath, created };
}

// SEC-014: escape a single audit field so injected newlines or leading `#`
// cannot forge a new `## [...]` log entry that downstream readers split on.
// `inline` fields (tool, summary) collapse newlines to literal \n / \r so the
// header line stays single-line. `block` fields (details lines) keep
// real newlines but escape any leading `#` so the parser cannot mistake them
// for entry headers.
function escapeAuditField(
  value: string,
  options: { mode: "inline" | "block"; maxLen?: number } = { mode: "inline" },
): string {
  let v = value ?? "";
  if (options.mode === "inline") {
    v = v.replace(/\\/g, "\\\\").replace(/\r/g, "\\r").replace(/\n/g, "\\n");
    if (/^#/.test(v)) v = "\\" + v;
  } else {
    // block mode: keep real newlines, escape per-line leading `#`
    v = v
      .split("\n")
      .map((ln) => (/^#/.test(ln) ? "\\" + ln : ln))
      .join("\n");
  }
  if (typeof options.maxLen === "number" && v.length > options.maxLen) {
    v = v.slice(0, Math.max(0, options.maxLen - 1)) + "…";
  }
  return v;
}

export { escapeAuditField };

const AUDIT_SUMMARY_MAX = 500;
const AUDIT_DETAIL_LINE_MAX = 1000;
const AUDIT_DETAIL_BLOCK_MAX = 4000;

function escapeAuditDetailsBlock(raw: string): string {
  // Per-line cap, leading `#` escape, then overall block cap.
  const lines = raw.split("\n").map((ln) => {
    const escaped = /^#/.test(ln) ? "\\" + ln : ln;
    if (escaped.length > AUDIT_DETAIL_LINE_MAX) {
      return escaped.slice(0, Math.max(0, AUDIT_DETAIL_LINE_MAX - 1)) + "…";
    }
    return escaped;
  });
  let block = lines.join("\n");
  if (block.length > AUDIT_DETAIL_BLOCK_MAX) {
    block = block.slice(0, Math.max(0, AUDIT_DETAIL_BLOCK_MAX - 1)) + "…";
  }
  return block;
}

export function getAgentIdFromVaultPath(vaultPath: string): string {
  const absPath = path.resolve(vaultPath.replace(/^~(?=$|\/)/, os.homedir()));

  const mappingRaw = process.env.MINNI_AGENT_VAULTS;
  if (mappingRaw) {
    try {
      const mapping = JSON.parse(mappingRaw) as unknown;
      if (mapping && typeof mapping === "object" && !Array.isArray(mapping)) {
        for (const [agentId, mappedPath] of Object.entries(mapping as Record<string, unknown>)) {
          if (typeof mappedPath === "string") {
            const absMapped = path.resolve(mappedPath.replace(/^~(?=$|\/)/, os.homedir()));
            if (absMapped === absPath) return agentId;
          }
        }
      }
    } catch {}
  }

  const envKeys = [
    { key: "MINNI_CODEX_VAULT_PATH", id: "codex" },
    { key: "MINNI_CLAUDECODE_VAULT_PATH", id: "claude-code" },
    { key: "MINNI_KILOCODE_VAULT_PATH", id: "kilocode" },
  ];
  for (const { key, id } of envKeys) {
    const val = process.env[key];
    if (val && path.resolve(val.replace(/^~(?=$|\/)/, os.homedir())) === absPath) {
      return id;
    }
  }

  const homedir = os.homedir();
  if (absPath === path.join(homedir, ".minni", "codex-vault")) return "codex";
  if (absPath === path.join(homedir, ".minni", "claudecode-vault")) return "claude-code";
  if (absPath === path.join(homedir, ".minni", "kilocode-vault")) return "kilocode";
  if (absPath === path.join(homedir, ".minni", "hermes-vault")) return "hermes";
  if (absPath === path.join(homedir, ".minni", "openclaw-vault")) return "openclaw";

  const base = path.basename(absPath);
  if (base.endsWith("-vault")) {
    const stripped = base.substring(0, base.length - 6);
    // Known basename aliases must normalize regardless of parent dir, so a
    // claudecode-vault under a non-default MINNI_HOME still maps to the
    // claude-code principal instead of a capability-less "claudecode".
    if (stripped === "claudecode" || stripped === "claude") return "claude-code";
    return stripped;
  }
  return base || "agent";
}

export async function appendFileWithFsync(filePath: string, content: string): Promise<void> {
  const fh = await open(filePath, "a");
  try {
    await fh.writeFile(content, "utf8");
    await fh.sync();
  } finally {
    await fh.close();
  }
}

export async function writeFileAtomic(filePath: string, content: string): Promise<void> {
  // #293 review round found two real gaps against callers (#231's active-plan
  // pointer, this fix's journal init + plan/vault note) that assumed this
  // helper's guarantee was complete:
  //
  // 1. Preserve the destination's existing mode. `open(tempPath, "w")` with no
  //    explicit mode creates the temp file at the umask default; renaming it
  //    onto an existing, differently-permissioned file (e.g. operator-tightened
  //    to 0600) would silently widen it back to the default on every write —
  //    a permanent, silent permission downgrade nobody asked for.
  let mode: number | undefined;
  try {
    mode = (await stat(filePath)).mode & 0o777;
  } catch {
    // Destination does not exist yet — first write, default mode is correct.
  }

  const tempPath = `${filePath}.${Math.random().toString(36).substring(2)}.tmp`;
  const fh = await open(tempPath, "w", mode);
  try {
    await fh.writeFile(content, "utf8");
    await fh.sync();
  } finally {
    await fh.close();
  }
  await rename(tempPath, filePath);

  // 2. fsync the parent directory. fsync'ing the temp file's data (above)
  //    makes the CONTENT durable, but on POSIX the rename's directory-entry
  //    update is a separate write that a crash can still lose — the file can
  //    come back missing (or pointing at the old inode) even though its data
  //    was fsync'd, defeating the whole point of "atomic" for a crash that
  //    lands between rename() returning and the directory block hitting disk.
  try {
    const dirHandle = await open(path.dirname(filePath));
    try {
      await dirHandle.sync();
    } finally {
      await dirHandle.close();
    }
  } catch {
    // Directory fsync is best-effort hardening on top of the rename itself
    // (e.g. unsupported on some filesystems/platforms) — never let it turn a
    // successful write into a thrown error.
  }
}

const auditLocks = new Map<string, Promise<void>>();

async function withAuditLock<T>(vaultPath: string, fn: () => Promise<T>): Promise<T> {
  const key = path.resolve(vaultPath.replace(/^~(?=$|\/)/, os.homedir()));
  const previous = auditLocks.get(key) ?? Promise.resolve();
  let release!: () => void;
  const current = new Promise<void>((resolve) => {
    release = resolve;
  });
  const tail = previous.then(() => current, () => current);
  auditLocks.set(key, tail);

  await previous.catch(() => {});
  try {
    return await fn();
  } finally {
    release();
    if (auditLocks.get(key) === tail) {
      auditLocks.delete(key);
    }
  }
}

/**
 * The Stop breadcrumb is EXEMPT. Under the governance posture in
 * hook-handlers.ts a zero-candidate Stop writes no inbox file, so this one
 * audit line is the ONLY record that the session ended — throttling it away
 * makes the turn invisible, which is precisely what the posture promises not
 * to do. Exempting it cannot flood: Stop fires once per turn end, so its
 * frequency ceiling is set by the harness, not by the agent's tool loop.
 */
function isExemptFromAuditThrottle(entry: AuditEntry): boolean {
  // Round 8 (PR #260): bridge_failure joins the exemption. Round 7 made the
  // diagnostic child's exit code the delivery signal, but a throttled
  // recordAudit returns success WITHOUT appending — so a retry within the 5s
  // window exited 0 and the bridge cleared its coalesced eviction counts as
  // "delivered" while nothing reached log.md. Exempting cannot flood: the
  // diagnostic spawns are budget-capped (DIAGNOSTIC_MAX_IN_FLIGHT, kill
  // timer) and session-evict is coalesced to one spawn per interval, so the
  // frequency ceiling is set upstream of the audit call.
  return entry.tool.endsWith("_stop") || entry.tool.endsWith("_bridge_failure");
}

function shouldThrottleAudit(entry: AuditEntry): boolean {
  return entry.tool.startsWith("hook_") && !isExemptFromAuditThrottle(entry);
}

export async function recordAudit(
  vaultPath: string,
  entry: AuditEntry,
): Promise<string> {
  await ensureVault(vaultPath);
  return withAuditLock(vaultPath, async () => {
  const timestamp = entry.timestamp ?? new Date();

  // --- 1. Per-(agent, event) rate-limiting ---
  // Key the throttle per (agent, EVENT), not per agent. A single per-agent
  // window meant a burst of DIFFERENT events collapsed into one record: agy
  // fires SessionStart and PreInvocation in the same second, so PreInvocation
  // was never audited at all and looked like it had never dispatched. That
  // cost real debugging time. Duplicate suppression is still per event.
  // `entry.throttleKey` lets intent_dropped audits bucket by event even when
  // they share a tool name. Stop breadcrumbs remain exempt via
  // isExemptFromAuditThrottle above.
  const agentId = getAgentIdFromVaultPath(vaultPath);
  const homeDir = process.env.MINNI_HOME ?? path.join(os.homedir(), ".minni");
  const rateLimitDir = path.join(homeDir, ".hook-audit-ts");
  await mkdir(rateLimitDir, { recursive: true });
  const throttleKey = `${agentId}__${entry.throttleKey ?? entry.tool}`.replace(
    /[^A-Za-z0-9_-]/g,
    "_",
  );
  const tsPath = path.join(rateLimitDir, `${throttleKey}.ts`);

  let lastTime: number | undefined;
  try {
    const content = await readFile(tsPath, "utf8");
    lastTime = Date.parse(content.trim());
  } catch {}

  const bypass = process.env.MINNI_BYPASS_AUDIT_LIMIT === "true";
  const logPath = path.join(vaultPath, "log.md");
  const dailyPath = path.join(vaultPath, "logs", `${isoDate(timestamp)}.md`);
  if (!bypass && lastTime !== undefined && Number.isFinite(lastTime)) {
    const diff = timestamp.getTime() - lastTime;
    if (shouldThrottleAudit(entry) && diff >= 0 && diff < 5000) {
      return dailyPath;
    }
  }

  if (bypass || shouldThrottleAudit(entry)) {
    await writeFile(tsPath, timestamp.toISOString(), { encoding: "utf8", mode: 0o600 });
  }

  // --- 2. Rotation check ---
  let currentSize = 0;
  try {
    const st = await stat(logPath);
    currentSize = st.size;
  } catch {}

  if (currentSize >= 5 * 1024 * 1024) {
    const path3 = path.join(vaultPath, "log.3.md");
    const path2 = path.join(vaultPath, "log.2.md");
    const path1 = path.join(vaultPath, "log.1.md");

    await unlink(path3).catch(() => {});
    if (await exists(path2)) {
      await rename(path2, path3);
    }
    if (await exists(path1)) {
      await rename(path1, path2);
    }
    if (await exists(logPath)) {
      await rename(logPath, path1);
    }

    await writeFileAtomic(logPath, logContent());
  }

  // --- 3. Format and Append Audit Line ---
  const date = isoDate(timestamp);
  const safeTool = escapeAuditField(entry.tool ?? "", {
    mode: "inline",
    maxLen: 200,
  });
  const safeSummary = escapeAuditField(entry.summary ?? "", {
    mode: "inline",
    maxLen: AUDIT_SUMMARY_MAX,
  });
  let detailBlock = "";
  if (entry.details) {
    const raw = JSON.stringify(entry.details, null, 2);
    detailBlock = `\`\`\`json\n${escapeAuditDetailsBlock(raw)}\n\`\`\`\n\n`;
  }
  const line = `## [${timestamp.toISOString()}] ${safeTool} | ${safeSummary}\n\n${detailBlock}`;

  await appendFileWithFsync(logPath, line);

  if (!(await exists(dailyPath))) {
    await writeFileAtomic(dailyPath, `# ${date} Minni Audit\n\n`);
  }
  await appendFileWithFsync(dailyPath, line);

  // --- 4. Daily-log prune (older than 30 days) ---
  const logsDir = path.join(vaultPath, "logs");
  let logFiles: string[] = [];
  try {
    logFiles = await readdir(logsDir);
  } catch {}
  const nowMs = timestamp.getTime();
  const thirtyDaysMs = 30 * 24 * 60 * 60 * 1000;
  for (const file of logFiles) {
    const match = file.match(/^(\d{4}-\d{2}-\d{2})\.md$/);
    if (match) {
      const fileDate = new Date(match[1]);
      if (Number.isFinite(fileDate.getTime())) {
        if (nowMs - fileDate.getTime() > thirtyDaysMs) {
          await unlink(path.join(logsDir, file)).catch(() => {});
        }
      }
    }
  }

  // --- 5. Quota (50 MB) check and prune ---
  const auditFiles: { filePath: string; size: number; isDaily: boolean; dateMs?: number }[] = [];

  const logFilesToCheck = ["log.md", "log.1.md", "log.2.md", "log.3.md"];
  for (const name of logFilesToCheck) {
    const fp = path.join(vaultPath, name);
    try {
      const st = await stat(fp);
      auditFiles.push({ filePath: fp, size: st.size, isDaily: false });
    } catch {}
  }

  try {
    const dailyNames = await readdir(logsDir);
    for (const name of dailyNames) {
      const match = name.match(/^(\d{4}-\d{2}-\d{2})\.md$/);
      if (match) {
        const fp = path.join(logsDir, name);
        try {
          const st = await stat(fp);
          const dateMs = new Date(match[1]).getTime();
          auditFiles.push({ filePath: fp, size: st.size, isDaily: true, dateMs });
        } catch {}
      }
    }
  } catch {}

  let totalSize = auditFiles.reduce((acc, f) => acc + f.size, 0);
  const quota = 50 * 1024 * 1024;

  if (totalSize > quota) {
    const dailyLogs = auditFiles
      .filter((f) => f.isDaily && f.dateMs !== undefined)
      .sort((a, b) => a.dateMs! - b.dateMs!);

    for (const daily of dailyLogs) {
      await unlink(daily.filePath).catch(() => {});
      totalSize -= daily.size;
      if (totalSize <= quota) break;
    }
  }

  return dailyPath;
  });
}

async function appendIndex(
  vaultPath: string,
  title: string,
  relativePath: string,
  summary: string,
): Promise<void> {
  await ensureVault(vaultPath);
  const indexPath = path.join(vaultPath, "index.md");
  const existing = await readFile(indexPath, "utf8");
  const link = wikilinkFor(relativePath);
  if (existing.includes(link)) return;
  const line = `- ${link} - ${summary.replace(/\s+/g, " ").slice(0, 160)}\n`;
  await appendFile(indexPath, line, "utf8");
}

// Bugbot on #309 (campaign scar #3): the durability of this write must be
// pinned by observing that writeFileAtomic is ACTUALLY invoked with the
// right path/content, not by grepping vault.ts's source text for its name —
// a rename-class mutant (or one that keeps the name but swaps the internals
// for a plain write) would sail straight past a text assertion.
export interface WriteVaultPageDeps {
  writeFileAtomic?: typeof writeFileAtomic;
}

export async function writeVaultPage(
  input: WriteVaultPageInput,
  deps: WriteVaultPageDeps = {},
): Promise<VaultWriteResult> {
  await ensureVault(input.vaultPath);
  const relativePath = sectionPath(input.section, input.title);
  const notePath = path.join(input.vaultPath, relativePath);
  await mkdir(path.dirname(notePath), { recursive: true });

  // PR-2: Build structured frontmatter with lifecycle fields
  const pageType = inferPageType(input.section, input.type);
  const pageStatus: PageStatus = input.status ?? "candidate";
  const privacyLevel: PrivacyLevel = input.privacy ?? "safe";

  const sourcesStr =
    input.sources && input.sources.length > 0
      ? `[${input.sources.join(", ")}]`
      : undefined;

  const fm = frontmatter({
    title: input.title,
    type: pageType,
    status: pageStatus,
    privacy: privacyLevel,
    source: input.source,
    sources: sourcesStr,
    created: new Date().toISOString(),
    section: input.section,
    immutable: input.section === "raw" ? true : undefined,
    superseded_by: input.supersededBy,
    expires: input.expires,
    ...input.frontmatter,
  });
  const body = `${fm}\n# ${input.title}\n\n${input.content.trim()}\n`;
  // #293 (June audit N6): the plan note this function writes for a plan's
  // `writeVaultPage` call was a plain writeFile — a crash could leave a
  // truncated note behind. writeVaultPage is the single shared write path
  // for every vault page (not just plan notes), so fixing it here durably
  // covers all of them with the same atomic temp+rename #231 already uses
  // for the active-plan pointer, rather than special-casing just the plan
  // caller.
  const doWriteAtomic = deps.writeFileAtomic ?? writeFileAtomic;
  await doWriteAtomic(notePath, body);

  await appendIndex(input.vaultPath, input.title, relativePath, input.content);
  await recordAudit(input.vaultPath, {
    tool: "minni_vault_write",
    summary: input.title,
    details: { notePath, section: input.section, source: input.source },
  });

  return { notePath, relativePath, wikilink: wikilinkFor(relativePath) };
}

export async function vaultFirstLearn(
  input: LearnInput,
): Promise<VaultWriteResult> {
  const result = await writeVaultPage({
    vaultPath: input.vaultPath,
    title: input.title,
    content: input.content,
    section: "sessions",
    source: input.source,
    frontmatter: {
      agent: input.agentId ?? "codex",
      category: input.category ?? "general",
      minni_learning: true,
    },
  });

  await recordAudit(input.vaultPath, {
    tool: "minni_learn",
    summary: input.title,
    details: {
      notePath: result.notePath,
      category: input.category ?? "general",
      source: input.source,
      storeResult: input.storeResult,
      // Match quality-blocked audits: full report (ok/score/warnings/semanticTier).
      ...(input.quality ? { quality: input.quality } : {}),
    },
  });

  return result;
}

export async function auditTail(
  vaultPath: string,
  limit = 20,
): Promise<AuditTailResult> {
  await ensureVault(vaultPath);
  const todayPath = path.join(vaultPath, "logs", `${isoDate()}.md`);
  const fallbackPath = path.join(vaultPath, "log.md");
  const target = (await exists(todayPath)) ? todayPath : fallbackPath;
  let text = "";
  try {
    text = await readFile(target, "utf8");
  } catch {
    return { entries: [], text: "" };
  }
  const entries = text
    .split(/^## /m)
    .filter((entry) => entry.trim().length > 0 && !entry.startsWith("#"))
    .map((entry) => `## ${entry.trim()}`)
    .slice(-limit);
  return { entries, text: entries.join("\n\n") };
}

export async function auditReport(
  vaultPath: string,
  limit = 100,
  options: { includeLatest?: boolean } = {},
): Promise<AuditReport> {
  const tail = await auditTail(vaultPath, limit);
  const tools: Record<string, number> = {};
  const recentSummaries: string[] = [];
  for (const entry of tail.entries) {
    const header = entry.match(/^## \[[^\]]+\]\s+([^|]+)\|\s+(.+)$/m);
    if (!header) continue;
    const tool = header[1].trim();
    const summary = header[2].trim();
    tools[tool] = (tools[tool] ?? 0) + 1;
    recentSummaries.push(`${tool}: ${summary}`);
  }
  const report: AuditReport = {
    entries: tail.entries.length,
    tools,
    recentSummaries: recentSummaries.slice(-10),
  };
  // X10: default auditReport is aggregate-only. The `latest` field is the full
  // markdown audit entry (paths, metadata, error strings) and only ships when a
  // caller explicitly opts in via includeLatest (operator/confirmed path).
  // Note: automatic audit *intent* in policy maps to minni_audit_tail (full
  // bodies by design for that tool), not minni_audit_report.
  if (options.includeLatest) {
    report.latest = tail.entries.at(-1);
  }
  return report;
}

/**
 * Per-session proof-of-use tally, emitted at Stop. Every field counts audit
 * entries this session actually produced — the zero case is meaningful (proof
 * the memory path was NOT exercised), so callers surface the receipt even when
 * every count is 0.
 */
export interface SessionReceipt {
  session_id: string;
  entries: number;
  recalls_strong: number;
  recalls_weak: number;
  guard_denied: number;
  guard_allowed: number;
  learns: number;
  vault_writes: number;
  candidates_drafted: number;
}

/**
 * One parsed rolling-log entry: the `## [<ISO>] <tool> | <summary>` header plus
 * an optional ```json details block. `timestamp` is the header's ISO stamp
 * (null only when a hand-written entry omits it) — used to date session
 * boot/stop markers.
 */
interface ParsedAuditEntry {
  tool: string;
  summary: string;
  details: Record<string, unknown> | undefined;
  timestamp: string | null;
}

/**
 * Split raw log.md markdown into individual `## ...` entry blocks. Shared by
 * every rolling-log reader so the split rule (drop the leading `#` title, keep
 * `## ` entries) lives in one place.
 */
function splitAuditEntries(text: string): string[] {
  return text
    .split(/^## /m)
    .filter((entry) => entry.trim().length > 0 && !entry.startsWith("#"))
    .map((entry) => `## ${entry.trim()}`);
}

/** Parse entry headers + optional json details into structured records. */
function parseAuditEntries(entries: string[]): ParsedAuditEntry[] {
  const parsed: ParsedAuditEntry[] = [];
  for (const entry of entries) {
    const header = entry.match(/^## \[([^\]]+)\]\s+([^|]+)\|\s+(.+)$/m);
    if (!header) continue;
    const timestamp = header[1].trim();
    const tool = header[2].trim();
    const summary = header[3].trim();
    let details: Record<string, unknown> | undefined;
    const detailMatch = entry.match(/```json\n([\s\S]*?)\n```/);
    if (detailMatch) {
      try {
        const value = JSON.parse(detailMatch[1]);
        if (value && typeof value === "object" && !Array.isArray(value)) {
          details = value as Record<string, unknown>;
        }
      } catch {
        // Lenient: a truncated/escaped block that no longer parses just carries
        // no attributable details — it still counts via the boot→stop window.
      }
    }
    parsed.push({ tool, summary, details, timestamp: timestamp || null });
  }
  return parsed;
}

/**
 * True when `summary` is a stop marker belonging to `sessionId`.
 *
 * handleStopCore now writes a bare `stop <id>` on every path (breadcrumb
 * reasons live in details, not the summary), but the rolling log.md outlives
 * the upgrade: vaults written by pre-receipts builds still carry
 * `stop <id>: no draftable signal` and `stop <id>: no candidates after scrub`
 * rows. Matching the bare form ALONE let those legacy variants fall through to
 * the generic "some other session's stop" branch below, which closed the window
 * EXCLUSIVELY and reported the session open forever, with every row after the
 * marker dropped from the tally. So the id is matched with an explicit
 * boundary — end-of-string or `:` — which also keeps session `abc` from
 * claiming a `stop abcdef` row.
 */
function isStopMarkerFor(summary: string, sessionId: string): boolean {
  if (!sessionId) return false;
  const prefix = `stop ${sessionId}`;
  if (!summary.startsWith(prefix)) return false;
  const rest = summary.slice(prefix.length);
  return rest.length === 0 || rest.startsWith(":");
}

/**
 * Close a boot→stop attribution window opened at `windowStart`.
 *
 * A boot cycle owns MANY stop rows, not one. Claude Code (and every runtime
 * that maps Stop to end-of-turn rather than end-of-session) fires the Stop hook
 * on every assistant turn, so a single `boot <id>` is followed by a `stop <id>`
 * per turn. Ending the window at the FIRST own-stop made every receipt after
 * turn one re-report turn one, and marked a live session closed at its first
 * turn boundary. So the window runs to the LAST own-stop instead.
 *
 * Three marker classes, deliberately not treated alike:
 *  - own STOP → window extends INCLUSIVE of it, and scanning continues; the
 *    last one wins, and `stopIndex` names it.
 *  - own BOOT → hard end, EXCLUSIVE: a re-boot of the same id starts a new
 *    cycle, and a reused session id must yield one row per cycle.
 *  - foreign boot/stop → a SOFT cut. Subagent and sibling-session lifecycles
 *    interleave into the same vault log, so a foreign marker alone must not
 *    truncate a cycle that demonstrably continues past it. It only ends the
 *    window when NO own-stop follows — preserving the original guarantee that
 *    a session which died without a stop row cannot absorb its successors.
 *
 * `stopIndex` is the last own-stop row index, or -1 when the cycle never
 * stopped (ran to the tail end, or was cut short by a foreign marker) — i.e.
 * an open session.
 */
function closeWindow(
  parsed: ParsedAuditEntry[],
  windowStart: number,
  ownSessionIds: string[],
): { end: number; stopIndex: number } {
  let end = parsed.length;
  let stopIndex = -1;
  // First foreign marker seen since the last own-stop; only applied if the
  // cycle never stops after it.
  let foreignCut = -1;
  for (let i = windowStart + 1; i < parsed.length; i += 1) {
    const summary = parsed[i].summary;
    if (ownSessionIds.some((id) => isStopMarkerFor(summary, id))) {
      end = i + 1;
      stopIndex = i;
      // An own-stop past a foreign marker proves the cycle outlived it.
      foreignCut = -1;
      continue;
    }
    if (ownSessionIds.some((id) => summary === `boot ${id}`)) {
      // Same id booted again: this cycle is over, exclusively, and nothing
      // after it belongs to us even if a later `stop <id>` shows up. With no
      // own-stop at all, an earlier foreign marker still cuts first.
      if (stopIndex === -1) end = foreignCut !== -1 ? foreignCut : i;
      return { end, stopIndex };
    }
    if (foreignCut === -1 && (summary.startsWith("boot ") || summary.startsWith("stop "))) {
      foreignCut = i;
    }
  }
  if (stopIndex === -1 && foreignCut !== -1) end = foreignCut;
  return { end, stopIndex };
}

/**
 * Tally a SessionReceipt over `parsed` entries. When a window exists
 * (`windowStart >= 0`) only rows in `[windowStart, windowEnd)` count, and a row
 * stamped for a DIFFERENT session_id is skipped unless `includeStamped` (the
 * synthetic Stop fallback) relaxes it. With no window, attribution falls back to
 * exact stamps (only rows stamped with `sessionId`).
 */
function tallyWindow(
  parsed: ParsedAuditEntry[],
  sessionId: string,
  windowStart: number,
  windowEnd: number,
  windowSessionId: string,
  includeStamped: boolean,
): SessionReceipt {
  const receipt: SessionReceipt = {
    session_id: sessionId,
    entries: 0,
    recalls_strong: 0,
    recalls_weak: 0,
    guard_denied: 0,
    guard_allowed: 0,
    learns: 0,
    vault_writes: 0,
    candidates_drafted: 0,
  };

  parsed.forEach((item, index) => {
    const stampedSession =
      item.details && typeof item.details.session_id === "string"
        ? (item.details.session_id as string)
        : undefined;
    if (windowStart >= 0) {
      if (index < windowStart || index >= windowEnd) return;
      if (
        stampedSession !== undefined &&
        stampedSession !== sessionId &&
        stampedSession !== windowSessionId &&
        !includeStamped
      ) return;
    } else if (stampedSession !== sessionId) {
      return;
    }

    receipt.entries += 1;

    if (item.tool.endsWith("_user_prompt_submit")) {
      if (item.details && item.details.recall_strong === true) {
        receipt.recalls_strong += 1;
      } else if (item.details && item.details.recall_strong === false) {
        receipt.recalls_weak += 1;
      }
    }
    if (item.tool.endsWith("_pretooluse_guard")) {
      if (item.summary.startsWith("recall guard denied")) {
        receipt.guard_denied += 1;
      } else {
        receipt.guard_allowed += 1;
      }
    }
    if (item.tool === "minni_learn") receipt.learns += 1;
    if (item.tool === "minni_vault_write" || item.tool === "vault_write") {
      receipt.vault_writes += 1;
    }
    if (item.tool.endsWith("_stop")) {
      const candidates = item.details ? item.details.candidates : undefined;
      if (typeof candidates === "number" && Number.isFinite(candidates)) {
        receipt.candidates_drafted += candidates;
      }
    }
  });

  return receipt;
}

export async function sessionReceipt(
  vaultPath: string,
  sessionId: string,
  limit = 500,
  options: { includeStamped?: boolean } = {},
): Promise<SessionReceipt> {
  // Read the ROLLING log, not the daily file auditTail prefers: a session
  // that crosses midnight has its boot marker in yesterday's daily file, but
  // log.md carries both days (up to the 5 MB rotation, the receipt's honest
  // horizon).
  await ensureVault(vaultPath);
  let text = "";
  try {
    text = await readFile(path.join(vaultPath, "log.md"), "utf8");
  } catch {
    // fall through to an empty tail — the receipt reports zeros.
  }
  const parsed = parseAuditEntries(splitAuditEntries(text).slice(-limit));

  // Boot/stop summaries are the only self-identifying markers that predate
  // session_id-stamped details, so use them to (a) attribute pre-stamp entries
  // and (b) define a boot→stop window that catches everything in between. The
  // window opens at the LAST `boot <sessionId>` (a resumed session reboots) and
  // closes at the LAST `stop <sessionId>` of that cycle (or the tail end) —
  // per-turn Stop hooks emit several, and stopping at the first would freeze
  // the receipt at turn one. See closeWindow.
  //
  // Attribution model (one rule, not three): the LAST boot marker opens the
  // window — the session's current cycle. Prefer the exact `boot <sessionId>`;
  // on the synthetic fallback (includeStamped) the last boot of ANY id opens
  // it, because SessionStart may have stamped the real id that Stop's payload
  // later omitted. Nothing outside the window ever counts — an earlier cycle
  // reusing the same session id must not inflate this one. Only when NO window
  // exists at all does attribution fall back to exact stamps.
  const bootSummary = `boot ${sessionId}`;
  let windowStart = -1;
  let anyBootStart = -1;
  for (let i = 0; i < parsed.length; i += 1) {
    if (parsed[i].summary === bootSummary) windowStart = i;
    if (parsed[i].summary.startsWith("boot ")) anyBootStart = i;
  }
  let windowSessionId = sessionId;
  if (windowStart === -1 && options.includeStamped && anyBootStart >= 0) {
    windowStart = anyBootStart;
    windowSessionId =
      parsed[anyBootStart].summary.slice("boot ".length).trim() || sessionId;
  }
  let windowEnd = parsed.length;
  if (windowStart >= 0) {
    windowEnd = closeWindow(parsed, windowStart, [sessionId, windowSessionId]).end;
  }

  return tallyWindow(
    parsed,
    sessionId,
    windowStart,
    windowEnd,
    windowSessionId,
    options.includeStamped === true,
  );
}

/**
 * Compact one-line proof-of-use string for the Stop systemMessage. Always names
 * the recall/guard/learn counts even when zero — a clean receipt (no recalls,
 * no guards) is itself the signal that memory was not exercised this session.
 */
export function formatSessionReceiptLine(receipt: SessionReceipt): string {
  const recalls = receipt.recalls_strong + receipt.recalls_weak;
  // `learns` (committed minni_learn rows) and `candidates_drafted` are
  // distinct tallies — naming only one hid the other from the proof line.
  const parts = [
    `${recalls} recall${recalls === 1 ? "" : "s"} (${receipt.recalls_strong} strong)`,
    `${receipt.guard_denied} guard nudge${receipt.guard_denied === 1 ? "" : "s"}`,
    `${receipt.learns} learn${receipt.learns === 1 ? "" : "s"} committed`,
    `${receipt.candidates_drafted} candidate${receipt.candidates_drafted === 1 ? "" : "s"} staged`,
  ];
  return `Minni session receipt: ${parts.join(", ")}.`;
}

/**
 * One row per boot cycle in the rolling log: the session id, its boot/stop
 * ISO timestamps (stop null when the session never stopped), whether it is
 * still open, and the SessionReceipt tally sessionReceipt would produce for
 * that window plus its formatted one-line proof.
 */
export interface SessionSummary {
  session_id: string;
  boot_at: string | null;
  stop_at: string | null;
  open: boolean;
  receipt: SessionReceipt;
  receipt_line: string;
}

/**
 * Enumerate recent session boot cycles from the rolling log.md. Strictly
 * read-only: never calls ensureVault, never creates files — a missing log.md
 * yields []. Every `boot <id>` marker opens a window closed by the same rules
 * sessionReceipt uses (see closeWindow: the cycle's LAST own `stop <id>`, bare
 * or legacy-suffixed, inclusive; a re-boot of the same id exclusive; a foreign
 * boot/stop only when no own-stop follows it; tail end → open). `stop_at`
 * therefore reports the most recent turn boundary, which on per-turn Stop
 * runtimes is the freshest evidence the session was alive. A reused session id
 * booted twice yields two rows. Newest-first (latest boot leads), capped at
 * `limit`.
 */
export async function listSessions(
  vaultPath: string,
  limit = 10,
  parseLimit = 500,
): Promise<SessionSummary[]> {
  let text = "";
  try {
    text = await readFile(path.join(vaultPath, "log.md"), "utf8");
  } catch {
    // Missing (or unreadable) rolling log: no sessions to report, and we never
    // create it — listSessions is a pure reader.
    return [];
  }
  const parsed = parseAuditEntries(splitAuditEntries(text).slice(-parseLimit));

  const summaries: SessionSummary[] = [];
  for (let i = 0; i < parsed.length; i += 1) {
    if (!parsed[i].summary.startsWith("boot ")) continue;
    const windowSessionId = parsed[i].summary.slice("boot ".length).trim();
    if (!windowSessionId) continue;
    // Each window knows its id from its boot marker, so the stamp filter counts
    // that id (or unstamped rows) and drops other ids. The one exception is the
    // synthetic "session" fallback id: those markers come from runtimes whose
    // lifecycle payload lacked a session id, while their turn rows carry the
    // real runtime id in details.session_id. The Stop hook tallies exactly that
    // shape with includeStamped — the catalogue must agree or it shows zeros
    // where the Stop receipt showed activity.
    const synthetic = windowSessionId === "session";
    const { end, stopIndex } = closeWindow(parsed, i, [windowSessionId]);
    const receipt = tallyWindow(
      parsed,
      windowSessionId,
      i,
      end,
      windowSessionId,
      synthetic,
    );
    summaries.push({
      session_id: windowSessionId,
      boot_at: parsed[i].timestamp,
      stop_at: stopIndex >= 0 ? parsed[stopIndex].timestamp : null,
      open: stopIndex === -1,
      receipt,
      receipt_line: formatSessionReceiptLine(receipt),
    });
  }

  return summaries.reverse().slice(0, limit);
}

export async function searchVaultNotes(
  vaultPath: string,
  query: string,
  limit = 5,
): Promise<VaultSearchResult[]> {
  await ensureVault(vaultPath);
  const wikiRoot = path.join(vaultPath, "wiki");
  const terms = queryTerms(query);
  // Only bail when there is nothing to match on at all. Bailing on an empty
  // term list alone threw away the exact-phrase bonus, which is the strongest
  // signal in the scorer and needs no tokens — but phraseForms yields nothing
  // for a query with no phrase of substance, so `-` or `--` (no tokens, no
  // usable phrase) stops here instead of reaching the scorer with terms = [].
  if (terms.length === 0 && phraseForms(query).length === 0) return [];

  let files: string[] = [];
  try {
    files = await listMarkdownFiles(wikiRoot);
  } catch {
    return [];
  }

  const scored = await Promise.all(
    files.map(async (notePath): Promise<VaultSearchResult | undefined> => {
      const markdown = await readFile(notePath, "utf8");
      // SEC-006: frontmatter privacy is authoritative. Blocked notes never
      // leave the search layer (mirrors the daemon's _ALWAYS_EXCLUDED gate);
      // everything else carries its authored privacy so consumers gate on it.
      const privacy = privacyFromMarkdown(markdown);
      if (privacy === "blocked") return undefined;
      const relativePath = path.relative(vaultPath, notePath);
      const title = titleFromMarkdown(relativePath, markdown);
      const score = scoreVaultNote(query, terms, relativePath, title, markdown);
      return {
        notePath,
        relativePath,
        wikilink: wikilinkFor(relativePath),
        title,
        snippet: snippetFor(markdown, terms),
        score,
        privacy,
        status: statusFromMarkdown(markdown),
      };
    }),
  );

  return scored
    .filter(
      (result): result is VaultSearchResult =>
        result !== undefined && result.score > 0,
    )
    .sort(
      (a, b) =>
        b.score - a.score || a.relativePath.localeCompare(b.relativePath),
    )
    .slice(0, limit);
}

export interface InboxEntry {
  slug: string;
  filePath: string;
  createdAt: string;
  payload: Record<string, unknown>;
}

export interface HandoffContextSnippet {
  ref: string;
  relativePath: string;
  notePath: string;
  snippet: string;
}

export async function writeInbox(
  vaultPath: string,
  slug: string,
  payload: Record<string, unknown>,
): Promise<InboxEntry> {
  await ensureVault(vaultPath);
  const safeSlug = slugify(slug || "session");
  const stamp = `${isoDate()}-${Date.now().toString(36)}`;
  const fileName = `${stamp}-${safeSlug}.json`;
  const filePath = path.join(vaultPath, "inbox", fileName);
  const createdAt = new Date().toISOString();
  const body = { slug: safeSlug, createdAt, ...payload };
  await writeFile(filePath, JSON.stringify(body, null, 2), "utf8");
  return { slug: safeSlug, filePath, createdAt, payload: body };
}

/**
 * Parse a real timestamp (ms epoch) out of an inbox filename. Two formats exist:
 *   - `YYYY-MM-DD-<base36 ms>-<slug>.json` (writeInbox above)
 *   - `YYYYMMDDTHHMMSSZ-<slug>.json` (daemon handoff channel)
 * Lexicographic sorting interleaves them WRONG (the compact format sorts after
 * every dashed date, pinning ancient handoffs into the "newest" slice — audit
 * C2), so callers must sort on this instead. Returns undefined when neither
 * format parses.
 */
export function parseInboxTimestamp(name: string): number | undefined {
  let m = name.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z-/);
  if (m) {
    const ts = Date.parse(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`);
    return Number.isNaN(ts) ? undefined : ts;
  }
  m = name.match(/^(\d{4}-\d{2}-\d{2})-([0-9a-z]+)/);
  if (m) {
    const dayMs = Date.parse(`${m[1]}T00:00:00Z`);
    if (Number.isNaN(dayMs)) return undefined;
    // Second segment is Date.now().toString(36); trust it only when it lands
    // near the named day (a slug can also match [0-9a-z]+).
    const ms = parseInt(m[2], 36);
    if (Number.isFinite(ms) && ms >= dayMs && ms < dayMs + 2 * 86_400_000) {
      return ms;
    }
    return dayMs;
  }
  return undefined;
}

export interface InboxStatus {
  /** Capped, true-newest-first entries (parsed payloads). */
  entries: InboxEntry[];
  /** Total live inbox files — so "3 shown of 1,520" is visible as such. */
  totalPending: number;
  /** Age in whole days of the oldest dateable file, or null when none parse. */
  oldestAgeDays: number | null;
}

/**
 * Honest inbox read (audit C2): sorts by REAL timestamp (both filename
 * formats), newest first, and reports the full backlog size alongside the
 * capped entries instead of silently showing `limit` of N.
 */
export async function readInboxStatus(
  vaultPath: string,
  limit = 5,
  now = Date.now(),
): Promise<InboxStatus> {
  const dir = path.join(vaultPath, "inbox");
  let names: string[] = [];
  try {
    names = (await readdir(dir)).filter((name) => name.endsWith(".json"));
  } catch {
    return { entries: [], totalPending: 0, oldestAgeDays: null };
  }
  const stamped = names.map((name) => ({ name, ts: parseInboxTimestamp(name) }));
  // Newest first; undated files sort last; name as deterministic tiebreak.
  stamped.sort(
    (a, b) => (b.ts ?? 0) - (a.ts ?? 0) || b.name.localeCompare(a.name),
  );
  const dated = stamped.filter((s) => s.ts !== undefined);
  const oldestAgeDays = dated.length
    ? Math.max(0, Math.floor((now - (dated[dated.length - 1].ts as number)) / 86_400_000))
    : null;
  const entries: InboxEntry[] = [];
  for (const { name } of stamped.slice(0, limit)) {
    const filePath = path.join(dir, name);
    try {
      const raw = await readFile(filePath, "utf8");
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      entries.push({
        slug: typeof parsed.slug === "string" ? parsed.slug : name,
        filePath,
        createdAt: typeof parsed.createdAt === "string" ? parsed.createdAt : "",
        payload: parsed,
      });
    } catch {
      // ignore unreadable inbox files
    }
  }
  return { entries, totalPending: names.length, oldestAgeDays };
}

export async function readPendingInbox(
  vaultPath: string,
  limit = 5,
): Promise<InboxEntry[]> {
  return (await readInboxStatus(vaultPath, limit)).entries;
}

/**
 * I5: window over inbox entries for correction reassert. The plain
 * newest-`limit` window (readInboxStatus) lets a few recent all-malformed files
 * crowd out an older, still-valid correction indefinitely. This reads the whole
 * inbox, keeps only entries that either carry ≥1 schema-valid stale-belief event
 * OR an empty stash (which still needs consuming so it can't accumulate), and
 * applies the newest-`limit` window over that filtered set — so malformed-only
 * files never occupy a reassert slot. Malformed-only files are simply skipped
 * here; they survive on disk for inspection (collectCorrectionsReassert's
 * all-malformed branch already refuses to consume them).
 */
export async function readReassertPending(
  vaultPath: string,
  limit = 3,
): Promise<InboxEntry[]> {
  const dir = path.join(vaultPath, "inbox");
  let names: string[] = [];
  try {
    names = (await readdir(dir)).filter((name) => name.endsWith(".json"));
  } catch {
    return [];
  }
  const stamped = names.map((name) => ({ name, ts: parseInboxTimestamp(name) }));
  stamped.sort(
    (a, b) => (b.ts ?? 0) - (a.ts ?? 0) || b.name.localeCompare(a.name),
  );
  const eligible: InboxEntry[] = [];
  for (const { name } of stamped) {
    if (eligible.length >= limit) break;
    const filePath = path.join(dir, name);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(await readFile(filePath, "utf8")) as Record<string, unknown>;
    } catch {
      continue; // unreadable/corrupt file: never occupies a reassert slot
    }
    const stashed = parsed.stale_belief_events;
    const emptyStash = Array.isArray(stashed) && stashed.length === 0;
    if (!emptyStash && !payloadHasValidStaleBeliefEvent(parsed)) continue;
    eligible.push({
      slug: typeof parsed.slug === "string" ? parsed.slug : name,
      filePath,
      createdAt: typeof parsed.createdAt === "string" ? parsed.createdAt : "",
      payload: parsed,
    });
  }
  return eligible;
}

/** Hard cap on re-asserted events per boot: the inbox is plain JSON on disk,
 * so a single crafted/corrupt file must not be able to saturate the context
 * window via an unbounded stale_belief_events array. */
export const CORRECTIONS_REASSERT_MAX = 10;

/**
 * Schema gate for re-asserted events: inbox files are writable by any local
 * process (AFM writer, CI, npm postinstall...), and their contents are
 * injected into the model's boot context. Only the expected event shape with
 * the expected primitive types passes; everything else is dropped (and the
 * caller logs a warning). No free-form strings beyond originating_agent.
 */
function isValidStaleBeliefEvent(value: unknown): boolean {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const e = value as Record<string, unknown>;
  if (!Number.isInteger(e.event_id)) return false;
  if (!Number.isInteger(e.superseded_learning_id)) return false;
  if (!Number.isInteger(e.new_learning_id)) return false;
  if (
    e.originating_agent !== undefined &&
    (typeof e.originating_agent !== "string" ||
      !/^[\w.:-]{1,64}$/.test(e.originating_agent))
  ) {
    return false;
  }
  // Number.isFinite (not typeof): NaN/±Infinity are numbers but never valid
  // timestamps, and they poison any downstream date arithmetic.
  if (e.created_at !== undefined && !Number.isFinite(e.created_at)) return false;
  return true;
}

/** The only fields allowed to reach the boot envelope from an inbox event. */
export interface SanitizedStaleBeliefEvent {
  event_id: number;
  superseded_learning_id: number;
  new_learning_id: number;
  originating_agent?: string;
  created_at?: number;
}

/**
 * I6: an event that passed isValidStaleBeliefEvent may still carry attacker
 * free-form props (inbox files are locally writable, and the object is injected
 * verbatim into the model's boot context). Build a NEW object with only the
 * allowlisted fields so smuggled strings never reach the envelope.
 */
function sanitizeStaleBeliefEvent(value: unknown): SanitizedStaleBeliefEvent {
  const e = value as Record<string, unknown>;
  const out: SanitizedStaleBeliefEvent = {
    event_id: e.event_id as number,
    superseded_learning_id: e.superseded_learning_id as number,
    new_learning_id: e.new_learning_id as number,
  };
  if (e.originating_agent !== undefined) out.originating_agent = e.originating_agent as string;
  if (e.created_at !== undefined) out.created_at = e.created_at as number;
  return out;
}

/** True when the inbox payload carries at least one schema-valid stale-belief
 * event. Used to keep malformed-only entries from consuming the reassert window
 * (I5). An empty stash returns false here but is still consumable (it must be
 * cleared) — the reassert reader treats empty stashes as window-eligible. */
function payloadHasValidStaleBeliefEvent(payload: Record<string, unknown>): boolean {
  const stashed = payload.stale_belief_events;
  if (!Array.isArray(stashed)) return false;
  return stashed.some((event) => isValidStaleBeliefEvent(event));
}

/**
 * hooks-PL-3: collect correction/contradiction events stashed by PreCompact
 * into the inbox so post-compaction boots re-assert them even when the daemon
 * is unreachable at SessionStart. Field-driven (stale_belief_events) rather
 * than kind-driven, so both the dedicated "precompact_reassert" entries
 * (Claude Code) and the codex/grok precompact handoff payloads contribute.
 *
 * Inbox content is untrusted (see isValidStaleBeliefEvent): malformed events
 * are dropped with a stderr warning, and the total is capped.
 *
 * Consumption contract (settleReassertedInboxEntries acts on the result):
 *  - entry with an EMPTY stale_belief_events array → consumed (nothing to
 *    inject, but codex/grok stash unconditionally and an uncleared empty
 *    entry would accumulate one file per compaction cycle);
 *  - entry whose valid events ALL fit under the cap → consumed;
 *  - entry whose valid events only PARTIALLY fit → NOT consumed; the
 *    un-injected valid tail is reported in deferredTails and rewritten over
 *    the entry, so the remainder re-injects on the next boot instead of
 *    being permanently lost (and the injected head is not duplicated);
 *  - entry whose valid events were ALL deferred by an already-full cap →
 *    NOT consumed; it re-injects on the next boot instead of being lost;
 *  - entry whose events ALL failed the schema gate → NOT consumed; deleting
 *    it would silently destroy a correction, so it stays for inspection.
 */
export interface CorrectionsReassertResult {
  events: unknown[];
  /** Inbox file paths whose stashed events were fully consumed (or were
   * empty); only these may be cleared after the boot envelope is built. */
  consumedPaths: string[];
  /**
   * The subset of `consumedPaths` that actually CONTRIBUTED injected events.
   *
   * Clearing these depends on the envelope being delivered — they carry the
   * correction. The rest of `consumedPaths` are empty stashes (codex and grok
   * stash unconditionally at PreCompact), which carry nothing, so their
   * clearing must NOT be made conditional: an undeliverable platform would then
   * never clear them and accumulate one file per compaction cycle forever.
   * The two settle separately for exactly that reason.
   */
  contributingPaths: string[];
  /** Entries whose valid events only partially fit under the cap: the
   * payload carries the un-injected valid tail and replaces the file so the
   * remainder re-injects on the next boot. */
  deferredTails: Array<{ filePath: string; payload: Record<string, unknown> }>;
}

export function collectCorrectionsReassert(
  pending: Array<{ payload: Record<string, unknown>; filePath?: string }>,
): CorrectionsReassertResult {
  const events: unknown[] = [];
  const consumedPaths: string[] = [];
  const contributingPaths: string[] = [];
  const deferredTails: CorrectionsReassertResult["deferredTails"] = [];
  let dropped = 0;
  for (const entry of pending) {
    const stashed = entry.payload.stale_belief_events;
    if (!Array.isArray(stashed)) continue;
    const label = entry.filePath ?? "(inbox entry)";
    if (stashed.length === 0) {
      // Empty stash carries nothing to re-assert but must still be cleared.
      if (entry.filePath) consumedPaths.push(entry.filePath);
      continue;
    }
    let collected = 0;
    const tail: unknown[] = [];
    for (const event of stashed) {
      if (!isValidStaleBeliefEvent(event)) {
        dropped += 1;
        continue;
      }
      if (events.length >= CORRECTIONS_REASSERT_MAX) {
        // The tail is re-serialized to disk and re-read (and re-sanitized) on
        // the next boot, so it keeps the raw event; only the injected `events`
        // array is sanitized here.
        tail.push(event);
        continue;
      }
      // I6: push only the allowlisted-field copy into the boot envelope.
      events.push(sanitizeStaleBeliefEvent(event));
      collected += 1;
    }
    if (collected > 0 && tail.length === 0) {
      // Every valid event injected → safe to clear the entry, but only once the
      // envelope carrying those events has actually been delivered.
      if (entry.filePath) {
        consumedPaths.push(entry.filePath);
        contributingPaths.push(entry.filePath);
      }
    } else if (collected > 0) {
      // Partially injected: never consume the entry, or the un-injected tail
      // would be permanently lost. Rewrite it with just the tail so the
      // remainder re-injects next boot without duplicating the head.
      if (entry.filePath) {
        deferredTails.push({
          filePath: entry.filePath,
          payload: { ...entry.payload, stale_belief_events: tail },
        });
        console.error(
          `minni: corrections_reassert cap deferred ${tail.length} valid event(s) from ${label} to next boot`,
        );
      } else {
        // No backing file to defer into — discard with a warning (the daemon
        // still holds the events).
        console.error(
          `minni: corrections_reassert cap discarded ${tail.length} valid event(s) from ${label}`,
        );
      }
    } else if (tail.length > 0) {
      // Cap was already full before this entry contributed anything: leave it
      // unconsumed so it re-injects on the next boot instead of being lost.
      console.error(
        `minni: corrections_reassert cap full — deferring ${label} to next boot`,
      );
    } else {
      // Every event failed the schema gate. Do NOT consume: clearing here
      // would silently destroy the stashed correction with zero injection.
      console.error(
        `minni: all stale_belief_events in ${label} failed the schema gate — entry left in place`,
      );
    }
  }
  if (dropped > 0) {
    // stderr only: hook stdout is the JSON protocol channel.
    console.error(
      `minni: dropped ${dropped} malformed stale_belief_events from inbox (schema gate)`,
    );
  }
  return { events, consumedPaths, contributingPaths, deferredTails };
}

/**
 * After a boot (or a Stop-routed delivery) has consumed stashed
 * stale_belief_events (corrections_reassert), settle the inbox: remove exactly
 * the entries collectCorrectionsReassert reported as consumed (so they re-inject
 * exactly once and do not accumulate across compaction cycles), and rewrite
 * partially-injected entries with their un-injected valid tail (so cap overflow
 * defers to the next delivery instead of being lost). Entries whose events were
 * all malformed or all cap-deferred are untouched and survive as-is.
 */
export async function settleReassertedInboxEntries(
  vaultPath: string,
  outcome: Pick<CorrectionsReassertResult, "consumedPaths" | "deferredTails">,
): Promise<void> {
  // I4: the containment root is the TRUSTED inbox directory, never a path derived
  // from the (attacker-writable) tail.filePath. Passing path.dirname(tail.filePath)
  // would compare the target's own parent against itself and defeat the check.
  const inboxRoot = path.join(vaultPath, "inbox");
  // Always land under inbox/.archive — even when the source lives in
  // inbox/.undeliverable/ (Stop delivery of a SessionStart structural drop).
  // archiveInboxEntry alone would put parked files into .undeliverable/.archive
  // and hide them from operators grepping the normal archive.
  const archiveDir = path.join(inboxRoot, ".archive");
  for (const filePath of outcome.consumedPaths) {
    // Inbox lifecycle policy (audit C2): archive, never unlink — the entry
    // moves to inbox/.archive/, which is invisible to readInboxStatus and the
    // engine's inbox_ingest glob, so the exactly-once contract still holds.
    await archiveInboxEntry(filePath, { archiveDir });
  }
  for (const tail of outcome.deferredTails) {
    try {
      // I4: the inbox file is attacker-writable; a bare writeFile would follow a
      // symlink swapped in under inbox/. Contain to the trusted inbox root and
      // write atomically (both helpers live in this module).
      assertWriteTargetUnder(tail.filePath, inboxRoot);
      await writeFileAtomic(
        tail.filePath,
        JSON.stringify(tail.payload, null, 2),
      );
    } catch {
      // Best effort: an unwritable tail leaves the original entry intact,
      // which re-injects (with duplicated head events) rather than losing any.
    }
  }
}

function normalizeWikilinkRef(ref: string): string {
  return ref
    .replace(/^\[\[/, "")
    .replace(/\]\]$/, "")
    .split("|")[0]
    .replace(/\.md$/, "")
    .replace(/^\/+/, "");
}

/**
 * RCM-005 / G23: assert path is under root after realpath (symlink escape reject).
 * Fail closed on any error or escape.
 */
export function assertUnder(fullPath: string, rootPath: string): void {
  let realFull: string;
  try {
    realFull = fs.realpathSync(fullPath);
  } catch (e: any) {
    if (e && e.code === "ENOENT") return; // non-existing candidate: let readFile fail naturally; no escape vector yet
    throw new Error(`path containment check failed for ${fullPath}`);
  }
  const realRoot = fs.realpathSync(rootPath);
  const rel = path.relative(realRoot, realFull);
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new Error(`path escapes vault root: ${fullPath}`);
  }
}

/**
 * H2/I4: symlink-safe write containment for a target that may not exist yet.
 * A bare `writeFile(target)` follows symlinks — an attacker who controls a
 * parent component (e.g. `<vault>/.runtime` → outside dir) or the target file
 * itself (an existing symlink) can redirect the write out of the vault, or make
 * a read-modify-write clobber an arbitrary file.
 *
 * This resolves the parent directory's realpath and asserts it stays under
 * `rootPath`, and rejects when the immediate target is itself a symlink. It does
 * NOT require the target to exist. Fail closed on any error.
 */
export function assertWriteTargetUnder(targetPath: string, rootPath: string): void {
  const realRoot = fs.realpathSync(rootPath);
  const parent = path.dirname(targetPath);
  let realParent: string;
  try {
    realParent = fs.realpathSync(parent);
  } catch {
    throw new Error(`write path containment check failed for ${targetPath}`);
  }
  const relParent = path.relative(realRoot, realParent);
  if (relParent.startsWith("..") || path.isAbsolute(relParent)) {
    throw new Error(`write path escapes root: ${targetPath}`);
  }
  // Reject a target that is itself a symlink (a read-modify-write or overwrite
  // would follow it out of the contained tree).
  try {
    const st = fs.lstatSync(targetPath);
    if (st.isSymbolicLink()) {
      throw new Error(`write target is a symlink: ${targetPath}`);
    }
  } catch (e: any) {
    if (!e || e.code !== "ENOENT") throw e; // ENOENT == fresh write, allowed
  }
}

// #340: a ref that resolves to a real file but is privacy-gated must be
// distinguishable from a ref that never resolved at all (broken link,
// outside the vault, genuinely absent) — resolveInboxHandoffContext below
// needs to count the former without resolveVaultRef leaking WHICH ref or
// WHY it was withheld (that stays fail-closed/silent, same as #312).
type ResolvedVaultRef =
  | { status: "resolved"; snippet: HandoffContextSnippet }
  | { status: "withheld" }
  | { status: "absent" };

async function resolveVaultRef(
  vaultPath: string,
  ref: string,
): Promise<ResolvedVaultRef> {
  const normalized = normalizeWikilinkRef(ref);
  const candidates = [
    path.join(vaultPath, `${normalized}.md`),
    path.join(vaultPath, normalized),
  ];
  for (const notePath of candidates) {
    try {
      assertUnder(notePath, vaultPath);
      const markdown = await readFile(notePath, "utf8");
      const relativePath = path.relative(vaultPath, notePath);
      // #312: a handoff's wikilink_refs can name ANY note in the vault,
      // including one authored (or heuristically flagged) privacy:private —
      // this read had no privacy gate at all before, worse than the SEC-006
      // gap #308 fixed for UserPromptSubmit's recall pointer, since this one
      // surfaces the note's BODY TEXT (a 520-char snippet), not just a
      // title/path. Gate through the exact same filterSafeVaultResults
      // machinery #308 already established (not a duplicated check), so the
      // two call sites can't silently drift the way the pre-#308 gap did.
      const candidate: VaultSearchResult = {
        notePath,
        relativePath,
        wikilink: normalized,
        title: titleFromMarkdown(relativePath, markdown),
        snippet: snippetFor(markdown, queryTerms(normalized), 520),
        score: 0,
        privacy: privacyFromMarkdown(markdown),
      };
      if (filterSafeVaultResults([candidate]).length === 0) {
        // Fail closed exactly like a containment reject below: this ref
        // resolves to a real file, but it's not safe to surface, so its
        // BODY/PATH/REASON are still never returned — only the fact that
        // one withholding happened is countable by the caller (#340).
        return { status: "withheld" };
      }
      return {
        status: "resolved",
        snippet: {
          ref: normalized,
          relativePath,
          notePath,
          snippet: candidate.snippet,
        },
      };
    } catch {
      // try the next (or containment reject -> fail closed, treat as absent)
    }
  }
  return { status: "absent" };
}

export interface InboxHandoffContext {
  snippets: HandoffContextSnippet[];
  // #340: count of refs that resolved to a real note but were withheld by
  // the privacy gate (#312) — distinguishes "this handoff had refs but none
  // were safe to surface" from "this handoff never referenced anything" (an
  // all-refs-gated handoff previously emitted an empty snippets array
  // indistinguishable from a handoff with none at all). Callers must omit
  // this from any model-facing payload when it is 0 — an emitted zero the
  // hook never actually withheld anything for is the same false all-clear
  // the campaign's H2 discipline exists to prevent elsewhere; absent means
  // "nothing withheld", not "zero, checked". Never carries which ref(s) or
  // why — only the count.
  withheldCount: number;
}

export async function resolveInboxHandoffContext(
  vaultPath: string,
  entries: InboxEntry[],
  limit = 8,
): Promise<InboxHandoffContext> {
  const refs = new Set<string>();
  for (const entry of entries) {
    if (entry.payload.kind !== "handoff") continue;
    const rawRefs = entry.payload.wikilink_refs;
    if (!Array.isArray(rawRefs)) continue;
    for (const ref of rawRefs) {
      // #340 review: dedup on the NORMALIZED ref, not the raw string —
      // "wiki/x", "[[wiki/x]]", "wiki/x.md", and "[[wiki/x|Alias]]" all name
      // the same note. Deduping on the raw string let one gated note be
      // counted multiple times in withheldCount (e.g. the RCM-005 symlink
      // fixture's own ["evil", "[[evil]]"] pair) — a wrong count published
      // to the model-facing envelope, worse than the original silent-empty
      // gap since it looked authoritative.
      if (typeof ref === "string" && ref.trim()) refs.add(normalizeWikilinkRef(ref.trim()));
    }
  }
  const snippets: HandoffContextSnippet[] = [];
  let withheldCount = 0;
  // #340 review: iterate every ref for the WITHHELD count, only cap what
  // gets pushed into `snippets`. The previous `break` on snippets.length
  // reaching `limit` stopped resolving refs entirely once enough safe ones
  // were found — a handoff with 8 safe refs followed by a 9th, gated ref
  // never looked at the 9th at all, so withheldCount silently under-reported
  // (0 instead of 1) exactly the case this field exists to surface. Handoffs
  // name a small, bounded number of refs in practice; trading a little extra
  // per-ref I/O for a correct count is the right side of that tradeoff here.
  for (const ref of refs) {
    const resolved = await resolveVaultRef(vaultPath, ref);
    if (resolved.status === "resolved") {
      if (snippets.length < limit) snippets.push(resolved.snippet);
    } else if (resolved.status === "withheld") {
      withheldCount += 1;
    }
  }
  return { snippets, withheldCount };
}

/**
 * Archive (never delete) an inbox entry: rename it into an archive dir,
 * preserving the filename (timestamp prefix on collision). Default archive is
 * the sibling `.archive/` of the file's parent; callers that settle entries
 * from `inbox/.undeliverable/` pass `archiveDir: inbox/.archive` so consumption
 * always lands in the canonical archive. `.archive/` is invisible to
 * readInboxStatus and to the engine's inbox_ingest glob, so archived entries
 * stop re-surfacing. Best-effort: returns the archived path, or undefined when
 * the file was already gone.
 */
export async function archiveInboxEntry(
  filePath: string,
  options?: { archiveDir?: string },
): Promise<string | undefined> {
  const archiveDir = options?.archiveDir ?? path.join(path.dirname(filePath), ".archive");
  const base = path.basename(filePath);
  let target = path.join(archiveDir, base);
  try {
    await mkdir(archiveDir, { recursive: true });
    try {
      await access(target);
      target = path.join(archiveDir, `${Date.now().toString(36)}-${base}`);
    } catch {
      // no collision
    }
    await rename(filePath, target);
    return target;
  } catch {
    return undefined; // best effort; nothing to do if already gone
  }
}

export interface ExpiredInboxHandoff {
  slug: string;
  filePath: string;
  /** Always set: an entry is only surfaced when THIS session archived it. */
  archivedPath: string;
  createdAt: string;
  ageDays: number;
  /**
   * "expired": TTL (or the lease's own expires_at) elapsed unacknowledged.
   * "acked": leftover packet whose lease was already acknowledged — archived,
   * never reported as expired.
   */
  status: "expired" | "acked";
  task?: unknown;
}

export function inboxHandoffTtlDays(): number {
  const raw = Number(process.env.MINNI_INBOX_HANDOFF_TTL_DAYS ?? "");
  return Number.isFinite(raw) && raw > 0 ? raw : 7;
}

/**
 * Plain `kind: handoff` inbox files are written ONLY by the daemon handoff
 * channel, which uses the compact `YYYYMMDDTHHMMSSZ-` stamp. Plugin-written
 * (dashed-date) files are stop candidates / precompact handoffs / failed
 * commands — never plain handoffs — so the reaper can skip them WITHOUT
 * reading, keeping SessionStart O(handoff files) instead of O(backlog).
 */
const COMPACT_HANDOFF_NAME = /^\d{8}T\d{6}Z-/;

/**
 * TTL reaper for the FILE handoff channel (audit C2/B3). Orphaned
 * `kind: handoff` inbox files (`requires_ack` falsy) are invisible to the
 * lease ack channel (minnid's listing skips them), so without a TTL they pin
 * the inbox forever. Semantics ported from the agent_ping lease model
 * (agent_ping.ts withExpiry / checkAndReapLease): expiry is evaluated at read
 * time, honoring each lease's OWN expiry first — classification happens
 * BEFORE the TTL so a short lease drains as soon as its own expiry passes
 * (the daemon default is created_at + 24h, far inside the 7d TTL), matching
 * scripts/inbox_cleanup.py classify_file:
 *   - `ack_status` set: lease already acknowledged — archive the leftover
 *     packet regardless of file age, surfaced as "acked" (never mislabeled
 *     "expired").
 *   - `requires_ack` truthy: a live ack-channel lease the daemon owns; reaped
 *     as soon as its own `expires_at` has passed, regardless of file age
 *     (missing/unparseable `expires_at` => never reaped here; the ack channel
 *     drains it).
 *   - otherwise (the orphan shape): the file-age TTL applies.
 * An entry is surfaced AT MOST once — only when this call's archive rename
 * succeeded — with an explicit status, never silently dropped. A failed or
 * raced archive surfaces nothing (the winner reports; a failure retries next
 * session). Rename only, never unlink.
 */
export async function expireStaleInboxHandoffs(
  vaultPath: string,
  ttlDays = inboxHandoffTtlDays(),
  now = Date.now(),
): Promise<ExpiredInboxHandoff[]> {
  const dir = path.join(vaultPath, "inbox");
  let names: string[] = [];
  try {
    names = (await readdir(dir)).filter((name) => name.endsWith(".json"));
  } catch {
    return [];
  }
  const cutoff = now - ttlDays * 86_400_000;
  const expired: ExpiredInboxHandoff[] = [];
  for (const name of names) {
    if (!COMPACT_HANDOFF_NAME.test(name)) continue; // cheap pre-filter, no read
    const filePath = path.join(dir, name);
    let ts = parseInboxTimestamp(name);
    if (ts === undefined) {
      try {
        ts = (await stat(filePath)).mtimeMs;
      } catch {
        continue;
      }
    }
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(await readFile(filePath, "utf8")) as Record<string, unknown>;
    } catch {
      continue; // unreadable files are not handoffs; leave them alone
    }
    // JSON.parse("null") succeeds, so a null payload would throw on .kind
    // below and abort the whole drain loop; non-objects are not handoffs.
    if (!payload || typeof payload !== "object" || payload.kind !== "handoff") continue;
    let status: "expired" | "acked";
    if (typeof payload.ack_status === "string" && payload.ack_status) {
      status = "acked"; // terminal leftover; archive regardless of age
    } else if (payload.requires_ack) {
      const leaseExpiry =
        typeof payload.expires_at === "string" ? Date.parse(payload.expires_at) : NaN;
      if (!Number.isFinite(leaseExpiry) || leaseExpiry > now) continue; // live lease: daemon owns it
      status = "expired"; // own expiry passed: drain now, never wait for the TTL
    } else {
      if (ts >= cutoff) continue; // orphan shape: the file-age TTL applies
      status = "expired";
    }
    const archivedPath = await archiveInboxEntry(filePath);
    if (!archivedPath) continue; // raced (winner reports) or failed (retry next session)
    expired.push({
      slug: typeof payload.slug === "string" ? payload.slug : name,
      filePath,
      archivedPath,
      createdAt:
        typeof payload.createdAt === "string"
          ? payload.createdAt
          : new Date(ts).toISOString(),
      ageDays: Math.floor((now - ts) / 86_400_000),
      status,
      task: payload.task,
    });
  }
  return expired;
}

/**
 * Shared SessionStart `pending_learnings` envelope section (audit C2/B2):
 * honest totals (`total_pending`, `oldest_age_days`, `showing`) alongside the
 * capped entries, plus the TTL reaper's once-only expired/acked handoffs.
 * All four hooks (claude-code, codex, grok, kilocode) MUST build the section
 * through this function so the shape cannot drift per hook.
 */
/**
 * Park inbox entries whose correction this platform can NEVER deliver on this
 * event, moving them to `inbox/.undeliverable/`.
 *
 * The bound on a structural wire drop. Refusing to archive an undelivered
 * correction is right — it was never injected — but on a platform whose wire
 * cannot inject at SessionStart at all, "retry next boot" never terminates: one
 * durable file per correction-bearing compaction, forever, each one permanently
 * occupying a slot in the newest-N reassert window and crowding out corrections
 * that COULD be delivered.
 *
 * Parking is deliberately not archiving. `.archive/` means consumed; these were
 * not. `.undeliverable/` means "kept, not delivered at SessionStart, pending
 * Stop delivery" — invisible to readInboxStatus and the SessionStart reassert
 * window (both read the inbox's top level only), so the window stays clear,
 * while the correction itself survives for `readParkedUndeliverablePending` +
 * Stop injection (issue #253). Bounded and audited beats both silent loss and
 * infinite retention.
 *
 * Returns the paths that were parked.
 */
export async function parkUndeliverableInboxEntries(
  filePaths: string[],
): Promise<string[]> {
  const parked: string[] = [];
  for (const filePath of filePaths) {
    const parkDir = path.join(path.dirname(filePath), ".undeliverable");
    const base = path.basename(filePath);
    let target = path.join(parkDir, base);
    try {
      await mkdir(parkDir, { recursive: true });
      if (await exists(target)) {
        target = path.join(parkDir, `${Date.now().toString(36)}-${base}`);
      }
      await rename(filePath, target);
      parked.push(target);
    } catch {
      // Best effort: an entry that cannot be parked simply stays in the inbox
      // and is re-considered next boot. Failing to tidy must not lose it.
    }
  }
  return parked;
}

/**
 * Issue #253: read corrections parked under `inbox/.undeliverable/` so Stop
 * (the only injectable event on platforms like Grok Build) can deliver them.
 *
 * Same eligibility rules as `readReassertPending` (schema-valid stale-belief
 * events or empty stashes; newest-`limit` window), but against the park dir
 * rather than the live inbox top level. Malformed-only files are skipped and
 * left for inspection.
 */
export async function readParkedUndeliverablePending(
  vaultPath: string,
  limit = 3,
): Promise<InboxEntry[]> {
  const parkDir = path.join(vaultPath, "inbox", ".undeliverable");
  let names: string[] = [];
  try {
    names = (await readdir(parkDir)).filter((name) => name.endsWith(".json"));
  } catch {
    return [];
  }
  const stamped = names.map((name) => ({ name, ts: parseInboxTimestamp(name) }));
  stamped.sort(
    (a, b) => (b.ts ?? 0) - (a.ts ?? 0) || b.name.localeCompare(a.name),
  );
  const eligible: InboxEntry[] = [];
  for (const { name } of stamped) {
    if (eligible.length >= limit) break;
    const filePath = path.join(parkDir, name);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(await readFile(filePath, "utf8")) as Record<string, unknown>;
    } catch {
      continue; // unreadable/corrupt file: never occupies a reassert slot
    }
    const stashed = parsed.stale_belief_events;
    const emptyStash = Array.isArray(stashed) && stashed.length === 0;
    if (!emptyStash && !payloadHasValidStaleBeliefEvent(parsed)) continue;
    eligible.push({
      slug: typeof parsed.slug === "string" ? parsed.slug : name,
      filePath,
      createdAt: typeof parsed.createdAt === "string" ? parsed.createdAt : "",
      payload: parsed,
    });
  }
  return eligible;
}

/**
 * The `expired_handoffs` list, as the boot envelope carries it.
 *
 * Extracted so a boot whose inbox read was cut by the budget can still ship the
 * reaper's one-shot result: the reaper is unbudgeted precisely because it
 * ARCHIVES as it walks, and an archived entry is invisible to every later read
 * — so a boot that reaps but does not report is the entry's only chance, spent.
 */
export function expiredHandoffsBody(
  expiredHandoffs: ExpiredInboxHandoff[],
): Array<Record<string, unknown>> {
  return expiredHandoffs.map((entry) => ({
    slug: entry.slug,
    status: entry.status,
    age_days: entry.ageDays,
    created: entry.createdAt,
    archived_to: entry.archivedPath,
  }));
}

export function buildPendingLearningsSection(
  inboxStatus: InboxStatus,
  expiredHandoffs: ExpiredInboxHandoff[],
): Record<string, unknown> {
  return {
    total_pending: inboxStatus.totalPending,
    oldest_age_days: inboxStatus.oldestAgeDays,
    showing: inboxStatus.entries.length,
    entries: inboxStatus.entries.map((entry) => ({
      slug: entry.slug,
      created: entry.createdAt,
      path: entry.filePath,
      candidates: entry.payload.candidates,
      kind: entry.payload.kind,
      task: entry.payload.task,
    })),
    expired_handoffs: expiredHandoffsBody(expiredHandoffs),
  };
}

export async function vaultExists(vaultPath: string): Promise<boolean> {
  try {
    const st = await stat(vaultPath);
    return st.isDirectory();
  } catch {
    return false;
  }
}

/**
 * Cap on the Layer 1 shelf content inlined into the SessionStart envelope
 * (bytes). SKILL.md documents a strict <4096 TOKEN budget for layer1/, but a
 * well-curated, scar-laden core.md runs close to that in bytes too — an 8KB
 * ceiling leaves headroom above a realistic full shelf while still bounding a
 * pathological or over-curated file. Same posture as compact-harvest's
 * SUMMARY_TEXT_MAX_CHARS: a safety ceiling, not a target.
 */
export const LAYER1_SHELF_MAX_BYTES = 8192;

export interface Layer1ShelfResult {
  ok: boolean;
  content?: string;
  truncated?: boolean;
  omittedBytes?: number;
  /** Set only when ok=false. Always prefixed "absent: " so a missing or
   * unreadable shelf is grep-able and never a silently missing envelope key. */
  reason?: string;
}

/**
 * Read `<vault>/layer1/core.md` for inlining into the SessionStart envelope.
 * ALWAYS a bounded read (never the unbounded `readFile` a "file fits under
 * the cap" shortcut would tempt): one `open`, one `fstat` on that same
 * descriptor (file-type check only — never the truncation source of truth,
 * see below), one `read`. `bytesRead` (not the buffer's allocated length) is
 * what becomes `content`, so a short read can never pad the boot envelope
 * with trailing NUL bytes. No RPC, no timers — this is local FS only, same
 * class of read as the reassert inbox scan the SessionStart handler already
 * runs unbudgeted.
 *
 * TRUNCATION IS DERIVED FROM THE READ ITSELF, NOT A PRE-READ `fstat`. The
 * probe buffer is LAYER1_SHELF_MAX_BYTES + 1: if the read fills all of it,
 * the file had at least one byte beyond the cap AT THE MOMENT OF THE READ —
 * the only claim `truncated` needs to make. Sizing the cap off an earlier
 * `fstat` instead (an earlier version of this function) let a file that
 * GREW between stat and read produce silently-incomplete content with
 * `truncated: false` (the exact H5 failure this field exists to prevent),
 * and a file that SHRANK produce a false `truncated: true` with a wrong
 * `omittedBytes`. `omittedBytes` itself still needs a size figure, so it
 * takes a SECOND fstat AFTER the read — a closer, but not perfectly
 * atomic, snapshot — and clamps to a 1-byte floor so it is always an honest
 * lower bound even if that snapshot disagrees with what was actually read.
 *
 * THE PROBE READ ITSELF IS LOOPED, NOT A SINGLE `handle.read` CALL. POSIX
 * (and Node's fs.read, which is a thin wrapper over it) permits a SHORT read
 * on a perfectly normal regular file — nothing guarantees one call fills the
 * buffer even when more bytes are available. A single-call version (an
 * earlier revision of this function) could read some bytesRead <= cap on a
 * file actually larger than the cap and report `truncated: false` on
 * incomplete content — the H5 failure one layer deeper than the fstat one
 * above. The loop keeps reading into the buffer at the current offset until
 * either the buffer is full (cap+1 bytes seen — truncated) or a read
 * returns 0 (genuine EOF — not truncated, whatever was accumulated is the
 * whole file).
 *
 * Known limitation: the byte cut is not UTF-8-boundary-aware, so a cap that
 * lands mid multi-byte character renders as U+FFFD in `content`. Acceptable
 * for ASCII-heavy markdown shelves; revisit if core.md content routinely
 * carries non-ASCII near the cap.
 */

/** Signature shared with `FileHandle.read` — narrowed to what `readFullOrEof`
 * needs so it can be driven by a real handle or a test double. */
export type BufferReadFn = (
  buffer: Buffer,
  offset: number,
  length: number,
  position: number,
) => Promise<{ bytesRead: number }>;

/**
 * Read repeatedly into `buffer` (from `buffer` offset 0, file position 0)
 * until `length` bytes have landed or `read` reports EOF (`bytesRead === 0`).
 * Exists because a single call to `FileHandle.read` is permitted by POSIX to
 * return FEWER bytes than requested even on an ordinary regular file — a
 * short read is not EOF, and treating it as the whole file is the same H5
 * silent-truncation failure one layer below the fstat-vs-read race
 * `readLayer1Shelf` already closed. Exported as its own function (not
 * inlined into `readLayer1Shelf`) so the accumulation logic can be pinned
 * directly against a fake short-read producer, not only through real
 * filesystem calls that rarely short-read in a test environment. Returns
 * the number of bytes actually accumulated, which may be less than `length`
 * on genuine EOF.
 */
export async function readFullOrEof(
  read: BufferReadFn,
  buffer: Buffer,
  length: number,
): Promise<number> {
  let totalRead = 0;
  while (totalRead < length) {
    const { bytesRead } = await read(buffer, totalRead, length - totalRead, totalRead);
    if (bytesRead === 0) break; // genuine EOF — a short read alone means nothing
    totalRead += bytesRead;
  }
  return totalRead;
}

export async function readLayer1Shelf(vaultPath: string): Promise<Layer1ShelfResult> {
  const corePath = path.join(vaultPath, "layer1", "core.md");
  let handle;
  try {
    handle = await open(corePath, "r");
  } catch (err) {
    const code = (err as NodeJS.ErrnoException)?.code;
    return {
      ok: false,
      reason:
        code === "ENOENT"
          ? "absent: no layer1/core.md in this vault"
          : `absent: open failed (${code ?? String((err as Error)?.message ?? err)})`,
    };
  }
  try {
    const info = await handle.stat();
    if (!info.isFile()) {
      return { ok: false, reason: "absent: layer1/core.md is not a regular file" };
    }
    // One byte beyond the cap: filling the probe buffer (or not) is what
    // tells `truncated` apart, not a size fetched before reading started.
    const probe = LAYER1_SHELF_MAX_BYTES + 1;
    const buffer = Buffer.alloc(probe);
    const totalRead = await readFullOrEof(
      (buf, offset, len, pos) => handle!.read(buf, offset, len, pos),
      buffer,
      probe,
    );
    // Emptiness read off the ACTUAL accumulated read, not the pre-read fstat
    // above — same TOCTOU posture as the truncation decision below.
    if (totalRead === 0) {
      return { ok: false, reason: "absent: layer1/core.md is empty" };
    }
    const truncated = totalRead > LAYER1_SHELF_MAX_BYTES;
    const contentBytes = truncated ? LAYER1_SHELF_MAX_BYTES : totalRead;
    const content = buffer.subarray(0, contentBytes).toString("utf8");
    if (!truncated) {
      return { ok: true, content, truncated: false };
    }
    // Best-effort omitted-byte count: a post-read fstat, clamped to a
    // 1-byte floor (the probe byte alone proves at least that much was
    // omitted) so the number is always truthful even under a concurrent
    // writer this snapshot cannot perfectly pin down.
    let omittedBytes = 1;
    try {
      const after = await handle.stat();
      omittedBytes = Math.max(after.size - LAYER1_SHELF_MAX_BYTES, 1);
    } catch {
      // Keep the honest 1-byte floor.
    }
    return { ok: true, content, truncated: true, omittedBytes };
  } catch (err) {
    return {
      ok: false,
      reason: `absent: read failed (${String((err as Error)?.message ?? err)})`,
    };
  } finally {
    await handle.close();
  }
}

/**
 * Envelope body for the `layer1_shelf` SessionStart section. Always present
 * (never a silently missing key, defect class H2): ok:false carries the
 * "absent: <reason>" string, ok:true carries the content plus an explicit
 * truncation marker whenever the cap trimmed it (defect class H5 — no silent
 * truncation).
 */
export function layer1ShelfBody(result: Layer1ShelfResult): Record<string, unknown> {
  if (!result.ok) {
    // JSON.stringify DROPS keys whose value is undefined — an unset `reason`
    // would silently degrade to `{"ok":false}` with no "absent:" string,
    // exactly the H2 failure mode this field exists to prevent. Every
    // production path above sets one; this default only guards a future
    // caller that forgets to.
    return { ok: false, reason: result.reason ?? "absent: unknown (no reason recorded)" };
  }
  return {
    ok: true,
    content: result.content,
    truncated: result.truncated ?? false,
    ...(result.truncated
      ? {
          omitted_bytes: result.omittedBytes,
          // "at least": omitted_bytes is a post-read fstat snapshot, not a
          // value pinned atomically to the read — truthful as a floor under
          // a concurrent writer, not a promise of exactness. See
          // readLayer1Shelf's docstring for why this can't be exact.
          note: `layer1/core.md exceeds the ${LAYER1_SHELF_MAX_BYTES}-byte inline cap; at least ${result.omittedBytes} byte(s) omitted from the tail.`,
        }
      : {}),
  };
}

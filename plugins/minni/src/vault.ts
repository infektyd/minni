import { createHash, randomBytes } from "node:crypto";
import {
  access,
  lstat,
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
import { DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH } from "./config.js";
import { durabilityOf, type DurabilityKind } from "./durability.js";
import { stripHarnessBlocks } from "./prompt-class.js";
// Type-only: use-report.ts imports FROM this module, so the runtime import
// below is dynamic and function-scoped (same pattern as task.ts above).
import type { UseReport } from "./use-report.js";

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
  annotation?: string;
  trigger_kind?: string;
  sessionId?: string;
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
  /** Recall-to-use report over the last `useDays` days (rev5 §4.5); only when asked for. */
  use?: UseReport | { error: string };
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
  /** Durability classification ("durable" | "ordinary" | "noise") from durabilityOf (rev5 §5.2). */
  durability?: DurabilityKind;
  /** Calibrated UPS strong-gate score in [0, 1] (rev5 §5.2 item 5). */
  gateScore?: number;
  /** UPS strong-gate route ("a" | "b" | null) when evaluated. */
  gateRoute?: "a" | "b" | null;
  /** Whether this note clears the UPS strong-gate threshold (`durability !== "noise"` and route "a" or "b"). */
  clearsGate?: boolean;
  /** Class-weighted gate score (`gateScore * classWeight`, where noise uses 0.25 and non-noise uses 1.0). */
  weightedScore?: number;
  /** Query terms matched in title, markdown headings, and body (when rank === "ups_gate"). */
  contributingTerms?: { title: string[]; heading: string[]; body: string[] };
  /** Count of distinctive query terms covered by this note (when rank === "ups_gate"). */
  coveredDistinctive?: number;
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

const MAX_REGEX_TERM_LENGTH = 256;
const MULTI_TERM_THRESHOLD = 8;

/** First WHOLE-WORD occurrence of `term` in `text`, or -1. */
function firstWordIndex(text: string, term: string): number {
  if (!term) return -1;
  if (term.length > MAX_REGEX_TERM_LENGTH) {
    return firstLongWordIndex(text, term, true);
  }
  try {
    const regex = wordRegex(term, true);
    if (!regex) return -1;
    regex.lastIndex = 0; // shared cached /g regex
    return regex.exec(text)?.index ?? -1;
  } catch {
    return firstLongWordIndex(text, term, true);
  }
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
  let firstHit = -1;
  let usedFastPath = false;
  if (terms.length > MULTI_TERM_THRESHOLD) {
    const matcher = getMultiTermMatcher(terms);
    if (!matcher.hasNonAsciiOrRejected) {
      const lower = plain.toLowerCase();
      if (plain.length === lower.length) {
        firstHit = matcher.firstSnippetHit(plain, lower);
        usedFastPath = true;
      }
    }
  }
  if (!usedFastPath) {
    const earliest = (indexes: number[]): number =>
      indexes.filter((index) => index >= 0).sort((a, b) => a - b)[0] ?? -1;
    firstHit = earliest(terms.map((term) => firstWordIndex(plain, term)));
    if (firstHit < 0) {
      // terms are lowercased by queryTerms; the whole-word regexes carry /i.
      const lower = plain.toLowerCase();
      firstHit = earliest(terms.map((term) => lower.indexOf(term)));
    }
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
  const entries = (await readdir(root, { withFileTypes: true })).sort((a, b) =>
    a.name < b.name ? -1 : a.name > b.name ? 1 : 0,
  );
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
const LEAD_STICKY_RE = new RegExp(NOT_WORD_BEFORE, "yu");
const TAIL_EXACT_STICKY_RE = new RegExp(NOT_WORD_AFTER, "yu");
const TAIL_INFLECT_STICKY_RE = new RegExp(
  `${MORPHOLOGICAL_SUFFIX}${NOT_WORD_AFTER}`,
  "yu",
);

function firstLongWordIndex(text: string, term: string, inflect = true): number {
  if (!term) return -1;
  const textLower = isPureLowerAscii(text) ? text : text.toLowerCase();
  const termLower = isPureLowerAscii(term) ? term : term.toLowerCase();
  let idx = textLower.indexOf(termLower);
  if (idx === -1) return -1;
  const last = termLower[termLower.length - 1];
  const leadRequired = WORD_EDGE_CHAR.test(termLower[0]);
  const tailRequired = WORD_EDGE_CHAR.test(last);
  if (!leadRequired && !tailRequired && !/[\p{L}\p{N}]/u.test(termLower)) {
    return -1;
  }
  const allowSuffix =
    inflect &&
    termLower.length >= MIN_MORPHOLOGICAL_LENGTH &&
    /\p{L}/u.test(last);
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

function countLongWordOccurrences(
  textLower: string,
  termLower: string,
  firstIdx: number,
  inflect: boolean,
  maxCount = 5,
): number {
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
  let idx = firstIdx;
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
// `/users/someuser/projects/` arrive here as ordinary terms with both edges `/`;
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

function isAsciiLowerWordChar(code: number): boolean {
  return (code >= 97 && code <= 122) || (code >= 48 && code <= 57);
}

function isAsciiLowerEdgeChar(code: number): boolean {
  return isAsciiLowerWordChar(code) || code === 95;
}

function isPureLowerAscii(str: string): boolean {
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i);
    if (c >= 128 || (c >= 65 && c <= 90)) return false;
  }
  return true;
}

function hasAsciiWordChar(str: string): boolean {
  for (let i = 0; i < str.length; i++) {
    if (isAsciiLowerWordChar(str.charCodeAt(i))) return true;
  }
  return false;
}

function countWordOccurrencesRegex(
  text: string,
  term: string,
  inflect: boolean,
  maxCount = 5,
): number {
  if (!term) return 0;
  const textLower = isPureLowerAscii(text) ? text : text.toLowerCase();
  const termLower = isPureLowerAscii(term) ? term : term.toLowerCase();
  const firstIdx = textLower.indexOf(termLower);
  if (firstIdx === -1) return 0;
  if (termLower.length > MAX_REGEX_TERM_LENGTH) {
    return countLongWordOccurrences(
      textLower,
      termLower,
      firstIdx,
      inflect,
      maxCount,
    );
  }
  try {
    const regex = wordRegex(term, inflect);
    if (!regex) return 0;
    const matches = text.match(regex);
    return matches ? Math.min(matches.length, maxCount) : 0;
  } catch {
    return countLongWordOccurrences(
      textLower,
      termLower,
      firstIdx,
      inflect,
      maxCount,
    );
  }
}

function countWordOccurrences(
  text: string,
  term: string,
  inflect = true,
  maxCount = 5,
): number {
  if (!term) return 0;
  if (!isPureLowerAscii(term)) {
    return countWordOccurrencesRegex(text, term, inflect, maxCount);
  }
  let idx = text.indexOf(term);
  if (idx === -1) return 0;

  const termLen = term.length;
  const firstCode = term.charCodeAt(0);
  const lastCode = term.charCodeAt(termLen - 1);
  const leadRequired = isAsciiLowerEdgeChar(firstCode);
  const tailRequired = isAsciiLowerEdgeChar(lastCode);
  if (!leadRequired && !tailRequired && !hasAsciiWordChar(term)) {
    return 0;
  }
  const allowSuffix =
    inflect &&
    termLen >= MIN_MORPHOLOGICAL_LENGTH &&
    lastCode >= 97 &&
    lastCode <= 122;

  const textLen = text.length;
  let count = 0;
  while (idx !== -1) {
    if (leadRequired && idx > 0) {
      const prevCode = text.charCodeAt(idx - 1);
      if (prevCode >= 128) {
        return countWordOccurrencesRegex(text, term, inflect);
      }
      if (isAsciiLowerWordChar(prevCode)) {
        idx = text.indexOf(term, idx + 1);
        continue;
      }
    }

    const afterIdx = idx + termLen;
    if (!tailRequired) {
      count++;
      if (count >= maxCount) return count;
      idx = text.indexOf(term, afterIdx);
      continue;
    }

    // Check trailing boundary and optional morphological suffix: (?:s|es|ing|ed)?
    let advance = -1;
    if (afterIdx >= textLen) {
      advance = afterIdx;
    } else {
      const c0 = text.charCodeAt(afterIdx);
      if (c0 >= 128) {
        return countWordOccurrencesRegex(text, term, inflect);
      }
      if (!isAsciiLowerWordChar(c0)) {
        advance = afterIdx;
      } else if (allowSuffix) {
        if (c0 === 115) {
          // 's'
          const p = afterIdx + 1;
          if (p >= textLen) {
            advance = p;
          } else {
            const cp = text.charCodeAt(p);
            if (cp >= 128) return countWordOccurrencesRegex(text, term, inflect);
            if (!isAsciiLowerWordChar(cp)) advance = p;
          }
        } else if (c0 === 101 && afterIdx + 1 < textLen) {
          // 'es' or 'ed'
          const c1 = text.charCodeAt(afterIdx + 1);
          if (c1 >= 128) return countWordOccurrencesRegex(text, term, inflect);
          if (c1 === 115 || c1 === 100) {
            const p = afterIdx + 2;
            if (p >= textLen) {
              advance = p;
            } else {
              const cp = text.charCodeAt(p);
              if (cp >= 128) return countWordOccurrencesRegex(text, term, inflect);
              if (!isAsciiLowerWordChar(cp)) advance = p;
            }
          }
        } else if (c0 === 105 && afterIdx + 2 < textLen) {
          // 'ing'
          const c1 = text.charCodeAt(afterIdx + 1);
          const c2 = text.charCodeAt(afterIdx + 2);
          if (c1 >= 128 || c2 >= 128) {
            return countWordOccurrencesRegex(text, term, inflect);
          }
          if (c1 === 110 && c2 === 103) {
            const p = afterIdx + 3;
            if (p >= textLen) {
              advance = p;
            } else {
              const cp = text.charCodeAt(p);
              if (cp >= 128) return countWordOccurrencesRegex(text, term, inflect);
              if (!isAsciiLowerWordChar(cp)) advance = p;
            }
          }
        }
      }
    }

    if (advance !== -1) {
      count++;
      if (count >= maxCount) return count;
      idx = text.indexOf(term, advance);
    } else {
      idx = text.indexOf(term, idx + 1);
    }
  }
  return count;
}

const MULTI_TERM_WEAK_CACHE = new WeakMap<readonly string[], MultiTermMatcher>();
const MULTI_TERM_LRU_CACHE = new Map<string, MultiTermMatcher>();
const MULTI_TERM_LRU_LIMIT = 64;

// Single-pass Aho-Corasick matcher over a query's pure-lower-ASCII terms with
// pre-resolved 128-wide ASCII goto transitions and exact word-boundary /
// morphological-suffix parity with countWordOccurrences (Backlog rows l & p).
class MultiTermMatcher {
  readonly numPatterns: number;
  readonly termToPatIdx: Int32Array;
  readonly hasNonAsciiOrRejected: boolean;
  private readonly patternStr: string[];
  private readonly patLen: Int32Array;
  private readonly maxPatLen: number;
  private readonly leadReq: Uint8Array;
  private readonly tailReq: Uint8Array;
  private readonly allowSuf: Uint8Array;
  private readonly gotoTable: Int32Array;
  private readonly firstMatchAtState: Int32Array;
  private readonly nextMatch: Int32Array;
  private readonly countsBuf: Int32Array;
  private readonly nextAllowedBuf: Int32Array;
  private readonly fallbackBuf: Uint8Array;

  constructor(terms: readonly string[]) {
    const termToPatIdx = new Int32Array(terms.length);
    const patternStr: string[] = [];
    const seenPatterns = new Map<string, number>();
    let hasNonAsciiOrRejected = false;
    let totalChars = 0;
    let maxPatLen = 0;

    for (let i = 0; i < terms.length; i++) {
      const t = terms[i];
      if (!t) {
        termToPatIdx[i] = -1;
        hasNonAsciiOrRejected = true;
        continue;
      }
      if (!isPureLowerAscii(t)) {
        termToPatIdx[i] = -2;
        hasNonAsciiOrRejected = true;
        continue;
      }
      const existing = seenPatterns.get(t);
      if (existing !== undefined) {
        termToPatIdx[i] = existing;
        continue;
      }
      const firstCode = t.charCodeAt(0);
      const lastCode = t.charCodeAt(t.length - 1);
      const leadRequired = isAsciiLowerEdgeChar(firstCode);
      const tailRequired = isAsciiLowerEdgeChar(lastCode);
      if (!leadRequired && !tailRequired && !hasAsciiWordChar(t)) {
        termToPatIdx[i] = -1;
        hasNonAsciiOrRejected = true;
        continue;
      }
      const patIdx = patternStr.length;
      patternStr.push(t);
      seenPatterns.set(t, patIdx);
      termToPatIdx[i] = patIdx;
      totalChars += t.length;
      if (t.length > maxPatLen) maxPatLen = t.length;
    }

    const numPatterns = patternStr.length;
    this.numPatterns = numPatterns;
    this.termToPatIdx = termToPatIdx;
    this.hasNonAsciiOrRejected = hasNonAsciiOrRejected;
    this.patternStr = patternStr;
    this.maxPatLen = maxPatLen;

    const patLen = new Int32Array(numPatterns);
    const leadReq = new Uint8Array(numPatterns);
    const tailReq = new Uint8Array(numPatterns);
    const allowSuf = new Uint8Array(numPatterns);
    const nextMatch = new Int32Array(numPatterns).fill(-1);

    const maxStates = totalChars + 1;
    const rawGoto = new Int32Array(maxStates * 128).fill(-1);
    const rawFirstMatch = new Int32Array(maxStates).fill(-1);
    let numStates = 1;

    for (let m = 0; m < numPatterns; m++) {
      const t = patternStr[m];
      const len = t.length;
      const firstCode = t.charCodeAt(0);
      const lastCode = t.charCodeAt(len - 1);
      patLen[m] = len;
      leadReq[m] = isAsciiLowerEdgeChar(firstCode) ? 1 : 0;
      tailReq[m] = isAsciiLowerEdgeChar(lastCode) ? 1 : 0;
      allowSuf[m] =
        len >= MIN_MORPHOLOGICAL_LENGTH && lastCode >= 97 && lastCode <= 122
          ? 1
          : 0;

      let state = 0;
      for (let j = 0; j < len; j++) {
        const code = t.charCodeAt(j);
        const edgeIdx = (state << 7) | code;
        let next = rawGoto[edgeIdx];
        if (next === -1) {
          next = numStates++;
          rawGoto[edgeIdx] = next;
        }
        state = next;
      }
      rawFirstMatch[state] = m;
    }

    const failLink = new Int32Array(numStates);
    const bfsQueue = new Int32Array(numStates);
    let qHead = 0;
    let qTail = 0;

    for (let c = 0; c < 128; c++) {
      const next = rawGoto[c];
      if (next !== -1) {
        failLink[next] = 0;
        bfsQueue[qTail++] = next;
      } else {
        rawGoto[c] = 0;
      }
    }

    while (qHead < qTail) {
      const u = bfsQueue[qHead++];
      const f = failLink[u];
      const fMatch = rawFirstMatch[f];
      if (fMatch >= 0) {
        const uMatch = rawFirstMatch[u];
        if (uMatch >= 0) {
          nextMatch[uMatch] = fMatch;
        } else {
          rawFirstMatch[u] = fMatch;
        }
      }
      const uBase = u << 7;
      const fBase = f << 7;
      for (let c = 0; c < 128; c++) {
        const next = rawGoto[uBase | c];
        if (next !== -1) {
          failLink[next] = rawGoto[fBase | c];
          bfsQueue[qTail++] = next;
        } else {
          rawGoto[uBase | c] = rawGoto[fBase | c];
        }
      }
    }

    this.patLen = patLen;
    this.leadReq = leadReq;
    this.tailReq = tailReq;
    this.allowSuf = allowSuf;
    this.gotoTable = rawGoto.subarray(0, numStates * 128);
    this.firstMatchAtState = rawFirstMatch.subarray(0, numStates);
    this.nextMatch = nextMatch;
    this.countsBuf = new Int32Array(numPatterns);
    this.nextAllowedBuf = new Int32Array(numPatterns);
    this.fallbackBuf = new Uint8Array(numPatterns);
  }

  countAll(text: string, maxCount = 5): Int32Array {
    const numPatterns = this.numPatterns;
    const counts = this.countsBuf;
    if (numPatterns === 0) return counts;
    counts.fill(0);
    const nextAllowedPos = this.nextAllowedBuf;
    nextAllowedPos.fill(0);
    const needsFallback = this.fallbackBuf;
    needsFallback.fill(0);

    const gotoTable = this.gotoTable;
    const firstMatchAtState = this.firstMatchAtState;
    const nextMatch = this.nextMatch;
    const patLen = this.patLen;
    const leadReq = this.leadReq;
    const tailReq = this.tailReq;
    const allowSuf = this.allowSuf;

    const textLen = text.length;
    let state = 0;
    let hasFallback = false;

    for (let i = 0; i < textLen; i++) {
      const code = text.charCodeAt(i);
      if (code >= 128) {
        state = 0;
        continue;
      }
      state = gotoTable[(state << 7) | code];
      let m = firstMatchAtState[state];
      if (m < 0) continue;

      const afterIdx = i + 1;
      for (; m >= 0; m = nextMatch[m]) {
        if (counts[m] >= maxCount || needsFallback[m] !== 0) continue;
        const idx = afterIdx - patLen[m];
        if (idx < nextAllowedPos[m]) continue;

        if (leadReq[m] !== 0 && idx > 0) {
          const prevCode = text.charCodeAt(idx - 1);
          if (prevCode >= 128) {
            needsFallback[m] = 1;
            hasFallback = true;
            continue;
          }
          if (isAsciiLowerWordChar(prevCode)) {
            continue;
          }
        }

        if (tailReq[m] === 0) {
          counts[m]++;
          nextAllowedPos[m] = afterIdx;
          continue;
        }

        let advance = -1;
        if (afterIdx >= textLen) {
          advance = afterIdx;
        } else {
          const c0 = text.charCodeAt(afterIdx);
          if (c0 >= 128) {
            needsFallback[m] = 1;
            hasFallback = true;
            continue;
          }
          if (!isAsciiLowerWordChar(c0)) {
            advance = afterIdx;
          } else if (allowSuf[m] !== 0) {
            if (c0 === 115) {
              const p = afterIdx + 1;
              if (p >= textLen) {
                advance = p;
              } else {
                const cp = text.charCodeAt(p);
                if (cp >= 128) {
                  needsFallback[m] = 1;
                  hasFallback = true;
                  continue;
                }
                if (!isAsciiLowerWordChar(cp)) advance = p;
              }
            } else if (c0 === 101 && afterIdx + 1 < textLen) {
              const c1 = text.charCodeAt(afterIdx + 1);
              if (c1 >= 128) {
                needsFallback[m] = 1;
                hasFallback = true;
                continue;
              }
              if (c1 === 115 || c1 === 100) {
                const p = afterIdx + 2;
                if (p >= textLen) {
                  advance = p;
                } else {
                  const cp = text.charCodeAt(p);
                  if (cp >= 128) {
                    needsFallback[m] = 1;
                    hasFallback = true;
                    continue;
                  }
                  if (!isAsciiLowerWordChar(cp)) advance = p;
                }
              }
            } else if (c0 === 105 && afterIdx + 2 < textLen) {
              const c1 = text.charCodeAt(afterIdx + 1);
              const c2 = text.charCodeAt(afterIdx + 2);
              if (c1 >= 128 || c2 >= 128) {
                needsFallback[m] = 1;
                hasFallback = true;
                continue;
              }
              if (c1 === 110 && c2 === 103) {
                const p = afterIdx + 3;
                if (p >= textLen) {
                  advance = p;
                } else {
                  const cp = text.charCodeAt(p);
                  if (cp >= 128) {
                    needsFallback[m] = 1;
                    hasFallback = true;
                    continue;
                  }
                  if (!isAsciiLowerWordChar(cp)) advance = p;
                }
              }
            }
          }
        }

        if (advance !== -1) {
          counts[m]++;
          nextAllowedPos[m] = advance;
        }
      }
    }

    if (hasFallback) {
      const patternStr = this.patternStr;
      for (let m = 0; m < numPatterns; m++) {
        if (needsFallback[m] !== 0) {
          const c = countWordOccurrencesRegex(text, patternStr[m], true);
          counts[m] = c > maxCount ? maxCount : c;
        }
      }
    }

    return counts;
  }

  firstSnippetHit(plain: string, lower: string): number {
    const numPatterns = this.numPatterns;
    if (numPatterns === 0) return -1;
    const gotoTable = this.gotoTable;
    const firstMatchAtState = this.firstMatchAtState;
    const nextMatch = this.nextMatch;
    const patLen = this.patLen;
    const leadReq = this.leadReq;
    const tailReq = this.tailReq;
    const allowSuf = this.allowSuf;
    const maxPatLen = this.maxPatLen;
    const needsFallback = this.fallbackBuf;
    let fallbackCleaned = false;

    const runFallback = (m: number): void => {
      if (!fallbackCleaned) {
        needsFallback.fill(0);
        fallbackCleaned = true;
      }
      if (needsFallback[m] !== 0) return;
      needsFallback[m] = 1;
      const wIdx = firstWordIndex(plain, this.patternStr[m]);
      if (wIdx >= 0 && (firstWordHit < 0 || wIdx < firstWordHit)) {
        firstWordHit = wIdx;
      }
    };

    const textLen = lower.length;
    let state = 0;
    let firstWordHit = -1;
    let firstSubHit = -1;

    for (let i = 0; i < textLen; i++) {
      if (firstWordHit >= 0 && i + 1 - maxPatLen >= firstWordHit) {
        break;
      }
      const code = lower.charCodeAt(i);
      if (code >= 128) {
        state = 0;
        continue;
      }
      state = gotoTable[(state << 7) | code];
      let m = firstMatchAtState[state];
      if (m < 0) continue;

      const afterIdx = i + 1;
      for (; m >= 0; m = nextMatch[m]) {
        const idx = afterIdx - patLen[m];
        if (firstSubHit < 0 || idx < firstSubHit) {
          firstSubHit = idx;
        }
        if (firstWordHit >= 0 && idx >= firstWordHit) {
          continue;
        }
        if (leadReq[m] !== 0 && idx > 0) {
          const prevCode = lower.charCodeAt(idx - 1);
          if (prevCode >= 128) {
            runFallback(m);
            continue;
          }
          if (isAsciiLowerWordChar(prevCode)) {
            continue;
          }
        }
        if (tailReq[m] === 0) {
          firstWordHit = idx;
          continue;
        }
        let matched = false;
        if (afterIdx >= textLen) {
          matched = true;
        } else {
          const c0 = lower.charCodeAt(afterIdx);
          if (c0 >= 128) {
            runFallback(m);
            continue;
          }
          if (!isAsciiLowerWordChar(c0)) {
            matched = true;
          } else if (allowSuf[m] !== 0) {
            if (c0 === 115) {
              const p = afterIdx + 1;
              if (p >= textLen) {
                matched = true;
              } else {
                const cp = lower.charCodeAt(p);
                if (cp >= 128) {
                  runFallback(m);
                  continue;
                }
                if (!isAsciiLowerWordChar(cp)) matched = true;
              }
            } else if (c0 === 101 && afterIdx + 1 < textLen) {
              const c1 = lower.charCodeAt(afterIdx + 1);
              if (c1 >= 128) {
                runFallback(m);
                continue;
              }
              if (c1 === 115 || c1 === 100) {
                const p = afterIdx + 2;
                if (p >= textLen) {
                  matched = true;
                } else {
                  const cp = lower.charCodeAt(p);
                  if (cp >= 128) {
                    runFallback(m);
                    continue;
                  }
                  if (!isAsciiLowerWordChar(cp)) matched = true;
                }
              }
            } else if (c0 === 105 && afterIdx + 2 < textLen) {
              const c1 = lower.charCodeAt(afterIdx + 1);
              const c2 = lower.charCodeAt(afterIdx + 2);
              if (c1 >= 128 || c2 >= 128) {
                runFallback(m);
                continue;
              }
              if (c1 === 110 && c2 === 103) {
                const p = afterIdx + 3;
                if (p >= textLen) {
                  matched = true;
                } else {
                  const cp = lower.charCodeAt(p);
                  if (cp >= 128) {
                    runFallback(m);
                    continue;
                  }
                  if (!isAsciiLowerWordChar(cp)) matched = true;
                }
              }
            }
          }
        }
        if (matched) {
          firstWordHit = idx;
        }
      }
    }
    return firstWordHit >= 0 ? firstWordHit : firstSubHit;
  }
}

function getMultiTermMatcher(terms: readonly string[]): MultiTermMatcher {
  const weak = MULTI_TERM_WEAK_CACHE.get(terms);
  if (weak) return weak;
  const key = terms.join("\0");
  let matcher = MULTI_TERM_LRU_CACHE.get(key);
  if (matcher) {
    MULTI_TERM_LRU_CACHE.delete(key);
    MULTI_TERM_LRU_CACHE.set(key, matcher);
    MULTI_TERM_WEAK_CACHE.set(terms, matcher);
    return matcher;
  }
  matcher = new MultiTermMatcher(terms);
  if (MULTI_TERM_LRU_CACHE.size >= MULTI_TERM_LRU_LIMIT) {
    const oldestKey = MULTI_TERM_LRU_CACHE.keys().next().value;
    if (oldestKey !== undefined) MULTI_TERM_LRU_CACHE.delete(oldestKey);
  }
  MULTI_TERM_LRU_CACHE.set(key, matcher);
  MULTI_TERM_WEAK_CACHE.set(terms, matcher);
  return matcher;
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
  if (terms.length > MULTI_TERM_THRESHOLD) {
    const matcher = getMultiTermMatcher(terms);
    const counts = matcher.countAll(haystack, 5);
    const termToPatIdx = matcher.termToPatIdx;
    for (let i = 0; i < terms.length; i++) {
      const term = terms[i];
      const patIdx = termToPatIdx[i];
      const count =
        patIdx >= 0
          ? counts[patIdx]
          : patIdx === -2
            ? countWordOccurrences(haystack, term, true, 5)
            : 0;
      if (count > 0) {
        termScore += Math.min(count, 5);
        if (countWordOccurrences(titleLower, term, true, 1) > 0) {
          termScore += 3;
        }
      }
    }
  } else {
    for (const term of terms) {
      const count = countWordOccurrences(haystack, term);
      if (count > 0) termScore += Math.min(count, 5);
      if (countWordOccurrences(titleLower, term) > 0) termScore += 3;
    }
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

export function isUsableAgentId(id: string | undefined | null): id is string {
  if (typeof id !== "string") return false;
  const trimmed = id.trim();
  return (
    trimmed.length > 0 &&
    trimmed !== "agent" &&
    trimmed !== "vault" &&
    trimmed !== "unknown" &&
    trimmed !== "unknown-vault" &&
    trimmed !== "unknown-agent"
  );
}

function isUsablePathAgentId(id: string | undefined | null): id is string {
  if (!isUsableAgentId(id)) return false;
  const trimmed = id.trim();
  return (
    trimmed !== "default" &&
    trimmed !== "team" &&
    trimmed !== "shared"
  );
}

export function normalizeAgentId(agentId: string): string {
  const trimmed = agentId.trim();
  if (trimmed === "claudecode" || trimmed === "claude") return "claude-code";
  if (trimmed === "grok") return "grok-build";
  return trimmed;
}

export function resolveVaultAgent(vaultPath: string): string | undefined {
  const absPath = path.resolve(vaultPath.replace(/^~(?=$|\/)/, os.homedir()));
  // Explicit mappings win even when the basename equals the agent id (e.g.
  // MINNI_GEMINI_VAULT_PATH=/home/u/gemini). The basename fallback is only
  // consulted for *-vault names, and sentinels are filtered on both paths.
  const explicit = explicitAgentIdForVault(absPath);
  if (isUsableAgentId(explicit)) {
    return explicit;
  }
  if (path.basename(absPath).endsWith("-vault")) {
    const fromPath = getAgentIdFromVaultPath(vaultPath);
    if (isUsablePathAgentId(fromPath)) {
      return fromPath;
    }
  }
  return undefined;
}

function resolveConfiguredDefaultAgent(vaultPath: string): string | undefined {
  const absVault = path.resolve(vaultPath.replace(/^~(?=$|\/)/, os.homedir()));
  const envDefaultVault =
    process.env.MINNI_VAULT_PATH ?? process.env.MINNI_CODEX_VAULT_PATH;
  const activeDefaultVault = path.resolve(
    (envDefaultVault ?? DEFAULT_VAULT_PATH).replace(/^~(?=$|\/)/, os.homedir()),
  );
  if (absVault !== activeDefaultVault) {
    return undefined;
  }
  const rawDefaultAgent = (
    process.env.MINNI_AGENT_ID ??
    process.env.MINNI_CODEX_AGENT_ID ??
    DEFAULT_AGENT_ID
  )?.trim();
  return isUsableAgentId(rawDefaultAgent) ? rawDefaultAgent : undefined;
}

function resolveEffectiveVaultAgent(
  vaultPath: string,
  explicitAgentId?: string,
): string | undefined {
  if (isUsableAgentId(explicitAgentId)) {
    return explicitAgentId.trim();
  }
  const absPath = path.resolve(vaultPath.replace(/^~(?=$|\/)/, os.homedir()));
  const explicitMapped = explicitAgentIdForVault(absPath);
  if (isUsableAgentId(explicitMapped)) {
    return explicitMapped;
  }
  const configuredDefault = resolveConfiguredDefaultAgent(vaultPath);
  if (configuredDefault !== undefined) {
    return configuredDefault;
  }
  return resolveVaultAgent(vaultPath);
}

function schemaContent(agentId?: string): string {
  const heading = agentId ? `${agentId} Minni Vault` : "Minni Vault";
  return `# ${heading}

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

function indexContent(agentId?: string): string {
  const heading = agentId ? `${agentId} Minni Index` : "Minni Index";
  const maintained = agentId ? `the Minni ${agentId} plugin` : "the Minni plugin";
  return `# ${heading}

This index is maintained by ${maintained}.

## Recent Pages

`;
}

function logContent(agentId?: string): string {
  const heading = agentId ? `${agentId} Minni Log` : "Minni Log";
  const desc = agentId ? `Append-only audit of ${agentId} memory operations.` : "Append-only audit of memory operations.";
  return `# ${heading}

${desc}

`;
}

function isErrno(error: unknown, code: string): boolean {
  return (error as NodeJS.ErrnoException).code === code;
}

async function refuseSymlink(filePath: string, kind: "seed" | "append"): Promise<void> {
  try {
    const st = await lstat(filePath);
    if (st.isSymbolicLink()) {
      throw new Error(`refusing to ${kind} through symlink: ${filePath}`);
    }
    if (!st.isFile()) {
      throw new Error(`refusing to ${kind} non-regular file: ${filePath}`);
    }
  } catch (error) {
    if (!isErrno(error, "ENOENT")) {
      throw error;
    }
  }
}

async function readRegularTextFile(filePath: string): Promise<string> {
  const flags =
    fs.constants.O_RDONLY |
    (fs.constants.O_NOFOLLOW ?? 0) |
    (fs.constants.O_NONBLOCK ?? 0);
  const fh = await open(filePath, flags);
  try {
    const st = await fh.stat();
    if (!st.isFile()) {
      const err = new Error(`refusing to read non-regular file: ${filePath}`) as NodeJS.ErrnoException;
      err.code = "EINVAL";
      throw err;
    }
    return await fh.readFile("utf8");
  } finally {
    await fh.close();
  }
}

async function rejectSymlinkOrEscape(
  destPath: string,
  vaultPath: string,
  rel: string,
): Promise<void> {
  try {
    const st = await lstat(destPath);
    if (st.isSymbolicLink()) {
      throw new Error(
        `vault contract ${rel} is a symlink; refusing to seed through it: ${destPath}`,
      );
    }
  } catch (error) {
    if (!isErrno(error, "ENOENT")) {
      throw error;
    }
  }
  assertWriteTargetUnder(destPath, vaultPath);
}

async function seedExclusiveFile(
  filePath: string,
  content: string,
): Promise<boolean> {
  await refuseSymlink(filePath, "seed");
  let fh;
  try {
    fh = await open(filePath, "ax", 0o600);
  } catch (error) {
    if (isErrno(error, "EEXIST")) {
      return false;
    }
    throw error;
  }
  try {
    const st = await fh.stat();
    if (st.size > 0) {
      return false;
    }
    await fh.write(Buffer.from(content, "utf8"));
    return true;
  } finally {
    await fh.close();
  }
}

const vaultFileLocks = new Map<string, Promise<void>>();

async function withVaultFileLock<T>(
  filePath: string,
  fn: () => Promise<T>,
): Promise<T> {
  const key = path.resolve(filePath.replace(/^~(?=$|\/)/, os.homedir()));
  const previous = vaultFileLocks.get(key) ?? Promise.resolve();
  let releaseMem!: () => void;
  const current = new Promise<void>((resolve) => {
    releaseMem = resolve;
  });
  const tail = previous.then(
    () => current,
    () => current,
  );
  vaultFileLocks.set(key, tail);

  await previous.catch(() => {});
  const lockDigest = createHash("sha256").update(key).digest("hex").slice(0, 32);
  const lockPath = path.join(os.tmpdir(), `minni-vlock-${lockDigest}.lock`);
  let acquiredDisk = false;
  try {
    const deadline = Date.now() + 2000;
    while (Date.now() < deadline) {
      try {
        const lfh = await open(lockPath, "wx", 0o600);
        await lfh.close();
        acquiredDisk = true;
        break;
      } catch (err) {
        if (!isErrno(err, "EEXIST")) break;
        try {
          const lst = await stat(lockPath);
          if (Date.now() - lst.mtimeMs > 5000) {
            await unlink(lockPath).catch(() => {});
            continue;
          }
        } catch {
          continue;
        }
        await new Promise((r) => setTimeout(r, 5));
      }
    }
    if (!acquiredDisk) {
      throw new Error(`timed out acquiring vault file lock: ${lockPath}`);
    }
    return await fn();
  } finally {
    if (acquiredDisk) {
      await unlink(lockPath).catch(() => {});
    }
    releaseMem();
    if (vaultFileLocks.get(key) === tail) {
      vaultFileLocks.delete(key);
    }
  }
}

async function seedOrPromoteVaultFile(
  filePath: string,
  targetContent: string,
  untouchedGenericSeeds: readonly string[],
  shouldPromote: boolean,
): Promise<void> {
  const seeded = await seedExclusiveFile(filePath, targetContent);
  if (seeded || !shouldPromote) {
    return;
  }
  await withVaultFileLock(filePath, async () => {
    await refuseSymlink(filePath, "seed");
    let beforeStat;
    let existing: string;
    try {
      beforeStat = await lstat(filePath);
      if (beforeStat.isSymbolicLink()) {
        throw new Error(`refusing to seed through symlink: ${filePath}`);
      }
      if (!beforeStat.isFile()) {
        throw new Error(`refusing to seed non-regular file: ${filePath}`);
      }
      existing = await readRegularTextFile(filePath);
    } catch (error) {
      if (isErrno(error, "ENOENT")) {
        return;
      }
      throw error;
    }
    if (!untouchedGenericSeeds.includes(existing)) {
      return;
    }
    const afterStat = await lstat(filePath);
    if (
      afterStat.isSymbolicLink() ||
      afterStat.ino !== beforeStat.ino ||
      afterStat.size !== Buffer.byteLength(existing, "utf8")
    ) {
      return;
    }
    await writeFileAtomic(filePath, targetContent);
  });
}

export async function ensureVault(
  vaultPath: string,
  options?: string | { agentId?: string },
): Promise<EnsureVaultResult> {
  const created: string[] = [];
  try {
    const st = await lstat(vaultPath);
    if (st.isSymbolicLink()) {
      throw new Error(`refusing symlinked vault root: ${vaultPath}`);
    }
  } catch (error) {
    if (!isErrno(error, "ENOENT")) {
      throw error;
    }
  }
  await mkdir(vaultPath, { recursive: true });
  for (const dir of VAULT_DIRS) {
    const full = path.join(vaultPath, dir);
    await rejectSymlinkOrEscape(full, vaultPath, dir);
    await mkdir(full, { recursive: true });
    created.push(full);
  }

  const explicitAgentId =
    typeof options === "string" ? options : options?.agentId;
  const inferred = resolveEffectiveVaultAgent(vaultPath, explicitAgentId);
  const shouldPromote = inferred !== undefined;

  await seedOrPromoteVaultFile(
    path.join(vaultPath, "schema", "AGENTS.md"),
    schemaContent(inferred),
    [schemaContent(undefined)],
    shouldPromote,
  );
  await seedOrPromoteVaultFile(
    path.join(vaultPath, "index.md"),
    indexContent(inferred),
    ["# Minni Index\n\n", indexContent(undefined)],
    shouldPromote,
  );
  await seedOrPromoteVaultFile(
    path.join(vaultPath, "log.md"),
    logContent(inferred),
    ["# Minni Log\n\n", logContent(undefined)],
    shouldPromote,
  );

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

// Explicit tenant mappings only (MINNI_AGENT_VAULTS, the fixed platform
// *_VAULT_PATH env vars, ~/.minni/<agent>-vault layout). Undefined when nothing
// is configured.
function explicitAgentIdForVault(absPath: string): string | undefined {
  const mappingRaw = process.env.MINNI_AGENT_VAULTS;
  if (mappingRaw) {
    try {
      const mapping = JSON.parse(mappingRaw) as unknown;
      if (mapping && typeof mapping === "object" && !Array.isArray(mapping)) {
        for (const [agentId, mappedPath] of Object.entries(mapping as Record<string, unknown>)) {
          if (typeof mappedPath === "string") {
            const absMapped = path.resolve(mappedPath.replace(/^~(?=$|\/)/, os.homedir()));
            if (absMapped === absPath && typeof agentId === "string" && agentId.trim().length > 0) {
              return agentId.trim();
            }
          }
        }
      }
    } catch {}
  }

  // Each platform vault var is paired with its *_AGENT_ID override (same
  // fallback as config.ts), so a tenant vault resolves to its configured owner.
  const envKeys = [
    { key: "MINNI_CODEX_VAULT_PATH", agentKey: "MINNI_CODEX_AGENT_ID", id: "codex" },
    { key: "MINNI_CLAUDECODE_VAULT_PATH", agentKey: "MINNI_CLAUDECODE_AGENT_ID", id: "claude-code" },
    { key: "MINNI_KILOCODE_VAULT_PATH", agentKey: "MINNI_KILOCODE_AGENT_ID", id: "kilocode" },
    { key: "MINNI_GEMINI_VAULT_PATH", agentKey: "MINNI_GEMINI_AGENT_ID", id: "gemini" },
    { key: "MINNI_GROK_VAULT_PATH", agentKey: "MINNI_GROK_AGENT_ID", id: "grok-build" },
    { key: "MINNI_CURSOR_VAULT_PATH", agentKey: "MINNI_CURSOR_AGENT_ID", id: "cursor" },
  ];
  for (const { key, agentKey, id } of envKeys) {
    const val = process.env[key];
    if (val && path.resolve(val.replace(/^~(?=$|\/)/, os.homedir())) === absPath) {
      const override = process.env[agentKey]?.trim();
      return override && override.length > 0 ? override : id;
    }
  }

  // No generic MINNI_<AGENT>_VAULT_PATH reverse mapping: the env key is lossy
  // (team.alpha and team-alpha both become TEAM_ALPHA), so an id derived from
  // it can name a nonexistent principal. Use MINNI_AGENT_VAULTS for those.

  const homedir = os.homedir();
  if (absPath === path.join(homedir, ".minni", "codex-vault")) return "codex";
  if (absPath === path.join(homedir, ".minni", "claudecode-vault")) return "claude-code";
  if (absPath === path.join(homedir, ".minni", "kilocode-vault")) return "kilocode";
  if (absPath === path.join(homedir, ".minni", "gemini-vault")) return "gemini";
  if (absPath === path.join(homedir, ".minni", "grok-build-vault")) return "grok-build";
  if (absPath === path.join(homedir, ".minni", "cursor-vault")) return "cursor";
  return undefined;
}

export function getAgentIdFromVaultPath(vaultPath: string): string {
  const absPath = path.resolve(vaultPath.replace(/^~(?=$|\/)/, os.homedir()));
  const explicit = explicitAgentIdForVault(absPath);
  if (explicit !== undefined) return explicit;

  const base = path.basename(absPath);
  if (base.endsWith("-vault")) {
    const stripped = base.substring(0, base.length - 6);
    // Known basename aliases must normalize regardless of parent dir, so a
    // claudecode-vault under a non-default MINNI_HOME still maps to the
    // claude-code principal instead of a capability-less "claudecode".
    return normalizeAgentId(stripped);
  }
  return base || "agent";
}

export async function appendFileWithFsync(filePath: string, content: string): Promise<void> {
  await withVaultFileLock(filePath, async () => {
    const nofollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
    const nonblock = typeof fs.constants.O_NONBLOCK === "number" ? fs.constants.O_NONBLOCK : 0;
    const fh = await open(
      filePath,
      fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_CREAT | nofollow | nonblock,
    );
    try {
      const st = await fh.stat();
      if (!st.isFile()) {
        throw new Error(`refusing to append non-regular file: ${filePath}`);
      }
      await fh.writeFile(content, "utf8");
      await fh.sync();
    } finally {
      await fh.close();
    }
  });
}

export async function writeFileAtomic(filePath: string, content: string): Promise<void> {
  // #293 review round found two real gaps against callers (#231's active-plan
  // pointer, this fix's journal init + plan/vault note) that assumed this
  // helper's guarantee was complete:
  //
  // 1. Preserve the destination's existing mode. Exclusive-creating the unique
  //    tmp without an explicit mode uses the umask default; renaming it onto
  //    an existing, differently-permissioned file (e.g. operator-tightened
  //    to 0600) would silently widen it back to the default on every write —
  //    a permanent, silent permission downgrade nobody asked for.
  let mode: number | undefined;
  try {
    mode = (await stat(filePath)).mode & 0o777;
  } catch {
    // Destination does not exist yet — first write, default mode is correct.
  }

  const nofollow = typeof fs.constants.O_NOFOLLOW === "number" ? fs.constants.O_NOFOLLOW : 0;
  const flags =
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | nofollow;
  let lastErr: unknown;
  let wrote = false;
  for (let i = 0; i < 8; i++) {
    const tempPath = `${filePath}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`;
    let created = false;
    try {
      const fh = await open(tempPath, flags, mode ?? 0o666);
      created = true;
      try {
        await fh.writeFile(content, "utf8");
        await fh.sync();
      } finally {
        await fh.close();
      }
      await rename(tempPath, filePath);
      wrote = true;
      lastErr = undefined;
      break;
    } catch (error) {
      lastErr = error;
      if (created) {
        try {
          await unlink(tempPath);
        } catch {
          // Best-effort; rename may already have consumed the tmp.
        }
      }
      if (isErrno(error, "EEXIST")) {
        continue;
      }
      throw error;
    }
  }
  if (!wrote) {
    throw lastErr instanceof Error
      ? lastErr
      : new Error(`could not exclusive-create unique tmp for ${filePath}`);
  }

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
  //
  // rev5 §8.2 (Row j): <prefix>_session_start is also exempt so rapid
  // consecutive boots never drop a details.use_ledger stretch boundary.
  return (
    entry.tool.endsWith("_stop") ||
    entry.tool.endsWith("_bridge_failure") ||
    entry.tool.endsWith("_session_start")
  );
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
    // An explicit ensureVault agent can't be recovered from the path, so keep
    // the tenant the current log header was seeded with.
    const seededHeader = await readRegularTextFile(logPath)
      .then((text) => text.split("\n", 1)[0] ?? "")
      .catch(() => "");
    const seededAgent =
      seededHeader.match(/^# (.+) Minni Log$/)?.[1] ??
      (seededHeader === "# Minni Codex Log" ? "codex" : undefined);
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

    // Exclusive seed, then the append below. writeFileAtomic replace would
    // wipe a second tap that O_EXCL-created+appended the new log.md.
    await seedExclusiveFile(
      logPath,
      logContent(resolveEffectiveVaultAgent(vaultPath, seededAgent)),
    );
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

  await refuseSymlink(logPath, "append");
  await appendFileWithFsync(logPath, line);

  await seedExclusiveFile(dailyPath, `# ${date} Minni Audit\n\n`);
  await refuseSymlink(dailyPath, "append");
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
  const existing = await readRegularTextFile(indexPath);
  const link = wikilinkFor(relativePath);
  if (existing.includes(link)) return;
  const line = `- ${link} - ${summary.replace(/\s+/g, " ").slice(0, 160)}\n`;
  await appendFileWithFsync(indexPath, line);
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
  const agent = input.frontmatter?.agent;
  await ensureVault(
    input.vaultPath,
    typeof agent === "string" ? { agentId: agent } : undefined,
  );
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
  updateVaultSearchCacheOnWrite(input.vaultPath, notePath, relativePath, input.title, body);

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
      agent: resolveEffectiveVaultAgent(input.vaultPath, input.agentId) ?? "agent",
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
      ...(input.sessionId ? { session_id: input.sessionId } : {}),
      ...(input.annotation ? { annotation: input.annotation } : {}),
      ...(input.trigger_kind ? { trigger_kind: input.trigger_kind } : {}),
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
    text = await readRegularTextFile(target);
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
  options: { includeLatest?: boolean; useDays?: number } = {},
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
  if (options.useDays !== undefined) {
    const days = Number.isSafeInteger(options.useDays) ? Math.min(Math.max(options.useDays, 1), 90) : 7;
    try {
      const { buildUseReport } = await import("./use-report.js");
      report.use = buildUseReport(vaultPath, { days });
    } catch (error) {
      report.use = { error: error instanceof Error ? error.message : String(error) };
    }
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
  jev_learns?: number;
  jev_annotation?: string;
  /** Recall-to-use counts from the use ledger (rev5 §4.5); absent when nothing was surfaced. */
  use?: SessionUseCounts;
}

/**
 * One session's surfacings and the standing outcome of each, from the use
 * ledger. `pending` counts surfacings the detector has not judged yet.
 */
export interface SessionUseCounts {
  surfaced: number;
  /** Title/content citations. */
  cited: number;
  /** Handle-style citations (wikilink, slug, m:/d: handle, learning #id). */
  cited_handle: number;
  applied: number;
  opened: number;
  dismissed: number;
  ignored: number;
  unknown: number;
  pending: number;
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
    if (item.tool === "minni_learn") {
      receipt.learns += 1;
      const details = item.details as Record<string, unknown> | undefined;
      if (details && details.source === "jev_harvest") {
        receipt.jev_learns = (receipt.jev_learns ?? 0) + 1;
        const ann =
          (details.annotation as string) ||
          (details.trigger_kind
            ? `${details.trigger_kind}: ${item.summary}`
            : item.summary);
        if (!receipt.jev_annotation && ann) {
          receipt.jev_annotation = ann;
        }
      }
    }
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
  // useCounts: read the session's use counts from the ledger (default on).
  // The Stop hook passes it only when ledger-gate says the ledger may be on,
  // because loading the reader makes Stop pay for the ledger (§8.2).
  options: { includeStamped?: boolean; useCounts?: boolean } = {},
): Promise<SessionReceipt> {
  // Read the ROLLING log, not the daily file auditTail prefers: a session
  // that crosses midnight has its boot marker in yesterday's daily file, but
  // log.md carries both days (up to the 5 MB rotation, the receipt's honest
  // horizon).
  await ensureVault(vaultPath).catch(() => {});
  let text = "";
  try {
    text = await readRegularTextFile(path.join(vaultPath, "log.md"));
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

  const receipt = tallyWindow(
    parsed,
    sessionId,
    windowStart,
    windowEnd,
    windowSessionId,
    options.includeStamped === true,
  );
  // Use counts come from the ledger, keyed by the real session id. The
  // synthetic fallback (includeStamped: the payload had no id) has none.
  if (options.includeStamped !== true && options.useCounts !== false) {
    try {
      const { sessionUseCounts } = await import("./use-report.js");
      const use = sessionUseCounts(vaultPath, sessionId);
      if (use) receipt.use = use;
    } catch {
      // A ledger problem never costs the receipt.
    }
  }
  return receipt;
}

/**
 * Compact one-line proof-of-use string for the Stop systemMessage. Always names
 * the recall/guard/learn counts even when zero — a clean receipt (no recalls,
 * no guards) is itself the signal that memory was not exercised this session.
 */
export function formatSessionReceiptLine(receipt: SessionReceipt): string {
  const recalls = receipt.recalls_strong + receipt.recalls_weak;
  const jevSuffix =
    receipt.jev_learns && receipt.jev_learns > 0
      ? ` (${receipt.jev_annotation ?? "jev"}) [jev]`
      : "";
  // `learns` (committed minni_learn rows) and `candidates_drafted` are
  // distinct tallies — naming only one hid the other from the proof line.
  const parts = [
    `${recalls} recall${recalls === 1 ? "" : "s"} (${receipt.recalls_strong} strong)`,
    `${receipt.guard_denied} guard nudge${receipt.guard_denied === 1 ? "" : "s"}`,
    `${receipt.learns} learn${receipt.learns === 1 ? "" : "s"} committed${jevSuffix}`,
    `${receipt.candidates_drafted} candidate${receipt.candidates_drafted === 1 ? "" : "s"} staged`,
  ];
  const line = `Minni session receipt: ${parts.join(", ")}.`;
  // Byte-identical to the line above whenever nothing was surfaced; otherwise
  // the use counts ride on the same line, which maskTelemetry already drops.
  const use = receipt.use;
  if (!use || !(typeof use.surfaced === "number" && use.surfaced > 0)) return line;
  const count = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) ? value : 0);
  // "cited" on the receipt counts every citation, title/content and handle alike.
  const cited = count(use.cited) + count(use.cited_handle);
  return `${line} Use: ${count(use.surfaced)} surfaced, ${cited} cited, ${count(use.applied)} applied.`;
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
    text = await readRegularTextFile(path.join(vaultPath, "log.md"));
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

interface CachedVaultNoteEntry {
  notePath: string;
  relativePath: string;
  wikilink: string;
  title: string;
  markdown: string;
  privacy: PrivacyLevel | undefined;
  status: PageStatus | undefined;
  skipByStatus: boolean;
  haystackLower: string;
  titleLower: string;
  bodyLower: string;
  headingsLower: string[];
  durability: DurabilityKind;
  handle: string;
  isSessionNote: boolean;
  isMinniLearning: boolean;
  hasCorrectionBoost: boolean;
  wordCounts: Map<string, number>;
}

// The per-process search cache, keyed by vault path. It always holds the
// parsed notes; reuse is gated by the warm-cache rule in searchVaultNotes (at
// least 20 notes, built within the last 30 s). `tokenMap` is the inverted
// lookup index (see indexNoteTokensIntoMap), built only by the "index"
// candidate path and only on first use — a process that only ever runs the
// "scan" path never builds it. See VaultSearchCandidates.
interface VaultSearchCacheEntry {
  builtAtMs: number;
  notes: Map<string, CachedVaultNoteEntry>;
  tokenMap?: Map<string, Set<string>>;
}

const _vaultSearchCache = new Map<string, VaultSearchCacheEntry>();

/**
 * A note search never scores (superseded, rejected, expired or draft, or one
 * that names its successor). Shared by the search cache and the use
 * detector's DF recount, so both count the same notes.
 */
function skippedByStatus(markdown: string): boolean {
  const rawStatus = frontmatterField(markdown, "status")?.toLowerCase();
  return (
    rawStatus === "superseded" ||
    rawStatus === "rejected" ||
    rawStatus === "expired" ||
    rawStatus === "draft" ||
    Boolean(frontmatterField(markdown, "superseded_by"))
  );
}

/**
 * The vault's notes as the search scan walks them (`wiki/**\/*.md` through
 * listMarkdownFiles: containment on every entry, symlinks never followed),
 * as sorted vault-relative paths. For the use detector's DF recount, which
 * reads them a slice at a time at Stop (rev5 §4.4). Never throws.
 */
export async function listVaultNoteRelPaths(vaultPath: string): Promise<string[]> {
  try {
    const files = await listMarkdownFiles(path.join(vaultPath, "wiki"));
    return files.map((file) => path.relative(vaultPath, file)).sort();
  } catch {
    return [];
  }
}

/**
 * How many notes `listVaultNoteRelPaths` would list, from directory entries
 * alone: no note is opened and no path resolved per entry. listMarkdownFiles
 * keeps exactly the `*.md` entries that are regular files, inside directories
 * that are real directories (a symlink entry is neither, so it is never
 * followed), so the entry types give the same count. For the use detector's
 * daily early-trigger check (rev5 §4.4). Null on an unexpected failure.
 */
export function countVaultNotes(vaultPath: string): number | null {
  const walk = (dir: string): number => {
    let count = 0;
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.isDirectory()) count += walk(path.join(dir, entry.name));
      else if (entry.isFile() && entry.name.endsWith(".md")) count += 1;
    }
    return count;
  };
  try {
    return walk(path.join(vaultPath, "wiki"));
  } catch (error) {
    return (error as { code?: unknown } | null)?.code === "ENOENT" ? 0 : null;
  }
}

const DF_NOTE_READ_FLAGS =
  fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);

/**
 * One note's text for the DF recount, `title + "\n" + markdown` exactly as
 * the search cache holds it; null for a note search never indexes or scores
 * (`blocked` privacy, `skippedByStatus`), one outside the vault, anything not
 * a regular file (judged on the opened, non-blocking descriptor: a FIFO never
 * blocks), or one over `maxBytes`. Synchronous and bounded. Never throws.
 */
export function readVaultNoteTextForDf(vaultPath: string, relativePath: string, maxBytes: number): string | null {
  let fd: number | undefined;
  try {
    if (typeof relativePath !== "string" || relativePath.length === 0 || path.isAbsolute(relativePath)) return null;
    const notePath = path.join(vaultPath, relativePath);
    assertUnder(notePath, path.join(vaultPath, "wiki"));
    fd = fs.openSync(notePath, DF_NOTE_READ_FLAGS);
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > maxBytes) return null;
    const buffer = Buffer.alloc(stat.size);
    let filled = 0;
    while (filled < buffer.length) {
      const n = fs.readSync(fd, buffer, filled, buffer.length - filled, filled);
      if (n <= 0) break;
      filled += n;
    }
    const markdown = buffer.toString("utf8", 0, filled);
    if (privacyFromMarkdown(markdown) === "blocked" || skippedByStatus(markdown)) return null;
    if (durabilityOf({ path: relativePath, content: markdown }) === "noise") return null;
    return `${titleFromMarkdown(relativePath, markdown)}\n${markdown}`;
  } catch {
    return null;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // A read-only descriptor: nothing to lose.
      }
    }
  }
}

/**
 * How searchVaultNotes picks the notes it scores. On a cache whose notes were
 * all read from disk, both paths select exactly the same notes in exactly the
 * same order, so results, scores and ranking are identical (pinned by
 * tests/vault-search-candidates-parity.test.mjs); they differ only in cost.
 *
 * - "index" (default): build an inverted index from every lookup key of every
 *   note once per cache, then answer each query with a few map lookups. The
 *   build dominates a cold search (most of its cost on a vault of several
 *   hundred notes), so this is for a long-lived process that keeps the cache
 *   warm across queries — the MCP server, and the prepare_task / handoff
 *   packet paths it serves.
 * - "scan": no index; test each query key against each note's text directly.
 *   This is for a one-shot process that searches once and exits — the hook
 *   processes (UserPromptSubmit) — which would otherwise pay the full index
 *   build on every prompt to answer a single query. A query whose keys keep
 *   missing (see SCAN_BUDGET_PASSES) is handed to the index instead, so a
 *   scan never costs much more than the build it avoids.
 *
 * The caller chooses the path for its process type. The only switch inside a
 * path is that deterministic work budget, and it cannot change what is
 * selected, only what selecting it costs.
 *
 * After writeVaultPage writes or rewrites a note in the same process,
 * updateVaultSearchCacheOnWrite keeps cached.notes in deterministic sorted
 * order and rebuilds cached.tokenMap (when present) so stale keys are removed
 * and bucket ordering matches both "scan" and a cold index build.
 */
export type VaultSearchCandidates = "index" | "scan";
export type VaultSearchRankMode = "legacy" | "ups_gate" | "noise_last";

export interface VaultSearchOptions {
  candidates?: VaultSearchCandidates;
  rank?: VaultSearchRankMode;
  excludePaths?: Iterable<string>;
  rho?: number;
}

interface UpsGateEvaluation {
  gateScore: number;
  gateRoute: "a" | "b" | null;
  clearsGate: boolean;
  weightedScore: number;
  contributingTerms: { title: string[]; heading: string[]; body: string[] };
  coveredDistinctive: number;
}

function computeVaultHandleForRelPath(relativePath: string): string {
  const norm = relativePath.replaceAll("\\", "/").replace(/^\.\/+/, "").replace(/^\/+/, "");
  return `m:${createHash("sha1").update(norm, "utf8").digest("hex").slice(0, 8)}`;
}

function buildCachedVaultNoteEntry(
  notePath: string,
  relativePath: string,
  title: string,
  markdown: string,
  privacy: PrivacyLevel | undefined,
): CachedVaultNoteEntry {
  const status = statusFromMarkdown(markdown);
  const skipByStatus = skippedByStatus(markdown);
  const pageType = frontmatterField(markdown, "type")?.toLowerCase();
  const bodyWithoutFrontmatter = markdown.replace(FRONTMATTER_RE, "");
  const bodyLower = bodyWithoutFrontmatter.toLowerCase();
  const headingsLower: string[] = [];
  for (const match of bodyWithoutFrontmatter.matchAll(/^#{1,6}\s+(.+)$/gm)) {
    if (match[1]) {
      headingsLower.push(match[1].trim().toLowerCase());
    }
  }
  const rawDurability = durabilityOf({ path: relativePath, title, content: markdown });
  const durability: DurabilityKind =
    rawDurability === "ordinary" && /^\s*\[Subagent Context\]/i.test(title)
      ? "noise"
      : rawDurability;
  return {
    notePath,
    relativePath,
    wikilink: wikilinkFor(relativePath),
    title,
    markdown,
    privacy,
    status,
    skipByStatus,
    haystackLower: `${title}\n${relativePath}\n${markdown}`.toLowerCase(),
    titleLower: title.toLowerCase(),
    bodyLower,
    headingsLower,
    durability,
    handle: computeVaultHandleForRelPath(relativePath),
    isSessionNote: relativePath.startsWith("wiki/sessions/"),
    isMinniLearning: /minni_learning:\s*true/i.test(markdown),
    hasCorrectionBoost: Boolean(pageType && CORRECTION_CLASS_TYPES.has(pageType)),
    wordCounts: new Map<string, number>(),
  };
}

function evaluateUpsGate(
  effectiveQueryTrimLen: number,
  phrases: string[],
  terms: string[],
  distinctiveSet: Set<string>,
  k_q: number,
  entry: CachedVaultNoteEntry,
  rawScore: number,
): UpsGateEvaluation {
  const { haystackLower, titleLower, headingsLower, durability, wordCounts } = entry;
  const phraseHit =
    effectiveQueryTrimLen >= 24 &&
    terms.length >= 2 &&
    phrases.some(
      (p) => p.length >= 24 && countWordOccurrences(haystackLower, p, false, 1) > 0,
    );

  const titleTerms: string[] = [];
  const headingTerms: string[] = [];
  const bodyTerms: string[] = [];
  let coveredDistinctive = 0;
  let hasTitleOrHeadingDistinctive = false;

  for (const term of terms) {
    const inHaystack =
      (wordCounts.has(term)
        ? wordCounts.get(term)!
        : countWordOccurrences(haystackLower, term, true, 1)) > 0;
    if (!inHaystack) continue;
    const inTitle = countWordOccurrences(titleLower, term, true, 1) > 0;
    const inHeading = headingsLower.some((h) => countWordOccurrences(h, term, true, 1) > 0);
    if (inTitle) titleTerms.push(term);
    if (inHeading) headingTerms.push(term);
    if (!inTitle && !inHeading) bodyTerms.push(term);
    if (distinctiveSet.has(term)) {
      coveredDistinctive += 1;
      if (inTitle || inHeading) {
        hasTitleOrHeadingDistinctive = true;
      }
    }
  }

  const routeA = phraseHit;
  const routeB = coveredDistinctive >= k_q && hasTitleOrHeadingDistinctive;
  let gateRoute: "a" | "b" | null = null;
  let gateScore = 0;
  if (routeA) {
    gateRoute = "a";
    gateScore = 1.0;
  } else if (routeB) {
    gateRoute = "b";
    gateScore = Math.min(1.0, Math.max(0.55, coveredDistinctive / Math.max(1, k_q)));
  } else {
    gateRoute = null;
    gateScore = Math.min(0.50, rawScore / 100);
  }

  const classWeight = durability === "noise" ? 0.25 : 1.0;
  const weightedScore = classWeight * gateScore;
  const clearsGate = durability !== "noise" && (gateRoute === "a" || gateRoute === "b");
  return {
    gateScore,
    gateRoute,
    clearsGate,
    weightedScore,
    contributingTerms: {
      title: titleTerms,
      heading: headingTerms,
      body: bodyTerms,
    },
    coveredDistinctive,
  };
}

function ensureEntryWordCounts(
  terms: string[],
  entry: CachedVaultNoteEntry,
): Map<string, number> {
  const { haystackLower, titleLower, wordCounts } = entry;
  let missingCount = 0;
  if (terms.length > MULTI_TERM_THRESHOLD) {
    for (let i = 0; i < terms.length; i++) {
      if (!wordCounts.has(terms[i])) {
        missingCount++;
        if (missingCount > MULTI_TERM_THRESHOLD) break;
      }
    }
  }
  if (missingCount > MULTI_TERM_THRESHOLD) {
    const matcher = getMultiTermMatcher(terms);
    const counts = matcher.countAll(haystackLower, 5);
    const termToPatIdx = matcher.termToPatIdx;
    for (let i = 0; i < terms.length; i++) {
      const term = terms[i];
      if (!wordCounts.has(term)) {
        const patIdx = termToPatIdx[i];
        const count =
          patIdx >= 0
            ? counts[patIdx]
            : patIdx === -2
              ? countWordOccurrences(haystackLower, term, true, 5)
              : 0;
        const contrib =
          count > 0
            ? Math.min(count, 5) +
              (countWordOccurrences(titleLower, term, true, 1) > 0 ? 3 : 0)
            : 0;
        if (wordCounts.size < 4096) {
          wordCounts.set(term, contrib);
        }
      }
    }
  } else {
    for (const term of terms) {
      if (!wordCounts.has(term)) {
        const count = countWordOccurrences(haystackLower, term, true, 5);
        const contrib =
          count > 0
            ? Math.min(count, 5) +
              (countWordOccurrences(titleLower, term, true, 1) > 0 ? 3 : 0)
            : 0;
        if (wordCounts.size < 4096) {
          wordCounts.set(term, contrib);
        }
      }
    }
  }
  return wordCounts;
}

function scoreVaultNoteCached(
  phrases: string[],
  terms: string[],
  entry: CachedVaultNoteEntry,
): number {
  if (entry.skipByStatus) return 0;
  const { haystackLower, titleLower } = entry;
  const wordCounts = ensureEntryWordCounts(terms, entry);
  let termScore = 0;
  for (const phrase of phrases) {
    if (countWordOccurrences(haystackLower, phrase, false, 1) > 0) {
      termScore += 50;
      break;
    }
  }
  for (const term of terms) {
    let contrib = wordCounts.get(term);
    if (contrib === undefined) {
      const count = countWordOccurrences(haystackLower, term, true, 5);
      contrib =
        count > 0
          ? Math.min(count, 5) +
            (countWordOccurrences(titleLower, term, true, 1) > 0 ? 3 : 0)
          : 0;
    }
    termScore += contrib;
  }
  if (termScore < MIN_TERM_SCORE) return 0;

  let score = termScore;
  if (entry.isSessionNote) score += 1;
  if (entry.isMinniLearning) score += 2;
  if (entry.hasCorrectionBoost) {
    score *= 1 + CORRECTION_SALIENCE_BOOST;
  }
  return score;
}

function indexNoteTokensIntoMap(
  tokenMap: Map<string, Set<string>>,
  notePath: string,
  haystackLower: string,
): void {
  const rawMatches = haystackLower.match(/[a-z0-9_]{2,}/g) ?? [];
  const keys = new Set<string>();
  const addTokenKeys = (tok: string): void => {
    keys.add(tok);
    if (tok.length > 3 && tok.endsWith("s")) {
      keys.add(tok.slice(0, -1));
    }
    if (tok.length >= 6 && (tok.endsWith("es") || tok.endsWith("ed"))) {
      keys.add(tok.slice(0, -2));
    }
    if (tok.length >= 7 && tok.endsWith("ing")) {
      keys.add(tok.slice(0, -3));
    }
    const maxPrefix = Math.min(tok.length - 1, 10);
    for (let len = 3; len <= maxPrefix; len++) {
      keys.add(tok.slice(0, len));
    }
  };
  for (const tok of rawMatches) {
    addTokenKeys(tok);
    if (tok.includes("_")) {
      const starts: number[] = [0];
      const ends: number[] = [];
      for (let i = 0; i < tok.length; i++) {
        if (tok.charCodeAt(i) === 95) {
          ends.push(i);
          if (i + 1 < tok.length) starts.push(i + 1);
        }
      }
      ends.push(tok.length);
      for (const s of starts) {
        if (s > 0 && tok.length - s >= 2) {
          addTokenKeys(tok.slice(s));
        }
        for (const e of ends) {
          if (e < tok.length && e - s >= 11) {
            addTokenKeys(tok.slice(s, e));
          }
        }
      }
      for (const sub of tok.split("_")) {
        if (sub.length >= 2) addTokenKeys(sub);
      }
    }
  }
  for (const key of keys) {
    let bucket = tokenMap.get(key);
    if (!bucket) {
      bucket = new Set<string>();
      tokenMap.set(key, bucket);
    }
    bucket.add(notePath);
  }
}

// The "index" path's index, built on first use from the cached notes in their
// cache order — the same bucket order an index built while reading them has.
function vaultSearchTokenMap(
  cache: VaultSearchCacheEntry,
): Map<string, Set<string>> {
  if (!cache.tokenMap) {
    const tokenMap = new Map<string, Set<string>>();
    for (const entry of cache.notes.values()) {
      if (!entry.skipByStatus) {
        indexNoteTokensIntoMap(tokenMap, entry.notePath, entry.haystackLower);
      }
    }
    cache.tokenMap = tokenMap;
  }
  return cache.tokenMap;
}

/**
 * The keys a query is looked up under, in lookup order: for each term, the
 * first `[a-z0-9_]{2,}` run of the term, then that run minus a trailing `s`
 * when it is longer than 3 characters. Undefined when some term has no such
 * run (or is not a plain `[a-z0-9_/-]` token), which makes every cached note a
 * candidate.
 */
function candidateLookupKeys(terms: string[]): string[] | undefined {
  const lookupSubtokens = terms.map((term) => {
    const t = term.toLowerCase();
    return /^[a-z0-9_][a-z0-9_/-]*[a-z0-9_]$/.test(t)
      ? (t.match(/[a-z0-9_]{2,}/g) ?? [])
      : [];
  });
  const allIndexableTokens =
    lookupSubtokens.length > 0 && lookupSubtokens.every((subs) => subs.length > 0);
  if (!allIndexableTokens) return undefined;
  const keys: string[] = [];
  for (const subs of lookupSubtokens) {
    const tok = subs[0];
    keys.push(tok);
    if (tok.length > 3 && tok.endsWith("s")) keys.push(tok.slice(0, -1));
  }
  return keys;
}

// End of the run of token characters ([a-z0-9_], indexNoteTokensIntoMap's
// regex) starting at `from`; with `pieceOnly`, `_` ends the run too (a
// `_`-split subtoken).
function indexTokenRunEnd(text: string, from: number, pieceOnly: boolean): number {
  let end = from;
  while (end < text.length) {
    const code = text.charCodeAt(end);
    if (!(isAsciiLowerWordChar(code) || (!pieceOnly && code === 95))) break;
    end++;
  }
  return end;
}

// Whether indexNoteTokensIntoMap's addTokenKeys(T) emits `key`, for a token T
// = text[start, end) already known to begin with `key`. Every key it emits is
// a prefix of T, so the only question is which prefix lengths it emits:
//   - T itself;
//   - T minus `s` (T longer than 3), minus `es`/`ed` (T at least 6), minus
//     `ing` (T at least 7);
//   - every prefix of 3..min(|T| - 1, 10) characters.
// So a key of 3..10 characters is emitted by every T it begins (T itself or a
// longer T's prefix), a 2-character key only by T equal to it (no rule above
// yields a 2-character key from a longer T), and a key of 11+ characters only
// by T equal to it or to it plus `s`, `es`, `ed` or `ing`.
function indexTokenEmitsKey(
  text: string,
  start: number,
  end: number,
  key: string,
): boolean {
  const keyLen = key.length;
  if (keyLen >= 3 && keyLen <= 10) return true;
  const rest = end - start - keyLen;
  if (keyLen < 3) return rest === 0;
  const at = start + keyLen;
  switch (rest) {
    case 0:
      return true;
    case 1:
      return text.charCodeAt(at) === 115; // s
    case 2:
      return (
        text.charCodeAt(at) === 101 && // e
        (text.charCodeAt(at + 1) === 115 || text.charCodeAt(at + 1) === 100) // s, d
      );
    case 3:
      return text.startsWith("ing", at);
    default:
      return false;
  }
}

/**
 * Whether the "index" path's index would list this note under `key`, decided
 * from the note's text alone. `key` is a `[a-z0-9_]{2,}` lookup key (see
 * candidateLookupKeys); `haystackLower` is the text indexNoteTokensIntoMap
 * indexes.
 *
 * The index lists a note under every key that addTokenKeys emits for one of
 * its tokens, where a token is a maximal `[a-z0-9_]{2,}` run of the text, or,
 * for a run containing `_`, any `_`-preceded suffix or `_`-separated piece of
 * 2+ characters. Every key is a prefix of the token that emits it, so it
 * suffices to visit each occurrence of `key` preceded by a non-`[a-z0-9]`
 * character (a run start or `_`) and ask whether a run/suffix or piece starts
 * there that emits it.
 */
function haystackHasIndexKey(haystackLower: string, key: string): boolean {
  const keyLen = key.length;
  if (keyLen === 0) return false;
  // A 3..10-character key is emitted by any token it begins, so its end
  // does not matter (indexTokenEmitsKey ignores it).
  const needsEnd = keyLen < 3 || keyLen > 10;
  let at = haystackLower.indexOf(key);
  while (at !== -1) {
    const prev = at > 0 ? haystackLower.charCodeAt(at - 1) : -1;
    if (!isAsciiLowerWordChar(prev)) {
      const end = needsEnd ? indexTokenRunEnd(haystackLower, at + keyLen, false) : 0;
      if (indexTokenEmitsKey(haystackLower, at, end, key)) return true;
      const pieceEnd = needsEnd ? indexTokenRunEnd(haystackLower, at + keyLen, true) : 0;
      if (indexTokenEmitsKey(haystackLower, at, pieceEnd, key)) return true;
    }
    at = haystackLower.indexOf(key, at + 1);
  }
  return false;
}

// "index" path: union the index buckets of each key, in key order.
function indexCandidateEntries(
  cache: VaultSearchCacheEntry,
  keys: string[],
): CachedVaultNoteEntry[] {
  const tokenMap = vaultSearchTokenMap(cache);
  const matchedPaths = new Set<string>();
  for (const key of keys) {
    const bucket = tokenMap.get(key);
    if (bucket) {
      for (const p of bucket) matchedPaths.add(p);
    }
  }
  const subset: CachedVaultNoteEntry[] = [];
  for (const p of matchedPaths) {
    const entry = cache.notes.get(p);
    if (entry && !entry.skipByStatus) subset.push(entry);
  }
  return subset;
}

// The scan path's work budget, in full passes over the text of the notes it
// tests. Testing a key against a note it does not admit reads the note's whole
// text, and a note that is never admitted is read again for every key; the
// number of keys is the number of distinct query terms, which nothing caps.
// So a query of many keys that match nothing (a pasted hex dump, a UUID list,
// code full of long identifiers) would cost a pass per key, and the hook would
// end up slower than the index build the scan exists to avoid. Once the reads
// spent on notes a key did not admit exceed this many passes, the scan stops
// and hands the query to the index path, which selects the same candidates in
// the same order. The worst case is then about this many passes plus one
// index build. It counts characters, not time, so the switch is deterministic.
// A note a key does admit is not counted: it leaves the scan, so all of those
// reads together come to at most one more pass.
//
// Derived, not measured on one vault: the cold index build of a real
// 783-note, 12 MB vault measured 265-306 ms. A pass for a key that matches
// nothing, measured on a synthetic vault of the same size (783 notes, 12.6 MB,
// English-like letter frequencies, two-byte text), costs 1-21 ms by key shape:
// about 2 ms for long hex ids, 4 ms for keys starting with a rare letter,
// 21 ms for short keys starting with `e` (every `e` is a substring-search
// candidate). Against the worst shape the crossover is 265 / 21.4 = 12.4
// passes, rounded down to 12. A worst-shape query then costs at most about two
// index builds; a typical one, one build and a little.
const SCAN_BUDGET_PASSES = 12;

// "scan" path: the union an index built from the cached notes gives, in the
// same order, without the index. The index path admits a note at the first
// key whose bucket holds it, and walks each bucket in cache order; so does
// this — keys in order, the notes not yet admitted in cache order — and a note
// admitted once is never tested again. Undefined once the work budget above
// is spent; the caller then runs the index path.
function scanCandidateEntries(
  cache: VaultSearchCacheEntry,
  keys: string[],
): CachedVaultNoteEntry[] | undefined {
  let pending: CachedVaultNoteEntry[] = [];
  let passChars = 0;
  for (const entry of cache.notes.values()) {
    if (!entry.skipByStatus) {
      pending.push(entry);
      passChars += entry.haystackLower.length;
    }
  }
  const budgetChars = SCAN_BUDGET_PASSES * passChars;
  let spentChars = 0;
  const subset: CachedVaultNoteEntry[] = [];
  const seenKeys = new Set<string>();
  for (const key of keys) {
    if (pending.length === 0) break;
    // A repeated key admits nothing the first visit did not.
    if (seenKeys.has(key)) continue;
    seenKeys.add(key);
    const stillPending: CachedVaultNoteEntry[] = [];
    for (const entry of pending) {
      if (haystackHasIndexKey(entry.haystackLower, key)) {
        subset.push(entry);
      } else {
        stillPending.push(entry);
        spentChars += entry.haystackLower.length;
      }
    }
    pending = stillPending;
    if (spentChars > budgetChars) return undefined;
  }
  return subset;
}

function selectCandidateEntries(
  cache: VaultSearchCacheEntry,
  terms: string[],
  candidates: VaultSearchCandidates,
): Iterable<CachedVaultNoteEntry> {
  const keys =
    cache.notes.size >= 20 ? candidateLookupKeys(terms) : undefined;
  if (!keys) return cache.notes.values();
  if (candidates === "scan") {
    const scanned = scanCandidateEntries(cache, keys);
    if (scanned) return scanned;
  }
  return indexCandidateEntries(cache, keys);
}

/**
 * What this process's search cache holds for `vaultPath`: how many notes, and
 * whether the "index" path's index has been built. Undefined when nothing is
 * cached. Read-only; it never builds or refreshes anything.
 */
export function vaultSearchCacheState(
  vaultPath: string,
): { notes: number; prefixIndex: boolean } | undefined {
  const cache = _vaultSearchCache.get(vaultPath);
  if (!cache) return undefined;
  return { notes: cache.notes.size, prefixIndex: cache.tokenMap !== undefined };
}

/**
 * The candidates `candidates` selects for `query` from this process's cache
 * for `vaultPath`, as relative paths in the order they are scored — the
 * selection searchVaultNotes runs, exposed so the parity test can compare the
 * two paths' candidates directly rather than only their scored results.
 * Undefined when nothing is cached; it never reads the vault. The "index"
 * path builds its index here if it is missing, exactly as a search would.
 */
export function vaultSearchCandidatePaths(
  vaultPath: string,
  query: string,
  candidates: VaultSearchCandidates,
): string[] | undefined {
  const cache = _vaultSearchCache.get(vaultPath);
  if (!cache) return undefined;
  return Array.from(
    selectCandidateEntries(cache, queryTerms(query), candidates),
    (entry) => entry.relativePath,
  );
}

function updateVaultSearchCacheOnWrite(
  vaultPath: string,
  notePath: string,
  relativePath: string,
  title: string,
  markdown: string,
): void {
  const cached = _vaultSearchCache.get(vaultPath);
  if (!cached) return;
  const wikiRel = path.relative(path.join(vaultPath, "wiki"), notePath);
  if (!wikiRel || wikiRel.startsWith("..") || path.isAbsolute(wikiRel)) return;
  const privacy = privacyFromMarkdown(markdown);
  if (privacy === "blocked") {
    cached.notes.delete(notePath);
    if (cached.tokenMap) {
      cached.tokenMap = undefined;
      vaultSearchTokenMap(cached);
    }
    cached.builtAtMs = Date.now();
    return;
  }
  const resolvedTitle = titleFromMarkdown(relativePath, markdown) || title;
  const entry = buildCachedVaultNoteEntry(
    notePath,
    relativePath,
    resolvedTitle,
    markdown,
    privacy,
  );
  if (cached.notes.has(notePath)) {
    cached.notes.set(notePath, entry);
  } else {
    const entries = [...cached.notes.entries(), [notePath, entry] as const].sort(
      (a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0),
    );
    cached.notes = new Map(entries);
  }
  if (cached.tokenMap) {
    cached.tokenMap = undefined;
    vaultSearchTokenMap(cached);
  }
  cached.builtAtMs = Date.now();
}

export async function searchVaultNotes(
  vaultPath: string,
  query: string,
  limit = 5,
  agentId?: string | VaultSearchOptions,
  options: VaultSearchOptions = {},
): Promise<VaultSearchResult[]> {
  const resolvedAgentId = typeof agentId === "string" ? agentId : undefined;
  const resolvedOptions =
    typeof agentId === "object" && agentId !== null ? agentId : options;
  const candidates = resolvedOptions.candidates ?? "index";
  const rankMode = resolvedOptions.rank ?? "legacy";
  const effectiveQuery = rankMode === "ups_gate" ? stripHarnessBlocks(query) : query;
  const terms = queryTerms(effectiveQuery);
  const phrases = phraseForms(effectiveQuery);
  // Only bail when there is nothing to match on at all. Bailing on an empty
  // term list alone threw away the exact-phrase bonus, which is the strongest
  // signal in the scorer and needs no tokens — but phraseForms yields nothing
  // for a query with no phrase of substance, so `-` or `--` (no tokens, no
  // usable phrase) stops here instead of reaching the scorer with terms = [].
  options = resolvedOptions;
  if (
    (rankMode === "ups_gate" && terms.length === 0) ||
    (terms.length === 0 && phrases.length === 0)
  ) {
    return [];
  }
  let cache = _vaultSearchCache.get(vaultPath);
  const now = Date.now();
  const hasWarmCache = Boolean(
    cache && cache.notes.size >= 20 && now - cache.builtAtMs <= 30_000,
  );
  if (!hasWarmCache && resolvedAgentId !== undefined && candidates !== "scan") {
    await ensureVault(vaultPath, { agentId: resolvedAgentId });
  }

  const wikiRoot = path.join(vaultPath, "wiki");
  if (!hasWarmCache || !cache) {
    let files: string[] = [];
    try {
      files = await listMarkdownFiles(wikiRoot);
    } catch {
      return [];
    }
    files.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    const loaded = await Promise.all(
      files.map(async (notePath) => {
        const markdown = await readFile(notePath, "utf8");
        const privacy = privacyFromMarkdown(markdown);
        if (privacy === "blocked") return undefined;
        const relativePath = path.relative(vaultPath, notePath);
        const title = titleFromMarkdown(relativePath, markdown);
        const entry = buildCachedVaultNoteEntry(
          notePath,
          relativePath,
          title,
          markdown,
          privacy,
        );
        return [notePath, entry] as const;
      }),
    );
    const notes = new Map<string, CachedVaultNoteEntry>();
    for (const item of loaded) {
      if (item) notes.set(item[0], item[1]);
    }
    cache = { builtAtMs: now, notes };
    _vaultSearchCache.set(vaultPath, cache);
  }
  // The "index" path keeps its index current for every later search and
  // write in this process, as it always has; the "scan" path never builds it.
  if (candidates === "index") vaultSearchTokenMap(cache);

  const candidateEntries = Array.from(
    selectCandidateEntries(cache, terms, candidates),
  );

  let excludeSet: Set<string> | undefined;
  if (options.excludePaths) {
    excludeSet = new Set<string>();
    for (const p of options.excludePaths) {
      if (typeof p === "string" && p.length > 0) {
        excludeSet.add(p);
        excludeSet.add(p.replaceAll("\\", "/").replace(/^\.\/+/, "").replace(/^\/+/, ""));
      }
    }
    if (excludeSet.size === 0) excludeSet = undefined;
  }

  const effectiveQueryTrimLen = effectiveQuery.trim().length;
  let distinctiveSet: Set<string> | undefined;
  let k_q = 3;
  if (rankMode === "ups_gate") {
    let nonNoiseCount = 0;
    const df = new Map<string, number>();
    for (const t of terms) df.set(t, 0);
    for (const entry of cache.notes.values()) {
      if (entry.skipByStatus || entry.durability === "noise") continue;
      if (
        excludeSet &&
        (excludeSet.has(entry.relativePath) ||
          excludeSet.has(entry.relativePath.replaceAll("\\", "/")) ||
          excludeSet.has(entry.notePath))
      ) {
        continue;
      }
      nonNoiseCount += 1;
    }
    for (const entry of candidateEntries) {
      if (entry.skipByStatus || entry.durability === "noise") continue;
      if (
        excludeSet &&
        (excludeSet.has(entry.relativePath) ||
          excludeSet.has(entry.relativePath.replaceAll("\\", "/")) ||
          excludeSet.has(entry.notePath))
      ) {
        continue;
      }
      const wordCounts = ensureEntryWordCounts(terms, entry);
      for (const t of terms) {
        const present =
          (wordCounts.has(t)
            ? wordCounts.get(t)!
            : countWordOccurrences(entry.haystackLower, t, true, 1)) > 0;
        if (present) {
          df.set(t, (df.get(t) ?? 0) + 1);
        }
      }
    }
    const dfThreshold = Math.max(1, Math.ceil(0.05 * Math.max(1, nonNoiseCount)));
    const distinctiveQueryTerms = terms.filter((t) => {
      const d = df.get(t) ?? 0;
      return d > 0 && d <= dfThreshold;
    });
    distinctiveSet = new Set(distinctiveQueryTerms);
    const rho =
      typeof options.rho === "number" && Number.isFinite(options.rho)
        ? options.rho
        : 0.2;
    k_q = Math.max(3, Math.ceil(rho * distinctiveQueryTerms.length));
  }

  const scored: Array<{
    entry: CachedVaultNoteEntry;
    score: number;
    gate?: UpsGateEvaluation;
  }> = [];

  for (const entry of candidateEntries) {
    if (
      excludeSet &&
      (excludeSet.has(entry.relativePath) ||
        excludeSet.has(entry.relativePath.replaceAll("\\", "/")) ||
        excludeSet.has(entry.notePath))
    ) {
      continue;
    }
    const score = scoreVaultNoteCached(phrases, terms, entry);
    if (score > 0) {
      const gate =
        rankMode === "ups_gate"
          ? evaluateUpsGate(
              effectiveQueryTrimLen,
              phrases,
              terms,
              distinctiveSet!,
              k_q,
              entry,
              score,
            )
          : undefined;
      scored.push({ entry, score, gate });
    }
  }

  if (rankMode === "ups_gate") {
    scored.sort((a, b) => {
      const ga = a.gate!;
      const gb = b.gate!;
      if (ga.clearsGate !== gb.clearsGate) {
        return gb.clearsGate ? 1 : -1;
      }
      return (
        gb.weightedScore - ga.weightedScore ||
        b.score - a.score ||
        a.entry.relativePath.localeCompare(b.entry.relativePath)
      );
    });
  } else if (rankMode === "noise_last") {
    scored.sort((a, b) => {
      const na = a.entry.durability === "noise" ? 1 : 0;
      const nb = b.entry.durability === "noise" ? 1 : 0;
      if (na !== nb) {
        return na - nb;
      }
      return (
        b.score - a.score ||
        a.entry.relativePath.localeCompare(b.entry.relativePath)
      );
    });
  } else {
    scored.sort(
      (a, b) =>
        b.score - a.score ||
        a.entry.relativePath.localeCompare(b.entry.relativePath),
    );
  }

  return scored.slice(0, limit).map(({ entry, score, gate }) => {
    const base: VaultSearchResult = {
      notePath: entry.notePath,
      relativePath: entry.relativePath,
      wikilink: entry.wikilink,
      title: entry.title,
      snippet: snippetFor(entry.markdown, terms),
      score,
      privacy: entry.privacy,
      status: entry.status,
      durability: entry.durability,
    };
    if (gate) {
      base.gateScore = gate.gateScore;
      base.gateRoute = gate.gateRoute;
      base.clearsGate = gate.clearsGate;
      base.weightedScore = gate.weightedScore;
      base.contributingTerms = gate.contributingTerms;
      base.coveredDistinctive = gate.coveredDistinctive;
    }
    return base;
  });
}

/**
 * Resolve a short vault handle (`m:<8hex>`) to a vault-relative path under
 * `wiki/` using the search cache when warm or `listVaultNoteRelPaths`.
 * Returns `null` when the handle is malformed or no matching note exists.
 */
export async function resolveVaultHandleRelPath(
  vaultPath: string,
  handle: string,
): Promise<string | null> {
  if (typeof handle !== "string") return null;
  const trimmed = handle.trim().toLowerCase();
  if (!/^m:[0-9a-f]{8}$/.test(trimmed)) return null;
  const cached = _vaultSearchCache.get(vaultPath);
  if (cached) {
    for (const entry of cached.notes.values()) {
      if (entry.handle === trimmed && !entry.skipByStatus) {
        if (await exists(entry.notePath)) {
          return entry.relativePath;
        }
      }
    }
  }
  const relPaths = await listVaultNoteRelPaths(vaultPath);
  for (const rel of relPaths) {
    if (computeVaultHandleForRelPath(rel) === trimmed) {
      return rel;
    }
  }
  return null;
}

export type ResolvedVaultHandleNote =
  | {
      status: "resolved";
      relativePath: string;
      title: string;
      content: string;
      wikilink: string;
    }
  | {
      status: "unresolved";
      reason: "missing" | "unsafe";
    };

/**
 * Resolve a short vault handle (`m:<8hex>`) locally against `vaultPath`,
 * enforcing containment, non-symlink file check, status check, and
 * `filterSafeVaultResults` privacy gating (rev5 §5.2.6 / §5.3).
 */
export async function resolveSafeVaultHandleNote(
  vaultPath: string,
  handle: string,
): Promise<ResolvedVaultHandleNote> {
  const relPath = await resolveVaultHandleRelPath(vaultPath, handle);
  if (!relPath) {
    return { status: "unresolved", reason: "missing" };
  }
  const notePath = path.join(vaultPath, relPath);
  try {
    assertUnder(notePath, vaultPath);
    const st = await lstat(notePath);
    if (st.isSymbolicLink() || !st.isFile()) {
      return { status: "unresolved", reason: "unsafe" };
    }
    const markdown = await readFile(notePath, "utf8");
    const privacy = privacyFromMarkdown(markdown);
    if (privacy === "blocked" || skippedByStatus(markdown)) {
      return { status: "unresolved", reason: "unsafe" };
    }
    const normalizedRel = relPath.replaceAll("\\", "/");
    const title = titleFromMarkdown(normalizedRel, markdown);
    const wikilink = wikilinkFor(normalizedRel);
    const candidate: VaultSearchResult = {
      notePath,
      relativePath: normalizedRel,
      wikilink,
      title,
      snippet: markdown.slice(0, 520),
      score: 1,
      privacy,
      status: statusFromMarkdown(markdown),
    };
    if (filterSafeVaultResults([candidate]).length === 0) {
      return { status: "unresolved", reason: "unsafe" };
    }
    return {
      status: "resolved",
      relativePath: normalizedRel,
      title,
      content: markdown,
      wikilink,
    };
  } catch {
    return { status: "unresolved", reason: "missing" };
  }
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
  await ensureVault(vaultPath).catch((error) => {
    if (!String((error as Error)?.message ?? "").includes("non-regular file")) {
      throw error;
    }
  });
  await mkdir(path.join(vaultPath, "inbox"), { recursive: true });
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

/**
 * S2a (§6.2.4, §8.3): 4,000-char ceiling for the inlined Layer 1 shelf boot
 * form. The raw reader (`readLayer1Shelf`, `LAYER1_SHELF_MAX_BYTES = 8192`)
 * stays untouched so `resolveActivePlanView` / `shelfDrift` always hash the
 * full shelf content before any boot-form inline trim occurs.
 */
export const LAYER1_BOOT_FORM_MAX_CHARS = 4000;

/**
 * Safely read `<vault>/layer1/boot.md` when present as a regular file inside
 * the vault. Refuses symlinks (`O_NOFOLLOW`) and FIFOs (`O_NONBLOCK` +
 * `fstatSync(fd).isFile()`). Returns `null` when absent, empty, or refused.
 */
export function readLayer1BootFileSync(vaultPath: string): string | null {
  if (!vaultPath || typeof vaultPath !== "string") return null;
  const bootPath = path.join(vaultPath, "layer1", "boot.md");
  let fd: number | undefined;
  try {
    assertWriteTargetUnder(bootPath, vaultPath);
    const flags =
      fs.constants.O_RDONLY |
      (fs.constants.O_NOFOLLOW ?? 0) |
      (fs.constants.O_NONBLOCK ?? 0);
    fd = fs.openSync(bootPath, flags);
    const st = fs.fstatSync(fd);
    if (!st.isFile() || st.size <= 0) return null;
    const cap = Math.min(st.size, LAYER1_SHELF_MAX_BYTES);
    const buf = Buffer.alloc(cap);
    let total = 0;
    while (total < cap) {
      const n = fs.readSync(fd, buf, total, cap - total, total);
      if (n <= 0) break;
      total += n;
    }
    if (total <= 0) return null;
    const text = buf.subarray(0, total).toString("utf8");
    return text.length > 0 ? text : null;
  } catch {
    return null;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // Ignore close errors.
      }
    }
  }
}

/**
 * Build the `<= 4,000`-char Layer 1 boot form string from `sourceContent`
 * (`layer1/boot.md` when present, else `layer1/core.md`), clipping at a
 * paragraph boundary (`\n\n`) when needed and appending the explicit marker:
 * `[layer1 shelf: boot form, <n> of <m> chars; full file: layer1/core.md]`.
 */
export function formatLayer1BootFormContent(
  sourceContent: string,
  totalCoreChars: number,
  maxChars: number = LAYER1_BOOT_FORM_MAX_CHARS,
): string {
  const m = Math.max(totalCoreChars, sourceContent.length);
  const worstMarker = `\n\n[layer1 shelf: boot form, ${maxChars} of ${Math.max(m, maxChars)} chars; full file: layer1/core.md]`;
  const maxExcerpt = Math.max(0, maxChars - worstMarker.length);
  let excerpt: string;
  if (sourceContent.length <= maxExcerpt) {
    excerpt = sourceContent;
  } else {
    const slice = sourceContent.slice(0, maxExcerpt);
    const lastParagraph = slice.lastIndexOf("\n\n");
    excerpt = lastParagraph > 0 ? slice.slice(0, lastParagraph) : slice;
  }
  const n = excerpt.length;
  const sep = n > 0 ? "\n\n" : "";
  return `${excerpt}${sep}[layer1 shelf: boot form, ${n} of ${m} chars; full file: layer1/core.md]`;
}

/**
 * Convert a `layer1_shelf` envelope object into its `<= 4,000`-char boot form,
 * preserving all reader fields (`ok`, `truncated`, `omitted_bytes`, `note`) and
 * setting `inline_trim: true`.
 */
export function applyLayer1BootForm(
  shelfBody: Record<string, unknown>,
  vaultPath?: string,
  maxChars: number = LAYER1_BOOT_FORM_MAX_CHARS,
): Record<string, unknown> {
  if (
    !shelfBody ||
    typeof shelfBody !== "object" ||
    shelfBody.ok !== true ||
    typeof shelfBody.content !== "string"
  ) {
    return shelfBody;
  }
  if (shelfBody.inline_trim === true && shelfBody.content.length <= maxChars) {
    return shelfBody;
  }
  const rawContent = shelfBody.content;
  const omittedBytes =
    shelfBody.truncated === true && typeof shelfBody.omitted_bytes === "number"
      ? shelfBody.omitted_bytes
      : 0;
  const totalCoreChars =
    shelfBody.truncated === true
      ? Math.max(rawContent.length, LAYER1_SHELF_MAX_BYTES) + omittedBytes
      : rawContent.length;
  const bootFileContent = vaultPath ? readLayer1BootFileSync(vaultPath) : null;
  const sourceContent = bootFileContent ?? rawContent;
  const bootContent = formatLayer1BootFormContent(sourceContent, totalCoreChars, maxChars);
  return {
    ...shelfBody,
    content: bootContent,
    inline_trim: true,
  };
}


// Shared durability predicate for learnings and vault notes (recall-to-use rev5 §5.2.3).
//
// Mirrors `src/minni/durability.py` and shares `tests/fixtures/durability_cases.json`
// across `plugins/minni/tests/durability-parity.test.mjs` and `tests/test_durability_parity.py`.
//
// IMPORTANT module-load invariant: import `classifyPromptEnvelope` from `./prompt-class.js`,
// NEVER from `./harness-envelope.js` or `./harness-envelope-rules.js`, because `vault.ts`
// and `daemon_transport.ts` load even when `MINNI_USE_LEDGER=off`.
import { classifyPromptEnvelope } from "./prompt-class.js";

export const DURABILITY_RULES_VERSION = 1;

export const CATEGORY_SYNONYMS: Readonly<Record<string, string>> = {
  decision: "decision",
  adr: "decision",
  fix: "fix",
  bugfix: "fix",
  scar: "fix",
  procedure: "procedure",
  architecture: "architecture",
  preference: "preference",
};

export const DURABLE_CATEGORIES = [
  "decision",
  "fix",
  "procedure",
  "architecture",
] as const;

const DURABLE_CATEGORY_SET: ReadonlySet<string> = new Set(DURABLE_CATEGORIES);

export const CONTENT_TEST_PREFIXES = [
  "Decision:",
  "Fix:",
  "Root cause:",
  "Resolution:",
  "Invariant:",
  "Never ",
  "Always ",
] as const;

export const CONTENT_SECTION_PATTERN = "^##\\s+(?:Resolution|Fix)\\b";

export const DURABILITY_RULE_IDS = [
  "noise.envelope_skip",
  "noise.envelope_wrapped",
  "noise.envelope_system_reminder",
  "noise.compaction_source",
  "noise.compaction_path",
  "noise.plan_journal",
  "noise.plan_complete",
  "noise.plan_candidate_resolved",
  "durable.category",
  "durable.content_prefix",
  "durable.content_section",
  "durable.preference_post_gate",
  "ordinary.default",
] as const;

export type DurabilityKind = "noise" | "durable" | "ordinary";
export type DurabilityRuleId = (typeof DURABILITY_RULE_IDS)[number];

export interface DurabilityClassification {
  kind: DurabilityKind;
  rule_id: DurabilityRuleId;
}

export interface DurabilityOptions {
  provenanceGateSince?: string | number | null | undefined;
  contentTestEnabled?: boolean | undefined;
}

const CONTENT_SECTION_RE = new RegExp(CONTENT_SECTION_PATTERN, "m");
const FRONTMATTER_BLOCK_RE = /^---[ \t]*\r?\n(?:([\s\S]*?)\r?\n)?---[ \t]*(?:\r?\n|$)/;
const FRONTMATTER_KV_RE = /^([A-Za-z0-9_-]+)[ \t]*:[ \t]*(.*?)[ \t]*$/;
const FRONTMATTER_STRIP_RE = /^---[ \t]*\r?\n(?:[\s\S]*?\r?\n)?---[ \t]*(?:\r?\n)?/;
const LEADING_H1_RE = /^#[ \t]+[^\r\n]*(?:\r?\n|$)/;
const CHECKBOX_OPEN_RE = /^[ \t]*-[ \t]*\[[ \t]\][ \t]+/m;
const CHECKBOX_DONE_RE = /^[ \t]*-[ \t]*\[[xX]\][ \t]+/m;
const COMPACTION_PATH_RE = /(?:^|.*\/)wiki\/sessions\/[^/]*-compact-[^/]*$/;
const PLAN_JOURNAL_PATH_RE = /(?:^|.*\/)wiki\/artifacts\/plan-[^/]*\.log(?:\.md)?$/;
const PLAN_NOTE_PATH_RE = /(?:^|.*\/)wiki\/artifacts\/plan-[^/]+\.md$/;
const NUMERIC_STR_RE = /^[-+]?\d+(?:\.\d+)?$/;
const NAIVE_ISO_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/;
const SPACE_ISO_RE = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})/;

export function durabilityRuleIds(): string[] {
  return [...DURABILITY_RULE_IDS];
}

export function normalizeCategory(rawCategory: unknown): string {
  if (typeof rawCategory !== "string") return "";
  const cleaned = rawCategory.trim().toLowerCase();
  return CATEGORY_SYNONYMS[cleaned] ?? cleaned;
}

function parseScalarFrontmatter(text: string): Record<string, string> {
  const match = FRONTMATTER_BLOCK_RE.exec(text);
  if (!match || !match[1]) return {};
  const out: Record<string, string> = {};
  for (const line of match[1].split(/\r?\n/)) {
    const kv = FRONTMATTER_KV_RE.exec(line);
    if (!kv) continue;
    const key = kv[1].trim().toLowerCase();
    let val = kv[2].trim();
    if (
      val.length >= 2 &&
      ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'")))
    ) {
      val = val.slice(1, -1);
    }
    if (key && val && !(key in out)) out[key] = val;
  }
  return out;
}

function stripFrontmatterAndH1(text: string): string {
  let body = text.replace(FRONTMATTER_STRIP_RE, "").trimStart();
  body = body.replace(LEADING_H1_RE, "").trimStart();
  return body;
}

function matchContentTestRule(text: string): "durable.content_prefix" | "durable.content_section" | null {
  if (typeof text !== "string" || !text.trim()) return null;
  const body = stripFrontmatterAndH1(text);
  if (CONTENT_TEST_PREFIXES.some((prefix) => body.startsWith(prefix))) {
    return "durable.content_prefix";
  }
  if (CONTENT_SECTION_RE.test(body)) {
    return "durable.content_section";
  }
  return null;
}

export function matchesContentTest(text: unknown): boolean {
  return typeof text === "string" && matchContentTestRule(text) !== null;
}

function normalizePath(raw: unknown): string {
  if (typeof raw !== "string") return "";
  let cleaned = raw.replace(/\\/g, "/").trim();
  if (cleaned.startsWith("[[") && cleaned.endsWith("]]")) {
    cleaned = cleaned.slice(2, -2).trim();
  }
  return cleaned.replace(/^\/+/, "");
}

function firstNonEmpty(...values: unknown[]): string {
  for (const val of values) {
    if (val === null || val === undefined) continue;
    const s = typeof val === "string" ? val : String(val);
    if (s.length > 0) return s;
  }
  return "";
}

function firstTrimmed(...values: unknown[]): string {
  for (const val of values) {
    if (val === null || val === undefined) continue;
    const s = (typeof val === "string" ? val : String(val)).trim();
    if (s.length > 0) return s;
  }
  return "";
}

function parseTs(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "string") {
    const s = value.trim();
    if (!s) return null;
    if (NUMERIC_STR_RE.test(s)) {
      const n = Number(s);
      return Number.isFinite(n) ? n : null;
    }
    const withT = s.replace(SPACE_ISO_RE, "$1T$2");
    const iso = NAIVE_ISO_RE.test(s) ? `${withT}Z` : withT;
    const ms = Date.parse(iso);
    return Number.isFinite(ms) ? ms / 1000 : null;
  }
  return null;
}

export function classifyDurability(
  item: unknown,
  options?: DurabilityOptions,
): DurabilityClassification {
  if (item === null || typeof item !== "object" || Array.isArray(item)) {
    return { kind: "ordinary", rule_id: "ordinary.default" };
  }
  const rec = item as Record<string, unknown>;
  const content = firstNonEmpty(rec.content, rec.body, rec.text);
  const fm = content.startsWith("---") ? parseScalarFrontmatter(content) : {};

  const relPath = normalizePath(firstTrimmed(rec.path, rec.rel_path, rec.note));
  const source = firstTrimmed(rec.source, fm.source);
  const normSourcePath = normalizePath(source);
  const status = firstTrimmed(rec.status, rec.review_state, rec.page_status, fm.status).toLowerCase();
  const noteType = firstTrimmed(rec.type, rec.page_type, fm.type).toLowerCase();
  const itemCat = rec.category;
  const rawCategory =
    typeof itemCat === "string" && itemCat.trim().length > 0
      ? itemCat
      : itemCat === undefined || itemCat === null || itemCat === ""
        ? fm.category
        : itemCat;

  // 1. Noise checks
  if (content.trim()) {
    for (const probeText of [content, stripFrontmatterAndH1(content)]) {
      if (!probeText.trim()) continue;
      const env = classifyPromptEnvelope(probeText);
      if (env.kind === "skip") {
        return { kind: "noise", rule_id: "noise.envelope_skip" };
      }
      if (env.kind === "wrapped") {
        return { kind: "noise", rule_id: "noise.envelope_wrapped" };
      }
      if (env.kind === "human" && env.subkind === "empty" && env.rule_id === "block.system_reminder") {
        return { kind: "noise", rule_id: "noise.envelope_system_reminder" };
      }
      if (probeText.trimStart().startsWith("<system-reminder") && !probeText.includes("</system-reminder>")) {
        return { kind: "noise", rule_id: "noise.envelope_system_reminder" };
      }
    }
  }

  if (source.startsWith("compact_distillation") || content.includes("compact_distillation:")) {
    return { kind: "noise", rule_id: "noise.compaction_source" };
  }

  const candidatePaths = [relPath, normSourcePath].filter(Boolean);
  if (candidatePaths.some((p) => COMPACTION_PATH_RE.test(p))) {
    return { kind: "noise", rule_id: "noise.compaction_path" };
  }

  if (candidatePaths.some((p) => PLAN_JOURNAL_PATH_RE.test(p))) {
    return { kind: "noise", rule_id: "noise.plan_journal" };
  }

  const isPlanNote =
    noteType === "plan" ||
    candidatePaths.some((p) => PLAN_NOTE_PATH_RE.test(p) && !p.endsWith(".log.md"));
  if (isPlanNote && status !== "accepted") {
    if (status === "complete") {
      return { kind: "noise", rule_id: "noise.plan_complete" };
    }
    if (status === "candidate") {
      if (rec.all_slices_resolved === true) {
        return { kind: "noise", rule_id: "noise.plan_candidate_resolved" };
      }
      if (CHECKBOX_DONE_RE.test(content) && !CHECKBOX_OPEN_RE.test(content)) {
        return { kind: "noise", rule_id: "noise.plan_candidate_resolved" };
      }
    }
  }

  // 2. Durable checks
  const normCat = normalizeCategory(rawCategory);
  if (DURABLE_CATEGORY_SET.has(normCat)) {
    return { kind: "durable", rule_id: "durable.category" };
  }

  const contentTestEnabled = options?.contentTestEnabled !== false;
  if (contentTestEnabled) {
    const contentRule = matchContentTestRule(content);
    if (contentRule !== null) {
      return { kind: "durable", rule_id: contentRule };
    }
  }

  if (normCat === "preference") {
    const origin = firstTrimmed(rec.origin, rec.origin_kind, fm.origin, fm.origin_kind).toLowerCase();
    const gateSince =
      options?.provenanceGateSince !== undefined && options?.provenanceGateSince !== null
        ? options.provenanceGateSince
        : process.env.MINNI_PROVENANCE_GATE_SINCE;
    if (origin === "human" && gateSince !== undefined && gateSince !== null) {
      const gateTs = parseTs(gateSince);
      let rawCreated: unknown = undefined;
      for (const cand of [rec.created_at, rec.created, fm.created_at, fm.created]) {
        if (cand !== undefined && cand !== null && cand !== "") {
          rawCreated = cand;
          break;
        }
      }
      const createdTs = parseTs(rawCreated);
      if (gateTs !== null && createdTs !== null && createdTs >= gateTs) {
        return { kind: "durable", rule_id: "durable.preference_post_gate" };
      }
    }
  }

  return { kind: "ordinary", rule_id: "ordinary.default" };
}

export function durabilityOf(item: unknown, options?: DurabilityOptions): DurabilityKind {
  return classifyDurability(item, options).kind;
}

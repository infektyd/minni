export type MemoryIntentAction = "recall" | "learn" | "vault_write" | "audit" | "status" | "none";

export interface MemoryIntent {
  action: MemoryIntentAction;
  confidence: number;
  automaticAllowed: boolean;
  reason: string;
  suggestedTool?: string;
  suggestedQuery?: string;
}

export interface LearningQualityReport {
  ok: boolean;
  score: number;
  warnings: string[];
  summary: string;
}

const RECALL_TERMS = [
  "remember",
  "recall",
  "memory",
  "prior",
  "previous",
  "context",
  "where did we leave",
  "what did we decide",
];

const LEARN_TERMS = ["learn", "remember this", "save to memory", "store this", "keep this", "make a note"];

/**
 * H1: imperative "write THIS" markers. A question can still carry an imperative
 * durable-write ("Can you learn this: …?") — that must route to `learn` (which
 * is not automatically allowed and goes through write-intent suppression), NOT
 * to the automatic recall branch below. Distinguishes "learn this/that/it" (a
 * command to store the following) from "learn about/anything" (a recall query).
 */
const IMPERATIVE_WRITE_MARKER =
  /\b(learn|remember|save|store|note)\s+(this|that|the following|it)\b/;
const VAULT_TERMS = ["vault note", "obsidian", "write note", "wiki page", "source note"];
const AUDIT_TERMS = ["audit", "logs", "log tail", "transparency"];
const STATUS_TERMS = ["status", "health", "daemon", "afm"];

function includesAny(text: string, terms: string[]): boolean {
  return terms.some((term) => text.includes(term));
}

/** Interrogative form: ends with "?" or opens with a question word. */
function isQuestion(text: string): boolean {
  return (
    text.trim().endsWith("?") ||
    /^\s*(what|which|who|whom|whose|when|where|why|how|did|do|does|is|are|was|were|have|has|had|can|could|should|would|anything|everything)\b/.test(
      text,
    )
  );
}

function clampScore(score: number): number {
  return Math.max(0, Math.min(1, Number(score.toFixed(2))));
}

function conciseQuery(task: string): string {
  return task.replace(/\s+/g, " ").trim().slice(0, 180);
}

export function routeMemoryIntent(task: string): MemoryIntent {
  const text = task.toLowerCase();
  // A QUESTION that mentions "learn" (e.g. "what did we learn about X?") asks to
  // RETRIEVE prior learnings — it must route to recall, not be swallowed by the
  // bare-"learn" write check below (which previously suppressed recall). Recall
  // is read-only and automatic, so erring this way on an ambiguous question is
  // safe; an explicit imperative ("learn this …") is not a question and still
  // routes to learn.
  if (
    isQuestion(text) &&
    /\blearn(ed|ing|t|ings|s)?\b/.test(text) &&
    !IMPERATIVE_WRITE_MARKER.test(text)
  ) {
    return {
      action: "recall",
      confidence: 0.74,
      automaticAllowed: true,
      reason: "Question about prior learnings — recall, not a durable write.",
      suggestedTool: "minni_recall",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, LEARN_TERMS)) {
    return {
      action: "learn",
      confidence: 0.92,
      automaticAllowed: false,
      reason: "The task explicitly asks for durable memory or learning.",
      suggestedTool: "minni_learn",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, VAULT_TERMS)) {
    return {
      action: "vault_write",
      confidence: 0.88,
      automaticAllowed: false,
      reason: "The task asks for a visible Obsidian/wiki note.",
      suggestedTool: "minni_vault_write",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, AUDIT_TERMS)) {
    return {
      action: "audit",
      confidence: 0.84,
      automaticAllowed: true,
      reason: "The task asks for transparent memory logs or audit state.",
      suggestedTool: "minni_audit_tail",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, STATUS_TERMS)) {
    return {
      action: "status",
      confidence: 0.8,
      automaticAllowed: true,
      reason: "The task asks about local service health or plugin status.",
      suggestedTool: "minni_status",
      suggestedQuery: conciseQuery(task),
    };
  }
  if (includesAny(text, RECALL_TERMS) || /continue|resume|pick up|integrat|debug|test|build/.test(text)) {
    return {
      action: "recall",
      confidence: 0.72,
      automaticAllowed: true,
      reason: "The task likely benefits from prior local project context; recall-only is allowed automatically.",
      suggestedTool: "minni_recall",
      suggestedQuery: conciseQuery(task),
    };
  }
  return {
    action: "none",
    confidence: 0.35,
    automaticAllowed: true,
    reason: "No memory action appears necessary from the task wording.",
  };
}

/**
 * Secret-material detection (#138). The gate flags credential MATERIAL, not
 * credential VOCABULARY: notes about `id-token` permissions, tokenizers, or
 * api-key hygiene are exactly the durable learnings worth keeping, while a
 * pasted `ghp_…` or a keyword assigned an opaque literal is what must block.
 */
const SECRET_PREFIX_RE = new RegExp(
  [
    "\\bpypi-[A-Za-z0-9_-]{16,}",
    "\\bghp_[A-Za-z0-9]{20,}",
    "\\bgithub_pat_[A-Za-z0-9_]{20,}",
    "\\bgh[ousr]_[A-Za-z0-9]{20,}",
    "\\bsk-[A-Za-z0-9_-]{20,}",
    "\\bxox[baprs]-[A-Za-z0-9-]{10,}",
    "\\bA(?:KIA|SIA)[0-9A-Z]{16}\\b",
    "-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "\\beyJ[A-Za-z0-9_-]{16,}\\.[A-Za-z0-9_-]{8,}", // JWT header.payload
  ].join("|"),
);

// A credential keyword directly assigned an opaque literal (`api_key = h8f…`).
// Keyword mentions WITHOUT an assigned literal ("the token was revoked",
// GitHub Actions' `id-token: write`) deliberately do not match. Value shapes
// that count:
// - QUOTED value of 8+ chars, any charset, either separator — covers
//   passwords with punctuation/spaces (`password: "aB3!dE5@gH7#jK9%"`).
// - `=`-assigned unquoted value of 8+ chars, ANY charset — `=` is config
//   syntax, not prose, so `password=correcthorsebatterystaple` and
//   `api_key=abcdefghijklmnopqrstuvwx` block without needing digits.
// - `:`-assigned unquoted value of 8+ chars carrying a digit or
//   password-style symbol — the colon appears in prose and YAML
//   (`id-token: write`, "token: authentication-related"), so plain words
//   after a colon stay clean while `password: Hunter22` blocks.
const SECRET_ASSIGNMENT_RE =
  /(secret|passwd|password|token|api[_ -]?key|private[_ -]?key|credential)s?["']?\s*(?:=\s*(?:["'][^"'\n]{8,}["']|[^\s"']{8,})|:\s*(?:["'][^"'\n]{8,}["']|(?=[^\s"']*[0-9!@#$%^&*?~+=])[^\s"']{8,}))/i;

// Public integrity checksums (npm/pnpm SRI: `sha512-…=`) are high-entropy but
// not secrets; strip them before the entropy fallback so lockfile-debugging
// notes aren't hard-blocked.
const SRI_CHECKSUM_RE = /\bsha\d+-[A-Za-z0-9+/=]{16,}/g;

function shannonEntropyPerChar(s: string): number {
  const counts = new Map<string, number>();
  for (const ch of s) counts.set(ch, (counts.get(ch) ?? 0) + 1);
  let bits = 0;
  for (const n of counts.values()) {
    const p = n / s.length;
    bits -= p * Math.log2(p);
  }
  return bits;
}

export function detectSecretMaterial(content: string): string | null {
  if (SECRET_PREFIX_RE.test(content)) {
    return "a string with a well-known secret prefix";
  }
  const assigned = content.match(SECRET_ASSIGNMENT_RE);
  if (assigned) {
    return `a credential keyword ("${assigned[1]}") assigned an opaque literal`;
  }
  // High-entropy opaque spans. Requiring lower+upper+digit together keeps
  // git SHAs / sha256 digests (hex: no uppercase) and prose/paths (no digits)
  // out; base64-ish secret material almost always carries all three. Public
  // SRI checksums (`sha512-…`) are stripped first — high-entropy, not secret.
  const scannable = content.replace(SRI_CHECKSUM_RE, " ");
  for (const span of scannable.match(/[A-Za-z0-9+/_=-]{24,}/g) ?? []) {
    const hasLower = /[a-z]/.test(span);
    const hasUpper = /[A-Z]/.test(span);
    const hasDigit = /[0-9]/.test(span);
    if (hasLower && hasUpper && hasDigit && shannonEntropyPerChar(span) >= 3.8) {
      return "a high-entropy opaque string";
    }
  }
  return null;
}

export function assessLearningQuality(input: {
  title: string;
  content: string;
  category?: string;
  source?: string;
}): LearningQualityReport {
  const warnings: string[] = [];
  let score = 0.35;
  const content = input.content.trim();
  const wordCount = content.split(/\s+/).filter(Boolean).length;

  if (input.title.trim().length >= 8) score += 0.15;
  else warnings.push("Title is very short; use a durable, searchable title.");

  if (wordCount >= 12) score += 0.2;
  else warnings.push("Content is short; durable memory works best with a complete fact, decision, or procedure.");

  if (input.category) score += 0.1;
  else warnings.push("Category is missing; defaulting to general.");

  if (input.source) score += 0.1;
  else warnings.push("Source is missing; add one when this came from a session, file, or user instruction.");

  if (/\b(todo|maybe|later|stuff|thing)\b/i.test(content)) {
    score -= 0.12;
    warnings.push("Content has vague wording; prefer specific facts and decisions.");
  }

  const secretMaterial = detectSecretMaterial(content);
  if (secretMaterial) {
    score -= 0.3;
    warnings.push(
      `Content appears to contain sensitive material (${secretMaterial}); ` +
        "never store secrets in memory.",
    );
  }

  const normalized = clampScore(score);
  return {
    ok: normalized >= 0.6 && !warnings.some((warning) => warning.includes("sensitive material")),
    score: normalized,
    warnings,
    summary: warnings.length === 0 ? "Learning looks durable and specific." : warnings.join(" "),
  };
}

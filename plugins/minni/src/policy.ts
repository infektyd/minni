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
    // pypi- must look token-shaped (mixed case + digit): kebab-case slugs
    // like `pypi-trusted-publisher-lowercase-claim` are vocabulary, not tokens.
    "\\bpypi-(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{16,}",
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
// GitHub Actions' `id-token: write`) deliberately do not match. Keywords are
// tiered by how often they appear benignly with a colon:
//
// HIGH-RISK (password/passwd/secret/private key): a following `:` or `=`
// with ANY 8+ char value — quoted or not, any charset — blocks
// (`password: correcthorsebatterystaple`, `private key: abcdef…`). These
// words followed by an assigned value are essentially never benign prose.
//
// UNQUOTED multi-word tails after a high-risk keyword (`password: correct
// horse battery staple` vs `password: use a manager`) are structurally
// indistinguishable to regex — only the first token is consumed, and short
// words pass. Quoted passphrases still block here. Issue #147 adds an AFM
// semantic tier (`assessLearningQualityAsync`) that runs ONLY when
// `findInconclusiveHighRiskAssignments` finds such a span; the pre-#138 gate
// "caught" them only by blocking every sentence containing these words,
// which is the exact false-positive trade #138 rejected.
//
// LOWER-RISK (token/api-key/credential): these appear constantly in benign
// YAML/prose (`id-token: write`, "token: authentication-related"), so the
// `:` branch additionally requires a digit or password-style symbol in the
// value; the `=` branch (config syntax, not prose) takes any 8+ char value.
// Quoted values of 8+ chars block for both separators.
const HIGH_RISK_ASSIGNMENT_RE =
  /(secret|passwd|password|private[_ -]?key)s?["']?\s*[:=]\s*(?:["'][^"'\n]{8,}["']|[^\s"']{8,})/i;
const LOWER_RISK_ASSIGNMENT_RE =
  /(token|api[_ -]?key|credential)s?["']?\s*(?:=\s*(?:["'][^"'\n]{8,}["']|[^\s"']{8,})|:\s*(?:["'][^"'\n]{8,}["']|(?=[^\s"']*[0-9!@#$%^&*?~+=])[^\s"']{8,}))/i;

/** High-risk keyword + unquoted multi-word value that the regex tier cannot judge. */
export interface InconclusiveHighRiskAssignment {
  keyword: string;
  /** Tail after `:`/`=` — never echoed into warnings (may be a passphrase). */
  tail: string;
}

// Straight ASCII quotes plus their smart/curly counterparts (“ ” ‘ ’) — a
// short curly-quoted decoy preceding a real passphrase
// (`password: "my dog" correct horse battery staple`) must split into
// candidates the same way the ASCII form does, not reach AFM as one glued
// blob.
const QUOTE_CHARS = "\"'“”‘’";
const QUOTE_CLASS = `[${QUOTE_CHARS}]`;
const LEADING_QUOTE_RE = new RegExp(`^${QUOTE_CLASS}`);
const TRIM_QUOTES_RE = new RegExp(`^${QUOTE_CLASS}+|${QUOTE_CLASS}+$`, "g");
const LEADING_QUOTES_RE = new RegExp(`^${QUOTE_CLASS}+`);
const TRAILING_QUOTES_RE = new RegExp(`${QUOTE_CLASS}+$`);
const QUOTE_SEGMENT_RE = new RegExp(`${QUOTE_CLASS}([^"'“”‘’\n]*)${QUOTE_CLASS}`, "g");

/**
 * Collect credential-shaped value candidates from a high-risk assignment
 * region. Multiple candidates are intentional: decoy quotes + real tails
 * (`"my dog" after correct horse…`) and quoted secrets + prose asides
 * (`"don'tusethispass" is stored…`) must BOTH reach AFM — the classifier
 * blocks if ANY span is credential material.
 */
function candidateAssignmentTails(clipped: string): string[] {
  const out: string[] = [];
  const add = (raw: string) => {
    let region = raw.trim();
    while (region.startsWith("\\")) region = region.slice(1).trim();
    region = region.replace(TRIM_QUOTES_RE, "").trim();
    if (!region) return;
    // Em/en dash separates clauses — emit BOTH sides so
    // `use a manager — correct horse…` still surfaces the passphrase, while
    // `secret — documented pad` still surfaces the secret on the left.
    for (const part of region.split(/\s+[—–]\s+/)) {
      const tokens = part.trim().split(/\s+/).filter(Boolean).slice(0, 8);
      if (tokens.length >= 2 || (tokens.length === 1 && (tokens[0]?.length ?? 0) >= 8)) {
        out.push(tokens.join(" "));
      }
    }
  };

  add(clipped);

  // Closed quote segments + everything after each closed quote.
  const quoteRe = new RegExp(QUOTE_SEGMENT_RE);
  let match: RegExpExecArray | null;
  while ((match = quoteRe.exec(clipped)) !== null) {
    add(match[1] ?? "");
    add(clipped.slice(match.index + match[0].length));
  }

  // Unclosed leading quote / triple-quote leftovers.
  const leading = clipped.trim();
  if (LEADING_QUOTE_RE.test(leading)) {
    add(leading.slice(1));
  }
  // Strip leading quote runs then add ("""foo""" → foo).
  add(leading.replace(LEADING_QUOTES_RE, "").replace(TRAILING_QUOTES_RE, ""));

  return [...new Set(out)];
}

/**
 * Spans the regex assignment tier leaves inconclusive (#147): a high-risk
 * credential keyword assigned a value the opaque-literal regex cannot own.
 * Callers run this only when `detectSecretMaterial` returned null.
 *
 * Each assignment is bounded to the same line and stops before the next
 * high-risk `keyword[:=]`. Multiple tails per assignment are emitted so
 * decoy quotes cannot hide a later passphrase from AFM.
 */
export function findInconclusiveHighRiskAssignments(
  content: string,
): InconclusiveHighRiskAssignment[] {
  const found: InconclusiveHighRiskAssignment[] = [];
  const re =
    /\b(secret|passwd|password|private[_ -]?key)s?["']?\s*[:=]\s*/gi;
  const nextAssignRe =
    /\b(?:secret|passwd|password|private[_ -]?key)s?["']?\s*[:=]/i;
  let match: RegExpExecArray | null;
  while ((match = re.exec(content)) !== null) {
    const keyword = match[1] ?? "password";
    const valueStart = match.index + match[0].length;
    const lineEndMatch = content.slice(valueStart).match(/\r?\n/);
    const lineEnd =
      lineEndMatch && lineEndMatch.index !== undefined
        ? valueStart + lineEndMatch.index
        : -1;
    const restOfLine = content.slice(valueStart, lineEnd === -1 ? undefined : lineEnd);
    const nextIdx = restOfLine.search(nextAssignRe);
    const clipped = (nextIdx === -1 ? restOfLine : restOfLine.slice(0, nextIdx)).trim();
    // True regex ownership on the live string.
    if (HIGH_RISK_ASSIGNMENT_RE.test(`${match[0]}${clipped}`)) continue;
    for (const tail of candidateAssignmentTails(clipped)) {
      found.push({ keyword, tail });
    }
    // One following line: `password: use a manager\ncorrect horse…` must not
    // leave the passphrase on line 2 invisible to AFM.
    if (lineEnd !== -1) {
      const afterNl = content.slice(lineEnd).replace(/^\r?\n/, "");
      const nextLineEndMatch = afterNl.match(/\r?\n/);
      const nextLine = afterNl
        .slice(0, nextLineEndMatch?.index ?? undefined)
        .trim();
      if (nextLine && !nextAssignRe.test(nextLine)) {
        for (const tail of candidateAssignmentTails(nextLine)) {
          found.push({ keyword, tail });
        }
      }
    }
  }
  return found;
}

export type InconclusiveCredentialVerdict = "credential" | "prose" | "unavailable";

export type InconclusiveCredentialClassifier = (
  spans: InconclusiveHighRiskAssignment[],
) => Promise<InconclusiveCredentialVerdict>;

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
  const assigned =
    content.match(HIGH_RISK_ASSIGNMENT_RE) ?? content.match(LOWER_RISK_ASSIGNMENT_RE);
  if (assigned) {
    return `a credential keyword ("${assigned[1]}") assigned an opaque literal`;
  }
  // High-entropy opaque spans. Requiring lower+upper+digit together keeps
  // git SHAs / sha256 digests (hex: no uppercase) and prose/paths (no digits)
  // out; base64-ish secret material almost always carries all three. Public
  // SRI checksums (`sha512-…`) are stripped first — high-entropy, not secret.
  const scannable = content.replace(SRI_CHECKSUM_RE, " ");
  for (const rawSpan of scannable.match(/[A-Za-z0-9+/_=-]{24,}/g) ?? []) {
    // Path-SHAPED spans (2+ slashes: `/Users/Hans/Projects/v2/…`,
    // `github.com/Org/Repo/runs/123…`) are evaluated per "/"-segment, so
    // they decompose into short low-entropy pieces. A span with 0-1 slashes
    // is evaluated WHOLE — a single mid-string slash in a base64 secret
    // must not split it under the 24-char floor and slip through.
    const slashes = (rawSpan.match(/\//g) ?? []).length;
    const spans = slashes >= 2 ? rawSpan.split("/") : [rawSpan];
    for (const span of spans) {
      if (span.length < 24) continue;
      const hasLower = /[a-z]/.test(span);
      const hasUpper = /[A-Z]/.test(span);
      const hasDigit = /[0-9]/.test(span);
      if (hasLower && hasUpper && hasDigit && shannonEntropyPerChar(span) >= 3.8) {
        return "a high-entropy opaque string";
      }
    }
  }
  return null;
}

/** The marker that makes a warning a hard block rather than a quality nudge. */
const SENSITIVE_MATERIAL_MARKER = "sensitive material";

function isSensitiveMaterialWarning(warning: string): boolean {
  return warning.includes(SENSITIVE_MATERIAL_MARKER);
}

/** True when the gate flagged credential material in any persisted channel. */
export function flagsSensitiveMaterial(report: LearningQualityReport): boolean {
  return report.warnings.some(isSensitiveMaterialWarning);
}

/**
 * A blocked learning's TITLE can itself be the credential — that is precisely
 * the hole this gate closes — so callers must not echo it into the audit log
 * or any other record. Blocking the vault write while logging the secret
 * verbatim would move the leak rather than close it.
 */
export function auditSafeTitle(title: string, report: LearningQualityReport): string {
  return flagsSensitiveMaterial(report) ? "[title withheld: flagged sensitive material]" : title;
}

export interface LearningInput {
  title: string;
  content: string;
  category?: string;
  source?: string;
}

/**
 * Every channel `minni_learn` persists into the vault note, in the order the
 * note carries them. The gate scanned `content` alone while all four are
 * written to disk, so a `ghp_…` pasted into a title or a source attribution
 * was stored unscanned. Detection must cover what is persisted, not just the
 * field most likely to hold prose.
 */
function persistedChannels(input: LearningInput): Array<{ field: string; text: string }> {
  return (
    [
      { field: "title", text: input.title },
      { field: "content", text: input.content },
      { field: "category", text: input.category },
      { field: "source", text: input.source },
    ] as Array<{ field: string; text?: string }>
  ).flatMap(({ field, text }) => (text?.trim() ? [{ field, text }] : []));
}

export function assessLearningQuality(input: LearningInput): LearningQualityReport {
  // Regex-only fast path. Learn / quality MCP + CLI use
  // `assessLearningQualityAsync` so the #147 AFM inconclusive tier runs.
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

  for (const channel of persistedChannels(input)) {
    const secretMaterial = detectSecretMaterial(channel.text);
    if (!secretMaterial) continue;
    score -= 0.3;
    // The field is named but the offending value is never echoed back.
    warnings.push(
      `The "${channel.field}" field appears to contain sensitive material ` +
        `(${secretMaterial}); never store secrets in memory.`,
    );
  }

  const normalized = clampScore(score);
  return {
    ok: normalized >= 0.6 && !warnings.some(isSensitiveMaterialWarning),
    score: normalized,
    warnings,
    summary: warnings.length === 0 ? "Learning looks durable and specific." : warnings.join(" "),
  };
}

/**
 * Learn-quality gate with the #147 AFM inconclusive tier.
 *
 * Fast path: identical to `assessLearningQuality` (regex material detector).
 * Slow path: when regex is clear BUT a high-risk keyword has an unquoted
 * multi-word tail, run `classifyInconclusive` (default: local AFM). A
 * `credential` verdict hard-blocks; `prose` / `unavailable` leave the
 * regex result unchanged (fail-open — AFM enhances, it does not replace).
 */
export async function assessLearningQualityAsync(
  input: LearningInput,
  options: {
    classifyInconclusive?: InconclusiveCredentialClassifier;
  } = {},
): Promise<LearningQualityReport> {
  const base = assessLearningQuality(input);
  if (!base.ok && flagsSensitiveMaterial(base)) {
    return base;
  }

  // Each persisted channel is scanned separately rather than concatenated:
  // the span finder is line-bounded, and joining fields would let one field's
  // tail run into the next field's text. The field is kept alongside its spans
  // so a block can name where the material was found.
  const flagged = persistedChannels(input)
    .map((channel) => ({
      field: channel.field,
      spans: findInconclusiveHighRiskAssignments(channel.text.trim()),
    }))
    .filter((channel) => channel.spans.length > 0);
  const spans = flagged.flatMap((channel) => channel.spans);
  if (spans.length === 0) return base;

  // Lazy default import keeps the sync path free of AFM for unit tests /
  // callers that only need regex. Injection overrides for deterministic tests.
  const classify =
    options.classifyInconclusive ??
    (await import("./policy-secret-afm.js")).classifyInconclusiveWithAfm;

  let verdict: InconclusiveCredentialVerdict;
  try {
    verdict = await classify(spans);
  } catch {
    verdict = "unavailable";
  }

  if (verdict !== "credential") return base;

  const keywords = [...new Set(spans.map((s) => s.keyword))];
  const keywordLabel = keywords.length === 1 ? keywords[0] : keywords.join("/");
  // The classifier returns ONE verdict over the whole span set, so it cannot
  // attribute the material to a single channel. Every field that contributed a
  // span is named — narrowing further would be a guess.
  const fieldLabel = flagged.map((channel) => `"${channel.field}"`).join(", ");
  const warnings = [
    ...base.warnings,
    `The ${fieldLabel} field appears to contain sensitive material ` +
      `(a credential keyword ("${keywordLabel ?? "password"}") assigned an ` +
      "unquoted multi-word value classified as a secret); never store secrets in memory.",
  ];
  const score = clampScore(base.score - 0.3);
  return {
    ok: false,
    score,
    warnings,
    summary: warnings.join(" "),
  };
}

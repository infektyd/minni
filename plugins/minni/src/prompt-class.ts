// Shared prompt classifier, normalization, cron-matching, and template-counts
// helpers (recall-to-use rev5 §5.2.1–5.2.2, slice S1a-3 / S3a).
//
// Why this module is separate from harness-envelope.ts / harness-envelope-rules.ts:
// `use-telemetry-flag-off-cost.test.mjs` and `use-telemetry-stop-detector.test.mjs`
// pin that `harness-envelope.js` and `harness-envelope-rules.js` are only loaded
// when `MINNI_USE_LEDGER=on` (and on Stop, only via `use-detector.js`).
// Hook handlers (`UserPromptSubmit`) and Jev harvest (`Stop` / `PreCompact`)
// classify prompts regardless of `MINNI_USE_LEDGER`, so they import from this
// module while `harness-envelope.ts` and `harness-envelope-rules.ts` re-export
// from here for the ledger capture and detector paths.
import { createHash, randomUUID } from "node:crypto";
import fs, { constants as fsConstants } from "node:fs";
import { mkdir, open, rename, rm, writeFile } from "node:fs/promises";
import type { FileHandle } from "node:fs/promises";
import path from "node:path";

import { assertWriteTargetUnder } from "./vault.js";

// ---------------------------------------------------------------------------
// Rules & constants (§5.2.1)
// ---------------------------------------------------------------------------

export const HARNESS_ENVELOPE_RULES = {
  version: 1,

  /**
   * Blocks stripped anywhere in the text, including nested ones. A block that is
   * never closed is left alone: an unterminated tag is not evidence of a block.
   * The open tag may carry attributes (at most 256 chars before `>`).
   */
  blockTags: [{ rule_id: "block.system_reminder", tag: "system-reminder" }],

  /**
   * Prompts whose stripped text STARTS with the prefix are machine output: no
   * prompt recall, no harvest. Prefixes are open ended on the right on purpose
   * (`<task-notification` also covers `<task-notification id="…">`).
   */
  skip: [
    { rule_id: "skip.task_notification", prefix: "<task-notification", subkind: "task_notification" },
    { rule_id: "skip.side_sessions_event", prefix: "<side-sessions-event>", subkind: "side_sessions_event" },
    { rule_id: "skip.ci_monitor_event", prefix: "<ci-monitor-event>", subkind: "ci_monitor_event" },
    { rule_id: "skip.command_name", prefix: "<command-name>", subkind: "slash_command" },
    { rule_id: "skip.command_message", prefix: "<command-message>", subkind: "slash_command" },
    { rule_id: "skip.local_command_stdout", prefix: "<local-command-stdout>", subkind: "local_command_output" },
    { rule_id: "skip.local_command_caveat", prefix: "<local-command-caveat>", subkind: "local_command_output" },
    { rule_id: "skip.bash_input", prefix: "<bash-input>", subkind: "human_shell" },
  ],

  /**
   * Wrappers are stripped and the body is kept as the payload. They are tried at
   * the start of the text and repeated, so a lead line followed by a tag pair
   * unwraps fully (at most `maxWrapperDepth` passes). The first rule that matched
   * names the result.
   */
  wrappers: [
    {
      rule_id: "wrap.cross_session_message",
      form: "tag_pair",
      tag: "cross-session-message",
      subkind: "cross_session_message",
    },
    {
      rule_id: "wrap.another_session_lead",
      form: "lead_line",
      prefix: "Another Claude session sent a message",
      subkind: "cross_session_lead",
    },
    {
      rule_id: "wrap.workflow_harness_header",
      form: "header",
      prefix: "[Workflow harness",
      subkind: "workflow_harness",
    },
    {
      rule_id: "wrap.artifact_comment_header",
      form: "header",
      prefix: "[Artifact comment",
      subkind: "artifact_comment",
    },
  ],

  maxWrapperDepth: 4,
  maxOpenTagChars: 512,
  maxBlockOpenTagChars: 256,
  maxHeaderChars: 256,
  suspiciousPrefixPattern: "^(?:<[A-Za-z]|\\[[A-Z])",

  templateRule: {
    rule_id: "scheduled.template",
    minChars: 80,
    hashInputChars: 2000,
    minPrior: 3,
    minSessions: 2,
    minSpanMs: 2 * 60 * 60 * 1000,
    ringMax: 16,
    sessionsMax: 8,
    entriesMax: 500,
    idleMaxMs: 14 * 24 * 60 * 60 * 1000,
    fileVersion: 1,
  },
} as const;

export const HARNESS_TRIM_CHARS = " \t\r\n\f\v";

export function harnessEnvelopeRuleIds(): string[] {
  return [
    ...HARNESS_ENVELOPE_RULES.blockTags.map((r) => r.rule_id),
    ...HARNESS_ENVELOPE_RULES.skip.map((r) => r.rule_id),
    ...HARNESS_ENVELOPE_RULES.wrappers.map((r) => r.rule_id),
  ];
}

export function classificationRuleIds(): string[] {
  return [...harnessEnvelopeRuleIds(), HARNESS_ENVELOPE_RULES.templateRule.rule_id];
}

const RULES = HARNESS_ENVELOPE_RULES;
const TEMPLATE = HARNESS_ENVELOPE_RULES.templateRule;
type WrapperRule = (typeof RULES.wrappers)[number];

export type PromptEnvelopeKind = "human" | "skip" | "wrapped" | "scheduled";

export interface PromptTemplateOccurrence {
  ts: number;
  pid: string;
}

/** One normalized prompt shape. Counts and times only: no text. */
export interface PromptTemplateEntry {
  /** Earliest occurrence ever recorded (epoch ms). Survives ring rollover. */
  first_seen: number;
  /** Newest 16 occurrences, oldest first. */
  occ: PromptTemplateOccurrence[];
  /** 8-char session key prefix -> that session's first sighting (epoch ms), earliest 8. */
  sessions: Record<string, number>;
}

export interface PromptTemplatesFile {
  v: 1;
  templates: Record<string, PromptTemplateEntry>;
}

/** What the template rule needs from the caller. With no ctx the rule is off. */
export interface PromptTemplateContext {
  /** When the prompt happened (epoch ms). */
  at: number;
  /** The prompt's own id (the host's `prompt_id` / transcript `promptId`). */
  pid: string;
  /** Look up the counts entry for a template hash. */
  templateEntry: (hash: string) => PromptTemplateEntry | null | undefined;
}

export interface PromptTemplateInfo {
  hash: string;
  scheduled: boolean;
}

export interface PromptEnvelope {
  /** `scheduled` is only ever produced by the template rule, so only with a ctx. */
  kind: PromptEnvelopeKind;
  /** `typed`/`empty` for human, `template` for scheduled, else the matched rule's subkind. */
  subkind: string;
  /** The block-stripped text; for `wrapped`, the unwrapped body. Trimmed. */
  body: string;
  /** Present when the template rule ran on an eligible prompt (at least 80 chars). */
  template?: PromptTemplateInfo;
  template_rule: "on" | "off";
  /**
   * The rule that decided the class. For `human` it is the block rule, when a
   * block was removed; a skip/wrapper/template rule outranks the block rule.
   */
  rule_id?: string;
  /** `human` text that starts like harness output (`<tag` or `[Header`) but matched no rule. */
  suspicious_prefix?: boolean;
}

// ---------------------------------------------------------------------------
// Text rules (pure)
// ---------------------------------------------------------------------------

/** Trim exactly the characters in HARNESS_TRIM_CHARS (JS trim() and Python strip() differ). */
function trimHarness(s: string): string {
  let start = 0;
  let end = s.length;
  while (start < end && HARNESS_TRIM_CHARS.includes(s.charAt(start))) start += 1;
  while (end > start && HARNESS_TRIM_CHARS.includes(s.charAt(end - 1))) end -= 1;
  return s.slice(start, end);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Remove every complete `<tag …>…</tag>` block, nesting included. A block that is
 * never closed, and a stray close tag, stay in the text.
 */
function stripBlockTag(text: string, tag: string): { text: string; stripped: boolean } {
  const re = new RegExp(
    `<(/?)${escapeRegExp(tag)}(?=[ \\t\\r\\n>])[^>]{0,${RULES.maxBlockOpenTagChars}}>`,
    "g",
  );
  let depth = 0;
  let blockStart = 0;
  let last = 0;
  let stripped = false;
  let out = "";
  for (const m of text.matchAll(re)) {
    const index = m.index ?? 0;
    const closing = m[1] === "/";
    if (depth === 0) {
      if (closing) continue;
      depth = 1;
      blockStart = index;
    } else if (closing) {
      depth -= 1;
      if (depth === 0) {
        out += text.slice(last, blockStart);
        last = index + m[0].length;
        stripped = true;
      }
    } else {
      depth += 1;
    }
  }
  out += text.slice(last);
  return { text: out, stripped };
}

function stripBlocksDetailed(text: string): { text: string; ruleIds: string[] } {
  let current = typeof text === "string" ? text : "";
  const ruleIds: string[] = [];
  for (const rule of RULES.blockTags) {
    const result = stripBlockTag(current, rule.tag);
    current = result.text;
    if (result.stripped) ruleIds.push(rule.rule_id);
  }
  return { text: trimHarness(current), ruleIds };
}

/** Remove harness blocks (such as `system-reminder`) anywhere in the text, then trim. */
export function stripHarnessBlocks(text: string): string {
  return stripBlocksDetailed(text).text;
}

/** One wrapper rule applied to the start of `text`: the body, or null when it does not apply. */
function unwrap(rule: WrapperRule, text: string): string | null {
  switch (rule.form) {
    case "tag_pair": {
      const open = `<${rule.tag}`;
      if (!text.startsWith(open)) return null;
      const next = text.charAt(open.length);
      let openEnd: number;
      if (next === ">") {
        openEnd = open.length + 1;
      } else if (next !== "" && HARNESS_TRIM_CHARS.includes(next)) {
        const gt = text.indexOf(">", open.length);
        if (gt < 0 || gt - open.length > RULES.maxOpenTagChars) return null;
        openEnd = gt + 1;
      } else {
        return null;
      }
      const rest = text.slice(openEnd);
      const closeTag = `</${rule.tag}>`;
      const closeAt = rest.indexOf(closeTag);
      return closeAt < 0 ? rest : rest.slice(0, closeAt) + rest.slice(closeAt + closeTag.length);
    }
    case "lead_line": {
      if (!text.startsWith(rule.prefix)) return null;
      const newline = text.indexOf("\n");
      return newline < 0 ? "" : text.slice(newline + 1);
    }
    case "header": {
      if (!text.startsWith(rule.prefix)) return null;
      const close = text.indexOf("]", rule.prefix.length);
      const newline = text.indexOf("\n");
      if (close < 0) return null;
      if (newline >= 0 && close > newline) return null;
      if (close - rule.prefix.length > RULES.maxHeaderChars) return null;
      return text.slice(close + 1);
    }
  }
}

const SUSPICIOUS_PREFIX = new RegExp(RULES.suspiciousPrefixPattern);

// ---------------------------------------------------------------------------
// Template rule & cron matching (pure)
// ---------------------------------------------------------------------------

const WHITESPACE_RUN = new RegExp(`[${escapeRegExp(HARNESS_TRIM_CHARS)}]+`, "g");

/**
 * Normalize a prompt for template comparison: strip blocks, lowercase,
 * digit runs -> `N`, collapse whitespace, and cap at 2,000 chars.
 */
export function normalizeTemplateText(text: string): string {
  const normalized = stripBlocksDetailed(text)
    .text.toLowerCase()
    .replace(/\d+/g, "N")
    .replace(WHITESPACE_RUN, " ");
  return normalized.length > TEMPLATE.hashInputChars
    ? normalized.slice(0, TEMPLATE.hashInputChars)
    : normalized;
}

/**
 * The template of a whole prompt: strip blocks, lowercase, digit runs -> `N`,
 * collapse whitespace, keep the first 2,000 chars, then `sha1[:12]`. The cut comes
 * after normalizing, so a changing number cannot move it.
 */
export function templateHash(text: string): string {
  return createHash("sha1").update(normalizeTemplateText(text), "utf8").digest("hex").slice(0, 12);
}

/**
 * Check whether `text` matches any prompt in a host `session_crons` marker list
 * after template normalization (§5.2.2 step 5).
 */
export function isCronScheduledPrompt(text: string, sessionCrons: unknown): boolean {
  const normalizedPrompt = normalizeTemplateText(text);
  if (!normalizedPrompt) return false;

  const list: unknown[] = Array.isArray(sessionCrons)
    ? sessionCrons
    : isRecord(sessionCrons)
      ? Array.isArray(sessionCrons.crons)
        ? sessionCrons.crons
        : Array.isArray(sessionCrons.jobs)
          ? sessionCrons.jobs
          : Array.isArray(sessionCrons.entries)
            ? sessionCrons.entries
            : [sessionCrons]
      : typeof sessionCrons === "string"
        ? [sessionCrons]
        : [];

  for (const item of list) {
    let candidate = "";
    if (typeof item === "string") {
      candidate = item;
    } else if (isRecord(item)) {
      const raw = item.prompt ?? item.text ?? item.command ?? item.instruction;
      if (typeof raw === "string") candidate = raw;
    }
    if (!candidate) continue;
    if (normalizeTemplateText(candidate) === normalizedPrompt) {
      return true;
    }
  }
  return false;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isScheduledTemplate(
  entry: PromptTemplateEntry | null | undefined,
  at: number,
  pid: string,
): boolean {
  if (!isFiniteNumber(at) || entry === null || entry === undefined || typeof entry !== "object") {
    return false;
  }
  const occ: unknown[] = Array.isArray(entry.occ) ? entry.occ : [];
  let prior = 0;
  let own: PromptTemplateOccurrence | undefined;
  for (const item of occ) {
    if (typeof item !== "object" || item === null) continue;
    const o = item as Partial<PromptTemplateOccurrence>;
    if (typeof o.pid !== "string" || !isFiniteNumber(o.ts)) continue;
    if (o.pid === pid) {
      own ??= { ts: o.ts, pid: o.pid };
      continue;
    }
    if (o.ts < at) prior += 1;
  }
  if (prior < TEMPLATE.minPrior) return false;

  if (isFiniteNumber(entry.first_seen) && at - entry.first_seen >= TEMPLATE.minSpanMs) return true;

  const sessions =
    typeof entry.sessions === "object" && entry.sessions !== null ? entry.sessions : {};
  let before = 0;
  let ownExcluded = false;
  for (const firstTs of Object.values(sessions)) {
    if (!isFiniteNumber(firstTs) || firstTs >= at) continue;
    if (!ownExcluded && own !== undefined && firstTs === own.ts) {
      ownExcluded = true;
      continue;
    }
    before += 1;
  }
  return before >= TEMPLATE.minSessions;
}

/**
 * Classify one prompt. Pure: the text rules decide `skip` and `wrapped`; a `human`
 * prompt that clears the template rule becomes `scheduled`. Pass `ctx` to evaluate
 * the template rule; without it `template_rule` is `"off"`.
 */
export function classifyPromptEnvelope(text: string, ctx?: PromptTemplateContext): PromptEnvelope {
  const template_rule = ctx === undefined ? "off" : "on";
  const { text: stripped, ruleIds } = stripBlocksDetailed(text);
  const blockRuleId = ruleIds[0];

  if (stripped === "") {
    return {
      kind: "human",
      subkind: "empty",
      body: "",
      template_rule,
      ...(blockRuleId !== undefined ? { rule_id: blockRuleId } : {}),
    };
  }

  for (const rule of RULES.skip) {
    if (stripped.startsWith(rule.prefix)) {
      return {
        kind: "skip",
        subkind: rule.subkind,
        body: stripped,
        template_rule,
        rule_id: rule.rule_id,
      };
    }
  }

  let current = stripped;
  let outermost: WrapperRule | undefined;
  for (let pass = 0; pass < RULES.maxWrapperDepth; pass += 1) {
    let matched = false;
    for (const rule of RULES.wrappers) {
      const body = unwrap(rule, current);
      if (body === null) continue;
      outermost ??= rule;
      current = trimHarness(body);
      matched = true;
      break;
    }
    if (!matched) break;
  }
  if (outermost !== undefined) {
    return {
      kind: "wrapped",
      subkind: outermost.subkind,
      body: current,
      template_rule,
      rule_id: outermost.rule_id,
    };
  }

  const suspicious = SUSPICIOUS_PREFIX.test(stripped);
  let template: PromptTemplateInfo | undefined;
  if (ctx !== undefined && stripped.length >= TEMPLATE.minChars) {
    const hash = templateHash(text);
    template = {
      hash,
      scheduled: isScheduledTemplate(ctx.templateEntry(hash), ctx.at, ctx.pid),
    };
  }

  const flags = {
    ...(template !== undefined ? { template } : {}),
    ...(suspicious ? { suspicious_prefix: true } : {}),
  };
  if (template?.scheduled === true) {
    return {
      kind: "scheduled",
      subkind: "template",
      body: stripped,
      template_rule,
      rule_id: TEMPLATE.rule_id,
      ...flags,
    };
  }
  return {
    kind: "human",
    subkind: "typed",
    body: stripped,
    template_rule,
    ...(blockRuleId !== undefined ? { rule_id: blockRuleId } : {}),
    ...flags,
  };
}

// ---------------------------------------------------------------------------
// Counts file: `<vault>/.runtime/prompt-templates.json`
// ---------------------------------------------------------------------------

/** Relative location of the counts file under the vault. */
export const PROMPT_TEMPLATES_RELPATH = path.join(".runtime", "prompt-templates.json");

const HASH_PATTERN = /^[0-9a-f]{12}$/;
const SESSION_KEY_PATTERN = /^[0-9A-Za-z_-]{1,8}$/;
const MAX_PID_CHARS = 128;
/** A legitimate file is well under 1 MB (500 entries); anything past this is not ours. */
const MAX_FILE_BYTES = 2 * 1024 * 1024;
const READ_FLAGS =
  fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0) | (fsConstants.O_NONBLOCK ?? 0);

export function promptTemplatesPath(vaultPath: string): string {
  return path.join(vaultPath, PROMPT_TEMPLATES_RELPATH);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validPid(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_PID_CHARS;
}

function compareStrings(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function lastSeen(entry: PromptTemplateEntry): number {
  const newest = entry.occ.length > 0 ? entry.occ[entry.occ.length - 1].ts : entry.first_seen;
  return Math.max(entry.first_seen, newest);
}

/** Rebuild an entry from untrusted JSON, dropping what does not validate. */
function sanitizeEntry(raw: unknown): PromptTemplateEntry | null {
  if (!isRecord(raw) || !isFiniteNumber(raw.first_seen)) return null;
  const occ: PromptTemplateOccurrence[] = [];
  if (Array.isArray(raw.occ)) {
    for (const item of raw.occ) {
      if (isRecord(item) && isFiniteNumber(item.ts) && validPid(item.pid)) {
        occ.push({ ts: item.ts, pid: item.pid });
      }
    }
  }
  occ.sort((a, b) => a.ts - b.ts);
  const sessions: Record<string, number> = {};
  if (isRecord(raw.sessions)) {
    const pairs: Array<[string, number]> = [];
    for (const [key, value] of Object.entries(raw.sessions)) {
      if (SESSION_KEY_PATTERN.test(key) && isFiniteNumber(value)) pairs.push([key, value]);
    }
    pairs.sort((a, b) => a[1] - b[1] || compareStrings(a[0], b[0]));
    for (const [key, value] of pairs.slice(0, TEMPLATE.sessionsMax)) sessions[key] = value;
  }
  return { first_seen: raw.first_seen, occ: occ.slice(-TEMPLATE.ringMax), sessions };
}

/** Rebuild the file from untrusted JSON. Null for a foreign version or shape. */
function sanitizeFile(raw: unknown): PromptTemplatesFile | null {
  if (!isRecord(raw) || raw.v !== TEMPLATE.fileVersion || !isRecord(raw.templates)) return null;
  const templates: Record<string, PromptTemplateEntry> = {};
  for (const [hash, value] of Object.entries(raw.templates)) {
    if (!HASH_PATTERN.test(hash)) continue;
    const entry = sanitizeEntry(value);
    if (entry !== null) templates[hash] = entry;
  }
  return { v: TEMPLATE.fileVersion, templates };
}

function errorCode(error: unknown): unknown {
  return typeof error === "object" && error !== null && "code" in error
    ? (error as { code?: unknown }).code
    : undefined;
}

function isAbsentError(error: unknown): boolean {
  const code = errorCode(error);
  return code === "ENOENT" || code === "ENOTDIR";
}

function isUnusableError(error: unknown): boolean {
  const code = errorCode(error);
  return code === "EISDIR" || code === "EACCES" || code === "EPERM" || code === "ELOOP";
}

function isExpectedReadFailure(error: unknown): boolean {
  if (error instanceof SyntaxError || isAbsentError(error) || isUnusableError(error)) return true;
  return error instanceof Error && /symlink|escapes root|containment/i.test(error.message);
}

/**
 * Load and sanitize the file. Containment failures THROW (callers decide);
 * absent, oversize, malformed and foreign-version files come back as null.
 */
async function loadPromptTemplates(vaultPath: string): Promise<PromptTemplatesFile | null> {
  const filePath = promptTemplatesPath(vaultPath);
  assertWriteTargetUnder(filePath, vaultPath);
  let handle: FileHandle | undefined;
  try {
    handle = await open(filePath, READ_FLAGS);
    const info = await handle.stat();
    if (!info.isFile() || info.size > MAX_FILE_BYTES) return null;
    return sanitizeFile(JSON.parse(await handle.readFile("utf8")));
  } catch (error) {
    if (error instanceof SyntaxError || isAbsentError(error)) return null;
    throw error;
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

export async function readPromptTemplates(vaultPath: string): Promise<PromptTemplatesFile | null> {
  try {
    return await loadPromptTemplates(vaultPath);
  } catch (error) {
    if (isExpectedReadFailure(error)) return null;
    throw error;
  }
}

/**
 * Synchronous read-only reader for `isHumanTypedInput` / `screenTranscriptChunk`.
 * Never throws: returns null when the file is absent, malformed, oversize, or refused.
 */
export function readPromptTemplatesSync(vaultPath: string): PromptTemplatesFile | null {
  if (!vaultPath || typeof vaultPath !== "string") return null;
  let fd: number | undefined;
  try {
    const filePath = promptTemplatesPath(vaultPath);
    assertWriteTargetUnder(filePath, vaultPath);
    fd = fs.openSync(filePath, READ_FLAGS);
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size <= 0 || info.size > MAX_FILE_BYTES) return null;
    const buf = Buffer.alloc(info.size);
    let total = 0;
    while (total < info.size) {
      const n = fs.readSync(fd, buf, total, info.size - total, total);
      if (n <= 0) break;
      total += n;
    }
    return sanitizeFile(JSON.parse(buf.subarray(0, total).toString("utf8")));
  } catch {
    return null;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // Ignore close error.
      }
    }
  }
}

/**
 * Check whether `MINNI_PROMPT_CLASSES` is enabled (default `"on"`) without
 * importing `flags.js` (preserving zero-capture-module-load when ledger is off).
 */
export function isPromptClassesEnabled(vaultPath?: string, explicit?: boolean): boolean {
  if (explicit === false) return false;
  if (explicit === true) return true;
  const rawEnv = process.env.MINNI_PROMPT_CLASSES;
  if (rawEnv === "off") return false;
  if (rawEnv === "on") return true;
  if (!vaultPath || typeof vaultPath !== "string") return true;
  const flagsPath = path.join(vaultPath, ".runtime", "flags.json");
  let fd: number | undefined;
  try {
    assertWriteTargetUnder(flagsPath, vaultPath);
    fd = fs.openSync(flagsPath, READ_FLAGS);
    const st = fs.fstatSync(fd);
    if (!st.isFile() || st.size <= 0 || st.size > 65_536) return true;
    const buf = Buffer.alloc(st.size);
    let total = 0;
    while (total < st.size) {
      const n = fs.readSync(fd, buf, total, st.size - total, total);
      if (n <= 0) break;
      total += n;
    }
    const parsed: unknown = JSON.parse(buf.subarray(0, total).toString("utf8"));
    if (isRecord(parsed)) {
      if (parsed.MINNI_PROMPT_CLASSES === "off") return false;
      if (parsed.MINNI_PROMPT_CLASSES === "on") return true;
    }
  } catch {
    // Fall back to default "on".
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // Ignore close error.
      }
    }
  }
  return true;
}

function pruneTemplates(file: PromptTemplatesFile, now: number): void {
  for (const [hash, entry] of Object.entries(file.templates)) {
    if (now - lastSeen(entry) > TEMPLATE.idleMaxMs) delete file.templates[hash];
  }
  const hashes = Object.keys(file.templates);
  if (hashes.length <= TEMPLATE.entriesMax) return;
  hashes.sort(
    (a, b) =>
      lastSeen(file.templates[a]) - lastSeen(file.templates[b]) || compareStrings(a, b),
  );
  for (const hash of hashes.slice(0, hashes.length - TEMPLATE.entriesMax)) {
    delete file.templates[hash];
  }
}

export interface RecordOccurrenceInput {
  /** `templateHash(...)` of the prompt: 12 lowercase hex chars. */
  hash: string;
  /** When the prompt happened (epoch ms). */
  ts: number;
  /** The prompt's own id. Recording the same pid twice counts once. */
  pid: string;
  /** Session key; stored as its first 8 chars. */
  sessionKeyPrefix: string;
  /** The clock used for pruning (epoch ms). Defaults to the current time. */
  now?: number;
}

export async function recordOccurrence(
  vaultPath: string,
  input: RecordOccurrenceInput,
): Promise<boolean> {
  const { hash, ts, pid } = input;
  if (typeof hash !== "string" || !HASH_PATTERN.test(hash)) {
    throw new TypeError("recordOccurrence: hash must be 12 lowercase hex chars");
  }
  if (!isFiniteNumber(ts)) throw new TypeError("recordOccurrence: ts must be a finite number");
  if (!validPid(pid)) throw new TypeError("recordOccurrence: pid must be a non-empty string");
  const sessionKey =
    typeof input.sessionKeyPrefix === "string" ? input.sessionKeyPrefix.slice(0, 8) : "";
  if (!SESSION_KEY_PATTERN.test(sessionKey)) {
    throw new TypeError("recordOccurrence: sessionKeyPrefix must be 1-8 chars of [0-9A-Za-z_-]");
  }
  const now = input.now ?? Date.now();
  if (!isFiniteNumber(now)) throw new TypeError("recordOccurrence: now must be a finite number");

  const filePath = promptTemplatesPath(vaultPath);
  await mkdir(path.dirname(filePath), { recursive: true });
  assertWriteTargetUnder(filePath, vaultPath);

  const file = (await loadPromptTemplates(vaultPath)) ?? { v: TEMPLATE.fileVersion, templates: {} };
  const entry = file.templates[hash] ?? { first_seen: ts, occ: [], sessions: {} };
  if (entry.occ.some((o) => o.pid === pid)) return false;

  entry.first_seen = Math.min(entry.first_seen, ts);
  entry.occ.push({ ts, pid });
  entry.occ.sort((a, b) => a.ts - b.ts);
  if (entry.occ.length > TEMPLATE.ringMax) entry.occ.splice(0, entry.occ.length - TEMPLATE.ringMax);

  const known = Object.hasOwn(entry.sessions, sessionKey) ? entry.sessions[sessionKey] : undefined;
  entry.sessions[sessionKey] = known === undefined ? ts : Math.min(known, ts);
  const keep = Object.entries(entry.sessions).sort(
    (a, b) => a[1] - b[1] || compareStrings(a[0], b[0]),
  );
  for (const [key] of keep.slice(TEMPLATE.sessionsMax)) delete entry.sessions[key];

  file.templates[hash] = entry;
  pruneTemplates(file, now);

  const tmpPath = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
  try {
    await writeFile(tmpPath, JSON.stringify(file), { encoding: "utf8", flag: "wx", mode: 0o600 });
    assertWriteTargetUnder(filePath, vaultPath);
    await rename(tmpPath, filePath);
  } catch (error) {
    await rm(tmpPath, { force: true }).catch(() => undefined);
    throw error;
  }
  return true;
}

import { createHash } from "node:crypto";
import { appendFile, mkdir, open, readFile, rename, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { expandTilde } from "./config.js";
import { durabilityOf } from "./durability.js";
import { asString, inboxPrincipalForVaultPath } from "./hook-utils.js";
import { assessLearningQualityAsync, auditSafeTitle, flagsSensitiveMaterial } from "./policy.js";
import type { LearningQualityReport } from "./policy.js";
import {
  classifyPromptEnvelope,
  isCronScheduledPrompt,
  isPromptClassesEnabled,
  readPromptTemplates,
  readPromptTemplatesSync,
  stripHarnessBlocks,
} from "./prompt-class.js";
import type { PromptTemplateContext, PromptTemplatesFile } from "./prompt-class.js";
import { isAuditTelemetryLine } from "./task.js";
import { recordAudit, vaultFirstLearn } from "./vault.js";
import type { VaultWriteResult } from "./vault.js";
import { learnMemory } from "./daemon_transport.js";
import type { JsonResult } from "./daemon_transport.js";

const execFileAsync = promisify(execFile);

/** Relative location of Jev inline learning harvest state under the vault. */
export const JEV_HARVEST_STATE_RELPATH = path.join(".runtime", "jev-harvest-state.json");

export interface TranscriptEntry {
  source?: string;
  type?: string;
  timestamp?: string;
  content?: unknown;
  tool_name?: string;
  is_error?: boolean;
  isMeta?: boolean;
  origin?: {
    kind?: string;
    [key: string]: unknown;
  };
  message?: {
    role?: string;
    content?: unknown;
    isMeta?: boolean;
    origin?: {
      kind?: string;
      [key: string]: unknown;
    };
  };
  [key: string]: unknown;
}

export interface JevProvenanceOptions {
  vaultPath?: string;
  templates?: PromptTemplatesFile | null;
  sessionCrons?: unknown;
  session_crons?: unknown;
  promptClasses?: boolean;
}

export type TriggerKind = "operator_directive" | "test_insight" | "scar";

export interface JevTrigger {
  kind: TriggerKind;
  triggerText: string;
  contextWindow: string[];
  timestamp?: string;
}

export interface JevLearnPayload {
  title: string;
  content: string;
  category: string;
  source: "jev_harvest";
  triggerKind?: TriggerKind;
  annotation?: string;
}

export interface JevHarvestState {
  cursors: Record<string, number>;
  harvested: Record<string, string>;
}

export interface JevHarvestConfig {
  vaultPath: string;
  workspaceId?: string;
  agentId?: string;
  auditPrefix?: string;
  extractor?: (trigger: JevTrigger, contextText: string) => Promise<JevLearnPayload | undefined>;
}

export interface JevHarvestInput {
  transcriptPath?: string;
  sessionId: string;
  event?: "Stop" | "PreCompact" | string;
  sessionCrons?: unknown;
  session_crons?: unknown;
}

export interface JevHarvestResult {
  harvested: boolean;
  learnings: JevLearnPayload[];
  reason?: string;
}

// ---------------------------------------------------------------------------
// Telemetry Masking (Invariant 2: Self-Echo Suppression)
// ---------------------------------------------------------------------------

/**
 * Mask all Minni telemetry, Evidence envelopes, and audit log lines before
 * trigger detection to eliminate self-echo feedback loops.
 */
export function maskTelemetry(text: string): string {
  if (!text) return "";
  const result = text
    .replace(/<EVIDENCE\b[^>]*>[\s\S]*?<\/EVIDENCE>/gi, "")
    .replace(/<minni:context\b[^>]*>[\s\S]*?<\/minni:context>/gi, "")
    .replace(/<minni:packet\b[^>]*>[\s\S]*?<\/minni:packet>/gi, "");

  const lines = result.split("\n");
  const filteredLines: string[] = [];
  let inTelemetryBlock = false;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      if (inTelemetryBlock) {
        inTelemetryBlock = false;
        continue;
      }
      filteredLines.push(line);
      continue;
    }
    if (
      /^📓\s*\d+\s+relevant\s+memor(?:y|ies)\b/iu.test(trimmed) ||
      /^📓\s*Minni\s+failure\s+lesson\b/iu.test(trimmed) ||
      /^⚠️\s*STALE\s+recall\s+pointer\b/iu.test(trimmed) ||
      /^(?:PreToolUse|PostToolUse(?:Failure)?)(?::[A-Za-z0-9_.-]+)?\s+hook\s+additional\s+context:/iu.test(trimmed)
    ) {
      continue;
    }
    if (
      /^📓\s+Minni\s+recall\s+guard:/iu.test(trimmed) ||
      /^Minni\s+recall\s+guard\b/iu.test(trimmed) ||
      /^Top\s+\d+\s+relevant\s+memor(?:y|ies)\b/iu.test(trimmed) ||
      /^\[Minni\s+Subagent\s+Brief\]$/iu.test(trimmed) ||
      /^Minni\s+subagent\s+brief\b/iu.test(trimmed)
    ) {
      inTelemetryBlock = true;
      continue;
    }
    if (
      /^Treat\s+recalled\s+snippets\s+as\s+evidence\b/iu.test(trimmed) ||
      /^You\s+have\s+unconsulted\s+recall\s+for\s+this\s+turn\b/iu.test(trimmed)
    ) {
      inTelemetryBlock = false;
      continue;
    }
    if (
      /^-\s+"[^"]+"\s+(?:\[\[[^\]]+\]\]|\([md]:[0-9a-fA-Z:_.-]+\))\s*(?:\(score\s+[0-9.]+\)|—)/u.test(
        trimmed,
      ) ||
      /^-\s+\[\[[^\]]+\]\]\s+—\s+.*\((?:score=[0-9.?]+)\)/u.test(trimmed) ||
      /^-\s+\([md]:[0-9a-fA-Z:_.-]+\)\s+/u.test(trimmed)
    ) {
      continue;
    }
    if (inTelemetryBlock) {
      if (
        /^(?:Active\s+thread|Goal|Current\s+slice|Relevant\s+memories):/iu.test(trimmed) ||
        /^-\s+"/.test(trimmed) ||
        /^-\s+\([md]:/i.test(trimmed) ||
        /^-\s+\[\[/.test(trimmed)
      ) {
        continue;
      }
      inTelemetryBlock = false;
    }

    if (trimmed.startsWith("Minni session receipt:")) continue;
    if (/^>\s*hook_[a-z0-9_]+/i.test(trimmed)) continue;
    if (/^\*\s*minni_[a-z0-9_]+/i.test(trimmed)) continue;
    if (/^Minni:\s*\d+\s*candidate\s*learning/i.test(trimmed)) continue;
    if (isAuditTelemetryLine(trimmed)) continue;
    filteredLines.push(line);
  }

  return filteredLines.join("\n").trim();
}

// ---------------------------------------------------------------------------
// Origin Filtering (Invariant 1: Directives strictly from human-typed input)
// ---------------------------------------------------------------------------

function extractOriginKind(obj: unknown): string | undefined {
  if (!obj || typeof obj !== "object") return undefined;
  const origin = (obj as Record<string, unknown>).origin;
  if (typeof origin === "string") return origin;
  if (!origin || typeof origin !== "object") return undefined;
  const kind = (origin as Record<string, unknown>).kind;
  return typeof kind === "string" ? kind : undefined;
}

function hasHumanRoleOrSource(entry: TranscriptEntry): boolean {
  if (!entry || typeof entry !== "object") return false;

  // Explicit tool execution and result filters
  if (entry.type === "tool_result" || entry.type === "tool_call") return false;
  if (entry.source === "TOOL") return false;
  if (entry.tool_name !== undefined || entry.tool_use_id !== undefined) return false;

  if (Array.isArray(entry.content)) {
    if (
      entry.content.some(
        (b) =>
          b &&
          typeof b === "object" &&
          (b as Record<string, unknown>).type === "tool_result",
      )
    ) {
      return false;
    }
  }

  // Claude Code / Codex structured message checking
  if (entry.message && typeof entry.message === "object") {
    const msg = entry.message as { role?: string; content?: unknown };
    if (msg.role === "assistant") return false;
    if (Array.isArray(msg.content)) {
      if (
        msg.content.some(
          (b) =>
            b &&
            typeof b === "object" &&
            (b as Record<string, unknown>).type === "tool_result",
        )
      ) {
        return false;
      }
    }
    if (entry.type === "user" || msg.role === "user") {
      return true;
    }
  }

  // Model and agent response rejection
  if (entry.source === "MODEL") return false;
  if (entry.type === "assistant" || entry.type === "PLANNER_RESPONSE") return false;

  // Genuine human inputs across platforms
  if (entry.source === "USER_EXPLICIT") return true;
  if (entry.type === "USER_INPUT") return true;
  if (entry.type === "queued_command") return true;
  if (entry.type === "user") return true;

  return false;
}

/**
 * Check if a transcript entry originates from genuine human typing.
 * Rejects all tool results, web searches, subagent responses, files,
 * and (when MINNI_PROMPT_CLASSES is enabled, §5.3) runs the 5-step
 * provenance gate:
 *   1. Reject `isMeta`.
 *   2. Reject a present `origin.kind` other than `"human"`.
 *   3. Strip `<system-reminder>` blocks (`stripHarnessBlocks`) and reject empty.
 *   4. Classify with `classifyPromptEnvelope` and reject `skip` and `wrapped`.
 *   5. Reject `scheduled`, by host marker or by the template rule (read-only).
 */
export function isHumanTypedInput(
  entry: TranscriptEntry,
  options?: JevProvenanceOptions,
): boolean {
  if (!entry || typeof entry !== "object") return false;

  const promptClassesOn =
    options?.promptClasses ?? isPromptClassesEnabled(options?.vaultPath);
  if (!promptClassesOn) {
    return hasHumanRoleOrSource(entry);
  }

  const msgObj =
    entry.message && typeof entry.message === "object"
      ? (entry.message as Record<string, unknown>)
      : undefined;

  // Step 1: Reject `isMeta` (top-level or nested on `entry.message`).
  if (entry.isMeta === true || msgObj?.isMeta === true) {
    return false;
  }

  // Step 2: Reject a present `origin.kind` other than `"human"`.
  const entryOriginKind = extractOriginKind(entry);
  if (entryOriginKind !== undefined && entryOriginKind !== "human") {
    return false;
  }
  const msgOriginKind = extractOriginKind(entry.message);
  if (msgOriginKind !== undefined && msgOriginKind !== "human") {
    return false;
  }

  // Base role/source check (tool results, assistant messages, etc.)
  if (!hasHumanRoleOrSource(entry)) return false;

  // Step 3: Strip `<system-reminder>` blocks and reject empty.
  const rawText = maskTelemetry(extractEntryText(entry));
  const strippedText = stripHarnessBlocks(rawText);
  if (!strippedText) return false;

  // Step 4: Classify with `classifyPromptEnvelope` and reject `skip` and `wrapped`.
  const templates =
    options?.templates !== undefined
      ? options.templates
      : options?.vaultPath
        ? readPromptTemplatesSync(options.vaultPath)
        : null;
  const rawTs = entry.timestamp ?? entry.ts ?? msgObj?.timestamp ?? msgObj?.ts;
  const parsedTs =
    typeof rawTs === "string"
      ? Date.parse(rawTs)
      : typeof rawTs === "number"
        ? rawTs
        : NaN;
  const at = Number.isFinite(parsedTs) ? parsedTs : Date.now();
  const pid =
    asString(entry.promptId) ||
    asString(entry.prompt_id) ||
    asString(entry.uuid) ||
    asString(entry.id) ||
    asString(msgObj?.promptId) ||
    asString(msgObj?.prompt_id) ||
    asString(msgObj?.uuid) ||
    asString(msgObj?.id) ||
    "__harvest__";
  const templateCtx: PromptTemplateContext | undefined =
    templates !== null
      ? {
          at,
          pid,
          templateEntry: (hash) =>
            Object.hasOwn(templates.templates, hash)
              ? templates.templates[hash]
              : undefined,
        }
      : undefined;

  const classified = classifyPromptEnvelope(rawText, templateCtx);
  if (classified.kind === "skip" || classified.kind === "wrapped") {
    return false;
  }

  // Step 5: Reject `scheduled`, by host marker or by the template rule (read-only).
  if (entry.scheduled === true || entry.isScheduled === true) return false;
  if (msgObj?.scheduled === true || msgObj?.isScheduled === true) return false;
  const crons =
    options?.sessionCrons ??
    options?.session_crons ??
    entry.session_crons ??
    entry.sessionCrons ??
    msgObj?.session_crons ??
    msgObj?.sessionCrons;
  if (isCronScheduledPrompt(rawText, crons)) {
    return false;
  }
  if (classified.kind === "scheduled") {
    return false;
  }

  return true;
}

export function extractEntryText(entry: TranscriptEntry): string {
  if (!entry) return "";
  let text = "";
  if (typeof entry.content === "string") {
    text = entry.content;
  } else if (Array.isArray(entry.content)) {
    text = entry.content
      .map((b) => (typeof b === "string" ? b : asString(b?.text || b?.content)))
      .filter(Boolean)
      .join("\n");
  } else if (entry.message && typeof entry.message === "object") {
    const msgContent = entry.message.content;
    if (typeof msgContent === "string") {
      text = msgContent;
    } else if (Array.isArray(msgContent)) {
      text = msgContent
        .map((b) => (typeof b === "string" ? b : asString(b?.text || b?.content)))
        .filter(Boolean)
        .join("\n");
    }
  }

  // Strip XML tag envelopes if present e.g. <USER_REQUEST>
  const reqMatch = /<USER_REQUEST>\s*([\s\S]*?)\s*<\/USER_REQUEST>/.exec(text);
  if (reqMatch) {
    text = reqMatch[1];
  }
  return text.trim();
}

// ---------------------------------------------------------------------------
// Cursor Safety & Delta Harvester (Invariant 3: Read up to last complete newline)
// ---------------------------------------------------------------------------

export async function readTranscriptDelta(
  transcriptPath: string,
  fromOffset = 0,
): Promise<{ entries: TranscriptEntry[]; newOffset: number }> {
  try {
    const info = await stat(transcriptPath);
    if (!info.isFile() || info.size <= fromOffset) {
      return { entries: [], newOffset: fromOffset };
    }

    const handle = await open(transcriptPath, "r");
    let buffer: Buffer;
    let bytesRead = 0;
    try {
      const length = info.size - fromOffset;
      buffer = Buffer.alloc(length);
      const res = await handle.read(buffer, 0, length, fromOffset);
      bytesRead = res.bytesRead;
    } finally {
      await handle.close();
    }

    if (bytesRead === 0) {
      return { entries: [], newOffset: fromOffset };
    }

    // Invariant 3: Strictly up to the last complete newline (\n), never to raw EOF
    const lastNewline = buffer.subarray(0, bytesRead).lastIndexOf(0x0a);
    if (lastNewline === -1) {
      return { entries: [], newOffset: fromOffset };
    }

    const validChunk = buffer.subarray(0, lastNewline + 1).toString("utf8");
    const newOffset = fromOffset + lastNewline + 1;

    const lines = validChunk.split("\n");
    const entries: TranscriptEntry[] = [];
    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;
      try {
        const parsed = JSON.parse(line) as TranscriptEntry;
        if (parsed && typeof parsed === "object") {
          entries.push(parsed);
        }
      } catch {
        // Discard malformed JSON lines
      }
    }

    return { entries, newOffset };
  } catch {
    return { entries: [], newOffset: fromOffset };
  }
}

// ---------------------------------------------------------------------------
// Deterministic Trigger Screener
// ---------------------------------------------------------------------------

const DIRECTIVE_PATTERN =
  /\b(?:from now on|always|never|don't|do not|remember to|stop doing|make sure to|ensure that|prefer|must not|only use)\b/i;

/**
 * Fast-path guard rejecting raw operator slash-dispatch prompts (/boost, /apex),
 * multi-section # DIRECTIVE: task briefs, subagent delegation boilerplate, or
 * multi-command terminal transcript dumps from being harvested as candidate learnings.
 */
const PROMPT_DISPATCH_BLOCK_PATTERN =
  /(?:^\s*(?:\*\*Task\*\*:\s*)?\/(?:boost|apex)\b|^#+\s*DIRECTIVE:|\b#+\s*DIRECTIVE:\s+[^\n]{5,}|Executing the \*\*Delegation Routine\*\*|\bRan command:\s+[^\n]+\n\s*Ran command:)/im;

export function isPromptDispatchBlock(text: string): boolean {
  if (!text) return false;
  return PROMPT_DISPATCH_BLOCK_PATTERN.test(text);
}

const TEST_FAIL_PATTERN =
  /\b(?:FAIL|failed|AssertionError|AssertionFailure|failures:|\d+\s+failed)\b/i;
const TEST_PASS_PATTERN =
  /\b(?:PASS|passed|12 passed|all \d+ tests passed|0 failed)\b/i;
const TEST_COMMAND_PATTERN =
  /\b(?:npm test|pytest|cargo test|swift test|go test|vitest|jest|test:)\b/i;

const TOOL_ERROR_PATTERN =
  /(?:\berror\b|\bunknown flag\b|\bunrecognized argument\b|\bcommand not found\b|\bENOENT\b|\bEACCES\b|\binvalid option\b|\bfailed with code\b)/i;
const EDIT_TOOL_PATTERN =
  /\b(?:replace_file_content|write_to_file|str_replace_editor|edit|patch)\b/i;

export function screenTranscriptChunk(
  entries: TranscriptEntry[],
  options?: JevProvenanceOptions,
): JevTrigger[] {
  const triggers: JevTrigger[] = [];
  const promptClassesOn =
    options?.promptClasses ?? isPromptClassesEnabled(options?.vaultPath);
  const templates =
    promptClassesOn
      ? options?.templates !== undefined
        ? options.templates
        : options?.vaultPath
          ? readPromptTemplatesSync(options.vaultPath)
          : null
      : null;
  const resolvedOptions: JevProvenanceOptions = {
    ...options,
    promptClasses: promptClassesOn,
    templates,
  };

  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    const rawEntryText = maskTelemetry(extractEntryText(entry));
    const entryText = promptClassesOn ? stripHarnessBlocks(rawEntryText) : rawEntryText;
    if (isPromptDispatchBlock(entryText)) {
      continue;
    }

    // 1. Operator Directives (strictly human input, excluding raw /boost, /apex, or # DIRECTIVE: task briefs)
    if (isHumanTypedInput(entry, resolvedOptions)) {
      if (
        entryText.length >= 10 &&
        entryText.length <= 2000 &&
        DIRECTIVE_PATTERN.test(entryText)
      ) {
        triggers.push({
          kind: "operator_directive",
          triggerText: entryText,
          contextWindow: [entryText],
          timestamp: asString(entry.timestamp) || new Date().toISOString(),
        });
      }
    }

    // 2. Test Insight (test red -> edit -> test green)
    if (
      TEST_FAIL_PATTERN.test(entryText) &&
      (TEST_COMMAND_PATTERN.test(entryText) || entry.tool_name === "run_command" || entry.type === "TOOL_RESULT")
    ) {
      // Look ahead for an edit followed by a test pass
      let editIndex = -1;
      for (let j = i + 1; j < entries.length && j <= i + 5; j++) {
        const nextEntry = entries[j];
        const nextTool = String(nextEntry.tool_name || nextEntry.type || "");
        if (EDIT_TOOL_PATTERN.test(nextTool) || extractEntryText(nextEntry).toLowerCase().includes("fix")) {
          editIndex = j;
          break;
        }
      }

      if (editIndex !== -1) {
        for (let k = editIndex + 1; k < entries.length && k <= editIndex + 5; k++) {
          const passEntry = entries[k];
          const passText = maskTelemetry(extractEntryText(passEntry));
          if (TEST_PASS_PATTERN.test(passText)) {
            triggers.push({
              kind: "test_insight",
              triggerText: `Test failure recovered after edit: ${passText.slice(0, 100)}`,
              contextWindow: [
                entryText.slice(0, 300),
                extractEntryText(entries[editIndex]).slice(0, 300),
                passText.slice(0, 300),
              ],
              timestamp: asString(passEntry.timestamp) || new Date().toISOString(),
            });
            i = k; // Jump past this sequence
            break;
          }
        }
      }
    }

    // 3. Scar (tool error -> flag/command recovery)
    if (
      TOOL_ERROR_PATTERN.test(entryText) &&
      (entry.type === "TOOL_RESULT" || entry.source === "TOOL" || entry.is_error === true)
    ) {
      for (let j = i + 1; j < entries.length && j <= i + 4; j++) {
        const recoveryEntry = entries[j];
        const recText = maskTelemetry(extractEntryText(recoveryEntry));
        if (
          (recoveryEntry.type === "TOOL_RESULT" || recoveryEntry.source === "TOOL") &&
          !TOOL_ERROR_PATTERN.test(recText) &&
          recText.length > 0
        ) {
          triggers.push({
            kind: "scar",
            triggerText: `Tool error resolved by recovery command: ${entryText.slice(0, 80)}`,
            contextWindow: [entryText.slice(0, 300), recText.slice(0, 300)],
            timestamp: asString(recoveryEntry.timestamp) || new Date().toISOString(),
          });
          i = j; // Jump past
          break;
        }
      }
    }
  }

  return triggers;
}

// ---------------------------------------------------------------------------
// Jev Extraction Pass (typesafe_extract)
// ---------------------------------------------------------------------------

const CANONICAL_JEV_CATEGORIES = new Set([
  "procedure",
  "decision",
  "fix",
  "pattern",
  "preference",
  "fact",
  "architecture",
  "codebase",
  "general",
]);

const LEGACY_JEV_CATEGORY_MAP: Record<string, string> = {
  operational: "procedure",
  design: "decision",
  debugging: "fix",
  testing: "pattern",
  convention: "preference",
};

function defaultCategoryForTrigger(kind?: string): string {
  switch (kind) {
    case "operator_directive":
      return "preference";
    case "test_insight":
      return "pattern";
    case "scar":
      return "fix";
    default:
      return "general";
  }
}

export function normalizeJevCategory(raw: unknown, triggerKind?: string): string {
  if (typeof raw === "string") {
    const cleaned = raw.trim().toLowerCase();
    if (CANONICAL_JEV_CATEGORIES.has(cleaned)) return cleaned;
    if (LEGACY_JEV_CATEGORY_MAP[cleaned]) return LEGACY_JEV_CATEGORY_MAP[cleaned];
  }
  return defaultCategoryForTrigger(triggerKind);
}

export async function extractJevLearning(
  trigger: JevTrigger,
  extractor?: (trigger: JevTrigger, contextText: string) => Promise<JevLearnPayload | undefined>,
): Promise<JevLearnPayload | undefined> {
  const contextText = trigger.contextWindow.join("\n---\n");

  if (isPromptDispatchBlock(trigger.triggerText) || isPromptDispatchBlock(contextText)) {
    return undefined;
  }

  if (extractor) {
    return extractor(trigger, contextText);
  }

  // Try calling typesafe-mcp CLI if present on host
  try {
    const typesafeBinary =
      process.env.TYPESAFE_BIN ||
      path.join(process.env.HOME || "", ".local", "bin", "typesafe-mcp");
    const want = {
      title: "Short concise title under 8 words",
      content: "The rule, finding, or recovery instruction",
      category: "One of: procedure, decision, fix, pattern, preference, fact, architecture, codebase, general",
    };
    const req = JSON.stringify({
      text: contextText.slice(0, 2000),
      want,
    });

    const { stdout } = await execFileAsync(typesafeBinary, ["--call", "typesafe_extract", req], {
      timeout: 2500,
    });
    const parsed = JSON.parse(stdout);
    if (parsed.status === "ok" && parsed.values) {
      const rawTitle = String(parsed.values.title?.value || trigger.triggerText.slice(0, 60));
      const title = rawTitle.replace(/\s+/g, " ").trim();
      const content = parsed.values.content?.value || trigger.triggerText;
      if (isPromptDispatchBlock(title) || isPromptDispatchBlock(String(content))) {
        return undefined;
      }
      const category = normalizeJevCategory(parsed.values.category?.value, trigger.kind);
      return {
        title,
        content,
        category,
        source: "jev_harvest",
        triggerKind: trigger.kind,
        annotation: `${trigger.kind}: ${title}`,
      };
    }
  } catch {
    // Fall back to deterministic extraction
  }

  // Deterministic structural extraction fallback
  let title = "";
  let content = "";
  let category = defaultCategoryForTrigger(trigger.kind);

  switch (trigger.kind) {
    case "operator_directive":
      title = trigger.triggerText.split(/[.\n]/)[0].replace(/\s+/g, " ").slice(0, 60).trim();
      content = trigger.triggerText;
      category = "preference";
      break;
    case "test_insight":
      title = "Test fix insight";
      content = trigger.contextWindow.join("\n");
      category = "pattern";
      break;
    case "scar":
      title = "Tool error recovery";
      content = trigger.contextWindow.join("\n");
      category = "fix";
      break;
  }

  return {
    title,
    content,
    category,
    source: "jev_harvest",
    triggerKind: trigger.kind,
    annotation: `${trigger.kind}: ${title}`,
  };
}

// ---------------------------------------------------------------------------
// Pipeline Execution (Existing minni_learn / handle_learn pipeline)
// ---------------------------------------------------------------------------

const SESSION_CHATTER_PATTERNS: ReadonlyArray<RegExp> = [
  /\b\d+\s*\/\s*\d+\s+(?:tests?\s+|specs?\s+|suites?\s+)?(?:pass(?:ed|ing)?|green)\b/i,
  /\b\d+\s+(?:tests?\s+|specs?\s+|suites?\s+)?(?:pass(?:ed|ing)|green)\b/i,
  /\ball\s+\d+\s+tests?\s+pass(?:ed|ing)?\b/i,
  /\b(?:phase|slice|sprint|wave|workstream|step)\s+[a-z0-9._-]+\s+(?:is\s+|are\s+)?(?:complete(?:d)?|done|finished|shipped|landed|green|verified)\b/i,
  /\b(?:complete(?:d)?|finished|shipped|landed|verified)\s+(?:phase|slice|sprint|wave|workstream|step)\s+[a-z0-9._-]+\b/i,
  /\b(?:pushed|committed|merged|landed)\s+(?:commit\s+|sha\s+|branch\s+|pr\s+#?\d+\s+)?(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b/i,
  /\bcommit\s+(?=[0-9a-f]*\d)[0-9a-f]{7,40}\s+(?:(?:was|is|has\s+been)\s+)?(?:pushed|merged|landed)\b/i,
];

export function isSessionStatusChatter(text: string): boolean {
  if (typeof text !== "string" || !text.trim()) return false;
  return SESSION_CHATTER_PATTERNS.some((re) => re.test(text));
}

export function resolveSessionHandoffsDir(vaultPath: string): string {
  const envDir = process.env.MINNI_SESSION_HANDOFFS_DIR;
  if (typeof envDir === "string" && envDir.trim().length > 0) {
    return path.resolve(envDir.trim());
  }
  const dotMinni =
    typeof process.env.MINNI_HOME === "string" && process.env.MINNI_HOME.trim().length > 0
      ? path.resolve(process.env.MINNI_HOME.trim())
      : path.resolve(os.homedir(), ".minni");
  const resolvedVault = path.resolve(vaultPath);
  if (resolvedVault === dotMinni || resolvedVault.startsWith(dotMinni + path.sep)) {
    return path.join(dotMinni, "session_handoffs");
  }
  return path.join(vaultPath, ".runtime", "session_handoffs");
}

export type JevTriageVerdict = boolean | { durable?: boolean; accept?: boolean; verdict?: string };
export type JevTriageFn = (candidate: {
  title: string;
  content: string;
  category: string;
  source: string;
}) => Promise<JevTriageVerdict> | JevTriageVerdict;

function parseTriageVerdict(verdict: unknown): boolean {
  if (typeof verdict === "boolean") return verdict;
  if (verdict && typeof verdict === "object") {
    const rec = verdict as Record<string, unknown>;
    if (typeof rec.durable === "boolean") return rec.durable;
    if (typeof rec.accept === "boolean") return rec.accept;
    if (typeof rec.verdict === "string") {
      const v = rec.verdict.trim().toLowerCase();
      if (v === "reject" || v === "rejected" || v === "handoff" || v === "ordinary" || v === "noise") {
        return false;
      }
      if (v === "accept" || v === "accepted" || v === "durable") {
        return true;
      }
    }
  }
  return true;
}

async function runStage2JevTriage(
  candidate: { title: string; content: string; category: string; source: string },
  triageFn?: JevTriageFn,
): Promise<boolean> {
  if (typeof triageFn === "function") {
    try {
      const res = await triageFn(candidate);
      return parseTriageVerdict(res);
    } catch {
      return true;
    }
  }
  if (process.env.MINNI_JEV_TRIAGE_CLI === "1") {
    try {
      const wbBin = process.env.WB_AGY_BIN || "wb-agy";
      const { stdout } = await execFileAsync(
        wbBin,
        ["jev", "triage", JSON.stringify(candidate)],
        { timeout: 1500 },
      );
      const parsed = JSON.parse(stdout.trim());
      return parseTriageVerdict(parsed);
    } catch {
      // Deterministic fallback so unit tests or offline hosts never block
      return true;
    }
  }
  return true;
}

export async function executeLearnPipeline(input: {
  vaultPath: string;
  workspaceId?: string;
  agentId?: string;
  sessionId?: string;
  payload: JevLearnPayload;
  triage?: JevTriageFn;
  learnMemoryFn?: typeof learnMemory;
  socketPath?: string;
}): Promise<{
  ok: boolean;
  status: string;
  quality?: LearningQualityReport;
  note?: VaultWriteResult;
  store?: JsonResult;
  handoffPath?: string;
}> {
  const { vaultPath, workspaceId, agentId, sessionId, payload } = input;
  const { title, content, source } = payload;
  const effectiveSource = source ?? "jev_harvest";
  const category = normalizeJevCategory(payload.category, payload.triggerKind);

  // 1. Credential & secret scan (flagsSensitiveMaterial)
  const quality = await assessLearningQualityAsync({ title, content, category, source: effectiveSource });
  if (flagsSensitiveMaterial(quality) || !quality.ok) {
    await recordAudit(vaultPath, {
      tool: "minni_learn",
      summary: `quality-blocked (credential material): ${auditSafeTitle(title, quality)}`,
      details: { quality, source: effectiveSource, trigger_kind: payload.triggerKind, ...(sessionId ? { session_id: sessionId } : {}) },
    });
    return { ok: false, status: "quality-blocked", quality };
  }

  const effectivePrincipal = agentId ?? inboxPrincipalForVaultPath(vaultPath) ?? "codex";

  // 2. Durability + session status chatter + optional Stage-2 triage gate
  const dur = durabilityOf({
    content,
    category,
    source: effectiveSource,
    origin: "agent",
  });
  const chatter = isSessionStatusChatter(`${title}\n${content}`);
  const triageAccepted =
    dur === "durable" && !chatter
      ? await runStage2JevTriage({ title, content, category, source: effectiveSource }, input.triage)
      : false;

  if (dur !== "durable" || chatter || !triageAccepted) {
    const handoffDir = resolveSessionHandoffsDir(vaultPath);
    const nowIso = new Date().toISOString();
    const day = nowIso.slice(0, 10);
    const handoffPath = path.join(handoffDir, `handoffs-${day}.jsonl`);
    await mkdir(handoffDir, { recursive: true });
    const handoffRow = {
      timestamp: nowIso,
      agentId: effectivePrincipal,
      ...(sessionId ? { session_id: sessionId } : {}),
      category,
      source: effectiveSource,
      durability: chatter && dur === "durable" ? "ordinary" : dur,
      title,
      content,
    };
    await appendFile(handoffPath, `${JSON.stringify(handoffRow)}\n`, "utf8");
    return { ok: true, status: "routed-to-session-handoff", quality, handoffPath };
  }

  // 3. Daemon memory store (learnMemory) - stages candidate or performs durable learn
  const dotMinni = path.resolve(os.homedir(), ".minni");
  const resolvedVault = path.resolve(vaultPath);
  const isDefaultVault = resolvedVault === dotMinni || resolvedVault.startsWith(dotMinni + path.sep);
  const rawEffectiveSocketPath =
    input.socketPath ??
    process.env.MINNI_SOCKET_PATH?.trim() ??
    process.env.MINNI_DAEMON_SOCKET?.trim() ??
    (!isDefaultVault && !process.env.MINNI_HOME?.trim()
      ? path.join(vaultPath, ".runtime", "minnid.sock")
      : undefined);
  const effectiveSocketPath = rawEffectiveSocketPath
    ? expandTilde(rawEffectiveSocketPath)
    : undefined;

  const doLearnMemory = input.learnMemoryFn ?? learnMemory;
  const store = await doLearnMemory({
    content,
    category,
    agentId: effectivePrincipal,
    workspaceId: workspaceId ?? "default",
    source: effectiveSource,
    origin: "agent",
    ...(effectiveSocketPath ? { socketPath: effectiveSocketPath } : {}),
  }).catch((err) => ({ ok: false, error: String(err), data: undefined }));

  // 4. Only write wiki/sessions/*.md via vaultFirstLearn as offline fallback when daemon is unreachable;
  // never index wiki/sessions/*.md via vault_index_doc.
  if (!store.ok) {
    const note = await vaultFirstLearn({
      vaultPath,
      title,
      content,
      category,
      source: effectiveSource,
      agentId: effectivePrincipal,
      storeResult: { ok: store.ok, data: store.data, error: store.error },
      quality,
      annotation: payload.annotation ?? `${payload.triggerKind}: ${title}`,
      trigger_kind: payload.triggerKind,
      sessionId,
    });
    return { ok: true, status: "learned", quality, note, store };
  }

  await recordAudit(vaultPath, {
    tool: "minni_learn",
    summary: title,
    details: {
      category,
      source: effectiveSource,
      storeResult: { ok: store.ok, data: store.data, error: store.error },
      ...(sessionId ? { session_id: sessionId } : {}),
      annotation: payload.annotation ?? `${payload.triggerKind}: ${title}`,
      ...(payload.triggerKind ? { trigger_kind: payload.triggerKind } : {}),
      ...(quality ? { quality } : {}),
    },
  });

  return { ok: true, status: "learned", quality, store };
}

// ---------------------------------------------------------------------------
// State Persistence & Orchestration
// ---------------------------------------------------------------------------

export async function readJevHarvestState(vaultPath: string): Promise<JevHarvestState> {
  const target = path.join(vaultPath, JEV_HARVEST_STATE_RELPATH);
  try {
    const raw = await readFile(target, "utf8");
    const parsed = JSON.parse(raw);
    return {
      cursors: parsed.cursors && typeof parsed.cursors === "object" ? parsed.cursors : {},
      harvested: parsed.harvested && typeof parsed.harvested === "object" ? parsed.harvested : {},
    };
  } catch {
    return { cursors: {}, harvested: {} };
  }
}

export async function writeJevHarvestState(
  vaultPath: string,
  state: JevHarvestState,
): Promise<void> {
  const target = path.join(vaultPath, JEV_HARVEST_STATE_RELPATH);
  const tmp = `${target}.tmp`;
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(tmp, JSON.stringify(state, null, 2), "utf8");
  await rename(tmp, target);
}

export async function harvestTranscriptDelta(
  config: JevHarvestConfig,
  input: JevHarvestInput,
): Promise<JevHarvestResult> {
  const transcriptPath = asString(input.transcriptPath);
  if (!transcriptPath) {
    return { harvested: false, learnings: [], reason: "no_transcript_path" };
  }

  const state = await readJevHarvestState(config.vaultPath);
  const currentOffset = state.cursors[transcriptPath] ?? 0;

  const { entries, newOffset } = await readTranscriptDelta(transcriptPath, currentOffset);
  if (entries.length === 0) {
    return { harvested: false, learnings: [], reason: "no_new_entries" };
  }

  // Update cursor position
  state.cursors[transcriptPath] = newOffset;

  const promptClassesOn = isPromptClassesEnabled(config.vaultPath);
  const templates = promptClassesOn
    ? await readPromptTemplates(config.vaultPath).catch(() => null)
    : null;
  const triggers = screenTranscriptChunk(entries, {
    vaultPath: config.vaultPath,
    templates,
    sessionCrons: input.sessionCrons ?? input.session_crons,
    promptClasses: promptClassesOn,
  });
  if (triggers.length === 0) {
    await writeJevHarvestState(config.vaultPath, state);
    return { harvested: false, learnings: [], reason: "no_triggers" };
  }

  const learnings: JevLearnPayload[] = [];
  let blockedReason: string | undefined;

  for (const trigger of triggers) {
    const triggerHash = createHash("sha1").update(trigger.triggerText).digest("hex");
    if (state.harvested[triggerHash]) {
      continue;
    }

    const payload = await extractJevLearning(trigger, config.extractor);
    if (!payload) continue;

    const learnRes = await executeLearnPipeline({
      vaultPath: config.vaultPath,
      workspaceId: config.workspaceId,
      agentId: config.agentId,
      sessionId: input.sessionId,
      payload,
    });

    if (!learnRes.ok) {
      if (learnRes.status === "quality-blocked") {
        blockedReason = "quality-blocked";
      }
      continue;
    }

    state.harvested[triggerHash] = new Date().toISOString();
    learnings.push(payload);
  }

  await writeJevHarvestState(config.vaultPath, state);

  if (learnings.length > 0) {
    return { harvested: true, learnings };
  }

  return {
    harvested: false,
    learnings: [],
    reason: blockedReason ?? "no_learnings_extracted",
  };
}

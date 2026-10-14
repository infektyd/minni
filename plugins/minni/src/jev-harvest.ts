import { createHash } from "node:crypto";
import { mkdir, open, readFile, rename, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { asString, inboxPrincipalForVaultPath } from "./hook-utils.js";
import { assessLearningQualityAsync, auditSafeTitle, flagsSensitiveMaterial } from "./policy.js";
import type { LearningQualityReport } from "./policy.js";
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
  message?: {
    role?: string;
    content?: unknown;
  };
  [key: string]: unknown;
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
  const filteredLines = lines.filter((line) => {
    const trimmed = line.trim();
    if (!trimmed) return true;
    if (trimmed.startsWith("Minni session receipt:")) return false;
    if (/^>\s*hook_[a-z0-9_]+/i.test(trimmed)) return false;
    if (/^\*\s*minni_[a-z0-9_]+/i.test(trimmed)) return false;
    if (/^Minni:\s*\d+\s*candidate\s*learning/i.test(trimmed)) return false;
    if (isAuditTelemetryLine(trimmed)) return false;
    return true;
  });

  return filteredLines.join("\n").trim();
}

// ---------------------------------------------------------------------------
// Origin Filtering (Invariant 1: Directives strictly from human-typed input)
// ---------------------------------------------------------------------------

/**
 * Check if a transcript entry originates from genuine human typing.
 * Rejects all tool results, web searches, subagent responses, and files.
 */
export function isHumanTypedInput(entry: TranscriptEntry): boolean {
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

export function screenTranscriptChunk(entries: TranscriptEntry[]): JevTrigger[] {
  const triggers: JevTrigger[] = [];

  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    const entryText = maskTelemetry(extractEntryText(entry));
    if (isPromptDispatchBlock(entryText)) {
      continue;
    }

    // 1. Operator Directives (strictly human input, excluding raw /boost, /apex, or # DIRECTIVE: task briefs)
    if (isHumanTypedInput(entry)) {
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

export async function executeLearnPipeline(input: {
  vaultPath: string;
  workspaceId?: string;
  agentId?: string;
  sessionId?: string;
  payload: JevLearnPayload;
}): Promise<{
  ok: boolean;
  status: string;
  quality?: LearningQualityReport;
  note?: VaultWriteResult;
  store?: JsonResult;
}> {
  const { vaultPath, workspaceId, agentId, sessionId, payload } = input;
  const { title, content, source } = payload;
  const category = normalizeJevCategory(payload.category, payload.triggerKind);

  // 1. Credential & secret scan (flagsSensitiveMaterial)
  const quality = await assessLearningQualityAsync({ title, content, category, source });
  if (flagsSensitiveMaterial(quality) || !quality.ok) {
    await recordAudit(vaultPath, {
      tool: "minni_learn",
      summary: `quality-blocked (credential material): ${auditSafeTitle(title, quality)}`,
      details: { quality, source, trigger_kind: payload.triggerKind, ...(sessionId ? { session_id: sessionId } : {}) },
    });
    return { ok: false, status: "quality-blocked", quality };
  }

  // 2. Daemon memory store (learnMemory) - stages candidate or performs durable learn
  const effectivePrincipal = agentId ?? inboxPrincipalForVaultPath(vaultPath) ?? "codex";
  const store = await learnMemory({
    content,
    category,
    agentId: effectivePrincipal,
    workspaceId: workspaceId ?? "default",
  }).catch((err) => ({ ok: false, error: String(err), data: undefined }));

  // 3. Vault Markdown note creation (vaultFirstLearn)
  const note = await vaultFirstLearn({
    vaultPath,
    title,
    content,
    category,
    source,
    agentId: effectivePrincipal,
    storeResult: { ok: store.ok, data: store.data, error: store.error },
    quality,
    annotation: payload.annotation ?? `${payload.triggerKind}: ${title}`,
    trigger_kind: payload.triggerKind,
    sessionId,
  });

  try {
    const { jsonRpcSocketRequestWithFallback } = await import("./daemon_transport.js");
    const fullContent = `---\ntitle: ${title}\nsection: sessions\nstatus: candidate\nprivacy: safe\ncategory: ${category}\n---\n# ${title}\n\n${content}`;
    await jsonRpcSocketRequestWithFallback("vault_index_doc", {
      content: fullContent,
      path: note.relativePath,
      agent_id: effectivePrincipal,
      agent: effectivePrincipal,
      sigil: "📄",
      privacy_level: "safe",
      page_status: "candidate",
      layer: "knowledge",
    });
  } catch {
    // Fail-open: vault note is on disk and will be indexed on next pass
  }

  return { ok: true, status: "learned", quality, note, store };
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

  const triggers = screenTranscriptChunk(entries);
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

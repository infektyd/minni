// Shared hook HANDLERS (review panel, plan-parity follow-up): codex-hook.ts,
// grok-hook.ts and kilocode-hook.ts were ~360-line near-clones whose four
// handler bodies differed ONLY in config constants and a few flagged
// behaviors. hook-utils.ts holds the protocol leaf helpers; this module holds
// the stateful handler logic, parameterized by a typed per-agent config, so
// future changes (evidence envelope format, plan injection, inbox drain
// logic) have ONE maintenance surface instead of four. #283: hook.ts
// (claude-code) migrated onto this factory too — it was the last platform
// carrying its own copy of the four handler bodies, which is what let PR
// #282's Layer 1 shelf change land in hook.ts:356 and hook-handlers.ts:679
// separately instead of once. Claude-specific behavior (identity-recall boot
// identity, the lifecycle representation, boot-time handoff acking) lives
// behind opt-in `AgentHookConfig` knobs — see their doc comments — rather
// than a forked handler set.
//
// Handlers here return INTENT, not wire shapes: what reaches the model is
// decided per platform by hook-platform.ts. Emitting Claude Code's envelope
// everywhere is what silently voided Codex's PreCompact output and discarded
// Grok Build's memory outright.
import { createHash, randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import {
  MEMORY_CONTRACT,
  MINNI_LIFECYCLE_LINE,
  buildLifecycleEmphasis,
  envelopeBudgetFor,
  fitEnvelope,
  hashTaskSignature,
  lifecycleNudgeMode,
  lifecycleSurfaceForIntent,
  wrapEnvelope,
} from "./agent_envelope.js";
import type { EnvelopeEvent, LifecycleSurface } from "./agent_envelope.js";
import {
  BRIDGE_FAILURE_EVENT,
  VALID_EVENTS,
  asString,
  effectiveHookBudgetMs,
  emit,
  inboxPrincipalForVaultPath,
  readStdin,
  stringArray,
  vaultRecallToBody,
  withBudget,
  workspaceFromPayload,
} from "./hook-utils.js";
import type { HookOutput } from "./hook-utils.js";
import {
  deferUntilDelivered,
  discardDeliveryCommits,
  emitAndCommit,
  exitAfterDelivery,
  failAndExit,
  markOutputDropped,
} from "./hook-delivery.js";
import { harvestCompactSummary, harvestSummaryText } from "./compact-harvest.js";
import { harvestTranscriptDelta } from "./jev-harvest.js";
import type { JevTrigger, JevLearnPayload } from "./jev-harvest.js";
import { injectIntent, noIntent, noteIntent } from "./hook-intent.js";
import type { HookIntent } from "./hook-intent.js";
import { canInject, renderIntent, wireCapabilities, wireFor } from "./hook-platform.js";
import type { PlatformWire } from "./hook-platform.js";
import { compactPlanPointer, resolveActivePlanView } from "./plan.js";
import {
  activePlanIdFromVault,
  confirmDeliveryInVault,
  deliverySucceededForHostHook,
  pendingAttentionForHook,
} from "./thread-notification-relay.js";
import type { RelayHook } from "./thread-notification-relay.js";


import { routeMemoryIntent } from "./policy.js";
import {
  advanceSessionTurnIfPresent,
  armingKeyFor,
  buildRecallPointer,
  buildStaleRecallPointer,
  claimArming,
  clearRecallState,
  clearSessionSurfacedOnReset,
  commitSurfacedPointer,
  extractStrongRecall,
  markRecallConsumed,
  pruneOldClaimFiles,
  readLifecycleState,
  readRecallState,
  readSessionSurfacedState,
  recallPointerThreshold,
  recallStatePath,
  recordSessionHandles,
  sanitizeRecallField,
  selectPointerHits,
  sessionStateKey,
  shouldSuppressHit,
  writeLifecycleState,
  writeRecallState,
} from "./recall-state.js";
import type { RecallStateHit } from "./recall-state.js";
import {
  PRE_TOOL_USE_EVENT,
  decideGuard,
  isConsultTool,
  isSubagentSpawnTool,
  preToolUseAllow,
  preToolUseContext,
  preToolUseDeny,
  recallGuardMode as resolveRecallGuardModeFromEnv,
} from "./recall-guard.js";
import type {
  PreToolUseDecisionOutput,
  RecallGuardMode,
} from "./recall-guard.js";
import {
  BOOT_RECALL_LAYERS,
  ackHandoff,
  buildBootRecallSlice,
  buildStatusReport,
  extractIdentityBody,
  extractLearningsSection,
  truncateToTokenCharBudget,
  fetchStaleBeliefEvents,
  formatRecall,
  listPendingHandoffs,
  readAgentContext,
  JSON_RPC_TIMEOUT_ERROR,
  recallMemory,
  stashPrecompactReassert,
  subscribeContradictions,
} from "./daemon_transport.js";
import { classifyIntent, extractScarTissue, filterSafeVaultResults, prepareOutcome } from "./task.js";
// S1a-2: use-capture.js (and use-ledger, flags, harness-envelope behind it) is
// loaded with a dynamic import, and only when ledger-gate.ts says the ledger
// may be on. These type imports are erased: with the ledger off, a hook loads
// nothing it did not load before. S1b: Stop never loads use-capture.js; with
// the ledger maybe on it loads flags.js, use-detector.js and use-ledger.js
// (after delivery) and use-report.js (the receipt), all dynamically.
import { LEDGER_EVENTS, ledgerMayBeOn } from "./ledger-gate.js";
import type { TurnRecord, UseCapture } from "./use-capture.js";
import {
  POST_TOOL_USE_EVENTS,
  clearSessionFailureLessonsOnReset,
  markFailureLessonFired,
  matchPostToolUseFailureLesson,
  matchPreToolUseFailureLesson,
} from "./failure-lessons.js";
import type { PostToolUseDecisionOutput } from "./failure-lessons.js";

type CaptureApi = typeof import("./use-capture.js");

/** One event's use-ledger capture: the loaded module and its capture context. */
interface LedgerCapture {
  api: CaptureApi;
  capture: UseCapture;
}

/** What handleUserPromptSubmit receives from its dispatch arm. */
interface UpsLedgerCapture extends LedgerCapture {
  turn: TurnRecord;
}

interface OpenedLedger {
  ledger: LedgerCapture | null;
  settle(): Promise<void>;
}

// Process-scoped, like hook-delivery's state: a hook process handles one event.
let captureApi: CaptureApi | undefined;

/**
 * use-capture.js, loaded on first use; null when it cannot be loaded (a
 * stripped or half-updated install), so a missing file degrades to "ledger
 * off" instead of failing the hook. Never rejects.
 */
async function loadCaptureApi(): Promise<CaptureApi | null> {
  if (captureApi) return captureApi;
  try {
    captureApi = await import("./use-capture.js");
    return captureApi;
  } catch {
    return null;
  }
}

/**
 * After delivery: one throttled audit line when this process failed to write
 * ledger rows, by count only. Nothing to do when the ledger never loaded.
 */
async function auditLedgerFailures(config: AgentHookConfig): Promise<void> {
  try {
    const failed = captureApi?.ledgerFailureCount() ?? 0;
    if (failed <= 0) return;
    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_use_ledger_failed`,
      summary: `use ledger: ${failed} row(s) not written by this hook`,
      throttleKey: `${config.auditPrefix}_use_ledger_failed`,
    });
  } catch {
    // Losing the count must not cost the event.
  }
}
import {
  assertWriteTargetUnder,
  auditTail,
  collectCorrectionsReassert,
  ensureVault,
  buildPendingLearningsSection,
  expireStaleInboxHandoffs,
  expiredHandoffsBody,
  formatSessionReceiptLine,
  layer1ShelfBody,
  readInboxStatus,
  readLayer1Shelf,
  parkUndeliverableInboxEntries,
  readParkedUndeliverablePending,
  readReassertPending,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  sessionReceipt,
  settleReassertedInboxEntries,
  writeInbox,
} from "./vault.js";

import {
  classifyPromptEnvelope,
  readPromptTemplates,
  recordOccurrence,
} from "./prompt-class.js";
import type { PromptEnvelope, PromptEnvelopeKind } from "./prompt-class.js";

export const SUBAGENT_EVENTS: ReadonlySet<string> = new Set(["SubagentStart", "SubagentStop"]);

export interface SubagentHookOutput {
  continue: true;
  hookSpecificOutput?: {
    hookEventName: "SubagentStart" | "SubagentStop";
    additionalContext: string;
  };
}

type LocalFeatureFlag =
  | "MINNI_USE_LEDGER"
  | "MINNI_BOOT_FIT"
  | "MINNI_GUARD_V2"
  | "MINNI_SUBAGENT_BRIEF"
  | "MINNI_PROMPT_CLASSES"
  | "MINNI_UPS_RANK_V2"
  | "MINNI_RECALL_NOISE_LAST";

/**
 * S2a/S2b/S3a (§8.1): check whether a default-"on" feature switch is enabled
 * without importing `flags.js` when `MINNI_USE_LEDGER` is off (preserving the
 * zero-capture-module-load guarantee pinned by `use-telemetry-flag-off-cost`
 * and `use-telemetry-session-start-audit`). When `ledger` is open, callers read
 * `ledger.capture.context.features.includes(flagName)` instead.
 */
function isFeatureEnabled(vaultPath: string, flagName: LocalFeatureFlag): boolean {
  const rawEnv = process.env[flagName];
  if (rawEnv === "off") return false;
  if (rawEnv === "on") return true;
  if (!vaultPath || typeof vaultPath !== "string") return true;
  const flagsPath = path.join(vaultPath, ".runtime", "flags.json");
  let fd: number | undefined;
  try {
    assertWriteTargetUnder(flagsPath, vaultPath);
    const openFlags =
      fs.constants.O_RDONLY |
      (fs.constants.O_NOFOLLOW ?? 0) |
      (fs.constants.O_NONBLOCK ?? 0);
    fd = fs.openSync(flagsPath, openFlags);
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
    if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
      const val = (parsed as Record<string, unknown>)[flagName];
      if (val === "off") return false;
      if (val === "on") return true;
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

function isBootFitEnabled(vaultPath: string): boolean {
  return isFeatureEnabled(vaultPath, "MINNI_BOOT_FIT");
}

const SUBAGENT_PENDING_TTL_MS = 10 * 60 * 1000;
const SUBAGENT_BRIEF_MAX_CHARS = 600;
const SUBAGENT_RECALL_BUDGET_MS = 1500;

interface SubagentPendingEntry {
  tool_use_id: string;
  subagent_type: string;
  prompt_hash: string;
  prompt: string;
  targetPaths?: string[];
  ts: string;
}

function extractSubagentTargetPaths(toolInput: Record<string, unknown>, prompt: string): string[] {
  const seen = new Set<string>();
  const addPath = (raw: unknown): void => {
    if (typeof raw !== "string") return;
    const cleaned = raw.trim().replace(/^['"`(]+|[)'"`,.;:]+$/g, "");
    if (!cleaned || cleaned.includes("\0")) return;
    seen.add(cleaned);
  };
  for (const field of [toolInput.target_paths, toolInput.targetPaths, toolInput.paths, toolInput.files]) {
    if (Array.isArray(field)) {
      for (const item of field) addPath(item);
    }
  }
  const combinedText = [
    prompt,
    asString(toolInput.prompt),
    asString(toolInput.description),
    asString(toolInput.task),
  ]
    .filter(Boolean)
    .join("\n");
  const pathRegex = /[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,10}/g;
  for (const match of combinedText.matchAll(pathRegex)) {
    const token = match[0];
    const lastDot = token.lastIndexOf(".");
    const stem = lastDot > 0 ? token.slice(0, lastDot) : "";
    const ext = lastDot > 0 ? token.slice(lastDot + 1) : "";
    if (/[A-Za-z]/.test(stem) && /[A-Za-z]/.test(ext)) {
      addPath(token);
    }
  }
  return [...seen].slice(0, 16);
}

function subagentPendingFallbackDir(vaultPath: string, create = false): string | null {
  const segments = [".runtime", "subagent-pending.d"];
  let current = vaultPath;
  for (const segment of segments) {
    const next = path.join(current, segment);
    assertWriteTargetUnder(next, vaultPath);
    let st: fs.Stats | undefined;
    try {
      st = fs.lstatSync(next);
    } catch (error) {
      if ((error as { code?: unknown } | null)?.code !== "ENOENT") throw error;
    }
    if (!st) {
      if (!create) return null;
      try {
        fs.mkdirSync(next, { mode: 0o700 });
      } catch (error) {
        if ((error as { code?: unknown } | null)?.code !== "EEXIST") throw error;
      }
      st = fs.lstatSync(next);
    }
    if (st.isSymbolicLink()) {
      assertWriteTargetUnder(fs.realpathSync(next), vaultPath);
      st = fs.statSync(next);
    }
    if (!st.isDirectory()) {
      throw new Error(`subagent pending path component is not a directory: ${next}`);
    }
    current = next;
  }
  return current;
}

function subagentSessionKey(payload: Record<string, unknown>): string {
  const sessionId = asString(payload.session_id) || asString(payload.sessionId);
  if (sessionId) {
    return sessionStateKey(sessionId) ?? createHash("sha256").update(sessionId, "utf8").digest("hex").slice(0, 16);
  }
  const transcript = asString(payload.transcript_path) || asString(payload.transcriptPath);
  if (transcript && !transcript.includes("\0")) {
    return `tx-${createHash("sha256").update(path.resolve(transcript), "utf8").digest("hex").slice(0, 16)}`;
  }
  return "unlabeled";
}

function subagentSessionDir(vaultPath: string, sessionKey: string, create = false): string | null {
  const safeKey = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(sessionKey) ? sessionKey : "unlabeled";
  const segments = [".runtime", "recall-session.d", safeKey];
  let current = vaultPath;
  for (const segment of segments) {
    const next = path.join(current, segment);
    assertWriteTargetUnder(next, vaultPath);
    let st: fs.Stats | undefined;
    try {
      st = fs.lstatSync(next);
    } catch (error) {
      if ((error as { code?: unknown } | null)?.code !== "ENOENT") throw error;
    }
    if (!st) {
      if (!create) return null;
      try {
        fs.mkdirSync(next, { mode: 0o700 });
      } catch (error) {
        if ((error as { code?: unknown } | null)?.code !== "EEXIST") throw error;
      }
      st = fs.lstatSync(next);
    }
    if (st.isSymbolicLink()) {
      assertWriteTargetUnder(fs.realpathSync(next), vaultPath);
      st = fs.statSync(next);
    }
    if (!st.isDirectory()) {
      throw new Error(`subagent session path component is not a directory: ${next}`);
    }
    current = next;
  }
  return current;
}

function readSubagentPendingList(pendingFile: string, vaultPath: string, nowMs: number): SubagentPendingEntry[] {
  let fd: number | undefined;
  try {
    assertWriteTargetUnder(pendingFile, vaultPath);
    const openFlags =
      fs.constants.O_RDONLY |
      (fs.constants.O_NOFOLLOW ?? 0) |
      (fs.constants.O_NONBLOCK ?? 0);
    fd = fs.openSync(pendingFile, openFlags);
    const st = fs.fstatSync(fd);
    if (!st.isFile() || st.size <= 0 || st.size > 512 * 1024) return [];
    const buf = Buffer.alloc(st.size);
    let total = 0;
    while (total < st.size) {
      const n = fs.readSync(fd, buf, total, st.size - total, total);
      if (n <= 0) break;
      total += n;
    }
    const raw = JSON.parse(buf.subarray(0, total).toString("utf8")) as unknown;
    const entries = Array.isArray(raw)
      ? raw
      : raw !== null && typeof raw === "object" && Array.isArray((raw as { entries?: unknown }).entries)
        ? ((raw as { entries: unknown[] }).entries)
        : [];
    const out: SubagentPendingEntry[] = [];
    for (const item of entries) {
      if (!item || typeof item !== "object") continue;
      const rec = item as Record<string, unknown>;
      const toolUseId = typeof rec.tool_use_id === "string" ? rec.tool_use_id : "";
      const subagentType = typeof rec.subagent_type === "string" ? rec.subagent_type : "";
      const promptHash = typeof rec.prompt_hash === "string" ? rec.prompt_hash : "";
      const prompt = typeof rec.prompt === "string" ? rec.prompt : "";
      const targetPaths = Array.isArray(rec.targetPaths)
        ? rec.targetPaths.filter((x): x is string => typeof x === "string")
        : undefined;
      const ts = typeof rec.ts === "string" ? rec.ts : "";
      if (!toolUseId || !ts) continue;
      const parsedMs = Date.parse(ts);
      if (!Number.isFinite(parsedMs) || nowMs - parsedMs > SUBAGENT_PENDING_TTL_MS) continue;
      out.push({
        tool_use_id: toolUseId,
        subagent_type: subagentType,
        prompt_hash: promptHash,
        prompt,
        ...(targetPaths && targetPaths.length > 0 ? { targetPaths } : {}),
        ts,
      });
    }
    return out;
  } catch {
    return [];
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

function stashSubagentPending(
  vaultPath: string,
  payload: Record<string, unknown>,
  toolInput: Record<string, unknown>,
): void {
  try {
    const rawPrompt = (
      asString(toolInput.prompt) ||
      asString(toolInput.description) ||
      asString(toolInput.task)
    ).trim();
    if (!rawPrompt) return;
    const prompt = rawPrompt.slice(0, 2000);
    const targetPaths = extractSubagentTargetPaths(toolInput, prompt);
    const subagentType =
      asString(toolInput.subagent_type) ||
      asString(toolInput.subagentType) ||
      asString(toolInput.agent_type) ||
      asString(payload.subagent_type) ||
      asString(payload.agent_type) ||
      "default";
    const rawToolUseId =
      asString(payload.tool_use_id) ||
      asString(payload.toolUseId) ||
      randomUUID();
    const safeToolUseId = rawToolUseId.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 64) || randomUUID();
    const rawSessionId = (asString(payload.session_id) || asString(payload.sessionId)).trim();
    const sessionKey = subagentSessionKey(payload);
    const safeKey = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(sessionKey) ? sessionKey : "unlabeled";
    const dir = subagentSessionDir(vaultPath, sessionKey, true);
    if (!dir) return;
    const pendingFile = path.join(dir, "subagent-pending.json");
    assertWriteTargetUnder(pendingFile, vaultPath);
    const nowMs = Date.now();
    const existing = readSubagentPendingList(pendingFile, vaultPath, nowMs).filter(
      (entry) => entry.tool_use_id !== safeToolUseId,
    );
    existing.push({
      tool_use_id: safeToolUseId,
      subagent_type: subagentType.slice(0, 128),
      prompt_hash: createHash("sha1").update(prompt, "utf8").digest("hex").slice(0, 12),
      prompt,
      targetPaths,
      ts: new Date(nowMs).toISOString(),
    });
    const bounded = existing.slice(-32);
    const serialized = `${JSON.stringify({ v: 1, entries: bounded })}\n`;
    const tmpFile = `${pendingFile}.${process.pid}.${randomUUID()}.tmp`;
    assertWriteTargetUnder(tmpFile, vaultPath);
    try {
      fs.writeFileSync(tmpFile, serialized, {
        encoding: "utf8",
        mode: 0o600,
        flag: "wx",
      });
      fs.renameSync(tmpFile, pendingFile);
    } finally {
      try {
        fs.rmSync(tmpFile, { force: true });
      } catch {
        // Ignore cleanup failure.
      }
    }

    const fallbackDir = subagentPendingFallbackDir(vaultPath, true);
    if (fallbackDir) {
      const fallbackFile = path.join(fallbackDir, `${safeKey}.json`);
      assertWriteTargetUnder(fallbackFile, vaultPath);
      const fallbackTmp = `${fallbackFile}.${process.pid}.${randomUUID()}.tmp`;
      assertWriteTargetUnder(fallbackTmp, vaultPath);
      const fallbackSerialized = `${JSON.stringify({
        v: 1,
        session_id: rawSessionId || safeKey,
        targetPaths,
        entries: bounded,
      })}\n`;
      try {
        fs.writeFileSync(fallbackTmp, fallbackSerialized, {
          encoding: "utf8",
          mode: 0o600,
          flag: "wx",
        });
        fs.renameSync(fallbackTmp, fallbackFile);
      } finally {
        try {
          fs.rmSync(fallbackTmp, { force: true });
        } catch {
          // Ignore cleanup failure.
        }
      }
    }
  } catch {
    // Best-effort stash: never blocks Agent/Task PreToolUse.
  }
}

function claimSubagentPending(
  vaultPath: string,
  payload: Record<string, unknown>,
): SubagentPendingEntry | null {
  try {
    const sessionKey = subagentSessionKey(payload);
    const safeKey = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(sessionKey) ? sessionKey : "unlabeled";
    const nowMs = Date.now();
    let dir = subagentSessionDir(vaultPath, sessionKey, false);
    let entries: SubagentPendingEntry[] = [];
    if (dir) {
      const pendingFile = path.join(dir, "subagent-pending.json");
      entries = readSubagentPendingList(pendingFile, vaultPath, nowMs);
    }
    if (entries.length === 0) {
      const fallbackDir = subagentPendingFallbackDir(vaultPath, false);
      if (fallbackDir) {
        const fallbackFile = path.join(fallbackDir, `${safeKey}.json`);
        entries = readSubagentPendingList(fallbackFile, vaultPath, nowMs);
        if (entries.length > 0 && !dir) {
          dir = subagentSessionDir(vaultPath, sessionKey, true) ?? fallbackDir;
        }
      }
    }
    if (!dir || entries.length === 0) return null;
    try {
      for (const name of fs.readdirSync(dir)) {
        if (!name.startsWith("subagent-claim-") || !name.endsWith(".claim")) continue;
        const claimFile = path.join(dir, name);
        const st = fs.lstatSync(claimFile);
        if (st.isFile() && nowMs - st.mtimeMs > SUBAGENT_PENDING_TTL_MS) {
          fs.unlinkSync(claimFile);
        }
      }
    } catch {
      // Ignore cleanup errors.
    }
    const requestedType = (asString(payload.agent_type) || asString(payload.subagent_type)).trim();
    const matchesType = (entry: SubagentPendingEntry): boolean => {
      if (!requestedType || !entry.subagent_type || entry.subagent_type === "default") return true;
      return entry.subagent_type.toLowerCase() === requestedType.toLowerCase();
    };
    const ordered = [
      ...entries.filter(matchesType),
      ...entries.filter((e) => !matchesType(e)),
    ];
    for (const entry of ordered) {
      const safeId = entry.tool_use_id.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 64);
      if (!safeId) continue;
      const claimPath = path.join(dir, `subagent-claim-${safeId}.claim`);
      try {
        assertWriteTargetUnder(claimPath, vaultPath);
        const fd = fs.openSync(claimPath, "wx", 0o600);
        try {
          fs.writeFileSync(fd, `${process.pid} ${nowMs}\n`, "utf8");
        } finally {
          fs.closeSync(fd);
        }
        return entry;
      } catch {
        // Already claimed by a sibling SubagentStart or refused; try next entry.
      }
    }
    return null;
  } catch {
    return null;
  }
}

function enrichTaskHitsWithSnippets(
  hits: readonly RecallStateHit[],
  recallData: Awaited<ReturnType<typeof recallMemory>>["data"] | undefined,
  vaultResults: ReturnType<typeof filterSafeVaultResults>,
  threshold: number,
): Array<RecallStateHit & { snippet?: string }> {
  const pool: Array<{ hit: RecallStateHit; snippet: string }> = [];
  const daemonResults = recallData && Array.isArray(recallData.results) ? recallData.results : [];
  for (const raw of daemonResults) {
    const rec = (raw ?? {}) as Record<string, unknown>;
    const extracted = extractStrongRecall({ results: [raw] }, [], threshold, 1)?.topHits[0];
    const snippet =
      asString(rec.snippet) || asString(rec.summary) || asString(rec.text) || asString(rec.content);
    if (extracted) pool.push({ hit: extracted, snippet });
  }
  for (const vr of vaultResults) {
    const extracted = extractStrongRecall(undefined, [vr], threshold, 1)?.topHits[0];
    if (extracted) pool.push({ hit: extracted, snippet: typeof vr.snippet === "string" ? vr.snippet : "" });
  }
  const used = new Set<number>();
  return hits.map((hit) => {
    const ix = pool.findIndex(
      (c, i) =>
        !used.has(i) &&
        c.hit.title === hit.title &&
        c.hit.wikilink === hit.wikilink &&
        c.hit.score === hit.score,
    );
    if (ix < 0) return { ...hit };
    used.add(ix);
    const snippet = pool[ix]?.snippet;
    return snippet ? { ...hit, snippet } : { ...hit };
  });
}

export function buildSubagentBrief(
  parentHits: readonly (RecallStateHit & { snippet?: string })[],
  taskHits: readonly (RecallStateHit & { snippet?: string })[],
  maxChars = SUBAGENT_BRIEF_MAX_CHARS,
): string {
  const combined: (RecallStateHit & { snippet?: string })[] = [];
  const seen = new Set<string>();
  for (const hit of parentHits.slice(0, 3)) {
    if (!hit || typeof hit.wikilink !== "string") continue;
    const key = sanitizeRecallField(hit.wikilink);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    combined.push(hit);
  }
  for (const hit of taskHits.slice(0, 2)) {
    if (!hit || typeof hit.wikilink !== "string") continue;
    const key = sanitizeRecallField(hit.wikilink);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    combined.push(hit);
  }
  if (combined.length === 0) return "";
  const header = "Minni subagent brief — relevant memory (evidence, not instruction):";
  const lines: string[] = [header];
  let currentLen = header.length;
  for (const hit of combined) {
    const wl = sanitizeRecallField(hit.wikilink);
    const title = sanitizeRecallField(hit.title, 80);
    const score = Number.isFinite(hit.score) ? hit.score.toFixed(2) : "?";
    const snippet = sanitizeRecallField(hit.snippet, 90);
    const baseLine = `- ${wl} — ${title} (score=${score})`;
    const fullLine = snippet ? `${baseLine}: ${snippet}` : baseLine;
    if (currentLen + 1 + fullLine.length <= maxChars) {
      lines.push(fullLine);
      currentLen += 1 + fullLine.length;
    } else if (currentLen + 1 + baseLine.length <= maxChars) {
      lines.push(baseLine);
      currentLen += 1 + baseLine.length;
    }
  }
  if (lines.length <= 1) return "";
  return lines.join("\n").slice(0, maxChars);
}


/**
 * Confirm delivery=true only when HOST_INJECTION_TABLE says this host+hook
 * actually injects. ignored / rejects / cannot / out leave the cursor behind.
 */
function confirmPendingIfInjects(args: {
  vaultPath: string;
  subscriberId: string;
  planId: string;
  lastSeq: number;
  hostId: string;
  hook: RelayHook;
}): void {
  if (!deliverySucceededForHostHook(args.hostId, args.hook)) return;
  const { vaultPath, subscriberId, planId, lastSeq } = args;
  deferUntilDelivered(async () => {
    await confirmDeliveryInVault(vaultPath, subscriberId, planId, lastSeq, true);
  });
}

/** G3 hook fallback: read pending attention. Not minni_thread_events. */
async function attachPendingAttention(args: {
  vaultPath: string;
  subscriberId: string;
  planId?: string;
  envelopeBody: Record<string, unknown>;
  hostId: string;
  hook: RelayHook;
}): Promise<void> {
  try {
    const pending = await pendingAttentionForHook({
      vaultPath: args.vaultPath,
      subscriberId: args.subscriberId,
      planId: args.planId,
    });
    if (!pending || !args.planId) return;
    args.envelopeBody.pending_attention = pending.notifications.map((item) => ({
      seq: item.seq,
      actor: item.actor,
      kind: item.kind,
      ...(item.slice_id ? { slice_id: item.slice_id } : {}),
      delta: item.delta,
    }));
    const lastSeq = pending.notifications[pending.notifications.length - 1]?.seq;
    if (lastSeq === undefined) return;
    confirmPendingIfInjects({
      vaultPath: args.vaultPath,
      subscriberId: args.subscriberId,
      planId: args.planId,
      lastSeq,
      hostId: args.hostId,
      hook: args.hook,
    });
  } catch {
    // Relay is not SoT. A missed attach leaves the cursor behind.
  }
}

export interface AgentHookConfig {
  /** Stamped agent identity (e.g. "codex", "grok-build"). */
  agentId: string;
  vaultPath: string;
  defaultWorkspaceId: string;
  contextWindow: number;
  hooksEnabled: boolean;
  /**
   * identity.runtime AND the `node dist/cli.js read <runtime>` target.
   * Required for the default "agent-context" boot identity; omit it (with
   * `bootIdentity: "identity-recall"`) for agents without a daemon layer1
   * channel (kilocode).
   */
  runtime?: string;
  /**
   * Entry-point script name for the layer1 fallback command (e.g.
   * "codex-hook.js"). Required alongside `runtime` for "agent-context" boots.
   */
  hookScript?: string;
  /** Audit tool-name prefix (e.g. "hook_codex" -> hook_codex_session_start). */
  auditPrefix: string;
  /**
   * Inbox kind for the PreCompact handoff (e.g. "codex_precompact_handoff").
   * The handoff payload also carries the hooks-PL-3 stale-belief stash
   * (`stale_belief_events`), written unconditionally — empty stashes are
   * consumed at the next boot. When omitted, PreCompact does NOT write an
   * inbox handoff file (kilocode's behavior — its envelope carries the scar
   * tissue directly) and instead stashes NON-EMPTY stale-belief events as a
   * dedicated `precompact_reassert` entry, mirroring the claude-code hook.
   */
  precompactKind?: string;
  /**
   * How SessionStart sources boot identity:
   * - "agent-context" (default; codex/grok): daemon readAgentContext layer1
   *   read, surfaced as `layer1_source` + `fallback_commands` and prefixed to
   *   the envelope as native layer1 text. The native layer already carries the
   *   recency-ordered "## Learnings" section, so the envelope intentionally
   *   omits `recent_learnings` (hooks-PL-2 deliberate asymmetry; the matrix
   *   test asserts both sides of this contract).
   * - "identity-recall" (kilocode): no daemon layer1 channel; the read context
   *   is trimmed to its Learnings slice and surfaced as `recent_learnings`.
   * Both modes issue the daemon `read` (the learning_reads writer that
   * stale_beliefs matches on) AND the widened BOOT_RECALL_LAYERS recall.
   */
  bootIdentity?: "agent-context" | "identity-recall";
  /**
   * Stop systemMessage call-to-action after "drafted to inbox (<path>).".
   * Defaults to the MCP-tool phrasing; kilocode points at /minni:learn.
   */
  stopCommitHint?: string;
  // RETIRED 2026-07-25: `alwaysWriteStopInbox`. It once let codex keep the
  // historical "write the Stop inbox file even with zero candidates" behavior;
  // e51ed45 flipped codex to false because always-write produced kind-less
  // inbox noise the AFM ingest loop skips as _unrecognized, leaving every
  // entrypoint on false. Under the governance posture below Stop never
  // self-drafts on any platform, so a per-agent knob for littering the inbox
  // has no legitimate setting — the zero-candidate skip is now unconditional.
  /**
   * s6 PreToolUse recall-guard mode override. When set, it wins over the
   * MINNI_RECALL_GUARD_MODE env default ("off" | "soft" | "strict"). Omit to
   * resolve from the environment (default "soft").
   */
  recallGuardMode?: RecallGuardMode;
  /**
   * Native wire contract for the platform actually running this hook. Defaults
   * to `wireFor(agentId)`. Override in tests, or when an agent identity does
   * not name its own platform.
   */
  wire?: PlatformWire;
  /**
   * This platform's manifest timeout (ms) for the PROMPT-SUBMIT event, as an
   * OUTER bound on the internal budget — see `effectiveHookBudgetMs`. Mirror
   * the `timeout` in `hooks/hooks-<platform>.json`; the two must be edited
   * together, since a manifest tightened below the budget would resurrect the
   * exact bug this bounds (killed mid-work, output discarded).
   *
   * Current values: codex/grok `UserPromptSubmit` 30s, cursor
   * `beforeSubmitPrompt` 30s, gemini `PreInvocation` **10s** (the tightest).
   * Omit for a platform with no declared timeout (kilocode has none at all).
   */
  promptHookTimeoutMs?: number;
  /**
   * This platform's manifest timeout (ms) for SESSION START, as an OUTER bound
   * on the boot budget — same contract as `promptHookTimeoutMs`, and the same
   * obligation to be edited together with `hooks/hooks-<platform>.json`.
   *
   * Boot needs its own bound because the deadlines differ: gemini declares
   * **10s** for SessionStart (hooks-gemini.json) while codex, grok and cursor
   * declare 30s. Without it a cold daemon runs the boot RPCs past gemini's kill
   * and the whole envelope — identity, corrections, active plan — is discarded.
   * `sessionstart-delivery.test.mjs` pins each value to its manifest.
   */
  sessionStartHookTimeoutMs?: number;
  /**
   * #283 migration knob (claude-code only today): the standing 4-surface
   * lifecycle line (`prepare_task -> prepare_outcome -> plan -> learn`) rides
   * SessionStart's envelope (mode-gated by `MINNI_LIFECYCLE_NUDGE_MODE`), and
   * UserPromptSubmit computes/persists a once-per-session emphasis surface
   * and injects a lifecycle-only envelope on the two turns that would
   * otherwise be a bare no-op (write-intent gate, nothing-salient gate) so
   * the line survives every turn. Omit (default false) to leave a platform's
   * envelope untouched by this feature — it was never on for any agent but
   * claude-code, and this knob keeps it that way rather than threading a new
   * per-platform branch through every other hook.
   */
  lifecycleEnabled?: boolean;
  /**
   * #283/#296: SessionStart lists this agent's pending handoff leases and
   * acks each one (stopping when the boot budget runs out — an unacked
   * lease just stays pending next boot), adding `handoff_acks` to the
   * envelope. A failed LIST degrades `handoff_leases`; any lease left
   * unacked (budget-cut or a failed ack) degrades `handoff_acks`. Every
   * platform sharing this factory sets this EXCEPT grok-build (see
   * grok-hook.ts's CONFIG for why): omitting it is a deliberate signal
   * that the wire cannot surface anything at SessionStart, not a default
   * to leave unset casually — acking here always tells the sending agent
   * "accepted" via the lease store, whether or not this platform can
   * actually show the recipient the content.
   */
  ackPendingHandoffsAtBoot?: boolean;
  /** Optional Jev extractor override for testing and inline learning extraction. */
  jevExtractor?: (trigger: JevTrigger, contextText: string) => Promise<JevLearnPayload | undefined>;
}

/** Test seam: lets behavioral tests drive the zero-candidate Stop branch. */
export interface AgentHookDeps {
  prepareOutcome?: typeof prepareOutcome;
}

/**
 * Per-platform inputs to `handleStopCore`. `AgentHookConfig` is a structural
 * superset, so the factory passes its own config straight through; hook.ts
 * builds one from its CLAUDECODE_* constants.
 */
export interface StopCoreConfig {
  agentId: string;
  vaultPath: string;
  defaultWorkspaceId: string;
  auditPrefix: string;
  stopCommitHint?: string;
  /** Platform wire for last-task text; optional so tests can omit it. */
  wire?: PlatformWire;
  /** Optional Jev extractor override for testing and inline learning extraction. */
  jevExtractor?: (trigger: JevTrigger, contextText: string) => Promise<JevLearnPayload | undefined>;
}

/**
 * The ONE Stop implementation, shared by the factory and by hook.ts.
 *
 * hook.ts keeps its own entrypoint because Claude Code's PreCompact cannot
 * inject context, so its handler SET diverges — but its Stop GOVERNANCE is not
 * allowed to diverge, and it did: the claude-code copy dropped
 * `kind`/`agent_id`/`workspace_id` from the inbox write (so
 * inbox_ingest.py filed every Claude Code candidate under workspace "default"
 * regardless of cwd) and omitted `workspace` from its audit breadcrumb, and the
 * zero-candidate guard had to be patched separately in both copies. Nothing
 * about Stop legitimately differs per platform; everything that varies is a
 * field of StopCoreConfig.
 */
export async function handleStopCore(
  config: StopCoreConfig,
  payload: Record<string, unknown>,
  prepareOutcomeFn: typeof prepareOutcome = prepareOutcome,
): Promise<HookOutput> {
  const core = await stopCore(config, payload, prepareOutcomeFn);
  // A direct caller registers nothing after this, so the ledger's work is last.
  if (core.measured) scheduleStopLedgerWork(config, payload);
  return core.output;
}

/** The transcript shape the use detector parses (use-detector.ts reads Claude Code JSONL). */
const DETECTOR_TRANSCRIPT_SHAPE = "claude-code";

interface StopCoreResult {
  output: HookOutput;
  /**
   * This Stop is measured: its wire's transcript is one the detector parses,
   * and ledger-gate says the ledger may be on. The receipt read its use counts,
   * and the caller must schedule the detector, after every other commit.
   */
  measured: boolean;
}

/**
 * S1b (§8.2): one decision for both ledger reads Stop makes, the receipt's use
 * counts and the detector run. Phase 1 measures only wires whose transcript the
 * detector parses (`transcriptShape`, hook-platform.ts), and only a Stop that
 * carries a real session id; anything else is unmeasured, so neither runs and
 * its Stop loads no ledger module, as it does with the ledger off (env, or
 * flags.json, ledger-gate's cheap check).
 * Measured and maybe on: the receipt loads the reader (use-report.js) and,
 * after delivery, the detector runs (use-detector.js). Neither ever loads the
 * capture writers.
 */
async function stopCore(
  config: StopCoreConfig,
  payload: Record<string, unknown>,
  prepareOutcomeFn: typeof prepareOutcome,
): Promise<StopCoreResult> {
  const wire = config.wire ?? wireFor(config.agentId);
  // A Stop with no real session id (the receipt's synthetic "session"
  // fallback) is unmeasured too: its rows would be keyed under a transcript
  // hash no capture used, and the receipt reads no counts on that path.
  const hasSessionId = Boolean(asString(payload.session_id) || asString(payload.sessionId));
  const measured =
    hasSessionId &&
    wireCapabilities(wire).transcriptShape === DETECTOR_TRANSCRIPT_SHAPE &&
    ledgerMayBeOn(config.vaultPath);
  const output = await stopCoreOutput(config, payload, prepareOutcomeFn, measured);
  return { output, measured };
}

/**
 * A measured Stop's post-delivery ledger work, in this order: the detector
 * judges the turn, then one capped slice of the DF recount (rev5 §4.4) runs.
 * Both record only; registered after every other commit (handleStop).
 */
function scheduleStopLedgerWork(config: StopCoreConfig, payload: Record<string, unknown>): void {
  scheduleStopUseDetector(config, payload);
  scheduleStopDfRecount(config);
}

/**
 * Stop: one slice of the DF snapshot recount, after the detector. Nothing
 * while the snapshot is under a day old (one lstat); otherwise about 50 ms of
 * counting (use-df-recount.ts). flags.ts confirms the ledger first. A failure
 * is audited once, throttled, by code. Never fails the hook.
 */
function scheduleStopDfRecount(config: StopCoreConfig): void {
  const vaultPath = config.vaultPath;
  const auditPrefix = config.auditPrefix;
  deferUntilDelivered(
    async () => {
      let problem: string | null = null;
      try {
        const flags = await import("./flags.js");
        if (!flags.flagOn(vaultPath, "MINNI_USE_LEDGER")) return;
        const recount = await import("./use-df-recount.js");
        const result = await recount.runDfRecountSlice(vaultPath);
        if (result.status === "failed") problem = "failed";
      } catch {
        problem = "unavailable";
      }
      if (problem === null) return;
      await recordAudit(vaultPath, {
        tool: `${auditPrefix}_df_recount_failed`,
        summary: `Stop: DF snapshot recount ${problem}; the snapshot stays as it was`,
        throttleKey: `${auditPrefix}_df_recount_failed`,
      }).catch(() => undefined);
    },
    { recordOnly: true },
  );
}

/** Detector statuses that mean this turn could not be judged: one audit line each, throttled. */
const DETECTOR_FAILURES: ReadonlySet<string> = new Set(["invalid", "refused", "write_failed", "error"]);

/**
 * Stop (§4.4): judge the main agent's surfacings against its transcript once
 * this Stop's output has reached the host. Deliver first, record only: the
 * output is already on the wire when the detector starts, so nothing it does
 * (a throw, a module that will not load, its 1.5 s / 2 MB caps) can change a
 * byte of it, and a failure never fails the hook. Nothing runs when the output
 * was not delivered; the next Stop carries the transcript forward. flags.ts
 * confirms the ledger before the detector loads. A failure is audited once,
 * throttled, by code: never the loader's or the detector's message, which can
 * name a path.
 */
function scheduleStopUseDetector(config: StopCoreConfig, payload: Record<string, unknown>): void {
  const vaultPath = config.vaultPath;
  const auditPrefix = config.auditPrefix;
  const wireId = (config.wire ?? wireFor(config.agentId)).id;
  const principal = config.agentId;
  const transcriptPath = asString(payload.transcript_path) || asString(payload.transcriptPath) || null;
  const lastAssistantMessage = asString(payload.last_assistant_message) || null;
  deferUntilDelivered(
    async () => {
      const problem = await runStopUseDetector({ vaultPath, wireId, principal, payload, transcriptPath, lastAssistantMessage });
      if (problem === null) return;
      await recordAudit(vaultPath, {
        tool: `${auditPrefix}_use_detector_failed`,
        summary: `Stop: use detector ${problem}; this turn's surfacings were not judged`,
        throttleKey: `${auditPrefix}_use_detector_failed`,
      }).catch(() => undefined);
    },
    { recordOnly: true },
  );
}

function scheduleSubagentStopUseDetector(config: StopCoreConfig, payload: Record<string, unknown>): void {
  const vaultPath = config.vaultPath;
  const auditPrefix = config.auditPrefix;
  const wireId = (config.wire ?? wireFor(config.agentId)).id;
  const principal = config.agentId;
  const agentTranscriptPath =
    asString(payload.agent_transcript_path) ||
    asString(payload.agentTranscriptPath) ||
    asString(payload.transcript_path) ||
    asString(payload.transcriptPath) ||
    null;
  const lastAssistantMessage = asString(payload.last_assistant_message) || null;
  const subagentType = asString(payload.agent_type) || asString(payload.subagent_type) || undefined;
  const payloadForKey =
    payload.transcript_path || !agentTranscriptPath
      ? payload
      : { ...payload, transcript_path: agentTranscriptPath };
  deferUntilDelivered(
    async () => {
      const problem = await runStopUseDetector({
        vaultPath,
        wireId,
        principal,
        payload: payloadForKey,
        transcriptPath: agentTranscriptPath,
        lastAssistantMessage,
        sessionEnd: true,
        subagentType,
      });
      if (problem === null) return;
      await recordAudit(vaultPath, {
        tool: `${auditPrefix}_use_detector_failed`,
        summary: `SubagentStop: use detector ${problem}; subagent surfacings were not judged`,
        throttleKey: `${auditPrefix}_use_detector_failed`,
      }).catch(() => undefined);
    },
    { recordOnly: true },
  );
}

/** One detector run for Stop or SubagentStop; the failure code, or null. Never rejects. */
async function runStopUseDetector(args: {
  vaultPath: string;
  wireId: string;
  principal: string;
  payload: Record<string, unknown>;
  transcriptPath: string | null;
  lastAssistantMessage: string | null;
  sessionEnd?: boolean;
  subagentType?: string;
}): Promise<string | null> {
  let flags: typeof import("./flags.js");
  try {
    flags = await import("./flags.js");
  } catch {
    return "unavailable";
  }
  try {
    if (!flags.flagOn(args.vaultPath, "MINNI_USE_LEDGER")) return null;
  } catch {
    return "error";
  }
  let detector: typeof import("./use-detector.js");
  let ledger: typeof import("./use-ledger.js");
  try {
    [detector, ledger] = await Promise.all([import("./use-detector.js"), import("./use-ledger.js")]);
  } catch {
    return "unavailable";
  }
  try {
    // Keyed exactly as capture keys its rows (ledgerContextFrom), so the
    // detector reads the session and agent the surfacings were written under.
    const result = detector.runDetector({
      vaultPath: args.vaultPath,
      wire: args.wireId,
      principal: args.principal,
      session_key: ledger.sessionKeyFrom(args.payload),
      agent_key: ledger.agentKeyFrom(args.payload),
      transcriptPath: regularFileOrNull(args.transcriptPath),
      lastAssistantMessage: args.lastAssistantMessage,
      endsTurn: true,
      ...(args.sessionEnd ? { sessionEnd: true } : {}),
      ...(args.subagentType ? { subagent_type: args.subagentType } : {}),
    });
    return DETECTOR_FAILURES.has(result.status) ? result.status : null;
  } catch {
    return "threw";
  }
}

/**
 * The transcript path when it names a regular file (links followed, as the
 * detector's own realpath follows them); null when it names anything else.
 * The detector treats a non-file as "no transcript" too, but only after an
 * open that a FIFO would block; the host waits for this process to exit, so
 * that open must never be reached. A missing path is passed through (the
 * detector's own "no transcript"). Never throws.
 */
function regularFileOrNull(transcriptPath: string | null): string | null {
  if (transcriptPath === null) return null;
  try {
    return fs.statSync(transcriptPath).isFile() ? transcriptPath : null;
  } catch {
    return transcriptPath;
  }
}

/** handleStopCore's body: the Stop output, built before anything is delivered. */
async function stopCoreOutput(
  config: StopCoreConfig,
  payload: Record<string, unknown>,
  prepareOutcomeFn: typeof prepareOutcome,
  useCounts: boolean,
): Promise<HookOutput> {
  await ensureVault(config.vaultPath, { agentId: config.agentId }).catch(() => {});
  // Stop keeps the defaulted "session" fallback (inbox filename, marker text,
  // sessionReceipt) unchanged: unlike recall/audit correlation, a runtime that
  // never labeled any turn with a real session_id still gets a best-effort
  // receipt merged onto the synthetic "session" bucket rather than no receipt.
  const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
  const sessionId = rawSessionId || "session";
  // On the synthetic fallback, turns that DID stamp a real session_id must
  // still count inside this window — otherwise the receipt reports zeros
  // despite real activity.
  // Use counts only when the ledger may be on (handleStopCore's one check):
  // they load the ledger reader. They stand as of this Stop's start: this
  // turn's own judgments land after delivery, so they show on the next Stop.
  const receiptOptions = { includeStamped: !rawSessionId, useCounts };
  const workspaceId = workspaceFromPayload(payload, config.defaultWorkspaceId);
  // Task and summary must stay distinct. Falling back to `summary` for the
  // task field made summary-only payloads compose `"<summary>: <summary>"` —
  // one of the two forward-compat shapes always produced a duplicated candidate.
  // Prefer the platform wire's Stop task text (assistant final message, etc.),
  // then a real user prompt when present; otherwise the session id is enough
  // context for the `"${task}: ${summary}"` form in prepareOutcome. Never fall
  // back to `summary`.
  const lastTask =
    (config.wire?.lastTaskText(payload) || "").trim() ||
    asString(payload.last_user_message) ||
    sessionId;

  // GOVERNANCE POSTURE — Stop auto-draft RETIRED 2026-07-24. The 2026-07-23
  // inbox investigation found Stop's audit-tail distillation produced 0 real
  // learnings across 40 drafts: the tail is Minni's OWN telemetry log, so the
  // draft was noise by construction. The durable-capture path is now
  // EXCLUSIVELY the agent's explicit minni_prepare_outcome / minni_learn call.
  // Stop no longer self-drafts. The `changedFiles`/`summary` branch below is a
  // documented FORWARD-COMPAT hook: it fires only if a future Stop harness
  // supplies genuine outcome material in the payload (today's real harnesses
  // never do). Absent that — or when the material scrubs to zero candidates
  // at the guard below — Stop records ONE log-only breadcrumb (so the session
  // is not silently invisible) and writes no inbox file. That breadcrumb is
  // load-bearing, and it is only actually reaching log.md because vault.ts
  // exempts `*_stop` from the hook_* audit throttle — before that exemption a
  // same-second UserPromptSubmit swallowed it and the claim above was false.
  // Keep the two in sync. This holds on EVERY
  // platform, claude-code included: there is no per-agent opt-out (see the
  // retired `alwaysWriteStopInbox` note on AgentHookConfig).
  // NOTE: this is a governance-posture change — it needs operator sign-off
  // before the plugin is re-propagated.
  //
  // Jev Inline Learning: harvest high-signal discoveries off hot-path at Stop.
  const transcriptPath =
    asString(payload.transcript_path) || asString(payload.transcriptPath);
  if (transcriptPath) {
    await harvestTranscriptDelta(
      {
        vaultPath: config.vaultPath,
        workspaceId,
        agentId: config.agentId,
        auditPrefix: config.auditPrefix,
        extractor: config.jevExtractor,
      },
      {
        transcriptPath,
        sessionId,
        event: "Stop",
        sessionCrons: payload.session_crons ?? payload.sessionCrons,
      },
    ).catch(() => undefined);
  }

  const changedFiles = stringArray(payload.changedFiles ?? payload.changed_files);
  const outcomeSummary = asString(payload.summary).trim();
  const hasDraftableSignal = changedFiles.length > 0 || outcomeSummary.length > 0;

  async function emitStopBreadcrumb(reason: string): Promise<HookOutput> {
    // Tally BEFORE writing the stop row so the receipt embeds in its own details
    // without counting this marker's candidates (always 0 on breadcrumb paths).
    const receipt = await sessionReceipt(
      config.vaultPath,
      sessionId,
      500,
      receiptOptions,
    ).catch(() => undefined);
    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_stop`,
      summary: `stop ${sessionId}`,
      details: {
        workspace: workspaceId,
        candidates: 0,
        reason,
        ...(receipt ? { receipt } : {}),
      },
    }).catch(() => {});
    // Surface the receipt even with zero activity: proof that no memory work
    // happened is itself the signal. Platforms without a note channel drop it
    // via renderIntent; the audit marker still closes the session window.
    return {
      continue: true,
      ...(receipt ? { systemMessage: formatSessionReceiptLine(receipt) } : {}),
    };
  }

  if (!hasDraftableSignal) {
    return emitStopBreadcrumb("no_draftable_signal");
  }

  const outcome = await prepareOutcomeFn({
    task: lastTask.slice(0, 200),
    summary: outcomeSummary,
    changedFiles,
    profile: "compact",
    vaultPath: config.vaultPath,
  });

  const candidates = outcome.outcomeDraft.learnCandidates;

  // The signal was draftable but prepareOutcome's telemetry filter
  // (isAuditTelemetryLine) scrubbed it to nothing — e.g. a harness that
  // hands Stop a slice of Minni's own audit log as `summary`. The scrub has
  // to hold at the WRITE layer too: writing the file anyway reintroduces
  // precisely the empty-inbox litter issue #173 removed, and the AFM ingest
  // loop then skips it as unrecognized noise. One log-only breadcrumb (same
  // shape as the no-draftable-signal branch above) keeps the session visible
  // without costing an inbox file. This is also the guard that keeps holding
  // when the drafter itself yields nothing for `changedFiles` without a
  // `summary` — the write layer must not depend on the drafter's verdict.
  if (candidates.length === 0) {
    return emitStopBreadcrumb("no_candidates_after_scrub");
  }

  // `kind`/`agent_id`/`workspace_id` are the INGEST CONTRACT, not decoration:
  // src/minni/afm_passes/inbox_ingest.py reads `workspace_id or "default"`, so
  // an unstamped file lands in the catch-all workspace no matter what cwd the
  // session ran in, and `agent_id` is cross-checked against the vault-derived
  // principal for provenance. `kind` names the FILE FORMAT, never the author.
  //
  // The `agent_id` stamp is the INGEST's principal (inboxPrincipalForVaultPath),
  // NOT `config.agentId`. That cross-check is an equality test that DROPS the
  // file on mismatch, and the configured id is operator-settable
  // (MINNI_CLAUDECODE_AGENT_ID=claudecode) while the principal is derived from
  // the vault dir name — so stamping the config id silently discarded every
  // Claude Code candidate as `_agent_mismatch`. Undefined (a vault dir with no
  // `-vault` suffix) writes NO stamp: an absent agent_id skips the cross-check,
  // which is the pre-stamp behavior and strictly better than a wrong guess.
  const inbox = await writeInbox(config.vaultPath, sessionId, {
    kind: "stop_candidates",
    agent_id: inboxPrincipalForVaultPath(config.vaultPath),
    workspace_id: workspaceId,
    candidates,
    log_only: outcome.outcomeDraft.logOnly,
    expires: outcome.outcomeDraft.expires,
    do_not_store: outcome.outcomeDraft.doNotStore,
    last_task: lastTask.slice(0, 200),
  });

  // Tally BEFORE writing the stop entry so the receipt embeds in its own audit
  // details (candidates_drafted then reflects prior stops, not this one — the
  // display line below merges THIS stop's drafts so it never contradicts the
  // candidate sentence beside it).
  const receipt = await sessionReceipt(
    config.vaultPath,
    sessionId,
    500,
    receiptOptions,
  ).catch(() => undefined);
  await recordAudit(config.vaultPath, {
    tool: `${config.auditPrefix}_stop`,
    summary: `stop ${sessionId}`,
    details: {
      candidates: candidates.length,
      workspace: workspaceId,
      inbox_path: inbox.filePath,
      ...(receipt ? { receipt } : {}),
    },
  }).catch(() => {});

  const displayReceipt = receipt
    ? { ...receipt, candidates_drafted: receipt.candidates_drafted + candidates.length }
    : undefined;
  const receiptLine = displayReceipt ? ` ${formatSessionReceiptLine(displayReceipt)}` : "";

  return {
    continue: true,
    systemMessage: `Minni: ${candidates.length} candidate learning${
      candidates.length === 1 ? "" : "s"
    } drafted to inbox (${inbox.filePath}). ${
      config.stopCommitHint ?? "Use minni_prepare_outcome/minni_learn to review and commit."
    }${receiptLine}`,
  };
}

export interface AgentHookHandlers {
  handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput>;
  handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput>;
  handlePreToolUse(payload: Record<string, unknown>): Promise<PreToolUseDecisionOutput>;
  handlePostToolUse(
    event: "PostToolUse" | "PostToolUseFailure",
    payload: Record<string, unknown>,
  ): Promise<PostToolUseDecisionOutput>;
  handlePreCompact(payload: Record<string, unknown>): Promise<HookOutput>;
  handlePostCompact(payload: Record<string, unknown>): Promise<HookOutput>;
  handleCompactSummary(payload: Record<string, unknown>): Promise<HookOutput>;
  handleStop(payload: Record<string, unknown>): Promise<HookOutput>;
  handleSubagentStart(payload: Record<string, unknown>): Promise<SubagentHookOutput>;
  handleSubagentStop(payload: Record<string, unknown>): Promise<SubagentHookOutput>;
  dispatch(
    event: string,
    payload: Record<string, unknown>,
  ): Promise<HookOutput | PreToolUseDecisionOutput | SubagentHookOutput | PostToolUseDecisionOutput>;
}

/**
 * Path-only compact harvest (no `source`, non-empty transcript path) is
 * **opt-in**. Default off so a host that always attaches a path and never
 * sends `source` (agy/gemini shape, and any unverified host) does not
 * rediscover the every-cold-boot tail-read tax.
 *
 * Empty by design until a live transcript is verified Claude-shaped
 * (`isCompactSummary: true`). Cursor's contract explicitly warns not to mine
 * `transcript_path` blind; codex / grok-build shapes are also unverified for
 * this extractor. Universal `source === "compact" | "resume"` still harvests
 * on every runtime. Matched against `config.runtime ?? config.agentId`.
 */
const PATH_ONLY_COMPACT_HARVEST_ALLOW = new Set<string>([]);

/** Wait cap for path-only residual SessionStart harvest (ms). That residual is
 * almost always a miss and must not burn the boot budget before
 * identity/corrections RPCs. `withBudget` only bounds wait time; it does not
 * cancel work. compact|resume is NOT wait-capped — see harvest block below. */
const COMPACT_HARVEST_PATH_ONLY_BUDGET_MS = 250;

/** How long before the UserPromptSubmit deadline the active-plan lock wait gives
 * up (ms). `withBudget` does not cancel work, so a wait that ends AT the
 * deadline keeps polling the plan lock after the hook has answered; this slack
 * lets the THREAD_BUSY audit land inside the budget instead. */
const PLAN_LOCK_SLACK_MS = 250;

/** True when path-only (no `source`) SessionStart may start a tail-read. */
export function isPathOnlyCompactHarvestAllowed(platform: string | undefined): boolean {
  const id = (platform ?? "").trim().toLowerCase();
  return id.length > 0 && PATH_ONLY_COMPACT_HARVEST_ALLOW.has(id);
}

/**
 * #283 (`config.lifecycleEnabled`): build this turn's lifecycle
 * representation fields. The PERSISTENT line is always present in "soft"
 * mode so the 4 surfaces stay in view; the situational `lifecycle_focus` is
 * added when the prompt's ambition intent maps to a surface NOT yet
 * emphasized this session. "off" mode returns `{}` — fully silent. The
 * caller must persist any returned `emphasizedSurface` so the focus fires at
 * most once per surface per session. Representation only — never a
 * permission decision. Pure / agent-agnostic (reads the env mode directly),
 * so it lives at module scope rather than inside `createHookHandlers`.
 */
function lifecycleFieldsFor(
  prompt: string,
  emphasized: Set<string>,
): {
  fields: { lifecycle?: string; lifecycle_focus?: string };
  emphasizedSurface?: LifecycleSurface;
} {
  if (lifecycleNudgeMode() === "off") return { fields: {} };
  const fields: { lifecycle?: string; lifecycle_focus?: string } = {
    lifecycle: MINNI_LIFECYCLE_LINE,
  };
  const surface = lifecycleSurfaceForIntent(classifyIntent(prompt));
  if (surface && !emphasized.has(surface)) {
    fields.lifecycle_focus = buildLifecycleEmphasis(surface);
    return { fields, emphasizedSurface: surface };
  }
  return { fields };
}

export function createHookHandlers(
  config: AgentHookConfig,
  deps: AgentHookDeps = {},
): AgentHookHandlers {
  const workspaceFor = (payload: Record<string, unknown>): string =>
    workspaceFromPayload(payload, config.defaultWorkspaceId);
  const bootIdentity = config.bootIdentity ?? "agent-context";
  const prepareOutcomeFn = deps.prepareOutcome ?? prepareOutcome;
  const wire = config.wire ?? wireFor(config.agentId);

  // Handlers express intent; the platform wire decides what can actually be
  // said. An injection the platform cannot carry is recorded, never silently
  // swallowed -- that silence is exactly what hid Grok Build's and Kilocode's
  // missing memory for so long.
  const render = async (intent: HookIntent): Promise<HookOutput> => {
    const { output, dropped } = renderIntent(wire, intent);
    if (dropped) {
      // The payload never went on the wire, so nothing it was carrying may be
      // consumed — see markOutputDropped. The write below still SUCCEEDS (it is
      // a noop), which is exactly why the flush alone cannot be trusted.
      markOutputDropped();
      try {
        await recordAudit(config.vaultPath, {
          tool: `${config.auditPrefix}_intent_dropped`,
          summary: `${dropped.event}: ${dropped.reason}`,
          // Bucket per EVENT. Every drop shares one tool name, so without this
          // a Stop drop within 5s of a UserPromptSubmit drop is throttled away
          // and reported as written -- losing the only record that memory
          // failed to land.
          throttleKey: `${config.auditPrefix}_intent_dropped__${dropped.event}`,
        });
      } catch {
        // Audit unavailable. The drop is still correct behavior; losing the
        // record must not also lose the event.
      }
    }
    return output as HookOutput;
  };

  // `ledger` (S1a-2) is passed only by dispatch, with the use ledger on; a
  // direct call records nothing.
  async function handleSessionStart(
    payload: Record<string, unknown>,
    ledger?: LedgerCapture,
  ): Promise<HookOutput> {
    // rawSessionId is the payload's own id, possibly empty — never the
    // "session" synthetic fallback. It is what gets threaded into the daemon
    // recall-trace (recallMemory's sessionId) so unlabeled runtimes never
    // conflate into one synthetic thread_id. sessionId keeps the historical
    // defaulted value for envelope identity / inbox filenames / markers.
    const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
    const sessionId = rawSessionId || "session";
    const workspaceId = workspaceFor(payload);
    // BOOT BUDGET: platforms kill SessionStart and DISCARD its output — gemini
    // at 10s (hooks-gemini.json). A boot that overruns loses the whole envelope,
    // so we own a shorter deadline and deliver whatever landed inside it. The
    // deadline is ABSOLUTE and shared: each read gets only what is left, and
    // once it is spent the rest resolve instantly with their degraded value so
    // the envelope — and, critically, the corrections below — still ship.
    //
    // The clock starts at handler ENTRY: the harness deadline runs from process
    // start, so any setup done first (vault creation + compaction harvest) is
    // time already spent against it.
    const budgetMs = effectiveHookBudgetMs(config.sessionStartHookTimeoutMs);
    const deadline = Date.now() + budgetMs;
    const remainingMs = (): number => Math.max(0, deadline - Date.now());
    const rpcTimedOut = { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR };
    await ensureVault(config.vaultPath, { agentId: config.agentId });

    // Compaction-summary harvest backstop (P3 / #227). Claude Code and Codex
    // deliver primarily via PostCompact (`hooks.json` / `hooks-codex.json`);
    // Kilo delivers via CompactSummary (its bridge SessionStart supplies
    // neither transcript_path nor source — so this backstop is dead for real
    // Kilo boots). For platforms without a post-compaction hook event (cursor,
    // grok-build, gemini) — or as a next-boot backstop if PostCompact failed —
    // this SessionStart path captures the summary from the transcript tail.
    //
    // Gate:
    //  1. compact|resume `source` — Claude parity (any runtime). FULLY
    //     awaited: withBudget races rather than cancels, and exitAfterDelivery
    //     then kills the process, so a budget-cut harvest is permanent loss of
    //     the only write path. Match hook.ts: bare await.
    //  2. path-only residual — only when `source` is absent entirely AND the
    //     runtime is on PATH_ONLY_COMPACT_HARVEST_ALLOW (opt-in). Default
    //     off: hosts that always attach a path and never send `source`
    //     (agy/gemini) would otherwise tax every cold boot for a guaranteed
    //     no_summary_found. Wait-capped only (still not cancel-safe).
    // Content-hash dedup keeps dual delivery safe. Fail-open, raw only —
    // daemon compact_distillation organizes later.
    const bootSource = asString(payload.source);
    const transcriptPath =
      asString(payload.transcript_path) || asString(payload.transcriptPath);
    const platform = config.runtime ?? config.agentId;
    const isCompactOrResume = bootSource === "compact" || bootSource === "resume";
    const pathOnlyAllowed =
      !bootSource &&
      Boolean(transcriptPath) &&
      isPathOnlyCompactHarvestAllowed(platform);
    if (isCompactOrResume) {
      // Do not start when the boot budget is already spent — a late start is
      // more likely to overrun the harness than to land a durable write. Once
      // started, await fully (never withBudget-race this path).
      if (remainingMs() > 0) {
        await harvestCompactSummary(
          {
            vaultPath: config.vaultPath,
            workspaceId,
            auditPrefix: config.auditPrefix,
            platform,
          },
          {
            transcriptPath,
            sessionId,
          },
        );
      }
    } else if (pathOnlyAllowed && remainingMs() > 0) {
      // Residual is almost always a miss → tight wait-cap so it cannot exhaust
      // identity/corrections RPCs. withBudget only bounds wait time; it does
      // not cancel the tail-read. Acceptable for opt-in residual; not for the
      // only write path above.
      await withBudget(
        harvestCompactSummary(
          {
            vaultPath: config.vaultPath,
            workspaceId,
            auditPrefix: config.auditPrefix,
            platform,
          },
          {
            transcriptPath,
            sessionId,
          },
        ),
        Math.min(remainingMs(), COMPACT_HARVEST_PATH_ONLY_BUDGET_MS),
        { harvested: false, reason: "harvest_error" },
      );
    }

    // #283 (config.lifecycleEnabled): reset the once-per-session lifecycle
    // emphasis on a fresh session, so the situational focus can fire again
    // this session. Best-effort — a state-write failure must not break boot.
    if (config.lifecycleEnabled) {
      await writeLifecycleState(config.vaultPath, {
        session_id: sessionId,
        emphasized: [],
      }).catch(() => {});
    }
    await clearSessionSurfacedOnReset(config.vaultPath, rawSessionId || sessionId).catch(() => {});
    clearSessionFailureLessonsOnReset(config.vaultPath, rawSessionId || sessionId);

    // TTL-reap stale file handoffs BEFORE the honest read so they neither occupy
    // the capped slice nor inflate totals; they surface once below as 'expired'.
    //
    // DELIBERATELY UNBUDGETED. The reaper ARCHIVES as it walks, and withBudget
    // races rather than cancels — so a budget-cut reaper would keep archiving in
    // the background while this boot reported an empty `expired` list. Archived
    // entries are invisible to readInboxStatus, so they would then be reported
    // ZERO times, breaking the surfaces-exactly-once contract. A budget may skip
    // a READ; it must not half-report a WRITE.
    const expiredHandoffs = await expireStaleInboxHandoffs(config.vaultPath);
    const [status, tail, identityRead, recall, contradictions, inboxStatus] = await Promise.all([
      withBudget(
        // timeoutMs, not just the withBudget race: the race ABANDONS the status
        // RPC, and an abandoned socket handle keeps this process alive past the
        // harness kill — which discards output we had already written.
        buildStatusReport({ vaultPath: config.vaultPath, timeoutMs: remainingMs() }),
        remainingMs(),
        undefined,
      ),
      withBudget(auditTail(config.vaultPath, 5), remainingMs(), undefined),
      // hooks-PL-2 leg (a): BOTH boot modes issue the daemon 'read' — it is
      // the recency-ordered learning surface AND the path that records
      // learning_reads, which stale_beliefs matches on. agent-context boots
      // inject it whole as native layer 1; identity-recall boots trim it to
      // recent_learnings below.
      withBudget(
        readAgentContext({ agentId: config.agentId, limit: 8, timeoutMs: remainingMs() }),
        remainingMs(),
        rpcTimedOut,
      ),
      // recall-F1: boot recall must include the correction-bearing layers, not
      // just the identity shelf (the widened search is what lets knowledge-
      // layer corrections rank in). See BOOT_RECALL_LAYERS for the policy.
      withBudget(
        recallMemory({
          query: `boot identity for ${workspaceId}`,
          layers: BOOT_RECALL_LAYERS,
          limit: 8,
          agentId: config.agentId,
          workspaceId,
          // The load-bearing deadline: it destroys the socket, which is what
          // lets the hook PROCESS exit. Racing a promise alone cannot do that.
          timeoutMs: remainingMs(),
          ...(rawSessionId ? { sessionId: rawSessionId } : {}),
        }),
        remainingMs(),
        rpcTimedOut,
      ),
      // hooks-PL-1/PL-2: corrections to beliefs this agent read must
      // re-surface at boot (stale_beliefs), on every platform.
      withBudget(
        subscribeContradictions({ agentId: config.agentId, timeoutMs: remainingMs() }),
        remainingMs(),
        rpcTimedOut,
      ),
      withBudget(readInboxStatus(config.vaultPath, 3), remainingMs(), undefined),
    ]);
    const pending = inboxStatus?.entries ?? [];
    // The `ok` flag distinguishes "the budget cut this read" from its natural
    // empty result — an [] fallback alone is indistinguishable from "no handoff
    // refs", which is the false all-clear this whole section exists to avoid.
    const handoffRead = await withBudget<{
      ok: boolean;
      result: Awaited<ReturnType<typeof resolveInboxHandoffContext>>;
    }>(
      resolveInboxHandoffContext(config.vaultPath, pending).then((result) => ({
        ok: true,
        result,
      })),
      remainingMs(),
      { ok: false, result: { snippets: [], withheldCount: 0 } },
    );
    const handoffContext = handoffRead.result.snippets;
    // #340: count of handoff_context refs withheld by #312's privacy gate
    // this boot. Zero and "budget cut this read" both read as "omit the
    // field" below — a budget-cut read genuinely has no withheld count to
    // report, and reporting 0 there would be the same false all-clear
    // degradedSections exists to prevent for handoff_context itself.
    const handoffContextWithheld = handoffRead.ok ? handoffRead.result.withheldCount : 0;

    // #283 (config.ackPendingHandoffsAtBoot): list this agent's pending
    // handoff leases and ack each one at boot. Sequential per-lease RPCs,
    // deliberately: unbounded in the number of pending handoffs, so it is
    // the loop most able to blow the deadline. Stop acking when the budget
    // is gone — an unacked lease is still pending next boot; a killed hook
    // is not. A FAILED ack is an unacked lease too — counting only the
    // budget-skip path would report a timed-out or refused ack as clean.
    let pendingHandoffs: { ok: boolean; data?: unknown; error?: unknown } = {
      ok: true,
      data: { handoffs: [] },
    };
    const ackedLeases: string[] = [];
    let unackedLeases = 0;
    if (config.ackPendingHandoffsAtBoot) {
      pendingHandoffs = await withBudget(
        listPendingHandoffs({ agentId: config.agentId, timeoutMs: remainingMs() }),
        remainingMs(),
        rpcTimedOut,
      );
      const pendingHandoffData =
        pendingHandoffs.ok && pendingHandoffs.data
          ? (pendingHandoffs.data as {
              handoffs?: Array<{ lease_id?: string; leaseId?: string }>;
            })
          : { handoffs: [] };
      for (const handoff of pendingHandoffData.handoffs ?? []) {
        const leaseId = handoff.lease_id ?? handoff.leaseId;
        if (!leaseId) continue;
        if (remainingMs() <= 0) {
          unackedLeases += 1;
          continue;
        }
        const ack = await withBudget(
          ackHandoff({
            leaseId,
            status: "accepted",
            agentId: config.agentId,
            timeoutMs: remainingMs(),
          }),
          remainingMs(),
          rpcTimedOut,
        );
        if (ack.ok) ackedLeases.push(leaseId);
        else unackedLeases += 1;
      }
    }

    // hooks-PL-3: re-assert corrections stashed by PreCompact, so the
    // post-compaction boot re-injects them even if the daemon is down now.
    // Consumed entries are settled (exactly-once re-injection, no unbounded
    // inbox growth); cap-overflowed tails are rewritten so they re-inject on
    // the next boot, and all-malformed entries survive for inspection.
    // I5: use the reassert-specific window so recent all-malformed files cannot
    // crowd valid corrections out of the newest-N slots.
    //
    // Deliberately UNBUDGETED: this is cheap local FS, and it is the one read
    // whose absence loses data rather than context.
    const reassertPending = await readReassertPending(config.vaultPath, 3);
    // Layer 1 shelf inline (operator scar: an envelope carrying only identity
    // keys + a pointer meant every agent re-hit scars already recorded in
    // layer1/core.md). SessionStart ONLY — UserPromptSubmit's envelope stays
    // lean, see handleUserPromptSubmit below, which does not call this.
    // Unbudgeted for the same reason as reassertPending above: cheap, bounded
    // local FS (stat + a capped read, never unbounded I/O), no RPC — not the
    // class of read the boot budget exists to bound.
    const layer1Shelf = await readLayer1Shelf(config.vaultPath);
    const { events: correctionsReassert, consumedPaths: reassertConsumed, contributingPaths: reassertContributing, deferredTails: reassertDeferred } =
      collectCorrectionsReassert(reassertPending);
    // R2: settle AFTER the envelope reaches the host, never before. Archiving
    // here would consume the correction inside the window where the boot can
    // still be killed, losing it for good — see hook-delivery.ts.
    //
    // The deferral is conditional on this envelope actually CARRYING something.
    // When nothing was injected, the consumed paths are empty stashes (codex and
    // grok stash unconditionally at PreCompact) — those carry no correction, so
    // clearing them does not depend on delivery, and making it depend would let
    // one file per compaction cycle pile up forever on a platform whose wire can
    // never inject at SessionStart.
    // Settled in TWO parts, because the two halves have different preconditions.
    // Empty stashes carry no correction, so clearing them cannot depend on
    // delivery — and must not, or a platform that can never inject would keep
    // them forever. Entries that CONTRIBUTED events (and partially-injected
    // tails) may only settle once the envelope carrying them lands. Deferring
    // both together held the empties hostage whenever one window contained a
    // real correction AND an empty stash.
    //
    // The three outcomes are counted separately for the audit below: settling
    // eagerly, waiting on delivery, and parked as undeliverable are different
    // facts, and one "pending" number covering all three would misreport two of
    // them. In a change about audit honesty that matters.
    const emptyPaths = reassertConsumed.filter(
      (filePath) => !reassertContributing.includes(filePath),
    );
    let reassertClearedEager = 0;
    let reassertPendingClear = 0;
    let reassertParked = 0;
    // Counted from the branch that REGISTERS the rewrite, not derived from
    // reassertPendingClear: a single file carrying MAX+N valid events defers a
    // tail while contributing no consumed path, so a count gated on
    // "contributing > 0" reported zero deferred tails while one was pending.
    let reassertTailsPendingDefer = 0;
    if (emptyPaths.length > 0) {
      reassertClearedEager = emptyPaths.length;
      await settleReassertedInboxEntries(config.vaultPath, {
        consumedPaths: emptyPaths,
        deferredTails: [],
      });
    }
    if (reassertContributing.length > 0 || reassertDeferred.length > 0) {
      if (canInject(wire, "SessionStart")) {
        reassertPendingClear = reassertContributing.length;
        reassertTailsPendingDefer = reassertDeferred.length;
        deferUntilDelivered(() =>
          settleReassertedInboxEntries(config.vaultPath, {
            consumedPaths: reassertContributing,
            deferredTails: reassertDeferred,
          }),
        );
      } else {
        // STRUCTURAL drop, not a transient one: this wire declares SessionStart
        // un-injectable, so "keep it and retry next boot" never terminates —
        // one durable file per correction-bearing compaction, each permanently
        // occupying a slot in the newest-N reassert window. Park them instead:
        // out of the window, still on disk, and audited, so the backlog is
        // countable rather than either silently destroyed or infinite.
        //
        // Delivery is issue #253: handleStop reads inbox/.undeliverable and
        // injects at Stop (the only injectable event on such a wire). Parking
        // here is the bound; Stop is the route. Do not archive until Stop
        // confirms delivery — same deliver-before-consume rule as boot.
        const parkPaths = [
          ...reassertContributing,
          ...reassertDeferred.map((tail) => tail.filePath),
        ];
        const parked = await parkUndeliverableInboxEntries(parkPaths);
        reassertParked = parked.length;
        await recordAudit(config.vaultPath, {
          tool: `${config.auditPrefix}_undeliverable_on_wire`,
          summary: `SessionStart: ${wire.id} cannot inject at SessionStart; parked ${parked.length} correction entr${
            parked.length === 1 ? "y" : "ies"
          } to inbox/.undeliverable for Stop delivery (issue #253)`,
          details: {
            wire: wire.id,
            parked: parked.length,
            events: correctionsReassert.length,
            delivery: "stop_routed",
          },
        }).catch(() => {});
      }
    }

    // Plan parity (audit C5): SessionStart injects the FULL active-plan view for
    // boot/rehydration, exactly like the claude-code hook.
    // `undefined` from resolveActivePlanView MEANS "no active plan" (or a
    // terminal/healed plan suppressed from injection). FS/lock failures throw
    // ActivePlanReadError / ThreadBusyError — withBudget maps those rejections
    // onto planRead.ok === false so a failed read is degraded, not empty.
    let planRead: { ok: boolean; view: Awaited<ReturnType<typeof resolveActivePlanView>> } = {
      ok: true,
      view: undefined,
    };
    // No try/catch around resolveActivePlanView: withBudget turns rejection
    // into its fallback. A budget cut still surfaces via planRead.ok === false
    // -> degraded.sections.
    const planBudgetMs = remainingMs();
    planRead = planBudgetMs <= 0 ? { ok: false, view: undefined } : await withBudget(
      resolveActivePlanView(
        config.vaultPath,
        layer1Shelf.ok && !layer1Shelf.truncated ? layer1Shelf.content : undefined,
        { lockWaitMs: Math.max(0, planBudgetMs - PLAN_LOCK_SLACK_MS) },
      ).then((view) => ({ ok: true, view })),
      remainingMs(),
      { ok: false, view: undefined },
    );
    const activePlan = planRead.view;

    // Sections the budget cut. Named so a degraded boot is legible as degraded
    // rather than as an agent with nothing in its memory (hooks-PL-5). Computed
    // AFTER every budgeted read, so a read that runs late can still report itself.
    const degradedSections = [
      status === undefined ? "status" : undefined,
      tail === undefined ? "audit_tail" : undefined,
      inboxStatus === undefined ? "pending_learnings" : undefined,
      // config.ackPendingHandoffsAtBoot only: a no-op (always undefined) on
      // every other platform, since unackedLeases stays 0 there.
      config.ackPendingHandoffsAtBoot && unackedLeases > 0 ? "handoff_acks" : undefined,
      // Also degraded when the PARENT read was cut: handoffContext is resolved
      // from inboxStatus.entries, so a cut inbox read makes it vacuously empty.
      // ok:true over an empty set the hook never actually had inputs for is the
      // same false all-clear this section exists to prevent.
      handoffRead.ok && inboxStatus !== undefined ? undefined : "handoff_context",
      planRead.ok ? undefined : "active_thread",
      // config.ackPendingHandoffsAtBoot only: a no-op (pendingHandoffs.ok
      // stays true) on every other platform.
      config.ackPendingHandoffsAtBoot && !pendingHandoffs.ok ? "handoff_leases" : undefined,
    ].filter((section): section is string => section !== undefined);

    const bootFitOn = ledger
      ? ledger.capture.context.features.includes("MINNI_BOOT_FIT")
      : isBootFitEnabled(config.vaultPath);

    const envelopeBody: Record<string, unknown> = {
      contract: MEMORY_CONTRACT,
      identity: {
        agent: config.agentId,
        workspace: workspaceId,
        vault: config.vaultPath,
        session_id: sessionId,
        // Unknown reads as not-ok, never as ok: `degraded.sections` below says
        // which of these the budget cut, so "false" is not read as "checked".
        daemon_ok: status?.socket.ok ?? false,
        afm_ok: status?.afm.ok ?? false,
        ...(config.runtime !== undefined ? { runtime: config.runtime } : {}),
      },
      // Omitted rather than zero-filled when the budget cut the inbox read: a
      // "0 pending" the hook never counted is a false all-clear.
      ...(inboxStatus !== undefined
        ? { pending_learnings: buildPendingLearningsSection(inboxStatus, expiredHandoffs) }
        : {}),
      // The reaper is UNBUDGETED and already archived these, so this boot is
      // their only chance to be reported — but they normally ride inside
      // pending_learnings, which the line above drops when the budgeted inbox
      // read degraded. That combination reported them ZERO times, the exact
      // failure the reaper is unbudgeted to avoid. Ship them on their own
      // whenever their usual carrier is missing.
      ...(inboxStatus === undefined && expiredHandoffs.length > 0
        ? { expired_handoffs: expiredHandoffsBody(expiredHandoffs) }
        : {}),
      handoff_context: handoffContext.map((snippet) => ({
        ref: snippet.ref,
        path: snippet.relativePath,
        snippet: snippet.snippet,
      })),
      // #340: present only when this boot actually withheld something —
      // absent means "nothing withheld", never "zero, checked" (same
      // omit-vs-zero discipline as pending_learnings/expired_handoffs
      // above). Distinguishes an all-refs-gated handoff (handoff_context:
      // [] because every ref was privacy-gated) from a handoff that
      // legitimately carried no refs at all — both previously produced the
      // identical empty array with no way to tell them apart. Count only:
      // never which ref(s) or why.
      ...(handoffContextWithheld > 0 ? { handoff_context_withheld: handoffContextWithheld } : {}),
      // config.ackPendingHandoffsAtBoot only.
      ...(config.ackPendingHandoffsAtBoot ? { handoff_acks: ackedLeases } : {}),
      // hooks-PL-1: discriminated stale-belief payload (matched /
      // checked_no_match from the daemon; explicit status:"error" here so
      // events:[] can never masquerade as "checked and clean").
      stale_beliefs:
        contradictions.ok && contradictions.data
          ? contradictions.data
          : { ok: false, status: "error", error: contradictions.error },
      recall:
        recall.ok && recall.data
          ? buildBootRecallSlice(recall.data, config.agentId, { diet: bootFitOn })
          : { ok: false, error: recall.error },
      ...(tail !== undefined
        ? { audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]) }
        : {}),
      // Always present (H2: never a silently missing key) — ok:false carries
      // an "absent: <reason>" string, ok:true carries the content plus an
      // explicit truncation marker whenever the 8KB cap trimmed it (H5: no
      // silent truncation). See vault.ts readLayer1Shelf / layer1ShelfBody.
      layer1_shelf: layer1ShelfBody(layer1Shelf),
    };

    // #283 (config.lifecycleEnabled): the standing 4-surface lifecycle line
    // at boot so the agent sees the spine from session start (unless the
    // feature is off). No other platform's SessionStart carries this field.
    if (config.lifecycleEnabled && lifecycleNudgeMode() !== "off") {
      envelopeBody.lifecycle = MINNI_LIFECYCLE_LINE;
    }

    if (correctionsReassert.length > 0) {
      envelopeBody.corrections_reassert = correctionsReassert;
    }

    if (degradedSections.length > 0) {
      // Say so IN the envelope: a boot the budget truncated must never be
      // indistinguishable from a boot that found nothing (hooks-PL-5).
      envelopeBody.degraded = {
        sections: degradedSections,
        budget_ms: budgetMs,
        note: "Boot exceeded the SessionStart budget (likely a cold daemon). The listed sections were NOT read — treat them as unknown, not empty, and call minni_recall explicitly if they matter.",
      };
    }

    if (bootIdentity === "agent-context") {
      envelopeBody.layer1_source =
        identityRead.ok && identityRead.data?.context
          ? {
              ok: true,
              agent_origin: identityRead.data.agent_id ?? config.agentId,
              backend: identityRead.data.backend,
            }
          : { ok: false, error: identityRead.error };
      envelopeBody.fallback_commands = {
        layer1: `node dist/${config.hookScript} SessionStart < /dev/null`,
        daemon_read: `node dist/cli.js read ${config.runtime}`,
        recall: "node dist/cli.js prepare '<task>'",
      };
      // hooks-PL-2 (deliberate asymmetry with hook.ts/kilocode): NO
      // recent_learnings envelope field here. agent-context boots inject the
      // FULL daemon read context as native Layer 1 below — including its
      // recency-ordered "## Learnings" section — so a trimmed duplicate
      // inside the envelope would be pure redundancy. The matrix test asserts
      // both sides of this contract (recent_learnings === undefined AND the
      // Learnings section present in the native layer).
    } else {
      envelopeBody.recent_learnings =
        identityRead.ok && identityRead.data?.context
          ? {
              ok: true,
              context:
                extractLearningsSection(identityRead.data.context) ??
                "No recent learnings.",
            }
          : { ok: false, error: identityRead.error };
    }

    if (activePlan !== undefined) {
      envelopeBody.active_thread = activePlan;
    }
    await attachPendingAttention({
      vaultPath: config.vaultPath,
      subscriberId: config.agentId,
      planId: activePlan?.plan_id,
      envelopeBody,
      hostId: wire.id,
      hook: "sessionStart",
    });

    const budget = envelopeBudgetFor(config.contextWindow);
    if (identityRead.ok && identityRead.data?.context) {
      const identityBody = extractIdentityBody(identityRead.data.context, config.agentId);
      if (identityBody) {
        envelopeBody.identity_body = truncateToTokenCharBudget(
          identityBody,
          Math.max(budget - 500, 0),
        );
      }
    }

    let nativeLayer1 =
      bootIdentity === "agent-context" && identityRead.ok && identityRead.data?.context
        ? truncateToTokenCharBudget(identityRead.data.context.trim(), budget)
        : "";

    const bodyForEnvelope = bootFitOn
      ? fitEnvelope(envelopeBody, {
          tokenLimit: budget,
          charLimit: wireCapabilities(wire).hostInlineCapChars,
          vaultPath: config.vaultPath,
          event: "SessionStart",
          agent: config.agentId,
          nativeLayer1,
          onNativeLayer1Trimmed: (trimmedNative) => {
            nativeLayer1 = trimmedNative;
          },
        })
      : envelopeBody;

    const envelope = wrapEnvelope({
      event: "SessionStart",
      agent: config.agentId,
      budget,
      body: bodyForEnvelope,
      legacyKeySort: !bootFitOn,
    });

    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_session_start`,
      summary: `boot ${sessionId}`,
      details: {
        daemon_ok: status?.socket.ok ?? false,
        afm_ok: status?.afm.ok ?? false,
        pending_inbox: inboxStatus?.totalPending ?? null,
        expired_handoffs: expiredHandoffs.length,
        handoff_context: handoffContext.length,
        workspace: workspaceId,
        corrections_reassert: correctionsReassert.length,
        budget_ms: budgetMs,
        ...(degradedSections.length > 0 ? { degraded_sections: degradedSections } : {}),
        // The three settle outcomes, reported separately. "pending" counts ONLY
        // what is still registered with deferUntilDelivered — empties already
        // cleared and entries parked as undeliverable are not waiting on
        // anything, and folding them in here claimed archives that were never
        // going to happen.
        reassert_entries_cleared_eager: reassertClearedEager,
        reassert_entries_pending_clear: reassertPendingClear,
        reassert_entries_parked: reassertParked,
        reassert_tails_pending_defer: reassertTailsPendingDefer,
        // S1b (§8.2 "ledger-off stretches stay visible"): whether this boot
        // records to the use ledger, and, when it does, the switches in effect
        // (the capture's own `features`: featuresInEffect's list, from the one
        // flags read dispatch made). "off" needs no module: dispatch opened no
        // capture because ledger-gate (or flags.ts) said off, or the capture
        // module would not load; a direct handler call records nothing either.
        use_ledger: ledger ? "on" : "off",
        ...(ledger ? { features: [...ledger.capture.context.features] } : {}),
      },
    });

    const bootText = [nativeLayer1, envelope].filter(Boolean).join("\n\n");
    const fittedRecall =
      bootFitOn &&
      bodyForEnvelope.recall !== null &&
      typeof bodyForEnvelope.recall === "object"
        ? (bodyForEnvelope.recall as Record<string, unknown>)
        : undefined;
    const recallForCapture: typeof recall =
      bootFitOn && recall.ok && recall.data && Array.isArray(fittedRecall?.results)
        ? {
            ...recall,
            data: {
              ...recall.data,
              results: fittedRecall.results as unknown[],
            },
          }
        : recall;
    const trimmedMap =
      bootFitOn &&
      bodyForEnvelope.trimmed !== null &&
      typeof bodyForEnvelope.trimmed === "object"
        ? (bodyForEnvelope.trimmed as Record<string, unknown>)
        : undefined;
    const fittedLearningsObj =
      bootFitOn &&
      bodyForEnvelope.recent_learnings !== null &&
      typeof bodyForEnvelope.recent_learnings === "object" &&
      !Array.isArray(bodyForEnvelope.recent_learnings)
        ? (bodyForEnvelope.recent_learnings as Record<string, unknown>)
        : undefined;
    const learningsTrimmed =
      bootIdentity === "agent-context" ||
      (trimmedMap !== undefined && "recent_learnings" in trimmedMap);
    const survivingLearningsText =
      bootIdentity === "agent-context"
        ? nativeLayer1
        : typeof fittedLearningsObj?.context === "string"
          ? fittedLearningsObj.context
          : undefined;
    const identityForCapture: typeof identityRead =
      learningsTrimmed &&
      identityRead.ok &&
      identityRead.data &&
      survivingLearningsText !== undefined
        ? {
            ...identityRead,
            data: {
              ...identityRead.data,
              context: survivingLearningsText,
              ...(Array.isArray(
                (identityRead.data as Record<string, unknown>).recent_learnings,
              )
                ? {
                    recent_learnings: (
                      (identityRead.data as Record<string, unknown>)
                        .recent_learnings as unknown[]
                    ).flatMap((entry) => {
                      if (!entry || typeof entry !== "object") return [];
                      const rec = entry as Record<string, unknown>;
                      const line = rec.line;
                      if (typeof line !== "string" || line.length === 0) return [];
                      if (survivingLearningsText.includes(line)) {
                        return [entry];
                      }
                      // Also match bullets clipped by fitEnvelope Step 9a (`${prefix}...`)
                      for (const survLine of survivingLearningsText.split("\n")) {
                        const trimmedSurv = survLine.trimEnd();
                        if (
                          trimmedSurv.endsWith("...") &&
                          trimmedSurv.length >= 20 &&
                          line.startsWith(trimmedSurv.slice(0, -3))
                        ) {
                          return [{ ...rec, line: trimmedSurv }];
                        }
                      }
                      return [];
                    }),
                  }
                : {}),
            },
          }
        : identityRead;
    // S1a-2: what this boot surfaced, judged on the exact emitted string. The
    // rows are deferred and land only once this output does.
    ledger?.api.captureBootSurfacings(ledger.capture, {
      emitted: bootText,
      recall: recallForCapture,
      identity: identityForCapture,
    });
    return render(injectIntent("SessionStart", bootText));
  }

  // `use` (S1a-2) comes from the dispatch arm's withTurnRecord: every return
  // below names its exit on it first. A direct call records nothing.
  async function handleUserPromptSubmit(
    payload: Record<string, unknown>,
    use?: UpsLedgerCapture,
  ): Promise<HookOutput> {
    const exit = (reason: string): void => {
      if (use) use.turn.exit = reason;
    };
    // BUDGET (parity with the claude-code hook): every host kills a prompt-time
    // hook and DISCARDS its output, so an overrun costs the whole turn's
    // injection silently. The budget is clamped to this platform's manifest
    // timeout (`promptHookTimeoutMs`) so it always finishes inside the deadline
    // that will actually kill it — gemini's 10s PreInvocation is the tightest.
    // The clock starts at handler ENTRY so vault setup below counts against it.
    const budgetMs = effectiveHookBudgetMs(config.promptHookTimeoutMs);
    const deadline = Date.now() + budgetMs;
    const remainingMs = (): number => Math.max(0, deadline - Date.now());

    const prompt = asString(payload.prompt) || asString(payload.user_prompt);
    if (!prompt.trim()) {
      exit("empty_prompt");
      return render(noIntent);
    }

    // Awaited, not raced: the budget clock already counts it (deadline set at
    // entry), and a raced ensureVault that lost the race kept seeding vault
    // files after this handler answered. Same as every other handler here.
    await ensureVault(config.vaultPath, { agentId: config.agentId });

    const workspaceId = workspaceFor(payload);
    // rawSessionId (possibly empty) is the audit/recall-trace correlation id;
    // sessionId keeps the "session" fallback for envelope identity only — see
    // handleSessionStart's comment for why the two must not be conflated.
    const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
    const signature = hashTaskSignature(prompt);

    // #283 (config.lifecycleEnabled): compute the lifecycle representation
    // once for this turn, BEFORE the early-return gates, so the persistent
    // line survives them. Read the once-per-session emphasis set and persist
    // any newly-emphasized surface. `lifecycleFields` stays `{}` — a no-op —
    // on every other platform.
    let lifecycleFields: { lifecycle?: string; lifecycle_focus?: string } = {};
    if (config.lifecycleEnabled) {
      const lifecycleStatePrev = await readLifecycleState(config.vaultPath);
      const emphasizedSurfaces = new Set(lifecycleStatePrev?.emphasized ?? []);
      const computed = lifecycleFieldsFor(prompt, emphasizedSurfaces);
      lifecycleFields = computed.fields;
      if (computed.emphasizedSurface) {
        emphasizedSurfaces.add(computed.emphasizedSurface);
        await writeLifecycleState(config.vaultPath, {
          session_id: lifecycleStatePrev?.session_id ?? "session",
          emphasized: [...emphasizedSurfaces],
        }).catch(() => {});
      }
    }

    // #283: emit an envelope carrying the lifecycle representation only —
    // used at this handler's two early-returns (write-intent and
    // nothing-salient gates) so the 4 surfaces survive turns that would
    // otherwise inject nothing. Degrades to a plain no-op when
    // `lifecycleFields` is empty (lifecycleEnabled is off, feature mode is
    // "off", or no fields were computed this turn) — safe to call
    // unconditionally on every platform.
    const lifecycleOnlyOutput = (): Promise<HookOutput> => {
      if (lifecycleFields.lifecycle === undefined && lifecycleFields.lifecycle_focus === undefined) {
        return render(noIntent);
      }
      const envelope = wrapEnvelope({
        event: "UserPromptSubmit",
        agent: config.agentId,
        body: {
          identity: {
            agent: config.agentId,
            workspace: workspaceId,
            task_signature: signature,
          },
          ...lifecycleFields,
        },
      });
      return render(injectIntent("UserPromptSubmit", envelope));
    };

    const guardV2 = use
      ? use.capture.context.features.includes("MINNI_GUARD_V2")
      : isFeatureEnabled(config.vaultPath, "MINNI_GUARD_V2");
    const promptClassesOn = use
      ? use.capture.context.features.includes("MINNI_PROMPT_CLASSES")
      : isFeatureEnabled(config.vaultPath, "MINNI_PROMPT_CLASSES");
    const recallStateOpts = { sessionId: rawSessionId || undefined, guardV2 };
    const promptAgentId = asString(payload.agent_id) || asString(payload.agentId) || undefined;
    const promptId = asString(payload.prompt_id) || asString(payload.promptId) || undefined;

    let promptEnvelope: PromptEnvelope | undefined;
    if (promptClassesOn) {
      if (use?.turn.classified !== undefined) {
        const turnClass = await use.turn.classified;
        if (turnClass.envelope_kind !== null && turnClass.subkind !== null) {
          promptEnvelope = {
            kind: turnClass.envelope_kind as PromptEnvelopeKind,
            subkind: turnClass.subkind,
            body: turnClass.body ?? classifyPromptEnvelope(prompt).body,
            template_rule: turnClass.template_rule ?? "off",
            ...(turnClass.rule_id !== undefined ? { rule_id: turnClass.rule_id } : {}),
            ...(turnClass.suspicious_prefix ? { suspicious_prefix: true } : {}),
          };
        } else {
          promptEnvelope = classifyPromptEnvelope(prompt);
        }
      } else {
        const at = Date.now();
        const validPromptId =
          promptId !== undefined && promptId.length > 0 && promptId.length <= 128
            ? promptId
            : undefined;
        const pid = validPromptId ?? `turn:${randomUUID()}`;
        const templates = await readPromptTemplates(config.vaultPath).catch(() => null);
        promptEnvelope = classifyPromptEnvelope(prompt, {
          at,
          pid,
          templateEntry: (hash) =>
            templates !== null && Object.hasOwn(templates.templates, hash)
              ? templates.templates[hash]
              : undefined,
        });
        if (
          promptEnvelope.template !== undefined &&
          isFeatureEnabled(config.vaultPath, "MINNI_USE_LEDGER")
        ) {
          const sessionKeyPrefix = (
            (rawSessionId ? sessionStateKey(rawSessionId) : null) ?? "unlabeled"
          ).slice(0, 8);
          await recordOccurrence(config.vaultPath, {
            hash: promptEnvelope.template.hash,
            ts: at,
            pid,
            sessionKeyPrefix,
            now: at,
          }).catch(() => false);
        }
      }
    }

    const isSkip = promptEnvelope?.kind === "skip";
    const recallQuery = promptEnvelope ? promptEnvelope.body : prompt;
    const envelopeAuditDetails = promptEnvelope
      ? {
          envelope_kind: promptEnvelope.kind,
          subkind: promptEnvelope.subkind,
          ...(promptEnvelope.suspicious_prefix ? { suspicious_prefix: true } : {}),
        }
      : {};

    const intent = routeMemoryIntent(isSkip ? prompt : recallQuery);
    // Explicit WRITE intents (learn/vault_write carry automaticAllowed:false) are
    // the user dictating memory, not asking the agent to recall — inject no
    // pointer and write no state. (s5 parity with the claude-code hook.)
    // S3a (§5.2.2): `skip` envelopes never clear or write `recall-state` and
    // must still carry `active_thread_ref` when a plan is active.
    if (!isSkip && !intent.automaticAllowed) {
      // Clear any stale strong state from a previous turn BEFORE returning: an
      // unconsumed pointer must not leak into this write-intent turn and let the
      // s6 guard deny an unrelated read/search here (parity with the weak-turn
      // path below, which also clears).
      await clearRecallState(config.vaultPath, recallStateOpts).catch(() => {});
      // #283: the persistent lifecycle visibility must survive this
      // write-intent early-return.
      exit("write_intent");
      return lifecycleOnlyOutput();
    }

    const threshold = recallPointerThreshold();
    // #313: searchVaultNotes scores and ranks across the WHOLE vault, then
    // slices to `limit` before privacy is ever considered (it only drops
    // `blocked` notes internally; `private`/`local-only` ride along, see
    // vault.ts). Asking for exactly 6 here meant a private-heavy vault could
    // fill all 6 slots with non-safe notes that outscore a genuinely safe
    // match ranked 7th+ — filterSafeVaultResults below would then correctly
    // exclude every one of those 6, and the safe note never even reached
    // this function to be considered. Not a privacy leak (nothing unsafe
    // escaped) but a silent availability gap: memory the agent should have
    // received was dropped because filtering ran on the wrong side of the
    // truncation. Over-fetch a wider pre-filter set, filter, THEN slice to
    // the actual 6 this call site has always exposed downstream (recall
    // pointer, recall-state top_hits, PreToolUse deny-reason text) — same
    // external cap, same behavior for the common case.
    //
    // NARROWS the gap, does not close it: a vault where 19+ non-safe notes
    // (frontmatter-authored OR heuristic-escalated — see task.ts
    // privacyForSource, a materially larger population than just explicit
    // `privacy: private` notes) all outscore the safe match reproduces the
    // identical crowd-out, just needing more decoys. A durable fix would
    // move the safe/non-safe gate inside searchVaultNotes itself, ahead of
    // its own sort→slice, so no caller-side limit can ever crowd out a
    // lower-scored safe note. Left as a follow-up (adversarial review
    // finding) rather than expanding this fix's scope.
    const VAULT_RESULT_LIMIT = 6;
    const VAULT_SEARCH_OVERFETCH = 18;
    let vaultResults: Awaited<ReturnType<typeof searchVaultNotes>> = [];
    let recall: Awaited<ReturnType<typeof recallMemory>> = {
      ok: true as const,
      data: { results: [] },
    };
    let daemonTimedOut = false;
    let strong: ReturnType<typeof extractStrongRecall> = null;
    let recallStateFile: string | undefined;
    let stalePointer: string | undefined;

    if (!isSkip) {
      const upsRankV2 = use
        ? use.capture.context.features.includes("MINNI_UPS_RANK_V2")
        : isFeatureEnabled(config.vaultPath, "MINNI_UPS_RANK_V2");
      const ledgerEnabled = isFeatureEnabled(config.vaultPath, "MINNI_USE_LEDGER");
      const surfacedState = upsRankV2
        ? await readSessionSurfacedState(
            config.vaultPath,
            rawSessionId || undefined,
            use?.turn.turn_ix,
            ledgerEnabled,
          )
        : undefined;
      // Only pre-exclude vault paths whose prior vault score is > 0.90 (so even
      // a max 1.00 score cannot achieve the >= 0.10 jump required to override suppression).
      const excludePaths: string[] = [];
      if (surfacedState) {
        for (const [mkey, entry] of Object.entries(surfacedState.surfaced)) {
          const priorVaultScore = entry?.scores?.vault ?? entry?.score ?? 0;
          if (
            mkey.startsWith("vault:") &&
            entry &&
            entry.reset_id === surfacedState.resetId &&
            surfacedState.thisTurn - entry.turn_ix >= 1 &&
            surfacedState.thisTurn - entry.turn_ix <= 10 &&
            priorVaultScore > 0.9
          ) {
            excludePaths.push(mkey.slice("vault:".length));
          }
        }
      }
      // A spent budget gets the same fallbacks withBudget(0) would return, but
      // without starting work that would keep running (and writing: the cold
      // vault scan seeds missing vault files) after this handler answers.
      const recallBudgetSpent = remainingMs() <= 0;
      const hasQuery = Boolean(recallQuery.trim());
      const [vaultResultsRaw, recallRaw] = !hasQuery
        ? [
            [] as Awaited<ReturnType<typeof searchVaultNotes>>,
            { ok: true as const, data: { results: [] } } as Awaited<ReturnType<typeof recallMemory>>,
          ]
        : recallBudgetSpent
          ? [
              [] as Awaited<ReturnType<typeof searchVaultNotes>>,
              { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR } as Awaited<
                ReturnType<typeof recallMemory>
              >,
            ]
          : await Promise.all([
              withBudget(
                // "scan": a hook is a fresh process that searches once and exits, so
                // the prefix index the "index" path builds would be paid on every
                // prompt for a single query. Same candidates, same results — see
                // VaultSearchCandidates in vault.ts.
                searchVaultNotes(
                  config.vaultPath,
                  recallQuery,
                  VAULT_SEARCH_OVERFETCH,
                  config.agentId,
                  upsRankV2
                    ? {
                        candidates: "scan",
                        rank: "ups_gate",
                        ...(excludePaths.length > 0 ? { excludePaths } : {}),
                      }
                    : {
                        candidates: "scan",
                      },
                ),
                remainingMs(),
                [],
              ),
              withBudget(
                recallMemory({
                  query: recallQuery,
                  limit: 6,
                  agentId: config.agentId,
                  workspaceId,
                  // The load-bearing deadline: it destroys the socket, which is what
                  // lets the hook PROCESS exit. Racing a promise alone cannot do that.
                  timeoutMs: remainingMs(),
                  ...(rawSessionId ? { sessionId: rawSessionId } : {}),
                }),
                remainingMs(),
                { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR },
              ),
            ]);
      // SEC-006 (found during #283 review): searchVaultNotes returns every
      // match regardless of privacy level — only `filterSafeVaultResults`
      // keeps `local-only`/`private` notes and privacy-heuristic escalations
      // (secrets/tokens/paths, see task.ts privacyForSource) out of the
      // model-facing recall pointer, the
      // persisted recall-state top_hits, and the PreToolUse guard's deny-
      // reason text (all three read from `strong`/`vaultResults` below). This
      // filter existed in claude-code's pre-#283 hook.ts but was never ported
      // here when codex/grok-build/cursor/gemini/kilocode were extracted onto
      // this factory — every platform sharing this handler has been leaking
      // non-safe notes into UserPromptSubmit. Fixed once, for all six.
      vaultResults = filterSafeVaultResults(vaultResultsRaw).slice(0, VAULT_RESULT_LIMIT);
      recall = recallRaw;
      // "Ran out of time" is NOT "there is nothing" — only a real answer may drive
      // the weak-turn path below, which CLEARS recall-state.
      daemonTimedOut = !recall.ok && recall.error === JSON_RPC_TIMEOUT_ERROR;
      // s5 strength gate: emit the light pointer + recall-state file ONLY when the
      // top recall strength clears the threshold; otherwise inject nothing and
      // clear any stale state left by a previous strong turn.
      strong = extractStrongRecall(
        recall.ok ? recall.data : undefined,
        vaultResults,
        threshold,
        5,
        { rankV2: upsRankV2, vaultPath: config.vaultPath },
      );
      if (upsRankV2 && strong && surfacedState) {
        const unsuppressed = strong.topHits.filter((hit) => !shouldSuppressHit(hit, surfacedState));
        strong =
          unsuppressed.length > 0 && selectPointerHits(unsuppressed).length > 0
            ? { topScore: unsuppressed[0].score, topHits: unsuppressed }
            : null;
      }
      if (strong) {
        // s6 UNCONSULTED deny is a backstop for an envelope the model actually
        // received. Grok UPS/SS and Cursor UPS structurally drop inject
        // (GROK_INJECTABLE={Stop}, CURSOR_INJECTABLE={SessionStart}); planting
        // consumed=false here would deny cold tools against memory the model
        // never saw. Do not expand those injectable sets — this gate follows them.
        if (canInject(wire, "UserPromptSubmit")) {
          try {
            recallStateFile = await writeRecallState(
              config.vaultPath,
              {
                task_signature: signature,
                intent: intent.action,
                top_hits: strong.topHits,
                top_score: strong.topScore,
                session_id: rawSessionId || undefined,
                agent_id: promptAgentId,
                prompt_id: promptId,
              },
              recallStateOpts,
            );
          } catch {
            // best-effort: a state-write failure must not break the hook
          }
          await recordSessionHandles(
            config.vaultPath,
            rawSessionId || undefined,
            strong.topHits,
          ).catch(() => {});
          if (upsRankV2 && surfacedState) {
            const pointerHits = selectPointerHits(strong.topHits);
            deferUntilDelivered(
              () =>
                commitSurfacedPointer(
                  config.vaultPath,
                  rawSessionId || undefined,
                  surfacedState,
                  pointerHits,
                ),
              { recordOnly: true },
            );
          }
        } else {
          // Prior dropped-UPS plants left consumed=false on disk; this turn
          // would have overwritten them with another false plant. Clear instead
          // of leaving the guard armed against an undelivered envelope.
          await clearRecallState(config.vaultPath, recallStateOpts).catch(() => {});
        }
      } else if (daemonTimedOut) {
        // DEGRADED, not weak — but only keep the pointer if this wire could
        // have delivered it. Grok UPS/SS and Cursor UPS drop inject, so a
        // leftover consumed=false would arm UNCONSULTED against an envelope
        // the model never saw. Same gate as the plant: canInject(UPS) or clear.
        if (canInject(wire, "UserPromptSubmit")) {
          stalePointer = buildStaleRecallPointer(
            await readRecallState(config.vaultPath, recallStateOpts).catch(() => null),
          );
        } else {
          await clearRecallState(config.vaultPath, recallStateOpts).catch(() => {});
        }
      } else {
        await clearRecallState(config.vaultPath, recallStateOpts).catch(() => {});
        if (
          upsRankV2 &&
          surfacedState &&
          surfacedState.hasExistingSurfaced &&
          canInject(wire, "UserPromptSubmit")
        ) {
          deferUntilDelivered(
            () =>
              advanceSessionTurnIfPresent(
                config.vaultPath,
                rawSessionId || undefined,
                surfacedState,
              ),
            { recordOnly: true },
          );
        }
      }
    }

    let activePlan: Awaited<ReturnType<typeof resolveActivePlanView>> = undefined;
    let activePlanReadOk = true;
    // Same contract as SessionStart: withBudget maps FS/lock throws AND a
    // spent prompt budget onto ok=false so a failed/unread plan is degraded,
    // not empty. A raw await here waits out THREAD_BUSY (default 5s lock)
    // after the recall budget — the host then kills the hook and discards
    // the envelope, which looks like "no active plan" again.
    //
    // withBudget races rather than cancels, so a read whose lock wait outlived
    // the budget kept polling the plan lock after this handler had answered.
    // Do not start it once the budget is spent (same degraded outcome), and
    // make its lock wait give up PLAN_LOCK_SLACK_MS before the deadline, so
    // the THREAD_BUSY audit lands inside the budget rather than after it.
    const planBudgetMs = remainingMs();
    const planRead = planBudgetMs <= 0 ? { ok: false as const, view: undefined } : await withBudget(
      resolveActivePlanView(config.vaultPath, undefined, {
        lockWaitMs: Math.max(0, planBudgetMs - PLAN_LOCK_SLACK_MS),
      })
        .then((view) => ({ ok: true as const, view }))
        .catch(async (error: unknown) => {
          await recordAudit(config.vaultPath, {
            tool: `${config.auditPrefix}_active_plan_error`,
            summary: `UserPromptSubmit: ${error instanceof Error ? error.message : String(error)}`,
          }).catch(() => {});
          return { ok: false as const, view: undefined };
        }),
      remainingMs(),
      { ok: false as const, view: undefined },
    );
    if (!planRead.ok) {
      activePlanReadOk = false;
    } else {
      activePlan = planRead.view;
    }

    const planRef = activePlan !== undefined ? compactPlanPointer(activePlan) : undefined;

    // Nothing salient to inject this turn: no strong recall, no stale fallback
    // pointer AND no active plan. A failed plan read is salient — degraded,
    // not the empty-pointer path.
    if (!strong && stalePointer === undefined && planRef === undefined && activePlanReadOk) {
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_user_prompt_submit`,
        summary: prompt.slice(0, 120),
        details: {
          intent: intent.action,
          vault_matches: vaultResults.map((result) => result.relativePath),
          daemon_ok: recall.ok,
          daemon_timed_out: daemonTimedOut,
          task_signature: signature,
          workspace: workspaceId,
          recall_strong: false,
          ...envelopeAuditDetails,
          // RAW id only — omit rather than stamp the synthetic "session"
          // fallback, so unlabeled turns don't conflate into one audit thread.
          ...(rawSessionId ? { session_id: rawSessionId } : {}),
        },
      });
      // #283: even with nothing salient (no strong recall, no active plan)
      // the persistent lifecycle line still rides this turn's envelope.
      exit("nothing_salient");
      return lifecycleOnlyOutput();
    }

    const envelopeBody: Record<string, unknown> = {
      // #283 (config.lifecycleEnabled): persistent lifecycle line (+
      // situational focus) rides the salient turn too. `{}` on every other
      // platform.
      ...lifecycleFields,
      identity: {
        agent: config.agentId,
        workspace: workspaceId,
        task_signature: signature,
      },
    };
    let recallPointer: string | undefined;
    if (strong) {
      // LIGHT POINTER, not the full pack: the full top hits live in the portable
      // recall-state file (read by the s6 guard); the prompt only gets a signpost.
      recallPointer = buildRecallPointer(strong);
      envelopeBody.recall_pointer = recallPointer;
      envelopeBody.recall_state = recallStateFile;
    } else if (stalePointer !== undefined) {
      envelopeBody.recall_pointer = stalePointer;
      envelopeBody.recall_stale = true;
      envelopeBody.recall_state = recallStatePath(config.vaultPath);
    }
    if (daemonTimedOut) {
      // A degraded turn must never be indistinguishable from a clean one: the
      // agent should read absent recall as "not looked up", not "nothing found".
      envelopeBody.degraded = {
        daemon_recall: "timed_out",
        budget_ms: budgetMs,
        note: "Daemon recall exceeded the hook budget (likely a cold daemon loading models). Memory was NOT consulted this turn — call minni_recall explicitly if it matters.",
      };
    }

    // Plan parity (audit C5): per-turn injection is a compact plan POINTER, not
    // the full plan — same budget discipline as the claude-code hook (Option C).
    // (planRef !== undefined iff activePlan !== undefined; guard on activePlan so
    // the compiler narrows it for compactPlanPointer.)
    if (activePlan !== undefined) {
      envelopeBody.active_thread_ref = compactPlanPointer(activePlan);
    }
    await attachPendingAttention({
      vaultPath: config.vaultPath,
      subscriberId: config.agentId,
      planId: activePlan?.plan_id,
      envelopeBody,
      hostId: wire.id,
      hook: "promptSubmit",
    });
    if (!activePlanReadOk) {
      const existing =
        envelopeBody.degraded !== undefined &&
        typeof envelopeBody.degraded === "object" &&
        !Array.isArray(envelopeBody.degraded)
          ? (envelopeBody.degraded as Record<string, unknown>)
          : {};
      const sections = Array.isArray(existing.sections)
        ? existing.sections.filter((section): section is string => typeof section === "string")
        : [];
      if (!sections.includes("active_thread")) {
        sections.push("active_thread");
      }
      envelopeBody.degraded = {
        ...existing,
        sections,
        ...(typeof existing.note === "string"
          ? {}
          : {
              note: "Active thread was not read (filesystem or lock). Treat it as unknown, not empty.",
            }),
      };
    }

    const envelope = wrapEnvelope({
      event: "UserPromptSubmit",
      agent: config.agentId,
      body: envelopeBody,
    });

    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_user_prompt_submit`,
      summary: prompt.slice(0, 120),
      details: {
        intent: intent.action,
        vault_matches: vaultResults.map((result) => result.relativePath),
        daemon_ok: recall.ok,
        daemon_timed_out: daemonTimedOut,
        recall_stale: stalePointer !== undefined,
        task_signature: signature,
        workspace: workspaceId,
        recall_strong: Boolean(strong),
        top_score: strong?.topScore,
        ...envelopeAuditDetails,
        // RAW id only — see the weak-path comment above.
        ...(rawSessionId ? { session_id: rawSessionId } : {}),
      },
    });

    if (use && strong && recallPointer !== undefined) {
      // S1a-2: the hit the pointer names, deliver first; and, when the guard
      // was armed, the keys of the hits it may list.
      use.api.capturePromptPointer(use, {
        emitted: envelope,
        pointer: recallPointer,
        strong,
        recall: recall.ok ? recall.data : undefined,
        vaultResults,
        threshold,
        taskSignature: signature,
        armed: recallStateFile !== undefined,
      });
    }
    exit(strong ? "pointer" : stalePointer !== undefined ? "stale_pointer" : "plan");
    return render(injectIntent("UserPromptSubmit", envelope));
  }

  // s6 PreToolUse recall guard (BACKSTOP). Same logic as the claude-code hook's
  // handlePreToolUse, against this agent's vault. The output is the
  // permissionDecision shape (deny-to-surface) or additionalContext, NOT an envelope.
  // `recordDeny` (S1a-2) is set only by dispatch; a direct call records nothing.
  async function handlePreToolUse(
    payload: Record<string, unknown>,
    options: { recordDeny?: boolean } = {},
  ): Promise<PreToolUseDecisionOutput> {
    const toolName = asString(payload.tool_name);
    if (!toolName) return preToolUseAllow();
    const toolInput =
      payload.tool_input && typeof payload.tool_input === "object"
        ? (payload.tool_input as Record<string, unknown>)
        : {};
    const guardSessionId = asString(payload.session_id) || asString(payload.sessionId);
    const guardAgentId = asString(payload.agent_id) || asString(payload.agentId);

    // S2b (§6.4): PreToolUse on Agent / Task stashes the task prompt for SubagentStart
    // and never surfaces or consumes the parent's recall guard arming.
    if (isSubagentSpawnTool(toolName)) {
      if (isFeatureEnabled(config.vaultPath, "MINNI_SUBAGENT_BRIEF")) {
        stashSubagentPending(config.vaultPath, payload, toolInput);
      }
      return preToolUseAllow();
    }

    const maybeSurfacePreToolUseFailureLesson = async (): Promise<PreToolUseDecisionOutput> => {
      if (!wireCapabilities(wire).preToolUseContext) return preToolUseAllow();
      const lesson = matchPreToolUseFailureLesson({
        vaultPath: config.vaultPath,
        toolName,
        toolInput,
        sessionId: guardSessionId || undefined,
        cwd: asString(payload.cwd) || undefined,
      });
      if (!lesson) return preToolUseAllow();
      markFailureLessonFired(config.vaultPath, guardSessionId || undefined, lesson.lessonId);
      if (options.recordDeny) {
        const opened = await openLedger(PRE_TOOL_USE_EVENT, payload);
        opened?.ledger?.api.captureFailureLesson(opened.ledger.capture, {
          mkey: lesson.mkey,
          handles: [lesson.handle],
          title: lesson.title,
          lessonText: lesson.lessonText,
          emitted: lesson.additionalContext,
        });
        await opened?.settle();
      }
      return {
        continue: true,
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          additionalContext: lesson.additionalContext,
        },
      };
    };

    const mode = config.recallGuardMode ?? resolveRecallGuardModeFromEnv();
    if (mode === "off") return maybeSurfacePreToolUseFailureLesson();
    // s6 deny is a backstop for an envelope the model actually received.
    // Dropped UPS (Grok, Cursor, unprofiled) never delivers that envelope;
    // leftover consumed=false must not deny cold tools.
    if (!canInject(wire, "UserPromptSubmit")) return preToolUseAllow();

    const guardV2 = isFeatureEnabled(config.vaultPath, "MINNI_GUARD_V2");
    // S2b (§6.3.6): a PreToolUse carrying agent_id is inside a subagent; it
    // never surfaces parent recall and never consumes parent arming.
    if (guardV2 && guardAgentId) return maybeSurfacePreToolUseFailureLesson();

    const recallStateOpts = { sessionId: guardSessionId || undefined, guardV2 };

    // S2b (§6.3.5): consult tools (minni_recall, minni_drill) consume the
    // armed state and claim the arming so a subsequent Read/Grep in the same
    // turn does not fire the guard after the model already consulted memory.
    if (guardV2 && isConsultTool(toolName)) {
      const consultState = await readRecallState(config.vaultPath, recallStateOpts).catch(
        () => null,
      );
      if (consultState && !consultState.consumed) {
        await claimArming(
          config.vaultPath,
          guardSessionId || consultState.session_id,
          armingKeyFor(consultState),
        ).catch(() => false);
        await markRecallConsumed(config.vaultPath, recallStateOpts).catch(() => false);
      }
      return preToolUseAllow();
    }

    const state = await readRecallState(config.vaultPath, recallStateOpts).catch(() => null);
    const threshold = recallPointerThreshold();
    const verdict = decideGuard({
      state,
      mode,
      threshold,
      toolName,
      toolInput,
      guardV2,
      agentId: guardAgentId || undefined,
    });
    if (verdict === "allow") return maybeSurfacePreToolUseFailureLesson();

    // S2b (§6.3.3): atomic claim across sibling PreToolUse processes in a
    // parallel batch. Only the single winner surfaces; losers return allow.
    if (guardV2) {
      const claimed = await claimArming(
        config.vaultPath,
        guardSessionId || state!.session_id,
        armingKeyFor(state!),
      ).catch(() => false);
      if (!claimed) return preToolUseAllow();
    }

    // DENY/CONTEXT surfaces the recall ONCE: flip consumed=true FIRST so the re-issued
    // call (and every other tool call this turn) passes. PR90-2: only surface if
    // that flag actually persisted — if the write failed, denying would loop the
    // WHOLE turn (every re-issued call re-reads consumed=false and is denied
    // again). On a persistence failure we FAIL OPEN and allow, trading a missed
    // nudge for availability.
    const consumed = await markRecallConsumed(config.vaultPath, recallStateOpts).catch(() => false);
    if (guardV2) {
      deferUntilDelivered(
        async () => {
          pruneOldClaimFiles(config.vaultPath);
        },
        { recordOnly: true },
      );
    }
    // The PreToolUse payload may carry a session_id; stamp it so the Stop
    // receipt can attribute this guard nudge. Only add it when actually present
    // — never invent a "session" placeholder on this path.
    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_pretooluse_guard`,
      summary: `recall guard ${consumed ? "denied" : "allowed (consume write failed)"} ${toolName} (mode=${mode})`,
      details: {
        tool: toolName,
        mode,
        consumed,
        top_score: state!.top_score,
        hits: state!.top_hits.length,
        task_signature: state!.task_signature,
        ...(guardSessionId ? { session_id: guardSessionId } : {}),
      },
    }).catch(() => {});
    if (!consumed) return preToolUseAllow();
    const deliveryOverride = (process.env.MINNI_RECALL_GUARD_DELIVERY ?? "").trim().toLowerCase();
    const useContextDelivery =
      guardV2 && deliveryOverride !== "deny" && wireCapabilities(wire).preToolUseContext;
    const surfacing = useContextDelivery ? preToolUseContext(state!) : preToolUseDeny(state!);
    if (options.recordDeny) {
      // S1a-2: only a guard surfacing actually returned is recorded, one row per
      // listed hit; the capture loads here, so an allowed call never pays for it.
      const opened = await openLedger(PRE_TOOL_USE_EVENT, payload);
      opened?.ledger?.api.captureGuardDeny(
        opened.ledger.capture,
        state!,
        surfacing.hookSpecificOutput?.additionalContext ??
          surfacing.hookSpecificOutput?.permissionDecisionReason ??
          "",
      );
      await opened?.settle();
    }
    return surfacing;
  }

  async function handlePreCompact(payload: Record<string, unknown>): Promise<HookOutput> {
    await ensureVault(config.vaultPath, { agentId: config.agentId });
    const tail = await auditTail(config.vaultPath, 60);
    const scarTissue = extractScarTissue(tail.entries);
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    const workspaceId = workspaceFor(payload);
    const transcript = asString(payload.trigger) || asString(payload.summary);
    await clearSessionSurfacedOnReset(config.vaultPath, sessionId).catch(() => {});
    clearSessionFailureLessonsOnReset(config.vaultPath, sessionId);

    // hooks-PL-3: compaction is exactly when a correction the agent already
    // saw can fall out of context. Stash the current stale-belief /
    // contradiction events durably in the inbox so the post-compaction boot
    // re-asserts them (corrections_reassert) even if the daemon is down at
    // next boot.
    const { ok: staleBeliefsOk, events: staleBeliefEvents } =
      await fetchStaleBeliefEvents(config.agentId);

    const transcriptPath =
      asString(payload.transcript_path) || asString(payload.transcriptPath);
    let durableLearningCommitted = false;
    if (transcriptPath) {
      const jevResult = await harvestTranscriptDelta(
        {
          vaultPath: config.vaultPath,
          workspaceId,
          agentId: config.agentId,
          auditPrefix: config.auditPrefix,
          extractor: config.jevExtractor,
        },
        {
          transcriptPath,
          sessionId,
          event: "PreCompact",
          sessionCrons: payload.session_crons ?? payload.sessionCrons,
        },
      ).catch(() => undefined);
      if (jevResult?.harvested && jevResult.learnings.length > 0) {
        durableLearningCommitted = true;
      }
    }

    // Agents WITH a precompactKind persist a durable inbox handoff (which
    // carries the stale-belief stash); agents without one (kilocode) carry
    // the scar tissue in the envelope only and stash non-empty stale-belief
    // events as a dedicated precompact_reassert entry, like the CC hook.
    const inbox = config.precompactKind
      ? await writeInbox(config.vaultPath, sessionId, {
          kind: config.precompactKind,
          agent_id: config.agentId,
          workspace_id: workspaceId,
          scar_tissue: scarTissue,
          stale_belief_events: staleBeliefEvents,
          audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
          compaction_trigger: transcript || "compaction in progress",
          durable_learning_committed: durableLearningCommitted,
        })
      : undefined;
    const reassertInboxPath = config.precompactKind
      ? undefined
      : await stashPrecompactReassert({
          vaultPath: config.vaultPath,
          sessionId,
          agentId: config.agentId,
          staleBeliefEvents,
          trigger: transcript,
        });

    const envelope = wrapEnvelope({
      event: "PreCompact",
      agent: config.agentId,
      body: {
        identity: {
          agent: config.agentId,
          workspace: workspaceId,
          session_id: sessionId,
        },
        scar_tissue: scarTissue,
        audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
        compaction_trigger: transcript || "compaction in progress",
        ...(inbox
          ? { inbox_path: inbox.filePath, durable_learning_committed: durableLearningCommitted }
          : {}),
      },
    });

    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_pre_compact`,
      summary: `pre-compact ${sessionId}`,
      details: {
        scar_count: scarTissue.length,
        trigger: transcript || "auto",
        workspace: workspaceId,
        stale_belief_events: staleBeliefEvents.length,
        stale_beliefs_ok: staleBeliefsOk,
        ...(inbox ? { inbox_path: inbox.filePath } : {}),
        ...(reassertInboxPath ? { reassert_inbox_path: reassertInboxPath } : {}),
      },
    });

    // PreCompact can inject on NO platform: Claude Code omits it from the
    // hookSpecificOutput union, and Codex's schema is additionalProperties:false
    // so the envelope voids the entire output. The handoff written above is the
    // real payload; the wire records the drop rather than pretending it landed.
    return render(injectIntent("PreCompact", envelope));
  }

  // Post-compaction summary delivery (plan-<id>). Platforms whose
  // bridge can read the finished summary back (Kilo: session.compacted event +
  // SDK read-back in the native plugin) deliver it here; the hook stores the
  // RAW text as a compact_summary inbox file and the daemon's AFM-loop
  // compact_distillation pass reviews/organizes it into governance-gated
  // candidates. Injects nothing — there is no model turn to inject into.
  async function handleCompactSummary(payload: Record<string, unknown>): Promise<HookOutput> {
    await ensureVault(config.vaultPath, { agentId: config.agentId });
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    await clearSessionSurfacedOnReset(config.vaultPath, sessionId).catch(() => {});
    clearSessionFailureLessonsOnReset(config.vaultPath, sessionId);
    // Same provenance stamp as SessionStart harvest — dual delivery paths
    // must not disagree on `platform` when runtime and agentId differ.
    await harvestSummaryText(
      {
        vaultPath: config.vaultPath,
        workspaceId: workspaceFor(payload),
        auditPrefix: config.auditPrefix,
        platform: config.runtime ?? config.agentId,
      },
      {
        summaryText: asString(payload.summary_text),
        summaryId: asString(payload.summary_id),
        sessionId,
        timestamp: asString(payload.summary_timestamp) || undefined,
      },
    );
    return { continue: true };
  }

  // #283: primary compaction-summary delivery for platforms whose native
  // hook system fires a dedicated post-compaction event with the finished
  // summary attached directly (Claude Code's `PostCompact`, which hands it
  // over as `compact_summary` — documented 2026-07-29). Distinct from
  // `handleCompactSummary` above (Kilo's bridge-specific `CompactSummary`
  // event, `summary_text`/`summary_id` fields) because the two are genuinely
  // different wire-level triggers with different payload shapes that both
  // funnel into the same `harvestSummaryText` sink. The SessionStart
  // tail-read backstop above stays as the fallback for what this can miss
  // (hook not yet registered when compaction ran, older CLI, crash between
  // compaction and hook); the shared content-hash dedup keeps the two paths
  // from double-harvesting. Injects nothing — there is no model turn to
  // inject into. Raw storage only; the daemon's compact_distillation pass
  // does the AFM review on its own timer.
  async function handlePostCompact(payload: Record<string, unknown>): Promise<HookOutput> {
    await ensureVault(config.vaultPath, { agentId: config.agentId });
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    await clearSessionSurfacedOnReset(config.vaultPath, sessionId).catch(() => {});
    clearSessionFailureLessonsOnReset(config.vaultPath, sessionId);
    const summary = asString(payload.compact_summary);
    if (summary) {
      await harvestSummaryText(
        {
          vaultPath: config.vaultPath,
          workspaceId: workspaceFor(payload),
          auditPrefix: config.auditPrefix,
          platform: config.runtime ?? config.agentId,
        },
        {
          summaryText: summary,
          sessionId,
        },
      );
    } else {
      const transcriptPath = asString(payload.transcript_path) || asString(payload.transcriptPath);
      if (transcriptPath) {
        await harvestCompactSummary(
          {
            vaultPath: config.vaultPath,
            workspaceId: workspaceFor(payload),
            auditPrefix: config.auditPrefix,
            platform: config.runtime ?? config.agentId,
          },
          {
            transcriptPath,
            sessionId,
          },
        );
      }
    }
    return { continue: true };
  }

  // Stop lives in the shared stopCore / handleStopCore (above) so the
  // governance posture has exactly one implementation across all five
  // entrypoints — see handleStopCore's doc comment for why hook.ts routes here
  // too.
  //
  // Issue #253: after the stop breadcrumb / candidate write, deliver any
  // corrections SessionStart parked under inbox/.undeliverable (wires that
  // cannot inject at boot). Ordering is intentional:
  //   1. the shared Stop core first — inbox write / receipt is independent of
  //      correction delivery and must not be gated on the park dir;
  //   2. then inject corrections_reassert at Stop when the wire can carry it;
  //   3. (S1b) the use detector's commit last, after everything above has
  //      registered its own.
  // "Re-assert at end of turn" is the honest semantics on a platform whose
  // only injectable event is Stop: the model sees the correction before the
  // next turn begins, not at session boot. Prefer acting on them next turn.
  async function handleStop(payload: Record<string, unknown>): Promise<HookOutput> {
    const core = await stopCore(config, payload, prepareOutcomeFn);
    const output = await stopOutput(payload, core.output);
    // S1b: the ledger's commits are registered LAST (the detector, then the DF
    // recount slice). Deferred commits run in registration order and the
    // detector may take its full 1.5 s cap, so everything Stop consumes or
    // confirms (a parked correction's archive, a relay cursor) settles first;
    // the ledger's work only records.
    if (core.measured) scheduleStopLedgerWork(config, payload);
    return output;
  }

  async function stopOutput(payload: Record<string, unknown>, result: HookOutput): Promise<HookOutput> {

    let correctionsEnvelope = "";
    // Only attempt when Stop can actually inject — parking only happens when
    // SessionStart cannot, and today that is Grok Build; probing canInject
    // keeps the route honest if another wire gains a structural SessionStart
    // drop later.
    if (canInject(wire, "Stop")) {
      // Unbudgeted local FS: same as SessionStart reassert. Absence loses data.
      const parkedPending = await readParkedUndeliverablePending(config.vaultPath, 3);
      if (parkedPending.length > 0) {
        const {
          events: parkedEvents,
          consumedPaths: parkedConsumed,
          contributingPaths: parkedContributing,
          deferredTails: parkedDeferred,
        } = collectCorrectionsReassert(parkedPending);

        // Empty parked stashes (should not be parked today — SessionStart
        // settles empties eagerly) still clear without waiting on delivery.
        const parkedEmpty = parkedConsumed.filter(
          (filePath) => !parkedContributing.includes(filePath),
        );
        if (parkedEmpty.length > 0) {
          await settleReassertedInboxEntries(config.vaultPath, {
            consumedPaths: parkedEmpty,
            deferredTails: [],
          });
        }

        if (parkedEvents.length > 0) {
          correctionsEnvelope = wrapEnvelope({
            event: "Stop",
            agent: config.agentId,
            budget: envelopeBudgetFor(config.contextWindow),
            body: {
              corrections_reassert: parkedEvents,
              // Named so operators / the model can tell this is not a boot
              // reassert: delivery was deferred from a structurally silent
              // SessionStart to the first injectable event.
              delivery: "stop_routed",
              note:
                "Corrections parked at SessionStart (this platform cannot inject there) are re-asserted at Stop — act on them before the next turn.",
            },
          });

          // Deliver-before-consume: archive only after emitAndCommit confirms
          // the inject reached the host. A failed Stop leaves the park intact
          // for the next end_turn (at-least-once).
          if (parkedContributing.length > 0 || parkedDeferred.length > 0) {
            deferUntilDelivered(() =>
              settleReassertedInboxEntries(config.vaultPath, {
                consumedPaths: parkedContributing,
                deferredTails: parkedDeferred,
              }),
            );
          }

          await recordAudit(config.vaultPath, {
            tool: `${config.auditPrefix}_stop_corrections_reassert`,
            summary: `Stop: delivering ${parkedEvents.length} SessionStart-parked correction(s) (issue #253)`,
            details: {
              wire: wire.id,
              corrections_reassert: parkedEvents.length,
              reassert_entries_pending_clear: parkedContributing.length,
              reassert_tails_pending_defer: parkedDeferred.length,
              delivery: "stop_routed",
            },
          }).catch(() => {});
        }
      }
    }

    let pendingStopText = "";
    try {
      const planId = await activePlanIdFromVault(config.vaultPath);
      const pending = await pendingAttentionForHook({
        vaultPath: config.vaultPath,
        subscriberId: config.agentId,
        planId,
      });
      if (pending && planId) {
        pendingStopText = pending.text;
        const lastSeq = pending.notifications[pending.notifications.length - 1]?.seq;
        if (lastSeq !== undefined) {
          confirmPendingIfInjects({
            vaultPath: config.vaultPath,
            subscriberId: config.agentId,
            planId,
            lastSeq,
            hostId: wire.id,
            hook: "stop",
          });
        }
      }
    } catch {
      // Relay is not SoT.
    }

    if (correctionsEnvelope || pendingStopText) {
      // Inject is the model-facing channel; append any stop note (receipt /
      // candidates CTA) so both land in one Stop output. Prefer inject over
      // note alone — on Grok both channels work at Stop, but inject is what
      // carries structured memory. Pending attention rides the same inject.
      const text = [correctionsEnvelope, pendingStopText, result.systemMessage]
        .filter((part): part is string => Boolean(part && part.trim()))
        .join("\n\n");
      return render(injectIntent("Stop", text));
    }
    if (result.systemMessage) {
      return render(noteIntent("Stop", result.systemMessage));
    }
    return render(noIntent);
  }

  // S2b (§6.4): SubagentStart brief injection (<= 600 chars) combining parent
  // turn's RecallState (up to 3 items) and bounded recall on the claimed
  // subagent task prompt (up to 2 gated items).
  async function handleSubagentStart(
    payload: Record<string, unknown>,
    ledger?: LedgerCapture,
  ): Promise<SubagentHookOutput> {
    const briefOn = ledger
      ? ledger.capture.context.features.includes("MINNI_SUBAGENT_BRIEF")
      : isFeatureEnabled(config.vaultPath, "MINNI_SUBAGENT_BRIEF");
    if (!briefOn) return { continue: true };

    await ensureVault(config.vaultPath, { agentId: config.agentId });
    const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
    const guardV2 = ledger
      ? ledger.capture.context.features.includes("MINNI_GUARD_V2")
      : isFeatureEnabled(config.vaultPath, "MINNI_GUARD_V2");

    const parentState = await readRecallState(config.vaultPath, {
      sessionId: rawSessionId || undefined,
      guardV2,
    }).catch(() => null);
    const parentHits = Array.isArray(parentState?.top_hits)
      ? parentState!.top_hits.slice(0, 3)
      : [];

    const claimedPending = claimSubagentPending(config.vaultPath, payload);
    const threshold = recallPointerThreshold();
    let taskStrong: ReturnType<typeof extractStrongRecall> = null;
    let taskRecallData: Awaited<ReturnType<typeof recallMemory>>["data"] | undefined;
    let safeVaultResults: ReturnType<typeof filterSafeVaultResults> = [];

    if (claimedPending && claimedPending.prompt.trim()) {
      const extraPaths = Array.isArray(claimedPending.targetPaths)
        ? claimedPending.targetPaths.filter((p) => !claimedPending.prompt.includes(p))
        : [];
      const taskPrompt =
        extraPaths.length > 0
          ? `${claimedPending.prompt.trim()} ${extraPaths.join(" ")}`
          : claimedPending.prompt.trim();
      const workspaceId = workspaceFor(payload);
      const [vaultRaw, recall] = await Promise.all([
        withBudget(
          searchVaultNotes(config.vaultPath, taskPrompt, 12, config.agentId, {
            candidates: "scan",
          }),
          SUBAGENT_RECALL_BUDGET_MS,
          [],
        ),
        withBudget(
          recallMemory({
            query: taskPrompt,
            limit: 4,
            agentId: config.agentId,
            workspaceId,
            timeoutMs: SUBAGENT_RECALL_BUDGET_MS,
            ...(rawSessionId ? { sessionId: rawSessionId } : {}),
          }),
          SUBAGENT_RECALL_BUDGET_MS,
          { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR },
        ),
      ]);
      safeVaultResults = filterSafeVaultResults(vaultRaw).slice(0, 4);
      taskRecallData = recall.ok ? recall.data : undefined;
      taskStrong = extractStrongRecall(taskRecallData, safeVaultResults, threshold, 2);
    }

    const taskHits = taskStrong
      ? enrichTaskHitsWithSnippets(taskStrong.topHits.slice(0, 2), taskRecallData, safeVaultResults, threshold)
      : [];
    const brief = buildSubagentBrief(parentHits, taskHits, SUBAGENT_BRIEF_MAX_CHARS);
    if (!brief) {
      return { continue: true };
    }

    ledger?.api.captureSubagentBrief(ledger.capture, {
      emitted: brief,
      parentState,
      taskStrong,
      taskRecall: taskRecallData,
      taskVaultResults: safeVaultResults,
      threshold,
    });

    return {
      continue: true,
      hookSpecificOutput: {
        hookEventName: "SubagentStart",
        additionalContext: brief,
      },
    };
  }

  // S2b (§6.4): SubagentStop passive detector run against the subagent's own
  // transcript (`agent_transcript_path`) with `sessionEnd: true`.
  async function handleSubagentStop(
    payload: Record<string, unknown>,
  ): Promise<SubagentHookOutput> {
    const hasSessionId = Boolean(asString(payload.session_id) || asString(payload.sessionId));
    const measured =
      hasSessionId &&
      wireCapabilities(wire).transcriptShape === DETECTOR_TRANSCRIPT_SHAPE &&
      ledgerMayBeOn(config.vaultPath);
    if (measured) {
      scheduleSubagentStopUseDetector(config, payload);
    }
    return { continue: true };
  }

  // Phase 1b (S4c–S4d, §7): PostToolUse and PostToolUseFailure failure-lesson
  // surfacing. Matches `tool_response` output text on every Bash call even when
  // `is_error === false` (solving exit-code masking `| tail`, `echo exit=$?`).
  async function handlePostToolUse(
    event: "PostToolUse" | "PostToolUseFailure",
    payload: Record<string, unknown>,
    options: { recordLedger?: boolean } = {},
  ): Promise<PostToolUseDecisionOutput> {
    if (!wireCapabilities(wire).preToolUseContext) {
      return { continue: true };
    }
    const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
    const lesson = matchPostToolUseFailureLesson({
      vaultPath: config.vaultPath,
      event,
      payload,
      sessionId: rawSessionId || undefined,
      cwd: asString(payload.cwd) || undefined,
    });
    if (!lesson) {
      return { continue: true };
    }
    markFailureLessonFired(config.vaultPath, rawSessionId || undefined, lesson.lessonId);
    if (options.recordLedger) {
      const opened = await openLedger(event, payload);
      opened?.ledger?.api.captureFailureLesson(opened.ledger.capture, {
        mkey: lesson.mkey,
        handles: [lesson.handle],
        title: lesson.title,
        lessonText: lesson.lessonText,
        emitted: lesson.additionalContext,
      });
      await opened?.settle();
    }
    return {
      continue: true,
      hookSpecificOutput: {
        hookEventName: event,
        additionalContext: lesson.additionalContext,
      },
    };
  }

  // S1a-2: the use-ledger capture is opened here, once per event (a hook
  // process handles one), so a direct handler call never records. One cheap
  // check decides whether the capture modules load at all; when they do, the
  // flags are read once through flags.ts, and a note about them is audited
  // after the handler has built its output, so it cannot reach this event's
  // audit_tail.
  //
  // PreToolUse is the exception: it fires on every tool call and records only
  // a deny, which is rare, so it opens its capture inside handlePreToolUse, for
  // a deny it is about to return. The allow path pays nothing.
  async function dispatch(
    event: string,
    payload: Record<string, unknown>,
  ): Promise<HookOutput | PreToolUseDecisionOutput | SubagentHookOutput | PostToolUseDecisionOutput> {
    const opened =
      event !== PRE_TOOL_USE_EVENT && !POST_TOOL_USE_EVENTS.has(event) && LEDGER_EVENTS.has(event)
        ? await openLedger(event, payload)
        : null;
    try {
      return await route(event, payload, opened?.ledger ?? null);
    } finally {
      await opened?.settle();
    }
  }

  /**
   * The use-ledger capture for one event: null when the cheap check says the
   * ledger is off (nothing loads). `ledger` is null when the capture module
   * cannot be loaded or flags.ts says off after all; the hook then runs exactly
   * as with the ledger off. `settle` audits what must not reach this event's
   * own output (a flag note, an unloadable capture module), so call it once the
   * handler has built its output. Never rejects.
   */
  async function openLedger(event: string, payload: Record<string, unknown>): Promise<OpenedLedger | null> {
    if (!ledgerMayBeOn(config.vaultPath)) return null;
    const api = await loadCaptureApi();
    if (api === null) {
      return {
        ledger: null,
        settle: () =>
          recordAudit(config.vaultPath, {
            tool: `${config.auditPrefix}_use_capture_unavailable`,
            // A code, never the loader's message: that names an install path.
            summary: `${event}: the use-ledger capture module could not be loaded; nothing was recorded`,
            throttleKey: `${config.auditPrefix}_use_capture_unavailable`,
          }).then(
            () => undefined,
            () => undefined,
          ),
      };
    }
    const session = api.openLedgerSession({
      vaultPath: config.vaultPath,
      event,
      payload,
      wire,
      principal: config.agentId,
    });
    return {
      ledger: session.capture ? { api, capture: session.capture } : null,
      settle: () => api.auditFlagNotes(config.vaultPath, config.auditPrefix, session.flags),
    };
  }

  async function route(
    event: string,
    payload: Record<string, unknown>,
    ledger: LedgerCapture | null,
  ): Promise<HookOutput | PreToolUseDecisionOutput | SubagentHookOutput | PostToolUseDecisionOutput> {
    switch (event) {
      case "SessionStart":
        if (ledger) {
          ledger.api.recordReset(ledger.capture, event, payload);
          // Retention, after delivery and bounded (§6.2.5).
          ledger.api.scheduleSessionSweep(ledger.capture);
        }
        return handleSessionStart(payload, ledger ?? undefined);
      case "UserPromptSubmit":
        // Every exit is counted: one `turn` row per prompt, written when the
        // handler returns or throws (§4.3).
        if (!ledger) return handleUserPromptSubmit(payload);
        return ledger.api.withTurnRecord(ledger.capture, payload, (turn) =>
          handleUserPromptSubmit(payload, { ...ledger, turn }),
        );
      case PRE_TOOL_USE_EVENT:
        return handlePreToolUse(payload, { recordDeny: true });
      case "PostToolUse":
        return handlePostToolUse("PostToolUse", payload, { recordLedger: true });
      case "PostToolUseFailure":
        return handlePostToolUse("PostToolUseFailure", payload, { recordLedger: true });
      case "PreCompact":
        ledger?.api.recordReset(ledger.capture, event, payload);
        return handlePreCompact(payload);
      // #283 / #227: primary compaction-summary delivery for platforms
      // whose own hook system fires a dedicated post-compaction event
      // (Claude Code in hooks.json and Codex in hooks-codex.json). Harmless
      // to route unconditionally — platforms without PostCompact in their
      // manifest simply never fire this case.
      case "PostCompact":
        ledger?.api.recordReset(ledger.capture, event, payload);
        return handlePostCompact(payload);
      case "CompactSummary":
        return handleCompactSummary(payload);
      case "Stop":
        return handleStop(payload);
      case "SubagentStart":
        return handleSubagentStart(payload, ledger ?? undefined);
      case "SubagentStop":
        return handleSubagentStop(payload);
      default:
        // P4 (#228): `render(noIntent)` reports a clean, successful no-op —
        // which is honest for "ran fine, nothing to say" but a lie for "this
        // event passed the VALID_EVENTS gate and then found no handler".
        // Record it, then continue as before.
        await recordUnroutedEvent(config.vaultPath, config.auditPrefix, event);
        return render(noIntent);
    }
  }

  return {
    handleSessionStart,
    handleUserPromptSubmit,
    handlePreCompact,
    handlePostCompact,
    handleCompactSummary,
    handleStop,
    handlePreToolUse,
    handlePostToolUse,
    handleSubagentStart,
    handleSubagentStop,
    dispatch,
  };
}

/**
 * P4 (#228): record an event that reached the hook and was not routed.
 *
 * Shares the `_intent_dropped` tool name with the wire-drop path in `render`
 * so every "the hook was reached and nothing was carried" case is countable
 * from one place, and buckets by event for the same reason that one does — a
 * throttle keyed on the bare tool name would collapse two different events'
 * drops into one record and report the second as written.
 *
 * Best-effort: losing the audit record must never also lose the event.
 */
export async function recordUnroutedEvent(
  vaultPath: string,
  auditPrefix: string,
  event: string,
): Promise<void> {
  try {
    await recordAudit(vaultPath, {
      tool: `${auditPrefix}_intent_dropped`,
      summary: `${event || "(empty)"}: event not routed by this hook (no handler)`,
      throttleKey: `${auditPrefix}_intent_dropped__unrouted__${event}`,
    });
  } catch {
    // Audit unavailable. The drop is still correct behavior.
  }
}

/**
 * P6 (#228): a native bridge (Kilo) has no vault handle of its own — it
 * reaches the vault only by spawning this hook. This is that channel: it
 * carries a bridge failure into the audit log so a persistently failing
 * bridge is distinguishable from an idle one. It is a diagnostic, not an
 * envelope event, and never reaches a handler.
 *
 * Round 7 (PR #260): the EXIT CODE is the delivery signal. The bridge
 * restores its coalesced eviction counts only when this child errors or
 * exits non-zero — so an audit that did not land must not exit clean, or
 * the parent clears counts that were never written anywhere durable.
 */
async function runBridgeFailureDiagnostic(
  config: AgentHookConfig,
  payload: Record<string, unknown>,
): Promise<never> {
  const failedEvent = asString(payload.failed_event) || "(unknown)";
  const bridge = asString(payload.bridge) || "(unknown)";
  // Round 9 (PR #260): storm suppress carries counts forward and flushes on
  // settle — name the dark interval so the audit row is not a silent single.
  const suppressed = payload.suppressed_since_last_report;
  const coalesced = payload.coalesced_count;
  const extras: string[] = [];
  if (typeof coalesced === "number" && coalesced > 1) {
    extras.push(`coalesced=${coalesced}`);
  }
  if (typeof suppressed === "number" && suppressed > 0) {
    extras.push(`suppressed_since_last_report=${suppressed}`);
  }
  const suffix = extras.length ? ` [${extras.join(", ")}]` : "";
  try {
    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_bridge_failure`,
      summary: `${bridge} bridge: ${failedEvent} failed: ${asString(payload.error)}${suffix}`,
      // Bucket per failed event, for the same reason _intent_dropped does:
      // one throttle across all of them would hide every failure after the
      // first and report the bridge as healthier than it is.
      throttleKey: `${config.auditPrefix}_bridge_failure__${failedEvent}`,
    });
  } catch {
    // Round 26: delivery is the EXIT CODE. Parent treats close(null)/non-zero
    // as undelivered. Setting exitCode and returning left the event loop
    // alive until SIGKILL → close(null) after a successful audit, reopening
    // P6 as "never delivered" after a durable write. Force exit.
    try {
      emit({ continue: true });
    } catch {
      /* best-effort envelope */
    }
    process.exit(1);
  }
  emit({ continue: true });
  // Same "delivery is exit" contract as every other settled hook path.
  exitAfterDelivery();
}

export async function runHookMain(config: AgentHookConfig): Promise<void> {
  // Round 7/26 (PR #260): the bridge diagnostic is routed BEFORE the
  // hooksEnabled gate — disabling memory hooks must not also disable the one
  // surface that says the bridge is broken. The early return here used to
  // exit 0 first, which the parent read as "audit delivered".
  //
  // Argv form is the Kilo spawn contract. Payload-only form
  // (hook_event_name: BridgeFailure, no argv) is the same diagnostic
  // channel and must also bypass hooksEnabled — read stdin once, then route
  // both before any hooks-disabled early return.
  const eventArg = (process.argv[2] ?? "").trim();
  if (eventArg === BRIDGE_FAILURE_EVENT) {
    const payload = (await readStdin()) as Record<string, unknown>;
    await runBridgeFailureDiagnostic(config, payload);
  }

  const payload = (await readStdin()) as Record<string, unknown>;
  const eventFromPayload = asString(payload.hook_event_name);
  if ((eventFromPayload || "").trim() === BRIDGE_FAILURE_EVENT) {
    await runBridgeFailureDiagnostic(config, payload);
  }

  if (!config.hooksEnabled) {
    emit({ continue: true });
    return;
  }

  const event = (eventArg || eventFromPayload || "").trim();
  // PreToolUse is dispatched here too but is NOT an EnvelopeEvent (its output is
  // the permissionDecision shape), so it is gated alongside VALID_EVENTS.
  if (event !== PRE_TOOL_USE_EVENT && !SUBAGENT_EVENTS.has(event) && !POST_TOOL_USE_EVENTS.has(event) && !VALID_EVENTS.includes(event as EnvelopeEvent)) {
    // P4 (#228): this exited with a SUCCESS status and no marker of any kind,
    // so an event the platform declared and sent but Minni does not route was
    // indistinguishable from an event that was never sent at all. That is the
    // whole clean-continue swallow: the manifest says the event is wired, the
    // exit code says it went fine, and nothing anywhere records that it was
    // dropped on the floor. Count it before continuing.
    await recordUnroutedEvent(config.vaultPath, config.auditPrefix, event);
    emit({ continue: true });
    return;
  }

  const wire = config.wire ?? wireFor(config.agentId);
  if (wire.shouldHandle && !wire.shouldHandle(event, payload)) {
    // A duplicate firing of an event the platform emits more than once per
    // logical occurrence -- running it again would double-count the outcome.
    emit(wire.noop());
    return;
  }

  try {
    const handlers = createHookHandlers(config);
    const output = await handlers.dispatch(event, payload);
    // emitAndCommit, not emit: work the handler deferred (archiving a consumed
    // correction) runs only once this output has actually reached the host.
    await emitAndCommit(output, {
      vaultPath: config.vaultPath,
      auditPrefix: config.auditPrefix,
      event,
    });
    // S1a-2: the deferred ledger rows have been written or abandoned by now.
    await auditLedgerFailures(config);
    // Delivery is settled; nothing left to wait for. See exitAfterDelivery.
    exitAfterDelivery();
  } catch (error) {
    // Nothing was delivered, so nothing the handler deferred may be committed.
    discardDeliveryCommits();
    const message = error instanceof Error ? error.message : String(error);
    try {
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_error`,
        summary: `${event}: ${message}`,
      });
    } catch {
      // audit unavailable; the systemMessage below still surfaces the failure
    }
    await auditLedgerFailures(config);
    // hooks-PL-5: a degraded boot must never look like a clean one — say so.
    // failAndExit, not emit: the degraded signal has to be FLUSHED before an
    // abandoned RPC handle can keep this process alive into the harness kill.
    await failAndExit({
      continue: true,
      systemMessage: `Minni hook degraded (${event}): ${message} — memory injection skipped this event; see vault log.md.`,
    });
  }
}

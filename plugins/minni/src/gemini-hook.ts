// Gemini/Antigravity (agy CLI) hook entry point (#133). Handler logic lives
// in the shared createHookHandlers factory (hook-handlers.ts); this file
// supplies the gemini-specific constants and — unlike codex/grok/kilocode,
// which speak Claude Code's hook protocol natively — wraps dispatch in the
// agy payload/output adapters (gemini-adapter.ts).
//
// It cannot reuse runHookMain: PreToolUse requires an explicit decision, so
// EVERY exit path — hooks disabled, unknown event, handler error — must emit
// {"decision":"allow"} instead of runHookMain's bare
// {continue:true}. See gemini-adapter.ts for the verified protocol notes.
//
// agy 1.1.1 dispatches native PreToolUse/Stop and its compatibility loader
// dispatches SessionStart. It does not dispatch UserPromptSubmit/PreCompact,
// so per-turn recall state and compaction hooks remain an honest host gap.
import type { EnvelopeEvent } from "./agent_envelope.js";
import {
  GEMINI_AGENT_ID,
  GEMINI_CONTEXT_WINDOW,
  GEMINI_HOOKS_ENABLED,
  GEMINI_VAULT_PATH,
  GEMINI_WORKSPACE_ID,
} from "./config.js";
import {
  adaptAgyPayload,
  adaptPreToolUseOutput,
  agyApprove,
  enrichAgyStopPayload,
} from "./gemini-adapter.js";
import { createHookHandlers } from "./hook-handlers.js";
import type { AgentHookConfig } from "./hook-handlers.js";
import { VALID_EVENTS, asString, emit, readStdin } from "./hook-utils.js";
import { PRE_TOOL_USE_EVENT } from "./recall-guard.js";
import type { PreToolUseDecisionOutput, RecallGuardMode } from "./recall-guard.js";
import { recordAudit } from "./vault.js";

// Codex review (PR #134): the shared guard's default "soft" mode deliberately
// ignores Bash — but on agy EVERY shell/search call is run_command, which the
// adapter maps to Bash, so soft mode would guard nothing on this surface.
// Default to "strict" (read/search commands only; mutations always pass) while
// still honoring an explicit MINNI_RECALL_GUARD_MODE override.
const GEMINI_GUARD_MODE: RecallGuardMode = (() => {
  const raw = (process.env.MINNI_RECALL_GUARD_MODE ?? "").trim().toLowerCase();
  if (raw === "off" || raw === "soft" || raw === "strict") return raw;
  return "strict";
})();

const CONFIG: AgentHookConfig = {
  agentId: GEMINI_AGENT_ID,
  vaultPath: GEMINI_VAULT_PATH,
  defaultWorkspaceId: GEMINI_WORKSPACE_ID,
  contextWindow: GEMINI_CONTEXT_WINDOW,
  hooksEnabled: GEMINI_HOOKS_ENABLED,
  runtime: "gemini",
  hookScript: "gemini-hook.js",
  auditPrefix: "hook_gemini",
  // No precompactKind: like kilocode, PreCompact (if agy ever dispatches it)
  // stashes stale-belief events as a precompact_reassert entry instead of a
  // durable handoff file.
  // Like grok/kilocode, an empty Stop outcome skips the inbox write entirely.
  alwaysWriteStopInbox: false,
  recallGuardMode: GEMINI_GUARD_MODE,
};

async function main(): Promise<void> {
  const eventArg = (process.argv[2] ?? "").trim();
  const isPreToolUse = eventArg === PRE_TOOL_USE_EVENT;
  const emitNoop = (): void => {
    emit(isPreToolUse ? agyApprove() : { continue: true });
  };

  if (!CONFIG.hooksEnabled) {
    emitNoop();
    return;
  }

  const raw = (await readStdin()) as Record<string, unknown>;
  const event = eventArg || asString(raw.hook_event_name).trim();
  if (event !== PRE_TOOL_USE_EVENT && !VALID_EVENTS.includes(event as EnvelopeEvent)) {
    emitNoop();
    return;
  }

  let payload = adaptAgyPayload(raw);
  if (event === "Stop") {
    // Best-effort: pull the real last user message from agy's transcript so
    // Stop drafts candidates about the actual task, not the conversation id.
    payload = await enrichAgyStopPayload(payload).catch(() => payload);
  }
  try {
    const handlers = createHookHandlers(CONFIG);
    const output = await handlers.dispatch(event, payload);
    if (event === PRE_TOOL_USE_EVENT) {
      emit(adaptPreToolUseOutput(output as PreToolUseDecisionOutput));
    } else {
      emit(output);
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    try {
      await recordAudit(CONFIG.vaultPath, {
        tool: `${CONFIG.auditPrefix}_error`,
        summary: `${event}: ${message}`,
      });
    } catch {
      // audit unavailable; the fallback output below still keeps agy unblocked
    }
    if (event === PRE_TOOL_USE_EVENT) {
      emit(agyApprove());
    } else {
      // hooks-PL-5: a degraded event must never look like a clean one — say so.
      emit({
        continue: true,
        systemMessage: `Minni hook degraded (${event}): ${message} — memory injection skipped this event; see vault log.md.`,
      });
    }
  }
}

void main();

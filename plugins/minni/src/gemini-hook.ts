// Gemini/Antigravity (agy CLI) hook entry point (#133). Handler logic lives
// in the shared createHookHandlers factory (hook-handlers.ts); this file
// supplies the gemini-specific constants and — unlike codex/grok/kilocode,
// which speak Claude Code's hook protocol natively — wraps dispatch in the
// agy payload/output adapters (gemini-adapter.ts).
//
// It cannot reuse runHookMain: agy errors on an empty PreToolUse decision, so
// EVERY exit path of a PreToolUse invocation — hooks disabled, unknown event,
// handler error — must emit an explicit {"decision":"allow"} instead of
// runHookMain's bare {continue:true}. See gemini-adapter.ts for the protocol.
//
// The manifest declares agy's OWN event names; AGY_EVENTS maps them onto
// Minni's internal ones. agy has no UserPromptSubmit — PreInvocation is the
// analogue and the documented injection point — and no PreCompact at all.
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
  agyAllow,
  enrichAgyPromptPayload,
  enrichAgyStopPayload,
} from "./gemini-adapter.js";
import { createHookHandlers } from "./hook-handlers.js";
import { geminiWire } from "./hook-platform.js";
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
  // Mirrors hooks/hooks-gemini.json PreInvocation "timeout": 10 — edit both.
  // This is the TIGHTEST prompt-time bound of any platform, so it, not the
  // MINNI_HOOK_BUDGET_MS default, is what sets gemini's effective budget (6s).
  promptHookTimeoutMs: 10_000,
  // No precompactKind: like kilocode, PreCompact (if agy ever dispatches it)
  // stashes stale-belief events as a precompact_reassert entry instead of a
  // durable handoff file.
  recallGuardMode: GEMINI_GUARD_MODE,
  wire: geminiWire,
};

/**
 * agy's native event names -> Minni's internal EnvelopeEvent names.
 *
 * Declaring Claude Code's names in an agy manifest is what made this
 * integration dead config: `UserPromptSubmit` and `PreCompact` simply do not
 * exist on agy, so those entries never fired and never would have.
 */
const AGY_EVENTS: Record<string, EnvelopeEvent> = {
  SessionStart: "SessionStart",
  PreInvocation: "UserPromptSubmit",
  Stop: "Stop",
};

async function main(): Promise<void> {
  const rawEventArg = (process.argv[2] ?? "").trim();
  const eventArg =
    rawEventArg === PRE_TOOL_USE_EVENT ? rawEventArg : (AGY_EVENTS[rawEventArg] ?? rawEventArg);
  const isPreToolUse = eventArg === PRE_TOOL_USE_EVENT;
  const emitNoop = (): void => {
    emit(isPreToolUse ? agyAllow() : { continue: true });
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
  if (event === "UserPromptSubmit") {
    // PreInvocation carries no prompt text; mine the transcript or the
    // shared handler returns noIntent and the per-turn recall pointer dies.
    payload = await enrichAgyPromptPayload(payload).catch(() => payload);
  }
  if (event === "Stop") {
    // Best-effort: pull the real last user message from agy's transcript so
    // the Stop breadcrumb (and any future summary-bearing payload) names the
    // actual task rather than the conversation id. Bare last_user_message is
    // NOT draftable outcome material under the retired Stop auto-draft posture
    // — only an explicit payload `summary` / `changedFiles` would draft.
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
      emit(agyAllow());
    } else {
      // hooks-PL-5: a degraded event must never look like a clean one — say
      // so, through agy's channel (it has no systemMessage).
      emit(
        geminiWire.note(
          event as EnvelopeEvent,
          `Minni hook degraded (${event}): ${message} — memory injection skipped this event; see vault log.md.`,
        ) ?? geminiWire.noop(),
      );
    }
  }
}

void main();

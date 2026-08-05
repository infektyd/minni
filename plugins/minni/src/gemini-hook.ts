// Gemini/Antigravity (agy CLI) hook entry point (#133). Handler logic lives
// in the shared createHookHandlers factory (hook-handlers.ts); this file
// supplies the gemini-specific constants and wraps dispatch in the agy
// payload/output adapters (gemini-adapter.ts).
//
// Codex and kilocode (via bridge) can use bare runHookMain with Claude-shaped
// PreToolUse. Grok Build does not — it has its own adapter (grok-adapter.ts /
// grok-hook.ts) for camelCase toolName/toolInput and {decision,reason} output.
// agy is a third non-Claude protocol: empty PreToolUse decisions error, so
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
  AGY_EVENTS,
  adaptAgyPayload,
  adaptPreToolUseOutput,
  agyAllow,
  enrichAgyPromptPayload,
  enrichAgyStopPayload,
} from "./gemini-adapter.js";
import { createHookHandlers, recordUnroutedEvent } from "./hook-handlers.js";
import { geminiWire } from "./hook-platform.js";
import type { AgentHookConfig } from "./hook-handlers.js";
import { VALID_EVENTS, asString, emit, readStdin } from "./hook-utils.js";
import {
  discardDeliveryCommits,
  emitAndCommit,
  exitAfterDelivery,
  failAndExit,
} from "./hook-delivery.js";
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
  // Mirrors hooks/hooks-gemini.json SessionStart "timeout": 10 — edit both.
  // agy kills BOOT at 10s too, the tightest SessionStart deadline of any
  // platform, so this is what bounds gemini's boot budget (6s).
  sessionStartHookTimeoutMs: 10_000,
  // No precompactKind: like kilocode, PreCompact (if agy ever dispatches it)
  // stashes stale-belief events as a precompact_reassert entry instead of a
  // durable handoff file.
  recallGuardMode: GEMINI_GUARD_MODE,
  // #296: SessionStart acks this agent's pending handoff leases at boot —
  // was claude-only before #296, an unstated inconsistency rather than a
  // deliberate choice. Every platform sharing this factory sets this
  // EXCEPT grok-build (see grok-hook.ts's CONFIG for why: its wire cannot
  // inject/note at SessionStart at all, so acking there would misreport
  // acceptance to the sender before the content is ever actually surfaced).
  ackPendingHandoffsAtBoot: true,
  wire: geminiWire,
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
    // P4 (#228): record the drop instead of exiting clean and silent.
    await recordUnroutedEvent(CONFIG.vaultPath, CONFIG.auditPrefix, event);
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
    // emitAndCommit, not emit: work the handler deferred (archiving a consumed
    // correction) runs only once this output has actually reached agy.
    await emitAndCommit(
      event === PRE_TOOL_USE_EVENT
        ? adaptPreToolUseOutput(output as PreToolUseDecisionOutput)
        : output,
      { vaultPath: CONFIG.vaultPath, auditPrefix: CONFIG.auditPrefix, event },
    );
    // Delivery is settled; nothing left to wait for. See exitAfterDelivery.
    exitAfterDelivery();
  } catch (error) {
    // Nothing was delivered, so nothing the handler deferred may be committed.
    discardDeliveryCommits();
    const message = error instanceof Error ? error.message : String(error);
    try {
      await recordAudit(CONFIG.vaultPath, {
        tool: `${CONFIG.auditPrefix}_error`,
        summary: `${event}: ${message}`,
      });
    } catch {
      // audit unavailable; the fallback output below still keeps agy unblocked
    }
    // failAndExit, not emit: agy kills at 10s, and a fire-and-forget write can
    // still be unbuffered when that lands — losing the degraded signal itself.
    await failAndExit(
      event === PRE_TOOL_USE_EVENT
        ? agyAllow()
        : // hooks-PL-5: a degraded event must never look like a clean one — say
          // so, through agy's channel (it has no systemMessage).
          (geminiWire.note(
            event as EnvelopeEvent,
            `Minni hook degraded (${event}): ${message} — memory injection skipped this event; see vault log.md.`,
          ) ?? geminiWire.noop()),
    );
  }
}

void main();

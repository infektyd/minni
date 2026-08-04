// Grok Build (xAI CLI) hook entry point. Handler logic lives in the shared
// createHookHandlers factory (hook-handlers.ts); this file supplies Grok-specific
// constants and — unlike bare runHookMain — wraps dispatch in the Grok payload /
// PreToolUse output adapters (grok-adapter.ts).
//
// Grok's PreToolUse protocol is not Claude's: stdin uses camelCase toolName /
// toolInput, tool names are Grok-native (read_file/list_dir/grep/…), and stdout
// must be {decision, reason?} (Claude's permissionDecision shape is ignored).
// Without the adapter the file-backed s6 guard never sees cold tools in scope.
import type { EnvelopeEvent } from "./agent_envelope.js";
import {
  GROK_CONTEXT_WINDOW,
  GROK_HOOKS_ENABLED,
  GROK_AGENT_ID,
  GROK_VAULT_PATH,
  GROK_WORKSPACE_ID,
} from "./config.js";
import {
  adaptGrokPayload,
  adaptGrokPreToolUseOutput,
  grokAllow,
} from "./grok-adapter.js";
import { createHookHandlers, recordUnroutedEvent } from "./hook-handlers.js";
import type { AgentHookConfig } from "./hook-handlers.js";
import { grokBuildWire } from "./hook-platform.js";
import { VALID_EVENTS, asString, emit, readStdin } from "./hook-utils.js";
import {
  discardDeliveryCommits,
  emitAndCommit,
  exitAfterDelivery,
  failAndExit,
} from "./hook-delivery.js";
import { PRE_TOOL_USE_EVENT } from "./recall-guard.js";
import type { PreToolUseDecisionOutput } from "./recall-guard.js";
import { recordAudit } from "./vault.js";

const CONFIG: AgentHookConfig = {
  agentId: GROK_AGENT_ID,
  vaultPath: GROK_VAULT_PATH,
  defaultWorkspaceId: GROK_WORKSPACE_ID,
  contextWindow: GROK_CONTEXT_WINDOW,
  hooksEnabled: GROK_HOOKS_ENABLED,
  runtime: "grok-build",
  hookScript: "grok-hook.js",
  auditPrefix: "hook_grok",
  // Mirrors hooks/hooks-grok.json UserPromptSubmit "timeout": 30 — edit both.
  promptHookTimeoutMs: 30_000,
  // Mirrors hooks/hooks-grok.json SessionStart "timeout": 30 — edit both.
  sessionStartHookTimeoutMs: 30_000,
  precompactKind: "grok_precompact_handoff",
  // Wire is the platform contract, not the memory principal. Agent id is
  // user-overridable (MINNI_GROK_AGENT_ID); deriving the wire from it would
  // disable Grok's Stop duplicate filter and drop-accounting.
  wire: grokBuildWire,
};

async function main(): Promise<void> {
  const eventArg = (process.argv[2] ?? "").trim();
  const isPreToolUse = eventArg === PRE_TOOL_USE_EVENT;
  const emitNoop = (): void => {
    emit(isPreToolUse ? grokAllow() : { continue: true });
  };

  if (!CONFIG.hooksEnabled) {
    emitNoop();
    return;
  }

  const raw = (await readStdin()) as Record<string, unknown>;
  const event =
    eventArg ||
    asString(raw.hook_event_name).trim() ||
    asString(raw.hookEventName).trim();

  if (event !== PRE_TOOL_USE_EVENT && !VALID_EVENTS.includes(event as EnvelopeEvent)) {
    // P4 (#228): record the drop instead of exiting clean and silent.
    await recordUnroutedEvent(CONFIG.vaultPath, CONFIG.auditPrefix, event);
    emitNoop();
    return;
  }

  // Grok fires an extra observe-only Stop at session end; filter before work.
  if (CONFIG.wire?.shouldHandle && !CONFIG.wire.shouldHandle(event, raw)) {
    emit(CONFIG.wire.noop());
    return;
  }

  const payload = adaptGrokPayload(raw);
  try {
    const handlers = createHookHandlers(CONFIG);
    const output = await handlers.dispatch(event, payload);
    // emitAndCommit, not emit: deferred handler work commits only after delivery.
    await emitAndCommit(
      event === PRE_TOOL_USE_EVENT
        ? adaptGrokPreToolUseOutput(output as PreToolUseDecisionOutput)
        : output,
      { vaultPath: CONFIG.vaultPath, auditPrefix: CONFIG.auditPrefix, event },
    );
    exitAfterDelivery();
  } catch (error) {
    discardDeliveryCommits();
    const message = error instanceof Error ? error.message : String(error);
    try {
      await recordAudit(CONFIG.vaultPath, {
        tool: `${CONFIG.auditPrefix}_error`,
        summary: `${event}: ${message}`,
      });
    } catch {
      // audit unavailable; fallback output below still keeps Grok unblocked
    }
    // failAndExit, not emit: flush degraded signal before harness kill.
    await failAndExit(
      event === PRE_TOOL_USE_EVENT
        ? grokAllow()
        : {
            continue: true,
            systemMessage:
              `Minni hook degraded (${event}): ${message} — memory injection ` +
              "skipped this event; see vault log.md.",
          },
    );
  }
}

void main();

import {
  CURSOR_AGENT_ID,
  CURSOR_CONTEXT_WINDOW,
  CURSOR_HOOKS_ENABLED,
  CURSOR_VAULT_PATH,
  CURSOR_WORKSPACE_ID,
} from "./config.js";
import { adaptCursorOutput, adaptCursorPayload, CURSOR_EVENTS } from "./cursor-adapter.js";
import { createHookHandlers } from "./hook-handlers.js";
import { asString, emit, readStdin } from "./hook-utils.js";
import { recordAudit } from "./vault.js";
import type { RecallGuardMode } from "./recall-guard.js";

const CURSOR_GUARD_MODE: RecallGuardMode = (() => {
  const raw = (process.env.MINNI_RECALL_GUARD_MODE ?? "").trim().toLowerCase();
  if (raw === "off" || raw === "soft" || raw === "strict") return raw;
  return "strict";
})();

const CONFIG = {
  agentId: CURSOR_AGENT_ID,
  vaultPath: CURSOR_VAULT_PATH,
  defaultWorkspaceId: CURSOR_WORKSPACE_ID,
  contextWindow: CURSOR_CONTEXT_WINDOW,
  hooksEnabled: CURSOR_HOOKS_ENABLED,
  runtime: "cursor",
  hookScript: "cursor-hook.js",
  auditPrefix: "hook_cursor",
  precompactKind: "cursor_precompact_handoff",
  alwaysWriteStopInbox: false,
  recallGuardMode: CURSOR_GUARD_MODE,
} as const;

async function main(): Promise<void> {
  const raw = (await readStdin()) as Record<string, unknown>;
  const cursorEvent = (process.argv[2] ?? asString(raw.hook_event_name)).trim();
  const event = CURSOR_EVENTS[cursorEvent];
  if (!CONFIG.hooksEnabled || !event) {
    emit(cursorEvent === "preToolUse" ? { permission: "allow" } : { continue: true });
    return;
  }
  try {
    const output = await createHookHandlers(CONFIG).dispatch(event, adaptCursorPayload(raw));
    emit(adaptCursorOutput(event, output as Record<string, unknown>));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await recordAudit(CONFIG.vaultPath, {
      tool: "hook_cursor_error",
      summary: `${event}: ${message}`,
    }).catch(() => undefined);
    emit(cursorEvent === "preToolUse"
      ? { permission: "allow" }
      : { continue: true, user_message: `Minni hook degraded (${event}): ${message}` });
  }
}

void main();

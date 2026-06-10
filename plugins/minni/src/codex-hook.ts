// Codex hook entry point. All handler logic lives in the shared
// createHookHandlers/runHookMain factory (hook-handlers.ts); this file only
// supplies the codex-specific constants.
import {
  CODEX_CONTEXT_WINDOW,
  CODEX_HOOKS_ENABLED,
  DEFAULT_AGENT_ID,
  DEFAULT_VAULT_PATH,
  DEFAULT_WORKSPACE_ID,
} from "./config.js";
import { runHookMain } from "./hook-handlers.js";

void runHookMain({
  agentId: DEFAULT_AGENT_ID,
  vaultPath: DEFAULT_VAULT_PATH,
  defaultWorkspaceId: DEFAULT_WORKSPACE_ID,
  contextWindow: CODEX_CONTEXT_WINDOW,
  hooksEnabled: CODEX_HOOKS_ENABLED,
  runtime: "codex",
  hookScript: "codex-hook.js",
  auditPrefix: "hook_codex",
  precompactKind: "codex_precompact_handoff",
  // Historical codex behavior: Stop writes the inbox + audit entry even when
  // the outcome draft is empty.
  alwaysWriteStopInbox: true,
});

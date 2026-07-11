// Codex hook entry point. All handler logic lives in the shared
// createHookHandlers/runHookMain factory (hook-handlers.ts); this file only
// supplies the codex-specific constants.
import {
  CODEX_AGENT_ID,
  CODEX_CONTEXT_WINDOW,
  CODEX_HOOKS_ENABLED,
  CODEX_VAULT_PATH,
  CODEX_WORKSPACE_ID,
} from "./config.js";
import { runHookMain } from "./hook-handlers.js";

void runHookMain({
  agentId: CODEX_AGENT_ID,
  vaultPath: CODEX_VAULT_PATH,
  defaultWorkspaceId: CODEX_WORKSPACE_ID,
  contextWindow: CODEX_CONTEXT_WINDOW,
  hooksEnabled: CODEX_HOOKS_ENABLED,
  runtime: "codex",
  hookScript: "codex-hook.js",
  auditPrefix: "hook_codex",
  precompactKind: "codex_precompact_handoff",
  // Empty outcome drafts are not reviewable memory proposals. Keep the Codex
  // inbox clean, matching the other maintained platform adapters.
  alwaysWriteStopInbox: false,
});

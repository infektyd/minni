// Grok (grok-build) hook entry point. All handler logic lives in the shared
// createHookHandlers/runHookMain factory (hook-handlers.ts); this file only
// supplies the grok-specific constants.
import {
  GROK_CONTEXT_WINDOW,
  GROK_HOOKS_ENABLED,
  GROK_AGENT_ID,
  GROK_VAULT_PATH,
  GROK_WORKSPACE_ID,
} from "./config.js";
import { runHookMain } from "./hook-handlers.js";

void runHookMain({
  agentId: GROK_AGENT_ID,
  vaultPath: GROK_VAULT_PATH,
  defaultWorkspaceId: GROK_WORKSPACE_ID,
  contextWindow: GROK_CONTEXT_WINDOW,
  hooksEnabled: GROK_HOOKS_ENABLED,
  runtime: "grok-build",
  hookScript: "grok-hook.js",
  auditPrefix: "hook_grok",
  precompactKind: "grok_precompact_handoff",
  // Grok behavior (review-panel improvement): an empty outcome draft skips the
  // inbox write entirely so the inbox is never littered with empty files.
  alwaysWriteStopInbox: false,
});

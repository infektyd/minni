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
import { grokBuildWire } from "./hook-platform.js";

void runHookMain({
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
  precompactKind: "grok_precompact_handoff",
  // Wire is the platform contract, not the memory principal. Agent id is
  // user-overridable (MINNI_GROK_AGENT_ID); deriving the wire from it would
  // disable Grok's Stop duplicate filter and drop-accounting.
  wire: grokBuildWire,
});

// Codex hook entry point. All handler logic lives in the shared
// createHookHandlers/runHookMain factory (hook-handlers.ts); this file only
// supplies the codex-specific constants. Identity is Codex-native
// (CODEX_*), never the generic MCP identity (DEFAULT_*) — the hook and the
// MCP server must be able to disagree without leaking one into the other.
import {
  CODEX_AGENT_ID,
  CODEX_CONTEXT_WINDOW,
  CODEX_HOOKS_ENABLED,
  CODEX_VAULT_PATH,
  CODEX_WORKSPACE_ID,
} from "./config.js";
import { runHookMain } from "./hook-handlers.js";
import { codexWire } from "./hook-platform.js";

void runHookMain({
  agentId: CODEX_AGENT_ID,
  vaultPath: CODEX_VAULT_PATH,
  defaultWorkspaceId: CODEX_WORKSPACE_ID,
  contextWindow: CODEX_CONTEXT_WINDOW,
  hooksEnabled: CODEX_HOOKS_ENABLED,
  runtime: "codex",
  hookScript: "codex-hook.js",
  auditPrefix: "hook_codex",
  // Mirrors hooks/hooks-codex.json UserPromptSubmit "timeout": 30 — edit both.
  promptHookTimeoutMs: 30_000,
  precompactKind: "codex_precompact_handoff",
  // Wire is the platform contract, not the memory principal. Agent id is
  // user-overridable; deriving the wire from it would fall through to the
  // Claude shape under a custom MINNI_CODEX_AGENT_ID.
  wire: codexWire,
});

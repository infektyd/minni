// KiloCode hook entry point. All handler logic lives in the shared
// createHookHandlers/runHookMain factory (hook-handlers.ts); this file only
// supplies the kilocode-specific constants. Genuine kilocode deviations are
// factory options: identity-recall boot (no daemon layer1 channel, so no
// runtime/hookScript fallback commands), no PreCompact inbox handoff
// (precompactKind omitted), and the /minni:learn Stop hint.
import {
  KILOCODE_AGENT_ID,
  KILOCODE_CONTEXT_WINDOW,
  KILOCODE_HOOKS_ENABLED,
  KILOCODE_VAULT_PATH,
  KILOCODE_WORKSPACE_ID,
} from "./config.js";
import { runHookMain } from "./hook-handlers.js";

void runHookMain({
  agentId: KILOCODE_AGENT_ID,
  vaultPath: KILOCODE_VAULT_PATH,
  defaultWorkspaceId: KILOCODE_WORKSPACE_ID,
  contextWindow: KILOCODE_CONTEXT_WINDOW,
  hooksEnabled: KILOCODE_HOOKS_ENABLED,
  auditPrefix: "hook",
  bootIdentity: "identity-recall",
  stopCommitHint: "Use /minni:learn to commit.",
  // Review-panel fix (shared root cause of five findings): Stop previously
  // wrote an inbox file UNCONDITIONALLY (zero-candidate litter) and without
  // the canonical stop_candidates kind or agent_id/workspace_id stamps. The
  // factory's guarded write supplies all three; like grok, an empty outcome
  // skips the write entirely.
  alwaysWriteStopInbox: false,
});

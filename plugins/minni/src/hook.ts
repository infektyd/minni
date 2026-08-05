// Claude Code hook entry point. #283: all handler logic lives in the shared
// createHookHandlers/runHookMain factory (hook-handlers.ts), same as every
// other platform's *-hook.ts; this file only supplies the claude-code-
// specific constants and the knobs that carry claude's own behavior
// (lifecycle representation, boot-time handoff acking, identity-recall boot
// identity) — see AgentHookConfig in hook-handlers.ts for what each knob
// does and why it defaults off for every other agent.
import {
  CLAUDECODE_AGENT_ID,
  CLAUDECODE_CONTEXT_WINDOW,
  CLAUDECODE_HOOKS_ENABLED,
  CLAUDECODE_VAULT_PATH,
  CLAUDECODE_WORKSPACE_ID,
} from "./config.js";
import { runHookMain } from "./hook-handlers.js";
import { claudeCodeWire } from "./hook-platform.js";

/**
 * Claude Code's SessionStart deadline, as an OUTER bound on the boot budget —
 * see `sessionStartHookTimeoutMs` on `AgentHookConfig`. Mirrors
 * `hooks/hooks.json` SessionStart `"timeout": 600`; the two must be edited
 * together, since a manifest tightened below the budget would resurrect the
 * bug this bounds (killed mid-boot, output discarded).
 *
 * 600s is Claude Code's DOCUMENTED PLATFORM default for a `command` hook.
 * SessionStart does not lower it (the events that do are UserPromptSubmit at
 * 30s, MessageDisplay at 10s and SessionEnd's shared 1.5s budget) — so 600 is
 * what this entry inherits when the manifest is silent. Those are the
 * platform's numbers, not ours: `hooks.json` separately sets UserPromptSubmit
 * to 60, which is OUR choice and unrelated to this constant.
 *
 * The manifest states 600 explicitly rather than inheriting it, so the value
 * this constant mirrors is visible in the file the platform reads — writing a
 * SMALLER number here would be a real tightening of the boot deadline, not
 * documentation of it.
 */
const CLAUDECODE_SESSION_START_TIMEOUT_MS = 600_000;

void runHookMain({
  agentId: CLAUDECODE_AGENT_ID,
  vaultPath: CLAUDECODE_VAULT_PATH,
  defaultWorkspaceId: CLAUDECODE_WORKSPACE_ID,
  contextWindow: CLAUDECODE_CONTEXT_WINDOW,
  hooksEnabled: CLAUDECODE_HOOKS_ENABLED,
  // Historical, pre-dates the "hook_<platform>" convention every later
  // platform uses — kept bare so existing audit tool names
  // (hook_session_start, hook_stop, ...) and any tooling/grep patterns that
  // depend on them stay stable. See hook-platform.test.mjs's audit-prefix
  // uniqueness check, which deliberately excludes this file for the same
  // reason.
  auditPrefix: "hook",
  // Claude Code has no daemon layer1 channel wired through `runtime` /
  // `hookScript` in its envelope — it has always surfaced the boot identity
  // read as `recent_learnings` (trimmed Learnings slice), the same shape
  // kilocode uses, not the native-layer1-prefix / layer1_source /
  // fallback_commands shape codex and grok-build get. `runtime` and
  // `hookScript` are deliberately omitted (not required by this mode) so the
  // envelope's `identity` object never gains a `runtime` key it never had.
  bootIdentity: "identity-recall",
  stopCommitHint: "Use /minni:learn to commit.",
  // c2-c5: the standing 4-surface lifecycle line + once-per-session
  // situational emphasis. claude-code only, historically inline in this
  // file; #283 promoted it to a factory knob rather than duplicating it.
  lifecycleEnabled: true,
  // SessionStart lists and acks this agent's pending handoff leases,
  // surfacing `handoff_acks` in the envelope. claude-code only.
  ackPendingHandoffsAtBoot: true,
  sessionStartHookTimeoutMs: CLAUDECODE_SESSION_START_TIMEOUT_MS,
  // promptHookTimeoutMs intentionally OMITTED (#283 residual, disclosed, not
  // fixed here): every other platform's UserPromptSubmit budget is clamped
  // to `promptHookTimeoutMs * 0.6` (effectiveHookBudgetMs) as an outer safety
  // ceiling against its own manifest timeout. claude-code's UserPromptSubmit
  // budget has never had that ceiling — hook.ts called the unclamped
  // `hookBudgetMs()` directly — and omitting this knob preserves that exact
  // behavior rather than silently tightening it as a side effect of this
  // migration. Claude Code's own manifest declares 60s for this event
  // (hooks/hooks.json); a future PR could set `promptHookTimeoutMs: 60_000`
  // to close this gap deliberately, with its own review.
  wire: claudeCodeWire,
});

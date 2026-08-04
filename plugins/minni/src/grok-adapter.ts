// Grok Build (xAI CLI) payload + PreToolUse output adaptation.
// Pure functions — grok-hook.ts composes these around createHookHandlers.
//
// Grok does NOT speak Claude Code's PreToolUse protocol end-to-end:
//   - stdin is camelCase throughout: toolName / toolInput / sessionId /
//     hookEventName / lastAssistantMessage (not tool_name / tool_input / …).
//   - Tool names are Grok-native: read_file, list_dir, grep, run_terminal_command
//     (matchers alias Claude Read/Grep/Glob/Bash onto these; the hook still
//     receives the real native name).
//   - PreToolUse stdout is {decision:"allow"|"deny", reason?} — Claude's
//     hookSpecificOutput.permissionDecision shape is not parsed. Exit 2 is
//     also an explicit deny, but JSON decision is preferred (honored regardless
//     of exit code).
// Verified against Grok Build docs (hooks user guide) and
// docs/contracts/hook-platforms.md.
import type { PreToolUseDecisionOutput } from "./recall-guard.js";
import { asString } from "./hook-utils.js";

/** Grok-native tool names → Claude Code names the shared recall guard understands. */
const GROK_TOOL_NAMES: Record<string, string> = {
  run_terminal_command: "Bash",
  read_file: "Read",
  list_dir: "Glob",
  grep: "Grep",
};

/**
 * Translate a Grok Build hook payload into the field names the shared factory
 * (hook-handlers.ts) and recall guard read. Original fields are preserved;
 * canonical fields fill only when absent so a future native snake_case envelope
 * wins without a code change.
 */
export function adaptGrokPayload(raw: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...raw };

  if (!asString(out.session_id)) {
    const sessionId = asString(raw.sessionId);
    if (sessionId) out.session_id = sessionId;
  }

  if (!asString(out.hook_event_name)) {
    const eventName = asString(raw.hookEventName);
    if (eventName) out.hook_event_name = eventName;
  }

  if (!asString(out.cwd)) {
    // workspaceRoot is Grok's project root; treat as cwd for the shared
    // workspace resolver (explicit workspace_id / workspaceId still win).
    const root = asString(raw.workspaceRoot);
    if (root) out.cwd = root;
  }

  if (!asString(out.last_assistant_message)) {
    const msg = asString(raw.lastAssistantMessage);
    if (msg) out.last_assistant_message = msg;
  }

  if (!asString(out.last_user_message)) {
    const msg = asString(raw.lastUserMessage) || asString(raw.userMessage);
    if (msg) out.last_user_message = msg;
  }

  if (!asString(out.prompt)) {
    const prompt =
      asString(raw.prompt) || asString(raw.userPrompt) || asString(raw.user_message);
    if (prompt) out.prompt = prompt;
  }

  // toolName → tool_name (+ native → Claude alias for CORE_SCOPE).
  const rawTool =
    asString(out.tool_name) || asString(raw.toolName) || asString(raw.tool_name);
  if (rawTool) {
    out.tool_name = GROK_TOOL_NAMES[rawTool] ?? rawTool;
  }

  if (
    out.tool_input === undefined &&
    raw.toolInput !== undefined &&
    typeof raw.toolInput === "object" &&
    raw.toolInput !== null &&
    !Array.isArray(raw.toolInput)
  ) {
    out.tool_input = raw.toolInput;
  }

  return out;
}

/** Grok PreToolUse decision shape (allow|deny only; no ask/defer). */
export interface GrokPreToolDecision {
  decision: "allow" | "deny";
  reason?: string;
}

/** Explicit allow — preferred over empty stdout for PreToolUse. */
export function grokAllow(): GrokPreToolDecision {
  return { decision: "allow" };
}

/**
 * Translate the shared guard's Claude permissionDecision output into Grok's
 * {decision, reason} vocabulary. Deny carries the recall pointer (deny-to-surface).
 */
export function adaptGrokPreToolUseOutput(
  output: PreToolUseDecisionOutput,
): GrokPreToolDecision {
  if (output.hookSpecificOutput?.permissionDecision === "deny") {
    return {
      decision: "deny",
      reason: output.hookSpecificOutput.permissionDecisionReason,
    };
  }
  return grokAllow();
}

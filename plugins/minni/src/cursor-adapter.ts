import type { PreToolUseDecisionOutput } from "./recall-guard.js";
import { asString } from "./hook-utils.js";

export const CURSOR_EVENTS: Record<string, string> = {
  sessionStart: "SessionStart",
  beforeSubmitPrompt: "UserPromptSubmit",
  preCompact: "PreCompact",
  stop: "Stop",
  preToolUse: "PreToolUse",
};

export function adaptCursorPayload(raw: Record<string, unknown>): Record<string, unknown> {
  const out = { ...raw };
  if (!asString(out.session_id)) out.session_id = asString(raw.conversation_id);
  if (!asString(out.prompt)) out.prompt = asString(raw.user_message);
  if (!asString(out.workspace_id) && Array.isArray(raw.workspace_roots)) {
    out.workspace_id = raw.workspace_roots.find(
      (value): value is string => typeof value === "string" && value.trim() !== "",
    );
  }
  if (asString(raw.tool_name) === "Shell") out.tool_name = "Bash";
  return out;
}

export function adaptCursorOutput(event: string, output: Record<string, unknown>): Record<string, unknown> {
  if (event === "SessionStart") {
    const specific = output.hookSpecificOutput as Record<string, unknown> | undefined;
    const context = asString(specific?.additionalContext);
    return context ? { additional_context: context } : { continue: true };
  }
  if (event === "PreToolUse") {
    const specific = (output as unknown as PreToolUseDecisionOutput).hookSpecificOutput;
    if (specific?.permissionDecision === "deny") {
      return { permission: "deny", user_message: specific.permissionDecisionReason };
    }
    return { permission: "allow" };
  }
  // beforeSubmitPrompt only accepts continue/user_message; its current schema
  // cannot inject prompt-specific recall. The handler still records the prompt
  // and prepares recall state for the guard.
  return { continue: true };
}

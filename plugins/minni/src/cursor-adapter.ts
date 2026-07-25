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
  if (event === "Stop") {
    // Cursor's stop validator accepts ONLY `followup_message`; `continue` is
    // dropped. The shared handler's candidate announcement rides `systemMessage`
    // (Claude Code's field), so translate it or it reaches nobody.
    const message = asString(output.systemMessage);
    return message ? { followup_message: message } : {};
  }
  if (event === "PreCompact") {
    // preCompact accepts only `user_message`, and cannot block. PreCompact's
    // real work here is the inbox handoff side effect, not its output.
    return {};
  }
  // beforeSubmitPrompt CANNOT inject context: its documented output is
  // `continue` + `user_message` only (https://cursor.com/docs/hooks), and there
  // are open vendor feature requests to add `additional_context`. The bundle
  // does validate the field, but relying on undocumented behavior here would be
  // exactly the mistake this module exists to avoid. The handler still records
  // the prompt and prepares recall state for the guard.
  return { continue: true };
}

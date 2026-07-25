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
  // PreToolUse is NOT an EnvelopeEvent -- it bypasses the platform wire and
  // arrives in the shared guard's Claude-shaped permissionDecision form, so it
  // still needs translating here.
  if (event === "PreToolUse") {
    const specific = (output as unknown as PreToolUseDecisionOutput).hookSpecificOutput;
    if (specific?.permissionDecision === "deny") {
      return { permission: "deny", user_message: specific.permissionDecisionReason };
    }
    return { permission: "allow" };
  }
  // Everything else is already native: cursorWire (hook-platform.ts) renders
  // Cursor's own flat shape, so there is nothing left to convert. This used to
  // translate Claude envelopes after the fact, which silently discarded any
  // intent Cursor could not carry instead of recording the drop.
  return output;
}

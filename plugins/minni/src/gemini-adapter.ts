// Gemini/Antigravity (agy CLI) payload + output adaptation. Pure functions,
// no side effects — the gemini-hook.ts entrypoint composes these around the
// shared createHookHandlers factory, and tests import them directly.
//
// The agy CLI (Antigravity CLI) loads Claude Code-style hooks.json manifests
// from ~/.gemini/config/plugins/<name>/hooks.json, but its hook PROTOCOL is
// not Claude Code's (all of this verified live against agy 1.0.15 on
// 2026-07-03; payload capture in the #133 investigation):
//   - Only PreToolUse, PostToolUse and Stop events exist. SessionStart,
//     UserPromptSubmit and PreCompact are not in the binary's event set.
//   - The stdin payload has agy's own field names: conversationId (not
//     session_id), toolCall {name, args} (not tool_name/tool_input),
//     workspacePaths (not cwd), plus stepIdx/modelName/transcriptPath/
//     artifactDirectoryPath.
//   - Tool names are agy-native (e.g. "run_command", args {CommandLine, Cwd}),
//     not Claude Code's ("Bash", args {command}).
//   - PreToolUse hooks must print a NON-EMPTY decision: agy 1.0.15's
//     permission manager errors on empty decision strings (fixed upstream
//     after 1.0.15, per the agy changelog). The accepted allow value is
//     "approve" (verified live); "block" is the deny value from the same
//     legacy Claude Code decision vocabulary agy borrows from.
import type { PreToolUseDecisionOutput } from "./recall-guard.js";
import { asString } from "./hook-utils.js";

/** agy tool names -> Claude Code tool names the recall guard understands. */
const AGY_TOOL_NAMES: Record<string, string> = {
  run_command: "Bash",
};

/** Per-agy-tool argument key mapping into the guard's expected tool_input. */
const AGY_ARG_KEYS: Record<string, Record<string, string>> = {
  run_command: { CommandLine: "command", Cwd: "cwd" },
};

/**
 * Translate an agy hook payload into the field names the shared hook factory
 * (hook-handlers.ts) reads. Original fields are preserved; canonical fields
 * are only filled when absent, so a future agy that speaks the Claude Code
 * schema natively wins without a code change.
 */
export function adaptAgyPayload(
  raw: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...raw };

  const conversationId = asString(raw.conversationId);
  if (conversationId && !asString(out.session_id)) {
    out.session_id = conversationId;
  }

  if (!asString(out.workspace_id) && Array.isArray(raw.workspacePaths)) {
    const workspace = raw.workspacePaths.find(
      (entry): entry is string => typeof entry === "string" && entry.trim() !== "",
    );
    if (workspace) out.workspace_id = workspace;
  }

  const toolCall = raw.toolCall;
  if (toolCall && typeof toolCall === "object" && !Array.isArray(toolCall)) {
    const call = toolCall as Record<string, unknown>;
    const agyName = asString(call.name);
    if (agyName && !asString(out.tool_name)) {
      out.tool_name = AGY_TOOL_NAMES[agyName] ?? agyName;
    }
    if (
      out.tool_input === undefined &&
      call.args &&
      typeof call.args === "object" &&
      !Array.isArray(call.args)
    ) {
      const keyMap = AGY_ARG_KEYS[agyName] ?? {};
      const mapped: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(call.args as Record<string, unknown>)) {
        mapped[keyMap[key] ?? key] = value;
      }
      out.tool_input = mapped;
    }
  }

  return out;
}

/**
 * agy's PreToolUse decision shape. Minimal on purpose: "approve" is the only
 * live-verified allow value on 1.0.15, and extra fields risk tripping a parser
 * that already errors on empty decisions.
 */
export interface AgyPreToolDecision {
  decision: "approve" | "block";
  reason?: string;
}

/** The always-safe allow. PreToolUse must NEVER emit an empty/absent decision. */
export function agyApprove(): AgyPreToolDecision {
  return { decision: "approve" };
}

/**
 * Translate the shared recall guard's Claude Code permissionDecision output
 * into agy's decision vocabulary. The deny reason carries the recall pointer
 * (deny-to-surface); everything else collapses to the explicit approve.
 */
export function adaptPreToolUseOutput(
  output: PreToolUseDecisionOutput,
): AgyPreToolDecision {
  if (output.hookSpecificOutput?.permissionDecision === "deny") {
    return {
      decision: "block",
      reason: output.hookSpecificOutput.permissionDecisionReason,
    };
  }
  return agyApprove();
}

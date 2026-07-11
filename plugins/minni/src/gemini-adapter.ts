// Gemini/Antigravity (agy CLI) payload + output adaptation. Pure functions,
// no side effects — the gemini-hook.ts entrypoint composes these around the
// shared createHookHandlers factory, and tests import them directly.
//
// The agy CLI (Antigravity CLI) loads Claude Code-style hooks.json manifests
// from ~/.gemini/config/plugins/<name>/hooks.json, but its hook PROTOCOL is
// not Claude Code's (payload captured on 1.0.15 and re-verified on 1.1.1):
//   - PreToolUse and Stop are native; 1.1.1's compatibility layer also
//     dispatches SessionStart. UserPromptSubmit and PreCompact remain absent.
//   - The stdin payload has agy's own field names: conversationId (not
//     session_id), toolCall {name, args} (not tool_name/tool_input),
//     workspacePaths (not cwd), plus stepIdx/modelName/transcriptPath/
//     artifactDirectoryPath.
//   - Tool names are agy-native (e.g. "run_command", args {CommandLine, Cwd}),
//     not Claude Code's ("Bash", args {command}).
//   - Current agy (1.1.x) documents allow/deny/ask/force_ask. The legacy
//     approve/block vocabulary from 1.0.15 is rejected by current releases.
import { open, stat } from "node:fs/promises";

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
 * agy's current PreToolUse decision shape. Minimal on purpose: "allow" and
 * "deny" are the only values Minni needs from the documented vocabulary.
 */
export interface AgyPreToolDecision {
  decision: "allow" | "deny";
  reason?: string;
}

/** The always-safe allow. PreToolUse must NEVER emit an empty/absent decision. */
export function agyApprove(): AgyPreToolDecision {
  return { decision: "allow" };
}

/**
 * Translate the shared recall guard's Claude Code permissionDecision output
 * into agy's decision vocabulary. The deny reason carries the recall pointer
 * (deny-to-surface); everything else collapses to the explicit allow.
 */
export function adaptPreToolUseOutput(
  output: PreToolUseDecisionOutput,
): AgyPreToolDecision {
  if (output.hookSpecificOutput?.permissionDecision === "deny") {
    return {
      decision: "deny",
      reason: output.hookSpecificOutput.permissionDecisionReason,
    };
  }
  return agyApprove();
}

/** Never read more than this much transcript tail; sessions can grow unbounded. */
const TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024;
/** handleStop truncates its task to 200 chars; a small margin keeps this cheap. */
const LAST_USER_MESSAGE_MAX = 400;

/**
 * Codex review (PR #134): agy's Stop payload carries no task text, so the
 * shared handleStop would fall back to the conversation id and could draft
 * "session ended"-grade candidates. agy DOES point at its transcript
 * (transcript_full.jsonl: one JSON object per line; explicit user prompts are
 * source USER_EXPLICIT / type USER_INPUT with the prompt wrapped in
 * <USER_REQUEST> tags — format live-verified on agy 1.0.15). Pull the LAST
 * explicit user message out of the transcript tail and surface it as
 * last_user_message. Best-effort on purpose: any miss (no path, unreadable
 * file, format drift) leaves the payload untouched, and handleStop's existing
 * fallback chain applies.
 */
export async function enrichAgyStopPayload(
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (asString(payload.last_user_message) || asString(payload.summary)) {
    return payload;
  }
  const transcriptPath = asString(payload.transcriptPath);
  if (!transcriptPath) return payload;

  let tail: string;
  try {
    const info = await stat(transcriptPath);
    if (!info.isFile() || info.size === 0) return payload;
    const start = Math.max(0, info.size - TRANSCRIPT_TAIL_BYTES);
    const handle = await open(transcriptPath, "r");
    try {
      const length = info.size - start;
      const buffer = Buffer.alloc(length);
      await handle.read(buffer, 0, length, start);
      tail = buffer.toString("utf8");
    } finally {
      await handle.close();
    }
  } catch {
    return payload;
  }

  const lines = tail.split("\n");
  // A mid-file start offset can leave a partial first line; JSON.parse below
  // rejects it naturally, so no special-casing is needed.
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;
    let entry: Record<string, unknown>;
    try {
      entry = JSON.parse(line) as Record<string, unknown>;
    } catch {
      continue;
    }
    if (entry.source !== "USER_EXPLICIT" || entry.type !== "USER_INPUT") continue;
    const content = asString(entry.content);
    if (!content) continue;
    const request = /<USER_REQUEST>\s*([\s\S]*?)\s*<\/USER_REQUEST>/.exec(content);
    const message = (request?.[1] ?? content).trim();
    if (!message) continue;
    return { ...payload, last_user_message: message.slice(0, LAST_USER_MESSAGE_MAX) };
  }
  return payload;
}

// Gemini/Antigravity (agy CLI) payload + output adaptation. Pure functions,
// no side effects — the gemini-hook.ts entrypoint composes these around the
// shared createHookHandlers factory, and tests import them directly.
//
// agy's hook protocol is NOT Claude Code's, and its manifest format is not
// either -- see hooks-gemini.json and docs/contracts/hook-platforms.md.
// Re-verified against agy 1.1.7 and https://antigravity.google/docs/hooks;
// the previous "verified live, agy 1.0.15" notes here were stale and two of
// them were affirmatively wrong.
//   - Events are PreToolUse, PostToolUse, PreInvocation, PostInvocation, Stop
//     and (undocumented but real) SessionStart. There is NO UserPromptSubmit
//     and NO PreCompact; PreInvocation is the prompt-submit analogue.
//   - The stdin payload has agy's own field names: conversationId (not
//     session_id), toolCall {name, args} (not tool_name/tool_input),
//     workspacePaths (not cwd), plus stepIdx/modelName/transcriptPath/
//     artifactDirectoryPath.
//   - Tool names are agy-native (e.g. "run_command", args {CommandLine, Cwd}),
//     not Claude Code's ("Bash", args {command}).
//   - PreToolUse hooks must print a NON-EMPTY decision; an empty one is
//     rejected and denies the call.
//   - The decision enum is agy's own: "allow" | "deny" | "ask" | "force_ask".
//     Claude Code's "approve"/"block" are NOT accepted -- agy fails the step
//     outright with: unknown pre-tool hook decision "approve".
import { open, stat } from "node:fs/promises";

import type { EnvelopeEvent } from "./agent_envelope.js";
import type { PreToolUseDecisionOutput } from "./recall-guard.js";
import { asString } from "./hook-utils.js";

/**
 * agy's native event names -> Minni's internal EnvelopeEvent names.
 *
 * Declaring Claude Code's names in an agy manifest is what made this
 * integration dead config: `UserPromptSubmit` and `PreCompact` simply do not
 * exist on agy, so those entries never fired and never would have.
 *
 * Exported from the adapter (round 3, PR #260) so the P4 parity gate can
 * read the routed set without importing the hook entry point, which executes
 * on import.
 */
export const AGY_EVENTS: Record<string, EnvelopeEvent> = {
  SessionStart: "SessionStart",
  PreInvocation: "UserPromptSubmit",
  Stop: "Stop",
};

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

  // workspacePaths is agy's CWD, not a workspace LABEL. Filling `workspace_id`
  // from it short-circuited workspaceFromPayload's explicit-id branch, so the
  // agy surface skipped both the project-root walk and the realpath that every
  // other entrypoint applies: a session started in `<repo>/sub` (or through a
  // symlink) labelled itself `<repo>/sub` where claude-code labelled the same
  // physical directory `<repo>`, splitting one project's memory in two.
  // Populate `cwd` instead and let the shared resolver do its job — an
  // explicit, caller-supplied `workspace_id` still wins, which is what that
  // short-circuit is actually for.
  if (!asString(out.cwd) && Array.isArray(raw.workspacePaths)) {
    const workspace = raw.workspacePaths.find(
      (entry): entry is string => typeof entry === "string" && entry.trim() !== "",
    );
    if (workspace) out.cwd = workspace;
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
 * agy's PreToolUse decision shape. Minimal on purpose: extra fields risk
 * tripping a parser that already errors on empty decisions.
 */
export interface AgyPreToolDecision {
  decision: "allow" | "deny" | "ask" | "force_ask";
  reason?: string;
}

/** The always-safe allow. PreToolUse must NEVER emit an empty/absent decision. */
export function agyAllow(): AgyPreToolDecision {
  return { decision: "allow" };
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
      decision: "deny",
      reason: output.hookSpecificOutput.permissionDecisionReason,
    };
  }
  return agyAllow();
}

/** Never read more than this much transcript tail; sessions can grow unbounded. */
const TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024;
/** handleStop truncates its task to 200 chars; a small margin keeps this cheap. */
const LAST_USER_MESSAGE_MAX = 400;

/**
 * Mine the last explicit user message from an agy transcript_full.jsonl.
 * Best-effort: any miss (no path, unreadable file, format drift) returns "".
 */
async function lastUserMessageFromTranscript(transcriptPath: string): Promise<string> {
  let tail: string;
  try {
    const info = await stat(transcriptPath);
    if (!info.isFile() || info.size === 0) return "";
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
    return "";
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
    return message.slice(0, LAST_USER_MESSAGE_MAX);
  }
  return "";
}

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
  const message = await lastUserMessageFromTranscript(transcriptPath);
  if (!message) return payload;
  return { ...payload, last_user_message: message };
}

/**
 * PreInvocation is agy's prompt-submit analogue, but its stdin schema has no
 * `prompt` field — only invocation counters plus common metadata (including
 * transcriptPath). Without mining the transcript, handleUserPromptSubmit
 * sees an empty prompt and returns noIntent on every real agy turn, so the
 * per-turn recall pointer never writes. Surface the last user message as
 * `prompt` (the field the shared handler reads).
 */
export async function enrichAgyPromptPayload(
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (asString(payload.prompt) || asString(payload.user_prompt)) {
    return payload;
  }
  const transcriptPath = asString(payload.transcriptPath);
  if (!transcriptPath) return payload;
  const message = await lastUserMessageFromTranscript(transcriptPath);
  if (!message) return payload;
  return { ...payload, prompt: message };
}

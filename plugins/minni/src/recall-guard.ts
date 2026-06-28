// Slice s6: the PreToolUse recall guard (the BACKSTOP layer).
//
// When the agent reaches for a COLD search tool (Grep/Read/Glob, or a
// read/search Bash command) on a turn where strong recall EXISTS and is
// UNCONSULTED, this guard intercepts that tool call ONCE, DENYs it, and surfaces
// the recall — forcing the agent to consult its own memory before searching from
// scratch. s5 (recall-state.ts) already wrote the strong recall to
// <vault>/.runtime/recall-state.json; this module reads it and decides.
//
// CONTRACT (verified against CC docs): PreToolUse has NO additionalContext. The
// ONLY way to surface text to the model is to DENY the tool with a reason:
//   {"hookSpecificOutput":{"hookEventName":"PreToolUse",
//     "permissionDecision":"deny","permissionDecisionReason":"<text>"}}
// To ALLOW/no-op we emit a bare {continue:true} (NO permissionDecision) so the
// user's normal permission flow proceeds. We NEVER emit permissionDecision:
// "allow" (that would skip the user's prompt).
//
// IDEMPOTENCY (load-bearing): the guard denies AT MOST ONCE per turn. On the
// first deny it flips `consumed` to true in the state file; once consumed===true
// the guard ALLOWS for ALL subsequent tool calls this turn, so the re-issued
// call ALWAYS passes. A block loop here would be catastrophic; the consumed flag
// is what prevents it.
import type { RecallState } from "./recall-state.js";

/** PreToolUse is NOT an EnvelopeEvent — its output is the permissionDecision shape. */
export const PRE_TOOL_USE_EVENT = "PreToolUse";

export type RecallGuardMode = "off" | "soft" | "strict";

/** The bare cold search/read tools the guard scopes in every (non-off) mode. */
const CORE_SCOPE_TOOLS = new Set(["Grep", "Read", "Glob"]);

/**
 * Resolve the guard mode from config. Default "soft" (Grep/Read/Glob only; Bash
 * untouched). "strict" additionally guards read/search Bash; "off" disables it.
 * Anything unrecognized falls back to the default rather than guessing.
 */
export function recallGuardMode(
  env: NodeJS.ProcessEnv = process.env,
): RecallGuardMode {
  const raw = (env.MINNI_RECALL_GUARD_MODE ?? "").trim().toLowerCase();
  if (raw === "off" || raw === "soft" || raw === "strict") return raw;
  return "soft";
}

/**
 * Read/search Bash commands the STRICT guard fires on. We fire ONLY for pure
 * read/search; we NEVER fire for anything that edits/builds/runs/moves. When in
 * doubt, this returns false (ALLOW) — a missed guard is cheap, a denied editing
 * command is a loop risk and a correctness hazard.
 *
 * Detection walks each command segment (split on shell separators) and requires
 * that EVERY segment lead with a known read/search verb AND that the whole
 * command be free of mutation signals (redirects, in-place edits, etc.). A
 * single non-read segment or any mutation signal => ALLOW.
 */
const READ_SEARCH_VERBS = new Set([
  "grep",
  "rg",
  "egrep",
  "fgrep",
  "cat",
  "find",
  "ls",
  "head",
  "tail",
  "awk", // read-only awk; mutation signals (e.g. output redirect) are caught below
]);

// Mutation / side-effecting signals anywhere in the command => never guard.
// File redirection is handled separately so descriptor merges like `2>&1` do
// not bypass the guard for otherwise pure read/search commands.
const MUTATION_SIGNAL = /\|\s*tee\b|\bsed\b.*-i|\bxargs\b/;
const FILE_REDIRECT_SIGNAL = /(?:^|[\s;|&])(?:\d*)>>?\s*(?!&)\S/;

export function isReadSearchBashCommand(command: string): boolean {
  const cmd = (command ?? "").trim();
  if (!cmd) return false;

  // File redirects / pipe-to-tee / in-place edit / xargs fan-out => ALLOW.
  // Descriptor merges such as `2>&1` are read-safe and common in agent shells.
  if (FILE_REDIRECT_SIGNAL.test(cmd)) return false;
  if (MUTATION_SIGNAL.test(cmd)) return false;

  // Split on shell separators that chain commands; every segment must be a
  // pure read/search verb for us to fire. A pipeline of greps/cats is still a
  // read; a pipeline that ends in `node`/`python`/`sh` is NOT.
  const segments = cmd
    .split(/\|\||&&|;|\|/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (segments.length === 0) return false;

  for (const segment of segments) {
    // Leading env-var assignments (FOO=bar grep ...) are a mutation-ish shape we
    // don't want to reason about — be conservative and ALLOW.
    const firstToken = segment.split(/\s+/)[0] ?? "";
    if (firstToken.includes("=")) return false;
    // Strip a leading path (e.g. /usr/bin/grep -> grep, ./scan -> scan).
    const verb = firstToken.split("/").pop() ?? firstToken;
    if (!READ_SEARCH_VERBS.has(verb)) return false;
  }
  return true;
}

/**
 * Is this tool call in scope for the guard under the given mode? minni_* / MCP
 * and any non-listed tool are NEVER in scope (defense-in-depth: even though hook
 * matcher only registers Grep|Read|Glob|Bash, we re-check the name here).
 */
export function isToolInScope(
  mode: RecallGuardMode,
  toolName: string,
  toolInput: Record<string, unknown>,
): boolean {
  if (mode === "off") return false;
  // Never guard minni_* / MCP tools, whatever the matcher does.
  if (toolName.startsWith("minni_") || toolName.startsWith("mcp__")) return false;

  if (CORE_SCOPE_TOOLS.has(toolName)) return true;

  if (toolName === "Bash") {
    if (mode !== "strict") return false; // soft mode leaves Bash untouched
    const command =
      toolInput && typeof toolInput.command === "string" ? toolInput.command : "";
    return isReadSearchBashCommand(command);
  }

  return false;
}

export interface PreToolUseDecisionOutput {
  continue: boolean;
  hookSpecificOutput?: {
    hookEventName: "PreToolUse";
    permissionDecision: "deny";
    permissionDecisionReason: string;
  };
}

/** The terse no-op: normal permission flow proceeds (NO permissionDecision). */
export function preToolUseAllow(): PreToolUseDecisionOutput {
  return { continue: true };
}

/**
 * Build the deny reason block: the top recall hits (title + wikilink + score)
 * followed by the instruction to consult memory and re-issue the exact call.
 */
export function buildGuardDenyReason(state: RecallState): string {
  const lines = state.top_hits
    .slice(0, 5)
    .map((hit) => `  - ${hit.title} ${hit.wikilink} (score ${hit.score.toFixed(2)})`);
  return (
    "📓 Minni recall guard: you have UNCONSULTED recall for this turn.\n" +
    `Top ${lines.length} relevant ${lines.length === 1 ? "memory" : "memories"}:\n` +
    `${lines.join("\n")}\n` +
    "You have unconsulted recall for this turn — read these (or call minni_recall) " +
    "before searching from scratch. Re-issue this exact call to proceed."
  );
}

export function preToolUseDeny(state: RecallState): PreToolUseDecisionOutput {
  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: buildGuardDenyReason(state),
    },
  };
}

/**
 * Pure decision: given the current state + mode + threshold + tool call, should
 * the guard FIRE (deny) or ALLOW (no-op)? Does NOT mutate state and does NO I/O;
 * the caller writes `consumed=true` back after a "deny" verdict.
 *
 * FIRE only if: state exists AND consumed===false AND top_score>=threshold AND
 * mode!=off AND the tool is in scope. ALLOW otherwise — in particular when
 * consumed===true (the idempotent re-issue path).
 */
export function decideGuard(args: {
  state: RecallState | null;
  mode: RecallGuardMode;
  threshold: number;
  toolName: string;
  toolInput: Record<string, unknown>;
}): "deny" | "allow" {
  const { state, mode, threshold, toolName, toolInput } = args;
  if (mode === "off") return "allow";
  if (!state) return "allow";
  if (state.consumed === true) return "allow"; // idempotent re-issue ALWAYS passes
  if (!(typeof state.top_score === "number" && state.top_score >= threshold)) return "allow";
  if (!isToolInScope(mode, toolName, toolInput)) return "allow";
  return "deny";
}

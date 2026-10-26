// Slice s6 + S2b: the PreToolUse recall guard (the BACKSTOP layer).
//
// When the agent reaches for a COLD search tool (Grep/Read/Glob, or a
// read/search Bash command) on a turn where strong recall EXISTS and is
// UNCONSULTED, this guard intercepts that tool call ONCE and surfaces the
// recall — nudging the agent to consult its own memory before searching from
// scratch. s5/S2b (recall-state.ts) writes the strong recall to
// <vault>/.runtime/recall-state.d/<session_key>.json (and legacy mirror
// <vault>/.runtime/recall-state.json); this module reads it and decides.
//
// CONTRACT (S2b §6.3.3, verified against CC docs):
//   - On hosts whose wire declares `preToolUseContext: true` (Claude Code), we
//     surface the recall non-blockingly via `hookSpecificOutput.additionalContext`:
//       {"continue":true,"hookSpecificOutput":{"hookEventName":"PreToolUse",
//         "additionalContext":"<text>"}}
//     with NO `permissionDecision`, so the tool call proceeds without a retry.
//   - On hosts without `preToolUseContext` (or when `MINNI_RECALL_GUARD_DELIVERY=deny`
//     or `MINNI_GUARD_V2=off`), we fall back to `permissionDecision: "deny"`:
//       {"continue":true,"hookSpecificOutput":{"hookEventName":"PreToolUse",
//         "permissionDecision":"deny","permissionDecisionReason":"<text>"}}
//   - To ALLOW/no-op we emit a bare {continue:true} (NO permissionDecision) so
//     the user's normal permission flow proceeds. We NEVER emit
//     permissionDecision: "allow" (that would skip the user's prompt).
//
// IDEMPOTENCY (load-bearing): the guard surfaces AT MOST ONCE per arming. On
// the first surfacing it claims the arming (`claimArming` with O_CREAT|O_EXCL)
// and flips `consumed` to true in the state file; once claimed/consumed the
// guard ALLOWS for ALL subsequent tool calls in the batch and turn.
import type { RecallState } from "./recall-state.js";
import { sanitizeRecallField } from "./recall-state.js";

/** PreToolUse is NOT an EnvelopeEvent — its output is the PreToolUse hookSpecificOutput shape. */
export const PRE_TOOL_USE_EVENT = "PreToolUse";

export type RecallGuardMode = "off" | "soft" | "strict";

/**
 * Bare cold search/read tools the guard scopes in every (non-off) mode.
 * Includes Claude Code names and Grok Build natives (Read→read_file, Grep→grep,
 * Glob/ListDir→list_dir). Adapters map hosts onto Claude names when they can;
 * dual listing keeps the guard live if a native name still reaches decideGuard.
 */
export const CORE_SCOPE_TOOLS = new Set([
  "Grep",
  "Read",
  "Glob",
  "grep",
  "read_file",
  "list_dir",
]);

/** Shell tools guarded in strict mode when the command is pure read/search. */
export const SHELL_SCOPE_TOOLS = new Set(["Bash", "run_terminal_command"]);

/** Subagent spawn tools whose PreToolUse call stashes the task prompt for SubagentStart (§6.3.6). */
export const SUBAGENT_SPAWN_TOOLS = new Set(["Agent", "Task"]);

const CONSULT_TOOL_RE = /(?:^|__)minni_(?:recall|drill)$/;

/** S2b (§6.3.4): true when toolName is bare or MCP-prefixed minni_recall / minni_drill. */
export function isConsultTool(toolName: string): boolean {
  return typeof toolName === "string" && CONSULT_TOOL_RE.test(toolName.trim());
}

/** S2b (§6.3.6): true when toolName is a subagent spawn tool (Agent / Task). */
export function isSubagentSpawnTool(toolName: string): boolean {
  return typeof toolName === "string" && SUBAGENT_SPAWN_TOOLS.has(toolName.trim());
}

/**
 * S2b (§6.3.7): build the PreToolUse regex matcher for hooks/hooks.json from
 * the guard's scope sets, consult tools, and subagent spawn tools.
 */
export function buildPreToolUseMatcher(): string {
  const literalTools = [
    ...CORE_SCOPE_TOOLS,
    ...SHELL_SCOPE_TOOLS,
    ...SUBAGENT_SPAWN_TOOLS,
  ];
  return `^(?:${literalTools.join("|")}|(?:mcp__.*__)?minni_(?:recall|drill))$`;
}

export const PRE_TOOL_USE_MATCHER = buildPreToolUseMatcher();

/**
 * Resolve the guard mode from config. Default "soft" (Grep/Read/Glob only; Bash
 * untouched). "strict" additionally guards read/search Bash; "off" disables it.
 * Anything unrecognized falls back to the default rather than guessing.
 *
 * PR90-1: MINNI_RECALL_GUARD_MODE is the master switch for the s6 PreToolUse
 * RECALL GUARD — set it to "off" to disable the guard backstop. This
 * is DISTINCT from MINNI_LIFECYCLE_NUDGE_MODE (agent_envelope.ts), which only
 * governs the lifecycle representation/nudge and does NOT control this guard.
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
 * Is this tool call in scope for the cold-search guard under the given mode?
 * minni_* / MCP, Agent/Task, and any non-listed tool are NEVER in scope here
 * (hooks.json matcher also admits consult and subagent spawn tools so
 * handlePreToolUse can consume armings or stash subagent task prompts, while
 * this helper restricts cold-search guard surfacing to CORE_SCOPE_TOOLS and
 * strict read/search Bash).
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

  // Bash (Claude) and run_terminal_command (Grok native; adapter maps to Bash).
  if (SHELL_SCOPE_TOOLS.has(toolName)) {
    if (mode !== "strict") return false; // soft mode leaves shell untouched
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
    permissionDecision?: "deny";
    permissionDecisionReason?: string;
    additionalContext?: string;
  };
}

/** Max hits included in non-blocking PreToolUse additionalContext delivery (S2b §6.3.3). */
export const GUARD_CONTEXT_MAX_HITS = 3;

/** The terse no-op: normal permission flow proceeds (NO permissionDecision). */
export function preToolUseAllow(): PreToolUseDecisionOutput {
  return { continue: true };
}

/**
 * Build the non-blocking context block (S2b §6.3.3): up to 3 top recall hits
 * (title + wikilink + score) delivered via hookSpecificOutput.additionalContext
 * without denying the tool call.
 */
export function buildGuardContextReason(state: RecallState): string {
  const lines = state.top_hits
    .slice(0, GUARD_CONTEXT_MAX_HITS)
    .map(
      (hit) =>
        `  - "${sanitizeRecallField(hit.title)}" ${sanitizeRecallField(hit.wikilink)} ` +
        `(score ${hit.score.toFixed(2)})`,
    );
  return (
    "📓 Minni recall guard: you have UNCONSULTED recall for this turn.\n" +
    `Top ${lines.length} relevant ${lines.length === 1 ? "memory" : "memories"}:\n` +
    `${lines.join("\n")}\n` +
    "You have unconsulted recall for this turn — Consult minni_recall/minni_drill on these memories " +
    "so you do not re-derive settled context from scratch."
  );
}

export function preToolUseContext(state: RecallState): PreToolUseDecisionOutput {
  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      additionalContext: buildGuardContextReason(state),
    },
  };
}

/**
 * Build the deny reason block: the top recall hits (title + wikilink + score)
 * followed by the instruction to consult memory and re-issue the exact call.
 */
export function buildGuardDenyReason(state: RecallState): string {
  // H3: hit.title / hit.wikilink are untrusted vault content. Sanitize (strip
  // newlines/control chars, clamp length) and present each as a clearly-quoted
  // inert datum so a crafted title cannot introduce its own line that reads as a
  // fresh instruction. The imperative framing stays a fixed template.
  const lines = state.top_hits
    .slice(0, 5)
    .map(
      (hit) =>
        `  - "${sanitizeRecallField(hit.title)}" ${sanitizeRecallField(hit.wikilink)} ` +
        `(score ${hit.score.toFixed(2)})`,
    );
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
 * the guard FIRE ("deny" verdict -> surface via context or deny) or ALLOW
 * (no-op)? Does NOT mutate state and does NO I/O; the caller claims the arming
 * and writes `consumed=true` back after a "deny" verdict.
 *
 * FIRE only if: state exists AND consumed===false AND top_score>=threshold AND
 * mode!=off AND not subagent context AND not already surfaced in soft mode AND
 * the tool is in scope. ALLOW otherwise.
 */
export function decideGuard(args: {
  state: RecallState | null;
  mode: RecallGuardMode;
  threshold: number;
  toolName: string;
  toolInput: Record<string, unknown>;
  guardV2?: boolean;
  agentId?: string;
}): "deny" | "allow" {
  const { state, mode, threshold, toolName, toolInput, guardV2 = true, agentId } = args;
  if (mode === "off") return "allow";
  if (guardV2 && typeof agentId === "string" && agentId.trim().length > 0) return "allow";
  if (!state) return "allow";
  if (state.consumed === true) return "allow"; // idempotent re-issue ALWAYS passes
  if (
    guardV2 &&
    mode === "soft" &&
    Array.isArray(state.pointer_named) &&
    state.pointer_named.some((item) => typeof item === "string" && item.trim().length > 0)
  ) {
    return "allow";
  }
  if (!(typeof state.top_score === "number" && state.top_score >= threshold)) return "allow";
  if (!isToolInScope(mode, toolName, toolInput)) return "allow";
  return "deny";
}

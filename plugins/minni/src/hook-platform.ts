// Per-platform hook wire contracts.
//
// The six agent platforms Minni hooks into do NOT share a hook contract. The
// historical failure mode was emitting Claude Code's wire shape everywhere and
// assuming it landed: on Grok Build the stdout of passive events is discarded
// outright, on Codex a PreCompact `hookSpecificOutput` fails schema validation
// and voids the whole output, and agy rejects `decision: "approve"` entirely.
// Every one of those platforms fails SILENTLY -- the hook runs, does its vault
// work, writes its audit entry, and the memory never reaches the model.
//
// So handlers no longer emit wire shapes. They express INTENT
// (see ./hook-intent.js) and a PlatformWire renders it natively, or reports
// that the platform cannot express it. A dropped injection is recorded rather
// than silently swallowed.
//
// Contract sources and per-platform caveats: docs/contracts/hook-platforms.md.
// Re-verify on upgrade -- no vendor publishes a hook deprecation policy.
import type { EnvelopeEvent } from "./agent_envelope.js";
import type { HookIntent } from "./hook-intent.js";
import { asString } from "./hook-utils.js";

/**
 * Why an intent could not be rendered. Surfaced to the audit log so a platform
 * that structurally cannot carry memory is visible in the record instead of
 * looking like a clean run.
 */
export interface DroppedIntent {
  event: string;
  reason: string;
}

export interface PlatformWire {
  /** Platform id, matching the agent identity (e.g. "codex", "grok-build"). */
  readonly id: string;
  /** Native JSON for "ran, nothing to say". */
  noop(): object;
  /**
   * Native JSON injecting `text` into the model's context at `event`, or null
   * if this platform cannot inject there. Null is a real answer, not a failure.
   */
  inject(event: EnvelopeEvent, text: string): object | null;
  /**
   * Native JSON surfacing a human-facing note (not model context), or null if
   * the platform has no such channel on this event.
   */
  note(event: EnvelopeEvent, text: string): object | null;
  /**
   * Text describing what the turn was about, from a Stop payload. Used to key
   * the Stop-time learn candidates.
   *
   * NB: the codebase read `last_user_message` here for a long time. No VENDOR
   * sends that field, so it always resolved to "" and every learn candidate
   * degraded to a bare session id. Claude Code and Codex send
   * `last_assistant_message`; Grok Build sends `lastAssistantMessage`. agy
   * sends nothing at all, so we synthesize it from its transcript -- which is
   * why that one profile legitimately reads `last_user_message`.
   */
  lastTaskText(payload: Record<string, unknown>): string;
  /**
   * Per-platform gate: should this dispatch run at all?
   *
   * Some platforms fire an event more than once per logical occurrence. Default
   * is "always run"; a profile overrides it to suppress the duplicates.
   */
  shouldHandle?(event: string, payload: Record<string, unknown>): boolean;
}

/** Claude Code's `hookSpecificOutput.additionalContext` envelope. */
function claudeShapedInject(event: EnvelopeEvent, text: string): object {
  return {
    continue: true,
    hookSpecificOutput: { hookEventName: event, additionalContext: text },
  };
}

const snakeAssistantMessage = (payload: Record<string, unknown>): string =>
  asString(payload.last_assistant_message);

// --- Claude Code -----------------------------------------------------------
// Injects on SessionStart, UserPromptSubmit and Stop. PreCompact is absent from
// the documented hookSpecificOutput union -- it can block compaction, nothing
// more.
const CLAUDE_INJECTABLE: ReadonlySet<EnvelopeEvent> = new Set([
  "SessionStart",
  "UserPromptSubmit",
  "Stop",
]);

export const claudeCodeWire: PlatformWire = {
  id: "claude-code",
  noop: () => ({ continue: true }),
  inject: (event, text) =>
    CLAUDE_INJECTABLE.has(event) ? claudeShapedInject(event, text) : null,
  note: (_event, text) => ({ continue: true, systemMessage: text }),
  lastTaskText: snakeAssistantMessage,
};

// --- Codex -----------------------------------------------------------------
// Codex models itself as a Claude-compatibility layer, so the envelope shape is
// shared -- but its generated JSON Schemas are `additionalProperties: false`
// and diverge in two places: Stop output has NO hookSpecificOutput key (block
// only), and PreCompact can neither inject nor block. Emitting Claude's shape
// on either voids the entire output.
const CODEX_INJECTABLE: ReadonlySet<EnvelopeEvent> = new Set([
  "SessionStart",
  "UserPromptSubmit",
]);

export const codexWire: PlatformWire = {
  id: "codex",
  noop: () => ({ continue: true }),
  inject: (event, text) =>
    CODEX_INJECTABLE.has(event) ? claudeShapedInject(event, text) : null,
  note: (_event, text) => ({ continue: true, systemMessage: text }),
  lastTaskText: snakeAssistantMessage,
};

// --- Grok Build (xAI) ------------------------------------------------------
// "For passive events, stdout is ignored; exit 0 on success." Only PreToolUse
// and Stop/SubagentStop parse hook output at all, so Stop is the ONLY event on
// which memory can reach the model. Session-start hydration has to travel by
// skills/instructions/MCP instead -- hooks structurally cannot carry it.
const GROK_INJECTABLE: ReadonlySet<EnvelopeEvent> = new Set(["Stop"]);

export const grokBuildWire: PlatformWire = {
  id: "grok-build",
  noop: () => ({ continue: true }),
  inject: (event, text) =>
    GROK_INJECTABLE.has(event) ? claudeShapedInject(event, text) : null,
  // Passive-event stdout is discarded, so a note only lands on the Stop gate.
  note: (event, text) =>
    GROK_INJECTABLE.has(event) ? { continue: true, systemMessage: text } : null,
  lastTaskText: (payload) =>
    // Grok's envelope is camelCase throughout.
    asString(payload.lastAssistantMessage) || snakeAssistantMessage(payload),
  // Grok fires an EXTRA observe-only Stop at session end (reason
  // "channel_closed" / "shutdown") on top of the real end-of-turn one, and its
  // docs say to filter on end_turn. Without this every Grok session wrote a
  // second stop_candidates inbox entry for the same turn.
  shouldHandle: (event, payload) => {
    if (event !== "Stop") return true;
    const reason = asString(payload.reason);
    return reason === "" || reason === "end_turn";
  },
};

// --- Kilocode --------------------------------------------------------------
// Kilocode (an opencode fork) has NO command-hook system -- it loads in-process
// JS plugins. So the consumer of this process's stdout is not Kilocode itself
// but Minni's own bridge plugin (kilo/minni-plugin.js), which spawns the hook
// script and reads `hookSpecificOutput.additionalContext || systemMessage`,
// then pushes the result into opencode's `output.system` / `output.context`.
//
// The bridge is therefore the wire contract here, and it is deliberately
// Claude-shaped because both ends are ours. Note the consequence: Kilocode is
// the ONE platform that can inject at PreCompact, because opencode's
// `experimental.session.compacting` accepts replacement context.
const KILOCODE_INJECTABLE: ReadonlySet<EnvelopeEvent> = new Set([
  "SessionStart",
  "UserPromptSubmit",
  "PreCompact",
]);

export const kilocodeWire: PlatformWire = {
  id: "kilocode",
  noop: () => ({ continue: true }),
  inject: (event, text) =>
    KILOCODE_INJECTABLE.has(event) ? claudeShapedInject(event, text) : null,
  // The bridge falls back to systemMessage when no additionalContext is present.
  note: (_event, text) => ({ continue: true, systemMessage: text }),
  lastTaskText: snakeAssistantMessage,
};

// --- Gemini / Antigravity (agy CLI) ----------------------------------------
// agy shares NOTHING with Claude Code's output shape. Context is injected with
// `injectSteps`, documented at https://antigravity.google/docs/hooks:
//     { "injectSteps": [ { "ephemeralMessage": "Remember to lint" } ] }
//
// Its events are its own too: there is no UserPromptSubmit and no PreCompact.
// `PreInvocation` (fires before the model is called) is the real prompt-submit
// analogue and the documented injection point; gemini-hook.ts maps agy's event
// names onto Minni's internal ones.
//
// SessionStart is real on agy 1.1.7 -- it dispatches with a full payload -- but
// is absent from every Google-published page, so injecting there is riding on
// undocumented behavior. Kept, because losing boot hydration is the worse of
// the two risks.
const AGY_INJECTABLE: ReadonlySet<EnvelopeEvent> = new Set([
  "SessionStart",
  "UserPromptSubmit", // rendered onto agy's PreInvocation
]);

export const geminiWire: PlatformWire = {
  id: "gemini",
  // agy tolerates an empty object on passive events; PreToolUse is the one
  // event that must never emit an empty decision (see gemini-adapter.ts).
  noop: () => ({}),
  inject: (event, text) =>
    AGY_INJECTABLE.has(event) ? { injectSteps: [{ ephemeralMessage: text }] } : null,
  // agy has no systemMessage channel, and `injectSteps` is NOT universal --
  // its Stop output is a different proto (decision/reason) that rejects the
  // field outright. Caught live on agy 1.1.7:
  //     failed to unmarshal result from hook jsonhook__minni_Stop_0_0 via
  //     protojson: ... unknown field "injectSteps"
  // So a note only lands where injectSteps is actually valid; anywhere else it
  // is dropped and recorded rather than emitted into a parse error.
  note: (event, text) =>
    AGY_INJECTABLE.has(event) ? { injectSteps: [{ ephemeralMessage: text }] } : null,
  lastTaskText: (payload) =>
    // agy's Stop payload carries no task text at all. gemini-adapter.ts
    // back-fills `last_user_message` from agy's own transcript_full.jsonl, so
    // here -- uniquely -- that field is real and synthesized by us.
    asString(payload.last_user_message) || snakeAssistantMessage(payload),
};

// --- Cursor ----------------------------------------------------------------
// Cursor's hook output is FLAT and per-event: each event validates a different
// small set of fields and ignores everything else. It shares nothing with
// Claude Code's envelope.
//
// Injection is possible at sessionStart ONLY. beforeSubmitPrompt is documented
// as accepting `continue` + `user_message` and nothing else -- there are open
// vendor requests to add `additional_context`, and the bundle validates the
// field, but relying on undocumented behavior is the exact mistake this module
// exists to prevent. preCompact takes only `user_message`; stop only
// `followup_message`.
const CURSOR_INJECTABLE: ReadonlySet<EnvelopeEvent> = new Set(["SessionStart"]);

export const cursorWire: PlatformWire = {
  id: "cursor",
  noop: () => ({ continue: true }),
  inject: (event, text) =>
    CURSOR_INJECTABLE.has(event) ? { additional_context: text } : null,
  // Cursor has NO safe human-facing note channel.
  //
  // `followup_message` is the only field `stop` accepts, and it is NOT
  // informational -- it is Cursor's AUTO-FOLLOW-UP mechanism: it drives another
  // agent turn, bounded by `loop_limit` (default 5). Using it to announce a
  // drafted learn candidate is self-feeding: Stop drafts -> follow-up turn ->
  // Stop drafts again. Observed live, 6 Stop hooks in 90 seconds from a single
  // prompt, each drafting an inbox candidate built from the audit trail the
  // previous one had just written.
  //
  // So notes are DROPPED here (and recorded as such). Announcing a candidate is
  // never worth spending the user's tokens on an extra model turn.
  note: () => null,
  // Cursor's stop payload carries status/loop_count/token counts and no message
  // text at all, so there is nothing to key the learn candidate on and it falls
  // back to the session id. Cursor does supply `transcript_path`; mining it the
  // way gemini-adapter mines agy's transcript would close this, but that is a
  // separate change.
  lastTaskText: () => "",
};

export interface RenderedIntent {
  /** Native JSON to write to stdout. */
  output: object;
  /** Set when the platform could not carry the intent. Record it; never drop silently. */
  dropped?: DroppedIntent;
}

/**
 * Render an intent through a platform's wire.
 *
 * An injection the platform cannot carry does NOT become a silent noop: it is
 * reported back so the caller can record it. That is the whole point -- a
 * platform structurally unable to carry memory should be legible in the audit
 * log, not indistinguishable from a healthy run.
 */
export function renderIntent(wire: PlatformWire, intent: HookIntent): RenderedIntent {
  if (intent.kind === "none") return { output: wire.noop() };

  if (intent.kind === "note") {
    // Notes are addressed to the human; there is no event context to place
    // them against, so probe the platform's most permissive channel.
    const noted = wire.note("Stop", intent.text);
    return noted
      ? { output: noted }
      : {
          output: wire.noop(),
          dropped: { event: "note", reason: `${wire.id} has no human-facing note channel` },
        };
  }

  const injected = wire.inject(intent.event, intent.text);
  if (injected) return { output: injected };

  return {
    output: wire.noop(),
    dropped: {
      event: intent.event,
      reason: `${wire.id} cannot inject context on ${intent.event}`,
    },
  };
}

const WIRES: ReadonlyArray<PlatformWire> = [
  claudeCodeWire,
  codexWire,
  grokBuildWire,
  kilocodeWire,
  geminiWire,
  cursorWire,
];

/**
 * Resolve a wire by platform id, falling back to the Claude Code shape.
 *
 * The fallback is deliberate but narrow: an unknown platform is far more likely
 * to be a Claude-contract clone (Codex and Grok Build both are) than anything
 * else. It is still a guess -- add a profile rather than relying on it.
 */
export function wireFor(id: string): PlatformWire {
  return WIRES.find((wire) => wire.id === id) ?? claudeCodeWire;
}

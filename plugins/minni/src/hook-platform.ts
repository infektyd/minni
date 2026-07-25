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
   * The assistant's final message for the turn, from a Stop payload.
   *
   * NB: `last_user_message` exists on NO platform -- it was read here for a
   * long time and always resolved to empty, degrading every Stop-time learn
   * candidate to a bare session id. Every platform supplies the *assistant's*
   * message instead, under three different spellings.
   */
  lastAssistantMessage(payload: Record<string, unknown>): string;
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
  lastAssistantMessage: snakeAssistantMessage,
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
  lastAssistantMessage: snakeAssistantMessage,
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
  lastAssistantMessage: (payload) =>
    // Grok's envelope is camelCase throughout.
    asString(payload.lastAssistantMessage) || snakeAssistantMessage(payload),
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
  lastAssistantMessage: snakeAssistantMessage,
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

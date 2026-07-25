// What a hook handler MEANS, independent of any platform's wire format.
//
// Handlers return these. A PlatformWire (./hook-platform.js) renders them into
// the native shape for the platform actually running, or reports that it
// cannot. Keeping intent and wire apart is what stops one platform's contract
// leaking into another's -- the bug class that left agy, Grok Build and
// Kilocode silently carrying no memory at all.
import type { EnvelopeEvent } from "./agent_envelope.js";

export type HookIntent =
  /** Put `text` into the model's context. The point of the whole system. */
  | { kind: "inject"; event: EnvelopeEvent; text: string }
  /** Tell the human something. Not model-visible; never a memory carrier. */
  | { kind: "note"; text: string }
  /** Ran fine, nothing to say. */
  | { kind: "none" };

export const injectIntent = (event: EnvelopeEvent, text: string): HookIntent => ({
  kind: "inject",
  event,
  text,
});

export const noteIntent = (text: string): HookIntent => ({ kind: "note", text });

export const noIntent: HookIntent = { kind: "none" };

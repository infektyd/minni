// Shared hook plumbing (review panel, plan-parity follow-up): the four hook
// entrypoints (hook.ts, codex-hook.ts, grok-hook.ts, kilocode-hook.ts) share
// this protocol boilerplate byte-for-byte. Per-hook differences are only the
// config constants and the handler implementations — keep them there.
import type { EnvelopeEvent } from "./agent_envelope.js";
import type { VaultSearchResult } from "./vault.js";

export interface HookOutput {
  continue?: boolean;
  hookSpecificOutput?: {
    hookEventName: EnvelopeEvent;
    additionalContext: string;
  };
  systemMessage?: string;
}

export const VALID_EVENTS: ReadonlyArray<EnvelopeEvent> = [
  "SessionStart",
  "UserPromptSubmit",
  "PreCompact",
  "Stop",
];

export async function readStdin(): Promise<unknown> {
  if (process.stdin.isTTY) return {};
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => {
      if (!data.trim()) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(data));
      } catch {
        resolve({});
      }
    });
    process.stdin.on("error", () => resolve({}));
  });
}

// Accepts both the envelope HookOutput and the s6 PreToolUse permissionDecision
// shape (a structurally different object); both are plain JSON-serializable, so
// the param is the broad `object` rather than a union that would couple this
// protocol leaf to the guard module.
export function emit(output: object): void {
  process.stdout.write(`${JSON.stringify(output)}\n`);
}

export function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function workspaceFromPayload(
  payload: Record<string, unknown>,
  fallback: string,
): string {
  return (
    asString(payload.workspace_id) ||
    asString(payload.workspaceId) ||
    asString(payload.cwd) ||
    asString(payload.working_directory) ||
    fallback
  );
}

export function vaultRecallToBody(vault: VaultSearchResult[]): unknown {
  return vault.slice(0, 6).map((result) => ({
    wikilink: result.wikilink,
    score: result.score,
    snippet: result.snippet.replace(/\s+/g, " ").slice(0, 240),
  }));
}

export function withHookContext(event: EnvelopeEvent, additionalContext: string): HookOutput {
  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: event,
      additionalContext,
    },
  };
}

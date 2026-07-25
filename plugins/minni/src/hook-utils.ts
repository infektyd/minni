// Shared hook plumbing (review panel, plan-parity follow-up): the four hook
// entrypoints (hook.ts, codex-hook.ts, grok-hook.ts, kilocode-hook.ts) share
// this protocol boilerplate byte-for-byte. Per-hook differences are only the
// config constants and the handler implementations — keep them there.
import fs from "node:fs";
import path from "node:path";
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
      try {
        resolve(JSON.parse(data));
      } catch {
        resolve({});
      }
    });
    process.stdin.on("error", () => resolve({}));
  });
}

// Helper to resolve sub-directories to canonical project root directory
export function findProjectRoot(dirPath: string): string {
  try {
    let curr = path.resolve(dirPath);
    const root = path.parse(curr).root;
    while (curr && curr !== root) {
      if (fs.existsSync(path.join(curr, ".git"))) {
        return curr;
      }
      const parent = path.dirname(curr);
      if (parent === curr) break;
      curr = parent;
    }
  } catch {
    // Return original path on any filesystem error
  }
  return path.resolve(dirPath);
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

/** Non-empty string entries of an array-valued payload field, else []. */
export function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
}

export function workspaceFromPayload(
  payload: Record<string, unknown>,
  fallback: string,
): string {
  const explicit = asString(payload.workspace_id) || asString(payload.workspaceId);
  if (explicit) return explicit;
  const rawCwd = asString(payload.cwd) || asString(payload.working_directory);
  if (rawCwd) return findProjectRoot(rawCwd);
  return fallback;
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

// Shared hook plumbing (review panel, plan-parity follow-up): the four hook
// entrypoints (hook.ts, codex-hook.ts, grok-hook.ts, kilocode-hook.ts) share
// this protocol boilerplate byte-for-byte. Per-hook differences are only the
// config constants and the handler implementations — keep them there.
import fs from "node:fs";
import os from "node:os";
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
      // Fast path for the empty-stdin case (hooks invoked with no payload):
      // JSON.parse("") would reach the same {} via the catch below, but the
      // explicit return states the intent instead of routing a normal, expected
      // condition through exception handling.
      if (!data.trim()) {
        resolve({});
        return;
      }
      try {
        const parsed: unknown = JSON.parse(data);
        // A hook payload is a JSON OBJECT. `null`, arrays and bare scalars all
        // PARSE successfully but are the wrong shape, and every caller casts the
        // result to Record<string, unknown> and dereferences it OUTSIDE its try
        // block — so a literal `null` on stdin crashed the hook with an uncaught
        // TypeError instead of degrading. Narrow to the only shape the callers
        // can use; anything else degrades to the same {} as unparseable input.
        resolve(
          parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
            ? parsed
            : {},
        );
      } catch {
        resolve({});
      }
    });
    process.stdin.on("error", () => resolve({}));
  });
}

/** Expand a leading `~` (alone or before a separator) to the user's home dir. */
function expandTilde(dirPath: string): string {
  const home = os.homedir();
  return home ? dirPath.replace(/^~(?=$|[/\\])/, home) : dirPath;
}

/**
 * Canonicalize an input path for the project-root walk: expand `~`, resolve to
 * absolute, then COLLAPSE SYMLINKS. path.resolve does not touch symlinks, so
 * without the realpath a directory reached two ways — `/w/link` where `link` ->
 * `/w/repo/src` — yields two different workspace labels and splits one
 * project's memory across two partitions. Paths not yet on disk make realpath
 * throw; they keep the plain resolution rather than failing outright.
 */
function canonicalizeDir(dirPath: string): string {
  const resolved = path.resolve(dirPath);
  try {
    return fs.realpathSync(resolved);
  } catch {
    return resolved;
  }
}

// Resolves a sub-directory to the canonical project root: the nearest ancestor
// holding a `.git` entry. Existence — not type — is tested on purpose: in git
// worktrees and submodules `.git` is a FILE pointing at the real gitdir, and
// those checkouts are project roots just as much as a classic `.git/` dir.
//
// INPUT CONTRACT — the argument must be ABSOLUTE (a leading `~` is expanded
// first). A relative path would be anchored to the HOOK PROCESS's cwd, which
// the harness controls rather than the workspace does, so the same session
// could label itself differently on two runs. There is no honest root for such
// an input, so this returns undefined and the caller falls back to its
// configured default (a stable label beats a cwd-dependent guess).
//
// The walk STOPS AT the user's home directory, so no DESCENDANT of $HOME is
// ever attributed to $HOME. A dotfiles repo at $HOME is common, and without the
// stop every non-repo directory under $HOME would resolve to $HOME — collapsing
// unrelated projects into one workspace label and cross-contaminating their
// memory. $HOME ITSELF still maps to itself: that is the identity result, not a
// collapse (no other input can produce it), and for a session actually running
// in a home-directory repo it is the only correct label.
//
// Deliberately SYNCHRONOUS: hook processes are single-shot and short-lived, so
// this runs once per event and the async plumbing would cost readability
// without buying any concurrency.
export function findProjectRoot(dirPath: string): string | undefined {
  const expanded = expandTilde(dirPath);
  if (!path.isAbsolute(expanded)) return undefined;
  const base = canonicalizeDir(expanded);
  try {
    let curr = base;
    const root = path.parse(curr).root;
    // Empty homedir (unset $HOME, no passwd entry) must NOT become path.resolve("")
    // — that is cwd, which would silently halt the walk at the process's own dir.
    // The ceiling is canonicalized like the input: comparing a realpath'd walk
    // against a symlinked $HOME would never match, and the walk would run past it.
    const rawHome = os.homedir();
    const home = rawHome ? canonicalizeDir(rawHome) : "";
    while (curr && curr !== root && curr !== home) {
      if (fs.existsSync(path.join(curr, ".git"))) {
        return curr;
      }
      const parent = path.dirname(curr);
      if (parent === curr) break;
      curr = parent;
    }
  } catch {
    // Return the canonicalized input path on any filesystem error
  }
  return base;
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
  // findProjectRoot returns undefined for a cwd it cannot anchor
  // deterministically (see its INPUT CONTRACT) — fall back rather than mint a
  // label that depends on where the hook process happened to be launched.
  if (rawCwd) return findProjectRoot(rawCwd) ?? fallback;
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

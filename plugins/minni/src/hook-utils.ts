// Shared hook plumbing: stdin/stdout leaf helpers common to every entrypoint
// (hook.ts, codex-hook.ts, grok-hook.ts, kilocode-hook.ts, cursor-hook.ts).
//
// Only genuinely platform-NEUTRAL helpers belong here. Anything describing what
// a platform accepts on the wire goes in hook-platform.ts — this module used to
// hold Claude Code's output shape and quietly made it everyone's. Per-hook
// differences stay in the entrypoints and factory handlers.
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
  // Claude Code's native post-compaction event — delivers the finished
  // summary as `compact_summary` (primary harvest path).
  "PostCompact",
  // Post-compaction summary delivery from platforms whose bridge reads the
  // summary back itself (Kilo SDK read-back through the native plugin).
  "CompactSummary",
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
 * Platforms whose default filesystem is case-INSENSITIVE, so `…/Projects` and
 * `…/projects` name one directory and must produce one workspace label.
 */
const CASE_INSENSITIVE_PLATFORM = process.platform === "darwin" || process.platform === "win32";

/**
 * Canonicalize an input path for the project-root walk: expand `~`, resolve to
 * absolute, then COLLAPSE SYMLINKS. path.resolve does not touch symlinks, so
 * without the realpath a directory reached two ways — `/w/link` where `link` ->
 * `/w/repo/src` — yields two different workspace labels and splits one
 * project's memory across two partitions. Paths not yet on disk make realpath
 * throw; they keep the plain resolution rather than failing outright.
 *
 * On darwin/win32 the JS realpath is NOT enough: it collapses symlinks but
 * preserves whatever casing the caller typed, so on APFS one directory reached
 * as `…/Projects/Minni` and as `…/projects/minni` still yielded two labels.
 * `realpathSync.native` delegates to the OS, which returns the TRUE ON-DISK
 * casing — the same answer for every spelling of the path.
 *
 * Chosen over the cheaper `.toLowerCase()`: the label is a memory PARTITION
 * KEY, so it has to be stable, and correcting it a second time would orphan
 * history twice. On-disk casing is a property of the filesystem, not of the
 * caller, so it is the fixed point; a lowercased label would additionally have
 * to be un-lowercased to be used as a path anywhere. The cost is one extra
 * syscall path per hook process, which runs once per event.
 */
function canonicalizeDir(dirPath: string): string {
  const resolved = path.resolve(dirPath);
  if (CASE_INSENSITIVE_PLATFORM) {
    try {
      return fs.realpathSync.native(resolved);
    } catch {
      // fall through to the portable realpath, then to the plain resolution
    }
  }
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

/**
 * Default internal time budget for a prompt-time hook, in ms.
 *
 * This exists because the HARNESS deadline is not ours to negotiate: Claude Code
 * kills a UserPromptSubmit hook at 30s and DISCARDS its output entirely ("hook
 * timed out after 30s — output discarded"), so an overrun costs the whole turn's
 * recall injection, corrections matching and active-plan pointer — silently.
 * A hook that is merely slow is worse than a hook that gives up: the slow one
 * loses everything, the quitter still emits the pointers it already has.
 *
 * 8s is chosen well under the 30s kill so there is room left to write the
 * envelope and audit after the budget trips, and above the warm-daemon search
 * p95 (~4s observed) so a healthy daemon is never cut off. The pathological case
 * this bounds is a COLD daemon: the first search after a restart lazily loads
 * the embedder, cross-encoder and FAISS index — a path that also makes live
 * HuggingFace HTTP calls — and can exceed 30s on its own.
 *
 * Override with MINNI_HOOK_BUDGET_MS.
 */
export const DEFAULT_HOOK_BUDGET_MS = 8000;

/** The internal hook time budget in ms (MINNI_HOOK_BUDGET_MS, else the default). */
export function hookBudgetMs(env: NodeJS.ProcessEnv = process.env): number {
  const raw = Number(env.MINNI_HOOK_BUDGET_MS);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_HOOK_BUDGET_MS;
}

/**
 * Fraction of a platform's HARNESS timeout the internal budget may occupy.
 *
 * The budget has to finish AND leave room for what follows it: building the
 * envelope, writing recall-state, and the audit append. 0.6 keeps ~40% of the
 * manifest timeout as that tail. This matters most where the manifest is tight
 * — Gemini's `PreInvocation` is 10s, so the flat 8s default would leave only 2s
 * and the hook could still be killed after paying its whole budget.
 */
export const HOOK_BUDGET_HARNESS_FRACTION = 0.6;

/**
 * The internal budget for a hook whose harness kills it at `harnessTimeoutMs`.
 *
 * Takes the TIGHTER of the configured budget and the harness-derived ceiling, so
 * MINNI_HOOK_BUDGET_MS can always shrink the budget but can never push it past
 * the deadline it exists to stay inside. Omit `harnessTimeoutMs` (platform with
 * no declared bound) to get the configured budget unchanged.
 */
export function effectiveHookBudgetMs(
  harnessTimeoutMs?: number,
  env: NodeJS.ProcessEnv = process.env,
): number {
  const configured = hookBudgetMs(env);
  if (
    harnessTimeoutMs === undefined ||
    !Number.isFinite(harnessTimeoutMs) ||
    harnessTimeoutMs <= 0
  ) {
    return configured;
  }
  const ceiling = Math.floor(harnessTimeoutMs * HOOK_BUDGET_HARNESS_FRACTION);
  return Math.max(1, Math.min(configured, ceiling));
}

/**
 * Resolve `work` with `fallback` if it has not settled within `ms`.
 *
 * BELT-AND-BRACES ONLY. Racing a promise does not make a node process exit — an
 * in-flight socket handle keeps the event loop alive long after the winner
 * resolves — so the load-bearing deadline is the one threaded down into the
 * JSON-RPC call itself (`timeoutMs`), which destroys the socket. This wrapper
 * covers the stalls that are NOT the daemon socket (a wedged filesystem under
 * the vault search, say), where nothing else would give up.
 */
export function withBudget<T>(work: Promise<T>, ms: number, fallback: T): Promise<T> {
  if (ms <= 0) return Promise.resolve(fallback);
  return new Promise<T>((resolve) => {
    // unref so this timer alone never holds the process open: if `work` settles
    // and the loop is otherwise drained, the hook must exit immediately.
    const timer = setTimeout(() => resolve(fallback), ms);
    timer.unref?.();
    work.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      () => {
        clearTimeout(timer);
        resolve(fallback);
      },
    );
  });
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

/**
 * Vault dir slug -> canonical agent id. A VERBATIM mirror of
 * `_VAULT_SLUG_TO_AGENT_ID` in src/minni/afm_passes/inbox_ingest.py; the two
 * tables must be edited together.
 */
const VAULT_SLUG_TO_AGENT_ID: Readonly<Record<string, string>> = {
  claudecode: "claude-code",
  codex: "codex",
  gemini: "gemini",
  hermes: "hermes",
  kilocode: "kilocode",
  openclaw: "openclaw",
  "grok-build": "grok-build",
  "grok-beta": "grok-build",
  grok: "grok-build",
};

/**
 * The agent id the INGEST will attribute a file in this vault's inbox to —
 * i.e. `_principal_for_inbox` (src/minni/afm_passes/inbox_ingest.py) computed
 * on this side, from the vault DIR NAME and nothing else.
 *
 * This exists because inbox_ingest cross-checks a file's `agent_id` against
 * that principal and DROPS the file (`_agent_mismatch`) on any disagreement.
 * A writer must therefore stamp the principal, not its own configured id: with
 * `MINNI_CLAUDECODE_AGENT_ID=claudecode` the two differ ("claudecode" vs
 * "claude-code") and every candidate is silently discarded. Deriving both
 * sides from the same input makes them agree by construction.
 *
 * Deliberately NOT `getAgentIdFromVaultPath` (vault.ts): that one answers "who
 * owns this vault" and consults MINNI_AGENT_VAULTS / MINNI_*_VAULT_PATH env
 * mappings the Python side never sees, so it can return an id for a directory
 * whose NAME implies a different principal.
 *
 * Returns undefined when the dir has no `-vault` suffix (e.g. the daemon's own
 * `~/.minni/vault`): Python then falls back to a principal this side cannot
 * know, so there is no honest stamp to write — and an ABSENT `agent_id` skips
 * the cross-check entirely rather than failing it.
 */
export function inboxPrincipalForVaultPath(vaultPath: string): string | undefined {
  const base = path.basename(path.resolve(expandTilde(vaultPath)));
  if (!base.endsWith("-vault")) return undefined;
  const slug = base.slice(0, -"-vault".length);
  if (!slug) return undefined;
  return VAULT_SLUG_TO_AGENT_ID[slug] ?? slug;
}

export function vaultRecallToBody(vault: VaultSearchResult[]): unknown {
  return vault.slice(0, 6).map((result) => ({
    wikilink: result.wikilink,
    score: result.score,
    snippet: result.snippet.replace(/\s+/g, " ").slice(0, 240),
  }));
}

// `withHookContext` used to live here and hard-coded Claude Code's
// `hookSpecificOutput` envelope for EVERY platform. That is what silently voided
// Codex's PreCompact output and threw away Grok Build's memory entirely.
// Building that envelope is now a per-platform decision: see hook-platform.ts.

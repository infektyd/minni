// Retention of the use ledger (design rev5 §4.3 "Retention", slice S1b).
//
// `.runtime/use-ledger/<session_key>/` directories are removed once they are
// past the ledger's 90-day horizon AND the use detector holds no open work for
// the session. Removing a ledger whose windows are still open would lose the
// rows those windows' judgments are written against, so "idle" alone is not
// enough: the detector's per-session state has to agree.
//
// The sweep is modelled on use-capture.ts's `sweepSessionDirs`, which keeps
// `recall-session.d` on the same horizon:
//   - it runs once a SessionStart's output is settled (use-capture.ts
//     `scheduleSessionSweep` runs both sweeps);
//   - at most 20 directories per run, each renamed to `.trash-<ts>-<key>`
//     first, so a removal cut short leaves a name the next run finishes;
//   - a cooperative budget, checked between items, and a cursor in
//     `use-ledger/.sweep.json` so the next run resumes where this one stopped;
//     after a complete pass the next waits a day;
//   - the root is reached through the ledger's containment walk, and nothing in
//     it that is a symlink (or a plain file) is followed or removed.
// Readers of the ledger skip every name with a leading dot, so `.sweep.json`
// and `.trash-*` are invisible to them.
//
// The detector's state is READ here, never written: its writer is
// use-detector.ts (one writer per session, `windows.lock`). Its layout is
// mirrored below rather than imported, because importing use-detector.js would
// make every SessionStart load the detector; use-retention tests pin the
// layout against the real detector.
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { LEDGER_RELDIR, containedDir } from "./use-ledger.js";
import { assertWriteTargetUnder } from "./vault.js";

/** A ledger session directory idle this long may go (the ledger's horizon). */
export const LEDGER_IDLE_MAX_MS = 90 * 24 * 60 * 60 * 1000;
/** After a complete pass, the next sweep waits this long. */
export const LEDGER_SWEEP_INTERVAL_MS = 24 * 60 * 60 * 1000;
/** Cooperative budget for one run (§6.2.5), checked at every item boundary. */
export const LEDGER_SWEEP_BUDGET_MS = 150;
/** At most this many session directories are removed per run (§4.3). */
export const LEDGER_SWEEP_MAX_REMOVALS = 20;
/** A session directory holding more entries than this is never judged idle (bounds the lstat calls). */
const MAX_ENTRIES_PER_DIR = 256;
const SWEEP_STATE_FILE = ".sweep.json";
const SESSION_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const TRASH_RE = /^\.trash-\d{1,16}-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const MAX_SIDE_FILE_BYTES = 64 * 1024;

// use-detector.ts's per-session state: DETECTOR_RELDIR/sessions/<key>/windows.json,
// written under windows.lock. Mirrored, not imported (see the header).
const DETECTOR_SESSIONS = [".runtime", "use-detector", "sessions"];
const DETECTOR_STATE_FILE = "windows.json";
const DETECTOR_LOCK_FILE = "windows.lock";
/** use-detector.ts LOCK_STALE_MS: a lock younger than this belongs to a live run. */
const DETECTOR_LOCK_STALE_MS = 30_000;
/** use-detector.ts MAX_STATE_BYTES: a larger state file is not one the detector wrote. */
const MAX_DETECTOR_STATE_BYTES = 8 * 1024 * 1024;
const DETECTOR_STATE_VERSION = 1;

const READ_FLAGS =
  fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);

function errorCode(error: unknown): unknown {
  return (error as { code?: unknown } | null)?.code;
}

/**
 * A regular file's bytes, opened without following a link and without
 * blocking (a FIFO in its place cannot hang the hook). `missing` when it is
 * absent; undefined when it is not a regular file or is over `maxBytes`.
 * Throws on any other failure.
 */
function readRegularFile<M>(file: string, vaultPath: string, maxBytes: number, missing: M): Buffer | M | undefined {
  assertWriteTargetUnder(file, vaultPath);
  let fd: number;
  try {
    fd = fs.openSync(file, READ_FLAGS);
  } catch (error) {
    if (errorCode(error) === "ENOENT") return missing;
    throw error;
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > maxBytes) return undefined;
    const buffer = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < buffer.length) {
      const n = fs.readSync(fd, buffer, offset, buffer.length - offset, offset);
      if (n <= 0) break;
      offset += n;
    }
    return buffer.subarray(0, offset);
  } finally {
    fs.closeSync(fd);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** The detector's view of one session, as far as retention needs it. */
export type DetectorSessionState =
  /** No state: the detector never opened anything for this session. */
  | "none"
  /** State present, and every agent's windows and epoch are closed. */
  | "closed"
  /** Some window, epoch or unflushed reply is still open. */
  | "open"
  /** A run holds the session lock right now. */
  | "locked"
  /** Unreadable, refused, oversize, or a shape this reader does not know: never judged closed. */
  | "unknown";

/** Whether one agent's detector state still holds open work. */
function agentHasOpenWork(agent: unknown): boolean {
  if (!isRecord(agent)) return true;
  const windows = agent.windows;
  const exposures = agent.exposures;
  if (!Array.isArray(windows) || !Array.isArray(exposures)) return true;
  if (windows.length > 0 || exposures.length > 0) return true;
  // A turn whose Stop came before its reply reached the transcript: its
  // provisional judgments still wait on the next run.
  const turn = agent.turn;
  if (isRecord(turn) && turn.ended !== undefined) return true;
  return false;
}

/**
 * What the use detector holds for `sessionKey`: read-only, through the
 * containment walk, never following a link. Never throws; anything it cannot
 * read with certainty is "unknown", which retention treats as open.
 */
export function detectorSessionState(vaultPath: string, sessionKey: string, now: number = Date.now()): DetectorSessionState {
  try {
    if (!SESSION_KEY_RE.test(sessionKey)) return "unknown";
    const dir = containedDir(vaultPath, [...DETECTOR_SESSIONS, sessionKey], false);
    if (dir === null) return "none";
    try {
      const lock = fs.lstatSync(path.join(dir, DETECTOR_LOCK_FILE));
      // A stale lock is a crashed holder the detector breaks on its next run;
      // it says nothing about the state, so only a fresh one defers.
      if (now - lock.mtimeMs <= DETECTOR_LOCK_STALE_MS) return "locked";
    } catch (error) {
      if (errorCode(error) !== "ENOENT") return "unknown";
    }
    const bytes = readRegularFile(path.join(dir, DETECTOR_STATE_FILE), vaultPath, MAX_DETECTOR_STATE_BYTES, null);
    if (bytes === null) return "none";
    if (bytes === undefined) return "unknown";
    const raw: unknown = JSON.parse(bytes.toString("utf8"));
    if (!isRecord(raw) || raw.v !== DETECTOR_STATE_VERSION || raw.session_key !== sessionKey || !isRecord(raw.agents)) {
      return "unknown";
    }
    for (const agent of Object.values(raw.agents)) {
      if (agentHasOpenWork(agent)) return "open";
    }
    return "closed";
  } catch {
    return "unknown";
  }
}

interface SweepState {
  v: 1;
  /** No new pass starts before this (epoch ms). */
  next_after: number;
  /** The last name a cut-off pass handled; the next run resumes after it. */
  cursor: string | null;
}

export interface LedgerSweepResult {
  removed: number;
  scanned: number;
  /** Past the horizon, but the detector's state is locked, unknown, or still open after a finalize. */
  kept_open: number;
  /** Past the horizon with open work: handed to the detector's idle close this run. */
  finalized: number;
  /** Finalize candidates this run did not reach (budget): the cursor resumes before the first. */
  carried: number;
  /** The pass reached the end of the directory. */
  complete: boolean;
  /** Nothing to do: no ledger yet, or the last complete pass is too recent. */
  skipped: boolean;
}

function sweepStateIn(raw: unknown): SweepState | undefined {
  if (!isRecord(raw)) return undefined;
  if (raw.v !== 1 || typeof raw.next_after !== "number" || !Number.isFinite(raw.next_after)) return undefined;
  const cursor = raw.cursor;
  if (cursor !== null && !(typeof cursor === "string" && (SESSION_KEY_RE.test(cursor) || TRASH_RE.test(cursor)))) {
    return undefined;
  }
  return { v: 1, next_after: raw.next_after, cursor };
}

function readSweepState(file: string, vaultPath: string): SweepState | undefined {
  try {
    const bytes = readRegularFile(file, vaultPath, MAX_SIDE_FILE_BYTES, null);
    if (!bytes) return undefined;
    return sweepStateIn(JSON.parse(bytes.toString("utf8")));
  } catch {
    return undefined;
  }
}

/** Replace the cursor file atomically: a fresh temp name (never followed), then a rename. Throws on failure. */
function writeSweepState(file: string, value: SweepState, vaultPath: string): void {
  const tmp = `${file}.${process.pid}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(tmp, JSON.stringify(value), { flag: "wx", mode: 0o600 });
    assertWriteTargetUnder(file, vaultPath);
    fs.renameSync(tmp, file);
  } catch (error) {
    try {
      fs.rmSync(tmp, { force: true });
    } catch {
      // A stray temp file is harmless; the error below is what matters.
    }
    throw error;
  }
}

/** Whether every entry of a ledger session directory (and the directory) is past the horizon. */
function isPastHorizon(dir: string, dirStat: fs.Stats, now: number): boolean {
  let newest = dirStat.mtimeMs;
  const entries = fs.readdirSync(dir);
  if (entries.length > MAX_ENTRIES_PER_DIR) return false;
  for (const entry of entries) {
    newest = Math.max(newest, fs.lstatSync(path.join(dir, entry)).mtimeMs);
  }
  return now - newest > LEDGER_IDLE_MAX_MS;
}

type SweepOutcome = "removed" | "kept" | "kept_open" | "candidate";

/** Handle one name. Never follows a symlink; throws on a filesystem failure. */
function sweepOne(vaultPath: string, root: string, name: string, now: number): SweepOutcome {
  const target = path.join(root, name);
  const stat = fs.lstatSync(target);
  // A symlink or a plain file is never followed and never removed.
  if (!stat.isDirectory()) return "kept";
  if (TRASH_RE.test(name)) {
    // A run cut off between its rename and its removal.
    fs.rmSync(target, { recursive: true, force: true });
    return "removed";
  }
  if (!isPastHorizon(target, stat, now)) return "kept";
  const detector = detectorSessionState(vaultPath, name, now);
  // Open work past the horizon: a finalize candidate (closed, then removed,
  // by the caller). Locked or unknown state is never finalized or removed.
  if (detector === "open") return "candidate";
  if (detector !== "none" && detector !== "closed") return "kept_open";
  removeSessionDir(root, name, now);
  return "removed";
}

/**
 * Renamed first, so a removal cut short leaves a .trash- name the next run
 * finishes, never a half-emptied session directory. Throws on failure.
 */
function removeSessionDir(root: string, name: string, now: number): void {
  const trash = path.join(root, `.trash-${Math.floor(now)}-${name}`);
  fs.renameSync(path.join(root, name), trash);
  fs.rmSync(trash, { recursive: true, force: true });
}

/** The detector's idle close, scoped and budgeted (use-detector.ts `finalizeIdleSessions`). */
export type Finalizer = (vaultPath: string, options: { sessionKeys: readonly string[]; budgetMs: number }) => unknown;

async function detectorFinalizer(): Promise<Finalizer> {
  // Lazy: SessionStart loads the detector only when a candidate needs closing.
  const { finalizeIdleSessions } = await import("./use-detector.js");
  return finalizeIdleSessions;
}

/**
 * Remove `.runtime/use-ledger/<session_key>` directories past the 90-day
 * horizon whose detector state is closed (or absent), and finish any `.trash-`
 * a cut-off run left. A directory past the horizon whose state still holds
 * open work (an epoch, a window, an unflushed reply) is a finalize candidate:
 * the detector's own idle close (`finalizeIdleSessions`, scoped to the
 * candidates and to the budget left) closes it, then its state is re-read and
 * the directory removed only if it is now closed. The horizon is judged before
 * the finalize, whose own rows would otherwise make it look fresh.
 *
 * Bounded: at most 20 removals per run and about `budgetMs` (or the shared
 * `deadline`), checked between items, with one item's slack: the item in
 * progress, and on a spent budget the first candidate's close-and-remove, so
 * every run that finds a candidate makes progress. Every candidate the close
 * examined and closed is removed in the same run, budget or not (its own
 * close rows would otherwise hide it from the horizon for 90 days). The scan keeps half the
 * budget for its candidates. A cursor resumes the pass, also before the first
 * candidate a cut-off run did not reach; after a complete pass the next waits
 * a day. A budget spent on arrival does nothing at all. use-detector.js is
 * loaded only when a candidate exists.
 * Never throws.
 */
export async function sweepLedgerDirs(
  vaultPath: string,
  options: { now?: number; budgetMs?: number; clock?: () => number; deadline?: number; finalizer?: Finalizer } = {},
): Promise<LedgerSweepResult> {
  const clock = options.clock ?? Date.now;
  const started = options.deadline === undefined ? clock() : undefined;
  // A shared `deadline` (runSessionMaintenance) or this call's own budget.
  const deadline = options.deadline ?? (started as number) + (options.budgetMs ?? LEDGER_SWEEP_BUDGET_MS);
  const now = options.now ?? started ?? Date.now();
  const result: LedgerSweepResult = {
    removed: 0,
    scanned: 0,
    kept_open: 0,
    finalized: 0,
    carried: 0,
    complete: false,
    skipped: false,
  };
  const left = (): number => deadline - clock();
  const spent = (): boolean => left() <= 0;
  // The budget this run has, read once (no extra clock reading on the
  // budgetMs path, so item checks stay where they were).
  const budgetHere = options.deadline === undefined ? (options.budgetMs ?? LEDGER_SWEEP_BUDGET_MS) : left();
  if (!(budgetHere > 0)) {
    result.skipped = true;
    return result;
  }
  try {
    const root = containedDir(vaultPath, LEDGER_RELDIR.split(path.sep), false);
    if (root === null) {
      result.skipped = true;
      return result;
    }
    const stateFile = path.join(root, SWEEP_STATE_FILE);
    const state: SweepState = readSweepState(stateFile, vaultPath) ?? { v: 1, next_after: 0, cursor: null };
    if (state.cursor === null && now < state.next_after) {
      result.skipped = true;
      return result;
    }
    const names = fs
      .readdirSync(root)
      .filter((name) => SESSION_KEY_RE.test(name) || TRASH_RE.test(name))
      .sort();
    const cursor = state.cursor;
    let index = cursor === null ? 0 : names.findIndex((name) => name > cursor);
    if (index < 0) index = names.length;
    let last = cursor;
    const candidates: string[] = [];
    // Half the budget is kept for closing and removing the candidates found,
    // so a run that finds some always gets to work on them.
    const reserveMs = budgetHere / 2;
    for (; index < names.length; index += 1) {
      if (spent() || result.removed + candidates.length >= LEDGER_SWEEP_MAX_REMOVALS) break;
      if (candidates.length > 0 && left() <= reserveMs) break;
      const name = names[index] as string;
      result.scanned += 1;
      try {
        const outcome = sweepOne(vaultPath, root, name, now);
        if (outcome === "removed") result.removed += 1;
        else if (outcome === "kept_open") result.kept_open += 1;
        else if (outcome === "candidate") candidates.push(name);
      } catch {
        // One directory that cannot be judged or removed does not stop the pass.
      }
      last = name;
    }
    result.complete = index >= names.length;
    // Finalize, then remove EVERY candidate the close examined and left closed,
    // whatever the clock says: each was judged past the horizon before the
    // close, and the close's own rows would make it look fresh to any later
    // run, so stopping here would keep it for another 90 days. That costs one
    // rename and removal per examined candidate (at most 20). A spent budget
    // still hands the close its first candidate, so every run that finds one
    // makes progress; candidates the close did not reach are carried.
    let firstUnreached: string | null = null;
    if (candidates.length > 0) {
      const batch = spent() ? candidates.slice(0, 1) : candidates;
      let examined = batch.length;
      try {
        const finalize = options.finalizer ?? (await detectorFinalizer());
        const closed = finalize(vaultPath, { sessionKeys: batch, budgetMs: Math.max(1, left()) }) as
          | { remaining?: unknown }
          | undefined;
        const remaining = closed && typeof closed.remaining === "number" ? closed.remaining : 0;
        if (Number.isSafeInteger(remaining) && remaining > 0 && remaining <= batch.length) examined = batch.length - remaining;
      } catch {
        // A detector that will not load closes nothing; the candidates stay open.
      }
      for (let i = 0; i < batch.length; i += 1) {
        const name = batch[i] as string;
        if (i >= examined) {
          firstUnreached = name;
          break;
        }
        try {
          const after = detectorSessionState(vaultPath, name, now);
          if (after === "none" || after === "closed") {
            removeSessionDir(root, name, now);
            result.removed += 1;
            result.finalized += 1;
          } else {
            result.kept_open += 1;
          }
        } catch {
          result.kept_open += 1;
        }
      }
      if (firstUnreached === null && batch.length < candidates.length) firstUnreached = candidates[batch.length] as string;
    }
    if (firstUnreached !== null) {
      result.carried = candidates.length - candidates.indexOf(firstUnreached);
      result.complete = false;
      const before = names[names.indexOf(firstUnreached) - 1];
      last = before !== undefined && before > (cursor ?? "") ? before : cursor;
    }
    writeSweepState(
      stateFile,
      result.complete
        ? { v: 1, next_after: now + LEDGER_SWEEP_INTERVAL_MS, cursor: null }
        : { v: 1, next_after: state.next_after, cursor: last },
      vaultPath,
    );
  } catch {
    // Best effort: retention runs again next boot.
  }
  return result;
}

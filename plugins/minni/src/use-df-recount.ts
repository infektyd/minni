// The use detector's DF snapshot, recounted at Stop in capped slices (design
// rev5 §4.4 "Distinctive tokens", slice S1b).
//
// The detector judges titles against `.runtime/df-snapshot.json`: per token,
// how many notes carry it. Counting the whole vault costs about 145 ms of
// tokenisation at the real vault size (800 notes, 12 MB), which no hook may
// spend in one go: the host waits for every hook to exit. So the count runs at
// the cheapest moment, after a measured Stop's output is delivered, one slice
// per Stop, and only when it is due:
//
//   - Cadence: weekly. A pass starts when the snapshot is missing or at least
//     7 days old, or when the early trigger fires: the vault's note count (a
//     directory listing only, no note read) differs by more than 5% from the
//     count the last completed pass listed. That check runs at most once a
//     day per vault. A pass that started over 7 days ago restarts. DF is a
//     statistic, so a pass that spans a few turns (and a vault that changes
//     under it) is acceptable.
//   - A pass lists the notes ONCE, at its first slice, through the search
//     scan's own walker (vault.ts `listVaultNoteRelPaths`: `wiki/**/*.md`,
//     containment on every entry, symlinks never followed), and keeps the
//     sorted list in its partial state. Each later slice reads the next notes
//     from that list (`readVaultNoteTextForDf`: containment again, `blocked`
//     and `skippedByStatus` notes left out, a size cap, non-blocking opens), so
//     no slice repeats the walk.
//   - Counting is `useTokens` per note into a Map: exactly the tokens and the
//     per-note counting of `documentFrequencies`, so a pass in slices sums to
//     the one-shot count (pinned by a test).
//   - The partial state, `.runtime/df-snapshot.partial.json` =
//     `{v: 2, kind, started_at, paths, next, notes, tokens, counts}` (parallel
//     arrays: compact, and cheap to parse), is read once and replaced once per
//     slice, atomically. The snapshot is written (`writeDfSnapshot`, temp +
//     rename) only when a pass completes; the partial is then removed.
//   - `.runtime/df-recount.json` = `{v, kind, listed, checked_at, empty_at?}`
//     holds the early trigger's baseline (the listed count of the last
//     completed pass), the time of the last daily check, and when a pass last
//     found no countable note (which parks the cadence for a week).
//   - A slice's counting window is capped at 40 ms of its 50 ms, and starts
//     AFTER setup: the partial read, the per-slice note count, and on a pass's
//     first slice the one walk of the vault, none of which the cap bounds. The
//     remaining 10 ms are kept for the partial write; one note's work is the
//     slack, and every slice counts at least one note.
//   - A corrupt or foreign partial or state file restarts cleanly. A path a
//     safe write cannot replace (a symlink, a directory) makes the slice do
//     nothing. A partial too large to keep parks the pass for a week, so a
//     pathological vault cannot cost every Stop.
//
// Only Stop calls this, through hook-handlers.ts, last among its deferred
// commits, on a measured wire with the ledger on. It never throws.
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { DF_SNAPSHOT_RELPATH, useTokens, writeDfSnapshot } from "./use-detector.js";
import { assertWriteTargetUnder, countVaultNotes, listVaultNoteRelPaths, readVaultNoteTextForDf } from "./vault.js";

const DAY_MS = 24 * 60 * 60 * 1000;
/** A snapshot this old is recounted (weekly cadence). */
export const DF_SNAPSHOT_MAX_AGE_MS = 7 * DAY_MS;
/** A pass that started longer ago than this restarts from the first note. */
export const DF_PASS_MAX_AGE_MS = 7 * DAY_MS;
/** The early trigger's note-count check runs at most this often per vault. */
export const DF_CHECK_INTERVAL_MS = DAY_MS;
/** The early trigger fires when the note count moved by more than this share of the last pass's. */
export const DF_TRIGGER_RATIO = 0.05;
/** One Stop's slice, wall time from its start. */
export const DF_RECOUNT_SLICE_MS = 50;
/** Kept back from the slice for the partial state's write. */
export const DF_RECOUNT_WRITE_RESERVE_MS = 10;
/** Notes over this size are left out of the count. */
export const DF_NOTE_MAX_BYTES = 2 * 1024 * 1024;
export const DF_PARTIAL_RELPATH = path.join(".runtime", "df-snapshot.partial.json");
export const DF_STATE_RELPATH = path.join(".runtime", "df-recount.json");
const MAX_PARTIAL_BYTES = 16 * 1024 * 1024;
const MAX_STATE_BYTES = 64 * 1024;
/** An mtime or timestamp this far ahead of the clock is a skewed clock, not a fresh one. */
const FUTURE_SLACK_MS = 5 * 60 * 1000;
const PASS_KIND = "df-recount-pass";
const STATE_KIND = "df-recount-state";
const READ_FLAGS = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);

interface PassState {
  v: 2;
  kind: typeof PASS_KIND;
  started_at: number;
  /** The vault's note count when the pass started (`paths.length`): a later count past the trigger discards it. */
  listed: number;
  /** The notes this pass counts: the sorted listing taken at its first slice. */
  paths: string[];
  /** Index into `paths` of the next note to count. */
  next: number;
  notes: number;
  tokens: string[];
  counts: number[];
  /** Abandoned (too large to keep): no new pass before started_at + a week. */
  failed?: true;
}

interface RecountState {
  v: 1;
  kind: typeof STATE_KIND;
  /** Notes the last completed pass listed: the early trigger's baseline (null before any). */
  listed: number | null;
  /** When the daily note-count check last ran (epoch ms). */
  checked_at: number;
  /** When a pass last completed with no countable note: no new pass for a week unless the count moves. */
  empty_at?: number;
}

export type DfRecountStatus =
  /** Not due: nothing counted (and, at most daily, the note count checked). */
  | "fresh"
  /** A path a safe write cannot replace (symlink, directory): nothing done. */
  | "refused"
  /** A slice was counted; the pass continues next Stop. */
  | "partial"
  /** The pass completed and the snapshot was written. */
  | "written"
  /** The pass completed with no countable note: no snapshot. */
  | "empty"
  /** The pass is parked, or a write failed. */
  | "failed";

export interface DfRecountResult {
  status: DfRecountStatus;
  /** Notes counted by this slice. */
  counted: number;
  /** Notes counted by the pass so far. */
  pass_notes: number;
  /** Notes the pass lists in all. */
  pass_paths: number;
  /** This slice started the pass (none running, an old one, or a corrupt or foreign one). */
  restarted: boolean;
  /** Why a pass is running: the snapshot's age, or the early trigger. */
  reason?: "age" | "trigger";
  /** The daily note-count check ran in this call. */
  checked: boolean;
}

function errorCode(error: unknown): unknown {
  return (error as { code?: unknown } | null)?.code;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/**
 * One of this module's own side files (the partial, the state): a symlink in
 * its place is never read through or written through; the link itself is
 * removed (its target untouched) so a fresh 0600 file replaces it. A
 * directory there refuses the slice. True when the path is now replaceable.
 */
function ownFileReplaceable(file: string, vaultPath: string): boolean {
  try {
    const stat = fs.lstatSync(file);
    if (stat.isDirectory()) return false;
    if (stat.isSymbolicLink()) {
      // Only a link that sits inside the vault is removed (unlink removes the
      // link, never its target).
      const parent = fs.realpathSync(path.dirname(file));
      const rel = path.relative(fs.realpathSync(vaultPath), parent);
      if (rel.startsWith("..") || path.isAbsolute(rel)) return false;
      fs.unlinkSync(file);
    }
  } catch (error) {
    if (errorCode(error) !== "ENOENT") return false;
  }
  return replaceable(file, vaultPath);
}

/** Whether `file` can be replaced by a temp-and-rename: absent, or not a symlink or directory. */
function replaceable(file: string, vaultPath: string): boolean {
  try {
    const stat = fs.lstatSync(file);
    if (stat.isSymbolicLink() || stat.isDirectory()) return false;
  } catch (error) {
    if (errorCode(error) !== "ENOENT") return false;
  }
  try {
    assertWriteTargetUnder(file, vaultPath);
    return true;
  } catch {
    // A missing .runtime fails the parent realpath; a write would create it.
    return !fs.existsSync(path.dirname(file));
  }
}

/** The snapshot's age in ms (one lstat), or null when it is missing or not a regular file. */
function snapshotAge(file: string, now: number): number | null {
  try {
    const stat = fs.lstatSync(file);
    return stat.isFile() ? now - stat.mtimeMs : null;
  } catch {
    return null;
  }
}

/** A small regular file's bytes, opened non-blocking without following a link; null otherwise. */
function readRegular(file: string, maxBytes: number): Buffer | null {
  let fd: number | undefined;
  try {
    fd = fs.openSync(file, READ_FLAGS);
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > maxBytes) return null;
    const buffer = Buffer.alloc(stat.size);
    let filled = 0;
    while (filled < buffer.length) {
      const n = fs.readSync(fd, buffer, filled, buffer.length - filled, filled);
      if (n <= 0) break;
      filled += n;
    }
    return buffer.subarray(0, filled);
  } catch {
    return null;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // A read-only descriptor: nothing to lose.
      }
    }
  }
}

function readJson(file: string, maxBytes: number): unknown {
  const bytes = readRegular(file, maxBytes);
  if (bytes === null) return undefined;
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch {
    return undefined;
  }
}

function passStateIn(raw: unknown): PassState | null {
  if (!isRecord(raw) || raw.v !== 2 || raw.kind !== PASS_KIND) return null;
  if (typeof raw.started_at !== "number" || !Number.isFinite(raw.started_at)) return null;
  if (raw.failed === true) {
    return { v: 2, kind: PASS_KIND, started_at: raw.started_at, listed: 0, paths: [], next: 0, notes: 0, tokens: [], counts: [], failed: true };
  }
  const { paths, tokens, counts, next, notes, listed } = raw;
  if (!Array.isArray(paths) || !Array.isArray(tokens) || !Array.isArray(counts) || tokens.length !== counts.length) return null;
  if (listed !== paths.length) return null;
  if (!Number.isSafeInteger(next) || (next as number) < 0 || (next as number) > paths.length) return null;
  if (!Number.isSafeInteger(notes) || (notes as number) < 0 || (notes as number) > (next as number)) return null;
  for (const entry of paths) {
    if (typeof entry !== "string" || entry.length === 0 || path.isAbsolute(entry)) return null;
  }
  for (let i = 0; i < tokens.length; i += 1) {
    const count = counts[i];
    // A token cannot appear in more notes than the pass has counted.
    if (typeof tokens[i] !== "string" || !Number.isSafeInteger(count) || (count as number) < 1 || (count as number) > (notes as number)) {
      return null;
    }
  }
  return {
    v: 2,
    kind: PASS_KIND,
    started_at: raw.started_at,
    listed: paths.length,
    paths: paths as string[],
    next: next as number,
    notes: notes as number,
    tokens: tokens as string[],
    counts: counts as number[],
  };
}

function recountStateIn(raw: unknown): RecountState | null {
  if (!isRecord(raw) || raw.v !== 1 || raw.kind !== STATE_KIND) return null;
  if (typeof raw.checked_at !== "number" || !Number.isFinite(raw.checked_at)) return null;
  const listed = raw.listed;
  if (listed !== null && (!Number.isSafeInteger(listed) || (listed as number) < 0)) return null;
  const state: RecountState = { v: 1, kind: STATE_KIND, listed: listed as number | null, checked_at: raw.checked_at };
  if (typeof raw.empty_at === "number" && Number.isFinite(raw.empty_at)) state.empty_at = raw.empty_at;
  return state;
}

/** Replace a side file atomically (fresh temp name, never followed, then a rename). Throws on failure. */
function writeAtomic(file: string, value: unknown, vaultPath: string, maxBytes: number): void {
  const text = JSON.stringify(value);
  if (Buffer.byteLength(text, "utf8") > maxBytes) throw new RangeError("df recount: state too large");
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.${process.pid}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(tmp, text, { flag: "wx", mode: 0o600 });
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

function removeFile(file: string, vaultPath: string): void {
  try {
    assertWriteTargetUnder(file, vaultPath);
    fs.rmSync(file, { force: true });
  } catch {
    // Left behind, it restarts cleanly: a completed pass is newer than it.
  }
}

/** Whether `count` moved from the baseline `listed` by more than the trigger ratio. */
export function earlyTriggerFires(listed: number | null, count: number): boolean {
  if (listed === null || listed < 0) return false;
  // From an empty vault, any note at all is more than 5%.
  if (listed === 0) return count > 0;
  return Math.abs(count - listed) > DF_TRIGGER_RATIO * listed;
}

/**
 * One Stop's slice of the DF recount: nothing while it is not due (and at
 * most daily, the cheap note-count check for the early trigger); otherwise
 * count the next notes within `budgetMs` and keep the partial state, writing
 * the snapshot when the pass completes. Never throws.
 */
export async function runDfRecountSlice(
  vaultPath: string,
  options: { now?: number; budgetMs?: number; clock?: () => number } = {},
): Promise<DfRecountResult> {
  const clock = options.clock ?? Date.now;
  const now = options.now ?? Date.now();
  const budgetMs = options.budgetMs ?? DF_RECOUNT_SLICE_MS;
  const result: DfRecountResult = { status: "fresh", counted: 0, pass_notes: 0, pass_paths: 0, restarted: false, checked: false };
  try {
    const snapshotFile = path.join(vaultPath, DF_SNAPSHOT_RELPATH);
    const partialFile = path.join(vaultPath, DF_PARTIAL_RELPATH);
    const stateFile = path.join(vaultPath, DF_STATE_RELPATH);
    if (!replaceable(snapshotFile, vaultPath) || !ownFileReplaceable(partialFile, vaultPath) || !ownFileReplaceable(stateFile, vaultPath)) {
      result.status = "refused";
      return result;
    }
    let pass = passStateIn(readJson(partialFile, MAX_PARTIAL_BYTES));
    const onDisk = pass !== null || fs.existsSync(partialFile);
    if (pass?.failed) {
      if (now - pass.started_at < DF_PASS_MAX_AGE_MS && pass.started_at <= now + FUTURE_SLACK_MS) {
        result.status = "failed";
        return result;
      }
      pass = null;
    }
    // A pass never lingers: older than a week, dated in the future, or a vault
    // whose note count has moved past the trigger since it started.
    let outgrown = false;
    if (pass !== null) {
      const aged = now - pass.started_at > DF_PASS_MAX_AGE_MS || pass.started_at > now + FUTURE_SLACK_MS;
      const count = aged ? null : countVaultNotes(vaultPath);
      outgrown = count !== null && earlyTriggerFires(pass.listed, count);
      if (aged || outgrown) pass = null;
    }
    // Whatever was on disk and is not a pass this slice continues (corrupt,
    // foreign, aged, outgrown) goes now, whether or not a new pass starts.
    if (pass === null && onDisk) removeFile(partialFile, vaultPath);
    const state = recountStateIn(readJson(stateFile, MAX_STATE_BYTES));

    if (pass === null) {
      // Is a pass due? The snapshot's age first (one lstat), then, at most
      // daily, the note count (a directory listing, no note read).
      const age = snapshotAge(snapshotFile, now);
      // A pass that found no countable note parks the cadence for a week,
      // whether or not an older snapshot still exists (only the trigger wakes it).
      const parkedEmpty =
        state?.empty_at !== undefined && now - state.empty_at < DF_SNAPSHOT_MAX_AGE_MS && state.empty_at <= now + FUTURE_SLACK_MS;
      let reason: "age" | "trigger" | null =
        !parkedEmpty && (age === null || age >= DF_SNAPSHOT_MAX_AGE_MS || age <= -FUTURE_SLACK_MS) ? "age" : null;
      // A pass the vault outgrew restarts at once: the count has moved past the trigger.
      if (reason === null && outgrown) reason = "trigger";
      if (reason === null) {
        const lastCheck = state?.checked_at ?? Number.NEGATIVE_INFINITY;
        if (now - lastCheck < DF_CHECK_INTERVAL_MS && lastCheck <= now + FUTURE_SLACK_MS) return result;
        const count = countVaultNotes(vaultPath);
        result.checked = true;
        const fires = count !== null && earlyTriggerFires(state?.listed ?? null, count);
        // A first check with no completed pass on record takes today's count as the baseline.
        const next: RecountState = { v: 1, kind: STATE_KIND, listed: state?.listed ?? count, checked_at: now };
        if (state?.empty_at !== undefined && !fires) next.empty_at = state.empty_at;
        writeAtomic(stateFile, next, vaultPath, MAX_STATE_BYTES);
        if (!fires) return result;
        reason = "trigger";
      }
      result.reason = reason;
      result.restarted = true;
      const paths = await listVaultNoteRelPaths(vaultPath);
      pass = { v: 2, kind: PASS_KIND, started_at: now, listed: paths.length, paths, next: 0, notes: 0, tokens: [], counts: [] };
    }

    // The counting window starts after setup (the partial read, and on a
    // pass's first slice its one walk of the vault), and always counts at
    // least one note, so a slice can never spend itself on setup alone.
    const countUntil = clock() + Math.max(0, budgetMs - DF_RECOUNT_WRITE_RESERVE_MS);
    const df = new Map<string, number>();
    for (let i = 0; i < pass.tokens.length; i += 1) df.set(pass.tokens[i] as string, pass.counts[i] as number);
    let attempted = 0;
    while (pass.next < pass.paths.length) {
      if (attempted > 0 && clock() >= countUntil) break;
      const text = readVaultNoteTextForDf(vaultPath, pass.paths[pass.next] as string, DF_NOTE_MAX_BYTES);
      if (text !== null) {
        for (const token of useTokens(text)) df.set(token, (df.get(token) ?? 0) + 1);
        pass.notes += 1;
        result.counted += 1;
      }
      pass.next += 1;
      attempted += 1;
    }
    result.pass_notes = pass.notes;
    result.pass_paths = pass.paths.length;

    if (pass.next >= pass.paths.length) {
      if (pass.notes >= 1) {
        result.status = writeDfSnapshot(vaultPath, { notes: pass.notes, df: Object.fromEntries(df) }) ? "written" : "failed";
      } else {
        result.status = "empty";
      }
      if (result.status === "failed") {
        // Keep the pass, counts included, so the next Stop retries the write.
        pass.tokens = [...df.keys()];
        pass.counts = [...df.values()];
        try {
          writeAtomic(partialFile, pass, vaultPath, MAX_PARTIAL_BYTES);
        } catch {
          // The partial on disk (one slice older) resumes instead.
        }
        return result;
      }
      const done: RecountState = { v: 1, kind: STATE_KIND, listed: pass.paths.length, checked_at: now };
      if (result.status === "empty") done.empty_at = now;
      try {
        writeAtomic(stateFile, done, vaultPath, MAX_STATE_BYTES);
      } catch {
        // The baseline stays as it was; the next daily check uses it.
      }
      // Deleted once the snapshot's atomic write has succeeded (or there was nothing to count).
      removeFile(partialFile, vaultPath);
      return result;
    }
    pass.tokens = [...df.keys()];
    pass.counts = [...df.values()];
    try {
      writeAtomic(partialFile, pass, vaultPath, MAX_PARTIAL_BYTES);
      result.status = "partial";
    } catch (error) {
      result.status = "failed";
      if (error instanceof RangeError) {
        // Too large to keep: park the pass for a week rather than recount it every Stop.
        writeAtomic(
          partialFile,
          { v: 2, kind: PASS_KIND, started_at: now, listed: 0, paths: [], next: 0, notes: 0, tokens: [], counts: [], failed: true },
          vaultPath,
          MAX_PARTIAL_BYTES,
        );
      }
    }
    return result;
  } catch {
    result.status = "failed";
    return result;
  }
}

// The use detector's DF snapshot, recounted at Stop in capped slices (design
// rev5 §4.4 "Distinctive tokens", slice S1b).
//
// The detector judges titles against `.runtime/df-snapshot.json`: per token,
// how many notes carry it. Counting the whole vault costs about 145 ms of
// tokenisation at the real vault size (800 notes, 12 MB; reading the notes is
// about 8 ms), which no hook may spend in one go: UPS would break the prompt
// budget, and the host waits for every hook to exit. So the count runs at the
// cheapest moment, after a Stop's output is delivered, one slice per Stop:
//
//   - A pass starts when the snapshot is missing or over 24 h old. A pass that
//     started over 24 h ago restarts; DF is a statistic, so a pass that spans a
//     few turns (and a vault that changes under it) is acceptable.
//   - Each slice reads the next notes in sorted vault-relative order, through
//     the search scan's own walker and filters (vault.ts
//     `listVaultNoteRelPaths` / `readVaultNoteTextForDf`: `wiki/**/*.md`,
//     containment, symlinks never followed, `blocked` and `skippedByStatus`
//     notes left out, a size cap, non-blocking opens), and adds their counts
//     (`documentFrequencies`, per note, so a pass in slices sums to exactly the
//     one-shot count) to the partial state.
//   - The partial state, `.runtime/df-snapshot.partial.json` =
//     `{v, kind, started_at, cursor, notes, df}`, is replaced atomically after
//     every slice. The snapshot itself is written (`writeDfSnapshot`, temp +
//     rename) only when a pass completes, and the partial is then removed.
//   - A slice is capped at about 50 ms, checked between notes, with time kept
//     back for the partial write. One note's work is the slack.
//   - A corrupt or foreign partial restarts the pass cleanly. A snapshot or
//     partial path that a safe write cannot replace (a symlink, a directory)
//     makes the slice do nothing. A partial too large to keep ends the pass
//     for a day, so a pathological vault cannot cost every Stop.
//
// Only Stop calls this, through hook-handlers.ts, last among its deferred
// commits, on a measured wire with the ledger on. It never throws.
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { DF_SNAPSHOT_RELPATH, documentFrequencies, writeDfSnapshot } from "./use-detector.js";
import { assertWriteTargetUnder, listVaultNoteRelPaths, readVaultNoteTextForDf } from "./vault.js";

/** A snapshot older than this is recounted. */
export const DF_SNAPSHOT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
/** A pass that started longer ago than this restarts from the first note. */
export const DF_PASS_MAX_AGE_MS = 24 * 60 * 60 * 1000;
/** One Stop's slice. */
export const DF_RECOUNT_SLICE_MS = 50;
/** Kept back from the slice for the partial state's write. */
export const DF_RECOUNT_WRITE_RESERVE_MS = 10;
/** Notes over this size are left out of the count (the search scan reads them; the count does not). */
export const DF_NOTE_MAX_BYTES = 2 * 1024 * 1024;
export const DF_PARTIAL_RELPATH = path.join(".runtime", "df-snapshot.partial.json");
const MAX_PARTIAL_BYTES = 16 * 1024 * 1024;
/** An mtime this far ahead of the clock is a skewed clock, not a fresh file. */
const FUTURE_SLACK_MS = 5 * 60 * 1000;
const PARTIAL_KIND = "df-recount";
const READ_FLAGS = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);

interface PartialState {
  v: 1;
  kind: typeof PARTIAL_KIND;
  started_at: number;
  /** The last note counted (vault-relative); the next slice starts after it. */
  cursor: string | null;
  notes: number;
  df: Record<string, number>;
  /** The pass was abandoned (state too large to keep): no new pass before started_at + a day. */
  failed?: true;
}

export type DfRecountStatus =
  /** The snapshot is under a day old: nothing to do. */
  | "fresh"
  /** A path a safe write cannot replace (symlink, directory): nothing done. */
  | "refused"
  /** A slice was counted; the pass continues next Stop. */
  | "partial"
  /** The pass completed and the snapshot was written. */
  | "written"
  /** The pass completed with no countable note: no snapshot. */
  | "empty"
  /** The pass is parked for a day, or a write failed. */
  | "failed";

export interface DfRecountResult {
  status: DfRecountStatus;
  /** Notes counted by this slice. */
  counted: number;
  /** Notes counted by the pass so far. */
  pass_notes: number;
  /** The pass restarted (no partial, an old pass, or a corrupt or foreign one). */
  restarted: boolean;
}

function errorCode(error: unknown): unknown {
  return (error as { code?: unknown } | null)?.code;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
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

/** Whether the snapshot is due: missing, older than a day, or dated well in the future. One lstat. */
function snapshotDue(file: string, now: number): boolean {
  try {
    const stat = fs.lstatSync(file);
    if (!stat.isFile()) return true;
    const age = now - stat.mtimeMs;
    return !(age > -FUTURE_SLACK_MS && age < DF_SNAPSHOT_MAX_AGE_MS);
  } catch {
    return true;
  }
}

function partialStateIn(raw: unknown): PartialState | null {
  if (!isRecord(raw) || raw.v !== 1 || raw.kind !== PARTIAL_KIND) return null;
  if (typeof raw.started_at !== "number" || !Number.isFinite(raw.started_at)) return null;
  if (raw.failed === true) return { v: 1, kind: PARTIAL_KIND, started_at: raw.started_at, cursor: null, notes: 0, df: {}, failed: true };
  if (raw.cursor !== null && (typeof raw.cursor !== "string" || raw.cursor.length === 0 || path.isAbsolute(raw.cursor))) return null;
  if (!Number.isSafeInteger(raw.notes) || (raw.notes as number) < 0 || !isRecord(raw.df)) return null;
  const df: Record<string, number> = {};
  for (const [token, count] of Object.entries(raw.df)) {
    if (!Number.isSafeInteger(count) || (count as number) < 1) return null;
    df[token] = count as number;
  }
  return { v: 1, kind: PARTIAL_KIND, started_at: raw.started_at, cursor: raw.cursor as string | null, notes: raw.notes as number, df };
}

/** The partial state, or null when absent, not a regular file, oversize, or not ours. Never blocks. */
function readPartial(file: string): PartialState | null {
  let fd: number | undefined;
  try {
    fd = fs.openSync(file, READ_FLAGS);
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > MAX_PARTIAL_BYTES) return null;
    const buffer = Buffer.alloc(stat.size);
    let filled = 0;
    while (filled < buffer.length) {
      const n = fs.readSync(fd, buffer, filled, buffer.length - filled, filled);
      if (n <= 0) break;
      filled += n;
    }
    return partialStateIn(JSON.parse(buffer.toString("utf8", 0, filled)));
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

/** Replace the partial state atomically (fresh temp name, never followed, then a rename). Throws on failure. */
function writePartial(file: string, state: PartialState, vaultPath: string): void {
  const text = JSON.stringify(state);
  if (Buffer.byteLength(text, "utf8") > MAX_PARTIAL_BYTES) throw new RangeError("df recount: partial state too large");
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

function removePartial(file: string, vaultPath: string): void {
  try {
    assertWriteTargetUnder(file, vaultPath);
    fs.rmSync(file, { force: true });
  } catch {
    // Left behind, it restarts cleanly: a completed pass is newer than it.
  }
}

/**
 * One Stop's slice of the DF recount: nothing while the snapshot is fresh;
 * otherwise count the next notes within `budgetMs` and keep the partial
 * state, writing the snapshot when the pass completes. Never throws.
 */
export async function runDfRecountSlice(
  vaultPath: string,
  options: { now?: number; budgetMs?: number; clock?: () => number } = {},
): Promise<DfRecountResult> {
  const clock = options.clock ?? Date.now;
  const started = clock();
  const now = options.now ?? Date.now();
  const budgetMs = options.budgetMs ?? DF_RECOUNT_SLICE_MS;
  const countUntil = started + Math.max(0, budgetMs - DF_RECOUNT_WRITE_RESERVE_MS);
  const result: DfRecountResult = { status: "fresh", counted: 0, pass_notes: 0, restarted: false };
  try {
    const snapshot = path.join(vaultPath, DF_SNAPSHOT_RELPATH);
    const partialFile = path.join(vaultPath, DF_PARTIAL_RELPATH);
    if (!snapshotDue(snapshot, now)) return result;
    if (!replaceable(snapshot, vaultPath) || !replaceable(partialFile, vaultPath)) {
      result.status = "refused";
      return result;
    }
    let state = readPartial(partialFile);
    if (state?.failed) {
      if (now - state.started_at < DF_PASS_MAX_AGE_MS && state.started_at <= now + FUTURE_SLACK_MS) {
        result.status = "failed";
        return result;
      }
      state = null;
    }
    if (state === null || now - state.started_at > DF_PASS_MAX_AGE_MS || state.started_at > now + FUTURE_SLACK_MS) {
      state = { v: 1, kind: PARTIAL_KIND, started_at: now, cursor: null, notes: 0, df: {} };
      result.restarted = true;
    }
    const names = await listVaultNoteRelPaths(vaultPath);
    const cursor = state.cursor;
    let index = cursor === null ? 0 : names.findIndex((name) => name > cursor);
    if (index < 0) index = names.length;
    for (; index < names.length; index += 1) {
      if (clock() >= countUntil) break;
      const name = names[index] as string;
      const text = readVaultNoteTextForDf(vaultPath, name, DF_NOTE_MAX_BYTES);
      if (text !== null) {
        const counts = documentFrequencies([text]);
        for (const [token, n] of Object.entries(counts.df)) state.df[token] = (state.df[token] ?? 0) + n;
        state.notes += counts.notes;
        result.counted += counts.notes;
      }
      state.cursor = name;
    }
    result.pass_notes = state.notes;
    if (index >= names.length) {
      if (state.notes >= 1) {
        result.status = writeDfSnapshot(vaultPath, { notes: state.notes, df: state.df }) ? "written" : "failed";
      } else {
        result.status = "empty";
      }
      if (result.status !== "failed") removePartial(partialFile, vaultPath);
      return result;
    }
    try {
      writePartial(partialFile, state, vaultPath);
      result.status = "partial";
    } catch (error) {
      result.status = "failed";
      if (error instanceof RangeError) {
        // Too large to keep: park the pass for a day rather than recount it every Stop.
        writePartial(partialFile, { v: 1, kind: PARTIAL_KIND, started_at: now, cursor: null, notes: 0, df: {}, failed: true }, vaultPath);
      }
    }
    return result;
  } catch {
    result.status = "failed";
    return result;
  }
}

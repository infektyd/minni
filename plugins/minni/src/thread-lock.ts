import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { link, mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { appendFileWithFsync } from "./vault.js";

export const DEFAULT_WAIT_MS = 5_000;
const DEFAULT_STALE_MS = 120_000;
const DEFAULT_POLL_MS = 25;

/**
 * DEFAULT_WAIT_MS is overflow/stuck for exclusive holders (replan, drain
 * retry). Worker writes dump-and-return instead of sitting this budget.
 * Do not silently enlarge it. Do not steal a live owner.
 */

/**
 * Cross-process Thread lock owner. `processStartMarker` is the local OS
 * process-start identity for `pid` (Linux: /proc/<pid>/stat starttime). A
 * reused PID with a different marker is not the live owner — stale recovery
 * may proceed. Legacy owner.json without a marker still treats a live PID as
 * live (never steal on age alone).
 */
export interface ThreadLockOwner {
  pid: number;
  operationId: string;
  acquiredAt: string;
  processStartMarker?: string;
}

export type ThreadLockRecoveryReason =
  | "stale_dead"
  | "stale_pid_reuse"
  | "stale_ownerless"
  | "stale_invalid_owner";

export interface ThreadLockRecoveryAudit {
  at: string;
  plan_id: string;
  reason: ThreadLockRecoveryReason;
  previous_owner?: ThreadLockOwner;
}

export interface ThreadLockOptions {
  waitMs?: number;
  staleMs?: number;
  pollMs?: number;
  now?: () => Date;
  isProcessAlive?: (pid: number) => boolean;
  /** Injectable process-start marker lookup (defaults to /proc on Linux). */
  getProcessStartMarker?: (pid: number) => string | undefined;
}

export class ThreadBusyError extends Error {
  readonly code = "THREAD_BUSY" as const;
  readonly owner?: ThreadLockOwner;

  constructor(owner?: ThreadLockOwner) {
    super("Thread mutation lock is busy");
    this.name = "ThreadBusyError";
    this.owner = owner;
  }
}

function lockKey(planId: string): string {
  return createHash("sha256").update(planId).digest("hex").slice(0, 32);
}

function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

/**
 * Linux process-start marker from /proc/<pid>/stat field 22 (starttime).
 * Returns undefined when /proc is unavailable or unreadable — callers then
 * fall back to PID-only liveness (never steal a live PID on age alone).
 */
export function readProcessStartMarker(pid: number): string | undefined {
  if (!Number.isInteger(pid) || pid <= 0) return undefined;
  try {
    const raw = readFileSync(`/proc/${pid}/stat`, "utf8");
    // comm may contain spaces/parens — starttime is field 22, which is index
    // 19 among the fields after the closing ") " of (comm).
    const close = raw.lastIndexOf(") ");
    if (close < 0) return undefined;
    const trailing = raw.slice(close + 2).trim().split(/\s+/);
    const starttime = trailing[19];
    if (!starttime || !/^\d+$/.test(starttime)) return undefined;
    return starttime;
  } catch {
    return undefined;
  }
}

function ownerLooksLive(
  owner: ThreadLockOwner,
  isProcessAlive: (pid: number) => boolean,
  getProcessStartMarker: (pid: number) => string | undefined,
): { live: boolean; reasonIfStale?: ThreadLockRecoveryReason } {
  if (!isProcessAlive(owner.pid)) {
    return { live: false, reasonIfStale: "stale_dead" };
  }
  // Live PID: compare start markers when both sides have one. Mismatch =
  // PID reuse under a new incarnation → not the lock owner.
  if (typeof owner.processStartMarker === "string" && owner.processStartMarker.length > 0) {
    const liveMarker = getProcessStartMarker(owner.pid);
    if (
      typeof liveMarker === "string" &&
      liveMarker.length > 0 &&
      liveMarker !== owner.processStartMarker
    ) {
      return { live: false, reasonIfStale: "stale_pid_reuse" };
    }
  }
  return { live: true };
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

function isErrno(error: unknown, code: string): boolean {
  return (error as NodeJS.ErrnoException).code === code;
}

function parseOwner(value: string): ThreadLockOwner | undefined {
  try {
    const owner = JSON.parse(value) as Partial<ThreadLockOwner>;
    if (
      !Number.isInteger(owner.pid) ||
      (owner.pid ?? 0) <= 0 ||
      typeof owner.operationId !== "string" ||
      owner.operationId.length === 0 ||
      typeof owner.acquiredAt !== "string" ||
      !Number.isFinite(Date.parse(owner.acquiredAt))
    ) {
      return undefined;
    }
    const parsed: ThreadLockOwner = {
      pid: owner.pid as number,
      operationId: owner.operationId,
      acquiredAt: owner.acquiredAt,
    };
    if (
      typeof owner.processStartMarker === "string" &&
      owner.processStartMarker.length > 0
    ) {
      parsed.processStartMarker = owner.processStartMarker;
    }
    return parsed;
  } catch {
    return undefined;
  }
}

async function readOwner(ownerPath: string): Promise<ThreadLockOwner | undefined> {
  try {
    return parseOwner(await readFile(ownerPath, "utf8"));
  } catch {
    return undefined;
  }
}

type StrictOwnerRead =
  | { status: "missing" }
  | { status: "unparseable" }
  | { status: "owner"; value: ThreadLockOwner };

/**
 * Release-path owner read. Unlike the acquire path (which treats any read
 * failure as "no observable owner"), release must distinguish absent or
 * replaced owners (never delete those) from a transient IO failure (retry
 * bounded, then fail loud rather than leak a live lock while reporting
 * success). Only ENOENT is quiet; every other IO error throws.
 */
async function readOwnerStrict(ownerPath: string): Promise<StrictOwnerRead> {
  let raw: string;
  try {
    raw = await readFile(ownerPath, "utf8");
  } catch (error) {
    if (isErrno(error, "ENOENT")) return { status: "missing" };
    throw error;
  }
  const owner = parseOwner(raw);
  if (owner === undefined) return { status: "unparseable" };
  return { status: "owner", value: owner };
}

/** Bounded release attempts for transient owner-read failures. */
const RELEASE_OWNER_READ_ATTEMPTS = 3;

interface LockDirIdentity {
  dev: number;
  ino: number;
}

/**
 * Remove the lock dir only when it is still verifiably ours: a successful
 * owner read with our operation nonce plus an unchanged directory identity
 * (dev+ino captured at acquire). A missing, unparseable, or replaced owner
 * is never deleted. Returns undefined on release (or clean skip), otherwise
 * the persistent release error for the caller to surface.
 */
async function releaseOwnLock(input: {
  lockDir: string;
  ownerPath: string;
  operationId: string;
  acquired: LockDirIdentity | undefined;
  pollMs: number;
}): Promise<unknown> {
  let lastReadError: unknown;
  for (
    let attempt = 0;
    attempt < RELEASE_OWNER_READ_ATTEMPTS;
    attempt += 1
  ) {
    if (attempt > 0) await sleep(input.pollMs);
    let read: StrictOwnerRead;
    try {
      read = await readOwnerStrict(input.ownerPath);
    } catch (error) {
      lastReadError = error;
      continue;
    }
    lastReadError = undefined;
    if (read.status === "missing") return undefined;
    if (read.status === "unparseable") return undefined;
    if (read.value.operationId !== input.operationId) return undefined;
    if (input.acquired !== undefined) {
      let current: LockDirIdentity;
      try {
        const lockStat = await stat(input.lockDir);
        current = { dev: lockStat.dev, ino: lockStat.ino };
      } catch (error) {
        if (isErrno(error, "ENOENT")) return undefined;
        return error;
      }
      if (
        current.dev !== input.acquired.dev ||
        current.ino !== input.acquired.ino
      ) {
        return undefined;
      }
    }
    try {
      await rm(input.lockDir, { recursive: true, force: true });
    } catch (error) {
      return error;
    }
    return undefined;
  }
  return lastReadError;
}

function recoveryAuditPath(vaultPath: string): string {
  return path.join(vaultPath, ".runtime", "thread-locks", "recovery.jsonl");
}

/**
 * Append one lock-side recovery audit line. Lock recovery often happens
 * before a plan journal is in scope — this file is the durable trail that
 * distinguishes recovery from theft (no daemon table).
 */
async function appendRecoveryAudit(
  vaultPath: string,
  entry: ThreadLockRecoveryAudit,
): Promise<void> {
  const filePath = recoveryAuditPath(vaultPath);
  await mkdir(path.dirname(filePath), { recursive: true });
  await appendFileWithFsync(filePath, `${JSON.stringify(entry)}\n`);
}

/** Test/ops helper: read lock-side recovery audit lines for a vault. */
export async function readThreadLockRecoveryAudit(
  vaultPath: string,
): Promise<ThreadLockRecoveryAudit[]> {
  try {
    const raw = await readFile(recoveryAuditPath(vaultPath), "utf8");
    return raw
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line.length > 0)
      .map((line) => JSON.parse(line) as ThreadLockRecoveryAudit);
  } catch (error) {
    if (isErrno(error, "ENOENT")) return [];
    throw error;
  }
}

export async function withThreadLock<T>(
  vaultPath: string,
  planId: string,
  operationId: string,
  fn: () => Promise<T>,
  options: ThreadLockOptions = {},
): Promise<T> {
  const waitMs = Math.max(0, options.waitMs ?? DEFAULT_WAIT_MS);
  const staleMs = Math.max(0, options.staleMs ?? DEFAULT_STALE_MS);
  const pollMs = Math.max(0, options.pollMs ?? DEFAULT_POLL_MS);
  const now = options.now ?? (() => new Date());
  const isProcessAlive = options.isProcessAlive ?? processAlive;
  const getProcessStartMarker =
    options.getProcessStartMarker ?? readProcessStartMarker;
  const locksRoot = path.join(vaultPath, ".runtime", "thread-locks");
  const lockDir = path.join(locksRoot, `${lockKey(planId)}.lock`);
  const ownerPath = path.join(lockDir, "owner.json");
  const deadline = Date.now() + waitMs;
  const selfMarker = getProcessStartMarker(process.pid);
  const owner: ThreadLockOwner = {
    pid: process.pid,
    operationId,
    acquiredAt: now().toISOString(),
    ...(selfMarker !== undefined ? { processStartMarker: selfMarker } : {}),
  };
  let observedOwner: ThreadLockOwner | undefined;

  await mkdir(locksRoot, { recursive: true });

  while (true) {
    try {
      await mkdir(lockDir);
      try {
        await writeFile(ownerPath, `${JSON.stringify(owner)}\n`, {
          encoding: "utf8",
          flag: "wx",
          mode: 0o600,
        });
      } catch (error) {
        await rm(lockDir, { recursive: true, force: true }).catch(() => {});
        throw error;
      }
      break;
    } catch (error) {
      if (!isErrno(error, "EEXIST")) {
        throw error;
      }
    }

    observedOwner = await readOwner(ownerPath);
    let ageMs: number | undefined;
    try {
      const lockStat = await stat(lockDir);
      ageMs = Math.max(0, now().getTime() - lockStat.mtimeMs);
    } catch (error) {
      if (!isErrno(error, "ENOENT")) {
        throw error;
      }
    }

    let recoveryReason: ThreadLockRecoveryReason | undefined;
    if (ageMs !== undefined && ageMs > staleMs) {
      if (observedOwner === undefined) {
        // Distinguish missing owner file from unparseable JSON for the audit.
        try {
          await readFile(ownerPath, "utf8");
          recoveryReason = "stale_invalid_owner";
        } catch (error) {
          if (isErrno(error, "ENOENT")) {
            recoveryReason = "stale_ownerless";
          } else {
            recoveryReason = "stale_invalid_owner";
          }
        }
      } else {
        const liveness = ownerLooksLive(
          observedOwner,
          isProcessAlive,
          getProcessStartMarker,
        );
        if (!liveness.live) {
          recoveryReason = liveness.reasonIfStale;
        }
      }
    }

    if (recoveryReason !== undefined) {
      const quarantineDir = `${lockDir}.stale-${randomUUID()}`;
      try {
        await rename(lockDir, quarantineDir);
      } catch (error) {
        if (!isErrno(error, "ENOENT")) {
          throw error;
        }
      }
      await rm(quarantineDir, { recursive: true, force: true }).catch(() => {});
      // Best-effort audit: recovery must still succeed if the audit write
      // fails (lock progress > audit completeness).
      await appendRecoveryAudit(vaultPath, {
        at: now().toISOString(),
        plan_id: planId,
        reason: recoveryReason,
        ...(observedOwner ? { previous_owner: observedOwner } : {}),
      }).catch(() => {});
      continue;
    }

    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) {
      throw new ThreadBusyError(observedOwner);
    }
    await sleep(Math.min(pollMs, remainingMs));
  }

  // Directory identity at acquire: the release below removes the dir only
  // when this identity still matches, so a reaped-and-replaced lock (even
  // under a reused operation id) is never deleted. Best-effort: without it
  // release falls back to the operation-nonce check alone.
  let acquiredIdentity: LockDirIdentity | undefined;
  try {
    const lockStat = await stat(lockDir);
    acquiredIdentity = { dev: lockStat.dev, ino: lockStat.ino };
  } catch {
    acquiredIdentity = undefined;
  }

  let fnError: unknown;
  let fnFailed = false;
  let result: T | undefined;
  try {
    result = await fn();
  } catch (error) {
    fnError = error;
    fnFailed = true;
  }
  const releaseError = await releaseOwnLock({
    lockDir,
    ownerPath,
    operationId,
    acquired: acquiredIdentity,
    pollMs,
  });
  // A persistent release failure is loud when the mutation itself succeeded
  // (returning success while leaking a live lock parks every later waiter).
  // When the mutation already failed, its error stays authoritative.
  if (releaseError !== undefined && !fnFailed) {
    throw releaseError;
  }
  if (fnFailed) {
    throw fnError;
  }
  return result as T;
}

function exclusiveReplanReservationPath(vaultPath: string, planId: string): string {
  return path.join(
    vaultPath,
    ".runtime",
    "thread-locks",
    `${lockKey(planId)}.exclusive-replan.json`,
  );
}

/**
 * Exclusive replan announces itself before the persist lock. Kick/drain
 * must yield while this is live so an accepting process that stays up
 * cannot journal slice.started on a parent that replan then supersedes.
 * Dead reservation owners are not live — kick still drains when no
 * exclusive replan is in flight. THREAD_BUSY stays overflow, not the default.
 */
export async function exclusiveReplanReservationIsLive(
  vaultPath: string,
  planId: string,
  options: Pick<
    ThreadLockOptions,
    "isProcessAlive" | "getProcessStartMarker" | "staleMs" | "now"
  > = {},
): Promise<boolean> {
  const reservationPath = exclusiveReplanReservationPath(vaultPath, planId);
  let raw: string;
  try {
    raw = await readFile(reservationPath, "utf8");
  } catch (error) {
    if (isErrno(error, "ENOENT")) return false;
    throw error;
  }
  const owner = parseOwner(raw);
  if (owner === undefined) {
    // Empty/unparseable is the publish window (tmp+link or wx create
    // before JSON lands). Same as withThreadLock: invalid owners are
    // live until stale grace, then kick may drain and acquire may reap.
    return !(await reservationOlderThanStale(reservationPath, options));
  }
  const isProcessAlive = options.isProcessAlive ?? processAlive;
  const getProcessStartMarker =
    options.getProcessStartMarker ?? readProcessStartMarker;
  return ownerLooksLive(owner, isProcessAlive, getProcessStartMarker).live;
}

async function reservationOlderThanStale(
  reservationPath: string,
  options: Pick<ThreadLockOptions, "staleMs" | "now">,
): Promise<boolean> {
  const staleMs = Math.max(0, options.staleMs ?? DEFAULT_STALE_MS);
  const now = options.now ?? (() => new Date());
  try {
    const st = await stat(reservationPath);
    return now().getTime() - st.mtimeMs > staleMs;
  } catch (error) {
    if (isErrno(error, "ENOENT")) return false;
    throw error;
  }
}

/**
 * Publish a complete owner JSON onto the reservation path. Prefer
 * tmp+link so dest never appears empty. Filesystems that reject hard
 * links (NFS/SMB/virtiofs) fall back to exclusive wx write — the
 * young-unparseable stale grace covers that create-before-write window.
 * link/wx EEXIST means another owner already holds the name.
 * wx can create dest then fail the write (ENOSPC/EIO); reap that dest
 * unless EEXIST, same as withThreadLock's owner publish, so kick does
 * not yield on our leftover empty file.
 */
async function publishExclusiveReservation(
  reservationPath: string,
  owner: ThreadLockOwner,
): Promise<void> {
  const payload = `${JSON.stringify(owner)}\n`;
  const tmpPath = `${reservationPath}.${randomUUID()}.tmp`;
  await writeFile(tmpPath, payload, {
    encoding: "utf8",
    mode: 0o600,
  });
  try {
    try {
      await link(tmpPath, reservationPath);
    } catch (error) {
      if (
        !isErrno(error, "ENOTSUP") &&
        !isErrno(error, "EOPNOTSUPP") &&
        !isErrno(error, "EPERM") &&
        !isErrno(error, "EXDEV")
      ) {
        throw error;
      }
      try {
        await writeFile(reservationPath, payload, {
          encoding: "utf8",
          flag: "wx",
          mode: 0o600,
        });
      } catch (writeError) {
        if (!isErrno(writeError, "EEXIST")) {
          await rm(reservationPath, { force: true }).catch(() => {});
        }
        throw writeError;
      }
    }
  } finally {
    await rm(tmpPath, { force: true }).catch(() => {});
  }
}

/**
 * Reserve exclusive replan before withThreadLock so kick sees it and yields.
 * Same persist authority. Replan is never a Q item.
 */
export async function withExclusiveReplanReservation<T>(
  vaultPath: string,
  planId: string,
  operationId: string,
  fn: () => Promise<T>,
  options: ThreadLockOptions = {},
): Promise<T> {
  const waitMs = Math.max(0, options.waitMs ?? DEFAULT_WAIT_MS);
  const staleMs = Math.max(0, options.staleMs ?? DEFAULT_STALE_MS);
  const pollMs = Math.max(0, options.pollMs ?? DEFAULT_POLL_MS);
  const now = options.now ?? (() => new Date());
  const isProcessAlive = options.isProcessAlive ?? processAlive;
  const getProcessStartMarker =
    options.getProcessStartMarker ?? readProcessStartMarker;
  const reservationPath = exclusiveReplanReservationPath(vaultPath, planId);
  const deadline = Date.now() + waitMs;
  const selfMarker = getProcessStartMarker(process.pid);
  const owner: ThreadLockOwner = {
    pid: process.pid,
    operationId,
    acquiredAt: now().toISOString(),
    ...(selfMarker !== undefined ? { processStartMarker: selfMarker } : {}),
  };

  await mkdir(path.dirname(reservationPath), { recursive: true });

  while (true) {
    try {
      await publishExclusiveReservation(reservationPath, owner);
      break;
    } catch (error) {
      if (!isErrno(error, "EEXIST")) {
        throw error;
      }
    }

    let raw: string | undefined;
    try {
      raw = await readFile(reservationPath, "utf8");
    } catch (readError) {
      if (isErrno(readError, "ENOENT")) continue;
      throw readError;
    }
    const observed = parseOwner(raw ?? "");
    if (observed === undefined) {
      // Young unparseable: publish window, not a dead owner. Wait.
      // Aged unparseable: reap like withThreadLock stale_invalid_owner.
      if (await reservationOlderThanStale(reservationPath, { staleMs, now })) {
        await rm(reservationPath, { force: true }).catch(() => {});
        continue;
      }
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) {
        throw new ThreadBusyError();
      }
      await sleep(Math.min(pollMs, remainingMs));
      continue;
    }
    const live = ownerLooksLive(
      observed,
      isProcessAlive,
      getProcessStartMarker,
    ).live;
    if (!live) {
      await rm(reservationPath, { force: true }).catch(() => {});
      continue;
    }
    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) {
      throw new ThreadBusyError(observed);
    }
    await sleep(Math.min(pollMs, remainingMs));
  }

  try {
    return await fn();
  } finally {
    try {
      const current = parseOwner(await readFile(reservationPath, "utf8"));
      if (current?.operationId === operationId) {
        await rm(reservationPath, { force: true });
      }
    } catch {
      // Reservation already gone.
    }
  }
}


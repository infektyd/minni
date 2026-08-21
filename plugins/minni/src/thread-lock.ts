import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { link, mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { appendFileWithFsync } from "./vault.js";

const DEFAULT_WAIT_MS = 5_000;
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

  try {
    return await fn();
  } finally {
    const currentOwner = await readOwner(ownerPath);
    if (currentOwner?.operationId === operationId) {
      await rm(lockDir, { recursive: true, force: true });
    }
  }
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
 * Publish a complete owner JSON onto the reservation path. Write the
 * payload to a tmp file first, then link — dest never appears empty.
 * link fails with EEXIST when another owner already holds the name.
 */
async function publishExclusiveReservation(
  reservationPath: string,
  owner: ThreadLockOwner,
): Promise<void> {
  const tmpPath = `${reservationPath}.${randomUUID()}.tmp`;
  await writeFile(tmpPath, `${JSON.stringify(owner)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  try {
    await link(tmpPath, reservationPath);
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


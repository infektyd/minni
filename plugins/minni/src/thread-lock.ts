import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { mkdir, readdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { appendFileWithFsync } from "./vault.js";

const DEFAULT_WAIT_MS = 5_000;
const DEFAULT_STALE_MS = 120_000;
const DEFAULT_POLL_MS = 25;

/**
 * `waitMs` is the stall budget: max time without lock progress (owner
 * change, release, or stale recovery). It is not a total-wait cap.
 * A FIFO waiter queue lets a Thread-C burst of overlapping starts acquire
 * the same plan lock while the lock keeps moving. THREAD_BUSY is
 * fail-closed overflow for a stuck live owner — not the N=40 default.
 * Do not steal a live owner. Do not silently enlarge DEFAULT_WAIT_MS.
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

interface ThreadLockWaiter {
  ticketId: string;
  operationId: string;
  pid: number;
  enqueuedAt: string;
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


function waitDirFor(locksRoot: string, planId: string): string {
  return path.join(locksRoot, `${lockKey(planId)}.wait`);
}

function parseWaiter(value: string): ThreadLockWaiter | undefined {
  try {
    const waiter = JSON.parse(value) as Partial<ThreadLockWaiter>;
    if (
      typeof waiter.ticketId !== "string" ||
      waiter.ticketId.length === 0 ||
      typeof waiter.operationId !== "string" ||
      waiter.operationId.length === 0 ||
      !Number.isInteger(waiter.pid) ||
      (waiter.pid ?? 0) <= 0 ||
      typeof waiter.enqueuedAt !== "string" ||
      !Number.isFinite(Date.parse(waiter.enqueuedAt))
    ) {
      return undefined;
    }
    const parsed: ThreadLockWaiter = {
      ticketId: waiter.ticketId,
      operationId: waiter.operationId,
      pid: waiter.pid as number,
      enqueuedAt: waiter.enqueuedAt,
    };
    if (
      typeof waiter.processStartMarker === "string" &&
      waiter.processStartMarker.length > 0
    ) {
      parsed.processStartMarker = waiter.processStartMarker;
    }
    return parsed;
  } catch {
    return undefined;
  }
}

function waiterLooksLive(
  waiter: ThreadLockWaiter,
  isProcessAlive: (pid: number) => boolean,
  getProcessStartMarker: (pid: number) => string | undefined,
): boolean {
  return ownerLooksLive(
    {
      pid: waiter.pid,
      operationId: waiter.operationId,
      acquiredAt: waiter.enqueuedAt,
      ...(waiter.processStartMarker !== undefined
        ? { processStartMarker: waiter.processStartMarker }
        : {}),
    },
    isProcessAlive,
    getProcessStartMarker,
  ).live;
}

function progressKey(
  lockExists: boolean,
  owner: ThreadLockOwner | undefined,
): string {
  if (!lockExists) return "free";
  if (owner === undefined) return "held-unknown";
  return `owner:${owner.pid}:${owner.operationId}:${owner.acquiredAt}`;
}

async function listLivingWaiters(
  waitDir: string,
  isProcessAlive: (pid: number) => boolean,
  getProcessStartMarker: (pid: number) => string | undefined,
): Promise<ThreadLockWaiter[]> {
  let names: string[];
  try {
    names = await readdir(waitDir);
  } catch (error) {
    if (isErrno(error, "ENOENT")) return [];
    throw error;
  }
  const waiters: ThreadLockWaiter[] = [];
  for (const name of names) {
    if (!name.endsWith(".json")) continue;
    const ticketPath = path.join(waitDir, name);
    let waiter: ThreadLockWaiter | undefined;
    try {
      waiter = parseWaiter(await readFile(ticketPath, "utf8"));
    } catch {
      waiter = undefined;
    }
    if (waiter === undefined || !waiterLooksLive(waiter, isProcessAlive, getProcessStartMarker)) {
      await rm(ticketPath, { force: true }).catch(() => {});
      continue;
    }
    waiters.push(waiter);
  }
  waiters.sort((a, b) => {
    const byTime = a.enqueuedAt.localeCompare(b.enqueuedAt);
    return byTime !== 0 ? byTime : a.ticketId.localeCompare(b.ticketId);
  });
  return waiters;
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
  const waitDir = waitDirFor(locksRoot, planId);
  const selfMarker = getProcessStartMarker(process.pid);
  const owner: ThreadLockOwner = {
    pid: process.pid,
    operationId,
    acquiredAt: now().toISOString(),
    ...(selfMarker !== undefined ? { processStartMarker: selfMarker } : {}),
  };
  let observedOwner: ThreadLockOwner | undefined;
  let ticketPath: string | undefined;
  let lastProgressKey: string | undefined;
  let stallDeadline = Date.now() + waitMs;

  await mkdir(locksRoot, { recursive: true });

  const enqueueWaiter = async (): Promise<void> => {
    if (ticketPath !== undefined) return;
    const ticketId = randomUUID();
    const waiter: ThreadLockWaiter = {
      ticketId,
      operationId,
      pid: process.pid,
      enqueuedAt: now().toISOString(),
      ...(selfMarker !== undefined ? { processStartMarker: selfMarker } : {}),
    };
    await mkdir(waitDir, { recursive: true });
    const nextPath = path.join(waitDir, `${ticketId}.json`);
    await writeFile(nextPath, `${JSON.stringify(waiter)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    });
    ticketPath = nextPath;
  };

  const noteProgress = (lockExists: boolean, current: ThreadLockOwner | undefined) => {
    const key = progressKey(lockExists, current);
    if (lastProgressKey === undefined) {
      lastProgressKey = key;
      return;
    }
    if (key !== lastProgressKey) {
      lastProgressKey = key;
      stallDeadline = Date.now() + waitMs;
    }
  };

  try {
    while (true) {
      // Waiter liveness is the OS process that queued, not the lock-owner
      // injectors (those exist to recover/refuse the current owner).
      const living = ticketPath
        ? await listLivingWaiters(waitDir, processAlive, readProcessStartMarker)
        : [];
      const myTicket =
        ticketPath === undefined
          ? undefined
          : path.basename(ticketPath, ".json");
      const isHead =
        myTicket === undefined ||
        living.length === 0 ||
        living[0]?.ticketId === myTicket;

      if (isHead) {
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
      }

      await enqueueWaiter();

      observedOwner = await readOwner(ownerPath);
      let lockExists = false;
      let ageMs: number | undefined;
      try {
        const lockStat = await stat(lockDir);
        lockExists = true;
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
        noteProgress(false, undefined);
        continue;
      }

      noteProgress(lockExists, observedOwner);
      const remainingMs = stallDeadline - Date.now();
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
  } finally {
    if (ticketPath !== undefined) {
      await rm(ticketPath, { force: true }).catch(() => {});
    }
  }
}

import { createHash, randomUUID } from "node:crypto";
import { mkdir, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { ThreadBusyError, readProcessStartMarker, type ThreadLockOwner } from "./thread-lock.js";

/**
 * Per-Thread dump-and-return queue for worker writes.
 *
 * When the plan lock is held, a worker write is ACCEPTED onto this queue and
 * the caller returns immediately. Accepted is not applied: journal seq, ready
 * set, and slice state change only when the daemon drains one item at a time
 * under `withThreadLock` (the existing one persist authority).
 *
 * Replan is exclusive and is never a queue item.
 * THREAD_BUSY is fail-closed overflow: queue full, or drain stuck.
 * Do not silently enlarge the lock DEFAULT_WAIT_MS. Do not steal a live owner.
 */
export const DEFAULT_QUEUE_MAX = 256;
export const DEFAULT_DRAIN_STUCK_MS = 5_000;

export interface QueuedWorkerWrite {
  ticketId: string;
  enqueuedAt: string;
  planId: string;
  sliceId: string;
  workerAgentId: string;
  token: string;
  idempotencyKey: string;
  action: unknown;
  applyNow?: string;
}

export interface WorkerWriteDrainProgress {
  headTicketId: string;
  remaining: number;
  at: string;
}

function lockKey(planId: string): string {
  return createHash("sha256").update(planId).digest("hex").slice(0, 32);
}

function idempotencyFileName(idempotencyKey: string): string {
  return `${createHash("sha256").update(idempotencyKey).digest("hex").slice(0, 32)}.json`;
}

export function queuedWriteActionName(item: QueuedWorkerWrite): string {
  const action = item.action;
  if (
    typeof action === "object" &&
    action !== null &&
    "action" in action &&
    typeof (action as { action?: unknown }).action === "string"
  ) {
    return (action as { action: string }).action;
  }
  return "";
}

/** Start for a slice drains before that slice's complete. Not naive FIFO. */
export function pickNextQueuedWorkerWrite(
  items: QueuedWorkerWrite[],
): QueuedWorkerWrite | undefined {
  if (items.length === 0) return undefined;
  const startSlices = new Set(
    items
      .filter((item) => queuedWriteActionName(item) === "start")
      .map((item) => item.sliceId),
  );
  for (const item of items) {
    if (queuedWriteActionName(item) === "complete" && startSlices.has(item.sliceId)) {
      continue;
    }
    return item;
  }
  return items[0];
}

function isErrno(error: unknown, code: string): boolean {
  return (error as NodeJS.ErrnoException).code === code;
}

function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
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

function ownerLooksLive(owner: ThreadLockOwner): boolean {
  if (!processAlive(owner.pid)) return false;
  if (typeof owner.processStartMarker === "string" && owner.processStartMarker.length > 0) {
    const liveMarker = readProcessStartMarker(owner.pid);
    if (
      typeof liveMarker === "string" &&
      liveMarker.length > 0 &&
      liveMarker !== owner.processStartMarker
    ) {
      return false;
    }
  }
  return true;
}

export function workerWriteQueueDir(vaultPath: string, planId: string): string {
  return path.join(
    vaultPath,
    ".runtime",
    "thread-locks",
    `${lockKey(planId)}.q`,
  );
}

function progressPath(vaultPath: string, planId: string): string {
  return path.join(workerWriteQueueDir(vaultPath, planId), "progress.json");
}

function lockDirFor(vaultPath: string, planId: string): string {
  return path.join(
    vaultPath,
    ".runtime",
    "thread-locks",
    `${lockKey(planId)}.lock`,
  );
}

function parseQueuedWrite(value: string): QueuedWorkerWrite | undefined {
  try {
    const item = JSON.parse(value) as Partial<QueuedWorkerWrite>;
    if (
      typeof item.ticketId !== "string" ||
      item.ticketId.length === 0 ||
      typeof item.enqueuedAt !== "string" ||
      !Number.isFinite(Date.parse(item.enqueuedAt)) ||
      typeof item.planId !== "string" ||
      item.planId.length === 0 ||
      typeof item.sliceId !== "string" ||
      item.sliceId.length === 0 ||
      typeof item.workerAgentId !== "string" ||
      item.workerAgentId.length === 0 ||
      typeof item.token !== "string" ||
      item.token.length === 0 ||
      typeof item.idempotencyKey !== "string" ||
      item.idempotencyKey.length === 0 ||
      item.action === undefined
    ) {
      return undefined;
    }
    const parsed: QueuedWorkerWrite = {
      ticketId: item.ticketId,
      enqueuedAt: item.enqueuedAt,
      planId: item.planId,
      sliceId: item.sliceId,
      workerAgentId: item.workerAgentId,
      token: item.token,
      idempotencyKey: item.idempotencyKey,
      action: item.action,
    };
    if (typeof item.applyNow === "string" && Number.isFinite(Date.parse(item.applyNow))) {
      parsed.applyNow = item.applyNow;
    }
    return parsed;
  } catch {
    return undefined;
  }
}

export async function listQueuedWorkerWrites(
  vaultPath: string,
  planId: string,
): Promise<QueuedWorkerWrite[]> {
  const dir = workerWriteQueueDir(vaultPath, planId);
  let names: string[];
  try {
    names = await readdir(dir);
  } catch (error) {
    if (isErrno(error, "ENOENT")) return [];
    throw error;
  }
  const items: QueuedWorkerWrite[] = [];
  for (const name of names) {
    if (!name.endsWith(".json") || name === "progress.json") continue;
    const filePath = path.join(dir, name);
    let item: QueuedWorkerWrite | undefined;
    try {
      item = parseQueuedWrite(await readFile(filePath, "utf8"));
    } catch {
      item = undefined;
    }
    if (item === undefined) {
      await rm(filePath, { force: true }).catch(() => {});
      continue;
    }
    items.push(item);
  }
  items.sort((a, b) => {
    const byTime = a.enqueuedAt.localeCompare(b.enqueuedAt);
    return byTime !== 0 ? byTime : a.ticketId.localeCompare(b.ticketId);
  });
  return items;
}

export async function readWorkerWriteDrainProgress(
  vaultPath: string,
  planId: string,
): Promise<WorkerWriteDrainProgress | undefined> {
  try {
    const raw = JSON.parse(await readFile(progressPath(vaultPath, planId), "utf8")) as Partial<WorkerWriteDrainProgress>;
    if (
      typeof raw.headTicketId !== "string" ||
      raw.headTicketId.length === 0 ||
      !Number.isInteger(raw.remaining) ||
      typeof raw.at !== "string" ||
      !Number.isFinite(Date.parse(raw.at))
    ) {
      return undefined;
    }
    return {
      headTicketId: raw.headTicketId,
      remaining: raw.remaining as number,
      at: raw.at,
    };
  } catch {
    return undefined;
  }
}

export async function recordWorkerWriteDrainProgress(
  vaultPath: string,
  planId: string,
  progress: WorkerWriteDrainProgress,
): Promise<void> {
  const dir = workerWriteQueueDir(vaultPath, planId);
  await mkdir(dir, { recursive: true });
  await writeFile(progressPath(vaultPath, planId), `${JSON.stringify(progress)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
}

export async function removeQueuedWorkerWrite(
  vaultPath: string,
  planId: string,
  idempotencyKey: string,
): Promise<void> {
  const filePath = path.join(
    workerWriteQueueDir(vaultPath, planId),
    idempotencyFileName(idempotencyKey),
  );
  await rm(filePath, { force: true }).catch(() => {});
}

export async function findQueuedWorkerWrite(
  vaultPath: string,
  planId: string,
  idempotencyKey: string,
): Promise<QueuedWorkerWrite | undefined> {
  const filePath = path.join(
    workerWriteQueueDir(vaultPath, planId),
    idempotencyFileName(idempotencyKey),
  );
  try {
    return parseQueuedWrite(await readFile(filePath, "utf8"));
  } catch (error) {
    if (isErrno(error, "ENOENT")) return undefined;
    throw error;
  }
}

async function lockHeldLive(vaultPath: string, planId: string): Promise<boolean> {
  const lockDir = lockDirFor(vaultPath, planId);
  try {
    await stat(lockDir);
  } catch (error) {
    if (isErrno(error, "ENOENT")) return false;
    throw error;
  }
  let raw: string;
  try {
    raw = await readFile(path.join(lockDir, "owner.json"), "utf8");
  } catch {
    return true;
  }
  const owner = parseOwner(raw);
  if (owner === undefined) return true;
  return ownerLooksLive(owner);
}

export async function isWorkerWriteDrainStuck(
  vaultPath: string,
  planId: string,
  now: Date,
  stuckMs: number = DEFAULT_DRAIN_STUCK_MS,
): Promise<boolean> {
  const items = await listQueuedWorkerWrites(vaultPath, planId);
  if (items.length === 0) return false;
  if (!(await lockHeldLive(vaultPath, planId))) return false;
  const head = items[0];
  const progress = await readWorkerWriteDrainProgress(vaultPath, planId);
  const referenceAt =
    progress && progress.headTicketId === head.ticketId
      ? Date.parse(progress.at)
      : Date.parse(head.enqueuedAt);
  return now.getTime() - referenceAt > stuckMs;
}

export interface EnqueueWorkerWriteInput {
  vaultPath: string;
  planId: string;
  sliceId: string;
  workerAgentId: string;
  token: string;
  idempotencyKey: string;
  action: unknown;
  now?: Date;
  applyNow?: Date;
  queueMax?: number;
  stuckMs?: number;
}

export async function enqueueWorkerWrite(
  input: EnqueueWorkerWriteInput,
): Promise<{ alreadyQueued: boolean; item: QueuedWorkerWrite }> {
  const now = input.now ?? new Date();
  const queueMax = input.queueMax ?? DEFAULT_QUEUE_MAX;
  const stuckMs = input.stuckMs ?? DEFAULT_DRAIN_STUCK_MS;
  const dir = workerWriteQueueDir(input.vaultPath, input.planId);
  await mkdir(dir, { recursive: true });
  const filePath = path.join(dir, idempotencyFileName(input.idempotencyKey));
  try {
    const existing = parseQueuedWrite(await readFile(filePath, "utf8"));
    if (existing !== undefined) {
      return { alreadyQueued: true, item: existing };
    }
  } catch (error) {
    if (!isErrno(error, "ENOENT")) throw error;
  }

  const items = await listQueuedWorkerWrites(input.vaultPath, input.planId);
  if (items.length >= queueMax) {
    throw new ThreadBusyError();
  }
  if (await isWorkerWriteDrainStuck(input.vaultPath, input.planId, now, stuckMs)) {
    const ownerPath = path.join(lockDirFor(input.vaultPath, input.planId), "owner.json");
    let owner: ThreadLockOwner | undefined;
    try {
      owner = parseOwner(await readFile(ownerPath, "utf8"));
    } catch {
      owner = undefined;
    }
    throw new ThreadBusyError(owner);
  }

  const item: QueuedWorkerWrite = {
    ticketId: randomUUID(),
    enqueuedAt: now.toISOString(),
    planId: input.planId,
    sliceId: input.sliceId,
    workerAgentId: input.workerAgentId,
    token: input.token,
    idempotencyKey: input.idempotencyKey,
    action: input.action,
    ...(input.applyNow instanceof Date ? { applyNow: input.applyNow.toISOString() } : {}),
  };
  try {
    await writeFile(filePath, `${JSON.stringify(item)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    });
  } catch (error) {
    if (isErrno(error, "EEXIST")) {
      const existing = parseQueuedWrite(await readFile(filePath, "utf8"));
      if (existing !== undefined) {
        return { alreadyQueued: true, item: existing };
      }
    }
    throw error;
  }
  return { alreadyQueued: false, item };
}

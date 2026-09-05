import { createHash, randomUUID } from "node:crypto";
import { link, mkdir, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { ThreadBusyError, readProcessStartMarker, type ThreadLockOwner } from "./thread-lock.js";

/**
 * Per-Thread dump-and-return queue for worker writes.
 *
 * When the plan lock is held, a worker write is ACCEPTED onto this queue and
 * the caller returns immediately. Accepted is not applied: journal seq, ready
 * set, and slice state change only when a drain applies one item at a time
 * under `withThreadLock` (the existing one persist authority). That drain
 * must outlive the accepting process: Q + stamp stay on disk, and a later
 * process (not that in-process kick) can apply. Not a second graph.
 *
 * Replan is exclusive and is never a queue item.
 * THREAD_BUSY is fail-closed overflow: queue full, or drain stuck.
 * Do not silently enlarge the lock DEFAULT_WAIT_MS. Do not steal a live owner.
 *
 * Q JSON stores a token digest only (like the start-accepted stamp). The raw
 * claim token stays in the existing claim-secret store
 * (`.runtime/thread-claims/`). Do not write it onto the journal or stamp.
 */
export const DEFAULT_QUEUE_MAX = 256;
export const DEFAULT_DRAIN_STUCK_MS = 5_000;

export interface QueuedWorkerWrite {
  ticketId: string;
  enqueuedAt: string;
  planId: string;
  sliceId: string;
  workerAgentId: string;
  /** SHA-256 hex of the presented claim token. Never the raw token. */
  tokenDigest: string;
  idempotencyKey: string;
  action: unknown;
  applyNow?: string;
  /** Slice generation at accept. Leftover tickets must not apply after this advances. */
  generation?: number;
  /**
   * Accepting process pid at enqueue. Standing drain uses this to yield a
   * live start while that process is still up in the accept→reserve window.
   * Legacy tickets omit it and apply as today (no defer).
   */
  acceptorPid?: number;
  /** Optional OS start marker for acceptorPid. Mismatch means stale/reused pid. */
  processStartMarker?: string;
}

export interface WorkerWriteDrainProgress {
  headTicketId: string;
  remaining: number;
  at: string;
}

const TOKEN_DIGEST_PATTERN = /^[0-9a-f]{64}$/;

function lockKey(planId: string): string {
  return createHash("sha256").update(planId).digest("hex").slice(0, 32);
}

function tokenDigestOf(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

function queuedTokenDigest(item: Partial<QueuedWorkerWrite> & { token?: unknown }): string | undefined {
  if (typeof item.tokenDigest === "string" && TOKEN_DIGEST_PATTERN.test(item.tokenDigest)) {
    return item.tokenDigest;
  }
  // Legacy Q files stored the raw token. Hash in memory; do not keep it.
  if (typeof item.token === "string" && item.token.length > 0) {
    return tokenDigestOf(item.token);
  }
  return undefined;
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

/** True when the ticket recorded an acceptor pid (not a legacy Q file). */
export function queuedWriteHasAcceptor(item: QueuedWorkerWrite): boolean {
  return Number.isInteger(item.acceptorPid) && (item.acceptorPid ?? 0) > 0;
}

/**
 * Reuses ownerLooksLive: pid dead or processStartMarker mismatch is stale.
 * Legacy tickets without acceptor pid are not live acceptors.
 */
export function queuedWriteAcceptorLooksLive(item: QueuedWorkerWrite): boolean {
  if (!queuedWriteHasAcceptor(item) || item.acceptorPid === undefined) return false;
  const owner: ThreadLockOwner = {
    pid: item.acceptorPid,
    operationId: "queued-write-acceptor",
    acquiredAt: item.enqueuedAt,
  };
  if (
    typeof item.processStartMarker === "string" &&
    item.processStartMarker.length > 0
  ) {
    owner.processStartMarker = item.processStartMarker;
  }
  return ownerLooksLive(owner);
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
      typeof item.idempotencyKey !== "string" ||
      item.idempotencyKey.length === 0 ||
      item.action === undefined
    ) {
      return undefined;
    }
    const tokenDigest = queuedTokenDigest(item);
    if (tokenDigest === undefined) {
      return undefined;
    }
    const parsed: QueuedWorkerWrite = {
      ticketId: item.ticketId,
      enqueuedAt: item.enqueuedAt,
      planId: item.planId,
      sliceId: item.sliceId,
      workerAgentId: item.workerAgentId,
      tokenDigest,
      idempotencyKey: item.idempotencyKey,
      action: item.action,
    };
    if (typeof item.applyNow === "string" && Number.isFinite(Date.parse(item.applyNow))) {
      parsed.applyNow = item.applyNow;
    }
    if (item.generation !== undefined) {
      if (!Number.isSafeInteger(item.generation) || (item.generation ?? -1) < 0) {
        return undefined;
      }
      parsed.generation = item.generation;
    }
    if (Number.isInteger(item.acceptorPid) && (item.acceptorPid ?? 0) > 0) {
      parsed.acceptorPid = item.acceptorPid as number;
    }
    if (
      typeof item.processStartMarker === "string" &&
      item.processStartMarker.length > 0
    ) {
      parsed.processStartMarker = item.processStartMarker;
    }
    return parsed;
  } catch {
    return undefined;
  }
}

export async function listPendingWorkerWritePlanIds(
  vaultPath: string,
): Promise<string[]> {
  const root = path.join(path.resolve(vaultPath), ".runtime", "thread-locks");
  let names: string[];
  try {
    names = await readdir(root);
  } catch (error) {
    if (isErrno(error, "ENOENT")) return [];
    throw error;
  }
  const planIds = new Set<string>();
  for (const name of names) {
    if (!name.endsWith(".q")) continue;
    const dir = path.join(root, name);
    let files: string[];
    try {
      files = await readdir(dir);
    } catch {
      continue;
    }
    for (const file of files) {
      if (!file.endsWith(".json") || file === "progress.json") continue;
      let item: QueuedWorkerWrite | undefined;
      try {
        item = parseQueuedWrite(await readFile(path.join(dir, file), "utf8"));
      } catch {
        item = undefined;
      }
      if (item !== undefined) {
        planIds.add(item.planId);
        break;
      }
    }
  }
  return [...planIds].sort();
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
    } catch (error) {
      // A concurrent drain may already have removed this ticket. Other read
      // failures do not establish corruption and must never delete accepted
      // work (for example a temporary permission or I/O failure).
      if (isErrno(error, "ENOENT")) continue;
      throw error;
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
  return queueSnapshotLooksStuck(vaultPath, planId, now, stuckMs, items);
}

async function queueSnapshotLooksStuck(
  vaultPath: string,
  planId: string,
  now: Date,
  stuckMs: number,
  items: QueuedWorkerWrite[],
): Promise<boolean> {
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
  /** Presented by the worker. Persisted as tokenDigest only — never raw on Q. */
  token: string;
  idempotencyKey: string;
  action: unknown;
  now?: Date;
  applyNow?: Date;
  queueMax?: number;
  stuckMs?: number;
  generation?: number;
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
  // Capacity and stuck detection use the same validated snapshot from this
  // enqueue attempt. Never retain it across requests or use it to select work
  // for drain; drain still reads the authoritative queue under its lock.
  if (await queueSnapshotLooksStuck(input.vaultPath, input.planId, now, stuckMs, items)) {
    const ownerPath = path.join(lockDirFor(input.vaultPath, input.planId), "owner.json");
    let owner: ThreadLockOwner | undefined;
    try {
      owner = parseOwner(await readFile(ownerPath, "utf8"));
    } catch {
      owner = undefined;
    }
    throw new ThreadBusyError(owner);
  }

  if (
    input.generation !== undefined &&
    (!Number.isSafeInteger(input.generation) || input.generation < 0)
  ) {
    throw new Error("queued worker write generation is invalid");
  }
  const acceptorPid = process.pid;
  const processStartMarker = readProcessStartMarker(acceptorPid);
  const item: QueuedWorkerWrite = {
    ticketId: randomUUID(),
    enqueuedAt: now.toISOString(),
    planId: input.planId,
    sliceId: input.sliceId,
    workerAgentId: input.workerAgentId,
    tokenDigest: tokenDigestOf(input.token),
    idempotencyKey: input.idempotencyKey,
    action: input.action,
    acceptorPid,
    ...(typeof processStartMarker === "string" && processStartMarker.length > 0
      ? { processStartMarker }
      : {}),
    ...(input.applyNow instanceof Date ? { applyNow: input.applyNow.toISOString() } : {}),
    ...(input.generation !== undefined ? { generation: input.generation } : {}),
  };
  // Readers prune malformed .json tickets. Finish a private sibling first so
  // they cannot observe (and remove) an empty or partially written final file.
  // link publishes without replacing an existing idempotency winner; rename
  // would overwrite that winner. Staging names are outside the .json scan.
  const stagingPath = path.join(dir, `.${randomUUID()}.tmp`);
  let cleanupStaging = true;
  try {
    try {
      await writeFile(stagingPath, `${JSON.stringify(item)}\n`, {
        encoding: "utf8",
        flag: "wx",
        mode: 0o600,
      });
    } catch (error) {
      // A collision is not our file. Other write failures can leave a partial
      // staging file, which still needs cleanup before propagating the error.
      if (isErrno(error, "EEXIST")) cleanupStaging = false;
      throw error;
    }
    try {
      await link(stagingPath, filePath);
    } catch (error) {
      if (isErrno(error, "EEXIST")) {
        const existing = parseQueuedWrite(await readFile(filePath, "utf8"));
        if (existing !== undefined) {
          return { alreadyQueued: true, item: existing };
        }
      }
      throw error;
    }
  } finally {
    if (cleanupStaging) await rm(stagingPath, { force: true }).catch(() => {});
  }
  return { alreadyQueued: false, item };
}

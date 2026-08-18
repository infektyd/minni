import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

const DEFAULT_WAIT_MS = 5_000;
const DEFAULT_STALE_MS = 120_000;
const DEFAULT_POLL_MS = 25;

export interface ThreadLockOwner {
  pid: number;
  operationId: string;
  acquiredAt: string;
}

export interface ThreadLockOptions {
  waitMs?: number;
  staleMs?: number;
  pollMs?: number;
  now?: () => Date;
  isProcessAlive?: (pid: number) => boolean;
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
    return owner as ThreadLockOwner;
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
  const locksRoot = path.join(vaultPath, ".runtime", "thread-locks");
  const lockDir = path.join(locksRoot, `${lockKey(planId)}.lock`);
  const ownerPath = path.join(lockDir, "owner.json");
  const deadline = Date.now() + waitMs;
  const owner: ThreadLockOwner = {
    pid: process.pid,
    operationId,
    acquiredAt: now().toISOString(),
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

    if (
      ageMs !== undefined &&
      ageMs > staleMs &&
      observedOwner !== undefined &&
      !isProcessAlive(observedOwner.pid)
    ) {
      const quarantineDir = `${lockDir}.stale-${randomUUID()}`;
      try {
        await rename(lockDir, quarantineDir);
      } catch (error) {
        if (!isErrno(error, "ENOENT")) {
          throw error;
        }
      }
      await rm(quarantineDir, { recursive: true, force: true }).catch(() => {});
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

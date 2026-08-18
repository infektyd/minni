import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import fs from "node:fs";
import {
  mkdir,
  mkdtemp,
  readFile,
  rm,
  utimes,
  writeFile,
} from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { withThreadLock } from "../dist/thread-lock.js";

const execFileAsync = promisify(execFile);

function lockDirFor(root, planId) {
  const key = createHash("sha256").update(planId).digest("hex").slice(0, 32);
  return path.join(root, ".runtime", "thread-locks", `${key}.lock`);
}

async function seedOwner(root, planId, owner) {
  const lockDir = lockDirFor(root, planId);
  await mkdir(lockDir, { recursive: true });
  await writeFile(
    path.join(lockDir, "owner.json"),
    `${JSON.stringify(owner)}\n`,
    { mode: 0o600 },
  );
  const old = new Date(owner.acquiredAt);
  await utimes(lockDir, old, old);
}

async function seedOwnerlessLock(root, planId, ownerContent) {
  const lockDir = lockDirFor(root, planId);
  await mkdir(lockDir, { recursive: true });
  if (ownerContent !== undefined) {
    await writeFile(path.join(lockDir, "owner.json"), ownerContent, {
      mode: 0o600,
    });
  }
  const old = new Date("2026-01-01T00:00:00.000Z");
  await utimes(lockDir, old, old);
  return lockDir;
}

test("withThreadLock serializes two OS processes for one plan", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const log = path.join(root, "critical.log");
  const worker = new URL("./fixtures/thread-lock-worker.mjs", import.meta.url);
  const args = [fileURLToPath(worker), root, "plan-shared", log];

  const [a, b] = await Promise.all([
    execFileAsync(process.execPath, args),
    execFileAsync(process.execPath, args),
  ]);

  assert.equal(a.stderr, "");
  assert.equal(b.stderr, "");
  const intervals = (await readFile(log, "utf8"))
    .trim()
    .split("\n")
    .map(JSON.parse);
  assert.equal(intervals.length, 2);
  const [first, second] = intervals.sort((x, y) => x.entered - y.entered);
  assert.ok(first.left <= second.entered, JSON.stringify(intervals));
});

test("withThreadLock never steals a live owner's old lock", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await seedOwner(root, "plan-live", {
    pid: process.pid,
    operationId: "live-op",
    acquiredAt: "2026-01-01T00:00:00.000Z",
  });

  await assert.rejects(
    withThreadLock(root, "plan-live", "contender", async () => undefined, {
      waitMs: 40,
      staleMs: 1,
      pollMs: 5,
      isProcessAlive: () => true,
    }),
    (error) => error?.code === "THREAD_BUSY",
  );
});

test("withThreadLock recovers only a stale dead-owner directory", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await seedOwner(root, "plan-dead", {
    pid: 999999,
    operationId: "dead-op",
    acquiredAt: "2026-01-01T00:00:00.000Z",
  });
  let entered = false;

  await withThreadLock(
    root,
    "plan-dead",
    "recovery",
    async () => {
      entered = true;
    },
    {
      staleMs: 1,
      isProcessAlive: () => false,
    },
  );

  assert.equal(entered, true);
});

test("withThreadLock atomically quarantines a stale lock with no owner file", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const lockDir = await seedOwnerlessLock(root, "plan-ownerless");
  const originalRename = fs.promises.rename;
  const renames = [];
  fs.promises.rename = async (from, to) => {
    renames.push({ from, to });
    return originalRename(from, to);
  };
  syncBuiltinESMExports();
  let entered = false;

  try {
    await withThreadLock(
      root,
      "plan-ownerless",
      "ownerless-recovery",
      async () => {
        entered = true;
      },
      {
        waitMs: 40,
        staleMs: 1,
        pollMs: 5,
        isProcessAlive: () => {
          throw new Error("ownerless recovery must not check a PID");
        },
      },
    );
  } finally {
    fs.promises.rename = originalRename;
    syncBuiltinESMExports();
  }

  assert.equal(entered, true);
  assert.equal(renames.length, 1);
  assert.equal(renames[0].from, lockDir);
  assert.match(
    renames[0].to,
    new RegExp(`^${lockDir.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\.stale-`),
  );
});

test("withThreadLock recovers a stale lock with invalid owner JSON", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await seedOwnerlessLock(root, "plan-invalid-owner", "{not valid JSON");
  let entered = false;

  await withThreadLock(
    root,
    "plan-invalid-owner",
    "invalid-owner-recovery",
    async () => {
      entered = true;
    },
    {
      waitMs: 40,
      staleMs: 1,
      pollMs: 5,
      isProcessAlive: () => {
        throw new Error("invalid-owner recovery must not check a PID");
      },
    },
  );

  assert.equal(entered, true);
});

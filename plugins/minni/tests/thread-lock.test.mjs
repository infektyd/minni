import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import {
  mkdir,
  mkdtemp,
  readFile,
  rm,
  utimes,
  writeFile,
} from "node:fs/promises";
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

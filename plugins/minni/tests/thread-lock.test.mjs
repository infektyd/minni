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

import {
  exclusiveReplanReservationIsLive,
  withExclusiveReplanReservation,
  withThreadLock,
} from "../dist/thread-lock.js";

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
    processStartMarker: "live-start-marker",
  });

  await assert.rejects(
    withThreadLock(root, "plan-live", "contender", async () => undefined, {
      waitMs: 40,
      staleMs: 1,
      pollMs: 5,
      isProcessAlive: () => true,
      // Same start marker as the seeded owner — still the live incarnation.
      getProcessStartMarker: () => "live-start-marker",
    }),
    (error) => error?.code === "THREAD_BUSY",
  );
});

// Wave 3: PID reuse. Age is stale and kill(pid,0) succeeds, but the recorded
// process-start marker no longer matches the live PID's incarnation — recover.
test("withThreadLock recovers a stale lock when the PID was reused under a new start marker", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await seedOwner(root, "plan-reused-pid", {
    pid: process.pid,
    operationId: "old-incarnation",
    acquiredAt: "2026-01-01T00:00:00.000Z",
    processStartMarker: "boot-generation-1",
  });
  let entered = false;

  await withThreadLock(
    root,
    "plan-reused-pid",
    "after-reuse",
    async () => {
      entered = true;
    },
    {
      waitMs: 40,
      staleMs: 1,
      pollMs: 5,
      isProcessAlive: () => true,
      getProcessStartMarker: () => "boot-generation-2",
    },
  );

  assert.equal(entered, true);

  const { readThreadLockRecoveryAudit } = await import("../dist/thread-lock.js");
  const audit = await readThreadLockRecoveryAudit(root);
  const last = audit.at(-1);
  assert.equal(last?.reason, "stale_pid_reuse");
  assert.equal(last?.previous_owner?.processStartMarker, "boot-generation-1");
});

// Wave 3: when the audit write succeeds, recovery leaves a trail
// distinguishable from theft. The write is best-effort, not required.
test("withThreadLock records a recovery audit when it quarantines a stale lock", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await seedOwner(root, "plan-audit-recovery", {
    pid: 999999,
    operationId: "dead-op",
    acquiredAt: "2026-01-01T00:00:00.000Z",
    processStartMarker: "gone",
  });

  await withThreadLock(
    root,
    "plan-audit-recovery",
    "recovery-with-audit",
    async () => undefined,
    {
      staleMs: 1,
      isProcessAlive: () => false,
    },
  );

  const { readThreadLockRecoveryAudit } = await import("../dist/thread-lock.js");
  const audit = await readThreadLockRecoveryAudit(root);
  assert.ok(Array.isArray(audit) && audit.length >= 1, "expected at least one recovery audit line");
  const last = audit.at(-1);
  assert.equal(last.plan_id, "plan-audit-recovery");
  assert.equal(last.reason, "stale_dead");
  assert.equal(last.previous_owner?.operationId, "dead-op");
  assert.equal(typeof last.at, "string");
});

// Audit is best-effort. A failed recovery.jsonl write must not block
// quarantine; do not claim every recovery appends a line.
test("withThreadLock still recovers when the recovery audit write fails", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await seedOwner(root, "plan-audit-fail", {
    pid: 999999,
    operationId: "dead-op",
    acquiredAt: "2026-01-01T00:00:00.000Z",
    processStartMarker: "gone",
  });
  const auditPath = path.join(root, ".runtime", "thread-locks", "recovery.jsonl");
  await mkdir(auditPath, { recursive: true });
  let entered = false;

  await withThreadLock(
    root,
    "plan-audit-fail",
    "recovery-despite-audit-fail",
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

  const { readThreadLockRecoveryAudit } = await import("../dist/thread-lock.js");
  const audit = await readThreadLockRecoveryAudit(root);
  const last = audit.at(-1);
  assert.equal(last?.reason, "stale_ownerless");
  assert.equal(last?.plan_id, "plan-ownerless");
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

  const { readThreadLockRecoveryAudit } = await import("../dist/thread-lock.js");
  const audit = await readThreadLockRecoveryAudit(root);
  const last = audit.at(-1);
  assert.equal(last?.reason, "stale_invalid_owner");
  assert.equal(last?.plan_id, "plan-invalid-owner");
});

test("withThreadLock keeps DEFAULT_WAIT_MS at 5s; silent bump is fake close", async () => {
  const src = await readFile(new URL("../src/thread-lock.ts", import.meta.url), "utf8");
  assert.match(src, /const DEFAULT_WAIT_MS = 5_000;/);
  assert.match(src, /const DEFAULT_STALE_MS = 120_000;/);
  assert.doesNotMatch(src, /const DEFAULT_WAIT_MS = (?:[6-9]\d{3}|[1-9]\d{4,})/);
  assert.doesNotMatch(src, /FIFO waiter queue/);
  assert.doesNotMatch(src, /stallDeadline/);
});

test("exclusive replan reservation is live only for a live owner", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-exclusive-replan-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  assert.equal(await exclusiveReplanReservationIsLive(root, "plan-x"), false);
  let sawLive = false;
  await withExclusiveReplanReservation(root, "plan-x", "replan-1", async () => {
    sawLive = await exclusiveReplanReservationIsLive(root, "plan-x");
  });
  assert.equal(sawLive, true);
  assert.equal(await exclusiveReplanReservationIsLive(root, "plan-x"), false);
  const key = createHash("sha256").update("plan-x").digest("hex").slice(0, 32);
  const reservationPath = path.join(
    root,
    ".runtime",
    "thread-locks",
    `${key}.exclusive-replan.json`,
  );
  await mkdir(path.dirname(reservationPath), { recursive: true });
  await writeFile(
    reservationPath,
    `${JSON.stringify({
      pid: 2_147_483_647,
      operationId: "dead-replan",
      acquiredAt: "2026-01-01T00:00:00.000Z",
    })}\n`,
    { mode: 0o600 },
  );
  assert.equal(
    await exclusiveReplanReservationIsLive(root, "plan-x", { isProcessAlive: () => false }),
    false,
    "dead exclusive-replan owner must not block kick",
  );
});

test("corrupt exclusive-replan reservation is not live and acquire reaps it", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-exclusive-replan-corrupt-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const key = createHash("sha256").update("plan-x").digest("hex").slice(0, 32);
  const reservationPath = path.join(
    root,
    ".runtime",
    "thread-locks",
    `${key}.exclusive-replan.json`,
  );
  await mkdir(path.dirname(reservationPath), { recursive: true });
  await writeFile(reservationPath, "{not a reservation owner\n", { mode: 0o600 });

  assert.equal(
    await exclusiveReplanReservationIsLive(root, "plan-x"),
    false,
    "unparseable reservation must not look live (kick must still drain)",
  );

  let entered = false;
  await withExclusiveReplanReservation(root, "plan-x", "replan-after-corrupt", async () => {
    entered = true;
    assert.equal(await exclusiveReplanReservationIsLive(root, "plan-x"), true);
  });
  assert.equal(entered, true, "acquire must reap corrupt reservation instead of THREAD_BUSY");
  assert.equal(await exclusiveReplanReservationIsLive(root, "plan-x"), false);
  await assert.rejects(readFile(reservationPath, "utf8"), { code: "ENOENT" });
});

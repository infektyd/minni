// Dump-and-return Thread lock Q. Accepted is not applied.
// Drain is one persist authority. Replan is exclusive, not a Q item.
// THREAD_BUSY is overflow (Q full or drain stuck), not N=40.
// Durable drain outlives the accepting process; in-process kick does not.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { mkdir, mkdtemp, readdir, readFile, rm, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { applySliceDelta, createPlan, journalPathFor, persistPlan, rehydratePlan } from "../dist/plan.js";
import { readThreadEvents } from "../dist/thread-events.js";
import {
  exclusiveReplanReservationIsLive,
  withExclusiveReplanReservation,
  withThreadLock,
} from "../dist/thread-lock.js";
import {
  assignSlice,
  claimSlice,
  deleteClaimSecretsBestEffort,
  drainPendingWorkerWritesForVault,
  drainWorkerWrites,
  isAcceptedWorkerWrite,
  kickWorkerWriteDrain,
  pruneSliceReceiptsAfterPlanMutation,
  revokedClaimIds,
  START_ACCEPTED_RECEIPT_KIND,
  startAcceptedReceiptKey,
  updateClaimedSlice,
  workerUpdateMcpPayload,
  withThreadPlanLock,
} from "../dist/thread-worker.js";
import { readWorkerUpdateReceipt } from "../dist/thread-claims.js";
import {
  DEFAULT_QUEUE_MAX,
  enqueueWorkerWrite,
  isWorkerWriteDrainStuck,
  listPendingWorkerWritePlanIds,
  listQueuedWorkerWrites,
  pickNextQueuedWorkerWrite,
  workerWriteQueueDir,
} from "../dist/thread-write-queue.js";

const THREAD_START = new Date("2026-08-18T12:00:00.000Z");
const TEST_ORCHESTRATOR_ACTOR = "orchestrator-caller";

async function readRawQTickets(vaultPath, planId) {
  const dir = workerWriteQueueDir(vaultPath, planId);
  const names = await readdir(dir);
  const tickets = [];
  for (const name of names) {
    if (!name.endsWith(".json") || name === "progress.json") continue;
    tickets.push(JSON.parse(await readFile(path.join(dir, name), "utf8")));
  }
  return tickets;
}

function assertQTicketHasNoRawToken(ticket, rawToken) {
  assert.equal("token" in ticket, false, "Q JSON must omit raw claim token");
  assert.equal(typeof ticket.tokenDigest, "string");
  assert.match(ticket.tokenDigest, /^[0-9a-f]{64}$/);
  assert.notEqual(ticket.tokenDigest, rawToken);
  assert.equal(
    JSON.stringify(ticket).includes(rawToken),
    false,
    "Q JSON must not contain the raw claim token",
  );
  assert.equal(
    ticket.tokenDigest,
    createHash("sha256").update(rawToken).digest("hex"),
  );
}


async function burstFixture(t, n) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), `minni-thread-lock-q-${n}-`));
  t.after(async () => {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      try {
        await rm(vaultPath, { recursive: true, force: true });
        return;
      } catch (error) {
        if (error?.code !== "ENOTEMPTY" && error?.code !== "EBUSY") throw error;
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    await rm(vaultPath, { recursive: true, force: true });
  });
  const slices = Array.from({ length: n }, (_, index) => ({
    id: `s${index}`,
    title: `Slice ${index}`,
  }));
  const created = await createPlan(
    {
      goal: `Dump-and-return lock Q burst N=${n}`,
      slices,
      vaultPath,
    },
    {
      vaultPath,
      now: () => new Date(THREAD_START),
    },
  );
  return {
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    n,
  };
}

async function assignAndClaimAll(fixture) {
  const claims = [];
  for (let index = 0; index < fixture.n; index += 1) {
    await assignSlice({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: `s${index}`,
      actorAgentId: TEST_ORCHESTRATOR_ACTOR,
      workerAgentId: `worker-${index}`,
      now: THREAD_START,
    });
    const claim = await claimSlice({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: `s${index}`,
      workerAgentId: `worker-${index}`,
      idempotencyKey: `claim-${index}`,
      now: THREAD_START,
    });
    claims.push(claim);
  }
  return claims;
}

function settle(promise, index) {
  return promise
    .then((value) => ({
      index,
      ok: true,
      accepted: isAcceptedWorkerWrite(value),
      applied: !isAcceptedWorkerWrite(value),
      value,
    }))
    .catch((error) => ({
      index,
      ok: false,
      accepted: false,
      applied: false,
      code: error?.code,
      message: error instanceof Error ? error.message : String(error),
    }));
}

function startBurst(fixture, claims) {
  return Promise.all(
    claims.map((claim, index) =>
      settle(
        updateClaimedSlice({
          vaultPath: fixture.vaultPath,
          notePath: fixture.notePath,
          planId: fixture.planId,
          sliceId: `s${index}`,
          workerAgentId: `worker-${index}`,
          token: claim.token,
          idempotencyKey: `start-${index}`,
          action: { action: "start" },
          now: new Date("2026-08-18T12:01:00.000Z"),
        }),
        index,
      ),
    ),
  );
}

function completeBurst(fixture, claims) {
  return Promise.all(
    claims.map((claim, index) =>
      settle(
        updateClaimedSlice({
          vaultPath: fixture.vaultPath,
          notePath: fixture.notePath,
          planId: fixture.planId,
          sliceId: `s${index}`,
          workerAgentId: `worker-${index}`,
          token: claim.token,
          idempotencyKey: `complete-${index}`,
          action: {
            action: "complete",
            evidence: `Verification: slice s${index} done via test ID T-${index}`,
          },
          now: new Date("2026-08-18T12:02:00.000Z"),
        }),
        index,
      ),
    ),
  );
}

async function readStartAcceptedStamp(fixture, claim) {
  return readWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: claim.slice_id,
    workerAgentId: claim.worker_agent_id,
    generation: claim.generation,
    idempotencyKey: startAcceptedReceiptKey(
      fixture.planId,
      claim.slice_id,
      claim.generation,
      claim.claim_id,
    ),
    claimId: claim.claim_id,
  });
}

async function journalState(fixture) {
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 4000);
  const started = events.filter((event) => event.kind === "slice.started").map((event) => event.slice_id);
  const completed = events
    .filter((event) => event.kind === "slice.completed")
    .map((event) => event.slice_id);
  const seqs = events.map((event) => event.seq);
  const dense =
    seqs.length > 0 && seqs.every((seq, index) => index === 0 || seq === seqs[index - 1] + 1);
  return {
    events,
    started,
    completed,
    completesWithoutStarts: completed.filter((id) => !started.includes(id)),
    dense,
    firstSeq: seqs[0],
    lastSeq: seqs.at(-1),
  };
}

async function journalTimeoutDiagnostics({ last, startFail = [], completeFail = [] }, readQueue) {
  // Never serialize exception text, tokens/digests, idempotency keys or action
  // payloads. Only fixture slice numbers, known actions and error codes help.
  const failures = rows => rows.map(row => ({
    index: Number.isSafeInteger(row.index) ? row.index : null,
    code: typeof row.code === "string" && /^(?:THREAD_[A-Z_]+|E[A-Z0-9_]+)$/.test(row.code) ? row.code : "unclassified",
  }));
  let pending;
  let queueReadFailed = false;
  try { pending = await readQueue(); } catch { queueReadFailed = true; }
  return {
    started: last?.started?.length,
    completed: last?.completed?.length,
    completesWithoutStarts: last?.completesWithoutStarts,
    startFail: failures(startFail), completeFail: failures(completeFail),
    queueReadFailed,
    leftover: pending?.length ?? null,
    remainingTickets: pending?.map(item => ({
      slice: /^s\d+$/.test(item.sliceId) ? item.sliceId : "other",
      action: ["start", "complete", "progress", "propose_structure"].includes(item.action?.action) ? item.action.action : "other",
    })) ?? null,
  };
}

async function waitForJournal(fixture, { started = 0, completed = 0, queueEmpty = false, startFail = [], completeFail = [] }, timeoutMs = 20_000, options = {}) {
  const {
    stallMs = timeoutMs, clock = Date.now, readState = journalState,
    readQueue = () => listQueuedWorkerWrites(fixture.vaultPath, fixture.planId),
    kick = () => kickWorkerWriteDrain({ vaultPath: fixture.vaultPath, notePath: fixture.notePath, planId: fixture.planId }),
    pause = () => new Promise(resolve => setTimeout(resolve, 25)),
  } = options;
  const begin = clock();
  let progressedAt = begin;
  let progress = 0;
  let last;
  let leftover;
  while (clock() - begin < timeoutMs && clock() - progressedAt < stallMs) {
    last = await readState(fixture);
    const count = last.started.length + last.completed.length;
    if (count > progress) { progress = count; progressedAt = clock(); }
    leftover = queueEmpty
      ? await readQueue()
      : undefined;
    if (
      last.started.length >= started &&
      last.completed.length >= completed &&
      (!queueEmpty || leftover.length === 0)
    ) {
      return last;
    }
    kick();
    await pause();
  }
  const diagnostic = await journalTimeoutDiagnostics({ last, startFail, completeFail },
    readQueue);
  throw new Error(`timeout waiting for ${started} starts / ${completed} completes; elapsed=${clock() - begin}ms stalled=${clock() - progressedAt}ms; got ${JSON.stringify(diagnostic)}`);
}

async function runWetBurst(t, n) {
  const fixture = await burstFixture(t, n);
  const claims = await assignAndClaimAll(fixture);
  const starts = await startBurst(fixture, claims);
  const startBusy = starts.filter((result) => result.code === "THREAD_BUSY");
  const startFail = starts.filter((result) => !result.ok);
  const completes = await completeBurst(fixture, claims);
  const completeBusy = completes.filter((result) => result.code === "THREAD_BUSY");
  const completeFail = completes.filter((result) => !result.ok);
  // A correctness burst may exceed 20s under filesystem contention. Keep a
  // 20s no-progress failure and a 50s total cap inside the 60s test deadline.
  const journal = await waitForJournal(fixture, { started: n, completed: n, startFail, completeFail }, 50_000, { stallMs: 20_000 });
  return {
    n,
    startOk: starts.filter((result) => result.ok).length,
    startAccepted: starts.filter((result) => result.accepted).length,
    startBusy: startBusy.length,
    startFail,
    completeOk: completes.filter((result) => result.ok).length,
    completeBusy: completeBusy.length,
    completeFail,
    journal,
  };
}

test("queue scans cannot delete a ticket while its writer is publishing it", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-queue-publication-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const originalWrite = fs.promises.writeFile;
  let intercepted = 0;
  fs.promises.writeFile = async (file, data, options) => {
    if (typeof file !== "string" || !file.startsWith(root) || options?.flag !== "wx") {
      return originalWrite(file, data, options);
    }
    // Force a real queue scan between open(O_EXCL) and writing the contents.
    const handle = await fs.promises.open(file, options.flag, options.mode);
    intercepted += 1;
    try {
      assert.deepEqual(await listQueuedWorkerWrites(root, "publication"), []);
      await handle.writeFile(data, options);
    } finally {
      await handle.close();
    }
  };
  syncBuiltinESMExports();
  try {
    const accepted = await enqueueWorkerWrite({
      vaultPath: root, planId: "publication", sliceId: "s0", workerAgentId: "worker",
      token: "test-token", idempotencyKey: "publish-once", action: { action: "start" },
    });
    assert.equal(intercepted, 1, "exercise a scan during the actual enqueue write");
    assert.equal(accepted.alreadyQueued, false);
    const queued = await listQueuedWorkerWrites(root, "publication");
    assert.equal(queued.length, 1, "accepted write must survive a scan during publication");
    assert.equal(queued[0].ticketId, accepted.item.ticketId);
  } finally {
    fs.promises.writeFile = originalWrite;
    syncBuiltinESMExports();
  }
});

test("enqueue shares one validated queue snapshot while independent stuck checks stay fresh", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-queue-snapshot-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const input = { vaultPath: root, planId: "snapshot", sliceId: "s0", workerAgentId: "worker",
    token: "test-token", idempotencyKey: "first", action: { action: "start" } };
  await enqueueWorkerWrite(input);
  const dir = workerWriteQueueDir(root, input.planId);
  const [name] = await readdir(dir);
  const ticketPath = path.join(dir, name);
  const originalRead = fs.promises.readFile;
  let reads = 0;
  fs.promises.readFile = async (file, ...args) => {
    if (file === ticketPath) reads += 1;
    return originalRead(file, ...args);
  };
  syncBuiltinESMExports();
  try {
    await enqueueWorkerWrite({ ...input, sliceId: "s1", idempotencyKey: "second" });
    assert.equal(reads, 1, "capacity and stuck detection should share the validated snapshot");
    await isWorkerWriteDrainStuck(root, input.planId, new Date());
    assert.equal(reads, 2, "a separate check must read the current queue again");
  } finally {
    fs.promises.readFile = originalRead;
    syncBuiltinESMExports();
  }
  assert.equal((await listQueuedWorkerWrites(root, input.planId)).length, 2);
});

test("queue read failures preserve accepted tickets for a later drain", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-queue-read-failure-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const accepted = await enqueueWorkerWrite({
    vaultPath: root, planId: "read-failure", sliceId: "s0", workerAgentId: "worker",
    token: "test-token", idempotencyKey: "read-later", action: { action: "start" },
  });
  const originalRead = fs.promises.readFile;
  fs.promises.readFile = async (file, ...args) => {
    if (typeof file === "string" && file.startsWith(root) && file.endsWith(".json")) {
      throw Object.assign(new Error("simulated read failure"), { code: "EACCES" });
    }
    return originalRead(file, ...args);
  };
  syncBuiltinESMExports();
  try {
    await assert.rejects(listQueuedWorkerWrites(root, "read-failure"), { code: "EACCES" });
  } finally {
    fs.promises.readFile = originalRead;
    syncBuiltinESMExports();
  }
  const queued = await listQueuedWorkerWrites(root, "read-failure");
  assert.equal(queued.length, 1);
  assert.equal(queued[0].ticketId, accepted.item.ticketId);
});

test("concurrent publication of one idempotency key preserves one winning ticket", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-queue-idempotent-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const input = {
    vaultPath: root, planId: "same-key", sliceId: "s0", workerAgentId: "worker",
    token: "test-token", idempotencyKey: "one-key", action: { action: "start" },
  };
  const results = await Promise.all(Array.from({ length: 20 }, () => enqueueWorkerWrite(input)));
  assert.equal(results.filter(result => !result.alreadyQueued).length, 1);
  assert.equal(new Set(results.map(result => result.item.ticketId)).size, 1);
  const queued = await listQueuedWorkerWrites(root, "same-key");
  assert.equal(queued.length, 1);
  assert.equal(queued[0].ticketId, results[0].item.ticketId);
  assert.equal((await readdir(workerWriteQueueDir(root, "same-key"))).filter(name => name.endsWith(".tmp")).length, 0);
});

for (const failure of ["read", "corrupt"]) {
 for (const mode of ["full", "one-shot", "standing"]) {
  test(`${mode} drain preserves accepted work when plan authority is ${failure}`, async (t) => {
    const fixture = await burstFixture(t, 1);
    const [claim] = await assignAndClaimAll(fixture);
    await enqueueWorkerWrite({
      ...fixture, sliceId: "s0", workerAgentId: "worker-0", token: claim.token,
      idempotencyKey: "authority-retry", action: { action: "start" },
      generation: claim.generation, applyNow: new Date("2026-08-18T12:01:00.000Z"),
    });
    const originalNote = await readFile(fixture.notePath, "utf8");
    const originalRead = fs.promises.readFile;
    if (failure === "corrupt") {
      await writeFile(fixture.notePath, "temporarily invalid plan", "utf8");
    } else {
      fs.promises.readFile = async (file, ...args) => {
        if (file === fixture.notePath) {
          throw Object.assign(new Error("temporary plan read failure"), { code: "EIO" });
        }
        return originalRead(file, ...args);
      };
      syncBuiltinESMExports();
    }
    try {
      await assert.rejects(drainWorkerWrites(fixture, {}, {
        oneShotYield: mode === "one-shot", standingDefer: mode === "standing",
      }));
    } finally {
      fs.promises.readFile = originalRead;
      syncBuiltinESMExports();
      await writeFile(fixture.notePath, originalNote, "utf8");
    }
    assert.equal((await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId)).length, 1);
    await drainWorkerWrites(fixture);
    assert.deepEqual((await journalState(fixture)).started, ["s0"]);
    assert.equal((await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId)).length, 0);
  });
 }
}

test("queue timeout diagnostics preserve submission failures and pending actions without secrets", async () => {
  const secret = "secret-claim-token";
  const details = await journalTimeoutDiagnostics({
    last: { started: ["s0", "s1"], completed: ["s0"], completesWithoutStarts: [] },
    startFail: [{ index: 1, code: "THREAD_BUSY", message: secret }],
    completeFail: [{ index: 2, code: secret, message: secret }],
  }, async () => [{ sliceId: "s1", action: { action: "complete", evidence: secret }, token: secret, tokenDigest: secret, idempotencyKey: secret }]);
  assert.equal(details.started, 2);
  assert.equal(details.completed, 1);
  assert.deepEqual(details.startFail, [{ index: 1, code: "THREAD_BUSY" }]);
  assert.deepEqual(details.completeFail, [{ index: 2, code: "unclassified" }]);
  assert.equal(details.leftover, 1);
  assert.deepEqual(details.remainingTickets, [{ slice: "s1", action: "complete" }]);
  assert.equal(JSON.stringify(details).includes(secret), false);
  const unreadable = await journalTimeoutDiagnostics({}, async () => { throw new Error(secret); });
  assert.equal(unreadable.queueReadFailed, true);
  assert.equal(unreadable.leftover, null);
  assert.equal(unreadable.remainingTickets, null);
  assert.equal(JSON.stringify(unreadable).includes(secret), false);
});

test("accepted-into-Q is not applied until drain", async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "exclusive-replan-hold", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const outcome = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(outcome), true);
  assert.equal(outcome.applied, false);
  assert.equal(outcome.queued, true);
  assert.ok(!("slice" in outcome));
  assert.ok(!("ready_after" in outcome));
  assert.ok(!("rev" in outcome));
  const mid = await journalState(fixture);
  assert.equal(mid.started.length, 0, "mid-burst events must not show applied-before-drain");
  const planMid = await rehydratePlan(fixture.notePath);
  assert.equal(planMid.slices[0].status, "pending");
  const queued = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(queued.length, 1);
  assert.equal(queued[0].idempotencyKey, "start-0");
  assert.equal("token" in queued[0], false);
  const rawTickets = await readRawQTickets(fixture.vaultPath, fixture.planId);
  assert.equal(rawTickets.length, 1);
  assertQTicketHasNoRawToken(rawTickets[0], claim.token);
  const stampAtAccept = await readStartAcceptedStamp(fixture, claim);
  assert.equal(stampAtAccept?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(stampAtAccept?.status, "pending");
  assert.equal("token" in (stampAtAccept ?? {}), false);
  assert.equal(JSON.stringify(stampAtAccept).includes(claim.token), false);
  const again = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(again), true);
  const queuedAgain = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(queuedAgain.length, 1, "same idempotency_key while queued must not double-enqueue");
  release();
  await holder;
  const journal = await waitForJournal(fixture, { started: 1, queueEmpty: true });
  assert.equal(journal.started.length, 1);
  const planAfter = await rehydratePlan(fixture.notePath);
  assert.equal(planAfter.slices[0].status, "in_progress");
  const journalText = await readFile(journalPathFor(fixture.notePath, fixture.planId), "utf8");
  assert.equal(journalText.includes(claim.token), false, "raw claim token must stay off the journal");
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0);
});


test("drain apply authenticates against claim-secret store, not a Q token copy", async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "store-auth-hold", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  const raw = await readRawQTickets(fixture.vaultPath, fixture.planId);
  assert.equal(raw.length, 1);
  assertQTicketHasNoRawToken(raw[0], claim.token);
  await deleteClaimSecretsBestEffort(fixture.vaultPath, fixture.planId, [claim.claim_id]);
  release();
  await holder;
  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  }).catch(() => {});
  const journal = await journalState(fixture);
  assert.equal(journal.started.length, 0, "without the claim-secret store, drain must not apply");
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.ok(
    leftover.some((item) => item.idempotencyKey === "start-0"),
    "ticket stays when store auth fails",
  );
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "pending");
  const journalText = await readFile(journalPathFor(fixture.notePath, fixture.planId), "utf8");
  assert.equal(journalText.includes(claim.token), false);
});

test("THREAD_BUSY is Q-full overflow, not N=40", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-q-full-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (let index = 0; index < DEFAULT_QUEUE_MAX; index += 1) {
    await enqueueWorkerWrite({
      vaultPath: root,
      planId: "plan-full",
      sliceId: `s${index}`,
      workerAgentId: `w${index}`,
      token: `tok-${index}`,
      idempotencyKey: `key-${index}`,
      action: { action: "start" },
      now: new Date("2026-08-18T12:01:00.000Z"),
    });
  }
  await assert.rejects(
    enqueueWorkerWrite({
      vaultPath: root,
      planId: "plan-full",
      sliceId: "overflow",
      workerAgentId: "w-overflow",
      token: "tok-overflow",
      idempotencyKey: "key-overflow",
      action: { action: "start" },
      now: new Date("2026-08-18T12:01:00.000Z"),
    }),
    (error) => error?.code === "THREAD_BUSY",
  );
  const again = await enqueueWorkerWrite({
    vaultPath: root,
    planId: "plan-full",
    sliceId: "s0",
    workerAgentId: "w0",
    token: "tok-0",
    idempotencyKey: "key-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(again.alreadyQueued, true);
  const rawFull = await readRawQTickets(root, "plan-full");
  assert.equal(rawFull.length, DEFAULT_QUEUE_MAX);
  for (const ticket of rawFull) {
    assert.equal("token" in ticket, false, "Q-full tickets must omit raw token");
    assert.match(ticket.tokenDigest, /^[0-9a-f]{64}$/);
  }
});

test("THREAD_BUSY is drain-stuck overflow when the live owner is not draining", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-q-stuck-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const holder = withThreadLock(root, "plan-stuck", "live-owner", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  await enqueueWorkerWrite({
    vaultPath: root,
    planId: "plan-stuck",
    sliceId: "s0",
    workerAgentId: "w0",
    token: "tok-0",
    idempotencyKey: "key-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:00:00.000Z"),
  });
  await assert.rejects(
    enqueueWorkerWrite({
      vaultPath: root,
      planId: "plan-stuck",
      sliceId: "s1",
      workerAgentId: "w1",
      token: "tok-1",
      idempotencyKey: "key-1",
      action: { action: "start" },
      now: new Date("2026-08-18T12:00:06.000Z"),
      stuckMs: 50,
    }),
    (error) => error?.code === "THREAD_BUSY",
  );
  release();
  await holder;
});

test("wet N=40 starts+completes dump-and-return without start THREAD_BUSY", { timeout: 60_000 }, async (t) => {
  const result = await runWetBurst(t, 40);
  assert.equal(result.startBusy, 0, `start THREAD_BUSY must not be the N=40 default: ${JSON.stringify(result.startFail)}`);
  assert.equal(result.startOk, 40, JSON.stringify(result.startFail));
  assert.equal(result.completeBusy, 0, JSON.stringify(result.completeFail));
  assert.equal(result.completeOk, 40, JSON.stringify(result.completeFail));
  assert.equal(result.journal.started.length, 40);
  assert.equal(result.journal.completed.length, 40);
  assert.deepEqual(result.journal.completesWithoutStarts, []);
  assert.equal(result.journal.dense, true, `journal seq not dense ${result.journal.firstSeq}..${result.journal.lastSeq}`);
  for (const sliceId of result.journal.completed) {
    const startSeq = result.journal.events.find((event) => event.kind === "slice.started" && event.slice_id === sliceId)?.seq;
    const completeSeq = result.journal.events.find((event) => event.kind === "slice.completed" && event.slice_id === sliceId)?.seq;
    assert.ok(startSeq < completeSeq, `${sliceId} start ${startSeq} must drain before complete ${completeSeq}`);
  }
});

test("wet N=20 starts+completes still hold", { timeout: 45_000 }, async (t) => {
  const result = await runWetBurst(t, 20);
  assert.equal(result.startBusy, 0);
  assert.equal(result.startOk, 20, JSON.stringify(result.startFail));
  assert.equal(result.completeOk, 20, JSON.stringify(result.completeFail));
  assert.deepEqual(result.journal.completesWithoutStarts, []);
  assert.equal(result.journal.dense, true);
});

test("replan during an N=40 start burst stays exclusive and is not a Q item", { timeout: 60_000 }, async (t) => {
  const fixture = await burstFixture(t, 40);
  const claims = await assignAndClaimAll(fixture);
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const replan = withThreadPlanLock(
    {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      operationId: "burst-replan-expand",
    },
    async (plan) => {
      await hold;
      const mid = await journalState(fixture);
      assert.equal(mid.started.length, 0, "replan must not interleave a worker persist mid-hold");
      const queued = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
      assert.ok(queued.length > 0, "worker writes dump to Q while replan holds the lock");
      assert.ok(
        queued.every((item) => item.action?.action === "start"),
        "replan itself is not a Q item",
      );
      const updated = applySliceDelta(plan, {
        add_slices: [{ id: "replan-extra", title: "Replan during burst" }],
      });
      await persistPlan(updated, {
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
      });
      return updated;
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  const startsP = startBurst(fixture, claims);
  const waitDump = Date.now();
  while (Date.now() - waitDump < 2_000) {
    const queued = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
    if (queued.length > 0) break;
    await new Promise((resolve) => setTimeout(resolve, 15));
  }
  releaseHold();
  const [startResults, replanResult] = await Promise.all([
    startsP,
    replan.then(() => ({ ok: true })).catch((error) => ({
      ok: false,
      code: error?.code,
      message: error instanceof Error ? error.message : String(error),
    })),
  ]);
  const startBusy = startResults.filter((result) => result.code === "THREAD_BUSY");
  assert.equal(startBusy.length, 0, JSON.stringify(startResults.filter((result) => !result.ok)));
  assert.equal(startResults.filter((result) => result.ok).length, 40);
  assert.ok(startResults.some((result) => result.accepted), "dump-and-return must accept some starts while replan holds");
  assert.equal(replanResult.ok, true, JSON.stringify(replanResult));
  kickWorkerWriteDrain({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  const journal = await waitForJournal(fixture, { started: 40 });
  const final = await rehydratePlan(fixture.notePath);
  assert.ok(final.slices.some((slice) => slice.id === "replan-extra"), "expand replan must land");
  assert.equal(
    final.slices.filter((slice) => slice.id.startsWith("s") && slice.status === "in_progress").length,
    40,
  );
  assert.deepEqual(journal.completesWithoutStarts, []);
  assert.equal(journal.dense, true);
  const begin = Date.now();
  while (Date.now() - begin < 5000) {
    const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
    if (leftover.length === 0) break;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
});


test("fail-closed drain keeps accepted start when apply throws; complete cannot persist done with no start", async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let persistCalls = 0;
  const throwingPersist = async (plan, opts) => {
    persistCalls += 1;
    const slice = plan.slices.find((item) => item.id === "s0");
    // Fail only the start persist. A dropped start ticket would let
    // complete persist done with no start; that must still fail.
    if (slice?.status === "in_progress") {
      throw new Error("injected start apply failure");
    }
    return persistPlan(plan, opts);
  };
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  let entered;
  const acquired = new Promise((resolve) => { entered = resolve; });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "apply-throw-hold", async () => {
    entered();
    await held;
  });
  await acquired;
  const start = await updateClaimedSlice(
    {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: "s0",
      workerAgentId: "worker-0",
      token: claim.token,
      idempotencyKey: "start-0",
      action: { action: "start" },
      now: new Date("2026-08-18T12:01:00.000Z"),
    },
    { persistPlan: throwingPersist },
  );
  assert.equal(isAcceptedWorkerWrite(start), true);
  const queuedBefore = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(queuedBefore.length, 1);
  assert.equal(queuedBefore[0].idempotencyKey, "start-0");
  assert.equal(queuedBefore[0].action.action, "start");
  const stampAtAccept = await readStartAcceptedStamp(fixture, claim);
  assert.equal(stampAtAccept?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(stampAtAccept?.status, "pending");
  assert.notEqual(stampAtAccept?.response.slice.status, "in_progress");
  assert.notEqual(stampAtAccept?.response.slice.status, "done");
  assert.deepEqual(stampAtAccept?.response.ready_before, stampAtAccept?.response.ready_after);
  release();
  await holder;
  const begin = Date.now();
  while (persistCalls === 0 && Date.now() - begin < 5_000) {
    kickWorkerWriteDrain(
      {
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
        planId: fixture.planId,
      },
      { persistPlan: throwingPersist },
    );
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.ok(persistCalls >= 1, `drain must attempt apply; persistCalls=${persistCalls}`);
  const queuedAfterThrow = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.ok(
    queuedAfterThrow.some((item) => item.idempotencyKey === "start-0"),
    `accepted start must stay after apply throw: ${JSON.stringify(queuedAfterThrow)}`,
  );
  const stampAfterThrow = await readStartAcceptedStamp(fixture, claim);
  assert.equal(stampAfterThrow?.kind, START_ACCEPTED_RECEIPT_KIND, "apply throw must keep the start-accepted receipt");
  assert.equal(stampAfterThrow?.status, "pending");
  assert.notEqual(stampAfterThrow?.response.slice.status, "in_progress");
  const complete = await updateClaimedSlice(
    {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: "s0",
      workerAgentId: "worker-0",
      token: claim.token,
      idempotencyKey: "complete-0",
      action: {
        action: "complete",
        evidence: "Verification: slice s0 done via test ID T-0",
      },
      now: new Date("2026-08-18T12:02:00.000Z"),
    },
    { persistPlan: throwingPersist },
  );
  assert.equal(isAcceptedWorkerWrite(complete), true, "complete-while-start-queued must enqueue, not apply");
  const afterComplete = persistCalls;
  const waitComplete = Date.now();
  while (Date.now() - waitComplete < 1_000) {
    kickWorkerWriteDrain(
      {
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
        planId: fixture.planId,
      },
      { persistPlan: throwingPersist },
    );
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.ok(
    leftover.some((item) => item.idempotencyKey === "start-0"),
    `start ticket must still be present after complete: ${JSON.stringify(leftover)}`,
  );
  assert.equal(pickNextQueuedWorkerWrite(leftover)?.idempotencyKey, "start-0", "drain order still start-before-complete");
  const journal = await journalState(fixture);
  assert.equal(journal.started.length, 0, "failed start apply must not journal slice.started");
  assert.equal(journal.completed.length, 0, "complete must not persist done with no start");
  assert.deepEqual(journal.completesWithoutStarts, []);
  const plan = await rehydratePlan(fixture.notePath);
  assert.notEqual(plan.slices[0].status, "done");
  assert.notEqual(plan.slices[0].status, "in_progress");
  assert.ok(persistCalls >= afterComplete, `persistCalls=${persistCalls}`);
  const stampFinal = await readStartAcceptedStamp(fixture, claim);
  assert.equal(stampFinal?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(stampFinal?.status, "pending");

  // Stamp alone still blocks done if the Q ticket is gone (the old drop).
  const { removeQueuedWorkerWrite } = await import("../dist/thread-write-queue.js");
  await removeQueuedWorkerWrite(fixture.vaultPath, fixture.planId, "start-0");
  await removeQueuedWorkerWrite(fixture.vaultPath, fixture.planId, "complete-0");
  await assert.rejects(
    updateClaimedSlice(
      {
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
        planId: fixture.planId,
        sliceId: "s0",
        workerAgentId: "worker-0",
        token: claim.token,
        idempotencyKey: "complete-stamp-only",
        action: {
          action: "complete",
          evidence: "Verification: slice s0 done via test ID T-stamp",
        },
        now: new Date("2026-08-18T12:03:00.000Z"),
      },
      { persistPlan: throwingPersist },
    ),
    /start must apply before complete/,
  );
  const planAfterStampOnly = await rehydratePlan(fixture.notePath);
  assert.notEqual(planAfterStampOnly.slices[0].status, "done");
  const journalAfter = await journalState(fixture);
  assert.equal(journalAfter.completed.length, 0);
  assert.deepEqual(journalAfter.completesWithoutStarts, []);
});

test("claimed pending complete without start/stamp/ticket cannot persist done", async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  const queued = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(queued.length, 0, "GO case has no start ticket");
  const stamp = await readStartAcceptedStamp(fixture, claim);
  assert.equal(stamp, undefined, "GO case has no start-accepted stamp");
  const planBefore = await rehydratePlan(fixture.notePath);
  assert.equal(planBefore.slices[0].status, "pending");
  assert.ok(planBefore.slices[0].claim);

  await assert.rejects(
    updateClaimedSlice({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: "s0",
      workerAgentId: "worker-0",
      token: claim.token,
      idempotencyKey: "complete-pending-no-start",
      action: {
        action: "complete",
        evidence: "Verification: slice s0 done via test ID T-pending-no-start",
      },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /complete cannot persist done without start/,
  );

  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "pending");
  assert.notEqual(plan.slices[0].status, "done");
  const journal = await journalState(fixture);
  assert.equal(journal.started.length, 0);
  assert.equal(journal.completed.length, 0);
  assert.deepEqual(journal.completesWithoutStarts, []);
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0);
});

test("accept start, kill that process, later drain (not that kick) journals slice.started", async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "kill-accept-hold", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const workerModule = new URL("../dist/thread-worker.js", import.meta.url).href;
  const child = spawn(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      `
      import { updateClaimedSlice } from ${JSON.stringify(workerModule)};
      const input = JSON.parse(process.env.MINNI_DRAIN_INPUT);
      if (typeof input.now === "string") input.now = new Date(input.now);
      const result = await updateClaimedSlice(input);
      process.stdout.write(JSON.stringify(result) + "\\n");
      await new Promise(() => {});
      `,
    ],
    {
      env: {
        ...process.env,
        MINNI_DRAIN_INPUT: JSON.stringify({
          vaultPath: fixture.vaultPath,
          notePath: fixture.notePath,
          planId: fixture.planId,
          sliceId: "s0",
          workerAgentId: "worker-0",
          token: claim.token,
          idempotencyKey: "start-0",
          action: { action: "start" },
          now: "2026-08-18T12:01:00.000Z",
        }),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  t.after(() => {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
    }
  });
  let childOut = "";
  child.stdout.on("data", (chunk) => {
    childOut += chunk.toString();
  });
  const waitAccept = Date.now();
  let queued = [];
  while (Date.now() - waitAccept < 5_000) {
    queued = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
    if (queued.some((item) => item.idempotencyKey === "start-0") && childOut.includes('"accepted":true')) {
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.ok(
    queued.some((item) => item.idempotencyKey === "start-0"),
    `accepting process must enqueue start: ${JSON.stringify(queued)} out=${childOut}`,
  );
  const rawAtAccept = await readRawQTickets(fixture.vaultPath, fixture.planId);
  const startTicket = rawAtAccept.find((item) => item.idempotencyKey === "start-0");
  assert.ok(startTicket, "start Q file must exist");
  assertQTicketHasNoRawToken(startTicket, claim.token);
  const pendingPlans = await listPendingWorkerWritePlanIds(fixture.vaultPath);
  assert.deepEqual(pendingPlans, [fixture.planId]);
  const stampAtAccept = await readStartAcceptedStamp(fixture, claim);
  assert.equal(stampAtAccept?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(stampAtAccept?.status, "pending");
  assert.notEqual(stampAtAccept?.response.slice.status, "in_progress");
  assert.notEqual(stampAtAccept?.response.slice.status, "done");

  child.kill("SIGKILL");
  await new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) {
      resolve();
      return;
    }
    child.once("exit", resolve);
  });

  // Enqueue complete without kicking. A later updateClaimedSlice complete
  // would kick in THIS process and hide the dead accepting kick.
  await enqueueWorkerWrite({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "complete-0",
    action: {
      action: "complete",
      evidence: "Verification: slice s0 done via test ID T-kill",
    },
    now: new Date("2026-08-18T12:02:00.000Z"),
    applyNow: new Date("2026-08-18T12:02:00.000Z"),
  });
  const afterKill = await journalState(fixture);
  assert.equal(afterKill.started.length, 0, "killed kick must not journal slice.started");
  assert.equal(afterKill.completed.length, 0, "complete must not persist done first");
  const planAfterKill = await rehydratePlan(fixture.notePath);
  assert.notEqual(planAfterKill.slices[0].status, "done");
  assert.notEqual(planAfterKill.slices[0].status, "in_progress");
  const queuedAfterKill = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(pickNextQueuedWorkerWrite(queuedAfterKill)?.idempotencyKey, "start-0");

  release();
  await holder;

  const waitDeadKick = Date.now();
  let mid;
  while (Date.now() - waitDeadKick < 250) {
    mid = await journalState(fixture);
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.equal(mid.started.length, 0, "accepting-process kick must stay dead after lock release");
  assert.equal(mid.completed.length, 0);

  const later = await drainPendingWorkerWritesForVault(fixture.vaultPath);
  assert.ok(later.planIds.includes(fixture.planId), JSON.stringify(later));
  const journal = await journalState(fixture);
  assert.deepEqual(journal.started, ["s0"]);
  assert.deepEqual(journal.completesWithoutStarts, []);
  assert.ok(journal.started.length >= 1, "later drain (not that kick) must journal slice.started");
  if (journal.completed.length > 0) {
    const startIdx = journal.events.findIndex((event) => event.kind === "slice.started");
    const completeIdx = journal.events.findIndex((event) => event.kind === "slice.completed");
    assert.ok(startIdx >= 0 && completeIdx > startIdx, "start must apply before complete");
  }
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0);
  const planAfter = await rehydratePlan(fixture.notePath);
  assert.ok(["in_progress", "done"].includes(planAfter.slices[0].status));
});

test("DEFAULT_WAIT_MS stays 5000 — silent bump is fake close", async () => {
  const source = await readFile(new URL("../src/thread-lock.ts", import.meta.url), "utf8");
  assert.match(source, /const DEFAULT_WAIT_MS = 5_000;/);
  assert.doesNotMatch(source, /FIFO waiter queue/);
});

test("drain pick applies start before that slice's complete", () => {
  const complete = {
    ticketId: "c",
    enqueuedAt: "2026-08-18T12:00:00.000Z",
    planId: "p",
    sliceId: "s0",
    workerAgentId: "w",
    tokenDigest: "a".repeat(64),
    idempotencyKey: "complete-0",
    action: { action: "complete" },
  };
  const start = {
    ticketId: "s",
    enqueuedAt: "2026-08-18T12:00:01.000Z",
    planId: "p",
    sliceId: "s0",
    workerAgentId: "w",
    tokenDigest: "a".repeat(64),
    idempotencyKey: "start-0",
    action: { action: "start" },
  };
  const other = {
    ticketId: "o",
    enqueuedAt: "2026-08-18T12:00:02.000Z",
    planId: "p",
    sliceId: "s1",
    workerAgentId: "w",
    tokenDigest: "a".repeat(64),
    idempotencyKey: "start-1",
    action: { action: "start" },
  };
  const picked = pickNextQueuedWorkerWrite([complete, start, other]);
  assert.equal(picked?.idempotencyKey, "start-0");
});

test("MCP queued payload is not the applied slice/ready/rev shape", async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "mcp-hold", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const outcome = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "mcp-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  const payload = workerUpdateMcpPayload(outcome);
  assert.equal(payload.status, "accepted");
  assert.equal(payload.applied, false);
  assert.equal(payload.queued, true);
  assert.equal(payload.plan_id, fixture.planId);
  assert.equal(payload.slice_id, "s0");
  assert.equal(payload.action, "start");
  assert.equal(payload.idempotency_key, "mcp-start");
  assert.equal("slice" in payload, false);
  assert.equal("ready_before" in payload, false);
  assert.equal("ready_after" in payload, false);
  assert.equal("rev" in payload, false);
  release();
  await holder;
  await waitForJournal(fixture, { started: 1 });
});

test("leftover accepted start does not apply live after orch split supersedes s0", { timeout: 30_000 }, async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  const acceptGeneration = claim.generation;
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const split = withThreadPlanLock(
    {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      operationId: "orch-split-s0",
    },
    async (plan) => {
      await hold;
      const queued = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
      assert.ok(
        queued.some((item) => item.idempotencyKey === "start-0" && item.action?.action === "start"),
        `start must be queued before split: ${JSON.stringify(queued)}`,
      );
      assert.equal(queued.find((item) => item.idempotencyKey === "start-0")?.generation, acceptGeneration);
      const stamp = await readStartAcceptedStamp(fixture, claim);
      assert.equal(stamp?.kind, START_ACCEPTED_RECEIPT_KIND);
      assert.equal(stamp?.status, "pending");
      assert.notEqual(stamp?.response.slice.status, "in_progress");
      const updated = applySliceDelta(plan, {
        drop_slice_ids: ["s0"],
        add_slices: [
          { id: "child-a", title: "Child A" },
          { id: "child-b", title: "Child B" },
        ],
      });
      assert.equal(updated.slices.find((slice) => slice.id === "s0")?.status, "superseded");
      assert.equal(
        updated.slices.some((slice) => slice.id === "s0" && slice.status !== "superseded"),
        false,
      );
      assert.deepEqual(
        updated.slices.filter((slice) => slice.status !== "superseded").map((slice) => slice.id).sort(),
        ["child-a", "child-b"],
      );
      await persistPlan(updated, {
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
      });
      await pruneSliceReceiptsAfterPlanMutation(
        fixture.vaultPath,
        fixture.planId,
        plan,
        updated,
      );
      await deleteClaimSecretsBestEffort(
        fixture.vaultPath,
        fixture.planId,
        revokedClaimIds(plan, updated),
      );
      return updated;
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  assert.equal(start.applied, false);
  releaseHold();
  await split;
  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0, `stale leftover start must not stay as live Q: ${JSON.stringify(leftover)}`);
  const plan = await rehydratePlan(fixture.notePath);
  const parent = plan.slices.find((slice) => slice.id === "s0");
  assert.ok(parent, "split never deletes the parent");
  assert.equal(parent.status, "superseded");
  assert.notEqual(parent.status, "in_progress");
  assert.notEqual(parent.status, "done");
  assert.equal(parent.claim, undefined);
  assert.ok(parent.generation === undefined || parent.generation > acceptGeneration);
  assert.equal(plan.slices.filter((slice) => slice.id === "s0").length, 1);
  assert.deepEqual(
    plan.slices.filter((slice) => slice.status !== "superseded").map((slice) => slice.id).sort(),
    ["child-a", "child-b"],
  );
  const journal = await journalState(fixture);
  assert.equal(
    journal.started.includes("s0"),
    false,
    `journal must not show leftover start as live work on the dead parent: ${JSON.stringify(journal.started)}`,
  );
  assert.equal(journal.completed.includes("s0"), false);
  assert.deepEqual(journal.completesWithoutStarts, []);
  await assert.rejects(
    updateClaimedSlice({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: "s0",
      workerAgentId: "worker-0",
      token: claim.token,
      idempotencyKey: "complete-s0-after-split",
      action: {
        action: "complete",
        evidence: "Verification: slice s0 done via test ID T-split",
      },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /claim scope mismatch|not worker-updatable|cannot persist done|claim token mismatch|claim expired/,
  );
  const afterComplete = await rehydratePlan(fixture.notePath);
  assert.equal(afterComplete.slices.find((slice) => slice.id === "s0")?.status, "superseded");
  assert.notEqual(afterComplete.slices.find((slice) => slice.id === "s0")?.status, "done");
  const journalAfter = await journalState(fixture);
  assert.equal(journalAfter.completed.includes("s0"), false);
});

test("generation-N leftover start does not authorize or block N+1", { timeout: 30_000 }, async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claimN] = await assignAndClaimAll(fixture);
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const advance = withThreadPlanLock(
    {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      operationId: "reassign-s0-n1",
    },
    async (plan) => {
      await hold;
      const queued = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
      assert.equal(queued[0]?.idempotencyKey, "start-0");
      assert.equal(queued[0]?.generation, claimN.generation);
      const stampN = await readStartAcceptedStamp(fixture, claimN);
      assert.equal(stampN?.kind, START_ACCEPTED_RECEIPT_KIND);
      const slice = plan.slices[0];
      const nextGeneration = (slice.generation ?? 0) + 1;
      const updated = {
        ...plan,
        slices: [
          {
            ...slice,
            assigned_to: "worker-1",
            generation: nextGeneration,
            claim: undefined,
          },
        ],
      };
      await persistPlan(updated, {
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
      });
      await pruneSliceReceiptsAfterPlanMutation(
        fixture.vaultPath,
        fixture.planId,
        plan,
        updated,
      );
      await deleteClaimSecretsBestEffort(
        fixture.vaultPath,
        fixture.planId,
        revokedClaimIds(plan, updated),
      );
      return updated;
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  const accepted = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claimN.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(accepted), true);
  releaseHold();
  await advance;
  const claimN1 = await claimSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-1",
    idempotencyKey: "claim-n1",
    now: THREAD_START,
  });
  assert.ok(claimN1.generation > claimN.generation);
  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0, `N leftover start must not remain live at N+1: ${JSON.stringify(leftover)}`);
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "pending");
  assert.notEqual(plan.slices[0].status, "in_progress");
  const journal = await journalState(fixture);
  assert.equal(journal.started.length, 0, "old-generation start must not journal as live work");
  await assert.rejects(
    updateClaimedSlice({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: "s0",
      workerAgentId: "worker-1",
      token: claimN1.token,
      idempotencyKey: "complete-n1-no-start",
      action: {
        action: "complete",
        evidence: "Verification: slice s0 done via test ID T-n1",
      },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /complete cannot persist done without start/,
  );
  const after = await rehydratePlan(fixture.notePath);
  assert.equal(after.slices[0].status, "pending");
  const startN1 = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-1",
    token: claimN1.token,
    idempotencyKey: "start-n1",
    action: { action: "start" },
    now: new Date("2026-08-18T12:03:00.000Z"),
  });
  if (isAcceptedWorkerWrite(startN1)) {
    await drainWorkerWrites({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
    });
  }
  const live = await rehydratePlan(fixture.notePath);
  assert.equal(live.slices[0].status, "in_progress");
  const journalLive = await journalState(fixture);
  assert.deepEqual(journalLive.started, ["s0"]);
});

test("kick one-shot yield: lock free before orch reserve, process stays up", { timeout: 30_000 }, async (t) => {
  // Not lock-until-reserved: release persist lock while reservation is NOT live,
  // so kick is free to apply before orch announces. That is the IRL TOCTOU.
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  const acceptGeneration = claim.generation;
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "accept-hold", async () => {
    await hold;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  assert.equal(start.applied, false);
  assert.equal(
    await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId),
    false,
    "reservation must not be live at accept return",
  );
  const queuedAtAccept = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.ok(
    queuedAtAccept.some((item) => item.idempotencyKey === "start-0" && item.action?.action === "start"),
    `start must be queued while hold is live: ${JSON.stringify(queuedAtAccept)}`,
  );
  const stamp = await readStartAcceptedStamp(fixture, claim);
  assert.equal(stamp?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(stamp?.status, "pending");
  assert.notEqual(stamp?.response.slice.status, "in_progress");
  const journalAtAccept = await journalState(fixture);
  assert.equal(journalAtAccept.started.includes("s0"), false, "stamp is not slice.started");

  // Persist lock is NOT held across accept→replan. Kick is free; reservation still absent.
  releaseHold();
  await holder;
  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), false);
  await new Promise((resolve) => setTimeout(resolve, 50));
  const journalAfterFreeLock = await journalState(fixture);
  assert.equal(
    journalAfterFreeLock.started.includes("s0"),
    false,
    `kick must not apply start after accept while orch has not reserved: ${JSON.stringify(journalAfterFreeLock.started)}`,
  );

  let reservedBeforeLock = false;
  await withExclusiveReplanReservation(
    fixture.vaultPath,
    fixture.planId,
    "orch-split-s0",
    async () => {
      reservedBeforeLock = await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId);
      return withThreadPlanLock(
        {
          vaultPath: fixture.vaultPath,
          notePath: fixture.notePath,
          planId: fixture.planId,
          operationId: "orch-split-s0",
        },
        async (plan) => {
          const mid = await journalState(fixture);
          assert.equal(
            mid.started.includes("s0"),
            false,
            `kick must not land slice.started before exclusive split: ${JSON.stringify(mid.started)}`,
          );
          const updated = applySliceDelta(plan, {
            drop_slice_ids: ["s0"],
            add_slices: [
              { id: "child-a", title: "Child A" },
              { id: "child-b", title: "Child B" },
            ],
          });
          assert.equal(updated.slices.find((slice) => slice.id === "s0")?.status, "superseded");
          assert.deepEqual(
            updated.slices.filter((slice) => slice.status !== "superseded").map((slice) => slice.id).sort(),
            ["child-a", "child-b"],
          );
          await persistPlan(updated, {
            vaultPath: fixture.vaultPath,
            notePath: fixture.notePath,
          });
          await pruneSliceReceiptsAfterPlanMutation(
            fixture.vaultPath,
            fixture.planId,
            plan,
            updated,
          );
          await deleteClaimSecretsBestEffort(
            fixture.vaultPath,
            fixture.planId,
            revokedClaimIds(plan, updated),
          );
          return updated;
        },
      );
    },
  );
  assert.equal(reservedBeforeLock, true, "reservation must be live before exclusive replan takes the persist lock");

  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0, `leftover start must drop after supersede: ${JSON.stringify(leftover)}`);
  const plan = await rehydratePlan(fixture.notePath);
  const parent = plan.slices.find((slice) => slice.id === "s0");
  assert.ok(parent, "split never deletes the parent");
  assert.equal(parent.status, "superseded");
  assert.notEqual(parent.status, "in_progress");
  assert.notEqual(parent.status, "done");
  assert.equal(parent.claim, undefined);
  assert.ok(parent.generation === undefined || parent.generation > acceptGeneration);
  assert.deepEqual(
    plan.slices.filter((slice) => slice.status !== "superseded").map((slice) => slice.id).sort(),
    ["child-a", "child-b"],
  );
  const journal = await journalState(fixture);
  assert.equal(
    journal.started.includes("s0"),
    false,
    `process-stays-up GO: journal must not have slice.started on superseded s0: ${JSON.stringify(journal.started)}`,
  );
  assert.equal(journal.completed.includes("s0"), false);
  assert.deepEqual(journal.completesWithoutStarts, []);
  await assert.rejects(
    updateClaimedSlice({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: "s0",
      workerAgentId: "worker-0",
      token: claim.token,
      idempotencyKey: "complete-s0-after-yield-split",
      action: {
        action: "complete",
        evidence: "Verification: slice s0 done via test ID T-yield-split",
      },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /claim scope mismatch|not worker-updatable|cannot persist done|claim token mismatch|claim expired/,
  );
  const afterComplete = await rehydratePlan(fixture.notePath);
  assert.equal(afterComplete.slices.find((slice) => slice.id === "s0")?.status, "superseded");
  assert.notEqual(afterComplete.slices.find((slice) => slice.id === "s0")?.status, "done");
  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), false);
});

test("standing tick yields live start before orch reserve (process stays up)", { timeout: 30_000 }, async (t) => {
  // Standing drain is a different process from the accepting MCP. #29
  // in-process one-shot yield does not cover it. Arm standing tick (not
  // kick / drainWorkerWrites) while the acceptor stays up.
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  const acceptGeneration = claim.generation;
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "standing-tick-hold", async () => {
    await hold;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  assert.equal(start.applied, false);
  assert.equal(
    await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId),
    false,
    "reservation must not be live at accept return",
  );
  const queuedAtAccept = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  const startTicket = queuedAtAccept.find(
    (item) => item.idempotencyKey === "start-0" && item.action?.action === "start",
  );
  assert.ok(startTicket, `start must be queued while hold is live: ${JSON.stringify(queuedAtAccept)}`);
  const rawAtAccept = await readRawQTickets(fixture.vaultPath, fixture.planId);
  const rawStart = rawAtAccept.find((item) => item.idempotencyKey === "start-0");
  assert.ok(rawStart, "start Q file must exist");
  assertQTicketHasNoRawToken(rawStart, claim.token);
  assert.equal(rawStart.acceptorPid, process.pid);
  assert.equal("token" in rawStart, false);

  const stamp = await readStartAcceptedStamp(fixture, claim);
  assert.equal(stamp?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(stamp?.status, "pending");
  assert.notEqual(stamp?.response.slice.status, "in_progress");
  const journalAtAccept = await journalState(fixture);
  assert.equal(journalAtAccept.started.includes("s0"), false, "stamp is not slice.started");

  releaseHold();
  await holder;
  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), false);
  await new Promise((resolve) => setTimeout(resolve, 50));
  const journalAfterFreeLock = await journalState(fixture);
  assert.equal(
    journalAfterFreeLock.started.includes("s0"),
    false,
    `accept-path kick one-shot must not journal slice.started: ${JSON.stringify(journalAfterFreeLock.started)}`,
  );

  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), false);
  const standingBegan = Date.now();
  const standing = await drainPendingWorkerWritesForVault(fixture.vaultPath);
  const standingMs = Date.now() - standingBegan;
  assert.ok(
    standingMs < 2_000,
    `standing tick must yield this tick without sitting the 60s drain loop: ${standingMs}ms ${JSON.stringify(standing)}`,
  );
  const journalAfterStanding = await journalState(fixture);
  assert.equal(
    journalAfterStanding.started.includes("s0"),
    false,
    `standing tick must not apply start while acceptor is live in the accept→reserve window: ${JSON.stringify(journalAfterStanding.started)}`,
  );
  const queuedAfterStanding = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.ok(
    queuedAfterStanding.some((item) => item.idempotencyKey === "start-0"),
    `live start ticket must stay for later drain: ${JSON.stringify(queuedAfterStanding)}`,
  );
  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), false);

  let reservedBeforeLock = false;
  await withExclusiveReplanReservation(
    fixture.vaultPath,
    fixture.planId,
    "orch-split-standing-tick",
    async () => {
      reservedBeforeLock = await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId);
      return withThreadPlanLock(
        {
          vaultPath: fixture.vaultPath,
          notePath: fixture.notePath,
          planId: fixture.planId,
          operationId: "orch-split-standing-tick",
        },
        async (plan) => {
          const mid = await journalState(fixture);
          assert.equal(
            mid.started.includes("s0"),
            false,
            `standing tick must not land slice.started before exclusive split: ${JSON.stringify(mid.started)}`,
          );
          const updated = applySliceDelta(plan, {
            drop_slice_ids: ["s0"],
            add_slices: [
              { id: "child-a", title: "Child A" },
              { id: "child-b", title: "Child B" },
            ],
          });
          assert.equal(updated.slices.find((slice) => slice.id === "s0")?.status, "superseded");
          assert.deepEqual(
            updated.slices.filter((slice) => slice.status !== "superseded").map((slice) => slice.id).sort(),
            ["child-a", "child-b"],
          );
          await persistPlan(updated, {
            vaultPath: fixture.vaultPath,
            notePath: fixture.notePath,
          });
          await pruneSliceReceiptsAfterPlanMutation(
            fixture.vaultPath,
            fixture.planId,
            plan,
            updated,
          );
          await deleteClaimSecretsBestEffort(
            fixture.vaultPath,
            fixture.planId,
            revokedClaimIds(plan, updated),
          );
          return updated;
        },
      );
    },
  );
  assert.equal(reservedBeforeLock, true, "reservation must be live before exclusive replan takes the persist lock");

  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0, `leftover start must drop after supersede: ${JSON.stringify(leftover)}`);
  const plan = await rehydratePlan(fixture.notePath);
  const parent = plan.slices.find((slice) => slice.id === "s0");
  assert.ok(parent, "split never deletes the parent");
  assert.equal(parent.status, "superseded");
  assert.notEqual(parent.status, "in_progress");
  assert.notEqual(parent.status, "done");
  assert.equal(parent.claim, undefined);
  assert.ok(parent.generation === undefined || parent.generation > acceptGeneration);
  assert.deepEqual(
    plan.slices.filter((slice) => slice.status !== "superseded").map((slice) => slice.id).sort(),
    ["child-a", "child-b"],
  );
  const journal = await journalState(fixture);
  assert.equal(
    journal.started.includes("s0"),
    false,
    `standing-tick GO: journal must not have slice.started on superseded s0: ${JSON.stringify(journal.started)}`,
  );
  assert.equal(journal.completed.includes("s0"), false);
  assert.deepEqual(journal.completesWithoutStarts, []);
  await assert.rejects(
    updateClaimedSlice({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: "s0",
      workerAgentId: "worker-0",
      token: claim.token,
      idempotencyKey: "complete-s0-after-standing-split",
      action: {
        action: "complete",
        evidence: "Verification: slice s0 done via test ID T-standing-split",
      },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /claim scope mismatch|not worker-updatable|cannot persist done|claim token mismatch|claim expired/,
  );
  const afterComplete = await rehydratePlan(fixture.notePath);
  assert.equal(afterComplete.slices.find((slice) => slice.id === "s0")?.status, "superseded");
  assert.notEqual(afterComplete.slices.find((slice) => slice.id === "s0")?.status, "done");
  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), false);
});

test("complete-behind-start follow-up must not apply start before orch reserve", { timeout: 30_000 }, async (t) => {
  // Accept start arms a one-shot kick. Complete-behind-start while that
  // run is in flight must not reassert a full drain that journals
  // slice.started in the accept→reserve window. Process stays up.
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  const acceptGeneration = claim.generation;
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "follow-up-hold", async () => {
    await hold;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);

  const complete = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "complete-0",
    action: {
      action: "complete",
      evidence: "Verification: slice s0 done via test ID T-follow-up-yield",
    },
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(complete), true, "complete-behind-start must enqueue");
  assert.equal(
    await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId),
    false,
    "reservation must not be live at complete accept",
  );

  releaseHold();
  await holder;
  await new Promise((resolve) => setTimeout(resolve, 50));
  const journalAfterFollowUp = await journalState(fixture);
  assert.equal(
    journalAfterFollowUp.started.includes("s0"),
    false,
    `follow-up full drain must not apply start before orch reserve: ${JSON.stringify(journalAfterFollowUp.started)}`,
  );
  const queuedAfterFollowUp = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.ok(
    queuedAfterFollowUp.some((item) => item.idempotencyKey === "start-0"),
    `start ticket must stay for later drain: ${JSON.stringify(queuedAfterFollowUp)}`,
  );

  let reservedBeforeLock = false;
  await withExclusiveReplanReservation(
    fixture.vaultPath,
    fixture.planId,
    "orch-split-follow-up",
    async () => {
      reservedBeforeLock = await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId);
      return withThreadPlanLock(
        {
          vaultPath: fixture.vaultPath,
          notePath: fixture.notePath,
          planId: fixture.planId,
          operationId: "orch-split-follow-up",
        },
        async (plan) => {
          const mid = await journalState(fixture);
          assert.equal(
            mid.started.includes("s0"),
            false,
            `kick must not land slice.started before exclusive split: ${JSON.stringify(mid.started)}`,
          );
          const updated = applySliceDelta(plan, {
            drop_slice_ids: ["s0"],
            add_slices: [
              { id: "child-a", title: "Child A" },
              { id: "child-b", title: "Child B" },
            ],
          });
          await persistPlan(updated, {
            vaultPath: fixture.vaultPath,
            notePath: fixture.notePath,
          });
          await pruneSliceReceiptsAfterPlanMutation(
            fixture.vaultPath,
            fixture.planId,
            plan,
            updated,
          );
          await deleteClaimSecretsBestEffort(
            fixture.vaultPath,
            fixture.planId,
            revokedClaimIds(plan, updated),
          );
          return updated;
        },
      );
    },
  );
  assert.equal(reservedBeforeLock, true);

  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0, `leftover Q must drop after supersede: ${JSON.stringify(leftover)}`);
  const plan = await rehydratePlan(fixture.notePath);
  const parent = plan.slices.find((slice) => slice.id === "s0");
  assert.ok(parent, "split never deletes the parent");
  assert.equal(parent.status, "superseded");
  assert.notEqual(parent.status, "in_progress");
  assert.notEqual(parent.status, "done");
  assert.ok(parent.generation === undefined || parent.generation > acceptGeneration);
  const journal = await journalState(fixture);
  assert.equal(
    journal.started.includes("s0"),
    false,
    `process-stays-up GO: follow-up must not journal slice.started on superseded s0: ${JSON.stringify(journal.started)}`,
  );
  assert.equal(journal.completed.includes("s0"), false);
  assert.deepEqual(journal.completesWithoutStarts, []);
  await assert.rejects(
    updateClaimedSlice({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      sliceId: "s0",
      workerAgentId: "worker-0",
      token: claim.token,
      idempotencyKey: "complete-s0-after-follow-up-split",
      action: {
        action: "complete",
        evidence: "Verification: slice s0 done via test ID T-follow-up-split",
      },
      now: new Date("2026-08-18T12:03:00.000Z"),
    }),
    /claim scope mismatch|not worker-updatable|cannot persist done|claim token mismatch|claim expired/,
  );
  const afterComplete = await rehydratePlan(fixture.notePath);
  assert.equal(afterComplete.slices.find((slice) => slice.id === "s0")?.status, "superseded");
  assert.notEqual(afterComplete.slices.find((slice) => slice.id === "s0")?.status, "done");
});

test("complete-behind-start tickets stay for later drain after one-shot yield", { timeout: 20_000 }, async (t) => {
  // Accept start + complete-behind-start while one-shot is in flight.
  // Follow-up must not apply in the yield window. Tickets stay; later
  // drain still applies start first when no exclusive replan is in flight.
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "coalesce-hold", async () => {
    await hold;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);

  const complete = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "complete-0",
    action: {
      action: "complete",
      evidence: "Verification: slice s0 done via test ID T-coalesce",
    },
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(complete), true, "complete-behind-start must enqueue");

  releaseHold();
  await holder;
  await new Promise((resolve) => setTimeout(resolve, 50));
  const journalAfterYield = await journalState(fixture);
  assert.equal(
    journalAfterYield.started.includes("s0"),
    false,
    `one-shot / complete-behind-start must not apply start in the yield window: ${JSON.stringify(journalAfterYield.started)}`,
  );
  const queuedAfterYield = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.ok(
    queuedAfterYield.some((item) => item.idempotencyKey === "start-0"),
    `start ticket must stay: ${JSON.stringify(queuedAfterYield)}`,
  );
  assert.ok(
    queuedAfterYield.some((item) => item.idempotencyKey === "complete-0"),
    `complete ticket must stay: ${JSON.stringify(queuedAfterYield)}`,
  );

  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  const leftover = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
  assert.equal(leftover.length, 0, `later drain must apply start then complete: ${JSON.stringify(leftover)}`);
  const journal = await journalState(fixture);
  assert.ok(journal.started.includes("s0"), `later drain must apply start: ${JSON.stringify(journal.started)}`);
  assert.ok(journal.completed.includes("s0"), `later drain must apply complete after start: ${JSON.stringify(journal.completed)}`);
  assert.deepEqual(journal.completesWithoutStarts, []);
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "done");
});

test("complete-behind-start after one-shot returned still yields", { timeout: 20_000 }, async (t) => {
  // One-shot already exited. Complete-behind-start must not start a full
  // drain that journals slice.started before orch can reserve.
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "after-yield-hold", async () => {
    await hold;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  releaseHold();
  await holder;
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal((await journalState(fixture)).started.includes("s0"), false);

  const complete = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "complete-0",
    action: {
      action: "complete",
      evidence: "Verification: slice s0 done via test ID T-after-yield",
    },
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(complete), true);
  await new Promise((resolve) => setTimeout(resolve, 50));
  const journalAfterComplete = await journalState(fixture);
  assert.equal(
    journalAfterComplete.started.includes("s0"),
    false,
    `complete-behind-start after one-shot must not apply start: ${JSON.stringify(journalAfterComplete.started)}`,
  );

  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  const journal = await journalState(fixture);
  assert.ok(journal.started.includes("s0"));
  assert.ok(journal.completed.includes("s0"));
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "done");
});

test("corrupt exclusive-replan file does not park an accepted start", { timeout: 15_000 }, async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  const key = createHash("sha256").update(fixture.planId).digest("hex").slice(0, 32);
  const reservationPath = path.join(
    fixture.vaultPath,
    ".runtime",
    "thread-locks",
    `${key}.exclusive-replan.json`,
  );
  await mkdir(path.dirname(reservationPath), { recursive: true });
  await writeFile(reservationPath, "{not a reservation owner\n", { mode: 0o600 });
  const aged = new Date(Date.now() - 200_000);
  await utimes(reservationPath, aged, aged);
  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), false);

  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  if (isAcceptedWorkerWrite(start)) {
    await drainWorkerWrites({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
    });
  }
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "in_progress");
  const journal = await journalState(fixture);
  assert.deepEqual(journal.started, ["s0"]);
});

test("young empty exclusive-replan file parks kick (publish window)", { timeout: 15_000 }, async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  const key = createHash("sha256").update(fixture.planId).digest("hex").slice(0, 32);
  const reservationPath = path.join(
    fixture.vaultPath,
    ".runtime",
    "thread-locks",
    `${key}.exclusive-replan.json`,
  );
  await mkdir(path.dirname(reservationPath), { recursive: true });
  await writeFile(reservationPath, "", { mode: 0o600 });
  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), true);

  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  await kickWorkerWriteDrain(
    {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
    },
    { oneShotYield: true },
  );
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "pending", "kick must yield during young empty publish window");
  const journal = await journalState(fixture);
  assert.deepEqual(journal.started, []);
});

test("kick still applies start when no exclusive replan is in flight", { timeout: 20_000 }, async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "no-replan-hold", async () => {
    await hold;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  assert.equal(await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId), false);
  releaseHold();
  await holder;
  // Accept kick one-shot yields; later same-process drain still applies when no replan.
  await drainWorkerWrites({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
  });
  await waitForJournal(fixture, { started: 1 });
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "in_progress");
  const journal = await journalState(fixture);
  assert.deepEqual(journal.started, ["s0"]);
});

// Pin GO: live MCP, process stays up, reservation not live at accept return,
// persist lock not held across accept→replan. Not lock-until-reserved library GO.
test("live MCP: accept kick yields before orch reserve (process stays up)", { timeout: 60_000 }, async (t) => {
  const net = await import("node:net");
  const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;
  const root = await mkdtemp(path.join(tmpdir(), "minni-kick-live-mcp-"));
  t.after(async () => {
    await rm(root, { recursive: true, force: true }).catch(() => {});
  });
  const home = path.join(root, "home");
  const vaultPath = path.join(root, "vault");
  await mkdir(home, { recursive: true });
  await mkdir(vaultPath, { recursive: true });
  const socketPath = path.join(home, "minnid.sock");
  const daemon = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        if (!line.trim()) continue;
        const request = JSON.parse(line);
        socket.write(
          `${JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { ok: true } })}\n`,
        );
      }
    });
  });
  await new Promise((resolve) => daemon.listen(socketPath, resolve));
  t.after(() => daemon.close());

  const created = await createPlan(
    {
      goal: "Live MCP kick vs exclusive replan",
      slices: [{ id: "s0", title: "Parent" }],
      vaultPath,
    },
    { vaultPath, now: () => THREAD_START },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;

  function attachMcp(child) {
    const responses = new Map();
    const waiters = new Map();
    let buffered = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffered += chunk;
      let nl;
      while ((nl = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, nl).trim();
        buffered = buffered.slice(nl + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined) {
            responses.set(msg.id, msg);
            waiters.get(msg.id)?.(msg);
          }
        } catch {
          // protocol noise
        }
      }
    });
    let nextId = 1;
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    const awaitResponse = (id, ms = 20000) =>
      responses.get(id) ??
      new Promise((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error(`timeout waiting for response ${id}`)),
          ms,
        );
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
      });
    const allocId = () => nextId++;
    const call = async (name, args) => {
      const id = allocId();
      send({
        jsonrpc: "2.0",
        id,
        method: "tools/call",
        params: { name, arguments: args },
      });
      const reply = await awaitResponse(id);
      if (reply.error) throw new Error(`${name}: ${JSON.stringify(reply.error)}`);
      if (reply.result?.isError) {
        throw new Error(`${name}: ${reply.result.content?.[0]?.text}`);
      }
      return JSON.parse(reply.result.content[0].text);
    };
    return { send, awaitResponse, call, allocId };
  }

  async function bootMcp() {
    const child = spawn(process.execPath, [SERVER_PATH], {
      env: {
        ...process.env,
        MINNI_HOME: home,
        MINNI_SOCKET_PATH: socketPath,
        MINNI_VAULT_PATH: vaultPath,
        MINNI_CLAUDECODE_VAULT_PATH: vaultPath,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const client = attachMcp(child);
    const initId = client.allocId();
    client.send({
      jsonrpc: "2.0",
      id: initId,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "kick-live-mcp-go", version: "0.0.0" },
      },
    });
    await client.awaitResponse(initId);
    client.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    return { child, ...client };
  }

  const orch = await bootMcp();
  t.after(() => {
    if (orch.child.exitCode === null && orch.child.signalCode === null) {
      orch.child.kill("SIGTERM");
    }
  });
  await orch.call("minni_thread_assign", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
  });
  const claim = await orch.call("minni_thread_claim", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
    idempotency_key: "claim-0",
  });
  assert.ok(claim.token);

  let releaseHold;
  const held = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(vaultPath, planId, "live-mcp-accept-hold", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const worker = await bootMcp();
  t.after(() => {
    if (worker.child.exitCode === null && worker.child.signalCode === null) {
      worker.child.kill("SIGTERM");
    }
  });
  const started = await worker.call("minni_thread_worker_update", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
    claim_token: claim.token,
    idempotency_key: "start-0",
    action: "start",
  });
  assert.equal(started.status, "accepted");
  assert.equal(started.applied, false);
  assert.equal(
    await exclusiveReplanReservationIsLive(vaultPath, planId),
    false,
    "reservation must not be live at accept return",
  );
  assert.equal(worker.child.exitCode, null, "accepting MCP must stay up");
  assert.equal(worker.child.signalCode, null, "accepting MCP must stay up (no SIGKILL)");

  const queued = await listQueuedWorkerWrites(vaultPath, planId);
  assert.ok(
    queued.some((item) => item.idempotencyKey === "start-0"),
    `start must be queued: ${JSON.stringify(queued)}`,
  );

  // Lock NOT held across accept→replan. Kick free; reservation still absent.
  releaseHold();
  await holder;
  assert.equal(await exclusiveReplanReservationIsLive(vaultPath, planId), false);
  await new Promise((resolve) => setTimeout(resolve, 75));
  const midJournal = await journalState({ notePath, planId });
  assert.equal(
    midJournal.started.includes("s0"),
    false,
    `kick must not apply before orch reserves: ${JSON.stringify(midJournal.started)}`,
  );
  assert.equal(worker.child.exitCode, null, "process stays up after lock free");

  const replan = await orch.call("minni_thread_replan", {
    plan_id: planId,
    drop_slice_ids: ["s0"],
    add_slices: [
      { id: "child-a", title: "Child A" },
      { id: "child-b", title: "Child B" },
    ],
  });
  assert.ok(replan.plan_id === planId || replan.plan?.plan_id === planId || replan.slices);

  await drainWorkerWrites({ vaultPath, notePath, planId });
  const leftover = await listQueuedWorkerWrites(vaultPath, planId);
  assert.equal(leftover.length, 0, `leftover must drop: ${JSON.stringify(leftover)}`);
  const plan = await rehydratePlan(notePath);
  const parent = plan.slices.find((slice) => slice.id === "s0");
  assert.ok(parent, "split never deletes parent");
  assert.equal(parent.status, "superseded");
  assert.notEqual(parent.status, "in_progress");
  assert.notEqual(parent.status, "done");
  const journal = await journalState({ notePath, planId });
  assert.equal(
    journal.started.includes("s0"),
    false,
    `live MCP GO: no slice.started on superseded s0: ${JSON.stringify(journal.started)}`,
  );
  assert.equal(worker.child.exitCode, null, "accepting MCP still up at end (no SIGKILL)");

  const complete = await worker.call("minni_thread_worker_update", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
    claim_token: claim.token,
    idempotency_key: "complete-s0-after-live-split",
    action: "complete",
    evidence: "Verification: slice s0 done via live MCP after split",
  }).catch((error) => ({ status: "error", error: String(error) }));
  assert.equal(
    complete.status,
    "error",
    `complete on superseded s0 must error (MCP returns status:error, not transport isError): ${JSON.stringify(complete)}`,
  );
  const after = await rehydratePlan(notePath);
  assert.equal(after.slices.find((slice) => slice.id === "s0")?.status, "superseded");
  assert.notEqual(after.slices.find((slice) => slice.id === "s0")?.status, "done");
});

// Pin GO: live MCP, standing tick is a separate Node child (not worker
// in-process kick). Process stays up. Exclusive split supersedes s0.
test("live MCP: standing tick yields before orch reserve (process stays up)", { timeout: 60_000 }, async (t) => {
  const net = await import("node:net");
  const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;
  const STANDING_TICK_JS = new URL("../dist/standing-drain-tick.js", import.meta.url).pathname;
  const root = await mkdtemp(path.join(tmpdir(), "minni-standing-live-mcp-"));
  t.after(async () => {
    await rm(root, { recursive: true, force: true }).catch(() => {});
  });
  const home = path.join(root, "home");
  const vaultPath = path.join(root, "vault");
  await mkdir(home, { recursive: true });
  await mkdir(vaultPath, { recursive: true });
  const socketPath = path.join(home, "minnid.sock");
  const daemon = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        if (!line.trim()) continue;
        const request = JSON.parse(line);
        socket.write(
          `${JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { ok: true } })}\n`,
        );
      }
    });
  });
  await new Promise((resolve) => daemon.listen(socketPath, resolve));
  t.after(() => daemon.close());

  const created = await createPlan(
    {
      goal: "Live MCP standing tick vs exclusive replan",
      slices: [{ id: "s0", title: "Parent" }],
      vaultPath,
    },
    { vaultPath, now: () => THREAD_START },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;

  function attachMcp(child) {
    const responses = new Map();
    const waiters = new Map();
    let buffered = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffered += chunk;
      let nl;
      while ((nl = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, nl).trim();
        buffered = buffered.slice(nl + 1);
        if (!line.trim()) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined) {
            responses.set(msg.id, msg);
            waiters.get(msg.id)?.(msg);
          }
        } catch {
          // protocol noise
        }
      }
    });
    let nextId = 1;
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    const awaitResponse = (id, ms = 20000) =>
      responses.get(id) ??
      new Promise((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error(`timeout waiting for response ${id}`)),
          ms,
        );
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
      });
    const allocId = () => nextId++;
    const call = async (name, args) => {
      const id = allocId();
      send({
        jsonrpc: "2.0",
        id,
        method: "tools/call",
        params: { name, arguments: args },
      });
      const reply = await awaitResponse(id);
      if (reply.error) throw new Error(`${name}: ${JSON.stringify(reply.error)}`);
      if (reply.result?.isError) {
        throw new Error(`${name}: ${reply.result.content?.[0]?.text}`);
      }
      return JSON.parse(reply.result.content[0].text);
    };
    return { send, awaitResponse, call, allocId };
  }

  async function bootMcp() {
    const child = spawn(process.execPath, [SERVER_PATH], {
      env: {
        ...process.env,
        MINNI_HOME: home,
        MINNI_SOCKET_PATH: socketPath,
        MINNI_VAULT_PATH: vaultPath,
        MINNI_CLAUDECODE_VAULT_PATH: vaultPath,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const client = attachMcp(child);
    const initId = client.allocId();
    client.send({
      jsonrpc: "2.0",
      id: initId,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "standing-live-mcp-go", version: "0.0.0" },
      },
    });
    await client.awaitResponse(initId);
    client.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    return { child, ...client };
  }

  const orch = await bootMcp();
  t.after(() => {
    if (orch.child.exitCode === null && orch.child.signalCode === null) {
      orch.child.kill("SIGTERM");
    }
  });
  await orch.call("minni_thread_assign", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
  });
  const claim = await orch.call("minni_thread_claim", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
    idempotency_key: "claim-0",
  });
  assert.ok(claim.token);

  let releaseHold;
  const held = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(vaultPath, planId, "live-mcp-standing-hold", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));

  const worker = await bootMcp();
  t.after(() => {
    if (worker.child.exitCode === null && worker.child.signalCode === null) {
      worker.child.kill("SIGTERM");
    }
  });
  const started = await worker.call("minni_thread_worker_update", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
    claim_token: claim.token,
    idempotency_key: "start-0",
    action: "start",
  });
  assert.equal(started.status, "accepted");
  assert.equal(started.applied, false);
  assert.equal(
    await exclusiveReplanReservationIsLive(vaultPath, planId),
    false,
    "reservation must not be live at accept return",
  );
  assert.equal(worker.child.exitCode, null, "accepting MCP must stay up");
  assert.equal(worker.child.signalCode, null, "accepting MCP must stay up (no SIGKILL)");

  const queued = await listQueuedWorkerWrites(vaultPath, planId);
  assert.ok(
    queued.some((item) => item.idempotencyKey === "start-0"),
    `start must be queued: ${JSON.stringify(queued)}`,
  );
  const rawQueued = await readRawQTickets(vaultPath, planId);
  const rawStart = rawQueued.find((item) => item.idempotencyKey === "start-0");
  assert.ok(rawStart);
  assertQTicketHasNoRawToken(rawStart, claim.token);
  assert.equal(rawStart.acceptorPid, worker.child.pid);

  releaseHold();
  await holder;
  assert.equal(await exclusiveReplanReservationIsLive(vaultPath, planId), false);
  await new Promise((resolve) => setTimeout(resolve, 75));
  const midJournal = await journalState({ notePath, planId });
  assert.equal(
    midJournal.started.includes("s0"),
    false,
    `kick must not apply before orch reserves: ${JSON.stringify(midJournal.started)}`,
  );
  assert.equal(worker.child.exitCode, null, "process stays up after lock free");

  const standingBegan = Date.now();
  const tick = spawn(process.execPath, [STANDING_TICK_JS, vaultPath], {
    env: { ...process.env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let tickErr = "";
  tick.stderr.on("data", (chunk) => {
    tickErr += chunk.toString();
  });
  const tickExit = await new Promise((resolve, reject) => {
    tick.once("error", reject);
    tick.once("exit", (code, signal) => resolve({ code, signal }));
  });
  const standingMs = Date.now() - standingBegan;
  assert.equal(tickExit.signal, null, `standing tick child must exit cleanly: ${tickErr}`);
  assert.equal(tickExit.code, 0, `standing tick child must exit 0: ${tickErr}`);
  assert.ok(
    standingMs < 2_000,
    `standing tick child must not sit the 60s drain loop: ${standingMs}ms`,
  );
  assert.equal(worker.child.exitCode, null, "worker still up after standing tick");
  assert.equal(worker.child.signalCode, null, "worker still up after standing tick (no SIGKILL)");
  const afterTick = await journalState({ notePath, planId });
  assert.equal(
    afterTick.started.includes("s0"),
    false,
    `standing tick child must not apply start while worker is live: ${JSON.stringify(afterTick.started)}`,
  );

  const replan = await orch.call("minni_thread_replan", {
    plan_id: planId,
    drop_slice_ids: ["s0"],
    add_slices: [
      { id: "child-a", title: "Child A" },
      { id: "child-b", title: "Child B" },
    ],
  });
  assert.ok(replan.plan_id === planId || replan.plan?.plan_id === planId || replan.slices);

  await drainWorkerWrites({ vaultPath, notePath, planId });
  const leftover = await listQueuedWorkerWrites(vaultPath, planId);
  assert.equal(leftover.length, 0, `leftover must drop: ${JSON.stringify(leftover)}`);
  const plan = await rehydratePlan(notePath);
  const parent = plan.slices.find((slice) => slice.id === "s0");
  assert.ok(parent, "split never deletes parent");
  assert.equal(parent.status, "superseded");
  assert.notEqual(parent.status, "in_progress");
  assert.notEqual(parent.status, "done");
  const journal = await journalState({ notePath, planId });
  assert.equal(
    journal.started.includes("s0"),
    false,
    `live MCP standing GO: no slice.started on superseded s0: ${JSON.stringify(journal.started)}`,
  );
  assert.equal(worker.child.exitCode, null, "accepting MCP still up at end (no SIGKILL)");

  const complete = await worker.call("minni_thread_worker_update", {
    plan_id: planId,
    slice_id: "s0",
    worker_agent_id: "worker-0",
    claim_token: claim.token,
    idempotency_key: "complete-s0-after-standing-live-split",
    action: "complete",
    evidence: "Verification: slice s0 done via live MCP standing tick after split",
  }).catch((error) => ({ status: "error", error: String(error) }));
  assert.equal(
    complete.status,
    "error",
    `complete on superseded s0 must error (MCP returns status:error, not transport isError): ${JSON.stringify(complete)}`,
  );
  const after = await rehydratePlan(notePath);
  assert.equal(after.slices.find((slice) => slice.id === "s0")?.status, "superseded");
  assert.notEqual(after.slices.find((slice) => slice.id === "s0")?.status, "done");
});

test("standing drain applies live start after DEFAULT_WAIT_MS while acceptor stays up", { timeout: 20_000 }, async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  let entered;
  const acquired = new Promise((resolve) => { entered = resolve; });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "aged-standing-hold", async () => {
    entered();
    await hold;
  });
  await acquired;
  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  releaseHold();
  await holder;
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal((await journalState(fixture)).started.includes("s0"), false);

  const dir = workerWriteQueueDir(fixture.vaultPath, fixture.planId);
  const names = await readdir(dir);
  let aged = false;
  for (const name of names) {
    if (!name.endsWith(".json") || name === "progress.json") continue;
    const filePath = path.join(dir, name);
    const ticket = JSON.parse(await readFile(filePath, "utf8"));
    if (ticket.idempotencyKey !== "start-0") continue;
    ticket.enqueuedAt = new Date(Date.now() - 6_000).toISOString();
    await writeFile(filePath, `${JSON.stringify(ticket)}\n`, { mode: 0o600 });
    aged = true;
  }
  assert.equal(aged, true, "start ticket must exist to age past DEFAULT_WAIT_MS");

  const later = await drainPendingWorkerWritesForVault(fixture.vaultPath);
  assert.ok(later.planIds.includes(fixture.planId), JSON.stringify(later));
  const journal = await journalState(fixture);
  assert.deepEqual(journal.started, ["s0"]);
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "in_progress");
});

test("legacy queued start without acceptor pid still applies on standing drain", { timeout: 20_000 }, async (t) => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  let releaseHold;
  const hold = new Promise((resolve) => {
    releaseHold = resolve;
  });
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "legacy-standing-hold", async () => {
    await hold;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const start = await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "s0",
    workerAgentId: "worker-0",
    token: claim.token,
    idempotencyKey: "start-0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(start), true);
  releaseHold();
  await holder;
  await new Promise((resolve) => setTimeout(resolve, 50));

  const dir = workerWriteQueueDir(fixture.vaultPath, fixture.planId);
  const names = await readdir(dir);
  let stripped = false;
  for (const name of names) {
    if (!name.endsWith(".json") || name === "progress.json") continue;
    const filePath = path.join(dir, name);
    const ticket = JSON.parse(await readFile(filePath, "utf8"));
    if (ticket.idempotencyKey !== "start-0") continue;
    delete ticket.acceptorPid;
    delete ticket.processStartMarker;
    await writeFile(filePath, `${JSON.stringify(ticket)}\n`, { mode: 0o600 });
    stripped = true;
  }
  assert.equal(stripped, true, "start ticket must exist to strip acceptor pid");

  const later = await drainPendingWorkerWritesForVault(fixture.vaultPath);
  assert.ok(later.planIds.includes(fixture.planId), JSON.stringify(later));
  const journal = await journalState(fixture);
  assert.deepEqual(journal.started, ["s0"]);
});


test('busy completion rejects a superseded parent instead of accepting an undrainable ticket', async t => {
  const fixture = await burstFixture(t, 1);
  const [claim] = await assignAndClaimAll(fixture);
  const plan = await rehydratePlan(fixture.notePath);
  const split = applySliceDelta(plan, {
    drop_slice_ids: ['s0'],
    add_slices: [{ id: 'replacement', title: 'Replacement slice' }],
  });
  await persistPlan(split, { vaultPath: fixture.vaultPath, notePath: fixture.notePath });
  await withThreadLock(fixture.vaultPath, fixture.planId, 'hold-after-split', async () => {
    await assert.rejects(updateClaimedSlice({
      ...fixture,
      sliceId: 's0',
      workerAgentId: 'worker-0',
      token: claim.token,
      idempotencyKey: 'complete-after-split-while-busy',
      action: { action: 'complete', evidence: 'Stale parent cannot be completed' },
      now: new Date('2026-08-18T12:03:00.000Z'),
    }), /not worker-updatable/);
    assert.deepEqual(await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId), []);
  });
  assert.equal((await rehydratePlan(fixture.notePath)).slices.find(s => s.id === 's0').status, 'superseded');
});

// Counts actual immutable ticket reads, not elapsed time or mocked drain calls.
test("ordinary drain reads each shrinking queue only for locked selection and fresh progress", async (t) => {
  const fixture = await burstFixture(t, 3);
  const claims = await assignAndClaimAll(fixture);
  const queueDir = workerWriteQueueDir(fixture.vaultPath, fixture.planId);
  const counts = [];
  for (const action of ["start", "complete"]) {
    for (let index = 0; index < fixture.n; index += 1) {
      await enqueueWorkerWrite({
        vaultPath: fixture.vaultPath, planId: fixture.planId,
        sliceId: `s${index}`, workerAgentId: `worker-${index}`,
        token: claims[index].token, idempotencyKey: `${action}-${index}`,
        action: action === "complete" ? { action, evidence: "Verified fixture completion" } : { action }, now: new Date("2026-08-18T12:01:00.000Z"),
      });
    }
    const original = fs.promises.readFile;
    let ticketReads = 0;
    fs.promises.readFile = async function(file, ...args) {
      if (typeof file === "string" && path.dirname(file) === queueDir &&
          file.endsWith(".json") && path.basename(file) !== "progress.json") ticketReads += 1;
      return original.call(this, file, ...args);
    };
    syncBuiltinESMExports();
    try {
      await drainWorkerWrites({ ...fixture, now: new Date("2026-08-18T12:01:00.000Z") });
    } finally {
      fs.promises.readFile = original;
      syncBuiltinESMExports();
    }
    counts.push(ticketReads);
    assert.equal((await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId)).length, 0);
    const current = await rehydratePlan(fixture.notePath);
    assert.ok(current.slices.every((slice) => slice.status === (action === "start" ? "in_progress" : "done")));
  }
  const journal = await journalState(fixture);
  assert.equal(journal.started.length, 3);
  assert.equal(journal.completed.length, 3);
  t.diagnostic(`queue ticket reads start/complete: ${JSON.stringify(counts)}`);
  assert.deepEqual(counts, [9, 9]);
});

for (const hold of [withThreadLock, withExclusiveReplanReservation]) {
  test(`empty ordinary drain returns while ${hold.name} is held`, async (t) => {
    const fixture = await burstFixture(t, 1);
    await hold(fixture.vaultPath, fixture.planId, "empty-held", async () => {
      let timer;
      try {
        const result = await Promise.race([
          drainWorkerWrites(fixture),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("empty drain waited for held lock")), 1000); }),
        ]);
        assert.equal(result, false);
      } finally {
        clearTimeout(timer);
      }
    });
  });
}

test("idle ordinary kick does not contend for the persist lock", async (t) => {
  const fixture = await burstFixture(t, 1);
  const original = fs.promises.mkdir;
  let lockAttempts = 0;
  fs.promises.mkdir = async function(dir, ...args) {
    if (String(dir).includes("thread-locks")) lockAttempts += 1;
    return original.call(this, dir, ...args);
  };
  syncBuiltinESMExports();
  try {
    assert.equal(await drainWorkerWrites(fixture), false);
  } finally {
    fs.promises.mkdir = original;
    syncBuiltinESMExports();
  }
  assert.equal(lockAttempts, 0, "empty drain must not force another worker into accepted-only behavior");
});

// Virtual time distinguishes useful progress from a parked queue without a
// machine-speed assertion. These exercise the same waiter the wet burst uses.
for (const scenario of ["progress", "stalled", "never-finishes"]) {
  test(`journal waiter budget: ${scenario}`, async () => {
    let elapsed = 0;
    const options = {
      stallMs: 20_000, clock: () => elapsed, kick: () => {},
      pause: async () => { elapsed += 10_000; }, readQueue: async () => [],
      readState: async () => ({
        started: Array.from({ length: scenario === "stalled" ? 0 : elapsed / 10_000 }),
        completed: [], completesWithoutStarts: [],
      }),
    };
    if (scenario === "progress") {
      const result = await waitForJournal({}, { started: 3 }, 50_000, options);
      assert.equal(result.started.length, 3);
      assert.equal(elapsed, 30_000);
    } else {
      await assert.rejects(waitForJournal({}, { started: 99 }, 50_000, options), /timeout waiting/);
      assert.equal(elapsed, scenario === "stalled" ? 20_000 : 50_000);
    }
  });
}

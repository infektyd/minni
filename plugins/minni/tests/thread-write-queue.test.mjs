// Dump-and-return Thread lock Q. Accepted is not applied.
// Drain is one persist authority. Replan is exclusive, not a Q item.
// THREAD_BUSY is overflow (Q full or drain stuck), not N=40.
// Durable drain outlives the accepting process; in-process kick does not.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
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

async function waitForJournal(fixture, { started = 0, completed = 0 }, timeoutMs = 20_000) {
  const begin = Date.now();
  let last;
  while (Date.now() - begin < timeoutMs) {
    last = await journalState(fixture);
    if (last.started.length >= started && last.completed.length >= completed) {
      return last;
    }
    kickWorkerWriteDrain({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
    });
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(
    `timeout waiting for ${started} starts / ${completed} completes; got ${JSON.stringify({
      started: last?.started?.length,
      completed: last?.completed?.length,
      completesWithoutStarts: last?.completesWithoutStarts,
    })}`,
  );
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
  const journal = await waitForJournal(fixture, { started: n, completed: n });
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
  const journal = await waitForJournal(fixture, { started: 1 });
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
  const holder = withThreadLock(fixture.vaultPath, fixture.planId, "apply-throw-hold", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
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

test("kick yields to exclusive replan with accepting process still up", { timeout: 30_000 }, async (t) => {
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

  let reservedBeforeLock = false;
  const split = withExclusiveReplanReservation(
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
          const queued = await listQueuedWorkerWrites(fixture.vaultPath, fixture.planId);
          assert.ok(
            queued.some((item) => item.idempotencyKey === "start-0"),
            `start must still be queued when exclusive replan acquires: ${JSON.stringify(queued)}`,
          );
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

  const waitReserved = Date.now();
  while (Date.now() - waitReserved < 2_000) {
    if (await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId)) break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(
    await exclusiveReplanReservationIsLive(fixture.vaultPath, fixture.planId),
    true,
    "exclusive replan must reserve before the persist lock is free",
  );

  releaseHold();
  await holder;
  await split;
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
  await waitForJournal(fixture, { started: 1 });
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].status, "in_progress");
  const journal = await journalState(fixture);
  assert.deepEqual(journal.started, ["s0"]);
});

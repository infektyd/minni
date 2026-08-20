// Dump-and-return Thread lock Q. Accepted is not applied.
// Drain is one persist authority. Replan is exclusive, not a Q item.
// THREAD_BUSY is overflow (Q full or drain stuck), not N=40.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { applySliceDelta, createPlan, journalPathFor, persistPlan, rehydratePlan } from "../dist/plan.js";
import { readThreadEvents } from "../dist/thread-events.js";
import { withThreadLock } from "../dist/thread-lock.js";
import {
  assignSlice,
  claimSlice,
  isAcceptedWorkerWrite,
  kickWorkerWriteDrain,
  updateClaimedSlice,
  workerUpdateMcpPayload,
  withThreadPlanLock,
} from "../dist/thread-worker.js";
import {
  DEFAULT_QUEUE_MAX,
  enqueueWorkerWrite,
  listQueuedWorkerWrites,
  pickNextQueuedWorkerWrite,
} from "../dist/thread-write-queue.js";

const THREAD_START = new Date("2026-08-18T12:00:00.000Z");
const TEST_ORCHESTRATOR_ACTOR = "orchestrator-caller";

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
  await new Promise((resolve) => setTimeout(resolve, 40));
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
    token: "t",
    idempotencyKey: "complete-0",
    action: { action: "complete" },
  };
  const start = {
    ticketId: "s",
    enqueuedAt: "2026-08-18T12:00:01.000Z",
    planId: "p",
    sliceId: "s0",
    workerAgentId: "w",
    token: "t",
    idempotencyKey: "start-0",
    action: { action: "start" },
  };
  const other = {
    ticketId: "o",
    enqueuedAt: "2026-08-18T12:00:02.000Z",
    planId: "p",
    sliceId: "s1",
    workerAgentId: "w",
    token: "t",
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

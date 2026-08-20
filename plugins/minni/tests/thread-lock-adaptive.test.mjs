import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { applySliceDelta, createPlan, journalPathFor, persistPlan, rehydratePlan } from "../dist/plan.js";
import { readThreadEvents } from "../dist/thread-events.js";
import { withThreadLock } from "../dist/thread-lock.js";
import {
  assignSlice,
  claimSlice,
  updateClaimedSlice,
  withThreadPlanLock,
} from "../dist/thread-worker.js";

const THREAD_START = new Date("2026-08-18T12:00:00.000Z");
const TEST_ORCHESTRATOR_ACTOR = "orchestrator-caller";

async function burstFixture(t, n) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), `minni-thread-lock-burst-${n}-`));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  const slices = Array.from({ length: n }, (_, index) => ({
    id: `s${index}`,
    title: `Slice ${index}`,
  }));
  const created = await createPlan(
    {
      goal: `Adaptive lock burst N=${n}`,
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
    .then(() => ({ index, ok: true }))
    .catch((error) => ({
      index,
      ok: false,
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

async function runWetBurst(t, n) {
  const fixture = await burstFixture(t, n);
  const claims = await assignAndClaimAll(fixture);
  const starts = await startBurst(fixture, claims);
  const startBusy = starts.filter((result) => result.code === "THREAD_BUSY");
  const startFail = starts.filter((result) => !result.ok);
  const completes = await completeBurst(fixture, claims);
  const completeBusy = completes.filter((result) => result.code === "THREAD_BUSY");
  const completeFail = completes.filter((result) => !result.ok);
  const journal = await journalState(fixture);
  return {
    n,
    startOk: starts.filter((result) => result.ok).length,
    startBusy: startBusy.length,
    startFail,
    completeOk: completes.filter((result) => result.ok).length,
    completeBusy: completeBusy.length,
    completeFail,
    journal,
  };
}

test("wet N=40 starts+completes acquire without start THREAD_BUSY and stay journal-dense", { timeout: 60_000 }, async (t) => {
  const result = await runWetBurst(t, 40);
  assert.equal(result.startBusy, 0, `start THREAD_BUSY must not be the N=40 default: ${JSON.stringify(result.startFail)}`);
  assert.equal(result.startOk, 40, JSON.stringify(result.startFail));
  assert.equal(result.completeBusy, 0, JSON.stringify(result.completeFail));
  assert.equal(result.completeOk, 40, JSON.stringify(result.completeFail));
  assert.equal(result.journal.started.length, 40);
  assert.equal(result.journal.completed.length, 40);
  assert.deepEqual(result.journal.completesWithoutStarts, []);
  assert.equal(result.journal.dense, true, `journal seq not dense ${result.journal.firstSeq}..${result.journal.lastSeq}`);
});

test("wet N=20 starts+completes still hold after the adaptive wait", { timeout: 45_000 }, async (t) => {
  const result = await runWetBurst(t, 20);
  assert.equal(result.startBusy, 0);
  assert.equal(result.startOk, 20, JSON.stringify(result.startFail));
  assert.equal(result.completeOk, 20, JSON.stringify(result.completeFail));
  assert.deepEqual(result.journal.completesWithoutStarts, []);
  assert.equal(result.journal.dense, true);
});

test("replan during an N=40 start burst stays exclusive on the shared Thread lock", { timeout: 60_000 }, async (t) => {
  const fixture = await burstFixture(t, 40);
  const claims = await assignAndClaimAll(fixture);
  const intervals = [];

  const starts = startBurst(fixture, claims);
  const replan = withThreadPlanLock(
    {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      operationId: "burst-replan-expand",
    },
    async (plan) => {
      const entered = Date.now();
      const updated = applySliceDelta(plan, {
        add_slices: [{ id: "replan-extra", title: "Replan during burst" }],
      });
      await persistPlan(updated, {
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
      });
      const left = Date.now();
      intervals.push({ kind: "replan", entered, left });
      return updated;
    },
  );

  const [startResults, replanResult] = await Promise.all([
    starts,
    replan.then(() => ({ ok: true })).catch((error) => ({
      ok: false,
      code: error?.code,
      message: error instanceof Error ? error.message : String(error),
    })),
  ]);

  const startBusy = startResults.filter((result) => result.code === "THREAD_BUSY");
  assert.equal(startBusy.length, 0, JSON.stringify(startResults.filter((result) => !result.ok)));
  assert.equal(startResults.filter((result) => result.ok).length, 40);
  assert.equal(replanResult.ok, true, JSON.stringify(replanResult));

  const final = await rehydratePlan(fixture.notePath);
  assert.ok(
    final.slices.some((slice) => slice.id === "replan-extra"),
    "expand replan must land",
  );
  assert.equal(
    final.slices.filter((slice) => slice.id.startsWith("s") && slice.status === "in_progress").length,
    40,
  );

  const journal = await journalState(fixture);
  assert.equal(journal.started.length, 40);
  assert.deepEqual(journal.completesWithoutStarts, []);
  assert.equal(journal.dense, true);

  // Lock-level exclusivity: a distinguished replan-shaped holder cannot overlap
  // a burst of starts on the same plan lock.
  const lockRoot = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-replan-excl-"));
  t.after(() => rm(lockRoot, { recursive: true, force: true }));
  const lockIntervals = [];
  const holders = [
    ...Array.from({ length: 40 }, (_, index) =>
      withThreadLock(lockRoot, "plan-replan-excl", `start-${index}`, async () => {
        const entered = Date.now();
        await new Promise((resolve) => setTimeout(resolve, 20));
        const left = Date.now();
        lockIntervals.push({ kind: "start", index, entered, left });
      }),
    ),
    withThreadLock(lockRoot, "plan-replan-excl", "replan", async () => {
      const entered = Date.now();
      await new Promise((resolve) => setTimeout(resolve, 20));
      const left = Date.now();
      lockIntervals.push({ kind: "replan", entered, left });
    }),
  ];
  await Promise.all(holders);
  lockIntervals.sort((a, b) => a.entered - b.entered);
  assert.equal(lockIntervals.filter((row) => row.kind === "replan").length, 1);
  assert.equal(lockIntervals.filter((row) => row.kind === "start").length, 40);
  for (let i = 1; i < lockIntervals.length; i += 1) {
    assert.ok(
      lockIntervals[i - 1].left <= lockIntervals[i].entered,
      `replan/start overlapped: ${JSON.stringify(lockIntervals.slice(i - 1, i + 1))}`,
    );
  }
});

test("THREAD_BUSY remains fail-closed overflow for a stuck live owner", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-overflow-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  let releaseHolder;
  const held = new Promise((resolve) => {
    releaseHolder = resolve;
  });
  const holder = withThreadLock(root, "plan-overflow", "live-owner", async () => {
    await held;
  });
  await new Promise((resolve) => setTimeout(resolve, 30));
  await assert.rejects(
    withThreadLock(root, "plan-overflow", "overflow-waiter", async () => undefined, {
      waitMs: 40,
      staleMs: 120_000,
      pollMs: 5,
      isProcessAlive: () => true,
    }),
    (error) => error?.code === "THREAD_BUSY",
  );
  releaseHolder();
  await holder;
});

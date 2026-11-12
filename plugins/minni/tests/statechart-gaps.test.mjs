import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  activeThreadSessionPath,
  createPlan,
  historyPathFor,
  journalPathFor,
  listLiveActiveThreadBindings,
  PlanHistoryAppendError,
  rehydratePlan,
  setActivePlan,
  sweepDeadActiveThreadPointers,
} from "../dist/plan.js";
import { sessionStateKey } from "../dist/recall-state.js";
import { readWorkerUpdateReceipt } from "../dist/thread-claims.js";
import { readThreadEvents } from "../dist/thread-events.js";
import { withThreadLock } from "../dist/thread-lock.js";
import {
  formatSessionSubscriberId,
  liveSessionSubscribersForPlan,
  loadRelayStore,
  saveRelayStore,
  sweepDeadRelayCursors,
} from "../dist/thread-notification-relay.js";
import {
  assignSlice,
  claimSlice,
  drainWorkerWrites,
  isAcceptedWorkerWrite,
  joinWorkerWriteDrains,
  START_ACCEPTED_RECEIPT_KIND,
  startAcceptedReceiptKey,
  updateClaimedSlice,
} from "../dist/thread-worker.js";
import {
  listQueuedWorkerWrites,
  removeQueuedWorkerWrite,
} from "../dist/thread-write-queue.js";
import { auditTail, ensureVault } from "../dist/vault.js";

const SERVER_PATH = new URL("../dist/server.js", import.meta.url).pathname;
const THREAD_START = new Date("2026-08-18T12:00:00.000Z");

async function makeTempVault(t, prefix) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), prefix));
  t.after(async () => {
    await joinWorkerWriteDrains(vaultPath).catch(() => {});
    await rm(vaultPath, { recursive: true, force: true }).catch(() => {});
  });
  return vaultPath;
}

test("Chart 3a: after lease expiry on an in_progress slice, reclaiming worker cannot complete without starting in the new generation", async (t) => {
  const vaultPath = await makeTempVault(t, "minni-chart3-expiry-");
  const created = await createPlan(
    {
      goal: "Chart 3 lease expiry start-before-complete",
      slices: [{ id: "s1", title: "Slice 1" }],
      vaultPath,
    },
    { vaultPath, now: () => new Date(THREAD_START) },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;

  await assignSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    actorAgentId: "orchestrator",
    now: new Date("2026-08-18T12:00:00.000Z"),
  });
  const claimG0 = await claimSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a-g0",
    ttlSeconds: 60,
    now: new Date("2026-08-18T12:00:05.000Z"),
  });
  const startedG0 = await updateClaimedSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    token: claimG0.token,
    idempotencyKey: "start-a-g0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:00:10.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(startedG0), false);
  assert.equal(startedG0.slice.status, "in_progress");

  // Lease expires (t + 120s > 60s TTL); worker-a reclaims s1 in g1 while status stays in_progress.
  const claimG1 = await claimSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a-g1",
    ttlSeconds: 300,
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.ok(claimG1.generation > claimG0.generation);
  const afterReclaim = await rehydratePlan(notePath);
  assert.equal(afterReclaim.slices[0].status, "in_progress");

  // Completing directly in g1 without starting in g1 MUST be rejected.
  await assert.rejects(
    updateClaimedSlice({
      vaultPath,
      notePath,
      planId,
      sliceId: "s1",
      workerAgentId: "worker-a",
      token: claimG1.token,
      idempotencyKey: "complete-a-g1-without-start",
      action: {
        action: "complete",
        evidence: "Attempted completion without starting in g1",
      },
      now: new Date("2026-08-18T12:02:10.000Z"),
    }),
    /complete cannot persist done without start/,
  );

  // Also rejected on busy fallback path (refuseCompleteUntilStartApplied).
  await withThreadLock(vaultPath, planId, "hold-chart3-expiry", async () => {
    await assert.rejects(
      updateClaimedSlice({
        vaultPath,
        notePath,
        planId,
        sliceId: "s1",
        workerAgentId: "worker-a",
        token: claimG1.token,
        idempotencyKey: "complete-a-g1-without-start-busy",
        action: {
          action: "complete",
          evidence: "Attempted busy completion without starting in g1",
        },
        now: new Date("2026-08-18T12:02:15.000Z"),
      }),
      /complete cannot persist done without start/,
    );
  });

  // Starting in g1 and then completing succeeds.
  await updateClaimedSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    token: claimG1.token,
    idempotencyKey: "start-a-g1",
    action: { action: "start" },
    now: new Date("2026-08-18T12:02:20.000Z"),
  });
  const completedG1 = await updateClaimedSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    token: claimG1.token,
    idempotencyKey: "complete-a-g1-after-start",
    action: {
      action: "complete",
      evidence: "Verified after starting in g1",
    },
    now: new Date("2026-08-18T12:02:30.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(completedG1), false);
  assert.equal(completedG1.slice.status, "done");
});

test("Chart 3b: after orchestrator assignSlice reassignment on an in_progress slice, new worker cannot complete without starting first", async (t) => {
  const vaultPath = await makeTempVault(t, "minni-chart3-reassign-");
  const created = await createPlan(
    {
      goal: "Chart 3 reassignment start-before-complete",
      slices: [{ id: "s1", title: "Slice 1" }],
      vaultPath,
    },
    { vaultPath, now: () => new Date(THREAD_START) },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;

  await assignSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    actorAgentId: "orchestrator",
    now: new Date("2026-08-18T12:00:00.000Z"),
  });
  const claimA = await claimSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a-g0",
    now: new Date("2026-08-18T12:00:05.000Z"),
  });
  await updateClaimedSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    token: claimA.token,
    idempotencyKey: "start-a-g0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:00:10.000Z"),
  });

  // Orchestrator reassigns s1 from worker-a to worker-b while s1.status === "in_progress".
  const reassigned = await assignSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-b",
    actorAgentId: "orchestrator",
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(reassigned.slice.status, "in_progress");

  const claimB = await claimSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-b",
    idempotencyKey: "claim-b-g1",
    now: new Date("2026-08-18T12:01:05.000Z"),
  });
  assert.ok(claimB.generation > claimA.generation);

  await assert.rejects(
    updateClaimedSlice({
      vaultPath,
      notePath,
      planId,
      sliceId: "s1",
      workerAgentId: "worker-b",
      token: claimB.token,
      idempotencyKey: "complete-b-no-start",
      action: {
        action: "complete",
        evidence: "Attempted completion on reassigned slice before start",
      },
      now: new Date("2026-08-18T12:01:10.000Z"),
    }),
    /complete cannot persist done without start/,
  );

  await updateClaimedSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-b",
    token: claimB.token,
    idempotencyKey: "start-b-g1",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:20.000Z"),
  });
  const completedB = await updateClaimedSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-b",
    token: claimB.token,
    idempotencyKey: "complete-b-ok",
    action: {
      action: "complete",
      evidence: "Completed cleanly after starting in reassigned generation",
    },
    now: new Date("2026-08-18T12:01:30.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(completedB), false);
  assert.equal(completedB.slice.status, "done");
});

test("Chart 3c: queued start + complete on an already-in_progress reassigned slice blocks pending start stamp and drains start -> complete cleanly", async (t) => {
  const vaultPath = await makeTempVault(t, "minni-chart3-queued-");
  const created = await createPlan(
    {
      goal: "Chart 3 queued start before complete on in_progress slice",
      slices: [{ id: "s1", title: "Slice 1" }],
      vaultPath,
    },
    { vaultPath, now: () => new Date(THREAD_START) },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;

  await assignSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    actorAgentId: "orchestrator",
    now: new Date("2026-08-18T12:00:00.000Z"),
  });
  const claimA = await claimSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a-g0",
    now: new Date("2026-08-18T12:00:05.000Z"),
  });
  await updateClaimedSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    token: claimA.token,
    idempotencyKey: "start-a-g0",
    action: { action: "start" },
    now: new Date("2026-08-18T12:00:10.000Z"),
  });

  await assignSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-b",
    actorAgentId: "orchestrator",
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  const claimB = await claimSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-b",
    idempotencyKey: "claim-b-g1",
    now: new Date("2026-08-18T12:01:05.000Z"),
  });

  // 1. Enqueue start under lock so start_accepted receipt is written with slice.status === "in_progress".
  await withThreadLock(vaultPath, planId, "hold-for-stamp-check", async () => {
    const acceptedStart = await updateClaimedSlice({
      vaultPath,
      notePath,
      planId,
      sliceId: "s1",
      workerAgentId: "worker-b",
      token: claimB.token,
      idempotencyKey: "start-b-stamp-only",
      action: { action: "start" },
      now: new Date("2026-08-18T12:01:10.000Z"),
    });
    assert.equal(isAcceptedWorkerWrite(acceptedStart), true);
    await removeQueuedWorkerWrite(vaultPath, planId, "start-b-stamp-only");
  });
  await joinWorkerWriteDrains(vaultPath, planId);

  const pendingStamp = await readWorkerUpdateReceipt({
    vaultPath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-b",
    generation: claimB.generation,
    idempotencyKey: startAcceptedReceiptKey(
      planId,
      "s1",
      claimB.generation,
      claimB.claim_id,
    ),
    claimId: claimB.claim_id,
  });
  assert.equal(pendingStamp?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(pendingStamp?.status, "pending");
  assert.equal(pendingStamp?.response.slice.status, "in_progress");

  // Pending start_accepted stamp alone (even with slice.status === "in_progress") MUST block complete!
  await assert.rejects(
    updateClaimedSlice({
      vaultPath,
      notePath,
      planId,
      sliceId: "s1",
      workerAgentId: "worker-b",
      token: claimB.token,
      idempotencyKey: "complete-b-while-stamp-pending",
      action: {
        action: "complete",
        evidence: "Should be blocked by pending start_accepted stamp",
      },
      now: new Date("2026-08-18T12:01:15.000Z"),
    }),
    /start must apply before complete/,
  );

  // 2. Now enqueue both start and complete under lock contention, then drain cleanly!
  await withThreadLock(vaultPath, planId, "hold-for-queued-drain", async () => {
    const qStart = await updateClaimedSlice({
      vaultPath,
      notePath,
      planId,
      sliceId: "s1",
      workerAgentId: "worker-b",
      token: claimB.token,
      idempotencyKey: "start-b-queued",
      action: { action: "start" },
      now: new Date("2026-08-18T12:01:20.000Z"),
    });
    assert.equal(isAcceptedWorkerWrite(qStart), true);

    const qComplete = await updateClaimedSlice({
      vaultPath,
      notePath,
      planId,
      sliceId: "s1",
      workerAgentId: "worker-b",
      token: claimB.token,
      idempotencyKey: "complete-b-queued",
      action: {
        action: "complete",
        evidence: "Drained cleanly after queued start promoted receipt",
      },
      now: new Date("2026-08-18T12:01:25.000Z"),
    });
    assert.equal(isAcceptedWorkerWrite(qComplete), true);
  });

  await drainWorkerWrites({ vaultPath, notePath, planId });
  await joinWorkerWriteDrains(vaultPath, planId);

  assert.equal((await listQueuedWorkerWrites(vaultPath, planId)).length, 0);
  const finalPlan = await rehydratePlan(notePath);
  assert.equal(finalPlan.slices[0].status, "done");

  const committedStamp = await readWorkerUpdateReceipt({
    vaultPath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-b",
    generation: claimB.generation,
    idempotencyKey: startAcceptedReceiptKey(
      planId,
      "s1",
      claimB.generation,
      claimB.claim_id,
    ),
    claimId: claimB.claim_id,
  });
  assert.equal(committedStamp?.status, "committed");

  const { events } = await readThreadEvents(journalPathFor(notePath, planId), 0, 200);
  const bClaimIdx = events.findIndex(
    (e) => e.kind === "slice.claimed" && e.actor === "worker-b",
  );
  const bStartIdx = events.findIndex(
    (e, idx) => idx > bClaimIdx && e.kind === "slice.started" && e.actor === "worker-b",
  );
  const bCompleteIdx = events.findIndex(
    (e, idx) => idx > bClaimIdx && e.kind === "slice.completed" && e.actor === "worker-b",
  );
  assert.ok(bClaimIdx >= 0 && bStartIdx > bClaimIdx && bCompleteIdx > bStartIdx);
});

test("Chart 3d: after PlanHistoryAppendError on progress (no slice.progressed journal event), complete in same claim generation succeeds via committed start stamp", async (t) => {
  const vaultPath = await makeTempVault(t, "minni-chart3-history-err-");
  const created = await createPlan(
    {
      goal: "Chart 3 PlanHistoryAppendError recovery",
      slices: [{ id: "s1", title: "Slice 1" }],
      vaultPath,
    },
    { vaultPath, now: () => new Date(THREAD_START) },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;

  await assignSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    actorAgentId: "orchestrator",
    now: new Date("2026-08-18T12:00:00.000Z"),
  });
  const claim = await claimSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a-hist",
    now: new Date("2026-08-18T12:00:05.000Z"),
  });

  // Force PlanHistoryAppendError after note write so slice.progressed never reaches journal.
  const historyPath = historyPathFor(notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  await assert.rejects(
    updateClaimedSlice({
      vaultPath,
      notePath,
      planId,
      sliceId: "s1",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "progress-a-hist-err",
      action: {
        action: "progress",
        evidence: "Progress written before history EISDIR",
      },
      now: new Date("2026-08-18T12:01:00.000Z"),
    }),
    (err) => err instanceof PlanHistoryAppendError,
  );

  const { events: beforeCompleteEvents } = await readThreadEvents(
    journalPathFor(notePath, planId),
    0,
    100,
  );
  assert.equal(
    beforeCompleteEvents.some(
      (e) => e.kind === "slice.started" || e.kind === "slice.progressed",
    ),
    false,
    "no start/progress event was journaled due to PlanHistoryAppendError",
  );
  const progressCommittedStamp = await readWorkerUpdateReceipt({
    vaultPath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    generation: claim.generation,
    idempotencyKey: startAcceptedReceiptKey(
      planId,
      "s1",
      claim.generation,
      claim.claim_id,
    ),
    claimId: claim.claim_id,
  });
  assert.equal(progressCommittedStamp?.kind, START_ACCEPTED_RECEIPT_KIND);
  assert.equal(progressCommittedStamp?.status, "committed");

  // Restore history path and complete in the same claim generation.
  await rm(historyPath, { recursive: true, force: true });
  const completed = await updateClaimedSlice({
    vaultPath,
    notePath,
    planId,
    sliceId: "s1",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "complete-a-after-hist-err",
    action: {
      action: "complete",
      evidence: "Completed via committed start stamp after history repair",
    },
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.equal(isAcceptedWorkerWrite(completed), false);
  assert.equal(completed.slice.status, "done");
});

test("Chart 9: live host_pid > 1 session pointers survive > 7d while PID-less > 7d and dead-PID > 24h pointers are pruned", async (t) => {
  const tmpRoot = await makeTempVault(t, "minni-chart9-session-gc-");
  const vaultPath = path.join(tmpRoot, "claudecode-vault");
  await ensureVault(vaultPath, { agentId: "claude-code" });

  const created = await createPlan(
    {
      goal: "Chart 9 long-lived PID session protection",
      slices: [{ id: "s1", title: "Long running slice", status: "in_progress" }],
      vaultPath,
      activate: false,
    },
    { vaultPath },
  );
  const planId = created.plan.plan_id;
  const notePath = created.write.notePath;

  const nowMs = Date.now();
  const eightDaysAgoIso = new Date(nowMs - 8 * 24 * 60 * 60 * 1000).toISOString();
  const twentyFiveHoursAgoIso = new Date(nowMs - 25 * 60 * 60 * 1000).toISOString();

  const liveKey = sessionStateKey("sess-live-8d");
  const pidlessKey = sessionStateKey("sess-pidless-8d");
  const zeroPidKey = sessionStateKey("sess-zeropid-8d");
  const deadKey = sessionStateKey("sess-dead-25h");

  for (const [key, sessionId, hostPid, stampIso] of [
    [liveKey, "sess-live-8d", process.pid, eightDaysAgoIso],
    [pidlessKey, "sess-pidless-8d", undefined, eightDaysAgoIso],
    [zeroPidKey, "sess-zeropid-8d", 0, eightDaysAgoIso],
    [deadKey, "sess-dead-25h", 999_999, twentyFiveHoursAgoIso],
  ]) {
    await setActivePlan(vaultPath, planId, notePath, {
      sessionKey: key,
      sessionId,
      ...(typeof hostPid === "number" ? { hostPid } : {}),
    });
    const pFile = activeThreadSessionPath(vaultPath, key);
    const raw = JSON.parse(await readFile(pFile, "utf8"));
    raw.set_at = stampIso;
    raw.updated_at = stampIso;
    if (hostPid === undefined) {
      delete raw.host_pid;
    } else {
      raw.host_pid = hostPid;
    }
    await writeFile(pFile, JSON.stringify(raw, null, 2), "utf8");
  }

  // Also write an 8-day-old active-session hint for a live process to verify sweepDeadRelayCursors keeps it.
  const hintOnlyKey = sessionStateKey("sess-hint-live-8d");
  await writeFile(
    path.join(vaultPath, ".runtime", `active-session-claude-code-${process.pid}.json`),
    JSON.stringify({
      agent_id: "claude-code",
      host_pid: process.pid,
      session_key: hintOnlyKey,
      updated_at: eightDaysAgoIso,
    }),
    "utf8",
  );

  // Seed 8-day-old composite relay cursors (empty pending queue so only liveSessionKeys protects them).
  await saveRelayStore(vaultPath, {
    version: 1,
    graph: false,
    cursors: [
      {
        subscriber_id: formatSessionSubscriberId("claude-code", liveKey),
        plan_id: planId,
        last_delivered_seq: 1,
        updated_at: eightDaysAgoIso,
      },
      {
        subscriber_id: formatSessionSubscriberId("claude-code", hintOnlyKey),
        plan_id: planId,
        last_delivered_seq: 1,
        updated_at: eightDaysAgoIso,
      },
      {
        subscriber_id: formatSessionSubscriberId("claude-code", pidlessKey),
        plan_id: planId,
        last_delivered_seq: 1,
        updated_at: eightDaysAgoIso,
      },
      {
        subscriber_id: formatSessionSubscriberId("claude-code", zeroPidKey),
        plan_id: planId,
        last_delivered_seq: 1,
        updated_at: eightDaysAgoIso,
      },
      {
        subscriber_id: formatSessionSubscriberId("claude-code", deadKey),
        plan_id: planId,
        last_delivered_seq: 1,
        updated_at: twentyFiveHoursAgoIso,
      },
    ],
    pending: [],
  });

  // 1. Before sweep: listLiveActiveThreadBindings and liveSessionSubscribersForPlan include ONLY liveKey!
  const liveBindings = await listLiveActiveThreadBindings(vaultPath, undefined, nowMs);
  assert.deepEqual(
    liveBindings.map((b) => b.session_key),
    [liveKey],
  );
  const liveSubs = await liveSessionSubscribersForPlan(vaultPath, planId, "claude-code");
  assert.deepEqual(liveSubs, [formatSessionSubscriberId("claude-code", liveKey)]);

  // 2. Sweep prunes pidless (>7d), zeroPid (>7d), and dead (>24h), while keeping liveKey (>7d) and hintOnlyKey (>7d)!
  const sweep1 = await sweepDeadActiveThreadPointers(vaultPath, nowMs);
  assert.equal(sweep1.prunedPointers, 3);
  assert.equal(sweep1.prunedCursors, 3);

  assert.equal(existsSync(activeThreadSessionPath(vaultPath, liveKey)), true);
  assert.equal(existsSync(activeThreadSessionPath(vaultPath, pidlessKey)), false);
  assert.equal(existsSync(activeThreadSessionPath(vaultPath, zeroPidKey)), false);
  assert.equal(existsSync(activeThreadSessionPath(vaultPath, deadKey)), false);

  const storeAfter = await loadRelayStore(vaultPath);
  assert.deepEqual(
    storeAfter.cursors.map((c) => c.subscriber_id).sort(),
    [
      formatSessionSubscriberId("claude-code", hintOnlyKey),
      formatSessionSubscriberId("claude-code", liveKey),
    ].sort(),
  );

  // 3. Re-running sweep and sweepDeadRelayCursors directly with only the 8d live pointer prunes 0.
  const sweep2 = await sweepDeadActiveThreadPointers(vaultPath, nowMs);
  assert.equal(sweep2.prunedPointers, 0);
  assert.equal(sweep2.prunedCursors, 0);
  const relaySweep = await sweepDeadRelayCursors(vaultPath, nowMs);
  assert.equal(relaySweep.prunedCursors, 0);
});

test("Chart 1: minni_handoff_send echoes canonicalized from_agent and to_agent from daemon.handoff", async (t) => {
  const root = await makeTempVault(t, "minni-chart1-handoff-");
  const home = path.join(root, "home");
  await mkdir(home, { recursive: true });
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
        const req = JSON.parse(line);
        if (req.method === "daemon.handoff") {
          socket.write(
            `${JSON.stringify({
              jsonrpc: "2.0",
              id: req.id,
              result: {
                ok: true,
                delivered: true,
                status: "ok",
                lease_id: "lease-canonical-1",
                from_agent: "codex",
                to_agent: "claude-code",
                expires_at: "2026-11-13T00:00:00.000Z",
              },
            })}\n`,
          );
          continue;
        }
        socket.write(
          `${JSON.stringify({
            jsonrpc: "2.0",
            id: req.id,
            result: { ok: true, results: [] },
          })}\n`,
        );
      }
    });
  });
  await new Promise((resolve) => daemon.listen(socketPath, resolve));
  t.after(() => daemon.close());

  const child = spawn(process.execPath, [SERVER_PATH], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: socketPath,
      MINNI_VAULT_PATH: root,
      MINNI_CLAUDECODE_VAULT_PATH: root,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  t.after(() => child.kill("SIGKILL"));

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
        // ignore non-json lines
      }
    }
  });
  const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
  const awaitResponse = (id, ms = 15_000) =>
    responses.get(id) ??
    new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`timeout for ${id}`)), ms);
      waiters.set(id, (msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
    });

  send({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "chart1-test", version: "0.0.0" },
    },
  });
  await awaitResponse(1);
  send({ jsonrpc: "2.0", method: "notifications/initialized" });

  send({
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: {
      name: "minni_handoff_send",
      arguments: {
        to_agent: "claudecode",
        task: "Verify canonical handoff party echo",
      },
    },
  });
  const reply = await awaitResponse(2);
  assert.equal(reply.error, undefined);
  assert.notEqual(reply.result?.isError, true);
  const receipt = JSON.parse(reply.result.content[0].text);
  assert.equal(receipt.status, "ok");
  assert.equal(receipt.lease_id, "lease-canonical-1");
  assert.equal(receipt.from_agent, "codex");
  assert.equal(receipt.to_agent, "claude-code");

  const audit = await auditTail(root, 10);
  assert.match(audit.text, /minni_handoff_send/);
  assert.match(audit.text, /"agent":\s*"codex"/);
  assert.match(audit.text, /"to_agent":\s*"claude-code"/);
});

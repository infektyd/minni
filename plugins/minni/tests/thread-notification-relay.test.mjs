// G3: notification relay is not a second graph. Cursor is fail-closed.
// Immediate wake is unsupported (not wet). Hooks do not poll minni_thread_events.
// G2 in-session complete is not a spawn. GROK_WORKER_START stays null.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { GROK_WORKER_START } from "../dist/thread-host-dispatch.js";
import {
  HOOKS_POLL_MINNI_THREAD_EVENTS,
  HOST_INJECTION_TABLE,
  RELAY_IS_GRAPH_STATE,
  SQLITE_020_LIVE,
  SPAWNED,
  advanceCursor,
  attemptImmediateWake,
  confirmDelivery,
  cursorFor,
  deliverySucceededForHostHook,
  emptyRelayStore,
  formatStateDelta,
  hostHookInjects,
  ingestJournalEvents,
  ingestJournalIntoVault,
  ingestPlanJournalFromDisk,
  loadRelayStore,
  pendingAttentionForHook,
  readPendingAttention,
  rebuildQueueFromJournal,
  saveRelayStore,
  seedRelaySubscribers,
  storeHoldsGraphState,
  vaultPrincipalSubscriberId,
} from "../dist/thread-notification-relay.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { geminiWire, grokBuildWire } from "../dist/hook-platform.js";
import { discardDeliveryCommits, runDeliveryCommits } from "../dist/hook-delivery.js";
import { appendOrderedEventBatch } from "../dist/thread-events.js";
import { createPlan } from "../dist/plan.js";
import { ensureVault } from "../dist/vault.js";

const DISPATCH_SRC = new URL("../src/thread-host-dispatch.ts", import.meta.url);
const RELAY_SRC = new URL("../src/thread-notification-relay.ts", import.meta.url);
const HANDLERS_SRC = new URL("../src/hook-handlers.ts", import.meta.url);
const EVENTS_SRC = new URL("../src/thread-events.ts", import.meta.url);
const SERVER_SRC = new URL("../src/server.ts", import.meta.url);
const WRITERS_PY = new URL("../../../src/minni/wire/writers.py", import.meta.url);

const PLAN = "plan-g3-relay";
const ORCH = "orchestrator-g3";

function events() {
  return [
    { seq: 1, kind: "slice.assigned", actor: ORCH, at: "2026-08-20T12:00:00.000Z", slice_id: "alpha" },
    { seq: 2, kind: "slice.completed", actor: "worker-a", at: "2026-08-20T12:01:00.000Z", slice_id: "alpha" },
    { seq: 3, kind: "slice.completed", actor: "worker-b", at: "2026-08-20T12:02:00.000Z", slice_id: "beta" },
  ];
}

test("store is not graph state and does not spawn", () => {
  const store = emptyRelayStore();
  assert.equal(RELAY_IS_GRAPH_STATE, false);
  assert.equal(store.graph, false);
  assert.equal(storeHoldsGraphState(store), false);
  assert.equal(SPAWNED, false);
  assert.equal(GROK_WORKER_START, null);
  assert.equal(HOOKS_POLL_MINNI_THREAD_EVENTS, false);
  assert.equal(SQLITE_020_LIVE, false);
  const forged = { ...store, slices: [{ id: "alpha" }] };
  assert.equal(storeHoldsGraphState(forged), true);
});

test("journal seq rebuilds the pending queue after the cursor", () => {
  const cursor = { subscriber_id: ORCH, plan_id: PLAN, last_delivered_seq: 1 };
  const queue = rebuildQueueFromJournal(PLAN, events(), cursor);
  assert.deepEqual(
    queue.map((item) => item.seq),
    [2, 3],
  );
  assert.equal(queue[0].delta.includes("evidence"), false);
  assert.match(queue[0].delta, /worker-a slice\.completed plan=plan-g3-relay slice=alpha seq=2/);
  assert.match(queue[1].delta, /worker-b slice\.completed/);
  assert.equal(formatStateDelta(events()[0], PLAN).includes("Remember to"), false);
});

test("failed wake leaves the cursor behind; success advances monotonically", () => {
  const cursor = { subscriber_id: ORCH, plan_id: PLAN, last_delivered_seq: 1 };
  const failed = advanceCursor(cursor, 3, false);
  assert.equal(failed.last_delivered_seq, 1);
  const skipped = advanceCursor(cursor, 0, true);
  assert.equal(skipped.last_delivered_seq, 1);
  const same = advanceCursor(cursor, 1, true);
  assert.equal(same.last_delivered_seq, 1);
  const ok = advanceCursor(cursor, 3, true);
  assert.equal(ok.last_delivered_seq, 3);
  const store = ingestJournalEvents(emptyRelayStore(), PLAN, events(), [ORCH]);
  const stillBehind = confirmDelivery(store, ORCH, PLAN, 3, false);
  assert.equal(
    stillBehind.cursors.find((row) => row.subscriber_id === ORCH).last_delivered_seq,
    0,
  );
  assert.equal(stillBehind.pending.length, 3);
  const delivered = confirmDelivery(store, ORCH, PLAN, 2, true);
  assert.equal(
    delivered.cursors.find((row) => row.subscriber_id === ORCH).last_delivered_seq,
    2,
  );
  assert.deepEqual(
    delivered.pending.map((item) => item.seq),
    [3],
  );
});

test("host injection table matches the existing wire and does not invent grok start", () => {
  assert.equal(HOST_INJECTION_TABLE.agy.sessionStart, "injects");
  assert.equal(HOST_INJECTION_TABLE.agy.promptSubmit, "injects");
  assert.equal(HOST_INJECTION_TABLE.agy.stop, "rejects");
  assert.equal(HOST_INJECTION_TABLE.agy.wake, "deferred");
  assert.equal(HOST_INJECTION_TABLE.agy.wetImmediate, false);
  assert.match(HOST_INJECTION_TABLE.agy.wire, /injectSteps/);

  assert.equal(HOST_INJECTION_TABLE.grok.sessionStart, "ignored");
  assert.equal(HOST_INJECTION_TABLE.grok.promptSubmit, "ignored");
  assert.equal(HOST_INJECTION_TABLE.grok.stop, "injects");
  assert.equal(HOST_INJECTION_TABLE.grok.wake, "deferred");
  assert.equal(HOST_INJECTION_TABLE.grok.grokWorkerStart, null);
  assert.match(HOST_INJECTION_TABLE.grok.wire, /~\/\.grok\/rules/);

  assert.equal(HOST_INJECTION_TABLE.codex.sessionStart, "injects");
  assert.equal(HOST_INJECTION_TABLE.codex.promptSubmit, "injects");
  assert.equal(HOST_INJECTION_TABLE.codex.stop, "cannot");
  assert.equal(HOST_INJECTION_TABLE.codex.wake, "unsupported");

  assert.equal(HOST_INJECTION_TABLE.cursor.wake, "unsupported");
  assert.equal(HOST_INJECTION_TABLE.cursor.sessionStart, "out");
  assert.equal(HOST_INJECTION_TABLE.cursor.promptSubmit, "out");
  assert.equal(HOST_INJECTION_TABLE.cursor.stop, "out");

  for (const host of ["agy", "grok", "codex", "cursor"]) {
    const wake = attemptImmediateWake(host);
    assert.equal(wake.spawned, false);
    assert.equal(wake.cursorAdvanced, false);
    assert.equal(wake.wetImmediate, false);
    assert.equal(wake.grokWorkerStart, null);
    assert.notEqual(wake.mode, "immediate");
  }
});

test("hooks read pending attention and do not poll minni_thread_events", async (t) => {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-g3-relay-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));

  const store = ingestJournalEvents(emptyRelayStore(), PLAN, events(), [ORCH]);
  await saveRelayStore(vaultPath, store);
  const pending = await pendingAttentionForHook({
    vaultPath,
    subscriberId: ORCH,
    planId: PLAN,
  });
  assert.equal(pending.hooksPollThreadEvents, false);
  assert.equal(pending.spawned, false);
  assert.equal(pending.notifications.length, 3);
  const view = readPendingAttention(store, ORCH, PLAN);
  assert.equal(view.hooksPollThreadEvents, false);

  const [relaySrc, handlersSrc, serverSrc, dispatchSrc, eventsSrc, writers] = await Promise.all([
    readFile(RELAY_SRC, "utf8"),
    readFile(HANDLERS_SRC, "utf8"),
    readFile(SERVER_SRC, "utf8"),
    readFile(DISPATCH_SRC, "utf8"),
    readFile(EVENTS_SRC, "utf8"),
    readFile(WRITERS_PY, "utf8"),
  ]);
  assert.match(relaySrc, /HOOKS_POLL_MINNI_THREAD_EVENTS\s*=\s*false/);
  assert.match(relaySrc, /GROK_WORKER_START/);
  assert.doesNotMatch(relaySrc, /registerTool/);
  assert.doesNotMatch(handlersSrc, /minni_thread_events\s*\(/);
  assert.doesNotMatch(handlersSrc, /from \"\.\/thread-events\.js\"/);
  assert.match(handlersSrc, /pendingAttentionForHook/);
  assert.match(handlersSrc, /deliverySucceededForHostHook/);
  assert.match(handlersSrc, /confirmPendingIfInjects/);
  assert.match(relaySrc, /ingestJournalIntoVault/);
  assert.match(relaySrc, /subscriber_id !== subscriberId/);
  assert.doesNotMatch(
    serverSrc,
    /registerTool\(\s*"dispatch"|registerTool\(\s*"thread_relay"|registerTool\(\s*"notification_relay"/,
  );
  assert.match(eventsSrc, /ingestJournalIntoVault/);
  assert.match(eventsSrc, /ingestRelayAfterJournalAppend/);
  assert.match(eventsSrc, /seedRelaySubscribers/);
  assert.match(eventsSrc, /vaultPrincipalSubscriberId/);
  assert.match(eventsSrc, /extraIds/);
  assert.match(relaySrc, /seedRelaySubscribers/);
  assert.match(relaySrc, /vaultPrincipalSubscriberId/);
  assert.match(relaySrc, /inboxPrincipalForVaultPath/);
  assert.match(relaySrc, /SQLITE_020_LIVE\s*=\s*false/);
  assert.doesNotMatch(eventsSrc, /minni_thread_events/);
  assert.match(dispatchSrc, /GROK_WORKER_START:\s*null\s*=\s*null/);
  assert.match(dispatchSrc, /outcome:\s*"CANNOT"/);
  const readonlyBlock = writers.match(/MINNI_READONLY_TOOLS = \(([\s\S]*?)\)/)[1];
  assert.doesNotMatch(readonlyBlock, /minni_thread_worker_update/);
  assert.match(writers, /MINNI_WORKER_TOOLS = \(\s*"minni_thread_worker_update",?\s*\)/);
  assert.match(writers, /MINNI_WILDCARD_GRANT = "mcp\(minni\/\*\)"/);
});


test("confirm delivery=true only when HOST_INJECTION_TABLE says the host+hook injects", () => {
  assert.equal(hostHookInjects("grok", "sessionStart"), false);
  assert.equal(hostHookInjects("grok", "promptSubmit"), false);
  assert.equal(hostHookInjects("grok", "stop"), true);
  assert.equal(hostHookInjects("agy", "sessionStart"), true);
  assert.equal(hostHookInjects("agy", "promptSubmit"), true);
  assert.equal(hostHookInjects("agy", "stop"), false);
  assert.equal(hostHookInjects("codex", "stop"), false);
  assert.equal(hostHookInjects("cursor", "sessionStart"), false);
  assert.equal(deliverySucceededForHostHook("grok-build", "sessionStart"), false);
  assert.equal(deliverySucceededForHostHook("grok-build", "stop"), true);
  assert.equal(deliverySucceededForHostHook("gemini", "sessionStart"), true);
  assert.equal(deliverySucceededForHostHook("gemini", "stop"), false);
  assert.equal(deliverySucceededForHostHook("kilocode", "sessionStart"), false);
});

test("A confirming seq 5 does not drop B pending 2-5", () => {
  const subA = "subscriber-a";
  const subB = "subscriber-b";
  const seqs = [1, 2, 3, 4, 5].map((seq) => ({
    seq,
    kind: "slice.completed",
    actor: ORCH,
    at: `2026-08-20T12:0${seq}:00.000Z`,
    slice_id: "alpha",
  }));
  let store = emptyRelayStore();
  store.cursors = [
    { subscriber_id: subA, plan_id: PLAN, last_delivered_seq: 0 },
    { subscriber_id: subB, plan_id: PLAN, last_delivered_seq: 1 },
  ];
  store = ingestJournalEvents(store, PLAN, seqs, [subA, subB]);
  assert.deepEqual(
    store.pending.filter((item) => item.subscriber_id === subB).map((item) => item.seq),
    [2, 3, 4, 5],
  );
  const after = confirmDelivery(store, subA, PLAN, 5, true);
  assert.equal(cursorFor(after, subA, PLAN).last_delivered_seq, 5);
  assert.equal(cursorFor(after, subB, PLAN).last_delivered_seq, 1);
  assert.deepEqual(
    after.pending.filter((item) => item.subscriber_id === subB).map((item) => item.seq),
    [2, 3, 4, 5],
  );
  assert.equal(after.pending.some((item) => item.subscriber_id === subA), false);
});

test("production ingest writes cursors.json from journal seq so hooks can read it", async (t) => {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-g3-ingest-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  const journalPath = path.join(vaultPath, "wiki", "artifacts", `${PLAN}.log.md`);
  await mkdir(path.dirname(journalPath), { recursive: true });
  await appendOrderedEventBatch({
    journalPath,
    planId: PLAN,
    rev: 1,
    actor: ORCH,
    at: "2026-08-20T12:00:00.000Z",
    events: [
      { idempotencyKey: "g3-assign", kind: "slice.assigned", sliceId: "alpha" },
      { idempotencyKey: "g3-complete", kind: "slice.completed", sliceId: "alpha" },
    ],
  });
  const pending = await pendingAttentionForHook({
    vaultPath,
    subscriberId: ORCH,
    planId: PLAN,
  });
  assert.ok(pending, "hooks must see pending after production journal ingest");
  assert.equal(pending.hooksPollThreadEvents, false);
  assert.ok(pending.notifications.length >= 2);
  assert.equal(pending.notifications[0].subscriber_id, ORCH);
  const fromDisk = await ingestPlanJournalFromDisk(vaultPath, PLAN, [ORCH]);
  assert.equal(fromDisk.graph, false);
  assert.ok(fromDisk.pending.some((item) => item.subscriber_id === ORCH));
});

test("old writer-only seed misses orchestrator; landed-actor seed notifies orch", () => {
  const WORKER = "worker-g3";
  const landed = [
    { seq: 1, kind: "slice.assigned", actor: ORCH, at: "2026-08-20T12:00:00.000Z", slice_id: "alpha" },
    { seq: 2, kind: "slice.completed", actor: WORKER, at: "2026-08-20T12:01:00.000Z", slice_id: "alpha" },
  ];
  const oldIds = [WORKER]; // old seed: {append actor} ∪ existing cursors (none)
  assert.deepEqual(oldIds, [WORKER]);
  const oldStore = ingestJournalEvents(emptyRelayStore(), PLAN, landed, oldIds);
  assert.equal(
    readPendingAttention(oldStore, ORCH, PLAN).notifications.length,
    0,
    "old seed: worker append, orchestrator reads empty",
  );
  assert.ok(
    readPendingAttention(oldStore, WORKER, PLAN).notifications.length > 0,
    "old seed notifies the writer only",
  );

  const newIds = seedRelaySubscribers({
    actor: WORKER,
    planId: PLAN,
    cursors: [],
    events: landed,
    extraIds: [ORCH],
  });
  assert.ok(newIds.includes(ORCH), "new seed includes orchestrator from landed actors / coordinator");
  assert.ok(newIds.includes(WORKER));
  const newStore = ingestJournalEvents(emptyRelayStore(), PLAN, landed, newIds);
  const orchPending = readPendingAttention(newStore, ORCH, PLAN);
  assert.ok(orchPending.notifications.length > 0, "new seed: worker append, orchestrator has pending");
  assert.ok(orchPending.notifications.some((item) => item.actor === WORKER));
});

test("first worker_update on a cursor-less store notifies the orchestrator", async (t) => {
  const WORKER = "worker-g3";
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-g3-orch-seed-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  const journalPath = path.join(vaultPath, "wiki", "artifacts", `${PLAN}.log.md`);
  await mkdir(path.dirname(journalPath), { recursive: true });

  // In-flight Thread: orchestrator events already on disk, no cursors.json.
  const preexisting = {
    thread_event_batch: [
      {
        seq: 1,
        rev: 1,
        event_id: "g3-inflight-assign",
        idempotency_key: "g3-inflight-assign",
        actor: ORCH,
        kind: "slice.assigned",
        at: "2026-08-20T12:00:00.000Z",
        slice_id: "alpha",
      },
    ],
  };
  await writeFile(
    journalPath,
    `# Minni Plan Journal\n\n## events\n${JSON.stringify(preexisting)}\n`,
    "utf8",
  );

  await appendOrderedEventBatch({
    journalPath,
    planId: PLAN,
    rev: 1,
    actor: WORKER,
    at: "2026-08-20T12:01:00.000Z",
    events: [
      { idempotencyKey: "g3-worker-complete", kind: "slice.completed", sliceId: "alpha" },
    ],
  });

  const pending = await pendingAttentionForHook({
    vaultPath,
    subscriberId: ORCH,
    planId: PLAN,
  });
  assert.ok(pending, "orchestrator must see pending after worker append on a cursor-less store");
  assert.equal(pending.hooksPollThreadEvents, false);
  assert.ok(pending.notifications.some((item) => item.actor === WORKER && item.kind === "slice.completed"));
  assert.ok(pending.notifications.every((item) => item.subscriber_id === ORCH));
});

test("vault principal is fail-closed: no -vault suffix is not a subscriber", () => {
  assert.equal(vaultPrincipalSubscriberId("/tmp/gemini-vault"), "gemini");
  assert.equal(vaultPrincipalSubscriberId("/tmp/grok-build-vault"), "grok-build");
  assert.equal(vaultPrincipalSubscriberId(`/tmp/${ORCH}-vault`), ORCH);
  assert.equal(vaultPrincipalSubscriberId("/tmp/plain-dir"), undefined);
  assert.equal(vaultPrincipalSubscriberId("/tmp/vault"), undefined);
});

test("worker-only journal seeds vault principal; no principal stays empty", async (t) => {
  const WORKER = "worker-g3";
  const workerOnly = [
    {
      idempotencyKey: "g3-worker-only-complete",
      kind: "slice.completed",
      sliceId: "alpha",
    },
  ];

  const honestRoot = await mkdtemp(path.join(tmpdir(), "minni-g3-honest-"));
  const honestVault = path.join(honestRoot, `${ORCH}-vault`);
  t.after(() => rm(honestRoot, { recursive: true, force: true }));
  const honestJournal = path.join(honestVault, "wiki", "artifacts", `${PLAN}.log.md`);
  await mkdir(path.dirname(honestJournal), { recursive: true });
  assert.equal(vaultPrincipalSubscriberId(honestVault), ORCH);

  await appendOrderedEventBatch({
    journalPath: honestJournal,
    planId: PLAN,
    rev: 1,
    actor: WORKER,
    at: "2026-08-20T12:01:00.000Z",
    events: workerOnly,
  });
  const pending = await pendingAttentionForHook({
    vaultPath: honestVault,
    subscriberId: ORCH,
    planId: PLAN,
  });
  assert.ok(pending, "worker-only journal: vault principal must still see pending");
  assert.equal(pending.hooksPollThreadEvents, false);
  assert.equal(pending.spawned, false);
  assert.ok(pending.notifications.some((item) => item.actor === WORKER));
  assert.ok(pending.notifications.every((item) => item.subscriber_id === ORCH));

  const closedVault = await mkdtemp(path.join(tmpdir(), "minni-g3-closed-"));
  t.after(() => rm(closedVault, { recursive: true, force: true }));
  const closedJournal = path.join(closedVault, "wiki", "artifacts", `${PLAN}.log.md`);
  await mkdir(path.dirname(closedJournal), { recursive: true });
  assert.equal(vaultPrincipalSubscriberId(closedVault), undefined, "no -vault suffix: empty is honest");

  await appendOrderedEventBatch({
    journalPath: closedJournal,
    planId: PLAN,
    rev: 1,
    actor: WORKER,
    at: "2026-08-20T12:02:00.000Z",
    events: [{ idempotencyKey: "g3-worker-only-closed", kind: "slice.completed", sliceId: "alpha" }],
  });
  const empty = await pendingAttentionForHook({
    vaultPath: closedVault,
    subscriberId: ORCH,
    planId: PLAN,
  });
  assert.equal(empty, null, "no honest vault principal: orchestrator pending stays empty");
  const workerPending = await pendingAttentionForHook({
    vaultPath: closedVault,
    subscriberId: WORKER,
    planId: PLAN,
  });
  assert.ok(workerPending, "writer is still seeded as the append actor");
});

async function withHookEnv(t, run) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-g3-hook-"));
  const vaultPath = path.join(root, "vault");
  const home = path.join(root, "home");
  const saved = {
    home: process.env.MINNI_HOME,
    socket: process.env.MINNI_SOCKET_PATH,
    afm: process.env.MINNI_AFM_HEALTH_URL,
    bypass: process.env.MINNI_BYPASS_AUDIT_LIMIT,
  };
  process.env.MINNI_HOME = home;
  process.env.MINNI_SOCKET_PATH = path.join(home, "missing.sock");
  process.env.MINNI_AFM_HEALTH_URL = "http://127.0.0.1:1/health";
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  await mkdir(home, { recursive: true });
  discardDeliveryCommits();
  t.after(async () => {
    discardDeliveryCommits();
    for (const [key, value] of [
      ["MINNI_HOME", saved.home],
      ["MINNI_SOCKET_PATH", saved.socket],
      ["MINNI_AFM_HEALTH_URL", saved.afm],
      ["MINNI_BYPASS_AUDIT_LIMIT", saved.bypass],
    ]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await rm(root, { recursive: true, force: true });
  });
  await ensureVault(vaultPath);
  return run({ vaultPath });
}

function hookConfig(vaultPath, agentId, wire) {
  return {
    agentId,
    vaultPath,
    defaultWorkspaceId: "workspace-g3",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
    wire,
    sessionStartHookTimeoutMs: 8_000,
    promptHookTimeoutMs: 8_000,
  };
}

test("grok SessionStart/UPS confirm does not advance cursor; agy Stop does not; injecting host+hook may", { timeout: 60_000 }, async (t) => {
  await withHookEnv(t, async ({ vaultPath }) => {
    const { plan } = await createPlan(
      {
        goal: "g3 confirm-gate plan",
        slices: [{ id: "s1", title: "one" }],
        vaultPath,
      },
      { vaultPath },
    );
    const planId = plan.plan_id;
    const seqEvents = [
      { seq: 1, kind: "slice.assigned", actor: ORCH, at: "2026-08-20T12:00:00.000Z", slice_id: "s1" },
      { seq: 2, kind: "slice.completed", actor: "worker-a", at: "2026-08-20T12:01:00.000Z", slice_id: "s1" },
      { seq: 3, kind: "slice.completed", actor: "worker-b", at: "2026-08-20T12:02:00.000Z", slice_id: "s1" },
    ];

    async function seed(subscriberId) {
      await ingestJournalIntoVault(vaultPath, planId, seqEvents, [subscriberId]);
    }

    await seed("grok-build");
    const grok = createHookHandlers(hookConfig(vaultPath, "grok-build", grokBuildWire));
    await grok.handleSessionStart({ session_id: "g3-grok-ss" });
    await runDeliveryCommits();
    assert.equal(
      cursorFor(await loadRelayStore(vaultPath), "grok-build", planId).last_delivered_seq,
      0,
      "grok SessionStart is ignored and must not confirm true",
    );

    await seed("grok-build");
    discardDeliveryCommits();
    await grok.handleUserPromptSubmit({
      prompt: "continue the g3 confirm-gate work with the active thread",
      session_id: "g3-grok-ups",
    });
    await runDeliveryCommits();
    assert.equal(
      cursorFor(await loadRelayStore(vaultPath), "grok-build", planId).last_delivered_seq,
      0,
      "grok UPS stdout is ignored and must not confirm true",
    );

    await seed("gemini");
    discardDeliveryCommits();
    const agy = createHookHandlers(hookConfig(vaultPath, "gemini", geminiWire));
    await agy.handleStop({ session_id: "g3-agy-stop" });
    await runDeliveryCommits();
    assert.equal(
      cursorFor(await loadRelayStore(vaultPath), "gemini", planId).last_delivered_seq,
      0,
      "agy Stop rejects injectSteps and must not confirm true",
    );

    await seed("gemini");
    discardDeliveryCommits();
    await agy.handleSessionStart({ session_id: "g3-agy-ss" });
    await runDeliveryCommits();
    assert.equal(
      cursorFor(await loadRelayStore(vaultPath), "gemini", planId).last_delivered_seq,
      3,
      "agy SessionStart injects and may confirm true",
    );

    await seed("grok-build");
    discardDeliveryCommits();
    await grok.handleStop({ session_id: "g3-grok-stop", reason: "end_turn" });
    await runDeliveryCommits();
    assert.equal(
      cursorFor(await loadRelayStore(vaultPath), "grok-build", planId).last_delivered_seq,
      3,
      "grok Stop injects and may confirm true",
    );
  });
});

// G3: notification relay is not a second graph. Cursor is fail-closed.
// Immediate wake is unsupported (not wet). Hooks do not poll minni_thread_events.
// G2 in-session complete is not a spawn. GROK_WORKER_START stays null.
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { GROK_WORKER_START } from "../dist/thread-host-dispatch.js";
import {
  HOOKS_POLL_MINNI_THREAD_EVENTS,
  HOST_INJECTION_TABLE,
  RELAY_IS_GRAPH_STATE,
  SPAWNED,
  advanceCursor,
  attemptImmediateWake,
  confirmDelivery,
  emptyRelayStore,
  formatStateDelta,
  ingestJournalEvents,
  pendingAttentionForHook,
  readPendingAttention,
  rebuildQueueFromJournal,
  saveRelayStore,
  storeHoldsGraphState,
} from "../dist/thread-notification-relay.js";

const DISPATCH_SRC = new URL("../src/thread-host-dispatch.ts", import.meta.url);
const RELAY_SRC = new URL("../src/thread-notification-relay.ts", import.meta.url);
const HANDLERS_SRC = new URL("../src/hook-handlers.ts", import.meta.url);
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

  const [relaySrc, handlersSrc, serverSrc, dispatchSrc, writers] = await Promise.all([
    readFile(RELAY_SRC, "utf8"),
    readFile(HANDLERS_SRC, "utf8"),
    readFile(SERVER_SRC, "utf8"),
    readFile(DISPATCH_SRC, "utf8"),
    readFile(WRITERS_PY, "utf8"),
  ]);
  assert.match(relaySrc, /HOOKS_POLL_MINNI_THREAD_EVENTS\s*=\s*false/);
  assert.match(relaySrc, /GROK_WORKER_START/);
  assert.doesNotMatch(relaySrc, /registerTool/);
  assert.doesNotMatch(handlersSrc, /minni_thread_events\s*\(/);
  assert.doesNotMatch(handlersSrc, /from \"\.\/thread-events\.js\"/);
  assert.match(handlersSrc, /pendingAttentionForHook/);
  assert.doesNotMatch(
    serverSrc,
    /registerTool\(\s*"dispatch"|registerTool\(\s*"thread_relay"|registerTool\(\s*"notification_relay"/,
  );
  assert.match(dispatchSrc, /GROK_WORKER_START:\s*null\s*=\s*null/);
  assert.match(dispatchSrc, /outcome:\s*"CANNOT"/);
  const readonlyBlock = writers.match(/MINNI_READONLY_TOOLS = \(([\s\S]*?)\)/)[1];
  assert.doesNotMatch(readonlyBlock, /minni_thread_worker_update/);
  assert.match(writers, /MINNI_WORKER_TOOLS = \(\s*"minni_thread_worker_update",?\s*\)/);
  assert.match(writers, /MINNI_WILDCARD_GRANT = "mcp\(minni\/\*\)"/);
});

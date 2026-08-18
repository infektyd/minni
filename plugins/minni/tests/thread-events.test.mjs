import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

import {
  appendJournal,
  createPlan,
  journalPathFor,
  persistPlan,
  rehydratePlan,
} from "../dist/plan.js";
import {
  appendOrderedEventBatch,
  appendOrderedThreadEvent,
  deriveClientEventKey,
  deriveSystemEventKey,
  readThreadEvents,
  reconcileThreadJournal,
} from "../dist/thread-events.js";
import { withThreadLock } from "../dist/thread-lock.js";

const execFileAsync = promisify(execFile);
const THREAD_START = new Date("2026-08-18T12:00:00.000Z");

async function createThread(t) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-thread-events-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  const created = await createPlan(
    {
      goal: "Ordered thread events",
      slices: [{ id: "a", title: "Slice A" }],
      vaultPath,
    },
    {
      vaultPath,
      now: () => THREAD_START,
    },
  );
  return {
    vaultPath,
    notePath: created.write.notePath,
    planId: created.plan.plan_id,
    journalPath: journalPathFor(created.write.notePath, created.plan.plan_id),
    rev: created.plan.rev,
  };
}

async function appendEventInChildUnderThreadLock(fixture, idempotencyKey) {
  const worker = new URL("./fixtures/thread-event-worker.mjs", import.meta.url);
  await execFileAsync(process.execPath, [
    fileURLToPath(worker),
    fixture.vaultPath,
    fixture.planId,
    fixture.journalPath,
    idempotencyKey,
    String(fixture.rev),
  ]);
}

async function seedOrderedEvents(journalPath, events) {
  for (const event of events) {
    await appendJournal(journalPath, event);
  }
}

async function seedNoteAtRev(fixture, rev) {
  let plan = await rehydratePlan(fixture.notePath);
  while (plan.rev < rev) {
    plan = {
      ...plan,
      next_action: `bump-${plan.rev + 1}`,
    };
    await persistPlan(plan, {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
    });
    plan = await rehydratePlan(fixture.notePath);
  }
  assert.equal(plan.rev, rev);
  return plan;
}

function reconcileInput(fixture, plan, actor = "test-orchestrator") {
  return {
    journalPath: fixture.journalPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    rev: plan.rev,
    actor,
    readySummary: { slices: [{ id: "a", title: "Slice A" }] },
  };
}

test("ordered events allocate unique monotonic seq under concurrent callers", async (t) => {
  const fixture = await createThread(t);
  await Promise.all(
    [...Array(12)].map((_, index) =>
      appendEventInChildUnderThreadLock(fixture, `event-${index}`)
    ),
  );
  const { events } = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.deepEqual(
    events.map((event) => event.seq),
    [...Array(12)].map((_, index) => index + 1),
  );
  assert.equal(new Set(events.map((event) => event.event_id)).size, 12);
});

test("cursor read excludes seq at or below since_seq", async (t) => {
  const fixture = await createThread(t);
  const { journalPath, planId } = fixture;
  const base = {
    rev: 3,
    event_id: "ignored",
    idempotency_key: "ignored",
    actor: "test",
    at: THREAD_START.toISOString(),
  };
  await seedOrderedEvents(journalPath, [
    { ...base, seq: 1, kind: "test.one", idempotency_key: "one" },
    { ...base, seq: 2, kind: "test.two", idempotency_key: "two" },
    { ...base, seq: 3, kind: "test.three", idempotency_key: "three" },
    { ...base, seq: 4, kind: "test.four", idempotency_key: "four" },
  ]);

  const result = await readThreadEvents(journalPath, 2, 20);
  assert.deepEqual(result.events.map((event) => event.seq), [3, 4]);
  assert.equal(result.next_seq, 4);
});

test("note ahead of journal appends state.recovered", async (t) => {
  const fixture = await createThread(t);
  const plan = await seedNoteAtRev(fixture, 5);
  await seedOrderedEvents(fixture.journalPath, [
    {
      seq: 1,
      rev: 4,
      event_id: "evt-1",
      idempotency_key: "seed-1",
      actor: "test",
      kind: "slice.assigned",
      at: THREAD_START.toISOString(),
    },
  ]);

  const input = reconcileInput(fixture, plan);
  await withThreadLock(
    fixture.vaultPath,
    fixture.planId,
    "reconcile-note-ahead",
    async () => {
      assert.equal(await reconcileThreadJournal(input), "recovered");
    },
  );

  const result = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.equal(result.events.at(-1).kind, "state.recovered");
  assert.equal(result.events.at(-1).rev, 5);
});

test("journal ahead of note blocks mutation", async (t) => {
  const fixture = await createThread(t);
  const plan = await seedNoteAtRev(fixture, 4);
  await seedOrderedEvents(fixture.journalPath, [
    {
      seq: 1,
      rev: 5,
      event_id: "evt-ahead",
      idempotency_key: "seed-ahead",
      actor: "test",
      kind: "slice.completed",
      at: THREAD_START.toISOString(),
    },
  ]);

  const input = reconcileInput(fixture, plan);
  await withThreadLock(
    fixture.vaultPath,
    fixture.planId,
    "reconcile-journal-ahead",
    async () => {
      await assert.rejects(reconcileThreadJournal(input), /thread_inconsistent/);
    },
  );
});

test("duplicate idempotency keys return the existing ordered event", async (t) => {
  const fixture = await createThread(t);
  const input = {
    journalPath: fixture.journalPath,
    planId: fixture.planId,
    rev: fixture.rev,
    idempotencyKey: "dup-key",
    actor: "worker-a",
    kind: "slice.claimed",
    sliceId: "a",
    at: THREAD_START.toISOString(),
  };

  let first;
  let second;
  await withThreadLock(fixture.vaultPath, fixture.planId, "dup-1", async () => {
    first = await appendOrderedThreadEvent(input);
    second = await appendOrderedThreadEvent(input);
  });

  assert.deepEqual(second, first);
  const { events } = await readThreadEvents(fixture.journalPath, 0, 10);
  assert.equal(events.length, 1);
});

test("legacy journal events without seq are ignored for ordering", async (t) => {
  const fixture = await createThread(t);
  await appendJournal(fixture.journalPath, {
    kind: "rehydrated",
    at: THREAD_START.toISOString(),
  });
  await withThreadLock(fixture.vaultPath, fixture.planId, "legacy-seq", async () => {
    await appendOrderedThreadEvent({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      idempotencyKey: "first-ordered",
      actor: "worker-a",
      kind: "slice.assigned",
      at: THREAD_START.toISOString(),
    });
  });

  const { events } = await readThreadEvents(fixture.journalPath, 0, 10);
  assert.equal(events.length, 1);
  assert.equal(events[0].seq, 1);
});

test("journal with no ordered events reconciles without assuming alignment", async (t) => {
  const fixture = await createThread(t);
  const plan = await seedNoteAtRev(fixture, 3);
  await appendJournal(fixture.journalPath, {
    kind: "rehydrated",
    at: THREAD_START.toISOString(),
  });

  const input = reconcileInput(fixture, plan);
  await withThreadLock(fixture.vaultPath, fixture.planId, "legacy-only", async () => {
    assert.equal(await reconcileThreadJournal({
      ...input,
      readySummary: { slices: [{ id: "a", title: "Slice A" }] },
    }), "ok");
  });
});

test("conflicting duplicate idempotency key fails typed", async (t) => {
  const fixture = await createThread(t);
  const input = {
    journalPath: fixture.journalPath,
    planId: fixture.planId,
    rev: fixture.rev,
    idempotencyKey: "conflict-key",
    actor: "worker-a",
    kind: "slice.started",
    at: THREAD_START.toISOString(),
  };

  await withThreadLock(fixture.vaultPath, fixture.planId, "conflict-1", async () => {
    await appendOrderedThreadEvent(input);
    await assert.rejects(
      appendOrderedThreadEvent({
        ...input,
        kind: "slice.completed",
      }),
      /thread_event_idempotency_conflict/,
    );
  });
});

test("truncated tail is ignored and later appends stay monotonic", async (t) => {
  const fixture = await createThread(t);
  const header = `# Minni Plan Journal\n\n## events\n`;
  const completeBatch = {
    thread_event_batch: [{
      seq: 1,
      rev: fixture.rev,
      event_id: "evt-1",
      idempotency_key: "batch-one",
      actor: "test",
      kind: "test.one",
      at: THREAD_START.toISOString(),
    }],
  };
  await writeFile(
    fixture.journalPath,
    `${header}${JSON.stringify(completeBatch)}\n{"thread_event_batch":[{"seq":2`,
    "utf8",
  );

  await withThreadLock(fixture.vaultPath, fixture.planId, "tail-repair", async () => {
    await appendOrderedEventBatch({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      actor: "test",
      events: [
        { idempotencyKey: "batch-two", kind: "test.two" },
        { idempotencyKey: "batch-two:ready", kind: "ready.changed", payload: { slices: [] } },
      ],
    });
    await appendOrderedEventBatch({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      actor: "test",
      events: [{ idempotencyKey: "batch-three", kind: "test.three" }],
    });
  });

  const { events } = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.deepEqual(events.map((event) => event.seq), [1, 2, 3, 4]);
  assert.deepEqual(
    events.map((event) => event.idempotency_key),
    ["batch-one", "batch-two", "batch-two:ready", "batch-three"],
  );
});

test("state.recovered carries the safe ready-set summary", async (t) => {
  const fixture = await createThread(t);
  const plan = await seedNoteAtRev(fixture, 5);
  await seedOrderedEvents(fixture.journalPath, [
    {
      seq: 1,
      rev: 4,
      event_id: "evt-1",
      idempotency_key: "seed-1",
      actor: "test",
      kind: "slice.assigned",
      at: THREAD_START.toISOString(),
    },
  ]);

  const input = {
    ...reconcileInput(fixture, plan),
    readySummary: { slices: [{ id: "a", title: "Slice A" }] },
  };
  await withThreadLock(fixture.vaultPath, fixture.planId, "recovery-summary", async () => {
    await reconcileThreadJournal(input);
  });
  const { events } = await readThreadEvents(fixture.journalPath, 0, 100);
  const recovered = events.find((event) => event.kind === "state.recovered");
  assert.deepEqual(recovered?.payload, {
    ready: { slices: [{ id: "a", title: "Slice A" }] },
  });
});

test("reconcile recovers by kind and rev when a conflicting recovery-shaped key exists", async (t) => {
  const fixture = await createThread(t);
  const plan = await seedNoteAtRev(fixture, 5);
  await seedOrderedEvents(fixture.journalPath, [
    {
      seq: 1,
      rev: 4,
      event_id: "evt-seed",
      idempotency_key: "state.recovered:5",
      actor: "attacker",
      kind: "slice.claimed",
      at: THREAD_START.toISOString(),
    },
  ]);

  const input = reconcileInput(fixture, plan);
  await withThreadLock(fixture.vaultPath, fixture.planId, "reconcile-conflict", async () => {
    assert.equal(await reconcileThreadJournal(input), "recovered");
  });

  const { events } = await readThreadEvents(fixture.journalPath, 0, 100);
  const recovered = events.filter(
    (event) => event.kind === "state.recovered" && event.rev === 5,
  );
  assert.equal(recovered.length, 1);
  assert.notEqual(recovered[0].idempotency_key, "state.recovered:5");
  assert.match(recovered[0].idempotency_key, /^system:state\.recovered:5/);
  const planted = events.find((event) => event.idempotency_key === "state.recovered:5");
  assert.equal(planted?.kind, "slice.claimed");
});

test("client claim idempotency keys are namespaced and cannot squat system recovery keys", async (t) => {
  const fixture = await createThread(t);
  const clientKey = "state.recovered:5";
  await withThreadLock(fixture.vaultPath, fixture.planId, "client-key-claim", async () => {
    await appendOrderedThreadEvent({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      idempotencyKey: deriveClientEventKey("claim", {
        plan_id: fixture.planId,
        slice_id: "a",
        worker_agent_id: "worker-a",
        idempotency_key: clientKey,
      }),
      actor: "worker-a",
      kind: "slice.claimed",
      sliceId: "a",
      at: THREAD_START.toISOString(),
    });
  });
  const { events } = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.equal(
    events.some((event) => event.idempotency_key === clientKey),
    false,
  );
  assert.ok(
    events.some(
      (event) =>
        event.kind === "slice.claimed" &&
        event.idempotency_key.startsWith("client:claim:"),
    ),
  );
});

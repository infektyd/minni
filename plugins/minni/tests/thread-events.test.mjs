import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
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
  orderedSnapshotMatchesJournal,
  readOrderedThreadEvents,
  parseOrderedThreadEvents,
  readThreadEvents,
  reconcileThreadJournal,
  ThreadCursorGapError,
  ThreadJournalReadError,
} from "../dist/thread-events.js";
import { withThreadLock } from "../dist/thread-lock.js";
import { appendFileWithFsync as realAppendFileWithFsync } from "../dist/vault.js";

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

// --- final-fix-5 -------------------------------------------------------
//
// appendJournalLine's catch-all used to treat ANY append/fsync failure as
// "journal missing" and overwrite it via writeFileAtomic — even when the
// journal already existed and the line had already landed on disk before
// fsync threw. That is real data loss, not recovery. The catch must be
// ENOENT / missing-file only.

test("final-fix-5: a post-write fsync throw does not get treated as a missing journal and overwrite existing events", async (t) => {
  const fixture = await createThread(t);
  await withThreadLock(fixture.vaultPath, fixture.planId, "seed", async () => {
    await appendOrderedEventBatch({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      actor: "test",
      events: [{ idempotencyKey: "seed-one", kind: "test.one" }],
    });
  });
  const before = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.equal(before.events.length, 1);
  assert.equal(before.events[0].idempotency_key, "seed-one");

  const landedThenThrows = async (filePath, content) => {
    // The write genuinely lands (like appendFileWithFsync's write() before
    // its sync() call throws) before the rejection propagates.
    await realAppendFileWithFsync(filePath, content);
    throw new Error("simulated fsync failure after write landed");
  };

  await withThreadLock(
    fixture.vaultPath,
    fixture.planId,
    "landed-then-throw",
    async () => {
      await assert.rejects(
        appendOrderedEventBatch(
          {
            journalPath: fixture.journalPath,
            planId: fixture.planId,
            rev: fixture.rev,
            actor: "test",
            events: [{ idempotencyKey: "landed-two", kind: "test.two" }],
          },
          { appendFileWithFsync: landedThenThrows },
        ),
        /simulated fsync failure/,
      );
    },
  );

  const after = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.ok(
    after.events.some((event) => event.idempotency_key === "seed-one"),
    "the pre-existing event must survive a post-write fsync throw, not be clobbered by writeFileAtomic",
  );
  assert.ok(
    after.events.some((event) => event.idempotency_key === "landed-two"),
    "the line that landed on disk before the throw must remain readable",
  );
});

// Cassandra PR #371 G1: the ordered Thread journal and the legacy plan
// journal are the same file. appendJournalLine already refused catch-all
// rewrite; appendJournal used to still do it — a failed fsync append of
// a legacy line wiped the now-canonical ordered events.
test("appendJournal fsync-fail does not wipe the now-canonical ordered event journal", async (t) => {
  const fixture = await createThread(t);
  await withThreadLock(fixture.vaultPath, fixture.planId, "seed", async () => {
    await appendOrderedEventBatch({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      actor: "test",
      events: [
        { idempotencyKey: "seed-one", kind: "test.one" },
        { idempotencyKey: "seed-two", kind: "test.two" },
      ],
    });
  });
  const before = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.equal(before.events.length, 2);

  const landedThenThrows = async (filePath, content) => {
    await realAppendFileWithFsync(filePath, content);
    throw new Error("simulated fsync failure after write landed");
  };

  await assert.rejects(
    () =>
      appendJournal(
        fixture.journalPath,
        { kind: "status_changed", at: "2026-01-01T00:00:00.000Z" },
        { appendFileWithFsync: landedThenThrows },
      ),
    /simulated fsync failure/,
  );

  const after = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.equal(
    after.events.length,
    2,
    "ordered events must survive a failed legacy appendJournal fsync, not be clobbered",
  );
  assert.ok(after.events.some((event) => event.idempotency_key === "seed-one"));
  assert.ok(after.events.some((event) => event.idempotency_key === "seed-two"));
});

// Cassandra PR #371 round 2: journal load used to catch-all return [] on any
// readFile failure. ENOENT is empty; EISDIR/EACCES must fail closed so
// minni_thread_events cannot report an empty cursor and prepareThreadMutation
// cannot mint seq=1 onto a journal the next successful read can see.

test("readOrderedThreadEvents returns [] only when the journal is missing", async (t) => {
  const fixture = await createThread(t);
  const missing = path.join(
    path.dirname(fixture.journalPath),
    "no-such-journal.log.md",
  );
  assert.deepEqual(await readOrderedThreadEvents(missing), []);
});

test("readOrderedThreadEvents does not treat an EISDIR journal as empty", async (t) => {
  const fixture = await createThread(t);
  await rm(fixture.journalPath, { force: true });
  await mkdir(fixture.journalPath);
  await assert.rejects(
    () => readOrderedThreadEvents(fixture.journalPath),
    (error) => {
      assert.ok(error instanceof ThreadJournalReadError);
      assert.equal(error.code, "THREAD_JOURNAL_UNREADABLE");
      assert.match(error.message, /EISDIR/);
      assert.equal(error.message.includes(fixture.journalPath), false);
      assert.equal(error.journalPath, fixture.journalPath);
      return true;
    },
  );
});

test("readThreadEvents fails closed on an unreadable journal instead of an empty cursor", async (t) => {
  const fixture = await createThread(t);
  await withThreadLock(fixture.vaultPath, fixture.planId, "seed", async () => {
    await appendOrderedEventBatch({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      actor: "test",
      events: [{ idempotencyKey: "seed-one", kind: "test.one" }],
    });
  });
  await rm(fixture.journalPath, { force: true });
  await mkdir(fixture.journalPath);
  await assert.rejects(
    () => readThreadEvents(fixture.journalPath, 0, 100),
    (error) => error instanceof ThreadJournalReadError,
  );
});

test("snapshot resync does not treat an unreadable journal as empty", async (t) => {
  const fixture = await createThread(t);
  const snapshot = [
    {
      seq: 1,
      rev: fixture.rev,
      event_id: "evt-1",
      idempotency_key: "seed-one",
      actor: "test",
      kind: "test.one",
      at: THREAD_START.toISOString(),
    },
  ];
  await rm(fixture.journalPath, { force: true });
  await mkdir(fixture.journalPath);
  await assert.rejects(
    () => orderedSnapshotMatchesJournal(snapshot, fixture.journalPath),
    (error) => error instanceof ThreadJournalReadError,
  );
});

test("failed append does not wipe the in-memory snapshot when resync cannot read the journal", async (t) => {
  const fixture = await createThread(t);
  const snapshot = [];
  await withThreadLock(fixture.vaultPath, fixture.planId, "seed", async () => {
    await appendOrderedEventBatch({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      actor: "test",
      orderedSnapshot: snapshot,
      events: [{ idempotencyKey: "seed-one", kind: "test.one" }],
    });
  });
  assert.equal(snapshot.length, 1);
  await rm(fixture.journalPath, { force: true });
  await mkdir(fixture.journalPath);
  await assert.rejects(() =>
    appendOrderedEventBatch({
      journalPath: fixture.journalPath,
      planId: fixture.planId,
      rev: fixture.rev,
      actor: "test",
      orderedSnapshot: snapshot,
      events: [{ idempotencyKey: "seed-two", kind: "test.two" }],
    }),
  );
  assert.equal(
    snapshot.length,
    1,
    "resync must not replace a live snapshot with [] when the journal is unreadable",
  );
  assert.equal(snapshot[0].idempotency_key, "seed-one");
});

// Ordered-journal bound protocol (stacked on #373): a since_seq poller must
// see journal_truncated with last_dropped_seq + first_kept_seq after a drop.
// Silent holes are a fail. Seq is never renumbered.

test("parseOrderedThreadEvents keeps journal_truncated as a first-class event", () => {
  const at = THREAD_START.toISOString();
  const parsed = parseOrderedThreadEvents(
    JSON.stringify({
      seq: 3,
      rev: 1,
      event_id: "trunc-3",
      idempotency_key: "system:journal_truncated:3:4",
      actor: "minni",
      kind: "journal_truncated",
      at,
      payload: { last_dropped_seq: 3, first_kept_seq: 4 },
    }) + "\n",
  );
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0].kind, "journal_truncated");
  assert.deepEqual(parsed[0].payload, {
    last_dropped_seq: 3,
    first_kept_seq: 4,
  });
});

test("since_seq poller sees journal_truncated after a simulated prefix drop", async (t) => {
  const fixture = await createThread(t);
  const at = THREAD_START.toISOString();
  const base = {
    rev: fixture.rev,
    actor: "test",
    at,
  };
  await seedOrderedEvents(fixture.journalPath, [
    { ...base, seq: 1, event_id: "e1", idempotency_key: "one", kind: "test.one" },
    { ...base, seq: 2, event_id: "e2", idempotency_key: "two", kind: "test.two" },
    { ...base, seq: 3, event_id: "e3", idempotency_key: "three", kind: "test.three" },
    { ...base, seq: 4, event_id: "e4", idempotency_key: "four", kind: "test.four" },
    { ...base, seq: 5, event_id: "e5", idempotency_key: "five", kind: "test.five" },
  ]);

  // Simulate an honest prefix drop: keep seq 4–5 at their original numbers,
  // and leave a durable journal_truncated occupying last_dropped_seq.
  const header = `# Minni Plan Journal\n\n## events\n`;
  const truncation = {
    seq: 3,
    rev: fixture.rev,
    event_id: "trunc-3",
    idempotency_key: "system:journal_truncated:3:4",
    actor: "minni",
    kind: "journal_truncated",
    at,
    payload: { last_dropped_seq: 3, first_kept_seq: 4 },
  };
  const kept = [
    { ...base, seq: 4, event_id: "e4", idempotency_key: "four", kind: "test.four" },
    { ...base, seq: 5, event_id: "e5", idempotency_key: "five", kind: "test.five" },
  ];
  await writeFile(
    fixture.journalPath,
    header
      + `${JSON.stringify(truncation)}\n`
      + kept.map((event) => `${JSON.stringify(event)}\n`).join(""),
    "utf8",
  );

  const page = await readThreadEvents(fixture.journalPath, 1, 20);
  assert.equal(page.events[0]?.kind, "journal_truncated");
  assert.deepEqual(page.events[0]?.payload, {
    last_dropped_seq: 3,
    first_kept_seq: 4,
  });
  assert.deepEqual(
    page.events.slice(1).map((event) => event.seq),
    [4, 5],
    "kept events must retain their original seq numbers",
  );
  assert.equal(page.next_seq, 5);

  // Poller that already consumed through the truncation seq does not re-see it.
  const after = await readThreadEvents(fixture.journalPath, 3, 20);
  assert.equal(
    after.events.some((event) => event.kind === "journal_truncated"),
    false,
  );
  assert.deepEqual(after.events.map((event) => event.seq), [4, 5]);
});

test("cursor_gap kind is accepted as the same truncation protocol", async (t) => {
  const fixture = await createThread(t);
  const at = THREAD_START.toISOString();
  const header = `# Minni Plan Journal\n\n## events\n`;
  const gap = {
    seq: 2,
    rev: fixture.rev,
    event_id: "gap-2",
    idempotency_key: "system:cursor_gap:2:3",
    actor: "minni",
    kind: "cursor_gap",
    at,
    payload: { last_dropped_seq: 2, first_kept_seq: 3 },
  };
  const kept = {
    seq: 3,
    rev: fixture.rev,
    event_id: "e3",
    idempotency_key: "three",
    actor: "test",
    kind: "test.three",
    at,
  };
  await writeFile(
    fixture.journalPath,
    `${header}${JSON.stringify(gap)}\n${JSON.stringify(kept)}\n`,
    "utf8",
  );

  const page = await readThreadEvents(fixture.journalPath, 0, 20);
  assert.equal(page.events[0]?.kind, "cursor_gap");
  assert.deepEqual(page.events[0]?.payload, {
    last_dropped_seq: 2,
    first_kept_seq: 3,
  });
});

test("unmarked seq hole fails closed instead of a silent cursor jump", async (t) => {
  const fixture = await createThread(t);
  const at = THREAD_START.toISOString();
  const base = { rev: fixture.rev, actor: "test", at };
  // Dropped 1–3 with no journal_truncated / cursor_gap marker.
  await seedOrderedEvents(fixture.journalPath, [
    { ...base, seq: 4, event_id: "e4", idempotency_key: "four", kind: "test.four" },
    { ...base, seq: 5, event_id: "e5", idempotency_key: "five", kind: "test.five" },
  ]);

  await assert.rejects(
    () => readThreadEvents(fixture.journalPath, 1, 20),
    (error) => {
      assert.ok(error instanceof ThreadCursorGapError);
      assert.equal(error.code, "THREAD_CURSOR_GAP");
      assert.match(error.message, /cursor_gap|journal_truncated|unmarked/i);
      return true;
    },
  );
});

test("since_seq inside a marked hole still surfaces journal_truncated", async (t) => {
  const fixture = await createThread(t);
  const at = THREAD_START.toISOString();
  const header = `# Minni Plan Journal\n\n## events\n`;
  // Non-adjacent keep: dropped 1–5, kept 10+. Marker seq = last_dropped.
  // A poller parked at since_seq=7 is inside the hole; naive seq>7 filtering
  // would skip the marker at seq=5 and silently jump to 10.
  const truncation = {
    seq: 5,
    rev: fixture.rev,
    event_id: "trunc-5",
    idempotency_key: "system:journal_truncated:5:10",
    actor: "minni",
    kind: "journal_truncated",
    at,
    payload: { last_dropped_seq: 5, first_kept_seq: 10 },
  };
  const kept = {
    seq: 10,
    rev: fixture.rev,
    event_id: "e10",
    idempotency_key: "ten",
    actor: "test",
    kind: "test.ten",
    at,
  };
  await writeFile(
    fixture.journalPath,
    `${header}${JSON.stringify(truncation)}\n${JSON.stringify(kept)}\n`,
    "utf8",
  );

  const page = await readThreadEvents(fixture.journalPath, 7, 20);
  assert.equal(page.events[0]?.kind, "journal_truncated");
  assert.deepEqual(page.events[0]?.payload, {
    last_dropped_seq: 5,
    first_kept_seq: 10,
  });
  assert.equal(page.events[1]?.seq, 10);
});

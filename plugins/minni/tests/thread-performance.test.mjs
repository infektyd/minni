import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import {
  mkdir,
  mkdtemp,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { claimFs } from "../dist/claim-fs.js";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { stableStringify } from "../dist/agent_envelope.js";
import {
  createPlan,
  journalPathFor,
  rehydratePlan,
} from "../dist/plan.js";
import {
  readWorkerUpdateReceipt,
  writePendingWorkerUpdateReceipt,
} from "../dist/thread-claims.js";
import {
  resetOrderedJournalParseCountForTests,
  orderedJournalParseCount,
} from "../dist/thread-events.js";
import { withThreadLock } from "../dist/thread-lock.js";
import {
  assignSlice,
  claimSlice,
  synchronizeExpiredClaimsAndReadReady,
  updateClaimedSlice,
  withThreadPlanLock,
} from "../dist/thread-worker.js";

const THREAD_START = new Date("2026-08-18T12:00:00.000Z");
const TEST_ORCHESTRATOR_ACTOR = "orchestrator-caller";

function hashSegment(value) {
  return createHash("sha256").update(value).digest("hex").slice(0, 32);
}

function receiptIdFor(planId, sliceId, workerAgentId, generation, idempotencyKey) {
  return createHash("sha256")
    .update(
      stableStringify({
        plan_id: planId,
        slice_id: sliceId,
        worker_agent_id: workerAgentId,
        generation,
        idempotency_key: idempotencyKey,
      }),
    )
    .digest("hex")
    .slice(0, 32);
}

async function threadFixture(t, slices) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-thread-perf-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  const { plan, write } = await createPlan({
    goal: "Performance contract fixture",
    slices,
    vaultPath,
  });
  return {
    vaultPath,
    notePath: write.notePath,
    planId: plan.plan_id,
    sliceId: slices[0].id ?? "a",
  };
}

async function journalSnapshot(notePath, planId) {
  const journalPath = journalPathFor(notePath, planId);
  const text = await readFile(journalPath, "utf8");
  return {
    journalPath,
    bytes: Buffer.byteLength(text, "utf8"),
    lines: text.split(/\r?\n/).filter((line) => line.trim().length > 0).length,
  };
}

test("final-fix-3: readWorkerUpdateReceipt opens only the direct receipt file among many decoys", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  const planHash = hashSegment(fixture.planId);
  const sliceHash = hashSegment("a");
  const updatesRoot = path.join(
    fixture.vaultPath,
    ".runtime",
    "thread-claims",
    planHash,
    "updates",
  );
  await mkdir(updatesRoot, { recursive: true, mode: 0o700 });
  for (let index = 0; index < 40; index += 1) {
    const decoySlice = hashSegment(`decoy-slice-${index}`);
    const decoyDir = path.join(updatesRoot, decoySlice, "g0");
    await mkdir(decoyDir, { recursive: true, mode: 0o700 });
    await writeFile(
      path.join(decoyDir, `${hashSegment(`decoy-${index}`)}.json`),
      '{"schema":"minni.thread-worker-update-receipt.v1","decoy":true}\n',
      { mode: 0o600 },
    );
  }
  const tokenDigest = createHash("sha256").update("perf-token").digest("hex");
  const idempotencyKey = "perf-direct-receipt";
  const generation = 0;
  await writePendingWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    claimId: hashSegment("claim-perf"),
    generation,
    idempotencyKey,
    kind: "slice.started",
    tokenDigest,
    rev: 2,
    response: {
      slice: {
        id: "a",
        title: "Slice A",
        status: "in_progress",
      },
      ready_before: ["a"],
      ready_after: [],
      rev: 2,
    },
  });

  const originalOpen = claimFs.open;
  let receiptJsonOpens = 0;
  claimFs.open = async (target, flags, ...args) => {
    const flagsNum = Number(flags);
    const accmode =
      typeof constants.O_ACCMODE === "number"
        ? flagsNum & constants.O_ACCMODE
        : flagsNum;
    const isWrite =
      typeof constants.O_WRONLY === "number" &&
      (accmode === constants.O_WRONLY ||
        (typeof constants.O_RDWR === "number" && accmode === constants.O_RDWR));
    if (!isWrite && String(target).endsWith(".json")) {
      receiptJsonOpens += 1;
    }
    return originalOpen(target, flags, ...args);
  };


  let receipt;
  try {
    receipt = await readWorkerUpdateReceipt({
      vaultPath: fixture.vaultPath,
      planId: fixture.planId,
      sliceId: "a",
      workerAgentId: "worker-a",
      generation,
      idempotencyKey,
      claimId: hashSegment("claim-perf"),
    });
  } finally {
    claimFs.open = originalOpen;

  }

  assert.ok(receipt);
  assert.equal(receiptJsonOpens, 1, "direct lookup must open exactly one receipt file");
});

test("final-fix-3: generation advance prunes old receipts and stale replay stays rejected", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  const planHash = hashSegment(fixture.planId);
  const sliceHash = hashSegment("a");
  const oldGenPath = path.join(
    fixture.vaultPath,
    ".runtime",
    "thread-claims",
    planHash,
    "updates",
    sliceHash,
    "g0",
  );
  const receiptPath = path.join(
    oldGenPath,
    `${receiptIdFor(fixture.planId, "a", "worker-a", 0, "prune-start")}.json`,
  );

  await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    workerAgentId: "worker-a",
    now: THREAD_START,
  });
  const claim = await claimSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "prune-claim",
    now: THREAD_START,
  });
  await updateClaimedSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "prune-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  try {
    await stat(receiptPath);
  } catch (error) {
    assert.fail(
      `expected receipt at ${receiptPath}: ${error instanceof Error ? error.message : error}`,
    );
  }

  await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    workerAgentId: "worker-b",
    now: new Date("2026-08-18T12:02:00.000Z"),
  });

  await assert.rejects(stat(receiptPath), (error) => error?.code === "ENOENT");

  const stale = await readWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    generation: 0,
    idempotencyKey: "prune-start",
    claimId: claim.claim_id,
  });
  assert.equal(stale, undefined);
});

test("final-fix-3: repeated rehydratePlan does not grow the legacy journal", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  const before = await journalSnapshot(fixture.notePath, fixture.planId);

  for (let index = 0; index < 5; index += 1) {
    await rehydratePlan(fixture.notePath);
  }

  const after = await journalSnapshot(fixture.notePath, fixture.planId);
  assert.equal(after.bytes, before.bytes);
  assert.equal(after.lines, before.lines);
});

test("final-fix-3: repeated ready and lock reads with no mutation do not grow the journal", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    workerAgentId: "worker-a",
    now: THREAD_START,
  });
  const before = await journalSnapshot(fixture.notePath, fixture.planId);

  for (let index = 0; index < 3; index += 1) {
    await synchronizeExpiredClaimsAndReadReady({
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      actor: TEST_ORCHESTRATOR_ACTOR,
      now: THREAD_START,
    });
    await withThreadPlanLock(
      {
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
        planId: fixture.planId,
        operationId: `read-only-lock-${index}`,
      },
      async (plan) => plan,
    );
    await withThreadLock(
      fixture.vaultPath,
      fixture.planId,
      `noop-lock-${index}`,
      async () => undefined,
    );
  }

  const after = await journalSnapshot(fixture.notePath, fixture.planId);
  assert.equal(after.bytes, before.bytes);
  assert.equal(after.lines, before.lines);
});

test("final-fix-3: prepareThreadMutation parses the ordered journal once per locked mutation", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  resetOrderedJournalParseCountForTests();
  await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    workerAgentId: "worker-a",
    now: THREAD_START,
  });
  assert.equal(
    orderedJournalParseCount,
    1,
    "one locked assign must parse the ordered journal once",
  );
});

test("final-fix-4: one journal parse holds even when claiming A also durably expires sibling B's claim in the same lock", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
    { id: "b", title: "Slice B" },
  ]);
  await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    workerAgentId: "worker-a",
    now: THREAD_START,
  });
  await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "b",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    workerAgentId: "worker-b",
    now: THREAD_START,
  });
  await claimSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "b",
    workerAgentId: "worker-b",
    idempotencyKey: "perf-claim-b-will-expire",
    ttlSeconds: 60,
    now: THREAD_START,
  });

  resetOrderedJournalParseCountForTests();
  await claimSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "perf-claim-a-expires-sibling-b",
    ttlSeconds: 60,
    now: new Date(THREAD_START.getTime() + 10 * 60_000),
  });
  assert.equal(
    orderedJournalParseCount,
    1,
    "claiming A must parse the ordered journal once even though it also durably expires sibling B's claim in the same lock",
  );
});

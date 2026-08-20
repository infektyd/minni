import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile, spawn } from "node:child_process";
import fs, { constants } from "node:fs";
import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { promisify } from "node:util";

import { stableStringify } from "../dist/agent_envelope.js";
import {
  addScar,
  computePlanDigest,
  computePlanDigestHexV2,
  createPlan,
  historyPathFor,
  journalPathFor,
  persistPlan,
  PlanDigestVersionError,
  PlanHistoryAppendError,
  rehydratePlan,
  replan,
  restorePlan,
} from "../dist/plan.js";
import {
  deriveClientEventKey,
  deriveReadyChangedKey,
  deriveSystemEventKey,
  readThreadEvents,
  ThreadCursorGapError,
  ThreadJournalAppendError,
  ThreadJournalReadError,
} from "../dist/thread-events.js";
import {
  commitWorkerUpdateReceipt,
  createClaimSecret,
  deleteClaimSecret,
  hashWorkerUpdateToken,
  pruneWorkerUpdateReceiptsForGeneration,
  readClaimByIdempotency,
  readWorkerUpdateReceipt,
  verifyClaimToken,
  writePendingWorkerUpdateReceipt,
} from "../dist/thread-claims.js";
import {
  assignSlice,
  claimSlice,
  isAcceptedWorkerWrite,
  kickWorkerWriteDrain,
  prepareThreadMutation,
  readySlices,
  threadWorkerErrorText,
  updateClaimedSlice as updateClaimedSliceImpl,
} from "../dist/thread-worker.js";
import { listQueuedWorkerWrites } from "../dist/thread-write-queue.js";
import * as threadWorkerRuntime from "../dist/thread-worker.js";
import { withThreadLock } from "../dist/thread-lock.js";
import { appendFileWithFsync as realAppendFileWithFsync } from "../dist/vault.js";

const BEFORE_EXPIRY = new Date("2026-08-18T14:59:00.000Z");
const AT_EXPIRY = new Date("2026-08-18T15:00:00.000Z");
const execFileAsync = promisify(execFile);
const THREAD_START = new Date("2026-08-18T12:00:00.000Z");

function clientClaimKey(planId, sliceId, workerAgentId, idempotencyKey) {
  return deriveClientEventKey("claim", {
    plan_id: planId,
    slice_id: sliceId,
    worker_agent_id: workerAgentId,
    idempotency_key: idempotencyKey,
  });
}

function clientWorkerKey(planId, sliceId, workerAgentId, idempotencyKey) {
  return deriveClientEventKey("worker", {
    plan_id: planId,
    slice_id: sliceId,
    worker_agent_id: workerAgentId,
    idempotency_key: idempotencyKey,
  });
}
const THREAD_WORKER_MODULE_URL = new URL(
  "../dist/thread-worker.js",
  import.meta.url,
).href;

function jsonRoundTrip(value) {
  return JSON.parse(JSON.stringify(value));
}

async function waitForWorkerQueue(vaultPath, notePath, planId, timeoutMs = 15000) {
  const begin = Date.now();
  while (Date.now() - begin < timeoutMs) {
    const leftover = await listQueuedWorkerWrites(vaultPath, planId);
    if (leftover.length === 0) return;
    kickWorkerWriteDrain({ vaultPath, notePath, planId });
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

let workerUpdateSeq = 0;
function workerUpdate(input, deps) {
  return updateClaimedSliceImpl(
    {
      idempotencyKey:
        input.idempotencyKey ??
        `test-worker-update-${(workerUpdateSeq += 1)}`,
      ...input,
    },
    deps,
  );
}

async function claimFixture(t, overrides = {}) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-thread-claim-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  return {
    vaultPath,
    planId: "plan-alpha",
    sliceId: "slice-a",
    generation: 2,
    workerAgentId: "worker-a",
    idempotencyKey: "claim-attempt-1",
    expiresAt: "2026-08-18T15:00:00.000Z",
    rev: 7,
    ...overrides,
  };
}

function claimPathParts(input) {
  const claimId = createHash("sha256")
    .update(stableStringify({
      plan_id: input.planId,
      slice_id: input.sliceId,
      generation: input.generation,
      idempotency_key: input.idempotencyKey,
    }))
    .digest("hex")
    .slice(0, 32);
  const planHash = createHash("sha256")
    .update(input.planId)
    .digest("hex")
    .slice(0, 32);
  return { claimId, planHash };
}

async function prepareRuntimeSwap(input, outside) {
  const { planHash } = claimPathParts(input);
  const outsidePlanDir = path.join(outside, "thread-claims", planHash);
  await mkdir(outsidePlanDir, { recursive: true, mode: 0o700 });
  return {
    runtimePath: path.join(input.vaultPath, ".runtime"),
    movedRuntimePath: path.join(input.vaultPath, ".runtime-original"),
    outsidePlanDir,
  };
}

async function threadFixture(t, slices = [
  { id: "a", title: "Slice A" },
  { id: "b", title: "Slice B" },
]) {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-thread-runtime-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  const created = await createPlan(
    {
      goal: "Exercise claimed worker mutations",
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
  };
}

const TEST_ORCHESTRATOR_ACTOR = "orchestrator-test";

async function assignWorker(
  fixture,
  sliceId,
  workerAgentId,
  actorAgentId = TEST_ORCHESTRATOR_ACTOR,
) {
  return assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId,
    workerAgentId,
    actorAgentId,
    now: new Date(THREAD_START),
  });
}

function startBarrierWorker(operation, input) {
  const script = `
    import { once } from "node:events";
    const worker = await import(process.argv[1]);
    const operation = process.argv[2];
    const input = JSON.parse(Buffer.from(process.argv[3], "base64url").toString("utf8"));
    if (input.now !== undefined) input.now = new Date(input.now);
    process.stdout.write(JSON.stringify({ phase: "ready" }) + "\\n");
    await once(process.stdin, "data");
    process.stdout.write(JSON.stringify({ phase: "started" }) + "\\n");
    try {
      const value = await worker[operation](input);
      const accepted = Boolean(value && value.accepted === true && value.applied === false);
      process.stdout.write(JSON.stringify({ phase: "result", ok: true, accepted, value }) + "\\n");
    } catch (error) {
      process.stdout.write(JSON.stringify({
        phase: "result",
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      }) + "\\n");
    }
  `;
  const encoded = Buffer.from(JSON.stringify(input)).toString("base64url");
  const child = spawn(process.execPath, [
    "--input-type=module",
    "--eval",
    script,
    THREAD_WORKER_MODULE_URL,
    operation,
    encoded,
  ], {
    stdio: ["pipe", "pipe", "pipe"],
  });
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");

  let stdout = "";
  let stderr = "";
  let readyResolve;
  let readyReject;
  let startedResolve;
  let startedReject;
  let resultResolve;
  let resultReject;
  const ready = new Promise((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });
  const started = new Promise((resolve, reject) => {
    startedResolve = resolve;
    startedReject = reject;
  });
  const result = new Promise((resolve, reject) => {
    resultResolve = resolve;
    resultReject = reject;
  });

  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
    while (stdout.includes("\n")) {
      const newline = stdout.indexOf("\n");
      const line = stdout.slice(0, newline);
      stdout = stdout.slice(newline + 1);
      if (!line) continue;
      const message = JSON.parse(line);
      if (message.phase === "ready") readyResolve();
      if (message.phase === "started") startedResolve();
      if (message.phase === "result") resultResolve(message);
    }
  });
  child.on("error", (error) => {
    readyReject(error);
    startedReject(error);
    resultReject(error);
  });
  child.on("close", (code) => {
    if (code !== 0) {
      const error = new Error(
        `barrier worker exited ${code}: ${stderr || stdout}`,
      );
      readyReject(error);
      startedReject(error);
      resultReject(error);
    } else if (stderr) {
      resultReject(new Error(`barrier worker wrote stderr: ${stderr}`));
    }
  });

  return {
    ready,
    started,
    release() {
      child.stdin.end("go\n");
    },
    result,
  };
}

async function releaseTogether(workers) {
  await Promise.all(workers.map((worker) => worker.ready));
  for (const worker of workers) worker.release();
  return Promise.all(workers.map((worker) => worker.result));
}

test("same claim idempotency key replays the identical token and response", async (t) => {
  const input = await claimFixture(t);
  const first = await createClaimSecret(input);
  const retry = await createClaimSecret({
    ...input,
    expiresAt: "2026-08-19T15:00:00.000Z",
    rev: 999,
  });

  assert.equal(retry.envelope.token, first.envelope.token);
  assert.equal(retry.envelope.claim_id, first.envelope.claim_id);
  assert.deepEqual(retry.envelope.response, first.envelope.response);
  assert.match(first.envelope.token, /^[A-Za-z0-9_-]{43}$/);

  const expectedClaimId = createHash("sha256")
    .update(stableStringify({
      plan_id: input.planId,
      slice_id: input.sliceId,
      generation: input.generation,
      idempotency_key: input.idempotencyKey,
    }))
    .digest("hex")
    .slice(0, 32);
  assert.equal(first.envelope.claim_id, expectedClaimId);
});

test("concurrent identical creates publish one complete private envelope", async (t) => {
  const input = await claimFixture(t);
  const claims = await Promise.all(
    Array.from({ length: 8 }, () => createClaimSecret(input)),
  );

  assert.equal(new Set(claims.map((claim) => claim.envelope.token)).size, 1);
  assert.equal(new Set(claims.map((claim) => claim.envelope.claim_id)).size, 1);
  const stored = JSON.parse(await readFile(claims[0].filePath, "utf8"));
  assert.deepEqual(stored, claims[0].envelope);
  assert.deepEqual(await readdir(path.dirname(claims[0].filePath)), [
    `${claims[0].envelope.claim_id}.json`,
  ]);
});

test("separate processes using one idempotency key receive one token", async (t) => {
  const input = await claimFixture(t);
  const moduleUrl = new URL("../dist/thread-claims.js", import.meta.url).href;
  const workerScript = `
    const { createClaimSecret } = await import(${JSON.stringify(moduleUrl)});
    const input = JSON.parse(Buffer.from(process.argv[1], "base64url").toString("utf8"));
    const claim = await createClaimSecret(input);
    process.stdout.write(JSON.stringify({
      claim_id: claim.envelope.claim_id,
      token: claim.envelope.token,
    }));
  `;
  const encodedInput = Buffer.from(JSON.stringify(input)).toString("base64url");

  const [first, second] = await Promise.all([
    execFileAsync(process.execPath, [
      "--input-type=module",
      "--eval",
      workerScript,
      encodedInput,
    ]),
    execFileAsync(process.execPath, [
      "--input-type=module",
      "--eval",
      workerScript,
      encodedInput,
    ]),
  ]);

  assert.equal(first.stderr, "");
  assert.equal(second.stderr, "");
  assert.deepEqual(JSON.parse(second.stdout), JSON.parse(first.stdout));
});

test("claim envelope is mode-0600 under the private non-Markdown runtime tree", async (t) => {
  const input = await claimFixture(t, {
    planId: "../plan/private",
    sliceId: "../../slice/private",
    idempotencyKey: "../retry/private",
  });
  const claim = await createClaimSecret(input);

  assert.equal((await stat(claim.filePath)).mode & 0o777, 0o600);
  assert.equal(
    (await stat(path.join(input.vaultPath, ".runtime"))).mode & 0o777,
    0o700,
  );
  assert.equal((await stat(path.dirname(claim.filePath))).mode & 0o777, 0o700);
  assert.equal(
    (await stat(path.dirname(path.dirname(claim.filePath)))).mode & 0o777,
    0o700,
  );
  assert.match(claim.filePath, /[\/\\]\.runtime[\/\\]thread-claims[\/\\]/);
  assert.equal(claim.filePath.endsWith(".md"), false);
  assert.equal(claim.filePath.includes(input.planId), false);
  assert.equal(claim.filePath.includes(input.sliceId), false);
  assert.equal(claim.filePath.includes(input.idempotencyKey), false);

  assert.deepEqual(Object.keys(claim.envelope.response).sort(), [
    "claim_id",
    "expires_at",
    "generation",
    "plan_id",
    "rev",
    "slice_id",
    "token",
    "worker_agent_id",
  ]);
  assert.equal(JSON.stringify(claim.envelope.response).includes(".runtime"), false);
  assert.equal("filePath" in claim.envelope.response, false);
});

test("verifyClaimToken uses claim scope and rejects wrong or expired tokens", async (t) => {
  const input = await claimFixture(t);
  const claim = await createClaimSecret(input);

  const verified = await verifyClaimToken({
    ...input,
    token: claim.envelope.token,
    now: BEFORE_EXPIRY,
  });
  assert.deepEqual(verified.envelope, claim.envelope);

  await assert.rejects(
    verifyClaimToken({ ...input, token: "wrong", now: BEFORE_EXPIRY }),
    /claim token mismatch/,
  );
  await assert.rejects(
    verifyClaimToken({
      ...input,
      token: claim.envelope.token,
      now: AT_EXPIRY,
    }),
    /claim expired/,
  );
  await assert.rejects(
    verifyClaimToken({
      ...input,
      claimId: claim.envelope.claim_id,
      sliceId: "slice-b",
      token: claim.envelope.token,
      now: BEFORE_EXPIRY,
    }),
    /claim path\/metadata mismatch/,
  );
});

test("readClaimByIdempotency retrieves the stored secret and deletion is idempotent", async (t) => {
  const input = await claimFixture(t);
  const claim = await createClaimSecret(input);

  assert.deepEqual(
    await readClaimByIdempotency(
      input.vaultPath,
      input.planId,
      input.sliceId,
      input.generation,
      input.idempotencyKey,
    ),
    claim.envelope,
  );

  await deleteClaimSecret({
    vaultPath: input.vaultPath,
    planId: input.planId,
    claimId: claim.envelope.claim_id,
  });
  await deleteClaimSecret({
    vaultPath: input.vaultPath,
    planId: input.planId,
    claimId: claim.envelope.claim_id,
  });
  assert.equal(
    await readClaimByIdempotency(
      input.vaultPath,
      input.planId,
      input.sliceId,
      input.generation,
      input.idempotencyKey,
    ),
    undefined,
  );
});

test("claim store rejects empty idempotency keys and identity mismatches", async (t) => {
  const input = await claimFixture(t);
  await assert.rejects(
    createClaimSecret({ ...input, idempotencyKey: " \t " }),
    /non-empty idempotency key/,
  );

  await createClaimSecret(input);
  await assert.rejects(
    createClaimSecret({ ...input, workerAgentId: "worker-b" }),
    /claim metadata mismatch/,
  );
});

test("claim store rejects tampered or group-readable envelopes", async (t) => {
  await t.test("identity metadata no longer matches its derived path", async (st) => {
    const input = await claimFixture(st);
    const claim = await createClaimSecret(input);
    const tampered = JSON.parse(await readFile(claim.filePath, "utf8"));
    tampered.worker_agent_id = "worker-b";
    await writeFile(claim.filePath, `${JSON.stringify(tampered)}\n`);

    await assert.rejects(
      readClaimByIdempotency(
        input.vaultPath,
        input.planId,
        input.sliceId,
        input.generation,
        input.idempotencyKey,
      ),
      /claim metadata mismatch/,
    );
  });

  await t.test("extra model-facing response fields are rejected", async (st) => {
    const input = await claimFixture(st);
    const claim = await createClaimSecret(input);
    const tampered = JSON.parse(await readFile(claim.filePath, "utf8"));
    tampered.response.private_path = claim.filePath;
    await writeFile(claim.filePath, `${JSON.stringify(tampered)}\n`);

    await assert.rejects(
      readClaimByIdempotency(
        input.vaultPath,
        input.planId,
        input.sliceId,
        input.generation,
        input.idempotencyKey,
      ),
      /claim metadata mismatch/,
    );
  });

  await t.test("secret file becomes group-readable", async (st) => {
    const input = await claimFixture(st);
    const claim = await createClaimSecret(input);
    await chmod(claim.filePath, 0o640);

    await assert.rejects(
      verifyClaimToken({
        ...input,
        token: claim.envelope.token,
        now: BEFORE_EXPIRY,
      }),
      /claim secret permissions mismatch/,
    );
  });
});

test("parent swap before temporary open cannot redirect a claim write", async (t) => {
  const input = await claimFixture(t);
  const outside = await mkdtemp(path.join(tmpdir(), "minni-claim-race-outside-"));
  t.after(() => rm(outside, { recursive: true, force: true }));
  const paths = await prepareRuntimeSwap(input, outside);
  const { planHash } = claimPathParts(input);
  const originalOpen = fs.promises.open;
  let swapped = false;

  fs.promises.open = async (target, flags, ...args) => {
    if (
      !swapped &&
      String(target).endsWith(".tmp") &&
      (Number(flags) & constants.O_CREAT) !== 0
    ) {
      swapped = true;
      await rename(paths.runtimePath, paths.movedRuntimePath);
      await symlink(outside, paths.runtimePath, "dir");
    }
    return originalOpen(target, flags, ...args);
  };
  syncBuiltinESMExports();

  try {
    await assert.rejects(
      createClaimSecret(input),
      /claim store parent changed during operation/,
    );
  } finally {
    fs.promises.open = originalOpen;
    syncBuiltinESMExports();
  }

  assert.equal(swapped, true);
  assert.deepEqual(await readdir(paths.outsidePlanDir), []);
  const originalPlanDir = path.join(
    paths.movedRuntimePath,
    "thread-claims",
    planHash,
  );
  assert.deepEqual(
    (await readdir(originalPlanDir)).filter(
      (name) => name.endsWith(".json") || name.endsWith(".tmp"),
    ),
    [],
  );
});

test("thread-claims and plan-directory swaps cannot redirect a claim write", async (t) => {
  for (const swappedParent of ["thread-claims", "plan-directory"]) {
    await t.test(swappedParent, async (st) => {
      const input = await claimFixture(st);
      const outside = await mkdtemp(
        path.join(tmpdir(), "minni-claim-race-outside-"),
      );
      st.after(() => rm(outside, { recursive: true, force: true }));
      const { planHash } = claimPathParts(input);
      const claimsPath = path.join(
        input.vaultPath,
        ".runtime",
        "thread-claims",
      );
      const parentPath =
        swappedParent === "thread-claims"
          ? claimsPath
          : path.join(claimsPath, planHash);
      const movedParentPath =
        swappedParent === "thread-claims"
          ? path.join(input.vaultPath, ".runtime", "thread-claims-original")
          : path.join(claimsPath, `${planHash}-original`);
      const outsidePlanDir =
        swappedParent === "thread-claims"
          ? path.join(outside, planHash)
          : outside;
      const originalPlanDir =
        swappedParent === "thread-claims"
          ? path.join(movedParentPath, planHash)
          : movedParentPath;
      await mkdir(outsidePlanDir, { recursive: true, mode: 0o700 });

      const originalOpen = fs.promises.open;
      let swapped = false;
      fs.promises.open = async (target, flags, ...args) => {
        if (
          !swapped &&
          String(target).endsWith(".tmp") &&
          (Number(flags) & constants.O_CREAT) !== 0
        ) {
          swapped = true;
          await rename(parentPath, movedParentPath);
          await symlink(outside, parentPath, "dir");
        }
        return originalOpen(target, flags, ...args);
      };
      syncBuiltinESMExports();

      try {
        await assert.rejects(
          createClaimSecret(input),
          /claim store parent changed during operation/,
        );
      } finally {
        fs.promises.open = originalOpen;
        syncBuiltinESMExports();
      }

      assert.equal(swapped, true);
      assert.deepEqual(await readdir(outsidePlanDir), []);
      assert.deepEqual(
        (await readdir(originalPlanDir)).filter(
          (name) => name.endsWith(".json") || name.endsWith(".tmp"),
        ),
        [],
      );
    });
  }
});

test("parent swap before atomic rename cannot redirect or orphan a claim write", async (t) => {
  const input = await claimFixture(t);
  const outside = await mkdtemp(path.join(tmpdir(), "minni-claim-race-outside-"));
  t.after(() => rm(outside, { recursive: true, force: true }));
  const paths = await prepareRuntimeSwap(input, outside);
  const { planHash } = claimPathParts(input);
  const originalRename = fs.promises.rename;
  let swapped = false;

  fs.promises.rename = async (from, to) => {
    if (
      !swapped &&
      String(from).endsWith(".tmp") &&
      String(to).endsWith(".json")
    ) {
      swapped = true;
      await originalRename(paths.runtimePath, paths.movedRuntimePath);
      await symlink(outside, paths.runtimePath, "dir");
    }
    return originalRename(from, to);
  };
  syncBuiltinESMExports();

  try {
    await assert.rejects(
      createClaimSecret(input),
      /claim store parent changed during operation/,
    );
  } finally {
    fs.promises.rename = originalRename;
    syncBuiltinESMExports();
  }

  assert.equal(swapped, true);
  assert.deepEqual(await readdir(paths.outsidePlanDir), []);
  const originalPlanDir = path.join(
    paths.movedRuntimePath,
    "thread-claims",
    planHash,
  );
  assert.deepEqual(
    (await readdir(originalPlanDir)).filter(
      (name) => name.endsWith(".json") || name.endsWith(".tmp"),
    ),
    [],
  );
});

test("parent swap before final read cannot redirect to an outside envelope", async (t) => {
  const input = await claimFixture(t);
  const claim = await createClaimSecret(input);
  const outside = await mkdtemp(path.join(tmpdir(), "minni-claim-race-outside-"));
  t.after(() => rm(outside, { recursive: true, force: true }));
  const paths = await prepareRuntimeSwap(input, outside);
  await writeFile(
    path.join(paths.outsidePlanDir, `${claim.envelope.claim_id}.json`),
    '{"outside":"decoy"}\n',
    { mode: 0o600 },
  );
  const originalOpen = fs.promises.open;
  let swapped = false;

  fs.promises.open = async (target, flags, ...args) => {
    if (
      !swapped &&
      path.basename(String(target)) === `${claim.envelope.claim_id}.json` &&
      (Number(flags) & constants.O_CREAT) === 0
    ) {
      swapped = true;
      await rename(paths.runtimePath, paths.movedRuntimePath);
      await symlink(outside, paths.runtimePath, "dir");
    }
    return originalOpen(target, flags, ...args);
  };
  syncBuiltinESMExports();

  let replay;
  try {
    replay = await readClaimByIdempotency(
      input.vaultPath,
      input.planId,
      input.sliceId,
      input.generation,
      input.idempotencyKey,
    );
  } finally {
    fs.promises.open = originalOpen;
    syncBuiltinESMExports();
  }

  assert.equal(swapped, true);
  assert.deepEqual(replay, claim.envelope);
});

test("claim store refuses symlink escapes from the vault runtime tree", async (t) => {
  for (const escapedSegment of [".runtime", "thread-claims", "plan-directory"]) {
    await t.test(escapedSegment, async (st) => {
      const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-claim-vault-"));
      const outside = await mkdtemp(path.join(tmpdir(), "minni-claim-outside-"));
      st.after(async () => {
        await rm(vaultPath, { recursive: true, force: true });
        await rm(outside, { recursive: true, force: true });
      });

      if (escapedSegment === ".runtime") {
        await symlink(outside, path.join(vaultPath, ".runtime"), "dir");
      } else {
        const claimsPath = path.join(vaultPath, ".runtime", "thread-claims");
        await mkdir(
          escapedSegment === "thread-claims"
            ? path.dirname(claimsPath)
            : claimsPath,
          { recursive: true },
        );
        const escapedPath =
          escapedSegment === "thread-claims"
            ? claimsPath
            : path.join(
                claimsPath,
                createHash("sha256")
                  .update("plan-escape")
                  .digest("hex")
                  .slice(0, 32),
              );
        await symlink(outside, escapedPath, "dir");
      }

      await assert.rejects(
        createClaimSecret({
          vaultPath,
          planId: "plan-escape",
          sliceId: "slice-escape",
          generation: 0,
          workerAgentId: "worker-escape",
          idempotencyKey: "retry-escape",
          expiresAt: "2026-08-18T15:00:00.000Z",
          rev: 1,
        }),
        /claim store path mismatch/,
      );
      assert.deepEqual(await readdir(outside), []);
    });
  }
});

test("readySlices is deterministic across dependencies and claim expiry", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "join", title: "Join", depends_on: ["source"] },
    { id: "source", title: "Source" },
    { id: "other", title: "Other" },
  ]);
  await assignWorker(fixture, "source", "worker-source");
  await claimSlice({
    ...fixture,
    sliceId: "source",
    workerAgentId: "worker-source",
    idempotencyKey: "source-claim",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });
  const plan = await rehydratePlan(fixture.notePath);

  assert.deepEqual(
    readySlices(plan, new Date("2026-08-18T12:00:30.000Z")).map(
      (slice) => slice.id,
    ),
    ["other"],
  );
  assert.deepEqual(
    readySlices(plan, new Date("2026-08-18T12:01:00.000Z")).map(
      (slice) => slice.id,
    ),
    ["other", "source"],
  );
});

test("claimSlice replays only its public response and increments attempt once", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const input = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  };

  const first = await claimSlice(input);
  const replay = await claimSlice({
    ...input,
    ttlSeconds: 999,
    now: new Date("2026-08-18T12:00:30.000Z"),
  });
  assert.deepEqual(replay, first);
  assert.deepEqual(Object.keys(first).sort(), [
    "claim_id",
    "expires_at",
    "generation",
    "plan_id",
    "rev",
    "slice_id",
    "token",
    "worker_agent_id",
  ]);
  assert.equal("envelope" in first, false);
  assert.equal("filePath" in first, false);

  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].attempt, 1);
  assert.equal(plan.slices[0].claim.claim_id, first.claim_id);
});

test("claimSlice removes its private envelope when note persistence fails", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const input = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-fails-to-persist",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  };

  await assert.rejects(
    claimSlice(input, {
      persistPlan: async () => {
        throw new Error("injected note persistence failure");
      },
    }),
    /injected note persistence failure/,
  );
  const plan = await rehydratePlan(fixture.notePath);
  assert.equal(plan.slices[0].claim, undefined);
  assert.equal(
    await readClaimByIdempotency(
      fixture.vaultPath,
      fixture.planId,
      "a",
      plan.slices[0].generation ?? 0,
      input.idempotencyKey,
    ),
    undefined,
  );
});

test("two real processes completing independent slices preserve both results", async (t) => {
  const fixture = await threadFixture(t);
  await Promise.all([
    assignWorker(fixture, "a", "worker-a"),
    assignWorker(fixture, "b", "worker-b"),
  ]);
  const [claimA, claimB] = await Promise.all([
    claimSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      idempotencyKey: "claim-a",
      now: new Date(THREAD_START),
    }),
    claimSlice({
      ...fixture,
      sliceId: "b",
      workerAgentId: "worker-b",
      idempotencyKey: "claim-b",
      now: new Date(THREAD_START),
    }),
  ]);
  const workers = [
    startBarrierWorker("updateClaimedSlice", {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claimA.token,
      idempotencyKey: "complete-a-process",
      action: {
        action: "complete",
        evidence: "Slice A verified in deterministic child-process output",
      },
      now: "2026-08-18T12:01:00.000Z",
    }),
    startBarrierWorker("updateClaimedSlice", {
      ...fixture,
      sliceId: "b",
      workerAgentId: "worker-b",
      token: claimB.token,
      idempotencyKey: "complete-b-process",
      action: {
        action: "complete",
        evidence: "Slice B verified in deterministic child-process output",
      },
      now: "2026-08-18T12:01:00.000Z",
    }),
  ];

  const results = await releaseTogether(workers);
  assert.deepEqual(results.map((result) => result.ok), [true, true]);
  await waitForWorkerQueue(fixture.vaultPath, fixture.notePath, fixture.planId);
  const final = await rehydratePlan(fixture.notePath);
  assert.equal(final.slices.find((slice) => slice.id === "a").status, "done");
  assert.equal(final.slices.find((slice) => slice.id === "b").status, "done");
});

test("simultaneous real-process double claim commits exactly one claim", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const workers = ["double-claim-a", "double-claim-b"].map(
    (idempotencyKey) => startBarrierWorker("claimSlice", {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      idempotencyKey,
      ttlSeconds: 60,
      now: "2026-08-18T12:00:00.000Z",
    }),
  );

  const results = await releaseTogether(workers);
  assert.equal(results.filter((result) => result.ok).length, 1, JSON.stringify(results));
  assert.equal(results.filter((result) => !result.ok).length, 1, JSON.stringify(results));
  assert.match(results.find((result) => !result.ok).error, /already claimed/);

  const final = await rehydratePlan(fixture.notePath);
  const winner = results.find((result) => result.ok).value;
  assert.equal(final.slices[0].attempt, 1);
  assert.equal(final.slices[0].claim.claim_id, winner.claim_id);
});

test("completion after expiry is rejected without a sweeper", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });

  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      action: {
        action: "complete",
        evidence: "Slice A verified after the lease deadline",
      },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /claim expired/,
  );
  const final = await rehydratePlan(fixture.notePath);
  assert.equal(final.slices[0].status, "pending");
});

test("barrier expiry-versus-complete race commits exactly one outcome", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const original = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "original-claim",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });
  const workers = [
    startBarrierWorker("updateClaimedSlice", {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: original.token,
      idempotencyKey: "barrier-complete-before-expiry",
      action: {
        action: "complete",
        evidence: "Completion won before the deterministic lease deadline",
      },
      now: "2026-08-18T12:00:59.000Z",
    }),
    startBarrierWorker("claimSlice", {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      idempotencyKey: "replacement-claim",
      ttlSeconds: 60,
      now: "2026-08-18T12:01:00.000Z",
    }),
  ];

  const results = await releaseTogether(workers);
  await waitForWorkerQueue(fixture.vaultPath, fixture.notePath, fixture.planId);
  const applied = results.filter((result) => result.ok && !result.accepted);
  assert.ok(applied.length >= 1, JSON.stringify(results));
  const final = await rehydratePlan(fixture.notePath);
  const completionWon = final.slices[0].status === "done";
  if (completionWon) {
    assert.equal(final.slices[0].status, "done");
    assert.equal(final.slices[0].claim, undefined);
    assert.match(results[1].error, /not claimable|claim scope mismatch/);
  } else {
    assert.equal(final.slices[0].status, "pending");
    assert.ok(final.slices[0].claim, JSON.stringify({ results, slice: final.slices[0] }));
    if (results[1].ok && results[1].value?.claim_id) {
      assert.equal(final.slices[0].claim.claim_id, results[1].value.claim_id);
    }
    if (!results[0].ok) {
      assert.match(results[0].error, /claim token mismatch|claim scope mismatch|claim expired/);
    } else {
      assert.equal(results[0].accepted, true);
    }
  }
});

test("reassignment revokes the old token and increments generation", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "worker-a-claim",
    now: new Date(THREAD_START),
  });
  const reassigned = await assignSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-b",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    assignmentProfile: "adversarial-review",
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(reassigned.slice.assigned_to, "worker-b");
  assert.equal(reassigned.slice.assignment_profile, "adversarial-review");
  assert.equal(reassigned.slice.generation, claim.generation + 1);
  assert.equal(reassigned.slice.claim, undefined);

  const [staleResult] = await releaseTogether([
    startBarrierWorker("updateClaimedSlice", {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "stale-generation-complete",
      action: {
        action: "complete",
        evidence: "Stale generation must not be accepted as completion",
      },
      now: "2026-08-18T12:01:00.000Z",
    }),
  ]);
  assert.equal(staleResult.ok, false);
  assert.match(staleResult.error, /claim scope mismatch/);
  await assert.rejects(
    verifyClaimToken({
      vaultPath: fixture.vaultPath,
      planId: fixture.planId,
      sliceId: "a",
      generation: claim.generation,
      workerAgentId: "worker-a",
      token: claim.token,
      claimId: claim.claim_id,
      now: new Date("2026-08-18T12:01:00.000Z"),
    }),
    /claim not found/,
  );
});

test("replan invalidates claims and generations when slice meaning changes", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A", gate: "old gate" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-before-replan",
    now: new Date(THREAD_START),
  });
  const current = await rehydratePlan(fixture.notePath);
  const replanned = replan(current, [
    { id: "a", title: "Slice A revised", gate: "new gate" },
  ]);
  assert.equal(replanned.slices[0].generation, claim.generation + 1);
  assert.equal(replanned.slices[0].claim, undefined);
  await persistPlan(replanned, {
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
  });

  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      action: { action: "start" },
      now: new Date("2026-08-18T12:01:00.000Z"),
    }),
    /claim scope mismatch/,
  );
});

test("worker token cannot mutate a sibling or Thread topology", async (t) => {
  const fixture = await threadFixture(t);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a",
    now: new Date(THREAD_START),
  });
  const before = await rehydratePlan(fixture.notePath);

  const rejectedWorkers = [
    startBarrierWorker("updateClaimedSlice", {
      ...fixture,
      sliceId: "b",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "sibling-start-reject",
      action: { action: "start" },
      now: "2026-08-18T12:01:00.000Z",
    }),
    startBarrierWorker("updateClaimedSlice", {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "replan-injection-reject",
      action: {
        action: "replan",
        slices: [{ id: "owned-by-caller", title: "Injected topology" }],
      },
      now: "2026-08-18T12:01:00.000Z",
    }),
  ];
  const rejected = await releaseTogether(rejectedWorkers);
  await waitForWorkerQueue(fixture.vaultPath, fixture.notePath, fixture.planId);
  assert.equal(rejected[1].ok, false);
  assert.match(rejected[1].error, /unsupported worker action/);
  if (rejected[0].ok) {
    assert.equal(rejected[0].accepted, true);
  } else {
    assert.match(rejected[0].error, /claim scope mismatch/);
  }

  const [startedResult] = await releaseTogether([
    startBarrierWorker("updateClaimedSlice", {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "start-with-extra-fields",
      action: {
        action: "start",
        depends_on: [],
        gate: "caller-controlled gate",
        assigned_to: "attacker",
        slices: [{ id: "injected", title: "Injected sibling" }],
      },
      now: "2026-08-18T12:01:00.000Z",
    }),
  ]);
  assert.equal(startedResult.ok, true, JSON.stringify(startedResult));
  const started = startedResult.value;
  assert.equal(started.slice.status, "in_progress");
  assert.equal(started.slice.assigned_to, "worker-a");
  assert.equal(started.slice.gate, before.slices[0].gate);
  assert.deepEqual(started.slice.depends_on, before.slices[0].depends_on);
  assert.deepEqual(
    started.plan.slices.map((slice) => slice.id),
    before.slices.map((slice) => slice.id),
  );
  assert.deepEqual(started.plan.constraints, before.constraints);
});

test("worker actions copy only discriminated scar and proposal fields", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a",
    now: new Date(THREAD_START),
  });
  const baseInput = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    now: new Date("2026-08-18T12:01:00.000Z"),
  };

  await workerUpdate({
    ...baseInput,
    action: {
      action: "scar",
      kind: "dead_end",
      signal: "Search path had no matching implementation",
      resolution: "Use the canonical plan module",
      injected: "must not persist",
    },
  });
  const proposed = await workerUpdate({
    ...baseInput,
    action: {
      action: "propose_structure",
      proposal: {
        kind: "split",
        reason: "The slice has two independently verifiable outputs",
        slices: [{
          id: "part-a",
          title: "Part A",
          gate: "tests pass",
          depends_on: ["a"],
          evidence: "proposal evidence",
          assigned_to: "attacker",
          injected: "must not persist",
        }],
        injected: "must not persist",
      },
      injected: "must not persist",
    },
  });

  assert.deepEqual(proposed.plan.scar_tissue.at(-1), {
    kind: "dead_end",
    signal: "Search path had no matching implementation",
    resolution: "Use the canonical plan module",
  });
  assert.deepEqual(proposed.slice.proposals.at(-1), {
    kind: "split",
    reason: "The slice has two independently verifiable outputs",
    slices: [{
      id: "part-a",
      title: "Part A",
      gate: "tests pass",
      depends_on: ["a"],
      evidence: "proposal evidence",
    }],
  });
});

test("queued claim and update clocks are sampled only after the Thread lock", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");

  let claimClockSamples = 0;
  let queuedClaim;
  await withThreadLock(
    fixture.vaultPath,
    fixture.planId,
    "hold-before-claim",
    async () => {
      queuedClaim = claimSlice({
        ...fixture,
        sliceId: "a",
        workerAgentId: "worker-a",
        idempotencyKey: "post-lock-clock",
        ttlSeconds: 60,
        now: () => {
          claimClockSamples += 1;
          return new Date("2026-08-18T12:05:00.000Z");
        },
      }).then(
        (value) => ({ ok: true, value }),
        (error) => ({ ok: false, error }),
      );
      await Promise.resolve();
      assert.equal(
        claimClockSamples,
        0,
        "a queued claim must not sample its lease clock before lock acquisition",
      );
    },
  );
  const claimResult = await queuedClaim;
  assert.equal(claimResult.ok, true, claimResult.error?.message);
  const claim = claimResult.value;
  assert.equal(claimClockSamples, 1);
  assert.equal(claim.expires_at, "2026-08-18T12:06:00.000Z");

  let updateClockSamples = 0;
  let queuedUpdate;
  await withThreadLock(
    fixture.vaultPath,
    fixture.planId,
    "hold-until-claim-expires",
    async () => {
      queuedUpdate = workerUpdate({
        ...fixture,
        sliceId: "a",
        workerAgentId: "worker-a",
        token: claim.token,
        action: {
          action: "complete",
          evidence: "Queued completion must use post-lock lease time",
        },
        now: () => {
          updateClockSamples += 1;
          return new Date("2026-08-18T12:07:00.000Z");
        },
      }).then(
        (value) => ({ ok: true, value }),
        (error) => ({ ok: false, error }),
      );
      await Promise.resolve();
      assert.equal(
        updateClockSamples,
        0,
        "a queued update must not sample its verification clock before lock acquisition",
      );
    },
  );
  const update = await queuedUpdate;
  assert.equal(update.ok, true, update.error?.message);
  assert.equal(isAcceptedWorkerWrite(update.value), true);
  await waitForWorkerQueue(fixture.vaultPath, fixture.notePath, fixture.planId);
  const final = await rehydratePlan(fixture.notePath);
  assert.equal(final.slices[0].status, "pending");
});

test("expired orphan idempotency envelopes cannot be replayed into a new claim", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const before = await rehydratePlan(fixture.notePath);
  const generation = before.slices[0].generation ?? 0;
  const stale = await createClaimSecret({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    generation,
    workerAgentId: "worker-a",
    idempotencyKey: "orphan-retry",
    expiresAt: "2026-08-18T12:00:30.000Z",
    rev: before.rev + 1,
  });

  const fresh = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "orphan-retry",
    ttlSeconds: 60,
    now: () => new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(fresh.generation, generation + 1);
  assert.notEqual(fresh.claim_id, stale.envelope.claim_id);
  assert.notEqual(fresh.token, stale.envelope.token);
  assert.equal(fresh.expires_at, "2026-08-18T12:02:00.000Z");
  const final = await rehydratePlan(fixture.notePath);
  assert.equal(final.slices[0].generation, generation + 1);
  assert.equal(final.slices[0].claim.claim_id, fresh.claim_id);
});

test("assignment persistence failure leaves the previous claim fully usable", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "survives-failed-reassignment",
    now: new Date(THREAD_START),
  });

  await assert.rejects(
    assignSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-b",
      actorAgentId: TEST_ORCHESTRATOR_ACTOR,
      now: new Date("2026-08-18T12:01:00.000Z"),
    }, {
      persistPlan: async () => {
        throw new Error("injected reassignment persistence failure");
      },
    }),
    /injected reassignment persistence failure/,
  );

  const unchanged = await rehydratePlan(fixture.notePath);
  assert.equal(unchanged.slices[0].assigned_to, "worker-a");
  assert.equal(unchanged.slices[0].claim.claim_id, claim.claim_id);
  const started = await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(started.slice.status, "in_progress");
});

test("cleanup failures cannot undo reassignment or completion", async (t) => {
  await t.test("reassignment", async (st) => {
    const fixture = await threadFixture(st, [
      { id: "a", title: "Slice A" },
    ]);
    await assignWorker(fixture, "a", "worker-a");
    const claim = await claimSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      idempotencyKey: "cleanup-failure-reassign",
      now: new Date(THREAD_START),
    });
    const reassigned = await assignSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-b",
      actorAgentId: TEST_ORCHESTRATOR_ACTOR,
      now: new Date("2026-08-18T12:01:00.000Z"),
    }, {
      deleteClaimSecret: async () => {
        throw new Error("injected cleanup failure");
      },
    });
    assert.equal(reassigned.slice.assigned_to, "worker-b");
    assert.equal(reassigned.slice.claim, undefined);
    assert.equal(reassigned.slice.generation, claim.generation + 1);
    const orphan = await verifyClaimToken({
      vaultPath: fixture.vaultPath,
      planId: fixture.planId,
      sliceId: "a",
      generation: claim.generation,
      workerAgentId: "worker-a",
      token: claim.token,
      claimId: claim.claim_id,
      now: new Date("2026-08-18T12:01:00.000Z"),
    });
    assert.equal(orphan.envelope.claim_id, claim.claim_id);
    await assert.rejects(
      workerUpdate({
        ...fixture,
        sliceId: "a",
        workerAgentId: "worker-a",
        token: claim.token,
        action: { action: "start" },
        now: new Date("2026-08-18T12:01:00.000Z"),
      }),
      /claim scope mismatch/,
    );
  });

  await t.test("completion", async (st) => {
    const fixture = await threadFixture(st, [
      { id: "a", title: "Slice A" },
    ]);
    await assignWorker(fixture, "a", "worker-a");
    const claim = await claimSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      idempotencyKey: "cleanup-failure-complete",
      now: new Date(THREAD_START),
    });
    const completed = await workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "cleanup-failure-complete-update",
      action: {
        action: "complete",
        evidence: "Completion remains committed after cleanup failure",
      },
      now: new Date("2026-08-18T12:01:00.000Z"),
    }, {
      deleteClaimSecret: async () => {
        throw new Error("injected cleanup failure");
      },
    });
    assert.equal(completed.slice.status, "done");
    assert.equal(completed.slice.claim, undefined);
    const final = await rehydratePlan(fixture.notePath);
    assert.equal(final.slices[0].status, "done");
    const orphan = await verifyClaimToken({
      vaultPath: fixture.vaultPath,
      planId: fixture.planId,
      sliceId: "a",
      generation: claim.generation,
      workerAgentId: "worker-a",
      token: claim.token,
      claimId: claim.claim_id,
      now: new Date("2026-08-18T12:01:00.000Z"),
    });
    assert.equal(orphan.envelope.claim_id, claim.claim_id);
  });
});

test("restore clears historical claims and advances beyond every old generation", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "historical-claim",
    now: new Date(THREAD_START),
  });
  const claimedSnapshot = await rehydratePlan(fixture.notePath);
  const restored = restorePlan(claimedSnapshot, claimedSnapshot);
  assert.equal(restored.slices[0].claim, undefined);
  assert.ok(restored.slices[0].generation > claim.generation);
  await persistPlan(restored, {
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
  });

  const orphan = await verifyClaimToken({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    generation: claim.generation,
    workerAgentId: "worker-a",
    token: claim.token,
    claimId: claim.claim_id,
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(orphan.envelope.claim_id, claim.claim_id);
  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      action: { action: "start" },
      now: new Date("2026-08-18T12:01:00.000Z"),
    }),
    /claim scope mismatch/,
  );

  await assignSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  const fresh = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "historical-claim",
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.ok(fresh.generation > claim.generation);
  assert.notEqual(fresh.claim_id, claim.claim_id);
  assert.notEqual(fresh.token, claim.token);
});

test("locked orchestrator scar and replan cannot clobber a worker completion", async (t) => {
  for (const operation of ["scar", "replan"]) {
    await t.test(operation, async (st) => {
      const fixture = await threadFixture(st);
      await Promise.all([
        assignWorker(fixture, "a", "worker-a"),
        assignWorker(fixture, "b", "worker-b"),
      ]);
      const claim = await claimSlice({
        ...fixture,
        sliceId: "a",
        workerAgentId: "worker-a",
        idempotencyKey: `worker-a-${operation}`,
        now: new Date(THREAD_START),
      });

      let resolveRead;
      const read = new Promise((resolve) => {
        resolveRead = resolve;
      });
      let releaseMutation;
      const mutationMayCommit = new Promise((resolve) => {
        releaseMutation = resolve;
      });
      const mutation = threadWorkerRuntime.withThreadPlanLock({
        vaultPath: fixture.vaultPath,
        notePath: fixture.notePath,
        planId: fixture.planId,
        operationId: `server-${operation}-regression`,
      }, async (plan) => {
        resolveRead();
        await mutationMayCommit;
        const next = operation === "scar"
          ? addScar(plan, {
              kind: "dead_end",
              signal: "Exact stale unlocked scar reproduction",
            })
          : replan(plan, [
              { id: "a", title: "Slice A" },
              { id: "b", title: "Slice B revised" },
            ]);
        await persistPlan(next, {
          vaultPath: fixture.vaultPath,
          notePath: fixture.notePath,
        });
        return next;
      });
      await read;

      const worker = startBarrierWorker("updateClaimedSlice", {
        ...fixture,
        sliceId: "a",
        workerAgentId: "worker-a",
        token: claim.token,
        idempotencyKey: `worker-complete-${operation}`,
        action: {
          action: "complete",
          evidence: `Worker completion survives concurrent ${operation}`,
        },
        now: "2026-08-18T12:01:00.000Z",
      });
      await worker.ready;
      worker.release();
      await worker.started;
      const completedWhileMutationHeldLock = await Promise.race([
        worker.result.then((result) => result),
        new Promise((resolve) => setTimeout(() => resolve(null), 150)),
      ]);
      assert.ok(
        completedWhileMutationHeldLock && completedWhileMutationHeldLock.accepted,
        "dump-and-return must accept the worker write while replan/scar holds the lock",
      );
      const mid = await rehydratePlan(fixture.notePath);
      assert.equal(
        mid.slices.find((slice) => slice.id === "a").status,
        "pending",
        "accepted is not applied mid-replan",
      );

      releaseMutation();
      const [, workerResult] = await Promise.all([mutation, worker.result]);
      assert.equal(workerResult.ok, true, JSON.stringify(workerResult));
      await waitForWorkerQueue(fixture.vaultPath, fixture.notePath, fixture.planId);
      const final = await rehydratePlan(fixture.notePath);
      assert.equal(final.slices.find((slice) => slice.id === "a").status, "done");
      if (operation === "scar") {
        assert.equal(
          final.scar_tissue.some(
            (scar) => scar.signal === "Exact stale unlocked scar reproduction",
          ),
          true,
        );
      } else {
        assert.equal(
          final.slices.find((slice) => slice.id === "b").title,
          "Slice B revised",
        );
      }
    });
  }
});

test("every production Thread read-modify-write path enters the shared lock", async () => {
  const serverSource = await readFile(
    new URL("../src/server.ts", import.meta.url),
    "utf8",
  );
  const planSource = await readFile(
    new URL("../src/plan.ts", import.meta.url),
    "utf8",
  );
  const handlerBlock = (name) => {
    const start = serverSource.indexOf(`"${name}"`);
    const end = serverSource.indexOf("server.registerTool", start + 1);
    return serverSource.slice(start, end < 0 ? undefined : end);
  };

  for (const name of [
    "minni_thread_update",
    "minni_thread_scar",
    "minni_thread_replan",
  ]) {
    assert.match(
      handlerBlock(name),
      /withThreadPlanLock/,
      `${name} must use the strict lock-before-rehydrate helper`,
    );
  }
  // status shares the locked expiry sweep with events/ready — that helper
  // enters withThreadPlanLock internally; do not require a second direct lock.
  assert.match(
    handlerBlock("minni_thread_status"),
    /synchronizeExpiredClaims/,
    "minni_thread_status must run the shared expiry sweep (lock + rehydrate)",
  );
  assert.match(
    handlerBlock("minni_thread_events"),
    /synchronizeExpiredClaims/,
    "minni_thread_events must run the shared expiry sweep before the cursor read",
  );
  const restoreBlock = handlerBlock("minni_thread_restore");
  assert.match(restoreBlock, /withThreadLock/);
  assert.ok(
    restoreBlock.indexOf("withThreadLock") <
      restoreBlock.indexOf("rehydratePlan(notePath)"),
    "restore must acquire the Thread lock before strict rehydration",
  );
  const resolveViewStart = planSource.indexOf(
    "export async function resolveActivePlanView",
  );
  const resolveViewBlock = planSource.slice(resolveViewStart);
  assert.match(resolveViewBlock, /withThreadLock/);
  assert.ok(
    resolveViewBlock.indexOf("withThreadLock") <
      resolveViewBlock.indexOf("rehydratePlan(active.notePath)"),
    "resolveActivePlanView self-heal must lock before strict rehydration",
  );
});

test("strict duplicate-id rejection prevents a claimed worker from replacing a sibling", async (t) => {
  const fixture = await threadFixture(t);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "duplicate-sibling",
    now: new Date(THREAD_START),
  });
  const canonical = await rehydratePlan(fixture.notePath);
  const duplicate = {
    ...canonical,
    slices: [
      { ...canonical.slices[0] },
      { ...canonical.slices[1], id: "a" },
    ],
  };
  duplicate.plan_digest = computePlanDigest(duplicate);
  const raw = await readFile(fixture.notePath, "utf8");
  const tampered = raw
    .replace(
      /^plan_slices:.*$/m,
      `plan_slices: ${JSON.stringify(duplicate.slices)}`,
    )
    .replace(/^plan_digest:.*$/m, `plan_digest: ${duplicate.plan_digest}`);
  await writeFile(fixture.notePath, tampered, "utf8");

  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      action: {
        action: "complete",
        evidence: "Duplicate sibling must remain untouched by worker update",
      },
      now: new Date("2026-08-18T12:01:00.000Z"),
    }),
    /duplicate slice id "a"/,
  );
  assert.equal(
    await readFile(fixture.notePath, "utf8"),
    tampered,
    "strict rejection must happen before either duplicate is persisted",
  );
});

test("orchestrator slice transitions revoke a claim across terminal reopen", async (t) => {
  const fixture = await threadFixture(t);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "orchestrator-transition",
    now: new Date(THREAD_START),
  });

  const terminal = await threadWorkerRuntime.withThreadPlanLock({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    operationId: "test-orchestrator-terminal",
  }, async (plan) => {
    const applied = threadWorkerRuntime.applyOrchestratorSliceUpdate(
      plan,
      "a",
      "done",
      "Orchestrator terminal transition verified in server path",
    );
    await persistPlan(applied.plan, {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
    });
    return applied;
  });
  assert.equal(terminal.slice.status, "done");
  assert.equal(terminal.slice.claim, undefined);
  assert.equal(terminal.slice.generation, claim.generation + 1);
  assert.equal(terminal.revoked_claim_id, claim.claim_id);

  const reopened = await threadWorkerRuntime.withThreadPlanLock({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    operationId: "test-orchestrator-reopen",
  }, async (plan) => {
    const applied = threadWorkerRuntime.applyOrchestratorSliceUpdate(
      plan,
      "a",
      "pending",
    );
    await persistPlan(applied.plan, {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
    });
    return applied;
  });
  assert.equal(reopened.slice.status, "pending");
  assert.equal(reopened.slice.generation, claim.generation + 1);
  assert.equal(reopened.plan.slices.find((slice) => slice.id === "b").status, "pending");

  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      action: { action: "start" },
      now: new Date("2026-08-18T12:01:00.000Z"),
    }),
    /claim scope mismatch/,
  );

  const serverSource = await readFile(
    new URL("../src/server.ts", import.meta.url),
    "utf8",
  );
  const start = serverSource.indexOf('"minni_thread_update"');
  const end = serverSource.indexOf("server.registerTool", start + 1);
  const block = serverSource.slice(start, end);
  assert.match(block, /applyOrchestratorSliceUpdate/);
  assert.match(block, /persistPlanThenRevokeClaimSecrets/);
});

test("threadWorkerErrorText never serializes PlanDigestVersionError.notePath", () => {
  const notePath = "/tmp/minni-vault/wiki/artifacts/plan-secret-path.md";
  const error = new PlanDigestVersionError(4, notePath);
  const text = threadWorkerErrorText(error);
  assert.match(text, /newer than this plugin/);
  assert.match(text, /v4/);
  assert.equal(error.notePath, notePath);
  assert.equal(text.includes(notePath), false);
  assert.equal(text.includes("wiki/artifacts"), false);
});

test("threadWorkerErrorText redacts notePath from a real rehydratePlan PlanDigestVersionError", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-worker-digest-redact-"));
  try {
    const { ensureVault } = await import("../dist/vault.js");
    await ensureVault(root);
    const created = await createPlan(
      { goal: "Redact a real digest-version notePath", vaultPath: root },
      { vaultPath: root },
    );
    const raw = await readFile(created.write.notePath, "utf8");
    await writeFile(
      created.write.notePath,
      raw.replace(/^plan_digest_v:.*$/m, "plan_digest_v: 4"),
      "utf8",
    );
    const failure = await rehydratePlan(created.write.notePath).then(
      (value) => ({ ok: true, value }),
      (error) => ({ ok: false, error }),
    );
    assert.equal(failure.ok, false);
    assert.ok(failure.error instanceof PlanDigestVersionError);
    const text = threadWorkerErrorText(failure.error);
    assert.match(text, /newer than this plugin/);
    assert.equal(text.includes(created.write.notePath), false);
    assert.equal(text.includes("wiki/artifacts"), false);
    assert.equal(failure.error.notePath, created.write.notePath);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("threadWorkerErrorText never serializes PlanHistoryAppendError.notePath", () => {
  const notePath = "/tmp/minni-vault/wiki/artifacts/plan-secret-path.md";
  const error = new PlanHistoryAppendError(
    notePath,
    9,
    new Error("EISDIR: illegal operation on a directory"),
  );
  const text = threadWorkerErrorText(error);
  assert.equal(
    text,
    "persistPlan: note committed at rev 9, but appending the history snapshot failed: history append failed",
  );
  assert.equal(text.includes(notePath), false);
  assert.equal(text.includes("wiki/artifacts"), false);
});

test("threadWorkerErrorText drops a Node-style history-file path from cause.message", () => {
  const notePath = "/tmp/minni-vault/wiki/artifacts/plan-secret-path.md";
  const historyPath = historyPathFor(notePath);
  const cause = Object.assign(
    new Error(`EISDIR: illegal operation on a directory, open '${historyPath}'`),
    { code: "EISDIR", path: historyPath },
  );
  const text = threadWorkerErrorText(new PlanHistoryAppendError(notePath, 9, cause));
  assert.equal(
    text,
    "persistPlan: note committed at rev 9, but appending the history snapshot failed: EISDIR",
  );
  assert.equal(text.includes(historyPath), false);
  assert.equal(text.includes(notePath), false);
  assert.equal(text.includes("wiki/artifacts"), false);
});

test("threadWorkerErrorText never forwards a Node EISDIR path from an untyped journal error", () => {
  const journalPath = "/tmp/minni-vault/wiki/artifacts/plan-secret.log.md";
  const error = Object.assign(
    new Error(`EISDIR: illegal operation on a directory, open '${journalPath}'`),
    { code: "EISDIR", path: journalPath },
  );
  const text = threadWorkerErrorText(error);
  assert.match(text, /EISDIR/);
  assert.equal(text.includes(journalPath), false);
  assert.equal(text.includes("wiki/artifacts"), false);
});

test("threadWorkerErrorText never forwards a Node EACCES path from an untyped journal error", () => {
  const journalPath = "/tmp/minni-vault/wiki/artifacts/plan-secret.log.md";
  const error = Object.assign(
    new Error(`EACCES: permission denied, open '${journalPath}'`),
    { code: "EACCES", path: journalPath },
  );
  const text = threadWorkerErrorText(error);
  assert.match(text, /EACCES/);
  assert.equal(text.includes(journalPath), false);
  assert.equal(text.includes("wiki/artifacts"), false);
});

test("threadWorkerErrorText drops notePath interpolated into a rehydratePlan Error.message", () => {
  const notePath = "/tmp/minni-vault/wiki/artifacts/plan-secret-path.md";
  const text = threadWorkerErrorText(
    new Error(`rehydratePlan: note ${notePath} missing plan_id in frontmatter`),
  );
  assert.equal(text.includes(notePath), false);
  assert.equal(text.includes("wiki/artifacts"), false);
});

test("threadWorkerErrorText still forwards path-free operational errors", () => {
  assert.equal(
    threadWorkerErrorText(new Error("claim scope mismatch")),
    "claim scope mismatch",
  );
});

test("threadWorkerErrorText forwards THREAD_CURSOR_GAP", () => {
  const gap = new ThreadCursorGapError(1, 4);
  assert.equal(threadWorkerErrorText(gap), gap.message);
  assert.match(threadWorkerErrorText(gap), /unmarked cursor_gap/);
});

test("threadWorkerErrorText never serializes ThreadJournalReadError.journalPath", () => {
  const journalPath = "/tmp/minni-vault/wiki/artifacts/plan-secret.log.md";
  const cause = Object.assign(
    new Error(`EISDIR: illegal operation on a directory, open '${journalPath}'`),
    { code: "EISDIR", path: journalPath },
  );
  const error = new ThreadJournalReadError(journalPath, cause);
  const text = threadWorkerErrorText(error);
  assert.match(text, /unreadable/);
  assert.match(text, /EISDIR/);
  assert.equal(error.journalPath, journalPath);
  assert.equal(text.includes(journalPath), false);
  assert.equal(text.includes("wiki/artifacts"), false);
});

test("threadWorkerErrorText never serializes ThreadJournalAppendError cause paths", () => {
  const journalPath = "/tmp/minni-vault/wiki/artifacts/plan-secret.log.md";
  const cause = Object.assign(
    new Error(`EISDIR: illegal operation on a directory, open '${journalPath}'`),
    { code: "EISDIR", path: journalPath },
  );
  const error = new ThreadJournalAppendError("op-key", "slice.claimed", cause);
  const text = threadWorkerErrorText(error);
  assert.match(text, /append failed/);
  assert.match(text, /EISDIR/);
  assert.equal(error.operationKey, "op-key");
  assert.equal(error.kind, "slice.claimed");
  assert.equal(text.includes(journalPath), false);
  assert.equal(text.includes("wiki/artifacts"), false);
  assert.equal(error.message.includes(journalPath), false);
});

test("prepareThreadMutation does not mint seq=1 onto an unreadable journal", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const plan = await rehydratePlan(fixture.notePath);
  await rm(journalPath, { force: true });
  await mkdir(journalPath);
  await assert.rejects(
    () =>
      prepareThreadMutation(
        {
          vaultPath: fixture.vaultPath,
          notePath: fixture.notePath,
          planId: fixture.planId,
          actor: "test",
        },
        plan,
        THREAD_START,
      ),
    (error) => error instanceof ThreadJournalReadError,
  );
  const st = await stat(journalPath);
  assert.ok(
    st.isDirectory(),
    "unreadable journal path must not be replaced with a seq=1 file",
  );
});

test("threadWorkerErrorText redacts the history path from a real persistPlan EISDIR", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-worker-hist-redact-"));
  try {
    const { ensureVault } = await import("../dist/vault.js");
    await ensureVault(root);
    const created = await createPlan(
      { goal: "Redact a real history-append path", vaultPath: root },
      { vaultPath: root },
    );
    const historyFile = historyPathFor(created.write.notePath);
    await rm(historyFile, { force: true });
    await mkdir(historyFile);
    const failure = await persistPlan(created.plan, {
      vaultPath: root,
      notePath: created.write.notePath,
    }).then(
      (value) => ({ ok: true, value }),
      (error) => ({ ok: false, error }),
    );
    assert.equal(failure.ok, false);
    assert.ok(failure.error instanceof PlanHistoryAppendError);
    const text = threadWorkerErrorText(failure.error);
    assert.match(text, /history snapshot failed/);
    assert.match(text, /EISDIR/);
    assert.equal(text.includes(created.write.notePath), false);
    assert.equal(text.includes(historyFile), false);
    assert.equal(text.includes("wiki/artifacts"), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("claimSlice surfaces a real persistPlan history-append failure, then an identical retry replays the same durable token", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const historyPath = historyPathFor(fixture.notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  const claimInput = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "history-eisdir",
    now: () => new Date("2026-08-18T12:01:00.000Z"),
  };

  // The first call must NOT silently return a success response — the note
  // committed but the history journal for this revision is missing, and
  // that must be visible to the caller, not swallowed.
  const failure = await claimSlice(claimInput).then(
    (value) => ({ ok: true, value }),
    (error) => ({ ok: false, error }),
  );
  assert.equal(failure.ok, false, JSON.stringify(failure));
  assert.ok(
    failure.error instanceof PlanHistoryAppendError,
    `expected PlanHistoryAppendError, got ${failure.error?.constructor?.name}: ${failure.error?.message}`,
  );
  assert.match(failure.error.message, /history snapshot failed/);

  // The note write itself is durable despite the thrown error: the claim,
  // generation, and attempt are already on disk, and the private secret for
  // that durable claim was NOT deleted.
  const durable = await rehydratePlan(fixture.notePath);
  const durableClaim = durable.slices[0].claim;
  assert.ok(durableClaim, "the durable note must already carry the claim");
  assert.equal(durable.slices[0].attempt, 1);

  // Identical idempotency retry must return the SAME usable token against
  // the durable claim — it must not mint a second claim or find the secret
  // missing.
  const retry = await claimSlice(claimInput);
  assert.equal(retry.claim_id, durableClaim.claim_id);
  assert.equal(retry.generation, durable.slices[0].generation);
  assert.equal(retry.worker_agent_id, "worker-a");
  assert.equal(retry.expires_at, durableClaim.expires_at);

  const afterRetry = await rehydratePlan(fixture.notePath);
  assert.equal(afterRetry.slices[0].attempt, 1, "retry must not mint a second attempt");

  const stored = await verifyClaimToken({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    generation: retry.generation,
    workerAgentId: "worker-a",
    token: retry.token,
    claimId: retry.claim_id,
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.equal(stored.envelope.claim_id, retry.claim_id);
});

test("workerUpdate surfaces a real persistPlan history-append failure on complete, then an identical retry replays the same committed result via its receipt", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a-history",
    now: new Date(THREAD_START),
  });

  const historyPath = historyPathFor(fixture.notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  const updateInput = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "complete-a-history-eisdir",
    action: {
      action: "complete",
      evidence: "Completed despite a broken history append",
    },
    now: () => new Date("2026-08-18T12:05:00.000Z"),
  };

  // The first call must NOT silently return a success response — the note
  // committed (including clearing the live claim) but the history journal
  // for this revision is missing, and that must be visible to the caller.
  const failure = await workerUpdate(updateInput).then(
    (value) => ({ ok: true, value }),
    (error) => ({ ok: false, error }),
  );
  assert.equal(failure.ok, false, JSON.stringify(failure));
  assert.ok(
    failure.error instanceof PlanHistoryAppendError,
    `expected PlanHistoryAppendError, got ${failure.error?.constructor?.name}: ${failure.error?.message}`,
  );
  assert.match(failure.error.message, /history snapshot failed/);
  assert.match(failure.error.message, /EISDIR/);
  assert.equal(failure.error.message.includes(historyPath), false);
  assert.equal(failure.error.message.includes("wiki/artifacts"), false);

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(
    durable.slices[0].status,
    "done",
    "the note write itself is durable despite the thrown error",
  );
  assert.equal(
    durable.slices[0].claim,
    undefined,
    "completion already cleared the live claim in the durable note",
  );
  assert.equal(
    await readClaimByIdempotency(
      fixture.vaultPath,
      fixture.planId,
      "a",
      claim.generation,
      "claim-a-history",
    ),
    undefined,
    "durable complete must delete the orphan claim secret even when history append fails",
  );
  await assert.rejects(
    verifyClaimToken({
      vaultPath: fixture.vaultPath,
      planId: fixture.planId,
      sliceId: "a",
      generation: claim.generation,
      workerAgentId: "worker-a",
      token: claim.token,
      claimId: claim.claim_id,
      now: new Date("2026-08-18T12:06:00.000Z"),
    }),
    /claim not found/,
  );

  // Same idempotency key + same token: a live-claim retry is now
  // impossible (completion cleared it), so this can only succeed via the
  // private worker-update receipt written before persist and promoted to
  // committed in the catch block above — the exact crash window the
  // adversarial review flagged.
  const retried = await workerUpdate(updateInput);
  assert.equal(retried.slice.status, "done");
  assert.equal(
    retried.slice.evidence,
    "Completed despite a broken history append",
  );
  assert.equal(
    retried.plan.rev,
    durable.rev,
    "a receipt replay must never appear to advance rev",
  );

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  assert.equal(
    events.filter(
      (event) => event.idempotency_key === "complete-a-history-eisdir",
    ).length,
    0,
    "a history-append failure must never leave a false slice.completed event behind",
  );
  // The receipt hit must still repair the scheduler journal it left behind:
  // the note is ahead of the journal (completion landed, but
  // recordThreadMutationEvents never ran because persist threw first), so
  // the receipt-hit path must run the exact same "note ahead of journal"
  // reconciliation every other locked mutation runs, appending exactly one
  // state.recovered carrying the CURRENT ready summary at the note's own
  // rev — never a fabricated slice.completed or ready.changed.
  const recovered = events.filter((event) => event.kind === "state.recovered");
  assert.equal(
    recovered.length,
    1,
    "exactly one state.recovered must repair the note-ahead-of-journal gap",
  );
  assert.equal(recovered[0].rev, durable.rev);
  assert.deepEqual(recovered[0].payload, { ready: { slices: [] } });
  assert.equal(
    events.some((event) => event.kind === "slice.completed"),
    false,
    "a receipt replay must never mint a fabricated slice.completed event",
  );

  // A second identical retry must add nothing further: the journal is now
  // aligned with the note, so reconciliation is a no-op and the committed
  // receipt keeps replaying the exact same result. Compare via a JSON
  // round trip: the receipt itself is a JSON file, so an explicit
  // `claim: undefined` key on the in-memory slice (never observable over
  // the wire, and dropped by JSON.stringify) must not register as a
  // content difference.
  const retriedAgain = await workerUpdate(updateInput);
  assert.deepEqual(jsonRoundTrip(retriedAgain), jsonRoundTrip(retried));
  const { events: eventsAfterSecondRetry } = await readThreadEvents(
    journalPath,
    0,
    100,
  );
  assert.deepEqual(eventsAfterSecondRetry, events);
});

async function persistPlantedClaim(fixture, sliceId, claim) {
  const plan = await rehydratePlan(fixture.notePath);
  const next = {
    ...plan,
    slices: plan.slices.map((slice) =>
      slice.id === sliceId ? { ...slice, claim } : slice,
    ),
  };
  await persistPlan(next, {
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
  });
  return claim;
}

test("assignSlice deletes the orphan claim secret when history append fails after a durable reassignment", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const planted = await persistPlantedClaim(fixture, "a", {
    claim_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    worker_agent_id: "worker-a",
    claimed_at: THREAD_START.toISOString(),
    expires_at: "2026-08-18T12:10:00.000Z",
  });

  const historyPath = historyPathFor(fixture.notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  const deleted = [];
  const failure = await assignSlice(
    {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-b",
      actorAgentId: TEST_ORCHESTRATOR_ACTOR,
      now: new Date("2026-08-18T12:05:00.000Z"),
    },
    {
      deleteClaimSecret: async ({ claimId }) => {
        deleted.push(claimId);
      },
    },
  ).then(
    (value) => ({ ok: true, value }),
    (error) => ({ ok: false, error }),
  );
  assert.equal(failure.ok, false, JSON.stringify(failure));
  assert.ok(
    failure.error instanceof PlanHistoryAppendError,
    `expected PlanHistoryAppendError, got ${failure.error?.constructor?.name}: ${failure.error?.message}`,
  );
  assert.match(failure.error.message, /history snapshot failed/);
  assert.equal(failure.error.message.includes(historyPath), false);
  assert.equal(failure.error.message.includes("wiki/artifacts"), false);

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(durable.slices[0].assigned_to, "worker-b");
  assert.equal(
    durable.slices[0].claim,
    undefined,
    "reassignment already cleared the live claim in the durable note",
  );
  assert.deepEqual(
    deleted,
    [planted.claim_id],
    "durable reassignment must delete the orphan claim secret even when history append fails",
  );
});

test("lazy expiry deletes the orphan claim secret when history append fails after a durable revoke", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const planted = await persistPlantedClaim(fixture, "a", {
    claim_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    worker_agent_id: "worker-a",
    claimed_at: THREAD_START.toISOString(),
    expires_at: "2026-08-18T12:01:00.000Z",
  });

  const historyPath = historyPathFor(fixture.notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  const deleted = [];
  const { synchronizeExpiredClaimsAndReadReady } = threadWorkerRuntime;
  const failure = await synchronizeExpiredClaimsAndReadReady(
    {
      vaultPath: fixture.vaultPath,
      notePath: fixture.notePath,
      planId: fixture.planId,
      actor: TEST_ORCHESTRATOR_ACTOR,
      now: new Date("2026-08-18T12:02:00.000Z"),
    },
    {
      deleteClaimSecret: async ({ claimId }) => {
        deleted.push(claimId);
      },
    },
  ).then(
    (value) => ({ ok: true, value }),
    (error) => ({ ok: false, error }),
  );
  assert.equal(failure.ok, false, JSON.stringify(failure));
  assert.ok(
    failure.error instanceof PlanHistoryAppendError,
    `expected PlanHistoryAppendError, got ${failure.error?.constructor?.name}: ${failure.error?.message}`,
  );
  assert.match(failure.error.message, /history snapshot failed/);
  assert.equal(failure.error.message.includes(historyPath), false);

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(
    durable.slices[0].claim,
    undefined,
    "expiry already cleared the live claim in the durable note",
  );
  assert.deepEqual(
    deleted,
    [planted.claim_id],
    "durable expiry must delete the orphan claim secret even when history append fails",
  );
});

test("workerUpdate receipt replay on the clean happy path adds no recovery event", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a-happy",
    now: new Date(THREAD_START),
  });

  const updateInput = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "complete-a-happy",
    action: { action: "complete", evidence: "Completed cleanly" },
    now: () => new Date("2026-08-18T12:05:00.000Z"),
  };

  const completed = await workerUpdate(updateInput);
  assert.equal(completed.slice.status, "done");

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const before = await readThreadEvents(journalPath, 0, 100);
  assert.ok(
    before.events.some((event) => event.kind === "slice.completed"),
    "the clean completion itself must record a real slice.completed event",
  );
  assert.equal(
    before.events.filter((event) => event.kind === "state.recovered").length,
    0,
  );

  // The journal is already aligned with the note (recordThreadMutationEvents
  // ran successfully), so a same-key retry's reconciliation step must be a
  // pure no-op: no state.recovered, no other new event, byte-identical
  // response (compared over its JSON wire shape, see jsonRoundTrip above).
  const retried = await workerUpdate(updateInput);
  assert.deepEqual(jsonRoundTrip(retried), jsonRoundTrip(completed));
  const after = await readThreadEvents(journalPath, 0, 100);
  assert.deepEqual(after.events, before.events);
});

test("a committed worker-update receipt still fails thread_inconsistent when the journal is ahead of the note", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a-ahead",
    now: new Date(THREAD_START),
  });

  const updateInput = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "complete-a-ahead",
    action: { action: "complete", evidence: "Completed before journal tampering" },
    now: () => new Date("2026-08-18T12:05:00.000Z"),
  };

  // Establish a real committed receipt via a clean completion first.
  await workerUpdate(updateInput);
  const durable = await rehydratePlan(fixture.notePath);

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  await writeFile(
    journalPath,
    `# Minni Plan Journal\n\n## events\n${JSON.stringify({
      thread_event_batch: [{
        seq: 999,
        rev: durable.rev + 5,
        event_id: "ahead",
        idempotency_key: "ahead",
        actor: "test",
        kind: "slice.completed",
        at: THREAD_START.toISOString(),
      }],
    })}\n`,
    "utf8",
  );

  // A matching committed receipt exists for this exact token/idempotency
  // key, but the strict Thread-lock reconciliation step must still run
  // FIRST and must still fail closed: a receipt can never be trusted to
  // paper over a journal that is ahead of this locked, strict note read.
  await assert.rejects(workerUpdate(updateInput), /thread_inconsistent/);
});

test("upgrade-eligible status reads hold the Thread lock through upgrade persistence", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
  ]);
  const canonical = await rehydratePlan(fixture.notePath);
  const legacyDigest = computePlanDigestHexV2(canonical);
  const raw = await readFile(fixture.notePath, "utf8");
  const legacy = raw
    .replace(/^plan_digest_v:.*\n/m, "")
    .replace(/^plan_digest:.*$/m, `plan_digest: ${legacyDigest}`);
  await writeFile(fixture.notePath, legacy, "utf8");

  let signalUpgradeRead;
  const upgradeRead = new Promise((resolve) => {
    signalUpgradeRead = resolve;
  });
  let releaseUpgradePersist;
  const upgradeMayPersist = new Promise((resolve) => {
    releaseUpgradePersist = resolve;
  });
  const statusRead = threadWorkerRuntime.withThreadPlanLock({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    operationId: "server-status-upgrade-race",
  }, async (plan) => plan, {
    rehydratePlan: (notePath) => rehydratePlan(notePath, {
      beforeUpgradePersist: async () => {
        signalUpgradeRead();
        await upgradeMayPersist;
      },
    }),
  });
  const firstPhase = await Promise.race([
    upgradeRead.then(() => "upgrade-paused"),
    statusRead.then(() => "status-completed"),
  ]);
  assert.equal(
    firstPhase,
    "upgrade-paused",
    "the test seam must pause after stale status read and before upgrade write",
  );

  const assignment = startBarrierWorker("assignSlice", {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    now: "2026-08-18T12:01:00.000Z",
  });
  await assignment.ready;
  assignment.release();
  await assignment.started;
  const assignedInsideStaleWindow = await Promise.race([
    assignment.result.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 150)),
  ]);
  assert.equal(
    assignedInsideStaleWindow,
    false,
    "assignment must not commit between status rehydrate and upgrade persistence",
  );

  releaseUpgradePersist();
  const [, assignmentResult] = await Promise.all([
    statusRead,
    assignment.result,
  ]);
  assert.equal(assignmentResult.ok, true, JSON.stringify(assignmentResult));
  const final = await rehydratePlan(fixture.notePath);
  assert.equal(final.slices[0].assigned_to, "worker-a");
  assert.equal(final.slices[0].generation, 0);
});

test("worker mutations append ordered operation and ready.changed events", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
    { id: "b", title: "Slice B", depends_on: ["a"] },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-a",
    now: new Date(THREAD_START),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "complete-a",
    action: {
      action: "complete",
      evidence: "Slice A verified in thread-worker event test output",
    },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  const kinds = events.map((event) => event.kind);
  assert.ok(kinds.includes("slice.assigned"));
  assert.ok(kinds.includes("slice.claimed"));
  assert.ok(kinds.includes("slice.completed"));
  assert.ok(kinds.includes("ready.changed"));

  const completeKey = clientWorkerKey(
    fixture.planId,
    "a",
    "worker-a",
    "complete-a",
  );
  const readyEvent = events.find(
    (event) => event.idempotency_key === deriveReadyChangedKey(completeKey),
  );
  assert.ok(readyEvent);
  assert.deepEqual(readyEvent.payload, {
    slices: [{ id: "b", title: "Slice B" }],
  });
  const claimKey = clientClaimKey(fixture.planId, "a", "worker-a", "claim-a");
  assert.ok(
    events.some(
      (event) =>
        event.kind === "ready.changed" &&
        event.idempotency_key === deriveReadyChangedKey(claimKey),
    ),
  );
});

test("operation and ready.changed idempotency keys replay exact events", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-exact",
    now: new Date(THREAD_START),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "start-exact",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const before = await readThreadEvents(journalPath, 0, 100);
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "start-exact",
    action: { action: "start" },
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  const after = await readThreadEvents(journalPath, 0, 100);
  assert.deepEqual(after.events, before.events);
  assert.ok(
    after.events.some(
      (event) =>
        event.idempotency_key ===
        clientWorkerKey(fixture.planId, "a", "worker-a", "start-exact"),
    ),
  );
});

test("failed event append is recovered on the next locked mutation", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const planAfterAssign = await rehydratePlan(fixture.notePath);
  const { events: afterAssign } = await readThreadEvents(journalPath, 0, 100);
  assert.ok(afterAssign.some((event) => event.kind === "state.baseline"));
  assert.ok(afterAssign.some((event) => event.kind === "slice.assigned"));

  const bumped = await rehydratePlan(fixture.notePath);
  bumped.next_action = "simulate note-ahead crash gap";
  await persistPlan(bumped, {
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
  });
  const planAhead = await rehydratePlan(fixture.notePath);
  assert.ok(planAhead.rev > planAfterAssign.rev);

  await assignWorker(fixture, "a", "worker-a");
  const { events } = await readThreadEvents(journalPath, 0, 100);
  const recovered = events.find((event) => event.kind === "state.recovered");
  assert.ok(recovered);
  assert.equal(recovered.rev, planAhead.rev);
  assert.ok(events.some((event) => event.kind === "slice.assigned"));
});

test("claim emits ready.changed when the claimed slice leaves ready", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const beforeClaim = await readThreadEvents(journalPath, 0, 100);
  assert.equal(
    beforeClaim.events.some(
      (event) =>
        event.idempotency_key ===
        deriveReadyChangedKey(
          clientClaimKey(fixture.planId, "a", "worker-a", "claim-ready"),
        ),
    ),
    false,
  );

  await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-ready",
    now: new Date(THREAD_START),
  });

  const { events } = await readThreadEvents(journalPath, 0, 100);
  const claimReadyKey = clientClaimKey(
    fixture.planId,
    "a",
    "worker-a",
    "claim-ready",
  );
  const readyEvent = events.find(
    (event) => event.idempotency_key === deriveReadyChangedKey(claimReadyKey),
  );
  assert.ok(readyEvent);
  assert.deepEqual(readyEvent.payload, { slices: [] });
});

test("duplicate worker start does not rev-bump or emit false recovery", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "dup-start-claim",
    now: new Date(THREAD_START),
  });
  const before = await rehydratePlan(fixture.notePath);
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "dup-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  const afterFirst = await rehydratePlan(fixture.notePath);
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "dup-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  const afterSecond = await rehydratePlan(fixture.notePath);
  assert.equal(afterSecond.rev, afterFirst.rev);
  assert.equal(afterSecond.rev, before.rev + 1);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  assert.equal(events.filter((event) => event.kind === "state.recovered").length, 0);
  assert.equal(
    events.filter(
      (event) =>
        event.idempotency_key ===
        clientWorkerKey(fixture.planId, "a", "worker-a", "dup-start"),
    ).length,
    1,
  );
});

// --- Wave 2 residual: note-ahead journal swallow (MED) ------------------------
//
// recordThreadMutationEvents used to catch a failed ordered append and return
// after the note was already durable, so the MCP tool reported OK while the
// mutation's ordered kind was missing until a later mutation wrote
// state.recovered. Pin: persist succeeds, ordered kind missing, tool still OK
// is the hole — success and cursor-moved must be the same moment, or the
// caller gets a typed error. Do not silently hide the gap.

test("assignSlice must not return OK when the ordered slice.assigned append fails to land", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const before = await readThreadEvents(journalPath, 0, 100);

  // Let prepareThreadMutation / baseline land, then fail only the post-persist
  // mutation batch — the note-ahead swallow window.
  const failAfterPrepare = async (filePath, content) => {
    const text = typeof content === "string" ? content : String(content);
    if (text.includes("slice.assigned")) {
      throw new Error("simulated ordered append failure before write");
    }
    return realAppendFileWithFsync(filePath, content);
  };

  const outcome = await assignSlice(
    {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      actorAgentId: TEST_ORCHESTRATOR_ACTOR,
      now: new Date(THREAD_START),
    },
    {
      appendJournalDeps: {
        appendFileWithFsync: failAfterPrepare,
        writeFileAtomic: async () => {
          throw new Error("simulated ordered append failure before write");
        },
      },
    },
  ).then(
    (value) => ({ ok: true, value }),
    (error) => ({ ok: false, error }),
  );

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(
    durable.slices[0].assigned_to,
    "worker-a",
    "note persist still lands — the hole is lying about the cursor, not durability",
  );

  const after = await readThreadEvents(journalPath, 0, 100);
  assert.equal(
    after.events.some((event) => event.kind === "slice.assigned"),
    false,
    "ordered slice.assigned must be missing when the append never landed",
  );

  // Hole today: ok===true with missing ordered kind. Fix: typed error (or
  // same-call repair that lands the kind before returning).
  if (outcome.ok) {
    assert.fail(
      "assignSlice returned OK while slice.assigned is missing from the ordered cursor (note-ahead swallow)",
    );
  }
  assert.equal(
    outcome.error?.code,
    "THREAD_JOURNAL_APPEND_FAILED",
    `expected typed THREAD_JOURNAL_APPEND_FAILED, got ${outcome.error?.name}: ${outcome.error?.message}`,
  );
  // prepareThreadMutation may have advanced the cursor (baseline); the
  // mutation's own kind must still be absent.
  assert.equal(
    after.events.some((event) => event.kind === "slice.assigned"),
    false,
  );
});

test("recordThreadMutationEvents continues when land-then-throw left the operation on disk", async (t) => {
  // Sibling of final-fix-5: write lands, fsync throws, snapshot refreshes —
  // the operation key is present, so the locked mutation may continue.
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  let forcedLandThenThrow = false;
  const landedThenThrows = async (filePath, content) => {
    const text = typeof content === "string" ? content : String(content);
    // Only the post-persist mutation batch (not prepare/baseline).
    if (!forcedLandThenThrow && text.includes("slice.assigned")) {
      forcedLandThenThrow = true;
      await realAppendFileWithFsync(filePath, content);
      throw new Error("simulated fsync failure after write landed");
    }
    return realAppendFileWithFsync(filePath, content);
  };

  const result = await assignSlice(
    {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      actorAgentId: TEST_ORCHESTRATOR_ACTOR,
      now: new Date(THREAD_START),
    },
    {
      appendJournalDeps: {
        appendFileWithFsync: landedThenThrows,
        writeFileAtomic: async () => {
          throw new Error("simulated fsync failure after write landed");
        },
      },
    },
  );
  assert.equal(result.slice.assigned_to, "worker-a");
  assert.equal(forcedLandThenThrow, true, "mutation append must have been forced");

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  assert.ok(
    events.some((event) => event.kind === "slice.assigned"),
    "land-then-throw must still leave slice.assigned on the ordered cursor",
  );
});

test("claim idempotent retry repairs missing slice.claimed and ready delta", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "repair-claim",
    now: new Date(THREAD_START),
  });
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  let raw = await readFile(journalPath, "utf8");
  raw = raw
    .split("\n")
    .filter((line) => !line.includes("repair-claim"))
    .join("\n");
  await writeFile(journalPath, raw, "utf8");

  const replay = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "repair-claim",
    now: new Date(THREAD_START),
  });
  assert.equal(replay.token, claim.token);

  const { events } = await readThreadEvents(journalPath, 0, 100);
  const repairKey = clientClaimKey(
    fixture.planId,
    "a",
    "worker-a",
    "repair-claim",
  );
  assert.ok(events.some((event) => event.idempotency_key === repairKey));
  assert.ok(
    events.some(
      (event) => event.idempotency_key === deriveReadyChangedKey(repairKey),
    ),
  );
});

test("worker paths reject journal-ahead state as thread_inconsistent", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const plan = await rehydratePlan(fixture.notePath);
  await writeFile(
    journalPath,
    `# Minni Plan Journal\n\n## events\n${JSON.stringify({
      thread_event_batch: [{
        seq: 99,
        rev: plan.rev + 5,
        event_id: "ahead",
        idempotency_key: "ahead",
        actor: "test",
        kind: "slice.completed",
        at: THREAD_START.toISOString(),
      }],
    })}\n`,
    "utf8",
  );

  await assert.rejects(
    assignSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      actorAgentId: TEST_ORCHESTRATOR_ACTOR,
      now: new Date(THREAD_START),
    }),
    /thread_inconsistent/,
  );
});

test("assignSlice stamps the caller-supplied orchestrator actor, never the assignment target", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
    { id: "b", title: "Slice B" },
  ]);
  const result = await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-target",
    actorAgentId: "orchestrator-caller",
    now: new Date(THREAD_START),
  });
  assert.equal(result.slice.assigned_to, "worker-target");

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  const assigned = events.find((event) => event.kind === "slice.assigned");
  assert.ok(assigned, "expected a slice.assigned ordered event");
  assert.equal(assigned.actor, "orchestrator-caller");
  assert.notEqual(assigned.actor, "worker-target");

  // The baseline/reconciliation actor recorded alongside the very first
  // ordered mutation must also reflect the caller, not the assignment
  // target — a worker never "acts" on the Thread journal merely by being
  // assigned to it.
  const baseline = events.find((event) => event.kind === "state.baseline");
  assert.ok(baseline, "expected a state.baseline ordered event");
  assert.equal(baseline.actor, "orchestrator-caller");

  // Assignment target/idempotency semantics are unchanged: reassigning the
  // same slice to the SAME worker with the SAME actor is still a no-op
  // idempotency-wise (generation only increments on a genuine reassignment).
  assert.equal(result.slice.generation, 0);
});

test("reassigning the same slice to a different worker with the same orchestrator actor still attributes the caller", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-one",
    actorAgentId: "orchestrator-caller",
    now: new Date(THREAD_START),
  });
  await assignSlice({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-two",
    actorAgentId: "orchestrator-caller",
    now: new Date("2026-08-18T12:01:00.000Z"),
  });

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  const assignedEvents = events.filter((event) => event.kind === "slice.assigned");
  assert.equal(assignedEvents.length, 2);
  for (const event of assignedEvents) {
    assert.equal(event.actor, "orchestrator-caller");
    assert.notEqual(event.actor, "worker-one");
    assert.notEqual(event.actor, "worker-two");
  }
});

test("first ordered mutation writes baseline before operation batch", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const raw = await readFile(journalPath, "utf8");
  const legacyOnly = raw
    .split("\n")
    .filter((line) => !line.includes("thread_event_batch"))
    .join("\n");
  await writeFile(journalPath, legacyOnly, "utf8");

  await assignWorker(fixture, "a", "worker-a");
  const { events } = await readThreadEvents(journalPath, 0, 100);
  const baselineIndex = events.findIndex((event) => event.kind === "state.baseline");
  const assignedIndex = events.findIndex((event) => event.kind === "slice.assigned");
  assert.ok(baselineIndex >= 0);
  assert.ok(assignedIndex > baselineIndex);
});

test("final-fix-2: client claim key cannot squat system status_changed namespace", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const attackKey = `status_changed:${fixture.planId}:a:99`;
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: attackKey,
    now: new Date(THREAD_START),
  });
  assert.ok(claim.token);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  assert.equal(
    events.some((event) => event.idempotency_key === attackKey),
    false,
  );
  assert.ok(
    events.some(
      (event) =>
        event.kind === "slice.claimed" &&
        event.idempotency_key ===
          clientClaimKey(fixture.planId, "a", "worker-a", attackKey),
    ),
  );
});

test("final-fix-2: wrong-token receipt retry does not append state.recovered", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-wrong-token",
    now: new Date(THREAD_START),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "complete-wrong-token",
    action: {
      action: "complete",
      evidence: "Verified in final-fix wrong-token reproduction output",
    },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  const bumped = await rehydratePlan(fixture.notePath);
  bumped.next_action = "note-ahead for wrong-token receipt test";
  await persistPlan(bumped, {
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
  });
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const before = await readThreadEvents(journalPath, 0, 100);
  const recoveredBefore = before.events.filter(
    (event) => event.kind === "state.recovered",
  ).length;

  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: "wrong-token-value-not-the-claim-secret",
      idempotencyKey: "complete-wrong-token",
      action: {
        action: "complete",
        evidence: "Should never land",
      },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /claim token mismatch/,
  );

  const after = await readThreadEvents(journalPath, 0, 100);
  assert.equal(
    after.events.filter((event) => event.kind === "state.recovered").length,
    recoveredBefore,
  );
});

test("final-fix-2: generation-bound receipts replay same-generation complete", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "gen-receipt-claim",
    now: new Date(THREAD_START),
  });
  const done = await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "gen-receipt-complete",
    action: {
      action: "complete",
      evidence: "Generation-bound receipt complete reproduction output",
    },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  const replay = await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "gen-receipt-complete",
    action: {
      action: "complete",
      evidence: "Generation-bound receipt complete reproduction output",
    },
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.equal(replay.slice.status, "done");
  assert.equal(replay.plan.rev, done.plan.rev);
});

test("final-fix-2: reassignment blocks generation-bound receipt replay", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "gen-receipt-claim-2",
    now: new Date(THREAD_START),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "gen-receipt-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  await assignWorker(fixture, "a", "worker-b");
  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "gen-receipt-start",
      action: { action: "start" },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /claim scope mismatch/,
  );
});

test("final-fix-2: replan invalidates generation-bound worker receipt replay", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A", gate: "old gate" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "receipt-replan-claim",
    now: new Date(THREAD_START),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "receipt-replan-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  const current = await rehydratePlan(fixture.notePath);
  const replanned = replan(current, [
    { id: "a", title: "Slice A revised", gate: "new gate" },
  ]);
  await persistPlan(replanned, {
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
  });
  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "receipt-replan-start",
      action: { action: "start" },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /claim scope mismatch/,
  );
});

test("final-fix-2: restore invalidates generation-bound worker receipt replay", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "receipt-restore-claim",
    now: new Date(THREAD_START),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "receipt-restore-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  const claimedSnapshot = await rehydratePlan(fixture.notePath);
  const restored = restorePlan(claimedSnapshot, claimedSnapshot);
  await persistPlan(restored, {
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
  });
  await assert.rejects(
    workerUpdate({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      token: claim.token,
      idempotencyKey: "receipt-restore-start",
      action: { action: "start" },
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    /claim scope mismatch/,
  );
});

test("final-fix-2: lazy expiry on ready emits lease_expired and attention without leaking evidence", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "ready-expiry-claim",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });
  const { synchronizeExpiredClaimsAndReadReady } = threadWorkerRuntime;
  const { plan, ready } = await synchronizeExpiredClaimsAndReadReady({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    actor: TEST_ORCHESTRATOR_ACTOR,
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.ok(ready.some((slice) => slice.id === "a"));
  assert.equal(plan.slices[0].claim, undefined);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  assert.ok(events.some((event) => event.kind === "slice.lease_expired"));
  assert.ok(events.some((event) => event.kind === "thread.attention_required"));
  assert.ok(events.some((event) => event.kind === "ready.changed"));
  for (const event of events) {
    const payload = JSON.stringify(event.payload ?? {});
    assert.doesNotMatch(payload, /token|evidence|\.runtime/i);
  }
});

// Phase-1 residual (2026-08-19): an orchestrator that only polls the event
// cursor / status must still observe dead claims. ready/claim/worker_update
// already run the locked expiry sweep; events + status must share that same
// helper — not invent a second expiry path, and not require a ready poll.
test("shared expiry sweep: events/status path expires without ready/claim/worker_update", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "events-status-expiry-claim",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });

  const before = await rehydratePlan(fixture.notePath);
  assert.ok(before.slices[0].claim, "claim must still be durable before the sweep");

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const beforeCursor = await readThreadEvents(journalPath, 0, 100);
  assert.equal(
    beforeCursor.events.some((event) => event.kind === "slice.lease_expired"),
    false,
    "readThreadEvents alone must not invent expiry — the shared sweep owns that",
  );

  const { synchronizeExpiredClaims } = threadWorkerRuntime;
  assert.equal(
    typeof synchronizeExpiredClaims,
    "function",
    "events/status must share synchronizeExpiredClaims (same helper ready already uses)",
  );

  const laterNow = new Date("2026-08-18T12:02:00.000Z");
  // events-path shape: sweep, then read the ordered cursor
  const swept = await synchronizeExpiredClaims({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    actor: TEST_ORCHESTRATOR_ACTOR,
    now: laterNow,
  });
  assert.equal(
    swept.plan.slices[0].claim,
    undefined,
    "status-path shape: returned plan must show a non-live (cleared) claim",
  );

  const afterCursor = await readThreadEvents(journalPath, beforeCursor.next_seq, 100);
  assert.ok(
    afterCursor.events.some((event) => event.kind === "slice.lease_expired"),
    "ordered cursor must surface slice.lease_expired after the shared sweep",
  );
  assert.ok(
    afterCursor.events.some((event) => event.kind === "thread.attention_required"),
    "ordered cursor must surface thread.attention_required after the shared sweep",
  );

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(durable.slices[0].claim, undefined);
});

test("shared expiry sweep skips journal I/O when no claim needs expiry", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "live-claim-no-journal-touch",
    ttlSeconds: 3600,
    now: new Date(THREAD_START),
  });

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  await rm(journalPath, { force: true });
  await mkdir(journalPath);

  const { synchronizeExpiredClaims } = threadWorkerRuntime;
  // Live claim + unreadable journal: sweep must still return the plan without
  // attempting journal prep (status contract beside EISDIR journals).
  const swept = await synchronizeExpiredClaims({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    actor: TEST_ORCHESTRATOR_ACTOR,
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.ok(swept.plan.slices[0].claim, "live claim must remain");
});

test("ready expiry delegates to the same synchronizeExpiredClaims helper", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "ready-delegates-expiry-claim",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });

  const { synchronizeExpiredClaims, synchronizeExpiredClaimsAndReadReady } =
    threadWorkerRuntime;
  const laterNow = new Date("2026-08-18T12:02:00.000Z");
  const viaShared = await synchronizeExpiredClaims({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    actor: TEST_ORCHESTRATOR_ACTOR,
    now: laterNow,
  });
  assert.equal(viaShared.plan.slices[0].claim, undefined);

  // Second call via ready must be a no-op re-entry of the same sweep (idempotent
  // lease_expired keys), not a divergent expiry implementation.
  const viaReady = await synchronizeExpiredClaimsAndReadReady({
    vaultPath: fixture.vaultPath,
    notePath: fixture.notePath,
    planId: fixture.planId,
    actor: TEST_ORCHESTRATOR_ACTOR,
    now: laterNow,
  });
  assert.equal(viaReady.plan.slices[0].claim, undefined);
  assert.ok(viaReady.ready.some((slice) => slice.id === "a"));

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  assert.equal(
    events.filter((event) => event.kind === "slice.lease_expired").length,
    1,
    "ready must reuse the shared sweep's idempotent expiry, not double-emit",
  );
});

// Phase-1 residual: claim ttl_seconds had no ceiling (Team already clamps at
// MAX_TEAM_TTL_SECONDS). Thread rejects over-cap with a typed error — claim
// validation already rejects non-positive TTL rather than clamping, and a
// silent clamp would lie about the lease the worker thinks it holds.
test("claimSlice rejects ttlSeconds above MAX_THREAD_CLAIM_TTL_SECONDS with a typed error", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const { MAX_THREAD_CLAIM_TTL_SECONDS, ThreadClaimTtlError } = threadWorkerRuntime;
  assert.equal(
    MAX_THREAD_CLAIM_TTL_SECONDS,
    7 * 24 * 3600,
    "Thread claim ceiling matches Team's MAX_TEAM_TTL_SECONDS bound",
  );

  await assert.rejects(
    claimSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      idempotencyKey: "ttl-ceiling-claim",
      ttlSeconds: MAX_THREAD_CLAIM_TTL_SECONDS + 1,
      now: new Date(THREAD_START),
    }),
    (error) =>
      error instanceof ThreadClaimTtlError &&
      error.code === "THREAD_CLAIM_TTL_INVALID" &&
      error.message.includes(String(MAX_THREAD_CLAIM_TTL_SECONDS)),
  );

  const atCap = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "ttl-at-cap-claim",
    ttlSeconds: MAX_THREAD_CLAIM_TTL_SECONDS,
    now: new Date(THREAD_START),
  });
  assert.equal(
    Date.parse(atCap.expires_at),
    THREAD_START.getTime() + MAX_THREAD_CLAIM_TTL_SECONDS * 1000,
  );
});

test("final-fix-2: block and completion lifecycle events are single-shot on retry", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "lifecycle-claim",
    now: new Date(THREAD_START),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "lifecycle-block",
    action: {
      action: "block",
      evidence: "SENTINEL_BLOCK_EVIDENCE_FINAL_FIX_2",
    },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "lifecycle-block",
    action: {
      action: "block",
      evidence: "SENTINEL_BLOCK_EVIDENCE_FINAL_FIX_2",
    },
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  let { events } = await readThreadEvents(journalPath, 0, 100);
  assert.equal(
    events.filter((event) => event.kind === "thread.attention_required").length,
    1,
  );
  assert.ok(
    !JSON.stringify(events).includes("SENTINEL_BLOCK_EVIDENCE_FINAL_FIX_2"),
  );

  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "lifecycle-complete",
    action: {
      action: "complete",
      evidence: "Slice A verified for thread.completed single-shot test",
    },
    now: new Date("2026-08-18T12:03:00.000Z"),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "lifecycle-complete",
    action: {
      action: "complete",
      evidence: "Slice A verified for thread.completed single-shot test",
    },
    now: new Date("2026-08-18T12:04:00.000Z"),
  });
  ({ events } = await readThreadEvents(journalPath, 0, 100));
  assert.equal(
    events.filter((event) => event.kind === "thread.completed").length,
    1,
  );
});

test("final-fix-2: reassignment emits slice.claim_revoked in the ordered journal", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "revoke-claim",
    now: new Date(THREAD_START),
  });
  await assignWorker(fixture, "a", "worker-b");
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  assert.ok(events.some((event) => event.kind === "slice.claim_revoked"));
});

// --- final-fix-4 ------------------------------------------------------------
//
// Regression 1: in-lock sibling expiry must land its events in the SAME
// mutable ordered snapshot the outer claim/update batch allocates seqs from.

test("final-fix-4: claiming slice A durably expires sibling slice B's claim inside the same lock, preserving strictly increasing unique seqs", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
    { id: "b", title: "Slice B" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  await assignWorker(fixture, "b", "worker-b");
  await claimSlice({
    ...fixture,
    sliceId: "b",
    workerAgentId: "worker-b",
    idempotencyKey: "final-fix-4-claim-b-will-expire",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });

  // B's claim will have expired well before A is claimed, forcing the
  // in-lock sibling expiry inside claimSlice("a")'s own locked mutation.
  const laterNow = new Date(THREAD_START.getTime() + 10 * 60_000);
  await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "final-fix-4-claim-a-triggers-sibling-expiry",
    ttlSeconds: 60,
    now: laterNow,
  });

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events: allEvents } = await readThreadEvents(journalPath, 0, 1000);
  assert.ok(allEvents.length > 0);

  const seqs = allEvents.map((event) => event.seq);
  assert.equal(
    new Set(seqs).size,
    seqs.length,
    `expected every seq to be unique, got ${JSON.stringify(seqs)}`,
  );
  for (let index = 1; index < seqs.length; index += 1) {
    assert.ok(
      seqs[index] > seqs[index - 1],
      `seq must strictly increase across the journal: ${seqs[index - 1]} -> ${seqs[index]}`,
    );
  }

  // A cursor walking the journal with limit=1 must surface every event
  // exactly once — a duplicate/non-increasing seq would let this pagination
  // silently skip (hide) an event forever.
  const paged = [];
  let sinceSeq = 0;
  for (let guard = 0; guard < allEvents.length + 5; guard += 1) {
    const { events, next_seq } = await readThreadEvents(journalPath, sinceSeq, 1);
    if (events.length === 0) break;
    paged.push(...events);
    sinceSeq = next_seq;
  }
  assert.deepEqual(
    paged.map((event) => event.event_id),
    allEvents.map((event) => event.event_id),
    "limit=1 pagination must surface every event exactly once, in order",
  );

  const leaseExpired = allEvents.find(
    (event) => event.kind === "slice.lease_expired" && event.slice_id === "b",
  );
  assert.ok(leaseExpired, "expected slice B's lease_expired event to be recorded");
  const claimed = allEvents.find(
    (event) => event.kind === "slice.claimed" && event.slice_id === "a",
  );
  assert.ok(claimed, "expected slice A's claimed event to be recorded");
  assert.ok(
    leaseExpired.seq < claimed.seq,
    "the in-lock sibling expiry must be ordered strictly before the triggering claim event",
  );
});

test("final-fix-4: an in-lock sibling expiry during a worker update also preserves strictly increasing unique seqs", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
    { id: "b", title: "Slice B" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  await assignWorker(fixture, "b", "worker-b");
  const claimA = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "final-fix-4-worker-update-claim-a",
    ttlSeconds: 3600,
    now: new Date(THREAD_START),
  });
  await claimSlice({
    ...fixture,
    sliceId: "b",
    workerAgentId: "worker-b",
    idempotencyKey: "final-fix-4-worker-update-claim-b-will-expire",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });

  const laterNow = new Date(THREAD_START.getTime() + 10 * 60_000);
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claimA.token,
    idempotencyKey: "final-fix-4-worker-update-start-a",
    action: { action: "start" },
    now: laterNow,
  });

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events: allEvents } = await readThreadEvents(journalPath, 0, 1000);
  const seqs = allEvents.map((event) => event.seq);
  assert.equal(new Set(seqs).size, seqs.length, `expected unique seqs, got ${JSON.stringify(seqs)}`);
  for (let index = 1; index < seqs.length; index += 1) {
    assert.ok(seqs[index] > seqs[index - 1], "seq must strictly increase across the journal");
  }
  assert.ok(
    allEvents.some((event) => event.kind === "slice.lease_expired" && event.slice_id === "b"),
  );
  assert.ok(
    allEvents.some((event) => event.kind === "slice.started" && event.slice_id === "a"),
  );
});

// Regression 2: receipt pruning must occur only after a durable generation
// advance — never before persist, and never on a non-committed failure.

test("final-fix-4: a non-committed reassignment persist failure leaves the previous generation's receipts intact for an idempotent same-key worker replay", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "final-fix-4-prune-guard-claim",
    now: new Date(THREAD_START),
  });
  const started = await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "final-fix-4-prune-guard-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });
  assert.equal(started.slice.generation, 0);

  await assert.rejects(
    assignSlice(
      {
        ...fixture,
        sliceId: "a",
        workerAgentId: "worker-b",
        actorAgentId: TEST_ORCHESTRATOR_ACTOR,
        now: new Date("2026-08-18T12:02:00.000Z"),
      },
      {
        persistPlan: async () => {
          throw new Error("injected non-committed reassignment failure");
        },
      },
    ),
    /injected non-committed reassignment failure/,
  );

  const unchanged = await rehydratePlan(fixture.notePath);
  assert.equal(unchanged.slices[0].assigned_to, "worker-a");
  assert.equal(unchanged.slices[0].generation, 0);

  const receipt = await readWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    generation: 0,
    idempotencyKey: "final-fix-4-prune-guard-start",
    claimId: claim.claim_id,
  });
  assert.ok(
    receipt,
    "the generation-0 receipt must survive a non-committed reassignment failure",
  );

  const replay = await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "final-fix-4-prune-guard-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:03:00.000Z"),
  });
  assert.equal(replay.slice.status, "in_progress");
  assert.equal(
    replay.plan.rev,
    started.plan.rev,
    "a same-key replay must never appear to advance rev",
  );
});

test("final-fix-4: a reassignment that commits via a history-append error still prunes the previous generation's receipts once durability is confirmed", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "final-fix-4-prune-commit-claim",
    now: new Date(THREAD_START),
  });
  await workerUpdate({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "final-fix-4-prune-commit-start",
    action: { action: "start" },
    now: new Date("2026-08-18T12:01:00.000Z"),
  });

  const historyPath = historyPathFor(fixture.notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  await assert.rejects(
    assignSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-b",
      actorAgentId: TEST_ORCHESTRATOR_ACTOR,
      now: new Date("2026-08-18T12:02:00.000Z"),
    }),
    (error) => error instanceof PlanHistoryAppendError,
  );

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(durable.slices[0].assigned_to, "worker-b");
  assert.equal(
    durable.slices[0].generation,
    1,
    "the reassignment note write is durable despite the thrown error",
  );

  const receipt = await readWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    generation: 0,
    idempotencyKey: "final-fix-4-prune-commit-start",
    claimId: claim.claim_id,
  });
  assert.equal(
    receipt,
    undefined,
    "generation 0's receipt must be pruned once the reassignment is confirmed durable",
  );
});

test("final-fix-4: a failed reassignment attempt does not cause a real retry to double-bump generation or rev", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const beforeAttempt = await rehydratePlan(fixture.notePath);

  await assert.rejects(
    assignSlice(
      {
        ...fixture,
        sliceId: "a",
        workerAgentId: "worker-b",
        actorAgentId: TEST_ORCHESTRATOR_ACTOR,
        now: new Date("2026-08-18T12:01:00.000Z"),
      },
      {
        persistPlan: async () => {
          throw new Error("injected reassignment failure before retry");
        },
      },
    ),
    /injected reassignment failure before retry/,
  );

  const afterFailure = await rehydratePlan(fixture.notePath);
  assert.deepEqual(afterFailure.slices[0], beforeAttempt.slices[0]);
  assert.equal(afterFailure.rev, beforeAttempt.rev);

  const retried = await assignSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-b",
    actorAgentId: TEST_ORCHESTRATOR_ACTOR,
    now: new Date("2026-08-18T12:02:00.000Z"),
  });
  assert.equal(retried.slice.assigned_to, "worker-b");
  assert.equal(
    retried.slice.generation,
    (beforeAttempt.slices[0].generation ?? 0) + 1,
    "generation must bump exactly once across the failed attempt and the real retry",
  );
  assert.equal(
    retried.plan.rev,
    beforeAttempt.rev + 1,
    "rev must bump exactly once across the failed attempt and the real retry",
  );
});

function finalFix4ClaimId(seed) {
  return createHash("sha256").update(seed).digest("hex").slice(0, 32);
}

test("final-fix-4: claimSlice's response-loss orphan generation-skip leaves that generation's receipts intact when the new claim fails to persist", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const before = await rehydratePlan(fixture.notePath);
  const generation = before.slices[0].generation ?? 0;

  // A response-loss orphan: the secret is durably written, but the
  // corresponding plan update never landed, so slice.claim stays unset.
  await createClaimSecret({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    generation,
    workerAgentId: "worker-a",
    idempotencyKey: "final-fix-4-orphan-persist-guard",
    expiresAt: "2026-08-18T12:00:30.000Z",
    rev: before.rev + 1,
  });

  // A receipt at that same generation that must not be deleted unless the
  // new claim's generation advance is confirmed durable.
  const claimIdForReceipt = finalFix4ClaimId("final-fix-4-orphan-persist-guard-receipt");
  await writePendingWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    claimId: claimIdForReceipt,
    generation,
    idempotencyKey: "final-fix-4-orphan-persist-guard-receipt",
    kind: "slice.started",
    tokenDigest: hashWorkerUpdateToken("final-fix-4-orphan-persist-guard-token"),
    rev: before.rev + 1,
    response: {
      slice: { id: "a", title: "Slice A", status: "in_progress" },
      ready_before: [],
      ready_after: [],
      rev: before.rev + 1,
    },
  });

  await assert.rejects(
    claimSlice(
      {
        ...fixture,
        sliceId: "a",
        workerAgentId: "worker-a",
        idempotencyKey: "final-fix-4-orphan-persist-guard",
        ttlSeconds: 60,
        now: () => new Date("2026-08-18T12:01:00.000Z"),
      },
      {
        persistPlan: async () => {
          throw new Error("injected non-committed claim failure");
        },
      },
    ),
    /injected non-committed claim failure/,
  );

  const receipt = await readWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    generation,
    idempotencyKey: "final-fix-4-orphan-persist-guard-receipt",
    claimId: claimIdForReceipt,
  });
  assert.ok(
    receipt,
    "the orphaned generation's receipt must survive a non-committed claim failure",
  );

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(
    durable.slices[0].generation,
    generation,
    "generation must not have durably advanced",
  );
  assert.equal(durable.slices[0].claim, undefined);
});

test("final-fix-4: claimSlice's response-loss orphan generation-skip prunes that generation's receipts once a history-append error confirms durability", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const before = await rehydratePlan(fixture.notePath);
  const generation = before.slices[0].generation ?? 0;

  await createClaimSecret({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    generation,
    workerAgentId: "worker-a",
    idempotencyKey: "final-fix-4-orphan-commit-guard",
    expiresAt: "2026-08-18T12:00:30.000Z",
    rev: before.rev + 1,
  });

  const claimIdForReceipt = finalFix4ClaimId("final-fix-4-orphan-commit-guard-receipt");
  await writePendingWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    claimId: claimIdForReceipt,
    generation,
    idempotencyKey: "final-fix-4-orphan-commit-guard-receipt",
    kind: "slice.started",
    tokenDigest: hashWorkerUpdateToken("final-fix-4-orphan-commit-guard-token"),
    rev: before.rev + 1,
    response: {
      slice: { id: "a", title: "Slice A", status: "in_progress" },
      ready_before: [],
      ready_after: [],
      rev: before.rev + 1,
    },
  });

  const historyPath = historyPathFor(fixture.notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  await assert.rejects(
    claimSlice({
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      idempotencyKey: "final-fix-4-orphan-commit-guard",
      ttlSeconds: 60,
      now: () => new Date("2026-08-18T12:01:00.000Z"),
    }),
    (error) => error instanceof PlanHistoryAppendError,
  );

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(
    durable.slices[0].generation,
    generation + 1,
    "the new claim's generation advance is durable despite the thrown error",
  );
  assert.ok(durable.slices[0].claim, "the durable note must already carry the new claim");

  const receipt = await readWorkerUpdateReceipt({
    vaultPath: fixture.vaultPath,
    planId: fixture.planId,
    sliceId: "a",
    workerAgentId: "worker-a",
    generation,
    idempotencyKey: "final-fix-4-orphan-commit-guard-receipt",
    claimId: claimIdForReceipt,
  });
  assert.equal(
    receipt,
    undefined,
    "the orphaned generation's receipt must be pruned once the new claim is confirmed durable",
  );
});

test("final-fix-4: workerUpdate surfaces a real persistPlan history-append failure on progress, then an identical retry replays the same committed result", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "final-fix-4-claim-a-progress-history",
    now: new Date(THREAD_START),
  });

  const historyPath = historyPathFor(fixture.notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  const updateInput = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "final-fix-4-progress-a-history-eisdir",
    action: {
      action: "progress",
      evidence: "Made progress despite a broken history append",
    },
    now: () => new Date("2026-08-18T12:05:00.000Z"),
  };

  const failure = await workerUpdate(updateInput).then(
    (value) => ({ ok: true, value }),
    (error) => ({ ok: false, error }),
  );
  assert.equal(failure.ok, false, JSON.stringify(failure));
  assert.ok(
    failure.error instanceof PlanHistoryAppendError,
    `expected PlanHistoryAppendError, got ${failure.error?.constructor?.name}: ${failure.error?.message}`,
  );

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(durable.slices[0].status, "in_progress");
  assert.equal(
    durable.slices[0].evidence,
    "Made progress despite a broken history append",
  );
  assert.ok(durable.slices[0].claim, "progress does not clear the live claim");

  const retried = await workerUpdate(updateInput);
  assert.equal(retried.slice.status, "in_progress");
  assert.equal(
    retried.slice.evidence,
    "Made progress despite a broken history append",
  );
  assert.equal(
    retried.plan.rev,
    durable.rev,
    "a receipt replay must never appear to advance rev",
  );

  // persist threw before recordThreadMutationEvents ever ran on the first
  // attempt, and the retry is satisfied entirely by the committed receipt
  // (no re-entry into recordThreadMutationEvents either) — the ordered
  // journal must never carry a false slice.progressed event for this
  // operation, exactly like the existing "complete" reproduction above.
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events } = await readThreadEvents(journalPath, 0, 100);
  const operationKey = clientWorkerKey(
    fixture.planId,
    "a",
    "worker-a",
    "final-fix-4-progress-a-history-eisdir",
  );
  assert.equal(
    events.filter((event) => event.idempotency_key === operationKey).length,
    0,
    "a history-append failure must never leave a false slice.progressed event behind",
  );
});

test("final-fix-4: workerUpdate surfaces a real persistPlan history-append failure on propose_structure, then an identical retry never duplicates the proposal", async (t) => {
  const fixture = await threadFixture(t, [{ id: "a", title: "Slice A" }]);
  await assignWorker(fixture, "a", "worker-a");
  const claim = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "final-fix-4-claim-a-propose-history",
    now: new Date(THREAD_START),
  });

  const historyPath = historyPathFor(fixture.notePath);
  await rm(historyPath, { force: true });
  await mkdir(historyPath);

  const updateInput = {
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    token: claim.token,
    idempotencyKey: "final-fix-4-propose-a-history-eisdir",
    action: {
      action: "propose_structure",
      proposal: {
        kind: "split",
        reason: "Needs to be split despite a broken history append",
        slices: [{ title: "New sub-slice" }],
      },
    },
    now: () => new Date("2026-08-18T12:05:00.000Z"),
  };

  const failure = await workerUpdate(updateInput).then(
    (value) => ({ ok: true, value }),
    (error) => ({ ok: false, error }),
  );
  assert.equal(failure.ok, false, JSON.stringify(failure));
  assert.ok(
    failure.error instanceof PlanHistoryAppendError,
    `expected PlanHistoryAppendError, got ${failure.error?.constructor?.name}: ${failure.error?.message}`,
  );

  const durable = await rehydratePlan(fixture.notePath);
  assert.equal(
    durable.slices[0].proposals?.length,
    1,
    "the proposal is already durable despite the thrown error",
  );
  assert.ok(durable.slices[0].claim, "propose_structure does not clear the live claim");

  const retried = await workerUpdate(updateInput);
  assert.equal(
    retried.slice.proposals?.length,
    1,
    "a same-key replay must never duplicate the proposal",
  );
  assert.equal(
    retried.plan.rev,
    durable.rev,
    "a receipt replay must never advance rev",
  );
});

// Regression 3: hardened, descriptor-anchored receipt-generation pruning.

test("final-fix-4: parent swap during receipt-generation pruning deletes the real directory and never redirects outside the vault", async (t) => {
  const vaultPath = await mkdtemp(path.join(tmpdir(), "minni-receipt-prune-"));
  t.after(() => rm(vaultPath, { recursive: true, force: true }));
  const planId = "plan-prune-swap";
  const sliceId = "slice-prune-swap";
  const generation = 0;
  const claimId = finalFix4ClaimId("final-fix-4-prune-swap-claim");

  await writePendingWorkerUpdateReceipt({
    vaultPath,
    planId,
    sliceId,
    workerAgentId: "worker-prune-swap",
    claimId,
    generation,
    idempotencyKey: "final-fix-4-prune-swap-key",
    kind: "slice.started",
    tokenDigest: hashWorkerUpdateToken("final-fix-4-prune-swap-token"),
    rev: 1,
    response: {
      slice: { id: sliceId, title: "Slice", status: "in_progress" },
      ready_before: [],
      ready_after: [],
      rev: 1,
    },
  });

  const planHash = createHash("sha256").update(planId).digest("hex").slice(0, 32);
  const sliceHash = createHash("sha256").update(sliceId).digest("hex").slice(0, 32);
  const runtimePath = path.join(vaultPath, ".runtime");
  const movedRuntimePath = path.join(vaultPath, ".runtime-original");
  const generationPath = path.join(
    runtimePath,
    "thread-claims",
    planHash,
    "updates",
    sliceHash,
    `g${generation}`,
  );
  const outside = await mkdtemp(path.join(tmpdir(), "minni-receipt-prune-outside-"));
  t.after(() => rm(outside, { recursive: true, force: true }));

  const before = await readdir(generationPath);
  assert.equal(before.length, 1);

  const originalOpen = fs.promises.open;
  let swapped = false;
  fs.promises.open = async (target, flags, ...args) => {
    if (
      !swapped &&
      String(target).endsWith(`g${generation}`) &&
      (Number(flags) & constants.O_DIRECTORY) !== 0
    ) {
      swapped = true;
      await rename(runtimePath, movedRuntimePath);
      await symlink(outside, runtimePath, "dir");
    }
    return originalOpen(target, flags, ...args);
  };
  syncBuiltinESMExports();

  try {
    await assert.rejects(
      pruneWorkerUpdateReceiptsForGeneration(vaultPath, planId, sliceId, generation),
      /claim store parent changed during operation/,
    );
  } finally {
    fs.promises.open = originalOpen;
    syncBuiltinESMExports();
  }

  assert.equal(swapped, true);
  assert.deepEqual(await readdir(outside), []);
  const originalGenerationPath = path.join(
    movedRuntimePath,
    "thread-claims",
    planHash,
    "updates",
    sliceHash,
    `g${generation}`,
  );
  await assert.rejects(
    readdir(originalGenerationPath),
    (error) => error?.code === "ENOENT",
  );
});

// --- final-fix-5 ------------------------------------------------------------
//
// In-lock sibling expiry can land its journal batch on disk and then throw
// (write succeeded, fsync/follow-up failed). If that error is swallowed while
// the shared orderedSnapshot stays behind the landed write, the outer claim
// batch reallocates those seqs and limit=1 pagination permanently hides the
// colliding expiry event.

test("final-fix-5: claimSlice sibling expiry that lands-then-throws must not let the outer claim reallocate those seqs", async (t) => {
  const fixture = await threadFixture(t, [
    { id: "a", title: "Slice A" },
    { id: "b", title: "Slice B" },
  ]);
  await assignWorker(fixture, "a", "worker-a");
  await assignWorker(fixture, "b", "worker-b");
  await claimSlice({
    ...fixture,
    sliceId: "b",
    workerAgentId: "worker-b",
    idempotencyKey: "final-fix-5-claim-b-will-expire",
    ttlSeconds: 60,
    now: new Date(THREAD_START),
  });

  // First in-lock journal append during claim A is the sibling-expiry batch.
  // Land the line on disk, then throw — matching appendFileWithFsync's
  // write-then-sync failure mode. writeFileAtomic also rejects so the current
  // catch-all cannot "recover" by overwriting and masking the hole.
  let remainingForcedFailures = 1;
  const landedThenThrows = async (filePath, content) => {
    if (remainingForcedFailures > 0) {
      remainingForcedFailures -= 1;
      await realAppendFileWithFsync(filePath, content);
      throw new Error("simulated fsync failure after write landed");
    }
    return realAppendFileWithFsync(filePath, content);
  };

  const laterNow = new Date(THREAD_START.getTime() + 10 * 60_000);
  const claimA = await claimSlice(
    {
      ...fixture,
      sliceId: "a",
      workerAgentId: "worker-a",
      idempotencyKey: "final-fix-5-claim-a-triggers-sibling-expiry",
      ttlSeconds: 60,
      now: laterNow,
    },
    {
      appendJournalDeps: {
        appendFileWithFsync: landedThenThrows,
        writeFileAtomic: async () => {
          throw new Error("simulated fsync failure after write landed");
        },
      },
    },
  );
  assert.ok(claimA?.claim_id, "claim A itself must succeed (or idempotent retry path)");
  assert.ok(claimA?.token, "claim A must return a token");

  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  const { events: allEvents } = await readThreadEvents(journalPath, 0, 1000);
  assert.ok(allEvents.length > 0);

  const seqs = allEvents.map((event) => event.seq);
  assert.equal(
    new Set(seqs).size,
    seqs.length,
    `expected every seq to be unique, got ${JSON.stringify(seqs)}`,
  );
  for (let index = 1; index < seqs.length; index += 1) {
    assert.ok(
      seqs[index] > seqs[index - 1],
      `seq must strictly increase across the journal: ${seqs[index - 1]} -> ${seqs[index]}`,
    );
  }

  const paged = [];
  let sinceSeq = 0;
  for (let guard = 0; guard < allEvents.length + 5; guard += 1) {
    const { events, next_seq } = await readThreadEvents(journalPath, sinceSeq, 1);
    if (events.length === 0) break;
    paged.push(...events);
    sinceSeq = next_seq;
  }
  assert.deepEqual(
    paged.map((event) => event.event_id),
    allEvents.map((event) => event.event_id),
    "limit=1 pagination must surface every event exactly once, in order",
  );

  const leaseExpired = allEvents.find(
    (event) => event.kind === "slice.lease_expired" && event.slice_id === "b",
  );
  assert.ok(leaseExpired, "expected slice B's lease_expired event to be recorded");
  const claimed = allEvents.find(
    (event) => event.kind === "slice.claimed" && event.slice_id === "a",
  );
  assert.ok(claimed, "expected slice A's claimed event to be recorded");
  assert.ok(
    leaseExpired.seq < claimed.seq,
    "the in-lock sibling expiry must be ordered strictly before the triggering claim event",
  );
});

// Stacked on #373: worker/orchestrator poller must see journal_truncated after
// a simulated ordered-journal prefix drop. Silent holes are a fail.
test("worker-side since_seq poller sees journal_truncated after simulated drop", async (t) => {
  const fixture = await threadFixture(t);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);

  await assignSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    actorAgentId: "test-orchestrator",
  });
  const claimed = await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-before-trunc",
    ttlSeconds: 3600,
    now: new Date(THREAD_START),
  });
  assert.ok(claimed.token);

  const before = await readThreadEvents(journalPath, 0, 1000);
  assert.ok(before.events.length >= 2, "assign/claim must leave ordered events");
  const lastDropped = before.events[before.events.length - 2];
  const firstKept = before.events[before.events.length - 1];
  assert.ok(lastDropped.seq + 1 === firstKept.seq);

  const at = THREAD_START.toISOString();
  const truncation = {
    seq: lastDropped.seq,
    rev: lastDropped.rev,
    event_id: `trunc-${lastDropped.seq}`,
    idempotency_key: `system:journal_truncated:${lastDropped.seq}:${firstKept.seq}`,
    actor: "minni",
    kind: "journal_truncated",
    at,
    payload: {
      last_dropped_seq: lastDropped.seq,
      first_kept_seq: firstKept.seq,
    },
  };
  const header = `# Minni Plan Journal\n\n## events\n`;
  await writeFile(
    journalPath,
    header
      + `${JSON.stringify(truncation)}\n`
      + `${JSON.stringify(firstKept)}\n`,
    "utf8",
  );

  // Cursor parked before the drop must observe the hole, not jump silently.
  const sinceSeq = Math.max(0, lastDropped.seq - 1);
  const page = await readThreadEvents(journalPath, sinceSeq, 50);
  assert.equal(page.events[0]?.kind, "journal_truncated");
  assert.deepEqual(page.events[0]?.payload, {
    last_dropped_seq: lastDropped.seq,
    first_kept_seq: firstKept.seq,
  });
  assert.equal(page.events[1]?.seq, firstKept.seq);
  assert.equal(page.events[1]?.event_id, firstKept.event_id);
});

test("worker poller bounded cursor read surfaces journal_truncated for an oversized journal", async (t) => {
  const fixture = await threadFixture(t);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);

  await assignSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    actorAgentId: "test-orchestrator",
  });
  await claimSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    idempotencyKey: "claim-before-bound",
    ttlSeconds: 3600,
    now: new Date(THREAD_START),
  });

  const before = await readThreadEvents(journalPath, 0, 1000);
  const at = THREAD_START.toISOString();
  const rev = before.events.at(-1)?.rev ?? 1;
  let seq = before.events.reduce((max, event) => Math.max(max, event.seq), 0);
  const pad = "y".repeat(180);
  const extras = [];
  for (let i = 0; i < 35; i += 1) {
    seq += 1;
    extras.push(JSON.stringify({
      seq,
      rev,
      event_id: `pad-${seq}`,
      idempotency_key: `pad-${seq}`,
      actor: "test",
      kind: "test.pad",
      at,
      payload: { pad },
    }));
  }
  const existing = await readFile(journalPath, "utf8");
  const appended = `${extras.join("\n")}\n`;
  await writeFile(journalPath, `${existing}${appended}`, "utf8");
  const fileSize = Buffer.byteLength(existing) + Buffer.byteLength(appended);
  const maxReadBytes = Math.min(2_800, Math.floor(fileSize / 4));
  assert.ok(maxReadBytes < fileSize);

  const page = await readThreadEvents(journalPath, 0, 80, { maxReadBytes });
  assert.ok(
    page.events[0]?.kind === "journal_truncated" || page.events[0]?.kind === "cursor_gap",
    `expected cursor gap kind, got ${page.events[0]?.kind}`,
  );
  assert.equal(
    page.events[0]?.payload?.last_dropped_seq,
    page.events[0]?.payload?.first_kept_seq - 1,
  );
  assert.ok(page.events[0].payload.first_kept_seq > 1);
  assert.equal(page.events[1]?.seq, page.events[0].payload.first_kept_seq);
  assert.equal(
    threadWorkerErrorText(new ThreadCursorGapError(0, page.events[0].payload.first_kept_seq)),
    new ThreadCursorGapError(0, page.events[0].payload.first_kept_seq).message,
  );
});

test("worker poller tail bound does not hide an unmarked leading hole", async (t) => {
  const fixture = await threadFixture(t);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  await assignSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    actorAgentId: "test-orchestrator",
  });

  const at = THREAD_START.toISOString();
  const header = `# Minni Plan Journal\n\n## events\n`;
  const pad = "y".repeat(180);
  const extras = [];
  for (let seq = 10; seq <= 45; seq += 1) {
    extras.push(JSON.stringify({
      seq,
      rev: 1,
      event_id: `pad-${seq}`,
      idempotency_key: `pad-${seq}`,
      actor: "test",
      kind: "test.pad",
      at,
      payload: { pad },
    }));
  }
  const text = `${header}${extras.join("\n")}\n`;
  await writeFile(journalPath, text, "utf8");
  const fileSize = Buffer.byteLength(text);
  const maxReadBytes = Math.min(2_800, Math.floor(fileSize / 4));
  assert.ok(maxReadBytes < fileSize);

  await assert.rejects(
    () => readThreadEvents(journalPath, 0, 80, { maxReadBytes }),
    (error) => {
      assert.equal(error?.code, "THREAD_CURSOR_GAP");
      assert.equal(
        threadWorkerErrorText(error),
        error.message,
      );
      return true;
    },
  );
});

test("worker-side since_seq poller fails closed on an unmarked seq hole", async (t) => {
  const fixture = await threadFixture(t);
  const journalPath = journalPathFor(fixture.notePath, fixture.planId);
  await assignSlice({
    ...fixture,
    sliceId: "a",
    workerAgentId: "worker-a",
    actorAgentId: "test-orchestrator",
  });
  const before = await readThreadEvents(journalPath, 0, 1000);
  const firstKept = before.events.at(-1);
  assert.ok(firstKept);
  const header = `# Minni Plan Journal\n\n## events\n`;
  await writeFile(journalPath, `${header}${JSON.stringify(firstKept)}\n`, "utf8");
  // firstKept.seq > 1 after assign (baseline + assigned). Drop everything
  // before it with no journal_truncated marker.
  assert.ok(firstKept.seq > 1);
  await assert.rejects(
    () => readThreadEvents(journalPath, 0, 50),
    (error) => {
      assert.equal(error?.code, "THREAD_CURSOR_GAP");
      return true;
    },
  );
});

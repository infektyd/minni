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
  PlanHistoryAppendError,
  rehydratePlan,
  replan,
  restorePlan,
} from "../dist/plan.js";
import { readThreadEvents } from "../dist/thread-events.js";
import {
  createClaimSecret,
  deleteClaimSecret,
  readClaimByIdempotency,
  verifyClaimToken,
} from "../dist/thread-claims.js";
import {
  assignSlice,
  claimSlice,
  readySlices,
  updateClaimedSlice as updateClaimedSliceImpl,
} from "../dist/thread-worker.js";
import * as threadWorkerRuntime from "../dist/thread-worker.js";
import { withThreadLock } from "../dist/thread-lock.js";

const BEFORE_EXPIRY = new Date("2026-08-18T14:59:00.000Z");
const AT_EXPIRY = new Date("2026-08-18T15:00:00.000Z");
const execFileAsync = promisify(execFile);
const THREAD_START = new Date("2026-08-18T12:00:00.000Z");
const THREAD_WORKER_MODULE_URL = new URL(
  "../dist/thread-worker.js",
  import.meta.url,
).href;

function jsonRoundTrip(value) {
  return JSON.parse(JSON.stringify(value));
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
      process.stdout.write(JSON.stringify({ phase: "result", ok: true, value }) + "\\n");
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
  assert.equal(results.filter((result) => result.ok).length, 1, JSON.stringify(results));
  const final = await rehydratePlan(fixture.notePath);
  const completionWon = results[0].ok;
  if (completionWon) {
    assert.equal(final.slices[0].status, "done");
    assert.equal(final.slices[0].claim, undefined);
    assert.match(results[1].error, /not claimable|claim scope mismatch/);
  } else {
    assert.equal(final.slices[0].status, "pending");
    assert.equal(final.slices[0].attempt, 2);
    assert.equal(
      final.slices[0].claim.claim_id,
      results[1].value.claim_id,
    );
    assert.match(results[0].error, /claim token mismatch|claim scope mismatch/);
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
  assert.deepEqual(rejected.map((result) => result.ok), [false, false]);
  assert.match(rejected[0].error, /claim scope mismatch/);
  assert.match(rejected[1].error, /unsupported worker action/);

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
  assert.equal(updateClockSamples, 1);
  assert.equal(update.ok, false);
  assert.match(update.error.message, /claim expired/);
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
        worker.result.then(() => true),
        new Promise((resolve) => setTimeout(() => resolve(false), 150)),
      ]);
      assert.equal(
        completedWhileMutationHeldLock,
        false,
        "worker must remain blocked after the orchestrator read and before its commit",
      );

      releaseMutation();
      const [, workerResult] = await Promise.all([mutation, worker.result]);
      assert.equal(workerResult.ok, true, JSON.stringify(workerResult));
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
    "minni_thread_status",
    "minni_thread_replan",
  ]) {
    assert.match(
      handlerBlock(name),
      /withThreadPlanLock/,
      `${name} must use the strict lock-before-rehydrate helper`,
    );
  }
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
  assert.match(block, /deleteClaimSecretsBestEffort/);
  assert.ok(
    block.indexOf("persistPlan") < block.indexOf("deleteClaimSecretsBestEffort"),
    "server update must persist revoked metadata before best-effort cleanup",
  );
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

  const readyEvent = events.find(
    (event) => event.idempotency_key === "complete-a:ready",
  );
  assert.ok(readyEvent);
  assert.deepEqual(readyEvent.payload, {
    slices: [{ id: "b", title: "Slice B" }],
  });
  assert.ok(
    events.some(
      (event) =>
        event.kind === "ready.changed" &&
        event.idempotency_key === "claim-a:ready",
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
  assert.ok(after.events.some((event) => event.idempotency_key === "start-exact"));
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
    beforeClaim.events.some((event) => event.idempotency_key === "claim-ready:ready"),
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
  const readyEvent = events.find(
    (event) => event.idempotency_key === "claim-ready:ready",
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
    events.filter((event) => event.idempotency_key === "dup-start").length,
    1,
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
  assert.ok(events.some((event) => event.idempotency_key === "repair-claim"));
  assert.ok(
    events.some((event) => event.idempotency_key === "repair-claim:ready"),
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

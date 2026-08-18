import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { stableStringify } from "../dist/agent_envelope.js";
import {
  createClaimSecret,
  deleteClaimSecret,
  readClaimByIdempotency,
  verifyClaimToken,
} from "../dist/thread-claims.js";

const BEFORE_EXPIRY = new Date("2026-08-18T14:59:00.000Z");
const AT_EXPIRY = new Date("2026-08-18T15:00:00.000Z");

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

test("claim envelope is mode-0600 under the private non-Markdown runtime tree", async (t) => {
  const input = await claimFixture(t, {
    planId: "../plan/private",
    sliceId: "../../slice/private",
    idempotencyKey: "../retry/private",
  });
  const claim = await createClaimSecret(input);

  assert.equal((await stat(claim.filePath)).mode & 0o777, 0o600);
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

test("claim store refuses symlink escapes from the vault runtime tree", async (t) => {
  for (const escapedSegment of [".runtime", "thread-claims"]) {
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
        await mkdir(path.join(vaultPath, ".runtime"));
        await symlink(
          outside,
          path.join(vaultPath, ".runtime", "thread-claims"),
          "dir",
        );
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

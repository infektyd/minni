import {
  createHash,
  randomBytes,
  randomUUID,
  timingSafeEqual,
} from "node:crypto";
import { constants } from "node:fs";
import {
  chmod,
  lstat,
  mkdir,
  open,
  realpath,
  rename,
  unlink,
} from "node:fs/promises";
import path from "node:path";

import { stableStringify } from "./agent_envelope.js";
import { withThreadLock } from "./thread-lock.js";

const CLAIM_SCHEMA = "minni.thread-claim.v1" as const;
const CLAIM_ID_PATTERN = /^[0-9a-f]{32}$/;
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const MAX_ENVELOPE_BYTES = 64 * 1024;
const ENVELOPE_KEYS = [
  "claim_id",
  "expires_at",
  "generation",
  "idempotency_key",
  "plan_id",
  "response",
  "schema",
  "slice_id",
  "token",
  "worker_agent_id",
] as const;
const RESPONSE_KEYS = [
  "claim_id",
  "expires_at",
  "generation",
  "plan_id",
  "rev",
  "slice_id",
  "token",
  "worker_agent_id",
] as const;

export interface ClaimSecretEnvelope {
  schema: "minni.thread-claim.v1";
  plan_id: string;
  slice_id: string;
  claim_id: string;
  generation: number;
  worker_agent_id: string;
  idempotency_key: string;
  token: string;
  expires_at: string;
  response: ThreadClaimResponse;
}

export interface ThreadClaimResponse {
  plan_id: string;
  slice_id: string;
  claim_id: string;
  generation: number;
  worker_agent_id: string;
  token: string;
  expires_at: string;
  rev: number;
}

/**
 * Internal persistence result. `filePath` is intentionally absent from
 * ThreadClaimResponse, which is the response shape safe for an MCP caller.
 */
export interface StoredClaimSecret {
  envelope: ClaimSecretEnvelope;
  filePath: string;
}

export interface CreateClaimSecretInput {
  vaultPath: string;
  planId: string;
  sliceId: string;
  generation: number;
  workerAgentId: string;
  idempotencyKey: string;
  expiresAt: string;
  rev: number;
}

export interface VerifyClaimTokenInput {
  vaultPath: string;
  planId: string;
  sliceId: string;
  generation: number;
  workerAgentId: string;
  token: string;
  now?: Date;
  claimId?: string;
  idempotencyKey?: string;
}

export interface DeleteClaimSecretInput {
  vaultPath: string;
  planId: string;
  claimId: string;
}

interface ClaimLocation {
  vaultRoot: string;
  claimsRoot: string;
  planDir: string;
  filePath: string;
}

interface ExpectedClaimIdentity {
  planId: string;
  claimId: string;
  sliceId?: string;
  generation?: number;
  workerAgentId?: string;
  idempotencyKey?: string;
}

function isErrno(error: unknown, code: string): boolean {
  return (error as NodeJS.ErrnoException).code === code;
}

function pathMismatch(): Error {
  return new Error("claim store path mismatch");
}

function metadataMismatch(): Error {
  return new Error("claim metadata mismatch");
}

function requireNonEmpty(value: string, label: string): void {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`claim requires non-empty ${label}`);
  }
}

function requireNonNegativeInteger(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`claim ${label} must be a non-negative safe integer`);
  }
}

function requireIsoTimestamp(value: string, label: string): void {
  if (
    typeof value !== "string" ||
    !Number.isFinite(Date.parse(value)) ||
    new Date(value).toISOString() !== value
  ) {
    throw new Error(`claim ${label} must be an ISO-8601 timestamp`);
  }
}

function assertContained(candidate: string, root: string): void {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw pathMismatch();
  }
}

function hashSegment(value: string): string {
  return createHash("sha256").update(value).digest("hex").slice(0, 32);
}

function claimIdFor(
  planId: string,
  sliceId: string,
  generation: number,
  idempotencyKey: string,
): string {
  return createHash("sha256")
    .update(stableStringify({
      plan_id: planId,
      slice_id: sliceId,
      generation,
      idempotency_key: idempotencyKey,
    }))
    .digest("hex")
    .slice(0, 32);
}

async function secureDirectory(
  directory: string,
  vaultRoot: string,
  create: boolean,
  makePrivate: boolean,
): Promise<boolean> {
  let directoryStat;
  try {
    directoryStat = await lstat(directory);
  } catch (error) {
    if (!isErrno(error, "ENOENT")) throw error;
    if (!create) return false;
    try {
      await mkdir(directory, { mode: makePrivate ? 0o700 : 0o755 });
    } catch (mkdirError) {
      if (!isErrno(mkdirError, "EEXIST")) throw mkdirError;
    }
    directoryStat = await lstat(directory);
  }

  if (directoryStat.isSymbolicLink() || !directoryStat.isDirectory()) {
    throw pathMismatch();
  }

  const canonical = await realpath(directory);
  if (canonical !== path.resolve(directory)) {
    throw pathMismatch();
  }
  assertContained(canonical, vaultRoot);

  if (makePrivate) {
    await chmod(canonical, 0o700);
    const privateStat = await lstat(canonical);
    if ((privateStat.mode & 0o777) !== 0o700) {
      throw new Error("claim store permissions mismatch");
    }
  }
  return true;
}

async function claimLocation(
  vaultPath: string,
  planId: string,
  claimId: string,
  create: boolean,
): Promise<ClaimLocation | undefined> {
  requireNonEmpty(vaultPath, "vault path");
  requireNonEmpty(planId, "plan id");
  if (!CLAIM_ID_PATTERN.test(claimId)) {
    throw pathMismatch();
  }

  const vaultRoot = await realpath(path.resolve(vaultPath));
  const vaultStat = await lstat(vaultRoot);
  if (!vaultStat.isDirectory() || vaultStat.isSymbolicLink()) {
    throw pathMismatch();
  }

  const runtimeRoot = path.join(vaultRoot, ".runtime");
  if (!(await secureDirectory(runtimeRoot, vaultRoot, create, false))) {
    return undefined;
  }
  const claimsRoot = path.join(runtimeRoot, "thread-claims");
  if (!(await secureDirectory(claimsRoot, vaultRoot, create, true))) {
    return undefined;
  }
  const planDir = path.join(claimsRoot, hashSegment(planId));
  if (!(await secureDirectory(planDir, vaultRoot, create, true))) {
    return undefined;
  }

  const filePath = path.join(planDir, `${claimId}.json`);
  assertContained(filePath, claimsRoot);
  return { vaultRoot, claimsRoot, planDir, filePath };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  const actual = Object.keys(value).sort();
  return (
    actual.length === expected.length &&
    actual.every((key, index) => key === expected[index])
  );
}

function parseEnvelope(
  raw: string,
  expected: ExpectedClaimIdentity,
): ClaimSecretEnvelope {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw metadataMismatch();
  }
  if (!isRecord(value) || !isRecord(value.response)) {
    throw metadataMismatch();
  }

  const response = value.response;
  if (
    !hasExactKeys(value, ENVELOPE_KEYS) ||
    !hasExactKeys(response, RESPONSE_KEYS) ||
    value.schema !== CLAIM_SCHEMA ||
    typeof value.plan_id !== "string" ||
    value.plan_id.trim().length === 0 ||
    typeof value.slice_id !== "string" ||
    value.slice_id.trim().length === 0 ||
    typeof value.claim_id !== "string" ||
    !Number.isSafeInteger(value.generation) ||
    (value.generation as number) < 0 ||
    typeof value.worker_agent_id !== "string" ||
    value.worker_agent_id.trim().length === 0 ||
    typeof value.idempotency_key !== "string" ||
    value.idempotency_key.trim().length === 0 ||
    typeof value.token !== "string" ||
    !TOKEN_PATTERN.test(value.token) ||
    typeof value.expires_at !== "string" ||
    !Number.isFinite(Date.parse(value.expires_at)) ||
    new Date(value.expires_at).toISOString() !== value.expires_at ||
    typeof response.plan_id !== "string" ||
    typeof response.slice_id !== "string" ||
    typeof response.claim_id !== "string" ||
    !Number.isSafeInteger(response.generation) ||
    (response.generation as number) < 0 ||
    typeof response.worker_agent_id !== "string" ||
    typeof response.token !== "string" ||
    typeof response.expires_at !== "string" ||
    !Number.isSafeInteger(response.rev) ||
    (response.rev as number) < 0
  ) {
    throw metadataMismatch();
  }

  const envelope = value as unknown as ClaimSecretEnvelope;
  const derivedClaimId = claimIdFor(
    envelope.plan_id,
    envelope.slice_id,
    envelope.generation,
    envelope.idempotency_key,
  );
  if (
    envelope.claim_id !== derivedClaimId ||
    envelope.plan_id !== expected.planId ||
    envelope.claim_id !== expected.claimId ||
    (expected.sliceId !== undefined &&
      envelope.slice_id !== expected.sliceId) ||
    (expected.generation !== undefined &&
      envelope.generation !== expected.generation) ||
    (expected.workerAgentId !== undefined &&
      envelope.worker_agent_id !== expected.workerAgentId) ||
    (expected.idempotencyKey !== undefined &&
      envelope.idempotency_key !== expected.idempotencyKey) ||
    envelope.response.plan_id !== envelope.plan_id ||
    envelope.response.slice_id !== envelope.slice_id ||
    envelope.response.claim_id !== envelope.claim_id ||
    envelope.response.generation !== envelope.generation ||
    envelope.response.worker_agent_id !== envelope.worker_agent_id ||
    envelope.response.token !== envelope.token ||
    envelope.response.expires_at !== envelope.expires_at
  ) {
    throw metadataMismatch();
  }
  return envelope;
}

async function readEnvelope(
  location: ClaimLocation | undefined,
  expected: ExpectedClaimIdentity,
): Promise<ClaimSecretEnvelope | undefined> {
  if (!location) return undefined;

  let handle;
  try {
    handle = await open(
      location.filePath,
      constants.O_RDONLY | constants.O_NOFOLLOW,
    );
  } catch (error) {
    if (isErrno(error, "ENOENT")) return undefined;
    if (isErrno(error, "ELOOP")) throw pathMismatch();
    throw error;
  }

  try {
    const fileStat = await handle.stat();
    if (!fileStat.isFile() || fileStat.nlink !== 1) {
      throw pathMismatch();
    }
    if ((fileStat.mode & 0o777) !== 0o600) {
      throw new Error("claim secret permissions mismatch");
    }
    if (fileStat.size > MAX_ENVELOPE_BYTES) {
      throw metadataMismatch();
    }
    const raw = await handle.readFile("utf8");
    const afterReadStat = await handle.stat();
    if (
      !afterReadStat.isFile() ||
      afterReadStat.nlink !== 1 ||
      (afterReadStat.mode & 0o777) !== 0o600
    ) {
      throw new Error("claim secret permissions mismatch");
    }
    return parseEnvelope(raw, expected);
  } finally {
    await handle.close();
  }
}

async function syncDirectory(directory: string): Promise<void> {
  const handle = await open(
    directory,
    constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
  );
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function writeEnvelopeAtomic(
  location: ClaimLocation,
  envelope: ClaimSecretEnvelope,
): Promise<void> {
  const temporaryPath = path.join(
    location.planDir,
    `.${envelope.claim_id}.${randomBytes(16).toString("hex")}.tmp`,
  );
  assertContained(temporaryPath, location.claimsRoot);

  let handle;
  try {
    handle = await open(
      temporaryPath,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        constants.O_NOFOLLOW,
      0o600,
    );
    await handle.chmod(0o600);
    await handle.writeFile(`${JSON.stringify(envelope)}\n`, "utf8");
    await handle.sync();
    await handle.close();
    handle = undefined;

    try {
      await lstat(location.filePath);
      throw metadataMismatch();
    } catch (error) {
      if (!isErrno(error, "ENOENT")) throw error;
    }

    await rename(temporaryPath, location.filePath);
    await syncDirectory(location.planDir);
  } finally {
    if (handle) await handle.close().catch(() => {});
    await unlink(temporaryPath).catch((error) => {
      if (!isErrno(error, "ENOENT")) throw error;
    });
  }
}

async function withClaimLock<T>(
  vaultPath: string,
  planId: string,
  claimId: string,
  fn: (location: ClaimLocation) => Promise<T>,
): Promise<T> {
  const initialLocation = await claimLocation(vaultPath, planId, claimId, true);
  if (!initialLocation) throw pathMismatch();
  return withThreadLock(
    initialLocation.vaultRoot,
    `claim:${hashSegment(planId)}:${claimId}`,
    randomUUID(),
    async () => {
      const lockedLocation = await claimLocation(
        initialLocation.vaultRoot,
        planId,
        claimId,
        true,
      );
      if (!lockedLocation) throw pathMismatch();
      return fn(lockedLocation);
    },
  );
}

function validateCreateInput(input: CreateClaimSecretInput): string {
  requireNonEmpty(input.planId, "plan id");
  requireNonEmpty(input.sliceId, "slice id");
  requireNonEmpty(input.workerAgentId, "worker agent id");
  requireNonEmpty(input.idempotencyKey, "idempotency key");
  requireNonNegativeInteger(input.generation, "generation");
  requireNonNegativeInteger(input.rev, "revision");
  requireIsoTimestamp(input.expiresAt, "expiry");
  return claimIdFor(
    input.planId,
    input.sliceId,
    input.generation,
    input.idempotencyKey,
  );
}

export async function readClaimByIdempotency(
  vaultPath: string,
  planId: string,
  sliceId: string,
  generation: number,
  idempotencyKey: string,
): Promise<ClaimSecretEnvelope | undefined> {
  requireNonEmpty(planId, "plan id");
  requireNonEmpty(sliceId, "slice id");
  requireNonEmpty(idempotencyKey, "idempotency key");
  requireNonNegativeInteger(generation, "generation");
  const claimId = claimIdFor(planId, sliceId, generation, idempotencyKey);
  const location = await claimLocation(vaultPath, planId, claimId, false);
  return readEnvelope(location, {
    planId,
    sliceId,
    claimId,
    generation,
    idempotencyKey,
  });
}

export async function createClaimSecret(
  input: CreateClaimSecretInput,
): Promise<StoredClaimSecret> {
  const claimId = validateCreateInput(input);
  return withClaimLock(
    input.vaultPath,
    input.planId,
    claimId,
    async (location) => {
      const expected: ExpectedClaimIdentity = {
        planId: input.planId,
        sliceId: input.sliceId,
        claimId,
        generation: input.generation,
        workerAgentId: input.workerAgentId,
        idempotencyKey: input.idempotencyKey,
      };
      const existing = await readEnvelope(location, expected);
      if (existing) {
        return { envelope: existing, filePath: location.filePath };
      }

      const token = randomBytes(32).toString("base64url");
      const response: ThreadClaimResponse = {
        plan_id: input.planId,
        slice_id: input.sliceId,
        claim_id: claimId,
        generation: input.generation,
        worker_agent_id: input.workerAgentId,
        token,
        expires_at: input.expiresAt,
        rev: input.rev,
      };
      const envelope: ClaimSecretEnvelope = {
        schema: CLAIM_SCHEMA,
        plan_id: input.planId,
        slice_id: input.sliceId,
        claim_id: claimId,
        generation: input.generation,
        worker_agent_id: input.workerAgentId,
        idempotency_key: input.idempotencyKey,
        token,
        expires_at: input.expiresAt,
        response,
      };

      await writeEnvelopeAtomic(location, envelope);
      const stored = await readEnvelope(location, expected);
      if (!stored) throw metadataMismatch();
      return { envelope: stored, filePath: location.filePath };
    },
  );
}

export async function verifyClaimToken(
  input: VerifyClaimTokenInput,
): Promise<StoredClaimSecret> {
  requireNonEmpty(input.planId, "plan id");
  requireNonEmpty(input.sliceId, "slice id");
  requireNonEmpty(input.workerAgentId, "worker agent id");
  requireNonEmpty(input.token, "token");
  requireNonNegativeInteger(input.generation, "generation");

  let claimId = input.claimId;
  if (claimId !== undefined && !CLAIM_ID_PATTERN.test(claimId)) {
    throw pathMismatch();
  }
  if (input.idempotencyKey !== undefined) {
    requireNonEmpty(input.idempotencyKey, "idempotency key");
    const derived = claimIdFor(
      input.planId,
      input.sliceId,
      input.generation,
      input.idempotencyKey,
    );
    if (claimId !== undefined && claimId !== derived) {
      throw new Error("claim path/metadata mismatch");
    }
    claimId = derived;
  }
  if (!claimId) {
    throw new Error("claim verification requires claim id or idempotency key");
  }

  const location = await claimLocation(
    input.vaultPath,
    input.planId,
    claimId,
    false,
  );
  const envelope = await readEnvelope(location, {
    planId: input.planId,
    sliceId: input.sliceId,
    claimId,
    generation: input.generation,
    workerAgentId: input.workerAgentId,
    idempotencyKey: input.idempotencyKey,
  });
  if (!envelope || !location) {
    throw new Error("claim not found");
  }

  const suppliedDigest = createHash("sha256").update(input.token).digest();
  const storedDigest = createHash("sha256").update(envelope.token).digest();
  if (!timingSafeEqual(suppliedDigest, storedDigest)) {
    throw new Error("claim token mismatch");
  }

  const now = input.now ?? new Date();
  if (!(now instanceof Date) || !Number.isFinite(now.getTime())) {
    throw new Error("claim verification time is invalid");
  }
  if (Date.parse(envelope.expires_at) <= now.getTime()) {
    throw new Error("claim expired");
  }
  return { envelope, filePath: location.filePath };
}

export async function deleteClaimSecret(
  input: DeleteClaimSecretInput,
): Promise<void> {
  requireNonEmpty(input.planId, "plan id");
  if (!CLAIM_ID_PATTERN.test(input.claimId)) {
    throw pathMismatch();
  }

  const existingLocation = await claimLocation(
    input.vaultPath,
    input.planId,
    input.claimId,
    false,
  );
  if (!existingLocation) return;

  await withClaimLock(
    existingLocation.vaultRoot,
    input.planId,
    input.claimId,
    async (location) => {
      const existing = await readEnvelope(location, {
        planId: input.planId,
        claimId: input.claimId,
      });
      if (!existing) return;
      try {
        await unlink(location.filePath);
      } catch (error) {
        if (!isErrno(error, "ENOENT")) throw error;
      }
      await syncDirectory(location.planDir);
    },
  );
}

import {
  createHash,
  randomBytes,
  randomUUID,
  timingSafeEqual,
} from "node:crypto";
import { constants } from "node:fs";
import {
  lstat,
  mkdir,
  open,
  readdir,
  rename,
  rmdir,
  stat,
  unlink,
  type FileHandle,
} from "node:fs/promises";
import path from "node:path";

import { stableStringify } from "./agent_envelope.js";
import type { PlanSlice } from "./plan.js";
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

// Worker-update receipts: a SEPARATE private record from the claim secret
// above. A claim-clearing action (e.g. "complete") deletes the live claim
// once it durably lands, so an identical retry has nothing left to
// authenticate against via verifyClaimToken. This receipt is what a retry
// authenticates against instead — keyed by plan/slice/worker/idempotency
// (never by claim id or generation, both of which the clearing action just
// invalidated), holding only a timing-safe digest of the claim token that
// authorized the original call, never the token itself.
const RECEIPT_SCHEMA = "minni.thread-worker-update-receipt.v1" as const;
const RECEIPT_ID_PATTERN = /^[0-9a-f]{32}$/;
const TOKEN_DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const MAX_RECEIPT_BYTES = 64 * 1024;
const RECEIPT_ENVELOPE_KEYS = [
  "claim_id",
  "generation",
  "idempotency_key",
  "kind",
  "plan_id",
  "response",
  "rev",
  "schema",
  "slice_id",
  "status",
  "token_digest",
  "worker_agent_id",
] as const;
const RECEIPT_RESPONSE_KEYS = [
  "ready_after",
  "ready_before",
  "rev",
  "slice",
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

/**
 * The exact public shape a worker-update tool call returns. Safe to store
 * verbatim — it is model-facing already — but it is not the same thing as a
 * journal event: it lives only in this private receipt, never in the
 * ordered Thread journal minni_thread_events reads.
 */
export interface WorkerUpdateReceiptResponse {
  slice: PlanSlice;
  ready_before: string[];
  ready_after: string[];
  rev: number;
}

export type WorkerUpdateReceiptStatus = "pending" | "committed";

export interface WorkerUpdateReceiptEnvelope {
  schema: "minni.thread-worker-update-receipt.v1";
  plan_id: string;
  slice_id: string;
  worker_agent_id: string;
  claim_id: string;
  generation: number;
  idempotency_key: string;
  kind: string;
  token_digest: string;
  status: WorkerUpdateReceiptStatus;
  rev: number;
  response: WorkerUpdateReceiptResponse;
}

export interface WorkerUpdateReceiptIdentity {
  vaultPath: string;
  planId: string;
  sliceId: string;
  workerAgentId: string;
  claimId?: string;
  generation: number;
  idempotencyKey: string;
}

export interface WriteWorkerUpdateReceiptInput extends WorkerUpdateReceiptIdentity {
  claimId: string;
  kind: string;
  tokenDigest: string;
  rev: number;
  response: WorkerUpdateReceiptResponse;
}

interface ClaimLocation {
  fdAliasRoot: string;
  vaultHandle: FileHandle;
  runtimeHandle: FileHandle;
  claimsHandle: FileHandle;
  planHandle: FileHandle;
  vaultPath: string;
  runtimePath: string;
  claimsPath: string;
  planPath: string;
  filePath: string;
  fileName: string;
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

class ClaimStoreParentChangedError extends Error {
  constructor() {
    super("claim store parent changed during operation");
    this.name = "ClaimStoreParentChangedError";
  }
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

/**
 * Receipt file identity: plan/slice/worker/generation/idempotency. claim_id is
 * stored and validated inside the envelope, not hashed into the path — a
 * same-generation replay (including post-complete) opens one known file.
 */
function receiptIdFor(
  planId: string,
  sliceId: string,
  workerAgentId: string,
  generation: number,
  idempotencyKey: string,
): string {
  return createHash("sha256")
    .update(stableStringify({
      plan_id: planId,
      slice_id: sliceId,
      worker_agent_id: workerAgentId,
      generation,
      idempotency_key: idempotencyKey,
    }))
    .digest("hex")
    .slice(0, 32);
}

function generationDirName(generation: number): string {
  return `g${generation}`;
}

function tokenDigestOf(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

/** Timing-safe: compares digests, never the raw token bytes. */
export function hashWorkerUpdateToken(token: string): string {
  requireNonEmpty(token, "claim token");
  return tokenDigestOf(token);
}

export function workerUpdateTokenMatches(
  suppliedToken: string,
  storedDigestHex: string,
): boolean {
  if (!TOKEN_DIGEST_PATTERN.test(storedDigestHex)) return false;
  const supplied = createHash("sha256").update(suppliedToken).digest();
  const stored = Buffer.from(storedDigestHex, "hex");
  return supplied.length === stored.length && timingSafeEqual(supplied, stored);
}

function directoryOpenFlags(): number {
  if (
    typeof constants.O_DIRECTORY !== "number" ||
    typeof constants.O_NOFOLLOW !== "number"
  ) {
    throw new Error(
      "claim store requires POSIX O_DIRECTORY and O_NOFOLLOW support",
    );
  }
  return constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW;
}

function fdAliasPath(fdAliasRoot: string, handle: FileHandle): string {
  return path.join(fdAliasRoot, String(handle.fd));
}

function childOfHandle(
  fdAliasRoot: string,
  parent: FileHandle,
  childName: string,
): string {
  if (
    childName.length === 0 ||
    childName === "." ||
    childName === ".." ||
    childName.includes("/") ||
    childName.includes("\\")
  ) {
    throw pathMismatch();
  }
  return path.join(fdAliasPath(fdAliasRoot, parent), childName);
}

async function detectFdAliasRoot(handle: FileHandle): Promise<string> {
  const expected = await handle.stat();
  for (const root of ["/proc/self/fd", "/dev/fd"]) {
    try {
      const aliasStat = await stat(path.join(root, String(handle.fd)));
      if (
        aliasStat.isDirectory() &&
        aliasStat.dev === expected.dev &&
        aliasStat.ino === expected.ino
      ) {
        return root;
      }
    } catch {
      // Try the next well-known descriptor alias.
    }
  }
  throw new Error(
    "claim store requires a verified /proc/self/fd or /dev/fd descriptor alias",
  );
}

async function openPrivateChildDirectory(
  parent: FileHandle,
  fdAliasRoot: string,
  childName: string,
  create: boolean,
): Promise<FileHandle | undefined> {
  const anchoredPath = childOfHandle(fdAliasRoot, parent, childName);
  if (create) {
    try {
      await mkdir(anchoredPath, { mode: 0o700 });
    } catch (error) {
      if (!isErrno(error, "EEXIST")) throw error;
    }
  }

  let handle: FileHandle;
  try {
    handle = await open(anchoredPath, directoryOpenFlags());
  } catch (error) {
    if (!create && isErrno(error, "ENOENT")) return undefined;
    if (isErrno(error, "ELOOP") || isErrno(error, "ENOTDIR")) {
      throw pathMismatch();
    }
    throw error;
  }

  try {
    const openedStat = await handle.stat();
    if (!openedStat.isDirectory()) throw pathMismatch();
    await handle.chmod(0o700);
    const privateStat = await handle.stat();
    if ((privateStat.mode & 0o777) !== 0o700) {
      throw new Error("claim store permissions mismatch");
    }
    return handle;
  } catch (error) {
    await handle.close().catch(() => {});
    throw error;
  }
}

async function withClaimLocation<T>(
  vaultPath: string,
  planId: string,
  claimId: string,
  create: boolean,
  fn: (location: ClaimLocation | undefined) => Promise<T>,
): Promise<T> {
  requireNonEmpty(vaultPath, "vault path");
  requireNonEmpty(planId, "plan id");
  if (!CLAIM_ID_PATTERN.test(claimId)) {
    throw pathMismatch();
  }

  const logicalVaultPath = path.resolve(vaultPath);
  const handles: FileHandle[] = [];
  try {
    let vaultHandle: FileHandle;
    try {
      vaultHandle = await open(logicalVaultPath, directoryOpenFlags());
    } catch (error) {
      if (isErrno(error, "ELOOP") || isErrno(error, "ENOTDIR")) {
        throw pathMismatch();
      }
      throw error;
    }
    handles.push(vaultHandle);
    const vaultStat = await vaultHandle.stat();
    if (!vaultStat.isDirectory()) throw pathMismatch();
    const fdAliasRoot = await detectFdAliasRoot(vaultHandle);

    const runtimeHandle = await openPrivateChildDirectory(
      vaultHandle,
      fdAliasRoot,
      ".runtime",
      create,
    );
    if (!runtimeHandle) return await fn(undefined);
    handles.push(runtimeHandle);

    const claimsHandle = await openPrivateChildDirectory(
      runtimeHandle,
      fdAliasRoot,
      "thread-claims",
      create,
    );
    if (!claimsHandle) return await fn(undefined);
    handles.push(claimsHandle);

    const planHash = hashSegment(planId);
    const planHandle = await openPrivateChildDirectory(
      claimsHandle,
      fdAliasRoot,
      planHash,
      create,
    );
    if (!planHandle) return await fn(undefined);
    handles.push(planHandle);

    const runtimePath = path.join(logicalVaultPath, ".runtime");
    const claimsPath = path.join(runtimePath, "thread-claims");
    const planPath = path.join(claimsPath, planHash);
    const fileName = `${claimId}.json`;
    return await fn({
      fdAliasRoot,
      vaultHandle,
      runtimeHandle,
      claimsHandle,
      planHandle,
      vaultPath: logicalVaultPath,
      runtimePath,
      claimsPath,
      planPath,
      filePath: path.join(planPath, fileName),
      fileName,
    });
  } finally {
    for (const handle of handles.reverse()) {
      await handle.close().catch(() => {});
    }
  }
}

interface ReceiptLocation {
  fdAliasRoot: string;
  vaultHandle: FileHandle;
  runtimeHandle: FileHandle;
  claimsHandle: FileHandle;
  planHandle: FileHandle;
  sliceHandle: FileHandle;
  generationHandle: FileHandle;
  vaultPath: string;
  runtimePath: string;
  claimsPath: string;
  planPath: string;
  slicePath: string;
  generationPath: string;
  filePath: string;
  fileName: string;
}

interface ReceiptSliceLocation {
  fdAliasRoot: string;
  vaultHandle: FileHandle;
  runtimeHandle: FileHandle;
  claimsHandle: FileHandle;
  planHandle: FileHandle;
  sliceHandle: FileHandle;
  vaultPath: string;
  runtimePath: string;
  claimsPath: string;
  planPath: string;
  slicePath: string;
}

/**
 * Same descriptor-anchored authority as withReceiptLocation, stopping one
 * level higher — at updates/<sliceHash> — the exact parent directory whose
 * child g<generation> directories pruneWorkerUpdateReceiptsForGeneration
 * deletes. Used only for pruning, never for reading/writing a receipt file.
 */
async function withReceiptSliceLocation<T>(
  vaultPath: string,
  planId: string,
  sliceId: string,
  create: boolean,
  fn: (location: ReceiptSliceLocation | undefined) => Promise<T>,
): Promise<T> {
  requireNonEmpty(vaultPath, "vault path");
  requireNonEmpty(planId, "plan id");
  requireNonEmpty(sliceId, "slice id");

  const logicalVaultPath = path.resolve(vaultPath);
  const handles: FileHandle[] = [];
  try {
    let vaultHandle: FileHandle;
    try {
      vaultHandle = await open(logicalVaultPath, directoryOpenFlags());
    } catch (error) {
      if (isErrno(error, "ELOOP") || isErrno(error, "ENOTDIR")) {
        throw pathMismatch();
      }
      throw error;
    }
    handles.push(vaultHandle);
    const vaultStat = await vaultHandle.stat();
    if (!vaultStat.isDirectory()) throw pathMismatch();
    const fdAliasRoot = await detectFdAliasRoot(vaultHandle);

    const runtimeHandle = await openPrivateChildDirectory(
      vaultHandle,
      fdAliasRoot,
      ".runtime",
      create,
    );
    if (!runtimeHandle) return await fn(undefined);
    handles.push(runtimeHandle);

    const claimsHandle = await openPrivateChildDirectory(
      runtimeHandle,
      fdAliasRoot,
      "thread-claims",
      create,
    );
    if (!claimsHandle) return await fn(undefined);
    handles.push(claimsHandle);

    const planHash = hashSegment(planId);
    const planHandle = await openPrivateChildDirectory(
      claimsHandle,
      fdAliasRoot,
      planHash,
      create,
    );
    if (!planHandle) return await fn(undefined);
    handles.push(planHandle);

    const updatesHandle = await openPrivateChildDirectory(
      planHandle,
      fdAliasRoot,
      "updates",
      create,
    );
    if (!updatesHandle) return await fn(undefined);
    handles.push(updatesHandle);

    const sliceHash = hashSegment(sliceId);
    const sliceHandle = await openPrivateChildDirectory(
      updatesHandle,
      fdAliasRoot,
      sliceHash,
      create,
    );
    if (!sliceHandle) return await fn(undefined);
    handles.push(sliceHandle);

    const runtimePath = path.join(logicalVaultPath, ".runtime");
    const claimsPath = path.join(runtimePath, "thread-claims");
    const planPath = path.join(claimsPath, planHash);
    const slicePath = path.join(planPath, "updates", sliceHash);
    return await fn({
      fdAliasRoot,
      vaultHandle,
      runtimeHandle,
      claimsHandle,
      planHandle,
      sliceHandle,
      vaultPath: logicalVaultPath,
      runtimePath,
      claimsPath,
      planPath,
      slicePath,
    });
  } finally {
    for (const handle of handles.reverse()) {
      await handle.close().catch(() => {});
    }
  }
}

/**
 * Same descriptor-anchored authority as withClaimLocation (vault ->
 * .runtime -> thread-claims -> <planHash> -> updates -> <sliceHash> ->
 * g<generation>), one level deeper than claim secrets so receipt files never
 * collide with <claimId>.json in the plan directory.
 */
async function withReceiptLocation<T>(
  vaultPath: string,
  planId: string,
  sliceId: string,
  generation: number,
  receiptId: string,
  create: boolean,
  fn: (location: ReceiptLocation | undefined) => Promise<T>,
): Promise<T> {
  requireNonEmpty(vaultPath, "vault path");
  requireNonEmpty(planId, "plan id");
  requireNonEmpty(sliceId, "slice id");
  requireNonNegativeInteger(generation, "generation");
  if (!RECEIPT_ID_PATTERN.test(receiptId)) {
    throw pathMismatch();
  }

  const logicalVaultPath = path.resolve(vaultPath);
  const handles: FileHandle[] = [];
  try {
    let vaultHandle: FileHandle;
    try {
      vaultHandle = await open(logicalVaultPath, directoryOpenFlags());
    } catch (error) {
      if (isErrno(error, "ELOOP") || isErrno(error, "ENOTDIR")) {
        throw pathMismatch();
      }
      throw error;
    }
    handles.push(vaultHandle);
    const vaultStat = await vaultHandle.stat();
    if (!vaultStat.isDirectory()) throw pathMismatch();
    const fdAliasRoot = await detectFdAliasRoot(vaultHandle);

    const runtimeHandle = await openPrivateChildDirectory(
      vaultHandle,
      fdAliasRoot,
      ".runtime",
      create,
    );
    if (!runtimeHandle) return await fn(undefined);
    handles.push(runtimeHandle);

    const claimsHandle = await openPrivateChildDirectory(
      runtimeHandle,
      fdAliasRoot,
      "thread-claims",
      create,
    );
    if (!claimsHandle) return await fn(undefined);
    handles.push(claimsHandle);

    const planHash = hashSegment(planId);
    const planHandle = await openPrivateChildDirectory(
      claimsHandle,
      fdAliasRoot,
      planHash,
      create,
    );
    if (!planHandle) return await fn(undefined);
    handles.push(planHandle);

    const updatesHandle = await openPrivateChildDirectory(
      planHandle,
      fdAliasRoot,
      "updates",
      create,
    );
    if (!updatesHandle) return await fn(undefined);
    handles.push(updatesHandle);

    const sliceHash = hashSegment(sliceId);
    const sliceHandle = await openPrivateChildDirectory(
      updatesHandle,
      fdAliasRoot,
      sliceHash,
      create,
    );
    if (!sliceHandle) return await fn(undefined);
    handles.push(sliceHandle);

    const generationDir = generationDirName(generation);
    const generationHandle = await openPrivateChildDirectory(
      sliceHandle,
      fdAliasRoot,
      generationDir,
      create,
    );
    if (!generationHandle) return await fn(undefined);
    handles.push(generationHandle);

    const runtimePath = path.join(logicalVaultPath, ".runtime");
    const claimsPath = path.join(runtimePath, "thread-claims");
    const planPath = path.join(claimsPath, planHash);
    const slicePath = path.join(planPath, "updates", sliceHash);
    const generationPath = path.join(slicePath, generationDir);
    const fileName = `${receiptId}.json`;
    return await fn({
      fdAliasRoot,
      vaultHandle,
      runtimeHandle,
      claimsHandle,
      planHandle,
      sliceHandle,
      generationHandle,
      vaultPath: logicalVaultPath,
      runtimePath,
      claimsPath,
      planPath,
      slicePath,
      generationPath,
      filePath: path.join(generationPath, fileName),
      fileName,
    });
  } finally {
    for (const handle of handles.reverse()) {
      await handle.close().catch(() => {});
    }
  }
}

async function logicalPathMatchesHandle(
  logicalPath: string,
  expectedHandle: FileHandle,
): Promise<boolean> {
  let currentHandle: FileHandle;
  try {
    currentHandle = await open(logicalPath, directoryOpenFlags());
  } catch {
    return false;
  }
  try {
    const [current, expected] = await Promise.all([
      currentHandle.stat(),
      expectedHandle.stat(),
    ]);
    return (
      current.isDirectory() &&
      current.dev === expected.dev &&
      current.ino === expected.ino
    );
  } finally {
    await currentHandle.close().catch(() => {});
  }
}

/**
 * Descriptor aliases make the secret operation itself race-safe. This
 * postcondition serves a different purpose: StoredClaimSecret.filePath is a
 * logical path, so never return it after an attacker detached the opened
 * directory tree from that path.
 */
async function assertLocationStillAttached(
  location: ClaimLocation,
): Promise<void> {
  const matches = await Promise.all([
    logicalPathMatchesHandle(location.vaultPath, location.vaultHandle),
    logicalPathMatchesHandle(location.runtimePath, location.runtimeHandle),
    logicalPathMatchesHandle(location.claimsPath, location.claimsHandle),
    logicalPathMatchesHandle(location.planPath, location.planHandle),
  ]);
  if (matches.some((match) => !match)) {
    throw new ClaimStoreParentChangedError();
  }
}

async function assertReceiptLocationStillAttached(
  location: ReceiptLocation,
): Promise<void> {
  const matches = await Promise.all([
    logicalPathMatchesHandle(location.vaultPath, location.vaultHandle),
    logicalPathMatchesHandle(location.runtimePath, location.runtimeHandle),
    logicalPathMatchesHandle(location.claimsPath, location.claimsHandle),
    logicalPathMatchesHandle(location.planPath, location.planHandle),
    logicalPathMatchesHandle(location.slicePath, location.sliceHandle),
    logicalPathMatchesHandle(location.generationPath, location.generationHandle),
  ]);
  if (matches.some((match) => !match)) {
    throw new ClaimStoreParentChangedError();
  }
}

async function assertReceiptSliceLocationStillAttached(
  location: ReceiptSliceLocation,
): Promise<void> {
  const matches = await Promise.all([
    logicalPathMatchesHandle(location.vaultPath, location.vaultHandle),
    logicalPathMatchesHandle(location.runtimePath, location.runtimeHandle),
    logicalPathMatchesHandle(location.claimsPath, location.claimsHandle),
    logicalPathMatchesHandle(location.planPath, location.planHandle),
    logicalPathMatchesHandle(location.slicePath, location.sliceHandle),
  ]);
  if (matches.some((match) => !match)) {
    throw new ClaimStoreParentChangedError();
  }
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
      childOfHandle(
        location.fdAliasRoot,
        location.planHandle,
        location.fileName,
      ),
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

async function writeEnvelopeAtomic(
  location: ClaimLocation,
  envelope: ClaimSecretEnvelope,
): Promise<void> {
  const temporaryName =
    `.${envelope.claim_id}.${randomBytes(16).toString("hex")}.tmp`;
  const temporaryPath = childOfHandle(
    location.fdAliasRoot,
    location.planHandle,
    temporaryName,
  );
  const finalPath = childOfHandle(
    location.fdAliasRoot,
    location.planHandle,
    location.fileName,
  );

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
      await lstat(finalPath);
      throw metadataMismatch();
    } catch (error) {
      if (!isErrno(error, "ENOENT")) throw error;
    }

    await rename(temporaryPath, finalPath);
    await location.planHandle.sync();
  } finally {
    if (handle) await handle.close().catch(() => {});
    await unlink(temporaryPath).catch((error) => {
      if (!isErrno(error, "ENOENT")) throw error;
    });
  }
}

async function unlinkEnvelope(location: ClaimLocation): Promise<void> {
  try {
    await unlink(
      childOfHandle(
        location.fdAliasRoot,
        location.planHandle,
        location.fileName,
      ),
    );
  } catch (error) {
    if (!isErrno(error, "ENOENT")) throw error;
  }
  await location.planHandle.sync();
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isPlanSliceLike(value: unknown): value is PlanSlice {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    value.id.length > 0 &&
    typeof value.title === "string" &&
    typeof value.status === "string"
  );
}

function parseReceiptEnvelope(
  raw: string,
  expected: WorkerUpdateReceiptIdentity & { receiptId: string },
): WorkerUpdateReceiptEnvelope {
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
    !hasExactKeys(value, RECEIPT_ENVELOPE_KEYS) ||
    !hasExactKeys(response, RECEIPT_RESPONSE_KEYS) ||
    value.schema !== RECEIPT_SCHEMA ||
    typeof value.plan_id !== "string" ||
    value.plan_id.trim().length === 0 ||
    typeof value.slice_id !== "string" ||
    value.slice_id.trim().length === 0 ||
    typeof value.worker_agent_id !== "string" ||
    value.worker_agent_id.trim().length === 0 ||
    typeof value.claim_id !== "string" ||
    !CLAIM_ID_PATTERN.test(value.claim_id) ||
    !Number.isSafeInteger(value.generation) ||
    (value.generation as number) < 0 ||
    typeof value.idempotency_key !== "string" ||
    value.idempotency_key.trim().length === 0 ||
    typeof value.kind !== "string" ||
    value.kind.trim().length === 0 ||
    typeof value.token_digest !== "string" ||
    !TOKEN_DIGEST_PATTERN.test(value.token_digest) ||
    (value.status !== "pending" && value.status !== "committed") ||
    !Number.isSafeInteger(value.rev) ||
    (value.rev as number) < 0 ||
    !isPlanSliceLike(response.slice) ||
    !isStringArray(response.ready_before) ||
    !isStringArray(response.ready_after) ||
    !Number.isSafeInteger(response.rev) ||
    (response.rev as number) < 0
  ) {
    throw metadataMismatch();
  }

  const envelope = value as unknown as WorkerUpdateReceiptEnvelope;
  const derivedReceiptId = receiptIdFor(
    envelope.plan_id,
    envelope.slice_id,
    envelope.worker_agent_id,
    envelope.generation,
    envelope.idempotency_key,
  );
  if (
    derivedReceiptId !== expected.receiptId ||
    envelope.plan_id !== expected.planId ||
    envelope.slice_id !== expected.sliceId ||
    envelope.worker_agent_id !== expected.workerAgentId ||
    envelope.generation !== expected.generation ||
    envelope.idempotency_key !== expected.idempotencyKey ||
    envelope.response.rev !== envelope.rev
  ) {
    throw metadataMismatch();
  }
  if (
    expected.claimId !== undefined &&
    envelope.claim_id !== expected.claimId
  ) {
    throw metadataMismatch();
  }
  return envelope;
}

async function readReceiptEnvelopeRaw(
  location: ReceiptLocation,
  receiptId: string,
): Promise<WorkerUpdateReceiptEnvelope | undefined> {
  let handle;
  try {
    handle = await open(
      childOfHandle(
        location.fdAliasRoot,
        location.generationHandle,
        location.fileName,
      ),
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
      throw new Error("worker update receipt permissions mismatch");
    }
    if (fileStat.size > MAX_RECEIPT_BYTES) {
      throw metadataMismatch();
    }
    const raw = await handle.readFile("utf8");
    let value: unknown;
    try {
      value = JSON.parse(raw);
    } catch {
      throw metadataMismatch();
    }
    if (!isRecord(value) || !isRecord(value.response)) {
      throw metadataMismatch();
    }
    const envelope = value as unknown as WorkerUpdateReceiptEnvelope;
    const derivedReceiptId = receiptIdFor(
      envelope.plan_id,
      envelope.slice_id,
      envelope.worker_agent_id,
      envelope.generation,
      envelope.idempotency_key,
    );
    if (derivedReceiptId !== receiptId) {
      throw metadataMismatch();
    }
    return parseReceiptEnvelope(raw, {
      vaultPath: location.vaultPath,
      planId: envelope.plan_id,
      sliceId: envelope.slice_id,
      workerAgentId: envelope.worker_agent_id,
      idempotencyKey: envelope.idempotency_key,
      claimId: envelope.claim_id,
      generation: envelope.generation,
      receiptId,
    });
  } finally {
    await handle.close();
  }
}

async function readReceiptEnvelope(
  location: ReceiptLocation | undefined,
  expected: WorkerUpdateReceiptIdentity & { receiptId: string },
): Promise<WorkerUpdateReceiptEnvelope | undefined> {
  if (!location) return undefined;
  return readReceiptEnvelopeRaw(location, expected.receiptId);
}

/**
 * Unlike writeEnvelopeAtomic (claim secrets are always create-once), this
 * unconditionally replaces any existing file at the destination. Every
 * caller has already decided, before reaching this write, that doing so is
 * safe: either no receipt existed yet, or the existing one was a pending
 * receipt already proven stale (its recorded result never actually landed).
 */
async function writeReceiptEnvelopeAtomic(
  location: ReceiptLocation,
  envelope: WorkerUpdateReceiptEnvelope,
): Promise<void> {
  const temporaryName = `.${location.fileName}.${randomBytes(16).toString("hex")}.tmp`;
  const temporaryPath = childOfHandle(
    location.fdAliasRoot,
    location.generationHandle,
    temporaryName,
  );
  const finalPath = childOfHandle(
    location.fdAliasRoot,
    location.generationHandle,
    location.fileName,
  );

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

    await rename(temporaryPath, finalPath);
    await location.generationHandle.sync();
  } finally {
    if (handle) await handle.close().catch(() => {});
    await unlink(temporaryPath).catch((error) => {
      if (!isErrno(error, "ENOENT")) throw error;
    });
  }
}

async function withReceiptLock<T>(
  vaultPath: string,
  planId: string,
  sliceId: string,
  generation: number,
  receiptId: string,
  fn: (location: ReceiptLocation) => Promise<T>,
): Promise<T> {
  let anchoredVaultPath = "";
  await withReceiptLocation(
    vaultPath,
    planId,
    sliceId,
    generation,
    receiptId,
    true,
    async (location) => {
      if (!location) throw pathMismatch();
      anchoredVaultPath = location.vaultPath;
    },
  );
  return withThreadLock(
    anchoredVaultPath,
    `update:${hashSegment(planId)}:${receiptId}`,
    randomUUID(),
    () =>
      withReceiptLocation(
        anchoredVaultPath,
        planId,
        sliceId,
        generation,
        receiptId,
        true,
        async (lockedLocation) => {
          if (!lockedLocation) throw pathMismatch();
          return fn(lockedLocation);
        },
      ),
  );
}

export interface WorkerUpdateReceiptLookupInput {
  vaultPath: string;
  planId: string;
  sliceId: string;
  workerAgentId: string;
  generation: number;
  idempotencyKey: string;
  claimId?: string;
}

/**
 * Deletes every file directly inside one already fd-anchored directory, then
 * removes the directory itself through its parent's descriptor — never via
 * a raw logical path. A generation directory only ever holds flat receipt
 * (and transient atomic-write temp) files; an unexpected nested directory is
 * treated as a shape mismatch and fails closed rather than being followed or
 * recursively deleted.
 */
async function removeAnchoredGenerationDirectory(
  fdAliasRoot: string,
  parentHandle: FileHandle,
  childName: string,
): Promise<void> {
  const childPath = childOfHandle(fdAliasRoot, parentHandle, childName);
  let handle: FileHandle | undefined;
  try {
    handle = await open(childPath, directoryOpenFlags());
  } catch (error) {
    if (isErrno(error, "ENOENT")) return;
    if (isErrno(error, "ELOOP") || isErrno(error, "ENOTDIR")) return;
    throw error;
  }
  try {
    const openedStat = await handle.stat();
    if (!openedStat.isDirectory()) return;
    const entries = await readdir(childPath, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.isDirectory()) {
        throw pathMismatch();
      }
      await unlink(childOfHandle(fdAliasRoot, handle, entry.name)).catch(
        (error) => {
          if (!isErrno(error, "ENOENT")) throw error;
        },
      );
    }
  } finally {
    await handle.close().catch(() => {});
  }
  await rmdir(childPath).catch((error) => {
    if (!isErrno(error, "ENOENT")) throw error;
  });
}

/**
 * Best-effort delete of every receipt file for one slice generation. Called
 * only when a slice's generation advances (reassign, replan, restore, expiry)
 * — never on the hot worker-update path.
 *
 * Deletion is anchored to the already-opened, verified `updates/<sliceHash>`
 * directory descriptor returned by withReceiptSliceLocation — the same
 * descriptor-anchored, no-follow private-directory traversal thread-claims
 * uses for every read/write above — never a raw path-based recursive `rm`
 * through `.runtime`'s logical (and therefore swappable) ancestors. A parent
 * swap or symlink mid-operation cannot redirect this deletion outside the
 * vault: the generation directory is opened and removed via the slice
 * handle's descriptor alias, which always resolves to the original directory
 * regardless of what the logical path is later made to point at.
 */
export async function pruneWorkerUpdateReceiptsForGeneration(
  vaultPath: string,
  planId: string,
  sliceId: string,
  generation: number,
): Promise<void> {
  requireNonEmpty(vaultPath, "vault path");
  requireNonEmpty(planId, "plan id");
  requireNonEmpty(sliceId, "slice id");
  requireNonNegativeInteger(generation, "generation");
  const generationDir = generationDirName(generation);
  await withReceiptSliceLocation(
    vaultPath,
    planId,
    sliceId,
    false,
    async (location) => {
      if (!location) return;
      await removeAnchoredGenerationDirectory(
        location.fdAliasRoot,
        location.sliceHandle,
        generationDir,
      );
      // Detect (rather than silently trust) a parent swap/symlink that
      // occurred mid-operation. The deletion above already landed on the
      // real, originally-opened directory regardless — this only decides
      // whether the caller can additionally trust the surrounding tree.
      await assertReceiptSliceLocationStillAttached(location);
    },
  );
}

export async function readWorkerUpdateReceipt(
  input: WorkerUpdateReceiptLookupInput,
): Promise<WorkerUpdateReceiptEnvelope | undefined> {
  requireNonEmpty(input.vaultPath, "vault path");
  requireNonEmpty(input.planId, "plan id");
  requireNonEmpty(input.sliceId, "slice id");
  requireNonEmpty(input.workerAgentId, "worker agent id");
  requireNonEmpty(input.idempotencyKey, "idempotency key");
  requireNonNegativeInteger(input.generation, "generation");
  if (input.claimId !== undefined) {
    requireNonEmpty(input.claimId, "claim id");
    if (!CLAIM_ID_PATTERN.test(input.claimId)) {
      throw pathMismatch();
    }
  }
  const receiptId = receiptIdFor(
    input.planId,
    input.sliceId,
    input.workerAgentId,
    input.generation,
    input.idempotencyKey,
  );
  return withReceiptLocation(
    input.vaultPath,
    input.planId,
    input.sliceId,
    input.generation,
    receiptId,
    false,
    (location) =>
      readReceiptEnvelope(location, {
        vaultPath: input.vaultPath,
        planId: input.planId,
        sliceId: input.sliceId,
        workerAgentId: input.workerAgentId,
        idempotencyKey: input.idempotencyKey,
        claimId: input.claimId,
        generation: input.generation,
        receiptId,
      }),
  );
}

function validateReceiptWriteInput(
  input: WriteWorkerUpdateReceiptInput,
): string {
  requireNonEmpty(input.vaultPath, "vault path");
  requireNonEmpty(input.planId, "plan id");
  requireNonEmpty(input.sliceId, "slice id");
  requireNonEmpty(input.workerAgentId, "worker agent id");
  requireNonEmpty(input.idempotencyKey, "idempotency key");
  requireNonEmpty(input.claimId, "claim id");
  if (!CLAIM_ID_PATTERN.test(input.claimId)) {
    throw pathMismatch();
  }
  requireNonNegativeInteger(input.generation, "generation");
  requireNonEmpty(input.kind, "operation kind");
  if (!TOKEN_DIGEST_PATTERN.test(input.tokenDigest)) {
    throw new Error("worker update receipt requires a valid token digest");
  }
  requireNonNegativeInteger(input.rev, "revision");
  if (input.response.rev !== input.rev) {
    throw new Error("worker update receipt response rev must match rev");
  }
  return receiptIdFor(
    input.planId,
    input.sliceId,
    input.workerAgentId,
    input.generation,
    input.idempotencyKey,
  );
}

async function writeWorkerUpdateReceipt(
  input: WriteWorkerUpdateReceiptInput,
  status: WorkerUpdateReceiptStatus,
): Promise<WorkerUpdateReceiptEnvelope> {
  const receiptId = validateReceiptWriteInput(input);
  return withReceiptLock(
    input.vaultPath,
    input.planId,
    input.sliceId,
    input.generation,
    receiptId,
    async (location) => {
    const envelope: WorkerUpdateReceiptEnvelope = {
      schema: RECEIPT_SCHEMA,
      plan_id: input.planId,
      slice_id: input.sliceId,
      worker_agent_id: input.workerAgentId,
      claim_id: input.claimId,
      generation: input.generation,
      idempotency_key: input.idempotencyKey,
      kind: input.kind,
      token_digest: input.tokenDigest,
      status,
      rev: input.rev,
      response: input.response,
    };
    await writeReceiptEnvelopeAtomic(location, envelope);
    await assertReceiptLocationStillAttached(location);
    return envelope;
  });
}

/**
 * Written BEFORE persistPlan is called, while still holding the Thread
 * lock, so a crash between this write and the actual persist leaves a
 * receipt whose recorded result never landed. readWorkerUpdateReceipt's
 * caller is responsible for treating a "pending" status as untrusted
 * unless the current strict Thread state matches — this function only
 * writes the record, it never decides whether one is trustworthy.
 */
export async function writePendingWorkerUpdateReceipt(
  input: WriteWorkerUpdateReceiptInput,
): Promise<WorkerUpdateReceiptEnvelope> {
  return writeWorkerUpdateReceipt(input, "pending");
}

/**
 * Promotes a pending receipt (or writes a fresh committed one directly) once
 * the caller has independently confirmed the note write actually landed.
 */
export async function commitWorkerUpdateReceipt(
  input: WriteWorkerUpdateReceiptInput,
): Promise<WorkerUpdateReceiptEnvelope> {
  return writeWorkerUpdateReceipt(input, "committed");
}

async function withClaimLock<T>(
  vaultPath: string,
  planId: string,
  claimId: string,
  fn: (location: ClaimLocation) => Promise<T>,
): Promise<T> {
  let anchoredVaultPath = "";
  await withClaimLocation(vaultPath, planId, claimId, true, async (location) => {
    if (!location) throw pathMismatch();
    anchoredVaultPath = location.vaultPath;
  });
  return withThreadLock(
    anchoredVaultPath,
    `claim:${hashSegment(planId)}:${claimId}`,
    randomUUID(),
    () =>
      withClaimLocation(
        anchoredVaultPath,
        planId,
        claimId,
        true,
        async (lockedLocation) => {
          if (!lockedLocation) throw pathMismatch();
          return fn(lockedLocation);
        },
      ),
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
  return withClaimLocation(
    vaultPath,
    planId,
    claimId,
    false,
    (location) =>
      readEnvelope(location, {
        planId,
        sliceId,
        claimId,
        generation,
        idempotencyKey,
      }),
  );
}

/**
 * Existing claim-secret store, by claim id. Drain uses this so a Q ticket
 * does not need a leaked raw token copy.
 */
export async function readClaimById(
  vaultPath: string,
  planId: string,
  claimId: string,
): Promise<ClaimSecretEnvelope | undefined> {
  requireNonEmpty(planId, "plan id");
  if (!CLAIM_ID_PATTERN.test(claimId)) {
    throw pathMismatch();
  }
  return withClaimLocation(
    vaultPath,
    planId,
    claimId,
    false,
    (location) =>
      readEnvelope(location, {
        planId,
        claimId,
      }),
  );
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
        await assertLocationStillAttached(location);
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

      let created = false;
      try {
        await writeEnvelopeAtomic(location, envelope);
        created = true;
        const stored = await readEnvelope(location, expected);
        if (!stored) throw metadataMismatch();
        await assertLocationStillAttached(location);
        return { envelope: stored, filePath: location.filePath };
      } catch (error) {
        if (created) {
          await unlinkEnvelope(location).catch(() => {});
        }
        throw error;
      }
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

  return withClaimLocation(
    input.vaultPath,
    input.planId,
    claimId,
    false,
    async (location) => {
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
      await assertLocationStillAttached(location);
      return { envelope, filePath: location.filePath };
    },
  );
}

export async function deleteClaimSecret(
  input: DeleteClaimSecretInput,
): Promise<void> {
  requireNonEmpty(input.planId, "plan id");
  if (!CLAIM_ID_PATTERN.test(input.claimId)) {
    throw pathMismatch();
  }

  let anchoredVaultPath = "";
  const hasClaimDirectory = await withClaimLocation(
    input.vaultPath,
    input.planId,
    input.claimId,
    false,
    async (location) => {
      if (!location) return false;
      anchoredVaultPath = location.vaultPath;
      return true;
    },
  );
  if (!hasClaimDirectory) return;

  await withClaimLock(
    anchoredVaultPath,
    input.planId,
    input.claimId,
    async (location) => {
      const existing = await readEnvelope(location, {
        planId: input.planId,
        claimId: input.claimId,
      });
      if (!existing) return;
      await unlinkEnvelope(location);
      await assertLocationStillAttached(location);
    },
  );
}

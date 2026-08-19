import { randomUUID } from "node:crypto";
import path from "node:path";

import {
  addScar,
  journalPathFor,
  persistPlan,
  planDigestVersionErrorMessage,
  planHistoryAppendErrorMessage,
  PlanDigestVersionError,
  rehydratePlan,
  unmetDependencies,
  updateSlice,
  PlanHistoryAppendError,
  type AppendJournalDeps,
  type PlanArtifact,
  type PlanSlice,
  type PlanSliceStatus,
  type StructuralProposal,
  type UpdateSliceOptions,
} from "./plan.js";
import type { ScarTissueEntry } from "./task.js";
import {
  appendOrderedEventBatch,
  assertOperationIdentity,
  deriveClientEventKey,
  deriveReadyChangedKey,
  deriveSystemEventKey,
  ensureOrderedBaseline,
  findOrderedEventByIdempotencyKey,
  orderedSnapshotMatchesJournal,
  reconcileThreadJournal,
  readOrderedThreadEvents,
  type OrderedThreadEvent,
  ThreadCursorGapError,
  ThreadEventIdempotencyConflictError,
  ThreadJournalAppendError,
  ThreadJournalReadError,
  type ReadySummaryPayload,
} from "./thread-events.js";
import {
  commitWorkerUpdateReceipt,
  createClaimSecret,
  deleteClaimSecret,
  hashWorkerUpdateToken,
  pruneWorkerUpdateReceiptsForGeneration,
  readClaimByIdempotency,
  readWorkerUpdateReceipt,
  verifyClaimToken,
  workerUpdateTokenMatches,
  writePendingWorkerUpdateReceipt,
  type ThreadClaimResponse,
  type WorkerUpdateReceiptEnvelope,
  type WorkerUpdateReceiptResponse,
} from "./thread-claims.js";
import { stableStringify } from "./agent_envelope.js";
import { withThreadLock } from "./thread-lock.js";
import { MAX_TEAM_TTL_SECONDS } from "./team.js";

const DEFAULT_CLAIM_TTL_SECONDS = 10 * 60;

/**
 * Upper bound on a Thread claim lease. Same ceiling as Team packet TTL
 * (MAX_TEAM_TTL_SECONDS) so the two surfaces share one max lease length;
 * Thread rejects over-cap (typed) rather than clamping, matching existing
 * claim TTL validation which already rejects non-positive values.
 */
export const MAX_THREAD_CLAIM_TTL_SECONDS = MAX_TEAM_TTL_SECONDS;

export class ThreadClaimTtlError extends Error {
  readonly code = "THREAD_CLAIM_TTL_INVALID" as const;

  constructor(ttlSeconds: number, maxSeconds: number) {
    super(
      `claim ttlSeconds ${ttlSeconds} exceeds maximum of ${maxSeconds}`,
    );
    this.name = "ThreadClaimTtlError";
  }
}

export type WorkerUpdateAction =
  | { action: "start" }
  | { action: "progress"; evidence: string }
  | { action: "block"; evidence: string }
  | {
      action: "scar";
      kind: ScarTissueEntry["kind"];
      signal: string;
      resolution?: string;
    }
  | { action: "propose_structure"; proposal: StructuralProposal }
  | { action: "complete"; evidence: string };

export interface ThreadMutationResult {
  plan: PlanArtifact;
  slice: PlanSlice;
  ready_before: string[];
  ready_after: string[];
}

interface ThreadPlanTarget {
  vaultPath: string;
  notePath: string;
  planId: string;
}

interface ThreadMutationTarget extends ThreadPlanTarget {
  sliceId: string;
  now?: Date | (() => Date);
}

export interface AssignSliceInput extends ThreadMutationTarget {
  /**
   * The orchestrator/caller identity that is PERFORMING the assignment.
   * This is distinct from `workerAgentId` (the assignment TARGET) and is
   * what the ordered journal must record as the event actor — a worker
   * does not "act" on the Thread merely by being assigned to it. Server
   * callers (server.ts's minni_thread_assign) must stamp this server-side
   * (DEFAULT_AGENT_ID); it is never model-suppliable.
   */
  actorAgentId: string;
  workerAgentId: string;
  assignmentProfile?: string;
}

export interface ClaimSliceInput extends ThreadMutationTarget {
  workerAgentId: string;
  idempotencyKey: string;
  ttlSeconds?: number;
}

export interface UpdateClaimedSliceInput extends ThreadMutationTarget {
  workerAgentId: string;
  token: string;
  action: WorkerUpdateAction;
  idempotencyKey: string;
}

export interface ThreadWorkerDeps {
  persistPlan?: typeof persistPlan;
  deleteClaimSecret?: typeof deleteClaimSecret;
  /** Injectable journal append/fsync seam for tests (landed-then-throw, etc.). */
  appendJournalDeps?: AppendJournalDeps;
}

function requireNonEmpty(value: string, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`thread worker requires non-empty ${label}`);
  }
  return value.trim();
}

function sampleNow(value: Date | (() => Date) | undefined): Date {
  const now = typeof value === "function" ? value() : (value ?? new Date());
  if (!(now instanceof Date) || !Number.isFinite(now.getTime())) {
    throw new Error("thread worker time is invalid");
  }
  return now;
}

function requireGeneration(slice: PlanSlice): number {
  const generation = slice.generation ?? 0;
  if (!Number.isSafeInteger(generation) || generation < 0) {
    throw new Error(`slice "${slice.id}" has invalid generation`);
  }
  return generation;
}

function requireAttempt(slice: PlanSlice): number {
  const attempt = slice.attempt ?? 0;
  if (!Number.isSafeInteger(attempt) || attempt < 0) {
    throw new Error(`slice "${slice.id}" has invalid attempt`);
  }
  return attempt;
}

function claimExpiresAt(slice: PlanSlice): number | undefined {
  if (!slice.claim) return undefined;
  const expiresAt = Date.parse(slice.claim.expires_at);
  if (!Number.isFinite(expiresAt)) {
    throw new Error(`slice "${slice.id}" has invalid claim expiry`);
  }
  return expiresAt;
}

function hasLiveClaim(slice: PlanSlice, now: Date): boolean {
  const expiresAt = claimExpiresAt(slice);
  return expiresAt !== undefined && expiresAt > now.getTime();
}

function isNonTerminal(slice: PlanSlice): boolean {
  return slice.status !== "done" && slice.status !== "superseded";
}

/**
 * Return the deterministic ready set: non-terminal slices with resolved
 * dependencies and no unexpired claim. Expired refs are treated as not live;
 * claimSlice performs the corresponding locked durable cleanup.
 */
export function readySlices(plan: PlanArtifact, now: Date): PlanSlice[] {
  const checkedNow = sampleNow(now);
  return plan.slices
    .filter(
      (slice) =>
        isNonTerminal(slice) &&
        unmetDependencies(plan, slice.id).length === 0 &&
        !hasLiveClaim(slice, checkedNow),
    )
    .slice()
    .sort((left, right) => left.id.localeCompare(right.id));
}

export function readyIds(plan: PlanArtifact, now: Date): string[] {
  return readySlices(plan, now).map((slice) => slice.id);
}

function assertNoteUnderVault(vaultPath: string, notePath: string): void {
  const vault = path.resolve(requireNonEmpty(vaultPath, "vault path"));
  const note = path.resolve(requireNonEmpty(notePath, "note path"));
  const relative = path.relative(vault, note);
  if (
    relative.length === 0 ||
    relative.startsWith("..") ||
    path.isAbsolute(relative)
  ) {
    throw new Error("thread note path is outside the vault");
  }
}

async function rehydrateAuthority(
  input: ThreadPlanTarget,
  rehydrate: typeof rehydratePlan = rehydratePlan,
): Promise<PlanArtifact> {
  assertNoteUnderVault(input.vaultPath, input.notePath);
  // Authority paths intentionally use only strict rehydration. In particular,
  // rehydratePlanScalars is a recovery helper whose lenient assignment/claim
  // metadata must never authorize a worker.
  const plan = await rehydrate(input.notePath);
  if (plan.plan_id !== input.planId) {
    throw new Error(
      `thread plan scope mismatch: expected ${input.planId}, found ${plan.plan_id}`,
    );
  }
  return plan;
}

export interface ThreadPlanLockInput extends ThreadPlanTarget {
  operationId: string;
}

export interface ThreadPlanLockDeps {
  rehydratePlan?: typeof rehydratePlan;
}

/**
 * Shared lock-before-read primitive for every production Thread
 * read-modify-write path. Callers receive only a strict, digest-verified plan
 * loaded after this process owns the plan lock.
 */
export async function withThreadPlanLock<T>(
  input: ThreadPlanLockInput,
  fn: (plan: PlanArtifact) => Promise<T>,
  deps: ThreadPlanLockDeps = {},
): Promise<T> {
  return withThreadLock(
    input.vaultPath,
    requireNonEmpty(input.planId, "plan id"),
    requireNonEmpty(input.operationId, "operation id"),
    async () =>
      fn(await rehydrateAuthority(input, deps.rehydratePlan ?? rehydratePlan)),
  );
}

export function claimIds(plan: PlanArtifact): string[] {
  return [
    ...new Set(
      plan.slices
        .map((slice) => slice.claim?.claim_id)
        .filter((claimId): claimId is string => typeof claimId === "string"),
    ),
  ];
}

export function revokedClaimIds(
  before: PlanArtifact,
  after: PlanArtifact,
): string[] {
  const afterClaimIds = new Set(claimIds(after));
  return claimIds(before).filter((claimId) => !afterClaimIds.has(claimId));
}

const NODE_ERRNO_CODE = /^E[A-Z][A-Z0-9]{1,30}$/;
const PATH_BEARING_MESSAGE =
  /wiki\/artifacts|(?:^|[\s'"`=(])(?:\/|\.\.?\/|[A-Za-z]:[\\/])/;

function nodeErrnoCode(error: unknown): string | undefined {
  if (typeof error === "object" && error !== null && "code" in error) {
    const code = (error as { code: unknown }).code;
    if (typeof code === "string" && NODE_ERRNO_CODE.test(code)) {
      return code;
    }
  }
  return undefined;
}

/**
 * Model-facing text for thread MCP handlers. PlanHistoryAppendError and
 * PlanDigestVersionError keep notePath as a typed field for internal
 * callers. Rebuild from rev + cause.code (history) or version (digest)
 * only — never interpolate notePath, historyPathFor(notePath),
 * cause.message, or cause.path (real EISDIR/EACCES embed wiki/artifacts).
 * Untyped Node errno / path-bearing messages are rebuilt the same way:
 * syscall code only, never Error.message.
 */
export function threadWorkerErrorText(error: unknown): string {
  if (error instanceof PlanHistoryAppendError) {
    return planHistoryAppendErrorMessage(error.rev, error.cause);
  }
  if (error instanceof PlanDigestVersionError) {
    return planDigestVersionErrorMessage(error.version);
  }
  if (error instanceof ThreadJournalReadError) {
    return error.message;
  }
  if (error instanceof ThreadJournalAppendError) {
    return error.message;
  }
  if (error instanceof ThreadCursorGapError) {
    return error.message;
  }
  const errno = nodeErrnoCode(error);
  if (errno) {
    return `thread worker failed: ${errno}`;
  }
  if (error instanceof Error) {
    if (PATH_BEARING_MESSAGE.test(error.message)) {
      return "thread worker failed";
    }
    return error.message;
  }
  return String(error);
}

export async function deleteClaimSecretsBestEffort(
  vaultPath: string,
  planId: string,
  claimIdsToDelete: Iterable<string>,
  deleteSecret: typeof deleteClaimSecret = deleteClaimSecret,
): Promise<void> {
  const uniqueClaimIds = new Set(claimIdsToDelete);
  await Promise.all(
    [...uniqueClaimIds].map((claimId) =>
      deleteSecret({ vaultPath, planId, claimId }).catch(() => {})
    ),
  );
}

/** Best-effort prune of receipts for a superseded slice generation. */
export async function pruneSliceReceiptsOnGenerationAdvance(
  vaultPath: string,
  planId: string,
  sliceId: string,
  previousGeneration: number,
): Promise<void> {
  await pruneWorkerUpdateReceiptsForGeneration(
    vaultPath,
    planId,
    sliceId,
    previousGeneration,
  ).catch(() => {});
}

/**
 * Best-effort prune of receipts for every generation collected while a
 * mutation walked past stale/expired generations in memory. Callers MUST
 * call this only once the corresponding generation advance is durable
 * (persisted, or independently confirmed committed) — never before, or a
 * precommit failure would delete receipts a same-key retry still needs.
 */
async function pruneCollectedGenerationsBestEffort(
  vaultPath: string,
  planId: string,
  sliceId: string,
  generations: number[],
): Promise<void> {
  const unique = [...new Set(generations)];
  await Promise.all(
    unique.map((generation) =>
      pruneSliceReceiptsOnGenerationAdvance(vaultPath, planId, sliceId, generation),
    ),
  );
}

/** Prune receipt generations for every slice whose generation advanced. */
export async function pruneSliceReceiptsAfterPlanMutation(
  vaultPath: string,
  planId: string,
  before: PlanArtifact,
  after: PlanArtifact,
): Promise<void> {
  for (const slice of before.slices) {
    const afterSlice = after.slices.find((candidate) => candidate.id === slice.id);
    if (!afterSlice) continue;
    const beforeGen = slice.generation ?? 0;
    const afterGen = afterSlice.generation ?? 0;
    if (afterGen > beforeGen) {
      await pruneSliceReceiptsOnGenerationAdvance(
        vaultPath,
        planId,
        slice.id,
        beforeGen,
      );
    }
  }
}

function findSlice(plan: PlanArtifact, sliceId: string): PlanSlice {
  const matches = plan.slices.filter((candidate) => candidate.id === sliceId);
  if (matches.length > 1) {
    throw new Error(`thread worker: duplicate slice id "${sliceId}"`);
  }
  const slice = matches[0];
  if (!slice) {
    throw new Error(`thread worker: no slice with id ${sliceId}`);
  }
  return slice;
}

function replaceSlice(
  plan: PlanArtifact,
  sliceId: string,
  replacement: PlanSlice,
): PlanArtifact {
  if (
    plan.slices.filter((slice) => slice.id === sliceId).length !== 1
  ) {
    throw new Error(
      `thread worker: slice "${sliceId}" must identify exactly one slice`,
    );
  }
  return {
    ...plan,
    slices: plan.slices.map((slice) =>
      slice.id === sliceId ? replacement : slice
    ),
  };
}

export interface OrchestratorSliceUpdateResult {
  plan: PlanArtifact;
  slice: PlanSlice;
  previous_slice: PlanSlice;
  revoked_claim_id?: string;
}

/**
 * Pure direct-orchestrator slice transition. Unlike a claimed worker update,
 * any orchestrator transition revokes extant worker authority, even when the
 * status will later be reopened.
 */
export function applyOrchestratorSliceUpdate(
  plan: PlanArtifact,
  sliceId: string,
  status: PlanSliceStatus,
  evidence?: string,
  options?: UpdateSliceOptions,
): OrchestratorSliceUpdateResult {
  const previousSlice = findSlice(plan, sliceId);
  let next = updateSlice(plan, sliceId, status, evidence, options);
  if (previousSlice.claim) {
    const transitioned = findSlice(next, sliceId);
    next = replaceSlice(next, sliceId, {
      ...transitioned,
      generation: requireGeneration(previousSlice) + 1,
      claim: undefined,
    });
  }
  return {
    plan: next,
    slice: findSlice(next, sliceId),
    previous_slice: previousSlice,
    ...(previousSlice.claim
      ? { revoked_claim_id: previousSlice.claim.claim_id }
      : {}),
  };
}

function mutationResult(
  plan: PlanArtifact,
  sliceId: string,
  readyBefore: string[],
  now: Date,
): ThreadMutationResult {
  return {
    plan,
    slice: findSlice(plan, sliceId),
    ready_before: readyBefore,
    ready_after: readyIds(plan, now),
  };
}

function structurallyEqual(left: unknown, right: unknown): boolean {
  return stableStringify(left) === stableStringify(right);
}

/**
 * Reconstructs the exact ThreadMutationResult shape from a worker-update
 * receipt's stored PUBLIC response. `plan` here is only ever used by a
 * caller for `.rev` — the receipt's recorded rev, not the (possibly
 * further-advanced) rev of the freshly loaded `plan` passed in, so a
 * replay's rev never appears to increase.
 */
function receiptMutationResult(
  plan: PlanArtifact,
  receipt: WorkerUpdateReceiptEnvelope,
): ThreadMutationResult {
  return {
    plan: { ...plan, rev: receipt.response.rev },
    slice: receipt.response.slice,
    ready_before: receipt.response.ready_before,
    ready_after: receipt.response.ready_after,
  };
}

function readyIdsEqual(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  const sortedLeft = [...left].sort();
  const sortedRight = [...right].sort();
  return sortedLeft.every((id, index) => id === sortedRight[index]);
}

function deriveClaimEventKey(
  planId: string,
  sliceId: string,
  workerAgentId: string,
  idempotencyKey: string,
): string {
  return deriveClientEventKey("claim", {
    plan_id: planId,
    slice_id: sliceId,
    worker_agent_id: workerAgentId,
    idempotency_key: idempotencyKey,
  });
}

function deriveWorkerEventKey(
  planId: string,
  sliceId: string,
  workerAgentId: string,
  idempotencyKey: string,
): string {
  return deriveClientEventKey("worker", {
    plan_id: planId,
    slice_id: sliceId,
    worker_agent_id: workerAgentId,
    idempotency_key: idempotencyKey,
  });
}

function planJustCompleted(before: PlanArtifact, after: PlanArtifact): boolean {
  return before.status !== "complete" && after.status === "complete";
}

type AttentionKind = "block" | "lease_expired";

function attentionPayload(sliceId: string, attentionKind: AttentionKind): Record<string, unknown> {
  return { slice_id: sliceId, attention_kind: attentionKind };
}

function committedReceiptAuthoritative(
  slice: PlanSlice,
  receipt: WorkerUpdateReceiptEnvelope,
): boolean {
  const generation = requireGeneration(slice);
  if (receipt.generation !== generation) return false;
  if (slice.claim) {
    return slice.claim.claim_id === receipt.claim_id;
  }
  return structurallyEqual(slice, receipt.response.slice);
}

function readySummary(plan: PlanArtifact, now: Date): ReadySummaryPayload {
  return {
    slices: readySlices(plan, now).map((slice) => ({
      id: slice.id,
      title: slice.title,
    })),
  };
}

/**
 * Reconcile the ordered scheduler journal against the just-rehydrated,
 * PRE-mutation plan, then ensure an ordered baseline exists — the exact
 * "before persistence" half of every locked mutation's event lifecycle.
 * Exported so every locked Thread mutation path (worker AND orchestrator —
 * assign/claim/worker_update as well as server.ts's
 * update/scar/replan/restore handlers) shares one scheduling implementation
 * instead of re-deriving reconcile/baseline logic per call site.
 */
export async function prepareThreadMutation(
  input: ThreadPlanTarget & { actor: string },
  plan: PlanArtifact,
  now: Date,
  appendJournalDeps: AppendJournalDeps = {},
): Promise<{
  journalPath: string;
  ordered: OrderedThreadEvent[];
}> {
  const journalPath = journalPathFor(input.notePath, input.planId);
  const summary = readySummary(plan, now);
  const ordered = await readOrderedThreadEvents(journalPath);
  await reconcileThreadJournal(
    {
      journalPath,
      notePath: input.notePath,
      planId: input.planId,
      rev: plan.rev,
      actor: input.actor,
      readySummary: summary,
      orderedSnapshot: ordered,
    },
    appendJournalDeps,
  );
  await ensureOrderedBaseline(
    {
      journalPath,
      planId: input.planId,
      rev: plan.rev,
      actor: input.actor,
      readySummary: summary,
      orderedSnapshot: ordered,
    },
    appendJournalDeps,
  );
  return { journalPath, ordered };
}

function workerEventKind(action: WorkerUpdateAction): string {
  switch (action.action) {
    case "start":
      return "slice.started";
    case "progress":
      return "slice.progressed";
    case "block":
      return "slice.blocked";
    case "complete":
      return "slice.completed";
    case "scar":
      return "scar_added";
    case "propose_structure":
      return "structure.proposed";
    default:
      throw new Error("unsupported worker action");
  }
}

/**
 * Append one operation event plus, when the ready set actually changed, one
 * coalesced ready.changed event — the "after persistence" half of every
 * locked mutation's event lifecycle.
 *
 * If the ordered append fails but the operation event actually landed
 * (write-then-fsync: land-then-throw with a refreshed snapshot), continue —
 * success and cursor-moved already agree. If the operation key is still
 * missing, throw THREAD_JOURNAL_APPEND_FAILED rather than returning MCP OK
 * while the cursor lags until a later state.recovered. Crash between note
 * persist and this call remains a true note-ahead gap recovered on the next
 * locked mutation per the v2 contract.
 */
export async function recordThreadMutationEvents(input: {
  journalPath: string;
  planId: string;
  rev: number;
  actor: string;
  operationKey: string;
  kind: string;
  sliceId?: string;
  payload?: Record<string, unknown>;
  readyBefore: string[];
  readyAfter: string[];
  plan: PlanArtifact;
  planBefore?: PlanArtifact;
  now: Date;
  orderedSnapshot?: OrderedThreadEvent[];
  appendJournalDeps?: AppendJournalDeps;
  supplementalEvents?: Array<{
    idempotencyKey: string;
    kind: string;
    sliceId?: string;
    payload?: Record<string, unknown>;
  }>;
}): Promise<void> {
  const at = input.now.toISOString();
  const events: Array<{
    idempotencyKey: string;
    kind: string;
    sliceId?: string;
    payload?: Record<string, unknown>;
  }> = [
    {
      idempotencyKey: input.operationKey,
      kind: input.kind,
      sliceId: input.sliceId,
      payload: input.payload,
    },
  ];
  if (input.supplementalEvents) {
    events.push(...input.supplementalEvents);
  }
  if (input.planBefore && planJustCompleted(input.planBefore, input.plan)) {
    events.push({
      idempotencyKey: deriveSystemEventKey(
        "thread.completed",
        input.planId,
        String(input.rev),
      ),
      kind: "thread.completed",
    });
  }
  if (!readyIdsEqual(input.readyBefore, input.readyAfter)) {
    events.push({
      idempotencyKey: deriveReadyChangedKey(input.operationKey),
      kind: "ready.changed",
      sliceId: undefined,
      payload: {
        slices: readySummary(input.plan, input.now).slices,
      },
    });
  }
  try {
    await appendOrderedEventBatch(
      {
        journalPath: input.journalPath,
        planId: input.planId,
        rev: input.rev,
        actor: input.actor,
        at,
        events,
        orderedSnapshot: input.orderedSnapshot,
      },
      input.appendJournalDeps ?? {},
    );
  } catch (error) {
    if (error instanceof ThreadEventIdempotencyConflictError) {
      throw error;
    }
    if (input.orderedSnapshot) {
      const snapshotTruthful = await orderedSnapshotMatchesJournal(
        input.orderedSnapshot,
        input.journalPath,
      );
      if (!snapshotTruthful) {
        throw error;
      }
    }
    // Snapshot matches the durable journal (or there is no live snapshot).
    // Continue only when this mutation's operation event actually landed
    // (land-then-throw refresh path). Otherwise the note is ahead and the
    // caller must see a typed failure — not MCP OK with a silent gap.
    const ordered =
      input.orderedSnapshot ??
      (await readOrderedThreadEvents(input.journalPath).catch(() => []));
    if (findOrderedEventByIdempotencyKey(ordered, input.operationKey)) {
      return;
    }
    throw new ThreadJournalAppendError(input.operationKey, input.kind, error);
  }
}

async function repairClaimSchedulerEvents(input: {
  journalPath: string;
  planId: string;
  rev: number;
  actor: string;
  operationKey: string;
  sliceId: string;
  readyBefore: string[];
  readyAfter: string[];
  plan: PlanArtifact;
  now: Date;
  orderedSnapshot?: OrderedThreadEvent[];
  appendJournalDeps?: AppendJournalDeps;
}): Promise<void> {
  const ordered =
    input.orderedSnapshot ?? await readOrderedThreadEvents(input.journalPath);
  const summary = readySummary(input.plan, input.now);
  await ensureOrderedBaseline(
    {
      journalPath: input.journalPath,
      planId: input.planId,
      rev: input.rev,
      actor: input.actor,
      readySummary: summary,
      orderedSnapshot: ordered,
    },
    input.appendJournalDeps ?? {},
  );
  const claimed = findOrderedEventByIdempotencyKey(ordered, input.operationKey);
  if (!claimed) {
    await recordThreadMutationEvents({
      journalPath: input.journalPath,
      planId: input.planId,
      rev: input.rev,
      actor: input.actor,
      operationKey: input.operationKey,
      kind: "slice.claimed",
      sliceId: input.sliceId,
      readyBefore: input.readyBefore,
      readyAfter: input.readyAfter,
      plan: input.plan,
      now: input.now,
      orderedSnapshot: ordered,
      appendJournalDeps: input.appendJournalDeps,
    });
    return;
  }
  assertOperationIdentity(claimed, {
    idempotencyKey: input.operationKey,
    kind: "slice.claimed",
    actor: input.actor,
    sliceId: input.sliceId,
  });
  if (!readyIdsEqual(input.readyBefore, input.readyAfter)) {
    const readyKey = deriveReadyChangedKey(input.operationKey);
    if (!findOrderedEventByIdempotencyKey(ordered, readyKey)) {
      await appendOrderedEventBatch(
        {
          journalPath: input.journalPath,
          planId: input.planId,
          rev: input.rev,
          actor: input.actor,
          at: input.now.toISOString(),
          events: [
            {
              idempotencyKey: readyKey,
              kind: "ready.changed",
              payload: { slices: summary.slices },
            },
          ],
          orderedSnapshot: ordered,
        },
        input.appendJournalDeps ?? {},
      );
    }
  }
}

async function expireStaleClaimForSlice(input: {
  vaultPath: string;
  notePath: string;
  planId: string;
  sliceId: string;
  actor: string;
  journalPath: string;
  plan: PlanArtifact;
  now: Date;
  persist: typeof persistPlan;
  deleteSecret: typeof deleteClaimSecret;
  orderedSnapshot?: OrderedThreadEvent[];
  appendJournalDeps?: AppendJournalDeps;
}): Promise<{ plan: PlanArtifact; expired: boolean; readyBefore: string[] }> {
  const slice = findSlice(input.plan, input.sliceId);
  if (!slice.claim || hasLiveClaim(slice, input.now)) {
    return {
      plan: input.plan,
      expired: false,
      readyBefore: readyIds(input.plan, input.now),
    };
  }
  const readyBefore = readyIds(input.plan, input.now);
  const revokedClaimId = slice.claim.claim_id;
  const previousGeneration = requireGeneration(slice);
  const generation = previousGeneration + 1;
  const nextSlice: PlanSlice = {
    ...slice,
    generation,
    claim: undefined,
  };
  const next = replaceSlice(input.plan, input.sliceId, nextSlice);
  const intendedRev = input.plan.rev + 1;
  try {
    await input.persist(next, {
      vaultPath: input.vaultPath,
      notePath: input.notePath,
    });
  } catch (error) {
    let committed = error instanceof PlanHistoryAppendError;
    if (!committed) {
      try {
        const canonical = await rehydrateAuthority(input);
        const durableSlice = findSlice(canonical, input.sliceId);
        committed =
          canonical.rev === intendedRev &&
          requireGeneration(durableSlice) === generation &&
          durableSlice.claim === undefined;
      } catch {
        committed = false;
      }
    }
    if (committed) {
      await deleteClaimSecretsBestEffort(
        input.vaultPath,
        input.planId,
        [revokedClaimId],
        input.deleteSecret,
      );
    }
    throw error;
  }
  await deleteClaimSecretsBestEffort(
    input.vaultPath,
    input.planId,
    [revokedClaimId],
    input.deleteSecret,
  );
  await pruneSliceReceiptsOnGenerationAdvance(
    input.vaultPath,
    input.planId,
    input.sliceId,
    previousGeneration,
  );
  const readyAfter = readyIds(next, input.now);
  const operationKey = deriveSystemEventKey(
    "slice.lease_expired",
    input.planId,
    input.sliceId,
    String(next.rev),
  );
  await recordThreadMutationEvents({
    journalPath: input.journalPath,
    planId: input.planId,
    rev: next.rev,
    actor: input.actor,
    operationKey,
    kind: "slice.lease_expired",
    sliceId: input.sliceId,
    readyBefore,
    readyAfter,
    plan: next,
    now: input.now,
    // Same mutable snapshot the outer claim/update batch will allocate seqs
    // from — a sibling's in-lock expiry must land its events in that shared
    // array (not a fresh re-parse) so the outer append computes seqs that
    // are strictly higher than these, never colliding with them.
    orderedSnapshot: input.orderedSnapshot,
    appendJournalDeps: input.appendJournalDeps,
    supplementalEvents: [
      {
        idempotencyKey: deriveSystemEventKey(
          "thread.attention_required",
          input.planId,
          input.sliceId,
          "lease_expired",
          String(next.rev),
        ),
        kind: "thread.attention_required",
        sliceId: input.sliceId,
        payload: attentionPayload(input.sliceId, "lease_expired"),
      },
    ],
  });
  return { plan: next, expired: true, readyBefore };
}

async function synchronizeExpiredClaimsForPlan(input: {
  vaultPath: string;
  notePath: string;
  planId: string;
  actor: string;
  journalPath: string;
  plan: PlanArtifact;
  now: Date;
  persist: typeof persistPlan;
  deleteSecret: typeof deleteClaimSecret;
  orderedSnapshot?: OrderedThreadEvent[];
  appendJournalDeps?: AppendJournalDeps;
}): Promise<PlanArtifact> {
  let plan = input.plan;
  for (const slice of plan.slices) {
    if (!slice.claim || hasLiveClaim(slice, input.now)) continue;
    const result = await expireStaleClaimForSlice({
      ...input,
      plan,
      sliceId: slice.id,
    });
    plan = result.plan;
  }
  return plan;
}

/**
 * Lazily expire stale claims under the Thread lock and persist
 * slice.lease_expired / thread.attention_required before returning.
 *
 * Single shared expiry sweep for every read path that must observe a dead
 * claim without going through ready/claim/worker_update first
 * (minni_thread_events, minni_thread_status, and ready via the wrapper below).
 * Do not invent a second expiry implementation beside this helper.
 *
 * When no claim is past expires_at, this is a lock+rehydrate only — no journal
 * write — so status can still resolve beside an unreadable journal.
 */
export async function synchronizeExpiredClaims(
  input: ThreadPlanTarget & { actor: string; now?: Date | (() => Date) },
  deps: ThreadWorkerDeps = {},
): Promise<{ plan: PlanArtifact }> {
  const planId = requireNonEmpty(input.planId, "plan id");
  const actor = requireNonEmpty(input.actor, "actor");
  const persist = deps.persistPlan ?? persistPlan;
  const deleteSecret = deps.deleteClaimSecret ?? deleteClaimSecret;
  const appendJournalDeps = deps.appendJournalDeps ?? {};

  return withThreadPlanLock(
    {
      vaultPath: input.vaultPath,
      notePath: input.notePath,
      planId,
      operationId: `sync-expiry:${randomUUID()}`,
    },
    async (initialPlan) => {
      const now = sampleNow(input.now);
      const needsExpiry = initialPlan.slices.some(
        (slice) => slice.claim !== undefined && !hasLiveClaim(slice, now),
      );
      if (!needsExpiry) {
        return { plan: initialPlan };
      }
      const { journalPath, ordered } = await prepareThreadMutation(
        { ...input, planId, actor },
        initialPlan,
        now,
        appendJournalDeps,
      );
      const plan = await synchronizeExpiredClaimsForPlan({
        vaultPath: input.vaultPath,
        notePath: input.notePath,
        planId,
        actor,
        journalPath,
        plan: initialPlan,
        now,
        persist,
        deleteSecret,
        orderedSnapshot: ordered,
        appendJournalDeps,
      });
      return { plan };
    },
  );
}

/**
 * Lazily expire stale claims under the Thread lock, then return the ready set.
 * Ready delegates to synchronizeExpiredClaims — the same sweep events/status use.
 */
export async function synchronizeExpiredClaimsAndReadReady(
  input: ThreadPlanTarget & { actor: string; now?: Date | (() => Date) },
  deps: ThreadWorkerDeps = {},
): Promise<{ plan: PlanArtifact; ready: PlanSlice[] }> {
  // Sample once so ready and expiry agree on the same injected clock, matching
  // the pre-extract behavior of a single now inside the lock.
  const now = sampleNow(input.now);
  const { plan } = await synchronizeExpiredClaims({ ...input, now }, deps);
  return { plan, ready: readySlices(plan, now) };
}

function claimRevokedEvent(
  planId: string,
  sliceId: string,
  rev: number,
): { idempotencyKey: string; kind: string; sliceId: string; payload: Record<string, unknown> } {
  return {
    idempotencyKey: deriveSystemEventKey(
      "slice.claim_revoked",
      planId,
      sliceId,
      String(rev),
    ),
    kind: "slice.claim_revoked",
    sliceId,
    payload: { slice_id: sliceId },
  };
}

function publicClaimResponse(
  response: ThreadClaimResponse,
): ThreadClaimResponse {
  return {
    plan_id: response.plan_id,
    slice_id: response.slice_id,
    claim_id: response.claim_id,
    generation: response.generation,
    worker_agent_id: response.worker_agent_id,
    token: response.token,
    expires_at: response.expires_at,
    rev: response.rev,
  };
}

function assignmentProfile(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  const profile = value.trim();
  return profile.length > 0 ? profile : undefined;
}

export async function assignSlice(
  input: AssignSliceInput,
  deps: ThreadWorkerDeps = {},
): Promise<ThreadMutationResult> {
  const planId = requireNonEmpty(input.planId, "plan id");
  const sliceId = requireNonEmpty(input.sliceId, "slice id");
  const workerAgentId = requireNonEmpty(
    input.workerAgentId,
    "worker agent id",
  );
  const actorAgentId = requireNonEmpty(
    input.actorAgentId,
    "actor agent id",
  );
  const profile = assignmentProfile(input.assignmentProfile);
  const persist = deps.persistPlan ?? persistPlan;
  const deleteSecret = deps.deleteClaimSecret ?? deleteClaimSecret;
  const appendJournalDeps = deps.appendJournalDeps ?? {};

  return withThreadPlanLock(
    {
      vaultPath: input.vaultPath,
      notePath: input.notePath,
      planId,
      operationId: `assign:${randomUUID()}`,
    },
    async (plan) => {
      const now = sampleNow(input.now);
      const { journalPath, ordered } = await prepareThreadMutation(
        { ...input, planId, actor: actorAgentId },
        plan,
        now,
        appendJournalDeps,
      );
      const slice = findSlice(plan, sliceId);
      const readyBefore = readyIds(plan, now);
      const structurallyReady =
        isNonTerminal(slice) &&
        unmetDependencies(plan, sliceId).length === 0;
      if (slice.status !== "pending" && !structurallyReady) {
        throw new Error(`slice "${sliceId}" is not assignable`);
      }

      const generation = requireGeneration(slice);
      const reassigned = slice.assigned_to !== undefined;
      const previousGeneration = generation;
      const nextGeneration = generation + (reassigned ? 1 : 0);
      const nextSlice: PlanSlice = {
        ...slice,
        assigned_to: workerAgentId,
        assignment_profile: profile,
        generation: nextGeneration,
        claim: undefined,
      };
      const next = replaceSlice(plan, sliceId, nextSlice);
      // Receipts at previousGeneration are pruned only once this generation
      // advance is durable — never before persist, and never on a persist
      // failure unless a strict reread confirms the note actually committed.
      const intendedRev = plan.rev + 1;
      try {
        await persist(next, {
          vaultPath: input.vaultPath,
          notePath: input.notePath,
        });
      } catch (error) {
        let committed = error instanceof PlanHistoryAppendError;
        if (!committed) {
          try {
            const canonical = await rehydrateAuthority(input);
            const durableSlice = findSlice(canonical, sliceId);
            committed =
              canonical.rev === intendedRev &&
              requireGeneration(durableSlice) === nextGeneration &&
              durableSlice.assigned_to === workerAgentId &&
              durableSlice.assignment_profile === profile;
          } catch {
            committed = false;
          }
        }
        if (committed && reassigned) {
          await pruneCollectedGenerationsBestEffort(
            input.vaultPath,
            planId,
            sliceId,
            [previousGeneration],
          );
        }
        // Assign is claim-clearing: the durable note already has no live
        // claim, so the mode-0600 envelope is an orphan and must go even
        // when history append failed after the note write.
        if (committed && slice.claim) {
          await deleteClaimSecretsBestEffort(
            input.vaultPath,
            planId,
            [slice.claim.claim_id],
            deleteSecret,
          );
        }
        throw error;
      }
      if (reassigned) {
        await pruneCollectedGenerationsBestEffort(
          input.vaultPath,
          planId,
          sliceId,
          [previousGeneration],
        );
      }
      if (slice.claim) {
        await deleteClaimSecretsBestEffort(
          input.vaultPath,
          planId,
          [slice.claim.claim_id],
          deleteSecret,
        );
      }
      const result = mutationResult(next, sliceId, readyBefore, now);
      const supplemental: Array<{
        idempotencyKey: string;
        kind: string;
        sliceId?: string;
        payload?: Record<string, unknown>;
      }> = [];
      if (slice.claim) {
        supplemental.push(
          claimRevokedEvent(planId, sliceId, next.rev),
        );
      }
      await recordThreadMutationEvents({
        journalPath,
        planId,
        rev: next.rev,
        actor: actorAgentId,
        operationKey: deriveSystemEventKey(
          "slice.assigned",
          planId,
          sliceId,
          workerAgentId,
          String(nextSlice.generation),
        ),
        kind: "slice.assigned",
        sliceId,
        readyBefore: result.ready_before,
        readyAfter: result.ready_after,
        plan: next,
        now,
        orderedSnapshot: ordered,
        appendJournalDeps,
        supplementalEvents: supplemental.length > 0 ? supplemental : undefined,
      });
      return result;
    },
  );
}

function claimMetadataMatches(
  slice: PlanSlice,
  envelope: Awaited<ReturnType<typeof readClaimByIdempotency>>,
  planId: string,
  workerAgentId: string,
): envelope is NonNullable<typeof envelope> {
  if (!slice.claim || !envelope) return false;
  return (
    envelope.plan_id === planId &&
    envelope.slice_id === slice.id &&
    envelope.claim_id === slice.claim.claim_id &&
    envelope.generation === requireGeneration(slice) &&
    envelope.worker_agent_id === workerAgentId &&
    envelope.expires_at === slice.claim.expires_at &&
    slice.claim.worker_agent_id === workerAgentId
  );
}

export async function claimSlice(
  input: ClaimSliceInput,
  deps: ThreadWorkerDeps = {},
): Promise<ThreadClaimResponse> {
  const planId = requireNonEmpty(input.planId, "plan id");
  const sliceId = requireNonEmpty(input.sliceId, "slice id");
  const workerAgentId = requireNonEmpty(
    input.workerAgentId,
    "worker agent id",
  );
  const idempotencyKey = requireNonEmpty(
    input.idempotencyKey,
    "idempotency key",
  );
  const ttlSeconds = input.ttlSeconds ?? DEFAULT_CLAIM_TTL_SECONDS;
  if (!Number.isSafeInteger(ttlSeconds) || ttlSeconds <= 0) {
    throw new Error("claim ttlSeconds must be a positive safe integer");
  }
  if (ttlSeconds > MAX_THREAD_CLAIM_TTL_SECONDS) {
    throw new ThreadClaimTtlError(ttlSeconds, MAX_THREAD_CLAIM_TTL_SECONDS);
  }
  const persist = deps.persistPlan ?? persistPlan;
  const deleteSecret = deps.deleteClaimSecret ?? deleteClaimSecret;
  const appendJournalDeps = deps.appendJournalDeps ?? {};

  return withThreadPlanLock(
    {
      vaultPath: input.vaultPath,
      notePath: input.notePath,
      planId,
      operationId: `claim:${randomUUID()}`,
    },
    async (initialPlan) => {
      const now = sampleNow(input.now);
      const { journalPath, ordered } = await prepareThreadMutation(
        { ...input, planId, actor: workerAgentId },
        initialPlan,
        now,
        appendJournalDeps,
      );
      let plan = initialPlan;
      plan = await synchronizeExpiredClaimsForPlan({
        vaultPath: input.vaultPath,
        notePath: input.notePath,
        planId,
        actor: workerAgentId,
        journalPath,
        plan,
        now,
        persist,
        deleteSecret,
        orderedSnapshot: ordered,
        appendJournalDeps,
      });
      let slice = findSlice(plan, sliceId);
      if (!isNonTerminal(slice)) {
        throw new Error(`slice "${sliceId}" is not claimable`);
      }
      if (slice.assigned_to !== workerAgentId) {
        throw new Error(
          `slice "${sliceId}" is assigned to ${slice.assigned_to ?? "nobody"}, not ${workerAgentId}`,
        );
      }
      let generation = requireGeneration(slice);
      const staleClaimIds: string[] = [];
      // Generations walked past in-memory while probing for a live claim
      // slot. Collected, never pruned here — pruning must wait until the
      // persist below durably lands (or a post-commit reread confirms it
      // did), otherwise a precommit failure would delete receipts a
      // same-key retry still needs against the still-durable old generation.
      const generationsToPrune: number[] = [];

      if (slice.claim && !hasLiveClaim(slice, now)) {
        staleClaimIds.push(slice.claim.claim_id);
        const staleGeneration = generation;
        generation += 1;
        generationsToPrune.push(staleGeneration);
        const expiredSlice: PlanSlice = {
          ...slice,
          generation,
          claim: undefined,
        };
        plan = replaceSlice(plan, sliceId, expiredSlice);
        slice = expiredSlice;
      }

      if (slice.claim) {
        const existing = await readClaimByIdempotency(
          input.vaultPath,
          planId,
          sliceId,
          generation,
          idempotencyKey,
        );
        if (
          claimMetadataMatches(
            slice,
            existing,
            planId,
            workerAgentId,
          )
        ) {
          const unclaimedPlan = replaceSlice(plan, sliceId, {
            ...slice,
            claim: undefined,
          });
          const readyBefore = readyIds(unclaimedPlan, now);
          const readyAfter = readyIds(plan, now);
          const operationKey = deriveClaimEventKey(
            planId,
            sliceId,
            workerAgentId,
            idempotencyKey,
          );
          await repairClaimSchedulerEvents({
            journalPath,
            planId,
            rev: plan.rev,
            actor: workerAgentId,
            operationKey,
            sliceId,
            readyBefore,
            readyAfter,
            plan,
            now,
            orderedSnapshot: ordered,
            appendJournalDeps,
          });
          return publicClaimResponse(existing.response);
        }
        throw new Error(`slice "${sliceId}" is already claimed`);
      }

      const readyBefore = readyIds(plan, now);
      const unmet = unmetDependencies(plan, sliceId);
      if (unmet.length > 0) {
        throw new Error(
          `slice "${sliceId}" dependencies are unresolved: ${unmet.join(", ")}`,
        );
      }

      const expiresAt = new Date(
        now.getTime() + ttlSeconds * 1_000,
      ).toISOString();
      let stored: Awaited<ReturnType<typeof createClaimSecret>> | undefined;
      // A response-loss orphan may occupy the deterministic idempotency path.
      // Never reattach an expired envelope: advance generation so the stale
      // token's identity can never collide with the new claim, even when its
      // best-effort deletion fails.
      for (let staleGenerations = 0; staleGenerations < 100; staleGenerations += 1) {
        const candidate = await createClaimSecret({
          vaultPath: input.vaultPath,
          planId,
          sliceId,
          generation,
          workerAgentId,
          idempotencyKey,
          expiresAt,
          rev: plan.rev + 1,
        });
        if (Date.parse(candidate.envelope.expires_at) > now.getTime()) {
          stored = candidate;
          break;
        }
        staleClaimIds.push(candidate.envelope.claim_id);
        const staleGeneration = generation;
        generation += 1;
        generationsToPrune.push(staleGeneration);
        slice = {
          ...slice,
          generation,
          claim: undefined,
        };
        plan = replaceSlice(plan, sliceId, slice);
      }
      if (!stored) {
        throw new Error(
          `slice "${sliceId}" has too many stale idempotency generations`,
        );
      }
      const nextSlice: PlanSlice = {
        ...slice,
        generation,
        attempt: requireAttempt(slice) + 1,
        claim: {
          claim_id: stored.envelope.claim_id,
          worker_agent_id: workerAgentId,
          claimed_at: now.toISOString(),
          expires_at: stored.envelope.expires_at,
        },
      };
      const next = replaceSlice(plan, sliceId, nextSlice);

      try {
        await persist(next, {
          vaultPath: input.vaultPath,
          notePath: input.notePath,
        });
      } catch (error) {
        // persistPlan writes the canonical note before appending history, and
        // now throws the typed PlanHistoryAppendError specifically for that
        // ordering — never a bare Error a caller could mistake for "nothing
        // was written". Its own construction proves the note write already
        // succeeded, so it is trusted directly. Any OTHER propagated failure
        // shape has no such guarantee, so it is instead judged by reconciling
        // against a fresh strict read while still holding the Thread lock.
        let committed = error instanceof PlanHistoryAppendError;
        if (!committed) {
          try {
            const canonical = await rehydrateAuthority(input);
            const durableSlice = findSlice(canonical, sliceId);
            committed =
              canonical.rev === stored.envelope.response.rev &&
              requireGeneration(durableSlice) === generation &&
              durableSlice.attempt === nextSlice.attempt &&
              durableSlice.claim?.claim_id === stored.envelope.claim_id &&
              durableSlice.claim.worker_agent_id === workerAgentId &&
              durableSlice.claim.claimed_at === now.toISOString() &&
              durableSlice.claim.expires_at === stored.envelope.expires_at;
          } catch {
            committed = false;
          }
        }
        // Either way this is surfaced to the caller below — a post-commit
        // failure must never be silently swallowed into a success return,
        // even though the claim is durable. What differs is only whether the
        // newly staged secret is safe to keep: on a committed write, retaining
        // it lets an identical idempotency retry replay the same token
        // against the now-durable claim instead of orphaning it.
        await deleteClaimSecretsBestEffort(
          input.vaultPath,
          planId,
          committed ? staleClaimIds : [stored.envelope.claim_id, ...staleClaimIds],
          deleteSecret,
        );
        if (committed) {
          await pruneCollectedGenerationsBestEffort(
            input.vaultPath,
            planId,
            sliceId,
            generationsToPrune,
          );
        }
        throw error;
      }
      await deleteClaimSecretsBestEffort(
        input.vaultPath,
        planId,
        staleClaimIds,
        deleteSecret,
      );
      await pruneCollectedGenerationsBestEffort(
        input.vaultPath,
        planId,
        sliceId,
        generationsToPrune,
      );
      const response = publicClaimResponse(stored.envelope.response);
      const readyAfter = readyIds(next, now);
      const claimOperationKey = deriveClaimEventKey(
        planId,
        sliceId,
        workerAgentId,
        idempotencyKey,
      );
      await recordThreadMutationEvents({
        journalPath,
        planId,
        rev: next.rev,
        actor: workerAgentId,
        operationKey: claimOperationKey,
        kind: "slice.claimed",
        sliceId,
        readyBefore,
        readyAfter,
        plan: next,
        now,
        orderedSnapshot: ordered,
        appendJournalDeps,
      });
      return response;
    },
  );
}

function requireEvidence(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`worker ${label} requires non-empty evidence`);
  }
  return value.trim();
}

function copyProposal(value: unknown): StructuralProposal {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("worker structural proposal is invalid");
  }
  const proposal = value as Record<string, unknown>;
  const reason = requireEvidence(proposal.reason, "structural proposal");
  if (proposal.kind === "contract") {
    if (
      !Array.isArray(proposal.slice_ids) ||
      proposal.slice_ids.some(
        (sliceId) => typeof sliceId !== "string" || sliceId.trim().length === 0,
      )
    ) {
      throw new Error("worker contraction proposal requires slice_ids");
    }
    return {
      kind: "contract",
      reason,
      slice_ids: proposal.slice_ids.map((sliceId) =>
        (sliceId as string).trim()
      ),
    };
  }
  if (proposal.kind !== "expand" && proposal.kind !== "split") {
    throw new Error("worker structural proposal kind is invalid");
  }
  if (!Array.isArray(proposal.slices)) {
    throw new Error("worker structural proposal requires slices");
  }
  const slices = proposal.slices.map((candidate) => {
    if (
      typeof candidate !== "object" ||
      candidate === null ||
      Array.isArray(candidate)
    ) {
      throw new Error("worker structural proposal slice is invalid");
    }
    const proposedSlice = candidate as Record<string, unknown>;
    const title = requireNonEmpty(
      proposedSlice.title as string,
      "proposal slice title",
    );
    if (
      proposedSlice.depends_on !== undefined &&
      (
        !Array.isArray(proposedSlice.depends_on) ||
        proposedSlice.depends_on.some(
          (dependency) =>
            typeof dependency !== "string" ||
            dependency.trim().length === 0,
        )
      )
    ) {
      throw new Error("worker proposal slice dependencies are invalid");
    }
    return {
      id:
        typeof proposedSlice.id === "string" &&
        proposedSlice.id.trim().length > 0
          ? proposedSlice.id.trim()
          : undefined,
      title,
      gate:
        typeof proposedSlice.gate === "string"
          ? proposedSlice.gate
          : undefined,
      depends_on: Array.isArray(proposedSlice.depends_on)
        ? proposedSlice.depends_on.map((dependency) =>
            (dependency as string).trim()
          )
        : undefined,
      evidence:
        typeof proposedSlice.evidence === "string"
          ? proposedSlice.evidence
          : undefined,
    };
  });
  return {
    kind: proposal.kind,
    reason,
    slices,
  };
}

function applyWorkerAction(
  plan: PlanArtifact,
  sliceId: string,
  action: WorkerUpdateAction,
): { plan: PlanArtifact; completed: boolean } {
  if (typeof action !== "object" || action === null || Array.isArray(action)) {
    throw new Error("worker action is invalid");
  }
  switch ((action as { action?: unknown }).action) {
    case "start":
      return {
        plan: updateSlice(plan, sliceId, "in_progress"),
        completed: false,
      };
    case "progress":
      return {
        plan: updateSlice(
          plan,
          sliceId,
          "in_progress",
          requireEvidence(
            (action as { evidence?: unknown }).evidence,
            "progress",
          ),
        ),
        completed: false,
      };
    case "block":
      return {
        plan: updateSlice(
          plan,
          sliceId,
          "blocked",
          requireEvidence(
            (action as { evidence?: unknown }).evidence,
            "block",
          ),
        ),
        completed: false,
      };
    case "scar": {
      const scarAction = action as {
        kind?: unknown;
        signal?: unknown;
        resolution?: unknown;
      };
      if (
        scarAction.kind !== "failed_command" &&
        scarAction.kind !== "dead_end" &&
        scarAction.kind !== "rejected_hypothesis"
      ) {
        throw new Error("worker scar kind is invalid");
      }
      const signal = requireEvidence(scarAction.signal, "scar");
      if (
        scarAction.resolution !== undefined &&
        typeof scarAction.resolution !== "string"
      ) {
        throw new Error("worker scar resolution is invalid");
      }
      return {
        plan: addScar(plan, {
          kind: scarAction.kind,
          signal,
          resolution:
            typeof scarAction.resolution === "string"
              ? scarAction.resolution
              : undefined,
        }),
        completed: false,
      };
    }
    case "propose_structure": {
      const proposal = copyProposal(
        (action as { proposal?: unknown }).proposal,
      );
      const slice = findSlice(plan, sliceId);
      const nextSlice: PlanSlice = {
        ...slice,
        proposals: [...(slice.proposals ?? []), proposal],
      };
      return {
        plan: replaceSlice(plan, sliceId, nextSlice),
        completed: false,
      };
    }
    case "complete":
      return {
        plan: updateSlice(
          plan,
          sliceId,
          "done",
          requireEvidence(
            (action as { evidence?: unknown }).evidence,
            "completion",
          ),
        ),
        completed: true,
      };
    default:
      throw new Error("unsupported worker action");
  }
}

/**
 * Root-cause fix for the claim-clearing idempotency hole: a successful
 * "complete" (or any other action that clears the live claim) leaves an
 * identical retry with no live claim to authenticate against, so the OLD
 * claim-scope check threw before an idempotency check was ever consulted.
 * The receipt below is checked first, before a live claim is required at
 * all, and authenticates the retry itself (a timing-safe digest of the
 * original claim token) — the ordered Thread journal alone was never
 * sufficient because it holds no secret to authenticate against, by design.
 */
async function loadWorkerUpdateReceipt(
  input: UpdateClaimedSliceInput,
  planId: string,
  sliceId: string,
  workerAgentId: string,
  idempotencyKey: string,
  kind: string,
  token: string,
  generation: number,
  claimId?: string,
): Promise<WorkerUpdateReceiptEnvelope | undefined> {
  const receipt = await readWorkerUpdateReceipt({
    vaultPath: input.vaultPath,
    planId,
    sliceId,
    workerAgentId,
    generation,
    idempotencyKey,
    claimId,
  });
  if (!receipt) return undefined;
  if (receipt.kind !== kind) {
    throw new ThreadEventIdempotencyConflictError(idempotencyKey);
  }
  if (!workerUpdateTokenMatches(token, receipt.token_digest)) {
    throw new Error("claim token mismatch");
  }
  return receipt;
}

export async function updateClaimedSlice(
  input: UpdateClaimedSliceInput,
  deps: ThreadWorkerDeps = {},
): Promise<ThreadMutationResult> {
  const planId = requireNonEmpty(input.planId, "plan id");
  const sliceId = requireNonEmpty(input.sliceId, "slice id");
  const workerAgentId = requireNonEmpty(
    input.workerAgentId,
    "worker agent id",
  );
  const token = requireNonEmpty(input.token, "claim token");
  const idempotencyKey = requireNonEmpty(
    input.idempotencyKey,
    "idempotency key",
  );
  const persist = deps.persistPlan ?? persistPlan;
  const deleteSecret = deps.deleteClaimSecret ?? deleteClaimSecret;
  const appendJournalDeps = deps.appendJournalDeps ?? {};
  const kind = workerEventKind(input.action);

  return withThreadPlanLock(
    {
      vaultPath: input.vaultPath,
      notePath: input.notePath,
      planId,
      operationId: `worker-update:${randomUUID()}`,
    },
    async (initialPlan) => {
      const now = sampleNow(input.now);
      const workerOperationKey = deriveWorkerEventKey(
        planId,
        sliceId,
        workerAgentId,
        idempotencyKey,
      );
      const receiptSlice = findSlice(initialPlan, sliceId);
      const receiptGeneration = requireGeneration(receiptSlice);
      const receiptClaimId = receiptSlice.claim?.claim_id;

      // Authenticate any existing receipt BEFORE journal reconciliation so a
      // wrong-token retry cannot append state.recovered.
      const existingReceipt = await loadWorkerUpdateReceipt(
        input,
        planId,
        sliceId,
        workerAgentId,
        idempotencyKey,
        kind,
        token,
        receiptGeneration,
        receiptClaimId,
      );
      if (
        existingReceipt?.status === "committed" &&
        committedReceiptAuthoritative(findSlice(initialPlan, sliceId), existingReceipt)
      ) {
        const { journalPath, ordered } = await prepareThreadMutation(
          { ...input, planId, actor: workerAgentId },
          initialPlan,
          now,
          appendJournalDeps,
        );
        void journalPath;
        void ordered;
        return receiptMutationResult(initialPlan, existingReceipt);
      }
      if (existingReceipt?.status === "committed") {
        throw new Error("claim scope mismatch");
      }

      const { journalPath, ordered } = await prepareThreadMutation(
        { ...input, planId, actor: workerAgentId },
        initialPlan,
        now,
        appendJournalDeps,
      );
      let plan = await synchronizeExpiredClaimsForPlan({
        vaultPath: input.vaultPath,
        notePath: input.notePath,
        planId,
        actor: workerAgentId,
        journalPath,
        plan: initialPlan,
        now,
        persist,
        deleteSecret,
        orderedSnapshot: ordered,
        appendJournalDeps,
      });
      const sliceBeforeExpiry = findSlice(initialPlan, sliceId);
      const sliceAfterExpiry = findSlice(plan, sliceId);
      if (
        sliceBeforeExpiry.claim &&
        !sliceAfterExpiry.claim &&
        !hasLiveClaim(sliceBeforeExpiry, now)
      ) {
        throw new Error("claim expired");
      }

      if (existingReceipt) {
        let looksCommitted = false;
        try {
          looksCommitted =
            plan.rev >= existingReceipt.rev &&
            structurallyEqual(
              findSlice(plan, sliceId),
              existingReceipt.response.slice,
            );
        } catch {
          looksCommitted = false;
        }
        if (looksCommitted) {
          const committed = await commitWorkerUpdateReceipt({
            vaultPath: input.vaultPath,
            planId,
            sliceId,
            workerAgentId,
            claimId: existingReceipt.claim_id,
            generation: existingReceipt.generation,
            idempotencyKey,
            kind,
            tokenDigest: existingReceipt.token_digest,
            rev: existingReceipt.rev,
            response: existingReceipt.response,
          });
          return receiptMutationResult(plan, committed);
        }
      }

      const slice = findSlice(plan, sliceId);
      const claim = slice.claim;
      if (!claim || claim.worker_agent_id !== workerAgentId) {
        throw new Error("claim scope mismatch");
      }
      const generation = requireGeneration(slice);
      const stored = await verifyClaimToken({
        vaultPath: input.vaultPath,
        planId,
        sliceId,
        generation,
        workerAgentId,
        token,
        now,
        claimId: claim.claim_id,
      });
      if (
        stored.envelope.plan_id !== planId ||
        stored.envelope.slice_id !== sliceId ||
        stored.envelope.generation !== generation ||
        stored.envelope.worker_agent_id !== workerAgentId ||
        stored.envelope.claim_id !== claim.claim_id ||
        stored.envelope.expires_at !== claim.expires_at
      ) {
        throw new Error("claim scope mismatch");
      }
      if (!isNonTerminal(slice)) {
        throw new Error(`slice "${sliceId}" is not worker-updatable`);
      }

      const planBefore = plan;
      const readyBefore = readyIds(plan, now);
      const applied = applyWorkerAction(plan, sliceId, input.action);
      let next = applied.plan;
      if (applied.completed) {
        const completedSlice = findSlice(next, sliceId);
        next = replaceSlice(next, sliceId, {
          ...completedSlice,
          claim: undefined,
        });
      }

      const intendedRev = plan.rev + 1;
      const tokenDigest = hashWorkerUpdateToken(token);
      const publicResponse: WorkerUpdateReceiptResponse = {
        slice: findSlice(next, sliceId),
        ready_before: readyBefore,
        ready_after: readyIds(next, now),
        rev: intendedRev,
      };

      await writePendingWorkerUpdateReceipt({
        vaultPath: input.vaultPath,
        planId,
        sliceId,
        workerAgentId,
        claimId: claim.claim_id,
        generation,
        idempotencyKey,
        kind,
        tokenDigest,
        rev: intendedRev,
        response: publicResponse,
      });

      try {
        await persist(next, {
          vaultPath: input.vaultPath,
          notePath: input.notePath,
        });
      } catch (error) {
        let committed = error instanceof PlanHistoryAppendError;
        if (!committed) {
          try {
            const canonical = await rehydrateAuthority(input);
            committed =
              canonical.rev === intendedRev &&
              structurallyEqual(
                findSlice(canonical, sliceId),
                publicResponse.slice,
              );
          } catch {
            committed = false;
          }
        }
        if (committed) {
          await commitWorkerUpdateReceipt({
            vaultPath: input.vaultPath,
            planId,
            sliceId,
            workerAgentId,
            claimId: claim.claim_id,
            generation,
            idempotencyKey,
            kind,
            tokenDigest,
            rev: intendedRev,
            response: publicResponse,
          }).catch(() => {});
          // Complete is the opposite of claim: the durable note already has
          // no live claim, so the mode-0600 envelope is an orphan and must
          // go even when history append failed after the note write.
          if (applied.completed) {
            await deleteClaimSecretsBestEffort(
              input.vaultPath,
              planId,
              [claim.claim_id],
              deleteSecret,
            );
          }
        }
        throw error;
      }

      if (applied.completed) {
        await deleteClaimSecretsBestEffort(
          input.vaultPath,
          planId,
          [claim.claim_id],
          deleteSecret,
        );
      }
      await commitWorkerUpdateReceipt({
        vaultPath: input.vaultPath,
        planId,
        sliceId,
        workerAgentId,
        claimId: claim.claim_id,
        generation,
        idempotencyKey,
        kind,
        tokenDigest,
        rev: intendedRev,
        response: publicResponse,
      });
      const result = mutationResult(next, sliceId, readyBefore, now);
      const supplemental: Array<{
        idempotencyKey: string;
        kind: string;
        sliceId?: string;
        payload?: Record<string, unknown>;
      }> = [];
      if (input.action.action === "block") {
        supplemental.push({
          idempotencyKey: deriveSystemEventKey(
            "thread.attention_required",
            planId,
            sliceId,
            "block",
            String(next.rev),
          ),
          kind: "thread.attention_required",
          sliceId,
          payload: attentionPayload(sliceId, "block"),
        });
      }
      await recordThreadMutationEvents({
        journalPath,
        planId,
        rev: next.rev,
        actor: workerAgentId,
        operationKey: workerOperationKey,
        kind,
        sliceId,
        readyBefore: result.ready_before,
        readyAfter: result.ready_after,
        plan: next,
        planBefore,
        now,
        orderedSnapshot: ordered,
        appendJournalDeps,
        supplementalEvents: supplemental.length > 0 ? supplemental : undefined,
      });
      return result;
    },
  );
}

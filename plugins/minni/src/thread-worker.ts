import { randomUUID } from "node:crypto";
import path from "node:path";

import {
  addScar,
  journalPathFor,
  persistPlan,
  rehydratePlan,
  unmetDependencies,
  updateSlice,
  PlanHistoryAppendError,
  type PlanArtifact,
  type PlanSlice,
  type PlanSliceStatus,
  type StructuralProposal,
  type UpdateSliceOptions,
} from "./plan.js";
import type { ScarTissueEntry } from "./task.js";
import {
  appendOrderedThreadEvent,
  reconcileThreadJournal,
} from "./thread-events.js";
import {
  createClaimSecret,
  deleteClaimSecret,
  readClaimByIdempotency,
  verifyClaimToken,
  type ThreadClaimResponse,
} from "./thread-claims.js";
import { withThreadLock } from "./thread-lock.js";

const DEFAULT_CLAIM_TTL_SECONDS = 10 * 60;

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
  idempotencyKey?: string;
}

export interface ThreadWorkerDeps {
  persistPlan?: typeof persistPlan;
  deleteClaimSecret?: typeof deleteClaimSecret;
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

function readyIds(plan: PlanArtifact, now: Date): string[] {
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

function readyIdsEqual(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  const sortedLeft = [...left].sort();
  const sortedRight = [...right].sort();
  return sortedLeft.every((id, index) => id === sortedRight[index]);
}

async function reconcileThreadState(
  input: ThreadPlanTarget & { actor: string },
  plan: PlanArtifact,
): Promise<void> {
  await reconcileThreadJournal({
    journalPath: journalPathFor(input.notePath, input.planId),
    notePath: input.notePath,
    planId: input.planId,
    rev: plan.rev,
    actor: input.actor,
  });
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

async function recordThreadMutationEvents(input: {
  notePath: string;
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
  now: Date;
}): Promise<void> {
  const journalPath = journalPathFor(input.notePath, input.planId);
  const at = input.now.toISOString();
  try {
    await appendOrderedThreadEvent({
      journalPath,
      planId: input.planId,
      rev: input.rev,
      idempotencyKey: input.operationKey,
      actor: input.actor,
      kind: input.kind,
      at,
      sliceId: input.sliceId,
      payload: input.payload,
    });
    if (!readyIdsEqual(input.readyBefore, input.readyAfter)) {
      await appendOrderedThreadEvent({
        journalPath,
        planId: input.planId,
        rev: input.rev,
        idempotencyKey: `${input.operationKey}:ready`,
        actor: input.actor,
        kind: "ready.changed",
        at,
        payload: {
          slices: readySlices(input.plan, input.now).map((slice) => ({
            id: slice.id,
            title: slice.title,
          })),
        },
      });
    }
  } catch {
    // The note is already durable; the next locked mutation reconciles first.
  }
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
  const profile = assignmentProfile(input.assignmentProfile);
  const persist = deps.persistPlan ?? persistPlan;
  const deleteSecret = deps.deleteClaimSecret ?? deleteClaimSecret;

  return withThreadPlanLock(
    {
      vaultPath: input.vaultPath,
      notePath: input.notePath,
      planId,
      operationId: `assign:${randomUUID()}`,
    },
    async (plan) => {
      const now = sampleNow(input.now);
      await reconcileThreadState(
        { ...input, planId, actor: workerAgentId },
        plan,
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
      const nextSlice: PlanSlice = {
        ...slice,
        assigned_to: workerAgentId,
        assignment_profile: profile,
        generation: generation + (reassigned ? 1 : 0),
        claim: undefined,
      };
      const next = replaceSlice(plan, sliceId, nextSlice);
      await persist(next, {
        vaultPath: input.vaultPath,
        notePath: input.notePath,
      });
      if (slice.claim) {
        await deleteClaimSecretsBestEffort(
          input.vaultPath,
          planId,
          [slice.claim.claim_id],
          deleteSecret,
        );
      }
      const result = mutationResult(next, sliceId, readyBefore, now);
      await recordThreadMutationEvents({
        notePath: input.notePath,
        planId,
        rev: next.rev,
        actor: workerAgentId,
        operationKey: `assign:${sliceId}:${workerAgentId}:${nextSlice.generation}`,
        kind: "slice.assigned",
        sliceId,
        readyBefore: result.ready_before,
        readyAfter: result.ready_after,
        plan: next,
        now,
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
  const persist = deps.persistPlan ?? persistPlan;
  const deleteSecret = deps.deleteClaimSecret ?? deleteClaimSecret;

  return withThreadPlanLock(
    {
      vaultPath: input.vaultPath,
      notePath: input.notePath,
      planId,
      operationId: `claim:${randomUUID()}`,
    },
    async (initialPlan) => {
      const now = sampleNow(input.now);
      await reconcileThreadState(
        { ...input, planId, actor: workerAgentId },
        initialPlan,
      );
      let plan = initialPlan;
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

      if (slice.claim && !hasLiveClaim(slice, now)) {
        staleClaimIds.push(slice.claim.claim_id);
        generation += 1;
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
          return publicClaimResponse(existing.response);
        }
        throw new Error(`slice "${sliceId}" is already claimed`);
      }

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
        generation += 1;
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
        throw error;
      }
      await deleteClaimSecretsBestEffort(
        input.vaultPath,
        planId,
        staleClaimIds,
        deleteSecret,
      );
      const response = publicClaimResponse(stored.envelope.response);
      const readyAfter = readyIds(next, now);
      await recordThreadMutationEvents({
        notePath: input.notePath,
        planId,
        rev: next.rev,
        actor: workerAgentId,
        operationKey: idempotencyKey,
        kind: "slice.claimed",
        sliceId,
        readyBefore: readyAfter,
        readyAfter,
        plan: next,
        now,
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
  const persist = deps.persistPlan ?? persistPlan;
  const deleteSecret = deps.deleteClaimSecret ?? deleteClaimSecret;

  return withThreadPlanLock(
    {
      vaultPath: input.vaultPath,
      notePath: input.notePath,
      planId,
      operationId: `worker-update:${randomUUID()}`,
    },
    async (plan) => {
      const now = sampleNow(input.now);
      await reconcileThreadState(
        { ...input, planId, actor: workerAgentId },
        plan,
      );
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
      await persist(next, {
        vaultPath: input.vaultPath,
        notePath: input.notePath,
      });
      if (applied.completed) {
        await deleteClaimSecretsBestEffort(
          input.vaultPath,
          planId,
          [claim.claim_id],
          deleteSecret,
        );
      }
      const result = mutationResult(next, sliceId, readyBefore, now);
      const operationKey =
        input.idempotencyKey?.trim() ||
        `${sliceId}:${input.action.action}:${generation}`;
      await recordThreadMutationEvents({
        notePath: input.notePath,
        planId,
        rev: next.rev,
        actor: workerAgentId,
        operationKey,
        kind: workerEventKind(input.action),
        sliceId,
        readyBefore: result.ready_before,
        readyAfter: result.ready_after,
        plan: next,
        now,
      });
      return result;
    },
  );
}

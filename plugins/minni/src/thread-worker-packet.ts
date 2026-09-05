import { DEFAULT_AGENT_ID } from "./config.js";
import type { PlanArtifact, PlanSlice } from "./plan.js";
import { prepareTask } from "./task.js";
import type { PreparedTaskPacket, TaskProfile } from "./task.js";
import type { ThreadClaimResponse } from "./thread-claims.js";

/**
 * Wave 2 Team adapter: one worker packet, built after claim.
 *
 * Order is assign → claim → this function. It is not a daemon hydrator,
 * not an MCP tool, and not part of `minni_team_runtime`. HydrationPacket
 * remains coordinator-side prepare_task context; this packet is the
 * worker contract. `claim_token` copies `ThreadClaimResponse.token`.
 * The live worker tool stays `minni_thread_worker_update` (arg `claim_token`).
 */
export const WORKER_PACKET_ALLOWED_MUTATIONS = [
  "start",
  "progress",
  "block",
  "scar",
  "propose_structure",
  "complete",
] as const;

export type WorkerPacketMutation = (typeof WORKER_PACKET_ALLOWED_MUTATIONS)[number];

export interface WorkerPacketSlice {
  title: string;
  status: PlanSlice["status"];
  gate?: string;
  depends_on: string[];
  assigned_to?: string;
}

export interface WorkerPacketEvidenceRef {
  slice_id: string;
  paths: string[];
}

export interface WorkerPacket {
  plan_id: string;
  slice_id: string;
  generation: number;
  claim_token: string;
  slice: WorkerPacketSlice;
  goal: string;
  constraints: string[];
  evidence_refs: WorkerPacketEvidenceRef[];
  recall: PreparedTaskPacket;
  allowed_mutations: readonly WorkerPacketMutation[];
}

export interface BuildWorkerPacketInput {
  claim: ThreadClaimResponse;
  plan: PlanArtifact;
  vaultPath: string;
  workspaceId?: string;
  profile?: TaskProfile;
  limit?: number;
  includeVault?: boolean;
  useAfm?: boolean;
}

export interface BuildWorkerPacketDeps {
  prepare?: typeof prepareTask;
}

const PATH_LIKE = /(?:\.{1,2}\/)?(?:[\w.-]+\/)+[\w.-]+\.[A-Za-z0-9]+/g;

function evidencePaths(evidence: string | undefined): string[] {
  if (!evidence) return [];
  return [...new Set(evidence.match(PATH_LIKE) ?? [])];
}

function completedDepRefs(plan: PlanArtifact, slice: PlanSlice): WorkerPacketEvidenceRef[] {
  const refs: WorkerPacketEvidenceRef[] = [];
  for (const depId of slice.depends_on ?? []) {
    const dep = plan.slices.find((candidate) => candidate.id === depId);
    if (!dep) continue;
    if (dep.status !== "done" && dep.status !== "superseded") continue;
    refs.push({
      slice_id: dep.id,
      paths: evidencePaths(dep.evidence),
    });
  }
  return refs;
}

function claimedSlice(plan: PlanArtifact, claim: ThreadClaimResponse): PlanSlice {
  if (claim.plan_id !== plan.plan_id) {
    throw new Error(
      `worker packet plan_id mismatch: claim ${claim.plan_id} vs plan ${plan.plan_id}`,
    );
  }
  const token = claim.token?.trim();
  if (!token) {
    throw new Error("worker packet requires a claim token");
  }
  const slice = plan.slices.find((candidate) => candidate.id === claim.slice_id);
  if (!slice) {
    throw new Error(`worker packet slice not found: ${claim.slice_id}`);
  }
  if (!slice.claim || slice.claim.claim_id !== claim.claim_id) {
    throw new Error(`slice "${claim.slice_id}" is not claimed`);
  }
  const generation = slice.generation ?? 0;
  if (generation !== claim.generation) {
    throw new Error(`worker packet generation mismatch for slice "${claim.slice_id}"`);
  }
  return slice;
}

function packetSlice(slice: PlanSlice): WorkerPacketSlice {
  return {
    title: slice.title,
    status: slice.status,
    depends_on: [...(slice.depends_on ?? [])],
    ...(slice.gate ? { gate: slice.gate } : {}),
    ...(slice.assigned_to ? { assigned_to: slice.assigned_to } : {}),
  };
}

export async function buildWorkerPacketAfterClaim(
  input: BuildWorkerPacketInput,
  deps: BuildWorkerPacketDeps = {},
): Promise<WorkerPacket> {
  const slice = claimedSlice(input.plan, input.claim);
  const prepare = deps.prepare ?? prepareTask;
  // G11: daemon recall stays on the server-provisioned principal. Do not put
  // the claim token in the prepare_task task string — recall is a source,
  // and prepare_task may audit/index that text.
  const recall = await prepare({
    task: `${input.plan.goal}\n\nSlice: ${slice.title}`,
    agentId: DEFAULT_AGENT_ID,
    recallAgentId: DEFAULT_AGENT_ID,
    vaultPath: input.vaultPath,
    workspaceId: input.workspaceId,
    profile: input.profile,
    limit: input.limit,
    includeVault: input.includeVault,
    useAfm: input.useAfm,
  });

  return {
    plan_id: input.claim.plan_id,
    slice_id: input.claim.slice_id,
    generation: input.claim.generation,
    claim_token: input.claim.token,
    slice: packetSlice(slice),
    goal: input.plan.goal,
    constraints: [...input.plan.constraints],
    evidence_refs: completedDepRefs(input.plan, slice),
    recall,
    allowed_mutations: WORKER_PACKET_ALLOWED_MUTATIONS,
  };
}

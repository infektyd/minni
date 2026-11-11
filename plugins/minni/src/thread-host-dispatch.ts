import type { WorkerPacket } from "./thread-worker-packet.js";

/**
 * Wave 3 Team adapter: one post-claim WorkerPacket, dispatched through
 * whatever start surface that host actually has.
 *
 * This sits next to `thread-worker-packet.ts`. It is not a daemon hydrator,
 * not an MCP tool, and not part of `minni_team_runtime`. Completion stays
 * `minni_thread_worker_update` (MCP arg `claim_token`, domain `token`).
 * `minni_team_evidence` is a promotion summary, not the worker SoT.
 *
 * Host start is not invented here. grok has no worker-start. agy's default
 * readonly allowlist cannot call the completion tool. A named worker
 * allowlist exists (`minni_thread_worker_update` only) and is still not
 * wet. Codex keeps its documented coordinator map (one packet → one
 * subagent) and marks wet dispatch UNPROVEN. Cursor is out of this first
 * wet set.
 */
export const FIRST_WET_SET = ["grok", "agy", "codex"] as const;
export type FirstWetSetHost = (typeof FIRST_WET_SET)[number];

export const WORKER_COMPLETION_TOOL = "minni_thread_update";

/** grok worker-start is missing. Do not fill this with a hook or handoff. */
export const GROK_WORKER_START: null = null;

/**
 * agy / Antigravity default auto-grants. Copied from
 * `MINNI_READONLY_TOOLS` in `src/minni/wire/writers.py` and
 * `plugins/minni/skills/minni-install/scripts/propagate.py`.
 * Read-only tools from the 12-tool v1 surface only; no mutating thread tools.
 */
export const AGY_DEFAULT_ALLOWLIST = [
  "minni_recall",
  "minni_drill",
  "minni_status",
  "minni_list_candidates",
  "minni_handoff_inbox",
  "minni_thread_status",
] as const;

/**
 * Named agy worker allowlist. Copied from `MINNI_WORKER_TOOLS` in
 * `src/minni/wire/writers.py`. Scoped to the 12-tool v1 surface.
 * Default dispatch still uses `AGY_DEFAULT_ALLOWLIST` and stays CANNOT.
 */
export const AGY_WORKER_ALLOWLIST = [
  "minni_thread_update",
  "minni_thread_scar",
  "minni_learn",
] as const;

export interface AgyWorkerAllowlistReport {
  host: "agy";
  exists: true;
  allowlist: typeof AGY_WORKER_ALLOWLIST;
  injectStepsIsStart: false;
  spawned: false;
}

/** Named list exists. Still not wet. injectSteps is not start. */
export function reportAgyWorkerAllowlist(): AgyWorkerAllowlistReport {
  return {
    host: "agy",
    exists: true,
    allowlist: AGY_WORKER_ALLOWLIST,
    injectStepsIsStart: false,
    spawned: false,
  };
}

export interface WorkerCompletion {
  tool: typeof WORKER_COMPLETION_TOOL;
  arg: "claim_token";
  domain: "token";
}

export type HostDispatchResult =
  | {
      host: "grok";
      outcome: "MISSING";
      start: "worker-start";
      spawned: false;
    }
  | {
      host: "agy";
      outcome: "CANNOT";
      reason: "default-allowlist";
      allowlist: typeof AGY_DEFAULT_ALLOWLIST;
      missing: readonly [typeof WORKER_COMPLETION_TOOL];
      injectStepsIsStart: false;
      spawned: false;
    }
  | {
      host: "codex";
      outcome: "UNPROVEN";
      mapping: "worker-packet-to-subagent";
      replaced: "temporaryProfile+HydrationPacket";
      wet: false;
      spawned: false;
      workerPacket: WorkerPacket;
      completion: WorkerCompletion;
    };

export interface DispatchWorkerPacketInput {
  host: FirstWetSetHost;
  packet: WorkerPacket;
}

function assertWorkerPacket(packet: WorkerPacket): void {
  if (!packet?.claim_token?.trim()) {
    throw new Error("host dispatch requires a WorkerPacket claim_token");
  }
  if (!packet.slice_id?.trim()) {
    throw new Error("host dispatch requires a WorkerPacket slice_id");
  }
  if (!packet.plan_id?.trim()) {
    throw new Error("host dispatch requires a WorkerPacket plan_id");
  }
  if (!packet.allowed_mutations?.length) {
    throw new Error("host dispatch requires a WorkerPacket allowed_mutations");
  }
}

function dispatchGrok(): Extract<HostDispatchResult, { host: "grok" }> {
  return {
    host: "grok",
    outcome: "MISSING",
    start: "worker-start",
    spawned: false,
  };
}

function dispatchAgy(): Extract<HostDispatchResult, { host: "agy" }> {
  return {
    host: "agy",
    outcome: "CANNOT",
    reason: "default-allowlist",
    allowlist: AGY_DEFAULT_ALLOWLIST,
    missing: [WORKER_COMPLETION_TOOL],
    injectStepsIsStart: false,
    spawned: false,
  };
}

function dispatchCodex(packet: WorkerPacket): Extract<HostDispatchResult, { host: "codex" }> {
  // Documented coordinator map used to be temporaryProfile + HydrationPacket
  // → one Codex subagent. Wave 3 replaces that input with the Wave 2 packet.
  // There is still no wet-tested start in this repo, so spawned stays false.
  return {
    host: "codex",
    outcome: "UNPROVEN",
    mapping: "worker-packet-to-subagent",
    replaced: "temporaryProfile+HydrationPacket",
    wet: false,
    spawned: false,
    workerPacket: packet,
    completion: {
      tool: WORKER_COMPLETION_TOOL,
      arg: "claim_token",
      domain: "token",
    },
  };
}

export function dispatchWorkerPacket(input: DispatchWorkerPacketInput): HostDispatchResult {
  assertWorkerPacket(input.packet);
  switch (input.host) {
    case "grok":
      return dispatchGrok();
    case "agy":
      return dispatchAgy();
    case "codex":
      return dispatchCodex(input.packet);
    default: {
      const _exhaustive: never = input.host;
      throw new Error(`host out of first wet set: ${String(_exhaustive)}`);
    }
  }
}

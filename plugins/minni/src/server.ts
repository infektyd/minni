import { randomUUID } from "node:crypto";
import { mkdirSync, openSync, readFileSync, realpathSync, unlinkSync, writeFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import {
  DEFAULT_AGENT_ID,
  DEFAULT_VAULT_PATH,
  DEFAULT_WORKSPACE_ID,
  normalizeWorkspaceId,
  resolveMcpSessionIdentity,
  resolveSessionWorkspaceId,
} from "./config.js";
import { assessLearningQualityAsync, auditSafeTitle, flagsSensitiveMaterial } from "./policy.js";
import {
  ackHandoff,
  drillMemory,
  gateSharedOperation,
  handoffMemory,
  identityDenialFrom,
  isDaemonResultEmpty,
  isRecallNoiseLastEnabled,
  isSharedGateUnavailable,
  learnMemory,
  listPendingHandoffs,
  recallAuditHits,
  recallCrossAgentDegrade,
  recallMemory,
  recallResponseText,
  recoveryRouteFrom,
  shouldPrescanVault,
  statusAndAudit,
} from "./daemon_transport.js";
import {
  buildHandoffPacket,
  extractScarTissue,
  filterSafeVaultResults,
  type ScarTissueEntry,
} from "./task.js";
import {
  activatePlanChecked,
  addScar,
  appendJournal,
  clearActivePlan,
  compactPlanView,
  createPlan,
  findPlanNote,
  getActivePlan,
  isTerminalPlanStatus,
  persistPlan,
  PlanHistoryAppendError,
  rehydratePlan,
  resolvePlanIdOrActive,
  shelfDrift,
  unmetDependencies,
  type PlanArtifact,
} from "./plan.js";
import {
  auditTail,
  recordAudit,
  searchVaultNotes,
  vaultFirstLearn,
} from "./vault.js";
import { wrapEnvelope } from "./agent_envelope.js";
import { disarmRecallStateOnConsult } from "./recall-state.js";
import {
  applyOrchestratorSliceUpdate,
  deleteClaimSecretsBestEffort,
  drainPendingWorkerWritesForVault,
  prepareThreadMutation,
  pruneSliceReceiptsOnGenerationAdvance,
  readyIds,
  recordThreadMutationEvents,
  synchronizeExpiredClaims,
  threadWorkerErrorText,
  withThreadPlanLock,
} from "./thread-worker.js";
import { deriveSystemEventKey } from "./thread-events.js";
import {
  acknowledgeSelfAuthoredPrefixInVault,
  ensureSessionRelayCursor,
  formatSessionSubscriberId,
  vaultPrincipalSubscriberId,
} from "./thread-notification-relay.js";
import {
  drainStatusForModel,
  modelListCandidatesPayload,
  modelSharedGatePayload,
  redactLocalValue,
} from "./list-candidates-model.js";
import { MINNI_INSTRUCTIONS } from "./mcp-instructions.js";
import { useStatusBlock } from "./use-report.js";

export { MINNI_INSTRUCTIONS };

// #339: searchVaultNotes reads/scores/snippets every markdown file in the
// vault's wiki tree regardless of `limit` — the limit is a post-scoring
// slice only, so asking for more than the final cap costs nothing extra.
const VAULT_SEARCH_OVERFETCH_MULTIPLIER = 3;

// §4.5 (P1-3, N4): Keep an open read-only file descriptor to resolved server.js
// for the lifetime of the process so `lsof -Fn -p <pid>` deterministically lists
// the loaded version directory, and write a runtime lease file in
// ~/.minni/run/mcp-servers/<pid>.json.
const selfRealpath = realpathSync(fileURLToPath(import.meta.url));
const versionRoot = path.dirname(path.dirname(selfRealpath));
const pinFd = openSync(selfRealpath, "r");
(globalThis as Record<string, unknown>).__minniSelfPinFd = pinFd;

function resolveServerGitSha(verRoot: string): string {
  for (const rel of ["payload-manifest.json", "dist/build-manifest.json"]) {
    try {
      const parsed = JSON.parse(readFileSync(path.join(verRoot, rel), "utf8")) as Record<string, unknown>;
      if (typeof parsed.git_sha === "string" && parsed.git_sha) {
        return parsed.git_sha;
      }
    } catch {
      // ignore missing/unreadable manifest
    }
  }
  return "";
}

function registerRuntimeServerLease(): void {
  try {
    const homeDir = process.env.HOME || os.homedir();
    const vaultEnv = process.env.MINNI_VAULT_PATH || "";
    let leaseDir = process.env.MINNI_RUNTIME_LEASE_DIR || "";
    if (!leaseDir) {
      if (
        vaultEnv &&
        homeDir &&
        !path.resolve(vaultEnv).startsWith(path.resolve(homeDir) + path.sep)
      ) {
        leaseDir = path.join(vaultEnv, ".runtime", "mcp-servers");
      } else if (homeDir) {
        leaseDir = path.join(homeDir, ".minni", "run", "mcp-servers");
      }
    }
    if (!leaseDir) return;
    mkdirSync(leaseDir, { recursive: true });
    const leasePath = path.join(leaseDir, `${process.pid}.json`);
    const payload = {
      pid: process.pid,
      ppid: process.ppid,
      agent_id: DEFAULT_AGENT_ID,
      realpath: selfRealpath,
      version_root: versionRoot,
      git_sha: resolveServerGitSha(versionRoot),
      started_at: new Date().toISOString(),
    };
    writeFileSync(leasePath, JSON.stringify(payload, null, 2) + "\n", "utf8");
    const cleanupLease = () => {
      try {
        unlinkSync(leasePath);
      } catch {
        // ignore
      }
    };
    process.once("exit", cleanupLease);
    process.once("SIGTERM", () => {
      cleanupLease();
      process.exit(0);
    });
    process.once("SIGINT", () => {
      cleanupLease();
      process.exit(0);
    });
  } catch {
    // Non-fatal in read-only environments
  }
}

registerRuntimeServerLease();

function textResult(text: string) {
  return {
    content: [{ type: "text" as const, text }],
  };
}

async function requireSharedGate(
  operation: string,
  details?: Record<string, unknown>,
): Promise<ReturnType<typeof textResult> | undefined> {
  const gate = await gateSharedOperation({
    operation,
    agentId: DEFAULT_AGENT_ID,
    workspaceId: DEFAULT_WORKSPACE_ID,
    details,
  });
  const data = gate.data as Record<string, unknown> | undefined;
  // Shape-based, not ok-based: the RPC client now reports a recovery envelope
  // as a failed call (#132 P1), but this gate's structured rejection payload
  // must stay identical for both the old (ok-wrapped) and new shapes.
  if (data?.status === "recovery_required") {
    return textResult(
      JSON.stringify(
        modelSharedGatePayload({
          status: "gate-rejected",
          operation,
          reason: data.reason ?? "recovery_required",
          gate: data,
        }),
        null,
        2,
      ),
    );
  }
  if (gate.ok) return undefined;
  const error = gate.error ?? "";
  if (isSharedGateUnavailable(error)) {
    return textResult(
      JSON.stringify(
        modelSharedGatePayload({
          status: "gate-unavailable",
          operation,
          error,
        }),
        null,
        2,
      ),
    );
  }
  return textResult(
    JSON.stringify(
      modelSharedGatePayload({
        status: "gate-rejected",
        operation,
        gate,
      }),
      null,
      2,
    ),
  );
}

// Task 6: v1 MCP tool registrations and shared helpers

async function persistPlanThenRevokeClaimSecrets(
  plan: PlanArtifact,
  opts: { vaultPath: string; notePath: string },
  planId: string,
  claimIdsToDelete: Iterable<string>,
): Promise<void> {
  try {
    await persistPlan(plan, opts);
  } catch (error) {
    if (error instanceof PlanHistoryAppendError) {
      await deleteClaimSecretsBestEffort(
        opts.vaultPath,
        planId,
        claimIdsToDelete,
      );
    }
    throw error;
  }
  await deleteClaimSecretsBestEffort(opts.vaultPath, planId, claimIdsToDelete);
}

function threadWorkerErrorResult(
  operation: string,
  error: unknown,
): ReturnType<typeof textResult> {
  const message = threadWorkerErrorText(error);
  const errorCode =
    error instanceof Error ? (error as unknown as { code?: unknown }).code : undefined;
  const code = typeof errorCode === "string" ? errorCode : undefined;
  return textResult(
    JSON.stringify(
      {
        status: "error",
        operation,
        error: message,
        ...(code ? { code } : {}),
      },
      null,
      2,
    ),
  );
}

const server = new McpServer(
  {
    name: "minni",
    version: JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version,
  },
  { instructions: MINNI_INSTRUCTIONS },
);

const MCP_PROCESS_SESSION_ID = `mcp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

// ---------------------------------------------------------------------------
// 1. minni_recall
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_recall",
  {
    title: "Minni Recall",
    description:
      "Recall Minni context and log the lookup in the vault.",
    inputSchema: {
      query: z.string().min(1),
      layer: z
        .enum(["identity", "episodic", "knowledge", "artifact"])
        .optional(),
      limit: z.number().int().min(1).max(20).optional(),
      workspaceId: z.string().optional(),
      scope: z.enum(["personal", "combined", "both"]).optional(),
      cross_agent: z.boolean().optional(),
      // G12: vaultPath removed from model-facing schema (SEC-003). Model cannot redirect recall/search to arbitrary vaults.
      includeVault: z.boolean().optional(),
    },
  },
  async ({ query, layer, limit, workspaceId, scope, cross_agent, includeVault }) => {
    const mcpSession = resolveMcpSessionIdentity(DEFAULT_VAULT_PATH);
    await disarmRecallStateOnConsult(DEFAULT_VAULT_PATH, mcpSession).catch(() => false);
    const effectiveWorkspaceId = resolveSessionWorkspaceId(DEFAULT_VAULT_PATH, workspaceId);
    // X5: run the daemon recall FIRST. Its results are workspace/agent-scoped and
    // subject to read policy. The local searchVaultNotes pre-scan is NOT scoped, so
    // it must only be used as an offline fallback (daemon unreachable) — never
    // injected alongside a successful daemon recall where it would leak
    // workspace-foreign snippets past the daemon boundary.
    const result = await recallMemory({
      query,
      layer,
      limit,
      scope,
      crossAgent: cross_agent,
      workspaceId: effectiveWorkspaceId,
      agentId: DEFAULT_AGENT_ID, // G11: server-side default only (model no longer supplies agentId)
      sessionId: mcpSession.sessionId ?? MCP_PROCESS_SESSION_ID,
    });
    const daemonOk = result.ok && !!result.data;
    const daemonEmpty = daemonOk && isDaemonResultEmpty(result.data);
    const denial = identityDenialFrom(result.data);
    const identityDenied = recoveryRouteFrom(result.data) !== undefined || denial !== undefined;
    const vaultResultCap = Math.min(limit ?? 5, 8);
    const noiseLast = isRecallNoiseLastEnabled(DEFAULT_VAULT_PATH);
    const vaultResults = !identityDenied && shouldPrescanVault(daemonOk, includeVault !== false, daemonEmpty)
      ? filterSafeVaultResults(
          await searchVaultNotes(
            DEFAULT_VAULT_PATH,
            query,
            vaultResultCap * VAULT_SEARCH_OVERFETCH_MULTIPLIER,
            undefined,
            { rank: noiseLast ? "noise_last" : "legacy" },
          ),
        ).slice(0, vaultResultCap)
      : [];
    const degradeText = await recallCrossAgentDegrade(
      { query, layer, limit, workspaceId: effectiveWorkspaceId, agentId: DEFAULT_AGENT_ID },
      cross_agent === true,
      denial,
    );
    const responseText = degradeText ?? recallResponseText(query, result, vaultResults);
    await recordAudit(DEFAULT_VAULT_PATH, {
      tool: "minni_recall",
      summary: query,
      details: {
        ok: result.ok,
        layer,
        limit,
        workspaceId,
        scope,
        cross_agent,
        agentId: DEFAULT_AGENT_ID,
        includeVault: includeVault !== false,
        vaultMatches: vaultResults.map((match) => match.relativePath),
        ...(daemonOk && !identityDenied ? recallAuditHits(result.data) : {}),
        error: result.error,
        crossAgentDegraded: degradeText !== undefined,
        session_id: mcpSession.sessionId ?? MCP_PROCESS_SESSION_ID,
      },
    });
    return textResult(responseText);
  },
);

// ---------------------------------------------------------------------------
// 2. minni_drill
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_drill",
  {
    title: "Minni Drill",
    description:
      "Drill headline recall results to snippet, chunk, or document depth by result/chunk id, wikilink, or handle (m:<8hex>, d:<src>:<id>).",
    inputSchema: {
      resultIds: z.array(z.number().int()).optional(),
      chunkIds: z.array(z.number().int()).optional(),
      references: z.array(z.union([z.string(), z.object({}).passthrough()])).optional(),
      handle: z.string().optional(),
      wikilink: z.string().optional(),
      depth: z.enum(["snippet", "chunk", "document"]).optional(),
    },
  },
  async ({ resultIds, chunkIds, references, handle, wikilink, depth }) => {
    const mcpSession = resolveMcpSessionIdentity(DEFAULT_VAULT_PATH);
    await disarmRecallStateOnConsult(DEFAULT_VAULT_PATH, mcpSession).catch(() => false);
    const result = await drillMemory({
      resultIds,
      chunkIds,
      references,
      handle,
      wikilink,
      depth,
      vaultPath: DEFAULT_VAULT_PATH,
      sessionId: mcpSession.sessionId ?? mcpSession.sessionKey ?? MCP_PROCESS_SESSION_ID,
      agentId: DEFAULT_AGENT_ID,
    });
    return textResult(JSON.stringify(result, null, 2));
  },
);

// ---------------------------------------------------------------------------
// 3. minni_learn
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_learn",
  {
    title: "Minni Learn",
    description:
      "Write a vault note first, then store the learning through Minni.",
    inputSchema: {
      title: z.string().min(1).optional(),
      content: z.string().min(1),
      category: z.string().optional(),
      source: z.string().optional(),
      workspaceId: z.string().optional(),
      // G12: vaultPath removed from model-facing schema. Writes now target the operator-controlled DEFAULT_VAULT_PATH only.
      // Issue #125: quality floor is default-on; pass requireQuality:false to deliberately store a weak note.
      requireQuality: z.boolean().optional().default(true),
    },
  },
  async ({ title: rawTitle, content, category, source, workspaceId, requireQuality }) => {
    const title =
      rawTitle?.trim() ||
      content
        .trim()
        .split(/\r?\n/)[0]
        ?.replace(/^#+\s*/, "")
        .slice(0, 72)
        .trim() ||
      "Untitled learning";
    const quality = await assessLearningQualityAsync({ title, content, category, source });
    if (requireQuality === false && flagsSensitiveMaterial(quality)) {
      await recordAudit(DEFAULT_VAULT_PATH, {
        tool: "minni_learn",
        summary: `quality-blocked (credential material, not opt-outable): ${auditSafeTitle(title, quality)}`,
        details: { quality },
      });
      return textResult(JSON.stringify({ status: "quality-blocked", quality }, null, 2));
    }
    if (requireQuality !== false && !quality.ok) {
      await recordAudit(DEFAULT_VAULT_PATH, {
        tool: "minni_learn",
        summary: `quality-blocked: ${auditSafeTitle(title, quality)}`,
        details: { quality },
      });
      return textResult(
        JSON.stringify(
          {
            status: "quality-blocked",
            quality,
          },
          null,
          2,
        ),
      );
    }
    const store = await learnMemory({
      content,
      category,
      agentId: DEFAULT_AGENT_ID,
      workspaceId: resolveSessionWorkspaceId(DEFAULT_VAULT_PATH, workspaceId),
      ...(source ? { source } : {}),
    });
    const note = await vaultFirstLearn({
      vaultPath: DEFAULT_VAULT_PATH,
      title,
      content,
      category,
      source,
      agentId: DEFAULT_AGENT_ID,
      storeResult: { ok: store.ok, data: store.data, error: store.error },
      quality,
    });
    // Pillar 3: do NOT call vault_index_doc on wiki/sessions/*.md candidate notes;
    // accepted learnings are indexed synchronously by the daemon (_index_durable_learning).
    const storeRecovery = recoveryRouteFrom(store.data);
    const storeData =
      store.ok && store.data !== null && typeof store.data === "object"
        ? (store.data as Record<string, unknown>)
        : undefined;
    const isPromoted = storeData?.status === "accepted";
    const promotedLearningId =
      typeof storeData?.learning_id === "number" ? storeData.learning_id : undefined;
    return textResult(
      JSON.stringify(
        {
          status: store.ok
            ? "learned"
            : storeRecovery
              ? "identity-recovery-required"
              : "vault-written-memory-store-failed",
          ...(isPromoted
            ? {
                promotion_status: "accepted",
                ...(promotedLearningId !== undefined
                  ? { learning_id: promotedLearningId }
                  : {}),
              }
            : {}),
          quality,
          note,
          store,
        },
        null,
        2,
      ),
    );
  },
);

// ---------------------------------------------------------------------------
// 4. minni_list_candidates
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_list_candidates",
  {
    title: "Minni List Candidates",
    description:
      "List staged learning candidates for this runtime principal (own rows only). Defaults to status=proposed (the drain queue). Only proposed packet content is returned.",
    inputSchema: {
      status: z.enum(["proposed"]).optional(),
      limit: z.number().int().min(1).max(500).optional(),
      // G11: no caller-controlled identity. Server stamps DEFAULT_AGENT_ID; daemon list_candidates filters WHERE principal=stamped.
    },
  },
  async ({ status, limit }) => {
    const gated = await requireSharedGate("candidates.list", { status, limit });
    if (gated) return gated;
    const drainStatus = drainStatusForModel(status);
    const { jsonRpcSocketRequestWithFallback } = await import("./daemon_transport.js");
    const rpc = await jsonRpcSocketRequestWithFallback("list_candidates", {
      status: drainStatus,
      ...(limit != null ? { limit } : {}),
      agent_id: DEFAULT_AGENT_ID,
    });
    return textResult(JSON.stringify(modelListCandidatesPayload(rpc, drainStatus), null, 2));
  },
);

// ---------------------------------------------------------------------------
// 5. minni_resolve_candidate
// ---------------------------------------------------------------------------
// G15 / RCM-009: (1) this TS handler surface (no agentId in schema),
// (2) minnid._resolve_candidate (resolve_effective_principal + is_operator_principal),
// (3) principal resolver + is_operator_principal itself.
server.registerTool(
  "minni_resolve_candidate",
  {
    title: "Minni Resolve Candidate",
    description:
      "Resolve a staged candidate (accept→durable learn, reject, redact, merge, etc.). Owner may resolve own rows; accept into durable memory still requires operator/govern. Cross-principal resolve requires an explicit resolve_candidate/govern grant.",
    inputSchema: {
      candidate_id: z.number().int(),
      decision: z.enum([
        "accept",
        "learn",
        "reject",
        "redact",
        "do_not_store",
        "log_only",
        "merge",
        "supersede",
      ]),
      reason: z.string().optional(),
    },
  },
  async ({ candidate_id, decision, reason }) => {
    const gated = await requireSharedGate("candidates.resolve", { candidate_id, decision });
    if (gated) return gated;
    const { jsonRpcSocketRequestWithFallback } = await import("./daemon_transport.js");
    const rpc = await jsonRpcSocketRequestWithFallback("resolve_candidate", {
      candidate_id,
      decision,
      reason: reason || "",
      agent_id: DEFAULT_AGENT_ID,
    });
    return textResult(JSON.stringify(redactLocalValue(rpc), null, 2));
  },
);

// ---------------------------------------------------------------------------
// 6. minni_status
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_status",
  {
    title: "Minni Status",
    description:
      "Check Minni daemon, AFM health, vault, and audit state.",
    inputSchema: {},
  },
  async () => {
    const gated = await requireSharedGate("audit.status", { tool: "minni_status" });
    if (gated) return gated;
    const report = await statusAndAudit(DEFAULT_VAULT_PATH);
    return textResult(JSON.stringify({ ...report, use: useStatusBlock(DEFAULT_VAULT_PATH) }, null, 2));
  },
);

// ---------------------------------------------------------------------------
// 7. minni_handoff_send
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_handoff_send",
  {
    title: "Minni Handoff Send",
    description:
      "Send a 1-step work-transfer handoff packet to the next session of this agent (default) or any named agent (codex, claude-code, gemini, grok-build, etc.), returning a compact <1 KB lease receipt.",
    inputSchema: {
      to_agent: z.string().min(1).optional(),
      task: z.string().min(1),
      summary: z.string().optional(),
      files: z.array(z.string()).optional(),
      next_steps: z.array(z.string()).optional(),
      open_questions: z.array(z.string()).optional(),
      workspace_id: z.string().optional(),
    },
  },
  async ({
    to_agent,
    task,
    summary,
    files,
    next_steps,
    open_questions,
    workspace_id,
  }) => {
    const fromAgent = DEFAULT_AGENT_ID;
    const targetAgent = to_agent?.trim() || DEFAULT_AGENT_ID;
    const gated = await requireSharedGate("handoff.send", {
      to_agent: targetAgent,
    });
    if (gated) return gated;

    const effectiveVaultPath = DEFAULT_VAULT_PATH;
    const effectiveWorkspaceId = resolveSessionWorkspaceId(effectiveVaultPath, workspace_id);
    const tail = await auditTail(effectiveVaultPath, 60);
    const scarTissue = extractScarTissue(tail.entries);
    const packet = await buildHandoffPacket({
      task,
      agentId: fromAgent,
      workspaceId: effectiveWorkspaceId,
      vaultPath: effectiveVaultPath,
      openQuestions: open_questions,
      scarTissue,
    });
    const envelope = wrapEnvelope({
      event: "Handoff",
      agent: packet.agentOrigin,
      body: {
        identity: packet.identity,
        task: packet.task,
        ...(summary ? { summary } : {}),
        ...(files?.length ? { files } : {}),
        ...(next_steps?.length ? { next_steps } : {}),
        recall: packet.topRecalls.map((source) => ({
          wikilink: source.wikilink,
          score: source.score,
          authority: source.authority,
          freshness: source.freshness,
          snippet: source.snippet,
        })),
        scar_tissue: packet.scarTissue,
        open_questions: packet.openQuestions,
        daemon: { ok: packet.daemonOk, lead: packet.daemonLead },
      },
    });
    const fallbackLeaseId = `handoff-${randomUUID().replace(/-/g, "").slice(0, 16)}`;
    const fallbackExpiresAt = new Date(Date.now() + 24 * 3600 * 1000).toISOString();
    const handoffPacket: Record<string, unknown> = {
      from_agent: packet.agentOrigin,
      to_agent: targetAgent,
      workspace_id: packet.workspace,
      kind: "handoff",
      task: packet.task,
      ...(summary ? { summary } : {}),
      ...(files?.length ? { files } : {}),
      ...(next_steps?.length ? { next_steps } : {}),
      ...(open_questions?.length ? { open_questions } : {}),
      ...(packet.scarTissue.length ? { scar_tissue: packet.scarTissue } : {}),
      envelope,
      wikilink_refs: packet.topRecalls.map((source) =>
        source.relativePath.replace(/\.md$/, ""),
      ),
      trace_id: `plugin-${Date.now().toString(36)}`,
      lease_id: fallbackLeaseId,
      requires_ack: true,
      expires_at: fallbackExpiresAt,
      created_at: new Date().toISOString(),
    };
    const delivery = await handoffMemory({
      fromAgent: packet.agentOrigin,
      toAgent: targetAgent,
      packet: handoffPacket,
    });
    const deliveryData =
      delivery.ok && delivery.data && typeof delivery.data === "object"
        ? (delivery.data as Record<string, unknown>)
        : undefined;
    const delivered = delivery.ok && deliveryData?.delivered !== false;
    const status = !delivery.ok
      ? "error"
      : typeof deliveryData?.status === "string"
        ? deliveryData.status
        : delivered
          ? "ok"
          : "degraded";
    const leaseId = delivered
      ? typeof deliveryData?.lease_id === "string"
        ? deliveryData.lease_id
        : fallbackLeaseId
      : null;
    const expiresAt = delivered
      ? typeof deliveryData?.expires_at === "string"
        ? deliveryData.expires_at
        : fallbackExpiresAt
      : null;

    await recordAudit(effectiveVaultPath, {
      tool: "minni_handoff_send",
      summary: task.slice(0, 120),
      details: {
        agent: packet.agentOrigin,
        to_agent: targetAgent,
        workspace: packet.workspace,
        lease_id: leaseId,
        recalls: packet.topRecalls.length,
        scar_tissue: packet.scarTissue.length,
        delivered,
        delivery_error: delivery.ok ? undefined : delivery.error,
      },
    });

    const receipt: Record<string, unknown> = {
      status,
      lease_id: leaseId,
      from_agent: packet.agentOrigin,
      to_agent: targetAgent,
      expires_at: expiresAt,
      ...(!delivery.ok && delivery.error
        ? { error: delivery.error.slice(0, 240) }
        : typeof deliveryData?.reason === "string" && deliveryData.reason
          ? { reason: deliveryData.reason.slice(0, 240) }
          : {}),
    };
    return textResult(JSON.stringify(receipt, null, 2));
  },
);

// ---------------------------------------------------------------------------
// 8. minni_handoff_inbox
// ---------------------------------------------------------------------------
async function readHandoffPacketPreview(packetPath: unknown): Promise<{
  workspace_id?: string;
  summary?: string;
  files?: string[];
  next_steps?: string[];
  open_questions?: string[];
  scar_tissue?: unknown[];
  wikilink_refs?: string[];
}> {
  if (typeof packetPath !== "string" || !packetPath.trim()) return {};
  try {
    const raw = await readFile(packetPath, "utf8");
    if (raw.length > 256 * 1024) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object") return {};
    let envelopeBodyObj: Record<string, unknown> | undefined;
    if (typeof parsed.envelope === "string" && parsed.envelope.includes("<minni:context")) {
      const firstNl = parsed.envelope.indexOf("\n");
      const closeTag = parsed.envelope.lastIndexOf("\n</minni:context>");
      if (firstNl > 0 && closeTag > firstNl) {
        try {
          const inner = JSON.parse(parsed.envelope.slice(firstNl + 1, closeTag)) as unknown;
          if (inner && typeof inner === "object" && !Array.isArray(inner)) {
            envelopeBodyObj = inner as Record<string, unknown>;
          }
        } catch {
          // Ignore malformed envelope body fallback
        }
      }
    }
    const rawWs =
      typeof parsed.workspace_id === "string" && parsed.workspace_id.trim()
        ? parsed.workspace_id.trim()
        : envelopeBodyObj?.identity &&
            typeof envelopeBodyObj.identity === "object" &&
            typeof (envelopeBodyObj.identity as Record<string, unknown>).workspace === "string"
          ? String((envelopeBodyObj.identity as Record<string, unknown>).workspace).trim()
          : "";
    const normalizedWs = rawWs ? normalizeWorkspaceId(rawWs) || rawWs : "";
    const rawScars = Array.isArray(parsed.scar_tissue)
      ? parsed.scar_tissue
      : Array.isArray(envelopeBodyObj?.scar_tissue)
        ? (envelopeBodyObj.scar_tissue as unknown[])
        : undefined;
    return {
      ...(normalizedWs ? { workspace_id: normalizedWs } : {}),
      ...(typeof parsed.summary === "string" && parsed.summary
        ? { summary: parsed.summary.slice(0, 1500) }
        : {}),
      ...(Array.isArray(parsed.files)
        ? { files: parsed.files.filter((x): x is string => typeof x === "string").slice(0, 20) }
        : {}),
      ...(Array.isArray(parsed.next_steps)
        ? { next_steps: parsed.next_steps.filter((x): x is string => typeof x === "string").slice(0, 10) }
        : {}),
      ...(Array.isArray(parsed.open_questions)
        ? { open_questions: parsed.open_questions.filter((x): x is string => typeof x === "string").slice(0, 10) }
        : {}),
      ...(rawScars ? { scar_tissue: rawScars.slice(0, 10) } : {}),
      ...(Array.isArray(parsed.wikilink_refs)
        ? { wikilink_refs: parsed.wikilink_refs.filter((x): x is string => typeof x === "string").slice(0, 20) }
        : {}),
    };
  } catch {
    return {};
  }
}

server.registerTool(
  "minni_handoff_inbox",
  {
    title: "Minni Handoff Inbox",
    description:
      "List pending handoffs addressed to this agent, acknowledge a specific lease_id, or pass ack:true to read and clear all pending handoffs in a single call.",
    inputSchema: {
      ack: z.boolean().optional(),
      lease_id: z.string().min(1).optional(),
      status: z
        .enum([
          "accepted",
          "rejected_stale",
          "rejected_contradicts",
          "rejected_scope",
        ])
        .optional(),
      note: z.string().optional(),
      workspace_id: z.string().optional(),
    },
  },
  async ({ ack, lease_id, status, note, workspace_id }) => {
    const gated = await requireSharedGate("handoff.inbox", {
      ack: ack ?? Boolean(lease_id),
      ...(lease_id ? { lease_id } : {}),
      ...(status ? { status } : {}),
    });
    if (gated) return gated;

    const ackStatus = status ?? "accepted";
    const targetWs =
      typeof workspace_id === "string" && workspace_id.trim()
        ? normalizeWorkspaceId(workspace_id)
        : "";
    const matchesTargetWorkspace = (
      item: Record<string, unknown>,
      preview: { workspace_id?: string },
    ): boolean => {
      if (!targetWs) return true;
      const rawItemWs =
        typeof item.workspace_id === "string" && item.workspace_id.trim()
          ? item.workspace_id
          : (preview.workspace_id ?? "");
      return normalizeWorkspaceId(rawItemWs) === targetWs;
    };

    if (lease_id && ack !== false) {
      let handoffBody: Record<string, unknown> | undefined;
      try {
        const pendingBeforeAck = await listPendingHandoffs({ agentId: DEFAULT_AGENT_ID });
        if (pendingBeforeAck.ok && pendingBeforeAck.data && typeof pendingBeforeAck.data === "object") {
          const pendingList = Array.isArray((pendingBeforeAck.data as Record<string, unknown>).handoffs)
            ? ((pendingBeforeAck.data as Record<string, unknown>).handoffs as Array<Record<string, unknown>>)
            : [];
          const matchedItem = pendingList.find((it) => it.lease_id === lease_id);
          if (matchedItem) {
            const preview = await readHandoffPacketPreview(matchedItem.path);
            if (!matchesTargetWorkspace(matchedItem, preview)) {
              return textResult(
                JSON.stringify(
                  redactLocalValue({
                    status: "error",
                    lease_id,
                    error: `workspace mismatch: lease does not belong to ${targetWs}`,
                  }),
                  null,
                  2,
                ),
              );
            }
            handoffBody = {
              lease_id: matchedItem.lease_id ?? lease_id,
              from_agent: matchedItem.from_agent ?? null,
              to_agent: matchedItem.to_agent ?? DEFAULT_AGENT_ID,
              task: matchedItem.task ?? "",
              expires_at: matchedItem.expires_at ?? null,
              ...(typeof matchedItem.workspace_id === "string" && matchedItem.workspace_id.trim()
                ? {
                    workspace_id:
                      normalizeWorkspaceId(matchedItem.workspace_id) || matchedItem.workspace_id,
                  }
                : {}),
              ...preview,
            };
          }
        }
      } catch {
        // Best-effort pre-ack preview hydration
      }

      const ackRes = await ackHandoff({
        leaseId: lease_id,
        status: ackStatus,
        agentId: DEFAULT_AGENT_ID,
      });
      if (note?.trim()) {
        await recordAudit(DEFAULT_VAULT_PATH, {
          tool: "minni_handoff_inbox",
          summary: `ack ${lease_id} (${ackStatus})`,
          details: { lease_id, status: ackStatus, note: note.trim().slice(0, 500) },
        });
      }
      if (!ackRes.ok) {
        return textResult(
          JSON.stringify(
            redactLocalValue({
              status: "error",
              lease_id,
              error: ackRes.error ?? "ack failed",
            }),
            null,
            2,
          ),
        );
      }
      return textResult(
        JSON.stringify(
          {
            status: ackStatus,
            lease_id,
            agent_id: DEFAULT_AGENT_ID,
            acked: true,
            ...(handoffBody ? { handoff: handoffBody } : {}),
            ...(note?.trim() ? { note: note.trim().slice(0, 500) } : {}),
          },
          null,
          2,
        ),
      );
    }

    const pending = await listPendingHandoffs({ agentId: DEFAULT_AGENT_ID });
    if (!pending.ok) {
      return textResult(
        JSON.stringify(
          redactLocalValue({
            status: "error",
            agent_id: DEFAULT_AGENT_ID,
            handoffs: [],
            error: pending.error ?? "failed to list pending handoffs",
          }),
          null,
          2,
        ),
      );
    }
    const pendingData =
      pending.data && typeof pending.data === "object"
        ? (pending.data as Record<string, unknown>)
        : {};
    const rawItems = Array.isArray(pendingData.handoffs)
      ? (pendingData.handoffs as Array<Record<string, unknown>>)
      : [];
    const filteredItems = lease_id
      ? rawItems.filter((item) => item.lease_id === lease_id)
      : rawItems;

    const items: Array<Record<string, unknown>> = [];
    let ackedCount = 0;
    for (const item of filteredItems) {
      const itemLeaseId = typeof item.lease_id === "string" ? item.lease_id : "";
      const preview = await readHandoffPacketPreview(item.path);
      if (!matchesTargetWorkspace(item, preview)) {
        continue;
      }
      let itemAckStatus: string | undefined;
      if (ack === true && itemLeaseId) {
        const ackRes = await ackHandoff({
          leaseId: itemLeaseId,
          status: ackStatus,
          agentId: DEFAULT_AGENT_ID,
        });
        if (ackRes.ok) {
          ackedCount += 1;
          itemAckStatus = ackStatus;
        } else {
          itemAckStatus = "ack_failed";
        }
      }
      items.push({
        lease_id: item.lease_id ?? null,
        from_agent: item.from_agent ?? null,
        to_agent: item.to_agent ?? DEFAULT_AGENT_ID,
        task: item.task ?? "",
        expires_at: item.expires_at ?? null,
        ...(typeof item.workspace_id === "string" && item.workspace_id.trim()
          ? { workspace_id: normalizeWorkspaceId(item.workspace_id) || item.workspace_id }
          : {}),
        ...preview,
        ...(itemAckStatus ? { ack_status: itemAckStatus } : {}),
      });
    }
    if (ack === true && note?.trim() && ackedCount > 0) {
      await recordAudit(DEFAULT_VAULT_PATH, {
        tool: "minni_handoff_inbox",
        summary: `inline-acked ${ackedCount} handoff(s) (${ackStatus})`,
        details: { acked_count: ackedCount, status: ackStatus, note: note.trim().slice(0, 500) },
      });
    }
    return textResult(
      JSON.stringify(
        {
          status: "ok",
          agent_id: DEFAULT_AGENT_ID,
          count: items.length,
          ...(ack === true ? { acked_count: ackedCount } : {}),
          handoffs: items,
        },
        null,
        2,
      ),
    );
  },
);

// ---------------------------------------------------------------------------
// Shared Thread Schemas & Target Resolver
// ---------------------------------------------------------------------------
const planSliceInputSchema = z.object({
  id: z.string().optional(),
  title: z.string().min(1),
  gate: z.string().optional(),
  depends_on: z.array(z.string()).optional(),
  evidence: z.string().optional(),
});

const planShelfRefInputSchema = z.object({
  agent: z.string().optional(),
  wikilink: z.string().optional(),
  pull_hint: z.string().optional(),
  approx_tokens: z.number().optional(),
  shelf_hash: z.string().optional(),
  shelf_content: z.string().optional(),
});

async function resolvePlanTarget(
  planIdInput: string | undefined,
): Promise<
  | { ok: true; plan_id: string; notePath: string }
  | { ok: false; result: ReturnType<typeof textResult> }
> {
  try {
    const mcpSession = resolveMcpSessionIdentity(DEFAULT_VAULT_PATH);
    const resolved = await resolvePlanIdOrActive(DEFAULT_VAULT_PATH, planIdInput, {
      sessionKey: mcpSession.sessionKey,
      sessionId: mcpSession.sessionId,
      hostPid: mcpSession.hostPid,
    });
    if ("error" in resolved) {
      return { ok: false, result: textResult(JSON.stringify({ error: resolved.error }, null, 2)) };
    }
    const plan_id = resolved.plan_id;
    const notePath = await findPlanNote(DEFAULT_VAULT_PATH, plan_id);
    if (!notePath) {
      return {
        ok: false,
        result: textResult(JSON.stringify({ error: `plan not found: ${plan_id}` }, null, 2)),
      };
    }
    return { ok: true, plan_id, notePath };
  } catch (error) {
    return { ok: false, result: textResult(JSON.stringify({ error: threadWorkerErrorText(error) }, null, 2)) };
  }
}

async function ensureRelayCursorsForActiveSession(
  vaultPath: string,
  planId: string,
  sessionKey: string | undefined,
): Promise<void> {
  if (!sessionKey) return;
  const vp = vaultPrincipalSubscriberId(vaultPath);
  await ensureSessionRelayCursor(
    vaultPath,
    planId,
    formatSessionSubscriberId(DEFAULT_AGENT_ID, sessionKey),
  ).catch(() => {});
  if (vp && vp !== DEFAULT_AGENT_ID) {
    await ensureSessionRelayCursor(
      vaultPath,
      planId,
      formatSessionSubscriberId(vp, sessionKey),
    ).catch(() => {});
  }
}

function clipStringUtf8(str: string, maxChars: number): string {
  if (str.length <= maxChars) return str;
  if (maxChars <= 3) return str.slice(0, Math.max(0, maxChars));
  return `${str.slice(0, maxChars - 3)}...`;
}

function deepClipStrings(
  value: unknown,
  maxChars: number,
  preserveKeys = new Set<string>(["plan_id", "status", "id"]),
): unknown {
  if (typeof value === "string") {
    return clipStringUtf8(value, maxChars);
  }
  if (Array.isArray(value)) {
    return value.map((item) => deepClipStrings(item, maxChars, preserveKeys));
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (preserveKeys.has(k) && typeof v === "string") {
        out[k] = clipStringUtf8(v, Math.max(maxChars, 96));
      } else {
        out[k] = deepClipStrings(v, maxChars, preserveKeys);
      }
    }
    return out;
  }
  return value;
}

function serializeCompactBudget(
  payload: Record<string, unknown>,
  maxBytes = 1950,
): string {
  const hardCapBytes = Math.min(2000, Math.max(maxBytes, 256));
  const targetBytes = Math.min(maxBytes, hardCapBytes);
  const fits = (obj: unknown): string | null => {
    const s = JSON.stringify(obj);
    return Buffer.byteLength(s, "utf8") <= targetBytes ? s : null;
  };

  const pretty = JSON.stringify(payload, null, 2);
  if (Buffer.byteLength(pretty, "utf8") <= targetBytes) return pretty;
  const dense = JSON.stringify(payload);
  if (Buffer.byteLength(dense, "utf8") <= targetBytes) return dense;

  let working: Record<string, unknown> = { ...payload };

  // 1. Progressive slice array shrink down to 0 with truncated_count
  if (Array.isArray(working.slices)) {
    const allSlices = working.slices as unknown[];
    for (let keep = Math.min(allSlices.length, 16); keep >= 0; keep--) {
      const nextWork: Record<string, unknown> = {
        ...working,
        slice_count: allSlices.length,
        slices_omitted: allSlices.length - keep,
        ...(keep < allSlices.length ? { truncated_count: allSlices.length - keep } : {}),
        slices: allSlices.slice(0, keep),
      };
      const ok = fits(nextWork);
      if (ok !== null) return ok;
      if (keep === 0) working = nextWork;
    }
  }

  // 2. Progressive view.{open_questions, scars, pending} shrink down to 0
  if (working.view && typeof working.view === "object" && !Array.isArray(working.view)) {
    let viewWork: Record<string, unknown> = { ...(working.view as Record<string, unknown>) };

    if (Array.isArray(viewWork.open_questions)) {
      const allQs = viewWork.open_questions as unknown[];
      for (let keep = Math.min(allQs.length, 6); keep >= 0; keep--) {
        const nextView: Record<string, unknown> = {
          ...viewWork,
          open_questions: allQs.slice(0, keep),
          ...(keep < allQs.length ? { open_questions_truncated_count: allQs.length - keep } : {}),
        };
        const candidate = { ...working, view: nextView };
        const ok = fits(candidate);
        if (ok !== null) return ok;
        if (keep === 0) {
          viewWork = nextView;
          working = candidate;
        }
      }
    }

    if (Array.isArray(viewWork.scars)) {
      const allScars = viewWork.scars as unknown[];
      for (let keep = Math.min(allScars.length, 5); keep >= 0; keep--) {
        const nextView: Record<string, unknown> = {
          ...viewWork,
          scars: allScars.slice(0, keep),
          ...(keep < allScars.length ? { scars_truncated_count: allScars.length - keep } : {}),
        };
        const candidate = { ...working, view: nextView };
        const ok = fits(candidate);
        if (ok !== null) return ok;
        if (keep === 0) {
          viewWork = nextView;
          working = candidate;
        }
      }
    }

    if (Array.isArray(viewWork.pending)) {
      const allPending = viewWork.pending as unknown[];
      for (let keep = Math.min(allPending.length, 8); keep >= 0; keep--) {
        const nextView: Record<string, unknown> = {
          ...viewWork,
          pending: allPending.slice(0, keep),
          ...(keep < allPending.length ? { truncated_count: allPending.length - keep } : {}),
        };
        const candidate = { ...working, view: nextView };
        const ok = fits(candidate);
        if (ok !== null) return ok;
        if (keep === 0) {
          viewWork = nextView;
          working = candidate;
        }
      }
    }
  }

  // 3. Top-level scars array shrink down to 0 (minni_thread_update)
  if (Array.isArray(working.scars)) {
    const allScars = working.scars as unknown[];
    for (let keep = Math.min(allScars.length, 5); keep >= 0; keep--) {
      const candidate: Record<string, unknown> = {
        ...working,
        scars: allScars.slice(0, keep),
        ...(keep < allScars.length ? { scars_truncated_count: allScars.length - keep } : {}),
      };
      const ok = fits(candidate);
      if (ok !== null) return ok;
      if (keep === 0) working = candidate;
    }
  }

  // 4. Trim updated_slice.{gate, title, evidence} and next_action / headline
  if (
    working.updated_slice &&
    typeof working.updated_slice === "object" &&
    !Array.isArray(working.updated_slice)
  ) {
    const us = working.updated_slice as Record<string, unknown>;
    const trimmedUs: Record<string, unknown> = { ...us };
    for (const k of ["gate", "title", "evidence"] as const) {
      if (typeof trimmedUs[k] === "string") {
        trimmedUs[k] = clipStringUtf8(trimmedUs[k] as string, 120);
      }
    }
    working = { ...working, updated_slice: trimmedUs };
    const ok = fits(working);
    if (ok !== null) return ok;
  }

  for (const field of ["next_action", "headline", "goal", "title", "warning", "note"] as const) {
    if (typeof working[field] === "string") {
      working = { ...working, [field]: clipStringUtf8(working[field] as string, 140) };
      const ok = fits(working);
      if (ok !== null) return ok;
    }
  }

  // 5. Progressive deep string clipping until strictly <= hardCapBytes (<= 2000 UTF-8 bytes)
  for (const cap of [200, 120, 64, 32, 16]) {
    const clipped = deepClipStrings(working, cap) as Record<string, unknown>;
    const s = JSON.stringify(clipped);
    if (Buffer.byteLength(s, "utf8") <= hardCapBytes) return s;
    working = clipped;
  }

  // Final deterministic emergency floor guaranteed <= 2000 bytes
  const minimal: Record<string, unknown> = {
    ...(working.plan_id !== undefined
      ? { plan_id: typeof working.plan_id === "string" ? clipStringUtf8(working.plan_id, 120) : working.plan_id }
      : {}),
    ...(working.rev !== undefined ? { rev: working.rev } : {}),
    ...(working.status !== undefined ? { status: working.status } : {}),
    ...(working.active !== undefined ? { active: working.active } : {}),
    truncated: true,
  };
  return JSON.stringify(minimal);
}

// ---------------------------------------------------------------------------
// 9. minni_thread_create
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_thread_create",
  {
    title: "Minni Thread Create",
    description:
      "Create a proposal-first Minni thread artifact in the vault with per-session active pointer support, returning a compact <2 KB receipt.",
    inputSchema: {
      goal: z.string().min(1),
      constraints: z.array(z.string()).optional(),
      slices: z.array(planSliceInputSchema).optional(),
      open_questions: z.array(z.string()).optional(),
      seed_scar_from_audit: z.boolean().optional(),
      shelf_ref: planShelfRefInputSchema.optional(),
      activate: z.boolean().optional(),
    },
  },
  async ({ goal, constraints, slices, open_questions, seed_scar_from_audit, shelf_ref, activate }) => {
    const gated = await requireSharedGate("plan.create", { slices: slices?.length ?? 0 });
    if (gated) return gated;
    const effectiveVaultPath = DEFAULT_VAULT_PATH;
    const mcpSession = resolveMcpSessionIdentity(effectiveVaultPath);
    let scar_tissue: ScarTissueEntry[] | undefined;
    if (seed_scar_from_audit) {
      const tail = await auditTail(effectiveVaultPath, 60);
      scar_tissue = extractScarTissue(tail.entries);
    }
    const { plan, displaced_active } = await createPlan(
      {
        goal,
        constraints,
        slices,
        open_questions,
        scar_tissue,
        shelf_ref,
        vaultPath: effectiveVaultPath,
        activate,
        sessionKey: mcpSession.sessionKey,
        sessionId: mcpSession.sessionId,
        hostPid: mcpSession.hostPid,
      },
      { vaultPath: effectiveVaultPath },
    );
    if (activate !== false && mcpSession.sessionKey) {
      await ensureRelayCursorsForActiveSession(effectiveVaultPath, plan.plan_id, mcpSession.sessionKey);
    }
    const compactSlices = plan.slices.map((s) => ({
      id: s.id,
      title: s.title.length <= 80 ? s.title : `${s.title.slice(0, 77)}...`,
      status: s.status,
      ...(s.depends_on?.length ? { depends_on: s.depends_on } : {}),
    }));
    return textResult(
      serializeCompactBudget({
        plan_id: plan.plan_id,
        rev: plan.rev,
        title: plan.goal.length <= 200 ? plan.goal : `${plan.goal.slice(0, 197)}...`,
        status: plan.status,
        active: activate !== false,
        slices: compactSlices,
        ...(displaced_active
          ? {
              displaced_active,
              warning: `active plan ${displaced_active} was in-flight and has been displaced by this new plan; re-activate it with minni_thread_update ({ plan_id: "${displaced_active}", activate: true }) if unintended.`,
            }
          : {}),
      }),
    );
  },
);

// ---------------------------------------------------------------------------
// 10. minni_thread_status
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_thread_status",
  {
    title: "Minni Thread Status",
    description:
      "Compact <2 KB thread view for agent context; optional live shelf content surfaces drift only (never auto-pull). plan_id defaults to the active plan.",
    inputSchema: {
      plan_id: z.string().min(1).optional(),
      live_shelf_content: z.string().optional(),
    },
  },
  async ({ plan_id: planIdInput, live_shelf_content }) => {
    const gated = await requireSharedGate("plan.status", { plan_id: planIdInput });
    if (gated) return gated;
    const effectiveVaultPath = DEFAULT_VAULT_PATH;
    try {
      const target = await resolvePlanTarget(planIdInput);
      if (!target.ok) return target.result;
      const { plan_id, notePath } = target;
      const { plan } = await synchronizeExpiredClaims({
        vaultPath: effectiveVaultPath,
        notePath,
        planId: plan_id,
        actor: DEFAULT_AGENT_ID,
      });
      const mcpSession = resolveMcpSessionIdentity(effectiveVaultPath);
      const activePointer = await getActivePlan(effectiveVaultPath, {
        sessionKey: mcpSession.sessionKey,
      });
      const active = activePointer?.plan_id === plan_id;
      const view = compactPlanView(plan);
      const drift = live_shelf_content
        ? shelfDrift(plan, live_shelf_content)
        : undefined;
      return textResult(
        serializeCompactBudget({ plan_id, view, drift, status: plan.status, rev: plan.rev, active }),
      );
    } catch (error) {
      return threadWorkerErrorResult("plan.status", error);
    }
  },
);

// ---------------------------------------------------------------------------
// 11. minni_thread_update
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_thread_update",
  {
    title: "Minni Thread Update",
    description:
      "Update one plan slice status (evidence required for done; depends_on enforced unless force + force_reason are supplied) and/or set per-session plan activation (activate: true | false). Defaults compact to true (<2 KB); plan_id defaults to the active plan.",
    inputSchema: {
      plan_id: z.string().min(1).optional(),
      slice_id: z.string().min(1).optional(),
      status: z.enum(["pending", "in_progress", "done", "blocked", "superseded"]).optional(),
      evidence: z.string().optional(),
      force: z.boolean().optional(),
      force_reason: z.string().optional(),
      activate: z.boolean().optional(),
      compact: z.boolean().optional(),
      include_full_plan: z.boolean().optional(),
    },
  },
  async ({
    plan_id: planIdInput,
    slice_id,
    status,
    evidence,
    force,
    force_reason,
    activate,
    compact,
    include_full_plan,
  }) => {
    const gated = await requireSharedGate("plan.update", {
      plan_id: planIdInput,
      slice_id,
      status,
      force,
      force_reason,
      ...(activate !== undefined ? { activate } : {}),
    });
    if (gated) return gated;

    try {
      const effectiveVaultPath = DEFAULT_VAULT_PATH;
      const mcpSession = resolveMcpSessionIdentity(effectiveVaultPath);

      if ((slice_id === undefined) !== (status === undefined)) {
        return textResult(
          JSON.stringify({ error: "slice_id and status must be provided together" }, null, 2),
        );
      }
      if (slice_id === undefined && status === undefined && activate === undefined) {
        return textResult(
          JSON.stringify({ error: "provide slice_id + status and/or activate" }, null, 2),
        );
      }

      // Activation / deactivation-only call (no slice mutation)
      if (slice_id === undefined || status === undefined) {
        if (activate === false) {
          const preTarget = await resolvePlanTarget(planIdInput);
          await clearActivePlan(effectiveVaultPath, {
            sessionKey: mcpSession.sessionKey,
            sessionOnly: true,
          });
          if (preTarget.ok) {
            const plan = await rehydratePlan(preTarget.notePath);
            const view = compactPlanView(plan);
            return textResult(
              serializeCompactBudget({
                plan_id: plan.plan_id,
                rev: plan.rev,
                status: plan.status,
                active: false,
                headline: view.headline,
                progress: view.progress,
                next_action: plan.next_action,
                updated_slice: null,
                scars: view.scars,
              }),
            );
          }
          return textResult(
            serializeCompactBudget({ plan_id: planIdInput ?? null, active: false }),
          );
        }

        const target = await resolvePlanTarget(planIdInput);
        if (!target.ok) return target.result;
        const activated = await activatePlanChecked(
          effectiveVaultPath,
          target.plan_id,
          target.notePath,
          {
            sessionKey: mcpSession.sessionKey,
            sessionId: mcpSession.sessionId,
            hostPid: mcpSession.hostPid,
          },
        );
        if (!activated.ok) {
          return textResult(JSON.stringify({ error: activated.error }, null, 2));
        }
        await ensureRelayCursorsForActiveSession(
          effectiveVaultPath,
          target.plan_id,
          mcpSession.sessionKey,
        );
        const plan = await rehydratePlan(target.notePath);
        const view = compactPlanView(plan);
        return textResult(
          serializeCompactBudget({
            plan_id: plan.plan_id,
            rev: plan.rev,
            status: plan.status,
            active: true,
            headline: view.headline,
            progress: view.progress,
            next_action: plan.next_action,
            updated_slice: null,
            scars: view.scars,
          }),
        );
      }

      const target = await resolvePlanTarget(planIdInput);
      if (!target.ok) return target.result;
      const { plan_id, notePath } = target;
      const next = await withThreadPlanLock(
        {
          vaultPath: effectiveVaultPath,
          notePath,
          planId: plan_id,
          operationId: `server-status-update:${randomUUID()}`,
        },
        async (plan) => {
          const now = new Date();
          const targetSlice = plan.slices.find((s) => s.id === slice_id);
          const from = targetSlice?.status ?? ("pending" as const);
          const unmetBeforeUpdate =
            status === "done" ? unmetDependencies(plan, slice_id) : [];
          const readyBefore = readyIds(plan, now);
          const { journalPath, ordered } = await prepareThreadMutation(
            { vaultPath: effectiveVaultPath, notePath, planId: plan_id, actor: DEFAULT_AGENT_ID },
            plan,
            now,
          );
          const applied = applyOrchestratorSliceUpdate(
            plan,
            slice_id,
            status,
            evidence,
            { force, forceReason: force_reason },
          );
          const updated = applied.plan;
          try {
            await persistPlanThenRevokeClaimSecrets(
              updated,
              { vaultPath: effectiveVaultPath, notePath },
              plan_id,
              applied.revoked_claim_id ? [applied.revoked_claim_id] : [],
            );
          } catch (error) {
            if (error instanceof PlanHistoryAppendError && applied.revoked_claim_id) {
              await pruneSliceReceiptsOnGenerationAdvance(
                effectiveVaultPath,
                plan_id,
                slice_id,
                applied.previous_slice.generation ?? 0,
              );
            }
            throw error;
          }
          if (applied.revoked_claim_id) {
            await pruneSliceReceiptsOnGenerationAdvance(
              effectiveVaultPath,
              plan_id,
              slice_id,
              applied.previous_slice.generation ?? 0,
            );
          }
          await appendJournal(journalPath, {
            kind: "status_changed",
            slice_id,
            from,
            to: status,
            evidence,
            at: now.toISOString(),
            ...(unmetBeforeUpdate.length > 0
              ? {
                  depends_on_override: {
                    unmet: unmetBeforeUpdate,
                    reason: force_reason,
                    forced_by: DEFAULT_AGENT_ID,
                  },
                }
              : {}),
          });
          if (
            status === "done" &&
            targetSlice &&
            targetSlice.gate &&
            targetSlice.gate.trim()
          ) {
            await appendJournal(journalPath, {
              kind: "gate_passed",
              slice_id,
              evidence: evidence ?? "",
              at: now.toISOString(),
            });
          }
          const supplemental: Array<{
            idempotencyKey: string;
            kind: string;
            sliceId?: string;
            payload?: Record<string, unknown>;
          }> = [];
          if (applied.revoked_claim_id) {
            supplemental.push({
              idempotencyKey: deriveSystemEventKey(
                "slice.claim_revoked",
                plan_id,
                slice_id,
                String(updated.rev),
              ),
              kind: "slice.claim_revoked",
              sliceId: slice_id,
              payload: { slice_id },
            });
          }
          const lockSession = resolveMcpSessionIdentity(effectiveVaultPath);
          await recordThreadMutationEvents({
            journalPath,
            planId: plan_id,
            rev: updated.rev,
            actor: DEFAULT_AGENT_ID,
            actorSessionKey: lockSession.sessionKey,
            operationKey: deriveSystemEventKey(
              "status_changed",
              plan_id,
              slice_id,
              String(updated.rev),
            ),
            kind: "status_changed",
            sliceId: slice_id,
            payload: { from, to: status },
            readyBefore,
            readyAfter: readyIds(updated, now),
            plan: updated,
            planBefore: plan,
            now,
            orderedSnapshot: ordered,
            supplementalEvents: supplemental.length > 0 ? supplemental : undefined,
          });
          const vp = vaultPrincipalSubscriberId(effectiveVaultPath);
          await acknowledgeSelfAuthoredPrefixInVault(
            effectiveVaultPath,
            plan_id,
            [
              formatSessionSubscriberId(DEFAULT_AGENT_ID, lockSession.sessionKey),
              DEFAULT_AGENT_ID,
              ...(vp ? [formatSessionSubscriberId(vp, lockSession.sessionKey), vp] : []),
            ],
            { actorSessionKey: lockSession.sessionKey },
          ).catch(() => {});
          // P10/H6: terminal plans stop being injected without clobbering sibling _active_plan.json.
          if (isTerminalPlanStatus(updated.status)) {
            try {
              await clearActivePlan(effectiveVaultPath, {
                sessionKey: lockSession.sessionKey,
                expectedPlanId: plan_id,
                markDetached: false,
              });
            } catch {
              // active pointer maintenance is advisory
            }
          }
          return updated;
        },
      );

      let activeState: boolean | undefined;
      if (activate === false) {
        await clearActivePlan(effectiveVaultPath, {
          sessionKey: mcpSession.sessionKey,
          sessionOnly: true,
        });
        activeState = false;
      } else if (activate === true) {
        if (isTerminalPlanStatus(next.status)) {
          activeState = false;
        } else {
          const activated = await activatePlanChecked(
            effectiveVaultPath,
            plan_id,
            notePath,
            {
              sessionKey: mcpSession.sessionKey,
              sessionId: mcpSession.sessionId,
              hostPid: mcpSession.hostPid,
            },
          );
          if (!activated.ok) {
            return textResult(JSON.stringify({ error: activated.error }, null, 2));
          }
          await ensureRelayCursorsForActiveSession(
            effectiveVaultPath,
            plan_id,
            mcpSession.sessionKey,
          );
          activeState = true;
        }
      }

      const view = compactPlanView(next);
      const rawUpdatedSlice = next.slices.find((s) => s.id === slice_id) ?? null;
      const useCompact = compact !== false && include_full_plan !== true;
      if (useCompact) {
        const compactSlice = rawUpdatedSlice
          ? {
              id: rawUpdatedSlice.id,
              title: rawUpdatedSlice.title,
              status: rawUpdatedSlice.status,
              ...(rawUpdatedSlice.depends_on?.length
                ? { depends_on: rawUpdatedSlice.depends_on }
                : {}),
              ...(rawUpdatedSlice.gate ? { gate: rawUpdatedSlice.gate } : {}),
              ...(typeof rawUpdatedSlice.evidence === "string"
                ? {
                    evidence:
                      rawUpdatedSlice.evidence.length <= 240
                        ? rawUpdatedSlice.evidence
                        : `${rawUpdatedSlice.evidence.slice(0, 237)}...`,
                  }
                : {}),
            }
          : null;
        return textResult(
          serializeCompactBudget({
            plan_id: next.plan_id,
            rev: next.rev,
            status: next.status,
            ...(activeState !== undefined ? { active: activeState } : {}),
            headline: view.headline,
            progress: view.progress,
            next_action: next.next_action,
            updated_slice: compactSlice,
            scars: view.scars,
          }),
        );
      }

      const responsePlan =
        include_full_plan === true
          ? next
          : {
              ...next,
              slices: next.slices.map((s) =>
                s.id === slice_id
                  ? s
                  : {
                      ...s,
                      ...(typeof s.evidence === "string" && s.evidence.length > 240
                        ? { evidence: `${s.evidence.slice(0, 240)}…` }
                        : {}),
                    },
              ),
              scar_tissue: next.scar_tissue.slice(-5),
              scar_tissue_total: next.scar_tissue.length,
            };
      return textResult(
        JSON.stringify(
          {
            plan_id: next.plan_id,
            rev: next.rev,
            status: next.status,
            ...(activeState !== undefined ? { active: activeState } : {}),
            headline: view.headline,
            progress: view.progress,
            next_action: next.next_action,
            updated_slice: rawUpdatedSlice,
            scars: view.scars,
            plan: responsePlan,
          },
          null,
          2,
        ),
      );
    } catch (error) {
      return threadWorkerErrorResult("plan.update", error);
    }
  },
);

// ---------------------------------------------------------------------------
// 12. minni_thread_scar
// ---------------------------------------------------------------------------
server.registerTool(
  "minni_thread_scar",
  {
    title: "Minni Thread Scar",
    description:
      "Record a dead-end, failed command, or rejected hypothesis during plan execution to prevent retries (and promote durable resolved scars to repo memory), returning a compact <1 KB receipt. plan_id defaults to the active plan.",
    inputSchema: {
      plan_id: z.string().min(1).optional(),
      slice_id: z.string().min(1).optional(),
      kind: z.enum(["failed_command", "dead_end", "rejected_hypothesis"]),
      signal: z.string().min(1),
      resolution: z.string().optional(),
      promote: z.boolean().optional(),
      workspace_id: z.string().optional(),
    },
  },
  async ({ plan_id: planIdInput, slice_id, kind, signal, resolution, promote, workspace_id }) => {
    const gated = await requireSharedGate("plan.scar", {
      plan_id: planIdInput,
      ...(slice_id ? { slice_id } : {}),
      kind,
    });
    if (gated) return gated;
    try {
      const effectiveVaultPath = DEFAULT_VAULT_PATH;
      const target = await resolvePlanTarget(planIdInput);
      if (!target.ok) return target.result;
      const { plan_id, notePath } = target;
      const next = await withThreadPlanLock(
        {
          vaultPath: effectiveVaultPath,
          notePath,
          planId: plan_id,
          operationId: `server-scar:${randomUUID()}`,
        },
        async (plan) => {
          const now = new Date();
          const readyBefore = readyIds(plan, now);
          const { journalPath, ordered } = await prepareThreadMutation(
            { vaultPath: effectiveVaultPath, notePath, planId: plan_id, actor: DEFAULT_AGENT_ID },
            plan,
            now,
          );
          const updated = addScar(plan, { kind, signal, resolution });
          await persistPlan(updated, {
            vaultPath: effectiveVaultPath,
            notePath,
          });
          await appendJournal(journalPath, {
            kind: "scar_added",
            signal,
            at: now.toISOString(),
          });
          const mcpSession = resolveMcpSessionIdentity(effectiveVaultPath);
          await recordThreadMutationEvents({
            journalPath,
            planId: plan_id,
            rev: updated.rev,
            actor: DEFAULT_AGENT_ID,
            actorSessionKey: mcpSession.sessionKey,
            operationKey: deriveSystemEventKey("scar_added", plan_id, String(updated.rev)),
            kind: "scar_added",
            ...(slice_id ? { sliceId: slice_id } : {}),
            payload: { kind },
            readyBefore,
            readyAfter: readyIds(updated, now),
            plan: updated,
            now,
            orderedSnapshot: ordered,
          });
          const vp = vaultPrincipalSubscriberId(effectiveVaultPath);
          await acknowledgeSelfAuthoredPrefixInVault(
            effectiveVaultPath,
            plan_id,
            [
              formatSessionSubscriberId(DEFAULT_AGENT_ID, mcpSession.sessionKey),
              DEFAULT_AGENT_ID,
              ...(vp ? [formatSessionSubscriberId(vp, mcpSession.sessionKey), vp] : []),
            ],
            { actorSessionKey: mcpSession.sessionKey },
          ).catch(() => {});
          return updated;
        },
      );

      let promoted = false;
      const shouldPromote =
        promote === true ||
        (promote !== false && typeof resolution === "string" && resolution.trim().length > 0);
      if (shouldPromote) {
        try {
          const cleanSignal = signal.replace(/\s+/g, " ").trim();
          const cleanResolution =
            typeof resolution === "string" ? resolution.replace(/\s+/g, " ").trim() : "";
          const scarTitle = `Scar (${kind}): ${cleanSignal.slice(0, 64)}`;
          const scarText = [
            `Scar (${kind}) in plan ${next.plan_id}${slice_id ? ` (${slice_id})` : ""}: ${cleanSignal.slice(0, 600)}`,
            ...(cleanResolution ? [`Resolution: ${cleanResolution.slice(0, 600)}`] : []),
          ].join("\n");
          const quality = await assessLearningQualityAsync({
            title: scarTitle,
            content: scarText,
            category: "scar",
            source: next.plan_id,
          });
          if (!flagsSensitiveMaterial(quality) && quality.passed) {
            const effectiveWs = resolveSessionWorkspaceId(effectiveVaultPath, workspace_id);
            const store = await learnMemory({
              content: scarText,
              category: "scar",
              agentId: DEFAULT_AGENT_ID,
              workspaceId: effectiveWs,
              source: next.plan_id,
            }).catch((err: unknown) => ({
              ok: false,
              error: err instanceof Error ? err.message : String(err),
              data: undefined,
            }));
            await vaultFirstLearn({
              vaultPath: effectiveVaultPath,
              title: scarTitle,
              content: scarText,
              category: "scar",
              source: next.plan_id,
              agentId: DEFAULT_AGENT_ID,
              storeResult: { ok: store.ok, data: store.data, error: store.error },
              quality,
            });
            promoted = true;
          }
        } catch {
          promoted = false;
        }
      }

      const matchedIdx = next.scar_tissue.findIndex(
        (s) => s.kind === kind && s.signal === signal,
      );
      const scarIdx = matchedIdx >= 0 ? matchedIdx : Math.max(0, next.scar_tissue.length - 1);
      const signalPreview = signal.length <= 200 ? signal : `${signal.slice(0, 197)}...`;
      return textResult(
        serializeCompactBudget(
          {
            plan_id: next.plan_id,
            rev: next.rev,
            scar: {
              id: `scar-${scarIdx + 1}`,
              kind,
              slice_id: slice_id ?? null,
              signal: signalPreview,
            },
            scar_count: next.scar_tissue.length,
            promoted,
          },
          980,
        ),
      );
    } catch (error) {
      return threadWorkerErrorResult("plan.scar", error);
    }
  },
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  void drainPendingWorkerWritesForVault(DEFAULT_VAULT_PATH).catch(() => {});
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { CLAUDECODE_AGENT_ID, DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH, DEFAULT_WORKSPACE_ID } from "./config.js";
import { assessLearningQuality, routeMemoryIntent } from "./policy.js";
import { compileVault, formatRecall, handoffMemory, learnMemory, recallMemory, statusAndAudit } from "./sovereign.js";
import { buildHandoffPacket, extractScarTissue, prepareOutcome, prepareTask } from "./task.js";
import { buildTeamEvidencePacket, buildTeamPromotionPacket, buildTeamRuntime } from "./team.js";
import { auditReport, auditTail, recordAudit, searchVaultNotes, vaultFirstLearn, writeVaultPage } from "./vault.js";
import { wrapEnvelope } from "./agent_envelope.js";

function textResult(text: string) {
  return {
    content: [{ type: "text" as const, text }],
  };
}

const server = new McpServer({
  name: "sovereign-memory",
  version: "0.1.0",
});

server.registerTool(
  "sovereign_prepare_task",
  {
    title: "Sovereign Prepare Task",
    description:
      "Build a compact Codex task packet from vault notes, daemon recall, constraints, and optional AFM distillation.",
    inputSchema: {
      task: z.string().min(1),
      budgetTokens: z.number().int().min(1000).max(32000).optional(),
      profile: z.enum(["compact", "standard", "deep"]).optional(),
      useAfm: z.boolean().optional(),
      layer: z.enum(["identity", "episodic", "knowledge", "artifact"]).optional(),
      limit: z.number().int().min(1).max(12).optional(),
      workspaceId: z.string().optional(),
      agentId: z.string().optional(),
      vaultPath: z.string().optional(),
      includeVault: z.boolean().optional(),
      afmPrepareUrl: z.string().optional(),
      afmModel: z.string().optional(),
    },
  },
  async ({
    task,
    budgetTokens,
    profile,
    useAfm,
    layer,
    limit,
    workspaceId,
    agentId,
    vaultPath,
    includeVault,
    afmPrepareUrl,
    afmModel,
  }) => {
    const packet = await prepareTask({
      task,
      budgetTokens,
      profile,
      useAfm,
      layer,
      limit,
      workspaceId,
      agentId,
      vaultPath,
      includeVault,
      afmPrepareUrl,
      afmModel,
    });
    return textResult(JSON.stringify(packet, null, 2));
  },
);

server.registerTool(
  "sovereign_prepare_outcome",
  {
    title: "Sovereign Prepare Outcome",
    description:
      "Build a dry-run post-task outcome packet with learn/log/expire/do-not-store recommendations without writing memory.",
    inputSchema: {
      task: z.string().min(1),
      summary: z.string().min(1),
      changedFiles: z.array(z.string()).optional(),
      verification: z.array(z.string()).optional(),
      profile: z.enum(["compact", "standard", "deep"]).optional(),
      useAfm: z.boolean().optional(),
      vaultPath: z.string().optional(),
      afmPrepareUrl: z.string().optional(),
      afmModel: z.string().optional(),
    },
  },
  async ({ task, summary, changedFiles, verification, profile, useAfm, vaultPath, afmPrepareUrl, afmModel }) => {
    const packet = await prepareOutcome({
      task,
      summary,
      changedFiles,
      verification,
      profile,
      useAfm,
      vaultPath,
      afmPrepareUrl,
      afmModel,
    });
    return textResult(JSON.stringify(packet, null, 2));
  },
);

const teamAgentSchema = z.object({
  agentId: z.string().optional(),
  role: z.enum(["explorer", "worker", "reviewer", "scribe"]).optional(),
  focus: z.string().min(1),
  ownership: z.array(z.string()).optional(),
  permissions: z.array(z.enum(["read", "write", "test", "network", "memory-recall"])).optional(),
  model: z.string().optional(),
});

const teamTemporaryProfileSchema = z.object({
  agentId: z.string().min(1),
  role: z.enum(["explorer", "worker", "reviewer", "scribe"]),
  focus: z.string().min(1),
  ownership: z.array(z.string()),
  permissions: z.array(z.enum(["read", "write", "test", "network", "memory-recall"])),
  model: z.string().optional(),
  memoryPolicy: z.object({
    recall: z.literal("allowed"),
    learn: z.literal("manual-only"),
    vaultWrites: z.literal("manual-only"),
  }),
  lifetime: z.literal("temporary"),
  promotionRule: z.string().min(1),
});

const teamPromotionCandidateSchema = z.object({
  agentId: z.string().min(1),
  recommended: z.boolean(),
  score: z.number(),
  reasons: z.array(z.string()),
  nextStep: z.string().min(1),
});

server.registerTool(
  "sovereign_team_runtime",
  {
    title: "Sovereign Team Runtime",
    description:
      "Build a deterministic temporary team runtime: agent profiles, task ledger, hydration packets, gates, and non-goals. Does not spawn agents or write durable learnings.",
    inputSchema: {
      task: z.string().min(1),
      agents: z.array(teamAgentSchema).optional(),
      coordinatorAgentId: z.string().optional(),
      workspaceId: z.string().optional(),
      vaultPath: z.string().optional(),
      profile: z.enum(["compact", "standard", "deep"]).optional(),
      limit: z.number().int().min(1).max(12).optional(),
      includeVault: z.boolean().optional(),
      useAfm: z.boolean().optional(),
    },
  },
  async ({ task, agents, coordinatorAgentId, workspaceId, vaultPath, profile, limit, includeVault, useAfm }) => {
    const packet = await buildTeamRuntime({
      task,
      agents,
      coordinatorAgentId,
      workspaceId,
      vaultPath,
      profile,
      limit,
      includeVault,
      useAfm,
    });
    return textResult(JSON.stringify(packet, null, 2));
  },
);

server.registerTool(
  "sovereign_team_evidence",
  {
    title: "Sovereign Team Evidence",
    description:
      "Summarize temporary agent evidence reports and promotion candidates. Dry-run only; promotion and learning remain explicit.",
    inputSchema: {
      task: z.string().min(1),
      runtimeId: z.string().optional(),
      results: z.array(
        z.object({
          agentId: z.string().min(1),
          status: z.enum(["queued", "in_progress", "blocked", "completed"]),
          summary: z.string().min(1),
          evidence: z.array(z.string()).optional(),
          changedFiles: z.array(z.string()).optional(),
          verification: z.array(z.string()).optional(),
          blockers: z.array(z.string()).optional(),
        }),
      ),
    },
  },
  async ({ task, runtimeId, results }) => {
    const packet = buildTeamEvidencePacket({ task, runtimeId, results });
    return textResult(JSON.stringify(packet, null, 2));
  },
);

server.registerTool(
  "sovereign_team_promotion",
  {
    title: "Sovereign Team Promotion",
    description:
      "Draft a permanent agent profile from a temporary team profile only after explicit approval. Dry-run only; never writes durable memory.",
    inputSchema: {
      agent: teamTemporaryProfileSchema,
      evidence: teamPromotionCandidateSchema,
      requestedPermissions: z.array(z.enum(["read", "write", "test", "network", "memory-recall"])).optional(),
      approved: z.boolean().optional(),
      permanentAgentId: z.string().optional(),
    },
  },
  async ({ agent, evidence, requestedPermissions, approved, permanentAgentId }) => {
    const packet = await buildTeamPromotionPacket({ agent, evidence, requestedPermissions, approved, permanentAgentId });
    return textResult(JSON.stringify(packet, null, 2));
  },
);

server.registerTool(
  "sovereign_status",
  {
    title: "Sovereign Memory Status",
    description: "Check Sovereign daemon, AFM health, Codex vault, and audit state.",
    inputSchema: {
      vaultPath: z.string().optional(),
    },
  },
  async ({ vaultPath }) => {
    const report = await statusAndAudit(vaultPath ?? DEFAULT_VAULT_PATH);
    return textResult(JSON.stringify(report, null, 2));
  },
);

server.registerTool(
  "sovereign_compile_vault",
  {
    title: "Sovereign Compile Vault",
    description:
      "Run an opt-in AFM compile pass against the vault. Defaults to dry-run and only drafts pages for review.",
    inputSchema: {
      passName: z.enum(["session_distillation", "synthesis", "procedure_extraction", "reorganization", "pruning"]).optional(),
      vaultPath: z.string().optional(),
      dryRun: z.boolean().optional(),
    },
  },
  async ({ passName, vaultPath, dryRun }) => {
    const result = await compileVault({
      passName: passName ?? "session_distillation",
      vaultPath: vaultPath ?? DEFAULT_VAULT_PATH,
      dryRun: dryRun ?? true,
    });
    return textResult(JSON.stringify(result, null, 2));
  },
);

server.registerTool(
  "sovereign_route",
  {
    title: "Sovereign Memory Intent Router",
    description: "Classify whether a task should recall, learn, write a vault note, show audit, or do nothing.",
    inputSchema: {
      task: z.string().min(1),
      vaultPath: z.string().optional(),
    },
  },
  async ({ task, vaultPath }) => {
    const intent = routeMemoryIntent(task);
    await recordAudit(vaultPath ?? DEFAULT_VAULT_PATH, {
      tool: "sovereign_route",
      summary: `${intent.action}: ${task.slice(0, 120)}`,
      details: intent as unknown as Record<string, unknown>,
    });
    return textResult(JSON.stringify(intent, null, 2));
  },
);

server.registerTool(
  "sovereign_recall",
  {
    title: "Sovereign Memory Recall",
    description: "Recall Sovereign Memory context and log the lookup in the Codex vault.",
    inputSchema: {
      query: z.string().min(1),
      layer: z.enum(["identity", "episodic", "knowledge", "artifact"]).optional(),
      limit: z.number().int().min(1).max(20).optional(),
      workspaceId: z.string().optional(),
      agentId: z.string().optional(),
      vaultPath: z.string().optional(),
      includeVault: z.boolean().optional(),
    },
  },
  async ({ query, layer, limit, workspaceId, agentId, vaultPath, includeVault }) => {
    const effectiveVaultPath = vaultPath ?? DEFAULT_VAULT_PATH;
    const vaultResults = includeVault === false ? [] : await searchVaultNotes(effectiveVaultPath, query, Math.min(limit ?? 5, 8));
    const result = await recallMemory({
      query,
      layer,
      limit,
      workspaceId: workspaceId ?? DEFAULT_WORKSPACE_ID,
      agentId: agentId ?? DEFAULT_AGENT_ID,
    });
    const responseText = result.ok && result.data ? formatRecall(query, result.data, vaultResults) : `Recall failed: ${result.error}`;
    await recordAudit(effectiveVaultPath, {
      tool: "sovereign_recall",
      summary: query,
      details: {
        ok: result.ok,
        layer,
        limit,
        workspaceId,
        agentId,
        includeVault: includeVault !== false,
        vaultMatches: vaultResults.map((match) => match.relativePath),
        error: result.error,
      },
    });
    return textResult(responseText);
  },
);

server.registerTool(
  "sovereign_learn",
  {
    title: "Sovereign Memory Learn",
    description: "Write a Codex vault note first, then store the learning through Sovereign Memory.",
    inputSchema: {
      title: z.string().min(1),
      content: z.string().min(1),
      category: z.string().optional(),
      source: z.string().optional(),
      agentId: z.string().optional(),
      workspaceId: z.string().optional(),
      vaultPath: z.string().optional(),
      requireQuality: z.boolean().optional(),
    },
  },
  async ({ title, content, category, source, agentId, workspaceId, vaultPath, requireQuality }) => {
    const quality = assessLearningQuality({ title, content, category, source });
    if (requireQuality === true && !quality.ok) {
      await recordAudit(vaultPath ?? DEFAULT_VAULT_PATH, {
        tool: "sovereign_learn",
        summary: `quality-blocked: ${title}`,
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
      agentId: agentId ?? DEFAULT_AGENT_ID,
      workspaceId: workspaceId ?? DEFAULT_WORKSPACE_ID,
    });
    const note = await vaultFirstLearn({
      vaultPath: vaultPath ?? DEFAULT_VAULT_PATH,
      title,
      content,
      category,
      source,
      agentId: agentId ?? DEFAULT_AGENT_ID,
      storeResult: { ok: store.ok, data: store.data, error: store.error },
    });
    return textResult(
      JSON.stringify(
        {
          status: store.ok ? "learned" : "vault-written-memory-store-failed",
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

server.registerTool(
  "sovereign_learning_quality",
  {
    title: "Sovereign Learning Quality",
    description: "Review a potential memory before writing it to the Codex vault or Sovereign daemon.",
    inputSchema: {
      title: z.string().min(1),
      content: z.string().min(1),
      category: z.string().optional(),
      source: z.string().optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ title, content, category, source, vaultPath }) => {
    const quality = assessLearningQuality({ title, content, category, source });
    await recordAudit(vaultPath ?? DEFAULT_VAULT_PATH, {
      tool: "sovereign_learning_quality",
      summary: title,
      details: { quality },
    });
    return textResult(JSON.stringify(quality, null, 2));
  },
);

server.registerTool(
  "sovereign_vault_write",
  {
    title: "Sovereign Vault Write",
    description: "Write a structured Codex Obsidian vault page without storing it as a durable learning.",
    inputSchema: {
      title: z.string().min(1),
      content: z.string().min(1),
      section: z.enum(["raw", "entities", "concepts", "decisions", "syntheses", "sessions"]),
      source: z.string().optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ title, content, section, source, vaultPath }) => {
    const note = await writeVaultPage({
      vaultPath: vaultPath ?? DEFAULT_VAULT_PATH,
      title,
      content,
      section,
      source,
    });
    return textResult(JSON.stringify({ status: "written", note }, null, 2));
  },
);

server.registerTool(
  "sovereign_audit_report",
  {
    title: "Sovereign Audit Report",
    description: "Summarize recent Sovereign Memory tool activity for transparent self-auditing.",
    inputSchema: {
      limit: z.number().int().min(1).max(200).optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ limit, vaultPath }) => {
    const report = await auditReport(vaultPath ?? DEFAULT_VAULT_PATH, limit ?? 100);
    return textResult(JSON.stringify(report, null, 2));
  },
);

server.registerTool(
  "sovereign_audit_tail",
  {
    title: "Sovereign Audit Tail",
    description: "Show recent Sovereign Memory audit entries from the Codex vault.",
    inputSchema: {
      limit: z.number().int().min(1).max(100).optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ limit, vaultPath }) => {
    const tail = await auditTail(vaultPath ?? DEFAULT_VAULT_PATH, limit ?? 20);
    return textResult(tail.text || "No audit entries yet.");
  },
);

server.registerTool(
  "sovereign_negotiate_handoff",
  {
    title: "Sovereign Negotiate Handoff",
    description:
      "Build an agent-to-agent handoff envelope (identity, top recalls with provenance, scar tissue, open questions, inbox pointer) optimized for another LLM to consume.",
    inputSchema: {
      task: z.string().min(1),
      agentId: z.string().optional(),
      toAgent: z.string().optional(),
      workspaceId: z.string().optional(),
      vaultPath: z.string().optional(),
      openQuestions: z.array(z.string()).optional(),
      inboxPointer: z.string().optional(),
      limit: z.number().int().min(1).max(12).optional(),
    },
  },
  async ({ task, agentId, toAgent, workspaceId, vaultPath, openQuestions, inboxPointer, limit }) => {
    const effectiveVaultPath = vaultPath ?? DEFAULT_VAULT_PATH;
    const fromAgent = agentId ?? DEFAULT_AGENT_ID;
    const targetAgent = toAgent ?? CLAUDECODE_AGENT_ID;
    const tail = await auditTail(effectiveVaultPath, 60);
    const scarTissue = extractScarTissue(tail.entries);
    const packet = await buildHandoffPacket({
      task,
      agentId: fromAgent,
      workspaceId,
      vaultPath: effectiveVaultPath,
      openQuestions,
      inboxPointer,
      scarTissue,
      limit,
    });
    const envelope = wrapEnvelope({
      event: "Handoff",
      agent: packet.agentOrigin,
      body: {
        identity: packet.identity,
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
        inbox_pointer: packet.inboxPointer,
        task: packet.task,
      },
    });
    const handoffPacket = {
      from_agent: packet.agentOrigin,
      to_agent: targetAgent,
      kind: "handoff",
      task: packet.task,
      envelope,
      wikilink_refs: packet.topRecalls.map((source) => source.relativePath.replace(/\.md$/, "")),
      trace_id: `plugin-${Date.now().toString(36)}`,
      created_at: new Date().toISOString(),
    };
    const delivery = await handoffMemory({
      fromAgent: packet.agentOrigin,
      toAgent: targetAgent,
      packet: handoffPacket,
    });
    await recordAudit(effectiveVaultPath, {
      tool: "sovereign_negotiate_handoff",
      summary: task.slice(0, 120),
      details: {
        agent: packet.agentOrigin,
        to_agent: targetAgent,
        workspace: packet.workspace,
        recalls: packet.topRecalls.length,
        scar_tissue: packet.scarTissue.length,
        delivered: delivery.ok,
        delivery_error: delivery.ok ? undefined : delivery.error,
      },
    });
    return textResult(JSON.stringify({ envelope, handoff_packet: handoffPacket, delivery }, null, 2));
  },
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

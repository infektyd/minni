// Shared hook HANDLERS (review panel, plan-parity follow-up): codex-hook.ts,
// grok-hook.ts and kilocode-hook.ts were ~360-line near-clones whose four
// handler bodies differed ONLY in config constants and a few flagged
// behaviors. hook-utils.ts holds the protocol leaf helpers; this module holds
// the stateful handler logic, parameterized by a typed per-agent config, so
// future changes (evidence envelope format, plan injection, inbox drain
// logic) have ONE maintenance surface instead of four. hook.ts (claude-code)
// diverges structurally (PreCompact cannot inject context) and keeps its own
// handlers.
import {
  MEMORY_CONTRACT,
  envelopeBudgetFor,
  hashTaskSignature,
  wrapEnvelope,
} from "./agent_envelope.js";
import type { EnvelopeEvent } from "./agent_envelope.js";
import {
  VALID_EVENTS,
  asString,
  emit,
  readStdin,
  vaultRecallToBody,
  withHookContext,
  workspaceFromPayload,
} from "./hook-utils.js";
import type { HookOutput } from "./hook-utils.js";
import { compactPlanPointer, resolveActivePlanView } from "./plan.js";
import { routeMemoryIntent } from "./policy.js";
import {
  buildStatusReport,
  formatRecall,
  readAgentContext,
  recallMemory,
} from "./sovereign.js";
import { extractScarTissue, prepareOutcome } from "./task.js";
import {
  auditTail,
  ensureVault,
  buildPendingLearningsSection,
  expireStaleInboxHandoffs,
  readInboxStatus,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  writeInbox,
} from "./vault.js";

export interface AgentHookConfig {
  /** Stamped agent identity (e.g. "codex", "grok-build"). */
  agentId: string;
  vaultPath: string;
  defaultWorkspaceId: string;
  contextWindow: number;
  hooksEnabled: boolean;
  /**
   * identity.runtime AND the `node dist/cli.js read <runtime>` target.
   * Required for the default "agent-context" boot identity; omit it (with
   * `bootIdentity: "identity-recall"`) for agents without a daemon layer1
   * channel (kilocode).
   */
  runtime?: string;
  /**
   * Entry-point script name for the layer1 fallback command (e.g.
   * "codex-hook.js"). Required alongside `runtime` for "agent-context" boots.
   */
  hookScript?: string;
  /** Audit tool-name prefix (e.g. "hook_codex" -> hook_codex_session_start). */
  auditPrefix: string;
  /**
   * Inbox kind for the PreCompact handoff (e.g. "codex_precompact_handoff").
   * When omitted, PreCompact does NOT write an inbox handoff file (kilocode's
   * behavior — its envelope carries the scar tissue directly).
   */
  precompactKind?: string;
  /**
   * How SessionStart sources boot identity:
   * - "agent-context" (default; codex/grok): daemon readAgentContext layer1
   *   read, surfaced as `layer1_source` + `fallback_commands` and prefixed to
   *   the envelope as native layer1 text.
   * - "identity-recall" (kilocode): identity-layer recallMemory, surfaced as
   *   the envelope's `recall` body.
   */
  bootIdentity?: "agent-context" | "identity-recall";
  /**
   * Stop systemMessage call-to-action after "drafted to inbox (<path>).".
   * Defaults to the MCP-tool phrasing; kilocode points at /minni:learn.
   */
  stopCommitHint?: string;
  /**
   * When true, Stop writes the inbox file + audit entry even with zero
   * candidates (codex's historical behavior); when false, an empty outcome
   * early-returns before any write so the inbox is never littered with empty
   * files (grok's and kilocode's behavior).
   */
  alwaysWriteStopInbox: boolean;
}

/** Test seam: lets behavioral tests drive the zero-candidate Stop branch. */
export interface AgentHookDeps {
  prepareOutcome?: typeof prepareOutcome;
}

export interface AgentHookHandlers {
  handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput>;
  handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput>;
  handlePreCompact(payload: Record<string, unknown>): Promise<HookOutput>;
  handleStop(payload: Record<string, unknown>): Promise<HookOutput>;
  dispatch(event: string, payload: Record<string, unknown>): Promise<HookOutput>;
}

export function createHookHandlers(
  config: AgentHookConfig,
  deps: AgentHookDeps = {},
): AgentHookHandlers {
  const workspaceFor = (payload: Record<string, unknown>): string =>
    workspaceFromPayload(payload, config.defaultWorkspaceId);
  const bootIdentity = config.bootIdentity ?? "agent-context";
  const prepareOutcomeFn = deps.prepareOutcome ?? prepareOutcome;

  async function handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput> {
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    const workspaceId = workspaceFor(payload);
    await ensureVault(config.vaultPath);

    // TTL-reap stale file handoffs BEFORE the honest read so they neither occupy
    // the capped slice nor inflate totals; they surface once below as 'expired'.
    const expiredHandoffs = await expireStaleInboxHandoffs(config.vaultPath);
    const [status, tail, identityRead, identityRecall, inboxStatus] = await Promise.all([
      buildStatusReport({ vaultPath: config.vaultPath }),
      auditTail(config.vaultPath, 5),
      bootIdentity === "agent-context"
        ? readAgentContext({ agentId: config.agentId, limit: 8 })
        : undefined,
      bootIdentity === "identity-recall"
        ? recallMemory({
            query: `boot identity for ${workspaceId}`,
            layer: "identity",
            limit: 4,
            agentId: config.agentId,
            workspaceId,
          })
        : undefined,
      readInboxStatus(config.vaultPath, 3),
    ]);
    const pending = inboxStatus.entries;
    const handoffContext = await resolveInboxHandoffContext(config.vaultPath, pending);

    // Plan parity (audit C5): SessionStart injects the FULL active-plan view for
    // boot/rehydration, exactly like the claude-code hook.
    let activePlan: Awaited<ReturnType<typeof resolveActivePlanView>>;
    try {
      activePlan = await resolveActivePlanView(config.vaultPath);
    } catch {
      // ignore
    }

    const envelopeBody: Record<string, unknown> = {
      contract: MEMORY_CONTRACT,
      identity: {
        agent: config.agentId,
        workspace: workspaceId,
        vault: config.vaultPath,
        session_id: sessionId,
        daemon_ok: status.socket.ok,
        afm_ok: status.afm.ok,
        ...(config.runtime !== undefined ? { runtime: config.runtime } : {}),
      },
      pending_learnings: buildPendingLearningsSection(inboxStatus, expiredHandoffs),
      handoff_context: handoffContext.map((snippet) => ({
        ref: snippet.ref,
        path: snippet.relativePath,
        snippet: snippet.snippet,
      })),
      audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]),
    };

    if (identityRead !== undefined) {
      envelopeBody.layer1_source =
        identityRead.ok && identityRead.data?.context
          ? {
              ok: true,
              agent_origin: identityRead.data.agent_id ?? config.agentId,
              backend: identityRead.data.backend,
            }
          : { ok: false, error: identityRead.error };
      envelopeBody.fallback_commands = {
        layer1: `node dist/${config.hookScript} SessionStart < /dev/null`,
        daemon_read: `node dist/cli.js read ${config.runtime}`,
        recall: "node dist/cli.js prepare '<task>'",
      };
    }

    if (identityRecall !== undefined) {
      envelopeBody.recall =
        identityRecall.ok && identityRecall.data
          ? {
              ok: true,
              results: identityRecall.data.results,
              agent_origin: identityRecall.data.agent_id ?? config.agentId,
              layer: identityRecall.data.layer,
            }
          : { ok: false, error: identityRecall.error };
    }

    if (activePlan !== undefined) {
      envelopeBody.active_plan = activePlan;
    }

    const envelope = wrapEnvelope({
      event: "SessionStart",
      agent: config.agentId,
      budget: envelopeBudgetFor(config.contextWindow),
      body: envelopeBody,
    });

    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_session_start`,
      summary: `boot ${sessionId}`,
      details: {
        daemon_ok: status.socket.ok,
        afm_ok: status.afm.ok,
        pending_inbox: inboxStatus.totalPending,
        expired_handoffs: expiredHandoffs.length,
        handoff_context: handoffContext.length,
        workspace: workspaceId,
      },
    });

    const nativeLayer1 =
      identityRead?.ok && identityRead.data?.context ? identityRead.data.context.trim() : "";
    return withHookContext("SessionStart", [nativeLayer1, envelope].filter(Boolean).join("\n\n"));
  }

  async function handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput> {
    const prompt = asString(payload.prompt) || asString(payload.user_prompt);
    if (!prompt.trim()) {
      return { continue: true };
    }

    const intent = routeMemoryIntent(prompt);
    if (intent.action === "none" && !intent.automaticAllowed) {
      return { continue: true };
    }

    const workspaceId = workspaceFor(payload);
    const signature = hashTaskSignature(prompt);
    const [vaultResults, recall] = await Promise.all([
      searchVaultNotes(config.vaultPath, prompt, 6),
      recallMemory({
        query: prompt,
        limit: 6,
        agentId: config.agentId,
        workspaceId,
      }),
    ]);

    if (vaultResults.length === 0 && (!recall.ok || !recall.data?.results)) {
      return { continue: true };
    }

    let activePlan: Awaited<ReturnType<typeof resolveActivePlanView>>;
    try {
      activePlan = await resolveActivePlanView(config.vaultPath);
    } catch {
      // ignore
    }

    const envelopeBody: Record<string, unknown> = {
      identity: {
        agent: config.agentId,
        workspace: workspaceId,
        task_signature: signature,
      },
      recall:
        recall.ok && recall.data
          ? formatRecall(prompt, recall.data, vaultResults)
          : { ok: false, error: recall.error },
      vault: vaultRecallToBody(vaultResults),
      intent: {
        action: intent.action,
        confidence: intent.confidence,
        suggested_tool: intent.suggestedTool,
        automatic_write: false,
      },
    };

    // Plan parity (audit C5): per-turn injection is a compact plan POINTER, not
    // the full plan — same budget discipline as the claude-code hook (Option C).
    if (activePlan !== undefined) {
      envelopeBody.active_plan_ref = compactPlanPointer(activePlan);
    }

    const envelope = wrapEnvelope({
      event: "UserPromptSubmit",
      agent: config.agentId,
      body: envelopeBody,
    });

    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_user_prompt_submit`,
      summary: prompt.slice(0, 120),
      details: {
        intent: intent.action,
        vault_matches: vaultResults.map((result) => result.relativePath),
        daemon_ok: recall.ok,
        task_signature: signature,
        workspace: workspaceId,
        automatic_write: false,
      },
    });

    return withHookContext("UserPromptSubmit", envelope);
  }

  async function handlePreCompact(payload: Record<string, unknown>): Promise<HookOutput> {
    await ensureVault(config.vaultPath);
    const tail = await auditTail(config.vaultPath, 60);
    const scarTissue = extractScarTissue(tail.entries);
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    const workspaceId = workspaceFor(payload);
    const transcript = asString(payload.trigger) || asString(payload.summary);

    // Agents WITH a precompactKind persist a durable inbox handoff; agents
    // without one (kilocode) carry the scar tissue in the envelope only.
    const inbox = config.precompactKind
      ? await writeInbox(config.vaultPath, sessionId, {
          kind: config.precompactKind,
          agent_id: config.agentId,
          workspace_id: workspaceId,
          scar_tissue: scarTissue,
          audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
          compaction_trigger: transcript || "compaction in progress",
          durable_learning_committed: false,
        })
      : undefined;

    const envelope = wrapEnvelope({
      event: "PreCompact",
      agent: config.agentId,
      body: {
        identity: {
          agent: config.agentId,
          workspace: workspaceId,
          session_id: sessionId,
        },
        scar_tissue: scarTissue,
        audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
        compaction_trigger: transcript || "compaction in progress",
        ...(inbox
          ? { inbox_path: inbox.filePath, durable_learning_committed: false }
          : {}),
      },
    });

    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_pre_compact`,
      summary: `pre-compact ${sessionId}`,
      details: {
        scar_count: scarTissue.length,
        trigger: transcript || "auto",
        workspace: workspaceId,
        ...(inbox ? { inbox_path: inbox.filePath } : {}),
      },
    });

    return withHookContext("PreCompact", envelope);
  }

  async function handleStop(payload: Record<string, unknown>): Promise<HookOutput> {
    await ensureVault(config.vaultPath);
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    const workspaceId = workspaceFor(payload);
    const lastTask = asString(payload.last_user_message) || asString(payload.summary) || sessionId;
    const tail = await auditTail(config.vaultPath, 30);
    const outcome = await prepareOutcomeFn({
      task: lastTask.slice(0, 200),
      summary: tail.entries.slice(-5).join("\n").slice(0, 600) || "session ended",
      profile: "compact",
      vaultPath: config.vaultPath,
    });

    const candidates = outcome.outcomeDraft.learnCandidates;
    // Nothing worth persisting: skip the inbox write and audit entry so we
    // don't litter the inbox with empty files or pad the audit log with noise
    // (unless this agent's config keeps the historical always-write behavior).
    if (!config.alwaysWriteStopInbox && candidates.length === 0) {
      return { continue: true };
    }

    const inbox = await writeInbox(config.vaultPath, sessionId, {
      kind: "stop_candidates",
      agent_id: config.agentId,
      workspace_id: workspaceId,
      candidates,
      log_only: outcome.outcomeDraft.logOnly,
      expires: outcome.outcomeDraft.expires,
      do_not_store: outcome.outcomeDraft.doNotStore,
      last_task: lastTask.slice(0, 200),
    });

    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_stop`,
      summary: `stop ${sessionId}`,
      details: {
        candidates: candidates.length,
        workspace: workspaceId,
        inbox_path: inbox.filePath,
      },
    });

    if (candidates.length === 0) {
      return { continue: true };
    }

    return {
      continue: true,
      systemMessage: `Minni: ${candidates.length} candidate learning${
        candidates.length === 1 ? "" : "s"
      } drafted to inbox (${inbox.filePath}). ${
        config.stopCommitHint ?? "Use minni_prepare_outcome/minni_learn to review and commit."
      }`,
    };
  }

  async function dispatch(event: string, payload: Record<string, unknown>): Promise<HookOutput> {
    switch (event) {
      case "SessionStart":
        return handleSessionStart(payload);
      case "UserPromptSubmit":
        return handleUserPromptSubmit(payload);
      case "PreCompact":
        return handlePreCompact(payload);
      case "Stop":
        return handleStop(payload);
      default:
        return { continue: true };
    }
  }

  return { handleSessionStart, handleUserPromptSubmit, handlePreCompact, handleStop, dispatch };
}

export async function runHookMain(config: AgentHookConfig): Promise<void> {
  if (!config.hooksEnabled) {
    emit({ continue: true });
    return;
  }

  const eventArg = process.argv[2];
  const payload = (await readStdin()) as Record<string, unknown>;
  const eventFromPayload = asString(payload.hook_event_name);
  const event = (eventArg || eventFromPayload || "").trim();
  if (!VALID_EVENTS.includes(event as EnvelopeEvent)) {
    emit({ continue: true });
    return;
  }

  try {
    const handlers = createHookHandlers(config);
    const output = await handlers.dispatch(event, payload);
    emit(output);
  } catch (error) {
    try {
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_error`,
        summary: `${event}: ${error instanceof Error ? error.message : String(error)}`,
      });
    } catch {
      // last-resort swallow
    }
    emit({ continue: true });
  }
}

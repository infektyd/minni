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
  buildRecallPointer,
  clearRecallState,
  extractStrongRecall,
  markRecallConsumed,
  readRecallState,
  recallPointerThreshold,
  writeRecallState,
} from "./recall-state.js";
import {
  PRE_TOOL_USE_EVENT,
  decideGuard,
  preToolUseAllow,
  preToolUseDeny,
  recallGuardMode as resolveRecallGuardModeFromEnv,
} from "./recall-guard.js";
import type {
  PreToolUseDecisionOutput,
  RecallGuardMode,
} from "./recall-guard.js";
import {
  BOOT_RECALL_LAYERS,
  buildStatusReport,
  extractIdentityBody,
  extractLearningsSection,
  truncateToTokenCharBudget,
  fetchStaleBeliefEvents,
  formatRecall,
  readAgentContext,
  recallMemory,
  stashPrecompactReassert,
  subscribeContradictions,
} from "./sovereign.js";
import { extractScarTissue, filterSafeVaultResults, prepareOutcome } from "./task.js";
import {
  auditTail,
  collectCorrectionsReassert,
  ensureVault,
  buildPendingLearningsSection,
  expireStaleInboxHandoffs,
  readInboxStatus,
  readReassertPending,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  settleReassertedInboxEntries,
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
   * The handoff payload also carries the hooks-PL-3 stale-belief stash
   * (`stale_belief_events`), written unconditionally — empty stashes are
   * consumed at the next boot. When omitted, PreCompact does NOT write an
   * inbox handoff file (kilocode's behavior — its envelope carries the scar
   * tissue directly) and instead stashes NON-EMPTY stale-belief events as a
   * dedicated `precompact_reassert` entry, mirroring the claude-code hook.
   */
  precompactKind?: string;
  /**
   * How SessionStart sources boot identity:
   * - "agent-context" (default; codex/grok): daemon readAgentContext layer1
   *   read, surfaced as `layer1_source` + `fallback_commands` and prefixed to
   *   the envelope as native layer1 text. The native layer already carries the
   *   recency-ordered "## Learnings" section, so the envelope intentionally
   *   omits `recent_learnings` (hooks-PL-2 deliberate asymmetry; the matrix
   *   test asserts both sides of this contract).
   * - "identity-recall" (kilocode): no daemon layer1 channel; the read context
   *   is trimmed to its Learnings slice and surfaced as `recent_learnings`.
   * Both modes issue the daemon `read` (the learning_reads writer that
   * stale_beliefs matches on) AND the widened BOOT_RECALL_LAYERS recall.
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
  /**
   * s6 PreToolUse recall-guard mode override. When set, it wins over the
   * MINNI_RECALL_GUARD_MODE env default ("off" | "soft" | "strict"). Omit to
   * resolve from the environment (default "soft").
   */
  recallGuardMode?: RecallGuardMode;
}

/** Test seam: lets behavioral tests drive the zero-candidate Stop branch. */
export interface AgentHookDeps {
  prepareOutcome?: typeof prepareOutcome;
}

export interface AgentHookHandlers {
  handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput>;
  handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput>;
  handlePreToolUse(payload: Record<string, unknown>): Promise<PreToolUseDecisionOutput>;
  handlePreCompact(payload: Record<string, unknown>): Promise<HookOutput>;
  handleStop(payload: Record<string, unknown>): Promise<HookOutput>;
  dispatch(
    event: string,
    payload: Record<string, unknown>,
  ): Promise<HookOutput | PreToolUseDecisionOutput>;
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
    const [status, tail, identityRead, recall, contradictions, inboxStatus] = await Promise.all([
      buildStatusReport({ vaultPath: config.vaultPath }),
      auditTail(config.vaultPath, 5),
      // hooks-PL-2 leg (a): BOTH boot modes issue the daemon 'read' — it is
      // the recency-ordered learning surface AND the path that records
      // learning_reads, which stale_beliefs matches on. agent-context boots
      // inject it whole as native layer 1; identity-recall boots trim it to
      // recent_learnings below.
      readAgentContext({ agentId: config.agentId, limit: 8 }),
      // recall-F1: boot recall must include the correction-bearing layers, not
      // just the identity shelf (the widened search is what lets knowledge-
      // layer corrections rank in). See BOOT_RECALL_LAYERS for the policy.
      recallMemory({
        query: `boot identity for ${workspaceId}`,
        layers: BOOT_RECALL_LAYERS,
        limit: 8,
        agentId: config.agentId,
        workspaceId,
      }),
      // hooks-PL-1/PL-2: corrections to beliefs this agent read must
      // re-surface at boot (stale_beliefs), on every platform.
      subscribeContradictions({ agentId: config.agentId }),
      readInboxStatus(config.vaultPath, 3),
    ]);
    const pending = inboxStatus.entries;
    const handoffContext = await resolveInboxHandoffContext(config.vaultPath, pending);
    // hooks-PL-3: re-assert corrections stashed by PreCompact, so the
    // post-compaction boot re-injects them even if the daemon is down now.
    // Consumed entries are settled (exactly-once re-injection, no unbounded
    // inbox growth); cap-overflowed tails are rewritten so they re-inject on
    // the next boot, and all-malformed entries survive for inspection.
    // I5: use the reassert-specific window so recent all-malformed files cannot
    // crowd valid corrections out of the newest-N slots.
    const reassertPending = await readReassertPending(config.vaultPath, 3);
    const { events: correctionsReassert, consumedPaths: reassertConsumed, deferredTails: reassertDeferred } =
      collectCorrectionsReassert(reassertPending);
    await settleReassertedInboxEntries(config.vaultPath, {
      consumedPaths: reassertConsumed,
      deferredTails: reassertDeferred,
    });

    // Plan parity (audit C5): SessionStart injects the FULL active-plan view for
    // boot/rehydration, exactly like the claude-code hook.
    let activePlan: Awaited<ReturnType<typeof resolveActivePlanView>>;
    try {
      activePlan = await resolveActivePlanView(config.vaultPath);
    } catch (error) {
      // hooks-PL-5: a failed plan resolution must not silently boot plan-less.
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_active_plan_error`,
        summary: `SessionStart: ${error instanceof Error ? error.message : String(error)}`,
      }).catch(() => {});
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
      // hooks-PL-1: discriminated stale-belief payload (matched /
      // checked_no_match from the daemon; explicit status:"error" here so
      // events:[] can never masquerade as "checked and clean").
      stale_beliefs:
        contradictions.ok && contradictions.data
          ? contradictions.data
          : { ok: false, status: "error", error: contradictions.error },
      recall:
        recall.ok && recall.data
          ? {
              ok: true,
              results: recall.data.results,
              agent_origin: recall.data.agent_id ?? config.agentId,
              layer: recall.data.layer,
              layers: BOOT_RECALL_LAYERS,
            }
          : { ok: false, error: recall.error },
      audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]),
    };

    if (correctionsReassert.length > 0) {
      envelopeBody.corrections_reassert = correctionsReassert;
    }

    if (bootIdentity === "agent-context") {
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
      // hooks-PL-2 (deliberate asymmetry with hook.ts/kilocode): NO
      // recent_learnings envelope field here. agent-context boots inject the
      // FULL daemon read context as native Layer 1 below — including its
      // recency-ordered "## Learnings" section — so a trimmed duplicate
      // inside the envelope would be pure redundancy. The matrix test asserts
      // both sides of this contract (recent_learnings === undefined AND the
      // Learnings section present in the native layer).
    } else {
      envelopeBody.recent_learnings =
        identityRead.ok && identityRead.data?.context
          ? {
              ok: true,
              context:
                extractLearningsSection(identityRead.data.context) ??
                "No recent learnings.",
            }
          : { ok: false, error: identityRead.error };
    }

    if (activePlan !== undefined) {
      envelopeBody.active_plan = activePlan;
    }

    const budget = envelopeBudgetFor(config.contextWindow);
    if (identityRead.ok && identityRead.data?.context) {
      const identityBody = extractIdentityBody(identityRead.data.context, config.agentId);
      if (identityBody) {
        envelopeBody.identity_body = truncateToTokenCharBudget(
          identityBody,
          Math.max(budget - 500, 0),
        );
      }
    }

    const envelope = wrapEnvelope({
      event: "SessionStart",
      agent: config.agentId,
      budget,
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
        corrections_reassert: correctionsReassert.length,
        reassert_entries_cleared: reassertConsumed.length,
        reassert_tails_deferred: reassertDeferred.length,
      },
    });

    const nativeLayer1 =
      bootIdentity === "agent-context" && identityRead.ok && identityRead.data?.context
        ? truncateToTokenCharBudget(identityRead.data.context.trim(), budget)
        : "";
    return withHookContext("SessionStart", [nativeLayer1, envelope].filter(Boolean).join("\n\n"));
  }

  async function handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput> {
    const prompt = asString(payload.prompt) || asString(payload.user_prompt);
    if (!prompt.trim()) {
      return { continue: true };
    }

    const workspaceId = workspaceFor(payload);
    const signature = hashTaskSignature(prompt);

    const intent = routeMemoryIntent(prompt);
    // Explicit WRITE intents (learn/vault_write carry automaticAllowed:false) are
    // the user dictating memory, not asking the agent to recall — inject no
    // pointer and write no state. (s5 parity with the claude-code hook.)
    if (!intent.automaticAllowed) {
      // Clear any stale strong state from a previous turn BEFORE returning: an
      // unconsumed pointer must not leak into this write-intent turn and let the
      // s6 guard deny an unrelated read/search here (parity with the weak-turn
      // path below, which also clears).
      await clearRecallState(config.vaultPath).catch(() => {});
      return { continue: true };
    }

    const threshold = recallPointerThreshold();
    const [vaultResults, recall] = await Promise.all([
      searchVaultNotes(config.vaultPath, prompt, 6),
      recallMemory({
        query: prompt,
        limit: 6,
        agentId: config.agentId,
        workspaceId,
      }),
    ]);
    // s5 strength gate: emit the light pointer + recall-state file ONLY when the
    // top recall strength clears the threshold; otherwise inject nothing and
    // clear any stale state left by a previous strong turn.
    const strong = extractStrongRecall(
      recall.ok ? recall.data : undefined,
      vaultResults,
      threshold,
    );
    let recallStateFile: string | undefined;
    if (strong) {
      try {
        recallStateFile = await writeRecallState(config.vaultPath, {
          task_signature: signature,
          intent: intent.action,
          top_hits: strong.topHits,
          top_score: strong.topScore,
        });
      } catch {
        // best-effort: a state-write failure must not break the hook
      }
    } else {
      await clearRecallState(config.vaultPath).catch(() => {});
    }

    let activePlan: Awaited<ReturnType<typeof resolveActivePlanView>>;
    try {
      activePlan = await resolveActivePlanView(config.vaultPath);
    } catch (error) {
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_active_plan_error`,
        summary: `UserPromptSubmit: ${error instanceof Error ? error.message : String(error)}`,
      }).catch(() => {});
    }

    const planRef = activePlan !== undefined ? compactPlanPointer(activePlan) : undefined;

    // Nothing salient to inject this turn: no strong recall AND no active plan.
    if (!strong && planRef === undefined) {
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_user_prompt_submit`,
        summary: prompt.slice(0, 120),
        details: {
          intent: intent.action,
          vault_matches: vaultResults.map((result) => result.relativePath),
          daemon_ok: recall.ok,
          task_signature: signature,
          workspace: workspaceId,
          recall_strong: false,
        },
      });
      return { continue: true };
    }

    const envelopeBody: Record<string, unknown> = {
      identity: {
        agent: config.agentId,
        workspace: workspaceId,
        task_signature: signature,
      },
    };
    if (strong) {
      // LIGHT POINTER, not the full pack: the full top hits live in the portable
      // recall-state file (read by the s6 guard); the prompt only gets a signpost.
      envelopeBody.recall_pointer = buildRecallPointer(strong);
      envelopeBody.recall_state = recallStateFile;
    }

    // Plan parity (audit C5): per-turn injection is a compact plan POINTER, not
    // the full plan — same budget discipline as the claude-code hook (Option C).
    // (planRef !== undefined iff activePlan !== undefined; guard on activePlan so
    // the compiler narrows it for compactPlanPointer.)
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
        recall_strong: Boolean(strong),
      },
    });

    return withHookContext("UserPromptSubmit", envelope);
  }

  // s6 PreToolUse recall guard (BACKSTOP). Same logic as the claude-code hook's
  // handlePreToolUse, against this agent's vault. The output is the
  // permissionDecision shape (deny-to-surface), NOT an envelope.
  async function handlePreToolUse(
    payload: Record<string, unknown>,
  ): Promise<PreToolUseDecisionOutput> {
    const mode = config.recallGuardMode ?? resolveRecallGuardModeFromEnv();
    if (mode === "off") return preToolUseAllow();

    const toolName = asString(payload.tool_name);
    if (!toolName) return preToolUseAllow();
    const toolInput =
      payload.tool_input && typeof payload.tool_input === "object"
        ? (payload.tool_input as Record<string, unknown>)
        : {};

    const state = await readRecallState(config.vaultPath).catch(() => null);
    const threshold = recallPointerThreshold();
    const verdict = decideGuard({ state, mode, threshold, toolName, toolInput });
    if (verdict === "allow") return preToolUseAllow();

    // DENY surfaces the recall ONCE: flip consumed=true FIRST so the re-issued
    // call (and every other tool call this turn) passes. PR90-2: only deny if
    // that flag actually persisted — if the write failed, denying would loop the
    // WHOLE turn (every re-issued call re-reads consumed=false and is denied
    // again). On a persistence failure we FAIL OPEN and allow, trading a missed
    // nudge for availability.
    const consumed = await markRecallConsumed(config.vaultPath).catch(() => false);
    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_pretooluse_guard`,
      summary: `recall guard ${consumed ? "denied" : "allowed (consume write failed)"} ${toolName} (mode=${mode})`,
      details: {
        tool: toolName,
        mode,
        consumed,
        top_score: state!.top_score,
        hits: state!.top_hits.length,
        task_signature: state!.task_signature,
      },
    }).catch(() => {});
    if (!consumed) return preToolUseAllow();
    return preToolUseDeny(state!);
  }

  async function handlePreCompact(payload: Record<string, unknown>): Promise<HookOutput> {
    await ensureVault(config.vaultPath);
    const tail = await auditTail(config.vaultPath, 60);
    const scarTissue = extractScarTissue(tail.entries);
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    const workspaceId = workspaceFor(payload);
    const transcript = asString(payload.trigger) || asString(payload.summary);

    // hooks-PL-3: compaction is exactly when a correction the agent already
    // saw can fall out of context. Stash the current stale-belief /
    // contradiction events durably in the inbox so the post-compaction boot
    // re-asserts them (corrections_reassert) even if the daemon is down at
    // next boot.
    const { ok: staleBeliefsOk, events: staleBeliefEvents } =
      await fetchStaleBeliefEvents(config.agentId);

    // Agents WITH a precompactKind persist a durable inbox handoff (which
    // carries the stale-belief stash); agents without one (kilocode) carry
    // the scar tissue in the envelope only and stash non-empty stale-belief
    // events as a dedicated precompact_reassert entry, like the CC hook.
    const inbox = config.precompactKind
      ? await writeInbox(config.vaultPath, sessionId, {
          kind: config.precompactKind,
          agent_id: config.agentId,
          workspace_id: workspaceId,
          scar_tissue: scarTissue,
          stale_belief_events: staleBeliefEvents,
          audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
          compaction_trigger: transcript || "compaction in progress",
          durable_learning_committed: false,
        })
      : undefined;
    const reassertInboxPath = config.precompactKind
      ? undefined
      : await stashPrecompactReassert({
          vaultPath: config.vaultPath,
          sessionId,
          agentId: config.agentId,
          staleBeliefEvents,
          trigger: transcript,
        });

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
        stale_belief_events: staleBeliefEvents.length,
        stale_beliefs_ok: staleBeliefsOk,
        ...(inbox ? { inbox_path: inbox.filePath } : {}),
        ...(reassertInboxPath ? { reassert_inbox_path: reassertInboxPath } : {}),
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

  async function dispatch(
    event: string,
    payload: Record<string, unknown>,
  ): Promise<HookOutput | PreToolUseDecisionOutput> {
    switch (event) {
      case "SessionStart":
        return handleSessionStart(payload);
      case "UserPromptSubmit":
        return handleUserPromptSubmit(payload);
      case PRE_TOOL_USE_EVENT:
        return handlePreToolUse(payload);
      case "PreCompact":
        return handlePreCompact(payload);
      case "Stop":
        return handleStop(payload);
      default:
        return { continue: true };
    }
  }

  return {
    handleSessionStart,
    handleUserPromptSubmit,
    handlePreCompact,
    handleStop,
    handlePreToolUse,
    dispatch,
  };
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
  // PreToolUse is dispatched here too but is NOT an EnvelopeEvent (its output is
  // the permissionDecision shape), so it is gated alongside VALID_EVENTS.
  if (event !== PRE_TOOL_USE_EVENT && !VALID_EVENTS.includes(event as EnvelopeEvent)) {
    emit({ continue: true });
    return;
  }

  try {
    const handlers = createHookHandlers(config);
    const output = await handlers.dispatch(event, payload);
    emit(output);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    try {
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_error`,
        summary: `${event}: ${message}`,
      });
    } catch {
      // audit unavailable; the systemMessage below still surfaces the failure
    }
    // hooks-PL-5: a degraded boot must never look like a clean one — say so.
    emit({
      continue: true,
      systemMessage: `Minni hook degraded (${event}): ${message} — memory injection skipped this event; see vault log.md.`,
    });
  }
}

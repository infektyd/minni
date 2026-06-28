import {
  CLAUDECODE_AGENT_ID,
  CLAUDECODE_CONTEXT_WINDOW,
  CLAUDECODE_HOOKS_ENABLED,
  CLAUDECODE_VAULT_PATH,
  CLAUDECODE_WORKSPACE_ID,
} from "./config.js";
import { compactPlanPointer, resolveActivePlanView } from "./plan.js";
import {
  MEMORY_CONTRACT,
  MINNI_LIFECYCLE_LINE,
  buildLifecycleEmphasis,
  envelopeBudgetFor,
  hashTaskSignature,
  lifecycleNudgeMode,
  lifecycleSurfaceForIntent,
  wrapEnvelope,
} from "./agent_envelope.js";
import type { EnvelopeEvent, LifecycleSurface } from "./agent_envelope.js";
import {
  asString,
  emit,
  readStdin,
  VALID_EVENTS,
} from "./hook-utils.js";
import type { HookOutput } from "./hook-utils.js";
import { routeMemoryIntent } from "./policy.js";
import {
  buildRecallPointer,
  clearRecallState,
  extractStrongRecall,
  markRecallConsumed,
  readLifecycleState,
  readRecallState,
  recallPointerThreshold,
  writeLifecycleState,
  writeRecallState,
} from "./recall-state.js";
import {
  PRE_TOOL_USE_EVENT,
  decideGuard,
  preToolUseAllow,
  preToolUseDeny,
  recallGuardMode,
} from "./recall-guard.js";
import type { PreToolUseDecisionOutput } from "./recall-guard.js";
import {
  ackHandoff,
  BOOT_RECALL_LAYERS,
  buildStatusReport,
  extractIdentityBody,
  extractLearningsSection,
  truncateToTokenCharBudget,
  fetchStaleBeliefEvents,
  listPendingHandoffs,
  readAgentContext,
  recallMemory,
  stashPrecompactReassert,
  subscribeContradictions,
} from "./sovereign.js";
import { classifyIntent, extractScarTissue, filterSafeVaultResults, prepareOutcome } from "./task.js";
import {
  auditTail,
  collectCorrectionsReassert,
  ensureVault,
  buildPendingLearningsSection,
  expireStaleInboxHandoffs,
  readInboxStatus,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  settleReassertedInboxEntries,
  writeInbox,
} from "./vault.js";

async function handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput> {
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  await ensureVault(CLAUDECODE_VAULT_PATH);
  // c4: reset the once-per-session lifecycle emphasis on a fresh session, so the
  // situational focus can fire again this session.
  await writeLifecycleState(CLAUDECODE_VAULT_PATH, {
    session_id: sessionId,
    emphasized: [],
  }).catch(() => {});
  const status = await buildStatusReport({ vaultPath: CLAUDECODE_VAULT_PATH });
  const tail = await auditTail(CLAUDECODE_VAULT_PATH, 5);
  // recall-F1: boot recall previously whitelisted layers=['identity'], which
  // dropped knowledge-layer corrections before rerank. Widen to the
  // correction-bearing layers (single query; the engine's correction salience
  // floor ranks fresh corrections above saturated habitual hits) instead of a
  // second corrections-only round-trip. See BOOT_RECALL_LAYERS for the policy.
  const recall = await recallMemory({
    query: `boot identity for ${CLAUDECODE_WORKSPACE_ID}`,
    layers: BOOT_RECALL_LAYERS,
    limit: 8,
    agentId: CLAUDECODE_AGENT_ID,
    workspaceId: CLAUDECODE_WORKSPACE_ID,
  });
  // hooks-PL-2 leg (a): the 'read' RPC is the recency-ordered learning surface
  // AND the daemon path that records learning_reads — without it, corrections
  // to beliefs this agent saw can never match in stale_beliefs.
  const recentLearnings = await readAgentContext({ agentId: CLAUDECODE_AGENT_ID, limit: 8 });
  // TTL-reap stale file handoffs BEFORE the honest read so they neither occupy
  // the capped slice nor inflate totals; they surface once below as 'expired'.
  const expiredHandoffs = await expireStaleInboxHandoffs(CLAUDECODE_VAULT_PATH);
  const inboxStatus = await readInboxStatus(CLAUDECODE_VAULT_PATH, 3);
  const pending = inboxStatus.entries;
  const handoffContext = await resolveInboxHandoffContext(CLAUDECODE_VAULT_PATH, pending);
  const pendingHandoffs = await listPendingHandoffs({ agentId: CLAUDECODE_AGENT_ID });
  const pendingHandoffData = pendingHandoffs.ok && pendingHandoffs.data
    ? pendingHandoffs.data as { handoffs?: Array<{ lease_id?: string; leaseId?: string }> }
    : { handoffs: [] };
  const ackedLeases: string[] = [];
  for (const handoff of pendingHandoffData.handoffs ?? []) {
    const leaseId = handoff.lease_id ?? handoff.leaseId;
    if (!leaseId) continue;
    const ack = await ackHandoff({ leaseId, status: "accepted", agentId: CLAUDECODE_AGENT_ID });
    if (ack.ok) ackedLeases.push(leaseId);
  }
  const contradictions = await subscribeContradictions({ agentId: CLAUDECODE_AGENT_ID });

  let activePlan: any = undefined;
  try {
    activePlan = await resolveActivePlanView(CLAUDECODE_VAULT_PATH);
  } catch (error) {
    // hooks-PL-5: a failed plan resolution must not silently boot plan-less.
    await recordAudit(CLAUDECODE_VAULT_PATH, {
      tool: "hook_active_plan_error",
      summary: `SessionStart: ${error instanceof Error ? error.message : String(error)}`,
    }).catch(() => {});
  }

  // hooks-PL-3: re-assert corrections stashed by PreCompact, so the
  // post-compaction boot re-injects them even if the daemon is down now.
  // Only fully-consumed entries are cleared (exactly-once re-injection, no
  // unbounded inbox growth); cap-overflowed tails are rewritten for the next
  // boot, and all-malformed entries survive for inspection.
  const { events: correctionsReassert, consumedPaths: reassertConsumed, deferredTails: reassertDeferred } =
    collectCorrectionsReassert(pending);
  await settleReassertedInboxEntries({
    consumedPaths: reassertConsumed,
    deferredTails: reassertDeferred,
  });

  const envelopeBody: any = {
    contract: MEMORY_CONTRACT,
    identity: {
      agent: CLAUDECODE_AGENT_ID,
      workspace: CLAUDECODE_WORKSPACE_ID,
      vault: CLAUDECODE_VAULT_PATH,
      session_id: sessionId,
      daemon_ok: status.socket.ok,
      afm_ok: status.afm.ok,
    },
    pending_learnings: buildPendingLearningsSection(inboxStatus, expiredHandoffs),
    handoff_context: handoffContext.map((snippet) => ({
      ref: snippet.ref,
      path: snippet.relativePath,
      snippet: snippet.snippet,
    })),
    handoff_acks: ackedLeases,
    // hooks-PL-1: pass the daemon's checked/matched discriminator through;
    // the error branch is explicitly status:"error" so events:[] can never
    // masquerade as "checked and clean".
    stale_beliefs:
      contradictions.ok && contradictions.data
        ? contradictions.data
        : { ok: false, status: "error", error: contradictions.error },
    recall:
      recall.ok && recall.data
        ? {
            ok: true,
            results: recall.data.results,
            agent_origin: recall.data.agent_id ?? CLAUDECODE_AGENT_ID,
            layer: recall.data.layer,
            layers: BOOT_RECALL_LAYERS,
          }
        : { ok: false, error: recall.error },
    recent_learnings:
      recentLearnings.ok && recentLearnings.data?.context
        ? {
            ok: true,
            context:
              extractLearningsSection(recentLearnings.data.context) ??
              "No recent learnings.",
          }
        : { ok: false, error: recentLearnings.error },
    audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]),
  };

  // c2/c5 (claude-code only): the standing 4-surface lifecycle line at boot so the
  // agent sees the spine from session start (unless the feature is off).
  // hook-handlers.ts is NOT touched.
  if (lifecycleNudgeMode() !== "off") {
    envelopeBody.lifecycle = MINNI_LIFECYCLE_LINE;
  }

  if (correctionsReassert.length > 0) {
    envelopeBody.corrections_reassert = correctionsReassert;
  }

  if (activePlan !== undefined) {
    envelopeBody.active_plan = activePlan;
  }

  const budget = envelopeBudgetFor(CLAUDECODE_CONTEXT_WINDOW);
  if (recentLearnings.ok && recentLearnings.data?.context) {
    const identityBody = extractIdentityBody(recentLearnings.data.context);
    if (identityBody) {
      envelopeBody.identity_body = truncateToTokenCharBudget(
        identityBody,
        Math.max(budget - 500, 0),
      );
    }
  }

  const envelope = wrapEnvelope({
    event: "SessionStart",
    agent: CLAUDECODE_AGENT_ID,
    budget,
    body: envelopeBody,
  });

  await recordAudit(CLAUDECODE_VAULT_PATH, {
    tool: "hook_session_start",
    summary: `boot ${sessionId}`,
    details: {
      daemon_ok: status.socket.ok,
      afm_ok: status.afm.ok,
      pending_inbox: inboxStatus.totalPending,
      expired_handoffs: expiredHandoffs.length,
      handoff_context: handoffContext.length,
      corrections_reassert: correctionsReassert.length,
      reassert_entries_cleared: reassertConsumed.length,
      reassert_tails_deferred: reassertDeferred.length,
    },
  });

  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: envelope,
    },
  };
}

/**
 * c4/c5 (claude-code only): build the lifecycle representation fields for this
 * turn. The PERSISTENT line (c3) is always present in "soft" mode so the 4
 * surfaces stay in view; the situational `lifecycle_focus` (c4) is added when the
 * prompt's ambition intent (task.ts `classifyIntent`) maps to a surface NOT yet
 * emphasized this session. "off" mode (c5) returns {} — fully silent. The caller
 * must persist any returned `emphasizedSurface` so the focus fires at most once
 * per surface per session. Representation only — never a permission decision.
 */
function lifecycleFieldsFor(
  prompt: string,
  emphasized: Set<string>,
): {
  fields: { lifecycle?: string; lifecycle_focus?: string };
  emphasizedSurface?: LifecycleSurface;
} {
  if (lifecycleNudgeMode() === "off") return { fields: {} };
  const fields: { lifecycle?: string; lifecycle_focus?: string } = {
    lifecycle: MINNI_LIFECYCLE_LINE,
  };
  const surface = lifecycleSurfaceForIntent(classifyIntent(prompt));
  if (surface && !emphasized.has(surface)) {
    fields.lifecycle_focus = buildLifecycleEmphasis(surface);
    return { fields, emphasizedSurface: surface };
  }
  return { fields };
}

/**
 * c3/c4: emit an envelope carrying the lifecycle representation only. Used at the
 * two early-returns of handleUserPromptSubmit (write-intent and nothing-salient
 * gates) so the surfaces survive turns that previously injected nothing. When the
 * feature is off (c5) `fields` is empty and this degrades to the original no-op.
 */
function lifecycleOnlyOutput(
  signature: string,
  fields: { lifecycle?: string; lifecycle_focus?: string },
): HookOutput {
  if (fields.lifecycle === undefined && fields.lifecycle_focus === undefined) {
    return { continue: true };
  }
  const envelope = wrapEnvelope({
    event: "UserPromptSubmit",
    agent: CLAUDECODE_AGENT_ID,
    body: {
      identity: {
        agent: CLAUDECODE_AGENT_ID,
        workspace: CLAUDECODE_WORKSPACE_ID,
        task_signature: signature,
      },
      ...fields,
    },
  });
  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext: envelope,
    },
  };
}

async function handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput> {
  const prompt = asString(payload.prompt) || asString(payload.user_prompt);
  if (!prompt.trim()) {
    return { continue: true };
  }
  const signature = hashTaskSignature(prompt);

  // c4/c5: compute the lifecycle representation once for this turn, BEFORE the
  // early-return gates, so the persistent line survives them. Read the
  // once-per-session emphasis set and persist any newly-emphasized surface.
  const lifecycleStatePrev = await readLifecycleState(CLAUDECODE_VAULT_PATH);
  const emphasizedSurfaces = new Set(lifecycleStatePrev?.emphasized ?? []);
  const { fields: lifecycleFields, emphasizedSurface } = lifecycleFieldsFor(
    prompt,
    emphasizedSurfaces,
  );
  if (emphasizedSurface) {
    emphasizedSurfaces.add(emphasizedSurface);
    await writeLifecycleState(CLAUDECODE_VAULT_PATH, {
      session_id: lifecycleStatePrev?.session_id ?? "session",
      emphasized: [...emphasizedSurfaces],
    }).catch(() => {});
  }

  const intent = routeMemoryIntent(prompt);
  // Keep the existing suppression of auto-recall on explicit WRITE intents
  // (learn/vault_write are the only intents with automaticAllowed:false): on
  // those turns the user is dictating memory, not asking the agent to recall, so
  // we inject no pointer and write no state. The historical guard
  // `intent.action === "none" && !intent.automaticAllowed` was dead — "none"
  // always carries automaticAllowed:true — and is replaced by the recall
  // STRENGTH gate below (NOT keyword classification). Substantive 'none'-intent
  // turns still run recall and get a pointer iff the hits are strong.
  if (!intent.automaticAllowed) {
    // Clear any stale strong state from a previous turn BEFORE returning: an
    // unconsumed pointer must not leak into this write-intent turn and let the
    // s6 guard deny an unrelated read/search here (parity with the weak-turn
    // path below, which also clears).
    await clearRecallState(CLAUDECODE_VAULT_PATH).catch(() => {});
    // c3: persistent lifecycle visibility must survive this write-intent
    // early-return — the agent still sees the 4 surfaces on learn/vault_write turns.
    return lifecycleOnlyOutput(signature, lifecycleFields);
  }
  const threshold = recallPointerThreshold();
  const [vaultResultsRaw, recall] = await Promise.all([
    searchVaultNotes(CLAUDECODE_VAULT_PATH, prompt, 6),
    recallMemory({
      query: prompt,
      limit: 6,
      agentId: CLAUDECODE_AGENT_ID,
      workspaceId: CLAUDECODE_WORKSPACE_ID,
    }),
  ]);
  const vaultResults = filterSafeVaultResults(vaultResultsRaw);

  // s5 strength gate: emit the light pointer + recall-state file ONLY when the
  // top recall strength clears the threshold; otherwise inject nothing and clear
  // any stale state from a previous strong turn.
  const strong = extractStrongRecall(
    recall.ok ? recall.data : undefined,
    vaultResults,
    threshold,
  );
  let recallStateFile: string | undefined;
  if (strong) {
    try {
      recallStateFile = await writeRecallState(CLAUDECODE_VAULT_PATH, {
        task_signature: signature,
        intent: intent.action,
        top_hits: strong.topHits,
        top_score: strong.topScore,
      });
    } catch {
      // best-effort: a state-write failure must not break the hook
    }
  } else {
    await clearRecallState(CLAUDECODE_VAULT_PATH).catch(() => {});
  }

  let activePlan: any = undefined;
  try {
    activePlan = await resolveActivePlanView(CLAUDECODE_VAULT_PATH);
  } catch (error) {
    // hooks-PL-5: surface plan-resolution failures instead of silently
    // continuing without the active plan pointer.
    await recordAudit(CLAUDECODE_VAULT_PATH, {
      tool: "hook_active_plan_error",
      summary: `UserPromptSubmit: ${error instanceof Error ? error.message : String(error)}`,
    }).catch(() => {});
  }

  let active_plan_ref: ReturnType<typeof compactPlanPointer> | undefined;
  if (activePlan !== undefined) {
    active_plan_ref = compactPlanPointer(activePlan);
  }

  // Nothing salient to inject this turn: no strong recall AND no active plan.
  if (!strong && active_plan_ref === undefined) {
    await recordAudit(CLAUDECODE_VAULT_PATH, {
      tool: "hook_user_prompt_submit",
      summary: prompt.slice(0, 120),
      details: {
        intent: intent.action,
        vault_matches: vaultResults.map((result) => result.relativePath),
        daemon_ok: recall.ok,
        task_signature: signature,
        recall_strong: false,
      },
    });
    // c3: even with nothing salient (no strong recall, no active plan) the
    // persistent lifecycle line still rides this turn's envelope.
    return lifecycleOnlyOutput(signature, lifecycleFields);
  }

  const envelopeBody: any = {
    // c3/c4: persistent lifecycle line (+ situational focus) rides the salient turn too.
    ...lifecycleFields,
    identity: {
      agent: CLAUDECODE_AGENT_ID,
      workspace: CLAUDECODE_WORKSPACE_ID,
      task_signature: signature,
    },
  };
  if (strong) {
    // LIGHT POINTER, not the full pack: the full top hits live in the portable
    // recall-state file (read by the s6 guard); the prompt only gets a signpost.
    envelopeBody.recall_pointer = buildRecallPointer(strong);
    envelopeBody.recall_state = recallStateFile;
  }

  // Option C: inject a compact plan POINTER per turn, not the full plan. The
  // headline + next_action are the actionable one-liners the agent needs every
  // turn; the full goal/open_questions/pending list is omitted (it barely changes
  // turn-to-turn) and pulled on demand via minni_plan_status. SessionStart still
  // injects the full plan view for boot/rehydration.
  if (activePlan !== undefined) {
    // Plan parity (audit C5): inline the compact-pointer call so all four hooks
    // share the same wire shape.
    envelopeBody.active_plan_ref = compactPlanPointer(activePlan);
  }

  const envelope = wrapEnvelope({
    event: "UserPromptSubmit",
    agent: CLAUDECODE_AGENT_ID,
    body: envelopeBody,
  });

  await recordAudit(CLAUDECODE_VAULT_PATH, {
    tool: "hook_user_prompt_submit",
    summary: prompt.slice(0, 120),
    details: {
      intent: intent.action,
      vault_matches: vaultResults.map((result) => result.relativePath),
      daemon_ok: recall.ok,
      task_signature: signature,
      recall_strong: Boolean(strong),
      top_score: strong?.topScore,
    },
  });

  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext: envelope,
    },
  };
}

async function handlePreCompact(payload: Record<string, unknown>): Promise<HookOutput> {
  await ensureVault(CLAUDECODE_VAULT_PATH);
  const tail = await auditTail(CLAUDECODE_VAULT_PATH, 60);
  const scarTissue = extractScarTissue(tail.entries);
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  const transcript = asString(payload.trigger) || asString(payload.summary);

  // NOTE: Claude Code's PreCompact hook does NOT support hookSpecificOutput /
  // additionalContext injection (only SessionStart, UserPromptSubmit, PostToolUse,
  // PostToolBatch do). Emitting that shape here fails schema validation with
  // "(root): Invalid input". Continuity across compaction is instead carried by the
  // post-compaction SessionStart hook, which rebuilds audit_tail + recall +
  // pending_learnings from the vault.
  //
  // hooks-PL-3: compaction is exactly when a correction the agent already saw
  // can fall out of context. Stash the current stale-belief/contradiction
  // events durably in the inbox so the post-compaction SessionStart re-asserts
  // them (corrections_reassert) even if the daemon is down at next boot.
  const { ok: staleBeliefsOk, events: staleBeliefEvents } =
    await fetchStaleBeliefEvents(CLAUDECODE_AGENT_ID);
  const reassertInboxPath = await stashPrecompactReassert({
    vaultPath: CLAUDECODE_VAULT_PATH,
    sessionId,
    agentId: CLAUDECODE_AGENT_ID,
    staleBeliefEvents,
    trigger: transcript,
  });

  await recordAudit(CLAUDECODE_VAULT_PATH, {
    tool: "hook_pre_compact",
    summary: `pre-compact ${sessionId}`,
    details: {
      scar_count: scarTissue.length,
      trigger: transcript || "auto",
      stale_belief_events: staleBeliefEvents.length,
      stale_beliefs_ok: staleBeliefsOk,
      ...(reassertInboxPath ? { reassert_inbox_path: reassertInboxPath } : {}),
    },
  });

  return { continue: true };
}

async function handleStop(payload: Record<string, unknown>): Promise<HookOutput> {
  await ensureVault(CLAUDECODE_VAULT_PATH);
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  const lastTask = asString(payload.last_user_message) || asString(payload.summary) || sessionId;
  const tail = await auditTail(CLAUDECODE_VAULT_PATH, 30);
  const outcome = await prepareOutcome({
    task: lastTask.slice(0, 200),
    summary: tail.entries.slice(-5).join("\n").slice(0, 600) || "session ended",
    profile: "compact",
    vaultPath: CLAUDECODE_VAULT_PATH,
  });

  const inbox = await writeInbox(CLAUDECODE_VAULT_PATH, sessionId, {
    candidates: outcome.outcomeDraft.learnCandidates,
    log_only: outcome.outcomeDraft.logOnly,
    expires: outcome.outcomeDraft.expires,
    do_not_store: outcome.outcomeDraft.doNotStore,
    last_task: lastTask.slice(0, 200),
  });

  await recordAudit(CLAUDECODE_VAULT_PATH, {
    tool: "hook_stop",
    summary: `stop ${sessionId}`,
    details: {
      candidates: outcome.outcomeDraft.learnCandidates.length,
      inbox_path: inbox.filePath,
    },
  });

  if (outcome.outcomeDraft.learnCandidates.length === 0) {
    return { continue: true };
  }

  return {
    continue: true,
    systemMessage: `Minni: ${outcome.outcomeDraft.learnCandidates.length} candidate learning${
      outcome.outcomeDraft.learnCandidates.length === 1 ? "" : "s"
    } drafted to inbox (${inbox.filePath}). Use /minni:learn to commit.`,
  };
}

// s6 PreToolUse recall guard (BACKSTOP). claude-code is NOT special: same logic
// as the shared factory's handlePreToolUse, against the claude-code vault. The
// output is the permissionDecision shape (deny-to-surface), NOT an envelope.
async function handlePreToolUse(
  payload: Record<string, unknown>,
): Promise<PreToolUseDecisionOutput> {
  const mode = recallGuardMode();
  if (mode === "off") return preToolUseAllow();

  const toolName = asString(payload.tool_name);
  if (!toolName) return preToolUseAllow();
  const toolInput =
    payload.tool_input && typeof payload.tool_input === "object"
      ? (payload.tool_input as Record<string, unknown>)
      : {};

  const state = await readRecallState(CLAUDECODE_VAULT_PATH).catch(() => null);
  const threshold = recallPointerThreshold();
  const verdict = decideGuard({ state, mode, threshold, toolName, toolInput });
  if (verdict === "allow") return preToolUseAllow();

  // DENY surfaces the recall ONCE: flip consumed=true FIRST so the re-issued
  // call (and every other tool call this turn) passes. PR90-2: only deny if that
  // flag actually persisted — if the write failed, denying would loop the WHOLE
  // turn (every re-issued call re-reads consumed=false and is denied again). On
  // a persistence failure we FAIL OPEN and allow, trading a missed nudge for
  // availability.
  const consumed = await markRecallConsumed(CLAUDECODE_VAULT_PATH).catch(() => false);
  await recordAudit(CLAUDECODE_VAULT_PATH, {
    tool: "hook_pretooluse_guard",
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

async function dispatch(
  event: string,
  payload: Record<string, unknown>,
): Promise<HookOutput | PreToolUseDecisionOutput> {
  switch (event) {
    case "SessionStart":
      return handleSessionStart(payload);
    case "UserPromptSubmit":
      return handleUserPromptSubmit(payload);
    case "PreCompact":
      return handlePreCompact(payload);
    case "Stop":
      return handleStop(payload);
    case PRE_TOOL_USE_EVENT:
      return handlePreToolUse(payload);
    default:
      return { continue: true };
  }
}

async function main(): Promise<void> {
  if (!CLAUDECODE_HOOKS_ENABLED) {
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
    const output = await dispatch(event, payload);
    emit(output);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    try {
      await recordAudit(CLAUDECODE_VAULT_PATH, {
        tool: "hook_error",
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

void main();

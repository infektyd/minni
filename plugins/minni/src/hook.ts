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
  effectiveHookBudgetMs,
  emit,
  hookBudgetMs,
  readStdin,
  VALID_EVENTS,
  withBudget,
  workspaceFromPayload,
} from "./hook-utils.js";
import {
  deferUntilDelivered,
  discardDeliveryCommits,
  emitAndCommit,
  exitAfterDelivery,
  failAndExit,
} from "./hook-delivery.js";
import { harvestCompactSummary, harvestSummaryText } from "./compact-harvest.js";
import { claudeCodeWire } from "./hook-platform.js";
import type { HookOutput } from "./hook-utils.js";
import { handleStopCore, recordUnroutedEvent } from "./hook-handlers.js";
import type { StopCoreConfig } from "./hook-handlers.js";
import { routeMemoryIntent } from "./policy.js";
import {
  buildRecallPointer,
  buildStaleRecallPointer,
  clearRecallState,
  extractStrongRecall,
  markRecallConsumed,
  readLifecycleState,
  readRecallState,
  recallPointerThreshold,
  recallStatePath,
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
  buildBootRecallSlice,
  buildStatusReport,
  extractIdentityBody,
  extractLearningsSection,
  truncateToTokenCharBudget,
  fetchStaleBeliefEvents,
  JSON_RPC_TIMEOUT_ERROR,
  listPendingHandoffs,
  readAgentContext,
  recallMemory,
  stashPrecompactReassert,
  subscribeContradictions,
} from "./sovereign.js";
import { classifyIntent, extractScarTissue, filterSafeVaultResults } from "./task.js";
import {
  auditTail,
  collectCorrectionsReassert,
  ensureVault,
  buildPendingLearningsSection,
  expireStaleInboxHandoffs,
  expiredHandoffsBody,
  readInboxStatus,
  readReassertPending,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  settleReassertedInboxEntries,
} from "./vault.js";

/**
 * Claude Code's SessionStart deadline, as an OUTER bound on the boot budget —
 * same contract as the factory's `sessionStartHookTimeoutMs`. Mirrors
 * `hooks/hooks.json` SessionStart `"timeout": 600`; the two must be edited
 * together, since a manifest tightened below the budget would resurrect the
 * bug this bounds (killed mid-boot, output discarded).
 *
 * 600s is Claude Code's DOCUMENTED PLATFORM default for a `command` hook.
 * SessionStart does not lower it (the events that do are UserPromptSubmit at
 * 30s, MessageDisplay at 10s and SessionEnd's shared 1.5s budget) — so 600 is
 * what this entry inherits when the manifest is silent. Those are the
 * platform's numbers, not ours: `hooks.json` separately sets UserPromptSubmit
 * to 60, which is OUR choice and unrelated to this constant.
 *
 * The manifest states 600 explicitly rather than inheriting it, so the value
 * this constant mirrors is visible in the file the platform reads — writing a
 * SMALLER number here would be a real tightening of the boot deadline, not
 * documentation of it.
 */
const CLAUDECODE_SESSION_START_TIMEOUT_MS = 600_000;

async function handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput> {
  // rawSessionId is the payload's own id, possibly empty — never the
  // "session" synthetic fallback. It is what gets threaded into the daemon
  // recall-trace (recallMemory's sessionId) so unlabeled runtimes never
  // conflate into one synthetic thread_id. sessionId keeps the historical
  // defaulted value for envelope identity / inbox filenames / markers.
  const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
  const sessionId = rawSessionId || "session";
  // BOOT BUDGET (parity with the shared factory): a boot that overruns the
  // host's SessionStart deadline is killed and its output DISCARDED, losing
  // identity, corrections and the active plan in one go. We own a shorter,
  // ABSOLUTE deadline and ship whatever landed inside it. These reads were also
  // strictly sequential, so a single cold RPC used to push every later one past
  // the deadline; the independent ones now share the clock concurrently.
  //
  // The clock starts HERE, at handler entry, not after the compaction harvest
  // below. The harness deadline runs from process start, so a budget that began
  // after an expensive harvest would be measuring the wrong interval and could
  // still overrun — on `compact`/`resume` boots, which is exactly when there is
  // a correction waiting to be re-asserted.
  const budgetMs = effectiveHookBudgetMs(CLAUDECODE_SESSION_START_TIMEOUT_MS);
  const deadline = Date.now() + budgetMs;
  const remainingMs = (): number => Math.max(0, deadline - Date.now());
  const rpcTimedOut = { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR };
  await ensureVault(CLAUDECODE_VAULT_PATH);
  // Compaction-summary harvest BACKSTOP (plan-3e5a410b9ab6f715). The primary
  // delivery is the PostCompact hook below, which receives the summary
  // directly (`compact_summary`). This transcript tail-read exists for what
  // PostCompact can miss — hook not yet registered when the compaction ran,
  // older CLI, crash between compaction and hook — and is safe to run on
  // every compact/resume boot because dedup is keyed on the summary CONTENT
  // hash shared with the PostCompact path. transcript_path is documented to
  // lag the in-memory conversation, so a summary missing here is expected
  // sometimes; it self-heals on the next boot. Fail-open, raw-only, no AFM —
  // the AFM-loop timer's compact_distillation pass organizes it daemon-side.
  const bootSource = asString(payload.source);
  if (bootSource === "compact" || bootSource === "resume") {
    await harvestCompactSummary(
      {
        vaultPath: CLAUDECODE_VAULT_PATH,
        workspaceId: workspaceFromPayload(payload, CLAUDECODE_WORKSPACE_ID),
        auditPrefix: "hook",
        platform: "claude-code",
      },
      {
        transcriptPath: asString(payload.transcript_path) || asString(payload.transcriptPath),
        sessionId,
      },
    );
  }
  // c4: reset the once-per-session lifecycle emphasis on a fresh session, so the
  // situational focus can fire again this session.
  await writeLifecycleState(CLAUDECODE_VAULT_PATH, {
    session_id: sessionId,
    emphasized: [],
  }).catch(() => {});
  // TTL-reap stale file handoffs BEFORE the honest read so they neither occupy
  // the capped slice nor inflate totals; they surface once below as 'expired'.
  // SEQUENTIAL on purpose: readInboxStatus below counts what is on disk, so
  // racing the reaper against it would report reaped files as still pending.
  //
  // DELIBERATELY UNBUDGETED. The reaper ARCHIVES as it walks, and withBudget
  // races rather than cancels — so a budget-cut reaper would keep archiving in
  // the background while this boot reported an empty `expired` list. Archived
  // entries are invisible to readInboxStatus, so they would then be reported
  // ZERO times, breaking the surfaces-exactly-once contract. A budget may skip
  // a READ; it must not half-report a WRITE.
  const expiredHandoffs = await expireStaleInboxHandoffs(CLAUDECODE_VAULT_PATH);
  const [status, tail, recall, recentLearnings, inboxStatus] =
    await Promise.all([
      withBudget(
        // timeoutMs, not just the withBudget race: the race ABANDONS the status
        // RPC, and an abandoned socket handle keeps this process alive past the
        // harness kill — which discards output we had already written.
        buildStatusReport({ vaultPath: CLAUDECODE_VAULT_PATH, timeoutMs: remainingMs() }),
        remainingMs(),
        undefined,
      ),
      withBudget(auditTail(CLAUDECODE_VAULT_PATH, 5), remainingMs(), undefined),
      // recall-F1: boot recall previously whitelisted layers=['identity'], which
      // dropped knowledge-layer corrections before rerank. Widen to the
      // correction-bearing layers (single query; the engine's correction salience
      // floor ranks fresh corrections above saturated habitual hits) instead of a
      // second corrections-only round-trip. See BOOT_RECALL_LAYERS for the policy.
      withBudget(
        recallMemory({
          query: `boot identity for ${CLAUDECODE_WORKSPACE_ID}`,
          layers: BOOT_RECALL_LAYERS,
          limit: 8,
          agentId: CLAUDECODE_AGENT_ID,
          workspaceId: CLAUDECODE_WORKSPACE_ID,
          // The load-bearing deadline: it destroys the socket, which is what
          // lets the hook PROCESS exit. Racing a promise alone cannot do that.
          timeoutMs: remainingMs(),
          ...(rawSessionId ? { sessionId: rawSessionId } : {}),
        }),
        remainingMs(),
        rpcTimedOut,
      ),
      // hooks-PL-2 leg (a): the 'read' RPC is the recency-ordered learning surface
      // AND the daemon path that records learning_reads — without it, corrections
      // to beliefs this agent saw can never match in stale_beliefs.
      withBudget(
        readAgentContext({
          agentId: CLAUDECODE_AGENT_ID,
          limit: 8,
          timeoutMs: remainingMs(),
        }),
        remainingMs(),
        rpcTimedOut,
      ),
      withBudget(readInboxStatus(CLAUDECODE_VAULT_PATH, 3), remainingMs(), undefined),
    ]);
  const pending = inboxStatus?.entries ?? [];
  // The `ok` flag distinguishes "the budget cut this read" from its natural
  // empty result — an [] fallback alone is indistinguishable from "no handoff
  // refs", which is the false all-clear this whole section exists to avoid.
  const handoffRead = await withBudget<{
    ok: boolean;
    snippets: Awaited<ReturnType<typeof resolveInboxHandoffContext>>;
  }>(
    resolveInboxHandoffContext(CLAUDECODE_VAULT_PATH, pending).then((snippets) => ({
      ok: true,
      snippets,
    })),
    remainingMs(),
    { ok: false, snippets: [] },
  );
  const handoffContext = handoffRead.snippets;
  const pendingHandoffs = await withBudget(
    listPendingHandoffs({ agentId: CLAUDECODE_AGENT_ID, timeoutMs: remainingMs() }),
    remainingMs(),
    rpcTimedOut,
  );
  const pendingHandoffData = pendingHandoffs.ok && pendingHandoffs.data
    ? pendingHandoffs.data as { handoffs?: Array<{ lease_id?: string; leaseId?: string }> }
    : { handoffs: [] };
  const ackedLeases: string[] = [];
  let unackedLeases = 0;
  for (const handoff of pendingHandoffData.handoffs ?? []) {
    const leaseId = handoff.lease_id ?? handoff.leaseId;
    if (!leaseId) continue;
    // One RPC PER LEASE: unbounded in the number of pending handoffs, so it is
    // the loop most able to blow the deadline. Stop acking when the budget is
    // gone — an unacked lease is still pending next boot; a killed hook is not.
    if (remainingMs() <= 0) {
      unackedLeases += 1;
      continue;
    }
    const ack = await withBudget(
      ackHandoff({
        leaseId,
        status: "accepted",
        agentId: CLAUDECODE_AGENT_ID,
        timeoutMs: remainingMs(),
      }),
      remainingMs(),
      rpcTimedOut,
    );
    // A FAILED ack is an unacked lease too — counting only the budget-skip path
    // reported a timed-out or refused ack as a clean boot.
    if (ack.ok) ackedLeases.push(leaseId);
    else unackedLeases += 1;
  }
  const contradictions = await withBudget(
    subscribeContradictions({ agentId: CLAUDECODE_AGENT_ID, timeoutMs: remainingMs() }),
    remainingMs(),
    rpcTimedOut,
  );
  // `undefined` from resolveActivePlanView MEANS "no active plan", so it cannot
  // double as the budget-cut fallback — the two must stay distinguishable.
  let planRead: {
    ok: boolean;
    view: Awaited<ReturnType<typeof resolveActivePlanView>>;
  } = { ok: true, view: undefined };
  // No try/catch: it would be DEAD CODE, and a catch that implies an audit
  // row which can never be written is exactly the health-signal overstatement
  // this work is removing elsewhere. Neither call can reject —
  // resolveActivePlanView swallows its own failures and returns undefined,
  // and withBudget turns any rejection into its fallback. A budget cut still
  // surfaces, via planRead.ok === false -> degraded.sections.
  //
  // Note the honest limit that leaves: a plan FS failure is indistinguishable
  // from 'no active plan', because resolveActivePlanView conflates them at
  // source. Pre-existing and out of scope here — fixing it belongs in plan.ts.
  planRead = await withBudget(
    resolveActivePlanView(CLAUDECODE_VAULT_PATH).then((view) => ({ ok: true, view })),
    remainingMs(),
    { ok: false, view: undefined },
  );
  const activePlan = planRead.view;

  // Sections the budget cut. Named so a degraded boot is legible as degraded
  // rather than as an agent with nothing in its memory (hooks-PL-5). Computed
  // AFTER every budgeted read, so a read that runs late can still report itself.
  const degradedSections = [
    status === undefined ? "status" : undefined,
    tail === undefined ? "audit_tail" : undefined,
    inboxStatus === undefined ? "pending_learnings" : undefined,
    unackedLeases > 0 ? "handoff_acks" : undefined,
    // Also degraded when the PARENT read was cut: handoffContext is resolved
    // from inboxStatus.entries, so a cut inbox read makes it vacuously empty.
    // ok:true over an empty set the hook never actually had inputs for is the
    // same false all-clear this section exists to prevent.
    handoffRead.ok && inboxStatus !== undefined ? undefined : "handoff_context",
    planRead.ok ? undefined : "active_thread",
    // A failed LIST is not "no pending handoffs": it degraded to an empty set,
    // and reporting handoff_acks:[] without saying so is a false all-clear.
    pendingHandoffs.ok ? undefined : "handoff_leases",
  ].filter((section): section is string => section !== undefined);

  // hooks-PL-3: re-assert corrections stashed by PreCompact, so the
  // post-compaction boot re-injects them even if the daemon is down now.
  // Only fully-consumed entries are cleared (exactly-once re-injection, no
  // unbounded inbox growth); cap-overflowed tails are rewritten for the next
  // boot, and all-malformed entries survive for inspection.
  //
  // Its OWN read, deliberately UNBUDGETED — it must not ride the budgeted
  // `readInboxStatus` above. That read degrades to undefined when the budget is
  // spent, and reassert would then see an empty `pending` and silently inject
  // nothing: the post-compaction boot, the one this whole path exists for,
  // would come up with no corrections. This is cheap local FS and it is the one
  // read whose absence loses data rather than context.
  //
  // readReassertPending (not readInboxStatus) is also the I5 window: it filters
  // to entries that actually carry stale-belief events before taking newest-N,
  // so unrelated inbox files cannot crowd a valid correction out of the slots.
  const reassertPending = await readReassertPending(CLAUDECODE_VAULT_PATH, 3);
  const { events: correctionsReassert, consumedPaths: reassertConsumed, contributingPaths: reassertContributing, deferredTails: reassertDeferred } =
    collectCorrectionsReassert(reassertPending);
  // R2: settle AFTER the envelope reaches the host, never before. Archiving
  // here would consume the correction inside the window where the boot can
  // still be killed, losing it for good — see hook-delivery.ts.
  //
  // Conditional on this envelope actually CARRYING something: when nothing was
  // injected, the consumed paths are empty stashes, which carry no correction,
  // so clearing them does not depend on delivery. (Parity with the factory —
  // claude-code can always inject at SessionStart, so its envelope is never
  // wire-dropped, but the rule belongs on both paths rather than in one.)
  // Settled in TWO parts, because the two halves have different preconditions.
  // Empty stashes carry no correction, so clearing them cannot depend on
  // delivery — and must not, or a platform that can never inject would keep
  // them forever. Entries that CONTRIBUTED events (and partially-injected
  // tails) may only settle once the envelope carrying them lands. Deferring
  // both together held the empties hostage whenever one window contained a
  // real correction AND an empty stash.
  // Counted separately for the audit below: settling eagerly and waiting on
  // delivery are different facts, and one "pending" number covering both
  // misreports the empties as archives that have not happened yet.
  const emptyPaths = reassertConsumed.filter(
    (filePath) => !reassertContributing.includes(filePath),
  );
  if (emptyPaths.length > 0) {
    await settleReassertedInboxEntries(CLAUDECODE_VAULT_PATH, {
      consumedPaths: emptyPaths,
      deferredTails: [],
    });
  }
  if (reassertContributing.length > 0 || reassertDeferred.length > 0) {
    deferUntilDelivered(() =>
      settleReassertedInboxEntries(CLAUDECODE_VAULT_PATH, {
        consumedPaths: reassertContributing,
        deferredTails: reassertDeferred,
      }),
    );
  }

  const envelopeBody: any = {
    contract: MEMORY_CONTRACT,
    identity: {
      agent: CLAUDECODE_AGENT_ID,
      workspace: CLAUDECODE_WORKSPACE_ID,
      vault: CLAUDECODE_VAULT_PATH,
      session_id: sessionId,
      // Unknown reads as not-ok, never as ok: `degraded.sections` below says
      // which of these the budget cut, so "false" is not read as "checked".
      daemon_ok: status?.socket.ok ?? false,
      afm_ok: status?.afm.ok ?? false,
    },
    // Omitted rather than zero-filled when the budget cut the inbox read: a
    // "0 pending" the hook never counted is a false all-clear.
    ...(inboxStatus !== undefined
      ? { pending_learnings: buildPendingLearningsSection(inboxStatus, expiredHandoffs) }
      : {}),
    // The reaper is UNBUDGETED and already archived these, so this boot is
    // their only chance to be reported — but they normally ride inside
    // pending_learnings, which the line above drops when the budgeted inbox
    // read degraded. That combination reported them ZERO times, the exact
    // failure the reaper is unbudgeted to avoid. Ship them on their own
    // whenever their usual carrier is missing.
    ...(inboxStatus === undefined && expiredHandoffs.length > 0
      ? { expired_handoffs: expiredHandoffsBody(expiredHandoffs) }
      : {}),
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
        ? buildBootRecallSlice(recall.data, CLAUDECODE_AGENT_ID)
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
    ...(tail !== undefined
      ? { audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]) }
      : {}),
  };

  if (degradedSections.length > 0) {
    // Say so IN the envelope: a boot the budget truncated must never be
    // indistinguishable from a boot that found nothing (hooks-PL-5).
    envelopeBody.degraded = {
      sections: degradedSections,
      budget_ms: budgetMs,
      note: "Boot exceeded the SessionStart budget (likely a cold daemon). The listed sections were NOT read — treat them as unknown, not empty, and call minni_recall explicitly if they matter.",
    };
  }

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
    envelopeBody.active_thread = activePlan;
  }

  const budget = envelopeBudgetFor(CLAUDECODE_CONTEXT_WINDOW);
  if (recentLearnings.ok && recentLearnings.data?.context) {
    const identityBody = extractIdentityBody(recentLearnings.data.context, CLAUDECODE_AGENT_ID);
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
      daemon_ok: status?.socket.ok ?? false,
      afm_ok: status?.afm.ok ?? false,
      pending_inbox: inboxStatus?.totalPending ?? null,
      expired_handoffs: expiredHandoffs.length,
      handoff_context: handoffContext.length,
      corrections_reassert: correctionsReassert.length,
      budget_ms: budgetMs,
      ...(degradedSections.length > 0 ? { degraded_sections: degradedSections } : {}),
      // Settle outcomes reported separately. "pending" counts ONLY what is
      // still registered with deferUntilDelivered; empties already cleared are
      // not waiting on anything, and folding them in claimed archives that had
      // in fact already happened. (claude-code's wire can always inject at
      // SessionStart, so there is no parked outcome on this path.)
      reassert_entries_cleared_eager: emptyPaths.length,
      reassert_entries_pending_clear: reassertContributing.length,
      reassert_tails_pending_defer: reassertDeferred.length,
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
  // rawSessionId (possibly empty) is the audit/recall-trace correlation id;
  // sessionId keeps the "session" fallback for envelope identity / lifecycle
  // state only — see handleSessionStart's comment for why the two must not
  // be conflated.
  const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
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
  // BUDGET (see hookBudgetMs): Claude Code kills this hook at 30s and DISCARDS
  // its output, losing the whole turn's injection silently. We own a shorter
  // deadline so a cold/slow daemon costs us the daemon recall only, not the
  // turn. The deadline is ABSOLUTE and shared: the daemon recall gets whatever
  // is left, and the local vault search (fast, no socket) races the same clock
  // independently so it still lands when only the daemon is stalled.
  const deadline = Date.now() + hookBudgetMs();
  const remainingMs = (): number => Math.max(0, deadline - Date.now());
  const [vaultResultsRaw, recall] = await Promise.all([
    withBudget(searchVaultNotes(CLAUDECODE_VAULT_PATH, prompt, 6), remainingMs(), []),
    withBudget(
      recallMemory({
        query: prompt,
        limit: 6,
        agentId: CLAUDECODE_AGENT_ID,
        workspaceId: CLAUDECODE_WORKSPACE_ID,
        // The load-bearing deadline: this destroys the socket, which is what
        // actually lets the hook process EXIT. The race below cannot do that.
        timeoutMs: remainingMs(),
        ...(rawSessionId ? { sessionId: rawSessionId } : {}),
      }),
      remainingMs(),
      { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR },
    ),
  ]);
  const vaultResults = filterSafeVaultResults(vaultResultsRaw);
  // "The daemon ran out of time" is NOT "the daemon said there is nothing".
  // Only a genuine answer (or a structured refusal) may drive the weak-turn
  // path below, which CLEARS recall-state.
  const daemonTimedOut = !recall.ok && recall.error === JSON_RPC_TIMEOUT_ERROR;

  // s5 strength gate: emit the light pointer + recall-state file ONLY when the
  // top recall strength clears the threshold; otherwise inject nothing and clear
  // any stale state from a previous strong turn.
  const strong = extractStrongRecall(
    recall.ok ? recall.data : undefined,
    vaultResults,
    threshold,
  );
  let recallStateFile: string | undefined;
  let stalePointer: string | undefined;
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
  } else if (daemonTimedOut) {
    // DEGRADED, not weak. Preserve the existing state file — clearing it would
    // destroy the s6 guard's only pointer on exactly the turns where the daemon
    // can't rebuild it — and surface it, clearly flagged, so this turn injects
    // something honest rather than nothing.
    stalePointer = buildStaleRecallPointer(
      await readRecallState(CLAUDECODE_VAULT_PATH).catch(() => null),
    );
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

  let active_thread_ref: ReturnType<typeof compactPlanPointer> | undefined;
  if (activePlan !== undefined) {
    active_thread_ref = compactPlanPointer(activePlan);
  }

  // Nothing salient to inject this turn: no strong recall, no stale fallback
  // pointer AND no active plan.
  if (!strong && stalePointer === undefined && active_thread_ref === undefined) {
    await recordAudit(CLAUDECODE_VAULT_PATH, {
      tool: "hook_user_prompt_submit",
      summary: prompt.slice(0, 120),
      details: {
        intent: intent.action,
        vault_matches: vaultResults.map((result) => result.relativePath),
        daemon_ok: recall.ok,
        daemon_timed_out: daemonTimedOut,
        task_signature: signature,
        recall_strong: false,
        // RAW id only — omit rather than stamp the synthetic "session"
        // fallback, so unlabeled turns don't conflate into one audit thread.
        ...(rawSessionId ? { session_id: rawSessionId } : {}),
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
  } else if (stalePointer !== undefined) {
    envelopeBody.recall_pointer = stalePointer;
    envelopeBody.recall_stale = true;
    envelopeBody.recall_state = recallStatePath(CLAUDECODE_VAULT_PATH);
  }
  if (daemonTimedOut) {
    // Say so IN the envelope: a degraded turn must never be indistinguishable
    // from a clean one (hooks-PL-5). The agent should treat absent recall as
    // "not looked up", not as "nothing to find".
    envelopeBody.degraded = {
      daemon_recall: "timed_out",
      budget_ms: hookBudgetMs(),
      note: "Daemon recall exceeded the hook budget (likely a cold daemon loading models). Memory was NOT consulted this turn — call minni_recall explicitly if it matters.",
    };
  }

  // Option C: inject a compact plan POINTER per turn, not the full plan. The
  // headline + next_action are the actionable one-liners the agent needs every
  // turn; the full goal/open_questions/pending list is omitted (it barely changes
  // turn-to-turn) and pulled on demand via minni_thread_status. SessionStart still
  // injects the full plan view for boot/rehydration.
  if (activePlan !== undefined) {
    // Plan parity (audit C5): inline the compact-pointer call so all four hooks
    // share the same wire shape.
    envelopeBody.active_thread_ref = compactPlanPointer(activePlan);
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
      daemon_timed_out: daemonTimedOut,
      recall_stale: stalePointer !== undefined,
      task_signature: signature,
      recall_strong: Boolean(strong),
      top_score: strong?.topScore,
      // RAW id only — see the weak-path comment above.
      ...(rawSessionId ? { session_id: rawSessionId } : {}),
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

// PRIMARY compaction-summary delivery (plan-3e5a410b9ab6f715). PostCompact
// hands the finished summary to the hook directly as `compact_summary`
// (documented 2026-07-29) — no transcript read, no flush race. The
// SessionStart tail-read above stays as the backstop; the shared content-hash
// dedup keeps the two paths from double-harvesting. Raw storage only; the
// daemon's compact_distillation pass does the AFM review on its own timer.
async function handlePostCompact(payload: Record<string, unknown>): Promise<HookOutput> {
  await ensureVault(CLAUDECODE_VAULT_PATH);
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  await harvestSummaryText(
    {
      vaultPath: CLAUDECODE_VAULT_PATH,
      workspaceId: workspaceFromPayload(payload, CLAUDECODE_WORKSPACE_ID),
      auditPrefix: "hook",
      platform: "claude-code",
    },
    {
      summaryText: asString(payload.compact_summary),
      sessionId,
    },
  );
  return { continue: true };
}

// Stop is NOT a claude-code-specific handler. It routes to the shared
// handleStopCore so the governance posture, the zero-candidate write guard and
// the inbox ingest stamps (kind/agent_id/workspace_id) exist exactly once —
// this file's private copy had drifted from the factory's on all three. Only
// the audit prefix and the commit call-to-action are claude-code's own.
const CLAUDECODE_STOP_CONFIG: StopCoreConfig = {
  agentId: CLAUDECODE_AGENT_ID,
  vaultPath: CLAUDECODE_VAULT_PATH,
  defaultWorkspaceId: CLAUDECODE_WORKSPACE_ID,
  auditPrefix: "hook",
  stopCommitHint: "Use /minni:learn to commit.",
  wire: claudeCodeWire,
};

async function handleStop(payload: Record<string, unknown>): Promise<HookOutput> {
  return handleStopCore(CLAUDECODE_STOP_CONFIG, payload);
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
  // The PreToolUse payload may carry a session_id; stamp it so the Stop receipt
  // can attribute this guard nudge. Only add it when present — never invent a
  // "session" placeholder on this path.
  const guardSessionId = asString(payload.session_id) || asString(payload.sessionId);
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
      ...(guardSessionId ? { session_id: guardSessionId } : {}),
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
    case "PostCompact":
      return handlePostCompact(payload);
    case "Stop":
      return handleStop(payload);
    case PRE_TOOL_USE_EVENT:
      return handlePreToolUse(payload);
    default:
      // P4 (#228): a valid event with no case here used to exit clean with no
      // marker, so a drop was indistinguishable from an event never sent.
      await recordUnroutedEvent(CLAUDECODE_VAULT_PATH, "hook", event);
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
    // P4 (#228): same clean-continue swallow as the dispatch default. Claude
    // Code is the primary manifest, so this is the path that mattered most.
    await recordUnroutedEvent(CLAUDECODE_VAULT_PATH, "hook", event);
    emit({ continue: true });
    return;
  }
  try {
    const output = await dispatch(event, payload);
    // emitAndCommit, not emit: work the handler deferred (archiving a consumed
    // correction) runs only once this output has actually reached the host.
    await emitAndCommit(output, {
      vaultPath: CLAUDECODE_VAULT_PATH,
      auditPrefix: "hook",
      event,
    });
    // Delivery is settled; nothing left to wait for. See exitAfterDelivery.
    exitAfterDelivery();
  } catch (error) {
    // Nothing was delivered, so nothing the handler deferred may be committed.
    discardDeliveryCommits();
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
    // failAndExit, not emit: the degraded signal has to be FLUSHED before an
    // abandoned RPC handle can keep this process alive into the harness kill.
    await failAndExit({
      continue: true,
      systemMessage: `Minni hook degraded (${event}): ${message} — memory injection skipped this event; see vault log.md.`,
    });
  }
}

void main();

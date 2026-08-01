// Shared hook HANDLERS (review panel, plan-parity follow-up): codex-hook.ts,
// grok-hook.ts and kilocode-hook.ts were ~360-line near-clones whose four
// handler bodies differed ONLY in config constants and a few flagged
// behaviors. hook-utils.ts holds the protocol leaf helpers; this module holds
// the stateful handler logic, parameterized by a typed per-agent config, so
// future changes (evidence envelope format, plan injection, inbox drain
// logic) have ONE maintenance surface instead of four. hook.ts (claude-code)
// diverges structurally (PreCompact cannot inject context) and keeps its own
// handler set — but NOT its own Stop: that one is `handleStopCore` below, which
// hook.ts imports, because the Stop governance posture must be identical on
// every platform and the two copies had already drifted.
//
// Handlers here return INTENT, not wire shapes: what reaches the model is
// decided per platform by hook-platform.ts. Emitting Claude Code's envelope
// everywhere is what silently voided Codex's PreCompact output and discarded
// Grok Build's memory outright.
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
  effectiveHookBudgetMs,
  emit,
  inboxPrincipalForVaultPath,
  readStdin,
  stringArray,
  vaultRecallToBody,
  withBudget,
  workspaceFromPayload,
} from "./hook-utils.js";
import type { HookOutput } from "./hook-utils.js";
import {
  deferUntilDelivered,
  discardDeliveryCommits,
  emitAndCommit,
  exitAfterDelivery,
  markOutputDropped,
} from "./hook-delivery.js";
import { harvestSummaryText } from "./compact-harvest.js";
import { injectIntent, noIntent, noteIntent } from "./hook-intent.js";
import type { HookIntent } from "./hook-intent.js";
import { canInject, renderIntent, wireFor } from "./hook-platform.js";
import type { PlatformWire } from "./hook-platform.js";
import { compactPlanPointer, resolveActivePlanView } from "./plan.js";
import { routeMemoryIntent } from "./policy.js";
import {
  buildRecallPointer,
  buildStaleRecallPointer,
  clearRecallState,
  extractStrongRecall,
  markRecallConsumed,
  readRecallState,
  recallPointerThreshold,
  recallStatePath,
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
  JSON_RPC_TIMEOUT_ERROR,
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
  expiredHandoffsBody,
  formatSessionReceiptLine,
  readInboxStatus,
  parkUndeliverableInboxEntries,
  readReassertPending,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  sessionReceipt,
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
  // RETIRED 2026-07-25: `alwaysWriteStopInbox`. It once let codex keep the
  // historical "write the Stop inbox file even with zero candidates" behavior;
  // e51ed45 flipped codex to false because always-write produced kind-less
  // inbox noise the AFM ingest loop skips as _unrecognized, leaving every
  // entrypoint on false. Under the governance posture below Stop never
  // self-drafts on any platform, so a per-agent knob for littering the inbox
  // has no legitimate setting — the zero-candidate skip is now unconditional.
  /**
   * s6 PreToolUse recall-guard mode override. When set, it wins over the
   * MINNI_RECALL_GUARD_MODE env default ("off" | "soft" | "strict"). Omit to
   * resolve from the environment (default "soft").
   */
  recallGuardMode?: RecallGuardMode;
  /**
   * Native wire contract for the platform actually running this hook. Defaults
   * to `wireFor(agentId)`. Override in tests, or when an agent identity does
   * not name its own platform.
   */
  wire?: PlatformWire;
  /**
   * This platform's manifest timeout (ms) for the PROMPT-SUBMIT event, as an
   * OUTER bound on the internal budget — see `effectiveHookBudgetMs`. Mirror
   * the `timeout` in `hooks/hooks-<platform>.json`; the two must be edited
   * together, since a manifest tightened below the budget would resurrect the
   * exact bug this bounds (killed mid-work, output discarded).
   *
   * Current values: codex/grok `UserPromptSubmit` 30s, cursor
   * `beforeSubmitPrompt` 30s, gemini `PreInvocation` **10s** (the tightest).
   * Omit for a platform with no declared timeout (kilocode has none at all).
   */
  promptHookTimeoutMs?: number;
  /**
   * This platform's manifest timeout (ms) for SESSION START, as an OUTER bound
   * on the boot budget — same contract as `promptHookTimeoutMs`, and the same
   * obligation to be edited together with `hooks/hooks-<platform>.json`.
   *
   * Boot needs its own bound because the deadlines differ: gemini declares
   * **10s** for SessionStart (hooks-gemini.json) while codex, grok and cursor
   * declare 30s. Without it a cold daemon runs the boot RPCs past gemini's kill
   * and the whole envelope — identity, corrections, active plan — is discarded.
   * `sessionstart-delivery.test.mjs` pins each value to its manifest.
   */
  sessionStartHookTimeoutMs?: number;
}

/** Test seam: lets behavioral tests drive the zero-candidate Stop branch. */
export interface AgentHookDeps {
  prepareOutcome?: typeof prepareOutcome;
}

/**
 * Per-platform inputs to `handleStopCore`. `AgentHookConfig` is a structural
 * superset, so the factory passes its own config straight through; hook.ts
 * builds one from its CLAUDECODE_* constants.
 */
export interface StopCoreConfig {
  agentId: string;
  vaultPath: string;
  defaultWorkspaceId: string;
  auditPrefix: string;
  stopCommitHint?: string;
  /** Platform wire for last-task text; optional so tests can omit it. */
  wire?: PlatformWire;
}

/**
 * The ONE Stop implementation, shared by the factory and by hook.ts.
 *
 * hook.ts keeps its own entrypoint because Claude Code's PreCompact cannot
 * inject context, so its handler SET diverges — but its Stop GOVERNANCE is not
 * allowed to diverge, and it did: the claude-code copy dropped
 * `kind`/`agent_id`/`workspace_id` from the inbox write (so
 * inbox_ingest.py filed every Claude Code candidate under workspace "default"
 * regardless of cwd) and omitted `workspace` from its audit breadcrumb, and the
 * zero-candidate guard had to be patched separately in both copies. Nothing
 * about Stop legitimately differs per platform; everything that varies is a
 * field of StopCoreConfig.
 */
export async function handleStopCore(
  config: StopCoreConfig,
  payload: Record<string, unknown>,
  prepareOutcomeFn: typeof prepareOutcome = prepareOutcome,
): Promise<HookOutput> {
  await ensureVault(config.vaultPath);
  // Stop keeps the defaulted "session" fallback (inbox filename, marker text,
  // sessionReceipt) unchanged: unlike recall/audit correlation, a runtime that
  // never labeled any turn with a real session_id still gets a best-effort
  // receipt merged onto the synthetic "session" bucket rather than no receipt.
  const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
  const sessionId = rawSessionId || "session";
  // On the synthetic fallback, turns that DID stamp a real session_id must
  // still count inside this window — otherwise the receipt reports zeros
  // despite real activity.
  const receiptOptions = { includeStamped: !rawSessionId };
  const workspaceId = workspaceFromPayload(payload, config.defaultWorkspaceId);
  // Task and summary must stay distinct. Falling back to `summary` for the
  // task field made summary-only payloads compose `"<summary>: <summary>"` —
  // one of the two forward-compat shapes always produced a duplicated candidate.
  // Prefer the platform wire's Stop task text (assistant final message, etc.),
  // then a real user prompt when present; otherwise the session id is enough
  // context for the `"${task}: ${summary}"` form in prepareOutcome. Never fall
  // back to `summary`.
  const lastTask =
    (config.wire?.lastTaskText(payload) || "").trim() ||
    asString(payload.last_user_message) ||
    sessionId;

  // GOVERNANCE POSTURE — Stop auto-draft RETIRED 2026-07-24. The 2026-07-23
  // inbox investigation found Stop's audit-tail distillation produced 0 real
  // learnings across 40 drafts: the tail is Minni's OWN telemetry log, so the
  // draft was noise by construction. The durable-capture path is now
  // EXCLUSIVELY the agent's explicit minni_prepare_outcome / minni_learn call.
  // Stop no longer self-drafts. The `changedFiles`/`summary` branch below is a
  // documented FORWARD-COMPAT hook: it fires only if a future Stop harness
  // supplies genuine outcome material in the payload (today's real harnesses
  // never do). Absent that — or when the material scrubs to zero candidates
  // at the guard below — Stop records ONE log-only breadcrumb (so the session
  // is not silently invisible) and writes no inbox file. That breadcrumb is
  // load-bearing, and it is only actually reaching log.md because vault.ts
  // exempts `*_stop` from the hook_* audit throttle — before that exemption a
  // same-second UserPromptSubmit swallowed it and the claim above was false.
  // Keep the two in sync. This holds on EVERY
  // platform, claude-code included: there is no per-agent opt-out (see the
  // retired `alwaysWriteStopInbox` note on AgentHookConfig).
  // NOTE: this is a governance-posture change — it needs operator sign-off
  // before the plugin is re-propagated.
  //
  // Session receipts (PR #166 slice): every Stop path still emits a proof-of-use
  // tally. The stop MARKER summary stays exactly `stop <id>` so sessionReceipt /
  // listSessions can close the boot→stop window; breadcrumb reasons live in
  // details, not the summary.
  const changedFiles = stringArray(payload.changedFiles ?? payload.changed_files);
  const outcomeSummary = asString(payload.summary).trim();
  const hasDraftableSignal = changedFiles.length > 0 || outcomeSummary.length > 0;

  async function emitStopBreadcrumb(reason: string): Promise<HookOutput> {
    // Tally BEFORE writing the stop row so the receipt embeds in its own details
    // without counting this marker's candidates (always 0 on breadcrumb paths).
    const receipt = await sessionReceipt(
      config.vaultPath,
      sessionId,
      500,
      receiptOptions,
    ).catch(() => undefined);
    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_stop`,
      summary: `stop ${sessionId}`,
      details: {
        workspace: workspaceId,
        candidates: 0,
        reason,
        ...(receipt ? { receipt } : {}),
      },
    });
    // Surface the receipt even with zero activity: proof that no memory work
    // happened is itself the signal. Platforms without a note channel drop it
    // via renderIntent; the audit marker still closes the session window.
    return {
      continue: true,
      ...(receipt ? { systemMessage: formatSessionReceiptLine(receipt) } : {}),
    };
  }

  if (!hasDraftableSignal) {
    return emitStopBreadcrumb("no_draftable_signal");
  }

  const outcome = await prepareOutcomeFn({
    task: lastTask.slice(0, 200),
    summary: outcomeSummary,
    changedFiles,
    profile: "compact",
    vaultPath: config.vaultPath,
  });

  const candidates = outcome.outcomeDraft.learnCandidates;

  // The signal was draftable but prepareOutcome's telemetry filter
  // (isAuditTelemetryLine) scrubbed it to nothing — e.g. a harness that
  // hands Stop a slice of Minni's own audit log as `summary`. The scrub has
  // to hold at the WRITE layer too: writing the file anyway reintroduces
  // precisely the empty-inbox litter issue #173 removed, and the AFM ingest
  // loop then skips it as unrecognized noise. One log-only breadcrumb (same
  // shape as the no-draftable-signal branch above) keeps the session visible
  // without costing an inbox file. This is also the guard that keeps holding
  // when the drafter itself yields nothing for `changedFiles` without a
  // `summary` — the write layer must not depend on the drafter's verdict.
  if (candidates.length === 0) {
    return emitStopBreadcrumb("no_candidates_after_scrub");
  }

  // `kind`/`agent_id`/`workspace_id` are the INGEST CONTRACT, not decoration:
  // src/minni/afm_passes/inbox_ingest.py reads `workspace_id or "default"`, so
  // an unstamped file lands in the catch-all workspace no matter what cwd the
  // session ran in, and `agent_id` is cross-checked against the vault-derived
  // principal for provenance. `kind` names the FILE FORMAT, never the author.
  //
  // The `agent_id` stamp is the INGEST's principal (inboxPrincipalForVaultPath),
  // NOT `config.agentId`. That cross-check is an equality test that DROPS the
  // file on mismatch, and the configured id is operator-settable
  // (MINNI_CLAUDECODE_AGENT_ID=claudecode) while the principal is derived from
  // the vault dir name — so stamping the config id silently discarded every
  // Claude Code candidate as `_agent_mismatch`. Undefined (a vault dir with no
  // `-vault` suffix) writes NO stamp: an absent agent_id skips the cross-check,
  // which is the pre-stamp behavior and strictly better than a wrong guess.
  const inbox = await writeInbox(config.vaultPath, sessionId, {
    kind: "stop_candidates",
    agent_id: inboxPrincipalForVaultPath(config.vaultPath),
    workspace_id: workspaceId,
    candidates,
    log_only: outcome.outcomeDraft.logOnly,
    expires: outcome.outcomeDraft.expires,
    do_not_store: outcome.outcomeDraft.doNotStore,
    last_task: lastTask.slice(0, 200),
  });

  // Tally BEFORE writing the stop entry so the receipt embeds in its own audit
  // details (candidates_drafted then reflects prior stops, not this one — the
  // display line below merges THIS stop's drafts so it never contradicts the
  // candidate sentence beside it).
  const receipt = await sessionReceipt(
    config.vaultPath,
    sessionId,
    500,
    receiptOptions,
  ).catch(() => undefined);
  await recordAudit(config.vaultPath, {
    tool: `${config.auditPrefix}_stop`,
    summary: `stop ${sessionId}`,
    details: {
      candidates: candidates.length,
      workspace: workspaceId,
      inbox_path: inbox.filePath,
      ...(receipt ? { receipt } : {}),
    },
  });

  const displayReceipt = receipt
    ? { ...receipt, candidates_drafted: receipt.candidates_drafted + candidates.length }
    : undefined;
  const receiptLine = displayReceipt ? ` ${formatSessionReceiptLine(displayReceipt)}` : "";

  return {
    continue: true,
    systemMessage: `Minni: ${candidates.length} candidate learning${
      candidates.length === 1 ? "" : "s"
    } drafted to inbox (${inbox.filePath}). ${
      config.stopCommitHint ?? "Use minni_prepare_outcome/minni_learn to review and commit."
    }${receiptLine}`,
  };
}

export interface AgentHookHandlers {
  handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput>;
  handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput>;
  handlePreToolUse(payload: Record<string, unknown>): Promise<PreToolUseDecisionOutput>;
  handlePreCompact(payload: Record<string, unknown>): Promise<HookOutput>;
  handleCompactSummary(payload: Record<string, unknown>): Promise<HookOutput>;
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
  const wire = config.wire ?? wireFor(config.agentId);

  // Handlers express intent; the platform wire decides what can actually be
  // said. An injection the platform cannot carry is recorded, never silently
  // swallowed -- that silence is exactly what hid Grok Build's and Kilocode's
  // missing memory for so long.
  const render = async (intent: HookIntent): Promise<HookOutput> => {
    const { output, dropped } = renderIntent(wire, intent);
    if (dropped) {
      // The payload never went on the wire, so nothing it was carrying may be
      // consumed — see markOutputDropped. The write below still SUCCEEDS (it is
      // a noop), which is exactly why the flush alone cannot be trusted.
      markOutputDropped();
      try {
        await recordAudit(config.vaultPath, {
          tool: `${config.auditPrefix}_intent_dropped`,
          summary: `${dropped.event}: ${dropped.reason}`,
          // Bucket per EVENT. Every drop shares one tool name, so without this
          // a Stop drop within 5s of a UserPromptSubmit drop is throttled away
          // and reported as written -- losing the only record that memory
          // failed to land.
          throttleKey: `${config.auditPrefix}_intent_dropped__${dropped.event}`,
        });
      } catch {
        // Audit unavailable. The drop is still correct behavior; losing the
        // record must not also lose the event.
      }
    }
    return output as HookOutput;
  };

  async function handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput> {
    // rawSessionId is the payload's own id, possibly empty — never the
    // "session" synthetic fallback. It is what gets threaded into the daemon
    // recall-trace (recallMemory's sessionId) so unlabeled runtimes never
    // conflate into one synthetic thread_id. sessionId keeps the historical
    // defaulted value for envelope identity / inbox filenames / markers.
    const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
    const sessionId = rawSessionId || "session";
    const workspaceId = workspaceFor(payload);
    // BOOT BUDGET: platforms kill SessionStart and DISCARD its output — gemini
    // at 10s (hooks-gemini.json). A boot that overruns loses the whole envelope,
    // so we own a shorter deadline and deliver whatever landed inside it. The
    // deadline is ABSOLUTE and shared: each read gets only what is left, and
    // once it is spent the rest resolve instantly with their degraded value so
    // the envelope — and, critically, the corrections below — still ship.
    //
    // The clock starts at handler ENTRY: the harness deadline runs from process
    // start, so any setup done first (vault creation, and on claude-code the
    // compaction harvest) is time already spent against it.
    const budgetMs = effectiveHookBudgetMs(config.sessionStartHookTimeoutMs);
    const deadline = Date.now() + budgetMs;
    const remainingMs = (): number => Math.max(0, deadline - Date.now());
    const rpcTimedOut = { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR };
    await ensureVault(config.vaultPath);

    // TTL-reap stale file handoffs BEFORE the honest read so they neither occupy
    // the capped slice nor inflate totals; they surface once below as 'expired'.
    //
    // DELIBERATELY UNBUDGETED. The reaper ARCHIVES as it walks, and withBudget
    // races rather than cancels — so a budget-cut reaper would keep archiving in
    // the background while this boot reported an empty `expired` list. Archived
    // entries are invisible to readInboxStatus, so they would then be reported
    // ZERO times, breaking the surfaces-exactly-once contract. A budget may skip
    // a READ; it must not half-report a WRITE.
    const expiredHandoffs = await expireStaleInboxHandoffs(config.vaultPath);
    const [status, tail, identityRead, recall, contradictions, inboxStatus] = await Promise.all([
      withBudget(
        // timeoutMs, not just the withBudget race: the race ABANDONS the status
        // RPC, and an abandoned socket handle keeps this process alive past the
        // harness kill — which discards output we had already written.
        buildStatusReport({ vaultPath: config.vaultPath, timeoutMs: remainingMs() }),
        remainingMs(),
        undefined,
      ),
      withBudget(auditTail(config.vaultPath, 5), remainingMs(), undefined),
      // hooks-PL-2 leg (a): BOTH boot modes issue the daemon 'read' — it is
      // the recency-ordered learning surface AND the path that records
      // learning_reads, which stale_beliefs matches on. agent-context boots
      // inject it whole as native layer 1; identity-recall boots trim it to
      // recent_learnings below.
      withBudget(
        readAgentContext({ agentId: config.agentId, limit: 8, timeoutMs: remainingMs() }),
        remainingMs(),
        rpcTimedOut,
      ),
      // recall-F1: boot recall must include the correction-bearing layers, not
      // just the identity shelf (the widened search is what lets knowledge-
      // layer corrections rank in). See BOOT_RECALL_LAYERS for the policy.
      withBudget(
        recallMemory({
          query: `boot identity for ${workspaceId}`,
          layers: BOOT_RECALL_LAYERS,
          limit: 8,
          agentId: config.agentId,
          workspaceId,
          // The load-bearing deadline: it destroys the socket, which is what
          // lets the hook PROCESS exit. Racing a promise alone cannot do that.
          timeoutMs: remainingMs(),
          ...(rawSessionId ? { sessionId: rawSessionId } : {}),
        }),
        remainingMs(),
        rpcTimedOut,
      ),
      // hooks-PL-1/PL-2: corrections to beliefs this agent read must
      // re-surface at boot (stale_beliefs), on every platform.
      withBudget(
        subscribeContradictions({ agentId: config.agentId, timeoutMs: remainingMs() }),
        remainingMs(),
        rpcTimedOut,
      ),
      withBudget(readInboxStatus(config.vaultPath, 3), remainingMs(), undefined),
    ]);
    const pending = inboxStatus?.entries ?? [];
    // The `ok` flag distinguishes "the budget cut this read" from its natural
    // empty result — an [] fallback alone is indistinguishable from "no handoff
    // refs", which is the false all-clear this whole section exists to avoid.
    const handoffRead = await withBudget<{
      ok: boolean;
      snippets: Awaited<ReturnType<typeof resolveInboxHandoffContext>>;
    }>(
      resolveInboxHandoffContext(config.vaultPath, pending).then((snippets) => ({
        ok: true,
        snippets,
      })),
      remainingMs(),
      { ok: false, snippets: [] },
    );
    const handoffContext = handoffRead.snippets;
    // hooks-PL-3: re-assert corrections stashed by PreCompact, so the
    // post-compaction boot re-injects them even if the daemon is down now.
    // Consumed entries are settled (exactly-once re-injection, no unbounded
    // inbox growth); cap-overflowed tails are rewritten so they re-inject on
    // the next boot, and all-malformed entries survive for inspection.
    // I5: use the reassert-specific window so recent all-malformed files cannot
    // crowd valid corrections out of the newest-N slots.
    //
    // Deliberately UNBUDGETED: this is cheap local FS, and it is the one read
    // whose absence loses data rather than context.
    const reassertPending = await readReassertPending(config.vaultPath, 3);
    const { events: correctionsReassert, consumedPaths: reassertConsumed, contributingPaths: reassertContributing, deferredTails: reassertDeferred } =
      collectCorrectionsReassert(reassertPending);
    // R2: settle AFTER the envelope reaches the host, never before. Archiving
    // here would consume the correction inside the window where the boot can
    // still be killed, losing it for good — see hook-delivery.ts.
    //
    // The deferral is conditional on this envelope actually CARRYING something.
    // When nothing was injected, the consumed paths are empty stashes (codex and
    // grok stash unconditionally at PreCompact) — those carry no correction, so
    // clearing them does not depend on delivery, and making it depend would let
    // one file per compaction cycle pile up forever on a platform whose wire can
    // never inject at SessionStart.
    // Settled in TWO parts, because the two halves have different preconditions.
    // Empty stashes carry no correction, so clearing them cannot depend on
    // delivery — and must not, or a platform that can never inject would keep
    // them forever. Entries that CONTRIBUTED events (and partially-injected
    // tails) may only settle once the envelope carrying them lands. Deferring
    // both together held the empties hostage whenever one window contained a
    // real correction AND an empty stash.
    const emptyPaths = reassertConsumed.filter(
      (filePath) => !reassertContributing.includes(filePath),
    );
    if (emptyPaths.length > 0) {
      await settleReassertedInboxEntries(config.vaultPath, {
        consumedPaths: emptyPaths,
        deferredTails: [],
      });
    }
    if (reassertContributing.length > 0 || reassertDeferred.length > 0) {
      if (canInject(wire, "SessionStart")) {
        deferUntilDelivered(() =>
          settleReassertedInboxEntries(config.vaultPath, {
            consumedPaths: reassertContributing,
            deferredTails: reassertDeferred,
          }),
        );
      } else {
        // STRUCTURAL drop, not a transient one: this wire declares SessionStart
        // un-injectable, so "keep it and retry next boot" never terminates —
        // one durable file per correction-bearing compaction, each permanently
        // occupying a slot in the newest-N reassert window. Park them instead:
        // out of the window, still on disk, and audited, so the backlog is
        // countable rather than either silently destroyed or infinite.
        //
        // Parking is the BOUND, not the delivery fix. Routing these to Stop —
        // the only injectable event on such a wire — is tracked in issue #253
        // and deliberately not attempted here; the two must not drift apart.
        const parkPaths = [
          ...reassertContributing,
          ...reassertDeferred.map((tail) => tail.filePath),
        ];
        const parked = await parkUndeliverableInboxEntries(parkPaths);
        await recordAudit(config.vaultPath, {
          tool: `${config.auditPrefix}_undeliverable_on_wire`,
          summary: `SessionStart: ${wire.id} cannot inject at SessionStart; parked ${parked.length} correction entr${
            parked.length === 1 ? "y" : "ies"
          } to inbox/.undeliverable (see issue #253 for delivery)`,
          details: {
            wire: wire.id,
            parked: parked.length,
            events: correctionsReassert.length,
          },
        }).catch(() => {});
      }
    }

    // Plan parity (audit C5): SessionStart injects the FULL active-plan view for
    // boot/rehydration, exactly like the claude-code hook.
    // `undefined` from resolveActivePlanView MEANS "no active plan", so it cannot
    // double as the budget-cut fallback — the two must stay distinguishable.
    let planRead: { ok: boolean; view: Awaited<ReturnType<typeof resolveActivePlanView>> } = {
      ok: true,
      view: undefined,
    };
    try {
      planRead = await withBudget(
        resolveActivePlanView(config.vaultPath).then((view) => ({ ok: true, view })),
        remainingMs(),
        { ok: false, view: undefined },
      );
    } catch (error) {
      // hooks-PL-5: a failed plan resolution must not silently boot plan-less.
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_active_plan_error`,
        summary: `SessionStart: ${error instanceof Error ? error.message : String(error)}`,
      }).catch(() => {});
    }
    const activePlan = planRead.view;

    // Sections the budget cut. Named so a degraded boot is legible as degraded
    // rather than as an agent with nothing in its memory (hooks-PL-5). Computed
    // AFTER every budgeted read, so a read that runs late can still report itself.
    const degradedSections = [
      status === undefined ? "status" : undefined,
      tail === undefined ? "audit_tail" : undefined,
      inboxStatus === undefined ? "pending_learnings" : undefined,
      handoffRead.ok ? undefined : "handoff_context",
      planRead.ok ? undefined : "active_thread",
    ].filter((section): section is string => section !== undefined);

    const envelopeBody: Record<string, unknown> = {
      contract: MEMORY_CONTRACT,
      identity: {
        agent: config.agentId,
        workspace: workspaceId,
        vault: config.vaultPath,
        session_id: sessionId,
        // Unknown reads as not-ok, never as ok: `degraded.sections` below says
        // which of these the budget cut, so "false" is not read as "checked".
        daemon_ok: status?.socket.ok ?? false,
        afm_ok: status?.afm.ok ?? false,
        ...(config.runtime !== undefined ? { runtime: config.runtime } : {}),
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
      ...(tail !== undefined
        ? { audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]) }
        : {}),
    };

    if (correctionsReassert.length > 0) {
      envelopeBody.corrections_reassert = correctionsReassert;
    }

    if (degradedSections.length > 0) {
      // Say so IN the envelope: a boot the budget truncated must never be
      // indistinguishable from a boot that found nothing (hooks-PL-5).
      envelopeBody.degraded = {
        sections: degradedSections,
        budget_ms: budgetMs,
        note: "Boot exceeded the SessionStart budget (likely a cold daemon). The listed sections were NOT read — treat them as unknown, not empty, and call minni_recall explicitly if they matter.",
      };
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
      envelopeBody.active_thread = activePlan;
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
        daemon_ok: status?.socket.ok ?? false,
        afm_ok: status?.afm.ok ?? false,
        pending_inbox: inboxStatus?.totalPending ?? null,
        expired_handoffs: expiredHandoffs.length,
        handoff_context: handoffContext.length,
        workspace: workspaceId,
        corrections_reassert: correctionsReassert.length,
        budget_ms: budgetMs,
        ...(degradedSections.length > 0 ? { degraded_sections: degradedSections } : {}),
        // PENDING at audit time: the archive itself is deferred until the
        // envelope reaches the host, so these are what WILL settle on delivery,
        // not what already has.
        reassert_entries_pending_clear: reassertConsumed.length,
        reassert_tails_pending_defer: reassertDeferred.length,
      },
    });

    const nativeLayer1 =
      bootIdentity === "agent-context" && identityRead.ok && identityRead.data?.context
        ? truncateToTokenCharBudget(identityRead.data.context.trim(), budget)
        : "";
    return render(
      injectIntent("SessionStart", [nativeLayer1, envelope].filter(Boolean).join("\n\n")),
    );
  }

  async function handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput> {
    const prompt = asString(payload.prompt) || asString(payload.user_prompt);
    if (!prompt.trim()) {
      return render(noIntent);
    }

    const workspaceId = workspaceFor(payload);
    // rawSessionId (possibly empty) is the audit/recall-trace correlation id;
    // sessionId keeps the "session" fallback for envelope identity only — see
    // handleSessionStart's comment for why the two must not be conflated.
    const rawSessionId = asString(payload.session_id) || asString(payload.sessionId);
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
      return render(noIntent);
    }

    const threshold = recallPointerThreshold();
    // BUDGET (parity with the claude-code hook): every host kills a prompt-time
    // hook and DISCARDS its output, so an overrun costs the whole turn's
    // injection silently. The budget is clamped to this platform's manifest
    // timeout (`promptHookTimeoutMs`) so it always finishes inside the deadline
    // that will actually kill it — gemini's 10s PreInvocation is the tightest.
    const budgetMs = effectiveHookBudgetMs(config.promptHookTimeoutMs);
    const deadline = Date.now() + budgetMs;
    const remainingMs = (): number => Math.max(0, deadline - Date.now());
    const [vaultResults, recall] = await Promise.all([
      withBudget(searchVaultNotes(config.vaultPath, prompt, 6), remainingMs(), []),
      withBudget(
        recallMemory({
          query: prompt,
          limit: 6,
          agentId: config.agentId,
          workspaceId,
          // The load-bearing deadline: it destroys the socket, which is what
          // lets the hook PROCESS exit. Racing a promise alone cannot do that.
          timeoutMs: remainingMs(),
          ...(rawSessionId ? { sessionId: rawSessionId } : {}),
        }),
        remainingMs(),
        { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR },
      ),
    ]);
    // "Ran out of time" is NOT "there is nothing" — only a real answer may drive
    // the weak-turn path below, which CLEARS recall-state.
    const daemonTimedOut = !recall.ok && recall.error === JSON_RPC_TIMEOUT_ERROR;
    // s5 strength gate: emit the light pointer + recall-state file ONLY when the
    // top recall strength clears the threshold; otherwise inject nothing and
    // clear any stale state left by a previous strong turn.
    const strong = extractStrongRecall(
      recall.ok ? recall.data : undefined,
      vaultResults,
      threshold,
    );
    let recallStateFile: string | undefined;
    let stalePointer: string | undefined;
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
    } else if (daemonTimedOut) {
      // DEGRADED, not weak. Keep the existing state file — clearing it would
      // destroy the s6 guard's only pointer on exactly the turns the daemon
      // cannot rebuild it — and surface it clearly flagged.
      stalePointer = buildStaleRecallPointer(
        await readRecallState(config.vaultPath).catch(() => null),
      );
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

    // Nothing salient to inject this turn: no strong recall, no stale fallback
    // pointer AND no active plan.
    if (!strong && stalePointer === undefined && planRef === undefined) {
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_user_prompt_submit`,
        summary: prompt.slice(0, 120),
        details: {
          intent: intent.action,
          vault_matches: vaultResults.map((result) => result.relativePath),
          daemon_ok: recall.ok,
          daemon_timed_out: daemonTimedOut,
          task_signature: signature,
          workspace: workspaceId,
          recall_strong: false,
          // RAW id only — omit rather than stamp the synthetic "session"
          // fallback, so unlabeled turns don't conflate into one audit thread.
          ...(rawSessionId ? { session_id: rawSessionId } : {}),
        },
      });
      return render(noIntent);
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
    } else if (stalePointer !== undefined) {
      envelopeBody.recall_pointer = stalePointer;
      envelopeBody.recall_stale = true;
      envelopeBody.recall_state = recallStatePath(config.vaultPath);
    }
    if (daemonTimedOut) {
      // A degraded turn must never be indistinguishable from a clean one: the
      // agent should read absent recall as "not looked up", not "nothing found".
      envelopeBody.degraded = {
        daemon_recall: "timed_out",
        budget_ms: budgetMs,
        note: "Daemon recall exceeded the hook budget (likely a cold daemon loading models). Memory was NOT consulted this turn — call minni_recall explicitly if it matters.",
      };
    }

    // Plan parity (audit C5): per-turn injection is a compact plan POINTER, not
    // the full plan — same budget discipline as the claude-code hook (Option C).
    // (planRef !== undefined iff activePlan !== undefined; guard on activePlan so
    // the compiler narrows it for compactPlanPointer.)
    if (activePlan !== undefined) {
      envelopeBody.active_thread_ref = compactPlanPointer(activePlan);
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
        daemon_timed_out: daemonTimedOut,
        recall_stale: stalePointer !== undefined,
        task_signature: signature,
        workspace: workspaceId,
        recall_strong: Boolean(strong),
        // RAW id only — see the weak-path comment above.
        ...(rawSessionId ? { session_id: rawSessionId } : {}),
      },
    });

    return render(injectIntent("UserPromptSubmit", envelope));
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
    // The PreToolUse payload may carry a session_id; stamp it so the Stop
    // receipt can attribute this guard nudge. Only add it when actually present
    // — never invent a "session" placeholder on this path.
    const guardSessionId = asString(payload.session_id) || asString(payload.sessionId);
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
        ...(guardSessionId ? { session_id: guardSessionId } : {}),
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

    // PreCompact can inject on NO platform: Claude Code omits it from the
    // hookSpecificOutput union, and Codex's schema is additionalProperties:false
    // so the envelope voids the entire output. The handoff written above is the
    // real payload; the wire records the drop rather than pretending it landed.
    return render(injectIntent("PreCompact", envelope));
  }

  // Post-compaction summary delivery (plan-3e5a410b9ab6f715). Platforms whose
  // bridge can read the finished summary back (Kilo: session.compacted event +
  // SDK read-back in the native plugin) deliver it here; the hook stores the
  // RAW text as a compact_summary inbox file and the daemon's AFM-loop
  // compact_distillation pass reviews/organizes it into governance-gated
  // candidates. Injects nothing — there is no model turn to inject into.
  async function handleCompactSummary(payload: Record<string, unknown>): Promise<HookOutput> {
    await ensureVault(config.vaultPath);
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    await harvestSummaryText(
      {
        vaultPath: config.vaultPath,
        workspaceId: workspaceFor(payload),
        auditPrefix: config.auditPrefix,
        platform: config.agentId,
      },
      {
        summaryText: asString(payload.summary_text),
        summaryId: asString(payload.summary_id),
        sessionId,
        timestamp: asString(payload.summary_timestamp) || undefined,
      },
    );
    return { continue: true };
  }

  // Stop lives in the shared handleStopCore (above) so the governance posture
  // has exactly one implementation across all five entrypoints — see its
  // doc comment for why hook.ts routes here too.
  async function handleStop(payload: Record<string, unknown>): Promise<HookOutput> {
    const result = await handleStopCore(config, payload, prepareOutcomeFn);
    if (result.systemMessage) {
      return render(noteIntent("Stop", result.systemMessage));
    }
    return render(noIntent);
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
      case "CompactSummary":
        return handleCompactSummary(payload);
      case "Stop":
        return handleStop(payload);
      default:
        return render(noIntent);
    }
  }

  return {
    handleSessionStart,
    handleUserPromptSubmit,
    handlePreCompact,
    handleCompactSummary,
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

  const wire = config.wire ?? wireFor(config.agentId);
  if (wire.shouldHandle && !wire.shouldHandle(event, payload)) {
    // A duplicate firing of an event the platform emits more than once per
    // logical occurrence -- running it again would double-count the outcome.
    emit(wire.noop());
    return;
  }

  try {
    const handlers = createHookHandlers(config);
    const output = await handlers.dispatch(event, payload);
    // emitAndCommit, not emit: work the handler deferred (archiving a consumed
    // correction) runs only once this output has actually reached the host.
    await emitAndCommit(output, {
      vaultPath: config.vaultPath,
      auditPrefix: config.auditPrefix,
      event,
    });
    // Delivery is settled; nothing left to wait for. See exitAfterDelivery.
    exitAfterDelivery();
  } catch (error) {
    // Nothing was delivered, so nothing the handler deferred may be committed.
    discardDeliveryCommits();
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

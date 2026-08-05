// Shared hook HANDLERS (review panel, plan-parity follow-up): codex-hook.ts,
// grok-hook.ts and kilocode-hook.ts were ~360-line near-clones whose four
// handler bodies differed ONLY in config constants and a few flagged
// behaviors. hook-utils.ts holds the protocol leaf helpers; this module holds
// the stateful handler logic, parameterized by a typed per-agent config, so
// future changes (evidence envelope format, plan injection, inbox drain
// logic) have ONE maintenance surface instead of four. #283: hook.ts
// (claude-code) migrated onto this factory too — it was the last platform
// carrying its own copy of the four handler bodies, which is what let PR
// #282's Layer 1 shelf change land in hook.ts:356 and hook-handlers.ts:679
// separately instead of once. Claude-specific behavior (identity-recall boot
// identity, the lifecycle representation, boot-time handoff acking) lives
// behind opt-in `AgentHookConfig` knobs — see their doc comments — rather
// than a forked handler set.
//
// Handlers here return INTENT, not wire shapes: what reaches the model is
// decided per platform by hook-platform.ts. Emitting Claude Code's envelope
// everywhere is what silently voided Codex's PreCompact output and discarded
// Grok Build's memory outright.
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
  BRIDGE_FAILURE_EVENT,
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
  failAndExit,
  markOutputDropped,
} from "./hook-delivery.js";
import { harvestCompactSummary, harvestSummaryText } from "./compact-harvest.js";
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
  recallGuardMode as resolveRecallGuardModeFromEnv,
} from "./recall-guard.js";
import type {
  PreToolUseDecisionOutput,
  RecallGuardMode,
} from "./recall-guard.js";
import {
  BOOT_RECALL_LAYERS,
  ackHandoff,
  buildBootRecallSlice,
  buildStatusReport,
  extractIdentityBody,
  extractLearningsSection,
  truncateToTokenCharBudget,
  fetchStaleBeliefEvents,
  formatRecall,
  listPendingHandoffs,
  readAgentContext,
  JSON_RPC_TIMEOUT_ERROR,
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
  expiredHandoffsBody,
  formatSessionReceiptLine,
  layer1ShelfBody,
  readInboxStatus,
  readLayer1Shelf,
  parkUndeliverableInboxEntries,
  readParkedUndeliverablePending,
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
  /**
   * #283 migration knob (claude-code only today): the standing 4-surface
   * lifecycle line (`prepare_task -> prepare_outcome -> plan -> learn`) rides
   * SessionStart's envelope (mode-gated by `MINNI_LIFECYCLE_NUDGE_MODE`), and
   * UserPromptSubmit computes/persists a once-per-session emphasis surface
   * and injects a lifecycle-only envelope on the two turns that would
   * otherwise be a bare no-op (write-intent gate, nothing-salient gate) so
   * the line survives every turn. Omit (default false) to leave a platform's
   * envelope untouched by this feature — it was never on for any agent but
   * claude-code, and this knob keeps it that way rather than threading a new
   * per-platform branch through every other hook.
   */
  lifecycleEnabled?: boolean;
  /**
   * #283 migration knob (claude-code only today): SessionStart lists this
   * agent's pending handoff leases and acks each one (stopping when the boot
   * budget runs out — an unacked lease just stays pending next boot), adding
   * `handoff_acks` to the envelope. A failed LIST degrades `handoff_leases`;
   * any lease left unacked (budget-cut or a failed ack) degrades
   * `handoff_acks`. Omit (default false) — no other platform's SessionStart
   * consults or acks pending handoffs today.
   */
  ackPendingHandoffsAtBoot?: boolean;
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
  handlePostCompact(payload: Record<string, unknown>): Promise<HookOutput>;
  handleCompactSummary(payload: Record<string, unknown>): Promise<HookOutput>;
  handleStop(payload: Record<string, unknown>): Promise<HookOutput>;
  dispatch(
    event: string,
    payload: Record<string, unknown>,
  ): Promise<HookOutput | PreToolUseDecisionOutput>;
}

/**
 * Path-only compact harvest (no `source`, non-empty transcript path) is
 * **opt-in**. Default off so a host that always attaches a path and never
 * sends `source` (agy/gemini shape, and any unverified host) does not
 * rediscover the every-cold-boot tail-read tax.
 *
 * Empty by design until a live transcript is verified Claude-shaped
 * (`isCompactSummary: true`). Cursor's contract explicitly warns not to mine
 * `transcript_path` blind; codex / grok-build shapes are also unverified for
 * this extractor. Universal `source === "compact" | "resume"` still harvests
 * on every runtime. Matched against `config.runtime ?? config.agentId`.
 */
const PATH_ONLY_COMPACT_HARVEST_ALLOW = new Set<string>([]);

/** Wait cap for path-only residual SessionStart harvest (ms). That residual is
 * almost always a miss and must not burn the boot budget before
 * identity/corrections RPCs. `withBudget` only bounds wait time; it does not
 * cancel work. compact|resume is NOT wait-capped — see harvest block below. */
const COMPACT_HARVEST_PATH_ONLY_BUDGET_MS = 250;

/** True when path-only (no `source`) SessionStart may start a tail-read. */
export function isPathOnlyCompactHarvestAllowed(platform: string | undefined): boolean {
  const id = (platform ?? "").trim().toLowerCase();
  return id.length > 0 && PATH_ONLY_COMPACT_HARVEST_ALLOW.has(id);
}

/**
 * #283 (`config.lifecycleEnabled`): build this turn's lifecycle
 * representation fields. The PERSISTENT line is always present in "soft"
 * mode so the 4 surfaces stay in view; the situational `lifecycle_focus` is
 * added when the prompt's ambition intent maps to a surface NOT yet
 * emphasized this session. "off" mode returns `{}` — fully silent. The
 * caller must persist any returned `emphasizedSurface` so the focus fires at
 * most once per surface per session. Representation only — never a
 * permission decision. Pure / agent-agnostic (reads the env mode directly),
 * so it lives at module scope rather than inside `createHookHandlers`.
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
    // start, so any setup done first (vault creation + compaction harvest) is
    // time already spent against it.
    const budgetMs = effectiveHookBudgetMs(config.sessionStartHookTimeoutMs);
    const deadline = Date.now() + budgetMs;
    const remainingMs = (): number => Math.max(0, deadline - Date.now());
    const rpcTimedOut = { ok: false as const, error: JSON_RPC_TIMEOUT_ERROR };
    await ensureVault(config.vaultPath);

    // Compaction-summary harvest backstop (P3 / #227). Claude Code's primary
    // path is PostCompact in hook.ts; Kilo delivers via CompactSummary (its
    // bridge SessionStart supplies neither transcript_path nor source — so
    // this backstop is dead for real Kilo boots). Platforms that share this
    // handler (codex, grok-build, cursor, gemini) previously had no harvest
    // path at all — and on those hosts this SessionStart path is the ONLY
    // capture write (no PostCompact).
    //
    // Gate:
    //  1. compact|resume `source` — Claude parity (any runtime). FULLY
    //     awaited: withBudget races rather than cancels, and exitAfterDelivery
    //     then kills the process, so a budget-cut harvest is permanent loss of
    //     the only write path. Match hook.ts: bare await.
    //  2. path-only residual — only when `source` is absent entirely AND the
    //     runtime is on PATH_ONLY_COMPACT_HARVEST_ALLOW (opt-in). Default
    //     off: hosts that always attach a path and never send `source`
    //     (agy/gemini) would otherwise tax every cold boot for a guaranteed
    //     no_summary_found. Wait-capped only (still not cancel-safe).
    // Content-hash dedup keeps dual delivery safe. Fail-open, raw only —
    // daemon compact_distillation organizes later.
    const bootSource = asString(payload.source);
    const transcriptPath =
      asString(payload.transcript_path) || asString(payload.transcriptPath);
    const platform = config.runtime ?? config.agentId;
    const isCompactOrResume = bootSource === "compact" || bootSource === "resume";
    const pathOnlyAllowed =
      !bootSource &&
      Boolean(transcriptPath) &&
      isPathOnlyCompactHarvestAllowed(platform);
    if (isCompactOrResume) {
      // Do not start when the boot budget is already spent — a late start is
      // more likely to overrun the harness than to land a durable write. Once
      // started, await fully (never withBudget-race this path).
      if (remainingMs() > 0) {
        await harvestCompactSummary(
          {
            vaultPath: config.vaultPath,
            workspaceId,
            auditPrefix: config.auditPrefix,
            platform,
          },
          {
            transcriptPath,
            sessionId,
          },
        );
      }
    } else if (pathOnlyAllowed && remainingMs() > 0) {
      // Residual is almost always a miss → tight wait-cap so it cannot exhaust
      // identity/corrections RPCs. withBudget only bounds wait time; it does
      // not cancel the tail-read. Acceptable for opt-in residual; not for the
      // only write path above.
      await withBudget(
        harvestCompactSummary(
          {
            vaultPath: config.vaultPath,
            workspaceId,
            auditPrefix: config.auditPrefix,
            platform,
          },
          {
            transcriptPath,
            sessionId,
          },
        ),
        Math.min(remainingMs(), COMPACT_HARVEST_PATH_ONLY_BUDGET_MS),
        { harvested: false, reason: "harvest_error" },
      );
    }

    // #283 (config.lifecycleEnabled): reset the once-per-session lifecycle
    // emphasis on a fresh session, so the situational focus can fire again
    // this session. Best-effort — a state-write failure must not break boot.
    if (config.lifecycleEnabled) {
      await writeLifecycleState(config.vaultPath, {
        session_id: sessionId,
        emphasized: [],
      }).catch(() => {});
    }

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

    // #283 (config.ackPendingHandoffsAtBoot): list this agent's pending
    // handoff leases and ack each one at boot. Sequential per-lease RPCs,
    // deliberately: unbounded in the number of pending handoffs, so it is
    // the loop most able to blow the deadline. Stop acking when the budget
    // is gone — an unacked lease is still pending next boot; a killed hook
    // is not. A FAILED ack is an unacked lease too — counting only the
    // budget-skip path would report a timed-out or refused ack as clean.
    let pendingHandoffs: { ok: boolean; data?: unknown; error?: unknown } = {
      ok: true,
      data: { handoffs: [] },
    };
    const ackedLeases: string[] = [];
    let unackedLeases = 0;
    if (config.ackPendingHandoffsAtBoot) {
      pendingHandoffs = await withBudget(
        listPendingHandoffs({ agentId: config.agentId, timeoutMs: remainingMs() }),
        remainingMs(),
        rpcTimedOut,
      );
      const pendingHandoffData =
        pendingHandoffs.ok && pendingHandoffs.data
          ? (pendingHandoffs.data as {
              handoffs?: Array<{ lease_id?: string; leaseId?: string }>;
            })
          : { handoffs: [] };
      for (const handoff of pendingHandoffData.handoffs ?? []) {
        const leaseId = handoff.lease_id ?? handoff.leaseId;
        if (!leaseId) continue;
        if (remainingMs() <= 0) {
          unackedLeases += 1;
          continue;
        }
        const ack = await withBudget(
          ackHandoff({
            leaseId,
            status: "accepted",
            agentId: config.agentId,
            timeoutMs: remainingMs(),
          }),
          remainingMs(),
          rpcTimedOut,
        );
        if (ack.ok) ackedLeases.push(leaseId);
        else unackedLeases += 1;
      }
    }

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
    // Layer 1 shelf inline (operator scar: an envelope carrying only identity
    // keys + a pointer meant every agent re-hit scars already recorded in
    // layer1/core.md). SessionStart ONLY — UserPromptSubmit's envelope stays
    // lean, see handleUserPromptSubmit below, which does not call this.
    // Unbudgeted for the same reason as reassertPending above: cheap, bounded
    // local FS (stat + a capped read, never unbounded I/O), no RPC — not the
    // class of read the boot budget exists to bound.
    const layer1Shelf = await readLayer1Shelf(config.vaultPath);
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
    //
    // The three outcomes are counted separately for the audit below: settling
    // eagerly, waiting on delivery, and parked as undeliverable are different
    // facts, and one "pending" number covering all three would misreport two of
    // them. In a change about audit honesty that matters.
    const emptyPaths = reassertConsumed.filter(
      (filePath) => !reassertContributing.includes(filePath),
    );
    let reassertClearedEager = 0;
    let reassertPendingClear = 0;
    let reassertParked = 0;
    // Counted from the branch that REGISTERS the rewrite, not derived from
    // reassertPendingClear: a single file carrying MAX+N valid events defers a
    // tail while contributing no consumed path, so a count gated on
    // "contributing > 0" reported zero deferred tails while one was pending.
    let reassertTailsPendingDefer = 0;
    if (emptyPaths.length > 0) {
      reassertClearedEager = emptyPaths.length;
      await settleReassertedInboxEntries(config.vaultPath, {
        consumedPaths: emptyPaths,
        deferredTails: [],
      });
    }
    if (reassertContributing.length > 0 || reassertDeferred.length > 0) {
      if (canInject(wire, "SessionStart")) {
        reassertPendingClear = reassertContributing.length;
        reassertTailsPendingDefer = reassertDeferred.length;
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
        // Delivery is issue #253: handleStop reads inbox/.undeliverable and
        // injects at Stop (the only injectable event on such a wire). Parking
        // here is the bound; Stop is the route. Do not archive until Stop
        // confirms delivery — same deliver-before-consume rule as boot.
        const parkPaths = [
          ...reassertContributing,
          ...reassertDeferred.map((tail) => tail.filePath),
        ];
        const parked = await parkUndeliverableInboxEntries(parkPaths);
        reassertParked = parked.length;
        await recordAudit(config.vaultPath, {
          tool: `${config.auditPrefix}_undeliverable_on_wire`,
          summary: `SessionStart: ${wire.id} cannot inject at SessionStart; parked ${parked.length} correction entr${
            parked.length === 1 ? "y" : "ies"
          } to inbox/.undeliverable for Stop delivery (issue #253)`,
          details: {
            wire: wire.id,
            parked: parked.length,
            events: correctionsReassert.length,
            delivery: "stop_routed",
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
      resolveActivePlanView(config.vaultPath).then((view) => ({ ok: true, view })),
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
      // config.ackPendingHandoffsAtBoot only: a no-op (always undefined) on
      // every other platform, since unackedLeases stays 0 there.
      config.ackPendingHandoffsAtBoot && unackedLeases > 0 ? "handoff_acks" : undefined,
      // Also degraded when the PARENT read was cut: handoffContext is resolved
      // from inboxStatus.entries, so a cut inbox read makes it vacuously empty.
      // ok:true over an empty set the hook never actually had inputs for is the
      // same false all-clear this section exists to prevent.
      handoffRead.ok && inboxStatus !== undefined ? undefined : "handoff_context",
      planRead.ok ? undefined : "active_thread",
      // config.ackPendingHandoffsAtBoot only: a no-op (pendingHandoffs.ok
      // stays true) on every other platform.
      config.ackPendingHandoffsAtBoot && !pendingHandoffs.ok ? "handoff_leases" : undefined,
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
      // config.ackPendingHandoffsAtBoot only.
      ...(config.ackPendingHandoffsAtBoot ? { handoff_acks: ackedLeases } : {}),
      // hooks-PL-1: discriminated stale-belief payload (matched /
      // checked_no_match from the daemon; explicit status:"error" here so
      // events:[] can never masquerade as "checked and clean").
      stale_beliefs:
        contradictions.ok && contradictions.data
          ? contradictions.data
          : { ok: false, status: "error", error: contradictions.error },
      recall:
        recall.ok && recall.data
          ? buildBootRecallSlice(recall.data, config.agentId)
          : { ok: false, error: recall.error },
      ...(tail !== undefined
        ? { audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]) }
        : {}),
      // Always present (H2: never a silently missing key) — ok:false carries
      // an "absent: <reason>" string, ok:true carries the content plus an
      // explicit truncation marker whenever the 8KB cap trimmed it (H5: no
      // silent truncation). See vault.ts readLayer1Shelf / layer1ShelfBody.
      layer1_shelf: layer1ShelfBody(layer1Shelf),
    };

    // #283 (config.lifecycleEnabled): the standing 4-surface lifecycle line
    // at boot so the agent sees the spine from session start (unless the
    // feature is off). No other platform's SessionStart carries this field.
    if (config.lifecycleEnabled && lifecycleNudgeMode() !== "off") {
      envelopeBody.lifecycle = MINNI_LIFECYCLE_LINE;
    }

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
        // The three settle outcomes, reported separately. "pending" counts ONLY
        // what is still registered with deferUntilDelivered — empties already
        // cleared and entries parked as undeliverable are not waiting on
        // anything, and folding them in here claimed archives that were never
        // going to happen.
        reassert_entries_cleared_eager: reassertClearedEager,
        reassert_entries_pending_clear: reassertPendingClear,
        reassert_entries_parked: reassertParked,
        reassert_tails_pending_defer: reassertTailsPendingDefer,
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

    // #283 (config.lifecycleEnabled): compute the lifecycle representation
    // once for this turn, BEFORE the early-return gates, so the persistent
    // line survives them. Read the once-per-session emphasis set and persist
    // any newly-emphasized surface. `lifecycleFields` stays `{}` — a no-op —
    // on every other platform.
    let lifecycleFields: { lifecycle?: string; lifecycle_focus?: string } = {};
    if (config.lifecycleEnabled) {
      const lifecycleStatePrev = await readLifecycleState(config.vaultPath);
      const emphasizedSurfaces = new Set(lifecycleStatePrev?.emphasized ?? []);
      const computed = lifecycleFieldsFor(prompt, emphasizedSurfaces);
      lifecycleFields = computed.fields;
      if (computed.emphasizedSurface) {
        emphasizedSurfaces.add(computed.emphasizedSurface);
        await writeLifecycleState(config.vaultPath, {
          session_id: lifecycleStatePrev?.session_id ?? "session",
          emphasized: [...emphasizedSurfaces],
        }).catch(() => {});
      }
    }

    // #283: emit an envelope carrying the lifecycle representation only —
    // used at this handler's two early-returns (write-intent and
    // nothing-salient gates) so the 4 surfaces survive turns that would
    // otherwise inject nothing. Degrades to a plain no-op when
    // `lifecycleFields` is empty (lifecycleEnabled is off, feature mode is
    // "off", or no fields were computed this turn) — safe to call
    // unconditionally on every platform.
    const lifecycleOnlyOutput = (): Promise<HookOutput> => {
      if (lifecycleFields.lifecycle === undefined && lifecycleFields.lifecycle_focus === undefined) {
        return render(noIntent);
      }
      const envelope = wrapEnvelope({
        event: "UserPromptSubmit",
        agent: config.agentId,
        body: {
          identity: {
            agent: config.agentId,
            workspace: workspaceId,
            task_signature: signature,
          },
          ...lifecycleFields,
        },
      });
      return render(injectIntent("UserPromptSubmit", envelope));
    };

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
      // #283: the persistent lifecycle visibility must survive this
      // write-intent early-return.
      return lifecycleOnlyOutput();
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
    const [vaultResultsRaw, recall] = await Promise.all([
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
    // SEC-006 (found during #283 review): searchVaultNotes returns every
    // match regardless of privacy level — only `filterSafeVaultResults` (not
    // yet applied here) keeps `local-only`/`private` notes and privacy-
    // heuristic escalations (secrets/tokens/paths, see task.ts
    // privacyForSource) out of the model-facing recall pointer, the
    // persisted recall-state top_hits, and the PreToolUse guard's deny-
    // reason text (all three read from `strong`/`vaultResults` below). This
    // filter existed in claude-code's pre-#283 hook.ts but was never ported
    // here when codex/grok-build/cursor/gemini/kilocode were extracted onto
    // this factory — every platform sharing this handler has been leaking
    // non-safe notes into UserPromptSubmit. Fixed once, for all six.
    const vaultResults = filterSafeVaultResults(vaultResultsRaw);
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
      // #283: even with nothing salient (no strong recall, no active plan)
      // the persistent lifecycle line still rides this turn's envelope.
      return lifecycleOnlyOutput();
    }

    const envelopeBody: Record<string, unknown> = {
      // #283 (config.lifecycleEnabled): persistent lifecycle line (+
      // situational focus) rides the salient turn too. `{}` on every other
      // platform.
      ...lifecycleFields,
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
        top_score: strong?.topScore,
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
    // Same provenance stamp as SessionStart harvest — dual delivery paths
    // must not disagree on `platform` when runtime and agentId differ.
    await harvestSummaryText(
      {
        vaultPath: config.vaultPath,
        workspaceId: workspaceFor(payload),
        auditPrefix: config.auditPrefix,
        platform: config.runtime ?? config.agentId,
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

  // #283: primary compaction-summary delivery for platforms whose native
  // hook system fires a dedicated post-compaction event with the finished
  // summary attached directly (Claude Code's `PostCompact`, which hands it
  // over as `compact_summary` — documented 2026-07-29). Distinct from
  // `handleCompactSummary` above (Kilo's bridge-specific `CompactSummary`
  // event, `summary_text`/`summary_id` fields) because the two are genuinely
  // different wire-level triggers with different payload shapes that both
  // funnel into the same `harvestSummaryText` sink. The SessionStart
  // tail-read backstop above stays as the fallback for what this can miss
  // (hook not yet registered when compaction ran, older CLI, crash between
  // compaction and hook); the shared content-hash dedup keeps the two paths
  // from double-harvesting. Injects nothing — there is no model turn to
  // inject into. Raw storage only; the daemon's compact_distillation pass
  // does the AFM review on its own timer.
  async function handlePostCompact(payload: Record<string, unknown>): Promise<HookOutput> {
    await ensureVault(config.vaultPath);
    const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
    await harvestSummaryText(
      {
        vaultPath: config.vaultPath,
        workspaceId: workspaceFor(payload),
        auditPrefix: config.auditPrefix,
        platform: config.runtime ?? config.agentId,
      },
      {
        summaryText: asString(payload.compact_summary),
        sessionId,
      },
    );
    return { continue: true };
  }

  // Stop lives in the shared handleStopCore (above) so the governance posture
  // has exactly one implementation across all five entrypoints — see its
  // doc comment for why hook.ts routes here too.
  //
  // Issue #253: after the stop breadcrumb / candidate write, deliver any
  // corrections SessionStart parked under inbox/.undeliverable (wires that
  // cannot inject at boot). Ordering is intentional:
  //   1. handleStopCore first — inbox write / receipt is independent of
  //      correction delivery and must not be gated on the park dir;
  //   2. then inject corrections_reassert at Stop when the wire can carry it.
  // "Re-assert at end of turn" is the honest semantics on a platform whose
  // only injectable event is Stop: the model sees the correction before the
  // next turn begins, not at session boot. Prefer acting on them next turn.
  async function handleStop(payload: Record<string, unknown>): Promise<HookOutput> {
    const result = await handleStopCore(config, payload, prepareOutcomeFn);

    let correctionsEnvelope = "";
    // Only attempt when Stop can actually inject — parking only happens when
    // SessionStart cannot, and today that is Grok Build; probing canInject
    // keeps the route honest if another wire gains a structural SessionStart
    // drop later.
    if (canInject(wire, "Stop")) {
      // Unbudgeted local FS: same as SessionStart reassert. Absence loses data.
      const parkedPending = await readParkedUndeliverablePending(config.vaultPath, 3);
      if (parkedPending.length > 0) {
        const {
          events: parkedEvents,
          consumedPaths: parkedConsumed,
          contributingPaths: parkedContributing,
          deferredTails: parkedDeferred,
        } = collectCorrectionsReassert(parkedPending);

        // Empty parked stashes (should not be parked today — SessionStart
        // settles empties eagerly) still clear without waiting on delivery.
        const parkedEmpty = parkedConsumed.filter(
          (filePath) => !parkedContributing.includes(filePath),
        );
        if (parkedEmpty.length > 0) {
          await settleReassertedInboxEntries(config.vaultPath, {
            consumedPaths: parkedEmpty,
            deferredTails: [],
          });
        }

        if (parkedEvents.length > 0) {
          correctionsEnvelope = wrapEnvelope({
            event: "Stop",
            agent: config.agentId,
            budget: envelopeBudgetFor(config.contextWindow),
            body: {
              corrections_reassert: parkedEvents,
              // Named so operators / the model can tell this is not a boot
              // reassert: delivery was deferred from a structurally silent
              // SessionStart to the first injectable event.
              delivery: "stop_routed",
              note:
                "Corrections parked at SessionStart (this platform cannot inject there) are re-asserted at Stop — act on them before the next turn.",
            },
          });

          // Deliver-before-consume: archive only after emitAndCommit confirms
          // the inject reached the host. A failed Stop leaves the park intact
          // for the next end_turn (at-least-once).
          if (parkedContributing.length > 0 || parkedDeferred.length > 0) {
            deferUntilDelivered(() =>
              settleReassertedInboxEntries(config.vaultPath, {
                consumedPaths: parkedContributing,
                deferredTails: parkedDeferred,
              }),
            );
          }

          await recordAudit(config.vaultPath, {
            tool: `${config.auditPrefix}_stop_corrections_reassert`,
            summary: `Stop: delivering ${parkedEvents.length} SessionStart-parked correction(s) (issue #253)`,
            details: {
              wire: wire.id,
              corrections_reassert: parkedEvents.length,
              reassert_entries_pending_clear: parkedContributing.length,
              reassert_tails_pending_defer: parkedDeferred.length,
              delivery: "stop_routed",
            },
          }).catch(() => {});
        }
      }
    }

    if (correctionsEnvelope) {
      // Inject is the model-facing channel; append any stop note (receipt /
      // candidates CTA) so both land in one Stop output. Prefer inject over
      // note alone — on Grok both channels work at Stop, but inject is what
      // carries structured memory.
      const text = [correctionsEnvelope, result.systemMessage]
        .filter((part): part is string => Boolean(part && part.trim()))
        .join("\n\n");
      return render(injectIntent("Stop", text));
    }
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
      // #283 / #227: primary compaction-summary delivery for a platform
      // whose own hook system fires a dedicated post-compaction event
      // (Claude Code's PostCompact). Harmless to route unconditionally —
      // no other platform's manifest declares this event today, so this
      // case simply never fires for them.
      case "PostCompact":
        return handlePostCompact(payload);
      case "CompactSummary":
        return handleCompactSummary(payload);
      case "Stop":
        return handleStop(payload);
      default:
        // P4 (#228): `render(noIntent)` reports a clean, successful no-op —
        // which is honest for "ran fine, nothing to say" but a lie for "this
        // event passed the VALID_EVENTS gate and then found no handler".
        // Record it, then continue as before.
        await recordUnroutedEvent(config.vaultPath, config.auditPrefix, event);
        return render(noIntent);
    }
  }

  return {
    handleSessionStart,
    handleUserPromptSubmit,
    handlePreCompact,
    handlePostCompact,
    handleCompactSummary,
    handleStop,
    handlePreToolUse,
    dispatch,
  };
}

/**
 * P4 (#228): record an event that reached the hook and was not routed.
 *
 * Shares the `_intent_dropped` tool name with the wire-drop path in `render`
 * so every "the hook was reached and nothing was carried" case is countable
 * from one place, and buckets by event for the same reason that one does — a
 * throttle keyed on the bare tool name would collapse two different events'
 * drops into one record and report the second as written.
 *
 * Best-effort: losing the audit record must never also lose the event.
 */
export async function recordUnroutedEvent(
  vaultPath: string,
  auditPrefix: string,
  event: string,
): Promise<void> {
  try {
    await recordAudit(vaultPath, {
      tool: `${auditPrefix}_intent_dropped`,
      summary: `${event || "(empty)"}: event not routed by this hook (no handler)`,
      throttleKey: `${auditPrefix}_intent_dropped__unrouted__${event}`,
    });
  } catch {
    // Audit unavailable. The drop is still correct behavior.
  }
}

/**
 * P6 (#228): a native bridge (Kilo) has no vault handle of its own — it
 * reaches the vault only by spawning this hook. This is that channel: it
 * carries a bridge failure into the audit log so a persistently failing
 * bridge is distinguishable from an idle one. It is a diagnostic, not an
 * envelope event, and never reaches a handler.
 *
 * Round 7 (PR #260): the EXIT CODE is the delivery signal. The bridge
 * restores its coalesced eviction counts only when this child errors or
 * exits non-zero — so an audit that did not land must not exit clean, or
 * the parent clears counts that were never written anywhere durable.
 */
async function runBridgeFailureDiagnostic(
  config: AgentHookConfig,
  payload: Record<string, unknown>,
): Promise<never> {
  const failedEvent = asString(payload.failed_event) || "(unknown)";
  const bridge = asString(payload.bridge) || "(unknown)";
  // Round 9 (PR #260): storm suppress carries counts forward and flushes on
  // settle — name the dark interval so the audit row is not a silent single.
  const suppressed = payload.suppressed_since_last_report;
  const coalesced = payload.coalesced_count;
  const extras: string[] = [];
  if (typeof coalesced === "number" && coalesced > 1) {
    extras.push(`coalesced=${coalesced}`);
  }
  if (typeof suppressed === "number" && suppressed > 0) {
    extras.push(`suppressed_since_last_report=${suppressed}`);
  }
  const suffix = extras.length ? ` [${extras.join(", ")}]` : "";
  try {
    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_bridge_failure`,
      summary: `${bridge} bridge: ${failedEvent} failed: ${asString(payload.error)}${suffix}`,
      // Bucket per failed event, for the same reason _intent_dropped does:
      // one throttle across all of them would hide every failure after the
      // first and report the bridge as healthier than it is.
      throttleKey: `${config.auditPrefix}_bridge_failure__${failedEvent}`,
    });
  } catch {
    // Round 26: delivery is the EXIT CODE. Parent treats close(null)/non-zero
    // as undelivered. Setting exitCode and returning left the event loop
    // alive until SIGKILL → close(null) after a successful audit, reopening
    // P6 as "never delivered" after a durable write. Force exit.
    try {
      emit({ continue: true });
    } catch {
      /* best-effort envelope */
    }
    process.exit(1);
  }
  emit({ continue: true });
  // Same "delivery is exit" contract as every other settled hook path.
  exitAfterDelivery();
}

export async function runHookMain(config: AgentHookConfig): Promise<void> {
  // Round 7/26 (PR #260): the bridge diagnostic is routed BEFORE the
  // hooksEnabled gate — disabling memory hooks must not also disable the one
  // surface that says the bridge is broken. The early return here used to
  // exit 0 first, which the parent read as "audit delivered".
  //
  // Argv form is the Kilo spawn contract. Payload-only form
  // (hook_event_name: BridgeFailure, no argv) is the same diagnostic
  // channel and must also bypass hooksEnabled — read stdin once, then route
  // both before any hooks-disabled early return.
  const eventArg = (process.argv[2] ?? "").trim();
  if (eventArg === BRIDGE_FAILURE_EVENT) {
    const payload = (await readStdin()) as Record<string, unknown>;
    await runBridgeFailureDiagnostic(config, payload);
  }

  const payload = (await readStdin()) as Record<string, unknown>;
  const eventFromPayload = asString(payload.hook_event_name);
  if ((eventFromPayload || "").trim() === BRIDGE_FAILURE_EVENT) {
    await runBridgeFailureDiagnostic(config, payload);
  }

  if (!config.hooksEnabled) {
    emit({ continue: true });
    return;
  }

  const event = (eventArg || eventFromPayload || "").trim();
  // PreToolUse is dispatched here too but is NOT an EnvelopeEvent (its output is
  // the permissionDecision shape), so it is gated alongside VALID_EVENTS.
  if (event !== PRE_TOOL_USE_EVENT && !VALID_EVENTS.includes(event as EnvelopeEvent)) {
    // P4 (#228): this exited with a SUCCESS status and no marker of any kind,
    // so an event the platform declared and sent but Minni does not route was
    // indistinguishable from an event that was never sent at all. That is the
    // whole clean-continue swallow: the manifest says the event is wired, the
    // exit code says it went fine, and nothing anywhere records that it was
    // dropped on the floor. Count it before continuing.
    await recordUnroutedEvent(config.vaultPath, config.auditPrefix, event);
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
    // failAndExit, not emit: the degraded signal has to be FLUSHED before an
    // abandoned RPC handle can keep this process alive into the harness kill.
    await failAndExit({
      continue: true,
      systemMessage: `Minni hook degraded (${event}): ${message} — memory injection skipped this event; see vault log.md.`,
    });
  }
}

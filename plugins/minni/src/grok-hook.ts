import {
  GROK_CONTEXT_WINDOW,
  GROK_HOOKS_ENABLED,
  GROK_AGENT_ID,
  GROK_VAULT_PATH,
  GROK_WORKSPACE_ID,
} from "./config.js";
import {
  MEMORY_CONTRACT,
  envelopeBudgetFor,
  hashTaskSignature,
  wrapEnvelope,
} from "./agent_envelope.js";
import type { EnvelopeEvent } from "./agent_envelope.js";
import { routeMemoryIntent } from "./policy.js";
import {
  BOOT_RECALL_LAYERS,
  buildStatusReport,
  fetchStaleBeliefEvents,
  formatRecall,
  readAgentContext,
  recallMemory,
  subscribeContradictions,
} from "./sovereign.js";
import { extractScarTissue, prepareOutcome } from "./task.js";
import {
  auditTail,
  clearReassertedInboxEntries,
  collectCorrectionsReassert,
  ensureVault,
  readPendingInbox,
  recordAudit,
  resolveInboxHandoffContext,
  searchVaultNotes,
  writeInbox,
} from "./vault.js";
import type { VaultSearchResult } from "./vault.js";

interface HookOutput {
  continue?: boolean;
  hookSpecificOutput?: {
    hookEventName: EnvelopeEvent;
    additionalContext: string;
  };
  systemMessage?: string;
}

const VALID_EVENTS: ReadonlyArray<EnvelopeEvent> = [
  "SessionStart",
  "UserPromptSubmit",
  "PreCompact",
  "Stop",
];

async function readStdin(): Promise<unknown> {
  if (process.stdin.isTTY) return {};
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => {
      if (!data.trim()) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(data));
      } catch {
        resolve({});
      }
    });
    process.stdin.on("error", () => resolve({}));
  });
}

function emit(output: HookOutput): void {
  process.stdout.write(`${JSON.stringify(output)}\n`);
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function workspaceFromPayload(payload: Record<string, unknown>): string {
  return (
    asString(payload.workspace_id) ||
    asString(payload.workspaceId) ||
    asString(payload.cwd) ||
    asString(payload.working_directory) ||
    GROK_WORKSPACE_ID
  );
}

function vaultRecallToBody(vault: VaultSearchResult[]): unknown {
  return vault.slice(0, 6).map((result) => ({
    wikilink: result.wikilink,
    score: result.score,
    snippet: result.snippet.replace(/\s+/g, " ").slice(0, 240),
  }));
}

function withHookContext(event: EnvelopeEvent, additionalContext: string): HookOutput {
  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: event,
      additionalContext,
    },
  };
}

async function handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput> {
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  const workspaceId = workspaceFromPayload(payload);
  await ensureVault(GROK_VAULT_PATH);

  const [status, tail, identityRead, pending, contradictions, recall] = await Promise.all([
    buildStatusReport({ vaultPath: GROK_VAULT_PATH }),
    auditTail(GROK_VAULT_PATH, 5),
    readAgentContext({ agentId: GROK_AGENT_ID, limit: 8 }),
    readPendingInbox(GROK_VAULT_PATH, 3),
    // hooks-PL-2/PL-3: corrections to beliefs this agent read must re-surface
    // at boot (stale_beliefs), mirroring the Claude Code hook.
    subscribeContradictions({ agentId: GROK_AGENT_ID }),
    // recall-F1: boot recall must include the correction-bearing layers, not
    // just the identity shelf (readAgentContext alone is the 'read' surface;
    // the widened search is what lets knowledge-layer corrections rank in).
    recallMemory({
      query: `boot identity for ${workspaceId}`,
      layers: BOOT_RECALL_LAYERS,
      limit: 8,
      agentId: GROK_AGENT_ID,
      workspaceId,
    }),
  ]);
  const handoffContext = await resolveInboxHandoffContext(GROK_VAULT_PATH, pending);
  // Consumed reassert events are cleared so they re-inject exactly once and
  // the inbox does not accumulate stale reassert files across compactions.
  const { events: correctionsReassert, consumedPaths: reassertConsumed } =
    collectCorrectionsReassert(pending);
  const clearedReasserts = await clearReassertedInboxEntries(reassertConsumed);

  const envelope = wrapEnvelope({
    event: "SessionStart",
    agent: GROK_AGENT_ID,
    budget: envelopeBudgetFor(GROK_CONTEXT_WINDOW),
    body: {
      contract: MEMORY_CONTRACT,
      identity: {
        agent: GROK_AGENT_ID,
        workspace: workspaceId,
        vault: GROK_VAULT_PATH,
        session_id: sessionId,
        daemon_ok: status.socket.ok,
        afm_ok: status.afm.ok,
        runtime: "grok-build",
      },
      pending_learnings: pending.map((entry) => ({
        slug: entry.slug,
        created: entry.createdAt,
        path: entry.filePath,
        candidates: entry.payload.candidates,
        kind: entry.payload.kind,
        task: entry.payload.task,
      })),
      handoff_context: handoffContext.map((snippet) => ({
        ref: snippet.ref,
        path: snippet.relativePath,
        snippet: snippet.snippet,
      })),
      // hooks-PL-1: discriminated stale-belief payload (matched /
      // checked_no_match from the daemon; explicit error here).
      stale_beliefs:
        contradictions.ok && contradictions.data
          ? contradictions.data
          : { ok: false, status: "error", error: contradictions.error },
      ...(correctionsReassert.length > 0
        ? { corrections_reassert: correctionsReassert }
        : {}),
      recall:
        recall.ok && recall.data
          ? {
              ok: true,
              results: recall.data.results,
              agent_origin: recall.data.agent_id ?? GROK_AGENT_ID,
              layer: recall.data.layer,
              layers: BOOT_RECALL_LAYERS,
            }
          : { ok: false, error: recall.error },
      layer1_source:
        identityRead.ok && identityRead.data?.context
          ? {
              ok: true,
              agent_origin: identityRead.data.agent_id ?? GROK_AGENT_ID,
              backend: identityRead.data.backend,
            }
          : { ok: false, error: identityRead.error },
      fallback_commands: {
        layer1: "node dist/grok-hook.js SessionStart < /dev/null",
        daemon_read: "node dist/cli.js read grok-build",
        recall: "node dist/cli.js prepare '<task>'",
      },
      audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]),
    },
  });

  await recordAudit(GROK_VAULT_PATH, {
    tool: "hook_grok_session_start",
    summary: `boot ${sessionId}`,
    details: {
      daemon_ok: status.socket.ok,
      afm_ok: status.afm.ok,
      pending_inbox: pending.length,
      handoff_context: handoffContext.length,
      workspace: workspaceId,
      corrections_reassert: correctionsReassert.length,
      reassert_entries_cleared: clearedReasserts.length,
    },
  });

  const nativeLayer1 = identityRead.ok && identityRead.data?.context ? identityRead.data.context.trim() : "";
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

  const workspaceId = workspaceFromPayload(payload);
  const signature = hashTaskSignature(prompt);
  const [vaultResults, recall] = await Promise.all([
    searchVaultNotes(GROK_VAULT_PATH, prompt, 6),
    recallMemory({
      query: prompt,
      limit: 6,
      agentId: GROK_AGENT_ID,
      workspaceId,
    }),
  ]);

  if (vaultResults.length === 0 && (!recall.ok || !recall.data?.results)) {
    return { continue: true };
  }

  const envelope = wrapEnvelope({
    event: "UserPromptSubmit",
    agent: GROK_AGENT_ID,
    body: {
      identity: {
        agent: GROK_AGENT_ID,
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
    },
  });

  await recordAudit(GROK_VAULT_PATH, {
    tool: "hook_grok_user_prompt_submit",
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
  await ensureVault(GROK_VAULT_PATH);
  const tail = await auditTail(GROK_VAULT_PATH, 60);
  const scarTissue = extractScarTissue(tail.entries);
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  const workspaceId = workspaceFromPayload(payload);
  const transcript = asString(payload.trigger) || asString(payload.summary);

  // hooks-PL-3: stash current stale-belief/contradiction events with the
  // precompact handoff so the post-compaction boot re-asserts them
  // (corrections_reassert) even if the daemon is down at next boot.
  const { ok: staleBeliefsOk, events: staleBeliefEvents } =
    await fetchStaleBeliefEvents(GROK_AGENT_ID);

  const inbox = await writeInbox(GROK_VAULT_PATH, sessionId, {
    kind: "grok_precompact_handoff",
    agent_id: GROK_AGENT_ID,
    workspace_id: workspaceId,
    scar_tissue: scarTissue,
    stale_belief_events: staleBeliefEvents,
    audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
    compaction_trigger: transcript || "compaction in progress",
    durable_learning_committed: false,
  });

  const envelope = wrapEnvelope({
    event: "PreCompact",
    agent: GROK_AGENT_ID,
    body: {
      identity: {
        agent: GROK_AGENT_ID,
        workspace: workspaceId,
        session_id: sessionId,
      },
      scar_tissue: scarTissue,
      audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
      compaction_trigger: transcript || "compaction in progress",
      inbox_path: inbox.filePath,
      durable_learning_committed: false,
    },
  });

  await recordAudit(GROK_VAULT_PATH, {
    tool: "hook_grok_pre_compact",
    summary: `pre-compact ${sessionId}`,
    details: {
      scar_count: scarTissue.length,
      trigger: transcript || "auto",
      workspace: workspaceId,
      inbox_path: inbox.filePath,
      stale_belief_events: staleBeliefEvents.length,
      stale_beliefs_ok: staleBeliefsOk,
    },
  });

  return withHookContext("PreCompact", envelope);
}

async function handleStop(payload: Record<string, unknown>): Promise<HookOutput> {
  await ensureVault(GROK_VAULT_PATH);
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  const workspaceId = workspaceFromPayload(payload);
  const lastTask = asString(payload.last_user_message) || asString(payload.summary) || sessionId;
  const tail = await auditTail(GROK_VAULT_PATH, 30);
  const outcome = await prepareOutcome({
    task: lastTask.slice(0, 200),
    summary: tail.entries.slice(-5).join("\n").slice(0, 600) || "session ended",
    profile: "compact",
    vaultPath: GROK_VAULT_PATH,
  });

  // Nothing worth persisting: skip the inbox write and audit entry so we don't
  // litter the inbox with empty files or pad the audit log with noise.
  if (outcome.outcomeDraft.learnCandidates.length === 0) {
    return { continue: true };
  }

  const inbox = await writeInbox(GROK_VAULT_PATH, sessionId, {
    kind: "stop_candidates",
    agent_id: GROK_AGENT_ID,
    workspace_id: workspaceId,
    candidates: outcome.outcomeDraft.learnCandidates,
    log_only: outcome.outcomeDraft.logOnly,
    expires: outcome.outcomeDraft.expires,
    do_not_store: outcome.outcomeDraft.doNotStore,
    last_task: lastTask.slice(0, 200),
  });

  await recordAudit(GROK_VAULT_PATH, {
    tool: "hook_grok_stop",
    summary: `stop ${sessionId}`,
    details: {
      candidates: outcome.outcomeDraft.learnCandidates.length,
      workspace: workspaceId,
      inbox_path: inbox.filePath,
    },
  });

  return {
    continue: true,
    systemMessage: `Minni: ${outcome.outcomeDraft.learnCandidates.length} candidate learning${
      outcome.outcomeDraft.learnCandidates.length === 1 ? "" : "s"
    } drafted to inbox (${inbox.filePath}). Use minni_prepare_outcome/minni_learn to review and commit.`,
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

async function main(): Promise<void> {
  if (!GROK_HOOKS_ENABLED) {
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
    const output = await dispatch(event, payload);
    emit(output);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    try {
      await recordAudit(GROK_VAULT_PATH, {
        tool: "hook_grok_error",
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

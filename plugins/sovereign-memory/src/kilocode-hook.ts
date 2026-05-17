import {
  KILOCODE_AGENT_ID,
  KILOCODE_CONTEXT_WINDOW,
  KILOCODE_HOOKS_ENABLED,
  KILOCODE_VAULT_PATH,
  KILOCODE_WORKSPACE_ID,
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
  buildStatusReport,
  formatRecall,
  recallMemory,
} from "./sovereign.js";
import { extractScarTissue, prepareOutcome } from "./task.js";
import {
  auditTail,
  clearInboxEntry,
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

function vaultRecallToBody(vault: VaultSearchResult[]): unknown {
  return vault.slice(0, 6).map((result) => ({
    wikilink: result.wikilink,
    score: result.score,
    snippet: result.snippet.replace(/\s+/g, " ").slice(0, 240),
  }));
}

async function handleSessionStart(payload: Record<string, unknown>): Promise<HookOutput> {
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  await ensureVault(KILOCODE_VAULT_PATH);
  const status = await buildStatusReport({ vaultPath: KILOCODE_VAULT_PATH });
  const tail = await auditTail(KILOCODE_VAULT_PATH, 5);
  const recall = await recallMemory({
    query: `boot identity for ${KILOCODE_WORKSPACE_ID}`,
    layer: "identity",
    limit: 4,
    agentId: KILOCODE_AGENT_ID,
    workspaceId: KILOCODE_WORKSPACE_ID,
  });
  const pending = await readPendingInbox(KILOCODE_VAULT_PATH, 3);
  const handoffContext = await resolveInboxHandoffContext(KILOCODE_VAULT_PATH, pending);

  const envelope = wrapEnvelope({
    event: "SessionStart",
    agent: KILOCODE_AGENT_ID,
    budget: envelopeBudgetFor(KILOCODE_CONTEXT_WINDOW),
    body: {
      contract: MEMORY_CONTRACT,
      identity: {
        agent: KILOCODE_AGENT_ID,
        workspace: KILOCODE_WORKSPACE_ID,
        vault: KILOCODE_VAULT_PATH,
        session_id: sessionId,
        daemon_ok: status.socket.ok,
        afm_ok: status.afm.ok,
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
      recall:
        recall.ok && recall.data
          ? {
              ok: true,
              results: recall.data.results,
              agent_origin: recall.data.agent_id ?? KILOCODE_AGENT_ID,
              layer: recall.data.layer,
            }
          : { ok: false, error: recall.error },
      audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]),
    },
  });

  await recordAudit(KILOCODE_VAULT_PATH, {
    tool: "hook_session_start",
    summary: `boot ${sessionId}`,
    details: {
      daemon_ok: status.socket.ok,
      afm_ok: status.afm.ok,
      pending_inbox: pending.length,
      handoff_context: handoffContext.length,
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

async function handleUserPromptSubmit(payload: Record<string, unknown>): Promise<HookOutput> {
  const prompt = asString(payload.prompt) || asString(payload.user_prompt);
  if (!prompt.trim()) {
    return { continue: true };
  }
  const intent = routeMemoryIntent(prompt);
  if (intent.action === "none" && !intent.automaticAllowed) {
    return { continue: true };
  }
  const signature = hashTaskSignature(prompt);
  const [vaultResults, recall] = await Promise.all([
    searchVaultNotes(KILOCODE_VAULT_PATH, prompt, 6),
    recallMemory({
      query: prompt,
      limit: 6,
      agentId: KILOCODE_AGENT_ID,
      workspaceId: KILOCODE_WORKSPACE_ID,
    }),
  ]);

  if (vaultResults.length === 0 && (!recall.ok || !recall.data?.results)) {
    return { continue: true };
  }

  const envelope = wrapEnvelope({
    event: "UserPromptSubmit",
    agent: KILOCODE_AGENT_ID,
    body: {
      identity: {
        agent: KILOCODE_AGENT_ID,
        workspace: KILOCODE_WORKSPACE_ID,
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
      },
    },
  });

  await recordAudit(KILOCODE_VAULT_PATH, {
    tool: "hook_user_prompt_submit",
    summary: prompt.slice(0, 120),
    details: {
      intent: intent.action,
      vault_matches: vaultResults.map((result) => result.relativePath),
      daemon_ok: recall.ok,
      task_signature: signature,
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
  await ensureVault(KILOCODE_VAULT_PATH);
  const tail = await auditTail(KILOCODE_VAULT_PATH, 60);
  const scarTissue = extractScarTissue(tail.entries);
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  const transcript = asString(payload.trigger) || asString(payload.summary);

  const envelope = wrapEnvelope({
    event: "PreCompact",
    agent: KILOCODE_AGENT_ID,
    body: {
      identity: {
        agent: KILOCODE_AGENT_ID,
        workspace: KILOCODE_WORKSPACE_ID,
        session_id: sessionId,
      },
      scar_tissue: scarTissue,
      audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
      compaction_trigger: transcript || "compaction in progress",
    },
  });

  await recordAudit(KILOCODE_VAULT_PATH, {
    tool: "hook_pre_compact",
    summary: `pre-compact ${sessionId}`,
    details: {
      scar_count: scarTissue.length,
      trigger: transcript || "auto",
    },
  });

  return {
    continue: true,
    hookSpecificOutput: {
      hookEventName: "PreCompact",
      additionalContext: envelope,
    },
  };
}

async function handleStop(payload: Record<string, unknown>): Promise<HookOutput> {
  await ensureVault(KILOCODE_VAULT_PATH);
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  const lastTask = asString(payload.last_user_message) || asString(payload.summary) || sessionId;
  const tail = await auditTail(KILOCODE_VAULT_PATH, 30);
  const outcome = await prepareOutcome({
    task: lastTask.slice(0, 200),
    summary: tail.entries.slice(-5).join("\n").slice(0, 600) || "session ended",
    profile: "compact",
    vaultPath: KILOCODE_VAULT_PATH,
  });

  const inbox = await writeInbox(KILOCODE_VAULT_PATH, sessionId, {
    candidates: outcome.outcomeDraft.learnCandidates,
    log_only: outcome.outcomeDraft.logOnly,
    expires: outcome.outcomeDraft.expires,
    do_not_store: outcome.outcomeDraft.doNotStore,
    last_task: lastTask.slice(0, 200),
  });

  await recordAudit(KILOCODE_VAULT_PATH, {
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
    systemMessage: `Sovereign Memory: ${outcome.outcomeDraft.learnCandidates.length} candidate learning${
      outcome.outcomeDraft.learnCandidates.length === 1 ? "" : "s"
    } drafted to inbox (${inbox.filePath}). Use /sovereign-memory:learn to commit.`,
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
  if (!KILOCODE_HOOKS_ENABLED) {
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
    try {
      await recordAudit(KILOCODE_VAULT_PATH, {
        tool: "hook_error",
        summary: `${event}: ${error instanceof Error ? error.message : String(error)}`,
      });
    } catch {
      // last-resort swallow
    }
    emit({ continue: true });
  }
}

void main();

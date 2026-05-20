import {
  CODEX_CONTEXT_WINDOW,
  CODEX_HOOKS_ENABLED,
  DEFAULT_AGENT_ID,
  DEFAULT_VAULT_PATH,
  DEFAULT_WORKSPACE_ID,
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
  readAgentContext,
  recallMemory,
} from "./sovereign.js";
import { extractScarTissue } from "./task.js";
import {
  auditTail,
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
    DEFAULT_WORKSPACE_ID
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
  await ensureVault(DEFAULT_VAULT_PATH);

  const [status, tail, identityRead, pending] = await Promise.all([
    buildStatusReport({ vaultPath: DEFAULT_VAULT_PATH }),
    auditTail(DEFAULT_VAULT_PATH, 5),
    readAgentContext({ agentId: DEFAULT_AGENT_ID, limit: 8 }),
    readPendingInbox(DEFAULT_VAULT_PATH, 3),
  ]);
  const handoffContext = await resolveInboxHandoffContext(DEFAULT_VAULT_PATH, pending);

  const envelope = wrapEnvelope({
    event: "SessionStart",
    agent: DEFAULT_AGENT_ID,
    budget: envelopeBudgetFor(CODEX_CONTEXT_WINDOW),
    body: {
      contract: MEMORY_CONTRACT,
      identity: {
        agent: DEFAULT_AGENT_ID,
        workspace: workspaceId,
        vault: DEFAULT_VAULT_PATH,
        session_id: sessionId,
        daemon_ok: status.socket.ok,
        afm_ok: status.afm.ok,
        runtime: "codex",
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
      layer1_source:
        identityRead.ok && identityRead.data?.context
          ? {
              ok: true,
              agent_origin: identityRead.data.agent_id ?? DEFAULT_AGENT_ID,
              backend: identityRead.data.backend,
            }
          : { ok: false, error: identityRead.error },
      fallback_commands: {
        layer1: "node dist/codex-hook.js SessionStart < /dev/null",
        daemon_read: "node dist/cli.js read codex",
        recall: "node dist/cli.js prepare '<task>'",
      },
      audit_tail: tail.entries.slice(-5).map((entry) => entry.split("\n")[0]),
    },
  });

  await recordAudit(DEFAULT_VAULT_PATH, {
    tool: "hook_codex_session_start",
    summary: `boot ${sessionId}`,
    details: {
      daemon_ok: status.socket.ok,
      afm_ok: status.afm.ok,
      pending_inbox: pending.length,
      handoff_context: handoffContext.length,
      workspace: workspaceId,
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
    searchVaultNotes(DEFAULT_VAULT_PATH, prompt, 6),
    recallMemory({
      query: prompt,
      limit: 6,
      agentId: DEFAULT_AGENT_ID,
      workspaceId,
    }),
  ]);

  if (vaultResults.length === 0 && (!recall.ok || !recall.data?.results)) {
    return { continue: true };
  }

  const envelope = wrapEnvelope({
    event: "UserPromptSubmit",
    agent: DEFAULT_AGENT_ID,
    body: {
      identity: {
        agent: DEFAULT_AGENT_ID,
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

  await recordAudit(DEFAULT_VAULT_PATH, {
    tool: "hook_codex_user_prompt_submit",
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
  await ensureVault(DEFAULT_VAULT_PATH);
  const tail = await auditTail(DEFAULT_VAULT_PATH, 60);
  const scarTissue = extractScarTissue(tail.entries);
  const sessionId = asString(payload.session_id) || asString(payload.sessionId) || "session";
  const workspaceId = workspaceFromPayload(payload);
  const transcript = asString(payload.trigger) || asString(payload.summary);

  const inbox = await writeInbox(DEFAULT_VAULT_PATH, sessionId, {
    kind: "codex_precompact_handoff",
    agent_id: DEFAULT_AGENT_ID,
    workspace_id: workspaceId,
    scar_tissue: scarTissue,
    audit_tail: tail.entries.slice(-10).map((entry) => entry.split("\n")[0]),
    compaction_trigger: transcript || "compaction in progress",
    durable_learning_committed: false,
  });

  const envelope = wrapEnvelope({
    event: "PreCompact",
    agent: DEFAULT_AGENT_ID,
    body: {
      identity: {
        agent: DEFAULT_AGENT_ID,
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

  await recordAudit(DEFAULT_VAULT_PATH, {
    tool: "hook_codex_pre_compact",
    summary: `pre-compact ${sessionId}`,
    details: {
      scar_count: scarTissue.length,
      trigger: transcript || "auto",
      workspace: workspaceId,
      inbox_path: inbox.filePath,
    },
  });

  return withHookContext("PreCompact", envelope);
}

async function dispatch(event: string, payload: Record<string, unknown>): Promise<HookOutput> {
  switch (event) {
    case "SessionStart":
      return handleSessionStart(payload);
    case "UserPromptSubmit":
      return handleUserPromptSubmit(payload);
    case "PreCompact":
      return handlePreCompact(payload);
    default:
      return { continue: true };
  }
}

async function main(): Promise<void> {
  if (!CODEX_HOOKS_ENABLED) {
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
      await recordAudit(DEFAULT_VAULT_PATH, {
        tool: "hook_codex_error",
        summary: `${event}: ${error instanceof Error ? error.message : String(error)}`,
      });
    } catch {
      // last-resort swallow
    }
    emit({ continue: true });
  }
}

void main();

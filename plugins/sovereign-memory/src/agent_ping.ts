import { randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, rename, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH } from "./config.js";
import { ensureVault, recordAudit } from "./vault.js";

export type AgentPingStatus = "pending" | "approved" | "denied" | "expired";
export type AgentPingDecision = "approve" | "deny";

export interface AgentPingEvent {
  at: string;
  actor: string;
  action: "created" | "viewed" | "approved" | "denied" | "expired";
  reason?: string;
}

export interface AgentPingContract {
  schema: "sovereign.agent-info-request.v1";
  requestId: string;
  nonce: string;
  status: AgentPingStatus;
  fromAgent: string;
  toAgent: string;
  question: string;
  purpose?: string;
  createdAt: string;
  expiresAt: string;
  policy: {
    maxResponseChars: number;
    allowedTopics: string[];
    dataMinimization: string;
  };
  response?: {
    decidedAt: string;
    decidedBy: string;
    decision: AgentPingDecision;
    answer?: string;
    reason?: string;
    redacted: boolean;
  };
  lifecycle: AgentPingEvent[];
}

export interface CreateAgentPingInput {
  toAgent: string;
  question: string;
  purpose?: string;
  allowedTopics?: string[];
  ttlMinutes?: number;
  maxResponseChars?: number;
  now?: Date;
}

export interface DecideAgentPingInput {
  requestId: string;
  decision: AgentPingDecision;
  answer?: string;
  reason?: string;
  now?: Date;
}

export interface AgentPingListResult {
  agentId: string;
  vaultPath: string;
  requests: AgentPingContract[];
}

export interface AgentPingWriteResult {
  contract: AgentPingContract;
  senderPath: string;
  recipientPath: string;
}

const CONTRACT_SCHEMA = "sovereign.agent-info-request.v1";
const DEFAULT_TTL_MINUTES = 24 * 60;
const MAX_TTL_MINUTES = 7 * 24 * 60;
const DEFAULT_MAX_RESPONSE_CHARS = 1200;
const MAX_RESPONSE_CHARS = 4000;
const MAX_QUESTION_CHARS = 2000;
const MAX_PURPOSE_CHARS = 500;
const REQUEST_ID_PATTERN = /^[A-Za-z0-9._-]{8,128}$/;

function nowIso(now = new Date()): string {
  return now.toISOString();
}

function clampInt(value: number | undefined, fallback: number, min: number, max: number): number {
  if (!Number.isFinite(value ?? NaN)) return fallback;
  return Math.max(min, Math.min(Math.trunc(value ?? fallback), max));
}

function agentEnvKey(agentId: string): string {
  return agentId.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function defaultAgentVault(agentId: string): string {
  const aliases: Record<string, string> = {
    "claude-code": "claudecode",
    claudecode: "claudecode",
    codex: "codex",
    hermes: "hermes",
    openclaw: "openclaw",
    kilocode: "kilocode",
  };
  const slug = aliases[agentId] ?? (agentId.toLowerCase().replace(/[^a-z0-9]+/g, "") || "agent");
  return path.join(os.homedir(), ".sovereign-memory", `${slug}-vault`);
}

export function resolveAgentVaultPath(agentId: string): string {
  if (agentId === DEFAULT_AGENT_ID) return DEFAULT_VAULT_PATH;

  const mappingRaw = process.env.SOVEREIGN_AGENT_VAULTS;
  if (mappingRaw) {
    try {
      const mapping = JSON.parse(mappingRaw) as unknown;
      if (mapping && typeof mapping === "object" && !Array.isArray(mapping)) {
        const mapped = (mapping as Record<string, unknown>)[agentId];
        if (typeof mapped === "string" && mapped.trim()) return path.resolve(mapped.replace(/^~(?=$|\/)/, os.homedir()));
      }
    } catch {
      // Invalid operator config should not let the model choose a path.
    }
  }

  const envValue = process.env[`SOVEREIGN_${agentEnvKey(agentId)}_VAULT_PATH`];
  if (envValue?.trim()) return path.resolve(envValue.replace(/^~(?=$|\/)/, os.homedir()));
  return defaultAgentVault(agentId);
}

function agentPingDir(vaultPath: string, box: "inbox" | "outbox"): string {
  return path.join(vaultPath, box, "agent-pings");
}

function agentPingPath(vaultPath: string, box: "inbox" | "outbox", requestId: string): string {
  if (!REQUEST_ID_PATTERN.test(requestId)) throw new Error("Invalid requestId.");
  return path.join(agentPingDir(vaultPath, box), `${requestId}.json`);
}

async function writeJsonAtomic(filePath: string, value: unknown): Promise<void> {
  await mkdir(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.${process.pid}.${Date.now().toString(36)}.tmp`;
  await writeFile(tmp, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(tmp, filePath);
}

async function readContract(filePath: string): Promise<AgentPingContract> {
  const parsed = JSON.parse(await readFile(filePath, "utf8")) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Contract is not an object.");
  const contract = parsed as AgentPingContract;
  if (contract.schema !== CONTRACT_SCHEMA) throw new Error("Unsupported contract schema.");
  if (!REQUEST_ID_PATTERN.test(contract.requestId)) throw new Error("Invalid contract requestId.");
  return contract;
}

function sanitizeShortText(value: string, maxChars: number): string {
  return value.replace(/\s+/g, " ").trim().slice(0, maxChars);
}

function sanitizeTopics(values: string[] | undefined): string[] {
  return [...new Set((values ?? []).map((item) => sanitizeShortText(item, 80)).filter(Boolean))].slice(0, 20);
}

function redactSensitiveText(value: string): { text: string; redacted: boolean } {
  let redacted = false;
  let text = value.replace(/\/Users\/[^\s"',)]+|\/Volumes\/[^\s"',)]+|\/private\/[^\s"',)]+/g, () => {
    redacted = true;
    return "[local-path]";
  });
  text = text.replace(
    /\b(api[_-]?key|password|secret|credential|private[_ -]?key|bearer|access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*[^\s,;<>"']+/gi,
    (match, key) => {
      redacted = true;
      return `${key}=[REDACTED]`;
    },
  );
  text = text.replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, () => {
    redacted = true;
    return "[REDACTED_PRIVATE_KEY]";
  });
  return { text, redacted };
}

function withExpiry(contract: AgentPingContract, now = new Date()): AgentPingContract {
  if (contract.status !== "pending") return contract;
  if (Date.parse(contract.expiresAt) > now.getTime()) return contract;
  return {
    ...contract,
    status: "expired",
    lifecycle: [
      ...contract.lifecycle,
      {
        at: nowIso(now),
        actor: "system",
        action: "expired",
        reason: "request ttl elapsed",
      },
    ],
  };
}

async function syncContract(contract: AgentPingContract): Promise<{ senderPath: string; recipientPath: string }> {
  const senderVault = resolveAgentVaultPath(contract.fromAgent);
  const recipientVault = resolveAgentVaultPath(contract.toAgent);
  await Promise.all([ensureVault(senderVault), ensureVault(recipientVault)]);
  const senderPath = agentPingPath(senderVault, "outbox", contract.requestId);
  const recipientPath = agentPingPath(recipientVault, "inbox", contract.requestId);
  await Promise.all([writeJsonAtomic(senderPath, contract), writeJsonAtomic(recipientPath, contract)]);
  return { senderPath, recipientPath };
}

export async function createAgentPingRequest(input: CreateAgentPingInput, actorAgent = DEFAULT_AGENT_ID): Promise<AgentPingWriteResult> {
  const toAgent = sanitizeShortText(input.toAgent, 120);
  const question = sanitizeShortText(input.question, MAX_QUESTION_CHARS);
  if (!toAgent) throw new Error("toAgent is required.");
  if (!question) throw new Error("question is required.");
  if (toAgent === actorAgent) throw new Error("toAgent must be a different agent.");

  const now = input.now ?? new Date();
  const ttlMinutes = clampInt(input.ttlMinutes, DEFAULT_TTL_MINUTES, 1, MAX_TTL_MINUTES);
  const maxResponseChars = clampInt(input.maxResponseChars, DEFAULT_MAX_RESPONSE_CHARS, 1, MAX_RESPONSE_CHARS);
  const requestId = randomUUID();
  const contract: AgentPingContract = {
    schema: CONTRACT_SCHEMA,
    requestId,
    nonce: randomUUID(),
    status: "pending",
    fromAgent: actorAgent,
    toAgent,
    question,
    purpose: input.purpose ? sanitizeShortText(input.purpose, MAX_PURPOSE_CHARS) : undefined,
    createdAt: nowIso(now),
    expiresAt: nowIso(new Date(now.getTime() + ttlMinutes * 60_000)),
    policy: {
      maxResponseChars,
      allowedTopics: sanitizeTopics(input.allowedTopics),
      dataMinimization:
        "Recipient must answer only the stated question, omit secrets and raw private memory, and approve or deny explicitly.",
    },
    lifecycle: [{ at: nowIso(now), actor: actorAgent, action: "created" }],
  };
  const paths = await syncContract(contract);
  await recordAudit(resolveAgentVaultPath(actorAgent), {
    tool: "sovereign_ping_agent_request",
    summary: `${actorAgent}->${toAgent} ${requestId}`,
    details: {
      requestId,
      toAgent,
      expiresAt: contract.expiresAt,
      allowedTopics: contract.policy.allowedTopics,
      maxResponseChars,
    },
  });
  return { contract, ...paths };
}

export async function listAgentPingInbox(agentId = DEFAULT_AGENT_ID, limit = 20, now = new Date()): Promise<AgentPingListResult> {
  const vaultPath = resolveAgentVaultPath(agentId);
  await ensureVault(vaultPath);
  const dir = agentPingDir(vaultPath, "inbox");
  let names: string[] = [];
  try {
    names = (await readdir(dir)).filter((name) => name.endsWith(".json")).sort();
  } catch {
    return { agentId, vaultPath, requests: [] };
  }
  const requests: AgentPingContract[] = [];
  for (const name of names.slice(-Math.max(1, Math.min(limit, 100))).reverse()) {
    try {
      const filePath = path.join(dir, name);
      const contract = withExpiry(await readContract(filePath), now);
      if (contract.toAgent !== agentId) continue;
      if (contract.status === "expired") await syncContract(contract);
      requests.push(contract);
    } catch {
      // Ignore malformed inbox artifacts; audits should be generated by the writer path.
    }
  }
  await recordAudit(vaultPath, {
    tool: "sovereign_ping_agent_inbox",
    summary: `${agentId} viewed ${requests.length} ping request(s)`,
    details: {
      agentId,
      requestIds: requests.map((item) => item.requestId),
      pending: requests.filter((item) => item.status === "pending").length,
    },
  });
  return { agentId, vaultPath, requests };
}

export async function decideAgentPingRequest(input: DecideAgentPingInput, actorAgent = DEFAULT_AGENT_ID): Promise<AgentPingWriteResult> {
  const vaultPath = resolveAgentVaultPath(actorAgent);
  const requestPath = agentPingPath(vaultPath, "inbox", input.requestId);
  let existing: AgentPingContract;
  try {
    existing = withExpiry(await readContract(requestPath), input.now ?? new Date());
  } catch (error) {
    // If the requester tries to decide its own outbox copy, return an
    // authorization error instead of leaking path details from the missing
    // recipient inbox file.
    try {
      const outboxCopy = await readContract(agentPingPath(vaultPath, "outbox", input.requestId));
      if (outboxCopy.toAgent !== actorAgent) throw new Error("Only the recipient agent can decide this request.");
    } catch (outboxError) {
      if (outboxError instanceof Error && outboxError.message.includes("Only the recipient agent")) throw outboxError;
    }
    throw error;
  }
  if (existing.toAgent !== actorAgent) throw new Error("Only the recipient agent can decide this request.");
  if (existing.status !== "pending") throw new Error(`Request is ${existing.status}; only pending requests can be decided.`);

  const now = input.now ?? new Date();
  const decision = input.decision;
  if (decision !== "approve" && decision !== "deny") throw new Error("decision must be approve or deny.");
  if (decision === "approve" && !input.answer?.trim()) throw new Error("Approved requests require an answer.");

  const rawAnswer = input.answer?.trim() ?? "";
  const cappedAnswer = rawAnswer.slice(0, existing.policy.maxResponseChars);
  const answerRedaction = redactSensitiveText(cappedAnswer);
  const reason = input.reason ? sanitizeShortText(input.reason, 500) : undefined;
  const contract: AgentPingContract = {
    ...existing,
    status: decision === "approve" ? "approved" : "denied",
    response: {
      decidedAt: nowIso(now),
      decidedBy: actorAgent,
      decision,
      answer: decision === "approve" ? answerRedaction.text : undefined,
      reason,
      redacted: answerRedaction.redacted || rawAnswer.length > cappedAnswer.length,
    },
    lifecycle: [
      ...existing.lifecycle,
      {
        at: nowIso(now),
        actor: actorAgent,
        action: decision === "approve" ? "approved" : "denied",
        reason,
      },
    ],
  };
  const paths = await syncContract(contract);
  await recordAudit(vaultPath, {
    tool: "sovereign_ping_agent_decide",
    summary: `${decision} ${input.requestId}`,
    details: {
      requestId: input.requestId,
      fromAgent: contract.fromAgent,
      toAgent: contract.toAgent,
      decision,
      redacted: contract.response?.redacted,
    },
  });
  await recordAudit(resolveAgentVaultPath(contract.fromAgent), {
    tool: "sovereign_ping_agent_status",
    summary: `${contract.status} ${input.requestId}`,
    details: {
      requestId: input.requestId,
      fromAgent: contract.fromAgent,
      toAgent: contract.toAgent,
      status: contract.status,
      redacted: contract.response?.redacted,
    },
  });
  return { contract, ...paths };
}

export async function getAgentPingStatus(requestId: string, actorAgent = DEFAULT_AGENT_ID, now = new Date()): Promise<AgentPingWriteResult> {
  const actorVault = resolveAgentVaultPath(actorAgent);
  const candidates = [agentPingPath(actorVault, "outbox", requestId), agentPingPath(actorVault, "inbox", requestId)];
  let contract: AgentPingContract | undefined;
  let lastError: unknown;
  for (const candidate of candidates) {
    try {
      contract = await readContract(candidate);
      break;
    } catch (error) {
      lastError = error;
    }
  }
  if (!contract) throw lastError instanceof Error ? lastError : new Error("Request not found.");
  if (contract.fromAgent !== actorAgent && contract.toAgent !== actorAgent) {
    throw new Error("Only the requester or recipient can view this request.");
  }
  const normalized = withExpiry(contract, now);
  const paths = normalized.status === contract.status ? {
    senderPath: agentPingPath(resolveAgentVaultPath(contract.fromAgent), "outbox", contract.requestId),
    recipientPath: agentPingPath(resolveAgentVaultPath(contract.toAgent), "inbox", contract.requestId),
  } : await syncContract(normalized);
  await recordAudit(actorVault, {
    tool: "sovereign_ping_agent_status",
    summary: `${actorAgent} checked ${requestId}`,
    details: {
      requestId,
      status: normalized.status,
      fromAgent: normalized.fromAgent,
      toAgent: normalized.toAgent,
    },
  });
  return { contract: normalized, ...paths };
}

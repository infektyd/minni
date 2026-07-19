import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { existsSync } from "node:fs";
import { stat, readdir } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { URL } from "node:url";
import { AFM_HEALTH_URL, AFM_PROVIDER_MODE, DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH, DEFAULT_WORKSPACE_ID, SOCKET_PATH } from "./config.js";
import {
  daemonAfmToProviderHealth,
  getAfmProviderHealth,
  GENERATION_PROBE_TTL_MS,
  resolveAfmProvider,
  resolvedNativeHelperPath,
  sanitizeAfmHealth,
  type AfmProviderMode,
  type AfmProviderResolution,
  type ProviderHealth,
} from "./afm.js";
import { auditTail, ensureVault, recordAudit, vaultExists, writeInbox } from "./vault.js";
import type { VaultSearchResult } from "./vault.js";

export interface JsonResult<T = unknown> {
  ok: boolean;
  data?: T;
  error?: string;
}

export interface RecallResponse {
  results?: string | unknown[];
  agent_id?: string;
  layer?: string;
  workspace_id?: string;
  backend?: string;
  backend_badge?: string;
  /** W5: the daemon's own hit count (recall.py response_payload, always paired
   *  with `results`) — the preferred, wire-shape signal for "answered but
   *  empty", ahead of inspecting `results` itself. */
  count?: number;
  /** P0-A diagnostic: present (non-empty) when the daemon's read gate
   *  filtered a non-empty candidate set to zero — a scoped ANSWER, not a
   *  miss. isDaemonResultEmpty must treat it as non-empty. */
  auth_suppression?: unknown[];
  /** Learnings surface separately from document `results`/`count`; a
   *  learnings-only reply is still a live scoped answer. */
  learnings?: unknown[];
}

export interface ReadContextResponse {
  agent_id?: string;
  context?: string;
  backend?: string;
  backend_badge?: string;
}

export interface ExtractorStatus {
  provider: string;
  tier: "local" | "cloud";
  generationVerified: boolean;
  probeAgeMs: number;
  /** Which leg produced this verdict — see StatusReport.afm's doc comment. */
  source: "daemon" | "plugin-probe";
}

export interface StatusReport {
  vault: {
    path: string;
    exists: boolean;
  };
  socket: JsonResult;
  /**
   * afm.ok is generationVerified (honest health), not mere /health
   * reachability. Single-authority (punch-list §3): when the daemon socket
   * carries a fresh, matching-mode probe (socket.data.afm), this field is
   * DERIVED from that same daemon data rather than an independent plugin
   * probe, so it can never disagree with socket.data.afm in content — only
   * fall back to the plugin's own probe when the daemon leg is unreachable,
   * stale, mode-mismatched, or malformed. afm.data.source records which leg
   * produced the verdict ("daemon" | "plugin-probe").
   */
  afm: JsonResult;
  afmProvider: AfmProviderResolution;
  extractor: ExtractorStatus;
  audit: {
    entries: number;
    latest?: string;
    volume: number;
  };
}

export function parseSovrdJson<T = unknown>(raw: string): JsonResult<T> {
  try {
    return { ok: true, data: JSON.parse(raw) as T };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

// NOTE: the old socketRequest() (HTTP-over-unix-socket) was removed. The daemon
// speaks JSON-RPC only, so that fallback could never succeed and instead masked
// real daemon errors (e.g. identity_mismatch) as "Parse Error: Expected HTTP/".
// All daemon calls now go through jsonRpcSocketRequest and surface real errors.

/**
 * Honest /health body inspection. The old behavior treated any HTTP<400
 * parseable-JSON body as ok without ever reading availability/status values,
 * so a bridge reporting status=error still counted as healthy.
 */
function afmHealthBodyProblem(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const record = data as Record<string, unknown>;
  // Only definitive negatives veto the downstream generation probe. Unknown
  // status strings (a bridge update adding e.g. "initializing" or
  // "degraded-but-serving") must NOT hard-fail health: the probe runs and
  // decides. The previous closed allowlist {ok,ready,healthy,available,bridge}
  // kept afm_ok=false even with working generation.
  const badHealthStatuses = new Set(["error", "fail", "failed", "down", "dead", "stopped", "unavailable"]);
  if (typeof record.status === "string" && badHealthStatuses.has(record.status.toLowerCase())) {
    return `afm health degraded: status=${record.status.slice(0, 40)}`;
  }
  if (typeof record.availability === "string" && record.availability.toLowerCase() !== "available") {
    return `afm health degraded: availability=${record.availability.slice(0, 40)}`;
  }
  if (record.available === false) return "afm health degraded: available=false";
  if (record.ok === false) return "afm health degraded: ok=false";
  return undefined;
}

export async function afmHealth(url = AFM_HEALTH_URL): Promise<JsonResult> {
  return new Promise((resolve) => {
    const parsedUrl = new URL(url);
    const client = parsedUrl.protocol === "https:" ? httpsRequest : httpRequest;
    const req = client(parsedUrl, { method: "GET", timeout: 2000 }, (res) => {
      let data = "";
      res.on("data", (chunk) => {
        data += chunk;
      });
      res.on("end", () => {
        const parsed = parseSovrdJson(data);
        if (res.statusCode && res.statusCode >= 400) {
          resolve({ ok: false, data: parsed.data, error: `HTTP ${res.statusCode}` });
          return;
        }
        if (!parsed.ok) {
          resolve(parsed);
          return;
        }
        const problem = afmHealthBodyProblem(parsed.data);
        if (problem) {
          resolve({ ok: false, data: parsed.data, error: problem });
          return;
        }
        resolve(parsed);
      });
    });
    req.on("timeout", () => {
      req.destroy(new Error("AFM health request timed out"));
    });
    req.on("error", (error) => resolve({ ok: false, error: error.message }));
    req.end();
  });
}

export async function socketHealth(): Promise<JsonResult> {
  // Daemon speaks JSON-RPC only; return its real result (including structured
  // errors) rather than masking them behind a dead HTTP-over-socket fallback.
  return jsonRpcSocketRequestWithFallback("status", {});
}

/**
 * Layer policy (recall-F1/recall-F6): the two recall surfaces are deliberately
 * complementary, not contradictory —
 *  - BOOT (SessionStart) queries BOOT_RECALL_LAYERS: the identity shelf PLUS
 *    the correction/decision-bearing layers (knowledge, episodic). The old
 *    identity-only whitelist dropped knowledge-layer corrections before rerank,
 *    which is how an already-corrected belief survived a 3-hour loop.
 *  - PER-TURN (UserPromptSubmit) recalls all layers but formatRecallLean drops
 *    identity-shelf hits, because the shelf was already injected at boot and
 *    never changes mid-session.
 * Boot = identity + fresh corrections; per-turn = query-relevant non-identity.
 */
export const BOOT_RECALL_LAYERS: ReadonlyArray<string> = ["identity", "knowledge", "episodic"];

export async function recallMemory(input: {
  query: string;
  agentId?: string;
  layer?: string;
  layers?: ReadonlyArray<string>;
  workspaceId?: string;
  limit?: number;
  scope?: "personal" | "combined" | "both";
  crossAgent?: boolean;
  cross_agent?: boolean;
}, requester: JsonRpcRequester = jsonRpcSocketRequest): Promise<JsonResult<RecallResponse>> {
  // Daemon is JSON-RPC only; surface its real result/error (e.g. identity_mismatch)
  // directly instead of masking it behind a dead HTTP-over-socket fallback.
  // M-2 fix: explicitly pass depth="snippet" so agents receive evidence text
  // (~120 tokens/result) rather than headline-only (wikilink + score, no text).
  // The daemon's previous default was "headline" despite the docstring claiming
  // "snippet". Both the daemon default and this call-site now agree on "snippet".
  return jsonRpcSocketRequestWithFallbackRequester("search", {
    query: input.query,
    agent_id: input.agentId ?? DEFAULT_AGENT_ID,
    layers: input.layers ?? (input.layer ? [input.layer] : undefined),
    limit: input.limit,
    scope: input.scope,
    cross_agent: input.crossAgent ?? input.cross_agent,
    depth: "snippet",
  }, requester) as Promise<JsonResult<RecallResponse>>;
}

export async function learnMemory(input: {
  content: string;
  category?: string;
  agentId?: string;
  workspaceId?: string;
}): Promise<JsonResult> {
  const body = {
    content: input.content,
    category: input.category ?? "general",
    agent_id: input.agentId ?? DEFAULT_AGENT_ID,
    workspace_id: input.workspaceId ?? DEFAULT_WORKSPACE_ID,
  };
  // JSON-RPC only; return the daemon's real result/error (no masking HTTP fallback).
  return jsonRpcSocketRequestWithFallback("learn", body);
}

export async function readAgentContext(input: {
  agentId?: string;
  limit?: number;
} = {}): Promise<JsonResult<ReadContextResponse>> {
  return jsonRpcSocketRequestWithFallback("read", {
    agent_id: input.agentId ?? DEFAULT_AGENT_ID,
    limit: input.limit ?? 8,
  }) as Promise<JsonResult<ReadContextResponse>>;
}

/**
 * recall-F1 / hooks-PL-2: the daemon 'read' context is identity-shelf heavy;
 * boot hooks only need its recency-ordered "## Learnings" slice (where fresh
 * corrections land as new active learnings). Extract just that section so the
 * read round-trip (which also records learning_reads) stays cheap to inject.
 */
export function extractLearningsSection(context: string | undefined): string | undefined {
  if (!context) return undefined;
  const match = context.match(/^## Learnings[^\n]*$/m);
  if (!match || match.index === undefined) return undefined;
  const rest = context.slice(match.index);
  const next = rest.slice(match[0].length).search(/^## /m);
  const section = next >= 0 ? rest.slice(0, match[0].length + next) : rest;
  return section.trim() || undefined;
}

/**
 * Boot identity delivery: the daemon `read` context leads with the whole-
 * document Layer 1 identity block (## Agent Identity …) before Prior Context /
 * Learnings. Extract that slice so SessionStart can rank it in the envelope as
 * `identity_body` instead of stripping it via extractLearningsSection().
 *
 * H5: the block is only trusted when it (a) sits at the LEADING position of the
 * context — an unanchored match anywhere in the body lets a learning/prior-
 * context entry smuggle a `## Agent Identity:` header that would be injected as
 * Layer 1 — and (b) names the stamped agent, when one is supplied. A header for
 * a different agent (or one buried mid-context) is rejected.
 */
export function extractIdentityBody(
  context: string | undefined,
  stampedAgent?: string,
): string | undefined {
  if (!context) return undefined;
  // Anchor to the leading position: allow only leading whitespace before the
  // identity header. A header found anywhere else is untrusted smuggled content.
  const lead = context.match(/^\s*(## Agent Identity[^\n]*)$/m);
  if (!lead || lead.index === undefined) return undefined;
  if (context.slice(0, lead.index).trim() !== "") return undefined;
  const headerLine = lead[1];
  // Validate the named agent against the server-stamped identity when provided.
  if (stampedAgent !== undefined) {
    const named = headerLine.replace(/^## Agent Identity:?\s*/, "").trim();
    if (named !== stampedAgent) return undefined;
  }
  const headerStart = lead.index + lead[0].indexOf(headerLine);
  const rest = context.slice(headerStart);
  const afterHeader = rest.slice(headerLine.length);
  const next = afterHeader.search(/^## (?:Prior Context|Learnings|Recent Activity)/m);
  const section = next >= 0 ? rest.slice(0, headerLine.length + next) : rest;
  return section.trim() || undefined;
}

/** Rough token→char budget (4 chars/token) for Layer-1 envelope gating. */
export function truncateToTokenCharBudget(text: string, tokenBudget: number): string {
  if (tokenBudget <= 0) return "";
  const maxChars = tokenBudget * 4;
  return text.length <= maxChars ? text : text.slice(0, maxChars);
}

export type JsonRpcRequester = (socketPath: string, method: string, params: Record<string, unknown>) => Promise<JsonResult>;

function jsonRpcSocketCandidates(): string[] {
  return [SOCKET_PATH];
}

/**
 * Extract the daemon's structured identity-recovery route from an RPC payload
 * (#121 / PR #132 P1). Handles both wire shapes:
 *  - a success-wrapped recovery envelope (`result.status === "recovery_required"`,
 *    e.g. gate.shared), and
 *  - a JSON-RPC error whose `error.data` carries the machine route (gated
 *    method capability denials).
 * Returns the route object, or undefined when the payload is not a recovery.
 */
export function recoveryRouteFrom(payload: unknown): Record<string, unknown> | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const record = payload as Record<string, unknown>;
  if (record.status === "recovery_required") return record;
  const error = record.error;
  if (!error || typeof error !== "object") return undefined;
  const data = (error as Record<string, unknown>).data;
  if (data && typeof data === "object" && (data as Record<string, unknown>).status === "recovery_required") {
    return data as Record<string, unknown>;
  }
  return undefined;
}

function recoveryErrorMessage(route: Record<string, unknown>): string {
  const reason = typeof route.reason === "string" ? route.reason : "recovery_required";
  const remediation = Array.isArray(route.remediation) ? route.remediation.join(" ") : "";
  return `recovery_required (${reason}): ${remediation}`.trim();
}

/**
 * Extract a live identity/authz denial from a JSON-RPC error envelope
 * (#132 P2). A -32004 (capability_denied / reserved_agent_id / recovery)
 * means the daemon ANSWERED and refused the caller's identity — a
 * misconfiguration, not an outage — so callers must not fall back to
 * offline framing. Transport failures carry no JSON-RPC error envelope
 * and return undefined, keeping the offline fallback intact.
 */
export function identityDenialFrom(payload: unknown): string | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const error = (payload as Record<string, unknown>).error;
  if (!error || typeof error !== "object") return undefined;
  const record = error as Record<string, unknown>;
  if (record.code !== -32004) return undefined;
  return typeof record.message === "string" && record.message
    ? record.message
    : "capability_denied";
}

/**
 * Convert a JSON-RPC success `result` into a JsonResult. A success-wrapped
 * recovery envelope means identity is unresolved and the call did NOT do what
 * was asked — it must read as a FAILED RPC (surfacing the remediation route),
 * never as ok (#132 P1: minni_recall rendered it as "No recall results" and
 * minni_learn as "learned"). The route stays on `data` for shape-aware
 * callers (e.g. requireSharedGate).
 */
function jsonRpcResultToJsonResult(result: unknown): JsonResult {
  const route = recoveryRouteFrom(result);
  if (route) {
    return { ok: false, data: result, error: recoveryErrorMessage(route) };
  }
  return { ok: true, data: result };
}

export function jsonRpcSocketRequest(socketPath: string, method: string, params: Record<string, unknown>): Promise<JsonResult> {
  return new Promise((resolve) => {
    if (!existsSync(socketPath)) {
      resolve({ ok: false, error: `Socket not found: ${socketPath}` });
      return;
    }
    const client = net.createConnection(socketPath);
    let data = "";
    let settled = false;
    const finish = (result: JsonResult) => {
      if (settled) return;
      settled = true;
      client.destroy();
      resolve(result);
    };
    client.setTimeout(30000, () => finish({ ok: false, error: "JSON-RPC request timed out" }));
    client.on("connect", () => {
      client.write(`${JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method, params })}\n`);
    });
    client.on("data", (chunk) => {
      data += chunk.toString("utf8");
      if (!data.includes("\n")) return;
      const line = data.split("\n")[0];
      const parsed = parseSovrdJson<{ result?: unknown; error?: { message?: string } }>(line);
      if (!parsed.ok) {
        finish(parsed);
        return;
      }
      if (parsed.data?.error) {
        finish({ ok: false, data: parsed.data, error: parsed.data.error.message ?? "JSON-RPC error" });
        return;
      }
      finish(jsonRpcResultToJsonResult(parsed.data?.result));
    });
    client.on("error", (error) => finish({ ok: false, error: error.message }));
    client.on("end", () => {
      if (!settled && data.trim()) {
        const parsed = parseSovrdJson<{ result?: unknown }>(data.trim());
        finish(parsed.ok ? jsonRpcResultToJsonResult(parsed.data?.result) : parsed);
      }
    });
  });
}

export async function handoffMemory(input: {
  fromAgent: string;
  toAgent: string;
  packet: Record<string, unknown>;
}): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallback("daemon.handoff", {
    from_agent: input.fromAgent,
    to_agent: input.toAgent,
    packet: input.packet,
  });
}

export async function gateSharedOperation(
  input: {
    operation: string;
    agentId?: string;
    workspaceId?: string;
    details?: Record<string, unknown>;
  },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester("gate.shared", {
    operation: input.operation,
    agent_id: input.agentId ?? DEFAULT_AGENT_ID,
    workspace_id: input.workspaceId ?? DEFAULT_WORKSPACE_ID,
    details: input.details,
  }, requester);
}

export function isSharedGateUnavailable(error: string | undefined): boolean {
  const message = error ?? "";
  if (message.includes("Method not found: gate.shared")) return true;
  if (message.startsWith("Socket not found:")) return true;
  if (message === "JSON-RPC request timed out") return true;
  // Anchor transport error codes to the START of the message (the shape Node
  // surfaces them in: "connect ECONNREFUSED ...", "ENOENT: no such file ...").
  // Matching them anywhere would let an IDENTITY/authz rejection whose reason
  // text merely *mentions* a code (e.g. "...path ENOENT...") be misclassified
  // as an availability degrade — inverting fail-loud-on-identity into
  // fail-open. Availability degrades; identity must stay loud.
  return /^(?:Error:\s*)?(?:connect\s+)?(?:ECONNREFUSED|ENOENT|EPERM|EHOSTUNREACH|ENETUNREACH|ETIMEDOUT|ENOTSOCK)\b/.test(message);
}

export async function drillMemory(
  input: {
    resultIds?: number[];
    chunkIds?: number[];
    references?: Array<Record<string, unknown> | string>;
    depth?: "snippet" | "chunk" | "document";
  },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester("sm_drill", {
    result_ids: input.resultIds,
    chunk_ids: input.chunkIds,
    references: input.references,
    depth: input.depth ?? "snippet",
  }, requester);
}

export async function exportContextPack(
  input: {
    query: string;
    budgetTokens: number;
    cacheKey: string;
    agentId?: string;
    workspaceId?: string;
  },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester("sm_export_pack", {
    query: input.query,
    budget_tokens: input.budgetTokens,
    cache_key: input.cacheKey,
    agent_id: input.agentId,
    workspace_id: input.workspaceId,
  }, requester);
}

export async function ackHandoff(
  input: {
    leaseId: string;
    status: "accepted" | "rejected_stale" | "rejected_contradicts" | "rejected_scope";
    contradictsId?: number;
    /**
     * A3 authz: the daemon now requires the stamped caller principal to match
     * the lease's to_agent — pass the agent identity (server-side config,
     * never model-supplied) so the daemon can stamp the platform principal,
     * mirroring listPendingHandoffs.
     */
    agentId?: string;
  },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester("minni_ack_handoff", {
    lease_id: input.leaseId,
    status: input.status,
    contradicts_id: input.contradictsId,
    agent_id: input.agentId,
  }, requester);
}

export async function listPendingHandoffs(
  input: { agentId: string },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester("minni_list_pending_handoffs", {
    agent_id: input.agentId,
  }, requester);
}

export async function awaitHandoff(
  input: { leaseId: string; timeoutMs?: number },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester("minni_await_handoff", {
    lease_id: input.leaseId,
    timeout_ms: input.timeoutMs,
  }, requester);
}

export async function subscribeContradictions(
  input: { agentId: string; sinceTs?: number },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester("minni_subscribe_contradictions", {
    agent_id: input.agentId,
    since_ts: input.sinceTs,
  }, requester);
}

/**
 * hooks-PL-3 shared PreCompact leg, part 1: fetch the current stale-belief /
 * contradiction events for an agent. Shared by all four hook binaries so the
 * extraction logic cannot drift one-sided (the repo's #1 bug class).
 */
export async function fetchStaleBeliefEvents(
  agentId: string,
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<{ ok: boolean; events: unknown[]; error?: string }> {
  const contradictions = await subscribeContradictions({ agentId }, requester);
  const events =
    contradictions.ok && Array.isArray((contradictions.data as any)?.events)
      ? ((contradictions.data as any).events as unknown[])
      : [];
  return { ok: contradictions.ok, events, error: contradictions.error };
}

/**
 * hooks-PL-3 shared PreCompact leg, part 2 (Claude Code / kilocode): stash
 * non-empty stale-belief events durably in the vault inbox as a dedicated
 * "precompact_reassert" entry so the post-compaction boot re-asserts them
 * (corrections_reassert) even if the daemon is down at next boot. Returns the
 * inbox path, or undefined when there was nothing to stash. (codex/grok carry
 * the same stale_belief_events field on their precompact handoff payloads.)
 */
export async function stashPrecompactReassert(input: {
  vaultPath: string;
  sessionId: string;
  agentId: string;
  staleBeliefEvents: unknown[];
  trigger?: string;
}): Promise<string | undefined> {
  if (input.staleBeliefEvents.length === 0) return undefined;
  const entry = await writeInbox(input.vaultPath, input.sessionId, {
    kind: "precompact_reassert",
    agent_id: input.agentId,
    stale_belief_events: input.staleBeliefEvents,
    compaction_trigger: input.trigger || "compaction in progress",
  });
  return entry.filePath;
}

export async function jsonRpcSocketRequestWithFallback(method: string, params: Record<string, unknown>): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester(method, params, jsonRpcSocketRequest);
}

async function jsonRpcSocketRequestWithFallbackRequester(
  method: string,
  params: Record<string, unknown>,
  requester: JsonRpcRequester,
): Promise<JsonResult> {
  let last: JsonResult = { ok: false, error: "No socket attempted" };
  for (const socketPath of jsonRpcSocketCandidates()) {
    last = await requester(socketPath, method, params);
    if (last.ok) return last;
  }
  return last;
}

export async function compileVault(
  input: {
    passName?: string;
    vaultPath?: string;
    dryRun?: boolean;
  },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  const socketCandidates = jsonRpcSocketCandidates();
  let last: JsonResult = { ok: false, error: "No socket attempted" };
  for (const socketPath of socketCandidates) {
    last = await requester(socketPath, "daemon.compile", {
      pass_name: input.passName ?? "session_distillation",
      vault_path: input.vaultPath,
      dry_run: input.dryRun ?? true,
    });
    if (last.ok) return last;
  }
  return last;
}

function firstLine(text: string): string {
  return text.split("\n").find((line) => line.trim().length > 0)?.trim() ?? "";
}

function compactDaemonResults(results: string | unknown[] | undefined): string {
  if (Array.isArray(results)) return JSON.stringify(results, null, 2);
  if (!results) return "No daemon recall results.";
  return results;
}

function formatVaultContext(results: VaultSearchResult[]): string {
  if (results.length === 0) return "No Codex vault wiki matches.";
  return results
    .map((result, index) => {
      const snippet = result.snippet.replace(/\s+/g, " ");
      return `${index + 1}. ${result.wikilink} (vault score=${result.score})\n   ${snippet}`;
    })
    .join("\n");
}

export function formatRecall(query: string, response: RecallResponse, vaultResults: VaultSearchResult[] = []): string {
  const backendBadge = response.backend ?? response.backend_badge;
  const results = Array.isArray(response.results)
    ? JSON.stringify(response.results, null, 2)
    : response.results ?? "No recall results.";
  const provenance = [
    response.agent_id ? `agent=${response.agent_id}` : undefined,
    response.layer ? `layer=${response.layer}` : undefined,
    response.workspace_id ? `workspace=${response.workspace_id}` : undefined,
  ]
    .filter(Boolean)
    .join(", ");
  const daemonLead = firstLine(compactDaemonResults(response.results));
  const sections = [
    "# Sovereign Recall",
    `Query: ${query}${backendBadge ? ` [${backendBadge}]` : ""}`,
    provenance ? `Provenance: ${provenance}` : undefined,
    "## AI Context Pack",
    formatVaultContext(vaultResults),
    daemonLead ? `Daemon lead: ${daemonLead}` : undefined,
    "## Daemon Results",
    results,
  ].filter(Boolean);
  return sections.join("\n\n");
}

/**
 * X5 / W5: decide whether to run the local `searchVaultNotes` pre-scan for a
 * recall.
 *
 * The local pre-scan reads Markdown straight off disk and is NOT subject to the
 * daemon's workspace scoping / read-privacy policy. When the daemon recall
 * succeeds WITH HITS, its (properly scoped) results are authoritative and the
 * unscoped local snippets must not be injected alongside them.
 *
 * W5 (punch-list #1, recall vs prepare_task asymmetry): the daemon's `search`
 * RPC returns JSON-RPC SUCCESS even when it has zero hits (results: [],
 * count: 0) — that is a live, scoped, empty answer, not an outage. The
 * original 2-arg gate (`includeVault && !daemonOk`) suppressed the pre-scan on
 * that daemon-empty case exactly like it does on a genuine outage, which made
 * minni_recall blinder than prepare_task's markdown/AFM path (which has no
 * such gate at all). `daemonEmpty` widens the trigger to cover "daemon
 * answered but empty" alongside "daemon unreachable" — the pre-scan is still
 * an OFFLINE/DEGRADED fallback, never injected alongside a nonempty daemon
 * answer, and the accepted tradeoff (unscoped local snippets can now surface
 * on a workspace-scoped-empty query, not only on a hard outage) is the punch
 * list's explicit ask. This must NOT be OR'd with an identity denial — a
 * denial is a refusal, not an empty, and callers must keep gating this
 * function on `!identityDenied` (see recallResponseText / server.ts's
 * minni_recall handler; recovery-denial.test.mjs's reserved_agent_id case
 * pins that invariant).
 */
export function shouldPrescanVault(daemonOk: boolean, includeVault: boolean, daemonEmpty: boolean = false): boolean {
  return includeVault && (!daemonOk || daemonEmpty);
}

/**
 * W5: detect "the daemon answered but has nothing" from the wire shape, not
 * from English prose. Prefers `count` (recall.py response_payload always
 * pairs it with `results`) over inspecting `results` itself; falls back to
 * array length, then to a known empty-results sentinel string for the rare
 * caller-synthesized RecallResponse that has neither `count` nor an array.
 */
export function isDaemonResultEmpty(response: RecallResponse | undefined): boolean {
  if (!response) return true;
  // Review r1 (P1): `count` covers document results only. A reply whose
  // documents were removed by the auth_suppression read gate, or that matched
  // only the separate `learnings` array, is a live SCOPED answer — treating
  // it as empty would trigger the workspace-unscoped vault pre-scan and bury
  // the suppression diagnostic under local markdown snippets.
  if (Array.isArray(response.auth_suppression) && response.auth_suppression.length > 0) return false;
  if (Array.isArray(response.learnings) && response.learnings.length > 0) return false;
  if (typeof response.count === "number") return response.count === 0;
  const { results } = response;
  if (Array.isArray(results)) return results.length === 0;
  if (typeof results === "string") return results.trim().length === 0 || results.trim() === "No recall results.";
  return results === undefined;
}

/**
 * Decide the minni_recall response text for a daemon recall result (#132 P1).
 * An identity-recovery denial is neither a recall miss nor a daemon outage:
 * it must surface the remediation route verbatim — never "No recall results"
 * (fake success) and never the "Daemon unavailable" offline-fallback framing
 * (the daemon answered; the caller's identity is unprovisioned).
 */
export function recallResponseText(
  query: string,
  result: JsonResult<RecallResponse>,
  vaultResults: VaultSearchResult[],
): string {
  // W5: pass vaultResults through — a daemon-empty answer is still `result.ok`,
  // and shouldPrescanVault (server.ts) may now have populated vaultResults for
  // exactly that case (punch-list #1). Discarding them here would silently
  // defeat the daemon-empty pre-scan fix by rendering "No Codex vault wiki
  // matches." even when the local scan found something.
  if (result.ok && result.data) return formatRecall(query, result.data, vaultResults);
  const recovery = recoveryRouteFrom(result.data);
  if (recovery) {
    return [
      "Recall denied — Minni identity recovery required (not a recall miss).",
      JSON.stringify(recovery, null, 2),
    ].join("\n");
  }
  // #132 P2: a routeless -32004 (e.g. reserved_agent_id, capability_denied)
  // is still a live daemon answer — surface its diagnostic, never the
  // "Daemon unavailable" offline framing or the unscoped local fallback.
  const denial = identityDenialFrom(result.data);
  if (denial) {
    // W5 (punch-list #4): cross_agent's default-deny (principal.py:798-803,
    // allows_cross_agent_recall) is correct-by-design policy, not an identity
    // misconfiguration — say so, name the capability, give the remedy. Every
    // OTHER -32004 (reserved_agent_id, other capability_denied methods) keeps
    // the original "misconfiguration, not an outage" framing: those really
    // are unprovisioned/misrouted identities (recovery-denial.test.mjs's
    // reserved_agent_id case pins this — do not regress).
    if (isCrossAgentCapabilityDenial(denial)) {
      return crossAgentDenialText(denial);
    }
    return `Recall denied by the daemon (identity/authz misconfiguration, not an outage): ${denial}`;
  }
  if (vaultResults.length) {
    return formatRecall(
      query,
      { results: "Daemon unavailable — offline vault fallback (workspace-unscoped)." },
      vaultResults,
    );
  }
  return `Recall failed: ${result.error}`;
}

/**
 * W5 (punch-list #4): match ONLY the exact cross_agent capability_denied
 * message shape emitted by principal.py:806-822 (`make_capability_denied_error`,
 * called from recall.py:165 for `cross_agent` specifically). The SAME helper
 * stamps -32004s for other capabilities (provenance.py:117,
 * enforce_method_capability) and for reserved_agent_id/unknown_identity
 * (provenance.py:98-116) with a similarly-shaped message — this regex must
 * leave those on the existing "not-an-outage, no-fallback" path untouched.
 *
 * Known coupling risk: this keys off Python-owned prose (principal.py:814).
 * If that message wording changes, this silently stops matching and
 * cross_agent denials fall back to the generic (still-correct, just less
 * ergonomic) framing above — fails safe, no crash. FOLLOW-UP (out of scope
 * for this package): stamp `error.data = {capability, method}` server-side
 * (JSON-RPC 2.0's `data` field is exactly for this, and the identity-recovery
 * path already does it — see recoveryRouteFrom) so the TS side can switch to
 * a structural check instead of parsing English.
 */
export function isCrossAgentCapabilityDenial(denialMessage: string): boolean {
  return /^capability_denied: 'cross_agent'/.test(denialMessage);
}

/** W5: the reworded cross_agent denial text — names the capability and the
 *  remedy, and deliberately never says "misconfiguration" (that word implies
 *  something is broken; a default-deny capability gate is working as designed). */
export function crossAgentDenialText(denial: string): string {
  return (
    "Cross-agent search denied by the daemon: this is a default-deny policy " +
    "(capability 'cross_agent' is off by default for this principal) — grant it " +
    "via the operator principal config, or omit cross_agent to search personal " +
    `scope. (${denial})`
  );
}

/**
 * W5 (punch-list #4c): pure formatter for the cross_agent graceful-degrade
 * path. Mirrors formatRecall's shape (so it reads identically to a normal
 * recall) but leads with an explicit note that cross-agent was denied and
 * these are personal-scope results instead — per graceful-degradation best
 * practice, the substitution must be visible, never silent. Independently
 * unit-testable without a fake daemon: callers pass the ALREADY-RESOLVED
 * personal-scope RecallResponse (the retry itself is an async RPC call and
 * lives in recallCrossAgentDegrade, keeping this function pure).
 */
export function formatCrossAgentDegrade(
  query: string,
  personalResponse: RecallResponse,
  vaultResults: VaultSearchResult[] = [],
): string {
  const note =
    "Note: cross-agent search was denied by design default (capability 'cross_agent' " +
    "is off for this principal) — showing personal-scope results instead.";
  return [note, "", formatRecall(query, personalResponse, vaultResults)].join("\n");
}

/**
 * W5 (punch-list #4c): on a cross_agent capability denial, gracefully degrade
 * IN-BAND to a personal-scope retry of the same query rather than returning
 * only an error. The retry is strictly narrower/safer than the denied
 * cross_agent call (personal scope is a subset), so it cannot fail for a NEW
 * reason the original call didn't already risk.
 *
 * Guarded two ways (belt-and-suspenders against a misfiring classifier or an
 * already-personal-scope call retrying itself into a loop):
 *  1. `denial` must match isCrossAgentCapabilityDenial exactly.
 *  2. `crossAgentRequested` must be the ORIGINAL request's own cross_agent
 *     flag (=== true), not inferred from the denial text alone.
 *
 * Returns undefined when the degrade path does not apply, or when the
 * personal-scope retry itself fails/comes back empty — callers should then
 * fall back to recallResponseText's plain (reworded) denial text.
 */
export async function recallCrossAgentDegrade(
  input: {
    query: string;
    layer?: string;
    limit?: number;
    workspaceId?: string;
    agentId?: string;
  },
  crossAgentRequested: boolean,
  denial: string | undefined,
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<string | undefined> {
  if (!denial || !isCrossAgentCapabilityDenial(denial) || crossAgentRequested !== true) {
    return undefined;
  }
  const personalRetry = await recallMemory(
    {
      query: input.query,
      layer: input.layer,
      limit: input.limit,
      workspaceId: input.workspaceId,
      agentId: input.agentId,
      scope: "personal",
      crossAgent: false,
    },
    requester,
  );
  if (!personalRetry.ok || !personalRetry.data) return undefined;
  return formatCrossAgentDegrade(input.query, personalRetry.data, []);
}

/**
 * Per-turn lean recall (UserPromptSubmit). Two reductions vs formatRecall:
 *  1. Drops the verbose per-result provenance (score ranks, decay_factor,
 *     query_variants, rrf/cross_encoder, trace_id, source path) — keeps only
 *     wikilink + rounded score + a short headline/snippet.
 *  2. Omits identity-layer "shelf" hits (boot agent-envelopes): those are loaded
 *     once at SessionStart and never change, so re-injecting them every turn is
 *     pure redundancy.
 * Together this roughly halves the per-turn recall payload. SessionStart still
 * uses the full formatRecall so boot/rehydration keeps complete context.
 */
export function formatRecallLean(
  query: string,
  response: RecallResponse,
  vaultResults: VaultSearchResult[] = [],
  limit = 5,
): string {
  const arr = Array.isArray(response.results) ? response.results : [];
  const lean = arr
    .map((r) => (r ?? {}) as Record<string, unknown>)
    .filter((r) => String(r.layer ?? "") !== "identity")
    .slice(0, limit)
    .map((r) => {
      const wikilink =
        typeof r.wikilink === "string"
          ? r.wikilink
          : typeof r.filename === "string"
            ? r.filename
            : "[[?]]";
      const score =
        typeof r.score === "number" ? Number((r.score as number).toFixed(2)) : undefined;
      const src = typeof r.src === "string" ? r.src : undefined;
      const headRaw = (r.headline ?? r.snippet ?? "") as unknown;
      const headline =
        typeof headRaw === "string" && headRaw.trim()
          ? headRaw.replace(/\s+/g, " ").slice(0, 140)
          : undefined;
      return { wikilink, src, score, headline };
    });
  const omitted = arr.length - lean.length;
  const sections = [
    "# Recall (lean)",
    `Query: ${query}`,
    "## AI Context Pack",
    formatVaultContext(vaultResults),
    lean.length
      ? "## Daemon Results (wikilink + headline; full provenance and identity-shelf hits omitted to save context — pull via minni_recall if needed)\n" +
        JSON.stringify(lean, null, 2)
      : "No non-identity daemon recall results.",
    omitted > 0
      ? `(${omitted} identity-shelf/extra hit(s) omitted; identity shelf is loaded at SessionStart.)`
      : undefined,
  ].filter(Boolean);
  return sections.join("\n\n");
}

export async function buildStatusReport(input?: {
  vaultPath?: string;
  socket?: JsonResult;
  afm?: JsonResult;
  afmProviderMode?: AfmProviderMode;
  /** Test seam: pre-computed verified-generation health (skips the live probe). */
  afmGeneration?: ProviderHealth;
  /** Test seam: transport for the 1-token generation probe. */
  afmGenerationTransport?: (url: string, payload: Record<string, unknown>) => Promise<JsonResult>;
  afmGenerationTtlMs?: number;
}): Promise<StatusReport> {
  const vaultPath = input?.vaultPath ?? DEFAULT_VAULT_PATH;
  await ensureVault(vaultPath);
  const tail = await auditTail(vaultPath, 1);

  // Fetch the daemon socket status BEFORE computing our own AFM verdict: when
  // the daemon is reachable it has ALREADY run afm_runtime_status() with its
  // own real (matching) probe budget (afm_provider.py:376-421, embedded here
  // as socket.data.afm by health.py:156-189). Reordered so the reuse check
  // below (daemonAfmToProviderHealth) can skip a second, independently-timed
  // generation probe entirely when that fresh result is usable.
  const socket = input?.socket ?? (await socketHealth());

  const rawAfm = input?.afm ?? (await afmHealth());

  let volume = 0;
  const logFiles = ["log.md", "log.1.md", "log.2.md", "log.3.md"];
  for (const name of logFiles) {
    try {
      const st = await stat(path.join(vaultPath, name));
      volume += st.size;
    } catch {}
  }
  const logsDir = path.join(vaultPath, "logs");
  try {
    const dailyNames = await readdir(logsDir);
    for (const name of dailyNames) {
      if (name.endsWith(".md")) {
        const st = await stat(path.join(logsDir, name));
        volume += st.size;
      }
    }
  } catch {}

  const afmProvider = resolveAfmProvider(input?.afmProviderMode ?? AFM_PROVIDER_MODE, {
    nativeHelperPath: resolvedNativeHelperPath(),
    health: rawAfm,
  });

  // Single-authority AFM verdict (punch-list §3): reuse the daemon's own
  // fresh, matching-mode probe when it's usable instead of spawning a second,
  // independently-timed generation probe (no cold native-helper subprocess,
  // no redundant bridge chat-completion call). daemonAfmToProviderHealth
  // returns undefined — falling through to the plugin's own probe below —
  // whenever the socket is unreachable, the daemon payload has no `afm`
  // block at all (back-compat: identical to "socket unreachable"), the
  // payload shape doesn't match (untrusted input), the provider mode doesn't
  // match this process's resolved provider, or the probe is stale.
  const ttlMs = input?.afmGenerationTtlMs ?? GENERATION_PROBE_TTL_MS;
  const daemonAfmData = socket.ok ? (socket.data as Record<string, unknown> | undefined)?.afm : undefined;
  const daemonGeneration = daemonAfmToProviderHealth(daemonAfmData, afmProvider.provider, ttlMs, Date.now);

  let generation: ProviderHealth;
  let source: "daemon" | "plugin-probe";
  if (input?.afmGeneration) {
    generation = input.afmGeneration;
    source = "plugin-probe";
  } else if (daemonGeneration) {
    generation = daemonGeneration;
    source = "daemon";
  } else {
    generation = await getAfmProviderHealth({
      mode: afmProvider.provider,
      // The HTTP /health result gates the generation probe only for the bridge
      // provider; in native mode a dead bridge must not veto the probe — the
      // native helper is exercised directly (mirror of afm_runtime_status).
      health: afmProvider.provider === "bridge" ? rawAfm : undefined,
      transport: input?.afmGenerationTransport,
      ttlMs: input?.afmGenerationTtlMs,
      nativeHelperPath: resolvedNativeHelperPath(),
    });
    source = "plugin-probe";
  }
  const sanitizedAfm = sanitizeAfmHealth(rawAfm);
  const generationError =
    generation.generationVerified
      ? undefined
      : generation.detail ?? (afmProvider.provider === "bridge" ? sanitizedAfm.error : undefined);
  // afm_ok is redefined as generationVerified (field name kept for envelope compat):
  // a verified 1-token completion within the probe TTL, not mere /health reachability.
  const afm: JsonResult<Record<string, unknown>> = {
    ok: generation.generationVerified,
    data: {
      ...sanitizedAfm.data,
      reachable: generation.reachable,
      generationVerified: generation.generationVerified,
      // Authority tag (punch-list §3c): which leg produced this verdict, so a
      // consumer never has to diff socket.data.afm against this field to spot
      // a contradiction — when source is "daemon" the two are, by construction,
      // the same probe.
      source,
    },
    error: generationError,
  };

  return {
    vault: {
      path: vaultPath,
      exists: await vaultExists(vaultPath),
    },
    socket,
    afm,
    afmProvider,
    extractor: {
      provider: afmProvider.provider,
      tier: "local",
      source,
      generationVerified: generation.generationVerified,
      probeAgeMs: generation.probeAgeMs,
    },
    audit: {
      entries: tail.entries.length,
      latest: tail.entries.at(-1),
      volume,
    },
  };
}

export async function statusAndAudit(vaultPath = DEFAULT_VAULT_PATH): Promise<StatusReport> {
  const report = await buildStatusReport({ vaultPath });
  await recordAudit(vaultPath, {
    tool: "minni_status",
    summary: `socket=${report.socket.ok ? "ok" : "error"} afm=${report.afm.ok ? "ok" : "error"}`,
    details: report as unknown as Record<string, unknown>,
  });
  return report;
}

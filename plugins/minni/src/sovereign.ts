import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { existsSync } from "node:fs";
import { stat, readdir } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { URL } from "node:url";
import { AFM_HEALTH_URL, AFM_PROVIDER_MODE, DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH, DEFAULT_WORKSPACE_ID, SOCKET_PATH } from "./config.js";
import { resolveAfmProvider, sanitizeAfmHealth, type AfmProviderMode, type AfmProviderResolution } from "./afm.js";
import { auditTail, ensureVault, recordAudit, vaultExists } from "./vault.js";
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
}

export interface ReadContextResponse {
  agent_id?: string;
  context?: string;
  backend?: string;
  backend_badge?: string;
}

export interface StatusReport {
  vault: {
    path: string;
    exists: boolean;
  };
  socket: JsonResult;
  afm: JsonResult;
  afmProvider: AfmProviderResolution;
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

export async function recallMemory(input: {
  query: string;
  agentId?: string;
  layer?: string;
  workspaceId?: string;
  limit?: number;
}): Promise<JsonResult<RecallResponse>> {
  // Daemon is JSON-RPC only; surface its real result/error (e.g. identity_mismatch)
  // directly instead of masking it behind a dead HTTP-over-socket fallback.
  return jsonRpcSocketRequestWithFallback("search", {
    query: input.query,
    agent_id: input.agentId ?? DEFAULT_AGENT_ID,
    layers: input.layer ? [input.layer] : undefined,
    limit: input.limit,
  }) as Promise<JsonResult<RecallResponse>>;
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

export type JsonRpcRequester = (socketPath: string, method: string, params: Record<string, unknown>) => Promise<JsonResult>;

function jsonRpcSocketCandidates(): string[] {
  return [SOCKET_PATH];
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
      finish({ ok: true, data: parsed.data?.result });
    });
    client.on("error", (error) => finish({ ok: false, error: error.message }));
    client.on("end", () => {
      if (!settled && data.trim()) {
        const parsed = parseSovrdJson<{ result?: unknown }>(data.trim());
        finish(parsed.ok ? { ok: true, data: parsed.data?.result } : parsed);
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

export async function drillMemory(
  input: {
    resultIds?: number[];
    chunkIds?: number[];
    depth?: "snippet" | "chunk" | "document";
  },
  requester: JsonRpcRequester = jsonRpcSocketRequest,
): Promise<JsonResult> {
  return jsonRpcSocketRequestWithFallbackRequester("sm_drill", {
    result_ids: input.resultIds,
    chunk_ids: input.chunkIds,
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
      const headRaw = (r.headline ?? r.snippet ?? "") as unknown;
      const headline =
        typeof headRaw === "string" && headRaw.trim()
          ? headRaw.replace(/\s+/g, " ").slice(0, 140)
          : undefined;
      return { wikilink, score, headline };
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
}): Promise<StatusReport> {
  const vaultPath = input?.vaultPath ?? DEFAULT_VAULT_PATH;
  await ensureVault(vaultPath);
  const tail = await auditTail(vaultPath, 1);
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

  return {
    vault: {
      path: vaultPath,
      exists: await vaultExists(vaultPath),
    },
    socket: input?.socket ?? (await socketHealth()),
    afm: sanitizeAfmHealth(rawAfm),
    afmProvider: resolveAfmProvider(input?.afmProviderMode ?? AFM_PROVIDER_MODE, {
      nativeHelperPath: process.env.MINNI_AFM_NATIVE_HELPER,
      health: rawAfm,
    }),
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

/**
 * bridge.ts — Unix socket HTTP client for sovereign-memory daemon
 * Handles reconnect logic and request/response serialization.
 *
 * PROVENANCE: Every call passes agentId so sovrd tags writes with agent=<agentId>.
 * This preserves "who made it" across the fleet (Infektyd's solo-pillar theory).
 *
 * Phase 2: Added layer + workspace_id params to recall/learn for
 * identity/episodic/knowledge filtering and cross-agent workspace scoping.
 */

import * as http from "http";
import * as fs from "fs";
import * as path from "path";
import type { MemoryLayer } from "./types.js";

const SOCKET_PATH = "/tmp/sovereign.sock";
const MAX_RECONNECTS = 5;
const RECONNECT_DELAY_MS = 1000;

export interface HealthResponse {
  status: "ok";
  agent: string;
}

export interface RecallRequest {
  q: string;
  agent_id?: string;
}

export interface RecallResponse {
  results: string | any[];
}

export interface LearnRequest {
  content: string;
  category?: string;
  agent_id?: string;
}

export interface LearnResponse {
  status: "learned";
  result: any;
}

export interface IdentityResponse {
  identity: any;
}

export interface FullResponse {
  context: any;
}

export interface ReadRequest {
  key: string;
  agent_id?: string;
}

export interface ReadResponse {
  results: any[];
}

export interface ErrorResponse {
  error: string;
}

/**
 * Make an HTTP request over Unix socket with reconnect logic.
 */
function socketRequest<T>(
  method: "GET" | "POST",
  endpoint: string,
  body?: object,
  reconnectsLeft: number = MAX_RECONNECTS
): Promise<T> {
  return new Promise((resolve, reject) => {
    if (!fs.existsSync(SOCKET_PATH)) {
      if (reconnectsLeft > 0) {
        setTimeout(() => {
          socketRequest<T>(method, endpoint, body, reconnectsLeft - 1)
            .then((val: T) => resolve(val))
            .catch((e: unknown) => reject(e));
        }, RECONNECT_DELAY_MS);
        return;
      }
      reject(new Error(`Socket not found: ${SOCKET_PATH}`));
      return;
    }

    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    let payload: string | undefined;
    if (body) {
      payload = JSON.stringify(body);
      headers["Content-Length"] = Buffer.byteLength(payload).toString();
    }

    const options: http.RequestOptions = {
      socketPath: SOCKET_PATH,
      path: endpoint,
      method,
      headers,
    };

    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          const parsed = JSON.parse(data);
          if (res.statusCode && res.statusCode >= 400) {
            reject(new Error((parsed as ErrorResponse).error || "Request failed"));
          } else {
            resolve(parsed as T);
          }
        } catch (e) {
          reject(e);
        }
      });
    });

    req.on("error", (err) => {
      if (reconnectsLeft > 0 && (err as NodeJS.ErrnoException).code === "ECONNREFUSED") {
        setTimeout(() => {
          socketRequest<T>(method, endpoint, body, reconnectsLeft - 1)
            .then((val: T) => resolve(val))
            .catch((e: unknown) => reject(e));
        }, RECONNECT_DELAY_MS);
        return;
      }
      reject(err);
    });

    if (payload) {
      req.write(payload);
    }
    req.end();
  });
}

/**
 * Health check endpoint.
 */
export async function health(): Promise<HealthResponse> {
  return socketRequest<HealthResponse>("GET", "/health");
}

/**
 * Recall memories by query with Phase 2 layer filtering and workspace scoping.
 */
export async function recall(
  query: string,
  agentId?: string,
  layer?: MemoryLayer,
  workspaceId?: string,
  limit?: number
): Promise<RecallResponse> {
  const params = new URLSearchParams();
  params.set("q", query);
  if (agentId) params.set("agent_id", agentId);
  if (layer) params.set("layer", layer);
  if (workspaceId) params.set("workspace_id", workspaceId);
  if (limit !== undefined) params.set("limit", String(limit));
  return socketRequest<RecallResponse>("GET", `/recall?${params.toString()}`);
}

/**
 * Learn new information with Phase 2 layer and workspace scoping.
 */
export async function learn(
  content: string,
  category?: string,
  agentId?: string,
  layer?: MemoryLayer,
  workspaceId?: string
): Promise<LearnResponse> {
  const body: Record<string, string | undefined> = {
    content,
    category,
    agent_id: agentId,
  };
  if (layer) body.layer = layer;
  if (workspaceId) body.workspace_id = workspaceId;
  return socketRequest<LearnResponse>("POST", "/learn", body);
}

/**
 * Read a specific memory by key.
 */
export async function read(key: string, agentId?: string): Promise<ReadResponse> {
  const params = new URLSearchParams();
  params.set("key", key);
  if (agentId) params.set("agent_id", agentId);
  return socketRequest<ReadResponse>("GET", `/read?${params.toString()}`);
}

/**
 * Get identity context for a specific agent.
 */
export async function identity(agentId?: string): Promise<IdentityResponse> {
  const params = new URLSearchParams();
  if (agentId) params.set("agent_id", agentId);
  const query = params.toString();
  return socketRequest<IdentityResponse>("GET", query ? `/identity?${query}` : "/identity");
}

/**
 * Get full context (identity + memory) for a specific agent.
 */
export async function full(agentId?: string): Promise<FullResponse> {
  const params = new URLSearchParams();
  if (agentId) params.set("agent_id", agentId);
  const query = params.toString();
  return socketRequest<FullResponse>("GET", query ? `/full?${query}` : "/full");
}

/**
 * Check if the daemon is running.
 */
export async function isRunning(): Promise<boolean> {
  try {
    const result = await health();
    return result.status === "ok";
  } catch {
    return false;
  }
}

// If run directly, perform a health check
if (require.main === module) {
  (async () => {
    try {
      const result = await health();
      console.log("Bridge health check:", JSON.stringify(result, null, 2));
    } catch (err) {
      console.error("Bridge health check failed:", err);
      process.exit(1);
    }
  })();
}

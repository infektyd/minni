/**
 * sovereign-manager.ts — SovereignMemoryManager implements OpenClaw's MemorySearchManager
 *
 * This is the TypeScript adapter wrapping the sovrd daemon (Python) via bridge.ts.
 * The sovrd daemon maintains a FAISS + FTS5 + reranker index and exposes
 * recall/learn/read/identity/full over a Unix domain socket.
 *
 * PROVENANCE: Every call passes the agentId so sovrd can tag writes with
 * agent=<agentId>, preserving "who made it" across the fleet.
 */

import * as fs from "fs";
import * as path from "path";
import * as os from "os";

import {
  health as bridgeHealth,
  recall as bridgeRecall,
  read as bridgeRead,
  learn as bridgeLearn,
  identity as bridgeIdentity,
  isRunning as bridgeIsRunning,
  type HealthResponse,
  type RecallResponse,
  type ReadResponse,
  type LearnResponse,
} from "./bridge.js";
import type { MemoryLayer } from "./types.js";
import { LAYER_FILTER_RULES } from "./types.js";

// ---------------------------------------------------------------------------
// Dual-write types
// ---------------------------------------------------------------------------

export interface LearnResult {
  success: boolean;
  sovereignId?: number;
  flatFileWritten?: boolean;
  flatFileError?: string;
}

// ---------------------------------------------------------------------------
// Re-export the OpenClaw contract types so downstream consumers can import
// them from this module without reaching into openclaw's internals.
// ---------------------------------------------------------------------------

export type MemorySource = "memory" | "sessions";

export interface MemorySearchResult {
  path: string;
  startLine: number;
  endLine: number;
  score: number;
  snippet: string;
  source: MemorySource;
  citation?: string;
}

export interface MemoryEmbeddingProbeResult {
  ok: boolean;
  error?: string;
}

export interface MemoryProviderStatus {
  backend: "builtin" | "qmd";
  provider: string;
  model?: string;
  files?: number;
  chunks?: number;
  dirty?: boolean;
  workspaceDir?: string;
  dbPath?: string;
  extraPaths?: string[];
  sources?: MemorySource[];
  custom?: Record<string, unknown>;
}

export interface MemorySyncProgressUpdate {
  completed: number;
  total: number;
  label?: string;
}

// ---------------------------------------------------------------------------
// MemorySearchManager contract
// ---------------------------------------------------------------------------

export interface MemorySearchManager {
  search(
    query: string,
    opts?: { maxResults?: number; minScore?: number; sessionKey?: string }
  ): Promise<MemorySearchResult[]>;

  readFile(params: {
    relPath: string;
    from?: number;
    lines?: number;
  }): Promise<{ text: string; path: string }>;

  status(): MemoryProviderStatus;

  sync?(params?: {
    reason?: string;
    force?: boolean;
    sessionFiles?: string[];
    progress?: (update: MemorySyncProgressUpdate) => void;
  }): Promise<void>;

  probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult>;
  probeVectorAvailability(): Promise<boolean>;
  close?(): Promise<void>;
}

// ---------------------------------------------------------------------------
// Result-parsing helpers
// ---------------------------------------------------------------------------

/**
 * /recall returns results as a multi-document markdown string.
 *
 * Live-tested shape:
 *   {"results": "### filename.md (score=-10.295)\n\ncontent here\n\n### other.md (score=-11.0)\n\n..."}
 *
 * We split on "### " boundaries and parse each block. This is the most
 * conservative parse possible — if the daemon ever returns a structured
 * array instead, we handle that gracefully too.
 */
function parseRecallResults(
  raw: RecallResponse,
  query: string,
  maxResults: number,
  minScore: number
): MemorySearchResult[] {
  const rawResults = raw.results;

  // If it's already an array, treat each element as a structured result.
  if (Array.isArray(rawResults)) {
    return rawResults
      .map((item: any, idx: number): MemorySearchResult | null => {
        const snippet = typeof item === "string" ? item : item.text || item.content || String(item);
        const p = typeof item === "string" ? `recall/block-${idx}.md` : item.path || item.file || `recall/block-${idx}.md`;
        const sc = typeof item === "number" ? item : (item.score ?? 0);
        if (sc < minScore) return null;
        return {
          path: p,
          startLine: 1,
          endLine: snippet.split("\n").length,
          score: normalizeScore(sc),
          snippet,
          source: "memory" as MemorySource,
          citation: `${p}#L1-L${snippet.split("\n").length}`,
        };
      })
      .filter(Boolean) as MemorySearchResult[];
  }

  // String-based multi-document format (the one we actually get from sovrd).
  if (typeof rawResults !== "string") {
    return [];
  }

  const blocks = rawResults.split(/(?=### )/g).filter((b) => b.trim());
  const results: MemorySearchResult[] = [];

  for (const block of blocks) {
    if (results.length >= maxResults) break;

    // Parse header: "### filename.md (score=-10.295)"
    const headerMatch = block.match(/^###\s+(.+?)\s*\(score=([^\)]+)\)/m);
    if (!headerMatch) continue;

    const fileName = headerMatch[1].trim();
    const rawScore = parseFloat(headerMatch[2]);
    const body = block.slice(headerMatch[0].length).trim();

    // Normalize score from sovrd's raw cosine distance to 0–1 range
    // sovrd uses negative cosine distance: more negative = worse match
    const normalizedScore = normalizeScore(rawScore);
    if (normalizedScore < minScore) continue;

    const lineCount = body.split("\n").length;
    results.push({
      path: fileName,
      startLine: 1,
      endLine: lineCount,
      score: normalizedScore,
      snippet: body,
      source: "memory" as MemorySource,
      citation: `${fileName}#L1-L${lineCount}`,
    });
  }

  return results;
}

/**
 * Normalize sovrd's raw score to 0.0–1.0 range.
 *
 * sovrd returns negative cosine distances (e.g. -10.3 = very good match,
 * -15.0 = weak match). We clamp and invert so 0.0 = worst, 1.0 = best.
 *
 * If the score is already in 0–1 range, pass through as-is.
 */
function normalizeScore(raw: number): number {
  // Already normalized?
  if (raw >= 0 && raw <= 1) return raw;

  // Negative cosine distance: map [-20, 0] → [0, 1]
  // Scores more negative than -20 floor to 0; scores close to 0 floor to 1.
  const clamped = Math.max(-20, Math.min(0, raw));
  return Math.round(((clamped + 20) / 20) * 1000) / 1000;
}

// ---------------------------------------------------------------------------
// SovereignMemoryManager
// ---------------------------------------------------------------------------

export class SovereignMemoryManager implements MemorySearchManager {
  constructor(
    private readonly agentId: string,
    private readonly workspaceId: string,
    private readonly vaultDir?: string
  ) {
    // Resolve vault directory (default: ~/wiki)
    if (!this.vaultDir) {
      this.vaultDir = path.join(os.homedir(), "wiki");
    }
  }

  // -----------------------------------------------------------------------
  // search(query, opts?) → MemorySearchResult[]
  // Phase 2: supports layer filtering via opts.layer
  // -----------------------------------------------------------------------
  async search(
    query: string,
    opts?: {
      maxResults?: number;
      minScore?: number;
      sessionKey?: string;
      layer?: MemoryLayer;
    }
  ): Promise<MemorySearchResult[]> {
    const maxResults = opts?.maxResults ?? 5;
    const minScore = opts?.minScore ?? 0;
    const layer = opts?.layer;

    // Apply layer-specific agent filtering:
    // - knowledge: fleet-shared, no agent_id filter needed
    // - identity/episodic/artifact: scoped to agent (default recall behavior)
    const filterAgentId = layer === "knowledge" ? "" : this.agentId;

    let effectiveQuery = query;
    let effectiveAgent = filterAgentId;

    // Override agent filter based on Layer 2 rules
    if (layer && LAYER_FILTER_RULES[layer]) {
      const rule = LAYER_FILTER_RULES[layer];
      if (rule.agentFilter === "none") {
        effectiveAgent = "";
      } else if (rule.agentFilter === "strict" && rule.agentTagTransform) {
        effectiveAgent = rule.agentTagTransform(this.agentId);
      }
    }

    try {
      const resp = await bridgeRecall(
        effectiveQuery,
        effectiveAgent || undefined,
        layer,
        this.workspaceId || undefined,
        maxResults
      );
      return parseRecallResults(resp, query, maxResults, minScore);
    } catch (err) {
      // FALLBACK: If sovrd is unreachable, return empty array.
      // Infektyd's constraint: "Do NOT block agent startup on Sovereign availability."
      console.warn(
        `[sovereign-memory] search() failed for agent ${this.agentId}:`,
        err instanceof Error ? err.message : String(err)
      );
      return [];
    }
  }

  // -----------------------------------------------------------------------
  // readFile({ relPath, from?, lines? }) → { text, path }
  // -----------------------------------------------------------------------
  async readFile(params: {
    relPath: string;
    from?: number;
    lines?: number;
  }): Promise<{ text: string; path: string }> {
    const { relPath, from, lines } = params;

    // Try sovrd first — it can read from the vault / DB.
    try {
      const resp = await bridgeRead(relPath, this.agentId);
      if (resp.results && Array.isArray(resp.results) && resp.results.length > 0) {
        const item = resp.results[0];
        const text = typeof item === "string" ? item : item.text || item.content || String(item);
        return { text, path: relPath };
      }
    } catch {
      // sovrd couldn't find it — fall back to filesystem.
    }

    // Fallback: read from the vault directory on disk.
    const vaultFile = path.join(this.vaultDir!, relPath);
    if (fs.existsSync(vaultFile)) {
      let fullText = fs.readFileSync(vaultFile, "utf-8");
      if (from !== undefined || lines !== undefined) {
        const allLines = fullText.split("\n");
        const start = from ?? 0;
        const end = lines !== undefined ? start + lines : allLines.length;
        fullText = allLines.slice(start, end).join("\n");
      }
      return { text: fullText, path: vaultFile };
    }

    throw new Error(`File not found: ${relPath} (vault: ${this.vaultDir})`);
  }

  // -----------------------------------------------------------------------
  // status() → MemoryProviderStatus
  // -----------------------------------------------------------------------
  status(): MemoryProviderStatus {
    return {
      backend: "qmd",        // Custom backends use "qmd" per the contract
      provider: "sovereign",
      model: "gte-large",    // Sovereign's embedding model
      sources: ["memory"],
      custom: {
        vault: this.vaultDir,
        agentId: this.agentId,
        workspaceId: this.workspaceId,
        socketPath: "/tmp/sovereign.sock",
        chunkSize: 512,
        chunkOverlap: 128,
      },
    };
  }

  // -----------------------------------------------------------------------
  // probeEmbeddingAvailability() → { ok: boolean; error?: string }
  // -----------------------------------------------------------------------
  async probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult> {
    try {
      const result = await bridgeHealth();
      return { ok: result.status === "ok" };
    } catch (err) {
      return {
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      };
    }
  }

  // -----------------------------------------------------------------------
  // probeVectorAvailability() → boolean
  // -----------------------------------------------------------------------
  async probeVectorAvailability(): Promise<boolean> {
    // sovrd always has FAISS loaded (it's the core recall engine).
    // If health passes, vector search is available.
    try {
      const result = await bridgeHealth();
      return result.status === "ok";
    } catch {
      return false;
    }
  }

  // -----------------------------------------------------------------------
  // close() — optional no-op (shared daemon, don't kill it)
  // -----------------------------------------------------------------------
  async close(): Promise<void> {
    // No-op: sovrd is a shared daemon managed by launchd / bridge-process.
    // We don't own its lifecycle.
  }

  // -----------------------------------------------------------------------
  // learn(content, category?, layer?) → { success, sovereignId?, flatFileWritten? }
  // Dual-write: primary to sovrd, mirror to flat-file workspace memory/
  // Phase 2: layer + workspace_id scoping
  // -----------------------------------------------------------------------
  async learn(
    content: string,
    category?: string,
    layer?: MemoryLayer
  ): Promise<LearnResult> {
    // 1️⃣ Primary: write to sovrd daemon (source of truth).
    let sovereignId: number | undefined;
    let sovrdOk = false;
    try {
      const resp = await bridgeLearn(
        content,
        category,
        this.agentId,
        layer,
        this.workspaceId
      );
      sovereignId = typeof resp.result === "number" ? resp.result : undefined;
      sovrdOk = resp.status === "learned" || resp.status === "duplicate";
    } catch (err) {
      console.warn(
        `[sovereign-memory] learn() sovrd write failed for ${this.agentId}:`,
        err instanceof Error ? err.message : String(err)
      );
    }

    // If the primary write failed, stop — no point in flat-file mirror.
    if (!sovrdOk) {
      return { success: false };
    }

    // 2️⃣ Best-effort mirror: write to workspace flat-file.
    const flatFileOk = await this._mirrorFlatFile(content, category);
    return {
      success: true,
      sovereignId,
      flatFileWritten: flatFileOk.ok,
      flatFileError: flatFileOk.error,
    };
  }

  /**
   * Attempt to append a learn entry to the flat-file workspace memory.
   * Path: ~/.openclaw/workspace-<agentId>/memory/YYYY-MM-DD.md
   * Format: `## YYYY-MM-DD HH:MM (category)\n<content>`
   * Best-effort: logs warning on failure but never throws.
   */
  private async _mirrorFlatFile(
    content: string,
    category?: string
  ): Promise<{ ok: boolean; error?: string }> {
    try {
      const workspaceDir = path.join(
        os.homedir(),
        ".openclaw",
        `workspace-${this.agentId}`
      );
      const memoryDir = path.join(workspaceDir, "memory");
      const today = new Date();
      const dateStr = today.toISOString().slice(0, 10); // YYYY-MM-DD
      const timeStr = today.toISOString().slice(11, 16); // HH:MM
      const dailyFile = path.join(memoryDir, `${dateStr}.md`);

      // Create dirs if needed.
      if (!fs.existsSync(memoryDir)) {
        fs.mkdirSync(memoryDir, { recursive: true });
      }

      const header = `## ${dateStr} ${timeStr} (${category ?? "general"})\n`;
      const entry = `${header}${content}\n\n`;

      fs.appendFileSync(dailyFile, entry, "utf-8");
      return { ok: true };
    } catch (err) {
      console.warn(
        `[sovereign-memory] flat-file mirror write failed for ${this.agentId}:`,
        err instanceof Error ? err.message : String(err)
      );
      return { ok: false, error: err instanceof Error ? err.message : String(err) };
    }
  }

  // -----------------------------------------------------------------------
  // sync() — optional no-op (Sovereign writes are direct to DB)
  // -----------------------------------------------------------------------
  async sync?(params?: {
    reason?: string;
    force?: boolean;
    sessionFiles?: string[];
    progress?: (update: MemorySyncProgressUpdate) => void;
  }): Promise<void> {
    // No-op: Sovereign handles writes internally via /learn.
  }
}

// ---------------------------------------------------------------------------
// Factory function matching OpenClaw's getMemorySearchManager pattern.
// ---------------------------------------------------------------------------

/**
 * Factory: getMemorySearchManager(agentId, workspaceId, vaultDir?)
 *
 * This is the entry point OpenClaw calls to instantiate the Sovereign backend.
 * Phase 2: workspaceId is required for per-agent DB scoping and layer filtering.
 */
export function getMemorySearchManager(
  agentId: string,
  workspaceId: string,
  vaultDir?: string
): SovereignMemoryManager {
  return new SovereignMemoryManager(agentId, workspaceId, vaultDir);
}

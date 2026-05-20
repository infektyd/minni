/**
 * types.ts — Sovereign-specific types for the OpenClaw adapter bridge.
 *
 * Phase 2 additions: ChunkMetadata schema, layer filtering types, workspace scoping.
 * These types mirror the schema expected by the sovrd daemon and the Sovereign DB.
 */

// ---------------------------------------------------------------------------
// Chunk Metadata Schema (Phase 2)
// ---------------------------------------------------------------------------

/** Memory layer classification — controls which data is shared vs. agent-private. */
export type MemoryLayer = "identity" | "episodic" | "knowledge" | "artifact";

/** Metadata attached to every chunk ingested into Sovereign Memory. */
export interface ChunkMetadata {
  // Identity (required)
  agent_id: string;        // "forge" | "syntra" | "recon" | "pulse" | "hermes"
  workspace_id: string;    // e.g., "workspace-syntra" | "workspace-default"

  // Provenance (required)
  source_path: string;     // Relative path: "wiki/architecture.md"
  chunk_index: number;     // Position in document (0-based)
  content_hash: string;    // SHA-256 of chunk text (dedup key)

  // Document context
  doc_id?: string;         // Stable ID for the source document
  title?: string;          // Extracted or inferred title
  doc_created?: string;    // ISO 8601
  doc_modified?: string;   // ISO 8601

  // Layer classification
  layer: MemoryLayer;
  /*
   * identity  → INENTITY.md, SOUL.md (agent-specific)
   * episodic  → memory/YYYY-MM-DD.md logs (per-agent)
   * knowledge → wiki/ documents (fleet-shared)
   * artifact  → task files, code outputs (by intent)
   */

  // Access control
  is_private: boolean;     // true = agent-private, false = fleet-shared

  // Quality signals
  is_code: boolean;        // Code block flag
  frontmatter?: object;    // Original YAML frontmatter if stripped
  header_lines?: number;   // For tables: how many header rows
  truncated?: boolean;     // true = exceeded max_tokens, truncated

  // Temporal (for recency ranking)
  learned_at: string;      // ISO 8601 when chunk was ingested
  accessed_at?: string;    // Last recall hit (updated on search)
}

// ---------------------------------------------------------------------------
// Recall Request / Response (updated for Phase 2)
// ---------------------------------------------------------------------------

export interface RecallRequest {
  q: string;
  agent_id?: string;
  layer?: MemoryLayer;
  workspace_id?: string;
  limit?: number;
}

export interface RecallResult {
  path: string;
  score: number;
  snippet: string;
  layer?: MemoryLayer;
  agent_id?: string;
  metadata?: Partial<ChunkMetadata>;
}

export interface LearnRequest {
  content: string;
  category?: string;
  agent_id?: string;
  layer?: MemoryLayer;
  workspace_id?: string;
}

// ---------------------------------------------------------------------------
// Layer Filtering Rules
// ---------------------------------------------------------------------------

/**
 * Per-layer filter behavior. Mirrors the Infektyd decisions:
 * - identity: strict agent_id → identity:{agent_id}
 * - episodic:  strict agent_id → {agent_id}
 * - knowledge: no agent_id filter (fleet-shared wiki)
 * - artifact:  default same-agent, opt-in cross-agent
 */
export const LAYER_FILTER_RULES: Record<MemoryLayer, {
  agentFilter: "strict" | "none" | "default";
  agentTagTransform?: (agentId: string) => string;
  isShared: boolean;
}> = {
  identity: {
    agentFilter: "strict",
    agentTagTransform: (id: string) => `identity:${id}`,
    isShared: false,
  },
  episodic: {
    agentFilter: "strict",
    isShared: false,
  },
  knowledge: {
    agentFilter: "none",
    isShared: true,
  },
  artifact: {
    agentFilter: "default",
    isShared: false,
  },
};

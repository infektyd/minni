"""
Sovereign Memory V3.1 — Centralized Configuration.

V3.1 changes:
- Removed all compression config (TurboQuant stripped entirely)
- Added FAISS index type config (flat → HNSW auto-switch)
- Added cross-encoder re-ranking config
- Added write-back memory config
- Added context window budgeting config
- Added markdown-aware chunking config
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class SovereignConfig:
    """All configuration in one place, overridable via env vars or constructor."""

    # Paths
    vault_path: str = os.environ.get(
        "SOVEREIGN_VAULT_PATH",
        os.path.expanduser("~/wiki/")
    )
    db_path: str = os.environ.get(
        "SOVEREIGN_DB_PATH",
        os.path.expanduser("~/.openclaw/sovereign_memory.db")
    )
    graph_export_dir: str = os.environ.get(
        "SOVEREIGN_GRAPH_DIR",
        os.path.expanduser("~/.openclaw/graphs/")
    )
    faiss_index_path: str = os.environ.get(
        "SOVEREIGN_FAISS_PATH",
        os.path.expanduser("~/.openclaw/sovereign_faiss.index")
    )

    # Multiple wiki paths to index alongside the vault
    wiki_paths: list = field(default_factory=lambda: [
        os.path.expanduser("~/wiki"),
    ])

    # Embedding model
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384
    # Optional downstream FAISS quantization. SQLite remains float32 truth.
    # Supported: "fp32" (default), "int8". Changing this requires reindex/rebuild.
    embedding_quantization: str = "fp32"

    # FAISS indexing
    # "flat" = brute-force (exact), "hnsw" = approximate (fast at scale)
    # "auto" = flat until hnsw_threshold vectors, then rebuild as HNSW
    faiss_index_type: str = "auto"
    hnsw_threshold: int = 50_000           # Switch from flat → HNSW at this count
    hnsw_m: int = 32                       # HNSW connections per node (higher = more accurate, more RAM)
    hnsw_ef_construction: int = 200        # HNSW build-time search width
    hnsw_ef_search: int = 128              # HNSW query-time search width

    # Retrieval
    fts_weight: float = 0.35              # RRF constant for FTS5 rank
    semantic_weight: float = 0.65         # RRF constant for semantic rank
    rrf_k: int = 60                       # Reciprocal Rank Fusion constant

    # Cross-encoder re-ranking
    reranker_enabled: bool = True
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_top_k: int = 20              # Re-rank top-K candidates from first pass
    reranker_final_k: int = 5             # Return top-K after re-ranking

    # Chunking (markdown-aware) — Phase 2 spec: 512 tokens, 128 overlap
    chunk_size: int = 512                 # Target tokens per chunk
    chunk_overlap: int = 128             # Overlap tokens between chunks
    chunk_strategy: str = "markdown"     # "markdown" (header-aware) or "sliding" (V3 behavior)

    # Phase 2 chunking refinements
    min_tokens: int = 64                 # Drop chunks below minimum (headers/fragments)
    max_tokens: int = 1024               # Hard cap — code blocks truncated at this limit
    sentence_snap: bool = True           # Snap to sentence boundaries, not raw token counts
    code_treatment: str = "single_chunk"  # "single_chunk" = preserve code blocks intact
    # Optional post-pass: merge adjacent same-heading chunks with cosine > 0.9.
    # Applies only to newly indexed or explicitly reindexed content.
    chunking_semantic_merge: bool = False

    # Write-back memory
    writeback_enabled: bool = True
    writeback_path: str = os.environ.get(
        "SOVEREIGN_WRITEBACK_PATH",
        os.path.expanduser("~/.openclaw/learnings/")
    )

    # Context window budgeting
    context_budget_tokens: int = 4096     # Max tokens to return in a single recall
    token_model: str = "cl100k_base"      # tiktoken encoding for counting

    # Feedback demotion (PR-9)
    feedback_enabled: bool = True

    # Query expansion (PR-7)
    # "rule" is default-on; "afm" is opt-in per request until eval-gated.
    query_expand_default: str = "rule"

    # Vault reorganization pass (PR-14)
    reorg_horizon_days: int = 30

    # HyDE cold-query second pass (PR-8)
    hyde_enabled: bool = True
    hyde_confidence_floor: float = 0.4

    # AFM self-organization loop (PR-12)
    # Opt-in. Set SOVEREIGN_AFM_LOOP=on/1/true to enable; "off" is a kill switch.
    afm_loop_schedule: dict = field(default_factory=lambda: {
        "enabled": os.environ.get("SOVEREIGN_AFM_LOOP", "off").lower() in {"1", "true", "yes", "on"},
        "idle_seconds": 300,
        "draft_ttl_days": 14,
        "passes": {
            "session_distillation": {
                "interval_seconds": 24 * 60 * 60,
                "lookback_hours": 24,
            },
            "synthesis": {
                "interval_seconds": 24 * 60 * 60,
                "stale_after_days": 30,
            },
            "procedure_extraction": {
                "interval_seconds": 24 * 60 * 60,
                "lookback_days": 90,
            },
            "reorganization": {
                "interval_seconds": 7 * 24 * 60 * 60,
            },
            "pruning": {
                "interval_seconds": 24 * 60 * 60,
            },
        },
    })

    # Memory decay (Phase 8)
    decay_half_life_days: float = 7.0
    decay_min_score: float = 0.05
    decay_cron_hour: int = 4

    # PR-3: Vector backend selection.
    # Supported values: "faiss-disk" (default), "faiss-mem".
    # Stubs (non-functional without extras): "qdrant", "lance".
    # Multiple values enable fan-out via multi.py; results merged with RRF.
    # Single value ["faiss-disk"] produces bit-identical results to pre-PR-3.
    vector_backends: list = field(default_factory=lambda: ["faiss-disk"])

    # Contradiction detection (PR-6)
    # Cosine similarity threshold above which two learnings are considered
    # contradictory. Callers can override per-request via the 'threshold' param.
    contradiction_threshold: float = 0.85

    # Thread propagation
    thread_bind_threshold: float = 0.55

    # Agent colors (for visualization)
    agent_colors: Dict[str, str] = field(default_factory=lambda: {
        "forge": "#00D4FF",
        "recon": "#6B5BFF",
        "heartbeat_router": "#FF00FF",
        "syntra": "#00FF88",
        "hermes": "#FF8800",
        "unknown": "#808080",
    })

    def ensure_dirs(self):
        """Create directories if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.graph_export_dir, exist_ok=True)
        if self.writeback_enabled:
            os.makedirs(self.writeback_path, exist_ok=True)


# Global default config — importable everywhere
DEFAULT_CONFIG = SovereignConfig()

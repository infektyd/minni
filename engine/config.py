"""
Minni V3.1 — Centralized Configuration.

V3.1 changes:
- Removed all compression config (TurboQuant stripped entirely)
- Added FAISS index type config (flat → HNSW auto-switch)
- Added cross-encoder re-ranking config
- Added write-back memory config
- Added context window budgeting config
- Added markdown-aware chunking config
"""

import json
import logging
import os
import stat as stat_module
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlparse

# G02 canonical home resolver (single source of truth for path defaults)
CANONICAL_SOVEREIGN_HOME: str = os.environ.get(
    "MINNI_HOME", os.path.expanduser("~/.minni")
)


def _positive_int_env(name: str, default: int) -> int:
    """Parse a positive-int env override; malformed or non-positive values
    fall back to the default rather than breaking config construction."""
    raw = (os.environ.get(name) or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return default


@dataclass
class SovereignConfig:
    """All configuration in one place, overridable via env vars or constructor."""

    # Paths — G02 unified canonical (prefer ~/.minni over legacy ~/.openclaw)
    vault_path: str = os.environ.get(
        "MINNI_VAULT_PATH",
        os.path.join(CANONICAL_SOVEREIGN_HOME, "vault/")
    )
    db_path: str = os.environ.get(
        "MINNI_DB_PATH",
        os.path.join(CANONICAL_SOVEREIGN_HOME, "minni.db")
    )
    graph_export_dir: str = os.environ.get(
        "MINNI_GRAPH_DIR",
        os.path.join(CANONICAL_SOVEREIGN_HOME, "graphs/")
    )
    faiss_index_path: str = os.environ.get(
        "MINNI_FAISS_PATH",
        os.path.join(CANONICAL_SOVEREIGN_HOME, "minni_faiss.index")
    )
    # Optional manifest override for explicit stores such as per-vault indexes.
    # When unset, FAISS persistence keeps the legacy db-dir-derived cache path.
    faiss_manifest_path: Optional[str] = None

    # Additional paths to index alongside the vault (operator-configurable)
    wiki_paths: list = field(default_factory=lambda: [])

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

    # Local NLI attribution scoring for claim/evidence support checks.
    attribution_enabled: bool = True
    attribution_model: str = "cross-encoder/nli-deberta-v3-small"

    # Reversible token-boundary perturbation of instruction-like evidence bodies.
    instruction_body_perturbation_enabled: bool = True

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
        "MINNI_WRITEBACK_PATH",
        os.path.join(CANONICAL_SOVEREIGN_HOME, "learnings/")
    )

    # Context window budgeting
    context_budget_tokens: int = 4096     # Max tokens to return in a single recall
    token_model: str = "cl100k_base"      # tiktoken encoding for counting

    # Proactive chunk trigger for native AFM calls (engine/afm_chunking.py) —
    # headroom below the ~4096-token model context window for the Swift
    # helper's instructions string + @Generable schema guide + JSON envelope
    # overhead, none of which is visible to Python/TypeScript callers.
    # Overridable via MINNI_AFM_INPUT_BUDGET_TOKENS (read at instance
    # creation); afm_chunking.resolve_afm_input_budget_tokens() consumes it.
    afm_input_budget_tokens: int = field(
        default_factory=lambda: _positive_int_env("MINNI_AFM_INPUT_BUDGET_TOKENS", 3200)
    )

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
    # Opt-in. Set MINNI_AFM_LOOP=on/1/true to enable; "off" is a kill switch.
    afm_loop_schedule: dict = field(default_factory=lambda: {
        "enabled": os.environ.get("MINNI_AFM_LOOP", "off").lower() in {"1", "true", "yes", "on"},
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
            # Drains the proposed candidate_packets queue into durable learnings.
            # Short interval so memory keeps moving; per-run cap bounds each tick.
            "consolidation": {
                "interval_seconds": 15 * 60,
                "max_per_run": 50,
                "max_batches_per_tick": 40,  # up to 40*50=2000 candidates/tick
                # Before draining, ingest inbox stop-candidate files
                # (<vault>/inbox/*.json — kind 'stop_candidates', legacy
                # 'codex_stop_candidates', or kind-less stop-candidate shape)
                # into candidate_packets so that channel stops piling up.
                # Idempotent; respects log_only/do_not_store; never deletes.
                "ingest_inbox": True,
                # Last-resort attribution when neither agent_id nor the vault
                # dir identifies the author. Never an agent name: Minni logic
                # is model-agnostic.
                "inbox_fallback_principal": "unknown",
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

    # Correction re-injection (audit cluster C1 / recall-F3, recall-F4).
    # Correction-class notes (corrections, contradiction resolutions, decisions,
    # fixes) carry a bounded salience channel so a fresh correction can outrank
    # a stale habitual hit whose decay saturated at 1.0 via access reinforcement.
    correction_page_types: tuple = ("correction", "contradiction", "decision", "fix")
    # Bounded multiplicative boost: final_score *= (1 + boost) for correction-class.
    correction_salience_boost: float = 0.25
    # Corrections younger than the grace window do not decay at all (recall-F4:
    # a 1-day-old unaccessed correction must not sit below a reread stale belief).
    correction_decay_grace_days: float = 7.0
    # After the grace window, corrections decay normally but never below this
    # floor, so a correction cannot fade below the belief it superseded.
    correction_decay_floor: float = 0.5

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


def correction_class_page_types(config) -> set:
    """Correction-class page types (recall-F3): notes that correct, supersede,
    or decide against a prior belief. Single source of truth shared by
    retrieval.py (salience boost) and decay.py (grace window + floor) so the
    two sides cannot drift. Falls back to the audited default set so
    duck-typed configs (eval harness) keep the salience channel."""
    raw = getattr(config, "correction_page_types", None) or (
        "correction", "contradiction", "decision", "fix",
    )
    return {str(t).lower() for t in raw}


# Global default config — importable everywhere
DEFAULT_CONFIG = SovereignConfig()


# --- Model provider chain config (P3) ----------------------------------------
# Mirrors plugins/minni/src/config.ts loadProvidersConfig with identical
# semantics. ~/.minni/providers.json configures the provider chain and
# per-operation routing policy; MINNI_AFM_* env vars keep precedence over file
# values. Secrets are NEVER stored in providers.json: cloud credentials come
# only from apiKeyEnv (env var name) or apiKeyFile (0600 file under
# ~/.minni/secrets/).

_DEFAULT_PROVIDERS_CONFIG: Dict[str, Any] = {
    "chain": ["afm"],
    "operations": {"retrieval": {"localOnly": True}},
    "providers": {},
}


def providers_config_path() -> str:
    return os.path.expanduser(
        os.environ.get(
            "MINNI_PROVIDERS_CONFIG",
            os.path.join(os.environ.get("MINNI_HOME", os.path.expanduser("~/.minni")), "providers.json"),
        )
    )


def load_providers_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load ~/.minni/providers.json; degrade to the AFM-only default on any error."""
    target = path or providers_config_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_PROVIDERS_CONFIG)
    if not isinstance(parsed, dict):
        return dict(_DEFAULT_PROVIDERS_CONFIG)
    chain = [item for item in parsed.get("chain", []) if isinstance(item, str) and item] if isinstance(
        parsed.get("chain"), list
    ) else list(_DEFAULT_PROVIDERS_CONFIG["chain"])
    operations = parsed.get("operations") if isinstance(parsed.get("operations"), dict) else dict(
        _DEFAULT_PROVIDERS_CONFIG["operations"]
    )
    providers = parsed.get("providers") if isinstance(parsed.get("providers"), dict) else {}
    cloud = providers.get("cloud")
    if isinstance(cloud, dict) and "apiKey" in cloud:
        # SEC: inline secrets are rejected outright — keys live in env or 0600 files only.
        logging.getLogger(__name__).warning(
            "providers.json: inline providers.cloud.apiKey is not allowed (use apiKeyEnv or apiKeyFile); cloud provider disabled"
        )
        cloud = {key: value for key, value in cloud.items() if key != "apiKey"}
        cloud["enabled"] = False
        providers = dict(providers)
        providers["cloud"] = cloud
    return {
        "chain": chain or ["afm"],
        "operations": operations,
        "providers": providers,
    }


def minni_secrets_dir() -> str:
    return os.path.join(os.environ.get("MINNI_HOME", os.path.expanduser("~/.minni")), "secrets")


def resolve_cloud_api_key(cloud: Optional[Dict[str, Any]], secrets_dir: Optional[str] = None) -> Dict[str, Any]:
    """Resolve the cloud provider API key (mirror of config.ts resolveCloudApiKey).

    Secrets come ONLY from apiKeyEnv or a 0600 apiKeyFile under
    ~/.minni/secrets/, never from providers.json. Returns {"key": ...} or a
    structured, key-free {"error": ...}.
    """
    if not cloud or cloud.get("enabled") is not True:
        return {}
    api_key_env = cloud.get("apiKeyEnv")
    if api_key_env:
        key = os.environ.get(str(api_key_env))
        if key:
            return {"key": key}
        return {"error": f"cloud_key_unavailable: env {api_key_env} is not set"}
    api_key_file = cloud.get("apiKeyFile")
    if api_key_file:
        resolved = os.path.realpath(os.path.expanduser(str(api_key_file)))
        root = os.path.realpath(secrets_dir or minni_secrets_dir()) + os.sep
        if not resolved.startswith(root):
            return {"error": "cloud_key_denied: apiKeyFile must live under ~/.minni/secrets/"}
        try:
            mode = os.stat(resolved).st_mode
            if not stat_module.S_ISREG(mode):
                return {"error": "cloud_key_denied: apiKeyFile must be a regular file"}
            if mode & (stat_module.S_IRWXG | stat_module.S_IRWXO):
                return {"error": "cloud_key_denied: apiKeyFile must be mode 0600 (no group/other access)"}
            with open(resolved, "r", encoding="utf-8") as handle:
                key = handle.read().strip()
            if key:
                return {"key": key}
            return {"error": "cloud_key_unavailable: apiKeyFile is empty"}
        except OSError:
            return {"error": "cloud_key_unavailable: apiKeyFile is not readable"}
    return {"error": "cloud_key_unavailable: cloud provider enabled without apiKeyEnv or apiKeyFile"}


# G13 (SEC-004): explicit operator allowlist for non-loopback model targets.
# MINNI_MODEL_ALLOWED_TARGETS is the provider-protocol alias for
# MINNI_AFM_ALLOWED_TARGETS; both are honored (union). Loopback always allowed;
# non-loopback targets additionally require HTTPS.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def model_allowed_targets() -> list:
    hosts = []
    for env_key in ("MINNI_AFM_ALLOWED_TARGETS", "MINNI_MODEL_ALLOWED_TARGETS"):
        for item in (os.environ.get(env_key) or "").split(","):
            item = item.strip()
            if item:
                hosts.append(item)
    return hosts


def check_model_target(target_url: str) -> Dict[str, Any]:
    """Mirror of afm.ts checkModelTarget: {"allowed": bool, "reason": str|None}."""
    if not target_url:
        return {"allowed": False, "reason": "invalid_url"}
    try:
        parsed = urlparse(target_url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return {"allowed": False, "reason": "invalid_url"}
    if not host:
        return {"allowed": False, "reason": "invalid_url"}
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return {"allowed": True, "reason": None}
    allowed = {item.lower() for item in model_allowed_targets()}
    if host not in allowed:
        return {"allowed": False, "reason": "not_allowlisted"}
    if parsed.scheme != "https":
        return {"allowed": False, "reason": "https_required"}
    return {"allowed": True, "reason": None}


def resolve_canonical_path(kind: str) -> str:
    """G02 single resolver: returns the unified default for a path kind.
    All call sites (config, sovrd, plugin parity) should go through this or
    the dataclass which now derives from CANONICAL_SOVEREIGN_HOME.
    Logs on MINNI_* env vs computed mismatch for operator visibility.
    """
    home = CANONICAL_SOVEREIGN_HOME
    mapping = {
        "home": home,
        "db": os.path.join(home, "minni.db"),
        "faiss": os.path.join(home, "minni_faiss.index"),
        "graph": os.path.join(home, "graphs/"),
        "writeback": os.path.join(home, "learnings/"),
        "vault": os.path.join(home, "vault/"),
        "socket": os.path.join(home, "run", "minnid.sock"),
    }
    val = mapping.get(kind, home)
    env_map = {
        "db": "MINNI_DB_PATH",
        "faiss": "MINNI_FAISS_PATH",
        "graph": "MINNI_GRAPH_DIR",
        "writeback": "MINNI_WRITEBACK_PATH",
        "vault": "MINNI_VAULT_PATH",
    }
    env_key = env_map.get(kind)
    if env_key and os.environ.get(env_key):
        env_val = os.path.expanduser(os.environ[env_key])
        if env_val != val:
            logging.getLogger(__name__).info(
                "Path mismatch: %s=%s but canonical for %s is %s (MINNI_HOME=%s)",
                env_key, env_val, kind, val, home
            )
    return val


# --- Workspace ID normalization (G14) ----------------------------------------
# Canonical convention: 'workspace-<lowercased basename of workspace path>'
# Examples: /Users/hansaxelsson/Projects/Minni -> 'workspace-minni'
#          /path/to/PROJECT -> 'workspace-project'
#          'workspace-minni' -> 'workspace-minni' (idempotent)
#          empty/None -> '' (passthrough)

def normalize_workspace_id(value: Optional[str]) -> str:
    """Normalize workspace_id to canonical form 'workspace-<basename>'.

    - If value is already 'workspace-*', lowercase and return it.
    - If value is a filesystem path, extract basename, lowercase, prepend 'workspace-'.
    - If empty or None, return empty string.
    """
    if not value:
        return ""
    value = str(value).strip()
    if not value:
        return ""
    # Already canonical form: normalize the suffix to lowercase
    if value.startswith("workspace-"):
        return "workspace-" + value[len("workspace-"):].lower()
    # Treat as filesystem path: extract basename, lowercase, prepend prefix
    basename = os.path.basename(value.rstrip("/"))
    if not basename:
        # Edge case: "/" -> use empty (no valid basename)
        return ""
    return "workspace-" + basename.lower()

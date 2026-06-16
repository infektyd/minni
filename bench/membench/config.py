"""Pinned membench configuration (§7.7).

Everything load-bearing for reproducibility and fairness is pinned HERE and
printed into the report header. Credentials are referenced by ENV-VAR NAME
only — never read into this module at import time, never a value (§7.14).

Slice s1 pins the fields the spec names. Fields used only by later slices
(agent/judge/curation models, lockfile hash) are present as typed stubs so the
names/types match the spec; their values are finalized in their owning slices.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Embedding model (fairness §7.2, open question §10.1 — decided in s1) ──────
# Minni's own embedding model. Every vector adapter (and Minni's own recall) use
# this one pinned id, so no adapter can win by using a better embedder. This is
# the exact model id Minni's engine uses (engine/config.py: embedding_model).
EMBEDDER_MODEL_ID: str = "all-MiniLM-L6-v2"
EMBEDDING_DIM: int = 384

# ── Canonical tokenizer (§7.8) ───────────────────────────────────────────────
CANONICAL_TOKENIZER_ID: str = "cl100k_base"
# Pinned tiktoken version is recorded in requirements.lock (later slice); the
# version actually loaded is printed into the report header at run time.

# ── Retrieval knobs (§6) ─────────────────────────────────────────────────────
K: int = 10  # default top-k, pinned (§6)
DEFAULT_MAX_TOKENS: int = 2048  # default TokenBudget.max_tokens (§3.1)
# TokenBudget.max_docs defaults to K (§3.1: "max_docs == k by default").

# ── Layer-2 trial count (§3.3) ───────────────────────────────────────────────
N: int = 5  # N trials per episode per adapter, pinned (§3.3)

# ── Per-band query minimums (§5.3) ───────────────────────────────────────────
MIN_PER_BAND: dict[str, int] = {
    "single-hop": 25,
    "multi-hop": 25,
    "contradiction": 20,
    "recency-sensitive": 20,
    "negatives": 20,
}

# ── Judge calibration minimum subset size (§3.3) ─────────────────────────────
JUDGE_MIN_SUBSET_N: int = 40

# ── API-cost guard (§7.15) ───────────────────────────────────────────────────
MAX_API_CALLS: int = 5000  # hard-abort ceiling on cumulative LLM calls

# ── Log-exposure defense in depth (§5.1) ─────────────────────────────────────
CONTEXT_LOG_TRUNCATE: int = 256  # max chars of context_string ever logged

# ── Determinism gate excluded fields (§3.2 / §9.1) ───────────────────────────
# Stripped (and ONLY these) before the byte-identical score comparison; every
# other field must survive byte-identical. Kept here AND mirrored in metrics.py
# per the spec's §11 placement; metrics.py is the gate's authority.
DETERMINISM_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {"wall_clock_ms", "build_wall_clock_ms"}
)

# ── Credential ENV-VAR NAMES (never values) (§7.14) ──────────────────────────
# config.py references credentials by env-var NAME only. Nothing here reads a
# secret at import time. Layer-2 / curation / hosted-embedding slices resolve
# these names from os.environ at call time.
CREDENTIAL_ENV_VARS: dict[str, str] = {
    "agent_api_key": "MEMBENCH_AGENT_API_KEY",
    "judge_api_key": "MEMBENCH_JUDGE_API_KEY",
    "llm_wiki_curation_api_key": "MEMBENCH_LLM_WIKI_API_KEY",
    "embedding_api_key": "MEMBENCH_EMBEDDING_API_KEY",
}

# ── Later-slice model pins (typed stubs; names/types match the spec) ──────────
# Finalized in s5 (judge calibration) / s3 / s6. Each model carries a
# model_family string (§3.3, §4.1). Empty strings here mean "not pinned yet";
# the owning slice sets them and CI then enforces the no-self-judge gate and the
# llm_wiki same-model_id rule.
@dataclass(frozen=True)
class ModelPin:
    model_id: str
    model_family: str


AGENT_MODEL: ModelPin = ModelPin(model_id="", model_family="")  # s5/s6
JUDGE_MODEL: ModelPin = ModelPin(model_id="", model_family="")  # s5
LLM_WIKI_CURATION_MODEL: ModelPin = ModelPin(model_id="", model_family="")  # s6
# CI rule (later slice): LLM_WIKI_CURATION_MODEL.model_id == AGENT_MODEL.model_id
# CI rule (later slice): AGENT_MODEL.model_family != JUDGE_MODEL.model_family

# ── Runtime / lockfile hash (§3.2) ───────────────────────────────────────────
# SHA-256 of requirements.lock, recorded in the report header so a float-bit
# perturbing runtime change is a config change, not a silent logic failure.
# Populated when the lockfile lands (later slice).
RUNTIME_LOCKFILE_HASH: str = ""

# ── Fixture corpus pin (§5.1, §9.5) ──────────────────────────────────────────
# Content-hash of the small PUBLIC synthetic fixture corpus, computed by
# membench.corpus.compute_content_hash over bench/membench/fixtures/
# corpus_synthetic/. Pinning it here exercises the loader's hash-mismatch
# refusal: load_corpus() recomputes and fails-closed if the tree is tampered.
FIXTURE_CORPUS_HASH: str = (
    "406588efeb21504653b657a992d779597514867e81a748b513db35e93faeadd7"
)

# The unique UUID embedded in exactly one fixture doc (03-teal-ledger.md), used
# by the doc-count over-count cross-check (§9.5).
FIXTURE_UNIQUE_UUID: str = "bb145163-d5e5-44a1-8869-214fd05a6b85"

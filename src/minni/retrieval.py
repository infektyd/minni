"""
Minni V3.1 — Retrieval Engine.

V3.1 changes over V3:
1. FAISS-backed semantic search (not raw numpy loops)
2. Cross-encoder re-ranking: first-pass retrieval is fast but approximate;
   second-pass re-ranking with a cross-encoder scores (query, passage) pairs
   directly for much higher precision
3. Context window budgeting: returns chunks up to a token budget, not just top-K
4. Heading context enrichment: chunks carry their heading breadcrumb
5. No compression anywhere — all vectors are raw float32[384]

PR-1b adds progressive disclosure depth tiers to retrieve():
  headline — wikilink, title, score, confidence, age_days (~30 tokens/result)
  snippet  — + text (≤280 chars) (~120 tokens/result)  [DEFAULT — zero change]
  chunk    — + full chunk text, heading_context, full provenance (~500 tokens)
  document — + full source document (whole_document=1 rows only)
"""

import time
import math
import concurrent.futures
import logging
import hashlib
import importlib.util
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Dict, Literal, Optional, Sequence, Tuple

import numpy as np

from minni.config import SovereignConfig, DEFAULT_CONFIG, correction_class_page_types
from minni.db import SovereignDB
from minni.episodic import NON_MEMORY_EVENT_TYPES as _NON_MEMORY_EVENT_TYPES
from minni.faiss_index import FAISSIndex
from minni.query_expand import expand as expand_query
from minni.query_expand import expand_with_status as expand_query_with_status
from minni.query_expand import summarize_with_afm

# G19/G20/G22: read gate + evidence envelopes
from minni.principal import EffectivePrincipal, agent_scope_for, can_read_document  # type: ignore
from minni.safety import is_instruction_like
from minni.timestamps import parse_epoch_or_report
from minni.wiki_indexer import WikiFrontmatter
from minni.request_deadline import (
    RequestDeadlineExceeded,
    allow_expired_sql,
    bind_copied_deadline,
    current_query_embed_cache,
    run_bound,
)

logger = logging.getLogger("sovereign.retrieval")


# Encode/FAISS/CE/HyDE cannot be cancelled once started inside to_thread.
# A binary elapsed check still launches those stages with a sliver of budget
# and the worker outlives DEFAULT_JSON_RPC_TIMEOUT_MS (30s).
# 1.0s still starts get_embedder()/get_cross_encoder() (HuggingFace download
# + uncancellable encode/predict). Match HyDE AFM's 2s remaining floor.
SEARCH_STAGE_MIN_REMAINING_S = 2.0
# Default leftover is 22.5s (25s * 0.9); MCP/CLI omit timeoutMs so
# recallMemory sends 30s → leftover 27s. 8s still starts get_embedder()
# / HF download that cannot finish before DEFAULT_JSON_RPC kill.
# Warm cache hits still use SEARCH_STAGE_MIN_REMAINING_S.
SEARCH_MODEL_LOAD_MIN_REMAINING_S = 27.0
# 2.1s leftover still passes SEARCH_STAGE_MIN_REMAINING_S; a cold/empty index
# with ~20s left must not wait on _faiss_load_lock for warmup/vault-watch's
# unbounded SELECT + FAISS build. Disk restore is <500ms and must run on
# default leftover (22.5s / 27s) when the lock is free; skip the rebuild
# after a disk miss, and skip the lock wait when leftover cannot finish a
# rebuild. Do not cancel an in-flight build. Default leftover is 22.5s
# (25s * 0.9); MCP/CLI omit timeoutMs so recallMemory sends 30s → leftover 27s.
SEARCH_FAISS_REBUILD_MIN_REMAINING_S = 27.0


def remaining_search_budget(deadline_monotonic: Optional[float]) -> Optional[float]:
    """Seconds left before ``deadline_monotonic``, or None if unbounded."""
    if deadline_monotonic is None:
        return None
    return deadline_monotonic - time.monotonic()


@contextmanager
def search_model_lock(lock, deadline_monotonic: Optional[float]):
    """Bound queueing by the stage budget; an acquired predict is not cancelled."""
    remaining = remaining_search_budget(deadline_monotonic)
    if remaining is None:
        acquired = lock.acquire()
    else:
        timeout = remaining - SEARCH_STAGE_MIN_REMAINING_S
        acquired = timeout > 0 and lock.acquire(timeout=timeout)
    try:
        yield bool(acquired)
    finally:
        if acquired:
            lock.release()


def past_search_deadline(
    deadline_monotonic: Optional[float],
    *,
    min_remaining: float = SEARCH_STAGE_MIN_REMAINING_S,
) -> bool:
    """True when a retrieve() call has exhausted its client-facing budget.

    Search runs inside ``asyncio.to_thread`` and cannot be cancelled; the
    handler must stop starting FAISS/expand/CE once remaining time is at or
    below ``min_remaining`` (DEFAULT_JSON_RPC_TIMEOUT_MS = 30s).
    """
    remaining = remaining_search_budget(deadline_monotonic)
    if remaining is None:
        return False
    return remaining <= min_remaining


def _cached_singleton_ready(getter) -> bool:
    """True when ``functools.cache`` already holds the process-wide model."""
    cache_info = getattr(getter, "cache_info", None)
    if not callable(cache_info):
        return False
    try:
        return cache_info().currsize > 0
    except Exception:
        return False


def should_skip_cold_model_load(deadline_monotonic: Optional[float], getter) -> bool:
    """True when leftover budget cannot finish a cold HuggingFace/ST load."""
    if not past_search_deadline(
        deadline_monotonic, min_remaining=SEARCH_MODEL_LOAD_MIN_REMAINING_S
    ):
        return False
    return not _cached_singleton_ready(getter)


def should_skip_faiss_rebuild(deadline_monotonic: Optional[float]) -> bool:
    """True when leftover budget cannot finish a full-table FAISS rebuild."""
    return past_search_deadline(
        deadline_monotonic, min_remaining=SEARCH_FAISS_REBUILD_MIN_REMAINING_S
    )


def faiss_load_lock_timeout(deadline_monotonic: Optional[float]) -> Optional[float]:
    """Seconds to wait for `_faiss_load_lock`, or None for unbounded.

    Default leftover (22.5s / 27s) is at or below the rebuild floor, so
    timeout is 0: acquire if free (disk restore still runs), skip if
    warmup or vault-watch holds the lock for a rebuild. Do not wait that
    rebuild out past JSON-RPC kill. Remaining above the floor may wait
    the surplus.
    """
    remaining = remaining_search_budget(deadline_monotonic)
    if remaining is None:
        return None
    return max(0.0, remaining - SEARCH_FAISS_REBUILD_MIN_REMAINING_S)

# Valid depth tiers for progressive disclosure.
DepthTier = Literal["headline", "snippet", "chunk", "document"]
_VALID_DEPTHS = {"headline", "snippet", "chunk", "document"}
_SNIPPET_MAX_CHARS = 280

# Page type → source authority mapping
_TYPE_TO_AUTHORITY = {
    "schema": "schema",
    "handoff": "handoff",
    "decision": "decision",
    "session": "session",
    "concept": "concept",
    "procedure": "procedure",
    "artifact": "artifact",
    "entity": "vault",
    "synthesis": "concept",
}


# --- vault_fts vtable DDL race (punch-list §2, fix (c)) --------------------
# The vault_fts virtual table is reconstructed via its FTS5 xConnect callback
# whenever a concurrent schema-cookie bump (another SovereignDB instance/process
# running schema init) moves the schema version mid-flight. A read racing that
# reconstruction can surface a transient "vtable constructor failed: vault_fts"
# (or "database schema has changed"). Retry the read a bounded number of times
# with a short backoff, then re-raise so genuine failures still fail loud. Only
# the two known transient message families are retried; every other
# OperationalError (e.g. a real SQL/syntax error) propagates immediately.
_FTS_RETRY_ATTEMPTS = 3
_FTS_RETRY_BACKOFFS = (0.05, 0.1, 0.2)
_FTS_TRANSIENT_MARKERS = ("vtable constructor failed", "schema has changed")

# perf/parallel-fanout (issue #388): bounded width + kill-switch for the
# per-variant fan-out in RetrievalEngine.retrieve. Each fan-out site creates
# its own pool on demand and drains it on gather, rather than sharing one
# global pool: corpus-leg workers in minnid_runtime/recall.py run variant
# fan-outs of their own, so a single shared pool would deadlock the moment leg
# tasks occupy every worker while their variant subtasks queue behind them.
# Per-site pools are also lifecycle-free (no atexit joins, no cross-test
# pollution). Thread creation per search is single-digit milliseconds against
# a multi-second search body.
# Cassandra YELLOW-3b: per-site cap is 4, not 8 — the fan-outs COMPOUND
# (2 both-scope legs x up to 9 combined legs x variant workers each), so
# 8-wide sites fielded 60+ threads per search. At 4/4 the worst case is
# ~2 + 4 + (4 x 4) + 4 ≈ 30 threads, still wider than the corpora that
# need it (≤8 vaults, ≤4 variants by query_expand._MAX_VARIANTS) with
# queueing absorbing
# the remainder. A shared semaphore was rejected: leg workers block in
# gather while holding it, so permits held by waiters could starve the
# variant subtasks they wait on (deadlock); smaller per-site caps cannot.
_MAX_VARIANT_WORKERS = 4
RETRIEVAL_VARIANT_PARALLEL = True


# Thread-local degradation slot name -> RetrievalCallState attribute, for the
# routed legacy properties below.
_DEGRADATION_STATE_ATTRS = {
    "rerank": "rerank_degraded",
    "query_expand": "query_expand_degraded",
    "vector": "vector_degraded",
    "hyde": "hyde_degraded",
    "document_hydration": "document_hydration_degraded",
}


@dataclass
class RetrievalCallState:
    """Per-call mutable verdicts for one retrieve() invocation.

    Hoisted off the engine (perf/parallel-fanout, #388): these verdict fields
    used to live only in thread-local properties on the engine
    (last_auth_suppression, last_*_degraded), which keeps concurrent REQUESTS
    on distinct threads apart but is invisible across the variant/corpus
    fan-out — a pool worker's writes land in ITS thread-local slot, unreadable
    from the gathering thread. One state object per call, threaded through
    retrieve() (explicit ``_state`` parameter) and reached from helpers via
    the per-thread push/pop stack (``_current_state``), means concurrent
    same-engine calls can never observe or clobber each other's verdicts,
    while every aggregation semantic stays identical. The thread-local
    properties remain as the read surface: a top-level retrieve() publishes
    its state there on return, so existing readers (recall._degradation_for,
    handlers, tests) are untouched.
    """

    auth_suppression: Optional[Dict] = None
    rerank_degraded: Optional[str] = None
    query_expand_degraded: Optional[str] = None
    vector_degraded: Optional[str] = None
    hyde_degraded: Optional[str] = None
    document_hydration_degraded: Optional[str] = None
    # Cassandra RED-1 (#388): the trace id stamped onto this call's rows.
    # last_trace_id used to be a plain shared instance attribute, so two
    # both-scope legs running concurrently on the same engine overwrote and
    # re-read each other's id. Carried here so each call owns its id; the
    # legacy last_trace_id property routes into it mid-call (see below).
    trace_id: Optional[str] = None


class _ThreadLocalStateProxy:
    """RetrievalCallState-shaped view of the legacy thread-local properties.

    Fallback for helper calls made OUTSIDE retrieve() (unit tests, operator
    tools calling _rerank/_encode_query directly): attribute reads/writes hit
    exactly the properties they always did, so no helper signature changes and
    no test-double breakage. Inside retrieve() helpers always see the pushed
    per-call state instead — this proxy is never on the stack.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def auth_suppression(self) -> Optional[Dict]:
        return self._engine.last_auth_suppression

    @auth_suppression.setter
    def auth_suppression(self, value: Optional[Dict]) -> None:
        self._engine.last_auth_suppression = value

    @property
    def rerank_degraded(self) -> Optional[str]:
        return self._engine.last_rerank_degraded

    @rerank_degraded.setter
    def rerank_degraded(self, value: Optional[str]) -> None:
        self._engine.last_rerank_degraded = value

    @property
    def query_expand_degraded(self) -> Optional[str]:
        return self._engine.last_query_expand_degraded

    @query_expand_degraded.setter
    def query_expand_degraded(self, value: Optional[str]) -> None:
        self._engine.last_query_expand_degraded = value

    @property
    def vector_degraded(self) -> Optional[str]:
        return self._engine.last_vector_degraded

    @vector_degraded.setter
    def vector_degraded(self, value: Optional[str]) -> None:
        self._engine.last_vector_degraded = value

    @property
    def hyde_degraded(self) -> Optional[str]:
        return self._engine.last_hyde_degraded

    @hyde_degraded.setter
    def hyde_degraded(self, value: Optional[str]) -> None:
        self._engine.last_hyde_degraded = value

    @property
    def document_hydration_degraded(self) -> Optional[str]:
        return self._engine.last_document_hydration_degraded

    @document_hydration_degraded.setter
    def document_hydration_degraded(self, value: Optional[str]) -> None:
        self._engine.last_document_hydration_degraded = value

    @property
    def trace_id(self) -> Optional[str]:
        return self._engine.last_trace_id

    @trace_id.setter
    def trace_id(self, value: Optional[str]) -> None:
        self._engine.last_trace_id = value

# Prefer a real embedded chunk over the raw vault_fts row. Unendorsed pages are
# deliberately unembedded, so FTS is their only body path — but vault_fts stores
# the whole markdown file (YAML first). Using that full file as chunk_text makes
# the FTS-only path (encoder down / dual-hit absent) burn the context budget on
# multi-KB pages and fills the default snippet with frontmatter soup.
_FTS_CHUNK_TEXT_EXPR = """COALESCE(
  (SELECT ce.chunk_text FROM chunk_embeddings ce
   WHERE ce.doc_id = d.doc_id
   ORDER BY ce.chunk_index LIMIT 1),
  f.content
)"""


def _strip_leading_frontmatter(text: str) -> str:
    """Drop a leading closed ``---`` YAML block from FTS/chunk body text.

    ``vault_fts`` stores the whole markdown file (YAML first). The first
    ``chunk_embeddings`` row often still opens with the same fence because the
    chunker folds the document top-down and joins lines with spaces. Snippet
    depth and the token budget must see body prose, not the FM header.

    Handles both the on-disk form (``---\\nkey: val\\n---\\n\\nbody``) and the
    chunker-collapsed form (``--- key: val --- body``).
    """
    if not text or not text.startswith("---"):
        return text
    # On-disk / FTS full-file: closing fence on its own line.
    end = text.find("\n---", 3)
    if end != -1:
        rest = text[end + 4 :]  # past the closing fence line
        if rest.startswith("\n"):
            rest = rest[1:]
        return rest
    # Chunker-collapsed: second ``---`` closes the header, body follows.
    collapsed = re.match(r"^---\s+.+?\s+---\s*", text, flags=re.DOTALL)
    if collapsed:
        return text[collapsed.end() :]
    return text


def _fts_execute_with_retry(cursor, sql, params, *, attempts: int = _FTS_RETRY_ATTEMPTS):
    """Execute a vault_fts MATCH select on ``cursor`` AND fetch its rows, with
    bounded retry on the transient schema-cookie / vtable-reconnect race.
    Returns the fetched rows. The fetch lives INSIDE the retry window (review
    r3, P2): SQLite can raise "database schema has changed" / "vtable
    constructor failed" while STEPPING the SELECT, i.e. during fetchall() after
    a successful execute(), so retrying execute alone still let the race
    surface at the call sites' fetch. Re-raises the last OperationalError after
    the budget is exhausted (fail-loud contract)."""
    last_exc: Optional[sqlite3.OperationalError] = None
    for attempt in range(attempts):
        try:
            cursor.execute(sql, params)
            return cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if not any(m in str(exc).lower() for m in _FTS_TRANSIENT_MARKERS):
                raise
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(_FTS_RETRY_BACKOFFS[min(attempt, len(_FTS_RETRY_BACKOFFS) - 1)])
    assert last_exc is not None  # only reachable after catching a transient error
    raise last_exc


def _query_class(query: str) -> str:
    """Stable coarse query class for feedback aggregation."""
    safe = RetrievalEngine._sanitize_fts_query(query).lower() if query else ""
    words = safe.split()[:8]
    normalized = " ".join(words)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _trace_ring():
    """Load engine/trace.py without colliding with Python's stdlib trace module."""
    module_name = "_sovereign_trace"
    if module_name in sys.modules:
        return sys.modules[module_name].GLOBAL_TRACE_RING
    trace_path = Path(__file__).with_name("trace.py")
    spec = importlib.util.spec_from_file_location(module_name, trace_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load trace module from {trace_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.GLOBAL_TRACE_RING


def _page_type_to_authority(page_type: Optional[str], agent: str) -> Optional[str]:
    """Map a page_type string to a source_authority value."""
    if page_type:
        return _TYPE_TO_AUTHORITY.get(page_type.lower(), "vault")
    if agent.startswith("wiki:"):
        sub = agent[5:]
        return _TYPE_TO_AUTHORITY.get(sub, "vault")
    if agent.startswith("identity:"):
        return "schema"
    return "vault"


# Correction-class page types (recall-F3) — shared single source of truth in
# config.py so retrieval and decay cannot drift. Kept as a module alias for
# existing call sites/tests.
_correction_class_page_types = correction_class_page_types


def _effective_decay(r: Dict, fallback: Optional[float] = None) -> Optional[float]:
    """The decay the ranking and confidence legs actually used.

    grok-review round 6 (finding 3): decay_applied carries the correction
    floor + clamp both scoring legs apply (_score_merged_doc and
    _apply_decay_rerank_attenuation). Provenance must report that value, not
    the raw decay_score column — a correction floored to 0.5 that provenance
    reports as 0.01 is a lie to any consumer re-blending the fields. `is not
    None` checks, not `or`: a legitimate 0.0 decay must survive.
    """
    for value in (r.get("decay_applied"), r.get("decay_score"), fallback):
        if value is not None:
            return value
    return None


def _path_to_wikilink(path: str) -> Optional[str]:
    """Convert an absolute path to a [[wikilink]] style reference."""
    if not path:
        return None
    import os
    # Strip extension and produce a relative-ish wiki link
    name = os.path.splitext(os.path.basename(path))[0]
    # Try to extract a wiki-relative path
    for marker in ("/wiki/", "/raw/", "/schema/"):
        idx = path.find(marker)
        if idx >= 0:
            rel = path[idx + 1:]  # e.g. wiki/concepts/foo
            rel_noext = os.path.splitext(rel)[0]
            return f"[[{rel_noext}]]"
    return f"[[{name}]]"


def _xml_attr_escape(value) -> str:
    """Escape XML attribute metacharacters so untrusted strings (paths, agent
    names) cannot break out of an EVIDENCE attribute value and forge
    attributes/tags. Mirrors xmlAttrEscape in plugins/minni/src/task.ts —
    the injection floor must be identical on both paths."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _evidence_body_escape(text: str) -> str:
    """Escape an evidence snippet body so it can neither close its own
    envelope (``</EVIDENCE>``) nor open a forged one with
    ``instruction_like="false"``. Also neutralizes markdown hazards (backticks,
    line-leading #). Mirrors the snippet escaping in plugins/minni/src/task.ts."""
    safe = str(text).replace("`", "\\`").replace("\n#", "\n\\#")
    safe = safe.replace("</", "<\\/")
    safe = re.sub(r"<EVIDENCE", "&#60;EVIDENCE", safe, flags=re.IGNORECASE)
    return safe


INSTRUCTION_BODY_BOUNDARY = "\u2063"
_INSTRUCTION_BODY_LITERAL = INSTRUCTION_BODY_BOUNDARY * 2
_INSTRUCTION_BODY_PLACEHOLDER = "\0MINNI_BOUNDARY\0"
_ATTRIBUTION_LABELS = {"entailed", "neutral", "contradicted"}
_NLI_OUTPUT_TO_ATTRIBUTION = {
    "entailment": "entailed",
    "entailed": "entailed",
    "neutral": "neutral",
    "contradiction": "contradicted",
    "contradicted": "contradicted",
}
_NLI_LABEL_ORDER = ("contradicted", "entailed", "neutral")


def _perturb_instruction_like_body(escaped_text: str) -> str:
    """Insert reversible token-boundary markers into instruction-like evidence."""
    escaped_text = str(escaped_text).replace(
        INSTRUCTION_BODY_BOUNDARY,
        _INSTRUCTION_BODY_LITERAL,
    )
    return re.sub(
        r"(?<=\w)(\s+)(?=\w)",
        INSTRUCTION_BODY_BOUNDARY + r"\1",
        escaped_text,
    )


def _recover_instruction_like_body(perturbed_text: str) -> str:
    """Recover the escaped evidence body produced before perturbation."""
    return (
        str(perturbed_text)
        .replace(_INSTRUCTION_BODY_LITERAL, _INSTRUCTION_BODY_PLACEHOLDER)
        .replace(INSTRUCTION_BODY_BOUNDARY, "")
        .replace(_INSTRUCTION_BODY_PLACEHOLDER, INSTRUCTION_BODY_BOUNDARY)
    )


def _normalize_attribution_label(label) -> Optional[str]:
    normalized = _NLI_OUTPUT_TO_ATTRIBUTION.get(str(label or "").strip().lower())
    return normalized if normalized in _ATTRIBUTION_LABELS else None


def build_evidence_envelope(
    *,
    source,
    agent,
    status,
    privacy,
    score: float,
    instruction_like: bool,
    visibility: str,
    text: str,
    attribution: Optional[str] = None,
    perturbation_enabled: bool = True,
) -> str:
    """G22 evidence-only envelope with attribute + body escaping (SEC-010).
    Module-level so the escaping contract is directly testable."""
    body = _evidence_body_escape(text)
    if instruction_like and perturbation_enabled:
        body = _perturb_instruction_like_body(body)
    attribution_label = _normalize_attribution_label(attribution)
    attribution_attr = (
        f' attribution="{_xml_attr_escape(attribution_label)}"'
        if attribution_label
        else ""
    )
    return (
        f'<EVIDENCE source="{_xml_attr_escape(source)}" agent="{_xml_attr_escape(agent)}" '
        f'status="{_xml_attr_escape(status)}" privacy="{_xml_attr_escape(privacy)}" '
        f'score="{float(score):.3f}" instruction_like="{str(bool(instruction_like)).lower()}"{attribution_attr} '
        f'visibility="{_xml_attr_escape(visibility)}">{body}</EVIDENCE>'
    )


def _parse_evidence_refs(raw) -> Optional[list]:
    """Parse evidence_refs field (stored as JSON string or None)."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    import json
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    # Try comma-split as fallback
    if isinstance(raw, str) and raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return None


def _recommended_action(
    status: Optional[str],
    instruction_like: Optional[bool],
    confidence: Optional[float],
) -> str:
    """Heuristic recommended action for the consuming agent."""
    if instruction_like:
        return "escalate"
    if status in ("superseded", "rejected", "expired"):
        return "ignore"
    if status == "draft":
        return "follow_up"
    if confidence is not None and confidence < 0.2:
        return "follow_up"
    return "cite"


class RetrievalEngine:
    """Hybrid FTS5 + FAISS semantic retrieval with cross-encoder re-ranking."""

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
        faiss_index: Optional[FAISSIndex] = None,
    ):
        self.db = db
        self.config = config
        self.faiss_index = faiss_index or FAISSIndex(config)
        self._model = None
        self._reranker = None
        self._attribution_model = None
        self._tokenizer = None
        self._feedback_cache = {}
        self._feedback_cache_loaded_at = 0.0
        # Cassandra RED-1 (#388): was a plain shared instance attribute, so
        # concurrent same-engine legs overwrote each other's trace id between
        # the write and the recall.py re-read. Now a per-thread slot behind
        # the last_trace_id property below, which routes into the running
        # call's RetrievalCallState.trace_id mid-call (same routing as the
        # verdict flags). Published there by retrieve() on return, exactly
        # like the other per-call verdicts.
        self._trace_id_local = threading.local()
        # P0-B (2026-07-19 blackout): the semantic leg must never die silently.
        # Set when _semantic_search finds no embedding model; cleared when the
        # model comes back. Surfaced by status/recall so an FTS-only session is
        # visible instead of masquerading as healthy.
        self.vector_model_down: bool = False
        # R5 (#226): _rerank swallows a reranker failure and returns candidates
        # carrying no rerank_score, which drops that corpus to raw RRF
        # magnitude. Recorded per-retrieve so the merged response can say the
        # ordering is not what it claims to be.
        # Thread-local for exactly the reason last_auth_suppression is (see the
        # comment on _auth_suppression_local below): dispatch runs each `search`
        # RPC on its own worker thread against ONE process-wide engine, so a
        # plain instance attribute would let a concurrent request clear this in
        # the window between retrieve() returning and the handler reading it —
        # reporting a degraded search as healthy, or pinning one caller's
        # failure on another. Review round 1 on PR #260 caught this.
        self._degradation_local = threading.local()
        self.last_trace_id = None
        # P0-A contract: when the read-authorization gate suppresses a
        # non-empty candidate set to zero, the reason is recorded so the caller
        # can return a diagnostic envelope instead of a bare [].
        # Review r4 (P2): dispatch runs each `search` RPC in its own worker
        # thread while `_lazy_retrieval()` hands every request the SAME process-
        # wide RetrievalEngine. A plain instance attribute is therefore shared
        # mutable state — a concurrent request's retrieve() could overwrite or
        # clear this in the window between another request's retrieve() returning
        # and its handler reading it, so an auth-suppressed caller could get a
        # bare zero-hit answer (or another caller's diagnostic). Back it with
        # thread-local storage: within one request the set-in-retrieve()/read-in-
        # handler pair runs on a single thread, so each request sees only its own
        # suppression regardless of what concurrent requests do on other threads.
        self._auth_suppression_local = threading.local()
        # perf/parallel-fanout (#388): per-thread stack of RetrievalCallState.
        # retrieve() pushes one state per call; helpers reach the innermost via
        # _current_state() instead of touching the legacy thread-locals, so
        # pool workers running concurrent same-engine calls stay isolated.
        self._call_state_local = threading.local()
        # Serializes _ensure_faiss_loaded. invalidate() turned "rare cold
        # start" into "every worker after every vault change", and the ensure
        # path is a multi-step read-build-save that must not run twice
        # concurrently against moving DB state.
        self._faiss_load_lock = threading.Lock()
        # recall-F3: the correction-class type set is config-invariant — compute
        # it once here instead of once per scored doc in _score_merged_doc.
        self._correction_types = _correction_class_page_types(config)

    def _stack_top(self) -> Optional["RetrievalCallState"]:
        """Innermost pushed per-call state on THIS thread, or None.

        Raw peek (no proxy fallback): the legacy properties use this to route
        mid-call reads/writes into the running call's state, so pipeline hooks
        that set engine.last_* (tests, tools) keep working with per-call
        isolation under the variant fan-out.
        """
        local = getattr(self, "_call_state_local", None)
        stack = getattr(local, "stack", None) if local is not None else None
        return stack[-1] if stack else None

    @property
    def last_auth_suppression(self) -> Optional[Dict]:
        """Per-thread P0-A read-gate suppression from the last retrieve() on
        THIS thread. Thread-local so concurrent `search` RPCs sharing this
        process-wide engine never read each other's (or a cleared) diagnostic.
        Defaults to None on a thread that has not run a gated retrieve() yet.

        perf/parallel-fanout (#388): routes into the running call's pushed
        state when read inside retrieve() on this thread, else the legacy
        thread-local. Serially observable behavior is unchanged (the state is
        published to the thread-local at return); under the fan-out each
        variant worker's writes land in its own variant state.
        """
        top = self._stack_top()
        if top is not None:
            return top.auth_suppression
        local = getattr(self, "_auth_suppression_local", None)
        return getattr(local, "value", None) if local is not None else None

    @last_auth_suppression.setter
    def last_auth_suppression(self, value: Optional[Dict]) -> None:
        top = self._stack_top()
        if top is not None:
            top.auth_suppression = value
            return
        # Lazy-init the backing store so instances built via object.__new__
        # (test fakes that bypass __init__) still get a working thread-local.
        local = getattr(self, "_auth_suppression_local", None)
        if local is None:
            local = threading.local()
            self._auth_suppression_local = local
        local.value = value

    @property
    def last_trace_id(self) -> Optional[str]:
        """Trace id stamped onto THIS thread's last retrieve() rows.

        Cassandra RED-1 (#388): was a plain shared instance attribute, so
        both-scope legs running concurrently on the same engine raced it.
        Routes into the running call's pushed RetrievalCallState.trace_id
        when read inside retrieve() on this thread, else the per-thread
        slot published at return. Serially observable behavior is unchanged;
        under the fan-out each leg observes only its own call's id.
        """
        top = self._stack_top()
        if top is not None:
            return top.trace_id
        local = getattr(self, "_trace_id_local", None)
        return getattr(local, "value", None) if local is not None else None

    @last_trace_id.setter
    def last_trace_id(self, value: Optional[str]) -> None:
        top = self._stack_top()
        if top is not None:
            top.trace_id = value
            return
        # Lazy-init like the auth-suppression setter, so instances built via
        # object.__new__ (test fakes bypassing __init__) still work.
        local = getattr(self, "_trace_id_local", None)
        if local is None:
            local = threading.local()
            self._trace_id_local = local
        local.value = value

    def _degradation_flag(self, name: str) -> Optional[str]:
        top = self._stack_top()
        if top is not None:
            return getattr(top, _DEGRADATION_STATE_ATTRS[name])
        local = getattr(self, "_degradation_local", None)
        return getattr(local, name, None) if local is not None else None

    def _set_degradation_flag(self, name: str, value: Optional[str]) -> None:
        top = self._stack_top()
        if top is not None:
            setattr(top, _DEGRADATION_STATE_ATTRS[name], value)
            return
        # Lazy-init like the auth-suppression setter, so instances built via
        # object.__new__ (test fakes bypassing __init__) still work.
        local = getattr(self, "_degradation_local", None)
        if local is None:
            local = threading.local()
            self._degradation_local = local
        setattr(local, name, value)

    @property
    def last_rerank_degraded(self) -> Optional[str]:
        """R5 (#226): why the reranker failed on THIS thread's last retrieve().

        A rerank failure leaves candidates with no rerank_score, so in a
        combined/both merge this corpus competes at raw RRF magnitude against
        reranked corpora and is silently evicted. Per-thread, so concurrent
        searches never read each other's verdict.
        """
        return self._degradation_flag("rerank")

    @last_rerank_degraded.setter
    def last_rerank_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("rerank", value)

    @property
    def last_query_expand_degraded(self) -> Optional[str]:
        """AFM-6 (#230): why expansion failed and recall fell back to the bare
        query, for THIS thread's last retrieve()."""
        return self._degradation_flag("query_expand")

    @last_query_expand_degraded.setter
    def last_query_expand_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("query_expand", value)

    @property
    def last_vector_degraded(self) -> Optional[str]:
        """P0-B, per request: whether THIS thread's last retrieve() lost its
        semantic leg. Review round 2 on PR #260: R4(a) reported the plain
        process-wide ``vector_model_down`` bool per RESPONSE, so a concurrent
        request flipping it in the set-in-retrieve()/read-in-handler window
        could report a lexical-only answer as healthy (or a healthy one as
        degraded). The response envelope reads this thread-local instead; the
        global bool stays as the process-level outage signal for the health
        surface and the log-once guard."""
        return self._degradation_flag("vector")

    @last_vector_degraded.setter
    def last_vector_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("vector", value)

    @property
    def last_hyde_degraded(self) -> Optional[str]:
        """Round 15 (PR #260): HyDE was triggered but did not complete on THIS
        thread's last retrieve(). Without this the response envelope could
        look healthy while enrichment never ran (trace-only signal)."""
        return self._degradation_flag("hyde")

    @last_hyde_degraded.setter
    def last_hyde_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("hyde", value)

    @property
    def last_document_hydration_degraded(self) -> Optional[str]:
        """Document-depth fetch timed out; ranked chunk still returned."""
        return self._degradation_flag("document_hydration")

    @last_document_hydration_degraded.setter
    def last_document_hydration_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("document_hydration", value)

    def _current_state(self) -> Any:
        """Innermost per-call state on THIS thread (never None).

        Inside retrieve() this is the pushed RetrievalCallState for the
        running call — variant pool workers each pushed their own, so
        concurrent same-engine calls stay isolated. Outside retrieve()
        (direct helper calls in tests/tools) it is a _ThreadLocalStateProxy
        over the legacy thread-local properties, preserving their exact
        behavior with no helper signature changes.
        """
        top = self._stack_top()
        if top is not None:
            return top
        return _ThreadLocalStateProxy(self)

    def _push_call_state(self, state: RetrievalCallState) -> None:
        local = getattr(self, "_call_state_local", None)
        if local is None:
            local = threading.local()
            self._call_state_local = local
        stack = getattr(local, "stack", None)
        if stack is None:
            stack = []
            local.stack = stack
        stack.append(state)

    def _pop_call_state(self) -> None:
        stack = self._call_state_local.stack
        stack.pop()

    def _publish_call_state(self, state: RetrievalCallState) -> None:
        """Publish a top-level call's verdicts to the legacy read surface.

        Runs on the calling thread at retrieve() return, so post-retrieve
        readers (recall._degradation_for, handlers, tests) observe exactly
        what the serial code left in the thread-locals — including on the
        exception path, where the partial state matches the incremental
        thread-local writes the serial code had made before throwing.
        """
        self.last_auth_suppression = state.auth_suppression
        self.last_rerank_degraded = state.rerank_degraded
        self.last_query_expand_degraded = state.query_expand_degraded
        self.last_vector_degraded = state.vector_degraded
        self.last_hyde_degraded = state.hyde_degraded
        self.last_document_hydration_degraded = state.document_hydration_degraded
        # RED-1: the call's own trace id (None when the merge never ran, e.g.
        # a raising variant — see the documented exception-path delta below).
        # Publish runs after the pop, so this lands in the thread-local slot.
        self.last_trace_id = state.trace_id

    def _deadline_skipped_vector(self) -> bool:
        """True when the returned ranking is FTS-only or CE-skipped due to deadline.

        A skipped HyDE enrichment does not change the first-pass ranking, so it
        must not withhold qty/calibration from hybrid winners.
        """
        for flag in (
            self.last_vector_degraded,
            self.last_rerank_degraded,
        ):
            if flag and "search deadline" in str(flag).lower():
                return True
        return False

    def _current_deadline(self) -> Optional[float]:
        local = getattr(self, "_degradation_local", None)
        if local is None:
            return None
        return getattr(local, "deadline_monotonic", None)

    def _set_current_deadline(self, value: Optional[float]) -> None:
        local = getattr(self, "_degradation_local", None)
        if local is None:
            local = threading.local()
            self._degradation_local = local
        local.deadline_monotonic = value

    @contextmanager
    def _query_encoding_scope(self):
        """Reuse successful embeddings only during one request's refill loop."""
        local = getattr(self, "_degradation_local", None)
        if local is None:
            local = threading.local()
            self._degradation_local = local
        previous = getattr(local, "query_embeddings", None)
        local.query_embeddings = {}
        try:
            yield
        finally:
            local.query_embeddings = previous

    @property
    def model(self):
        """Return the process-wide embedding model singleton."""
        from minni.models import get_embedder
        return get_embedder()

    @property
    def reranker(self):
        """Return the process-wide cross-encoder singleton."""
        if self._reranker is not None:
            return self._reranker
        from minni.models import get_cross_encoder
        if should_skip_cold_model_load(self._current_deadline(), get_cross_encoder):
            self.last_rerank_degraded = "search deadline; skipped rerank"
            return None
        self._reranker = get_cross_encoder()
        return self._reranker

    @property
    def attribution_model(self):
        """Return the process-wide NLI cross-encoder singleton."""
        if self._attribution_model is not None:
            return self._attribution_model
        from minni.models import get_attribution_cross_encoder
        if should_skip_cold_model_load(
            self._current_deadline(), get_attribution_cross_encoder
        ):
            return None
        self._attribution_model = get_attribution_cross_encoder()
        return self._attribution_model

    def _score_attribution(self, claim: Optional[str], evidence_text: str) -> Optional[Dict]:
        """Score whether evidence supports a caller-supplied claim using local NLI."""
        claim_text = str(claim or "").strip()
        if not claim_text:
            return None
        if not getattr(self.config, "attribution_enabled", True):
            return None
        if past_search_deadline(self._current_deadline()):
            return None
        from minni.models import get_attribution_cross_encoder

        if should_skip_cold_model_load(
            self._current_deadline(), get_attribution_cross_encoder
        ):
            return None
        model = self.attribution_model
        if model is None:
            return None
        try:
            from minni.models import get_attribution_lock

            with search_model_lock(get_attribution_lock(), self._current_deadline()) as acquired:
                if not acquired or past_search_deadline(self._current_deadline()):
                    return None
                raw = model.predict(
                    [(str(evidence_text or ""), claim_text)],
                    show_progress_bar=False,
                )
        except Exception as exc:  # noqa: BLE001 - recall degrades when NLI is unavailable.
            logger.debug("attribution scoring skipped: %s", exc)
            return None
        parsed = self._parse_attribution_prediction(raw)
        if parsed is None:
            return None
        label, score = parsed
        return {
            "attribution": label,
            "attribution_score": round(float(score), 6),
            "attribution_model": getattr(self.config, "attribution_model", "unknown"),
        }

    @staticmethod
    def _parse_attribution_prediction(raw) -> Optional[Tuple[str, float]]:
        """Normalize common CrossEncoder NLI outputs to Minni attribution labels."""
        if raw is None:
            return None

        first = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
        if isinstance(first, dict):
            label = _normalize_attribution_label(first.get("label"))
            if label is None:
                return None
            score = first.get("score", first.get("probability", 1.0))
            return label, float(score)

        arr = np.asarray(first, dtype=float)
        if arr.ndim == 0:
            return None
        arr = arr.reshape(-1)
        if arr.size < 3:
            return None
        logits = arr[:3]
        shifted = logits - np.max(logits)
        exp = np.exp(shifted)
        total = float(exp.sum())
        if not math.isfinite(total) or total <= 0:
            return None
        probs = exp / total
        best = int(np.argmax(probs))
        return _NLI_LABEL_ORDER[best], float(probs[best])

    @property
    def tokenizer(self):
        """Lazy-load tiktoken tokenizer for context budgeting."""
        if self._tokenizer is None:
            try:
                import tiktoken
                self._tokenizer = tiktoken.get_encoding(self.config.token_model)
            except ImportError:
                logger.warning("tiktoken not installed — using word-count approximation")
                self._tokenizer = False
        return self._tokenizer if self._tokenizer is not False else None

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        if self.tokenizer:
            return len(self.tokenizer.encode(text))
        # Fallback: rough word-count approximation (1 token ≈ 0.75 words)
        return int(len(text.split()) / 0.75)

    # ── FTS5 Search ────────────────────────────────────────────

    @staticmethod
    def _normalize_agent_filter(agent_filter: Optional[Sequence[str]]) -> list[str]:
        if agent_filter is None:
            return []
        if isinstance(agent_filter, str):
            raw = [agent_filter]
        else:
            raw = list(agent_filter)
        scope: list[str] = []
        seen: set[str] = set()
        for item in raw:
            agent = str(item).strip()
            if agent and agent not in seen:
                scope.append(agent)
                seen.add(agent)
        return scope

    def _fts_search(
        self,
        query: str,
        limit: int,
        agent_filter: Optional[Sequence[str]] = None,
        exclude_statuses: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        """FTS5 search using BM25 ranking.

        ``exclude_statuses`` filters in SQL rather than after the fact. The
        LIMIT below is a fixed window, so a lifecycle state the caller is going
        to discard anyway must not be allowed to occupy it — a post-filter
        cannot recover rows that were never fetched. This matters most on a
        vault dominated by unendorsed drafts, where an unfiltered window fills
        with pages nobody asked for and the accepted answer never enters the
        merge.
        """
        results = []
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return results
        agent_scope = self._normalize_agent_filter(agent_filter)

        with self.db.cursor() as c:
            agent_clause = ""
            scope_params: list = []
            if agent_scope:
                agent_clause = f" AND d.agent IN ({','.join('?' * len(agent_scope))})"
                scope_params.extend(agent_scope)
            skip = [str(s) for s in (exclude_statuses or [])]
            if skip:
                agent_clause += (
                    " AND COALESCE(d.page_status, 'candidate') NOT IN "
                    f"({','.join('?' * len(skip))})"
                )
                scope_params.extend(skip)

            def _match(match_expr: str):
                # Fetch happens inside the retry helper (review r3): the
                # vtable race can also fire while stepping the SELECT.
                # Prefer the first embedded chunk when the doc has one; fall
                # back to vault_fts content for deliberately unembedded rows
                # (draft/expired). The hollow-hit fix used f.content alone,
                # which made FTS-only (encoder down) attach multi-KB YAML-led
                # files as chunk_text and exhaust the default budget after a
                # couple of hits. Body-only post-strip is applied below.
                return _fts_execute_with_retry(c, f"""
                    SELECT f.doc_id, d.path, d.agent, d.sigil,
                           rank AS bm25_rank, d.decay_score,
                           d.page_status, d.privacy_level, d.page_type,
                           d.evidence_refs, d.indexed_at, d.layer,
                           {_FTS_CHUNK_TEXT_EXPR} AS chunk_text
                    FROM vault_fts f
                    JOIN documents d ON d.doc_id = f.doc_id
                    WHERE vault_fts MATCH ?
                    {agent_clause}
                    ORDER BY rank
                    LIMIT ?
                """, [match_expr, *scope_params, limit * 3])

            # Strict pass first: FTS5 space-joined terms are implicit AND —
            # precise when every term appears in the document. A dated/specific
            # query ("checkpoint 2026-07-18 plan-…") almost never has ALL its
            # tokens in the stored content, so a zero-hit AND query degrades to
            # OR semantics: bm25 still ranks the document matching the most /
            # rarest terms first, restoring recall without diluting queries the
            # strict pass already answers (same contract as learnings_fts).
            rows = _match(safe_query)
            terms = safe_query.split()
            if not rows and len(terms) > 1:
                # Lowercase the operands: FTS5 matching is case-insensitive,
                # but a literal uppercase "OR"/"AND"/"NOT" token from the query
                # would be parsed as an operator and corrupt the expression.
                rows = _match(" OR ".join(t.lower() for t in terms))

            for row in rows:
                results.append({
                    "doc_id": row["doc_id"],
                    "path": row["path"],
                    "agent": row["agent"],
                    "sigil": row["sigil"],
                    "bm25_rank": row["bm25_rank"],
                    "decay_score": row["decay_score"] or 1.0,
                    "page_status": row["page_status"] or "candidate",
                    "privacy_level": row["privacy_level"] or "safe",
                    "page_type": row["page_type"],
                    "evidence_refs": row["evidence_refs"],
                    "indexed_at": row["indexed_at"],
                    "layer": row["layer"] or "knowledge",
                    "chunk_text": _strip_leading_frontmatter(row["chunk_text"] or ""),
                })

        return results

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Sanitize query for FTS5 MATCH syntax."""
        import re
        # Replace hyphens with spaces (FTS5 treats - as NOT operator)
        cleaned = re.sub(r'[^\w\s]', ' ', query)
        words = cleaned.split()
        if not words:
            return ""
        return " ".join(words)

    @staticmethod
    def apply_read_gate(principal, workspace: str, merged: List[Dict]):
        """G19 read gate with the H2 silent-empty contract (P0-A, 2026-07-19).

        Filters ``merged`` through :func:`can_read_document`. When a NON-empty
        candidate set is filtered to zero, returns a machine-readable
        diagnostic instead of leaving the caller with a bare empty list —
        an authorization blackout must be distinguishable from "nothing
        matched".

        Returns ``(filtered, suppression)`` where ``suppression`` is ``None``
        unless every candidate was suppressed by scope.
        """
        filtered = [r for r in merged if can_read_document(principal, workspace, r)]
        suppression = None
        if merged and not filtered:
            suppression = {
                "pre_gate": len(merged),
                "suppressed": len(merged),
                "reason": (
                    f"{len(merged)} candidates suppressed by scope: "
                    f"principal={getattr(principal, 'agent_id', 'n/a')} "
                    f"ws={workspace}"
                ),
            }
        return filtered, suppression

    # ── FAISS Semantic Search ─────────────────────────────────

    def _semantic_search(
        self,
        query: str,
        limit: int,
        agent_filter: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        """
        Semantic search via FAISS index.
        Returns best-chunk-per-doc with chunk text and heading context.
        """
        # Round 7 (PR #260): route through _encode_query like every explicit
        # backend path — an encode-side degrade fixed only there would
        # otherwise miss the default path. Empty vector = encoder down, flag
        # already raised.
        deadline_monotonic = self._current_deadline()
        query_emb = self._encode_query(query, deadline_monotonic=deadline_monotonic)
        if query_emb.size == 0:
            return []
        if past_search_deadline(deadline_monotonic):
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return []

        # Search FAISS for top candidates
        search_limit = limit * 5  # Over-fetch for doc dedup
        faiss_results = self.faiss_index.search(query_emb, top_k=search_limit)

        if not faiss_results:
            # Fallback: load index from disk / rebuild from DB if empty.
            # Disk restore is <500ms and must run on default leftover
            # (22.5s / 27s). Do not skip _ensure_faiss_loaded at the 27s
            # rebuild floor — that floor lives inside ensure, after a disk
            # miss. Rebuild is uncancellable inside to_thread; skip starting
            # it once leftover cannot finish. A warm genuine miss must not
            # inherit this degrade — only a still-cold leftover skip is the
            # work that outlives DEFAULT_JSON_RPC kill.
            if past_search_deadline(deadline_monotonic):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return []
            self._ensure_faiss_loaded()
            # Inner leftover skip / lock-acquire timeout can leave the index
            # cold with remaining in (2, 27]: past_search_deadline (2s floor)
            # is still false, and an empty search would look like a genuine
            # miss — FTS-only ranking treated as healthy. Encode-after-lock
            # and a failed load-lock acquire already set this flag.
            if past_search_deadline(deadline_monotonic) or (
                not self.faiss_index.ready
                and (
                    should_skip_faiss_rebuild(deadline_monotonic)
                    or self._deadline_skipped_vector()
                )
            ):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return []
            faiss_results = self.faiss_index.search(query_emb, top_k=search_limit)

        if not faiss_results:
            return []

        # Fetch chunk metadata and deduplicate by doc_id (best chunk per doc)
        chunk_ids = [cid for cid, _ in faiss_results]
        score_map = {cid: score for cid, score in faiss_results}

        doc_best: Dict[int, Dict] = {}  # doc_id → best result
        agent_scope = self._normalize_agent_filter(agent_filter)

        with self.db.cursor() as c:
            placeholders = ",".join("?" * len(chunk_ids))
            agent_clause = ""
            params: list = list(chunk_ids)
            if agent_scope:
                agent_clause = f" AND d.agent IN ({','.join('?' * len(agent_scope))})"
                params.extend(agent_scope)
            c.execute(f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id IN ({placeholders})
                {agent_clause}
            """, params)

            for row in c.fetchall():
                cid = row["chunk_id"]
                did = row["doc_id"]
                sim = score_map.get(cid, 0.0)

                if did not in doc_best or sim > doc_best[did]["similarity"]:
                    doc_best[did] = {
                        "doc_id": did,
                        "chunk_id": cid,
                        "path": row["path"],
                        "agent": row["agent"],
                        "sigil": row["sigil"],
                        "similarity": sim,
                        "chunk_text": row["chunk_text"],
                        "heading_context": row["heading_context"] or "",
                        "decay_score": row["decay_score"] or 1.0,
                        "page_status": row["page_status"] or "candidate",
                        "privacy_level": row["privacy_level"] or "safe",
                        "page_type": row["page_type"],
                        "evidence_refs": row["evidence_refs"],
                        "indexed_at": row["indexed_at"],
                        "layer": row["layer"] or "knowledge",
                    }

        results = sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)
        return results[:limit * 3]

    def _ensure_faiss_loaded(self) -> None:
        """
        Load FAISS index from DB if not already loaded.

        PR-2: Attempt disk cache first (cold-start <500ms).
        On miss, rebuild from DB then save to disk.
        """
        # "Warm" is READINESS — a validated, generation-current build — never
        # bare count>0. A count-based gate is what let every partially
        # published state become permanent: whatever raced its way to a
        # non-zero count was suddenly the index of record.
        if self.faiss_index.ready:
            return
        if past_search_deadline(self._current_deadline()):
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return

        # One worker rebuilds; the rest wait here and find it warm — unless
        # leftover cannot finish a rebuild. Default leftover uses timeout=0:
        # acquire if free (disk restore still runs), skip if warmup/vault-watch
        # holds the lock for an unbounded SELECT+build. Do not wait that
        # rebuild out past JSON-RPC kill. Without the lock, every RPC worker
        # that arrives after an invalidate runs its own full-table SELECT +
        # build, from potentially different DB snapshots.
        timeout = faiss_load_lock_timeout(self._current_deadline())
        if timeout is None:
            acquired = self._faiss_load_lock.acquire()
        else:
            acquired = self._faiss_load_lock.acquire(timeout=timeout)
        if not acquired:
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return
        try:
            if self.faiss_index.ready:
                return
            if past_search_deadline(self._current_deadline()):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return

            # PR-2: Try disk cache first. try_load_from_disk re-checks the
            # checksum and generation under the FAISS lock immediately before
            # applying, so a vault-watch invalidate during the disk read
            # makes this a miss, not a stale warm restore.
            try:
                conn = self.db._get_conn()
                if self.faiss_index.try_load_from_disk(db_conn=conn):
                    return
            except RequestDeadlineExceeded:
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return
            except Exception as e:
                logger.debug("Disk cache load failed (non-fatal): %s", e)

            # Disk miss. Full-table SELECT + FAISS build cannot be cancelled
            # once started; skip it when leftover cannot finish.
            if should_skip_faiss_rebuild(self._current_deadline()):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return

            # Rebuild from DB, into a STAGED structure. The live index stays
            # cold while the build is unvalidated — a concurrent search sees
            # count==0 and degrades to lexical, never a partial semantic set.
            # The staged build is committed only if (a) the checksum is the
            # same one the SELECT ran under and (b) no invalidate() advanced
            # the generation since. Retry bounded; if the DB will not sit
            # still, leave the index COLD — cold is honest and self-heals on
            # the next search, warm-partial does not.
            from minni.faiss_persist import compute_db_checksum
            conn = self.db._get_conn()

            for _ in range(3):
                generation = self.faiss_index.generation
                checksum_before = compute_db_checksum(conn)

                chunk_ids = []
                embeddings = []
                with self.db.cursor() as c:
                    c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
                    for row in c.fetchall():
                        vec = np.frombuffer(row["embedding"], dtype=np.float32)
                        if vec.shape[0] == self.config.embedding_dim:
                            chunk_ids.append(row["chunk_id"])
                            embeddings.append(vec)

                if not chunk_ids:
                    # Nothing to serve; make sure no earlier state lingers.
                    self.faiss_index.invalidate()
                    return

                all_vecs = np.array(embeddings, dtype=np.float32)
                staged = self.faiss_index.stage_build(chunk_ids, all_vecs)

                if compute_db_checksum(conn) != checksum_before:
                    logger.debug("DB changed during FAISS rebuild; retrying")
                    continue

                if not self.faiss_index.commit_staged(staged, generation):
                    logger.debug("FAISS invalidated during rebuild; retrying")
                    continue

                logger.info("FAISS index loaded from DB: %d vectors", len(chunk_ids))
                # PR-2: Save to disk for next cold start, pinned to the
                # checksum of the exact snapshot the vectors came from.
                try:
                    self.faiss_index.save_to_disk(
                        db_conn=conn, db_checksum=checksum_before
                    )
                except Exception as e:
                    logger.debug("FAISS disk save failed (non-fatal): %s", e)
                return

            logger.warning(
                "FAISS rebuild could not get a stable DB snapshot; leaving the "
                "index cold for the next search"
            )
            self.faiss_index.invalidate()
        finally:
            self._faiss_load_lock.release()

    # ── Store-time semantic indexing (durable recall) ─────────

    @property
    def chunker(self):
        """Lazy MarkdownChunker — the SAME chunker VaultIndexer uses.

        Reusing the exact chunker (and the bi-encoder ``self.model`` below)
        keeps store-time semantic indexing bit-consistent with the out-of-band
        ``VaultIndexer.index_vault`` path, so a document indexed at store time is
        chunked/embedded identically to one indexed from disk — fairness +
        a single semantic index, not two divergent schemes.
        """
        if getattr(self, "_chunker", None) is None:
            from minni.chunker import MarkdownChunker
            self._chunker = MarkdownChunker(self.config)
        return self._chunker

    def index_durable_document(
        self,
        *,
        content: str,
        path: str,
        agent: str,
        sigil: str = "❓",
        privacy_level: str = "safe",
        page_status: str = "accepted",
        page_type: Optional[str] = None,
        layer: str = "knowledge",
        whole_document: int = 0,
        model_name: Optional[str] = None,
        repair_projection: bool = False,
        on_vectors=None,
    ) -> Dict:
        """Chunk + embed + index a durable document into the SEMANTIC index.

        Called when content becomes durable via the socket (governed promote in
        _resolve_candidate(accept), or the immediate-durable _handle_learn
        force=true path). It writes the SAME tables ``VaultIndexer.index_vault``
        writes — ``documents`` + ``vault_fts`` + ``chunk_embeddings`` — using the
        SAME chunker and bi-encoder embedder, then refreshes THIS engine's live
        in-memory FAISS index so a subsequent ``search`` in the same process
        returns the new chunks WITHOUT an out-of-band indexer run or restart.

        Idempotency: keyed on ``documents.path`` (UNIQUE). Re-storing the same
        path UPDATEs the doc row and DELETEs+reinserts its chunks/fts, so no
        duplicate ``chunk_embeddings`` rows accumulate on re-store/re-accept.

        FAIL-OPEN (Minni availability principle): every failure mode here —
        embedder unavailable, encode error, FAISS error — is caught and logged;
        this method NEVER raises. The caller's durable store has already
        committed before this runs, so a semantic-index hiccup degrades recall
        to lexical-only (the prior behaviour) but NEVER loses the memory.

        Returns a small status dict (status, doc_id, chunks) for diagnostics.
        """
        try:
            if not content or not content.strip():
                return {"status": "skipped", "reason": "empty_content"}

            if repair_projection:
                from minni.durable_projection import durable_doc_path, durable_metadata

                expected = durable_metadata(content)
                if (path != durable_doc_path(agent, "", self.config.vault_path, content)
                        or page_type != "learning"
                        or any(locals_value != expected[key] for key, locals_value in (
                            ("sigil", sigil), ("privacy_level", privacy_level),
                            ("page_status", page_status), ("layer", layer)))
                        or privacy_level == "blocked"
                        or page_status in {"draft", "expired", "rejected", "superseded"}):
                    return {"status": "skipped", "reason": "ineligible_projection"}

            now = time.time()
            model_name = model_name or self.config.embedding_model

            # 0) Chunk + embed OUTSIDE the write transaction. Computing every
            #    embedding up front means an encode failure on chunk N aborts
            #    the whole batch BEFORE any chunk row is INSERTed — so we never
            #    commit a partially-indexed document (all-or-nothing semantic
            #    chunks). FAIL-OPEN: if any chunk fails to embed, we drop the
            #    semantic chunks for this doc entirely but still land the doc +
            #    FTS row below (lexical recall), rather than aborting the store.
            prepared_chunks: List[Tuple[Any, np.ndarray]] = []
            embed_failed = False
            if self.model:
                chunks = self.chunker.chunk_document(content)
                if not chunks:
                    # Short-content floor: the chunker's min_tokens (64) filter
                    # drops intra-document fragments, but a WHOLE durable memory
                    # below that floor ("the lock code is X") would otherwise get
                    # ZERO embedded chunks — semantically invisible forever, with
                    # only lexical FTS recall. Embed the whole content as one
                    # chunk so short memories are first-class in the semantic
                    # index.
                    from minni.chunker import Chunk
                    chunks = [
                        Chunk(
                            text=content.strip(),
                            heading="",
                            heading_path="",
                            chunk_index=0,
                        )
                    ]
                for chunk in chunks:
                    try:
                        from minni.models import get_embedder_lock

                        with get_embedder_lock():
                            emb = self.model.encode(
                                chunk.text, show_progress_bar=False
                            ).astype(np.float32)
                    except Exception as exc:
                        logger.warning(
                            "durable-index: embed failed for doc %r chunk %s "
                            "(%s) — dropping semantic chunks for this doc, "
                            "keeping lexical (FTS) recall",
                            path, chunk.chunk_index, exc,
                        )
                        embed_failed = True
                        break
                    prepared_chunks.append((chunk, emb))
                if embed_failed:
                    prepared_chunks = []

            # 1) Upsert the document row (idempotent by UNIQUE path) + reset its
            #    FTS/chunk rows. Mirrors the new/changed-file branch of
            #    index_vault so retrieval's chunk↔document JOIN reads it the same.
            with self.db.transaction() as c:
                if repair_projection:
                    from minni.durable_projection import ACTIVE_LEARNING_SQL

                    # BEGIN IMMEDIATE serializes this check and publication with
                    # lifecycle writes. Encoding above never holds the DB lock.
                    active = c.execute(
                        f"SELECT 1 FROM learnings WHERE agent_id=? AND content=? "
                        f"AND {ACTIVE_LEARNING_SQL} LIMIT 1", (agent, content),
                    ).fetchone()
                    if not active:
                        return {"status": "skipped", "reason": "learning_changed"}
                c.execute(
                    "SELECT doc_id FROM documents WHERE path = ?", (path,)
                )
                row = c.fetchone()
                if row and repair_projection:
                    return {"status": "skipped", "reason": "projection_exists"}
                if row:
                    doc_id = row["doc_id"]
                    c.execute(
                        """UPDATE documents
                           SET agent=?, sigil=?, last_modified=?, indexed_at=?,
                               page_status=?, privacy_level=?, page_type=?,
                               layer=?, whole_document=?
                           WHERE doc_id=?""",
                        (agent, sigil, now, now, page_status, privacy_level,
                         page_type, layer, whole_document, doc_id),
                    )
                    old_chunk_ids = [
                        r["chunk_id"]
                        for r in c.execute(
                            "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?",
                            (doc_id,),
                        ).fetchall()
                    ]
                    self._invalidate_durable_rerank(old_chunk_ids)
                    # Drop the superseded chunks from the LIVE in-memory FAISS
                    # index too. Query correctness already holds (the
                    # chunk↔document JOIN filters orphaned chunk_ids out), but
                    # without this the in-memory index keeps the old chunk_ids in
                    # its active maps and inflates on every re-store. remove()
                    # tombstones each id (drops it from the searchable maps) so
                    # the live index stays bounded to the doc's CURRENT chunks.
                    self._remove_live_faiss(old_chunk_ids)
                    c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                    c.execute(
                        "DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,)
                    )
                else:
                    c.execute(
                        """INSERT INTO documents
                           (path, agent, sigil, last_modified, indexed_at,
                            page_status, privacy_level, page_type, layer,
                            whole_document)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (path, agent, sigil, now, now, page_status,
                         privacy_level, page_type, layer, whole_document),
                    )
                    doc_id = c.lastrowid

                # FTS5 full-content row (keyword stream parity with index_vault).
                c.execute(
                    """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
                       VALUES (?, ?, ?, ?, ?)""",
                    (doc_id, path, content, agent, sigil),
                )

                # 2) Insert the pre-computed (chunk, embedding) pairs. All
                #    embeddings were already computed in step 0, so every INSERT
                #    here is guaranteed to succeed or none ran at all — no
                #    partial semantic index can commit. If embedding was
                #    unavailable/failed, prepared_chunks is empty and only the
                #    doc + FTS row above land (lexical recall, FAIL-OPEN).
                new_chunk_ids: List[int] = []
                new_vectors: List[np.ndarray] = []
                for chunk, emb in prepared_chunks:
                    c.execute(
                        """INSERT INTO chunk_embeddings
                           (doc_id, chunk_index, chunk_text, embedding,
                            heading_context, model_name, computed_at, layer)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (doc_id, chunk.chunk_index, chunk.text,
                         emb.tobytes(), chunk.heading_path, model_name,
                         now, layer),
                    )
                    new_chunk_ids.append(c.lastrowid)
                    new_vectors.append(emb)

            # 3) Refresh the LIVE in-memory FAISS index so the very next search
            #    in this long-lived process sees the new chunks (no restart, no
            #    out-of-band index_all run). If the index is already warm we add
            #    the new vectors in place; if it is cold we leave it cold so the
            #    next search's _ensure_faiss_loaded rebuilds the full set from DB
            #    (which now includes these rows). Either way the chunks become
            #    searchable in-process.
            self._refresh_live_faiss(new_chunk_ids, new_vectors)
            if on_vectors is not None and new_chunk_ids:
                on_vectors(new_chunk_ids, new_vectors)

            return {
                "status": "ok",
                "doc_id": doc_id,
                "chunks": len(new_chunk_ids),
            }
        except Exception as exc:
            # FAIL-OPEN: never let a semantic-index failure surface to the
            # caller's durable store. The memory is already persisted; recall
            # degrades to lexical-only until the next index run.
            logger.warning(
                "durable-index: semantic indexing failed for %r (%s) — "
                "store succeeded; document projection needs repair",
                path, exc,
            )
            return {"status": "degraded", "reason": str(exc)}

    def purge_durable_document(self, path: str) -> dict:
        """M4: remove a durable synthetic doc (by path) from every index.

        When a durable learning is superseded/rejected, its synthetic document
        row is left with page_status='accepted' and its chunks stay in FTS/FAISS,
        so the stale content keeps surfacing in semantic/lexical doc search.
        This mirrors the indexer's blocked-page purge: drop the FTS row, the
        chunk_embeddings rows (tombstoning them out of the live FAISS index and
        invalidating the rerank cache), then the document row itself.

        Best-effort and fail-open: the learnings-table lifecycle is the source of
        truth; a purge hiccup only means recall stays slightly stale until the
        next reindex, never a lost write.
        """
        try:
            with self.db.transaction() as c:
                c.execute("SELECT doc_id FROM documents WHERE path = ?", (path,))
                row = c.fetchone()
                if row is None:
                    return {"status": "not_found", "path": path}
                doc_id = row["doc_id"]
                old_chunk_ids = [
                    r["chunk_id"]
                    for r in c.execute(
                        "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?",
                        (doc_id,),
                    ).fetchall()
                ]
                c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
                c.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            # Outside the txn: live-index maintenance is best-effort.
            self._invalidate_durable_rerank(old_chunk_ids)
            self._remove_live_faiss(old_chunk_ids)
            return {"status": "ok", "doc_id": doc_id, "chunks": len(old_chunk_ids)}
        except Exception as exc:
            logger.warning(
                "durable-index: purge failed for %r (%s) — learnings lifecycle "
                "stands, doc-search recall stays stale until reindex",
                path, exc,
            )
            return {"status": "degraded", "reason": str(exc)}

    def _invalidate_durable_rerank(self, chunk_ids: List[int]) -> None:
        """Best-effort rerank-cache invalidation for replaced chunks."""
        if not chunk_ids:
            return
        try:
            from minni.rerank_cache import invalidate_chunks
            invalidate_chunks(chunk_ids)
        except Exception as exc:
            logger.debug("durable-index: rerank invalidation skipped: %s", exc)

    def _remove_live_faiss(self, chunk_ids: List[int]) -> None:
        """Tombstone superseded chunk_ids out of the live FAISS index.

        Called on a re-store before the new chunks are added, so the in-memory
        index stays bounded to the document's CURRENT chunks instead of growing
        on every re-store. Best-effort: the rows are already being DELETEd from
        chunk_embeddings, so even if this is a no-op (cold index) a later
        search/rebuild reflects the correct set. faiss_index.remove drops the id
        from the searchable maps (_reverse_map / _id_map); search() filters on
        those, so a tombstoned chunk is no longer returned.
        """
        if not chunk_ids:
            return
        try:
            # Warm check and all removals happen atomically inside the FAISS
            # lock — an unlocked count>0 snapshot followed by per-id calls can
            # interleave with the vault-watch thread's invalidate().
            self.faiss_index.remove_batch(chunk_ids)
        except Exception as exc:
            logger.debug("durable-index: live FAISS remove skipped: %s", exc)

    def _refresh_live_faiss(
        self, chunk_ids: List[int], vectors: List[np.ndarray]
    ) -> bool:
        """Make new chunks searchable in the live FAISS index without restart.

        Warm index → add the new vectors directly (cheap, immediate). Cold index
        → leave it cold; the next search's _ensure_faiss_loaded rebuilds from the
        DB, which now contains these rows. Failures here are non-fatal: the rows
        are durably in chunk_embeddings, so a later search/rebuild still finds
        them. Return readiness so off-RPC callers can retain a retry when
        both notification and its immediate recovery fail.
        """
        if not chunk_ids:
            return True
        try:
            # add_batch re-checks warm/invalidated under the FAISS lock and
            # holds it for the whole batch. The previous shape — an unlocked
            # count>0 snapshot, then one lock acquisition per add — let the
            # vault-watch thread's invalidate() land between two adds, leaving
            # a residual index of only the new chunks that count>0 gates then
            # treated as warm: a semantic blackout until restart.
            if not self.faiss_index.add_batch(chunk_ids, vectors):
                # Cold or invalidated. Usually fine — the rows are in the DB
                # and the next ensure-load rebuilds. But an ensure that was
                # mid-SELECT when these rows committed builds WITHOUT them and
                # lands warm, and count>0 then gates every later rebuild. The
                # retry behind the load lock waits that ensure out: either the
                # index is now warm (add lands; idempotent if the rebuild
                # already picked the rows up) or it is still cold and the next
                # ensure sees the rows in the DB.
                with self._faiss_load_lock:
                    self.faiss_index.add_batch(chunk_ids, vectors)
            return self.faiss_index.ready
        except Exception as exc:
            logger.warning(
                "durable-index: live FAISS refresh failed (%s) — invalidating "
                "so next search reloads from DB", exc,
            )
            # Force a cold reload. Default leftover (22.5s / 27s) skips
            # in-request rebuild after invalidate, and disk restore misses
            # because generation/checksum moved — so unbounded-ensure here,
            # the same way vault-watch reloads the shared engine.
            try:
                self.faiss_index.invalidate()
            except Exception:
                pass
            try:
                self._set_current_deadline(None)
                self._ensure_faiss_loaded()
            except Exception as reload_exc:
                logger.debug(
                    "durable-index: unbounded FAISS reload after refresh "
                    "failure skipped: %s", reload_exc,
                )

        return self.faiss_index.ready

    # ── Cross-Encoder Re-Ranking ──────────────────────────────

    def _apply_correction_rerank_boost(self, candidates: List[Dict]) -> None:
        """recall-F3 (reranker leg): propagate the correction salience channel
        into the cross-encoder ordering. Without this, the boost only lived in
        final_score and was bypassed whenever reranker_enabled=True (the
        default) — exactly the warm top-K path the operator cares about.

        rerank_score is a raw logit (can be negative), so the bounded
        multiplicative boost is applied sign-safely: positive logits scale up,
        negative logits shrink toward zero — a correction-class candidate
        always moves up relative to its raw logit. A logit of exactly 0.0 is
        lifted to +boost (a multiplicative boost on zero is a no-op, which
        would leave the correction tied with zero-logit habitual hits). The
        rerank cache stores the raw model score BEFORE this adjustment, so
        cached entries stay model-pure and the boost is re-derived on every
        call.
        """
        boost = float(self.config.correction_salience_boost)
        if boost <= 0:
            return
        for c in candidates:
            page_type = str(c.get("page_type") or "").lower()
            if page_type not in self._correction_types:
                continue
            score = float(c.get("rerank_score") or 0.0)
            if score > 0:
                c["rerank_score"] = score * (1.0 + boost)
            elif score < 0:
                c["rerank_score"] = score / (1.0 + boost)
            else:
                c["rerank_score"] = boost
            c["salience_boost"] = boost

    def _apply_decay_rerank_attenuation(self, candidates: List[Dict]) -> None:
        """GA4-2 (reranker leg): propagate memory decay into the cross-encoder
        ordering. Exactly the bypass class _apply_correction_rerank_boost above
        was written to close: decay_score only ever entered final_score
        (_score_merged_doc), but the DEFAULT path (reranker_enabled=True) sorts
        and reports rerank_score, so a scheduled decay pass changed nothing an
        operator could observe on default config.

        Same sign-safe treatment as the correction boost, since rerank_score is
        a raw logit: a positive logit is scaled DOWN by decay, a negative logit
        is pushed further down (divided by decay), so attenuation always moves a
        stale candidate down relative to its raw logit. A logit of exactly 0.0
        maps to (decay - 1.0) — zero when undecayed, negative once decayed —
        because a multiplicative attenuation of zero is a no-op that would leave
        a fully-decayed candidate tied with a fresh one.

        Correction-class candidates get the same recall-F4 decay floor they get
        in _score_merged_doc, so the two legs agree on what a correction's
        effective decay is. Runs AFTER the correction boost (grok-review
        round 3, finding 3): running decay first mapped a raw 0.0 logit to the
        negative decay - 1.0, so the boost's zero-logit special case never
        fired and a decayed zero-logit correction sank BELOW an undecayed
        zero-logit non-correction. The non-zero branches are commutative
        multiplications, so only the zero dispatch depends on the order — and
        it must see the model-pure logit.

        The rerank cache stores the raw model score BEFORE this adjustment, so
        cached entries stay model-pure and attenuation is re-derived every call.
        """
        floor = float(self.config.correction_decay_floor)
        for c in candidates:
            decay = c.get("decay_score")
            if decay is None:
                decay = 1.0
            decay = float(decay)
            page_type = str(c.get("page_type") or "").lower()
            if page_type in self._correction_types:
                decay = max(decay, floor)
            # Clamp to the (0, 1] the decay pass is contracted to produce; a
            # decay > 1 would silently become a promotion channel.
            decay = max(0.0, min(1.0, decay))
            c["decay_applied"] = decay
            if decay == 1.0:
                continue
            score = float(c.get("rerank_score") or 0.0)
            if score > 0:
                c["rerank_score"] = score * decay
            elif score < 0:
                # decay == 0.0 would divide by zero; a fully decayed negative
                # logit is already as low as the ordering needs it to be.
                c["rerank_score"] = score / decay if decay > 0 else score * 2.0
            else:
                c["rerank_score"] = decay - 1.0

    def _apply_rerank_score_adjustments(self, candidates: List[Dict]) -> None:
        """Both post-model adjustments. Single call site so the two legs
        cannot drift.

        grok-review round 2 (finding 1): preserve the model-pure logit as
        raw_rerank_score BEFORE any adjustment. compute_confidence takes
        decay_factor as its own input, so feeding it the decay-attenuated
        rerank_score applies decay twice — biasing the HyDE trigger, the
        calibration window (record=True), and provenance for aged docs.
        Ranking sorts on the adjusted rerank_score; confidence and provenance
        read raw_rerank_score.

        grok-review round 3 (finding 3): boost BEFORE decay. Both adjustments
        special-case a 0.0 score, and only the boost-first order lets each
        special case see the score it was written for: the boost dispatches on
        the model-pure logit (zero-logit corrections lift to +boost, which
        decay then attenuates to boost * decay > 0), while decay's zero case
        keeps handling genuinely-zero non-corrections. The non-zero branches
        multiply commutatively, so the two legs still agree with
        final_score = rrf * decay * (1 + boost)."""
        for c in candidates:
            c["raw_rerank_score"] = float(c.get("rerank_score") or 0.0)
        self._apply_correction_rerank_boost(candidates)
        self._apply_decay_rerank_attenuation(candidates)

    def _rerank(self, query: str, candidates: List[Dict]) -> List[Dict]:
        """
        Re-rank candidates using a cross-encoder.

        Cross-encoders score (query, passage) pairs directly — much more
        accurate than bi-encoder similarity, but slower. We use it as a
        second pass on the top candidates from the fast first pass.
        """
        reranker = self.reranker
        if not reranker or not candidates:
            return candidates

        model_name, model_version = self._reranker_identity(reranker)
        try:
            from minni.rerank_cache import GLOBAL_RERANK_CACHE
            cache = GLOBAL_RERANK_CACHE
        except Exception:
            cache = None

        missing = []
        missing_indexes = []
        all_scores = [None] * len(candidates)
        corpus = self._rerank_corpus()
        generation = cache.generation if cache is not None else None
        if cache is not None:
            for i, c in enumerate(candidates):
                chunk_id = c.get("chunk_id")
                if chunk_id is None:
                    missing.append(c)
                    missing_indexes.append(i)
                    continue
                cached = cache.get(
                    model_name, model_version, query, int(chunk_id),
                    corpus=corpus, passage=self._rerank_passage(c),
                )
                if cached is None:
                    missing.append(c)
                    missing_indexes.append(i)
                else:
                    all_scores[i] = cached
        else:
            missing = candidates
            missing_indexes = list(range(len(candidates)))

        if not missing:
            for i, c in enumerate(candidates):
                c["rerank_score"] = float(all_scores[i])
            self._apply_rerank_score_adjustments(candidates)
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            return candidates

        # Prepare pairs for the cross-encoder
        pairs = [[query, self._rerank_passage(c)] for c in missing]

        # perf/parallel-fanout (#388) + Cassandra RED-2: the global lock is
        # skipped ONLY when the pinned-CPU precondition holds (CPU device +
        # fired torch-thread pin, see models.cross_encoder_unlocked_predict_safe).
        # A stress probe (8 threads x 3 rounds vs serial, committed as
        # tests/test_parallel_fanout_red.py::test_cross_encoder_concurrent_predict_matches_serial)
        # verified CrossEncoder.predict byte-identical under concurrency on
        # that path (torch.set_num_threads(1) + OMP/MKL pins in minnid.main /
        # models._pin_torch_threads_for_cpu_once — untouched). WITHOUT the
        # pin the same probe segfaults (OpenMP oversubscription, #299 class),
        # so every other device — and an unfired pin — takes the lock. The
        # proof therefore covers all paths, not just the pinned-CPU daemon.
        from minni.models import (
            cross_encoder_unlocked_predict_safe,
            get_cross_encoder_lock,
        )

        try:
            if past_search_deadline(self._current_deadline()):
                self.last_rerank_degraded = "search deadline; skipped rerank"
                return candidates
            if cross_encoder_unlocked_predict_safe():
                scores = reranker.predict(pairs, show_progress_bar=False)
                if past_search_deadline(self._current_deadline(), min_remaining=0):
                    self.last_rerank_degraded = (
                        "search deadline exceeded during nonpreemptible rerank"
                    )
            else:
                with search_model_lock(
                    get_cross_encoder_lock(), self._current_deadline()
                ) as acquired:
                    # Concurrent search can pass the pre-lock floor, then wait
                    # through another predict. Re-check after the lock so a waiter
                    # does not score after the client 30s kill.
                    if not acquired or past_search_deadline(self._current_deadline()):
                        self.last_rerank_degraded = "search deadline; skipped rerank"
                        return candidates
                    scores = reranker.predict(pairs, show_progress_bar=False)
                    if past_search_deadline(self._current_deadline(), min_remaining=0):
                        self.last_rerank_degraded = (
                            "search deadline exceeded during nonpreemptible rerank"
                        )

            for score_index, c, score_value in zip(missing_indexes, missing, scores):
                score = float(score_value)
                all_scores[score_index] = score
                chunk_id = c.get("chunk_id")
                if cache is not None and chunk_id is not None:
                    cache.set(
                        model_name, model_version, query, int(chunk_id), score,
                        corpus=corpus, passage=self._rerank_passage(c),
                        expected_generation=generation,
                    )

            for i, c in enumerate(candidates):
                c["rerank_score"] = float(all_scores[i] or 0.0)

            # Sort by cross-encoder score (correction salience applied first)
            self._apply_rerank_score_adjustments(candidates)
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        except Exception as e:
            logger.warning("Re-ranking failed: %s — falling back to RRF scores", e)
            # R5 (#226): the fallback leaves these candidates with no
            # rerank_score, so in a combined/both merge this corpus competes at
            # raw RRF magnitude against reranked corpora and is silently evicted.
            # Record it; the caller reports it rather than presenting the merge
            # as a clean cross-corpus ordering.
            self._current_state().rerank_degraded = str(e)

        return candidates

    def _rerank_corpus(self) -> str:
        db_path = getattr(self.config, "db_path", None)
        if not db_path or str(db_path) == ":memory:":
            db = getattr(self, "db", None)
            return f"memory:{id(db if db is not None else self)}"
        return str(Path(db_path).expanduser().resolve())

    @staticmethod
    def _rerank_passage(candidate: Dict) -> str:
        passage = candidate.get("chunk_text", "") or ""
        heading = candidate.get("heading_context", "")
        return f"[{heading}] {passage}" if heading else passage

    def _reranker_identity(self, reranker) -> tuple[str, str]:
        model_name = getattr(self.config, "reranker_model", None) or "unknown"
        for attr in ("model_name", "name"):
            value = getattr(reranker, attr, None)
            if value:
                model_name = str(value)
                break
        model_version = "unknown"
        for attr in ("model_version", "version", "revision"):
            value = getattr(reranker, attr, None)
            if value:
                model_version = str(value)
                break
        return model_name, model_version

    # ── Feedback ──────────────────────────────────────────────

    def record_feedback(
        self,
        query: str,
        result_id: int,
        useful: bool,
        agent_id: str = "main",
    ) -> Dict:
        """Store useful/not-useful feedback for a result id."""
        doc_id = None
        chunk_id = None
        with self.db.cursor() as c:
            c.execute(
                "SELECT chunk_id, doc_id FROM chunk_embeddings WHERE chunk_id = ?",
                (result_id,),
            )
            row = c.fetchone()
            if row is not None:
                chunk_id = row["chunk_id"]
                doc_id = row["doc_id"]
            else:
                c.execute("SELECT doc_id FROM documents WHERE doc_id = ?", (result_id,))
                row = c.fetchone()
                if row is not None:
                    doc_id = row["doc_id"]

            c.execute(
                """INSERT INTO feedback
                   (query_hash, query_text, doc_id, chunk_id, agent_id, useful, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    _query_class(query),
                    query,
                    doc_id,
                    chunk_id,
                    agent_id,
                    1 if useful else 0,
                    int(time.time()),
                ),
            )
            feedback_id = c.lastrowid

        self._feedback_cache_loaded_at = 0.0
        return {
            "status": "ok",
            "feedback_id": feedback_id,
            "query_hash": _query_class(query),
            "query_text": query,
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "agent_id": agent_id,
            "useful": bool(useful),
        }

    def _feedback_demotions(self, query: str, agent_id: Optional[str]) -> Dict[int, float]:
        """Return capped per-doc demotions for this agent/query class."""
        if not getattr(self.config, "feedback_enabled", True):
            return {}
        now = time.time()
        if now - self._feedback_cache_loaded_at > 60:
            self._refresh_feedback_cache()
        return self._feedback_cache.get((agent_id or "main", _query_class(query)), {})

    def _refresh_feedback_cache(self) -> None:
        """Refresh recent negative feedback cache; failures degrade to no demotion."""
        cache = {}
        cutoff = int(time.time()) - 30 * 86400
        try:
            with self.db.cursor() as c:
                c.execute(
                    """SELECT query_text, doc_id, agent_id, useful, COUNT(*) AS votes
                       FROM feedback
                       WHERE created_at >= ? AND doc_id IS NOT NULL
                       GROUP BY query_text, doc_id, agent_id, useful""",
                    (cutoff,),
                )
                rows = c.fetchall()
        except RequestDeadlineExceeded:
            logger.debug("feedback cache refresh skipped after deadline")
            return
        except Exception as exc:
            logger.debug("feedback cache refresh skipped: %s", exc)
            self._feedback_cache = {}
            self._feedback_cache_loaded_at = time.time()
            return

        for row in rows:
            if int(row["useful"] or 0) != 0:
                continue
            key = (row["agent_id"] or "main", _query_class(row["query_text"] or ""))
            doc_id = row["doc_id"]
            demote = max(-0.3, -0.05 * int(row["votes"] or 0))
            cache.setdefault(key, {})[doc_id] = min(
                demote,
                cache.setdefault(key, {}).get(doc_id, 0.0),
            )

        self._feedback_cache = cache
        self._feedback_cache_loaded_at = time.time()

    def _apply_feedback_demotions(
        self,
        merged: List[Dict],
        query: str,
        agent_id: Optional[str],
    ) -> List[Dict]:
        demotions = self._feedback_demotions(query, agent_id)
        if not demotions:
            return merged
        for r in merged:
            demote = demotions.get(r["doc_id"], 0.0)
            r["feedback_demote"] = demote
            base_score = r.get("rerank_score", r.get("final_score", 0.0))
            r["feedback_adjusted_score"] = base_score + demote
            if demote:
                r["final_score"] = r.get("final_score", 0.0) + demote
        merged.sort(
            key=lambda x: x.get(
                "feedback_adjusted_score",
                x.get("rerank_score", x.get("final_score", 0.0)),
            ),
            reverse=True,
        )
        return merged

    # ── Reciprocal Rank Fusion ─────────────────────────────────

    def _score_merged_doc(self, d: Dict) -> None:
        """Compute final_score for one RRF-merged doc, with the correction
        salience channel (recall-F3) and decay floor (recall-F4).

        Default scoring is unchanged: final_score = rrf_score * decay_score.
        Correction-class notes (page_type in config.correction_page_types) get
        a bounded multiplicative boost and a decay floor so a fresh correction
        can outrank a stale habitual hit whose decay saturated via access
        reinforcement (decay rewards rereads; corrections start unread).

        When the cross-encoder reranker is active, the boost is also
        propagated into the logit-driven ordering — see
        _apply_correction_rerank_boost, called from _rerank.
        """
        boost = 0.0
        # Defensive: merged docs are normally built with `or 1.0` defaults,
        # but an explicit decay_score=None from a downstream caller must not
        # TypeError inside max(). `is not None` (not falsy-or): a legitimate
        # decay_score of 0.0 must score as fully decayed, not be coerced to 1.0.
        decay = d.get("decay_score")
        if decay is None:
            decay = 1.0
        page_type = str(d.get("page_type") or "").lower()
        if page_type in self._correction_types:
            # Direct field access: correction_salience_boost / _decay_floor are
            # SovereignConfig dataclass fields with defaults (config.py); only
            # correction_class_page_types() tolerates duck-typed configs.
            boost = float(self.config.correction_salience_boost)
            floor = float(self.config.correction_decay_floor)
            decay = max(decay, floor)
        d["salience_boost"] = boost
        # grok-review round 5 (finding 3): stamp the EFFECTIVE decay (same
        # clamp as the rerank leg) so confidence reads the value ranking used —
        # a correction floored to 0.5 must not rank semi-fresh while its
        # confidence and recorded calibration sample say near-dead 0.01.
        # Round 7 (finding 2): final_score multiplies the SAME clamped value —
        # multiplying the raw decay let a poison decay_score of 2.0 promote
        # (rrf * 2) on the non-rerank leg while the rerank leg, the stamp, and
        # confidence all treated it as 1.0.
        decay = max(0.0, min(1.0, float(decay)))
        d["decay_applied"] = decay
        d["final_score"] = d["rrf_score"] * decay * (1.0 + boost)

    def _rrf_merge(
        self,
        fts_results: List[Dict],
        semantic_results: List[Dict],
        limit: int,
    ) -> List[Dict]:
        """
        Reciprocal Rank Fusion: merge FTS5 and semantic results.
        RRF(d) = Σ  1 / (k + rank_i(d))
        """
        k = self.config.rrf_k
        doc_scores: Dict[int, Dict] = {}

        for rank, r in enumerate(fts_results, start=1):
            did = r["doc_id"]
            if did not in doc_scores:
                doc_scores[did] = {
                    "doc_id": did,
                    "path": r["path"],
                    "agent": r["agent"],
                    "sigil": r["sigil"],
                    "rrf_score": 0.0,
                    "decay_score": r.get("decay_score", 1.0),
                    "fts_rank": rank,
                    "sem_rank": None,
                    "chunk_text": r.get("chunk_text", ""),
                    "heading_context": r.get("heading_context", ""),
                    "page_status": r.get("page_status", "candidate"),
                    "privacy_level": r.get("privacy_level", "safe"),
                    "page_type": r.get("page_type"),
                    "evidence_refs": r.get("evidence_refs"),
                    "indexed_at": r.get("indexed_at"),
                    "layer": r.get("layer", "knowledge"),
                }
            doc_scores[did]["rrf_score"] += self.config.fts_weight / (k + rank)

        for rank, r in enumerate(semantic_results, start=1):
            did = r["doc_id"]
            if did not in doc_scores:
                doc_scores[did] = {
                    "doc_id": did,
                    "path": r["path"],
                    "agent": r["agent"],
                    "sigil": r["sigil"],
                    "rrf_score": 0.0,
                    "decay_score": r.get("decay_score", 1.0),
                    "fts_rank": None,
                    "sem_rank": rank,
                    "chunk_text": r.get("chunk_text", ""),
                    "heading_context": r.get("heading_context", ""),
                    "page_status": r.get("page_status", "candidate"),
                    "privacy_level": r.get("privacy_level", "safe"),
                    "page_type": r.get("page_type"),
                    "evidence_refs": r.get("evidence_refs"),
                    "indexed_at": r.get("indexed_at"),
                    "layer": r.get("layer", "knowledge"),
                }
            doc_scores[did]["rrf_score"] += self.config.semantic_weight / (k + rank)
            if doc_scores[did]["sem_rank"] is None:
                doc_scores[did]["sem_rank"] = rank
            # Prefer the semantic chunk over FTS full-page content: FTS now
            # carries the whole file (frontmatter first) as a fallback body,
            # which must not shadow the matching passage on dual hits.
            if r.get("chunk_text"):
                doc_scores[did]["chunk_text"] = r["chunk_text"]
                doc_scores[did]["heading_context"] = r.get("heading_context", "")
                # Cache/attribution identity belongs to the selected passage,
                # never the FTS full page or a previously selected chunk.
                if r.get("chunk_id") is not None:
                    doc_scores[did]["chunk_id"] = r["chunk_id"]
                else:
                    doc_scores[did].pop("chunk_id", None)
            # Carry forward page metadata from semantic if FTS didn't provide it
            if not doc_scores[did].get("page_type") and r.get("page_type"):
                doc_scores[did]["page_type"] = r["page_type"]
            if not doc_scores[did].get("evidence_refs") and r.get("evidence_refs"):
                doc_scores[did]["evidence_refs"] = r["evidence_refs"]
            if not doc_scores[did].get("indexed_at") and r.get("indexed_at"):
                doc_scores[did]["indexed_at"] = r["indexed_at"]
            if not doc_scores[did].get("layer") and r.get("layer"):
                doc_scores[did]["layer"] = r["layer"]

        for d in doc_scores.values():
            self._score_merged_doc(d)

        ranked = sorted(doc_scores.values(), key=lambda x: x["final_score"], reverse=True)
        return ranked[:limit]

    # ── Context Window Budgeting ──────────────────────────────

    def _budget_results(self, results: List[Dict], query: str) -> List[Dict]:
        """
        Trim results to fit within context_budget_tokens.
        Returns as many results as fit within the token budget.
        """
        budget = self.config.context_budget_tokens
        if budget <= 0:
            return results

        budgeted = []
        total_tokens = 0

        for r in results:
            chunk_text = r.get("chunk_text", "")
            heading = r.get("heading_context", "")
            # Estimate tokens for this result's contribution to context
            entry_text = f"{heading}: {chunk_text}" if heading else chunk_text
            entry_tokens = self._count_tokens(entry_text)

            if total_tokens + entry_tokens > budget and budgeted:
                # Would exceed budget — stop
                break

            total_tokens += entry_tokens
            r["token_count"] = entry_tokens
            budgeted.append(r)

        return budgeted

    # ── Depth-tier field filtering ─────────────────────────────

    def _apply_depth(self, result: Dict, depth: str) -> Dict:
        """
        Filter a result dict to only the fields appropriate for *depth*.

        headline  — wikilink, title, score, confidence, age_days
        snippet   — + text (≤280 chars) [DEFAULT — matches current callers]
        chunk     — + full chunk text, heading_context, provenance
        document  — chunk + full_document_text (whole_document rows only)
        """
        if depth not in _VALID_DEPTHS:
            depth = "snippet"

        # Helper: add all PR-2 envelope fields (additive — present in all tiers)
        # Does NOT override provenance if already built by the caller.
        def _pr2_fields(result: Dict, existing: Optional[Dict] = None) -> Dict:
            fields = {
                "confidence": result.get("confidence"),
                # Survives every depth tier so the daemon RPC boundary can
                # record (then pop) the pre-calibration raw for the final
                # merged set — GA4-1, grok-review round 4 (finding 1).
                "confidence_raw": result.get("confidence_raw"),
                "rationale": result.get("rationale"),
                "privacy_level": result.get("privacy_level"),
                "source_authority": result.get("source_authority"),
                "review_state": result.get("review_state"),
                "instruction_like": result.get("instruction_like"),
                "wikilink": result.get("wikilink"),
                "evidence_refs": result.get("evidence_refs"),
                "recommended_action": result.get("recommended_action"),
                "recommended_wiki_updates": result.get("recommended_wiki_updates") or [],
                "attribution": result.get("attribution"),
                "attribution_score": result.get("attribution_score"),
                "attribution_model": result.get("attribution_model"),
            }
            if result.get("full_provenance") is not None:
                fields["full_provenance"] = result.get("full_provenance")
            # Only include provenance if not already in existing dict
            if existing is None or "provenance" not in existing:
                fields["provenance"] = result.get("provenance")
            return fields

        if depth == "headline":
            out = {
                "source": result.get("source", ""),
                "filename": result.get("filename", ""),
                "score": result.get("score", 0),
                "doc_id": result.get("doc_id"),
                "chunk_id": result.get("chunk_id"),
                "confidence": result.get("confidence"),
                # grok-review round 7 (finding 1): the GA4-1 carrier must ride
                # EVERY tier — omitting it here meant headline hits never fed
                # score_distribution and never got rewritten onto the shared
                # basis while the same query at snippet depth did both.
                # test_every_depth_tier_carries_the_calibration_carrier pins
                # all four tiers so they cannot drift again.
                "confidence_raw": result.get("confidence_raw"),
                "age_days": result.get("age_days"),
                "layer": result.get("layer"),
                "decay_factor": _effective_decay(result, result.get("decay_factor")),
                "privacy_level": result.get("privacy_level"),
                "review_state": result.get("review_state"),
                "instruction_like": result.get("instruction_like"),
                "attribution": result.get("attribution"),
                "attribution_score": result.get("attribution_score"),
                "attribution_model": result.get("attribution_model"),
                "wikilink": result.get("wikilink"),
                "depth": "headline",
            }
            if result.get("provenance") is not None:
                out["provenance"] = result["provenance"]
            return out

        if depth == "snippet":
            text = result.get("chunk_text", "")
            if len(text) > _SNIPPET_MAX_CHARS:
                text = text[:_SNIPPET_MAX_CHARS] + "…"
            out = {
                "text": text,
                "source": result.get("source", ""),
                "filename": result.get("filename", ""),
                "heading": result.get("heading_context", ""),
                "score": result.get("score", 0),
                "doc_id": result.get("doc_id"),
                "layer": result.get("layer"),
                "depth": "snippet",
            }
            # Keep token_count if already computed
            if "token_count" in result:
                out["token_count"] = result["token_count"]
            out.update(_pr2_fields(result, out))
            return out

        if depth in ("chunk", "document"):
            built_prov = result.get("provenance") or {
                "fts_rank": result.get("fts_rank"),
                "semantic_rank": result.get("sem_rank"),
                "rrf_score": result.get("rrf_score"),
                # Raw logit: provenance also exposes decay_factor, so a
                # consumer re-blending the two must not get a pre-decayed score.
                "cross_encoder_score": result.get(
                    "raw_rerank_score", result.get("rerank_score")
                ),
                # round 6 (finding 3): the EFFECTIVE decay ranking/confidence
                # used (correction floor + clamp), or provenance lies for
                # correction-class docs (reports 0.01 while everything ranked
                # on 0.5).
                "decay_factor": _effective_decay(result),
                "doc_id": result.get("doc_id"),
                "chunk_id": result.get("chunk_id"),
                "agent_origin": result.get("agent", ""),
                "backend": "faiss-disk",
            }
            out = {
                "text": result.get("chunk_text", ""),
                "source": result.get("source", ""),
                "filename": result.get("filename", ""),
                "heading": result.get("heading_context", ""),
                "score": result.get("score", 0),
                "doc_id": result.get("doc_id"),
                "chunk_id": result.get("chunk_id"),
                "agent": result.get("agent", ""),
                "sigil": result.get("sigil", ""),
                "layer": result.get("layer"),
                "fts_rank": result.get("fts_rank"),
                "sem_rank": result.get("sem_rank"),
                "provenance": built_prov,
                "depth": depth,
            }
            if "token_count" in result:
                out["token_count"] = result["token_count"]
            out.update(_pr2_fields(result, out))
            # document tier: add full_document_text if available in result
            if depth == "document" and "full_document_text" in result:
                out["full_document_text"] = result["full_document_text"]
            return out

        # Fallback: snippet
        return self._apply_depth(result, "snippet")

    _DOCUMENT_HYDRATION_DEADLINE = "search deadline; skipped full document"

    def _stamp_document_hydration_degraded(self, raw: Dict) -> None:
        """Keep the ranked chunk; record that document depth did not complete."""
        reason = self._DOCUMENT_HYDRATION_DEADLINE
        self.last_document_hydration_degraded = reason
        raw["requested_depth"] = "document"
        raw["delivered_depth"] = "chunk"
        raw["document_hydration"] = reason
        prov = raw.get("provenance")
        if isinstance(prov, dict):
            prov["requested_depth"] = "document"
            prov["delivered_depth"] = "chunk"
            prov["document_hydration"] = reason

    def _project_depth(self, raw: Dict, depth: str) -> Dict:
        apply_as = depth
        if (
            raw.get("document_hydration")
            and raw.get("delivered_depth") in _VALID_DEPTHS
        ):
            apply_as = raw["delivered_depth"]
        projected = self._apply_depth(raw, apply_as)
        for key in ("requested_depth", "delivered_depth", "document_hydration"):
            if key in raw:
                projected[key] = raw[key]
        if raw.get("document_hydration"):
            projected["depth"] = raw.get("delivered_depth") or "chunk"
            prov = projected.get("provenance")
            if isinstance(prov, dict):
                prov["requested_depth"] = raw.get("requested_depth")
                prov["delivered_depth"] = raw.get("delivered_depth")
                prov["document_hydration"] = raw.get("document_hydration")
        return projected

    def _fetch_full_document(self, doc_id: int) -> Optional[str]:
        """Fetch the full concatenated text for a whole_document row."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT chunk_text FROM chunk_embeddings
                WHERE doc_id = ?
                ORDER BY chunk_id
            """, (doc_id,))
            rows = c.fetchall()
            if rows:
                return "\n".join(row["chunk_text"] for row in rows)
            # No chunks is the NORMAL state for an unendorsed page (see
            # indexer.UNEMBEDDED_STATUSES), not a missing document. Fall back to
            # the FTS copy so document depth returns the page instead of None.
            c.execute("SELECT content FROM vault_fts WHERE doc_id = ?", (doc_id,))
            fts_row = c.fetchone()
        return fts_row["content"] if fts_row and fts_row["content"] else None

    # ── Public API ─────────────────────────────────────────────

    # R9: fan-out guard. A wire-supplied backend list of ["faiss-disk"]*N would
    # build N disk-loading backends; dedup and cap the list, and reject unknown
    # members loudly instead of silently skipping (a silent skip hid typos and
    # let an attacker probe backend names).
    _KNOWN_BACKENDS = ("faiss-disk", "faiss-mem", "qdrant", "lance")
    _MAX_BACKENDS = 4

    def _note_vector_model_down(self) -> None:
        """Raise the P0-B degradation flag, logging once per outage."""
        if not self.vector_model_down:
            logger.warning(
                "semantic leg DOWN: embedding model unavailable — recall "
                "degraded to lexical (FTS) only until the encoder loads"
            )
        self.vector_model_down = True
        # Per-request verdict for the response envelope (round 2, PR #260).
        # Per-call state (#388), not the thread-local: concurrent same-engine
        # calls must not share it. The process-wide bool above stays as the
        # health-surface outage signal and log-once guard.
        self._current_state().vector_degraded = (
            "embedding model unavailable; lexical (FTS) only"
        )

    def _reset_encode_ms(self) -> None:
        local = getattr(self, "_degradation_local", None)
        if local is None:
            local = threading.local()
            self._degradation_local = local
        local.encode_ms = 0.0

    def _add_encode_ms(self, ms: float) -> None:
        local = getattr(self, "_degradation_local", None)
        if local is None:
            local = threading.local()
            self._degradation_local = local
        local.encode_ms = float(getattr(local, "encode_ms", 0.0) or 0.0) + ms

    def _take_encode_ms(self) -> float:
        local = getattr(self, "_degradation_local", None)
        if local is None:
            return 0.0
        ms = float(getattr(local, "encode_ms", 0.0) or 0.0)
        local.encode_ms = 0.0
        return ms

    def _lookup_query_embedding(self, query: str):
        embeddings = getattr(getattr(self, "_degradation_local", None), "query_embeddings", None)
        if embeddings is not None and query in embeddings:
            # Backends may normalize/mutate their input; keep the memo intact.
            return embeddings[query].copy()
        memo = current_query_embed_cache()
        if memo is None:
            return None
        cached = memo.get(query)
        if cached is None:
            return None
        if embeddings is not None:
            embeddings[query] = cached.copy()
        return cached

    def _store_query_embedding(self, query: str, vec: np.ndarray) -> None:
        if vec is None or vec.size == 0:
            return
        embeddings = getattr(getattr(self, "_degradation_local", None), "query_embeddings", None)
        if embeddings is not None:
            embeddings[query] = vec.copy()
        memo = current_query_embed_cache()
        if memo is not None:
            memo.set(query, vec)

    def _encode_query(
        self,
        query: str,
        deadline_monotonic: Optional[float] = None,
    ) -> np.ndarray:
        """Encode ``query``, raising the P0-B flag when the encoder is down.

        R4(b) (#226): the explicit-backend branches used to inline
        ``self.model.encode(q) if self.model else np.array([])``, which fed an
        EMPTY query vector into the backend with no log line and no flag. The
        request succeeded and returned lexical-only results indistinguishable
        from a healthy hybrid search. Every path that needs a query vector now
        goes through here, so degradation is reported on the same code path as
        the default branch.

        Production search stamps one request_deadline around every corpus
        leg. The request-scoped memo reuses a successful encode of the same
        query string so serial vault legs do not pay the embedder again.
        Deadline and cold-load skips still run before any cache hit is used
        to start FAISS: an expired budget must not turn a cached vector into
        a silent hybrid ranking.
        """
        if deadline_monotonic is None:
            deadline_monotonic = self._current_deadline()
        if past_search_deadline(deadline_monotonic):
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return np.array([], dtype=np.float32)
        cached = self._lookup_query_embedding(query)
        if cached is not None:
            return cached
        # Round 18: only clear the process-wide down flag AFTER a successful
        # encode. Clearing before encode() meant an OOM/runtime fault left
        # health reading "encoder up" and hard-failed the request instead of
        # FTS-only degrade with last_vector_degraded set (R4(b) throw path).
        try:
            from minni.models import get_embedder, get_embedder_lock

            if should_skip_cold_model_load(deadline_monotonic, get_embedder):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return np.array([], dtype=np.float32)

            with search_model_lock(get_embedder_lock(), deadline_monotonic) as acquired:
                # Concurrent search can pass the pre-lock floor, then wait
                # through another encode/FAISS. Re-check after the lock so a
                # waiter does not encode after the client 30s kill.
                if not acquired or past_search_deadline(deadline_monotonic):
                    self.last_vector_degraded = "search deadline; lexical (FTS) only"
                    return np.array([], dtype=np.float32)
                if should_skip_cold_model_load(deadline_monotonic, get_embedder):
                    self.last_vector_degraded = "search deadline; lexical (FTS) only"
                    return np.array([], dtype=np.float32)
                cached = self._lookup_query_embedding(query)
                if cached is not None:
                    return cached
                if not self.model:
                    self._note_vector_model_down()
                    return np.array([], dtype=np.float32)
                started = time.perf_counter()
                try:
                    vec = self.model.encode(query, show_progress_bar=False).astype(np.float32)
                finally:
                    self._add_encode_ms((time.perf_counter() - started) * 1000)
                if past_search_deadline(deadline_monotonic, min_remaining=0):
                    self.last_vector_degraded = "search deadline exceeded during nonpreemptible encode"
        except Exception as exc:
            if not self.vector_model_down:
                logger.warning(
                    "semantic leg DOWN: embedding encode failed (%s) — "
                    "recall degraded to lexical (FTS) only",
                    exc,
                )
            self.vector_model_down = True
            self._current_state().vector_degraded = (
                f"embedding encode failed: {exc}"[:200]
            )
            return np.array([], dtype=np.float32)
        self.vector_model_down = False
        self._store_query_embedding(query, vec)
        return vec

    def _normalize_backend_names(self, backend_names: list) -> list:
        """Dedup (order-preserving), cap length, and reject unknown members."""
        seen: set = set()
        deduped: list = []
        for raw in backend_names:
            name = str(raw)
            if name not in self._KNOWN_BACKENDS:
                raise ValueError(
                    f"unknown backend {name!r}; known backends: "
                    f"{list(self._KNOWN_BACKENDS)}"
                )
            if name not in seen:
                seen.add(name)
                deduped.append(name)
        if len(deduped) > self._MAX_BACKENDS:
            raise ValueError(
                f"too many backends ({len(deduped)}); max {self._MAX_BACKENDS}"
            )
        return deduped

    def _resolve_backends(self, backend_names: list) -> list:
        """
        Resolve a list of backend name strings to VectorBackend instances.

        Currently supports: "faiss-disk", "faiss-mem".
        Stubs ("qdrant", "lance") raise ImportError at construction.

        R9: the input list is deduped, capped, and validated first; an unknown
        or over-long member is rejected loudly.
        """
        resolved = []
        for name in self._normalize_backend_names(backend_names):
            if name == "faiss-disk":
                from minni.backends.faiss_disk import FaissDiskBackend
                b = FaissDiskBackend(self.config, self.db)
                resolved.append(b)
            elif name == "faiss-mem":
                from minni.backends.faiss_mem import FaissMemBackend
                b = FaissMemBackend(self.config)
                resolved.append(b)
            elif name == "qdrant":
                from minni.backends.qdrant import QdrantBackend
                b = QdrantBackend(self.config)  # raises ImportError if not installed
                resolved.append(b)
            elif name == "lance":
                from minni.backends.lance import LanceBackend
                b = LanceBackend(self.config)  # raises ImportError if not installed
                resolved.append(b)
        return resolved

    def _hits_to_dicts(self, hits, query_emb: np.ndarray) -> List[Dict]:
        """
        Convert VectorHit list to the dict format used by _rrf_merge.

        Fetches chunk metadata from SQLite to fill path, agent, etc.
        """
        from minni.vector_backend import VectorHit
        if not hits:
            return []

        chunk_ids = [h.chunk_id for h in hits]
        score_map = {h.chunk_id: (h.score, h.backend) for h in hits}
        doc_best: Dict[int, Dict] = {}

        with self.db.cursor() as c:
            placeholders = ",".join("?" * len(chunk_ids))
            c.execute(f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id IN ({placeholders})
            """, chunk_ids)

            for row in c.fetchall():
                cid = row["chunk_id"]
                did = row["doc_id"]
                sim, bname = score_map.get(cid, (0.0, "unknown"))

                if did not in doc_best or sim > doc_best[did]["similarity"]:
                    doc_best[did] = {
                        "doc_id": did,
                        "chunk_id": cid,
                        "path": row["path"],
                        "agent": row["agent"],
                        "sigil": row["sigil"],
                        "similarity": sim,
                        "chunk_text": row["chunk_text"],
                        "heading_context": row["heading_context"] or "",
                        "decay_score": row["decay_score"] or 1.0,
                        "page_status": row["page_status"] or "candidate",
                        "privacy_level": row["privacy_level"] or "safe",
                        "page_type": row["page_type"],
                        "evidence_refs": row["evidence_refs"],
                        "indexed_at": row["indexed_at"],
                        "layer": row["layer"] or "knowledge",
                        "backend_name": bname,
                    }

        return sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)

    def _backend_search(
        self,
        query_emb: np.ndarray,
        limit: int,
        backend,
    ) -> List[Dict]:
        """
        Search using an explicit VectorBackend instance (PR-3 multi-backend path).

        Returns a list of dicts in the same format as _semantic_search(),
        with the 'backend_name' field populated from hit.backend.
        """
        # Review round 3 (PR #260): an empty vector means the encoder is down —
        # _encode_query already raised the P0-B flag. Feeding it to a live
        # index raises a dimension mismatch, turning the degrade into a -32000
        # error while the default `backend: auto` path degrades to FTS-only
        # cleanly. Same outage, same outcome: skip the semantic leg.
        if query_emb.size == 0:
            return []
        search_k = limit * 5
        hits = backend.search(query_emb, k=search_k, filter=None)

        if not hits:
            return []

        chunk_ids = [h.chunk_id for h in hits]
        score_map = {h.chunk_id: (h.score, h.backend) for h in hits}

        doc_best: Dict[int, Dict] = {}

        with self.db.cursor() as c:
            placeholders = ",".join("?" * len(chunk_ids))
            c.execute(f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id IN ({placeholders})
            """, chunk_ids)

            for row in c.fetchall():
                cid = row["chunk_id"]
                did = row["doc_id"]
                sim, backend_name = score_map.get(cid, (0.0, "unknown"))

                if did not in doc_best or sim > doc_best[did]["similarity"]:
                    doc_best[did] = {
                        "doc_id": did,
                        "chunk_id": cid,
                        "path": row["path"],
                        "agent": row["agent"],
                        "sigil": row["sigil"],
                        "similarity": sim,
                        "chunk_text": row["chunk_text"],
                        "heading_context": row["heading_context"] or "",
                        "decay_score": row["decay_score"] or 1.0,
                        "page_status": row["page_status"] or "candidate",
                        "privacy_level": row["privacy_level"] or "safe",
                        "page_type": row["page_type"],
                        "evidence_refs": row["evidence_refs"],
                        "indexed_at": row["indexed_at"],
                        "layer": row["layer"] or "knowledge",
                        "backend_name": backend_name,
                    }

        results = sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)
        return results[:limit * 3]

    def _rrf_merge_multi(
        self,
        fts_results: List[Dict],
        semantic_results: List[Dict],
        extra_backend_results: List[List[Dict]],
        limit: int,
    ) -> List[Dict]:
        """
        RRF merge: FTS + semantic + Nth backend stream(s).

        Each input is a ranked list. Additional backends each contribute
        with weight = semantic_weight (same as the primary semantic stream).
        """
        k = self.config.rrf_k
        doc_scores: Dict[int, Dict] = {}

        def _add_stream(ranked_list, weight, stream_label):
            for rank, r in enumerate(ranked_list, start=1):
                did = r["doc_id"]
                if did not in doc_scores:
                    doc_scores[did] = {
                        "doc_id": did,
                        "path": r.get("path", ""),
                        "agent": r.get("agent", ""),
                        "sigil": r.get("sigil", ""),
                        "rrf_score": 0.0,
                        "decay_score": r.get("decay_score", 1.0),
                        "fts_rank": None,
                        "sem_rank": None,
                        "chunk_text": r.get("chunk_text", ""),
                        "heading_context": r.get("heading_context", ""),
                        "page_status": r.get("page_status", "candidate"),
                        "privacy_level": r.get("privacy_level", "safe"),
                        "page_type": r.get("page_type"),
                        "evidence_refs": r.get("evidence_refs"),
                        "indexed_at": r.get("indexed_at"),
                        "layer": r.get("layer", "knowledge"),
                        "backend_name": r.get("backend_name", "faiss-disk"),
                    }
                doc_scores[did]["rrf_score"] += weight / (k + rank)
                if stream_label == "fts":
                    doc_scores[did]["fts_rank"] = rank
                elif stream_label == "sem" and doc_scores[did]["sem_rank"] is None:
                    doc_scores[did]["sem_rank"] = rank
                # Semantic streams overwrite FTS full-page content (the FTS
                # body is only a fallback for unembedded rows); the first
                # semantic stream to land keeps its chunk.
                if r.get("chunk_text") and not doc_scores[did].get("_sem_chunk"):
                    doc_scores[did]["chunk_text"] = r["chunk_text"]
                    doc_scores[did]["heading_context"] = r.get("heading_context", "")
                    if stream_label != "fts":
                        doc_scores[did]["_sem_chunk"] = True
                        if r.get("chunk_id") is not None:
                            doc_scores[did]["chunk_id"] = r["chunk_id"]
                if not doc_scores[did].get("page_type") and r.get("page_type"):
                    doc_scores[did]["page_type"] = r["page_type"]
                if not doc_scores[did].get("evidence_refs") and r.get("evidence_refs"):
                    doc_scores[did]["evidence_refs"] = r["evidence_refs"]
                if not doc_scores[did].get("indexed_at") and r.get("indexed_at"):
                    doc_scores[did]["indexed_at"] = r["indexed_at"]
                if not doc_scores[did].get("layer") and r.get("layer"):
                    doc_scores[did]["layer"] = r["layer"]

        _add_stream(fts_results, self.config.fts_weight, "fts")
        _add_stream(semantic_results, self.config.semantic_weight, "sem")
        for extra in extra_backend_results:
            _add_stream(extra, self.config.semantic_weight, "extra")

        for d in doc_scores.values():
            d.pop("_sem_chunk", None)
            self._score_merged_doc(d)

        ranked = sorted(doc_scores.values(), key=lambda x: x["final_score"], reverse=True)
        return ranked[:limit]

    def _normalize_layers(self, layers: Optional[Sequence[str]]) -> Optional[set]:
        """None → no filter. grok-review round 5 (finding 2): a bare string is
        ONE layer, not an iterable of its characters — iterating "episodic"
        produced an empty set, and empty sets fell open to an unscoped search.
        The RPC edge coerces strings too, but the function that owns the
        contract must not depend on every caller remembering to. An explicit
        filter that normalizes to zero valid layers stays an EMPTY set;
        filtering callers fail closed on it (match nothing), because a request
        that asked for a scope must never silently get the unscoped corpus."""
        if layers is None:
            return None
        if isinstance(layers, str):
            layers = [layers]
        valid = {"identity", "episodic", "knowledge", "artifact"}
        return {str(layer).lower() for layer in layers if str(layer).lower() in valid}

    def _parse_iso_date(self, value: Optional[str], end_of_day: bool = False) -> Optional[float]:
        if not value:
            return None
        from datetime import datetime, time as dt_time
        text = str(value)
        try:
            if len(text) == 10:
                day = datetime.fromisoformat(text)
                if end_of_day:
                    day = datetime.combine(day.date(), dt_time.max)
                return day.timestamp()
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            logger.warning("Ignoring invalid ISO date filter: %r", value)
            return None

    def _filter_candidates(
        self,
        candidates: List[Dict],
        layers: Optional[Sequence[str]],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> List[Dict]:
        layer_set = self._normalize_layers(layers)
        start_ts = self._parse_iso_date(start_date)
        end_ts = self._parse_iso_date(end_date, end_of_day=True)
        # `is None`, not falsy: an explicit filter with zero valid layers must
        # fail CLOSED (empty set → no candidate matches), not fall open.
        if layer_set is None and start_ts is None and end_ts is None:
            return candidates
        filtered = []
        for r in candidates:
            if layer_set is not None and (r.get("layer") or "knowledge") not in layer_set:
                continue
            if start_ts is None and end_ts is None:
                filtered.append(r)
                continue
            # Audit R0: a single row whose indexed_at was stored as TEXT used to
            # raise ValueError out of float() here, propagate through
            # handle_search, and abort the ENTIRE recall with -32000. Parse or
            # skip that one row instead — reported, never silently swallowed.
            #
            # grok-review (PR #242): the previous `r.get("created_at") or
            # r.get("indexed_at") or 0` picked created_at whenever truthy,
            # including a non-numeric TEXT value — shadowing a usable
            # indexed_at on the same candidate and dropping the row under a
            # date filter it would otherwise have passed. Parse created_at
            # first; only fall back to indexed_at if that parse genuinely
            # fails (same priority as the health.py stale_docs fix). If
            # neither is usable the row is still skipped (not defaulted to an
            # invented epoch) — unchanged from the prior behavior for a
            # candidate with no usable timestamp at all.
            created_at = parse_epoch_or_report(
                r.get("created_at"), field="created_at",
                source="retrieval._filter_candidates", doc_id=r.get("doc_id"),
            )
            if created_at is None:
                created_at = parse_epoch_or_report(
                    r.get("indexed_at"), field="indexed_at",
                    source="retrieval._filter_candidates", doc_id=r.get("doc_id"),
                )
            if created_at is None and r.get("created_at") is None and r.get("indexed_at") is None:
                created_at = 0.0
            if created_at is None:
                continue
            if start_ts is not None and created_at < start_ts:
                continue
            if end_ts is not None and created_at > end_ts:
                continue
            filtered.append(r)
        return filtered

    def _chronological_search(
        self,
        query: str,
        limit: int,
        layers: Optional[Sequence[str]],
        start_date: Optional[str],
        end_date: Optional[str],
        exclude_statuses: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return []
        layer_set = self._normalize_layers(layers)
        start_ts = self._parse_iso_date(start_date)
        end_ts = self._parse_iso_date(end_date, end_of_day=True)
        filter_params: list = []
        clauses = ["vault_fts MATCH ?"]
        if layer_set is not None and not layer_set:
            # Explicit filter, zero valid layers — fail closed, not unscoped.
            return []
        if layer_set:
            placeholders = ",".join("?" * len(layer_set))
            clauses.append(f"COALESCE(ce.layer, d.layer, 'knowledge') IN ({placeholders})")
            filter_params.extend(sorted(layer_set))
        if start_ts is not None:
            clauses.append("COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) >= ?")
            filter_params.append(start_ts)
        if end_ts is not None:
            clauses.append("COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) <= ?")
            filter_params.append(end_ts)
        # Same rule as _fts_search, for the same reason: the LIMIT below is a
        # fixed window, and dropping a lifecycle state after it has been spent
        # cannot recover the rows that never got fetched. This path needs it
        # MORE than the others -- it orders by age ascending over a vault whose
        # oldest pages are precisely the expired backlog, so an unfiltered
        # window is drafts almost by construction.
        skip = [str(s) for s in (exclude_statuses or [])]
        if skip:
            clauses.append(
                "COALESCE(d.page_status, 'candidate') NOT IN "
                f"({','.join('?' * len(skip))})"
            )
            filter_params.extend(skip)

        with self.db.cursor() as c:
            def _match(match_expr: str):
                # Fetch happens inside the retry helper (review r3): the
                # vtable race can also fire while stepping the SELECT.
                # LEFT JOIN, not JOIN: unendorsed pages are deliberately not
                # embedded (see indexer.UNEMBEDDED_STATUSES), and an inner join
                # made them unreachable by chronological recall even when the
                # caller passed include_drafts=True. The lifecycle filter, not
                # the presence of a vector, decides what comes back. Prefer a
                # real chunk (first by chunk_index); unembedded pages fall back
                # to the FTS row's content, body-only after the strip below.
                return _fts_execute_with_retry(c, f"""
                    SELECT ce.chunk_id, d.doc_id,
                           {_FTS_CHUNK_TEXT_EXPR} AS chunk_text,
                           ce.heading_context,
                           d.path, d.agent, d.sigil, d.decay_score,
                           d.page_status, d.privacy_level, d.page_type,
                           d.evidence_refs, d.indexed_at,
                           COALESCE(ce.layer, d.layer, 'knowledge') AS layer,
                           COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) AS created_at
                    FROM vault_fts f
                    JOIN documents d ON d.doc_id = f.doc_id
                    LEFT JOIN chunk_embeddings ce ON ce.doc_id = d.doc_id
                    WHERE {" AND ".join(clauses)}
                    ORDER BY created_at ASC, ce.chunk_id ASC
                    LIMIT ?
                """, [match_expr, *filter_params, limit])

            # P0-C parity (review r2): dated/specific chronological recalls hit
            # this path instead of _fts_search, so they need the SAME zero-hit
            # AND→OR degradation — space-joined FTS5 terms are implicit AND and
            # a query like "checkpoint 2026-07-18 plan-…" rarely has ALL its
            # tokens in one chunk. Operands lowercased for the same reason as
            # _fts_search: a literal "OR"/"AND"/"NOT" token must not be parsed
            # as an FTS5 operator.
            rows = _match(safe_query)
            terms = safe_query.split()
            if not rows and len(terms) > 1:
                rows = _match(" OR ".join(t.lower() for t in terms))

        return [
            {
                "doc_id": row["doc_id"],
                "chunk_id": row["chunk_id"],
                "path": row["path"],
                "agent": row["agent"],
                "sigil": row["sigil"],
                "final_score": 0.0,
                "rrf_score": None,
                "fts_rank": None,
                "sem_rank": None,
                "chunk_text": _strip_leading_frontmatter(row["chunk_text"] or ""),
                "heading_context": row["heading_context"] or "",
                "decay_score": row["decay_score"] or 1.0,
                "page_status": row["page_status"] or "candidate",
                "privacy_level": row["privacy_level"] or "safe",
                "page_type": row["page_type"],
                "evidence_refs": row["evidence_refs"],
                "indexed_at": row["indexed_at"],
                "created_at": row["created_at"],
                "layer": row["layer"] or "knowledge",
            }
            for row in rows
        ]

    def _resolve_query_variants(self, query: str, expand) -> List[str]:
        """Resolve the public expand flag to concrete query variants."""
        if expand is False or expand is None:
            return [query]
        mode = getattr(self.config, "query_expand_default", "rule") or "rule"
        if isinstance(expand, str):
            mode = expand
            if mode.lower() in {"false", "off", "none", "0"}:
                return [query]
        elif expand is True and str(mode).lower() not in {"rule", "afm"}:
            mode = "rule"
        try:
            # Prefer expand_with_status so mode=afm soft-fail (empty AFM, rule
            # fallback) is visible on the search envelope — not only hard
            # exceptions. HyDE sets last_hyde_degraded = "afm_unavailable" on
            # the analogous miss; query expand was still silent on that path.
            variants, degraded = expand_query_with_status(
                query, mode=str(mode).lower()
            )
            if degraded:
                self._current_state().query_expand_degraded = degraded
        except Exception as exc:  # noqa: BLE001 - recall must not raise here
            logger.warning("Query expansion failed: %s — using original query", exc)
            # AFM-6 (#230): the log line alone is not reachable from the call
            # site. Recorded so the response can say the search ran on the bare
            # query rather than presenting it as an expanded one.
            self._current_state().query_expand_degraded = str(exc)
            variants = [query]
        return variants or [query]

    def _chunk_index_empty(self) -> bool:
        """True when there are no vectors for semantic retrieval.

        A clean Minni home should answer recall with an empty result set after
        the cheap FTS pass, without cold-loading embedding or reranker models.
        Public CI exercises that path via scripts/repro-smoke.sh.
        """
        try:
            with self.db.cursor() as c:
                has_chunks = c.execute(
                    "SELECT 1 FROM chunk_embeddings LIMIT 1"
                ).fetchone()
            return has_chunks is None
        except Exception as exc:  # noqa: BLE001 - recall should degrade, not fail.
            logger.debug("empty chunk-index probe failed: %s", exc)
            return False

    def _merge_expanded_results(
        self,
        variant_results: List[List[Dict]],
        query_variants: List[str],
        limit: int,
    ) -> List[Dict]:
        """Merge formatted per-variant result lists with RRF by doc_id."""
        k = self.config.rrf_k
        merged: Dict[int, Dict] = {}
        for variant, results in zip(query_variants, variant_results):
            for rank, result in enumerate(results, start=1):
                doc_id = result.get("doc_id")
                if doc_id is None:
                    continue
                if doc_id not in merged:
                    merged[doc_id] = dict(result)
                    merged[doc_id]["matched_query_variants"] = []
                    merged[doc_id]["expansion_rrf_score"] = 0.0
                merged[doc_id]["expansion_rrf_score"] += 1.0 / (k + rank)
                merged[doc_id]["matched_query_variants"].append(variant)

        ranked = sorted(
            merged.values(),
            key=lambda r: (r.get("expansion_rrf_score", 0.0), r.get("score", 0.0)),
            reverse=True,
        )[:limit]
        for result in ranked:
            result["query_variants"] = query_variants
            result["score"] = round(float(result.get("expansion_rrf_score", 0.0)), 4)
            provenance = result.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
                result["provenance"] = provenance
            provenance["expansion_rrf_score"] = result.get("expansion_rrf_score", 0.0)
            provenance["matched_query_variants"] = result.get("matched_query_variants", [])
        return ranked

    def _extract_wiki_links(self, text: str) -> List[str]:
        links = []
        for raw in re.findall(r"\[\[([^\]]+)\]\]", text or ""):
            target = raw.split("|", 1)[0].strip()
            if target and target not in links:
                links.append(target)
        return links

    @staticmethod
    def _wikilink_has_like_metachar(candidate: str) -> bool:
        """R4: reject wikilink candidates carrying SQL LIKE metacharacters.

        A '[[%]]' or '[[_]]' wikilink would otherwise match every (or arbitrary)
        document path via the LIKE predicate, so any candidate containing an
        unescaped '%' or '_' is treated as untrusted and skipped rather than
        allowed to fan out across the whole documents table.
        """
        return "%" in candidate or "_" in candidate

    def _fetch_linked_context(
        self,
        links: List[str],
        limit: int = 8,
        *,
        principal: Optional[EffectivePrincipal] = None,
        workspace: str = "default",
    ) -> List[Dict]:
        if not links:
            return []
        contexts = []
        ws = workspace or getattr(principal, "workspace_id", "default") or "default"
        with self.db.cursor() as c:
            for link in links[:limit]:
                stem = link[:-3] if link.endswith(".md") else link
                basename = stem.split("/")[-1]
                row = None
                for candidate in (stem, f"{stem}.md", basename, f"{basename}.md"):
                    # R4: '%'/'_' are LIKE metacharacters; a wikilink containing
                    # them (e.g. '[[%]]') must not fan out across all docs.
                    if self._wikilink_has_like_metachar(candidate):
                        continue
                    c.execute(
                        """SELECT d.doc_id, d.path, d.agent, d.privacy_level,
                                  d.page_type, d.page_status,
                                  ce.chunk_text
                           FROM documents d
                           JOIN chunk_embeddings ce ON ce.doc_id = d.doc_id
                           WHERE d.path LIKE ?
                           ORDER BY ce.chunk_id
                           LIMIT 1""",
                        (f"%{candidate}",),
                    )
                    row = c.fetchone()
                    if row is not None:
                        break
                if row is not None:
                    # R4 / Finding 10: neighborhood summaries are a read surface —
                    # require a principal (fail closed) and gate every linked doc
                    # through can_read_document + the same lifecycle exclusions
                    # retrieve uses (draft/rejected/superseded/expired).
                    if principal is None:
                        continue
                    raw = {
                        "doc_id": row["doc_id"],
                        "path": row["path"],
                        "agent": row["agent"],
                        "privacy_level": row["privacy_level"],
                        "page_type": row["page_type"],
                        "page_status": row["page_status"],
                    }
                    status = (row["page_status"] or "candidate").lower()
                    if status in {"draft", "rejected", "superseded", "expired"}:
                        continue
                    if not can_read_document(principal, ws, raw):
                        continue
                    contexts.append({
                        "link": link,
                        "doc_id": row["doc_id"],
                        "path": row["path"],
                        "text": row["chunk_text"][:700],
                    })
        return contexts

    def _add_neighborhood_summaries(
        self,
        results: List[Dict],
        *,
        principal: Optional[EffectivePrincipal] = None,
        workspace: str = "default",
        deadline_monotonic: Optional[float] = None,
    ) -> List[Dict]:
        """Attach AFM summaries of 1-hop wikilinks; degrade to metadata."""
        for result in results:
            text = result.get("text") or result.get("full_document_text") or ""
            if not text and result.get("doc_id") is not None:
                text = self._fetch_full_document(result["doc_id"]) or ""
            links = self._extract_wiki_links(text)
            if not links and result.get("doc_id") is not None:
                text = self._fetch_full_document(result["doc_id"]) or ""
                links = self._extract_wiki_links(text)
            if not links:
                continue
            contexts = self._fetch_linked_context(
                links, principal=principal, workspace=workspace
            )
            prompt = "\n\n".join(
                f"{ctx['link']} ({ctx['path']}):\n{ctx['text']}" for ctx in contexts
            )
            context_meta = [
                {"link": ctx["link"], "doc_id": ctx["doc_id"], "path": ctx["path"]}
                for ctx in contexts
            ]
            # AFM summarize timeout is 1.5s; skip remaining neighbors if
            # the leftover budget cannot finish even one call.
            if contexts and past_search_deadline(
                deadline_monotonic, min_remaining=1.5
            ):
                result["neighborhood_summary"] = {
                    "status": "unavailable",
                    "summary": None,
                    "links": links,
                    "contexts": context_meta,
                }
                break
            summary = summarize_with_afm(prompt) if contexts else None
            result["neighborhood_summary"] = {
                "status": "ok" if summary else "unavailable",
                "summary": summary,
                "links": links,
                "contexts": context_meta,
            }
        return results

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        agent_id: Optional[str] = None,
        update_access: bool = True,
        budget_tokens: bool = True,
        depth: str = "snippet",
        include_superseded: bool = False,
        include_rejected: bool = False,
        include_drafts: bool = False,
        include_expired: bool = False,
        backend=None,
        layers: Optional[Sequence[str]] = None,
        sort: Literal["semantic", "chronological"] = "semantic",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        expand=True,
        summarize_neighborhood: bool = False,
        use_hyde: Optional[bool] = None,
        cross_agent: bool = False,
        claim: Optional[str] = None,
        document_agent_filter: Optional[Sequence[str]] = None,
        # G19/G20/G22: principal for can_read_document gate + evidence envelope (default None = back-compat)
        principal: Optional[EffectivePrincipal] = None,
        workspace: str = "default",
        deadline_monotonic: Optional[float] = None,
        # perf/parallel-fanout (#388, YELLOW-3a): private per-call state for
        # variant pool workers. Public callers never pass it. The signature
        # is explicit — NOT *args — because G20 introspects it; it mirrors
        # _retrieve_body plus this one private _state kwarg (it is not
        # literally identical to _retrieve_body, which takes no _state).
        _state: Optional[RetrievalCallState] = None,
    ) -> List[Dict]:
        """Hybrid retrieval (public entry; signature-compatible wrapper).

        All parameters are forwarded to :meth:`_retrieve_body` unchanged —
        existing positional/keyword callers (including the ``search()`` alias)
        are unaffected. ``_state`` is private: variant pool workers pass their
        own RetrievalCallState so concurrent same-engine calls stay isolated
        (perf/parallel-fanout, #388). A top-level call creates one state,
        pushes it for the duration, and publishes it to the legacy
        thread-local read surface on return (including the exception path,
        where the partial state matches the serial incremental writes).
        """
        outer = _state is None
        state = _state if _state is not None else RetrievalCallState()
        self._push_call_state(state)
        self._set_current_deadline(deadline_monotonic)
        try:
            return self._retrieve_body(
                query=query,
                limit=limit,
                agent_id=agent_id,
                update_access=update_access,
                budget_tokens=budget_tokens,
                depth=depth,
                include_superseded=include_superseded,
                include_rejected=include_rejected,
                include_drafts=include_drafts,
                include_expired=include_expired,
                backend=backend,
                layers=layers,
                sort=sort,
                start_date=start_date,
                end_date=end_date,
                expand=expand,
                summarize_neighborhood=summarize_neighborhood,
                use_hyde=use_hyde,
                cross_agent=cross_agent,
                claim=claim,
                document_agent_filter=document_agent_filter,
                principal=principal,
                workspace=workspace,
                deadline_monotonic=deadline_monotonic,
            )
        finally:
            self._pop_call_state()
            if outer:
                self._publish_call_state(state)

    def _retrieve_body(
        self,
        query: str,
        limit: int = 5,
        agent_id: Optional[str] = None,
        update_access: bool = True,
        budget_tokens: bool = True,
        depth: str = "snippet",
        include_superseded: bool = False,
        include_rejected: bool = False,
        include_drafts: bool = False,
        include_expired: bool = False,
        backend=None,
        layers: Optional[Sequence[str]] = None,
        sort: Literal["semantic", "chronological"] = "semantic",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        expand=True,
        summarize_neighborhood: bool = False,
        use_hyde: Optional[bool] = None,
        cross_agent: bool = False,
        claim: Optional[str] = None,
        document_agent_filter: Optional[Sequence[str]] = None,
        # G19/G20/G22: principal for can_read_document gate + evidence envelope (default None = back-compat)
        principal: Optional[EffectivePrincipal] = None,
        workspace: str = "default",
        deadline_monotonic: Optional[float] = None,
    ) -> List[Dict]:
        """
        Hybrid retrieval: FTS5 + FAISS semantic, RRF fusion, cross-encoder re-rank,
        context budgeting, with progressive disclosure depth tiers.

        Pipeline:
        1. FTS5 keyword search → top candidates
        2. FAISS semantic search → top candidates
        3. RRF fusion → merged rankings
        4. Cross-encoder re-rank → precision refinement
        5. Context budgeting → fit within token limit
        6. Depth-tier field filtering → progressive disclosure

        Args:
            query: Search query string.
            limit: Maximum results to return (default 5, max 20).
            agent_id: If set, filter results to this agent's documents.
            update_access: Whether to bump access_count in the DB.
            budget_tokens: Whether to apply context window budgeting.
            depth: Progressive disclosure tier.
                   "headline" — minimal fields (~30 tokens/result)
                   "snippet"  — + text (≤280 chars) (~120 tokens) [DEFAULT]
                   "chunk"    — + full chunk text, heading, provenance (~500 tokens)
                   "document" — + full source document (whole_document=1 only)
            backend: PR-3 backend override.
                   None (default) — use internal FAISSIndex (bit-identical to pre-PR-3)
                   VectorBackend  — use this backend for semantic search
                   list           — fan-out via MultiBackend, merge with RRF
            expand: False disables query expansion. True uses config.query_expand_default
                    (default "rule"). "rule" and "afm" select explicit modes.
            summarize_neighborhood: If true, summarize 1-hop wikilinks when AFM is available.
            use_hyde: Optional PR-8 override. None follows config.hyde_enabled.
            cross_agent: Learning-recall flag threaded from daemon handlers. It
                does not scope the shared document layer.
            claim: Optional claim that retrieved evidence should support. When
                supplied, Minni runs local NLI attribution scoring.
            document_agent_filter: Optional explicit document agent taxonomy
                filter for future callers. Default None preserves shared wiki recall.
            deadline_monotonic: Optional time.monotonic() cutoff. When elapsed,
                skip query expand, FAISS/encode, rerank, and HyDE; return FTS
                hits and set last_*_degraded. Search cannot cancel to_thread.

        Returns list of ranked results filtered to the requested depth tier.
        Existing callers that pass no depth receive identical results (snippet).
        With backend=None (default), results are bit-identical to pre-PR-3.
        """
        claim_text = str(claim or "").strip()
        # R5 (#226) / AFM-6 (#230): cleared per-retrieve so a stale flag from an
        # earlier call never reports this one as degraded. The multi-variant
        # branch below RECURSES, and each recursive call clears these again on
        # entry — so a degrade in an early variant would be wiped by a later
        # healthy one. Aggregated explicitly after the merge, exactly as
        # variant_suppressions already does. (Review round 1 on PR #260: an
        # earlier comment here claimed no re-clearing happened. It did.)
        # Per-call state (#388): this body always runs under a pushed state
        # (retrieve() pushed it), so `state` below is that call's own verdict
        # object — never a sibling call's, however the legs interleave.
        state = self._current_state()
        state.rerank_degraded = None
        state.query_expand_degraded = None
        state.vector_degraded = None
        state.hyde_degraded = None
        state.document_hydration_degraded = None
        self._reset_encode_ms()
        self._set_current_deadline(deadline_monotonic)
        if past_search_deadline(deadline_monotonic):
            query_variants = [query]
            if expand not in (False, None, "off"):
                state.query_expand_degraded = "search deadline; skipped query expand"
        else:
            query_variants = self._resolve_query_variants(query, expand)
        # Round 25: expand soft-fail (mode=afm → afm_unavailable) is set on the
        # parent by _resolve_query_variants *before* multi-variant recursion.
        # Children run with expand=False and clear flags on entry, so the
        # parent signal must be captured here or the merge wipes AFM-6 honesty
        # exactly when rule fallback yields ≥2 variants (the common case).
        parent_expand_degraded = state.query_expand_degraded
        if len(query_variants) > 1:
            total_t0 = time.perf_counter()
            # Review r1 (P2): each recursive single-variant call below rewrites
            # the per-call suppression, so without accumulation the P0-A
            # diagnostic only survives when the SUPPRESSING variant happens to
            # run last. Collect per-variant suppressions and re-aggregate after
            # the merge.
            variant_suppressions: List[Dict] = []
            # Same aggregation, same reason: the recursion clears these on entry.
            variant_rerank_degraded: List[str] = []
            variant_expand_degraded: List[str] = []
            variant_vector_degraded: List[str] = []
            variant_hyde_degraded: List[str] = []
            variant_document_hydration_degraded: List[str] = []
            # perf/parallel-fanout (#388): one isolated state per variant,
            # gathered in submission order (pool.map preserves it), so the
            # merge and every aggregation string below are deterministic and
            # identical to the serial loop. A variant that raises propagates
            # exactly as the serial append did (merge/access-bump/trace all
            # skipped, error surfaces to the caller).
            variant_states = [RetrievalCallState() for _ in query_variants]
            truncated_expand = None

            def _run_variant(index: int) -> List[Dict]:
                return self.retrieve(
                    query=query_variants[index],
                    limit=limit,
                    agent_id=agent_id,
                    update_access=False,
                    budget_tokens=budget_tokens,
                    depth=depth,
                    include_superseded=include_superseded,
                    include_rejected=include_rejected,
                    include_drafts=include_drafts,
                    include_expired=include_expired,
                    backend=backend,
                    layers=layers,
                    sort=sort,
                    start_date=start_date,
                    end_date=end_date,
                    expand=False,
                    summarize_neighborhood=False,
                    use_hyde=use_hyde,
                    cross_agent=cross_agent,
                    claim=claim,
                    document_agent_filter=document_agent_filter,
                    principal=principal,
                    workspace=workspace,
                    deadline_monotonic=deadline_monotonic,
                    _state=variant_states[index],
                )

            def _child_deadline_poisoned(child: RetrievalCallState) -> bool:
                return any(
                    flag and "search deadline" in str(flag).lower()
                    for flag in (child.vector_degraded, child.rerank_degraded)
                )

            def _record_kept_child(variant: str, child: RetrievalCallState) -> None:
                if child.auth_suppression:
                    variant_suppressions.append(
                        {**child.auth_suppression, "variant": variant}
                    )
                if child.rerank_degraded:
                    variant_rerank_degraded.append(
                        f"{variant}: {child.rerank_degraded}"
                    )
                if child.query_expand_degraded:
                    variant_expand_degraded.append(
                        f"{variant}: {child.query_expand_degraded}"
                    )
                if child.vector_degraded:
                    variant_vector_degraded.append(
                        f"{variant}: {child.vector_degraded}"
                    )
                if child.hyde_degraded:
                    variant_hyde_degraded.append(
                        f"{variant}: {child.hyde_degraded}"
                    )
                if child.document_hydration_degraded:
                    variant_document_hydration_degraded.append(
                        f"{variant}: {child.document_hydration_degraded}"
                    )

            def _should_drop_deadline_child(
                child: RetrievalCallState, kept_rows: List[List[Dict]]
            ) -> bool:
                # In-flight later retrieve can FTS-only/CE-skip after the
                # loop gate passed. Drop it before RRF-by-doc_id so it cannot
                # wipe a completed first-pass hybrid (HyDE banana-pudding class).
                # Gate on a prior ranking, not list-of-lists nonempty: a miss
                # is per_variant == [[]] and must keep the degraded later fill.
                prior_ranking = any(kept_rows)
                prior_deadline_poisoned = any(
                    "search deadline" in str(flag).lower()
                    for flag in (
                        *variant_vector_degraded,
                        *variant_rerank_degraded,
                    )
                )
                return (
                    prior_ranking
                    and _child_deadline_poisoned(child)
                    and not prior_deadline_poisoned
                )

            per_variant: List[List[Dict]] = []
            ran_variants: List[str] = []
            # A supplied deadline must keep origin/main's serial truncation:
            # later variants are not started once a ranking exists and the
            # clock is past. Eager pool.map submits every variant, so a
            # later unused child that raises aborts the whole retrieve
            # (P1) and FTS-counts 3 where serial did 1.
            # Preservation, not a latency fix: handle_search stamps
            # deadline_monotonic on EVERY RPC, so this pool is unreachable
            # on the RPC path by construction — no speedup is claimed or
            # measured there. The serial truncation order it preserves is
            # pinned by tests/test_search_deadline.py (loop-gate
            # truncation, in-flight poisoned-child drop, qty withholding).
            use_variant_pool = (
                RETRIEVAL_VARIANT_PARALLEL and deadline_monotonic is None
            )
            if use_variant_pool:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(_MAX_VARIANT_WORKERS, len(query_variants)),
                    thread_name_prefix="minni-variant",
                ) as _variant_pool:
                    # YELLOW-1/YELLOW-2 (documented, deliberate): a raising
                    # variant's partial verdicts die with its
                    # RetrievalCallState — the parent publishes its own
                    # entry-cleared state plus the pre-recursion
                    # parent_expand_degraded (the merge that would aggregate
                    # the children never runs). Serially the raising
                    # variant's incremental thread-local writes stayed
                    # visible. The delta is confined to the failure path
                    # (success-path aggregates are identical — see the parity
                    # tests). pool.map submits ALL variants eagerly, but the
                    # fate of a not-yet-started sibling when the gather
                    # aborts is a scheduling RACE, not a guarantee either
                    # way: abandoning the map iterator (as list() does on the
                    # first raise) cancels pending futures via the iterator's
                    # finally clause, while a worker that wins the race
                    # starts the sibling first and it then runs to completion
                    # (cancel only stops futures that never started). Probed
                    # on 3.14 both ways — immediate-raise cancels ~always, a
                    # GIL yield in the body lets the worker win — and pinned
                    # by test_variant_abort_envelope_equal_despite_race. The
                    # residue is observable below the RPC envelope whenever a
                    # sibling actually ran: variant bodies are read-only on
                    # documents (update_access=False) but DO leave trace-ring
                    # entries the serial run never wrote. The deterministic
                    # contract is RPC-envelope equality: pool.map yields in
                    # submission order, so the FIRST variant's exception is
                    # the one that propagates — exactly the serial raise, in
                    # every trial under either race outcome. as_completed +
                    # explicit cancel was rejected: it could surface a LATER
                    # variant's error first, breaking the serial-raise
                    # identity for zero gain (variant bodies are not
                    # cancellable work).
                    # Independent copy_context per variant: same absolute
                    # request deadline, never one Context entered from two
                    # workers. Serial-when-deadline (above) still skips the
                    # pool when deadline_monotonic is set.
                    bound = [
                        bind_copied_deadline(_run_variant, index)
                        for index in range(len(query_variants))
                    ]
                    raw_rows = list(_variant_pool.map(run_bound, bound))
                for variant, rows, child in zip(
                    query_variants, raw_rows, variant_states
                ):
                    if _should_drop_deadline_child(child, per_variant):
                        truncated_expand = (
                            "search deadline; truncated query expand"
                        )
                        break
                    per_variant.append(rows)
                    ran_variants.append(variant)
                    _record_kept_child(variant, child)
            else:
                for index, variant in enumerate(query_variants):
                    # Gate on a completed ranking, not list-of-lists nonempty.
                    # After an original-query miss, per_variant == [[]] is
                    # truthy but any(per_variant) is False — variant 2 must
                    # still start so cheap FTS after the deadline can fill.
                    if any(per_variant) and past_search_deadline(
                        deadline_monotonic
                    ):
                        truncated_expand = (
                            "search deadline; truncated query expand"
                        )
                        break
                    try:
                        rows = _run_variant(index)
                    except RequestDeadlineExceeded:
                        truncated_expand = (
                            "search deadline; truncated query expand"
                        )
                        break
                    child = variant_states[index]
                    if _should_drop_deadline_child(child, per_variant):
                        truncated_expand = (
                            "search deadline; truncated query expand"
                        )
                        break
                    per_variant.append(rows)
                    ran_variants.append(variant)
                    _record_kept_child(variant, child)
            results = self._merge_expanded_results(per_variant, ran_variants, limit)
            # Only variants that contributed to the merge stamp ranking-poison
            # flags. A dropped deadline variant must not sticky-join
            # last_vector_degraded onto a completed first-pass hybrid.
            # Set AFTER the loop, because the last variant's clear would
            # otherwise decide the whole verdict.
            state.rerank_degraded = (
                "; ".join(variant_rerank_degraded) if variant_rerank_degraded else None
            )
            child_expand = (
                "; ".join(variant_expand_degraded) if variant_expand_degraded else None
            )
            if parent_expand_degraded and child_expand:
                merged_expand = f"{parent_expand_degraded}; {child_expand}"
            else:
                merged_expand = parent_expand_degraded or child_expand
            # Truncation is neither parent (captured before the loop) nor child
            # (children run expand=False). Keep it or the merge wipes the only
            # signal that expand did not finish.
            if truncated_expand:
                if merged_expand:
                    state.query_expand_degraded = (
                        f"{merged_expand}; {truncated_expand}"
                    )
                else:
                    state.query_expand_degraded = truncated_expand
            else:
                state.query_expand_degraded = merged_expand
            state.vector_degraded = (
                "; ".join(variant_vector_degraded) if variant_vector_degraded else None
            )
            state.hyde_degraded = (
                "; ".join(variant_hyde_degraded) if variant_hyde_degraded else None
            )
            state.document_hydration_degraded = (
                "; ".join(variant_document_hydration_degraded)
                if variant_document_hydration_degraded
                else None
            )
            # Aggregate: any variant whose non-empty candidate set was gated to
            # zero keeps the blackout visible, regardless of variant order.
            # (recall.py only surfaces it when the merged result is empty.)
            if variant_suppressions:
                total_pre = sum(s.get("pre_gate", 0) for s in variant_suppressions)
                state.auth_suppression = {
                    "pre_gate": total_pre,
                    "suppressed": total_pre,
                    "reason": "; ".join(
                        str(s.get("reason", "")) for s in variant_suppressions
                    ),
                    "variants": [s.get("variant") for s in variant_suppressions],
                }
            else:
                state.auth_suppression = None
            if summarize_neighborhood and not past_search_deadline(
                deadline_monotonic
            ):
                results = self._add_neighborhood_summaries(
                    results,
                    principal=principal,
                    workspace=workspace,
                    deadline_monotonic=deadline_monotonic,
                )
            if update_access and not self._deadline_skipped_vector():
                with self.db.cursor() as c:
                    for result in results:
                        if result.get("doc_id") is None:
                            continue
                        c.execute(
                            """UPDATE documents
                               SET access_count = access_count + 1, last_accessed = ?
                               WHERE doc_id = ?""",
                            (time.time(), result["doc_id"]),
                        )
            try:
                expanded_trace = {
                    "query": query,
                    "variants": ran_variants,
                    "expansion": {"mode": expand, "variant_count": len(ran_variants)},
                    "final_ordering": [
                        {
                            "doc_id": r.get("doc_id"),
                            "score": r.get("score"),
                            "matched_query_variants": r.get("matched_query_variants", []),
                        }
                        for r in results
                    ],
                    "timing": {
                        "total_ms": round((time.perf_counter() - total_t0) * 1000, 3),
                    },
                }
                claim_text = str(claim or "").strip()
                if claim_text:
                    expanded_trace["claim"] = claim_text
                    expanded_trace["attribution_scores"] = [
                        {
                            "doc_id": r.get("doc_id"),
                            "chunk_id": r.get("chunk_id"),
                            "attribution": r.get("attribution"),
                            "score": r.get("attribution_score"),
                        }
                        for r in results
                        if r.get("attribution_score") is not None
                    ]
                # RED-1: stamped from the local into this call's own state
                # (the last_trace_id write below routes into the pushed
                # state, never a sibling call's) and onto these rows. Same
                # value serially; race-free in parallel.
                expanded_trace_id = _trace_ring().add(
                    expanded_trace, owner=getattr(principal, "agent_id", None)
                )
                self.last_trace_id = expanded_trace_id
                for result in results:
                    result["trace_id"] = expanded_trace_id
            except Exception as exc:
                logger.debug("expanded trace capture failed: %s", exc)
                self.last_trace_id = None
            return results

        total_t0 = time.perf_counter()
        timing = {
            "fts_ms": 0.0,
            "embedding_ms": 0.0,
            "semantic_ms": 0.0,
            "ce_ms": 0.0,
            "total_ms": 0.0,
        }
        trace = {
            "query": query,
            "variants": query_variants,
            "fts_hits": [],
            "semantic_hits": [],
            "rrf": {},
            "cross_encoder_scores": [],
            "decay_factors": [],
            "final_ordering": [],
            "hyde": {"triggered": False},
            "backends": [],
            "timing": timing,
        }

        if sort not in ("semantic", "chronological"):
            logger.warning("Unknown sort=%r, falling back to 'semantic'", sort)
            sort = "semantic"

        # Lifecycle exclusions are decided HERE, before any candidate window is
        # filled, and reused by every leg below. They used to be computed only
        # after merge + rerank + truncate, which meant states the caller had
        # already opted out of could occupy the FTS LIMIT, survive into the RRF
        # pool, consume final slots, and only then be dropped — returning fewer
        # than `limit` usable results. The late filter is still applied as
        # defense in depth (and for privacy), but it is no longer the only gate.
        skip_statuses = set()
        if not include_superseded:
            skip_statuses.add("superseded")
        if not include_rejected:
            skip_statuses.add("rejected")
        if not include_drafts:
            skip_statuses.add("draft")
        # expired is terminal (same bucket as rejected in _recommended_action)
        # and gets its own flag: piggy-backing on include_drafts meant that
        # once expiry actually ran, a draft-review call drowned in the months-
        # old expired backlog — chronological order is ascending by age, so
        # the backlog fills the window before any active draft.
        if not include_expired:
            skip_statuses.add("expired")
        # #229 M6: statuses that are valid but deliberately non-recallable
        # (a completed plan). The excluded page keeps its documents row so
        # wikilinks and doc_id references survive, so recall — not the
        # indexer — is what must keep it out of results. No include_* flag:
        # unlike superseded/rejected/draft/expired there is no review surface
        # that wants them back.
        skip_statuses.update(WikiFrontmatter.EXCLUDED_STATUSES)
        skip_list = sorted(skip_statuses)

        ws = (workspace or getattr(principal, "workspace_id", "default")) if principal else (workspace or "default")
        denied_by_scope: dict[tuple, Dict] = {}
        saw_authorized = False

        def _eligible(rows: List[Dict]) -> List[Dict]:
            # Before fusion/reranking/truncation, enforce the same read gate
            # as the final defense, plus lifecycle and caller filters.
            nonlocal saw_authorized
            rows = [r for r in rows if
                    (r.get("page_status") or "candidate") not in skip_statuses
                    and (r.get("privacy_level") or "safe") != "blocked"]
            rows = self._filter_candidates(rows, layers, start_date, end_date)
            agent_scope = self._normalize_agent_filter(document_agent_filter)
            if agent_scope:
                rows = [r for r in rows if r.get("agent") in agent_scope]
            if principal is None:
                return rows
            allowed = []
            for row in rows:
                if can_read_document(principal, ws, row):
                    saw_authorized = True
                    allowed.append(row)
                else:
                    denied_by_scope[(row.get("doc_id"), row.get("path"))] = row
            return allowed

        def _collect_eligible(fetch, wanted: int, *, sql_window: bool = False) -> List[Dict]:
            # Backends expose top-k, not policy-aware pagination. Refill their
            # bounded window when denied rows consume it, stopping when enough
            # eligible rows are found, at the deadline, or at a finite ceiling.
            # At that ceiling a trace records incomplete candidate coverage;
            # no result is admitted merely to fill the requested count.
            window = max(1, wanted)
            ceiling = max(window, 512)
            last_rows: List[Dict] = []
            with self._query_encoding_scope():
                while True:
                    try:
                        raw = fetch(window)
                        rows = _eligible(raw)
                    except RequestDeadlineExceeded:
                        if not last_rows:
                            raise
                        return last_rows
                    last_rows = rows
                    if len(rows) >= wanted or len(rows) == len(raw):
                        return rows
                    # SQL returns rows without vector-style document collapse.
                    # A short window therefore proves exhaustion. FTS currently
                    # over-fetches 3x; using the requested window is conservative
                    # and keeps this independent of that over-fetch factor.
                    if sql_window and len(raw) < window:
                        return rows
                    # Identical document lists do not prove exhaustion: many
                    # top-ranked chunks can collapse to one denied document.
                    if window >= ceiling or past_search_deadline(deadline_monotonic):
                        trace["candidate_window_exhausted"] = True
                        return rows
                    window = min(ceiling, window * 2)

        rerank_k = max(limit, self.config.reranker_top_k if self.config.reranker_enabled else limit)

        lexical_searched = False

        def _lexical_eligible(fetch):
            """Ranking-deadline lexical fill: short FTS/chrono may still run.

            Entry uses allow_expired_sql; the VM progress handler stays on
            so a recursive CTE cannot run unbounded after expiry.
            """
            nonlocal lexical_searched

            def tracked_fetch(window):
                nonlocal lexical_searched
                rows = fetch(window)
                lexical_searched = True
                return rows

            if past_search_deadline(deadline_monotonic):
                with allow_expired_sql():
                    return _collect_eligible(
                        tracked_fetch, rerank_k, sql_window=True
                    )
            return _collect_eligible(tracked_fetch, rerank_k, sql_window=True)

        if sort == "chronological":
            if past_search_deadline(deadline_monotonic):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
            chrono_t0 = time.perf_counter()
            try:
                merged = _lexical_eligible(lambda window: self._chronological_search(
                    query, window, layers, start_date, end_date,
                    exclude_statuses=skip_list,
                ))
            except RequestDeadlineExceeded:
                if not lexical_searched:
                    raise
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                merged = []
            timing["semantic_ms"] = round((time.perf_counter() - chrono_t0) * 1000, 3)
            trace["backends"] = ["chronological-sql"]
            merged = merged[:limit]
        else:
            # Step 1-2: Dual retrieval
            fts_t0 = time.perf_counter()
            try:
                if document_agent_filter is None:
                    fts_results = _lexical_eligible(lambda window: self._fts_search(
                        query, window, exclude_statuses=skip_list
                    ))
                else:
                    fts_results = _lexical_eligible(lambda window: self._fts_search(
                        query, window, agent_filter=document_agent_filter,
                        exclude_statuses=skip_list,
                    ))
            except RequestDeadlineExceeded:
                if not lexical_searched:
                    raise
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                fts_results = []
            timing["fts_ms"] = round((time.perf_counter() - fts_t0) * 1000, 3)
            trace["fts_hits"] = [
                {
                    "doc_id": r.get("doc_id"),
                    "bm25": r.get("bm25_rank"),
                    "rank": idx,
                    "path": r.get("path"),
                }
                for idx, r in enumerate(fts_results, start=1)
            ]

            # PR-3: When backend is provided, use it instead of (or in addition to)
            # the internal FAISSIndex.  With backend=None (default), the existing
            # _semantic_search path is taken — bit-identical to pre-PR-3.
            extra_backend_results: List[List[Dict]] = []
            semantic_t0 = time.perf_counter()
            if past_search_deadline(deadline_monotonic):
                semantic_results = []
                trace["backends"] = ["fts-deadline"]
                if lexical_searched:
                    self.last_vector_degraded = "search deadline; lexical (FTS) only"
            elif backend is None and not fts_results and self._chunk_index_empty():
                semantic_results = []
                trace["backends"] = ["faiss-disk-empty"]
            elif backend is None:
                # Default path — bit-identical to pre-PR-3
                # Gate taxonomy after fetching: filtering the SQL lookup of
                # a bounded FAISS window would hide why it needs refilling.
                try:
                    semantic_results = _collect_eligible(
                        lambda window: self._semantic_search(query, window), rerank_k,
                    )
                except RequestDeadlineExceeded:
                    semantic_results = []
                    if lexical_searched:
                        self.last_vector_degraded = "search deadline; lexical (FTS) only"
                trace["backends"] = ["faiss-disk"]
            elif isinstance(backend, list):
                # Fan-out: build a MultiBackend from the list of backend names/objects
                from minni.backends.multi import MultiBackend
                resolved = self._resolve_backends(backend)
                if len(resolved) == 1:
                    query_emb = self._encode_query(query)
                    semantic_results = _collect_eligible(lambda window: self._backend_search(
                        query_emb, window, resolved[0],
                    ), rerank_k)
                else:
                    multi = MultiBackend(resolved)
                    query_emb = self._encode_query(query)
                    # Same encoder-down guard as _backend_search (round 3,
                    # PR #260): degrade to FTS-only instead of raising.
                    semantic_results = _collect_eligible(lambda window: self._hits_to_dicts(
                        [] if query_emb.size == 0 else multi.search(query_emb, k=window),
                        query_emb,
                    ), rerank_k)
                trace["backends"] = [getattr(b, "name", str(b)) for b in resolved]
            else:
                # Single explicit backend object
                query_emb = self._encode_query(query)
                semantic_results = _collect_eligible(lambda window: self._backend_search(
                    query_emb, window, backend,
                ), rerank_k)
                trace["backends"] = [getattr(backend, "name", "custom")]
            timing["semantic_ms"] = round((time.perf_counter() - semantic_t0) * 1000, 3)
            trace["semantic_hits"] = [
                {
                    "doc_id": r.get("doc_id"),
                    "chunk_id": r.get("chunk_id"),
                    "cosine": r.get("similarity"),
                    "rank": idx,
                    "backend": r.get("backend_name", "faiss-disk"),
                }
                for idx, r in enumerate(semantic_results, start=1)
            ]

            # Drop excluded lifecycle states on BOTH legs before they can win
            # slots in the fusion pool. FTS is already filtered in SQL; the
            # vector legs are filtered here, since a chunk embedded before its
            # page changed status can still be returned by FAISS.
            semantic_results = _eligible(semantic_results)
            extra_backend_results = [_eligible(rows) for rows in extra_backend_results]

            # Step 3: RRF merge — with extra streams if multi-backend
            if extra_backend_results:
                merged = self._rrf_merge_multi(
                    fts_results, semantic_results, extra_backend_results, rerank_k
                )
            else:
                merged = self._rrf_merge(fts_results, semantic_results, rerank_k)
            trace["rrf"] = {
                "k": self.config.rrf_k,
                "fts_weight": self.config.fts_weight,
                "semantic_weight": self.config.semantic_weight,
                "merged": [
                    {
                        "doc_id": r.get("doc_id"),
                        "fts_rank": r.get("fts_rank"),
                        "semantic_rank": r.get("sem_rank"),
                        "rrf_score": r.get("rrf_score"),
                        "final_score": r.get("final_score"),
                    }
                    for r in merged
                ],
            }

            merged = _eligible(merged)

            # Step 4: Cross-encoder re-rank.
            # Deadline before `self.reranker`: that property lazily calls
            # get_cross_encoder() / CrossEncoder() and can outlive the client.
            if merged and self.config.reranker_enabled:
                if past_search_deadline(deadline_monotonic):
                    self.last_rerank_degraded = "search deadline; skipped rerank"
                    merged = merged[:limit]
                elif self.reranker:
                    ce_t0 = time.perf_counter()
                    merged = self._rerank(query, merged)
                    timing["ce_ms"] = round((time.perf_counter() - ce_t0) * 1000, 3)
                    trace["cross_encoder_scores"] = [
                        {"doc_id": r.get("doc_id"), "score": r.get("rerank_score")}
                        for r in merged
                    ]
                    # S-1 fix: respect the caller's limit. reranker_final_k is a
                    # precision-tuning floor (controls how many cross-encoder scores
                    # we pay for), NOT a hard recall cap. When limit > final_k
                    # (e.g. limit=10, final_k=5) the old code structurally capped
                    # recall@10 at 0.5 — we keep max(final_k, limit) so that
                    # limit=5 callers see no behaviour change and limit=10 callers
                    # get up to 10 post-rerank results.
                    merged = merged[:max(self.config.reranker_final_k, limit)]
                else:
                    merged = merged[:limit]
            else:
                merged = merged[:limit]

            # PR-8: HyDE cold-query second pass. This runs at most once and
            # gracefully returns the original pass if AFM is unavailable.
            hyde_enabled = self.config.hyde_enabled if use_hyde is None else bool(use_hyde)
            if hyde_enabled and past_search_deadline(deadline_monotonic):
                self.last_hyde_degraded = "search deadline; skipped hyde"
                hyde_enabled = False
            if hyde_enabled:
                try:
                    from minni.hyde import (
                        generate_hypothetical_answer,
                        merge_hyde_results,
                        should_trigger_hyde,
                    )
                    from minni.scoring import compute_confidence

                    probe_results = []
                    for r in merged[:limit]:
                        probe = dict(r)
                        probe["confidence"] = compute_confidence(
                            rrf_score=r.get("rrf_score"),
                            # grok-review round 2: the raw logit — rerank_score
                            # is already decay-attenuated and decay_factor is
                            # passed separately below.
                            cross_encoder_score=r.get(
                                "raw_rerank_score", r.get("rerank_score")
                            ),
                            # round 5 (finding 3): the effective decay ranking
                            # used (correction floor + clamp), not the raw one.
                            decay_factor=r.get(
                                "decay_applied", r.get("decay_score")
                            ),
                            # round 6 (finding 2): raw blend ONLY. Calibrating
                            # against self.db meant shared-window activation
                            # silently retuned when HyDE fires (the floor is
                            # tuned for raw blends) while vault engines kept
                            # comparing raw — a speculative trigger must not
                            # depend on calibration semantics, same rule as
                            # record.
                            db=None,
                        )
                        probe_results.append(probe)

                    if should_trigger_hyde(
                        probe_results,
                        enabled=True,
                        floor=self.config.hyde_confidence_floor,
                    ):
                        trace["hyde"]["triggered"] = True
                        trace["hyde"]["confidence_floor"] = self.config.hyde_confidence_floor
                        if past_search_deadline(deadline_monotonic):
                            self.last_hyde_degraded = "search deadline; skipped hyde"
                            trace["hyde"]["completed"] = False
                            trace["hyde"]["skipped"] = "deadline"
                        elif past_search_deadline(
                            deadline_monotonic, min_remaining=2.0
                        ):
                            # AFM's default timeout is 2s; skip rather than
                            # start a call that cannot finish in-budget.
                            self.last_hyde_degraded = "search deadline; skipped hyde"
                            trace["hyde"]["completed"] = False
                            trace["hyde"]["skipped"] = "deadline"
                        else:
                            hypothetical = generate_hypothetical_answer(
                                query, config=self.config
                            )
                            if not hypothetical:
                                # AFM-6 (#230): the leg was attempted and produced
                                # nothing. `triggered` above records the DECISION to
                                # run; `completed` records whether it actually did.
                                # Reading `triggered` alone told anyone debugging a
                                # bad result that the enrichment ran when it had not.
                                trace["hyde"]["completed"] = False
                                trace["hyde"]["skipped"] = "afm_unavailable"
                                self.last_hyde_degraded = "afm_unavailable"
                                logger.warning(
                                    "HyDE leg degraded: AFM produced no hypothetical "
                                    "answer — results are the un-enriched first pass"
                                )
                            elif past_search_deadline(deadline_monotonic):
                                self.last_hyde_degraded = "search deadline; skipped hyde"
                                trace["hyde"]["completed"] = False
                                trace["hyde"]["skipped"] = "deadline"
                            else:
                                trace["hyde"]["hypothetical_chars"] = len(hypothetical)
                                if document_agent_filter is None:
                                    hyde_fts = _collect_eligible(lambda window: self._fts_search(
                                        hypothetical, window, exclude_statuses=skip_list
                                    ), rerank_k, sql_window=True)
                                else:
                                    hyde_fts = _collect_eligible(lambda window: self._fts_search(
                                        hypothetical, window, agent_filter=document_agent_filter,
                                        exclude_statuses=skip_list,
                                    ), rerank_k, sql_window=True)
                                first_pass_vector = self.last_vector_degraded
                                first_pass_rerank = self.last_rerank_degraded
                                hyde_apply = True
                                hyde_merged_applied = False
                                try:
                                    if past_search_deadline(deadline_monotonic):
                                        hyde_semantic = []
                                        self.last_hyde_degraded = (
                                            "search deadline; skipped hyde"
                                        )
                                        trace["hyde"]["completed"] = False
                                        trace["hyde"]["skipped"] = "deadline"
                                        hyde_apply = False
                                    else:
                                        hyde_semantic = _collect_eligible(lambda window: self._semantic_search(
                                            hypothetical, window
                                        ), rerank_k)
                                    vector_flag = self.last_vector_degraded
                                    if hyde_apply and vector_flag and (
                                        "search deadline" in str(vector_flag).lower()
                                    ):
                                        self.last_hyde_degraded = (
                                            "search deadline; skipped hyde"
                                        )
                                        trace["hyde"]["completed"] = False
                                        trace["hyde"]["skipped"] = "deadline"
                                        hyde_apply = False
                                    if hyde_apply:
                                        hyde_semantic = _eligible(hyde_semantic)
                                        hyde_merged = self._rrf_merge(
                                            hyde_fts, hyde_semantic, rerank_k
                                        )
                                        hyde_merged = _eligible(hyde_merged)
                                        if self.config.reranker_enabled:
                                            # Deadline before `self.reranker`: that
                                            # property lazily calls get_cross_encoder()
                                            # and can outlive the client.
                                            if past_search_deadline(deadline_monotonic):
                                                self.last_hyde_degraded = (
                                                    "search deadline; skipped hyde"
                                                )
                                                trace["hyde"]["completed"] = False
                                                trace["hyde"]["skipped"] = "deadline"
                                                hyde_apply = False
                                            elif self.reranker:
                                                hyde_merged = self._rerank(
                                                    query, hyde_merged
                                                )
                                                rerank_flag = self.last_rerank_degraded
                                                if rerank_flag and (
                                                    "search deadline"
                                                    in str(rerank_flag).lower()
                                                ):
                                                    self.last_hyde_degraded = (
                                                        "search deadline; skipped hyde"
                                                    )
                                                    trace["hyde"]["completed"] = False
                                                    trace["hyde"]["skipped"] = "deadline"
                                                    hyde_apply = False
                                                else:
                                                    # S-1 fix (HyDE branch): same max()
                                                    # guard as the main rerank path —
                                                    # limit must not be capped below the
                                                    # caller's requested count.
                                                    hyde_merged = hyde_merged[:max(
                                                        self.config.reranker_final_k, limit
                                                    )]
                                            else:
                                                hyde_merged = hyde_merged[:limit]
                                        else:
                                            hyde_merged = hyde_merged[:limit]
                                    if hyde_apply and (
                                        past_search_deadline(deadline_monotonic)
                                        or self._deadline_skipped_vector()
                                    ):
                                        hyde_apply = False
                                        self.last_hyde_degraded = (
                                            "search deadline; skipped hyde"
                                        )
                                        trace["hyde"]["completed"] = False
                                        trace["hyde"]["skipped"] = "deadline"
                                    if hyde_apply:
                                        merged = merge_hyde_results(
                                            merged,
                                            hyde_merged,
                                            limit=rerank_k,
                                            rrf_k=self.config.rrf_k,
                                        )[:limit]
                                        hyde_merged_applied = True
                                        trace["hyde"]["result_doc_ids"] = [
                                            r.get("doc_id") for r in hyde_merged
                                        ]
                                finally:
                                    if not hyde_merged_applied:
                                        self.last_vector_degraded = first_pass_vector
                                        self.last_rerank_degraded = first_pass_rerank
                        trace["hyde"].setdefault("completed", True)
                except Exception as exc:  # noqa: BLE001 - recall must not stack trace.
                    # Was DEBUG — invisible at normal log levels, so a HyDE leg
                    # that failed on every query left no trace anyone would see.
                    logger.warning("HyDE leg failed after retrieval pass: %s", exc)
                    trace["hyde"]["completed"] = False
                    trace["hyde"]["skipped"] = "error"
                    trace["hyde"]["error"] = str(exc)
                    self._current_state().hyde_degraded = str(exc)[:400]

        # G19 gate (below, after status filter) is the single source of truth for visibility.
        # The prior ad-hoc "agent_id or unknown" filter is removed; legacy principal=None
        # paths get only the status/privacy filter (pre-G19 back-compat shape preserved for
        # callers that never passed principal).

        merged = self._apply_feedback_demotions(merged, query, agent_id)

        # PR-2: Status lifecycle filtering
        # default: skip superseded, rejected, draft, expired
        # callers can opt back in with include_* kwargs
        _ALWAYS_EXCLUDED = {"blocked"}  # privacy_level=blocked is always excluded
        # Same set the legs were filtered with above (computed once, near the
        # top of retrieve). Kept here as defense in depth and because privacy
        # exclusion has always been enforced at this point.
        _SKIP_STATUSES = skip_statuses

        if _SKIP_STATUSES or _ALWAYS_EXCLUDED:
            filtered = []
            for r in merged:
                status = r.get("page_status") or "candidate"
                privacy = r.get("privacy_level") or "safe"
                if status in _SKIP_STATUSES:
                    continue
                if privacy in _ALWAYS_EXCLUDED:
                    continue
                filtered.append(r)
            merged = filtered

        # G19/G20: ws always defined (hoisted) so G22 envelope loop and legacy principal=None
        # paths never hit UnboundLocalError. Gate only when principal supplied.
        ws = workspace or getattr(principal, "workspace_id", "default") if principal is not None else (workspace or "default")
        self._current_state().auth_suppression = None
        if principal is not None:
            merged, suppression = self.apply_read_gate(principal, ws, merged)
            self.last_auth_suppression = suppression
            if not merged and denied_by_scope and not saw_authorized:
                _, self.last_auth_suppression = self.apply_read_gate(
                    principal, ws, list(denied_by_scope.values())
                )

        # G22: attach evidence-only envelope + instruction_like + provenance/reasoning to every result
        # Model-facing content is always wrapped; raw executable instructions never treated as policy.
        for r in merged:
            txt = str(r.get("chunk_text") or r.get("full_document_text") or r.get("content") or "")
            r["instruction_like"] = bool(is_instruction_like(txt))
            attribution = (
                self._score_attribution(claim_text, txt)
                if claim_text and not past_search_deadline(deadline_monotonic)
                else None
            )
            if attribution is not None:
                r.update(attribution)
            vis = "authorized"
            pid = getattr(principal, "agent_id", None) if principal else None
            if r.get("agent") == pid:
                vis = "same-agent"
            elif str(r.get("page_type", "")).lower() in {"wiki", "handoff", "synthesis"}:
                vis = "shared-wiki-authorized"
            r["visibility"] = vis
            r["reasoning"] = f"can_read_document(principal={pid or 'n/a'}, ws={ws}) passed"
            # Safe wrapper: attribute + body escaping lives in
            # build_evidence_envelope (SEC-010 — untrusted paths/snippets must
            # not be able to forge attributes or a second EVIDENCE tag).
            src = r.get("path") or r.get("source") or r.get("filename") or "?"
            ag = r.get("agent", "?")
            st = r.get("page_status", "?")
            pr = r.get("privacy_level", "?")
            sc = float(r.get("score") or 0)
            r["evidence_envelope"] = build_evidence_envelope(
                source=src,
                agent=ag,
                status=st,
                privacy=pr,
                score=sc,
                instruction_like=r["instruction_like"],
                visibility=vis,
                text=txt,
                attribution=r.get("attribution"),
                perturbation_enabled=getattr(
                    self.config, "instruction_body_perturbation_enabled", True
                ),
            )
            if r["instruction_like"] or r.get("attribution_score") is not None:
                existing_full = r.get("full_provenance")
                if not isinstance(existing_full, dict):
                    existing_full = {}
                r["full_provenance"] = {
                    **existing_full,
                }
                # Deliberately NOT storing the raw unperturbed body here: recall
                # results are stringified into model-facing context, so shipping
                # the raw text would defeat the instruction-like perturbation.
                # The perturbation is reversible by construction — audit/display
                # can recover the original via _recover_instruction_like_body().
                if r.get("attribution_score") is not None:
                    r["full_provenance"].update({
                        "attribution": r.get("attribution"),
                        "attribution_score": r.get("attribution_score"),
                        "attribution_model": r.get("attribution_model"),
                    })
            # Primary text field becomes the envelope so downstream (sovrd JSON, agent_api, formatRecall) sees tagged evidence
            if "chunk_text" in r:
                r["chunk_text"] = r["evidence_envelope"]

        # Step 5: Context budgeting
        if budget_tokens:
            merged = self._budget_results(merged, query)

        # Validate depth tier; default to snippet (zero change for existing callers)
        if depth not in _VALID_DEPTHS:
            logger.warning("Unknown depth=%r, falling back to 'snippet'", depth)
            depth = "snippet"

        # Format output and update access counts
        import os
        from minni.scoring import compute_confidence
        from minni.rationale import explain

        results = []
        for r in merged:
            # Build a rich intermediate dict with all raw fields available.
            # _apply_depth will project it down to the requested tier.
            score = round(
                r.get(
                    "feedback_adjusted_score",
                    r.get("rerank_score", r.get("final_score", 0)),
                ),
                4,
            )

            # Compute age_days from indexed_at. Audit R0: parse-or-report, so a
            # TEXT indexed_at costs this one row its age instead of raising
            # ValueError and killing the whole formatted result set.
            indexed_at = parse_epoch_or_report(
                r.get("indexed_at"),
                field="indexed_at",
                source="retrieval._format_results",
                doc_id=r.get("doc_id"),
            )
            age_days: Optional[float] = None
            # grok-review (PR #242): `if indexed_at:` treats the migration's
            # own 0.0 sentinel (a deliberately visible "needs attention"
            # marker for unparseable rows) as "no timestamp" because 0.0 is
            # falsy — so a repaired-to-sentinel row loses its (very large,
            # very visible) age instead of showing it. `is not None` keeps 0.0.
            if indexed_at is not None:
                age_days = round((time.time() - indexed_at) / 86400.0, 1)

            # Compute confidence
            try:
                from minni.scoring import raw_confidence

                # grok-review round 2: raw logit, not the decay-attenuated
                # rerank_score — decay_factor below already applies decay
                # once; the attenuated value would apply it twice and bias
                # the calibration window low for aged docs.
                ce_for_confidence = r.get(
                    "raw_rerank_score", r.get("rerank_score")
                )
                # round 5 (finding 3): the effective decay ranking used
                # (correction floor + clamp), not the raw decay_score — the
                # legs must not disagree about how fresh a correction is.
                decay_for_confidence = r.get(
                    "decay_applied", r.get("decay_score")
                )
                confidence = compute_confidence(
                    rrf_score=r.get("rrf_score"),
                    cross_encoder_score=ce_for_confidence,
                    decay_factor=decay_for_confidence,
                    # GA4-1 / grok-review rounds 4-5 (finding 1): formatting
                    # neither records NOR calibrates. Production search is
                    # multi-call AND multi-engine — scope=both fans out
                    # personal (vault db) + combined (vaults + shared) — so
                    # per-engine calibration made one response mix percentile
                    # ranks (shared window) with raw blends (vault windows,
                    # never fed), and per-call recording padded the window
                    # with duplicate/discarded scores. The RPC boundary
                    # (recall.handle_search) records confidence_raw into the
                    # SHARED window and rewrites confidence onto that single
                    # basis for the whole payload.
                    db=None,
                    record=False,
                )
                confidence_raw = raw_confidence(
                    r.get("rrf_score"),
                    ce_for_confidence,
                    decay_for_confidence,
                )
            except Exception:
                confidence = None
                confidence_raw = None

            # Detect injection in chunk text
            chunk_text = r.get("chunk_text", "")
            instr_like = r.get("instruction_like")
            if instr_like is None:
                try:
                    instr_like = is_instruction_like(chunk_text)
                except Exception:
                    instr_like = None

            # Infer page type → source_authority mapping
            page_type = r.get("page_type")
            source_authority = _page_type_to_authority(page_type, r.get("agent", ""))

            # Wikilink from path
            path = r.get("path", "")
            rel_path = path  # full path; callers can relativize if needed
            wikilink = _path_to_wikilink(path)

            # Evidence refs (stored as JSON list or comma string)
            evidence_refs = _parse_evidence_refs(r.get("evidence_refs"))

            # Page status for envelope
            page_status = r.get("page_status") or "candidate"
            privacy_level = r.get("privacy_level") or "safe"

            # Build provenance dict
            provenance = {
                "fts_rank": r.get("fts_rank"),
                "semantic_rank": r.get("sem_rank"),
                "rrf_score": r.get("rrf_score"),
                # Raw logit: provenance also exposes decay_factor, so a
                # consumer re-blending the two must not get a pre-decayed score.
                "cross_encoder_score": r.get(
                    "raw_rerank_score", r.get("rerank_score")
                ),
                # round 6 (finding 3): the EFFECTIVE decay ranking and
                # confidence used (correction floor + clamp) — reporting the
                # raw 0.01 while every leg ranked on 0.5 made provenance lie
                # for correction-class docs. The raw column stays available as
                # decay_score on the full-depth envelope.
                "decay_factor": _effective_decay(r),
                "agent_origin": r.get("agent", ""),
                "age_days": age_days,
                "doc_id": r["doc_id"],
                "chunk_id": r.get("chunk_id"),
                "backend": "faiss-disk",
            }
            if "feedback_demote" in r:
                provenance["feedback_demote"] = r.get("feedback_demote", 0.0)
            if r.get("salience_boost"):
                # recall-F3: correction-class salience applied in RRF scoring
                provenance["salience_boost"] = r.get("salience_boost", 0.0)
            if r.get("provenance", {}).get("via_hyde"):
                provenance["via_hyde"] = True
            if r.get("attribution_score") is not None:
                provenance["attribution"] = r.get("attribution")
                provenance["attribution_score"] = r.get("attribution_score")
                provenance["attribution_model"] = r.get("attribution_model")

            raw = {
                "doc_id": r["doc_id"],
                "chunk_id": r.get("chunk_id"),
                "path": path,
                "source": r.get("path", ""),
                "filename": os.path.basename(path),
                "agent": r["agent"],
                "sigil": r["sigil"],
                "score": score,
                "fts_rank": r.get("fts_rank"),
                "sem_rank": r.get("sem_rank"),
                "rrf_score": r.get("rrf_score"),
                "rerank_score": r.get("rerank_score"),
                "decay_score": r.get("decay_score"),
                # round 6 (finding 3): carry the effective decay so
                # _apply_depth's headline decay_factor reports what the legs
                # actually used, not the raw column.
                "decay_applied": r.get("decay_applied"),
                "layer": r.get("layer", "knowledge"),
                "chunk_text": chunk_text,
                "heading_context": r.get("heading_context", ""),
                "token_count": r.get("token_count", 0),
                # PR-2 envelope fields
                "confidence": confidence,
                # Pre-calibration raw blend for the RPC boundary to record
                # (and pop) over the final merged result set — see the GA4-1
                # note on compute_confidence above.
                "confidence_raw": confidence_raw,
                "age_days": age_days,
                "provenance": provenance,
                "privacy_level": privacy_level,
                "source_authority": source_authority,
                "review_state": page_status,
                "instruction_like": instr_like,
                "wikilink": wikilink,
                "evidence_refs": evidence_refs,
                "recommended_action": _recommended_action(page_status, instr_like, confidence),
                "recommended_wiki_updates": [],
                "attribution": r.get("attribution"),
                "attribution_score": r.get("attribution_score"),
                "attribution_model": r.get("attribution_model"),
            }
            if r.get("full_provenance") is not None:
                raw["full_provenance"] = r.get("full_provenance")

            # Rationale is computed after provenance is assembled
            try:
                raw["rationale"] = explain(raw)
            except Exception:
                raw["rationale"] = None

            # For document depth, attach full document text if available.
            # Detection/attribution ran on the chunk above; the whole document is
            # what actually ships at this depth, so re-check the flag and re-score
            # the claim against it, and wrap it in an envelope — the raw body must
            # never ride outside the perturbed <EVIDENCE> form (same leak class as
            # chunk_text).
            if depth == "document":
                hydration_degraded = False
                try:
                    full_text = self._fetch_full_document(r["doc_id"])
                except RequestDeadlineExceeded:
                    full_text = None
                    hydration_degraded = True
                    self._stamp_document_hydration_degraded(raw)
                if full_text:
                    doc_flag = bool(raw.get("instruction_like")) or bool(
                        is_instruction_like(full_text)
                    )
                    raw["instruction_like"] = doc_flag
                    # recommended_action was computed from the chunk-level flag
                    # above — recompute so routing metadata matches the flag the
                    # full-document recheck just set (escalate, not cite).
                    raw["recommended_action"] = _recommended_action(
                        raw.get("review_state"), doc_flag, raw.get("confidence")
                    )
                    if claim_text and not past_search_deadline(deadline_monotonic):
                        attribution = self._score_attribution(claim_text, full_text)
                        if attribution is not None:
                            raw.update(attribution)
                            # Keep every attribution surface consistent with the
                            # full-document rescore: the merged row feeds the
                            # trace ring below, and provenance/full_provenance
                            # were assembled from the first-chunk score above.
                            r.update(attribution)
                            for meta in (
                                raw.get("provenance"),
                                raw.get("full_provenance"),
                            ):
                                if isinstance(meta, dict):
                                    meta.update({
                                        "attribution": raw.get("attribution"),
                                        "attribution_score": raw.get("attribution_score"),
                                        "attribution_model": raw.get("attribution_model"),
                                    })
                    raw["full_document_text"] = build_evidence_envelope(
                        source=raw.get("source", "?"),
                        agent=raw.get("agent", "?"),
                        status=raw.get("review_state", "?"),
                        privacy=raw.get("privacy_level", "?"),
                        score=float(raw.get("score") or 0),
                        instruction_like=doc_flag,
                        visibility=r.get("visibility", "authorized"),
                        text=full_text,
                        attribution=raw.get("attribution"),
                        perturbation_enabled=getattr(
                            self.config, "instruction_body_perturbation_enabled", True
                        ),
                    )
                elif not hydration_degraded:
                    raw["full_document_text"] = full_text

            # S7: self-labeling recall package — primary (rank 1) vs related (2..N).
            # Rank is 1-based by position in the final results list (post-rerank order).
            _result_rank = len(results) + 1
            raw["match_kind"] = "primary" if _result_rank == 1 else "related"
            raw["related_rank"] = None if _result_rank == 1 else _result_rank - 1

            projected = self._project_depth(raw, depth)
            projected["match_kind"] = raw["match_kind"]
            projected["related_rank"] = raw["related_rank"]
            projected["query_variants"] = query_variants
            results.append(projected)

            if update_access and not self._deadline_skipped_vector():
                with self.db.cursor() as c:
                    c.execute(
                        """UPDATE documents
                           SET access_count = access_count + 1, last_accessed = ?
                           WHERE doc_id = ?""",
                        (time.time(), r["doc_id"]),
                    )

        trace["decay_factors"] = [
            {"doc_id": r.get("doc_id"), "decay_factor": r.get("decay_score")}
            for r in merged
        ]
        trace["final_ordering"] = [
            {
                "doc_id": r.get("doc_id"),
                "chunk_id": r.get("chunk_id"),
                "score": r.get("rerank_score", r.get("final_score", 0.0)),
                "feedback_demote": r.get("feedback_demote", 0.0),
                "adjusted_score": r.get("feedback_adjusted_score"),
            }
            for r in merged
        ]
        if claim_text:
            trace["claim"] = claim_text
            trace["attribution_scores"] = [
                {
                    "doc_id": r.get("doc_id"),
                    "chunk_id": r.get("chunk_id"),
                    "attribution": r.get("attribution"),
                    "score": r.get("attribution_score"),
                }
                for r in merged
                if r.get("attribution_score") is not None
            ]
        timing["total_ms"] = round((time.perf_counter() - total_t0) * 1000, 3)
        timing["embedding_ms"] = round(self._take_encode_ms(), 3)
        try:
            # RED-1: see the expanded-trace branch above — routes into this
            # call's own state, never a sibling's.
            single_trace_id = _trace_ring().add(
                trace, owner=getattr(principal, "agent_id", None)
            )
            self.last_trace_id = single_trace_id
            for result in results:
                result["trace_id"] = single_trace_id
        except Exception as exc:
            logger.debug("trace capture failed: %s", exc)
            self.last_trace_id = None

        if summarize_neighborhood and not past_search_deadline(deadline_monotonic):
            results = self._add_neighborhood_summaries(
                results,
                principal=principal,
                workspace=workspace,
                deadline_monotonic=deadline_monotonic,
            )

        return results

    def search(self, *args, **kwargs) -> List[Dict]:
        """Backward-compatible alias for callers that use search() terminology."""
        return self.retrieve(*args, **kwargs)

    def expand_result(
        self,
        result_id: int,
        depth: str = "chunk",
        update_access: bool = True,
        # G19: gate expand (called by _handle_expand which now stamps principal)
        principal: Optional[EffectivePrincipal] = None,
        workspace: str = "default",
        claim: Optional[str] = None,
        # Identifier-kind disambiguation: "auto" keeps the legacy chunk-first
        # then doc fallback; "chunk"/"doc" restrict to that namespace only.
        id_kind: str = "auto",
    ) -> Optional[Dict]:
        """
        Re-fetch a specific result at a deeper depth tier.

        *result_id* may be either a chunk_id or a doc_id; with the default
        ``id_kind="auto"`` this method tries chunk_id first, then falls back
        to doc_id (legacy bare-result_id behavior). Pass ``id_kind="doc"``
        when the id is known to be a doc_id (source/path/wikilink drill
        resolution) or ``id_kind="chunk"`` for an explicit chunk_id, so a
        doc_id that numerically collides with another document's chunk_id
        cannot resolve to the wrong document.

        Args:
            result_id: chunk_id or doc_id from a prior search result.
            depth: Target depth tier ('chunk' or 'document'). Defaults to 'chunk'.
            update_access: Whether to bump access_count on the document.
            id_kind: "auto" (legacy), "chunk", or "doc".

        Returns:
            A result dict at the requested depth, or None if not found.
        """
        if depth not in _VALID_DEPTHS:
            depth = "chunk"
        normalized_kind = str(id_kind or "auto").strip().lower()
        if normalized_kind not in {"auto", "chunk", "doc"}:
            raise ValueError(
                f'unknown id_kind {id_kind!r}; valid values: "auto", "chunk", "doc"'
            )

        import os

        row = None
        if normalized_kind in {"auto", "chunk"}:
            with self.db.cursor() as c:
                # Try chunk_id first
                c.execute("""
                    SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                           d.path, d.agent, d.sigil, d.decay_score,
                           d.privacy_level, d.page_type, d.page_status
                    FROM chunk_embeddings ce
                    JOIN documents d ON d.doc_id = ce.doc_id
                    WHERE ce.chunk_id = ?
                """, (result_id,))
                row = c.fetchone()

        if row is None and normalized_kind in {"auto", "doc"}:
            # Fall back to doc_id: get the best chunk for this document
            with self.db.cursor() as c:
                c.execute("""
                    SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                           d.path, d.agent, d.sigil, d.decay_score,
                           d.privacy_level, d.page_type, d.page_status
                    FROM chunk_embeddings ce
                    JOIN documents d ON d.doc_id = ce.doc_id
                    WHERE ce.doc_id = ?
                    ORDER BY ce.chunk_id
                    LIMIT 1
                """, (result_id,))
                row = c.fetchone()

        if row is None:
            return None

        raw = {
            "doc_id": row["doc_id"],
            "chunk_id": row["chunk_id"],
            "path": row["path"],
            "source": row["path"],
            "filename": os.path.basename(row["path"]),
            "agent": row["agent"],
            "sigil": row["sigil"],
            "score": 0.0,
            "fts_rank": None,
            "sem_rank": None,
            "rrf_score": None,
            "rerank_score": None,
            "decay_score": row["decay_score"],
            "chunk_text": row["chunk_text"],
            "heading_context": row["heading_context"] or "",
            "token_count": 0,
            "confidence": None,
            "age_days": None,
            # M3: surface real privacy/status/type so can_read_document does not
            # silently default privacy to "safe" (which leaked private/blocked
            # docs on the direct-expand path).
            "privacy_level": row["privacy_level"] or "safe",
            "page_type": row["page_type"],
            "page_status": row["page_status"] or "candidate",
            # Note: `documents` has no workspace_id column in this schema (see
            # db.py CREATE TABLE documents); can_read_document already treats a
            # missing workspace_id as call-scope-matching "default", which is
            # the correct behavior here, not a privacy gap.
        }

        if depth == "document":
            try:
                raw["full_document_text"] = self._fetch_full_document(row["doc_id"])
            except RequestDeadlineExceeded:
                self._stamp_document_hydration_degraded(raw)

        if update_access:
            with self.db.cursor() as c:
                c.execute(
                    """UPDATE documents
                       SET access_count = access_count + 1, last_accessed = ?
                       WHERE doc_id = ?""",
                    (time.time(), row["doc_id"]),
                )

        # G19: gate expand_result (foreign/private docs denied even on direct id)
        if principal is not None:
            ws = workspace or getattr(principal, "workspace_id", "default")
            if not can_read_document(principal, ws, raw):
                return None
            # G22 envelope on expanded too. Prefer the full document text when it
            # was fetched (depth="document"): _apply_depth returns the whole
            # document, so instruction_like detection and attribution must be
            # scored against what is actually returned, not just the first chunk.
            txt = str(raw.get("full_document_text") or raw.get("chunk_text") or "")
            raw["instruction_like"] = bool(is_instruction_like(txt))
            attribution = self._score_attribution(claim, txt)
            if attribution is not None:
                raw.update(attribution)
            raw["visibility"] = "authorized-via-expand"
            raw["reasoning"] = f"expand can_read_document passed for {getattr(principal,'agent_id','?')}"
            src = raw.get("path", "?")
            ag = raw.get("agent", "?")
            st = raw.get("page_status", "?")
            pr = raw.get("privacy_level", "?")
            sc = float(raw.get("score") or 0)
            vis = raw.get("visibility", "authorized-via-expand")
            raw["evidence_envelope"] = build_evidence_envelope(
                source=src,
                agent=ag,
                status=st,
                privacy=pr,
                score=sc,
                instruction_like=raw["instruction_like"],
                visibility=vis,
                text=txt,
                attribution=raw.get("attribution"),
                perturbation_enabled=getattr(
                    self.config, "instruction_body_perturbation_enabled", True
                ),
            )
            if raw["instruction_like"] or raw.get("attribution_score") is not None:
                existing_full = raw.get("full_provenance")
                if not isinstance(existing_full, dict):
                    existing_full = {}
                raw["full_provenance"] = {
                    **existing_full,
                }
                # Deliberately NOT storing the raw unperturbed body here (see
                # retrieve()): the raw form must never ride outside the perturbed
                # <EVIDENCE> envelope; _recover_instruction_like_body() restores
                # it for audit/display.
                if raw.get("attribution_score") is not None:
                    raw["full_provenance"].update({
                        "attribution": raw.get("attribution"),
                        "attribution_score": raw.get("attribution_score"),
                        "attribution_model": raw.get("attribution_model"),
                    })
            if "chunk_text" in raw:
                raw["chunk_text"] = raw["evidence_envelope"]
            # The envelope body IS the full text at document depth — never ship
            # the raw document body alongside it (same leak class as chunk_text).
            if "full_document_text" in raw:
                raw["full_document_text"] = raw["evidence_envelope"]

        return self._project_depth(raw, depth)

    #: Event types that exist for observability, not as recallable memory.
    #: `recall` rows are the durable recall trace (minnid_runtime.recall writes
    #: one per search, TTL'd by trim_recall_traces) — surfacing them would make
    #: every episodic search return a log of its own past searches.
    #: Defined in episodic.py so this filter and the episodic coverage metric in
    #: health read one list — a trace type added to one but not the other would
    #: mean health counting rows search can never return.
    EPISODIC_NON_MEMORY_TYPES: tuple = _NON_MEMORY_EVENT_TYPES

    def search_episodic(
        self,
        query: str,
        agent_id: Optional[str] = None,
        limit: int = 10,
        exclude_event_types: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        """Search episodic events via FTS5.

        ``exclude_event_types`` drops observability-only rows; the search RPC
        passes EPISODIC_NON_MEMORY_TYPES. Default None keeps every event, so
        existing direct callers see no change.
        """
        safe_q = self._sanitize_fts_query(query)
        if not safe_q:
            return []

        excluded = [str(t) for t in (exclude_event_types or ())]
        type_clause = ""
        type_params: list = []
        if excluded:
            type_clause = (
                " AND (e.event_type IS NULL OR e.event_type NOT IN "
                f"({','.join('?' * len(excluded))}))"
            )
            type_params = excluded

        results = []
        with self.db.cursor() as c:
            if agent_id:
                c.execute(f"""
                    SELECT ef.event_id, ef.agent_id, ef.content, e.event_type,
                           e.task_id, e.thread_id, e.created_at
                    FROM episodic_fts ef
                    JOIN episodic_events e ON e.event_id = ef.event_id
                    WHERE episodic_fts MATCH ? AND ef.agent_id = ?{type_clause}
                    ORDER BY rank
                    LIMIT ?
                """, (safe_q, agent_id, *type_params, limit))
            else:
                c.execute(f"""
                    SELECT ef.event_id, ef.agent_id, ef.content, e.event_type,
                           e.task_id, e.thread_id, e.created_at
                    FROM episodic_fts ef
                    JOIN episodic_events e ON e.event_id = ef.event_id
                    WHERE episodic_fts MATCH ?{type_clause}
                    ORDER BY rank
                    LIMIT ?
                """, (safe_q, *type_params, limit))

            for row in c.fetchall():
                results.append(dict(row))

        # hooks-PL-2: the learning access/read tracking that previously lived
        # here referenced result["learning_id"], which episodic rows do not
        # carry (KeyError on any hit) — it belongs in search_learnings, where
        # it now lives. Episodic events need no learning_reads rows.
        return results

    def search_learnings(
        self,
        query: str,
        agent_id: Optional[str] = None,
        agent_scope: Optional[Sequence[str]] = None,
        cross_agent: bool = False,
        limit: int = 10,
        source: str = "retrieval.search_learnings",
        update_access: bool = True,
    ) -> List[Dict]:
        """Search write-back learnings via FTS5.

        ``source`` labels the learning_reads rows written below, so callers
        (e.g. the daemon search RPC) can distinguish their read channel in
        diagnostics.
        ``update_access`` False skips access_count / learning_reads writes —
        same qty gate as document retrieve() when the search deadline aborted.
        """
        safe_q = self._sanitize_fts_query(query)
        if not safe_q:
            return []
        if cross_agent:
            scope = []
        elif agent_scope is not None:
            scope = self._normalize_agent_filter(agent_scope)
        elif agent_id:
            scope = agent_scope_for(agent_id)
        else:
            scope = []

        def _match(match_q: str) -> List[Dict]:
            rows = []
            with self.db.cursor() as c:
                if scope:
                    placeholders = ",".join("?" * len(scope))
                    c.execute(f"""
                        SELECT lf.learning_id, lf.agent_id, lf.content, lf.category,
                               l.confidence, l.created_at, l.access_count
                        FROM learnings_fts lf
                        JOIN learnings l ON l.learning_id = lf.learning_id
                        WHERE learnings_fts MATCH ? AND lf.agent_id IN ({placeholders})
                              AND l.superseded_by IS NULL
                              AND (l.status IS NULL OR l.status NOT IN ('rejected','expired','superseded'))
                        ORDER BY rank
                        LIMIT ?
                    """, [match_q, *scope, limit])
                else:
                    c.execute("""
                        SELECT lf.learning_id, lf.agent_id, lf.content, lf.category,
                               l.confidence, l.created_at, l.access_count
                        FROM learnings_fts lf
                        JOIN learnings l ON l.learning_id = lf.learning_id
                        WHERE learnings_fts MATCH ?
                              AND l.superseded_by IS NULL
                              AND (l.status IS NULL OR l.status NOT IN ('rejected','expired','superseded'))
                        ORDER BY rank
                        LIMIT ?
                    """, (match_q, limit))
                for row in c.fetchall():
                    rows.append(dict(row))
            return rows

        # Strict pass first: FTS5 space-joined terms are implicit AND — precise
        # when every term appears in the learning. But a natural-language
        # question ("What is the hard timeout of the …?") almost never has ALL
        # its tokens in the stored content, so a zero-hit AND query degrades to
        # OR semantics: bm25 rank still puts the learning matching the most /
        # rarest terms first, restoring recall without diluting queries the
        # strict pass already answers.
        results = _match(safe_q)
        terms = safe_q.split()
        if not results and len(terms) > 1:
            # Lowercase the operands: FTS5 matching is case-insensitive, but a
            # literal uppercase "OR"/"AND"/"NOT" token from the query would be
            # parsed as an operator and corrupt the joined expression.
            results = _match(" OR ".join(t.lower() for t in terms))

        # hooks-PL-2 leg (a): searching learnings IS reading them. Record
        # access + a learning_reads row so subscribe_contradictions can later
        # match a correction against this read (the search path previously
        # wrote no learning_reads at all — moved here from search_episodic,
        # where it crashed on a missing learning_id key).
        if results and update_access:
            now = time.time()
            from minni.request_deadline import RequestDeadlineExceeded

            try:
                with self.db.cursor() as c:
                    for result in results[:limit]:
                        c.execute(
                            """UPDATE learnings
                               SET access_count = access_count + 1, last_accessed = ?
                               WHERE learning_id = ?""",
                            (now, result["learning_id"]),
                        )
                        try:
                            # OR IGNORE: two searches in the same clock tick
                            # collide on the (learning_id, agent_id, read_at) PK;
                            # the read is already recorded for that instant, so
                            # the duplicate is dropped instead of raising an
                            # IntegrityError that the except below would swallow
                            # as silently dropped tracking.
                            c.execute(
                                """INSERT OR IGNORE INTO learning_reads
                                   (learning_id, agent_id, read_at, source)
                                   VALUES (?, ?, ?, ?)""",
                                (
                                    result["learning_id"],
                                    agent_id or "unknown",
                                    now,
                                    source,
                                ),
                            )
                        except RequestDeadlineExceeded:
                            raise
                        except Exception as exc:
                            # hooks-PL-5: never silently drop read tracking — a
                            # missing row here is exactly what makes stale_beliefs
                            # fire events:[] forever.
                            logger.warning(
                                "learning_reads insert failed for learning #%s: %s",
                                result.get("learning_id"), exc,
                            )

            except RequestDeadlineExceeded:
                # cursor() rolled back the whole tracking transaction. The
                # completed read is still useful; the RPC reports expiration.
                pass

        return results

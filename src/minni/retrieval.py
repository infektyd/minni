"""
Minni V3.1 — Retrieval Engine.

Hybrid retrieval pipeline combining:
1. Tier 1 in-memory inverted prefix/token Trie (`minni.trie_index`)
2. SQLite FTS5 lexical BM25 & chronological search (`minni.fts_search`)
3. FAISS semantic search, RRF fusion & Cross-Encoder reranking (`minni.semantic_rerank`)
4. Progressive disclosure depth tiers (`headline`, `snippet`, `chunk`, `document`)
"""

import concurrent.futures
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import numpy as np

from minni.config import DEFAULT_CONFIG, MinniConfig, correction_class_page_types
from minni.db import MinniDB
from minni.episodic import NON_MEMORY_EVENT_TYPES as _NON_MEMORY_EVENT_TYPES
from minni.faiss_index import FAISSIndex
from minni.fts_search import (
    _FTS_CHUNK_TEXT_EXPR,
    _FTS_RETRY_ATTEMPTS,
    _FTS_RETRY_BACKOFFS,
    _FTS_TRANSIENT_MARKERS,
    FtsSearchMixin,
    _fts_execute_with_retry,
    _strip_leading_frontmatter,
)
from minni.principal import EffectivePrincipal, agent_scope_for, can_read_document  # type: ignore
from minni.query_expand import expand as expand_query
from minni.query_expand import expand_with_status as expand_query_with_status
from minni.query_expand import summarize_with_afm
from minni.request_deadline import (
    RequestDeadlineExceeded,
    allow_expired_sql,
    bind_copied_deadline,
    current_query_embed_cache,
    run_bound,
)
from minni.safety import is_instruction_like
from minni.semantic_rerank import SemanticRerankMixin
from minni.timestamps import parse_epoch_or_report
from minni.trie_index import TrieIndexMixin, _strip_frontmatter, _tokenize_trie_text
from minni.wiki_indexer import WikiFrontmatter

logger = logging.getLogger("sovereign.retrieval")

SEARCH_STAGE_MIN_REMAINING_S = 2.0
SEARCH_MODEL_LOAD_MIN_REMAINING_S = 27.0
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
    """True when a retrieve() call has exhausted its client-facing budget."""
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
    """Seconds to wait for `_faiss_load_lock`, or None for unbounded."""
    remaining = remaining_search_budget(deadline_monotonic)
    if remaining is None:
        return None
    return max(0.0, remaining - SEARCH_FAISS_REBUILD_MIN_REMAINING_S)


DepthTier = Literal["headline", "snippet", "chunk", "document"]
_VALID_DEPTHS = {"headline", "snippet", "chunk", "document"}
_SNIPPET_MAX_CHARS = 280

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

_MAX_VARIANT_WORKERS = 4
RETRIEVAL_VARIANT_PARALLEL = True

_DEGRADATION_STATE_ATTRS = {
    "rerank": "rerank_degraded",
    "query_expand": "query_expand_degraded",
    "vector": "vector_degraded",
    "hyde": "hyde_degraded",
    "document_hydration": "document_hydration_degraded",
}


@dataclass
class RetrievalCallState:
    """Per-call mutable verdicts for one retrieve() invocation."""

    auth_suppression: Optional[Dict] = None
    rerank_degraded: Optional[str] = None
    query_expand_degraded: Optional[str] = None
    vector_degraded: Optional[str] = None
    hyde_degraded: Optional[str] = None
    document_hydration_degraded: Optional[str] = None
    trace_id: Optional[str] = None


class _ThreadLocalStateProxy:
    """RetrievalCallState-shaped view of the legacy thread-local properties."""

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


_correction_class_page_types = correction_class_page_types


def _effective_decay(r: Dict, fallback: Optional[float] = None) -> Optional[float]:
    """The decay the ranking and confidence legs actually used."""
    for value in (r.get("decay_applied"), r.get("decay_score"), fallback):
        if value is not None:
            return value
    return None


def _path_to_wikilink(path: str) -> Optional[str]:
    """Convert an absolute path to a [[wikilink]] style reference."""
    if not path:
        return None
    name = os.path.splitext(os.path.basename(path))[0]
    for marker in ("/wiki/", "/raw/", "/schema/"):
        idx = path.find(marker)
        if idx >= 0:
            rel = path[idx + 1 :]
            rel_noext = os.path.splitext(rel)[0]
            return f"[[{rel_noext}]]"
    return f"[[{name}]]"


def _xml_attr_escape(value) -> str:
    """Escape XML attribute metacharacters so untrusted strings cannot break out."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _evidence_body_escape(text: str) -> str:
    """Escape an evidence snippet body against envelope breakout and markdown hazards."""
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
    """G22 evidence-only envelope with attribute + body escaping."""
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
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
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


class RetrievalEngine(TrieIndexMixin, FtsSearchMixin, SemanticRerankMixin):
    """Hybrid FTS5 + FAISS semantic retrieval with cross-encoder re-ranking."""

    def __init__(
        self,
        db: MinniDB,
        config: MinniConfig = DEFAULT_CONFIG,
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
        self._trace_id_local = threading.local()
        self.vector_model_down: bool = False
        self._degradation_local = threading.local()
        self.last_trace_id = None
        self._auth_suppression_local = threading.local()
        self._call_state_local = threading.local()
        self._faiss_load_lock = threading.Lock()
        self._correction_types = _correction_class_page_types(config)

    def _stack_top(self) -> Optional["RetrievalCallState"]:
        local = getattr(self, "_call_state_local", None)
        stack = getattr(local, "stack", None) if local is not None else None
        return stack[-1] if stack else None

    @property
    def last_auth_suppression(self) -> Optional[Dict]:
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
        local = getattr(self, "_auth_suppression_local", None)
        if local is None:
            local = threading.local()
            self._auth_suppression_local = local
        local.value = value

    @property
    def last_trace_id(self) -> Optional[str]:
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
        local = getattr(self, "_degradation_local", None)
        if local is None:
            local = threading.local()
            self._degradation_local = local
        setattr(local, name, value)

    @property
    def last_rerank_degraded(self) -> Optional[str]:
        return self._degradation_flag("rerank")

    @last_rerank_degraded.setter
    def last_rerank_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("rerank", value)

    @property
    def last_query_expand_degraded(self) -> Optional[str]:
        return self._degradation_flag("query_expand")

    @last_query_expand_degraded.setter
    def last_query_expand_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("query_expand", value)

    @property
    def last_vector_degraded(self) -> Optional[str]:
        return self._degradation_flag("vector")

    @last_vector_degraded.setter
    def last_vector_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("vector", value)

    @property
    def last_hyde_degraded(self) -> Optional[str]:
        return self._degradation_flag("hyde")

    @last_hyde_degraded.setter
    def last_hyde_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("hyde", value)

    @property
    def last_document_hydration_degraded(self) -> Optional[str]:
        return self._degradation_flag("document_hydration")

    @last_document_hydration_degraded.setter
    def last_document_hydration_degraded(self, value: Optional[str]) -> None:
        self._set_degradation_flag("document_hydration", value)

    def _current_state(self) -> Any:
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
        self.last_auth_suppression = state.auth_suppression
        self.last_rerank_degraded = state.rerank_degraded
        self.last_query_expand_degraded = state.query_expand_degraded
        self.last_vector_degraded = state.vector_degraded
        self.last_hyde_degraded = state.hyde_degraded
        self.last_document_hydration_degraded = state.document_hydration_degraded
        self.last_trace_id = state.trace_id

    def _deadline_skipped_vector(self) -> bool:
        for flag in (self.last_vector_degraded, self.last_rerank_degraded):
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
        from minni.models import get_embedder

        return get_embedder()

    @property
    def reranker(self):
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
        if self._attribution_model is not None:
            return self._attribution_model
        from minni.models import get_attribution_cross_encoder

        if should_skip_cold_model_load(
            self._current_deadline(), get_attribution_cross_encoder
        ):
            return None
        self._attribution_model = get_attribution_cross_encoder()
        return self._attribution_model

    def _score_attribution(
        self, claim: Optional[str], evidence_text: str
    ) -> Optional[Dict]:
        claim_text = str(claim or "").strip()
        if not claim_text or not getattr(self.config, "attribution_enabled", True):
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

            with search_model_lock(
                get_attribution_lock(), self._current_deadline()
            ) as acquired:
                if not acquired or past_search_deadline(self._current_deadline()):
                    return None
                raw = model.predict(
                    [(str(evidence_text or ""), claim_text)],
                    show_progress_bar=False,
                )
        except Exception as exc:
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
        if self._tokenizer is None:
            try:
                import tiktoken

                self._tokenizer = tiktoken.get_encoding(self.config.token_model)
            except ImportError:
                logger.warning("tiktoken not installed — using word-count approximation")
                self._tokenizer = False
        return self._tokenizer if self._tokenizer is not False else None

    def _count_tokens(self, text: str) -> int:
        if self.tokenizer:
            return len(self.tokenizer.encode(text))
        return int(len(text.split()) / 0.75)

    @staticmethod
    def apply_read_gate(principal, workspace: str, merged: List[Dict]):
        """G19 read gate with P0-A silent-empty suppression diagnostic."""
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

    # ── Store-time semantic indexing (durable recall) ─────────

    @property
    def chunker(self):
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
        """Chunk + embed + index a durable document into FTS, FAISS, and Trie."""
        try:
            if not content or not content.strip():
                return {"status": "skipped", "reason": "empty_content"}

            if repair_projection:
                from minni.durable_projection import durable_doc_path, durable_metadata

                expected = durable_metadata(content)
                if (
                    path != durable_doc_path(agent, "", self.config.vault_path, content)
                    or page_type != "learning"
                    or any(
                        locals_value != expected[key]
                        for key, locals_value in (
                            ("sigil", sigil),
                            ("privacy_level", privacy_level),
                            ("page_status", page_status),
                            ("layer", layer),
                        )
                    )
                    or privacy_level == "blocked"
                    or page_status in {"draft", "expired", "rejected", "superseded"}
                ):
                    return {"status": "skipped", "reason": "ineligible_projection"}

            now = time.time()
            model_name = model_name or self.config.embedding_model

            prepared_chunks: List[Tuple[Any, np.ndarray]] = []
            embed_failed = False
            if self.model:
                chunks = self.chunker.chunk_document(content)
                if not chunks:
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
                            path,
                            chunk.chunk_index,
                            exc,
                        )
                        embed_failed = True
                        break
                    prepared_chunks.append((chunk, emb))
                if embed_failed:
                    prepared_chunks = []

            with self.db.transaction() as c:
                if repair_projection:
                    from minni.durable_projection import ACTIVE_LEARNING_SQL

                    active = c.execute(
                        f"SELECT 1 FROM learnings WHERE agent_id=? AND content=? "
                        f"AND {ACTIVE_LEARNING_SQL} LIMIT 1",
                        (agent, content),
                    ).fetchone()
                    if not active:
                        return {"status": "skipped", "reason": "learning_changed"}
                c.execute(
                    "SELECT doc_id, agent, sigil, page_status, privacy_level,"
                    " layer FROM documents WHERE path = ?",
                    (path,),
                )
                row = c.fetchone()
                fill_placeholder = False
                if row and repair_projection:
                    fts = c.execute(
                        "SELECT 1 FROM vault_fts WHERE doc_id = ?", (row["doc_id"],)
                    ).fetchone()
                    if fts is not None:
                        return {"status": "skipped", "reason": "projection_exists"}
                    from minni.durable_projection import projection_row_closed

                    if projection_row_closed(row["page_status"], row["privacy_level"]):
                        return {"status": "skipped", "reason": "projection_closed"}
                    fill_placeholder = True
                if row:
                    doc_id = row["doc_id"]
                    if fill_placeholder:
                        agent, sigil = row["agent"], row["sigil"]
                        if row["layer"]:
                            layer = row["layer"]
                    else:
                        c.execute(
                            """UPDATE documents
                               SET agent=?, sigil=?, last_modified=?, indexed_at=?,
                                   page_status=?, privacy_level=?, page_type=?,
                                   layer=?, whole_document=?
                               WHERE doc_id=?""",
                            (
                                agent,
                                sigil,
                                now,
                                now,
                                page_status,
                                privacy_level,
                                page_type,
                                layer,
                                whole_document,
                                doc_id,
                            ),
                        )
                    old_chunk_ids = [
                        r["chunk_id"]
                        for r in c.execute(
                            "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?",
                            (doc_id,),
                        ).fetchall()
                    ]
                    self._invalidate_durable_rerank(old_chunk_ids)
                    self._remove_live_faiss(old_chunk_ids)
                    c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                    c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
                else:
                    c.execute(
                        """INSERT INTO documents
                           (path, agent, sigil, last_modified, indexed_at,
                            page_status, privacy_level, page_type, layer,
                            whole_document)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            path,
                            agent,
                            sigil,
                            now,
                            now,
                            page_status,
                            privacy_level,
                            page_type,
                            layer,
                            whole_document,
                        ),
                    )
                    doc_id = c.lastrowid

                c.execute(
                    """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
                       VALUES (?, ?, ?, ?, ?)""",
                    (doc_id, path, content, agent, sigil),
                )

                new_chunk_ids: List[int] = []
                new_vectors: List[np.ndarray] = []
                for chunk, emb in prepared_chunks:
                    c.execute(
                        """INSERT INTO chunk_embeddings
                           (doc_id, chunk_index, chunk_text, embedding,
                            heading_context, model_name, computed_at, layer)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            doc_id,
                            chunk.chunk_index,
                            chunk.text,
                            emb.tobytes(),
                            chunk.heading_path,
                            model_name,
                            now,
                            layer,
                        ),
                    )
                    new_chunk_ids.append(c.lastrowid)
                    new_vectors.append(emb)

            self._refresh_live_faiss(new_chunk_ids, new_vectors)
            if on_vectors is not None and new_chunk_ids:
                on_vectors(new_chunk_ids, new_vectors)
            self._index_doc_in_trie(
                doc_id=doc_id,
                path=path,
                agent=agent,
                sigil=sigil,
                decay_score=1.0,
                chunk_text=content,
                heading_context=prepared_chunks[0][0].heading_path
                if prepared_chunks
                else "",
                page_status=page_status,
                privacy_level=privacy_level,
                page_type=page_type,
                evidence_refs=None,
                indexed_at=now,
                layer=layer,
            )
            elig_cache = getattr(self, "_foreign_vault_elig_cache", None)
            if isinstance(elig_cache, dict):
                elig_cache.clear()

            return {
                "status": "ok",
                "doc_id": doc_id,
                "chunks": len(new_chunk_ids),
            }
        except Exception as exc:
            logger.warning(
                "durable-index: semantic indexing failed for %r (%s) — "
                "store succeeded; document projection needs repair",
                path,
                exc,
            )
            return {"status": "degraded", "reason": str(exc)}

    def purge_durable_document(self, path: str) -> dict:
        """Remove a durable synthetic doc (by path) from every index."""
        try:
            with self.db.transaction() as c:
                c.execute("SELECT * FROM documents WHERE path = ?", (path,))
                row = c.fetchone()
                if row is None:
                    return {"status": "not_found", "path": path}
                doc_id = row["doc_id"]
                from minni.durable_projection import projection_row_closed
                from minni.graph_commit import canonical_node_learning_state

                mapped, active, retired_status, successor = (
                    canonical_node_learning_state(c, doc_id)
                )
                closed = projection_row_closed(row["page_status"], row["privacy_level"])
                canonical = mapped or (
                    "memory_kind" in row.keys() and row["memory_kind"] == "learning"
                )
                if canonical and not mapped:
                    return {"status": "unmapped_kept", "doc_id": doc_id}
                if active and not closed:
                    return {"status": "shared_kept", "doc_id": doc_id}
                old_chunk_ids = [
                    r["chunk_id"]
                    for r in c.execute(
                        "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?",
                        (doc_id,),
                    ).fetchall()
                ]
                c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
                if mapped:
                    if not closed:
                        c.execute(
                            "UPDATE documents SET page_status=?, superseded_by=? WHERE doc_id=?",
                            (retired_status, successor, doc_id),
                        )
                else:
                    c.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            self._invalidate_durable_rerank(old_chunk_ids)
            self._remove_live_faiss(old_chunk_ids)
            return {"status": "ok", "doc_id": doc_id, "chunks": len(old_chunk_ids)}
        except Exception as exc:
            logger.warning(
                "durable-index: purge failed for %r (%s) — learnings lifecycle "
                "stands, doc-search recall stays stale until reindex",
                path,
                exc,
            )
            return {"status": "degraded", "reason": str(exc)}

    def _invalidate_durable_rerank(self, chunk_ids: List[int]) -> None:
        if not chunk_ids:
            return
        try:
            from minni.rerank_cache import invalidate_chunks

            invalidate_chunks(chunk_ids)
        except Exception as exc:
            logger.debug("durable-index: rerank invalidation skipped: %s", exc)

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
        if not getattr(self.config, "feedback_enabled", True):
            return {}
        now = time.time()
        if now - self._feedback_cache_loaded_at > 60:
            self._refresh_feedback_cache()
        return self._feedback_cache.get((agent_id or "main", _query_class(query)), {})

    def _refresh_feedback_cache(self) -> None:
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

    # ── Context Window Budgeting & Depth Projection ────────────

    def _budget_results(self, results: List[Dict], query: str) -> List[Dict]:
        budget = self.config.context_budget_tokens
        if budget <= 0:
            return results

        budgeted = []
        total_tokens = 0
        for r in results:
            chunk_text = r.get("chunk_text", "")
            heading = r.get("heading_context", "")
            entry_text = f"{heading}: {chunk_text}" if heading else chunk_text
            entry_tokens = self._count_tokens(entry_text)
            if total_tokens + entry_tokens > budget and budgeted:
                break
            total_tokens += entry_tokens
            r["token_count"] = entry_tokens
            budgeted.append(r)
        return budgeted

    def _apply_depth(self, result: Dict, depth: str) -> Dict:
        if depth not in _VALID_DEPTHS:
            depth = "snippet"

        def _pr2_fields(result: Dict, existing: Optional[Dict] = None) -> Dict:
            fields = {
                "confidence": result.get("confidence"),
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
            if result.get("evidence_envelope") is not None:
                fields["evidence_envelope"] = result.get("evidence_envelope")
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
            raw_body = result.get("raw_chunk_text")
            if isinstance(raw_body, str):
                snippet_body = (
                    raw_body[:_SNIPPET_MAX_CHARS] + "…"
                    if len(raw_body) > _SNIPPET_MAX_CHARS
                    else raw_body
                )
                text = build_evidence_envelope(
                    source=result.get("source")
                    or result.get("path")
                    or result.get("filename")
                    or "?",
                    agent=result.get("agent", "?"),
                    status=result.get("review_state")
                    or result.get("page_status")
                    or "?",
                    privacy=result.get("privacy_level", "?"),
                    score=float(result.get("score") or 0),
                    instruction_like=bool(result.get("instruction_like")),
                    visibility=result.get("visibility", "authorized"),
                    text=snippet_body,
                    attribution=result.get("attribution"),
                    perturbation_enabled=getattr(
                        getattr(self, "config", None),
                        "instruction_body_perturbation_enabled",
                        True,
                    ),
                )
            else:
                text = result.get("chunk_text", "")
                if (
                    text.startswith("<EVIDENCE ")
                    and text.endswith("</EVIDENCE>")
                    and ">" in text
                ):
                    open_end = text.find(">")
                    open_tag = text[: open_end + 1]
                    close_tag = "</EVIDENCE>"
                    inner = text[open_end + 1 : -len(close_tag)]
                    if len(inner) > _SNIPPET_MAX_CHARS:
                        inner = inner[:_SNIPPET_MAX_CHARS] + "…"
                    text = f"{open_tag}{inner}{close_tag}"
                elif len(text) > _SNIPPET_MAX_CHARS:
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
            if "token_count" in result:
                out["token_count"] = result["token_count"]
            out.update(_pr2_fields(result, out))
            return out

        if depth in ("chunk", "document"):
            built_prov = result.get("provenance") or {
                "fts_rank": result.get("fts_rank"),
                "semantic_rank": result.get("sem_rank"),
                "rrf_score": result.get("rrf_score"),
                "cross_encoder_score": result.get(
                    "raw_rerank_score", result.get("rerank_score")
                ),
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
            if depth == "document" and "full_document_text" in result:
                out["full_document_text"] = result["full_document_text"]
            return out

        return self._apply_depth(result, "snippet")

    _DOCUMENT_HYDRATION_DEADLINE = "search deadline; skipped full document"

    def _stamp_document_hydration_degraded(self, raw: Dict) -> None:
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
        with self.db.cursor() as c:
            c.execute(
                """
                SELECT chunk_text FROM chunk_embeddings
                WHERE doc_id = ?
                ORDER BY chunk_id
                """,
                (doc_id,),
            )
            rows = c.fetchall()
            if rows:
                return "\n".join(row["chunk_text"] for row in rows)
            c.execute("SELECT content FROM vault_fts WHERE doc_id = ?", (doc_id,))
            fts_row = c.fetchone()
        return fts_row["content"] if fts_row and fts_row["content"] else None

    def _resolve_query_variants(self, query: str, expand) -> List[str]:
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
            variants, degraded = expand_query_with_status(
                query, mode=str(mode).lower()
            )
            if degraded:
                self._current_state().query_expand_degraded = degraded
        except Exception as exc:
            logger.warning("Query expansion failed: %s — using original query", exc)
            self._current_state().query_expand_degraded = str(exc)
            variants = [query]
        return variants or [query]

    def _chunk_index_empty(self) -> bool:
        try:
            with self.db.cursor() as c:
                has_chunks = c.execute(
                    "SELECT 1 FROM chunk_embeddings LIMIT 1"
                ).fetchone()
            return has_chunks is None
        except Exception as exc:
            logger.debug("empty chunk-index probe failed: %s", exc)
            return False

    def _merge_expanded_results(
        self,
        variant_results: List[List[Dict]],
        query_variants: List[str],
        limit: int,
    ) -> List[Dict]:
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
            provenance["matched_query_variants"] = result.get(
                "matched_query_variants", []
            )
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
        principal: Optional[EffectivePrincipal] = None,
        workspace: str = "default",
        deadline_monotonic: Optional[float] = None,
        mode: Optional[str] = None,
        _state: Optional[RetrievalCallState] = None,
    ) -> List[Dict]:
        """Hybrid retrieval (public entry; signature-compatible wrapper)."""
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
                mode=mode,
            )
        finally:
            self._pop_call_state()
            if outer:
                self._publish_call_state(state)

    def _is_large_corpus(self) -> bool:
        cached = getattr(self, "_cached_large_corpus", None)
        if cached is not None:
            return bool(cached)
        try:
            with self.db.cursor() as c:
                row = c.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
                n = int(row["n"] if isinstance(row, dict) else row[0])
                is_large = n >= 50
                self._cached_large_corpus = is_large
                return is_large
        except Exception:
            return False

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
        principal: Optional[EffectivePrincipal] = None,
        workspace: str = "default",
        deadline_monotonic: Optional[float] = None,
        mode: Optional[str] = None,
    ) -> List[Dict]:
        """Hybrid retrieval pipeline: FTS5 + FAISS/Trie + RRF + Cross-Encoder + Depth projection."""
        claim_text = str(claim or "").strip()
        effective_fast = mode == "fast" or (
            mode is None
            and backend is None
            and not claim_text
            and sort != "chronological"
            and self._is_large_corpus()
        )
        if (
            effective_fast
            and document_agent_filter is None
            and principal is not None
            and getattr(principal, "agent_id", None) not in ("main", "operator")
            and "*" not in (getattr(principal, "capabilities", None) or ())
            and bool(getattr(principal, "allowed_vault_roots", None))
            and getattr(self.config, "vault_path", None)
            and not principal.allows_vault_root(self.config.vault_path)
        ):
            document_agent_filter = [
                principal.agent_id,
                f"identity:{principal.agent_id}",
            ]

        state = self._current_state()
        state.rerank_degraded = None
        state.query_expand_degraded = None
        state.vector_degraded = None
        state.hyde_degraded = None
        state.document_hydration_degraded = None
        self._reset_encode_ms()
        self._set_current_deadline(deadline_monotonic)
        if effective_fast:
            query_variants = [query]
        elif past_search_deadline(deadline_monotonic):
            query_variants = [query]
            if expand not in (False, None, "off"):
                state.query_expand_degraded = "search deadline; skipped query expand"
        else:
            query_variants = self._resolve_query_variants(query, expand)

        parent_expand_degraded = state.query_expand_degraded
        if len(query_variants) > 1:
            return self._retrieve_expanded_variants(
                query=query,
                query_variants=query_variants,
                parent_expand_degraded=parent_expand_degraded,
                state=state,
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
                claim_text=claim_text,
                document_agent_filter=document_agent_filter,
                principal=principal,
                workspace=workspace,
                deadline_monotonic=deadline_monotonic,
                mode=mode,
            )

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

        skip_statuses = set()
        if not include_superseded:
            skip_statuses.add("superseded")
        if not include_rejected:
            skip_statuses.add("rejected")
        if not include_drafts:
            skip_statuses.add("draft")
        if not include_expired:
            skip_statuses.add("expired")
        skip_statuses.update(WikiFrontmatter.EXCLUDED_STATUSES)
        skip_list = sorted(skip_statuses)

        ws = (
            (workspace or getattr(principal, "workspace_id", "default"))
            if principal
            else (workspace or "default")
        )
        denied_by_scope: dict[tuple, Dict] = {}
        saw_authorized = False

        def _eligible(rows: List[Dict]) -> List[Dict]:
            nonlocal saw_authorized
            rows = [
                r
                for r in rows
                if (r.get("page_status") or "candidate") not in skip_statuses
                and (r.get("privacy_level") or "safe") != "blocked"
            ]
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

        def _collect_eligible(
            fetch, wanted: int, *, sql_window: bool = False
        ) -> List[Dict]:
            window = max(1, wanted)
            ceiling = window if effective_fast else max(window, 512)
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
                    if sql_window and len(raw) < window:
                        return rows
                    if window >= ceiling or past_search_deadline(deadline_monotonic):
                        trace["candidate_window_exhausted"] = True
                        return rows
                    window = min(ceiling, window * 2)

        rerank_k = (
            limit
            if effective_fast
            else max(
                limit,
                self.config.reranker_top_k if self.config.reranker_enabled else limit,
            )
        )

        lexical_searched = False

        def _lexical_eligible(fetch):
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
                merged = _lexical_eligible(
                    lambda window: self._chronological_search(
                        query,
                        window,
                        layers,
                        start_date,
                        end_date,
                        exclude_statuses=skip_list,
                    )
                )
            except RequestDeadlineExceeded:
                if not lexical_searched:
                    raise
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                merged = []
            timing["semantic_ms"] = round((time.perf_counter() - chrono_t0) * 1000, 3)
            trace["backends"] = ["chronological-sql"]
            merged = merged[:limit]
        else:
            fts_t0 = time.perf_counter()
            try:
                if document_agent_filter is None:
                    fts_results = _lexical_eligible(
                        lambda window: self._fts_search(
                            query, window, exclude_statuses=skip_list
                        )
                    )
                else:
                    fts_results = _lexical_eligible(
                        lambda window: self._fts_search(
                            query,
                            window,
                            agent_filter=document_agent_filter,
                            exclude_statuses=skip_list,
                        )
                    )
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

            extra_backend_results: List[List[Dict]] = []
            semantic_t0 = time.perf_counter()
            if effective_fast:
                try:
                    semantic_results = _eligible(
                        self.trie_search(
                            query,
                            limit=rerank_k,
                            agent_filter=document_agent_filter,
                            exclude_statuses=skip_list,
                        )
                    )
                except Exception:
                    semantic_results = []
                trace["backends"] = ["trie-fast"]
            elif past_search_deadline(deadline_monotonic):
                semantic_results = []
                trace["backends"] = ["fts-deadline"]
                if lexical_searched:
                    self.last_vector_degraded = "search deadline; lexical (FTS) only"
            elif backend is None and not fts_results and self._chunk_index_empty():
                semantic_results = []
                trace["backends"] = ["faiss-disk-empty"]
            elif backend is None:
                try:
                    semantic_results = _collect_eligible(
                        lambda window: self._semantic_search(query, window),
                        rerank_k,
                    )
                except RequestDeadlineExceeded:
                    semantic_results = []
                    if lexical_searched:
                        self.last_vector_degraded = "search deadline; lexical (FTS) only"
                trace["backends"] = ["faiss-disk"]
            elif isinstance(backend, list):
                resolved = self._resolve_backends(backend)
                query_emb = self._encode_query(query)
                if len(resolved) == 1:
                    semantic_results = _collect_eligible(
                        lambda window: self._backend_search(
                            query_emb, window, resolved[0]
                        ),
                        rerank_k,
                    )
                else:
                    def _fanout_backends(window: int) -> List[Dict]:
                        combined: List[Dict] = []
                        seen_docs: set = set()
                        for b in resolved:
                            for hit in self._backend_search(query_emb, window, b):
                                did = hit.get("doc_id")
                                if did not in seen_docs:
                                    seen_docs.add(did)
                                    combined.append(hit)
                        return combined

                    semantic_results = _collect_eligible(_fanout_backends, rerank_k)
                trace["backends"] = [getattr(b, "name", str(b)) for b in resolved]
            else:
                query_emb = self._encode_query(query)
                semantic_results = _collect_eligible(
                    lambda window: self._backend_search(query_emb, window, backend),
                    rerank_k,
                )
                trace["backends"] = [getattr(backend, "name", "custom")]

            timing["semantic_ms"] = round(
                (time.perf_counter() - semantic_t0) * 1000, 3
            )
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

            semantic_results = _eligible(semantic_results)
            extra_backend_results = [_eligible(rows) for rows in extra_backend_results]

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

            if effective_fast:
                merged = merged[:limit]
            elif merged and self.config.reranker_enabled:
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
                    merged = self._apply_post_rerank_confidence_floor(
                        merged, fts_results, limit
                    )
                else:
                    merged = merged[:limit]
            else:
                merged = merged[:limit]

            hyde_enabled = (
                False
                if effective_fast
                else (self.config.hyde_enabled if use_hyde is None else bool(use_hyde))
            )
            if hyde_enabled and past_search_deadline(deadline_monotonic):
                self.last_hyde_degraded = "search deadline; skipped hyde"
                hyde_enabled = False
            if hyde_enabled:
                merged = self._run_hyde_enrichment(
                    query=query,
                    merged=merged,
                    limit=limit,
                    rerank_k=rerank_k,
                    skip_list=skip_list,
                    document_agent_filter=document_agent_filter,
                    deadline_monotonic=deadline_monotonic,
                    trace=trace,
                    eligible_fn=_eligible,
                    collect_eligible_fn=_collect_eligible,
                )

        merged = self._apply_feedback_demotions(merged, query, agent_id)

        _ALWAYS_EXCLUDED = {"blocked"}
        _SKIP_STATUSES = skip_statuses
        if _SKIP_STATUSES or _ALWAYS_EXCLUDED:
            filtered = []
            for r in merged:
                status = r.get("page_status") or "candidate"
                privacy = r.get("privacy_level") or "safe"
                if status in _SKIP_STATUSES or privacy in _ALWAYS_EXCLUDED:
                    continue
                filtered.append(r)
            merged = filtered

        ws = (
            workspace or getattr(principal, "workspace_id", "default")
            if principal is not None
            else (workspace or "default")
        )
        self._current_state().auth_suppression = None
        if principal is not None:
            merged, suppression = self.apply_read_gate(principal, ws, merged)
            self.last_auth_suppression = suppression
            if not merged and denied_by_scope and not saw_authorized:
                _, self.last_auth_suppression = self.apply_read_gate(
                    principal, ws, list(denied_by_scope.values())
                )

        for r in merged:
            txt = str(
                r.get("chunk_text")
                or r.get("full_document_text")
                or r.get("content")
                or ""
            )
            r["raw_chunk_text"] = txt
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
            elif str(r.get("page_type", "")).lower() in {
                "wiki",
                "handoff",
                "synthesis",
            }:
                vis = "shared-wiki-authorized"
            r["visibility"] = vis
            r["reasoning"] = f"can_read_document(principal={pid or 'n/a'}, ws={ws}) passed"
            if r["instruction_like"] or r.get("attribution_score") is not None:
                existing_full = r.get("full_provenance")
                if not isinstance(existing_full, dict):
                    existing_full = {}
                r["full_provenance"] = {**existing_full}
                if r.get("attribution_score") is not None:
                    r["full_provenance"].update({
                        "attribution": r.get("attribution"),
                        "attribution_score": r.get("attribution_score"),
                        "attribution_model": r.get("attribution_model"),
                    })

        if budget_tokens:
            merged = self._budget_results(merged, query)

        if depth not in _VALID_DEPTHS:
            logger.warning("Unknown depth=%r, falling back to 'snippet'", depth)
            depth = "snippet"

        results = self._format_retrieved_results(
            merged=merged,
            depth=depth,
            claim_text=claim_text,
            query_variants=query_variants,
            update_access=update_access,
            deadline_monotonic=deadline_monotonic,
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

    def _retrieve_expanded_variants(
        self,
        *,
        query: str,
        query_variants: List[str],
        parent_expand_degraded: Optional[str],
        state: RetrievalCallState,
        limit: int,
        agent_id: Optional[str],
        update_access: bool,
        budget_tokens: bool,
        depth: str,
        include_superseded: bool,
        include_rejected: bool,
        include_drafts: bool,
        include_expired: bool,
        backend,
        layers: Optional[Sequence[str]],
        sort: Literal["semantic", "chronological"],
        start_date: Optional[str],
        end_date: Optional[str],
        expand,
        summarize_neighborhood: bool,
        use_hyde: Optional[bool],
        cross_agent: bool,
        claim: Optional[str],
        claim_text: str,
        document_agent_filter: Optional[Sequence[str]],
        principal: Optional[EffectivePrincipal],
        workspace: str,
        deadline_monotonic: Optional[float],
        mode: Optional[str],
    ) -> List[Dict]:
        """Fan out expanded query variants, merge results, and record expanded trace."""
        total_t0 = time.perf_counter()
        variant_suppressions: List[Dict] = []
        variant_rerank_degraded: List[str] = []
        variant_expand_degraded: List[str] = []
        variant_vector_degraded: List[str] = []
        variant_hyde_degraded: List[str] = []
        variant_document_hydration_degraded: List[str] = []
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
                mode=mode,
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
        use_variant_pool = (
            RETRIEVAL_VARIANT_PARALLEL and deadline_monotonic is None
        )
        if use_variant_pool:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(_MAX_VARIANT_WORKERS, len(query_variants)),
                thread_name_prefix="minni-variant",
            ) as _variant_pool:
                bound = [
                    bind_copied_deadline(_run_variant, index)
                    for index in range(len(query_variants))
                ]
                raw_rows = list(_variant_pool.map(run_bound, bound))
            for variant, rows, child in zip(
                query_variants, raw_rows, variant_states
            ):
                if _should_drop_deadline_child(child, per_variant):
                    truncated_expand = "search deadline; truncated query expand"
                    break
                per_variant.append(rows)
                ran_variants.append(variant)
                _record_kept_child(variant, child)
        else:
            for index, variant in enumerate(query_variants):
                if any(per_variant) and past_search_deadline(deadline_monotonic):
                    truncated_expand = "search deadline; truncated query expand"
                    break
                try:
                    rows = _run_variant(index)
                except RequestDeadlineExceeded:
                    truncated_expand = "search deadline; truncated query expand"
                    break
                child = variant_states[index]
                if _should_drop_deadline_child(child, per_variant):
                    truncated_expand = "search deadline; truncated query expand"
                    break
                per_variant.append(rows)
                ran_variants.append(variant)
                _record_kept_child(variant, child)

        results = self._merge_expanded_results(per_variant, ran_variants, limit)
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
        if truncated_expand:
            state.query_expand_degraded = (
                f"{merged_expand}; {truncated_expand}"
                if merged_expand
                else truncated_expand
            )
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
        if summarize_neighborhood and not past_search_deadline(deadline_monotonic):
            results = self._add_neighborhood_summaries(
                results,
                principal=principal,
                workspace=workspace,
                deadline_monotonic=deadline_monotonic,
            )
        if update_access and not self._deadline_skipped_vector():
            self._record_retrieved_access(results)
        try:
            expanded_trace = {
                "query": query,
                "variants": ran_variants,
                "expansion": {"mode": expand, "variant_count": len(ran_variants)},
                "final_ordering": [
                    {
                        "doc_id": r.get("doc_id"),
                        "score": r.get("score"),
                        "matched_query_variants": r.get(
                            "matched_query_variants", []
                        ),
                    }
                    for r in results
                ],
                "timing": {
                    "total_ms": round(
                        (time.perf_counter() - total_t0) * 1000, 3
                    ),
                },
            }
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

    def _run_hyde_enrichment(
        self,
        *,
        query: str,
        merged: List[Dict],
        limit: int,
        rerank_k: int,
        skip_list: List[str],
        document_agent_filter: Optional[Sequence[str]],
        deadline_monotonic: Optional[float],
        trace: Dict,
        eligible_fn,
        collect_eligible_fn,
    ) -> List[Dict]:
        """Execute HyDE hypothetical document generation and second-pass merge."""
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
                ce_probe = r.get("raw_rerank_score", r.get("rerank_score"))
                rrf_probe = r.get("rrf_score")
                if ce_probe is None and rrf_probe is not None:
                    rrf_probe = float(rrf_probe) * (20.0 / 61.0)
                probe["confidence"] = compute_confidence(
                    rrf_score=rrf_probe,
                    cross_encoder_score=ce_probe,
                    decay_factor=r.get("decay_applied", r.get("decay_score")),
                    db=None,
                )
                probe_results.append(probe)

            if should_trigger_hyde(
                probe_results,
                enabled=True,
                floor=self.config.hyde_confidence_floor,
            ):
                trace["hyde"]["triggered"] = True
                trace["hyde"]["confidence_floor"] = (
                    self.config.hyde_confidence_floor
                )
                if past_search_deadline(deadline_monotonic) or past_search_deadline(
                    deadline_monotonic, min_remaining=2.0
                ):
                    self.last_hyde_degraded = "search deadline; skipped hyde"
                    trace["hyde"]["completed"] = False
                    trace["hyde"]["skipped"] = "deadline"
                else:
                    hypothetical = generate_hypothetical_answer(
                        query, config=self.config
                    )
                    if not hypothetical:
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
                            hyde_fts = collect_eligible_fn(
                                lambda window: self._fts_search(
                                    hypothetical,
                                    window,
                                    exclude_statuses=skip_list,
                                ),
                                rerank_k,
                                sql_window=True,
                            )
                        else:
                            hyde_fts = collect_eligible_fn(
                                lambda window: self._fts_search(
                                    hypothetical,
                                    window,
                                    agent_filter=document_agent_filter,
                                    exclude_statuses=skip_list,
                                ),
                                rerank_k,
                                sql_window=True,
                            )
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
                                hyde_semantic = collect_eligible_fn(
                                    lambda window: self._semantic_search(
                                        hypothetical, window
                                    ),
                                    rerank_k,
                                )
                            vector_flag = self.last_vector_degraded
                            if (
                                hyde_apply
                                and vector_flag
                                and "search deadline" in str(vector_flag).lower()
                            ):
                                self.last_hyde_degraded = (
                                    "search deadline; skipped hyde"
                                )
                                trace["hyde"]["completed"] = False
                                trace["hyde"]["skipped"] = "deadline"
                                hyde_apply = False
                            if hyde_apply:
                                hyde_semantic = eligible_fn(hyde_semantic)
                                hyde_merged = self._rrf_merge(
                                    hyde_fts, hyde_semantic, rerank_k
                                )
                                hyde_merged = eligible_fn(hyde_merged)
                                if self.config.reranker_enabled:
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
                                        if (
                                            rerank_flag
                                            and "search deadline"
                                            in str(rerank_flag).lower()
                                        ):
                                            self.last_hyde_degraded = (
                                                "search deadline; skipped hyde"
                                            )
                                            trace["hyde"]["completed"] = False
                                            trace["hyde"]["skipped"] = "deadline"
                                            hyde_apply = False
                                        else:
                                            hyde_merged = hyde_merged[
                                                : max(
                                                    self.config.reranker_final_k,
                                                    limit,
                                                )
                                            ]
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
        except Exception as exc:
            logger.warning("HyDE leg failed after retrieval pass: %s", exc)
            trace["hyde"]["completed"] = False
            trace["hyde"]["skipped"] = "error"
            trace["hyde"]["error"] = str(exc)
            self._current_state().hyde_degraded = str(exc)[:400]
        return merged

    def _format_retrieved_results(
        self,
        *,
        merged: List[Dict],
        depth: str,
        claim_text: str,
        query_variants: List[str],
        update_access: bool,
        deadline_monotonic: Optional[float],
    ) -> List[Dict]:
        """Format merged candidates with envelopes, confidence, provenance, and depth projection."""
        from minni.rationale import explain
        from minni.scoring import compute_confidence, raw_confidence

        results = []
        for r in merged:
            score = round(
                r.get(
                    "feedback_adjusted_score",
                    r.get("rerank_score", r.get("final_score", 0)),
                ),
                4,
            )
            r["score"] = score
            src = r.get("path") or r.get("source") or r.get("filename") or "?"
            ag = r.get("agent", "?")
            st = r.get("page_status", "?")
            pr = r.get("privacy_level", "?")
            raw_txt = str(r.get("raw_chunk_text") or r.get("chunk_text") or "")
            r["evidence_envelope"] = build_evidence_envelope(
                source=src,
                agent=ag,
                status=st,
                privacy=pr,
                score=float(score or 0),
                instruction_like=bool(r.get("instruction_like")),
                visibility=r.get("visibility", "authorized"),
                text=raw_txt,
                attribution=r.get("attribution"),
                perturbation_enabled=getattr(
                    self.config, "instruction_body_perturbation_enabled", True
                ),
            )
            if "chunk_text" in r:
                r["chunk_text"] = r["evidence_envelope"]

            indexed_at = parse_epoch_or_report(
                r.get("indexed_at"),
                field="indexed_at",
                source="retrieval._format_results",
                doc_id=r.get("doc_id"),
            )
            age_days: Optional[float] = None
            if indexed_at is not None:
                age_days = round((time.time() - indexed_at) / 86400.0, 1)

            try:
                ce_for_confidence = r.get(
                    "raw_rerank_score", r.get("rerank_score")
                )
                decay_for_confidence = r.get(
                    "decay_applied", r.get("decay_score")
                )
                confidence = compute_confidence(
                    rrf_score=r.get("rrf_score"),
                    cross_encoder_score=ce_for_confidence,
                    decay_factor=decay_for_confidence,
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

            chunk_text = r.get("chunk_text", "")
            instr_like = r.get("instruction_like")
            if instr_like is None:
                try:
                    instr_like = is_instruction_like(chunk_text)
                except Exception:
                    instr_like = None

            page_type = r.get("page_type")
            source_authority = _page_type_to_authority(page_type, r.get("agent", ""))
            path = r.get("path", "")
            wikilink = _path_to_wikilink(path)
            evidence_refs = _parse_evidence_refs(r.get("evidence_refs"))
            page_status = r.get("page_status") or "candidate"
            privacy_level = r.get("privacy_level") or "safe"

            provenance = {
                "fts_rank": r.get("fts_rank"),
                "semantic_rank": r.get("sem_rank"),
                "rrf_score": r.get("rrf_score"),
                "cross_encoder_score": r.get(
                    "raw_rerank_score", r.get("rerank_score")
                ),
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
                "decay_applied": r.get("decay_applied"),
                "layer": r.get("layer", "knowledge"),
                "chunk_text": chunk_text,
                "raw_chunk_text": r.get("raw_chunk_text", ""),
                "evidence_envelope": r.get("evidence_envelope"),
                "visibility": r.get("visibility", "authorized"),
                "heading_context": r.get("heading_context", ""),
                "token_count": r.get("token_count", 0),
                "confidence": confidence,
                "confidence_raw": confidence_raw,
                "age_days": age_days,
                "provenance": provenance,
                "privacy_level": privacy_level,
                "source_authority": source_authority,
                "review_state": page_status,
                "instruction_like": instr_like,
                "wikilink": wikilink,
                "evidence_refs": evidence_refs,
                "recommended_action": _recommended_action(
                    page_status, instr_like, confidence
                ),
                "recommended_wiki_updates": [],
                "attribution": r.get("attribution"),
                "attribution_score": r.get("attribution_score"),
                "attribution_model": r.get("attribution_model"),
            }
            if r.get("full_provenance") is not None:
                raw["full_provenance"] = r.get("full_provenance")

            try:
                raw["rationale"] = explain(raw)
            except Exception:
                raw["rationale"] = None

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
                    raw["recommended_action"] = _recommended_action(
                        raw.get("review_state"), doc_flag, raw.get("confidence")
                    )
                    if claim_text and not past_search_deadline(deadline_monotonic):
                        attribution = self._score_attribution(claim_text, full_text)
                        if attribution is not None:
                            raw.update(attribution)
                            r.update(attribution)
                            for meta in (
                                raw.get("provenance"),
                                raw.get("full_provenance"),
                            ):
                                if isinstance(meta, dict):
                                    meta.update({
                                        "attribution": raw.get("attribution"),
                                        "attribution_score": raw.get(
                                            "attribution_score"
                                        ),
                                        "attribution_model": raw.get(
                                            "attribution_model"
                                        ),
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
                            self.config,
                            "instruction_body_perturbation_enabled",
                            True,
                        ),
                    )
                elif not hydration_degraded:
                    raw["full_document_text"] = full_text

            _result_rank = len(results) + 1
            raw["match_kind"] = "primary" if _result_rank == 1 else "related"
            raw["related_rank"] = None if _result_rank == 1 else _result_rank - 1

            projected = self._project_depth(raw, depth)
            projected["match_kind"] = raw["match_kind"]
            projected["related_rank"] = raw["related_rank"]
            projected["query_variants"] = query_variants
            results.append(projected)

        if update_access and not self._deadline_skipped_vector():
            self._record_retrieved_access(results)
        return results

    def _record_retrieved_access(self, results: Sequence[Dict]) -> None:
        """Increment documents.access_count at most once per unique doc_id per retrieve call."""
        seen_doc_ids: set[int] = set()
        now = time.time()
        with self.db.cursor() as c:
            for item in results:
                did = item.get("doc_id")
                if did is None:
                    continue
                did_int = int(did)
                if did_int in seen_doc_ids:
                    continue
                seen_doc_ids.add(did_int)
                c.execute(
                    """UPDATE documents
                       SET access_count = access_count + 1, last_accessed = ?
                       WHERE doc_id = ?""",
                    (now, did_int),
                )

    def search(self, *args, **kwargs) -> List[Dict]:
        """Backward-compatible alias for callers that use search() terminology."""
        return self.retrieve(*args, **kwargs)

    def expand_result(
        self,
        result_id: int,
        depth: str = "chunk",
        update_access: bool = True,
        principal: Optional[EffectivePrincipal] = None,
        workspace: str = "default",
        claim: Optional[str] = None,
        id_kind: str = "auto",
    ) -> Optional[Dict]:
        """Re-fetch a specific result at a deeper depth tier."""
        if depth not in _VALID_DEPTHS:
            depth = "chunk"
        normalized_kind = str(id_kind or "auto").strip().lower()
        if normalized_kind not in {"auto", "chunk", "doc"}:
            raise ValueError(
                f'unknown id_kind {id_kind!r}; valid values: "auto", "chunk", "doc"'
            )

        row = None
        if normalized_kind in {"auto", "chunk"}:
            with self.db.cursor() as c:
                c.execute(
                    """
                    SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                           d.path, d.agent, d.sigil, d.decay_score,
                           d.privacy_level, d.page_type, d.page_status
                    FROM chunk_embeddings ce
                    JOIN documents d ON d.doc_id = ce.doc_id
                    WHERE ce.chunk_id = ?
                    """,
                    (result_id,),
                )
                row = c.fetchone()

        if row is None and normalized_kind in {"auto", "doc"}:
            with self.db.cursor() as c:
                c.execute(
                    """
                    SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                           d.path, d.agent, d.sigil, d.decay_score,
                           d.privacy_level, d.page_type, d.page_status
                    FROM chunk_embeddings ce
                    JOIN documents d ON d.doc_id = ce.doc_id
                    WHERE ce.doc_id = ?
                    ORDER BY ce.chunk_id
                    LIMIT 1
                    """,
                    (result_id,),
                )
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
            "privacy_level": row["privacy_level"] or "safe",
            "page_type": row["page_type"],
            "page_status": row["page_status"] or "candidate",
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

        if principal is not None:
            ws = workspace or getattr(principal, "workspace_id", "default")
            if not can_read_document(principal, ws, raw):
                return None
            txt = str(raw.get("full_document_text") or raw.get("chunk_text") or "")
            raw["instruction_like"] = bool(is_instruction_like(txt))
            attribution = self._score_attribution(claim, txt)
            if attribution is not None:
                raw.update(attribution)
            raw["visibility"] = "authorized-via-expand"
            raw["reasoning"] = (
                f"expand can_read_document passed for {getattr(principal,'agent_id','?')}"
            )
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
                raw["full_provenance"] = {**existing_full}
                if raw.get("attribution_score") is not None:
                    raw["full_provenance"].update({
                        "attribution": raw.get("attribution"),
                        "attribution_score": raw.get("attribution_score"),
                        "attribution_model": raw.get("attribution_model"),
                    })
            if "chunk_text" in raw:
                raw["chunk_text"] = raw["evidence_envelope"]
            if "full_document_text" in raw:
                raw["full_document_text"] = raw["evidence_envelope"]

        return self._project_depth(raw, depth)

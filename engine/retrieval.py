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
import logging
import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, List, Dict, Literal, Optional, Sequence, Tuple

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG, correction_class_page_types
from db import SovereignDB
from faiss_index import FAISSIndex
from query_expand import expand as expand_query
from query_expand import summarize_with_afm

# G19/G20/G22: read gate + evidence envelopes
from principal import EffectivePrincipal, agent_scope_for, can_read_document  # type: ignore
from safety import is_instruction_like

logger = logging.getLogger("sovereign.retrieval")

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
        self.last_trace_id: Optional[str] = None
        # recall-F3: the correction-class type set is config-invariant — compute
        # it once here instead of once per scored doc in _score_merged_doc.
        self._correction_types = _correction_class_page_types(config)

    @property
    def model(self):
        """Return the process-wide embedding model singleton."""
        from models import get_embedder
        return get_embedder()

    @property
    def reranker(self):
        """Return the process-wide cross-encoder singleton."""
        if self._reranker is not None:
            return self._reranker
        from models import get_cross_encoder
        self._reranker = get_cross_encoder()
        return self._reranker

    @property
    def attribution_model(self):
        """Return the process-wide NLI cross-encoder singleton."""
        if self._attribution_model is not None:
            return self._attribution_model
        from models import get_attribution_cross_encoder
        self._attribution_model = get_attribution_cross_encoder()
        return self._attribution_model

    def _score_attribution(self, claim: Optional[str], evidence_text: str) -> Optional[Dict]:
        """Score whether evidence supports a caller-supplied claim using local NLI."""
        claim_text = str(claim or "").strip()
        if not claim_text:
            return None
        if not getattr(self.config, "attribution_enabled", True):
            return None
        model = self.attribution_model
        if model is None:
            return None
        try:
            raw = model.predict([(str(evidence_text or ""), claim_text)])
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
    ) -> List[Dict]:
        """FTS5 search using BM25 ranking."""
        results = []
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return results
        agent_scope = self._normalize_agent_filter(agent_filter)

        with self.db.cursor() as c:
            agent_clause = ""
            params: list = [safe_query]
            if agent_scope:
                agent_clause = f" AND d.agent IN ({','.join('?' * len(agent_scope))})"
                params.extend(agent_scope)
            params.append(limit * 3)
            c.execute(f"""
                SELECT f.doc_id, d.path, d.agent, d.sigil,
                       rank AS bm25_rank, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at, d.layer
                FROM vault_fts f
                JOIN documents d ON d.doc_id = f.doc_id
                WHERE vault_fts MATCH ?
                {agent_clause}
                ORDER BY rank
                LIMIT ?
            """, params)

            for row in c.fetchall():
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
        if not self.model:
            return []

        query_emb = self.model.encode(query).astype(np.float32)

        # Search FAISS for top candidates
        search_limit = limit * 5  # Over-fetch for doc dedup
        faiss_results = self.faiss_index.search(query_emb, top_k=search_limit)

        if not faiss_results:
            # Fallback: build index from DB if empty
            self._ensure_faiss_loaded()
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
        if self.faiss_index.count > 0:
            return

        # PR-2: Try disk cache first
        try:
            conn = self.db._get_conn()
            if self.faiss_index.try_load_from_disk(db_conn=conn):
                return
        except Exception as e:
            logger.debug("Disk cache load failed (non-fatal): %s", e)

        # Rebuild from DB
        chunk_ids = []
        embeddings = []

        with self.db.cursor() as c:
            c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
            for row in c.fetchall():
                vec = np.frombuffer(row["embedding"], dtype=np.float32)
                if vec.shape[0] == self.config.embedding_dim:
                    chunk_ids.append(row["chunk_id"])
                    embeddings.append(vec)

        if chunk_ids:
            all_vecs = np.array(embeddings, dtype=np.float32)
            self.faiss_index.build_from_vectors(chunk_ids, all_vecs)
            logger.info("FAISS index loaded from DB: %d vectors", len(chunk_ids))

            # PR-2: Save to disk for next cold start
            try:
                conn = self.db._get_conn()
                self.faiss_index.save_to_disk(db_conn=conn)
            except Exception as e:
                logger.debug("FAISS disk save failed (non-fatal): %s", e)

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
            from chunker import MarkdownChunker
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
                for chunk in chunks:
                    try:
                        emb = self.model.encode(chunk.text).astype(np.float32)
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
                c.execute(
                    "SELECT doc_id FROM documents WHERE path = ?", (path,)
                )
                row = c.fetchone()
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
                "store succeeded, recall degraded to lexical until reindex",
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
            from rerank_cache import invalidate_chunks
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
            if self.faiss_index.count > 0:
                for cid in chunk_ids:
                    self.faiss_index.remove(cid)
        except Exception as exc:
            logger.debug("durable-index: live FAISS remove skipped: %s", exc)

    def _refresh_live_faiss(
        self, chunk_ids: List[int], vectors: List[np.ndarray]
    ) -> None:
        """Make new chunks searchable in the live FAISS index without restart.

        Warm index → add the new vectors directly (cheap, immediate). Cold index
        → leave it cold; the next search's _ensure_faiss_loaded rebuilds from the
        DB, which now contains these rows. Failures here are non-fatal: the rows
        are durably in chunk_embeddings, so a later search/rebuild still finds
        them.
        """
        if not chunk_ids:
            return
        try:
            if self.faiss_index.count > 0:
                for cid, vec in zip(chunk_ids, vectors):
                    self.faiss_index.add(cid, vec)
        except Exception as exc:
            logger.warning(
                "durable-index: live FAISS refresh failed (%s) — invalidating "
                "so next search reloads from DB", exc,
            )
            # Force a cold reload on the next search so the new DB rows are
            # picked up even if the in-place add path failed. Clearing the
            # in-memory state drops count to 0, which makes the next search's
            # _ensure_faiss_loaded rebuild the full set from chunk_embeddings
            # (now including these rows).
            try:
                fi = self.faiss_index
                fi._chunk_ids = []
                fi._vectors = []
                fi._id_map = {}
                fi._reverse_map = {}
                fi._index = None
            except Exception:
                pass

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
            from rerank_cache import GLOBAL_RERANK_CACHE
            cache = GLOBAL_RERANK_CACHE
        except Exception:
            cache = None

        missing = []
        missing_indexes = []
        all_scores = [None] * len(candidates)
        if cache is not None:
            for i, c in enumerate(candidates):
                chunk_id = c.get("chunk_id")
                if chunk_id is None:
                    missing.append(c)
                    missing_indexes.append(i)
                    continue
                cached = cache.get(model_name, model_version, query, int(chunk_id))
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
            self._apply_correction_rerank_boost(candidates)
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            return candidates

        # Prepare pairs for the cross-encoder
        pairs = []
        for c in missing:
            passage = c.get("chunk_text", "")
            heading = c.get("heading_context", "")
            # Prepend heading context so the re-ranker knows the section
            if heading:
                passage = f"[{heading}] {passage}"
            pairs.append([query, passage])

        try:
            scores = reranker.predict(pairs)

            for score_index, c, score_value in zip(missing_indexes, missing, scores):
                score = float(score_value)
                all_scores[score_index] = score
                chunk_id = c.get("chunk_id")
                if cache is not None and chunk_id is not None:
                    cache.set(model_name, model_version, query, int(chunk_id), score)

            for i, c in enumerate(candidates):
                c["rerank_score"] = float(all_scores[i] or 0.0)

            # Sort by cross-encoder score (correction salience applied first)
            self._apply_correction_rerank_boost(candidates)
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        except Exception as e:
            logger.warning("Re-ranking failed: %s — falling back to RRF scores", e)

        return candidates

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
            # Carry forward chunk_text from semantic results (FTS doesn't have it)
            if r.get("chunk_text") and not doc_scores[did].get("chunk_text"):
                doc_scores[did]["chunk_text"] = r["chunk_text"]
                doc_scores[did]["heading_context"] = r.get("heading_context", "")
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
                "age_days": result.get("age_days"),
                "layer": result.get("layer"),
                "decay_factor": result.get("decay_score") or result.get("decay_factor"),
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
                "cross_encoder_score": result.get("rerank_score"),
                "decay_factor": result.get("decay_score"),
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

    def _fetch_full_document(self, doc_id: int) -> Optional[str]:
        """Fetch the full concatenated text for a whole_document row."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT chunk_text FROM chunk_embeddings
                WHERE doc_id = ?
                ORDER BY chunk_id
            """, (doc_id,))
            rows = c.fetchall()
        if not rows:
            return None
        return "\n".join(row["chunk_text"] for row in rows)

    # ── Public API ─────────────────────────────────────────────

    # R9: fan-out guard. A wire-supplied backend list of ["faiss-disk"]*N would
    # build N disk-loading backends; dedup and cap the list, and reject unknown
    # members loudly instead of silently skipping (a silent skip hid typos and
    # let an attacker probe backend names).
    _KNOWN_BACKENDS = ("faiss-disk", "faiss-mem", "qdrant", "lance")
    _MAX_BACKENDS = 4

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
                from backends.faiss_disk import FaissDiskBackend
                b = FaissDiskBackend(self.config, self.db)
                resolved.append(b)
            elif name == "faiss-mem":
                from backends.faiss_mem import FaissMemBackend
                b = FaissMemBackend(self.config)
                resolved.append(b)
            elif name == "qdrant":
                from backends.qdrant import QdrantBackend
                b = QdrantBackend(self.config)  # raises ImportError if not installed
                resolved.append(b)
            elif name == "lance":
                from backends.lance import LanceBackend
                b = LanceBackend(self.config)  # raises ImportError if not installed
                resolved.append(b)
        return resolved

    def _hits_to_dicts(self, hits, query_emb: np.ndarray) -> List[Dict]:
        """
        Convert VectorHit list to the dict format used by _rrf_merge.

        Fetches chunk metadata from SQLite to fill path, agent, etc.
        """
        from vector_backend import VectorHit
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
                if r.get("chunk_text") and not doc_scores[did].get("chunk_text"):
                    doc_scores[did]["chunk_text"] = r["chunk_text"]
                    doc_scores[did]["heading_context"] = r.get("heading_context", "")
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
            self._score_merged_doc(d)

        ranked = sorted(doc_scores.values(), key=lambda x: x["final_score"], reverse=True)
        return ranked[:limit]

    def _normalize_layers(self, layers: Optional[Sequence[str]]) -> Optional[set]:
        if layers is None:
            return None
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
        if not layer_set and start_ts is None and end_ts is None:
            return candidates
        filtered = []
        for r in candidates:
            if layer_set and (r.get("layer") or "knowledge") not in layer_set:
                continue
            created_at = r.get("created_at") or r.get("indexed_at") or 0
            if start_ts is not None and float(created_at or 0) < start_ts:
                continue
            if end_ts is not None and float(created_at or 0) > end_ts:
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
    ) -> List[Dict]:
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return []
        layer_set = self._normalize_layers(layers)
        start_ts = self._parse_iso_date(start_date)
        end_ts = self._parse_iso_date(end_date, end_of_day=True)
        params = [safe_query]
        clauses = ["vault_fts MATCH ?"]
        if layer_set:
            placeholders = ",".join("?" * len(layer_set))
            clauses.append(f"COALESCE(ce.layer, d.layer, 'knowledge') IN ({placeholders})")
            params.extend(sorted(layer_set))
        if start_ts is not None:
            clauses.append("COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) <= ?")
            params.append(end_ts)
        params.append(limit)

        with self.db.cursor() as c:
            c.execute(f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer,
                       COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) AS created_at
                FROM vault_fts f
                JOIN documents d ON d.doc_id = f.doc_id
                JOIN chunk_embeddings ce ON ce.doc_id = d.doc_id
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at ASC, ce.chunk_id ASC
                LIMIT ?
            """, params)
            rows = c.fetchall()

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
                "chunk_text": row["chunk_text"],
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
            variants = expand_query(query, mode=str(mode).lower())
        except Exception as exc:  # noqa: BLE001 - recall must not raise here
            logger.warning("Query expansion failed: %s — using original query", exc)
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
                    # R4: neighborhood summaries are a read surface — gate every
                    # linked doc through can_read_document so restricted/foreign/
                    # blocked memory never leaks via the wikilink graph. When no
                    # principal is supplied (legacy back-compat), fall through
                    # ungated as before.
                    if principal is not None:
                        raw = {
                            "doc_id": row["doc_id"],
                            "path": row["path"],
                            "agent": row["agent"],
                            "privacy_level": row["privacy_level"],
                            "page_type": row["page_type"],
                            "page_status": row["page_status"],
                        }
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
            summary = summarize_with_afm(prompt) if contexts else None
            result["neighborhood_summary"] = {
                "status": "ok" if summary else "unavailable",
                "summary": summary,
                "links": links,
                "contexts": [
                    {"link": ctx["link"], "doc_id": ctx["doc_id"], "path": ctx["path"]}
                    for ctx in contexts
                ],
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

        Returns list of ranked results filtered to the requested depth tier.
        Existing callers that pass no depth receive identical results (snippet).
        With backend=None (default), results are bit-identical to pre-PR-3.
        """
        claim_text = str(claim or "").strip()
        query_variants = self._resolve_query_variants(query, expand)
        if len(query_variants) > 1:
            total_t0 = time.perf_counter()
            per_variant = []
            for variant in query_variants:
                per_variant.append(self.retrieve(
                    query=variant,
                    limit=limit,
                    agent_id=agent_id,
                    update_access=False,
                    budget_tokens=budget_tokens,
                    depth=depth,
                    include_superseded=include_superseded,
                    include_rejected=include_rejected,
                    include_drafts=include_drafts,
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
                ))
            results = self._merge_expanded_results(per_variant, query_variants, limit)
            if summarize_neighborhood:
                results = self._add_neighborhood_summaries(
                    results, principal=principal, workspace=workspace
                )
            if update_access:
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
                    "variants": query_variants,
                    "expansion": {"mode": expand, "variant_count": len(query_variants)},
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
                self.last_trace_id = _trace_ring().add(
                    expanded_trace, owner=getattr(principal, "agent_id", None)
                )
                for result in results:
                    result["trace_id"] = self.last_trace_id
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

        rerank_k = self.config.reranker_top_k if self.config.reranker_enabled else limit

        if sort == "chronological":
            chrono_t0 = time.perf_counter()
            merged = self._chronological_search(query, rerank_k, layers, start_date, end_date)
            timing["semantic_ms"] = round((time.perf_counter() - chrono_t0) * 1000, 3)
            trace["backends"] = ["chronological-sql"]
            merged = merged[:limit]
        else:
            # Step 1-2: Dual retrieval
            fts_t0 = time.perf_counter()
            if document_agent_filter is None:
                fts_results = self._fts_search(query, rerank_k)
            else:
                fts_results = self._fts_search(
                    query, rerank_k, agent_filter=document_agent_filter
                )
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
            if backend is None and not fts_results and self._chunk_index_empty():
                semantic_results = []
                trace["backends"] = ["faiss-disk-empty"]
            elif backend is None:
                # Default path — bit-identical to pre-PR-3
                if document_agent_filter is None:
                    semantic_results = self._semantic_search(query, rerank_k)
                else:
                    semantic_results = self._semantic_search(
                        query, rerank_k, agent_filter=document_agent_filter
                    )
                trace["backends"] = ["faiss-disk"]
            elif isinstance(backend, list):
                # Fan-out: build a MultiBackend from the list of backend names/objects
                from backends.multi import MultiBackend
                resolved = self._resolve_backends(backend)
                if len(resolved) == 1:
                    semantic_results = self._backend_search(
                        self.model.encode(query).astype(np.float32) if self.model else np.array([]),
                        rerank_k,
                        resolved[0],
                    )
                else:
                    multi = MultiBackend(resolved)
                    query_emb = self.model.encode(query).astype(np.float32) if self.model else np.array([])
                    hits = multi.search(query_emb, k=rerank_k)
                    # Convert VectorHit list to the dict format expected by _rrf_merge
                    semantic_results = self._hits_to_dicts(hits, query_emb)
                trace["backends"] = [getattr(b, "name", str(b)) for b in resolved]
            else:
                # Single explicit backend object
                query_emb = self.model.encode(query).astype(np.float32) if self.model else np.array([])
                semantic_results = self._backend_search(query_emb, rerank_k, backend)
                trace["backends"] = [getattr(backend, "name", "custom")]
            timing["semantic_ms"] = round((time.perf_counter() - semantic_t0) * 1000, 3)
            timing["embedding_ms"] = timing["semantic_ms"]
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

            merged = self._filter_candidates(merged, layers, start_date, end_date)

            # Step 4: Cross-encoder re-rank
            if merged and self.config.reranker_enabled and self.reranker:
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

            # PR-8: HyDE cold-query second pass. This runs at most once and
            # gracefully returns the original pass if AFM is unavailable.
            hyde_enabled = self.config.hyde_enabled if use_hyde is None else bool(use_hyde)
            if hyde_enabled:
                try:
                    from hyde import (
                        generate_hypothetical_answer,
                        merge_hyde_results,
                        should_trigger_hyde,
                    )
                    from scoring import compute_confidence

                    probe_results = []
                    for r in merged[:limit]:
                        probe = dict(r)
                        probe["confidence"] = compute_confidence(
                            rrf_score=r.get("rrf_score"),
                            cross_encoder_score=r.get("rerank_score"),
                            decay_factor=r.get("decay_score"),
                            db=self.db,
                        )
                        probe_results.append(probe)

                    if should_trigger_hyde(
                        probe_results,
                        enabled=True,
                        floor=self.config.hyde_confidence_floor,
                    ):
                        trace["hyde"]["triggered"] = True
                        trace["hyde"]["confidence_floor"] = self.config.hyde_confidence_floor
                        hypothetical = generate_hypothetical_answer(query, config=self.config)
                        if hypothetical:
                            trace["hyde"]["hypothetical_chars"] = len(hypothetical)
                            if document_agent_filter is None:
                                hyde_fts = self._fts_search(hypothetical, rerank_k)
                                hyde_semantic = self._semantic_search(hypothetical, rerank_k)
                            else:
                                hyde_fts = self._fts_search(
                                    hypothetical, rerank_k, agent_filter=document_agent_filter
                                )
                                hyde_semantic = self._semantic_search(
                                    hypothetical, rerank_k, agent_filter=document_agent_filter
                                )
                            hyde_merged = self._rrf_merge(hyde_fts, hyde_semantic, rerank_k)
                            hyde_merged = self._filter_candidates(
                                hyde_merged, layers, start_date, end_date
                            )
                            if self.config.reranker_enabled and self.reranker:
                                hyde_merged = self._rerank(query, hyde_merged)
                                # S-1 fix (HyDE branch): same max() guard as the
                                # main rerank path — limit must not be capped
                                # below the caller's requested count.
                                hyde_merged = hyde_merged[:max(self.config.reranker_final_k, limit)]
                            else:
                                hyde_merged = hyde_merged[:limit]
                            merged = merge_hyde_results(
                                merged,
                                hyde_merged,
                                limit=rerank_k,
                                rrf_k=self.config.rrf_k,
                            )[:limit]
                            trace["hyde"]["result_doc_ids"] = [
                                r.get("doc_id") for r in hyde_merged
                            ]
                        else:
                            trace["hyde"]["skipped"] = "afm_unavailable"
                except Exception as exc:  # noqa: BLE001 - recall must not stack trace.
                    logger.debug("HyDE skipped after retrieval pass: %s", exc)
                    trace["hyde"]["skipped"] = "error"

        # G19 gate (below, after status filter) is the single source of truth for visibility.
        # The prior ad-hoc "agent_id or unknown" filter is removed; legacy principal=None
        # paths get only the status/privacy filter (pre-G19 back-compat shape preserved for
        # callers that never passed principal).

        merged = self._apply_feedback_demotions(merged, query, agent_id)

        # PR-2: Status lifecycle filtering
        # default: skip superseded, rejected, draft, expired
        # callers can opt back in with include_* kwargs
        _ALWAYS_EXCLUDED = {"blocked"}  # privacy_level=blocked is always excluded
        _SKIP_STATUSES = set()
        if not include_superseded:
            _SKIP_STATUSES.add("superseded")
        if not include_rejected:
            _SKIP_STATUSES.add("rejected")
        if not include_drafts:
            _SKIP_STATUSES.add("draft")
            _SKIP_STATUSES.add("expired")

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
        if principal is not None:
            merged = [r for r in merged if can_read_document(principal, ws, r)]

        # G22: attach evidence-only envelope + instruction_like + provenance/reasoning to every result
        # Model-facing content is always wrapped; raw executable instructions never treated as policy.
        for r in merged:
            txt = str(r.get("chunk_text") or r.get("full_document_text") or r.get("content") or "")
            r["instruction_like"] = bool(is_instruction_like(txt))
            attribution = self._score_attribution(claim_text, txt) if claim_text else None
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
        from scoring import compute_confidence
        from rationale import explain

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

            # Compute age_days from indexed_at
            indexed_at = r.get("indexed_at")
            age_days: Optional[float] = None
            if indexed_at:
                age_days = round((time.time() - float(indexed_at)) / 86400.0, 1)

            # Compute confidence
            try:
                confidence = compute_confidence(
                    rrf_score=r.get("rrf_score"),
                    cross_encoder_score=r.get("rerank_score"),
                    decay_factor=r.get("decay_score"),
                    db=self.db,
                )
            except Exception:
                confidence = None

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
                "cross_encoder_score": r.get("rerank_score"),
                "decay_factor": r.get("decay_score"),
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
                "layer": r.get("layer", "knowledge"),
                "chunk_text": chunk_text,
                "heading_context": r.get("heading_context", ""),
                "token_count": r.get("token_count", 0),
                # PR-2 envelope fields
                "confidence": confidence,
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
                full_text = self._fetch_full_document(r["doc_id"])
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
                    if claim_text:
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
                else:
                    raw["full_document_text"] = full_text

            # S7: self-labeling recall package — primary (rank 1) vs related (2..N).
            # Rank is 1-based by position in the final results list (post-rerank order).
            _result_rank = len(results) + 1
            raw["match_kind"] = "primary" if _result_rank == 1 else "related"
            raw["related_rank"] = None if _result_rank == 1 else _result_rank - 1

            projected = self._apply_depth(raw, depth)
            projected["match_kind"] = raw["match_kind"]
            projected["related_rank"] = raw["related_rank"]
            projected["query_variants"] = query_variants
            results.append(projected)

            if update_access:
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
        try:
            self.last_trace_id = _trace_ring().add(
                trace, owner=getattr(principal, "agent_id", None)
            )
            for result in results:
                result["trace_id"] = self.last_trace_id
        except Exception as exc:
            logger.debug("trace capture failed: %s", exc)
            self.last_trace_id = None

        if summarize_neighborhood:
            results = self._add_neighborhood_summaries(
                results, principal=principal, workspace=workspace
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
    ) -> Optional[Dict]:
        """
        Re-fetch a specific result at a deeper depth tier.

        *result_id* may be either a chunk_id or a doc_id; this method tries
        chunk_id first, then falls back to doc_id.

        Args:
            result_id: chunk_id or doc_id from a prior search result.
            depth: Target depth tier ('chunk' or 'document'). Defaults to 'chunk'.
            update_access: Whether to bump access_count on the document.

        Returns:
            A result dict at the requested depth, or None if not found.
        """
        if depth not in _VALID_DEPTHS:
            depth = "chunk"

        import os

        row = None
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

        if row is None:
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
            raw["full_document_text"] = self._fetch_full_document(row["doc_id"])

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

        return self._apply_depth(raw, depth)

    def search_episodic(
        self,
        query: str,
        agent_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """Search episodic events via FTS5."""
        safe_q = self._sanitize_fts_query(query)
        if not safe_q:
            return []

        results = []
        with self.db.cursor() as c:
            if agent_id:
                c.execute("""
                    SELECT ef.event_id, ef.agent_id, ef.content, e.event_type,
                           e.task_id, e.thread_id, e.created_at
                    FROM episodic_fts ef
                    JOIN episodic_events e ON e.event_id = ef.event_id
                    WHERE episodic_fts MATCH ? AND ef.agent_id = ?
                    ORDER BY rank
                    LIMIT ?
                """, (safe_q, agent_id, limit))
            else:
                c.execute("""
                    SELECT ef.event_id, ef.agent_id, ef.content, e.event_type,
                           e.task_id, e.thread_id, e.created_at
                    FROM episodic_fts ef
                    JOIN episodic_events e ON e.event_id = ef.event_id
                    WHERE episodic_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (safe_q, limit))

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
    ) -> List[Dict]:
        """Search write-back learnings via FTS5.

        ``source`` labels the learning_reads rows written below, so callers
        (e.g. the daemon search RPC) can distinguish their read channel in
        diagnostics.
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

        results = []
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
                """, [safe_q, *scope, limit])
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
                """, (safe_q, limit))

            for row in c.fetchall():
                results.append(dict(row))

        # hooks-PL-2 leg (a): searching learnings IS reading them. Record
        # access + a learning_reads row so subscribe_contradictions can later
        # match a correction against this read (the search path previously
        # wrote no learning_reads at all — moved here from search_episodic,
        # where it crashed on a missing learning_id key).
        if results:
            now = time.time()
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
                    except Exception as exc:
                        # hooks-PL-5: never silently drop read tracking — a
                        # missing row here is exactly what makes stale_beliefs
                        # fire events:[] forever.
                        logger.warning(
                            "learning_reads insert failed for learning #%s: %s",
                            result.get("learning_id"), exc,
                        )

        return results

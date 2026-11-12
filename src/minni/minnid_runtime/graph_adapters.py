"""RetrievalEngine-bound closures for typed-graph prepare/repair (not activation).

Binds an explicit engine/database, canonical store identity, and principal
to the coordinator's prepare/repair callables. Phase A (embed/chunk/search/
classify) stays outside the write lock. The caller owns
``commit_prepared_learning`` and the governance transaction, including
candidate terminalization and review-fence updates.

Post-commit vector delivery decodes stored bytes to float32 copies, checks
id/vector count, dimension, and finiteness, then refreshes the live index.
It never runs while a write transaction is open. Refresh failure leaves
SQLite durable and invokes the EXISTING retry-token callback (the backfill
pending token in ``minnid``: keep a token, never a second vector queue).
``retry_requested`` is True only when that callback returned; a missing or
raising callback is an error result, not a thrown exception and not a
second queue.

This module is not wired into dispatch, governance, AFM, or backfill.
``config.graph_classification_enabled`` stays at its existing default (ON).
Do not flip it off because the AFM edge-inference gate is 2s.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from minni.config import normalize_workspace_scope

from minni.graph_candidates import REQUIRED_MEMORY_KIND, REQUIRED_PAGE_TYPE
from minni.graph_coordinator import (
    LearningFields,
    PrepareResult,
    RepairResult,
    prepare_learning_with_graph,
    repair_learning_projection,
)
from minni.principal import EffectivePrincipal, can_read_document

logger = logging.getLogger("sovereign.graph_adapters")

_CLOSED_STATUSES = frozenset({"draft", "expired", "rejected", "superseded"})
_SNAPSHOT_SAVEPOINT_PREFIX = "minni_graph_adapt_"

NEXT_INTEGRATION = (
    "Do not route production promotions from this module. Callsites "
    "(governance.store / resolve_candidate accept, handle_learn force, AFM "
    "consolidation) each: (1) GraphRuntimeAdapter.prepare OUTSIDE the lock; "
    "(2) BEGIN IMMEDIATE; caller revalidates/terminalizes candidate_packets "
    "and review-fence rows; commit_prepared_learning(cursor, payload); "
    "COMMIT or ROLLBACK; (3) deliver_postcommit_vectors AFTER commit, with "
    "on_refresh_retry bound to minnid._backfill_shared_refresh_pending[key] "
    "= object() — a retry token, never stashed vectors (retry rereads SQL). "
    "Muse owns that wiring. Classifier timeout stays EdgeClassifier's 2s "
    "local gate; do not add flags or silently set "
    "graph_classification_enabled=False."
)


@dataclass(frozen=True)
class VectorDeliveryResult:
    """Phase C outcome. ``ok`` means the live index accepted the batch.

    ``retry`` means SQLite rows stay durable and the existing retry-token
    callback accepted. ``error`` is a refusal (open transaction, store
    mismatch, invalid ids, missing/raising retry callback) — no refresh,
    and ``retry_requested`` is False unless the callback actually returned.
    """

    status: str  # "ok" | "retry" | "error"
    refreshed: bool = False
    retry_requested: bool = False
    error_code: Optional[str] = None
    error: Optional[str] = None


def canonical_store_id(db: Any) -> str:
    """PRAGMA database_list main file realpath — not ``config.db_path``."""
    getter = getattr(db, "_get_conn", None)
    if not callable(getter):
        raise ValueError("database has no _get_conn")
    conn = getter()
    rows = conn.execute("PRAGMA database_list").fetchall()
    file = None
    for row in rows:
        try:
            name, path = row["name"], row["file"]
        except (TypeError, KeyError, IndexError):
            name, path = row[1], row[2]
        if str(name) == "main":
            file = path
            break
    if not file:
        raise ValueError("expected a real on-disk main database")
    return os.path.realpath(str(file))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_unlocked(db: Any, label: str) -> None:
    getter = getattr(db, "_get_conn", None)
    if not callable(getter):
        return
    conn = getter()
    if getattr(conn, "in_transaction", False):
        raise RuntimeError(
            f"{label} must not run while a write transaction is open"
        )


def _documents_has_workspace_col(cursor: Any) -> bool:
    try:
        cols = {
            r["name"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
            for r in cursor.execute("PRAGMA table_info(documents)").fetchall()
        }
        return "workspace_id" in cols
    except Exception:
        return False


def _active_learning_category(cursor: Any, doc_id: int) -> Optional[str]:
    try:
        row = cursor.execute(
            """SELECT l.category
               FROM learning_documents ld
               JOIN learnings l ON l.learning_id = ld.learning_id
               WHERE ld.doc_id = ?
                 AND l.superseded_by IS NULL
                 AND (l.status IS NULL OR l.status NOT IN ('rejected', 'expired', 'superseded'))
               ORDER BY l.learning_id DESC
               LIMIT 1""",
            (doc_id,),
        ).fetchone()
        if row is not None:
            val = row["category"] if hasattr(row, "keys") else row[0]
            if val:
                return str(val)
    except Exception:
        return None
    return None


def _doc_metadata(row: Any) -> Dict[str, Any]:
    meta = {
        "doc_id": row["doc_id"],
        "path": row["path"],
        "agent": row["agent"],
        "privacy_level": row["privacy_level"],
        "page_status": row["page_status"],
        "page_type": row["page_type"],
        "memory_kind": row["memory_kind"],
    }
    try:
        if hasattr(row, "keys") and "workspace_id" in row.keys():
            meta["workspace_id"] = row["workspace_id"]
    except Exception:
        pass
    return meta


def _authorized(principal: EffectivePrincipal, workspace: str, metadata: Dict[str, Any]) -> bool:
    try:
        return bool(can_read_document(principal, workspace, metadata))
    except Exception:
        return False


def _eligible(metadata: Dict[str, Any]) -> bool:
    if metadata.get("privacy_level") == "blocked":
        return False
    if str(metadata.get("page_status") or "") in _CLOSED_STATUSES:
        return False
    if metadata.get("memory_kind") != REQUIRED_MEMORY_KIND:
        return False
    if str(metadata.get("page_type") or "").lower() != REQUIRED_PAGE_TYPE:
        return False
    return True


@contextmanager
def _read_snapshot(conn: Any) -> Iterator[Any]:
    """Read snapshot for auth then text. Never commits."""
    cur = conn.cursor()
    nested = bool(getattr(conn, "in_transaction", False))
    savepoint = _SNAPSHOT_SAVEPOINT_PREFIX + uuid.uuid4().hex
    created = False
    try:
        if nested:
            cur.execute(f"SAVEPOINT {savepoint}")
        else:
            cur.execute("BEGIN DEFERRED")
        created = True
        yield cur
    finally:
        try:
            if created:
                if nested:
                    sqlite3.Cursor.execute(cur, f"ROLLBACK TO SAVEPOINT {savepoint}")
                    sqlite3.Cursor.execute(cur, f"RELEASE SAVEPOINT {savepoint}")
                else:
                    conn.rollback()
        finally:
            cur.close()


class GraphRuntimeAdapter:
    """Reusable prepare/repair/Phase-C bindings for one engine + principal."""

    def __init__(
        self,
        engine: Any,
        principal: EffectivePrincipal,
        *,
        classifier: Any = None,
        on_refresh_retry: Optional[Callable[[], None]] = None,
    ) -> None:
        if not isinstance(principal, EffectivePrincipal):
            raise TypeError("principal must be EffectivePrincipal")
        db = getattr(engine, "db", None)
        if db is None:
            raise ValueError("engine must expose db")
        self.engine = engine
        self.principal = principal
        self.store_id = canonical_store_id(db)
        self.on_refresh_retry = on_refresh_retry
        if classifier is None:
            from minni.edge_classifier import EdgeClassifier

            # Existing local-only adapter; 2.0s is its AFM gate, not a new flag.
            classifier = EdgeClassifier()
        self._classifier = classifier

    def _bound_db(self) -> Any:
        db = getattr(self.engine, "db", None)
        if db is None:
            raise ValueError("engine has no db")
        actual = canonical_store_id(db)
        if actual != self.store_id:
            raise ValueError("engine database is not the bound store")
        return db

    @property
    def db(self) -> Any:
        return self._bound_db()

    @property
    def graph_enabled(self) -> bool:
        return bool(getattr(self.engine.config, "graph_classification_enabled", True))

    @property
    def embedding_dim(self) -> int:
        return int(getattr(self.engine.config, "embedding_dim", 384))

    def coordinator_hooks(self) -> Dict[str, Any]:
        """Keyword args for prepare_learning_with_graph / repair."""
        self._bound_db()
        return {
            "db": self.engine.db,
            "store_id": self.store_id,
            "principal": self.principal,
            "vault_path": str(self.engine.config.vault_path),
            "embedding_model": str(self.engine.config.embedding_model),
            "embed_text": self.embed_text,
            "chunk_texts": self.chunk_texts,
            "search_chunks": self.search_chunks,
            "get_metadata": self.get_metadata,
            "get_content": self.get_content,
            "classify": self.classify,
            "graph_enabled": self.graph_enabled,
            "workspace": self.principal.workspace_id,
        }

    def embed_text(self, text: str) -> bytes:
        """Bi-encoder contract: float32 bytes under the process embedder lock."""
        db = self._bound_db()
        _require_unlocked(db, "embed")
        model = self.engine.model
        if model is None:
            raise RuntimeError("embedder unavailable")
        from minni.models import get_embedder_lock

        with get_embedder_lock():
            raw = model.encode(text, show_progress_bar=False)
        arr = np.asarray(raw, dtype=np.float32).reshape(-1)
        if arr.size != self.embedding_dim or not np.isfinite(arr).all():
            raise RuntimeError("embedder produced invalid float32 vector")
        return np.ascontiguousarray(arr).tobytes()

    def chunk_texts(self, text: str) -> List[str]:
        """Same MarkdownChunker path as index_durable_document, plus short floor."""
        db = self._bound_db()
        _require_unlocked(db, "chunk")
        chunks = list(self.engine.chunker.chunk_document(text) or [])
        texts = [str(getattr(chunk, "text", "") or "") for chunk in chunks]
        texts = [t for t in texts if t.strip()]
        if not texts:
            stripped = (text or "").strip()
            return [stripped] if stripped else [text]
        return texts

    def _load_faiss_or_raise(self, db: Any) -> None:
        ensure = getattr(self.engine, "_ensure_faiss_loaded", None)
        if not callable(ensure):
            raise RuntimeError("engine has no _ensure_faiss_loaded")
        ensure()
        index = getattr(self.engine, "faiss_index", None)
        ready = bool(getattr(index, "ready", False))
        if ready:
            return
        conn = db._get_conn()
        row = conn.execute(
            "SELECT 1 FROM chunk_embeddings LIMIT 1"
        ).fetchone()
        if row is not None:
            raise RuntimeError(
                "faiss index not ready after loader; SQL vectors exist"
            )

    def search_chunks(self, vector: bytes, top_k: int) -> List[Dict[str, Any]]:
        """Load the live index, then FAISS hits + SQL doc_id join."""
        db = self._bound_db()
        _require_unlocked(db, "search")
        self._load_faiss_or_raise(db)
        if not vector:
            return []
        query = np.frombuffer(bytes(vector), dtype=np.float32).copy()
        if query.size != self.embedding_dim or not np.isfinite(query).all():
            return []
        hits = self.engine.faiss_index.search(query, top_k=int(top_k)) or []
        if not hits:
            return []
        chunk_ids = [int(cid) for cid, _score in hits]
        placeholders = ",".join("?" * len(chunk_ids))
        docs: Dict[int, int] = {}
        with db.cursor() as cursor:
            for row in cursor.execute(
                f"SELECT chunk_id, doc_id FROM chunk_embeddings"
                f" WHERE chunk_id IN ({placeholders})",
                chunk_ids,
            ).fetchall():
                docs[int(row["chunk_id"])] = int(row["doc_id"])
        out: List[Dict[str, Any]] = []
        for chunk_id, score in hits:
            doc_id = docs.get(int(chunk_id))
            if doc_id is None:
                continue
            cosine = float(score)
            if not math.isfinite(cosine):
                continue
            out.append({
                "store_id": self.store_id,
                "doc_id": doc_id,
                "chunk_id": int(chunk_id),
                "cosine": cosine,
            })
        return out

    def get_metadata(self, store_id: str, doc_id: int) -> Optional[Dict[str, Any]]:
        """Authorize the documents row before any vault_fts content read."""
        db = self._bound_db()
        if os.path.realpath(os.path.abspath(str(store_id))) != self.store_id:
            raise ValueError("get_metadata store_id does not identify this database")
        conn = db._get_conn()
        with _read_snapshot(conn) as cursor:
            has_ws = _documents_has_workspace_col(cursor)
            ws_col = ", workspace_id" if has_ws else ""
            doc = cursor.execute(
                f"SELECT doc_id, path, agent, privacy_level, page_status,"
                f" page_type, memory_kind{ws_col} FROM documents WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return None
            metadata = _doc_metadata(doc)
            if not _authorized(self.principal, self.principal.workspace_id, metadata):
                return None
            fts = cursor.execute(
                "SELECT content FROM vault_fts WHERE doc_id = ?", (doc_id,),
            ).fetchone()
            content = fts["content"] if fts else ""
            category = _active_learning_category(cursor, doc_id)
        out: Dict[str, Any] = {
            "store_id": self.store_id,
            "doc_id": metadata["doc_id"],
            "path": metadata["path"],
            "agent": metadata["agent"],
            "privacy_level": metadata["privacy_level"],
            "page_status": metadata["page_status"],
            "page_type": metadata["page_type"],
            "memory_kind": metadata["memory_kind"],
            "title": metadata["path"],
            "content_sha256": _sha256_text(content or ""),
        }
        if "workspace_id" in metadata:
            out["workspace_id"] = metadata["workspace_id"]
        if category:
            out["category"] = category
        return out

    def get_content(self, store_id: str, doc_id: int) -> Optional[Dict[str, Any]]:
        """Re-authorize and re-check eligibility in the same snapshot as text."""
        db = self._bound_db()
        if os.path.realpath(os.path.abspath(str(store_id))) != self.store_id:
            raise ValueError("get_content store_id does not identify this database")
        conn = db._get_conn()
        with _read_snapshot(conn) as cursor:
            has_ws = _documents_has_workspace_col(cursor)
            ws_col = ", workspace_id" if has_ws else ""
            doc = cursor.execute(
                f"SELECT doc_id, path, agent, privacy_level, page_status,"
                f" page_type, memory_kind{ws_col} FROM documents WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return None
            metadata = _doc_metadata(doc)
            if not _authorized(self.principal, self.principal.workspace_id, metadata):
                return None
            if not _eligible(metadata):
                return None
            fts = cursor.execute(
                "SELECT content FROM vault_fts WHERE doc_id = ?", (doc_id,),
            ).fetchone()
            if fts is None:
                return None
            content = fts["content"]
        return {
            "store_id": self.store_id,
            "doc_id": doc_id,
            "content": content,
        }

    def classify(self, source: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Any:
        db = self._bound_db()
        _require_unlocked(db, "classify")
        target = self._classifier
        method = getattr(target, "classify", None)
        if callable(method) and target is not method:
            return method(source, candidates)
        return target(source, candidates)

    def prepare(
        self,
        content: str,
        *,
        category: str = "general",
        learning_fields: Optional[LearningFields] = None,
    ) -> PrepareResult:
        """Phase A only. Caller owns the subsequent write transaction."""
        return prepare_learning_with_graph(
            content=content,
            category=category,
            learning_fields=learning_fields,
            **self.coordinator_hooks(),
        )

    def repair(self, learning_id: int) -> RepairResult:
        """Standing repair. Never mutates the committed learnings row."""
        hooks = self.coordinator_hooks()
        return repair_learning_projection(
            learning_id=int(learning_id),
            **hooks,
        )

    def deliver_postcommit_vectors(
        self,
        chunk_ids: Sequence[int],
        vector_bytes: Sequence[bytes],
    ) -> VectorDeliveryResult:
        """Phase C after the outer txn commits. Never called inside it."""
        try:
            db = self._bound_db()
        except ValueError as exc:
            return VectorDeliveryResult(
                status="error",
                error_code="store_binding_mismatch",
                error=str(exc),
            )
        getter = getattr(db, "_get_conn", None)
        conn = getter() if callable(getter) else None
        if conn is not None and getattr(conn, "in_transaction", False):
            return VectorDeliveryResult(
                status="error",
                error_code="refresh_in_transaction",
                error="post-commit FAISS refresh must not run under a write lock",
            )
        decoded, error_code, error = _decode_float32_copies(
            chunk_ids, vector_bytes, dim=self.embedding_dim,
        )
        if decoded is None:
            if error_code == "invalid_chunk_id":
                return VectorDeliveryResult(
                    status="error",
                    error_code=error_code,
                    error=error,
                )
            return self._retry_or_error(error_code, error)
        ids, arrays = decoded
        if not ids:
            return VectorDeliveryResult(status="ok", refreshed=True)
        try:
            ready = self.engine._refresh_live_faiss(ids, arrays)
        except Exception as exc:
            logger.warning("graph adapter: live FAISS refresh raised (%s)", exc)
            return self._retry_or_error("refresh_failed", str(exc))
        if ready is False:
            return self._retry_or_error(
                "index_cold", "live FAISS refresh left the index cold",
            )
        return VectorDeliveryResult(status="ok", refreshed=True)

    def _retry_or_error(
        self, error_code: Optional[str], error: Optional[str]
    ) -> VectorDeliveryResult:
        accepted, cb_code, cb_error = self._request_retry()
        if accepted:
            return VectorDeliveryResult(
                status="retry",
                retry_requested=True,
                error_code=error_code,
                error=error,
            )
        return VectorDeliveryResult(
            status="error",
            retry_requested=False,
            error_code=cb_code or error_code,
            error=cb_error or error,
        )

    def _request_retry(self) -> Tuple[bool, Optional[str], Optional[str]]:
        callback = self.on_refresh_retry
        if callback is None:
            logger.warning(
                "graph adapter: live-index retry needed but no on_refresh_retry "
                "callback is bound"
            )
            return False, "retry_callback_missing", (
                "no on_refresh_retry callback is bound"
            )
        try:
            callback()
        except Exception as exc:
            logger.warning(
                "graph adapter: on_refresh_retry raised (%s)", exc,
            )
            return False, "retry_callback_failed", str(exc)
        return True, None, None


def _decode_float32_copies(
    chunk_ids: Sequence[int],
    vector_bytes: Sequence[bytes],
    *,
    dim: int,
) -> Tuple[Optional[Tuple[List[int], List[np.ndarray]]], Optional[str], Optional[str]]:
    try:
        ids = [int(cid) for cid in chunk_ids]
    except (TypeError, ValueError) as exc:
        return None, "invalid_chunk_id", str(exc)
    blobs = list(vector_bytes)
    if len(ids) != len(blobs):
        return None, "count_mismatch", (
            f"chunk id count {len(ids)} != vector count {len(blobs)}"
        )
    arrays: List[np.ndarray] = []
    expected = int(dim)
    for index, blob in enumerate(blobs):
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            return None, "invalid_vector", f"vector {index} is not bytes"
        raw = bytes(blob)
        if len(raw) % 4 != 0:
            return None, "dimension_mismatch", (
                f"vector {index} length {len(raw)} is not a float32 payload"
            )
        arr = np.frombuffer(raw, dtype=np.float32).copy()
        if int(arr.size) != expected:
            return None, "dimension_mismatch", (
                f"vector {index} dim {arr.size} != {expected}"
            )
        if not np.isfinite(arr).all():
            return None, "non_finite", f"vector {index} contains NaN or Inf"
        arrays.append(arr)
    return (ids, arrays), None, None


@dataclass(frozen=True)
class PreparedGovernedPromotion:
    """Phase-A governed learning promotion bundle (prepared outside write lock)."""

    graph_principal: EffectivePrincipal
    adapter: Optional[GraphRuntimeAdapter] = None
    prepared: Any = None
    fallback_edges: Optional[Tuple[Any, Dict[str, str], Dict[int, Dict[str, Any]]]] = None


def _prepare_sql_lexical_edges(
    db: Any,
    *,
    graph_principal: EffectivePrincipal,
    content: str,
    category: str,
    workspace_id: str,
    vault_path: Optional[str],
) -> Optional[Tuple[Any, Dict[str, str], Dict[int, Dict[str, Any]]]]:
    """Build a bounded lexical shortlist + classify outside any write transaction.

    Covers embedder-less / mock-indexed stores and cold-index rows that have a
    canonical ``learning_documents`` node in SQLite without live FAISS vectors.
    """
    try:
        _require_unlocked(db, "lexical_prepare")
        store_id = canonical_store_id(db)
    except Exception:
        return None
    cfg = getattr(db, "config", None)
    if cfg is not None and not bool(getattr(cfg, "graph_classification_enabled", True)):
        return None
    from minni.durable_projection import durable_doc_path, durable_metadata
    from minni.edge_classifier import (
        EdgeClassifier,
        _ANCHORED_SUPERSEDE_PREFIX_RE,
        _INLINE_SUPERSEDES_WORD_RE,
        _content_and_subject_tokens,
    )
    from minni.graph_candidates import (
        CandidateDescriptor,
        MAX_CLASSIFIER_PAIRS,
        _provenance,
        _truncate_excerpt,
    )
    from minni.graph_commit import graph_node_schema_present
    from minni.graph_coordinator import _classify_validated

    src_tokens, _ = _content_and_subject_tokens(content or "")
    has_explicit_sup = bool(
        _ANCHORED_SUPERSEDE_PREFIX_RE.search(content or "")
        or _INLINE_SUPERSEDES_WORD_RE.search(content or "")
    )
    if not src_tokens and not has_explicit_sup:
        return None

    effective_vault = str(vault_path or getattr(cfg, "vault_path", "") or "")
    src_path = (
        durable_doc_path(graph_principal.agent_id, "", effective_vault, content)
        if effective_vault
        else ""
    )

    candidates_raw: List[Tuple[float, int, Dict[str, Any], str]] = []
    seen_docs: set[int] = set()
    try:
        with db.cursor() as c:
            if not graph_node_schema_present(c):
                return None
            has_ws = _documents_has_workspace_col(c)
            ws_col = ", d.workspace_id" if has_ws else ""
            rows = c.execute(
                f"""SELECT d.doc_id, d.path, d.agent, d.privacy_level,
                           d.page_status, d.page_type, d.memory_kind{ws_col},
                           fts.content AS fts_content,
                           l.learning_id, l.category, l.content AS l_content
                    FROM documents d
                    JOIN learning_documents ld ON ld.doc_id = d.doc_id
                    JOIN learnings l ON l.learning_id = ld.learning_id
                    LEFT JOIN vault_fts fts ON fts.doc_id = d.doc_id
                    WHERE d.memory_kind = 'learning'
                      AND LOWER(COALESCE(d.page_type, '')) = 'learning'
                      AND COALESCE(d.privacy_level, 'safe') != 'blocked'
                      AND COALESCE(d.page_status, 'accepted') NOT IN (
                          'draft', 'expired', 'rejected', 'superseded'
                      )
                      AND l.superseded_by IS NULL
                      AND (
                          l.status IS NULL
                          OR l.status NOT IN ('rejected', 'expired', 'superseded')
                      )
                    ORDER BY l.learning_id DESC
                    LIMIT 64"""
            ).fetchall()
    except Exception:
        return None

    norm_ws = normalize_workspace_scope(workspace_id, default="default")
    gate_ws = (
        "*"
        if (
            str(getattr(graph_principal, "workspace_id", "") or "") == "*"
            and norm_ws in ("default", "global", "*")
        )
        else str(getattr(graph_principal, "workspace_id", "") or norm_ws)
    )
    for row in rows:
        doc_id = int(row["doc_id"])
        if doc_id in seen_docs:
            continue
        if src_path and str(row["path"] or "") == src_path:
            continue
        meta = _doc_metadata(row)
        if not _authorized(graph_principal, gate_ws, meta):
            continue
        if not _eligible(meta):
            continue
        cand_text = str(row["fts_content"] or row["l_content"] or "").strip()
        if not cand_text:
            continue
        cand_tokens, _ = _content_and_subject_tokens(cand_text)
        union_n = len(src_tokens | cand_tokens)
        jacc = (len(src_tokens & cand_tokens) / union_n) if union_n > 0 else 0.0
        min_jacc = 0.08 if has_explicit_sup else 0.20
        if jacc < min_jacc:
            continue
        seen_docs.add(doc_id)
        candidates_raw.append((jacc, doc_id, meta, cand_text, str(row["category"] or "")))

    if not candidates_raw:
        return None
    candidates_raw.sort(key=lambda item: (-item[0], item[1]))
    top = candidates_raw[:MAX_CLASSIFIER_PAIRS]

    meta_src = durable_metadata(content)
    source_payload: Dict[str, Any] = {
        "content": content,
        "title": src_path or f"learning:{graph_principal.agent_id}",
        "metadata": {
            "store_id": store_id,
            "agent": graph_principal.agent_id,
            "privacy_level": meta_src["privacy_level"],
            "page_status": meta_src["page_status"],
            "page_type": meta_src["page_type"],
            "memory_kind": "learning",
            "path": src_path or f"/virtual/{graph_principal.agent_id}",
            "title": src_path or f"learning:{graph_principal.agent_id}",
            "content_sha256": _sha256_text(content),
            "workspace_id": norm_ws,
            "category": category,
        },
    }

    descriptors: Dict[str, CandidateDescriptor] = {}
    cand_payloads: List[Dict[str, Any]] = []
    snapshots: Dict[int, Dict[str, Any]] = {}
    for idx, (_jacc, doc_id, meta, cand_text, cand_cat) in enumerate(top):
        pair_id = f"lex_{idx}_{doc_id}"
        c_sha = _sha256_text(cand_text)
        t_meta: Dict[str, Any] = {
            "store_id": store_id,
            "doc_id": doc_id,
            "path": meta["path"],
            "agent": meta["agent"],
            "privacy_level": meta["privacy_level"],
            "page_status": meta["page_status"],
            "page_type": meta["page_type"],
            "memory_kind": meta["memory_kind"],
            "title": meta["path"],
            "content_sha256": c_sha,
        }
        if "workspace_id" in meta:
            t_meta["workspace_id"] = meta["workspace_id"]
        if cand_cat:
            t_meta["category"] = cand_cat
        excerpt, tokens, measured = _truncate_excerpt(cand_text)
        score = float(_jacc)
        prov = _provenance(store_id, doc_id, 0, score, t_meta, excerpt)
        desc = CandidateDescriptor(
            **prov,
            pair_id=pair_id,
            doc_id=doc_id,
            chunk_id=0,
            cosine=score,
            title=str(meta["path"]),
            status=str(meta["page_status"]),
            page_type=str(meta["page_type"]),
            excerpt=excerpt,
            excerpt_tokens=tokens,
            excerpt_measured=measured,
        )
        descriptors[pair_id] = desc
        snapshots[doc_id] = t_meta
        cand_payloads.append(
            {
                "pair_id": pair_id,
                "content": excerpt,
                "title": str(meta["path"]),
                "status": str(meta["page_status"]),
                "page_type": str(meta["page_type"]),
                "target_metadata": t_meta,
            }
        )

    try:
        classifier = EdgeClassifier()
        validated, info = _classify_validated(
            source_payload, cand_payloads, classifier.classify, descriptors
        )
    except Exception:
        return None
    if not validated:
        return None
    return (tuple(validated), dict(info), snapshots)


def prepare_governed_promotion(
    db: Any,
    *,
    author_agent: str,
    workspace_id: str,
    caller_principal: EffectivePrincipal,
    content: str,
    category: str,
    learning_fields: Optional[LearningFields] = None,
    vault_path: Optional[str] = None,
    resolve_retrieval_engine: Optional[Callable[[Any], Any]] = None,
    on_refresh_retry: Optional[Callable[[Any], None]] = None,
    allow_adapter: bool = True,
) -> PreparedGovernedPromotion:
    """Phase A helper for governed learning promotion (must run outside write txn)."""
    norm_ws = (
        "*"
        if str(workspace_id or "").strip() == "*"
        else normalize_workspace_scope(workspace_id, default="default")
    )
    caller_raw_ws = str(getattr(caller_principal, "workspace_id", None) or "default").strip() or "default"
    caller_ws = (
        "*"
        if caller_raw_ws == "*"
        else normalize_workspace_scope(caller_raw_ws, default="default")
    )
    if caller_principal is not None and caller_ws not in ("*", "default", "global"):
        graph_ws = caller_ws
    elif norm_ws in ("*", "default", "global"):
        graph_ws = "*"
    else:
        graph_ws = norm_ws

    if learning_fields is not None and learning_fields.workspace_id is not None:
        lf_ws = normalize_workspace_scope(learning_fields.workspace_id, default="default")
        if lf_ws != learning_fields.workspace_id:
            learning_fields = replace(learning_fields, workspace_id=lf_ws)

    caps = list(getattr(caller_principal, "capabilities", None) or ["read", "learn"])
    roots = list(getattr(caller_principal, "allowed_vault_roots", None) or [])
    graph_principal = EffectivePrincipal(
        agent_id=author_agent,
        workspace_id=graph_ws,
        transport=str(getattr(caller_principal, "transport", "uds") or "uds"),
        capabilities=caps,
        allowed_vault_roots=roots,
    )

    engine = None
    if allow_adapter and resolve_retrieval_engine is not None:
        try:
            engine = resolve_retrieval_engine(db)
        except Exception:
            engine = None

    if engine is not None:
        try:
            if canonical_store_id(getattr(engine, "db", None)) != canonical_store_id(db):
                engine = None
        except Exception:
            engine = None

    if engine is not None:
        retry_cb = (lambda: on_refresh_retry(db)) if on_refresh_retry is not None else None
        try:
            adapter = GraphRuntimeAdapter(
                engine, graph_principal, on_refresh_retry=retry_cb
            )
            prep = adapter.prepare(
                content, category=category, learning_fields=learning_fields
            )
            if prep.status == "ok" and prep.payload is not None:
                fallback = None
                if prep.payload.shortlist_no_pairs:
                    fallback = _prepare_sql_lexical_edges(
                        db,
                        graph_principal=graph_principal,
                        content=content,
                        category=category,
                        workspace_id=norm_ws,
                        vault_path=vault_path,
                    )
                return PreparedGovernedPromotion(
                    graph_principal=graph_principal,
                    adapter=adapter,
                    prepared=prep.payload,
                    fallback_edges=fallback,
                )
        except Exception as exc:
            logger.debug("prepare_governed_promotion: adapter prepare skipped (%s)", exc)

    fallback = _prepare_sql_lexical_edges(
        db,
        graph_principal=graph_principal,
        content=content,
        category=category,
        workspace_id=norm_ws,
        vault_path=vault_path,
    )
    return PreparedGovernedPromotion(
        graph_principal=graph_principal,
        adapter=None,
        prepared=None,
        fallback_edges=fallback,
    )


def apply_fallback_graph_edges(
    c: Any,
    fallback_edges: Optional[Tuple[Any, Dict[str, str], Dict[int, Dict[str, Any]]]],
    *,
    graph_principal: EffectivePrincipal,
    workspace_id: str,
    new_learning_id: int,
    new_doc_id: Optional[int],
    now: float,
) -> Tuple[Tuple[Any, ...], Tuple[int, ...]]:
    """Apply pre-classified lexical shortlist edges inside an open write txn."""
    if not fallback_edges or new_doc_id is None:
        return (), ()
    validated, info, snapshots = fallback_edges
    from minni.durable_projection import ACTIVE_LEARNING_SQL, CLOSED_PROJECTION_STATUSES
    from minni.graph_coordinator import (
        _apply_updates_supersession,
        _bind_edges,
        _run_id,
        _write_edges,
    )

    has_ws = _documents_has_workspace_col(c)
    ws_col = ", workspace_id" if has_ws else ""
    norm_ws = (
        "*"
        if str(workspace_id or "").strip() == "*"
        else normalize_workspace_scope(workspace_id, default="default")
    )
    gate_ws = (
        "*"
        if (
            str(getattr(graph_principal, "workspace_id", "") or "") == "*"
            and norm_ws in ("default", "global", "*")
        )
        else str(getattr(graph_principal, "workspace_id", "") or norm_ws)
    )
    still_valid = []
    for desc, label, direction, confidence in validated:
        if int(desc.doc_id) == int(new_doc_id):
            continue
        snap = snapshots.get(int(desc.doc_id))
        if snap is None:
            continue
        row = c.execute(
            f"SELECT doc_id, path, agent, privacy_level, page_status,"
            f" page_type, memory_kind{ws_col} FROM documents WHERE doc_id = ?",
            (int(desc.doc_id),),
        ).fetchone()
        if row is None:
            continue
        meta = _doc_metadata(row)
        if not _authorized(graph_principal, gate_ws, meta) or not _eligible(meta):
            continue
        if str(meta.get("page_status") or "") in CLOSED_PROJECTION_STATUSES:
            continue
        fts = c.execute(
            "SELECT content FROM vault_fts WHERE doc_id = ?", (int(desc.doc_id),)
        ).fetchone()
        if fts is not None:
            cur_text = str(fts["content"] or "")
        else:
            lrow = c.execute(
                f"""SELECT l.content FROM learning_documents ld
                    JOIN learnings l ON l.learning_id = ld.learning_id
                    WHERE ld.doc_id = ? AND {ACTIVE_LEARNING_SQL}
                    ORDER BY l.learning_id DESC LIMIT 1""",
                (int(desc.doc_id),),
            ).fetchone()
            if lrow is None:
                continue
            cur_text = str(lrow["content"] or "")
        if _sha256_text(cur_text) != desc.content_sha256:
            continue
        active_any = c.execute(
            f"""SELECT 1 FROM learning_documents ld
                JOIN learnings l ON l.learning_id = ld.learning_id
                WHERE ld.doc_id = ? AND {ACTIVE_LEARNING_SQL} LIMIT 1""",
            (int(desc.doc_id),),
        ).fetchone()
        if active_any is None:
            continue
        still_valid.append((desc, label, direction, confidence))

    if not still_valid:
        return (), ()
    bound = _bind_edges(still_valid, int(new_doc_id))
    committed: Tuple[Any, ...] = ()
    if bound:
        run = _run_id(
            str(info.get("model_id") or "deterministic-heuristic-v1"),
            str(info.get("prompt_version") or "edge_inference_v1"),
            str(info.get("evidence_hash") or ""),
        )
        committed, _ = _write_edges(
            c,
            bound,
            run_id=run,
            model_id=str(info.get("model_id") or "deterministic-heuristic-v1"),
            prompt_version=str(info.get("prompt_version") or "edge_inference_v1"),
            evidence={"evidence_hash": str(info.get("evidence_hash") or "")},
            now=now,
            new_learning_id=int(new_learning_id),
        )
    superseded, _ = _apply_updates_supersession(
        c,
        validated=still_valid,
        source_doc_id=int(new_doc_id),
        new_learning_id=int(new_learning_id),
        new_doc_id=int(new_doc_id),
        agent_id=graph_principal.agent_id,
        now=now,
        allow_retire=True,
    )
    return committed, superseded


def linked_learning_ids_for_edges(
    c: Any, edges: Sequence[Any], *, exclude_learning_id: int
) -> set[int]:
    """Return learning_ids mapped to endpoints of committed typed graph edges."""
    doc_ids: set[int] = set()
    for edge in edges or ():
        rel = str(getattr(edge, "relation_type", None) or getattr(edge, "label", "") or "").strip().lower()
        if rel and rel not in ("updates", "contradicts"):
            continue
        s_doc = getattr(edge, "source_doc_id", None)
        t_doc = getattr(edge, "target_doc_id", None)
        if s_doc is not None:
            doc_ids.add(int(s_doc))
        if t_doc is not None:
            doc_ids.add(int(t_doc))
    if not doc_ids:
        return set()
    placeholders = ",".join("?" * len(doc_ids))
    out: set[int] = set()
    try:
        rows = c.execute(
            f"SELECT learning_id FROM learning_documents WHERE doc_id IN ({placeholders})",
            tuple(sorted(doc_ids)),
        ).fetchall()
        for r in rows:
            lid = int(r["learning_id"] if hasattr(r, "keys") else r[0])
            if lid != int(exclude_learning_id):
                out.add(lid)
    except Exception:
        pass
    return out


def collect_superseded_purge_targets(
    c: Any, superseded_learning_ids: Sequence[int], default_agent: str
) -> List[Tuple[str, str]]:
    """Fetch (agent_id, content) pairs for graph-superseded learnings to purge vectors."""
    targets: List[Tuple[str, str]] = []
    seen: set[int] = set()
    for raw_lid in superseded_learning_ids or ():
        lid = int(raw_lid)
        if lid in seen:
            continue
        seen.add(lid)
        row = c.execute(
            "SELECT agent_id, content FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
        if row is None:
            continue
        ag = str((row["agent_id"] if hasattr(row, "keys") else row[0]) or default_agent)
        ct = str((row["content"] if hasattr(row, "keys") else row[1]) or "")
        if ct:
            targets.append((ag, ct))
    return targets


def finalize_governed_promotion(
    plan: PreparedGovernedPromotion,
    commit_res: Any,
    *,
    content: str,
    workspace_id: str,
) -> bool:
    """Phase C post-commit vector delivery, cold-start FAISS warmup, and trie update."""
    adapter = plan.adapter
    if adapter is None or commit_res is None:
        return False
    delivery = adapter.deliver_postcommit_vectors(
        commit_res.new_chunk_ids, commit_res.new_chunk_vectors
    )
    engine = adapter.engine
    if delivery.error_code == "index_cold":
        try:
            engine._set_current_deadline(None)
            engine._ensure_faiss_loaded()
        except Exception as exc:
            logger.debug("finalize_governed_promotion: cold FAISS load skipped (%s)", exc)
    try:
        from minni.durable_projection import durable_metadata

        meta = durable_metadata(content)
        prep = plan.prepared
        if prep is not None and hasattr(engine, "_index_doc_in_trie"):
            engine._index_doc_in_trie(
                doc_id=int(commit_res.doc_id),
                path=prep.path,
                agent=plan.graph_principal.agent_id,
                sigil=prep.sigil,
                decay_score=1.0,
                chunk_text=content,
                heading_context="",
                page_status=meta["page_status"],
                privacy_level=meta["privacy_level"],
                page_type=meta["page_type"],
                evidence_refs=None,
                indexed_at=prep.prepared_at,
                layer=prep.layer,
                workspace_id=workspace_id,
            )
        elig_cache = getattr(engine, "_foreign_vault_elig_cache", None)
        if isinstance(elig_cache, dict):
            elig_cache.clear()
    except Exception as exc:
        logger.debug("finalize_governed_promotion: trie/cache update skipped (%s)", exc)
    if not commit_res.new_chunk_ids:
        # Re-promotion sharing an existing canonical node: verify chunks exist.
        try:
            with adapter.db.cursor() as c:
                has_chunks = (
                    c.execute(
                        "SELECT 1 FROM chunk_embeddings WHERE doc_id = ? LIMIT 1",
                        (int(commit_res.doc_id),),
                    ).fetchone()
                    is not None
                )
            return has_chunks and delivery.status in ("ok", "retry")
        except Exception:
            return delivery.status == "ok"
    return delivery.status == "ok" or (
        delivery.error_code == "index_cold" and bool(getattr(engine.faiss_index, "ready", False))
    )


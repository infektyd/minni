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
import logging
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

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


def _doc_metadata(row: Any) -> Dict[str, Any]:
    return {
        "doc_id": row["doc_id"],
        "path": row["path"],
        "agent": row["agent"],
        "privacy_level": row["privacy_level"],
        "page_status": row["page_status"],
        "page_type": row["page_type"],
        "memory_kind": row["memory_kind"],
    }


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
            doc = cursor.execute(
                "SELECT doc_id, path, agent, privacy_level, page_status,"
                " page_type, memory_kind FROM documents WHERE doc_id = ?",
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
        return {
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

    def get_content(self, store_id: str, doc_id: int) -> Optional[Dict[str, Any]]:
        """Re-authorize and re-check eligibility in the same snapshot as text."""
        db = self._bound_db()
        if os.path.realpath(os.path.abspath(str(store_id))) != self.store_id:
            raise ValueError("get_content store_id does not identify this database")
        conn = db._get_conn()
        with _read_snapshot(conn) as cursor:
            doc = cursor.execute(
                "SELECT doc_id, path, agent, privacy_level, page_status,"
                " page_type, memory_kind FROM documents WHERE doc_id = ?",
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

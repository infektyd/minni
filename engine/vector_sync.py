"""
Sovereign Memory — SQLite ↔ VectorBackend Sync.

PR-3: Keeps each registered VectorBackend in sync with the chunk_embeddings
table in SQLite.

Sync strategy:
  - Incremental: reads chunk_embeddings rows WHERE rowid > last_synced_chunk_rowid.
  - Batched: processes up to batch_size rows per pass.
  - Per-backend independent: a slow backend does not block others.
  - Idempotent: safe to call multiple times; re-syncing the same rows is a no-op
    because upsert() replaces existing vectors.

Triggers:
  1. After indexer pass: VaultIndexer / WikiIndexer call sync_backend() when done.
  2. Daemon idle hook: every 30s if any backend has status='dirty'.
  3. CLI: python -m engine.sovereign_memory vectors --sync

Entry points:
  sync_backend(backend, db, config)       — sync one backend
  sync_all(backends, db, config)          — sync all backends (independent)
  mark_dirty(backend_name, db)            — flag a backend for idle re-sync
  get_backend_state(backend_name, db)     — query vector_backends row
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import numpy as np

from vector_backend import VectorItem

logger = logging.getLogger("sovereign.vector_sync")

# Default batch size for incremental sync
_DEFAULT_BATCH = 1000


def get_backend_state(backend_name: str, db) -> Dict:
    """
    Return the vector_backends row for *backend_name*, or default values.

    Returns dict with keys: name, last_synced_chunk_rowid, last_synced_at,
    vector_count, status.
    """
    with db.cursor() as c:
        c.execute(
            "SELECT name, last_synced_chunk_rowid, last_synced_at, "
            "       vector_count, status "
            "FROM vector_backends WHERE name = ?",
            (backend_name,),
        )
        row = c.fetchone()

    if row is None:
        return {
            "name": backend_name,
            "last_synced_chunk_rowid": 0,
            "last_synced_at": 0,
            "vector_count": 0,
            "status": "empty",
        }
    return dict(row)


def _upsert_backend_state(
    backend_name: str,
    db,
    *,
    last_synced_chunk_rowid: int,
    vector_count: int,
    status: str = "ok",
) -> None:
    """Insert or replace a vector_backends row."""
    now = int(time.time())
    with db.cursor() as c:
        c.execute(
            "INSERT INTO vector_backends "
            "(name, last_synced_chunk_rowid, last_synced_at, vector_count, status) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  last_synced_chunk_rowid = excluded.last_synced_chunk_rowid,"
            "  last_synced_at = excluded.last_synced_at,"
            "  vector_count = excluded.vector_count,"
            "  status = excluded.status",
            (backend_name, last_synced_chunk_rowid, now, vector_count, status),
        )


def mark_dirty(backend_name: str, db) -> None:
    """
    Mark a backend as dirty so the idle hook will re-sync it.

    Call this whenever chunk_embeddings rows are deleted (the incremental
    rowid approach only catches INSERTs, not DELETEs).
    """
    with db.cursor() as c:
        c.execute(
            "INSERT INTO vector_backends (name, status) VALUES (?, 'dirty') "
            "ON CONFLICT(name) DO UPDATE SET status = 'dirty'",
            (backend_name,),
        )


def sync_backend(
    backend,
    db,
    config=None,
    *,
    batch_size: int = _DEFAULT_BATCH,
    embedding_dim: Optional[int] = None,
    full_rebuild: bool = False,
) -> Dict:
    """
    Sync *backend* with chunk_embeddings in SQLite.

    Args:
        backend       : A VectorBackend instance.
        db            : SovereignDB instance.
        config        : SovereignConfig (for embedding_dim fallback).
        batch_size    : Rows to process per call.
        embedding_dim : Expected vector dim (auto-detected from backend.dim).
        full_rebuild  : If True, ignore last_synced_chunk_rowid and re-sync all.

    Returns:
        Dict with keys: backend, upserted, last_rowid, vector_count, status.
    """
    from config import DEFAULT_CONFIG
    cfg = config or DEFAULT_CONFIG
    dim = embedding_dim or backend.dim or cfg.embedding_dim

    state = get_backend_state(backend.name, db)
    start_rowid = 0 if full_rebuild else state["last_synced_chunk_rowid"]

    logger.debug(
        "Syncing backend %r: start_rowid=%d, batch=%d",
        backend.name, start_rowid, batch_size,
    )

    upserted = 0
    last_rowid = start_rowid

    with db.cursor() as c:
        c.execute(
            "SELECT rowid AS chunk_rowid, chunk_id, doc_id, embedding "
            "FROM chunk_embeddings "
            "WHERE rowid > ? "
            "ORDER BY rowid "
            "LIMIT ?",
            (start_rowid, batch_size),
        )
        rows = c.fetchall()

    if rows:
        items = []
        for row in rows:
            raw = row["embedding"]
            if raw is None:
                continue
            vec = np.frombuffer(raw, dtype=np.float32).copy()
            if vec.shape[0] != dim:
                logger.debug(
                    "Skipping chunk %d — wrong dim %d (expected %d)",
                    row["chunk_id"], vec.shape[0], dim,
                )
                continue
            items.append(VectorItem(
                chunk_id=row["chunk_id"],
                doc_id=row["doc_id"],
                vector=vec,
                metadata={},  # Full metadata fetched lazily if needed
            ))
            last_rowid = max(last_rowid, row["chunk_rowid"])

        if items:
            try:
                backend.upsert(items)
                upserted = len(items)
                logger.info(
                    "Synced %d vectors → backend %r (last_rowid=%d)",
                    upserted, backend.name, last_rowid,
                )
            except Exception as exc:
                logger.error(
                    "Backend %r upsert failed during sync: %s", backend.name, exc
                )
                _upsert_backend_state(
                    backend.name, db,
                    last_synced_chunk_rowid=start_rowid,  # Don't advance on failure
                    vector_count=state["vector_count"],
                    status="error",
                )
                return {
                    "backend": backend.name,
                    "upserted": 0,
                    "last_rowid": start_rowid,
                    "vector_count": state["vector_count"],
                    "status": "error",
                }

    # Count total vectors in the backend (use stats() if available)
    try:
        vector_count = backend.stats().get("vector_count", 0)
    except Exception:
        vector_count = state["vector_count"] + upserted

    _upsert_backend_state(
        backend.name, db,
        last_synced_chunk_rowid=last_rowid,
        vector_count=vector_count,
        status="ok",
    )

    return {
        "backend": backend.name,
        "upserted": upserted,
        "last_rowid": last_rowid,
        "vector_count": vector_count,
        "status": "ok",
    }


def sync_all(
    backends: List,
    db,
    config=None,
    *,
    batch_size: int = _DEFAULT_BATCH,
    full_rebuild: bool = False,
) -> List[Dict]:
    """
    Sync all backends independently.

    Each backend is synced in its own try/except so one failure does not
    prevent the others from syncing.

    Args:
        backends     : List of VectorBackend instances.
        db           : SovereignDB instance.
        config       : SovereignConfig.
        batch_size   : Rows per backend per call.
        full_rebuild : If True, re-sync all rows.

    Returns:
        List of sync result dicts, one per backend.
    """
    results = []
    for backend in backends:
        try:
            result = sync_backend(
                backend, db, config,
                batch_size=batch_size,
                full_rebuild=full_rebuild,
            )
            results.append(result)
        except Exception as exc:
            logger.error("sync_all: backend %r raised unexpectedly: %s", backend.name, exc)
            results.append({
                "backend": backend.name,
                "upserted": 0,
                "status": "error",
                "error": str(exc),
            })
    return results


def should_sync(backend_name: str, db) -> bool:
    """
    Return True if this backend needs a sync pass.

    Called by the daemon idle hook every 30s.
    Returns True if status == 'dirty' or 'empty' or 'error'.
    """
    state = get_backend_state(backend_name, db)
    return state["status"] in ("dirty", "empty", "error")

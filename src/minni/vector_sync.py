"""
Minni — SQLite ↔ VectorBackend Sync.

PR-3: Keeps each registered VectorBackend in sync with the chunk_embeddings
table in SQLite.

Sync strategy:
  - Incremental: reads chunk_embeddings rows WHERE chunk_id > last_synced_chunk_rowid.
  - Batched: processes up to batch_size rows per pass.
  - Per-backend independent: a slow backend does not block others.
  - Idempotent: safe to call multiple times; re-syncing the same rows is a no-op
    because upsert() replaces existing vectors.

Triggers:
  1. After indexer pass: VaultIndexer / WikiIndexer call sync_backend() when done.
  2. Daemon idle hook: every 30s if any backend has status='dirty'.
  3. CLI: minni vectors --sync

Entry points:
  sync_backend(backend, db, config)       — sync one backend
  sync_all(backends, db, config)          — sync all backends (independent)
  mark_dirty(backend_name, db)            — flag a backend for idle re-sync
  get_backend_state(backend_name, db)     — query vector_backends row
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Dict, List, Optional

import numpy as np

from minni.vector_backend import VectorItem

logger = logging.getLogger("sovereign.vector_sync")

# Default batch size for incremental sync
_DEFAULT_BATCH = 1000


def _ensure_dirty_seq_columns(c) -> bool:
    """Ensure dirty_seq and synced_dirty_seq exist on vector_backends (graceful on mock/legacy DBs)."""
    try:
        pragma = c.execute("PRAGMA table_info(vector_backends)").fetchall()
        cols = {
            row["name"] if isinstance(row, dict) or hasattr(row, "keys") else row[1]
            for row in pragma
        }
    except Exception:
        return False
    if not cols:
        return False
    if "dirty_seq" not in cols:
        try:
            c.execute(
                "ALTER TABLE vector_backends ADD COLUMN dirty_seq INTEGER NOT NULL DEFAULT 0"
            )
            cols.add("dirty_seq")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                cols.add("dirty_seq")
            else:
                return False
    if "synced_dirty_seq" not in cols:
        try:
            c.execute(
                "ALTER TABLE vector_backends ADD COLUMN synced_dirty_seq INTEGER NOT NULL DEFAULT 0"
            )
            cols.add("synced_dirty_seq")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                cols.add("synced_dirty_seq")
            else:
                return False
    return "dirty_seq" in cols and "synced_dirty_seq" in cols


def get_backend_state(backend_name: str, db) -> Dict:
    """
    Return the vector_backends row for *backend_name*, or default values.

    Returns dict with keys: name, last_synced_chunk_rowid, last_synced_at,
    vector_count, status, dirty_seq, synced_dirty_seq.
    """
    with db.cursor() as c:
        c.execute(
            "SELECT * FROM vector_backends WHERE name = ?",
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
            "dirty_seq": 0,
            "synced_dirty_seq": 0,
        }
    result = dict(row)
    result.setdefault("name", backend_name)
    result.setdefault("last_synced_chunk_rowid", 0)
    result.setdefault("last_synced_at", 0)
    result.setdefault("vector_count", 0)
    result.setdefault("status", "empty")
    result["dirty_seq"] = int(result.get("dirty_seq") or 0)
    result["synced_dirty_seq"] = int(result.get("synced_dirty_seq") or 0)
    return result


def _upsert_backend_state(
    backend_name: str,
    db,
    *,
    last_synced_chunk_rowid: int,
    vector_count: int,
    status: str = "ok",
    preserve_dirty: bool = False,
    cleanup_seq: Optional[int] = None,
) -> None:
    """Insert or update a vector_backends row."""
    now = int(time.time())
    with db.cursor() as c:
        has_seq = _ensure_dirty_seq_columns(c)
        if has_seq and cleanup_seq is not None:
            c.execute(
                "INSERT INTO vector_backends "
                "(name, last_synced_chunk_rowid, last_synced_at, vector_count, status, dirty_seq, synced_dirty_seq) "
                "VALUES (?, ?, ?, ?, ?, 0, 0) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  last_synced_chunk_rowid = excluded.last_synced_chunk_rowid,"
                "  last_synced_at = excluded.last_synced_at,"
                "  vector_count = excluded.vector_count,"
                "  status = CASE "
                "    WHEN vector_backends.status = 'dirty' OR vector_backends.dirty_seq > ? "
                "    THEN 'dirty' ELSE excluded.status END,"
                "  synced_dirty_seq = CASE "
                "    WHEN vector_backends.status = 'dirty' OR vector_backends.dirty_seq > ? "
                "    THEN vector_backends.synced_dirty_seq ELSE vector_backends.dirty_seq END",
                (
                    backend_name,
                    last_synced_chunk_rowid,
                    now,
                    vector_count,
                    status,
                    cleanup_seq,
                    cleanup_seq,
                ),
            )
            return

        status_expr = (
            "CASE WHEN vector_backends.status = 'dirty' THEN 'dirty' ELSE excluded.status END"
            if preserve_dirty
            else "excluded.status"
        )
        c.execute(
            "INSERT INTO vector_backends "
            "(name, last_synced_chunk_rowid, last_synced_at, vector_count, status) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "  last_synced_chunk_rowid = excluded.last_synced_chunk_rowid,"
            "  last_synced_at = excluded.last_synced_at,"
            "  vector_count = excluded.vector_count,"
            f"  status = {status_expr}",
            (backend_name, last_synced_chunk_rowid, now, vector_count, status),
        )


def _checkpoint_last_synced_rowid(
    backend_name: str,
    db,
    *,
    last_synced_chunk_rowid: int,
    fallback_vector_count: int,
) -> None:
    """Persist uploaded last_synced_chunk_rowid immediately after upsert succeeds
    without clearing dirty status or advancing synced_dirty_seq."""
    now = int(time.time())
    with db.cursor() as c:
        _ensure_dirty_seq_columns(c)
        c.execute(
            "INSERT INTO vector_backends "
            "(name, last_synced_chunk_rowid, last_synced_at, vector_count, status) "
            "VALUES (?, ?, ?, ?, 'syncing') "
            "ON CONFLICT(name) DO UPDATE SET "
            "  last_synced_chunk_rowid = CASE "
            "    WHEN excluded.last_synced_chunk_rowid > COALESCE(vector_backends.last_synced_chunk_rowid, 0) "
            "    THEN excluded.last_synced_chunk_rowid "
            "    ELSE vector_backends.last_synced_chunk_rowid END,"
            "  last_synced_at = excluded.last_synced_at",
            (backend_name, last_synced_chunk_rowid, now, fallback_vector_count),
        )


def mark_dirty(backend_name: str, db) -> None:
    """
    Mark a backend as dirty so the idle hook will re-sync it.

    Call this whenever chunk_embeddings rows are deleted (the incremental
    chunk_id cursor only catches INSERTs, not DELETEs). Note: Migration 027
    triggers also update vector_backends automatically on DELETE / UPDATE.
    """
    with db.cursor() as c:
        if _ensure_dirty_seq_columns(c):
            c.execute(
                "INSERT INTO vector_backends (name, status, dirty_seq, synced_dirty_seq) "
                "VALUES (?, 'dirty', 1, 0) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  status = 'dirty', "
                "  dirty_seq = vector_backends.dirty_seq + 1",
                (backend_name,),
            )
        else:
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
        db            : MinniDB instance.
        config        : MinniConfig (for embedding_dim fallback).
        batch_size    : Rows to process per call.
        embedding_dim : Expected vector dim (auto-detected from backend.dim).
        full_rebuild  : If True, ignore last_synced_chunk_rowid and re-sync all.

    Returns:
        Dict with keys: backend, upserted, last_rowid, vector_count, status.
    """
    from minni.config import DEFAULT_CONFIG
    cfg = config or DEFAULT_CONFIG
    dim = embedding_dim or backend.dim or cfg.embedding_dim

    state = get_backend_state(backend.name, db)
    cleanup_seq = int(state.get("dirty_seq", 0) or 0)
    was_dirty = (
        state["status"] in ("dirty", "error", "syncing")
        or cleanup_seq > int(state.get("synced_dirty_seq", 0) or 0)
    ) and not full_rebuild
    start_rowid = 0 if full_rebuild else state["last_synced_chunk_rowid"]

    logger.debug(
        "Syncing backend %r: start_rowid=%d, batch=%d",
        backend.name, start_rowid, batch_size,
    )

    upserted = 0
    last_rowid = start_rowid

    with db.cursor() as c:
        c.execute(
            # chunk_id is INTEGER PRIMARY KEY, a strict rowid alias in SQLite.
            # Use the named key so the sync cursor stays portable.
            "SELECT chunk_id AS chunk_rowid, chunk_id, doc_id, embedding "
            "FROM chunk_embeddings "
            "WHERE chunk_id > ? "
            "ORDER BY chunk_id "
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
                _checkpoint_last_synced_rowid(
                    backend.name,
                    db,
                    last_synced_chunk_rowid=max(state["last_synced_chunk_rowid"], last_rowid),
                    fallback_vector_count=state["vector_count"] + upserted,
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

    # Reconcile deletions. The incremental cursor above only catches INSERTs
    # (chunk_id > start_rowid); a deleted chunk leaves a stale vector in the
    # backend forever. On a dirty (post-delete), error-retry, or full_rebuild
    # pass — or if mark_dirty / a SQLite trigger arrived concurrently during
    # upsert — drop any vector whose chunk_id is no longer present in
    # chunk_embeddings.
    post_upsert_state = get_backend_state(backend.name, db)
    post_dirty_seq = int(post_upsert_state.get("dirty_seq", 0) or 0)
    post_synced_seq = int(post_upsert_state.get("synced_dirty_seq", 0) or 0)
    if (
        was_dirty
        or post_upsert_state["status"] == "dirty"
        or post_dirty_seq > post_synced_seq
        or full_rebuild
    ):
        with db.cursor() as c:
            has_seq = _ensure_dirty_seq_columns(c)
            if has_seq:
                seq_row = c.execute(
                    "SELECT dirty_seq FROM vector_backends WHERE name = ?",
                    (backend.name,),
                ).fetchone()
                if seq_row is not None:
                    cleanup_seq = int(
                        (
                            seq_row["dirty_seq"]
                            if isinstance(seq_row, dict) or hasattr(seq_row, "keys")
                            else seq_row[0]
                        )
                        or 0
                    )
            c.execute(
                "UPDATE vector_backends SET status = 'syncing' "
                "WHERE name = ? AND status = 'dirty'",
                (backend.name,),
            )
        prior_max = max(state["last_synced_chunk_rowid"], last_rowid)
        if prior_max > 0:
            with db.cursor() as c:
                c.execute(
                    "SELECT chunk_id FROM chunk_embeddings WHERE chunk_id <= ?",
                    (prior_max,),
                )
                existing = {row["chunk_id"] for row in c.fetchall()}
            to_remove = [cid for cid in range(1, prior_max + 1) if cid not in existing]
            if to_remove:
                try:
                    backend.remove(to_remove)
                    logger.info(
                        "Removed %d deleted vectors from backend %r",
                        len(to_remove), backend.name,
                    )
                except Exception as exc:
                    logger.error(
                        "Backend %r remove failed during dirty sync: %s",
                        backend.name, exc,
                    )
                    _upsert_backend_state(
                        backend.name, db,
                        last_synced_chunk_rowid=prior_max,
                        vector_count=state["vector_count"],
                        status="error",
                    )
                    return {
                        "backend": backend.name,
                        "upserted": upserted,
                        "last_rowid": last_rowid,
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
        last_synced_chunk_rowid=max(state["last_synced_chunk_rowid"], last_rowid),
        vector_count=vector_count,
        status="ok",
        preserve_dirty=True,
        cleanup_seq=cleanup_seq,
    )
    final_state = get_backend_state(backend.name, db)
    final_status = (
        "dirty"
        if (
            final_state["status"] == "dirty"
            or final_state.get("dirty_seq", 0) > final_state.get("synced_dirty_seq", 0)
        )
        else "ok"
    )

    return {
        "backend": backend.name,
        "upserted": upserted,
        "last_rowid": last_rowid,
        "vector_count": vector_count,
        "status": final_status,
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
        db           : MinniDB instance.
        config       : MinniConfig.
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
    Returns True if status == 'dirty', 'empty', 'error', or 'syncing', or if
    dirty_seq > synced_dirty_seq.
    """
    state = get_backend_state(backend_name, db)
    return (
        state["status"] in ("dirty", "empty", "error", "syncing")
        or state.get("dirty_seq", 0) > state.get("synced_dirty_seq", 0)
    )

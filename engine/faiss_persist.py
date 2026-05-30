"""
Sovereign Memory — FAISS Disk Persistence.

PR-2: Save/load FAISS index to/from disk with a manifest file.

The manifest captures:
  - embedding_model: which model produced the vectors
  - vector_dim: expected embedding dimension
  - embedding_quantization: FAISS-side quantization mode ("fp32" or "int8")
  - chunk_id_order: ordered list of chunk_ids (position → chunk_id)
  - chunk_count: total vectors
  - db_checksum: hash of live DB state (count, max rowid, max updated_at)
  - saved_at: ISO8601 timestamp

On cold start the loader checks:
  1. Manifest file exists
  2. db_checksum matches current DB
  3. FAISS index file exists and is loadable

On checksum mismatch → returns None (caller rebuilds and re-saves).

Default cache location: ${MINNI_DB_PATH%/*}/faiss/
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("sovereign.faiss_persist")


def _faiss_dir_for_db(db_path: str) -> str:
    """Return the default FAISS cache directory (sibling to the SQLite DB)."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "faiss")


def _index_path(manifest_path: str) -> str:
    """Derive the .faiss file path from the manifest path."""
    return manifest_path.replace(".manifest.json", ".faiss")


def compute_db_checksum(conn: sqlite3.Connection) -> str:
    """
    Compute a lightweight fingerprint of the chunk_embeddings table.

    Hash of: (row count, max rowid, max computed_at).
    If the table is empty or missing, returns 'empty'.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, MAX(rowid) as maxrow, MAX(computed_at) as maxts "
            "FROM chunk_embeddings"
        ).fetchone()
        if row is None or row[0] == 0:
            return "empty"
        checksum_input = f"{row[0]}:{row[1]}:{row[2]}"
        return hashlib.sha256(checksum_input.encode()).hexdigest()[:16]
    except Exception as e:
        logger.warning("Cannot compute DB checksum: %s", e)
        return "error"


def save(
    index,
    vectors: List[np.ndarray],
    chunk_ids: List[int],
    manifest_path: str,
    embedding_model: str,
    vector_dim: int,
    db_checksum: str,
    embedding_quantization: str = "fp32",
) -> bool:
    """
    Save a FAISS index + manifest to disk.

    Args:
        index: A faiss index object (or None if numpy fallback).
        vectors: Raw float32 vectors corresponding to chunk_ids (used when FAISS unavailable).
        chunk_ids: Ordered list of chunk_ids (index position → chunk_id).
        manifest_path: Full path to write the .manifest.json file.
                       The .faiss file is written alongside it.
        embedding_model: Model name string (recorded in manifest).
        vector_dim: Dimension of each vector.
        embedding_quantization: FAISS-side quantization mode. SQLite vectors
                                remain float32 regardless of this value.
        db_checksum: Pre-computed DB fingerprint.

    Returns:
        True on success, False on failure.
    """
    try:
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)

        faiss_path = _index_path(manifest_path)

        # Write FAISS index (if faiss available) or numpy backup
        if index is not None:
            try:
                import faiss
                if not hasattr(faiss, "write_index"):
                    raise AttributeError("faiss.write_index is unavailable")
                faiss.write_index(index, faiss_path)
            except Exception as e:
                logger.warning("Failed to write FAISS index: %s; trying numpy fallback", e)
                index = None

        if index is None:
            # numpy fallback — save as npz
            npz_path = faiss_path + ".npz"
            if vectors:
                arr = np.array(vectors, dtype=np.float32)
                np.savez_compressed(npz_path, vectors=arr, chunk_ids=np.array(chunk_ids))
            else:
                logger.warning("Cannot save numpy fallback: no raw vectors available")
                return False

        manifest = {
            "embedding_model": embedding_model,
            "vector_dim": vector_dim,
            "embedding_quantization": embedding_quantization,
            "chunk_id_order": chunk_ids,
            "chunk_count": len(chunk_ids),
            "db_checksum": db_checksum,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        logger.info(
            "FAISS index saved: %d vectors → %s (checksum=%s)",
            len(chunk_ids), faiss_path, db_checksum,
        )
        return True

    except Exception as e:
        logger.warning("FAISS save failed: %s", e)
        return False


def load(
    manifest_path: str,
    expected_db_checksum: str,
    expected_model: str = "",
    expected_dim: int = 0,
    expected_quantization: str = "",
) -> Optional[Tuple]:
    """
    Load a FAISS index from disk if the manifest checksum matches.

    Args:
        manifest_path: Path to the .manifest.json file.
        expected_db_checksum: Current DB fingerprint (from compute_db_checksum).
        expected_model: If non-empty, must match manifest.embedding_model.
        expected_dim: If > 0, must match manifest.vector_dim.
        expected_quantization: If non-empty, must match manifest.embedding_quantization.

    Returns:
        (faiss_index_or_None, chunk_ids, vectors) on success.
        None if cache miss, checksum mismatch, or load error.

    The returned faiss_index may be None if only the numpy vectors are available
    (faiss-cpu not installed). The caller should rebuild from vectors in that case.
    """
    if not os.path.exists(manifest_path):
        logger.debug("FAISS manifest not found: %s", manifest_path)
        return None

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        logger.warning("Cannot read FAISS manifest: %s", e)
        return None

    # Validate checksum
    saved_checksum = manifest.get("db_checksum", "")
    if saved_checksum != expected_db_checksum:
        logger.info(
            "FAISS cache miss — checksum mismatch (saved=%s, current=%s)",
            saved_checksum, expected_db_checksum,
        )
        return None

    # Validate model / dim if requested
    if expected_model and manifest.get("embedding_model") != expected_model:
        logger.info(
            "FAISS cache miss — model changed (%s → %s)",
            manifest.get("embedding_model"), expected_model,
        )
        return None
    if expected_dim > 0 and manifest.get("vector_dim") != expected_dim:
        logger.info(
            "FAISS cache miss — dim changed (%d → %d)",
            manifest.get("vector_dim"), expected_dim,
        )
        return None
    saved_quantization = manifest.get("embedding_quantization", "fp32")
    if expected_quantization and saved_quantization != expected_quantization:
        logger.info(
            "FAISS cache miss — quantization changed (%s → %s); rebuild required",
            saved_quantization, expected_quantization,
        )
        return None

    chunk_ids = manifest.get("chunk_id_order", [])
    faiss_path = _index_path(manifest_path)

    # Try loading FAISS index
    faiss_index = None
    if os.path.exists(faiss_path):
        try:
            import faiss
            if not hasattr(faiss, "read_index"):
                raise AttributeError("faiss.read_index is unavailable")
            faiss_index = faiss.read_index(faiss_path)
            logger.info(
                "FAISS index loaded from disk: %d vectors (checksum=%s)",
                len(chunk_ids), saved_checksum,
            )
            return (faiss_index, chunk_ids, [])
        except ImportError:
            pass  # faiss not installed, try numpy fallback
        except Exception as e:
            logger.warning("Cannot load FAISS index file: %s — trying numpy fallback", e)

    # numpy fallback (.npz)
    npz_path = faiss_path + ".npz"
    if os.path.exists(npz_path):
        try:
            data = np.load(npz_path)
            vectors = list(data["vectors"])
            loaded_ids = list(data["chunk_ids"].astype(int))
            logger.info("Numpy fallback loaded: %d vectors", len(loaded_ids))
            return (None, loaded_ids, vectors)
        except Exception as e:
            logger.warning("Cannot load numpy fallback: %s", e)
            return None

    logger.info("FAISS index file not found: %s — cache miss", faiss_path)
    return None

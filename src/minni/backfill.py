"""
Minni — embedding backfill and coverage.

Audit #225-R6 and GA1-1. Two write paths degrade to "no vector" and neither
ever retries, so the gap is permanent and invisible:

  documents  A document whose chunk/embed step failed (or predates it) has no
             rows in ``chunk_embeddings``. Measured live: 381 of 879 shared-index
             documents, 43.3%, all knowledge layer, indexed 2026-04-16..06-28 —
             a legacy backlog, not an ongoing failure. Semantic retrieval simply
             cannot see them; only the FTS leg can.

  learnings  ``promote_candidate_durable`` inserts ``embedding=NULL`` when the
             encoder is unavailable, and ``writeback.search_learnings_semantic``
             hard-filters ``embedding IS NOT NULL``. Measured live: 409 of 6,356
             learnings, permanently excluded from semantic recall.

The degraded status was logged at write time, but a log line is not a queue:
nothing re-attempted the encode, and no health surface compared document count
against vector count, so the ratio was invisible without a manual query.

This module supplies both halves — the retry (``backfill_*``) and the ratio
(``embedding_coverage``). The daemon schedules the retry; health reports the
ratio.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from minni.config import DEFAULT_CONFIG, SovereignConfig
from minni.db import SovereignDB

logger = logging.getLogger("sovereign.backfill")

#: Bound on one pass. The backlog is drained across passes rather than in one
#: transaction: encoding is the slow part, and a multi-minute write lock would
#: block live recall for the whole drain.
DEFAULT_BATCH = 200


def _numpy():
    import numpy as np

    return np


def embedding_coverage(db: SovereignDB) -> Dict[str, object]:
    """Document- and learning-level vector coverage.

    Counts only — no paths, no learning text — so this is safe to expose in the
    pre-identity health report alongside the other aggregate liveness fields.
    """
    coverage: Dict[str, object] = {}
    try:
        with db.cursor() as c:
            total_docs = c.execute(
                "SELECT COUNT(*) AS n FROM documents"
            ).fetchone()["n"]
            docs_with_vectors = c.execute(
                "SELECT COUNT(DISTINCT doc_id) AS n FROM chunk_embeddings"
            ).fetchone()["n"]
            total_learnings = c.execute(
                "SELECT COUNT(*) AS n FROM learnings WHERE superseded_by IS NULL"
            ).fetchone()["n"]
            learnings_with_embedding = c.execute(
                "SELECT COUNT(*) AS n FROM learnings "
                "WHERE superseded_by IS NULL AND embedding IS NOT NULL"
            ).fetchone()["n"]
    except Exception as exc:
        logger.debug("embedding_coverage unavailable: %s", exc)
        return {"error": str(exc)}

    def _ratio(have: int, total: int) -> Optional[float]:
        # None, not 1.0: an empty index has no coverage to report, and claiming
        # perfect coverage for zero documents is the kind of health-signal
        # overstatement this audit exists to remove.
        if total <= 0:
            return None
        return round(have / total, 4)

    coverage["documents_total"] = total_docs
    coverage["documents_with_vectors"] = docs_with_vectors
    coverage["documents_missing_vectors"] = max(0, total_docs - docs_with_vectors)
    coverage["documents_vector_ratio"] = _ratio(docs_with_vectors, total_docs)
    coverage["learnings_total"] = total_learnings
    coverage["learnings_with_embedding"] = learnings_with_embedding
    coverage["learnings_missing_embedding"] = max(
        0, total_learnings - learnings_with_embedding
    )
    coverage["learnings_embedding_ratio"] = _ratio(
        learnings_with_embedding, total_learnings
    )
    return coverage


def backfill_learning_embeddings(
    db: SovereignDB,
    config: SovereignConfig = DEFAULT_CONFIG,
    *,
    limit: int = DEFAULT_BATCH,
) -> Dict[str, int]:
    """Encode learnings stored with embedding=NULL (GA1-1).

    Returns counts; never raises. A learning that fails to encode is counted and
    left NULL for the next pass rather than being marked done — the gap must not
    become invisible just because a retry ran.
    """
    stats = {"candidates": 0, "embedded": 0, "failed": 0, "skipped_no_model": 0}
    np = _numpy()

    try:
        from minni.models import get_embedder

        model = get_embedder()
    except Exception as exc:
        logger.warning("Learning embedding backfill skipped: no encoder (%s)", exc)
        stats["skipped_no_model"] = 1
        return stats
    if model is None:
        stats["skipped_no_model"] = 1
        return stats

    with db.cursor() as c:
        rows = c.execute(
            """SELECT learning_id, content FROM learnings
               WHERE embedding IS NULL AND superseded_by IS NULL
                 AND (status IS NULL OR status NOT IN ('rejected','expired','superseded'))
               LIMIT ?""",
            (limit,),
        ).fetchall()

    stats["candidates"] = len(rows)
    for row in rows:
        content = row["content"] or ""
        if not content.strip():
            # Nothing to encode. Counted as failed, not silently dropped: an
            # empty durable learning is itself a finding.
            stats["failed"] += 1
            continue
        try:
            emb = model.encode(content).astype(np.float32)
            with db.cursor() as c:
                c.execute(
                    "UPDATE learnings SET embedding = ? WHERE learning_id = ?",
                    (emb.tobytes(), row["learning_id"]),
                )
            stats["embedded"] += 1
        except Exception as exc:
            logger.debug(
                "Learning embedding backfill failed for %s: %s",
                row["learning_id"], exc,
            )
            stats["failed"] += 1

    if stats["embedded"] or stats["failed"]:
        logger.info(
            "Learning embedding backfill: %d embedded, %d failed, %d candidates",
            stats["embedded"], stats["failed"], stats["candidates"],
        )
    return stats


def backfill_document_vectors(
    db: SovereignDB,
    config: SovereignConfig = DEFAULT_CONFIG,
    *,
    limit: int = DEFAULT_BATCH,
) -> Dict[str, int]:
    """Chunk and embed documents that have no rows in chunk_embeddings (#225-R6).

    Chunk text is read from vault_fts, which already holds the indexed content,
    so the backfill does not depend on the source file still being on disk at
    the same path — a document indexed months ago may well have moved.
    """
    stats = {"candidates": 0, "documents": 0, "chunks": 0, "failed": 0,
             "skipped_no_model": 0, "skipped_no_content": 0}
    np = _numpy()

    try:
        from minni.models import get_embedder

        model = get_embedder()
    except Exception as exc:
        logger.warning("Document vector backfill skipped: no encoder (%s)", exc)
        stats["skipped_no_model"] = 1
        return stats
    if model is None:
        stats["skipped_no_model"] = 1
        return stats

    from minni.chunker import MarkdownChunker

    chunker = MarkdownChunker(config)

    with db.cursor() as c:
        rows = c.execute(
            """SELECT d.doc_id, d.layer, f.content
               FROM documents d
               LEFT JOIN vault_fts f ON f.doc_id = d.doc_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM chunk_embeddings ce WHERE ce.doc_id = d.doc_id
               )
               LIMIT ?""",
            (limit,),
        ).fetchall()

    stats["candidates"] = len(rows)
    now = time.time()
    for row in rows:
        content = (row["content"] or "").strip()
        if not content:
            # No indexed text to embed. Left alone deliberately: re-ingesting
            # the source is the fix, and counting it here keeps the residue
            # visible instead of letting the backfill look complete.
            stats["skipped_no_content"] += 1
            continue
        layer = row["layer"] or "knowledge"
        try:
            chunks = chunker.chunk_document(content)
            if not chunks:
                stats["skipped_no_content"] += 1
                continue
            with db.transaction() as c:
                for chunk in chunks:
                    emb = model.encode(chunk.text).astype(np.float32)
                    c.execute(
                        """INSERT INTO chunk_embeddings
                           (doc_id, chunk_index, chunk_text, embedding,
                            heading_context, model_name, computed_at, layer)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["doc_id"], chunk.chunk_index, chunk.text,
                            emb.tobytes(), chunk.heading_path,
                            config.embedding_model, now, layer,
                        ),
                    )
                    stats["chunks"] += 1
            stats["documents"] += 1
        except Exception as exc:
            logger.debug(
                "Document vector backfill failed for doc %s: %s", row["doc_id"], exc
            )
            stats["failed"] += 1

    if stats["documents"] or stats["failed"]:
        logger.info(
            "Document vector backfill: %d documents, %d chunks, %d failed, "
            "%d candidates", stats["documents"], stats["chunks"],
            stats["failed"], stats["candidates"],
        )
    return stats


def run_backfill(
    db: SovereignDB,
    config: SovereignConfig = DEFAULT_CONFIG,
    *,
    limit: int = DEFAULT_BATCH,
) -> Dict[str, object]:
    """Both backfills plus the resulting coverage, for one index."""
    return {
        "documents": backfill_document_vectors(db, config, limit=limit),
        "learnings": backfill_learning_embeddings(db, config, limit=limit),
        "coverage": embedding_coverage(db),
    }

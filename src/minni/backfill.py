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
    stats = {"candidates": 0, "embedded": 0, "failed": 0, "skipped_no_model": 0,
             "unrecoverable": 0}
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

    # grok-review round 1 (finding 2): the batch predicate must EXCLUDE rows
    # that can never be encoded. The previous query took the LIMIT head
    # unfiltered, so ≥limit empty-content learnings would occupy every batch
    # forever and recoverable rows behind them would never enter one — a
    # bounded drain silently becomes a stuck queue. They are still counted
    # (below) and still reported as missing by embedding_coverage, so excluding
    # them from the batch hides nothing; it only stops them holding the queue.
    with db.cursor() as c:
        rows = c.execute(
            """SELECT learning_id, content FROM learnings
               WHERE embedding IS NULL AND superseded_by IS NULL
                 AND (status IS NULL OR status NOT IN ('rejected','expired','superseded'))
                 AND content IS NOT NULL AND TRIM(content) != ''
               LIMIT ?""",
            (limit,),
        ).fetchall()
        stats["unrecoverable"] = c.execute(
            """SELECT COUNT(*) AS n FROM learnings
               WHERE embedding IS NULL AND superseded_by IS NULL
                 AND (status IS NULL OR status NOT IN ('rejected','expired','superseded'))
                 AND (content IS NULL OR TRIM(content) = '')"""
        ).fetchone()["n"]

    stats["candidates"] = len(rows)
    for row in rows:
        content = row["content"] or ""
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
            "Learning embedding backfill: %d embedded, %d failed, %d candidates, "
            "%d unrecoverable (empty content)",
            stats["embedded"], stats["failed"], stats["candidates"],
            stats["unrecoverable"],
        )
    return stats


def backfill_document_vectors(
    db: SovereignDB,
    config: SovereignConfig = DEFAULT_CONFIG,
    *,
    limit: int = DEFAULT_BATCH,
    on_vectors=None,
) -> Dict[str, int]:
    """Chunk and embed documents that have no rows in chunk_embeddings (#225-R6).

    Chunk text is read from vault_fts, which already holds the indexed content,
    so the backfill does not depend on the source file still being on disk at
    the same path — a document indexed months ago may well have moved.

    ``on_vectors(chunk_ids, vectors)`` is invoked after each document commits.
    grok-review round 1 (finding 1): writing chunk_embeddings rows is NOT enough
    to make a document searchable on a warm daemon. retrieval._ensure_faiss_loaded
    early-returns while faiss_index.count > 0, so the live semantic index never
    picks up rows written underneath it — coverage would climb while the
    documents stayed invisible to the semantic leg until a process restart. The
    callback lets the daemon push them into the live index through the same
    path store-time indexing already uses (_refresh_live_faiss). Backfill stays
    engine-agnostic; the caller owns the index.
    """
    stats = {"candidates": 0, "documents": 0, "chunks": 0, "failed": 0,
             "skipped_no_model": 0, "skipped_no_content": 0, "unrecoverable": 0}
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

    # grok-review round 1 (finding 2): documents with no indexed text are
    # EXCLUDED from the batch, not merely skipped inside it. Taking the LIMIT
    # head unfiltered meant ≥limit contentless documents would fill every batch
    # forever and recoverable documents behind them would never enter one. They
    # are counted below and still reported missing by embedding_coverage, so
    # nothing is hidden — they just stop holding the queue.
    with db.cursor() as c:
        rows = c.execute(
            """SELECT d.doc_id, d.layer, f.content
               FROM documents d
               JOIN vault_fts f ON f.doc_id = d.doc_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM chunk_embeddings ce WHERE ce.doc_id = d.doc_id
               )
               AND f.content IS NOT NULL AND TRIM(f.content) != ''
               LIMIT ?""",
            (limit,),
        ).fetchall()
        stats["unrecoverable"] = c.execute(
            """SELECT COUNT(*) AS n
               FROM documents d
               LEFT JOIN vault_fts f ON f.doc_id = d.doc_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM chunk_embeddings ce WHERE ce.doc_id = d.doc_id
               )
               AND (f.content IS NULL OR TRIM(f.content) = '')"""
        ).fetchone()["n"]

    stats["candidates"] = len(rows)
    now = time.time()
    for row in rows:
        content = (row["content"] or "").strip()
        layer = row["layer"] or "knowledge"
        try:
            chunks = chunker.chunk_document(content)
            if not chunks:
                # Content that chunks to nothing (e.g. below min_tokens). Not a
                # queue hazard — the row keeps matching the batch predicate, but
                # counting it keeps the residue visible rather than letting the
                # backfill look complete.
                stats["skipped_no_content"] += 1
                continue
            # grok-review round 1 (finding 3): encode BEFORE opening the write
            # transaction. Encoding inside db.transaction() held BEGIN IMMEDIATE
            # across every model.encode call, so live recall writers blocked for
            # the encode duration and not merely the INSERT duration — exactly
            # the contention the per-pass batch bound exists to avoid. The
            # learnings path above already had this shape; the two now agree.
            encoded = [
                (chunk, model.encode(chunk.text).astype(np.float32))
                for chunk in chunks
            ]
            with db.transaction() as c:
                for chunk, emb in encoded:
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

            if on_vectors is not None:
                # Read the chunk_ids SQLite just assigned, so the live index is
                # keyed exactly as a search would look them up.
                try:
                    with db.cursor() as c:
                        new_ids = [
                            r["chunk_id"] for r in c.execute(
                                "SELECT chunk_id FROM chunk_embeddings "
                                "WHERE doc_id = ? ORDER BY chunk_index",
                                (row["doc_id"],),
                            ).fetchall()
                        ]
                    if len(new_ids) == len(encoded):
                        on_vectors(new_ids, [emb for _chunk, emb in encoded])
                except Exception as exc:
                    # Non-fatal by contract: the rows are durably committed, so
                    # a later cold load still finds them. Only immediacy is lost.
                    logger.debug(
                        "Backfill live-index refresh skipped for doc %s: %s",
                        row["doc_id"], exc,
                    )
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
    on_vectors=None,
) -> Dict[str, object]:
    """Both backfills plus the resulting coverage, for one index."""
    return {
        "documents": backfill_document_vectors(
            db, config, limit=limit, on_vectors=on_vectors
        ),
        "learnings": backfill_learning_embeddings(db, config, limit=limit),
        "coverage": embedding_coverage(db),
    }


def run_backfill_all_indexes(
    config: SovereignConfig = DEFAULT_CONFIG,
    *,
    limit: int = DEFAULT_BATCH,
    minni_home=None,
    on_vectors=None,
) -> Dict[str, Dict]:
    """Backfill the shared index AND every per-agent vault index.

    grok-review round 1 (finding 5): the first cut drained the shared index only,
    while the decay pass covered every index. The same writers historically fed
    both, so the asymmetry would have left any vault-side gap undrained forever.
    Per-index isolation matches run_decay_all_indexes: one unreadable vault is
    reported as its own error entry rather than costing every other index its
    pass.

    ``on_vectors`` applies to the SHARED index only. Each vault has its own
    FAISS index behind its own RetrievalEngine, and handing the shared engine a
    vault's chunk_ids would corrupt the mapping — vault indexes pick their new
    rows up on their next load instead.
    """
    from minni.index_all import discover_agent_vaults
    from minni.vault_index import build_vault_index_config

    results: Dict[str, Dict] = {}

    db = SovereignDB(config)
    try:
        results["shared"] = run_backfill(
            db, config, limit=limit, on_vectors=on_vectors
        )
    except Exception as exc:
        logger.exception("Backfill failed for the shared index")
        results["shared"] = {"error": str(exc)}
    finally:
        db.close()

    for vault in discover_agent_vaults(minni_home):
        name = vault.name
        vault_db = None
        try:
            vault_config = build_vault_index_config(vault, base_config=config)
            vault_db = SovereignDB(vault_config)
            results[name] = run_backfill(vault_db, vault_config, limit=limit)
        except Exception as exc:
            logger.warning("Backfill failed for vault %s: %s", name, exc)
            results[name] = {"error": str(exc)}
        finally:
            if vault_db is not None:
                try:
                    vault_db.close()
                except Exception:
                    pass

    return results

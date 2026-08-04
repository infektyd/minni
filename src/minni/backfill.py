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
from typing import Dict, Optional, Tuple

from minni.config import DEFAULT_CONFIG, SovereignConfig
from minni.db import SovereignDB
# Share the indexer's deliberate no-embed contract — do not fork the set.
# draft/expired pages are written to documents + vault_fts without vectors so
# they cannot occupy the fixed FAISS top-k window; retrieve drops them only
# AFTER the window is filled. A default-on backfill that re-embeds them would
# undo that policy and re-pollute semantic recall.
from minni.indexer import UNEMBEDDED_STATUSES

logger = logging.getLogger("sovereign.backfill")

#: Bound on one pass. The backlog is drained across passes rather than in one
#: transaction: encoding is the slow part, and a multi-minute write lock would
#: block live recall for the whole drain.
DEFAULT_BATCH = 200


def _numpy():
    import numpy as np

    return np


def _sql_in_literals(values) -> str:
    """Quote a set of SQL string literals for an IN (...) clause."""
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


def _embed_eligible_doc_sql(alias: str = "d") -> str:
    """SQL predicate: document is eligible for chunk embeddings.

    Mirrors ``indexer.UNEMBEDDED_STATUSES`` (draft/expired stay lexical-only).
    NULL page_status is treated as ``candidate`` — the column default.
    """
    col = f"{alias}.page_status" if alias else "page_status"
    return f"COALESCE({col}, 'candidate') NOT IN ({_sql_in_literals(UNEMBEDDED_STATUSES)})"


def _deliberate_unembed_doc_sql(alias: str = "d") -> str:
    """SQL predicate: document is deliberately left without vectors."""
    col = f"{alias}.page_status" if alias else "page_status"
    return f"COALESCE({col}, 'candidate') IN ({_sql_in_literals(UNEMBEDDED_STATUSES)})"


# grok-review round 3 (finding 1): per-(db, queue) batch cursors. The batch
# SELECTs had no ORDER BY and no cursor, so >=limit rows whose encode PERMANENTLY
# raises (OOM, corrupt payload, model reject) would occupy every LIMIT head
# forever — the third form of the stuck-queue class (empty content and short
# docs are excluded by static predicates; encode-raisers cannot be). Each pass
# now starts after the last id it attempted, so failures advance the cursor and
# recoverable rows behind them still drain; when a fetch comes back empty the
# cursor wraps to the head, so transiently-failing rows are retried on the next
# cycle rather than excluded forever. In-process state only: a restart retries
# everything, which is exactly right for transient encoder faults.
_batch_cursors: Dict[Tuple[str, str], int] = {}


def _cursor_batch(c, sql: str, key: Tuple[str, str], limit: int):
    """Fetch the next ordered LIMIT batch after the stored cursor, wrapping to
    the head when the tail is exhausted. ``sql`` takes (after_id, limit)."""
    after = _batch_cursors.get(key, 0)
    rows = c.execute(sql, (after, limit)).fetchall()
    if not rows and after:
        after = 0
        rows = c.execute(sql, (after, limit)).fetchall()
    return rows


def embedding_coverage(db: SovereignDB) -> Dict[str, object]:
    """Document- and learning-level vector coverage, plus episodic FTS coverage.

    Counts only — no paths, no learning text — so this is safe to expose in the
    pre-identity health report alongside the other aggregate liveness fields.

    Document totals are **embed-eligible** only (not draft/expired): those
    statuses are a deliberate no-embed policy shared with the indexer, and
    counting them as "missing vectors" would invent a permanent phantom gap
    the drain must not close.
    """
    coverage: Dict[str, object] = {}
    try:
        with db.cursor() as c:
            # grok-review (post-rebase tip, finding 1): align document coverage
            # with indexer.UNEMBEDDED_STATUSES — same honesty class as the
            # terminal-learnings filter below.
            _doc_eligible = _embed_eligible_doc_sql("d")
            total_docs = c.execute(
                f"SELECT COUNT(*) AS n FROM documents d WHERE {_doc_eligible}"
            ).fetchone()["n"]
            docs_with_vectors = c.execute(
                f"""SELECT COUNT(DISTINCT d.doc_id) AS n
                    FROM documents d
                    JOIN chunk_embeddings ce ON ce.doc_id = d.doc_id
                    WHERE {_doc_eligible}"""
            ).fetchone()["n"]
            docs_deliberately_unembedded = c.execute(
                f"""SELECT COUNT(*) AS n
                    FROM documents d
                    WHERE {_deliberate_unembed_doc_sql("d")}
                    AND NOT EXISTS (
                        SELECT 1 FROM chunk_embeddings ce WHERE ce.doc_id = d.doc_id
                    )"""
            ).fetchone()["n"]
            # grok-review round 3 (finding 2): align eligibility with what
            # backfill and semantic recall actually touch. Terminal learnings
            # (rejected/expired/superseded status) are skipped by both, so
            # counting their NULL embeddings here manufactured a permanent
            # phantom gap no scheduled drain could ever close — the same
            # health-signal overstatement the empty-index None-ratio refuses.
            _active = (
                "superseded_by IS NULL AND (status IS NULL OR "
                "status NOT IN ('rejected','expired','superseded'))"
            )
            total_learnings = c.execute(
                f"SELECT COUNT(*) AS n FROM learnings WHERE {_active}"
            ).fetchone()["n"]
            learnings_with_embedding = c.execute(
                f"SELECT COUNT(*) AS n FROM learnings "
                f"WHERE {_active} AND embedding IS NOT NULL"
            ).fetchone()["n"]
            # Surfaced separately so the excluded rows stay visible instead of
            # silently vanishing from the ratio.
            learnings_terminal_null = c.execute(
                "SELECT COUNT(*) AS n FROM learnings "
                "WHERE superseded_by IS NULL "
                "AND status IN ('rejected','expired','superseded') "
                "AND embedding IS NULL"
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
    coverage["documents_deliberately_unembedded"] = docs_deliberately_unembedded
    coverage["learnings_total"] = total_learnings
    coverage["learnings_with_embedding"] = learnings_with_embedding
    coverage["learnings_missing_embedding"] = max(
        0, total_learnings - learnings_with_embedding
    )
    coverage["learnings_embedding_ratio"] = _ratio(
        learnings_with_embedding, total_learnings
    )
    coverage["learnings_terminal_null_embedding"] = learnings_terminal_null
    coverage.update(episodic_index_coverage(db))
    return coverage


def episodic_index_coverage(db: SovereignDB) -> Dict[str, object]:
    """Episodic events versus what episodic_fts can actually find.

    Slice R7: embedding_coverage compared documents against vectors and
    learnings against embeddings, and stopped there. Episodic memory has no
    embeddings at all — search_episodic reads episodic_fts — so an events/FTS
    desync was invisible to every health surface. It was also real: 35 of 43
    non-trace events on the operator's database were absent from the index
    because they predate the AFTER-INSERT trigger, and the only way to see that
    was to write the join by hand. Migration 018 repairs the existing gap; this
    is what stops the next one being silent.

    Counts and ratios only — no event content — so it stays exposable next to
    the document and learning fields.

    Carries its own error boundary rather than joining embedding_coverage's:
    a schema without episodic_fts must cost the caller the episodic fields, not
    the document coverage it came for.
    """
    from minni.episodic import NON_MEMORY_EVENT_TYPES

    try:
        placeholders = ",".join("?" * len(NON_MEMORY_EVENT_TYPES))
        # Trace rows are excluded from the ratio and reported on their own line,
        # the same honesty rule documents_deliberately_unembedded follows: they
        # are not agent memory, search_episodic filters them out by the same
        # list, and folding thousands of them into the denominator would drown
        # the gap this field exists to expose (2597 traces against 43 real
        # events on the operator's DB).
        #
        # episodic_observability_events is a row count only — it deliberately
        # does NOT claim anything about whether traces are themselves indexed,
        # because that is not measured here. Their index state cannot move
        # episodic_index_ratio in either direction.
        memory_only = (
            f"content IS NOT NULL AND (event_type IS NULL"
            f" OR event_type NOT IN ({placeholders}))"
        )
        indexed_ids = (
            "SELECT CAST(event_id AS INTEGER) FROM episodic_fts"
            " WHERE event_id IS NOT NULL"
        )
        with db.cursor() as c:
            total_events = c.execute(
                f"SELECT COUNT(*) AS n FROM episodic_events WHERE {memory_only}",
                NON_MEMORY_EVENT_TYPES,
            ).fetchone()["n"]
            indexed_events = c.execute(
                f"SELECT COUNT(*) AS n FROM episodic_events"
                f" WHERE {memory_only} AND event_id IN ({indexed_ids})",
                NON_MEMORY_EVENT_TYPES,
            ).fetchone()["n"]
            observability_events = c.execute(
                f"SELECT COUNT(*) AS n FROM episodic_events"
                f" WHERE event_type IN ({placeholders})",
                NON_MEMORY_EVENT_TYPES,
            ).fetchone()["n"]
            # Index residue: FTS rows whose event is gone. Harmless to recall —
            # search_episodic INNER JOINs episodic_events — but reported rather
            # than hidden, since a climbing count means a delete path is
            # skipping the index.
            # A NULL event_id counts as an orphan too: it joins to no event and
            # the backfill can never claim it. Excluding it would leave
            # indexed + orphans failing to reconcile against the FTS row count,
            # which is how index junk stays invisible.
            orphans = c.execute(
                "SELECT COUNT(*) AS n FROM episodic_fts f"
                " WHERE f.event_id IS NULL"
                " OR CAST(f.event_id AS INTEGER) NOT IN"
                " (SELECT event_id FROM episodic_events)"
            ).fetchone()["n"]
    except Exception as exc:
        # warning, not debug: at the default level a debug line means a broken
        # coverage query is indistinguishable from a healthy one.
        logger.warning("episodic_index_coverage unavailable: %s", exc)
        # The key set stays stable so "unknown" cannot be misread as "empty".
        # Dropping the keys entirely would leave a consumer's
        # coverage.get("episodic_index_ratio") returning None — the exact value
        # this function uses to mean "no episodic events yet".
        return {
            "episodic_events_total": None,
            "episodic_events_indexed": None,
            "episodic_events_missing_index": None,
            "episodic_index_ratio": None,
            "episodic_observability_events": None,
            "episodic_fts_orphans": None,
            "episodic_error": str(exc),
        }

    return {
        "episodic_events_total": total_events,
        "episodic_events_indexed": indexed_events,
        "episodic_events_missing_index": max(0, total_events - indexed_events),
        # None rather than 1.0 on an empty table, matching _ratio above: an
        # empty episodic log has no coverage to report.
        "episodic_index_ratio": (
            round(indexed_events / total_events, 4) if total_events > 0 else None
        ),
        "episodic_observability_events": observability_events,
        "episodic_fts_orphans": orphans,
    }


def vault_embedding_coverage(
    minni_home=None,
    base_config: SovereignConfig = DEFAULT_CONFIG,
) -> Dict[str, Dict]:
    """Per-vault embedding_coverage, keyed by vault name.

    grok-review round 3 (finding 4): the drain is multi-index
    (run_backfill_all_indexes) but the health surface sampled only the shared
    DB, so an operator could read "coverage fine" while a vault's semantic
    recall was still gapped. Same per-index isolation as the drain: one
    unreadable vault reports its own error entry.
    """
    from minni.index_all import discover_agent_vaults
    from minni.vault_index import build_vault_index_config

    out: Dict[str, Dict] = {}
    for vault in discover_agent_vaults(minni_home):
        vault_db = None
        try:
            vault_config = build_vault_index_config(vault, base_config=base_config)
            vault_db = SovereignDB(vault_config)
            out[vault.name] = embedding_coverage(vault_db)
        except Exception as exc:
            out[vault.name] = {"error": str(exc)}
        finally:
            if vault_db is not None:
                try:
                    vault_db.close()
                except Exception:
                    pass
    return out


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
    # grok-review round 3 (finding 1): ordered, cursor-advanced batches — see
    # _batch_cursors. A learning whose encode permanently raises stays
    # embedding=NULL and would otherwise re-match the head of every pass.
    cursor_key = (str(config.db_path), "learnings")
    with db.cursor() as c:
        rows = _cursor_batch(
            c,
            """SELECT learning_id, content FROM learnings
               WHERE embedding IS NULL AND superseded_by IS NULL
                 AND (status IS NULL OR status NOT IN ('rejected','expired','superseded'))
                 AND content IS NOT NULL AND TRIM(content) != ''
                 AND learning_id > ?
               ORDER BY learning_id
               LIMIT ?""",
            cursor_key, limit,
        )
        _batch_cursors[cursor_key] = rows[-1]["learning_id"] if rows else 0
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
             "skipped_no_model": 0, "unrecoverable": 0}
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
    # grok-review round 3 (finding 1): ordered, cursor-advanced batches — see
    # _batch_cursors. A document whose encode permanently raises gains no
    # chunk_embeddings rows and would otherwise re-match the head of every pass.
    # grok-review (post-rebase tip, finding 1): also exclude draft/expired —
    # indexer.UNEMBEDDED_STATUSES deliberately leaves those without vectors so
    # they cannot fill the FAISS window; backfill must not undo that policy.
    _eligible = _embed_eligible_doc_sql("d")
    cursor_key = (str(config.db_path), "documents")
    with db.cursor() as c:
        rows = _cursor_batch(
            c,
            f"""SELECT d.doc_id, d.layer, f.content
               FROM documents d
               JOIN vault_fts f ON f.doc_id = d.doc_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM chunk_embeddings ce WHERE ce.doc_id = d.doc_id
               )
               AND f.content IS NOT NULL AND TRIM(f.content) != ''
               AND {_eligible}
               AND d.doc_id > ?
               ORDER BY d.doc_id
               LIMIT ?""",
            cursor_key, limit,
        )
        _batch_cursors[cursor_key] = rows[-1]["doc_id"] if rows else 0
        stats["unrecoverable"] = c.execute(
            f"""SELECT COUNT(*) AS n
               FROM documents d
               LEFT JOIN vault_fts f ON f.doc_id = d.doc_id
               WHERE NOT EXISTS (
                   SELECT 1 FROM chunk_embeddings ce WHERE ce.doc_id = d.doc_id
               )
               AND {_eligible}
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
                # grok-review round 2 (finding 3): mirror the short-content
                # floor of index_durable_document. Content below the chunker's
                # min_tokens chunks to nothing, but the row still matches the
                # batch predicate — skipping it both reopens the stuck-queue
                # hazard (short rows hold the LIMIT head forever) and leaves
                # short memories permanently vectorless while the live durable
                # path embeds them as one whole-body chunk. Do the same here.
                from minni.chunker import Chunk

                chunks = [
                    Chunk(
                        text=content,
                        heading="",
                        heading_path="",
                        chunk_index=0,
                    )
                ]
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
    vault's chunk_ids would corrupt the mapping. grok-review round 2
    (finding 2): "next load" alone is NOT enough on a warm daemon — cached
    vault engines early-return in _ensure_faiss_loaded while count > 0 — so
    the daemon caller must drop its per-vault engine cache whenever a vault
    made progress (minnid._backfill_sweep_once does).
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

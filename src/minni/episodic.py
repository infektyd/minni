"""
Minni V3.1 — Episodic Memory.

V3.1 changes:
- Removed all compression references (no TurboQuant anywhere)
- Thread auto-binding uses the FAISS index instead of raw numpy loops
- Raw blob compression uses zlib only (not TurboQuant — it was never
  the right tool for compressing chat logs)
"""

import json
import sqlite3
import time
import zlib
import logging
from typing import Dict, List, Optional
from datetime import datetime

import numpy as np

from minni.config import SovereignConfig, DEFAULT_CONFIG
from minni.db import SovereignDB

logger = logging.getLogger("sovereign.episodic")

# Observability-only event types: written by the recall trace, not agent memory.
# The single source of truth for "this row is not a memory" — retrieval's
# RetrievalEngine.EPISODIC_NON_MEMORY_TYPES and the episodic coverage metric
# both read this, so an added trace type cannot mean one thing to the search
# filter and another to the health surface.
NON_MEMORY_EVENT_TYPES: tuple = ("recall",)


def _episodic_tables_present(conn: sqlite3.Connection) -> bool:
    """True only when both episodic_events and episodic_fts exist.

    Partial schemas are real: migrations run against test fixtures that build a
    subset of tables, and the reconcile below must be a no-op there rather than
    taking the whole migration batch down.
    """
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE name IN ('episodic_events', 'episodic_fts')"
        ).fetchall()
    except sqlite3.Error:
        return False
    return len({r[0] for r in rows}) == 2


def reconcile_episodic_fts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Insert every content-bearing episodic_events row missing from episodic_fts.

    trg_episodic_fts_insert keeps the index in step going forward, but a trigger
    only sees rows written after it exists. Every event logged before the trigger
    was added stayed out of episodic_fts permanently, and nothing reconciled it:
    the text sat in episodic_events while search_episodic — which reads only the
    FTS table — could not return it. On the operator's own database that was 35
    of the 43 non-trace events, so ~81% of real episodic memory was unreachable.

    Runs in two set-wise passes and returns {missing_before, inserted, removed}:

      1. remove index rows that match no event on BOTH event_id and agent_id —
         orphans whose event is gone, and rows filed under the wrong agent;
      2. index every content-bearing event the cleaned index still lacks.

    Pass 1 is why episodic_events has no AFTER DELETE FTS trigger (see db.py):
    doing it here is one set-wise statement per sweep instead of a full scan of
    the fts content table per deleted row on the search hot path.

    Idempotent by construction: it repairs exactly the rows that disagree with
    episodic_events, so a second run changes nothing. Safe on a fresh or partial
    schema (no-op).
    """
    if not _episodic_tables_present(conn):
        logger.debug("reconcile_episodic_fts: episodic tables absent — no-op")
        return {"missing_before": 0, "inserted": 0, "removed": 0}

    # NOT EXISTS rather than the original NOT IN: it is NULL-safe by
    # construction, so a NULL event_id in the index (an UNINDEXED fts5 column
    # has no affinity and no NOT NULL constraint) can no longer make the whole
    # predicate evaluate to NULL and silently repair nothing.
    unmatched = (
        "NOT EXISTS (SELECT 1 FROM episodic_fts f"
        "            WHERE CAST(f.event_id AS INTEGER) = e.event_id"
        "              AND f.agent_id = e.agent_id)"
    )

    # Pass 1 — drop index rows that match no event on BOTH columns. This is the
    # sweep-side replacement for a per-row AFTER DELETE trigger (see db.py):
    # set-wise and once per sweep, rather than a full scan of the fts content
    # table per deleted event on the search hot path. It collects two classes at
    # once — orphans whose event is gone, and rows filed under the wrong agent,
    # which are unreachable by their owner AND returned to an agent that never
    # recorded them. The second class is why a delete-then-insert inside the
    # repair below was not enough: an event carrying BOTH a correct row and an
    # intruder row satisfied the repair predicate, so nothing was ever in the
    # repair set and the leak persisted at a reported ratio of 1.0.
    removed = conn.execute(
        """DELETE FROM episodic_fts
            WHERE event_id IS NULL
               OR NOT EXISTS (
                   SELECT 1 FROM episodic_events e
                    WHERE e.event_id = CAST(episodic_fts.event_id AS INTEGER)
                      AND e.agent_id = episodic_fts.agent_id)"""
    ).rowcount
    removed = removed if removed and removed > 0 else 0

    # Pass 2 — index every content-bearing event the cleaned index still lacks.
    #
    # "Missing" is spelled on event_id AND agent_id so this predicate reads
    # identically to episodic_index_coverage's "indexed" predicate: the repair
    # and the metric must not be able to disagree about what coverage means.
    # Keyed on event_id alone, they did — a wrong-agent row counted as present
    # here (nothing to repair) while the metric counted it as uncovered, so the
    # ratio sat below 1.0 and no number of sweeps could move it. A repair set
    # that cannot close the gap its own metric reports is worse than no repair:
    # it reports success forever.
    #
    # Honest note on the agent_id half: after Pass 1 it is redundant, because
    # every surviving index row already matches its event on both columns —
    # dropping it here leaves the suite green, and that mutant is equivalent
    # rather than escaped. It stays because the two predicates being literally
    # the same text is the property worth protecting; if Pass 1 is ever
    # narrowed, this half stops being redundant and starts being the fix again.
    missing_before = conn.execute(
        f"SELECT COUNT(*) FROM episodic_events e"
        f" WHERE e.content IS NOT NULL AND {unmatched}"
    ).fetchone()[0]

    if not missing_before:
        if removed:
            logger.info(
                "reconcile_episodic_fts: removed %d unreachable index row(s)",
                removed,
            )
        return {"missing_before": 0, "inserted": 0, "removed": removed}

    cur = conn.execute(
        f"""INSERT INTO episodic_fts(event_id, agent_id, content)
            SELECT e.event_id, e.agent_id, e.content
              FROM episodic_events e
             WHERE e.content IS NOT NULL
               AND {unmatched}"""
    )
    inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    logger.info(
        "reconcile_episodic_fts: indexed %d episodic event(s) the trigger never saw",
        inserted,
    )
    return {
        "missing_before": missing_before,
        "inserted": inserted,
        "removed": removed,
    }


class EpisodicMemory:
    """
    Episodic memory: agent events, task lifecycles, thread management.
    All writes go through the shared SovereignDB.
    """

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
    ):
        self.db = db
        self.config = config

    # ── Events ─────────────────────────────────────────────────

    def add_event(
        self,
        agent_id: str,
        event_type: str,
        content: str,
        task_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        raw_blob: Optional[bytes] = None,
        bind_thread: bool = True,
    ) -> int:
        """
        Log an episodic event.

        Args:
            agent_id: Which agent (forge, recon, etc.)
            event_type: task_start, task_end, query, finding, error, message
            content: Event content text
            task_id: Related task ID
            thread_id: Thread this event belongs to
            metadata: Arbitrary JSON metadata
            raw_blob: Optional raw data to compress (zlib) and store
            bind_thread: When False, skip semantic thread binding (observability
                writes that carry thread_id as a join key only).

        Returns:
            event_id
        """
        now = time.time()
        meta_json = json.dumps(metadata) if metadata else None

        # Compress raw blob with zlib if provided
        compressed_raw = None
        if raw_blob:
            compressed_raw = zlib.compress(raw_blob, level=6)

        with self.db.cursor() as c:
            c.execute("""
                INSERT INTO episodic_events
                (agent_id, event_type, content, task_id, thread_id,
                 metadata, compressed_raw, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, event_type, content, task_id, thread_id,
                meta_json, compressed_raw, now,
            ))
            event_id = c.lastrowid

        # Auto-bind thread to docs if thread_id is provided. bind_thread=False
        # is for observability writes (e.g. the recall trace): they carry a
        # thread_id as a join key but must stay inert — no thread_doc_links
        # mutation, no semantic search on the hot path.
        if bind_thread and thread_id and content:
            self._semantic_thread_bind(thread_id, content)

        return event_id

    # ── Tasks ──────────────────────────────────────────────────

    def start_task(self, agent_id: str, task_id: str, description: str) -> None:
        """Log task start."""
        now = time.time()
        with self.db.cursor() as c:
            c.execute("""
                INSERT OR REPLACE INTO task_logs
                (agent_id, task_id, description, status, start_time)
                VALUES (?, ?, ?, 'running', ?)
            """, (agent_id, task_id, description, now))

        self.add_event(agent_id, "task_start", description, task_id=task_id)

    def end_task(
        self,
        agent_id: str,
        task_id: str,
        status: str,
        result: str,
    ) -> None:
        """Log task completion."""
        now = time.time()
        with self.db.cursor() as c:
            c.execute("""
                UPDATE task_logs
                SET status = ?, end_time = ?, result = ?
                WHERE agent_id = ? AND task_id = ?
            """, (status, now, result, agent_id, task_id))

        self.add_event(agent_id, "task_end", f"{status}: {result}", task_id=task_id)

    # ── Threads ────────────────────────────────────────────────

    def create_thread(
        self,
        thread_id: str,
        title: str,
        agent_count: int = 1,
    ) -> None:
        """Create a new conversation thread hub."""
        now = time.time()
        with self.db.cursor() as c:
            c.execute("""
                INSERT OR IGNORE INTO threads
                (thread_id, title, created_at, updated_at, agent_count, message_count)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (thread_id, title, now, now, agent_count))

    def _semantic_thread_bind(self, thread_id: str, content: str) -> None:
        """
        Semantically bind a thread to relevant vault documents.
        Uses the retrieval engine's semantic search (FAISS-backed).
        """
        try:
            from minni.retrieval import RetrievalEngine
            engine = RetrievalEngine(self.db, self.config)
            results = engine._semantic_search(content, limit=5)

            threshold = self.config.thread_bind_threshold
            now = time.time()

            with self.db.cursor() as c:
                for r in results:
                    sim = r.get("similarity", 0)
                    if sim >= threshold:
                        c.execute("""
                            INSERT OR REPLACE INTO thread_doc_links
                            (thread_id, doc_id, similarity, created_at)
                            VALUES (?, ?, ?, ?)
                        """, (thread_id, r["doc_id"], sim, now))
                        logger.debug(
                            "Thread %s → doc %d (sim=%.3f)",
                            thread_id, r["doc_id"], sim
                        )
        except Exception as e:
            logger.warning("Thread auto-bind failed: %s", e)

    # ── Queries ────────────────────────────────────────────────

    def get_recent_events(
        self,
        agent_id: str,
        limit: int = 20,
    ) -> List[Dict]:
        """Get recent episodic events for an agent."""
        results = []
        with self.db.cursor() as c:
            c.execute("""
                SELECT event_id, event_type, content, task_id,
                       thread_id, created_at, metadata
                FROM episodic_events
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (agent_id, limit))

            for row in c.fetchall():
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
                results.append({
                    "event_id": row["event_id"],
                    "type": row["event_type"],
                    "content": row["content"],
                    "task_id": row["task_id"],
                    "thread_id": row["thread_id"],
                    "timestamp": datetime.fromtimestamp(row["created_at"]).isoformat(),
                    "metadata": meta,
                })

        return results

    def get_task_history(
        self,
        agent_id: str,
        limit: int = 10,
    ) -> List[Dict]:
        """Get agent's task history."""
        results = []
        with self.db.cursor() as c:
            c.execute("""
                SELECT task_id, description, status, start_time, end_time, result
                FROM task_logs
                WHERE agent_id = ?
                ORDER BY start_time DESC
                LIMIT ?
            """, (agent_id, limit))

            for row in c.fetchall():
                duration = (row["end_time"] - row["start_time"]) if row["end_time"] else 0
                results.append({
                    "task_id": row["task_id"],
                    "description": row["description"],
                    "status": row["status"],
                    "duration_seconds": round(duration, 2),
                    "result": row["result"],
                    "timestamp": datetime.fromtimestamp(row["start_time"]).isoformat(),
                })

        return results

    def get_thread_context(
        self,
        thread_id: str,
        limit: int = 20,
    ) -> Dict:
        """Get full thread context: events + linked docs."""
        thread_info = {}
        with self.db.cursor() as c:
            c.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
            row = c.fetchone()
            if not row:
                return {"error": f"Thread {thread_id} not found"}

            thread_info = {
                "thread_id": row["thread_id"],
                "title": row["title"],
                "message_count": row["message_count"],
                "agent_count": row["agent_count"],
                "created_at": row["created_at"],
            }

            c.execute("""
                SELECT event_id, agent_id, event_type, content, created_at
                FROM episodic_events
                WHERE thread_id = ?
                ORDER BY created_at ASC
                LIMIT ?
            """, (thread_id, limit))

            thread_info["events"] = [
                {
                    "agent": row["agent_id"],
                    "type": row["event_type"],
                    "content": row["content"],
                    "timestamp": row["created_at"],
                }
                for row in c.fetchall()
            ]

            c.execute("""
                SELECT d.doc_id, d.path, d.agent, d.sigil, tdl.similarity
                FROM thread_doc_links tdl
                JOIN documents d ON d.doc_id = tdl.doc_id
                WHERE tdl.thread_id = ?
                ORDER BY tdl.similarity DESC
                LIMIT 10
            """, (thread_id,))

            import os
            thread_info["linked_docs"] = [
                {
                    "doc_id": row["doc_id"],
                    "filename": os.path.basename(row["path"]),
                    "agent": row["agent"],
                    "sigil": row["sigil"],
                    "similarity": round(row["similarity"], 3),
                }
                for row in c.fetchall()
            ]

        return thread_info

    # ── Cleanup ────────────────────────────────────────────────

    def trim_recall_traces(self, max_age_seconds: int = 604800) -> int:
        """Reap expired recall-trace rows (event_type='recall') only. The
        trace path calls this on write so its own footprint honors the
        advertised TTL without depending on a global cleanup pass (which
        nothing schedules today); other event types are untouched."""
        cutoff = time.time() - max_age_seconds
        # Reads NON_MEMORY_EVENT_TYPES rather than repeating 'recall': a trace
        # type added to the shared list would otherwise be filtered out of
        # search, health and Recent Activity but never reaped, accumulating
        # forever in the one table this method exists to bound.
        marks = ",".join("?" * len(NON_MEMORY_EVENT_TYPES))
        with self.db.cursor() as c:
            c.execute(f"""
                DELETE FROM episodic_fts
                WHERE event_id IN (
                    SELECT event_id FROM episodic_events
                    WHERE event_type IN ({marks}) AND created_at < ?
                )
            """, (*NON_MEMORY_EVENT_TYPES, cutoff))
            c.execute(
                f"DELETE FROM episodic_events"
                f" WHERE event_type IN ({marks}) AND created_at < ?",
                (*NON_MEMORY_EVENT_TYPES, cutoff))
            return c.rowcount

    def cleanup_expired(self, max_age_seconds: int = 604800) -> int:
        """Remove episodic events older than max_age (default 7 days)."""
        cutoff = time.time() - max_age_seconds
        with self.db.cursor() as c:
            c.execute("""
                DELETE FROM episodic_fts
                WHERE event_id IN (
                    SELECT event_id FROM episodic_events WHERE created_at < ?
                )
            """, (cutoff,))
            c.execute("DELETE FROM episodic_events WHERE created_at < ?", (cutoff,))
            return c.rowcount

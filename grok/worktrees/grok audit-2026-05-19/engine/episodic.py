"""
Sovereign Memory V3.1 — Episodic Memory.

V3.1 changes:
- Removed all compression references (no TurboQuant anywhere)
- Thread auto-binding uses the FAISS index instead of raw numpy loops
- Raw blob compression uses zlib only (not TurboQuant — it was never
  the right tool for compressing chat logs)
"""

import json
import time
import zlib
import logging
from typing import Dict, List, Optional
from datetime import datetime

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB

logger = logging.getLogger("sovereign.episodic")


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

        # Auto-bind thread to docs if thread_id is provided
        if thread_id and content:
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
            from retrieval import RetrievalEngine
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

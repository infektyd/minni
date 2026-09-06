"""
Minni V3.1 — Write-Back Memory.

Agents don't just read from memory — they write back.

When a Claw discovers something useful during a task (a pattern, a fix,
a decision rationale, a learned preference), it can store that as a
"learning" that persists across sessions.

Learnings are:
- Indexed in FTS5 for keyword search
- Embedded for semantic search
- Categorized (pattern, fix, decision, preference, fact)
- Versioned: new learnings can supersede old ones
- Optionally written to disk as markdown files (for Obsidian integration)
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional
from datetime import datetime

# G09: SEC-018 frontmatter forgery guard helpers (repo-grounded, smallest addition)

from minni.config import SovereignConfig, DEFAULT_CONFIG
from minni.db import SovereignDB
from minni.graph_commit import ensure_canonical_learning_node
from minni.graph_readiness import memory_links_typed_columns_present
from minni.timestamps import coerce_epoch

logger = logging.getLogger("sovereign.writeback")


def _numpy():
    """Import NumPy only when an embedding path actually needs vector math."""
    import numpy as np
    return np


# Categories for learnings
CATEGORIES = {
    "pattern":    "Recurring patterns discovered in code, behavior, or data",
    "fix":        "Bug fixes, workarounds, solutions to known problems",
    "decision":   "Decisions made and their rationale",
    "preference": "User or agent preferences learned over time",
    "fact":       "Facts discovered about the codebase, infrastructure, or domain",
    "procedure":  "Step-by-step procedures that worked",
    "general":    "Uncategorized learning",
}


class WriteBackMemory:
    """
    Write-back memory: agents store new learnings for future recall.

    Usage:
        wb = WriteBackMemory(db, config)
        wb.store_learning(
            agent_id="forge",
            content="WebSocket reconnection needs a 500ms backoff before retry",
            category="fix",
            source_query="websocket connection drops",
            source_doc_ids=[42, 67],
        )

        # Later, another agent can find it:
        learnings = wb.recall_learnings("websocket connection issues", limit=5)
    """

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
    ):
        self.db = db
        self.config = config
        self._model = None

    @property
    def model(self):
        """Return the process-wide embedding model singleton."""
        from minni.models import get_embedder
        return get_embedder()

    def store_learning(
        self,
        agent_id: str,
        content: str,
        category: str = "general",
        source_query: Optional[str] = None,
        source_doc_ids: Optional[List[int]] = None,
        evidence_doc_ids: Optional[List[int]] = None,
        confidence: float = 1.0,
        supersedes: Optional[int] = None,
    ) -> int:
        """
        Store a new learning.

        Args:
            agent_id: Which agent discovered this
            content: The learning text
            category: One of CATEGORIES keys
            source_query: The query that led to this learning
            source_doc_ids: Document IDs that informed this learning
            evidence_doc_ids: PR-6 structured evidence document IDs
            confidence: How confident (0-1) the agent is
            supersedes: learning_id this replaces (versioning)

        Returns:
            learning_id
        """
        if category not in CATEGORIES:
            category = "general"

        now = time.time()
        doc_ids_json = json.dumps(source_doc_ids) if source_doc_ids else None

        # Embed the learning for semantic search
        emb_bytes = None
        if self.model:
            from minni.models import get_embedder_lock

            np = _numpy()
            with get_embedder_lock():
                emb = self.model.encode(content, show_progress_bar=False).astype(np.float32)
            emb_bytes = emb.tobytes()

        # Coordinated atomic commit: learning row + supersede marker +
        # canonical graph node + explicit edges share one cursor block, so any
        # failure rolls everything back (no partial durable state). Model
        # encoding stays outside the transaction, per the write-path contract.
        with self.db.cursor() as c:
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, source_doc_ids, source_query,
                 confidence, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, category, content, doc_ids_json, source_query,
                confidence, emb_bytes, now,
            ))
            learning_id = c.lastrowid

            # Mark superseded learning
            if supersedes:
                c.execute(
                    "UPDATE learnings SET superseded_by = ? WHERE learning_id = ?",
                    (learning_id, supersedes),
                )

            canonical_doc_id = ensure_canonical_learning_node(
                c,
                learning_id=learning_id,
                agent_id=agent_id,
                content=content,
                vault_path=self.config.vault_path,
                created_at=now,
            )

            self.add_derived_from_edges(
                learning_id=learning_id,
                agent_id=agent_id,
                category=category,
                content=content,
                evidence_doc_ids=evidence_doc_ids or source_doc_ids,
                created_at=now,
                _cursor=c,
                canonical_doc_id=canonical_doc_id,
            )

        logger.info(
            "Stored learning #%d [%s/%s]: %.60s...",
            learning_id, agent_id, category, content,
        )

        # Write to disk as markdown (for Obsidian integration)
        if self.config.writeback_enabled:
            self._write_to_disk(learning_id, agent_id, category, content, now)

        return learning_id

    def add_derived_from_edges(
        self,
        learning_id: int,
        agent_id: str,
        category: str,
        content: str,
        evidence_doc_ids: Optional[List[int]],
        created_at: Optional[float] = None,
        _cursor=None,
        canonical_doc_id: Optional[int] = None,
    ) -> Optional[int]:
        """
        Represent an evidence-backed learning as a graph document and link it
        to the documents it was derived from.

        ``memory_links`` is document-to-document, so this creates a synthetic
        ``learning://<id>`` document node only when evidence is supplied. This
        keeps the schema additive and lets graph_export surface the edge
        without a migration.

        When ``_cursor`` is supplied (coordinated commit from
        ``store_learning``), the writes join its transaction and failures
        propagate for full rollback. Otherwise a private cursor is used and
        failures stay lenient (warning + None), as before.

        When ``canonical_doc_id`` is supplied, edges source from that
        canonical node and no ``learning://`` alias is created (no orphan
        duplicate representations). Otherwise the legacy alias path runs.
        """
        if not evidence_doc_ids:
            return None

        # Audit R0: this is the only documents writer whose timestamp is
        # caller-supplied rather than a local time.time(), so it is the only one
        # that can hand a non-numeric value to the REAL-affinity indexed_at /
        # last_modified columns. Coerce (and log) before it reaches SQLite —
        # migration 016's triggers are the backstop for out-of-tree writers.
        now = coerce_epoch(
            created_at, field="indexed_at",
            default=None, context="writeback.add_derived_from_edges",
        ) or time.time()
        valid_ids = []
        seen = set()
        for raw_id in evidence_doc_ids:
            try:
                doc_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if doc_id not in seen:
                seen.add(doc_id)
                valid_ids.append(doc_id)
        if not valid_ids:
            return None

        if _cursor is not None:
            # Coordinated commit: run inside the caller's cursor so the edges
            # share its transaction; failures propagate for full rollback.
            return self._insert_derived_edges(
                _cursor, learning_id, agent_id, valid_ids, now,
                canonical_doc_id=canonical_doc_id,
            )
        try:
            with self.db.cursor() as c:
                return self._insert_derived_edges(
                    c, learning_id, agent_id, valid_ids, now,
                    canonical_doc_id=canonical_doc_id,
                )
        except Exception as exc:
            logger.warning("Failed to add derived_from provenance edges: %s", exc)
            return None

    def _insert_derived_edges(
        self, c, learning_id, agent_id, valid_ids, now, canonical_doc_id=None
    ):
        """Derived_from edges on an already-open cursor.

        With a canonical source the edges attach to the canonical node and no
        alias document is created. Otherwise the legacy ``learning://`` alias
        node is created (or reused) as the edge source. Raises on DB errors;
        the caller owns the failure policy.
        """
        if canonical_doc_id is not None:
            learning_doc_id = int(canonical_doc_id)
        else:
            path = f"learning://{learning_id}"
            c.execute("SELECT doc_id FROM documents WHERE path = ?", (path,))
            row = c.fetchone()
            if row:
                learning_doc_id = row["doc_id"]
            else:
                c.execute(
                    """
                    INSERT INTO documents
                    (path, agent, sigil, last_modified, indexed_at, access_count,
                     decay_score, whole_document, page_status, privacy_level,
                     page_type, evidence_refs)
                    VALUES (?, ?, ?, ?, ?, 0, 1.0, 0, 'accepted', 'safe',
                            'learning', ?)
                    """,
                    (
                        path,
                        f"learning:{agent_id}",
                        "L",
                        now,
                        now,
                        json.dumps(valid_ids),
                    ),
                )
                learning_doc_id = c.lastrowid

        c.execute(
            "SELECT doc_id FROM documents WHERE doc_id IN ({})".format(
                ",".join("?" for _ in valid_ids)
            ),
            valid_ids,
        )
        existing = [row["doc_id"] for row in c.fetchall()]
        for evidence_doc_id in existing:
            # Baseline fallback: when 021 is unavailable (db.py treats
            # a failed migrations run as non-fatal) the typed columns
            # are absent — write the legacy 5-column edge instead of
            # failing the whole writeback.
            if memory_links_typed_columns_present(c):
                c.execute(
                    """
                    INSERT INTO memory_links
                    (source_doc_id, target_doc_id, link_type, weight, created_at,
                     confidence, inference_method)
                    VALUES (?, ?, 'derived_from', 1.0, ?, 1.0, 'writeback_evidence')
                    ON CONFLICT(source_doc_id, target_doc_id, link_type)
                    DO UPDATE SET weight=excluded.weight,
                                  confidence=excluded.confidence,
                                  inference_method=excluded.inference_method
                    """,
                    (learning_doc_id, evidence_doc_id, now),
                )
            else:
                c.execute(
                    """
                    INSERT INTO memory_links
                    (source_doc_id, target_doc_id, link_type, weight, created_at)
                    VALUES (?, ?, 'derived_from', 1.0, ?)
                    ON CONFLICT(source_doc_id, target_doc_id, link_type)
                    DO UPDATE SET weight=excluded.weight
                    """,
                    (learning_doc_id, evidence_doc_id, now),
                )
        return learning_doc_id

    def recall_learnings(
        self,
        query: str,
        agent_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Recall relevant learnings via hybrid FTS + semantic search.
        Only returns non-superseded learnings.
        """
        results = []

        # FTS5 search
        import re
        safe_q = re.sub(r'[^\w\s\-]', ' ', query)
        words = safe_q.split()
        if not words:
            return results

        fts_query = " ".join(words)

        with self.db.cursor() as c:
            sql = """
                SELECT lf.learning_id, lf.agent_id, lf.content, lf.category,
                       l.confidence, l.created_at, l.access_count,
                       l.source_query, l.source_doc_ids
                FROM learnings_fts lf
                JOIN learnings l ON l.learning_id = lf.learning_id
                WHERE learnings_fts MATCH ?
                      AND l.superseded_by IS NULL
                      AND (l.status IS NULL OR l.status NOT IN ('rejected','expired','superseded'))
            """
            params = [fts_query]

            if agent_id:
                sql += " AND lf.agent_id = ?"
                params.append(agent_id)
            if category:
                sql += " AND lf.category = ?"
                params.append(category)

            sql += " ORDER BY rank LIMIT ?"
            params.append(limit * 2)

            try:
                c.execute(sql, params)
                for row in c.fetchall():
                    results.append({
                        "learning_id": row["learning_id"],
                        "agent_id": row["agent_id"],
                        "content": row["content"],
                        "category": row["category"],
                        "confidence": row["confidence"],
                        "created_at": datetime.fromtimestamp(row["created_at"]).isoformat(),
                        "access_count": row["access_count"],
                        "source_query": row["source_query"],
                    })
            except Exception as e:
                logger.warning("Learning FTS search failed: %s", e)

        # Semantic search (if we have embeddings)
        if self.model and len(results) < limit:
            semantic_results = self._semantic_search_learnings(
                query, agent_id, category, limit - len(results)
            )
            # Merge, avoiding duplicates
            seen_ids = {r["learning_id"] for r in results}
            for sr in semantic_results:
                if sr["learning_id"] not in seen_ids:
                    results.append(sr)
                    seen_ids.add(sr["learning_id"])

        # Update access counts
        for r in results[:limit]:
            with self.db.cursor() as c:
                c.execute(
                    """UPDATE learnings
                       SET access_count = access_count + 1, last_accessed = ?
                       WHERE learning_id = ?""",
                    (time.time(), r["learning_id"]),
                )

        return results[:limit]

    def _semantic_search_learnings(
        self,
        query: str,
        agent_id: Optional[str],
        category: Optional[str],
        limit: int,
    ) -> List[Dict]:
        """Semantic search over learning embeddings."""
        if not self.model:
            return []

        from minni.models import get_embedder_lock

        np = _numpy()
        with get_embedder_lock():
            query_emb = self.model.encode(query, show_progress_bar=False).astype(np.float32)
        query_norm = np.linalg.norm(query_emb)
        if query_norm < 1e-8:
            return []
        query_emb = query_emb / query_norm

        results = []
        with self.db.cursor() as c:
            sql = """
                SELECT learning_id, agent_id, category, content, confidence,
                       created_at, access_count, source_query, embedding
                FROM learnings
                WHERE superseded_by IS NULL AND embedding IS NOT NULL
                      AND (status IS NULL OR status NOT IN ('rejected','expired','superseded'))
            """
            params = []
            if agent_id:
                sql += " AND agent_id = ?"
                params.append(agent_id)
            if category:
                sql += " AND category = ?"
                params.append(category)

            c.execute(sql, params)
            scored = []
            for row in c.fetchall():
                emb = np.frombuffer(row["embedding"], dtype=np.float32)
                norm = np.linalg.norm(emb)
                if norm < 1e-8:
                    continue
                sim = float(np.dot(query_emb, emb / norm))
                scored.append((sim, row))

            scored.sort(key=lambda x: x[0], reverse=True)
            for sim, row in scored[:limit]:
                results.append({
                    "learning_id": row["learning_id"],
                    "agent_id": row["agent_id"],
                    "content": row["content"],
                    "category": row["category"],
                    "confidence": row["confidence"],
                    "created_at": datetime.fromtimestamp(row["created_at"]).isoformat(),
                    "access_count": row["access_count"],
                    "source_query": row["source_query"],
                    "similarity": round(sim, 4),
                })

        return results

    def _write_to_disk(
        self,
        learning_id: int,
        agent_id: str,
        category: str,
        content: str,
        timestamp: float,
    ) -> None:
        """Write learning as a markdown file to the writeback directory."""
        try:
            dt = datetime.fromtimestamp(timestamp)
            filename = f"{dt.strftime('%Y%m%d_%H%M%S')}_{agent_id}_{category}.md"
            filepath = os.path.join(self.config.writeback_path, filename)

            # G09 SEC-018: reject learn bodies containing bare --- lines or fenced code
            # that could be misparsed as frontmatter when the file is later indexed.
            if self._contains_forged_frontmatter(content):
                logger.warning(
                    "Writeback refused for learning #%d: content contains forged frontmatter fence (---) or fenced code (SEC-018)",
                    learning_id,
                )
                return

            md_content = f"""---
learning_id: {learning_id}
agent: {agent_id}
category: {category}
created: {dt.isoformat()}
---

# {category.title()}: {content[:80]}

{content}
"""
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md_content)

            logger.debug("Wrote learning to disk: %s", filepath)
        except Exception as e:
            logger.warning("Failed to write learning to disk: %s", e)

    def _contains_forged_frontmatter(self, content: str) -> bool:
        """Return True if content has a bare '---' line or a code fence containing one.
        Prevents the written .md from having its frontmatter "re-forged" by body content.

        NOTE (G09 / Issue 7): This is a heuristic and will refuse a *legitimate* fenced
        code block that happens to contain a '---' line (e.g. a YAML example in docs).
        The attack surface (learn body forging attribution) is considered higher priority
        than occasional false-positive refusals on disk writeback. The DB learn still succeeds.
        """
        if not content or "---" not in content:
            return False
        for line in content.splitlines():
            if line.strip() == "---":
                return True
        if "```" in content:
            parts = content.split("```")
            for i in range(1, len(parts), 2):
                if "---" in parts[i]:
                    for ln in parts[i].splitlines():
                        if ln.strip() == "---":
                            return True
        return False

    def detect_contradictions(
        self,
        content_or_assertion: str,
        agent_id: Optional[str] = None,
        threshold: Optional[float] = None,
    ) -> List[Dict]:
        """
        Detect active learnings that semantically contradict the given text.

        Uses cosine similarity between the embedded assertion and embeddings of
        all active learnings (status not in superseded/rejected/expired, and
        superseded_by IS NULL).

        Args:
            content_or_assertion: The text to check against existing learnings.
            agent_id: Optional — if supplied, scope detection to this agent.
            threshold: Override the config contradiction_threshold.

        Returns:
            List of candidate dicts, each containing:
              id, content, assertion, score, agent_id, created_at
            Sorted by score descending (highest similarity first).

        Graceful failure: if embedding is unavailable, logs a warning and
        returns [] so the caller can proceed with the write.
        """
        if not self.model:
            logger.warning(
                "detect_contradictions: embedding model unavailable — skipping detection"
            )
            return []

        np = _numpy()
        effective_threshold = (
            threshold if threshold is not None else self.config.contradiction_threshold
        )

        try:
            from minni.models import get_embedder_lock

            with get_embedder_lock():
                query_emb = self.model.encode(
                    content_or_assertion, show_progress_bar=False
                ).astype(np.float32)
        except Exception as exc:
            logger.warning(
                "detect_contradictions: failed to embed assertion (%s) — skipping detection",
                exc,
            )
            return []

        query_norm = np.linalg.norm(query_emb)
        if query_norm < 1e-8:
            return []
        query_emb = query_emb / query_norm

        candidates = []
        with self.db.cursor() as c:
            # Active learnings: not superseded via superseded_by FK, and
            # status column (if present) not in terminal states.
            sql = """
                SELECT learning_id, agent_id, content, assertion,
                       confidence, created_at, embedding
                FROM learnings
                WHERE superseded_by IS NULL
                  AND embedding IS NOT NULL
                  AND (status IS NULL OR status NOT IN ('superseded', 'rejected', 'expired'))
            """
            params = []
            if agent_id:
                sql += " AND agent_id = ?"
                params.append(agent_id)

            try:
                c.execute(sql, params)
                rows = c.fetchall()
            except Exception as exc:
                logger.warning(
                    "detect_contradictions: DB query failed (%s) — skipping detection",
                    exc,
                )
                return []

        for row in rows:
            try:
                emb = np.frombuffer(row["embedding"], dtype=np.float32)
                norm = np.linalg.norm(emb)
                if norm < 1e-8:
                    continue
                sim = float(np.dot(query_emb, emb / norm))
                if sim > effective_threshold:
                    candidates.append({
                        "id": row["learning_id"],
                        "content": row["content"],
                        "assertion": row["assertion"],
                        "score": round(sim, 4),
                        "agent_id": row["agent_id"],
                        "created_at": datetime.fromtimestamp(row["created_at"]).isoformat(),
                    })
            except Exception as exc:
                logger.debug("detect_contradictions: skipping row due to error: %s", exc)
                continue

        candidates.sort(key=lambda x: x["score"], reverse=True)

        # G18: log every detected contradiction for audit + candidate surfacing
        # (rows preserved; resolution_id wired on resolve_candidate)
        try:
            now = time.time()
            # Probe once per invocation: baseline 009 lacks resolution_status
            # (021 unavailable is non-fatal), so use the legacy column shape
            # there instead of dropping every audit row under the catch below.
            with self.db.cursor() as probe:
                log_cols = {
                    row[1]
                    for row in probe.execute(
                        "PRAGMA table_info(contradiction_log)"
                    ).fetchall()
                }
            typed_log = "resolution_status" in log_cols
            for cand in candidates:
                with self.db.cursor() as c:
                    if typed_log:
                        c.execute(
                            """
                            INSERT INTO contradiction_log
                            (memory_a_id, memory_b_id, detected_at, detection_method,
                             resolution_status)
                            VALUES (?, ?, ?, ?, 'legacy_unclassified')
                            """,
                            (cand.get("id"), None, now, "cosine"),
                        )
                    else:
                        c.execute(
                            """
                            INSERT INTO contradiction_log
                            (memory_a_id, memory_b_id, detected_at, detection_method)
                            VALUES (?, ?, ?, ?)
                            """,
                            (cand.get("id"), None, now, "cosine"),
                        )
        except Exception as exc:
            logger.debug("contradiction_log insert skipped (non-fatal): %s", exc)

        return candidates

    def get_stats(self) -> Dict:
        """Get write-back memory statistics."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(CASE WHEN superseded_by IS NULL THEN 1 END) as active,
                    COUNT(CASE WHEN superseded_by IS NOT NULL THEN 1 END) as superseded
                FROM learnings
            """)
            row = c.fetchone()
            total = row["total"]
            active = row["active"]

            # Per-category breakdown
            c.execute("""
                SELECT category, COUNT(*) as count
                FROM learnings
                WHERE superseded_by IS NULL
                GROUP BY category
                ORDER BY count DESC
            """)
            categories = {row["category"]: row["count"] for row in c.fetchall()}

            # Per-agent breakdown
            c.execute("""
                SELECT agent_id, COUNT(*) as count
                FROM learnings
                WHERE superseded_by IS NULL
                GROUP BY agent_id
                ORDER BY count DESC
            """)
            agents = {row["agent_id"]: row["count"] for row in c.fetchall()}

        return {
            "total_learnings": total,
            "active": active,
            "superseded": row["superseded"],
            "by_category": categories,
            "by_agent": agents,
        }

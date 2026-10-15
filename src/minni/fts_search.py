"""SQLite FTS5 lexical search, BM25 ranking, chronological search, and slot reservation."""

import logging
import re
import sqlite3
import time
from typing import Dict, List, Optional, Sequence

from minni.episodic import NON_MEMORY_EVENT_TYPES as _NON_MEMORY_EVENT_TYPES
from minni.request_deadline import RequestDeadlineExceeded

logger = logging.getLogger("sovereign.retrieval")

_FTS_RETRY_ATTEMPTS = 3
_FTS_RETRY_BACKOFFS = (0.05, 0.1, 0.2)
_FTS_TRANSIENT_MARKERS = ("vtable constructor failed", "schema has changed")

_FTS_CHUNK_TEXT_EXPR = """COALESCE(
  (SELECT ce.chunk_text FROM chunk_embeddings ce
   WHERE ce.doc_id = d.doc_id
   ORDER BY ce.chunk_index LIMIT 1),
  f.content
)"""


def _strip_leading_frontmatter(text: str) -> str:
    """Drop a leading closed ``---`` YAML block from FTS/chunk body text."""
    if not text or not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end != -1:
        rest = text[end + 4 :]
        if rest.startswith("\n"):
            rest = rest[1:]
        return rest
    collapsed = re.match(r"^---\s+.+?\s+---\s*", text, flags=re.DOTALL)
    if collapsed:
        return text[collapsed.end() :]
    return text


def _fts_execute_with_retry(
    cursor, sql, params, *, attempts: Optional[int] = None
):
    """Execute a vault_fts MATCH select and fetch its rows with bounded retry
    on transient schema-cookie / vtable-reconnect races."""
    import minni.retrieval as _ret

    effective_attempts = (
        int(_ret._FTS_RETRY_ATTEMPTS) if attempts is None else int(attempts)
    )
    markers = getattr(_ret, "_FTS_TRANSIENT_MARKERS", _FTS_TRANSIENT_MARKERS)
    backoffs = getattr(_ret, "_FTS_RETRY_BACKOFFS", _FTS_RETRY_BACKOFFS)
    last_exc: Optional[sqlite3.OperationalError] = None
    for attempt in range(effective_attempts):
        try:
            cursor.execute(sql, params)
            return cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if not any(m in str(exc).lower() for m in markers):
                raise
            last_exc = exc
            if attempt + 1 >= effective_attempts:
                break
            _ret.time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
    assert last_exc is not None
    raise last_exc


class FtsSearchMixin:
    """FTS5 lexical search, chronological search, learnings/episodic FTS, and slot reservation."""

    EPISODIC_NON_MEMORY_TYPES: tuple = _NON_MEMORY_EVENT_TYPES

    @staticmethod
    def _normalize_agent_filter(agent_filter: Optional[Sequence[str]]) -> list[str]:
        if agent_filter is None:
            return []
        raw = [agent_filter] if isinstance(agent_filter, str) else list(agent_filter)
        scope: list[str] = []
        seen: set[str] = set()
        for item in raw:
            agent = str(item).strip()
            if agent and agent not in seen:
                scope.append(agent)
                seen.add(agent)
        return scope

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Sanitize query for FTS5 MATCH syntax."""
        cleaned = re.sub(r"[^\w\s]", " ", query)
        words = cleaned.split()
        return " ".join(words) if words else ""

    def _fts_search(
        self,
        query: str,
        limit: int,
        agent_filter: Optional[Sequence[str]] = None,
        exclude_statuses: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        """FTS5 search using BM25 ranking with SQL-level lifecycle exclusions."""
        import minni.retrieval as _ret

        results: List[Dict] = []
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return results
        agent_scope = self._normalize_agent_filter(agent_filter)

        with self.db.cursor() as c:
            agent_clause = ""
            scope_params: list = []
            if agent_scope:
                agent_clause = f" AND d.agent IN ({','.join('?' * len(agent_scope))})"
                scope_params.extend(agent_scope)
            skip = [str(s) for s in (exclude_statuses or [])]
            if skip:
                agent_clause += (
                    " AND COALESCE(d.page_status, 'candidate') NOT IN "
                    f"({','.join('?' * len(skip))})"
                )
                scope_params.extend(skip)

            def _match(match_expr: str):
                return _ret._fts_execute_with_retry(
                    c,
                    f"""
                    SELECT m.doc_id, m.path, m.agent, m.sigil,
                           m.bm25_rank, m.decay_score,
                           m.page_status, m.privacy_level, m.page_type,
                           m.evidence_refs, m.indexed_at, m.layer,
                           COALESCE(
                               (SELECT ce.chunk_text FROM chunk_embeddings ce
                                WHERE ce.doc_id = m.doc_id
                                ORDER BY ce.chunk_index LIMIT 1),
                               m.content
                           ) AS chunk_text
                    FROM (
                        SELECT f.doc_id, f.content, d.path, d.agent, d.sigil,
                               rank AS bm25_rank, d.decay_score,
                               d.page_status, d.privacy_level, d.page_type,
                               d.evidence_refs, d.indexed_at, d.layer
                        FROM vault_fts f
                        JOIN documents d ON d.doc_id = f.doc_id
                        WHERE vault_fts MATCH ?
                        {agent_clause}
                        ORDER BY rank
                        LIMIT ?
                    ) m
                    """,
                    [match_expr, *scope_params, limit * 3],
                )

            terms = safe_query.split()
            lowered = [t.lower() for t in terms]
            selective = (
                self._selective_or_terms(lowered) if len(terms) > 1 else lowered
            )
            strict_q = (
                " ".join(selective)
                if (
                    selective
                    and len(selective) < len(lowered)
                    and self._is_large_corpus()
                )
                else safe_query
            )
            rows = _match(strict_q)
            if not rows and len(terms) > 1:
                if selective and len(selective) < len(lowered):
                    rows = _match(" OR ".join(selective))
                if not rows:
                    rows = _match(" OR ".join(lowered))

            for row in rows:
                results.append({
                    "doc_id": row["doc_id"],
                    "path": row["path"],
                    "agent": row["agent"],
                    "sigil": row["sigil"],
                    "bm25_rank": row["bm25_rank"],
                    "decay_score": row["decay_score"] or 1.0,
                    "page_status": row["page_status"] or "candidate",
                    "privacy_level": row["privacy_level"] or "safe",
                    "page_type": row["page_type"],
                    "evidence_refs": row["evidence_refs"],
                    "indexed_at": row["indexed_at"],
                    "layer": row["layer"] or "knowledge",
                    "chunk_text": _ret._strip_leading_frontmatter(row["chunk_text"] or ""),
                })

        return results

    def _reserve_top_fts_slots(
        self,
        ranked: List[Dict],
        fts_results: List[Dict],
        doc_scores: Dict[int, Dict],
        limit: int,
    ) -> List[Dict]:
        """Reserve up to min(10, len(fts_results), limit) slots in the top-limit
        window for top FTS5 BM25 hits so unembedded candidate notes and exact
        Rank-1 lexical matches are never evicted by semantic-only neighbors."""
        top_window = ranked[:limit]
        if not fts_results or limit <= 0:
            return top_window
        fts_quota = min(10, len(fts_results), limit)
        present_ids = {r["doc_id"] for r in top_window}
        missing_fts: List[Dict] = []
        seen_missing: set[int] = set()
        for r in fts_results[:fts_quota]:
            did = r.get("doc_id")
            if did in doc_scores and did not in present_ids and did not in seen_missing:
                missing_fts.append(doc_scores[did])
                seen_missing.add(did)
        if not missing_fts:
            return top_window
        keep_count = max(0, limit - len(missing_fts))
        kept = [
            r
            for r in top_window
            if r.get("fts_rank") is not None and r["fts_rank"] <= fts_quota
        ]
        for r in top_window:
            if len(kept) >= keep_count:
                break
            if r not in kept:
                kept.append(r)
        return sorted(
            kept + missing_fts,
            key=lambda x: x["final_score"],
            reverse=True,
        )[:limit]

    def _normalize_layers(self, layers: Optional[Sequence[str]]) -> Optional[set]:
        """None -> no filter; bare string -> single layer; invalid -> empty set (fail closed)."""
        if layers is None:
            return None
        if isinstance(layers, str):
            layers = [layers]
        valid = {"identity", "episodic", "knowledge", "artifact"}
        return {str(layer).lower() for layer in layers if str(layer).lower() in valid}

    def _parse_iso_date(
        self, value: Optional[str], end_of_day: bool = False
    ) -> Optional[float]:
        if not value:
            return None
        from datetime import datetime, time as dt_time

        text = str(value)
        try:
            if len(text) == 10:
                day = datetime.fromisoformat(text)
                if end_of_day:
                    day = datetime.combine(day.date(), dt_time.max)
                return day.timestamp()
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            logger.warning("Ignoring invalid ISO date filter: %r", value)
            return None

    def _filter_candidates(
        self,
        candidates: List[Dict],
        layers: Optional[Sequence[str]],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> List[Dict]:
        import minni.retrieval as _ret

        layer_set = self._normalize_layers(layers)
        start_ts = self._parse_iso_date(start_date)
        end_ts = self._parse_iso_date(end_date, end_of_day=True)
        if layer_set is None and start_ts is None and end_ts is None:
            return candidates
        filtered = []
        for r in candidates:
            if layer_set is not None and (r.get("layer") or "knowledge") not in layer_set:
                continue
            if start_ts is None and end_ts is None:
                filtered.append(r)
                continue
            created_at = _ret.parse_epoch_or_report(
                r.get("created_at"),
                field="created_at",
                source="retrieval._filter_candidates",
                doc_id=r.get("doc_id"),
            )
            if created_at is None:
                created_at = _ret.parse_epoch_or_report(
                    r.get("indexed_at"),
                    field="indexed_at",
                    source="retrieval._filter_candidates",
                    doc_id=r.get("doc_id"),
                )
            if (
                created_at is None
                and r.get("created_at") is None
                and r.get("indexed_at") is None
            ):
                created_at = 0.0
            if created_at is None:
                continue
            if start_ts is not None and created_at < start_ts:
                continue
            if end_ts is not None and created_at > end_ts:
                continue
            filtered.append(r)
        return filtered

    def _chronological_search(
        self,
        query: str,
        limit: int,
        layers: Optional[Sequence[str]],
        start_date: Optional[str],
        end_date: Optional[str],
        exclude_statuses: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        import minni.retrieval as _ret

        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return []
        layer_set = self._normalize_layers(layers)
        start_ts = self._parse_iso_date(start_date)
        end_ts = self._parse_iso_date(end_date, end_of_day=True)
        filter_params: list = []
        clauses = ["vault_fts MATCH ?"]
        if layer_set is not None and not layer_set:
            return []
        if layer_set:
            placeholders = ",".join("?" * len(layer_set))
            clauses.append(
                f"COALESCE(ce.layer, d.layer, 'knowledge') IN ({placeholders})"
            )
            filter_params.extend(sorted(layer_set))
        if start_ts is not None:
            clauses.append(
                "COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) >= ?"
            )
            filter_params.append(start_ts)
        if end_ts is not None:
            clauses.append(
                "COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) <= ?"
            )
            filter_params.append(end_ts)
        skip = [str(s) for s in (exclude_statuses or [])]
        if skip:
            clauses.append(
                "COALESCE(d.page_status, 'candidate') NOT IN "
                f"({','.join('?' * len(skip))})"
            )
            filter_params.extend(skip)

        with self.db.cursor() as c:
            def _match(match_expr: str):
                return _ret._fts_execute_with_retry(
                    c,
                    f"""
                    SELECT ce.chunk_id, d.doc_id,
                           {_FTS_CHUNK_TEXT_EXPR} AS chunk_text,
                           ce.heading_context,
                           d.path, d.agent, d.sigil, d.decay_score,
                           d.page_status, d.privacy_level, d.page_type,
                           d.evidence_refs, d.indexed_at,
                           COALESCE(ce.layer, d.layer, 'knowledge') AS layer,
                           COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) AS created_at
                    FROM vault_fts f
                    JOIN documents d ON d.doc_id = f.doc_id
                    LEFT JOIN chunk_embeddings ce ON ce.doc_id = d.doc_id
                    WHERE {" AND ".join(clauses)}
                    ORDER BY created_at ASC, ce.chunk_id ASC
                    LIMIT ?
                    """,
                    [match_expr, *filter_params, limit],
                )

            rows = _match(safe_query)
            terms = safe_query.split()
            if not rows and len(terms) > 1:
                rows = _match(" OR ".join(t.lower() for t in terms))

        return [
            {
                "doc_id": row["doc_id"],
                "chunk_id": row["chunk_id"],
                "path": row["path"],
                "agent": row["agent"],
                "sigil": row["sigil"],
                "final_score": 0.0,
                "rrf_score": None,
                "fts_rank": None,
                "sem_rank": None,
                "chunk_text": _ret._strip_leading_frontmatter(row["chunk_text"] or ""),
                "heading_context": row["heading_context"] or "",
                "decay_score": row["decay_score"] or 1.0,
                "page_status": row["page_status"] or "candidate",
                "privacy_level": row["privacy_level"] or "safe",
                "page_type": row["page_type"],
                "evidence_refs": row["evidence_refs"],
                "indexed_at": row["indexed_at"],
                "created_at": row["created_at"],
                "layer": row["layer"] or "knowledge",
            }
            for row in rows
        ]

    def search_episodic(
        self,
        query: str,
        agent_id: Optional[str] = None,
        limit: int = 10,
        exclude_event_types: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        """Search episodic events via FTS5."""
        safe_q = self._sanitize_fts_query(query)
        if not safe_q:
            return []

        excluded = [str(t) for t in (exclude_event_types or ())]
        type_clause = ""
        type_params: list = []
        if excluded:
            type_clause = (
                " AND (e.event_type IS NULL OR e.event_type NOT IN "
                f"({','.join('?' * len(excluded))}))"
            )
            type_params = excluded

        results = []
        with self.db.cursor() as c:
            if agent_id:
                c.execute(
                    f"""
                    SELECT ef.event_id, ef.agent_id, ef.content, e.event_type,
                           e.task_id, e.thread_id, e.created_at
                    FROM episodic_fts ef
                    JOIN episodic_events e ON e.event_id = ef.event_id
                    WHERE episodic_fts MATCH ? AND ef.agent_id = ?{type_clause}
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (safe_q, agent_id, *type_params, limit),
                )
            else:
                c.execute(
                    f"""
                    SELECT ef.event_id, ef.agent_id, ef.content, e.event_type,
                           e.task_id, e.thread_id, e.created_at
                    FROM episodic_fts ef
                    JOIN episodic_events e ON e.event_id = ef.event_id
                    WHERE episodic_fts MATCH ?{type_clause}
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (safe_q, *type_params, limit),
                )

            for row in c.fetchall():
                results.append(dict(row))

        return results

    def search_learnings(
        self,
        query: str,
        agent_id: Optional[str] = None,
        agent_scope: Optional[Sequence[str]] = None,
        cross_agent: bool = False,
        limit: int = 10,
        source: str = "retrieval.search_learnings",
        update_access: bool = True,
    ) -> List[Dict]:
        """Search write-back learnings via FTS5 and record learning_reads."""
        import minni.retrieval as _ret

        safe_q = self._sanitize_fts_query(query)
        if not safe_q:
            return []
        if cross_agent:
            scope = []
        elif agent_scope is not None:
            scope = self._normalize_agent_filter(agent_scope)
        elif agent_id:
            scope = _ret.agent_scope_for(agent_id)
        else:
            scope = []

        def _match(match_q: str) -> List[Dict]:
            rows = []
            with self.db.cursor() as c:
                if scope:
                    placeholders = ",".join("?" * len(scope))
                    c.execute(
                        f"""
                        SELECT lf.learning_id, lf.agent_id, lf.content, lf.category,
                               l.confidence, l.created_at, l.access_count
                        FROM learnings_fts lf
                        JOIN learnings l ON l.learning_id = lf.learning_id
                        WHERE learnings_fts MATCH ? AND lf.agent_id IN ({placeholders})
                              AND l.superseded_by IS NULL
                              AND (l.status IS NULL OR l.status NOT IN ('rejected','expired','superseded'))
                        ORDER BY rank
                        LIMIT ?
                        """,
                        [match_q, *scope, limit],
                    )
                else:
                    c.execute(
                        """
                        SELECT lf.learning_id, lf.agent_id, lf.content, lf.category,
                               l.confidence, l.created_at, l.access_count
                        FROM learnings_fts lf
                        JOIN learnings l ON l.learning_id = lf.learning_id
                        WHERE learnings_fts MATCH ?
                              AND l.superseded_by IS NULL
                              AND (l.status IS NULL OR l.status NOT IN ('rejected','expired','superseded'))
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (match_q, limit),
                    )
                for row in c.fetchall():
                    rows.append(dict(row))
            return rows

        terms = safe_q.split()
        lowered = [t.lower() for t in terms]
        selective = (
            self._selective_or_terms(lowered) if len(terms) > 1 else lowered
        )
        strict_q = (
            " ".join(selective)
            if (
                selective
                and len(selective) < len(lowered)
                and self._is_large_corpus()
            )
            else safe_q
        )
        results = _match(strict_q)
        if not results and len(terms) > 1:
            if selective and len(selective) < len(lowered):
                results = _match(" OR ".join(selective))
            if not results:
                results = _match(" OR ".join(lowered))

        if results and update_access:
            now = _ret.time.time()
            try:
                with self.db.cursor() as c:
                    for result in results[:limit]:
                        c.execute(
                            """UPDATE learnings
                               SET access_count = access_count + 1, last_accessed = ?
                               WHERE learning_id = ?""",
                            (now, result["learning_id"]),
                        )
                        try:
                            c.execute(
                                """INSERT OR IGNORE INTO learning_reads
                                   (learning_id, agent_id, read_at, source)
                                   VALUES (?, ?, ?, ?)""",
                                (
                                    result["learning_id"],
                                    agent_id or "unknown",
                                    now,
                                    source,
                                ),
                            )
                        except RequestDeadlineExceeded:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "learning_reads insert failed for learning #%s: %s",
                                result.get("learning_id"),
                                exc,
                            )
            except RequestDeadlineExceeded:
                pass

        return results

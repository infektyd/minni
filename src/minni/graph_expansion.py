"""P1.4 read-only 1-hop typed-graph expansion (spec §3.1–3.2).

Privacy-safe neighbor generation for a later retrieve() slot between RRF and
CE. This module is not invoked from retrieval yet.

Evidence (not inferred from spec alone):
- ``memory_links`` PK is ``(source_doc_id, target_doc_id, link_type)``
  (db.py CREATE TABLE + graph_readiness expected PK).
- Typed columns include ``edge_status`` default ``active``, ``confidence``,
  ``weight`` on the baseline table (db.py + 021_typed_memory_graph.sql).
- ``can_read_document`` denies ``principal is None`` and ``privacy=blocked``
  (principal.py).
- Phase 1 learning filter matches graph_candidates.REQUIRED_* and
  graph_commit GRAPH_MEMORY_KIND.
- Integer doc_ids are store-local (spec §3.2); identity is the real DB path
  plus doc_id, never a caller-chosen label.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from minni.graph_readiness import verify_graph_schema
from minni.principal import EffectivePrincipal, can_read_document
from minni.request_deadline import (
    RequestDeadlineExceeded,
    check_deadline,
    current_deadline,
    request_deadline,
)

logger = logging.getLogger("sovereign.graph_expansion")

# Spec §3.2 type factors. Phase 1 ships 1-hop only (depth cap unused here).
TYPE_FACTORS: Dict[str, float] = {
    "updates": 1.00,
    "contradicts": 0.95,
    "extends": 0.85,
    "derived_from": 0.75,
    "wikilink": 0.70,
    "relates": 0.55,
}
ALLOWED_LINK_TYPES = frozenset(TYPE_FACTORS)
BIDIRECTIONAL_TYPES = frozenset({"relates", "contradicts"})

MAX_SEEDS = 8
MAX_NEIGHBORS_PER_SEED = 6
MAX_GRAPH_CANDIDATES = 12
# Ranked-seed envelope: caller must pass already-ranked seeds (score desc,
# then doc_id). Only this many sequence items are inspected — no full-list
# sort of an unbounded custom sequence.
MAX_SEED_PREFIX = MAX_SEEDS
# Page size for deadline-bounded edge walks. Not a privacy cap: pages
# continue until 6 distinct eligible neighbors, exhaustion, or deadline.
# Spec §2.2's 48 is the FAISS shortlist, not this walk.
EDGE_PAGE_SIZE = 32

REQUIRED_MEMORY_KIND = "learning"
REQUIRED_PAGE_TYPE = "learning"
_CLOSED_STATUSES = frozenset({"draft", "expired", "rejected", "superseded"})
_CLOSED_SQL = ",".join("?" * len(_CLOSED_STATUSES))
_TYPE_SQL = ",".join("?" * len(ALLOWED_LINK_TYPES))
_SORTED_TYPES = tuple(sorted(ALLOWED_LINK_TYPES))

_SEED_META_SQL = """
SELECT doc_id, path, agent, sigil, privacy_level, page_status, page_type, memory_kind
FROM documents
WHERE doc_id = ?
"""

# Scope predicates encode Phase-1 eligibility (kind/type/closed/blocked).
# Authorization (can_read_document) still runs in Python. No LIMIT that can
# hide an eligible neighbor behind denied rows.
_EDGE_PAGE_SQL = """
SELECT
    ml.source_doc_id,
    ml.target_doc_id,
    ml.link_type,
    ml.weight,
    ml.confidence,
    ml.inference_run_id,
    ml.model_id,
    ml.inference_method,
    d.doc_id AS neighbor_doc_id,
    d.path,
    d.agent,
    d.sigil,
    d.privacy_level,
    d.page_status,
    d.page_type,
    d.memory_kind,
    {direction!r} AS direction
FROM memory_links ml
JOIN documents d ON d.doc_id = ml.{neighbor_col}
WHERE ml.{seed_col} = ?
  AND ml.edge_status = 'active'
  AND ml.link_type IN ({types})
  AND ml.source_doc_id != ml.target_doc_id
  AND d.memory_kind = ?
  AND lower(COALESCE(d.page_type, '')) = ?
  AND lower(COALESCE(d.privacy_level, 'safe')) != 'blocked'
  AND COALESCE(d.page_status, 'candidate') NOT IN ({closed})
  AND (d.doc_id, ml.link_type) > (?, ?)
ORDER BY d.doc_id, ml.link_type
LIMIT ?
"""


@dataclass(frozen=True)
class GraphExpansionResult:
    """Graph-derived neighbors only. Seeds are never echoed back.

    No withheld-count fields (addendum §3 / §6.1).
    """

    neighbors: Tuple[Dict[str, Any], ...] = ()
    graph_status: str = "ok"


def expand_typed_graph(
    *,
    db: Any,
    store_id: str,
    seeds: Sequence[Mapping[str, Any]],
    principal: Optional[EffectivePrincipal],
    workspace: str = "default",
    deadline_monotonic: Optional[float] = None,
) -> GraphExpansionResult:
    """Return ≤12 privacy-safe 1-hop learning neighbors for authorized seeds.

    Designed to sit between RRF fusion and CE. Retrieval is not wired to
    this function in this slice.

    Seeds must be a **pre-ranked envelope** (highest first), each tagged
    with ``store_id`` equal to this connection's ``PRAGMA database_list``
    main-file realpath. Only the first ``MAX_SEED_PREFIX`` items are read.

    A finite ``deadline_monotonic`` (or active ``request_deadline``) is
    required and is installed around connection, readiness, the read
    snapshot, SQL, lock waits, and hydration. There is no deadline-free
    graph leg.

    Seed authorization, neighbor authorization, and text hydration share
    one read snapshot (``BEGIN DEFERRED`` or a SAVEPOINT). That snapshot
    is rolled back or released — never committed — so this helper cannot
    commit caller writes. ``SovereignDB.cursor()`` is not used: its
    success path commits, and SELECT alone does not open a transaction.
    WAL readers keep the snapshot if another connection commits mid-walk.
    """
    if not isinstance(workspace, str) or not workspace:
        raise ValueError("workspace must be a non-empty string")
    if principal is None or not isinstance(principal, EffectivePrincipal):
        return GraphExpansionResult(graph_status="disabled")
    if not isinstance(store_id, str) or not store_id.strip():
        raise ValueError("store_id must be a non-empty opaque storage identity")
    deadline = _validated_deadline(deadline_monotonic)
    if deadline is None:
        return GraphExpansionResult(graph_status="disabled")

    ranked_seeds = _bounded_seeds(seeds, store_id)
    if not ranked_seeds:
        return GraphExpansionResult(graph_status="ok")

    collected: List[Dict[str, Any]] = []
    bound = store_id
    try:
        with request_deadline(deadline):
            check_deadline()
            conn = _connection(db)
            bound = _bound_store_id(conn)
            if store_id != bound:
                raise ValueError(
                    f"store_id {store_id!r} does not match db identity {bound!r}: "
                    "cross-corpus aliasing"
                )
            with _read_snapshot(conn) as cursor:
                check_deadline()
                report = verify_graph_schema(conn)
                check_deadline()
                if not report.ready:
                    return GraphExpansionResult(graph_status="schema_missing")
                authorized_seeds = _authorize_seeds(
                    cursor,
                    ranked_seeds,
                    store_id=bound,
                    principal=principal,
                    workspace=workspace,
                )
                seed_ids = {item[0] for item in authorized_seeds}
                for seed_doc_id, seed_score in authorized_seeds:
                    check_deadline()
                    collected.extend(
                        _eligible_neighbors_for_seed(
                            cursor,
                            store_id=bound,
                            seed_doc_id=seed_doc_id,
                            seed_score=seed_score,
                            seed_ids=seed_ids,
                            principal=principal,
                            workspace=workspace,
                        )
                    )
    except RequestDeadlineExceeded:
        return GraphExpansionResult(
            neighbors=_finalize(collected, bound),
            graph_status="degraded",
        )
    except ValueError:
        raise
    except Exception:
        logger.warning("graph expansion: query failed", exc_info=True)
        return GraphExpansionResult(graph_status="degraded")

    return GraphExpansionResult(
        neighbors=_finalize(collected, bound),
        graph_status="ok",
    )


_SNAPSHOT_SAVEPOINT_PREFIX = "minni_graph_expand_"


def _bound_store_id(conn: Any) -> str:
    """Canonical on-disk identity of the opened main database.

    ``PRAGMA database_list`` file + ``realpath``, not ``config.db_path``.
    """
    rows = conn.execute("PRAGMA database_list").fetchall()
    file = None
    for row in rows:
        name = _row_value(row, "name", 1)
        if str(name) == "main":
            file = _row_value(row, "file", 2)
            break
    if not file:
        raise ValueError("expected a real on-disk main database")
    return os.path.realpath(str(file))


@contextmanager
def _read_snapshot(conn):
    """Read-only snapshot for auth + hydration. Never commits.

    Production ``SovereignDB.cursor()`` commits on success and SELECT does
    not begin a transaction (isolation ``""``). This helper opens
    ``BEGIN DEFERRED`` when the connection is idle, or a SAVEPOINT when
    the caller already has a transaction, then ROLLBACK/RELEASE. DEFERRED
    is the WAL reader snapshot: a second connection may commit, we do not
    see mixed-version rows.
    """
    check_deadline()
    cur = conn.cursor()
    nested = bool(getattr(conn, "in_transaction", False))
    savepoint = _SNAPSHOT_SAVEPOINT_PREFIX + uuid.uuid4().hex
    created = False
    try:
        if nested:
            cur.execute(f"SAVEPOINT {savepoint}")
        else:
            cur.execute("BEGIN DEFERRED")
        created = True
        yield cur
    finally:
        try:
            if created:
                if nested:
                    # Like BudgetConnection.rollback(), cleanup must survive
                    # request expiry. Bypass only for these fixed control
                    # statements on our successfully created unique savepoint;
                    # never relax the budget for caller SQL or hydration.
                    sqlite3.Cursor.execute(cur, f"ROLLBACK TO SAVEPOINT {savepoint}")
                    sqlite3.Cursor.execute(cur, f"RELEASE SAVEPOINT {savepoint}")
                else:
                    conn.rollback()
        finally:
            cur.close()


def _validated_deadline(deadline_monotonic: Optional[float]) -> Optional[float]:
    ctx = current_deadline()
    if deadline_monotonic is None:
        chosen = ctx
    elif (
        isinstance(deadline_monotonic, bool)
        or not isinstance(deadline_monotonic, (int, float))
        or not math.isfinite(deadline_monotonic)
    ):
        return None
    elif ctx is None:
        chosen = float(deadline_monotonic)
    else:
        chosen = min(float(deadline_monotonic), float(ctx))
    if chosen is None:
        return None
    if not math.isfinite(chosen):
        return None
    return chosen


def _connection(db: Any):
    getter = getattr(db, "_get_conn", None)
    if callable(getter):
        return getter()
    if hasattr(db, "execute"):
        return db
    raise TypeError("db must be a SovereignDB or sqlite3 connection")


def _is_doc_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_unit(value: Any) -> Optional[float]:
    """Finite numeric in [0, 1], or None if absent/invalid. Never invents 1.0."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return None
    return score


def _legacy_or_unit(value: Any) -> Optional[float]:
    """NULL/missing → 1.0 (legacy explicit). Invalid typed numeric → None (drop)."""
    if value is None:
        return 1.0
    return _finite_unit(value)


def _legacy_or_weight(value: Any) -> Optional[float]:
    """NULL/missing → 1.0. Non-finite or negative weight is invalid (drop)."""
    if value is None:
        return 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    weight = float(value)
    if not math.isfinite(weight) or weight < 0.0:
        return None
    return weight


def _finite_score(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score):
        return None
    return score


def _bounded_seeds(
    seeds: Sequence[Mapping[str, Any]], store_id: str
) -> List[Tuple[int, float]]:
    """Prefix-scan a pre-ranked seed envelope.

    Does not sort or walk an unbounded sequence. Callers (RRF top-8) must
    already rank by score desc, doc_id asc. Only ``MAX_SEED_PREFIX`` items
    are inspected; missing ``store_id`` tags are skipped, not defaulted.
    """
    if isinstance(seeds, (str, bytes, dict)) or not hasattr(seeds, "__getitem__"):
        raise ValueError("seeds must be a sequence of ranked seed mappings")
    ranked: List[Tuple[int, float]] = []
    seen = set()
    try:
        prefix = min(len(seeds), MAX_SEED_PREFIX)
    except TypeError as exc:
        raise ValueError("seeds must be a sequence of ranked seed mappings") from exc
    for index in range(prefix):
        raw = seeds[index]
        if not isinstance(raw, Mapping):
            continue
        doc_id = raw.get("doc_id")
        score = _finite_score(raw.get("score", raw.get("rrf_score")))
        seed_store = raw.get("store_id")
        if not _is_doc_id(doc_id) or score is None:
            continue
        if seed_store is None:
            continue
        if seed_store != store_id:
            raise ValueError(
                f"seed doc_id {doc_id} store_id {seed_store!r} does not match "
                f"{store_id!r}: cross-corpus aliasing"
            )
        if doc_id in seen:
            continue
        seen.add(doc_id)
        ranked.append((doc_id, score))
        if len(ranked) >= MAX_SEEDS:
            break
    return ranked


def _authorize_seeds(
    cursor,
    ranked_seeds: Sequence[Tuple[int, float]],
    *,
    store_id: str,
    principal: EffectivePrincipal,
    workspace: str,
) -> List[Tuple[int, float]]:
    """Re-check seed metadata in this DB snapshot before any traversal/text."""
    allowed: List[Tuple[int, float]] = []
    for doc_id, score in ranked_seeds:
        check_deadline()
        cursor.execute(_SEED_META_SQL, (doc_id,))
        row = cursor.fetchone()
        if row is None:
            continue
        metadata = _document_metadata(row, store_id)
        if not _authorized(principal, workspace, metadata):
            continue
        if not _seed_usable(metadata):
            continue
        allowed.append((doc_id, score))
    return allowed


def _document_metadata(row: Any, store_id: str) -> Dict[str, Any]:
    doc_id = _row_value(row, "doc_id")
    return {
        "doc_id": doc_id,
        "store_id": store_id,
        "path": _row_value(row, "path"),
        "agent": _row_value(row, "agent"),
        "sigil": _row_value(row, "sigil"),
        "privacy_level": _row_value(row, "privacy_level") or "safe",
        "page_status": _row_value(row, "page_status") or "candidate",
        "page_type": _row_value(row, "page_type"),
        "memory_kind": _row_value(row, "memory_kind"),
    }


def _eligible_neighbors_for_seed(
    cursor,
    *,
    store_id: str,
    seed_doc_id: int,
    seed_score: float,
    seed_ids: set,
    principal: EffectivePrincipal,
    workspace: str,
) -> List[Dict[str, Any]]:
    best: Dict[int, Dict[str, Any]] = {}
    for direction, seed_col, neighbor_col in (
        ("outgoing", "source_doc_id", "target_doc_id"),
        ("incoming", "target_doc_id", "source_doc_id"),
    ):
        after_id = 0
        after_type = ""
        while True:
            check_deadline()
            rows = _fetch_edge_page(
                cursor,
                seed_doc_id,
                seed_col=seed_col,
                neighbor_col=neighbor_col,
                direction=direction,
                after_id=after_id,
                after_type=after_type,
            )
            if not rows:
                break
            for edge in rows:
                after_id = int(_row_value(edge, "neighbor_doc_id"))
                after_type = str(_row_value(edge, "link_type") or "")
                candidate = _consider_edge(
                    edge,
                    direction=direction,
                    store_id=store_id,
                    seed_doc_id=seed_doc_id,
                    seed_score=seed_score,
                    seed_ids=seed_ids,
                    principal=principal,
                    workspace=workspace,
                )
                if candidate is None:
                    continue
                doc_id = candidate["doc_id"]
                prev = best.get(doc_id)
                if prev is None or candidate["graph_score"] > prev["graph_score"] or (
                    candidate["graph_score"] == prev["graph_score"]
                    and candidate["link_type"] < prev["link_type"]
                ):
                    best[doc_id] = candidate
            if len(rows) < EDGE_PAGE_SIZE:
                break
    scored = sorted(best.values(), key=lambda item: (-item["graph_score"], item["doc_id"]))
    chosen = scored[:MAX_NEIGHBORS_PER_SEED]
    hydrated: List[Dict[str, Any]] = []
    for item in chosen:
        check_deadline()
        text, chunk_id, heading = _hydrate_text(cursor, item["doc_id"])
        item["chunk_text"] = text
        item["chunk_id"] = chunk_id
        item["heading_context"] = heading
        hydrated.append(item)
    return hydrated


def _fetch_edge_page(
    cursor,
    seed_doc_id: int,
    *,
    seed_col: str,
    neighbor_col: str,
    direction: str,
    after_id: int,
    after_type: str,
) -> List[Any]:
    sql = _EDGE_PAGE_SQL.format(
        direction=direction,
        seed_col=seed_col,
        neighbor_col=neighbor_col,
        types=_TYPE_SQL,
        closed=_CLOSED_SQL,
    )
    cursor.execute(
        sql,
        (
            seed_doc_id,
            *_SORTED_TYPES,
            REQUIRED_MEMORY_KIND,
            REQUIRED_PAGE_TYPE,
            *_CLOSED_STATUSES,
            after_id,
            after_type,
            EDGE_PAGE_SIZE,
        ),
    )
    return list(cursor.fetchall())


def _row_value(row: Any, key: str, index: int = None):
    if isinstance(row, Mapping):
        return row[key]
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if index is None:
            raise
        return row[index]


def _consider_edge(
    row: Any,
    *,
    direction: str,
    store_id: str,
    seed_doc_id: int,
    seed_score: float,
    seed_ids: set,
    principal: EffectivePrincipal,
    workspace: str,
) -> Optional[Dict[str, Any]]:
    link_type = str(_row_value(row, "link_type") or "")
    if link_type not in ALLOWED_LINK_TYPES:
        return None
    neighbor_id = _row_value(row, "neighbor_doc_id")
    if not _is_doc_id(neighbor_id) or neighbor_id == seed_doc_id:
        return None
    if neighbor_id in seed_ids:
        return None
    weight = _legacy_or_weight(_row_value(row, "weight"))
    confidence = _legacy_or_unit(_row_value(row, "confidence"))
    if weight is None or confidence is None:
        return None
    metadata = {
        "doc_id": neighbor_id,
        "store_id": store_id,
        "path": _row_value(row, "path"),
        "agent": _row_value(row, "agent"),
        "sigil": _row_value(row, "sigil"),
        "privacy_level": _row_value(row, "privacy_level") or "safe",
        "page_status": _row_value(row, "page_status") or "candidate",
        "page_type": _row_value(row, "page_type"),
        "memory_kind": _row_value(row, "memory_kind"),
    }
    if not _authorized(principal, workspace, metadata):
        return None
    if not _neighbor_eligible(metadata):
        return None
    factor = TYPE_FACTORS[link_type]
    graph_score = seed_score * factor * weight * confidence
    path = {
        "from_doc_id": seed_doc_id,
        "to_doc_id": neighbor_id,
        "link_type": link_type,
        "direction": direction,
        "confidence": confidence,
        "weight": weight,
        "inference_run_id": _row_value(row, "inference_run_id"),
        "model_id": _row_value(row, "model_id"),
        "inference_method": _row_value(row, "inference_method"),
        "store_id": store_id,
    }
    return {
        "doc_id": neighbor_id,
        "store_id": store_id,
        "path": metadata["path"],
        "source": metadata["path"],
        "agent": metadata["agent"],
        "sigil": metadata["sigil"],
        "privacy_level": metadata["privacy_level"],
        "page_status": metadata["page_status"],
        "page_type": metadata["page_type"],
        "memory_kind": metadata["memory_kind"],
        "score": graph_score,
        "graph_score": graph_score,
        "retrieval_origin": "graph",
        "seed_doc_id": seed_doc_id,
        "graph_paths": [path],
        "link_type": link_type,
    }


def _authorized(principal: EffectivePrincipal, workspace: str, metadata: Dict[str, Any]) -> bool:
    try:
        return bool(can_read_document(principal, workspace, metadata))
    except Exception:
        logger.warning("graph expansion: authorization check failed", exc_info=True)
        return False


def _seed_usable(metadata: Mapping[str, Any]) -> bool:
    """P1 learning sources may be superseded first-pass hits; blocked/wiki are not."""
    if metadata.get("memory_kind") != REQUIRED_MEMORY_KIND:
        return False
    if str(metadata.get("page_type") or "").lower() != REQUIRED_PAGE_TYPE:
        return False
    if str(metadata.get("privacy_level") or "safe").lower() == "blocked":
        return False
    return True


def _neighbor_eligible(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("memory_kind") != REQUIRED_MEMORY_KIND:
        return False
    if str(metadata.get("page_type") or "").lower() != REQUIRED_PAGE_TYPE:
        return False
    if str(metadata.get("privacy_level") or "safe").lower() == "blocked":
        return False
    if str(metadata.get("page_status") or "") in _CLOSED_STATUSES:
        return False
    return True


def _hydrate_text(cursor, doc_id: int) -> Tuple[str, Optional[int], str]:
    cursor.execute(
        """
        SELECT chunk_id, chunk_text, heading_context
        FROM chunk_embeddings
        WHERE doc_id = ?
        ORDER BY chunk_index
        LIMIT 1
        """,
        (doc_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return "", None, ""
    chunk_id = _row_value(row, "chunk_id", 0)
    text = _row_value(row, "chunk_text", 1) or ""
    heading = _row_value(row, "heading_context", 2) or ""
    try:
        chunk_id_int = int(chunk_id) if chunk_id is not None else None
    except (TypeError, ValueError):
        chunk_id_int = None
    return str(text), chunk_id_int, str(heading)


def _finalize(collected: Iterable[Dict[str, Any]], store_id: str) -> Tuple[Dict[str, Any], ...]:
    best: Dict[int, Dict[str, Any]] = {}
    for item in collected:
        if item.get("store_id") != store_id:
            continue
        doc_id = item["doc_id"]
        prev = best.get(doc_id)
        if prev is None or item["graph_score"] > prev["graph_score"] or (
            item["graph_score"] == prev["graph_score"] and doc_id < prev["doc_id"]
        ):
            best[doc_id] = item
    ordered = sorted(best.values(), key=lambda item: (-item["graph_score"], item["doc_id"]))
    capped = ordered[:MAX_GRAPH_CANDIDATES]
    for rank, item in enumerate(capped, start=1):
        item["graph_rank"] = rank
    return tuple(capped)

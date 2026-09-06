"""Atomic canonical-node commit step for governed learning promotion.

Design spec: docs/superpowers/specs/2026-07-09-typed-memory-graph-design.md
§1.2 (canonical learning nodes, N:1 by design) and §2.1 (one commit
coordinator).

Slice scope (deliberately minimal):
- Canonical ``documents`` row (``memory_kind='learning'``) + ``learning_documents``
  join row, written through the CALLER's cursor so they commit atomically with
  the learning row and explicit edges. No separate transaction is opened here.
- Edge inference is NOT invoked by this slice (the activation gate owns
  classifier invocation; ``graph_expansion_enabled`` stays default-off and the
  read path is untouched). Note: ``graph_classification_enabled`` defaults ON
  per config — this slice still performs no inference.
- Stores without the typed schema skip silently (baseline preserved). Full
  fail-loud promotion semantics belong to the activation gate, not this slice.
- Ownership/privacy/status come from the trusted ``durable_metadata``
  normalization (never invented): creation stamps production metadata, reuse
  preserves whatever the durable projection wrote and only adds the
  kind/uri mapping.
"""

import logging
from typing import Any, Optional

from minni.durable_projection import durable_doc_path

logger = logging.getLogger("sovereign.graph_commit")

GRAPH_MEMORY_KIND = "learning"


def graph_node_schema_present(cur: Any) -> bool:
    """True when the canonical-node tables/columns exist on this store.

    Only a genuinely absent table/column reads as absent: neither the
    sqlite_master lookup nor PRAGMA table_info raises for missing tables,
    so they yield False naturally. A schema READ failure (I/O error,
    closed or broken cursor/connection) propagates — a real store problem
    must never masquerade as a pre-graph baseline and silently skip the
    canonical commit.
    """
    tables = {
        row[0]
        for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "learning_documents" not in tables or "documents" not in tables:
        return False
    cols = {
        row[1] for row in cur.execute("PRAGMA table_info(documents)").fetchall()
    }
    return "memory_kind" in cols and "memory_uri" in cols


def ensure_canonical_learning_node(
    cur: Any,
    *,
    learning_id: int,
    agent_id: str,
    content: str,
    vault_path: str,
    created_at: float,
) -> Optional[int]:
    """Find-or-create the canonical document node for a learning.

    Identity is the content-keyed durable path ``(agent_id, content)``, so
    distinct learnings with identical content share one node (N:1 by design)
    while different agents never collide (the agent scopes the path). Lookup
    is by path ONLY: normal pre-graph projections already own a row at that
    path with ``memory_kind`` NULL, and filtering on kind would miss it and
    crash on the UNIQUE path.
    ``memory_uri`` is refreshed to ``learning://<learning_id>`` on every
    mapping (most-recent mapping per spec). The join insert is
    ``INSERT OR IGNORE``, so repeats are idempotent.

    Ownership/privacy/status are never invented here. Creation stamps the
    trusted ``durable_metadata(content)`` normalization with the caller's
    plain agent id (the same values the durable projection writes). Reuse
    preserves projection metadata, except that a new active mapping can
    reopen a matching aggregate supersession of a synthetic node. Explicit
    restrictive states and file-backed lifecycle authority remain closed.
    A different stored owner returns None instead of corrupting ownership.

    Must be called inside the caller's cursor/transaction; raises on DB
    errors so the coordinator can roll the whole commit back. Returns the
    doc_id, or None when inputs are empty, the typed schema is absent, or
    the existing row belongs to someone else.
    """
    if not learning_id or not agent_id or not content:
        return None
    if not graph_node_schema_present(cur):
        return None
    from minni.durable_projection import durable_metadata

    path = durable_doc_path(agent_id, f"learning:{learning_id}", vault_path, content)
    meta = durable_metadata(content)
    row = cur.execute(
        "SELECT doc_id, agent, page_status, privacy_level, superseded_by FROM documents WHERE path = ?", (path,)
    ).fetchone()
    if row is None:
        cur.execute(
            """
            INSERT INTO documents
            (path, agent, sigil, last_modified, indexed_at, page_status,
             privacy_level, page_type, layer, memory_kind, memory_uri)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'learning', ?)
            """,
            (
                path,
                agent_id,
                meta["sigil"],
                created_at,
                created_at,
                meta["page_status"],
                meta["privacy_level"],
                meta["page_type"],
                meta["layer"],
                f"learning://{learning_id}",
            ),
        )
        doc_id = int(cur.lastrowid)
    else:
        doc_id = int(row[0])
        if str(row[1]) != agent_id:
            logger.warning(
                "canonical node at %s owned by %r, not %r — skipping stamp",
                path,
                row[1],
                agent_id,
            )
            return None
        _reconcile_new_active_mapping(cur, row, learning_id, agent_id, content, meta, path)
        cur.execute(
            "UPDATE documents SET memory_kind = 'learning', memory_uri = ?"
            " WHERE doc_id = ?",
            (f"learning://{learning_id}", doc_id),
        )
    cur.execute(
        """INSERT OR IGNORE INTO learning_documents (learning_id, doc_id, created_at)
           VALUES (?, ?, ?)""",
        (learning_id, doc_id, created_at),
    )
    return doc_id


def canonical_node_learning_state(c, doc_id: int) -> tuple[bool, bool, str | None, int | None]:
    """Return mapping presence, liveness, and deterministic retirement state.

    The caller owns the transaction. Missing typed tables retain legacy purge
    semantics; query errors propagate instead of allowing destructive fallback.
    """
    if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_documents'"
    ).fetchone() is None:
        return False, False, None, None
    mapped = c.execute(
        "SELECT 1 FROM learning_documents WHERE doc_id=? LIMIT 1", (doc_id,),
    ).fetchone() is not None
    if not mapped:
        return False, False, None, None
    aggregate = c.execute(
        """SELECT
             MAX(CASE WHEN l.superseded_by IS NULL AND
                 (l.status IS NULL OR l.status NOT IN ('rejected','expired','superseded'))
                 THEN 1 ELSE 0 END) AS active,
             MAX(l.superseded_by) AS successor,
             MAX(CASE WHEN l.status='expired' THEN 1 ELSE 0 END) AS expired,
             MIN(CASE WHEN l.status='rejected' THEN 1 ELSE 0 END) AS rejected
           FROM learnings l JOIN learning_documents jd USING (learning_id)
           WHERE jd.doc_id=?""", (doc_id,),
    ).fetchone()
    if aggregate["active"]:
        return True, True, None, None
    if aggregate["successor"] is not None:
        successor = c.execute(
            "SELECT doc_id FROM learning_documents WHERE learning_id=? ORDER BY doc_id LIMIT 1",
            (aggregate["successor"],),
        ).fetchone()
        return True, False, "superseded", successor[0] if successor else None
    if aggregate["expired"]:
        return True, False, "expired", None
    if aggregate["rejected"]:
        return True, False, "rejected", None
    # Historical rows may carry status='superseded' without a successor.
    return True, False, "superseded", None


def _reconcile_new_active_mapping(cur, row, learning_id, agent_id, content, meta, path):
    """Reopen only a proven previous aggregate supersession, before adding a join.

    Governed canonical nodes have no backing file; file-backed restrictions
    and explicit blocked/rejected/draft/expired states remain authoritative.
    This does not interpret or reverse arbitrary direct SQL operator overrides.
    """
    import os
    from minni.durable_projection import ACTIVE_LEARNING_SQL, projection_row_closed

    if row[2] != "superseded" or row[3] == "blocked":
        return
    if projection_row_closed(meta["page_status"], meta["privacy_level"]):
        return
    # A real file (including a dangling symlink) gives the normal indexer an
    # independent lifecycle authority. Never infer automatic retirement there.
    if os.path.lexists(path):
        return
    doc_id = row[0]
    if cur.execute(
        "SELECT 1 FROM learning_documents WHERE doc_id=? AND learning_id=?",
        (doc_id, learning_id),
    ).fetchone():
        return
    mapped, active, status, successor = canonical_node_learning_state(cur, doc_id)
    if not mapped or active or (status, successor) != (row[2], row[4]):
        return
    if cur.execute(
        f"SELECT 1 FROM learnings WHERE learning_id=? AND agent_id=? AND content=? AND {ACTIVE_LEARNING_SQL}",
        (learning_id, agent_id, content),
    ).fetchone() is None:
        return
    cur.execute(
        "UPDATE documents SET page_status='accepted', superseded_by=NULL WHERE doc_id=?",
        (doc_id,),
    )

"""Issue #239: dual-resolution candidate_packets + virtual ``_durable`` index hygiene.

Background
----------
A one-time backfill-era double-ingest left 1,616 exact-duplicate
``candidate_packets`` rows (same ``inbox_file`` + ``candidate_index`` +
``content_sha1``). Consolidation then resolved nearly all of them both ways:
the first twin was ``accepted`` into a durable learning; the second was
``rejected`` as ``duplicate of existing learning``. That second decision is
correct *given* the double-ingest — but governance surfaces still see two
contradictory terminal rows for byte-identical content.

Separately, store-time semantic indexing writes synthetic document paths under
``…/_durable/<agent>__<digest>.md``. Those paths are **virtual identities**
(content lives in ``vault_fts`` / ``chunk_embeddings``; no file is written).
Stat-checking them as "dangling" is a false positive; true orphans are rows
with no FTS content (and for non-virtual paths, missing files).

Winner rule (stated, applied by ``repair_duplicate_candidate_pairs``)
--------------------------------------------------------------------
1. Prefer ``accepted`` over any other status (preserves the twin that produced
   the durable learning; never rewrites ``learnings``).
2. Else prefer any terminal status over ``proposed``.
3. Else keep the lowest ``candidate_id`` (oldest insert).
4. The loser is **deleted** from ``candidate_packets`` after an audit row is
   written to ``consolidation_actions`` (when that table exists). Learnings,
   FTS, and embeddings are never touched by the dual-candidate repair.

This module is idempotent and safe to re-run. Default is dry-run.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("minni.repair_dual_candidates")

# Higher is better. ``accepted`` must beat ``rejected`` so the promote twin wins.
_STATUS_RANK: Dict[str, int] = {
    "accepted": 100,
    "merged": 40,
    "superseded": 35,
    "redacted": 30,
    "do_not_store": 25,
    "log_only": 25,
    "expired": 20,
    "rejected": 10,
    "proposed": 0,
}

REPAIR_ACTION_TYPE = "issue239_dual_resolve"
VIRTUAL_DURABLE_MARKER = "/_durable/"


def _parse_derived(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _inbox_key(derived: Dict[str, Any]) -> Optional[Tuple[str, int, str]]:
    """Return (inbox_file, candidate_index, content_sha1) for inbox-sourced rows."""
    if derived.get("source") != "inbox":
        return None
    inbox_file = derived.get("inbox_file")
    idx = derived.get("candidate_index")
    sha = derived.get("content_sha1")
    if not isinstance(inbox_file, str) or not inbox_file:
        return None
    if isinstance(idx, bool) or not isinstance(idx, (int, float)):
        return None
    idx_i = int(idx)
    if idx_i != idx:  # reject non-integral floats
        return None
    if not isinstance(sha, str) or not sha:
        # Fall back to empty marker so pre-sha1 rows still group by file+index.
        sha = ""
    return (inbox_file, idx_i, sha)


def status_rank(status: Optional[str]) -> int:
    return _STATUS_RANK.get(str(status or "").strip().lower(), 1)


def choose_winner(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply the stated winner rule to a group of duplicate rows.

    Sort key: higher status_rank first, then lower candidate_id.
    """
    if not rows:
        raise ValueError("choose_winner requires at least one row")
    return min(
        rows,
        key=lambda r: (-status_rank(r.get("status")), int(r["candidate_id"])),
    )


def find_duplicate_candidate_groups(db) -> List[Dict[str, Any]]:
    """Group inbox-sourced candidates by (inbox_file, index, content_sha1).

    Returns only groups with 2+ rows. Each group dict has ``key`` and ``rows``.
    """
    groups: Dict[Tuple[str, int, str], List[Dict[str, Any]]] = defaultdict(list)
    with db.cursor() as c:
        c.execute(
            """
            SELECT candidate_id, principal, status, content, derived_from,
                   proposed_at, resolved_at, resolved_by, resolution_reason
            FROM candidate_packets
            WHERE derived_from IS NOT NULL
            """
        )
        for row in c.fetchall():
            item = dict(row)
            derived = _parse_derived(item.get("derived_from"))
            if not derived:
                continue
            key = _inbox_key(derived)
            if key is None:
                continue
            item["_key"] = key
            groups[key].append(item)

    out: List[Dict[str, Any]] = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        winner = choose_winner(rows)
        losers = [r for r in rows if r["candidate_id"] != winner["candidate_id"]]
        out.append(
            {
                "key": {
                    "inbox_file": key[0],
                    "candidate_index": key[1],
                    "content_sha1": key[2],
                },
                "winner_id": int(winner["candidate_id"]),
                "winner_status": winner.get("status"),
                "loser_ids": [int(r["candidate_id"]) for r in losers],
                "statuses": sorted({str(r.get("status")) for r in rows}),
                "row_count": len(rows),
            }
        )
    out.sort(key=lambda g: g["winner_id"])
    return out


def _table_exists(db, name: str) -> bool:
    with db.cursor() as c:
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        )
        return c.fetchone() is not None


def _audit_drop(c, loser_id: int, winner_id: int, winner_status: str, now: float) -> None:
    """Best-effort audit row; no-op if consolidation_actions is missing."""
    try:
        c.execute(
            """
            INSERT INTO consolidation_actions
            (action_type, claim, category, status, detail, created_at)
            VALUES (?, ?, 'general', 'applied', ?, ?)
            """,
            (
                REPAIR_ACTION_TYPE,
                str(loser_id),
                (
                    f"deleted dual twin #{loser_id}; kept #{winner_id} "
                    f"(status={winner_status}); rule=accepted>terminal>oldest"
                )[:500],
                now,
            ),
        )
    except Exception as exc:
        logger.debug("audit insert skipped for loser %s: %s", loser_id, exc)


def repair_duplicate_candidate_pairs(
    db, *, dry_run: bool = True, limit: Optional[int] = None
) -> Dict[str, Any]:
    """Collapse duplicate inbox candidate groups to one row each.

    Never touches ``learnings``. Idempotent: a second run finds zero groups.
    """
    groups = find_duplicate_candidate_groups(db)
    if limit is not None:
        groups = groups[: max(0, int(limit))]

    plan = []
    for g in groups:
        plan.append(
            {
                "keep": g["winner_id"],
                "keep_status": g["winner_status"],
                "delete": g["loser_ids"],
                "key": g["key"],
                "statuses": g["statuses"],
            }
        )

    deleted = 0
    if not dry_run and plan:
        now = time.time()
        with db.transaction() as c:
            for item in plan:
                for loser_id in item["delete"]:
                    # Re-check existence inside the txn.
                    c.execute(
                        "SELECT candidate_id FROM candidate_packets WHERE candidate_id=?",
                        (loser_id,),
                    )
                    if not c.fetchone():
                        continue
                    if _table_exists(db, "consolidation_actions"):
                        _audit_drop(
                            c, loser_id, item["keep"], str(item["keep_status"]), now
                        )
                    c.execute(
                        "DELETE FROM candidate_packets WHERE candidate_id=?",
                        (loser_id,),
                    )
                    deleted += 1

    return {
        "dry_run": dry_run,
        "groups_found": len(groups),
        "would_delete": sum(len(p["delete"]) for p in plan),
        "deleted": deleted if not dry_run else 0,
        "winner_rule": (
            "accepted > other terminal > proposed; tie-break lowest candidate_id"
        ),
        "learnings_touched": False,
        "sample": plan[:5],
    }


def ensure_inbox_dedup_index(db) -> Dict[str, Any]:
    """Create a partial unique index that blocks re-insertion of dual inbox keys.

    Requires duplicates to already be collapsed (call repair first). Returns
    status describing whether the index exists / was created / failed.
    """
    index_name = "idx_candidate_packets_inbox_sha1_unique"
    with db.cursor() as c:
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=? LIMIT 1",
            (index_name,),
        )
        if c.fetchone():
            return {"status": "exists", "index": index_name}

    remaining = find_duplicate_candidate_groups(db)
    if remaining:
        return {
            "status": "blocked_by_duplicates",
            "index": index_name,
            "duplicate_groups": len(remaining),
        }

    try:
        with db.transaction() as c:
            # Expression unique index: same inbox file + index + content_sha1
            # may only appear once when source=inbox. SQLite 3.9+ (macOS/homebrew
            # well above that). NULL content_sha1 rows are excluded by the
            # json_extract IS NOT NULL guard.
            c.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
                ON candidate_packets (
                    json_extract(derived_from, '$.inbox_file'),
                    CAST(json_extract(derived_from, '$.candidate_index') AS INTEGER),
                    json_extract(derived_from, '$.content_sha1')
                )
                WHERE json_extract(derived_from, '$.source') = 'inbox'
                  AND json_extract(derived_from, '$.inbox_file') IS NOT NULL
                  AND json_extract(derived_from, '$.content_sha1') IS NOT NULL
                """
            )
        return {"status": "created", "index": index_name}
    except Exception as exc:
        logger.warning("failed to create inbox dedup index: %s", exc)
        return {"status": "error", "index": index_name, "error": str(exc)}


def is_virtual_durable_path(path: str) -> bool:
    """True for store-time synthetic durable index identities (no disk file)."""
    if not path:
        return False
    # Normalize so Windows and doubled separators still match.
    norm = path.replace("\\", "/")
    return VIRTUAL_DURABLE_MARKER in norm


def find_missing_document_rows(
    db, *, include_virtual_durable: bool = False
) -> List[Dict[str, Any]]:
    """Document rows whose path is missing on disk.

    By default **excludes** virtual ``_durable`` paths (they are not files).
    Set ``include_virtual_durable=True`` only for diagnostics.
    """
    missing: List[Dict[str, Any]] = []
    with db.cursor() as c:
        c.execute("SELECT doc_id, path, agent, page_status FROM documents")
        rows = [dict(r) for r in c.fetchall()]
    for row in rows:
        path = row.get("path") or ""
        if is_virtual_durable_path(path) and not include_virtual_durable:
            continue
        if path and not os.path.isfile(path):
            missing.append(row)
    return missing


def find_orphan_virtual_durable(db) -> List[Dict[str, Any]]:
    """Virtual ``_durable`` rows with no FTS content — true index garbage.

    A healthy virtual durable row always has a ``vault_fts`` row (lexical
    recall). Rows without FTS (and optionally without chunks) are safe to prune.
    """
    orphans: List[Dict[str, Any]] = []
    with db.cursor() as c:
        c.execute(
            """
            SELECT d.doc_id, d.path, d.agent, d.page_status
            FROM documents d
            WHERE d.path LIKE '%/_durable/%'
              AND NOT EXISTS (
                  SELECT 1 FROM vault_fts f WHERE f.doc_id = d.doc_id
              )
            """
        )
        orphans = [dict(r) for r in c.fetchall()]
    return orphans


def prune_document_rows(
    db,
    doc_ids: Iterable[int],
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Delete documents + vault_fts + chunk_embeddings for the given doc_ids.

    Does not touch ``learnings``. Idempotent for already-absent ids.
    """
    ids = sorted({int(x) for x in doc_ids})
    if not ids:
        return {"dry_run": dry_run, "requested": 0, "deleted": 0}

    if dry_run:
        return {"dry_run": True, "requested": len(ids), "deleted": 0, "doc_ids": ids}

    deleted = 0
    with db.transaction() as c:
        for doc_id in ids:
            c.execute("SELECT doc_id FROM documents WHERE doc_id=?", (doc_id,))
            if not c.fetchone():
                continue
            c.execute("DELETE FROM vault_fts WHERE doc_id=?", (doc_id,))
            c.execute("DELETE FROM chunk_embeddings WHERE doc_id=?", (doc_id,))
            try:
                c.execute(
                    "DELETE FROM memory_links WHERE source_doc_id=? OR target_doc_id=?",
                    (doc_id, doc_id),
                )
            except Exception:
                pass  # table may not exist on older schemas
            c.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
            deleted += 1
    return {"dry_run": False, "requested": len(ids), "deleted": deleted}


def repair_index_disk_divergence(
    db, *, dry_run: bool = True
) -> Dict[str, Any]:
    """Prune safe-to-remove index rows; report virtual durable separately.

    Safe to remove:
      * non-virtual document paths missing on disk
      * virtual ``_durable`` rows with no ``vault_fts`` content

    NOT removed (by design):
      * virtual ``_durable`` rows that still carry FTS/chunk content — these
        are the store-time semantic index for durable learnings.
    """
    missing_real = find_missing_document_rows(db, include_virtual_durable=False)
    orphan_virtual = find_orphan_virtual_durable(db)
    virtual_healthy = 0
    with db.cursor() as c:
        c.execute(
            """
            SELECT COUNT(*) AS n FROM documents d
            WHERE d.path LIKE '%/_durable/%'
              AND EXISTS (SELECT 1 FROM vault_fts f WHERE f.doc_id = d.doc_id)
            """
        )
        virtual_healthy = int(c.fetchone()["n"])

    to_prune = [r["doc_id"] for r in missing_real] + [
        r["doc_id"] for r in orphan_virtual
    ]
    prune_result = prune_document_rows(db, to_prune, dry_run=dry_run)

    return {
        "dry_run": dry_run,
        "missing_on_disk_non_virtual": len(missing_real),
        "orphan_virtual_durable": len(orphan_virtual),
        "healthy_virtual_durable_kept": virtual_healthy,
        "virtual_durable_note": (
            "paths under /_durable/ are synthetic index identities; missing "
            "files are expected when FTS content is present"
        ),
        "prune": prune_result,
        "sample_missing": [
            {"doc_id": r["doc_id"], "path": r["path"]} for r in missing_real[:5]
        ],
        "sample_orphan_virtual": [
            {"doc_id": r["doc_id"], "path": r["path"]} for r in orphan_virtual[:5]
        ],
    }


def run_full_repair(
    db, *, dry_run: bool = True, create_index: bool = True
) -> Dict[str, Any]:
    """Run dual-candidate repair + safe index prune (+ optional unique index)."""
    dual = repair_duplicate_candidate_pairs(db, dry_run=dry_run)
    index = repair_index_disk_divergence(db, dry_run=dry_run)
    index_status: Dict[str, Any] = {"status": "skipped"}
    if create_index and not dry_run:
        index_status = ensure_inbox_dedup_index(db)
    elif create_index and dry_run:
        index_status = {"status": "would_create_if_clean", "dry_run": True}
    return {
        "dual_candidates": dual,
        "index_disk": index,
        "inbox_dedup_index": index_status,
    }

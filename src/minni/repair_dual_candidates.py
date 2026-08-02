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
    """Probe via auto-commit cursor — never call inside ``db.transaction()``.

    ``db.cursor()`` commits on exit; using it mid-transaction would end the
    outer ``BEGIN IMMEDIATE`` early and commit deletes piecewise.
    """
    with db.cursor() as c:
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        )
        return c.fetchone() is not None


def _table_exists_on_cursor(c, name: str) -> bool:
    """Probe using an open transaction cursor (no commit)."""
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
        # Probe once outside the write loop so we never open db.cursor()
        # (which commits) while BEGIN IMMEDIATE is held.
        has_audit = _table_exists(db, "consolidation_actions")
        with db.transaction() as c:
            # Re-probe on the txn cursor as a belt-and-suspenders check
            # without committing mid-batch.
            if not has_audit:
                has_audit = _table_exists_on_cursor(c, "consolidation_actions")
            for item in plan:
                for loser_id in item["delete"]:
                    # Re-check existence inside the txn.
                    c.execute(
                        "SELECT candidate_id FROM candidate_packets WHERE candidate_id=?",
                        (loser_id,),
                    )
                    if not c.fetchone():
                        continue
                    if has_audit:
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


def _normalize_vault_roots(
    vault_roots: Optional[Sequence[str]] = None,
) -> List[str]:
    """Return absolute, existing vault root directories."""
    roots: List[str] = []
    seen: set = set()
    for raw in vault_roots or ():
        if not raw:
            continue
        expanded = os.path.abspath(os.path.expanduser(str(raw)))
        if expanded in seen:
            continue
        seen.add(expanded)
        if os.path.isdir(expanded):
            roots.append(expanded)
    return roots


def resolve_document_path(
    path: str, vault_roots: Optional[Sequence[str]] = None
) -> Optional[str]:
    """Resolve a documents.path to an absolute filesystem path if it exists.

    Absolute paths are used as-is. Relative paths (vault-bridge style
    identities like ``wiki/decisions/foo.md``) are tried under each
    configured vault root. Returns the first existing file path, or None
    if no candidate exists on disk.
    """
    if not path:
        return None
    if os.path.isabs(path):
        return path if os.path.isfile(path) else None
    # Relative identity: try each vault root.
    norm = path.replace("\\", "/").lstrip("/")
    if not norm or ".." in norm.split("/"):
        return None
    for root in _normalize_vault_roots(vault_roots):
        candidate = os.path.join(root, *norm.split("/"))
        if os.path.isfile(candidate):
            return candidate
    # Also accept CWD-relative existence (legacy absolute-miswritten-as-rel).
    if os.path.isfile(path):
        return os.path.abspath(path)
    return None


def _doc_ids_with_fts(db, doc_ids: Sequence[int]) -> set:
    """Return the subset of doc_ids that still have vault_fts content."""
    if not doc_ids:
        return set()
    found: set = set()
    # Chunk to stay under SQLite variable limits on huge DBs.
    ids = [int(x) for x in doc_ids]
    with db.cursor() as c:
        for i in range(0, len(ids), 400):
            chunk = ids[i : i + 400]
            placeholders = ",".join("?" * len(chunk))
            c.execute(
                f"SELECT DISTINCT doc_id FROM vault_fts WHERE doc_id IN ({placeholders})",
                chunk,
            )
            for row in c.fetchall():
                found.add(int(row["doc_id"] if hasattr(row, "keys") else row[0]))
    return found


def find_missing_document_rows(
    db,
    *,
    include_virtual_durable: bool = False,
    vault_roots: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Document rows whose path is missing on disk after vault-root resolution.

    By default **excludes** virtual ``_durable`` paths (they are not files).
    Set ``include_virtual_durable=True`` only for diagnostics.

    Relative ``documents.path`` values (vault bridge) are resolved against
    ``vault_roots`` before the existence check. Relative paths that still
    carry FTS/chunk content are **never** classified missing unless every
    resolved absolute candidate is confirmed absent — this prevents an
    operator running repair from the wrong CWD from wiping healthy index
    rows.
    """
    roots = _normalize_vault_roots(vault_roots)
    missing: List[Dict[str, Any]] = []
    with db.cursor() as c:
        c.execute("SELECT doc_id, path, agent, page_status FROM documents")
        rows = [dict(r) for r in c.fetchall()]

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        path = row.get("path") or ""
        if is_virtual_durable_path(path) and not include_virtual_durable:
            continue
        if not path:
            continue
        if resolve_document_path(path, roots) is not None:
            continue
        candidates.append(row)

    if not candidates:
        return []

    # Protect relative FTS-backed rows when we could not resolve them: without
    # a confirmed absolute miss under a known vault root, treating them as
    # missing is unsafe (wrong CWD / NFS blip / multi-vault layout).
    fts_backed = _doc_ids_with_fts(db, [r["doc_id"] for r in candidates])
    for row in candidates:
        path = row.get("path") or ""
        is_rel = not os.path.isabs(path)
        if is_rel and int(row["doc_id"]) in fts_backed:
            # Relative + still recallable → keep unless roots were provided
            # and every root was checked (resolve already returned None).
            # Even with roots, a relative FTS-backed row may live under a
            # vault we were not told about; refuse to prune.
            continue
        if is_rel and not roots:
            # No vault roots + relative path: cannot prove absence.
            continue
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
    db,
    *,
    dry_run: bool = True,
    vault_roots: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Prune safe-to-remove index rows; report virtual durable separately.

    Safe to remove:
      * non-virtual document paths missing on disk after vault-root resolution
        (relative FTS-backed paths are never pruned — see
        ``find_missing_document_rows``)
      * virtual ``_durable`` rows with no ``vault_fts`` content

    NOT removed (by design):
      * virtual ``_durable`` rows that still carry FTS/chunk content — these
        are the store-time semantic index for durable learnings.
      * relative non-virtual paths that still have FTS content
    """
    missing_real = find_missing_document_rows(
        db, include_virtual_durable=False, vault_roots=vault_roots
    )
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
            "files are expected when FTS content is present. Relative "
            "non-virtual paths with FTS content are never pruned."
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
    db,
    *,
    dry_run: bool = True,
    create_index: bool = True,
    prune_index: bool = False,
    vault_roots: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Run dual-candidate repair (+ optional unique index / index prune).

    Default is dual-candidates only. Destructive document-index pruning
    requires ``prune_index=True`` (CLI: ``--prune-index``) so operators cannot
    accidentally wipe relative-path or offline-vault index rows while fixing
    dual-resolution twins.
    """
    dual = repair_duplicate_candidate_pairs(db, dry_run=dry_run)
    if prune_index:
        index: Dict[str, Any] = repair_index_disk_divergence(
            db, dry_run=dry_run, vault_roots=vault_roots
        )
    else:
        index = {
            "dry_run": dry_run,
            "skipped": True,
            "reason": "prune_index not requested; pass prune_index=True / --prune-index",
            "missing_on_disk_non_virtual": 0,
            "orphan_virtual_durable": 0,
            "healthy_virtual_durable_kept": 0,
            "virtual_durable_note": (
                "index prune skipped by default; dual-candidate repair does "
                "not touch documents/vault_fts/chunk_embeddings"
            ),
            "prune": {"dry_run": dry_run, "requested": 0, "deleted": 0, "skipped": True},
            "sample_missing": [],
            "sample_orphan_virtual": [],
        }
    index_status: Dict[str, Any] = {"status": "skipped"}
    if create_index and not dry_run:
        index_status = ensure_inbox_dedup_index(db)
    elif create_index and dry_run:
        index_status = {"status": "would_create_if_clean", "dry_run": True}
    return {
        "dual_candidates": dual,
        "index_disk": index,
        "inbox_dedup_index": index_status,
        "prune_index": prune_index,
    }

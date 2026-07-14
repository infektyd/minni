"""Surgically unpark AFM candidates blocked only by legacy NULL privacy.

Dry-run is the default. Apply mode stamps qualifying proposed candidates as
``privacy_level='safe'`` and supersedes their active ``afm_review`` actions in
one transaction. Audit rows are never deleted.

Usage::

    python -m minni.afm_passes.unpark_privacy_backlog
    python -m minni.afm_passes.unpark_privacy_backlog --apply
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import Any, Dict, Iterable, List, Set

from minni.afm_passes.consolidation import (
    _MIN_CONTENT_LEN,
    _normalize,
    _quality_blockers,
    content_hash,
)
from minni.safety import is_instruction_like


def _learning_hashes(cursor) -> Set[str]:
    """Return existing-learning hashes without mutating schema in dry-run."""
    columns = {
        row["name"] for row in cursor.execute("PRAGMA table_info(learnings)")
    }
    if "content_hash" in columns:
        rows = cursor.execute(
            "SELECT content_hash, content FROM learnings"
        ).fetchall()
        return {
            row["content_hash"] or content_hash(row["content"] or "")
            for row in rows
        }
    return {
        content_hash(row["content"] or "")
        for row in cursor.execute("SELECT content FROM learnings").fetchall()
    }


def _targets(cursor) -> List[Dict[str, Any]]:
    rows = cursor.execute(
        """
        SELECT cp.candidate_id, cp.content, cp.instruction_like
        FROM candidate_packets cp
        WHERE cp.status = 'proposed'
          AND cp.privacy_level IS NULL
          AND EXISTS (
              SELECT 1 FROM consolidation_actions ca
              WHERE ca.action_type = 'afm_review'
                AND ca.claim = CAST(cp.candidate_id AS TEXT)
                AND COALESCE(ca.status, '') != 'superseded'
          )
        ORDER BY cp.proposed_at ASC, cp.candidate_id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _non_privacy_blockers(
    candidate: Dict[str, Any], learning_hashes: Set[str]
) -> List[str]:
    """Replay consolidation's deterministic gates other than privacy."""
    content = candidate.get("content") or ""
    normalized = _normalize(content)
    if not normalized or len(normalized) < _MIN_CONTENT_LEN:
        return ["too_short_or_trivial"]
    if content_hash(content) in learning_hashes:
        return ["duplicate_learning"]
    if int(candidate.get("instruction_like") or 0) == 1 or is_instruction_like(
        content
    ):
        return ["instruction_like"]
    return [f"quality:{reason}" for reason in _quality_blockers(content)]


def _analyze(cursor) -> tuple[List[Dict[str, Any]], List[int], Counter]:
    targets = _targets(cursor)
    hashes = _learning_hashes(cursor)
    unpark_ids: List[int] = []
    reasons: Counter = Counter()
    for candidate in targets:
        blockers = _non_privacy_blockers(candidate, hashes)
        if blockers:
            reasons.update(blockers)
        else:
            unpark_ids.append(int(candidate["candidate_id"]))
    return targets, unpark_ids, reasons


def run(db, config=None, *, dry_run: bool = True) -> Dict[str, Any]:
    """Preview or apply the legacy NULL-privacy backlog repair.

    Only proposed candidates with NULL privacy and an active ``afm_review``
    marker are considered. Apply mode is atomic and idempotent.
    """
    del config  # accepted for pass-style call compatibility
    context = db.cursor() if dry_run else db.transaction()
    with context as cursor:
        targets, unpark_ids, reasons = _analyze(cursor)
        unparked = 0
        if not dry_run:
            for candidate_id in unpark_ids:
                cursor.execute(
                    """
                    UPDATE candidate_packets
                    SET privacy_level = 'safe'
                    WHERE candidate_id = ?
                      AND status = 'proposed'
                      AND privacy_level IS NULL
                    """,
                    (candidate_id,),
                )
                if cursor.rowcount != 1:
                    continue
                cursor.execute(
                    """
                    UPDATE consolidation_actions
                    SET status = 'superseded'
                    WHERE action_type = 'afm_review'
                      AND claim = ?
                      AND COALESCE(status, '') != 'superseded'
                    """,
                    (str(candidate_id),),
                )
                unparked += 1

    return {
        "dry_run": dry_run,
        "targeted": len(targets),
        "would_unpark": len(unpark_ids),
        "kept_parked": len(targets) - len(unpark_ids),
        "kept_parked_reasons": dict(sorted(reasons.items())),
        "unparked": unparked,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        help="database path (defaults to MINNI_DB_PATH / ~/.minni/minni.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply atomically; default is a read-only dry-run",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.db:
        os.environ["MINNI_DB_PATH"] = os.path.expanduser(args.db)

    from minni.config import DEFAULT_CONFIG
    from minni.db import SovereignDB

    db = SovereignDB(DEFAULT_CONFIG)
    try:
        result = run(db, DEFAULT_CONFIG, dry_run=not args.apply)
    finally:
        db.close()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

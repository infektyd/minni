"""Surgically unpark AFM candidates blocked only by learn-only privacy=review.

Dry-run is the default. Apply mode supersedes active ``afm_review`` fences on
proposed ``privacy_level='review'`` rows that have ``instruction_like=0`` and
no remaining non-privacy blockers (quality / length / content
instruction-like). Privacy stays ``review``. Quality-fail and content-IL
stay parked. Audit rows are never deleted.

Usage::

    python -m minni.afm_passes.unpark_review_privacy_backlog
    python -m minni.afm_passes.unpark_review_privacy_backlog --apply
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import Any, Dict, Iterable, List

from minni.afm_passes.unpark_privacy_backlog import (
    _learning_hashes,
    _non_privacy_blockers,
)


def _targets(cursor) -> List[Dict[str, Any]]:
    rows = cursor.execute(
        """
        SELECT cp.candidate_id, cp.content, cp.instruction_like
        FROM candidate_packets cp
        WHERE cp.status = 'proposed'
          AND lower(COALESCE(cp.privacy_level, '')) = 'review'
          AND COALESCE(cp.instruction_like, 0) = 0
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
    """Preview or apply the learn-only privacy=review fence repair.

    Only proposed candidates with ``privacy_level='review'``,
    ``instruction_like=0``, and an active ``afm_review`` marker are
    considered. Apply mode is atomic and idempotent. Privacy is not
    rewritten — review stays the AFM-filter label.
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
                    SELECT 1 FROM candidate_packets
                    WHERE candidate_id = ?
                      AND status = 'proposed'
                      AND lower(COALESCE(privacy_level, '')) = 'review'
                      AND COALESCE(instruction_like, 0) = 0
                    """,
                    (candidate_id,),
                )
                if cursor.fetchone() is None:
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
                if cursor.rowcount:
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

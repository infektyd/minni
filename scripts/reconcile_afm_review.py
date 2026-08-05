#!/usr/bin/env python3
"""Operator drain for orphaned ``afm_review`` markers (#229 M5).

A marker fences a candidate against re-examination. Until the resolve paths
learned to supersede them, every resolution left its fence active, so markers
outlived the candidates they referred to — 480 of 495 on the measured install.
The engine fix stops NEW orphans; this drains the ones already there.

Dry-run by default. Superseding a fence changes what consolidation will
re-examine, so applying is an explicit operator decision:

    python3 scripts/reconcile_afm_review.py                 # report only
    python3 scripts/reconcile_afm_review.py --apply         # supersede
    python3 scripts/reconcile_afm_review.py --db path/to/minni.db

Markers whose candidate is still ``proposed`` are LIVE fences and are never
touched — disarming one would let consolidation re-examine something an
operator deliberately parked.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

# Import-independent of the daemon runtime: this is an operator script that
# must run against a database file, not a live daemon.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from minni.afm_review_markers import (  # noqa: E402
    count_orphaned_afm_review,
    proposed_queue_stats,
    reconcile_orphaned_afm_review,
)

DEFAULT_DB = os.path.expanduser("~/.minni/minni.db")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    ap.add_argument("--apply", action="store_true", help="supersede orphans (default: dry-run)")
    ap.add_argument(
        "--stale-after-days", type=float, default=14.0,
        help="proposed-queue staleness threshold for the report (default 14)",
    )
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}", file=sys.stderr)
        return 2

    # 30s to match SovereignDB: the default --db IS the live database, so
    # contending with minnid is the expected case, not an exception.
    try:
        conn = sqlite3.connect(args.db, timeout=30.0)
    except sqlite3.OperationalError as exc:
        print(f"cannot open {args.db}: {exc}", file=sys.stderr)
        return 3
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        before = count_orphaned_afm_review(cursor)
        # The queue report is advisory. It must never be able to block the
        # drain it is printed next to — a single unparseable timestamp used to
        # take the whole reconcile down with a traceback before it superseded
        # anything.
        try:
            queue = proposed_queue_stats(cursor, stale_after_days=args.stale_after_days)
        except Exception as exc:
            queue = {"status": "unknown", "error": type(exc).__name__, "detail": str(exc)}
        result = reconcile_orphaned_afm_review(cursor, dry_run=not args.apply)
        if args.apply:
            conn.commit()
        report = {
            "db": args.db,
            "orphaned_before": before,
            "proposed_queue": queue,
            **result,
        }
    except sqlite3.OperationalError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()

    print(json.dumps(report, indent=2, sort_keys=True))
    if not args.apply and before:
        print(
            f"\n{before} orphaned marker(s) would be superseded. "
            "Re-run with --apply to drain.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

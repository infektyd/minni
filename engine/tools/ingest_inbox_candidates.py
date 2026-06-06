#!/usr/bin/env python3
"""CLI to manually drain inbox stop-candidates into candidate_packets.

Thin wrapper over ``afm_passes.inbox_ingest`` (the same logic the AFM loop runs
each consolidation tick). Use this for one-off backlog recovery or to ingest a
single vault. Dry-run by default.

Usage:
  python3 tools/ingest_inbox_candidates.py --vault ~/.minni/codex-vault --principal codex
  python3 tools/ingest_inbox_candidates.py --vault ~/.minni/codex-vault --principal codex --live
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", required=True, help="vault root (contains inbox/)")
    ap.add_argument("--principal", default="codex",
                    help="fallback principal if a file lacks agent_id")
    ap.add_argument("--db", default=os.environ.get("MINNI_DB_PATH"),
                    help="override minni.db path (else config/env)")
    ap.add_argument("--live", action="store_true",
                    help="actually insert (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry-run")
    args = ap.parse_args(argv)

    if args.db:
        os.environ["MINNI_DB_PATH"] = os.path.expanduser(args.db)

    from config import DEFAULT_CONFIG
    from db import SovereignDB
    from afm_passes.inbox_ingest import ingest

    dry_run = not args.live
    inbox = Path(os.path.expanduser(args.vault)).resolve() / "inbox"
    if not inbox.is_dir():
        print(f"ERROR: inbox not found: {inbox}", file=sys.stderr)
        return 2

    db = SovereignDB(DEFAULT_CONFIG)
    res = ingest(db, DEFAULT_CONFIG, inboxes=[inbox],
                 fallback_principal=args.principal, dry_run=dry_run)

    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"=== ingest_inbox_candidates [{mode}] ===")
    print(f"inbox:           {inbox}")
    print(f"db:              {DEFAULT_CONFIG.db_path}")
    print(f"eligible:        {res['eligible']}")
    print(f"already_present: {res['already_present']}")
    print(f"would_insert:    {res['would_insert']}")
    print(f"inserted:        {res['inserted']}")
    if dry_run:
        print("DRY-RUN: no rows written. Re-run with --live to insert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

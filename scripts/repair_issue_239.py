#!/usr/bin/env python3
"""Operator CLI for issue #239 — dual-resolution candidates + index/disk hygiene.

Dry-run by default. Pass ``--apply`` to mutate the DB.

  python3 scripts/repair_issue_239.py
  python3 scripts/repair_issue_239.py --apply
  python3 scripts/repair_issue_239.py --db /path/to/minni.db --apply

Never touches ``learnings``. See ``minni.repair_dual_candidates`` for the
winner rule and virtual-``_durable`` policy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Prefer the repo's src/ so this works without an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/.minni/minni.db"),
        help="path to minni.db (default: ~/.minni/minni.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply repairs (default is dry-run)",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="skip creating the inbox dedup unique index after repair",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON only",
    )
    args = parser.parse_args(argv)

    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.repair_dual_candidates import run_full_repair

    db_path = os.path.expanduser(args.db)
    if not os.path.isfile(db_path):
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 2

    cfg = SovereignConfig(db_path=db_path)
    db = SovereignDB(cfg)
    try:
        result = run_full_repair(
            db, dry_run=not args.apply, create_index=not args.no_index
        )
    finally:
        if hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        dual = result["dual_candidates"]
        idx = result["index_disk"]
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"issue #239 repair [{mode}] db={db_path}")
        print(f"  winner rule: {dual['winner_rule']}")
        print(
            f"  dual groups: {dual['groups_found']}  "
            f"would_delete={dual['would_delete']}  deleted={dual['deleted']}  "
            f"learnings_touched={dual['learnings_touched']}"
        )
        print(
            f"  missing on-disk (non-virtual): {idx['missing_on_disk_non_virtual']}"
        )
        print(
            f"  orphan virtual _durable: {idx['orphan_virtual_durable']}  "
            f"healthy virtual kept: {idx['healthy_virtual_durable_kept']}"
        )
        print(f"  note: {idx['virtual_durable_note']}")
        print(f"  inbox dedup index: {result['inbox_dedup_index']}")
        if not args.apply:
            print("  (re-run with --apply to mutate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Operator CLI for issue #239 — dual-resolution candidates + index/disk hygiene.

Dry-run by default. Pass ``--apply`` to mutate the DB.

Default ``--apply`` only collapses **byte-identical** dual-resolution
``candidate_packets`` twins (same inbox_file + candidate_index + content_sha1)
(+ optional inbox dedup unique index). Destructive document-index pruning
requires an explicit ``--prune-index``.

Prefer stopping the AFM/minnid daemon (or other writers) before ``--apply`` so
consolidation cannot race the repair plan; apply still re-validates winners
inside the write transaction and never deletes ``status=accepted`` rows.

The inbox unique index is **operator-only** (not a schema migration). Re-run
this CLI after ``candidate_packets`` rebuilds (e.g. migration 015).

  python3 scripts/repair_issue_239.py
  python3 scripts/repair_issue_239.py --apply
  python3 scripts/repair_issue_239.py --apply --prune-index --vault /path/to/vault
  python3 scripts/repair_issue_239.py --db /path/to/minni.db --apply

Exit codes:
  0  success (dry-run always; apply when dual repair ran and index ok/skipped)
  2  database file not found
  3  --apply requested inbox UNIQUE index but status is blocked/error
     (status not in {exists, created, skipped})

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


def _discover_vault_roots(cfg, extra: list[str] | None = None) -> list[str]:
    """Collect vault roots from config + optional CLI overrides + *-vault dirs."""
    import glob

    roots: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        if not raw:
            return
        expanded = os.path.abspath(os.path.expanduser(str(raw)))
        if expanded in seen:
            return
        seen.add(expanded)
        if os.path.isdir(expanded):
            roots.append(expanded)

    for r in extra or []:
        _add(r)
    _add(getattr(cfg, "vault_path", None))
    home = getattr(cfg, "CANONICAL_SOVEREIGN_HOME", None)
    if not home:
        vp = getattr(cfg, "vault_path", None) or "~/.minni/vault"
        home = str(Path(vp).expanduser().parent)
    home = os.path.expanduser(home)
    for path in glob.glob(os.path.join(home, "*-vault")):
        _add(path)
    _add(os.path.join(home, "vault"))
    return roots


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
        help="apply dual-candidate repair (default is dry-run)",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="skip creating the inbox dedup unique index after repair",
    )
    parser.add_argument(
        "--prune-index",
        action="store_true",
        help=(
            "also prune missing/orphan document index rows "
            "(destructive; default is dual-candidates only)"
        ),
    )
    parser.add_argument(
        "--force-prune-indexed",
        action="store_true",
        help=(
            "with --prune-index, also prune FTS/chunk-backed document rows "
            "whose files are missing (default keeps recallable rows)"
        ),
    )
    parser.add_argument(
        "--vault",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "vault root for resolving relative documents.path rows when "
            "pruning (repeatable); also discovers ~/.minni/*-vault"
        ),
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
    vault_roots = _discover_vault_roots(cfg, args.vault)
    db = SovereignDB(cfg)
    try:
        result = run_full_repair(
            db,
            dry_run=not args.apply,
            create_index=not args.no_index,
            prune_index=args.prune_index,
            force_prune_indexed=args.force_prune_indexed,
            vault_roots=vault_roots,
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
            f"  dual groups (byte-identical): {dual['groups_found']}  "
            f"would_delete={dual['would_delete']}  deleted={dual['deleted']}  "
            f"learnings_touched={dual['learnings_touched']}"
        )
        if dual.get("divergent_content_groups"):
            print(
                f"  divergent content groups (not deleted): "
                f"{dual['divergent_content_groups']}"
            )
        if dual.get("needs_operator_groups"):
            print(
                f"  needs-operator groups (proposed+terminal / dual-accepted, "
                f"not deleted): {dual['needs_operator_groups']}"
            )
        dual_acc = int(dual.get("dual_accepted_groups") or 0)
        if dual_acc > 0:
            print(
                f"  dual-accepted groups (block UNIQUE; no auto-delete): "
                f"{dual_acc}"
            )
            samples = dual.get("dual_accepted_sample") or []
            for s in samples[:3]:
                print(
                    f"    ids={s.get('candidate_ids')} "
                    f"key={s.get('key')}"
                )
        if args.apply and (
            dual.get("winner_replanned")
            or dual.get("skipped_accepted_guard")
            or dual.get("groups_skipped_stale")
            or dual.get("groups_needs_operator_live")
        ):
            print(
                f"  in-txn revalidate: replanned={dual.get('winner_replanned', 0)}  "
                f"accepted_guard={dual.get('skipped_accepted_guard', 0)}  "
                f"stale_skip={dual.get('groups_skipped_stale', 0)}  "
                f"needs_operator_live={dual.get('groups_needs_operator_live', 0)}"
            )
        # Loud operator signals (stderr) — dual-accepted has no repair path
        # other than manual resolution; UNIQUE install will stay blocked.
        if dual_acc > 0 and not args.json:
            print(
                f"WARNING: dual_accepted_groups={dual_acc} — multiple "
                f"status=accepted twins share an inbox key; repair never "
                f"hard-deletes accepted rows. Resolve manually (keep one, "
                f"retire extras) before UNIQUE index can be installed.",
                file=sys.stderr,
            )
        skipped_guard = int(dual.get("skipped_accepted_guard") or 0)
        if skipped_guard > 0 and not args.json:
            print(
                f"WARNING: skipped_accepted_guard={skipped_guard} — in-txn "
                f"refused to delete accepted twin(s). Remaining accepted "
                f"collisions still block the inbox UNIQUE backstop.",
                file=sys.stderr,
            )
        if idx.get("skipped"):
            print(
                "  index prune: SKIPPED (default). "
                "Re-run with --prune-index to remove missing/orphan docs."
            )
        else:
            print(
                f"  missing on-disk (non-virtual): {idx['missing_on_disk_non_virtual']}"
            )
            print(
                f"  orphan virtual _durable: {idx['orphan_virtual_durable']}  "
                f"healthy virtual kept: {idx['healthy_virtual_durable_kept']}"
            )
            samples = idx.get("sample_missing") or []
            if samples:
                print("  sample missing paths to prune:")
                for s in samples:
                    print(f"    doc_id={s['doc_id']} path={s['path']}")
            print(f"  note: {idx['virtual_durable_note']}")
            if vault_roots:
                print(f"  vault roots for path resolve: {vault_roots}")
        print(f"  inbox dedup index: {result['inbox_dedup_index']}")
        if not args.apply:
            print("  (re-run with --apply to mutate dual candidates)")
            if not args.prune_index:
                print("  (add --prune-index only if you intend document prune)")

    # --apply + index requested: surface blocked/error so automation does not
    # treat prevention as complete when the UNIQUE backstop was not installed.
    if args.apply and not args.no_index:
        idx_status = str(
            (result.get("inbox_dedup_index") or {}).get("status") or ""
        )
        if idx_status not in {"exists", "created", "skipped"}:
            if not args.json:
                print(
                    f"ERROR: inbox dedup index status={idx_status!r} "
                    f"(expected exists|created|skipped); UNIQUE backstop not "
                    f"installed. Resolve residual app-key collisions / "
                    f"needs-operator groups and re-run --apply.",
                    file=sys.stderr,
                )
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""One-time vault inbox cleanup (audit cluster C2) — operator-run.

Reconciles existing ``<home>/*-vault/inbox/*.json`` files against
``candidate_packets.derived_from`` in the Minni DB and ARCHIVES (never
deletes) files that no longer need to live in the hot inbox:

  * ``ingested_resolved`` — every candidate derived from the file is in a
    terminal state (accepted/rejected/redacted/expired/merged/superseded).
  * ``ingested``          — every eligible candidate string in the file has a
    DB row (any status). The DB rows carry the content from here on; the
    consolidation loop drains rows, not files.
  * ``expired_handoff``   — ``kind: handoff`` files older than the TTL
    (default 7 days). These are invisible to the lease ack channel
    (``requires_ack`` falsy), so without a TTL they pin the inbox forever —
    e.g. the orphaned 2026-04-26 ``...review-auth-migration-trace-pr.json``.

Everything else is left in place (not-yet-ingested stop candidates, fresh
handoffs, unknown kinds, unparseable files).

Safety contract:
  * DRY-RUN BY DEFAULT. Pass ``--apply`` to actually move files.
  * The ONLY file mutation is ``os.replace`` into ``<inbox>/.archive/`` with
    the filename preserved (numeric suffix on collision). This script
    contains no unlink/remove call by design — the tests assert that.
  * The DB is opened read-only.

Usage:
  python3 scripts/inbox_cleanup.py                      # dry-run, live home
  python3 scripts/inbox_cleanup.py --apply              # actually archive
  python3 scripts/inbox_cleanup.py --home /tmp/fixture --db /tmp/fixture.db
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_HOME = "~/.minni"
DEFAULT_DB = "~/.minni/minni.db"
DEFAULT_HANDOFF_TTL_DAYS = 7.0
ARCHIVE_DIRNAME = ".archive"

# Mirrors afm_passes.inbox_ingest.STOP_KINDS / afm_passes.inbox_archive.
STOP_KINDS = frozenset({"stop_candidates", "codex_stop_candidates"})
TERMINAL_STATUSES = frozenset(
    {"accepted", "rejected", "redacted", "expired", "merged", "superseded"}
)

# Both inbox filename formats:
#   2026-06-09-<base36 ms>-slug.json   (plugin writeInbox)
#   20260426T184233Z-slug.json         (daemon handoff channel)
_COMPACT_TS = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z-")
_DASHED_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})-([0-9a-z]+)")


def parse_name_epoch(name: str) -> Optional[float]:
    """Best-effort creation epoch (seconds) parsed from an inbox filename."""
    m = _COMPACT_TS.match(name)
    if m:
        try:
            import calendar
            parts = tuple(int(g) for g in m.groups())
            return float(calendar.timegm((*parts, 0, 0, 0)))
        except Exception:
            return None
    m = _DASHED_TS.match(name)
    if m:
        try:
            import calendar
            y, mo, d = (int(x) for x in m.group(1).split("-"))
            day = float(calendar.timegm((y, mo, d, 0, 0, 0, 0, 0, 0)))
        except Exception:
            return None
        try:
            ms = int(m.group(2), 36) / 1000.0
            # The second segment is Date.now().toString(36) when written by the
            # plugin; trust it only when it lands near the named day.
            if day <= ms < day + 2 * 86400:
                return ms
        except Exception:
            pass
        return day
    return None


def load_inbox_rows(db_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Map inbox filename -> [{candidate_index, status}, ...] from
    candidate_packets.derived_from. DB is opened read-only."""
    rows_by_file: Dict[str, List[Dict[str, Any]]] = {}
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, derived_from FROM candidate_packets "
            "WHERE derived_from LIKE '%\"source\": \"inbox\"%'"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            obj = json.loads(row["derived_from"])
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("source") != "inbox":
            continue
        name = obj.get("inbox_file")
        idx = obj.get("candidate_index")
        if not isinstance(name, str) or not name:
            continue
        rows_by_file.setdefault(name, []).append(
            {"candidate_index": idx, "status": row["status"]}
        )
    return rows_by_file


def _as_str_set(value: Any) -> set:
    return {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()


def eligible_candidate_indices(doc: Dict[str, Any]) -> Optional[List[int]]:
    """Indices inbox_ingest would ingest from this doc, or None when the file
    is not a stop-candidate file at all (kind gate). Mirrors _scan_inbox."""
    kind = doc.get("kind")
    is_stop_shape = (
        isinstance(doc.get("candidates"), list)
        and "slug" in doc
        and "last_task" in doc
    )
    if kind not in STOP_KINDS and not (kind is None and is_stop_shape):
        return None
    if doc.get("log_only") is True or doc.get("do_not_store") is True:
        return []
    log_only = _as_str_set(doc.get("log_only"))
    dns = _as_str_set(doc.get("do_not_store"))
    cands = doc.get("candidates") or []
    if not isinstance(cands, list):
        return []
    out: List[int] = []
    for idx, cand in enumerate(cands):
        if not isinstance(cand, str) or not cand.strip():
            continue
        if cand in log_only or cand in dns:
            continue
        out.append(idx)
    return out


def classify_file(
    path: Path,
    rows_by_file: Dict[str, List[Dict[str, Any]]],
    handoff_ttl_days: float,
    now: float,
) -> str:
    """Return an archive reason for `path`, or 'keep'."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "keep"
    if not isinstance(doc, dict):
        return "keep"

    rows = rows_by_file.get(path.name, [])
    eligible = eligible_candidate_indices(doc)
    if eligible is not None:
        # Stop-candidate file: archive only when every eligible candidate has a
        # DB row (the rows carry the content from here on).
        ingested_indices = {
            r["candidate_index"] for r in rows if isinstance(r["candidate_index"], int)
        }
        if eligible and set(eligible) <= ingested_indices:
            if all(r["status"] in TERMINAL_STATUSES for r in rows):
                return "ingested_resolved"
            return "ingested"
        return "keep"  # not (fully) ingested yet — or nothing ingestible

    if rows and all(r["status"] in TERMINAL_STATUSES for r in rows):
        # Odd/legacy shape that nonetheless has fully-resolved derived rows.
        return "ingested_resolved"

    if doc.get("kind") == "handoff":
        created = parse_name_epoch(path.name)
        if created is None:
            try:
                created = path.stat().st_mtime
            except OSError:
                return "keep"
        if now - created > handoff_ttl_days * 86400:
            return "expired_handoff"
    return "keep"


def archive_file(path: Path) -> str:
    """Rename `path` into its sibling .archive/ dir (name preserved; numeric
    suffix on collision). The only file mutation in this script."""
    archive_dir = path.parent / ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / path.name
    suffix = 1
    while target.exists():
        target = archive_dir / f"{path.stem}.{suffix}{path.suffix}"
        suffix += 1
    os.replace(path, target)
    return str(target)


def discover_vault_inboxes(home: Path) -> List[Path]:
    out = sorted(p for p in home.glob("*-vault/inbox") if p.is_dir())
    bare = home / "vault" / "inbox"
    if bare.is_dir() and bare not in out:
        out.append(bare)
    return out


def run_cleanup(
    home: Path,
    db_path: Path,
    handoff_ttl_days: float = DEFAULT_HANDOFF_TTL_DAYS,
    apply: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    now = time.time() if now is None else now
    rows_by_file = load_inbox_rows(db_path) if db_path.exists() else {}
    report: Dict[str, Any] = {
        "home": str(home),
        "db": str(db_path),
        "dry_run": not apply,
        "handoff_ttl_days": handoff_ttl_days,
        "vaults": {},
    }
    for inbox in discover_vault_inboxes(home):
        vault_name = inbox.parent.name
        counts = {"total": 0, "keep": 0, "ingested_resolved": 0, "ingested": 0, "expired_handoff": 0}
        to_archive: List[Dict[str, str]] = []
        for path in sorted(inbox.glob("*.json")):
            counts["total"] += 1
            reason = classify_file(path, rows_by_file, handoff_ttl_days, now)
            counts[reason] += 1
            if reason == "keep":
                continue
            entry = {"file": path.name, "reason": reason}
            if apply:
                entry["archived_to"] = archive_file(path)
            to_archive.append(entry)
        report["vaults"][vault_name] = {
            "inbox": str(inbox),
            "counts": counts,
            ("archived" if apply else "would_archive"): to_archive,
        }
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", default=DEFAULT_HOME, help="Minni home containing *-vault dirs")
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to minni.db")
    parser.add_argument(
        "--handoff-ttl-days", type=float, default=DEFAULT_HANDOFF_TTL_DAYS,
        help="Archive kind:handoff inbox files older than this many days",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Report only (default; use --apply to archive)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually move files into inbox/.archive/ (overrides --dry-run)",
    )
    args = parser.parse_args(argv)

    report = run_cleanup(
        home=Path(args.home).expanduser(),
        db_path=Path(args.db).expanduser(),
        handoff_ttl_days=args.handoff_ttl_days,
        apply=bool(args.apply),
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

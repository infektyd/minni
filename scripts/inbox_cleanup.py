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
  * ``expired_handoff``   — ``kind: handoff`` files past their drain point:
    pending ack-channel leases (``requires_ack`` truthy) ONLY once their own
    ``expires_at`` has passed (a live lease is the daemon's to drain, never
    ours); requires_ack-falsy orphans once older than the TTL (default 7
    days) — these are invisible to the lease ack channel, so without a TTL
    they pin the inbox forever, e.g. the orphaned 2026-04-26
    ``...review-auth-migration-trace-pr.json``.
  * ``acked_handoff``     — handoff packets whose lease was already
    acknowledged (``ack_status`` set): terminal leftovers, archived
    regardless of age and never mislabeled as expired.
  * ``nothing_ingestible`` — stop-candidate files inbox_ingest will NEVER
    take anything from (file-level ``log_only``/``do_not_store``, all
    candidates filtered or blank) with no derived DB rows, once older than
    the residue threshold (default 14 days).
  * ``unrecognized_kind`` — explicit non-handoff kinds inbox_ingest drops at
    its kind gate (``*_precompact_handoff``, ``failed_command``, ...), once
    older than the residue threshold. Without this the residue has no drain
    path at all.

Everything else is left in place (not-yet-ingested stop candidates, fresh
handoffs, live ack leases, fresh residue, unparseable files).

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
DEFAULT_RESIDUE_TTL_DAYS = 14.0
ARCHIVE_DIRNAME = ".archive"

ARCHIVE_REASONS = (
    "ingested_resolved",
    "ingested",
    "expired_handoff",
    "acked_handoff",
    "nothing_ingestible",
    "unrecognized_kind",
)

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


def parse_iso_epoch(value: Any) -> Optional[float]:
    """Epoch seconds for an ISO-8601 UTC string (the daemon's expires_at
    format), or None when missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        import calendar
        return float(calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return None


def _file_age_seconds(path: Path, now: float) -> Optional[float]:
    created = parse_name_epoch(path.name)
    if created is None:
        try:
            created = path.stat().st_mtime
        except OSError:
            return None
    return now - created


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
    residue_ttl_days: float = DEFAULT_RESIDUE_TTL_DAYS,
) -> str:
    """Return an archive reason for `path` (one of ARCHIVE_REASONS), or 'keep'."""
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
        if not eligible and not rows:
            # inbox_ingest will NEVER take anything from this file (file-level
            # log_only/do_not_store, all candidates filtered or blank) and no
            # DB row will ever drain it — archive the residue once stale.
            age = _file_age_seconds(path, now)
            if age is not None and age > residue_ttl_days * 86400:
                return "nothing_ingestible"
        return "keep"  # not (fully) ingested yet — or fresh residue

    if rows and all(r["status"] in TERMINAL_STATUSES for r in rows):
        # Odd/legacy shape that nonetheless has fully-resolved derived rows.
        return "ingested_resolved"

    kind = doc.get("kind")
    if kind == "handoff":
        if doc.get("ack_status"):
            # Lease already acknowledged — terminal leftover, never 'expired'.
            return "acked_handoff"
        if doc.get("requires_ack"):
            # Live ack-channel lease: the daemon owns it. Only its OWN expiry
            # drains it here; a missing/unparseable expires_at means keep.
            expires_at = parse_iso_epoch(doc.get("expires_at"))
            if expires_at is not None and expires_at <= now:
                return "expired_handoff"
            return "keep"
        age = _file_age_seconds(path, now)
        if age is not None and age > handoff_ttl_days * 86400:
            return "expired_handoff"
        return "keep"

    if isinstance(kind, str) and kind:
        # Explicit non-handoff kinds are dropped at inbox_ingest's kind gate
        # (*_precompact_handoff, failed_command, ...) and have no drain path —
        # archive the residue once stale so it stops pinning the inbox.
        age = _file_age_seconds(path, now)
        if age is not None and age > residue_ttl_days * 86400:
            return "unrecognized_kind"
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
    residue_ttl_days: float = DEFAULT_RESIDUE_TTL_DAYS,
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
        "residue_ttl_days": residue_ttl_days,
        "vaults": {},
    }
    for inbox in discover_vault_inboxes(home):
        vault_name = inbox.parent.name
        counts = {"total": 0, "keep": 0, **{reason: 0 for reason in ARCHIVE_REASONS}}
        to_archive: List[Dict[str, str]] = []
        for path in sorted(inbox.glob("*.json")):
            counts["total"] += 1
            reason = classify_file(path, rows_by_file, handoff_ttl_days, now, residue_ttl_days)
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
        help="Archive requires_ack-less kind:handoff inbox files older than this many days",
    )
    parser.add_argument(
        "--residue-ttl-days", type=float, default=DEFAULT_RESIDUE_TTL_DAYS,
        help="Archive never-ingestible residue (nothing_ingestible / "
        "unrecognized_kind) older than this many days",
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
        residue_ttl_days=args.residue_ttl_days,
        apply=bool(args.apply),
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

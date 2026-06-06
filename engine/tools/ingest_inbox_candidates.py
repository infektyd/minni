#!/usr/bin/env python3
"""Ingest inbox stop-candidates into candidate_packets so the AFM loop sees them.

Background
----------
Two proposal channels exist; only one feeds the AFM consolidation loop:

  (1) ``minni_learn`` -> INSERT INTO candidate_packets (status='proposed')
      -> the loop drains these -> durable ``learnings``. WORKS.

  (2) Stop/PreCompact hooks -> ``<vault>/inbox/*.json`` (kind
      'codex_stop_candidates' / 'codex_precompact_handoff'). These are NEVER
      ingested into candidate_packets, so the loop never sees them.

This tool drains channel (2)'s ``codex_stop_candidates`` files into
candidate_packets using the SAME canonical insert shape the daemon uses
(see ``_stage_candidate`` in minnid.py), so the inserted rows are picked up by
the consolidation pass (which selects purely on ``status='proposed'`` ordered
by ``proposed_at ASC`` — no principal filter; verified against
``afm_passes/consolidation.py``).

Safety / contract
-----------------
* Only ``kind == 'codex_stop_candidates'`` files are processed (NOT handoffs,
  NOT precompact).
* A FILE is skipped if its ``log_only`` or ``do_not_store`` is boolean ``True``
  (defensive: honors the literal "skip if log_only=true" instruction even
  though current files carry these as advisory string LISTS, never booleans).
* A CANDIDATE string is skipped if it appears verbatim in that file's
  ``log_only`` or ``do_not_store`` list (belt-and-suspenders so anything
  flagged transient/forbidden never gets stored).
* IDEMPOTENT: each inserted row carries
  ``derived_from = {"source":"inbox", "inbox_file": <basename>,
  "candidate_index": <i>, "kind": "codex_stop_candidates",
  "content_sha1": <hash>}``. Before inserting, the tool checks for an existing
  row (ANY status) with the same principal + inbox_file + candidate_index, so
  re-running is a no-op even after the loop has consumed (accepted/rejected)
  the row.
* ``--dry-run`` (DEFAULT) reports counts and writes NOTHING.

Usage
-----
  # dry-run (default) for the codex vault:
  python3 tools/ingest_inbox_candidates.py \
      --vault ~/.minni/codex-vault --principal codex

  # live insert:
  python3 tools/ingest_inbox_candidates.py \
      --vault ~/.minni/codex-vault --principal codex --live
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KIND = "codex_stop_candidates"
CONTENT_CAP = 200000  # matches minnid.py canonical insert bound
DEFAULT_DB = os.path.expanduser(
    os.environ.get("MINNI_DB_PATH", "~/.minni/minni.db")
)


def _content_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _is_bool_true(v: Any) -> bool:
    return v is True


def _as_str_set(v: Any) -> set:
    if isinstance(v, list):
        return {x for x in v if isinstance(x, str)}
    return set()


def _load_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _file_createdat_epoch(doc: Dict[str, Any]) -> Optional[float]:
    raw = doc.get("createdAt")
    if not isinstance(raw, str):
        return None
    # createdAt is ISO-8601 Zulu, e.g. "2026-06-06T13:06:21.890Z"
    try:
        from datetime import datetime, timezone

        s = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _existing_keys(conn: sqlite3.Connection, principal: str) -> set:
    """Return set of (inbox_file, candidate_index) already present for principal.

    Matches on derived_from regardless of status so re-runs are no-ops even
    after the loop has resolved (accepted/rejected) a previously-ingested row.
    """
    keys: set = set()
    cur = conn.execute(
        "SELECT derived_from FROM candidate_packets WHERE principal=?",
        (principal,),
    )
    for (df,) in cur.fetchall():
        if not df:
            continue
        try:
            obj = json.loads(df)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("source") != "inbox":
            continue
        f = obj.get("inbox_file")
        i = obj.get("candidate_index")
        if isinstance(f, str) and isinstance(i, int):
            keys.add((f, i))
    return keys


def collect(vault: Path, principal: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    """Scan inbox; classify each candidate. Returns counts + a list of inserts."""
    inbox = vault / "inbox"
    files = sorted(inbox.glob("*.json")) if inbox.is_dir() else []

    existing = _existing_keys(conn, principal)

    stats = {
        "inbox_dir": str(inbox),
        "files_scanned": 0,
        "files_matching_kind": 0,
        "files_skipped_bool_flag": 0,
        "candidates_total": 0,
        "candidates_skipped_log_only": 0,
        "candidates_skipped_do_not_store": 0,
        "candidates_skipped_empty": 0,
        "would_insert": 0,
        "already_present": 0,
    }
    to_insert: List[Dict[str, Any]] = []

    for path in files:
        stats["files_scanned"] += 1
        doc = _load_file(path)
        if not doc or doc.get("kind") != KIND:
            continue
        stats["files_matching_kind"] += 1

        log_only = doc.get("log_only")
        do_not_store = doc.get("do_not_store")
        if _is_bool_true(log_only) or _is_bool_true(do_not_store):
            stats["files_skipped_bool_flag"] += 1
            continue

        log_only_set = _as_str_set(log_only)
        dns_set = _as_str_set(do_not_store)

        ws = doc.get("workspace_id") or "default"
        # file agent_id is the canonical principal of origin; fall back to arg.
        file_principal = doc.get("agent_id") or principal

        created = _file_createdat_epoch(doc)
        proposed_at = created if created is not None else time.time()

        candidates = doc.get("candidates") or []
        if not isinstance(candidates, list):
            continue

        for idx, cand in enumerate(candidates):
            if not isinstance(cand, str) or not cand.strip():
                stats["candidates_skipped_empty"] += 1
                continue
            stats["candidates_total"] += 1

            if cand in log_only_set:
                stats["candidates_skipped_log_only"] += 1
                continue
            if cand in dns_set:
                stats["candidates_skipped_do_not_store"] += 1
                continue

            key = (path.name, idx)
            if key in existing:
                stats["already_present"] += 1
                continue

            content = cand.strip()[:CONTENT_CAP]
            derived_from = {
                "source": "inbox",
                "inbox_file": path.name,
                "candidate_index": idx,
                "kind": KIND,
                "content_sha1": _content_sha1(content),
            }
            to_insert.append(
                {
                    "principal": file_principal,
                    "workspace_id": ws,
                    "content": content,
                    "derived_from": json.dumps(derived_from),
                    "proposed_at": proposed_at,
                }
            )
            stats["would_insert"] += 1
            # guard within-run dup (same file+index can't recur, but defensive)
            existing.add(key)

    return {"stats": stats, "to_insert": to_insert}


def do_insert(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    """Insert rows using the canonical candidate_packets shape. Returns count."""
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            conn.execute(
                """
                INSERT INTO candidate_packets
                (principal, workspace_id, layer, privacy_level, content,
                 evidence_refs, derived_from, instruction_like, status, proposed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    r["principal"],
                    r["workspace_id"],
                    None,            # layer
                    None,            # privacy_level
                    r["content"],
                    json.dumps([]),  # evidence_refs (canonical: JSON array)
                    r["derived_from"],
                    0,               # instruction_like
                    r["proposed_at"],
                ),
            )
            n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", required=True, help="vault root (contains inbox/)")
    ap.add_argument("--principal", default="codex",
                    help="fallback principal if file lacks agent_id")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to minni.db")
    ap.add_argument("--live", action="store_true",
                    help="actually insert (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry-run (default behavior)")
    args = ap.parse_args(argv)

    dry_run = not args.live
    vault = Path(os.path.expanduser(args.vault)).resolve()
    db_path = os.path.expanduser(args.db)

    if not vault.is_dir():
        print(f"ERROR: vault not found: {vault}", file=sys.stderr)
        return 2
    if not os.path.exists(db_path):
        print(f"ERROR: db not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        result = collect(vault, args.principal, conn)
        stats = result["stats"]
        rows = result["to_insert"]

        mode = "DRY-RUN" if dry_run else "LIVE"
        print(f"=== ingest_inbox_candidates [{mode}] ===")
        print(f"vault:      {vault}")
        print(f"principal:  {args.principal} (file agent_id wins per-file)")
        print(f"db:         {db_path}")
        print(f"inbox dir:  {stats['inbox_dir']}")
        print("-" * 50)
        print(f"files scanned:                  {stats['files_scanned']}")
        print(f"files matching kind={KIND}: {stats['files_matching_kind']}")
        print(f"files skipped (bool flag):      {stats['files_skipped_bool_flag']}")
        print(f"candidate strings (non-empty):  {stats['candidates_total']}")
        print(f"  skipped empty:                {stats['candidates_skipped_empty']}")
        print(f"  skipped log_only match:       {stats['candidates_skipped_log_only']}")
        print(f"  skipped do_not_store match:   {stats['candidates_skipped_do_not_store']}")
        print(f"  already present (idempotent): {stats['already_present']}")
        print(f"  would-insert:                 {stats['would_insert']}")
        print("-" * 50)

        if dry_run:
            print("DRY-RUN: no rows written. Re-run with --live to insert.")
            return 0

        inserted = do_insert(conn, rows)
        print(f"LIVE: inserted {inserted} row(s) (status='proposed').")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

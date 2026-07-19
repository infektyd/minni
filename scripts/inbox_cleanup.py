#!/usr/bin/env python3
"""One-time vault inbox cleanup (audit cluster C2) — operator-run.

Reconciles existing ``<home>/*-vault/inbox/*.json`` files against
``candidate_packets.derived_from`` in the Minni DB and ARCHIVES (never
deletes) files that no longer need to live in the hot inbox:

  * ``ingested_resolved`` — every candidate derived from the file is in a
    terminal state (accepted/rejected/redacted/expired/merged/superseded).
  * ``ingested``          — every eligible candidate string in the file has a
    MATCHING DB row (any status; matched by ingest-written content_sha1 or
    stored content, so a same-named file in another vault never inherits the
    rows). The DB rows carry the content from here on; the consolidation loop
    drains rows, not files.
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
  * ``agent_mismatch_quarantine`` (audit §4 / W3) — a stop-candidate file
    whose declared ``agent_id`` disagrees with the principal derived from its
    vault dir name (mirrors ``inbox_ingest._scan_inbox``'s
    ``skipped_by_kind["_agent_mismatch"]`` test). This is a PERMANENT skip —
    the mismatch never self-resolves, so the file is never ingested and never
    gets a candidate_packets row, meaning ``ingested``/``ingested_resolved``
    above can never trigger for it either. Once older than the residue
    threshold it is MOVED (not archived — the row-based reasons above ARCHIVE
    because the DB carries the content onward; this file's content was NEVER
    captured anywhere, so it is QUARANTINED with a reason sidecar instead) to
    ``<inbox>/quarantine/`` via the same daemon-side helper this reason name
    matches (``afm_passes.inbox_quarantine``); this script re-implements the
    move+sidecar logic locally rather than importing it (see "Standalone
    script" below).

Everything else is left in place (not-yet-ingested stop candidates, fresh
handoffs, live ack leases, fresh residue, unparseable files).

Standalone script (no `minni` package import):
  This script deliberately duplicates rather than imports
  ``afm_passes.inbox_ingest`` / ``afm_passes.inbox_quarantine`` constants and
  helpers (``STOP_KINDS``, ``CONTENT_CAP``, ``_content_sha1``,
  ``eligible_candidates``, the vault-slug agent alias table, the
  quarantine-move helper — all mirrored here, not imported). This is
  deliberate, not an oversight: the usage contract above is bare
  ``python3 scripts/inbox_cleanup.py`` with only ``scripts/`` on
  ``sys.path`` (see the ``sys.path.insert`` a few lines down) — there is no
  guarantee ``src/`` is importable or that `minni` is installed in whatever
  interpreter runs this (verified: plain `python3 -c "import minni"` from
  this repo resolves to an unrelated/broken namespace package, not the real
  ``src/minni`` — the daemon's own dependency is only guaranteed inside the
  project's ``.venv``). Importing the daemon package here would silently
  break the standalone invocation this script exists for.

Safety contract:
  * DRY-RUN BY DEFAULT. Pass ``--apply`` to actually move files.
  * The ONLY file mutations are ``os.replace`` into ``<inbox>/.archive/`` or
    ``<inbox>/quarantine/``, both with the filename preserved (numeric suffix
    on collision). This script contains no unlink/remove call by design — the
    tests assert that.
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
    "agent_mismatch_quarantine",
)
# Reasons in ARCHIVE_REASONS that MOVE to quarantine/ instead of .archive/ —
# their content was never captured in the DB, unlike every other reason here.
QUARANTINE_REASONS = frozenset({"agent_mismatch_quarantine"})
QUARANTINE_DIRNAME = "quarantine"
QUARANTINE_REASON_SUFFIX = ".reason.json"

# Mirrors afm_passes.inbox_ingest.STOP_KINDS / afm_passes.inbox_archive.
STOP_KINDS = frozenset({"stop_candidates", "codex_stop_candidates"})
TERMINAL_STATUSES = frozenset(
    {"accepted", "rejected", "redacted", "expired", "merged", "superseded"}
)

# Mirrors afm_passes.inbox_ingest._VAULT_SLUG_TO_AGENT_ID / _principal_for_inbox
# (triplicated per this script's standalone-import-independence contract —
# see the module docstring's "Standalone script" section).
_VAULT_SLUG_TO_AGENT_ID: Dict[str, str] = {
    "claudecode": "claude-code",
    "codex": "codex",
    "gemini": "gemini",
    "hermes": "hermes",
    "kilocode": "kilocode",
    "openclaw": "openclaw",
    "grok-build": "grok-build",
    "grok-beta": "grok-build",
    "grok": "grok-build",
}


def _principal_for_inbox_dir(vault_dir_name: str) -> str:
    """Owning agent for a `<agent>-vault` dir name, mirroring
    afm_passes.inbox_ingest._principal_for_inbox. Falls back to the raw slug
    (never a global default here — this script has no fallback_principal
    concept) when the alias table has no entry."""
    if vault_dir_name.endswith("-vault"):
        slug = vault_dir_name[: -len("-vault")]
        return _VAULT_SLUG_TO_AGENT_ID.get(slug, slug) or slug
    return vault_dir_name


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
    """Map inbox filename -> [{candidate_index, status, content_sha1,
    content}, ...] from candidate_packets.derived_from. DB is opened
    read-only. NOTE: derived_from records only a bare filename, so the same
    name in two vaults shares one row list — classify_file resolves the
    ambiguity per file via content correspondence."""
    rows_by_file: Dict[str, List[Dict[str, Any]]] = {}
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status, content, derived_from FROM candidate_packets "
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
            {
                "candidate_index": idx,
                "status": row["status"],
                "content_sha1": obj.get("content_sha1"),
                "content": row["content"],
            }
        )
    return rows_by_file


def _as_str_set(value: Any) -> set:
    return {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()


CONTENT_CAP = 200000  # mirrors afm_passes.inbox_ingest.CONTENT_CAP


def _content_sha1(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def eligible_candidates(doc: Dict[str, Any]) -> Optional[Dict[int, str]]:
    """``{index: capped content}`` inbox_ingest would ingest from this doc, or
    None when the file is not a stop-candidate file at all (kind gate).
    Mirrors _scan_inbox."""
    kind = doc.get("kind")
    is_stop_shape = (
        isinstance(doc.get("candidates"), list)
        and "slug" in doc
        and "last_task" in doc
    )
    if kind not in STOP_KINDS and not (kind is None and is_stop_shape):
        return None
    if doc.get("log_only") is True or doc.get("do_not_store") is True:
        return {}
    log_only = _as_str_set(doc.get("log_only"))
    dns = _as_str_set(doc.get("do_not_store"))
    cands = doc.get("candidates") or []
    if not isinstance(cands, list):
        return {}
    out: Dict[int, str] = {}
    for idx, cand in enumerate(cands):
        if not isinstance(cand, str) or not cand.strip():
            continue
        if cand in log_only or cand in dns:
            continue
        out[idx] = cand.strip()[:CONTENT_CAP]
    return out


def _row_matches_candidate(row: Dict[str, Any], content: str) -> bool:
    """True when a DB row provably corresponds to a file candidate: its
    ingest-written content_sha1 matches, or (legacy rows without a sha) its
    stored content matches. Rows carrying neither fingerprint are tolerated by
    index alone (pre-fingerprint legacy rows)."""
    sha = row.get("content_sha1")
    if isinstance(sha, str) and sha:
        return sha == _content_sha1(content)
    stored = row.get("content")
    if isinstance(stored, str):
        return stored == content
    return True  # legacy row without any fingerprint: index-only match


def _legacy_row_corresponds(row: Dict[str, Any], raw_text: str) -> bool:
    """Correspondence check for the odd/legacy-shape branch (no candidate
    indices to address): the row's stored content must literally appear in the
    file text. Rows without stored content (fingerprint-less legacy) are
    tolerated."""
    stored = row.get("content")
    if isinstance(stored, str) and stored.strip():
        return stored in raw_text
    return True


def classify_file(
    path: Path,
    rows_by_file: Dict[str, List[Dict[str, Any]]],
    handoff_ttl_days: float,
    now: float,
    residue_ttl_days: float = DEFAULT_RESIDUE_TTL_DAYS,
) -> str:
    """Return an archive reason for `path` (one of ARCHIVE_REASONS), or 'keep'."""
    try:
        raw_text = path.read_text(encoding="utf-8")
        doc = json.loads(raw_text)
    except Exception:
        return "keep"
    if not isinstance(doc, dict):
        return "keep"

    rows = rows_by_file.get(path.name, [])
    eligible = eligible_candidates(doc)
    if eligible is not None:
        # Stop-candidate file: archive only when every eligible candidate has a
        # MATCHING DB row (the rows carry the content from here on). Rows are
        # keyed by bare filename across all vaults, so a same-named file in
        # another vault (different content) must NOT inherit this file's rows —
        # _row_matches_candidate pins each row to the candidate text it was
        # ingested from.
        matched = [
            r
            for r in rows
            if isinstance(r["candidate_index"], int)
            and r["candidate_index"] in eligible
            and _row_matches_candidate(r, eligible[r["candidate_index"]])
        ]
        if eligible and set(eligible) <= {r["candidate_index"] for r in matched}:
            if all(r["status"] in TERMINAL_STATUSES for r in matched):
                return "ingested_resolved"
            return "ingested"
        if not eligible and not rows:
            # inbox_ingest will NEVER take anything from this file (file-level
            # log_only/do_not_store, all candidates filtered or blank) and no
            # DB row will ever drain it — archive the residue once stale.
            age = _file_age_seconds(path, now)
            if age is not None and age > residue_ttl_days * 86400:
                return "nothing_ingestible"
        if eligible and not matched:
            # Real, ingestible content, but ZERO DB rows correspond to THIS
            # file at all (not merely partially-ingested — inbox_ingest never
            # took anything from it). Two known causes: (1) genuinely fresh,
            # not yet picked up by the loop — leave it; (2) a PERMANENT
            # agent_id/vault-principal mismatch (audit §4 / W3) — the loop's
            # kind gate skips it identically on every tick, forever, since the
            # mismatch never self-resolves. Scoped to that confirmed cohort
            # only: an agent-less or agent-matching file with zero rows is
            # simply fresh, not broken, and stays 'keep'.
            file_agent = str(doc.get("agent_id") or "").strip()
            vault_principal = _principal_for_inbox_dir(path.parent.parent.name)
            if file_agent and file_agent != vault_principal:
                age = _file_age_seconds(path, now)
                if age is not None and age > residue_ttl_days * 86400:
                    return "agent_mismatch_quarantine"
        return "keep"  # not (fully) ingested yet — or fresh residue

    kind = doc.get("kind")
    if kind != "handoff" and rows and all(
        r["status"] in TERMINAL_STATUSES for r in rows
    ) and all(_legacy_row_corresponds(r, raw_text) for r in rows):
        # Odd/legacy shape that nonetheless has fully-resolved derived rows
        # whose content provably came from this file. kind:handoff never takes
        # this branch (forged rows must not drain a live lease early); handoffs
        # drain through their own TTL/ack predicates below.
        return "ingested_resolved"

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
    suffix on collision). Renames only — never unlinks."""
    archive_dir = path.parent / ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / path.name
    suffix = 1
    while target.exists():
        target = archive_dir / f"{path.stem}.{suffix}{path.suffix}"
        suffix += 1
    os.replace(path, target)
    return str(target)


def _agent_mismatch_reason_payload(
    path: Path, now: float, residue_ttl_days: float
) -> Dict[str, Any]:
    """Sidecar payload for an ``agent_mismatch_quarantine`` move. Uses the
    same ``reason`` value ("_agent_mismatch") as
    ``afm_passes.inbox_quarantine`` writes from the live AFM loop, so a
    health_report ``by_reason`` count is meaningful regardless of which of
    the two quarantine sources (loop drain vs. this one-time migration)
    produced it."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        doc = {}
    file_agent = str(doc.get("agent_id") or "").strip()
    vault_principal = _principal_for_inbox_dir(path.parent.parent.name)
    return {
        "reason": "_agent_mismatch",
        "detected_agent_id": file_agent,
        "resolved_vault_principal": vault_principal,
        "quarantined_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "ttl_days": residue_ttl_days,
    }


def quarantine_file(path: Path, reason_payload: Dict[str, Any]) -> str:
    """Rename `path` into its sibling quarantine/ dir (name preserved;
    numeric suffix on collision) and write a `<name>.reason.json` sidecar
    alongside it. The only OTHER file mutation in this script besides
    archive_file — still os.replace only, never unlink."""
    q_dir = path.parent / QUARANTINE_DIRNAME
    q_dir.mkdir(parents=True, exist_ok=True)
    target = q_dir / path.name
    suffix = 1
    while target.exists():
        target = q_dir / f"{path.stem}.{suffix}{path.suffix}"
        suffix += 1
    os.replace(path, target)
    reason_path = target.parent / f"{target.name}{QUARANTINE_REASON_SUFFIX}"
    reason_path.write_text(json.dumps(reason_payload, indent=2), encoding="utf-8")
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
                if reason in QUARANTINE_REASONS:
                    payload = _agent_mismatch_reason_payload(path, now, residue_ttl_days)
                    entry["archived_to"] = quarantine_file(path, payload)
                else:
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
    # Dry-run is implied by the absence of --apply; the flags are mutually
    # exclusive so an explicitly requested dry run can never mutate
    # (`--dry-run --apply` is an argparse error, not a silent apply).
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="Report only (the default; mutually exclusive with --apply)",
    )
    mode.add_argument(
        "--apply", action="store_true",
        help="Actually move files into inbox/.archive/",
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

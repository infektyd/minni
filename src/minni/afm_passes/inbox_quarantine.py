"""Quarantine drain for permanently-unresolvable inbox stop-candidate files
(audit §4 "Consolidation inbox residue" / W3).

Background
----------
``afm_passes.inbox_ingest._scan_inbox`` skips a stop-candidate file when its
declared ``agent_id`` disagrees with the principal derived from its vault dir
name (``_principal_for_inbox``) and buckets it under
``skipped_by_kind["_agent_mismatch"]`` (inbox_ingest.py:256-259). That
disagreement never self-resolves: the file's ``agent_id`` is static and the
vault-dir-derived principal is a pure function of the dir name, so the SAME
file produces the IDENTICAL skip on every future tick, forever. Because the
file is never ingested, it never gets a ``candidate_packets`` row, so
``afm_passes.inbox_archive`` (which archives strictly on DB-row resolution)
can never touch it either — a genuine dead end with no drain path (the
2026-07-19 audit's 59-file ``unknown-vault/inbox`` cohort: slug ``unknown``
!= agent ``unknown-agent``).

This module is the drain: once such a file has sat past a TTL (grace window
for merely-fresh files), it is moved into a new sibling ``<inbox>/quarantine/``
dir with a ``<name>.reason.json`` sidecar recording why, so an operator can
inspect and remediate instead of the residue growing invisibly forever.

Scope
-----
Deliberately narrow: only ``_agent_mismatch`` files (the confirmed permanent,
non-self-healing cohort) are drained here. ``_malformed_kind`` and
``_unrecognized`` are DIFFERENT bugs (a malformed/absent kind is not proof the
content is unrecoverable) and are explicitly out of scope for this pass —
extending to them is a stretch goal, not core to the punch-list fix.

Contract
--------
* NEVER hard-deletes. The only file operation is ``os.replace`` — mirrors the
  ``.archive/`` convention in ``inbox_archive.py``.
* Idempotent and best-effort: once a file is quarantined it is gone from the
  live inbox, so a re-run naturally sees nothing to do (no separate bookkeeping
  needed). A missing file is a quiet no-op.
* ``quarantine/`` is invisible to ``inbox_ingest`` (its inbox glob is
  non-recursive ``*.json`` at inbox root) and to ``inbox_archive`` for the same
  reason — exactly like ``.archive/`` already is.
* The TTL is a grace window, not an instant trigger: a fresh agent-mismatch
  file might still be corrected upstream (e.g. a hook fix); only files older
  than ``ttl_days`` (default ``DEFAULT_RESIDUE_TTL_DAYS`` — mirrors
  ``scripts/inbox_cleanup.py``'s residue TTL convention/value, operator-tunable
  via ``config.afm_loop_schedule["passes"]["consolidation"]
  ["inbox_quarantine_ttl_days"]``) are moved.
* Fail-closed stays intact: this module never changes what ``_scan_inbox``
  ingests or skips. It only decides what happens to an already-skipped file
  AFTER the skip. Fail-LOUD is what this module (plus the health_report /
  counter surfaces that consume it) adds: the condition becomes
  operator-visible instead of a silent, permanent pile-up.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from minni.afm_passes.inbox_ingest import (
    STOP_KINDS,
    _file_createdat_epoch,
    _is_stop_candidate_shape,
    _principal_for_inbox,
    discover_inboxes,
)

logger = logging.getLogger("minnid")

QUARANTINE_DIRNAME = "quarantine"
REASON_SUFFIX = ".reason.json"

# Mirrors scripts/inbox_cleanup.py's DEFAULT_RESIDUE_TTL_DAYS convention (same
# name, same default value) — kept as an independent constant here rather than
# imported, since scripts/inbox_cleanup.py is deliberately import-independent
# of the `minni` package (see that module's docstring / this package's commit
# message) and this module must not create a reverse dependency on it.
DEFAULT_RESIDUE_TTL_DAYS = 14.0


def quarantine_inbox_file(file_path: Path, reason_payload: Dict[str, Any]) -> Optional[str]:
    """Move an inbox file into its sibling ``quarantine/`` dir, writing a
    ``<name>.reason.json`` sidecar alongside it. NEVER unlinks; the filename
    is preserved (a numeric suffix is added on collision, mirroring
    ``inbox_archive.archive_inbox_file``). Returns the quarantined path, or
    ``None`` if the file was already gone or the target would land outside
    the quarantine dir (containment check, defense-in-depth)."""
    file_path = Path(file_path)
    if not file_path.is_file():
        return None
    q_dir = file_path.parent / QUARANTINE_DIRNAME
    q_dir.mkdir(parents=True, exist_ok=True)
    target = q_dir / file_path.name
    suffix = 1
    while target.exists():
        target = q_dir / f"{file_path.stem}.{suffix}{file_path.suffix}"
        suffix += 1
    # Containment (defense-in-depth, mirrors archive_inbox_file): the rename
    # target must stay inside the sibling quarantine dir.
    try:
        if target.resolve().parent != q_dir.resolve():
            logger.warning("inbox quarantine: refusing non-contained target %s", target)
            return None
    except OSError:
        return None
    os.replace(file_path, target)
    reason_path = target.parent / f"{target.name}{REASON_SUFFIX}"
    try:
        reason_path.write_text(json.dumps(reason_payload, indent=2), encoding="utf-8")
    except OSError:
        logger.warning(
            "inbox quarantine: %s moved but reason sidecar failed to write", target
        )
    return str(target)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _stale_agent_mismatch_files(
    inbox: Path, fallback_principal: str, ttl_seconds: float, now: float,
) -> List[Tuple[Path, Dict[str, Any]]]:
    """``(path, reason_payload)`` for every file in ``inbox`` that
    ``inbox_ingest._scan_inbox`` would bucket under
    ``skipped_by_kind["_agent_mismatch"]`` (the SAME ``file_agent !=
    inbox_principal`` test, inbox_ingest.py:256-259) and that is older than
    ``ttl_seconds``. Scoped to that cohort only — a malformed/missing kind or
    an unrecognized kind is a different bug and is left untouched here."""
    out: List[Tuple[Path, Dict[str, Any]]] = []
    inbox_principal = _principal_for_inbox(inbox, fallback_principal)
    for path in sorted(inbox.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        if not isinstance(kind, str) and kind is not None:
            continue  # _malformed_kind: different bug, out of scope here
        if kind not in STOP_KINDS and not (kind is None and _is_stop_candidate_shape(doc)):
            continue  # _unrecognized / explicit other kind: different bug
        file_agent = str(doc.get("agent_id") or "").strip()
        if not file_agent or file_agent == inbox_principal:
            continue  # not an _agent_mismatch file at all
        created = _file_createdat_epoch(doc)
        if created is None:
            try:
                created = path.stat().st_mtime
            except OSError:
                continue
        if (now - created) <= ttl_seconds:
            continue  # within the grace window: might still self-correct
        out.append((
            path,
            {
                "reason": "_agent_mismatch",
                "detected_agent_id": file_agent,
                "resolved_vault_principal": inbox_principal,
                "quarantined_at": _iso(now),
                "ttl_days": round(ttl_seconds / 86400.0, 6),
            },
        ))
    return out


def quarantine_stale_agent_mismatch(
    config,
    inboxes: Optional[List[Path]] = None,
    *,
    fallback_principal: str = "unknown",
    ttl_days: Optional[float] = None,
    now: Optional[float] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Quarantine every stale ``_agent_mismatch`` stop-candidate file across
    ``inboxes`` (default: ``discover_inboxes(config)``, same discovery
    ``inbox_ingest.ingest`` uses).

    Returns a summary dict. ``dry_run=True`` reports counts without moving
    anything. Idempotent: a file that is already quarantined has left the
    live inbox, so a re-run naturally finds nothing to do for it.
    """
    if inboxes is None:
        inboxes = discover_inboxes(config)
    ttl = DEFAULT_RESIDUE_TTL_DAYS if ttl_days is None else float(ttl_days)
    ttl_seconds = ttl * 86400.0
    now = time.time() if now is None else now

    would_quarantine = 0
    quarantined_files: List[str] = []
    for inbox in inboxes:
        for path, reason_payload in _stale_agent_mismatch_files(
            inbox, fallback_principal, ttl_seconds, now
        ):
            would_quarantine += 1
            if dry_run:
                continue
            target = quarantine_inbox_file(path, reason_payload)
            if target:
                quarantined_files.append(target)

    return {
        "inboxes": [str(p) for p in inboxes],
        "would_quarantine": would_quarantine,
        "quarantined": len(quarantined_files),
        "quarantined_files": quarantined_files,
        "ttl_days": ttl,
        "dry_run": dry_run,
    }

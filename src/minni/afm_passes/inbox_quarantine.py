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


# M4 (#229): the AFM dead-letter cohort. ``afm_writer._write_batch`` writes
# ``afm-drafts-<date>.json`` and ``afm_passes.pruning._write_inbox`` writes
# ``afm-pruning-<date>.json``. Neither carries a ``kind`` and neither matches
# the stop-candidate shape, so ``_scan_inbox`` buckets both under
# ``skipped_by_kind["_unrecognized"]`` on every tick — and no reader exists
# for either name anywhere in the repo. A pure dead letter: 107 files, the
# oldest 61 days, growing monotonically with nothing to age them and only a
# log line to report them.
#
# Scoped to these two NAMES rather than to ``_unrecognized`` as a class: an
# unrecognized kind is not by itself proof a file is unreadable, and a future
# writer that does have a reader must not be swept up by this drain.
AFM_DEAD_LETTER_PREFIXES = ("afm-drafts-", "afm-pruning-")


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


def _afm_dead_letter_files(inbox: Path, now: float) -> List[Tuple[Path, float, bool]]:
    """``(path, age_seconds, unreadable)`` for live AFM dead-letter files.

    Non-recursive, so already-quarantined and archived files are invisible: a
    drained backlog must report zero rather than stay permanently non-zero.

    Defensive: a file is only claimed while it still looks like the unread
    payload. If one ever grows a ``kind``, or matches the stop-candidate shape
    the ingest pass reads, it has a reader and is left alone.
    """
    out: List[Tuple[Path, float, bool]] = []
    for path in sorted(inbox.glob("*.json")):
        if not path.name.startswith(AFM_DEAD_LETTER_PREFIXES):
            continue
        # Both writers do a non-atomic read-modify-write on the same dated
        # file, so a crash mid-write leaves a truncated or list-shaped
        # payload. Skipping those made them invisible to the count AND
        # undrainable — a permanently stuck file, which is the exact class
        # this drain exists to remove. Claim them as unreadable instead: the
        # NAME already proves which writer produced them, and no reader
        # exists for that name either way.
        unreadable = False
        doc: Any = None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            unreadable = True
        if not unreadable:
            if not isinstance(doc, dict):
                unreadable = True
            elif doc.get("kind") is not None:
                continue
            elif _is_stop_candidate_shape(doc):
                continue
        created = None if unreadable else _file_createdat_epoch(doc)
        if created is None:
            try:
                created = path.stat().st_mtime
            except OSError:
                continue
        out.append((path, max(now - created, 0.0), unreadable))
    return out


def count_afm_dead_letter(
    inboxes: Optional[List[Path]] = None,
    *,
    config: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Depth and oldest age of the AFM dead-letter backlog, without draining.

    Half of M4 was that nothing reported the count, so counting must not
    require moving anything — a health surface reads this on every call.
    """
    if inboxes is None:
        inboxes = discover_inboxes(config)
    now = time.time() if now is None else now
    ages: List[float] = []
    unreadable = 0
    for inbox in inboxes:
        for _path, age, is_unreadable in _afm_dead_letter_files(Path(inbox), now):
            ages.append(age)
            unreadable += 1 if is_unreadable else 0
    return {
        "files": len(ages),
        "oldest_age_days": (max(ages) / 86400.0) if ages else None,
        "unreadable": unreadable,
    }


def quarantine_afm_dead_letter(
    config,
    inboxes: Optional[List[Path]] = None,
    *,
    ttl_days: Optional[float] = None,
    now: Optional[float] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Drain stale AFM dead-letter files into ``quarantine/``.

    Same contract as :func:`quarantine_stale_agent_mismatch`: never unlinks,
    always leaves a reason sidecar, and the TTL is a grace window rather than
    an instant trigger.
    """
    if inboxes is None:
        inboxes = discover_inboxes(config)
    ttl = DEFAULT_RESIDUE_TTL_DAYS if ttl_days is None else float(ttl_days)
    ttl_seconds = ttl * 86400.0
    now = time.time() if now is None else now

    would_quarantine = 0
    quarantined_files: List[str] = []
    for inbox in inboxes:
        for path, age, unreadable in _afm_dead_letter_files(Path(inbox), now):
            if age <= ttl_seconds:
                continue
            would_quarantine += 1
            if dry_run:
                continue
            target = quarantine_inbox_file(path, {
                "reason": (
                    "_afm_dead_letter_unreadable" if unreadable else "_afm_dead_letter"
                ),
                "detail": (
                    "written by afm_writer/afm_passes.pruning; no reader exists "
                    "for this filename, so it can never be ingested"
                ),
                "quarantined_at": _iso(now),
                "age_days": round(age / 86400.0, 6),
                "ttl_days": round(ttl_seconds / 86400.0, 6),
            })
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


def _unusable_compact_files(
    inbox: Path, fallback_principal: str, ttl_seconds: float, now: float,
) -> List[Tuple[Path, Dict[str, Any]]]:
    """``(path, reason_payload)`` for stale unusable compact-summary files.

    #336: these had NO drain. ``quarantine_stale_agent_mismatch`` gates on
    STOP_KINDS, so an explicit ``compact_summary`` kind is excluded, and
    ``quarantine_afm_dead_letter`` is filename-scoped — so such a file stayed
    in the inbox and was re-processed and re-dropped on every tick, ~96
    times/day at the 900s consolidation interval, each with its own warning.

    Classification is delegated to compact_distillation so the drain and the
    health count cannot disagree about which files are unusable.
    """
    from minni.afm_passes.compact_distillation import classify_unusable_compact_file

    out: List[Tuple[Path, Dict[str, Any]]] = []
    principal = _principal_for_inbox(inbox, fallback_principal)
    for path in sorted(inbox.glob("*.json")):
        reason = classify_unusable_compact_file(path, principal)
        if reason is None:
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age <= ttl_seconds:
            continue  # grace window: might still be corrected upstream
        out.append((
            path,
            {
                "reason": reason,
                "resolved_vault_principal": principal,
                "quarantined_at": _iso(now),
                "age_days": round(age / 86400.0, 6),
                "ttl_days": round(ttl_seconds / 86400.0, 6),
            },
        ))
    return out


def quarantine_unusable_compact_summaries(
    config,
    inboxes: Optional[List[Path]] = None,
    *,
    fallback_principal: str = "unknown",
    ttl_days: Optional[float] = None,
    now: Optional[float] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Drain stale unusable compact-summary files into ``quarantine/``.

    Same contract as the two sibling drains: ``os.replace`` only (never an
    unlink), a ``<name>.reason.json`` sidecar naming the cohort, and a TTL
    grace window rather than an instant trigger.
    """
    if inboxes is None:
        inboxes = discover_inboxes(config)
    ttl = DEFAULT_RESIDUE_TTL_DAYS if ttl_days is None else float(ttl_days)
    ttl_seconds = ttl * 86400.0
    now = time.time() if now is None else now

    would_quarantine = 0
    quarantined_files: List[str] = []
    for inbox in inboxes:
        for path, reason_payload in _unusable_compact_files(
            Path(inbox), fallback_principal, ttl_seconds, now,
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

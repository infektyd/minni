"""Inbox file lifecycle: archive-on-resolution for the vault file-inbox channel.

Background
----------
``afm_passes.inbox_ingest`` drains ``<vault>/inbox/*.json`` stop-candidate
files into ``candidate_packets`` but by design never touches the files, and
its idempotency is keyed on ``derived_from`` — so the file channel only ever
GROWS (audit cluster C2: 1,500+ stale files, resolved candidates re-surfacing
in every SessionStart envelope).

This module closes the loop: once EVERY candidate derived from a given inbox
file has reached a terminal DB state (accepted / rejected / redacted / merged
/ superseded / expired — anything but ``proposed``), the source file is moved
to ``<vault>/inbox/.archive/`` with its filename preserved.

Contract
--------
* NEVER hard-deletes. The only file operation is a rename into ``.archive/``.
* Idempotent and best-effort: a missing file, an unparseable ``derived_from``
  or a non-inbox candidate are all quiet no-ops.
* ``.archive/`` is invisible to both ``inbox_ingest`` (non-recursive
  ``inbox.glob('*.json')``) and the hooks' ``readPendingInbox`` (readdir of
  ``inbox/`` only), so archived files stop re-surfacing everywhere.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from afm_passes.inbox_ingest import discover_inboxes

logger = logging.getLogger("minnid")

ARCHIVE_DIRNAME = ".archive"

# Every candidate_packets status except 'proposed' (the schema CHECK set).
TERMINAL_STATUSES = frozenset(
    {"accepted", "rejected", "redacted", "expired", "merged", "superseded"}
)


def archive_inbox_file(file_path: Path) -> Optional[str]:
    """Move an inbox file into its sibling ``.archive/`` dir. NEVER unlinks;
    the filename is preserved (a numeric suffix is added on collision).
    Returns the archived path, or ``None`` if the file was already gone."""
    file_path = Path(file_path)
    if not file_path.is_file():
        return None
    archive_dir = file_path.parent / ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / file_path.name
    suffix = 1
    while target.exists():
        target = archive_dir / f"{file_path.stem}.{suffix}{file_path.suffix}"
        suffix += 1
    os.replace(file_path, target)
    return str(target)


def _derived_inbox_file(derived_from: Any) -> Optional[str]:
    """Inbox source filename recorded in a ``derived_from`` JSON blob, if any."""
    if not isinstance(derived_from, str) or not derived_from:
        return None
    try:
        obj = json.loads(derived_from)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("source") != "inbox":
        return None
    name = obj.get("inbox_file")
    return name if isinstance(name, str) and name else None


def _statuses_for_inbox_file(db, inbox_file: str) -> List[str]:
    """Statuses of every candidate derived from ``inbox_file``. The LIKE is a
    cheap pre-filter; rows are confirmed by parsing ``derived_from``."""
    like = f'%"inbox_file": "{inbox_file}"%'
    with db.cursor() as c:
        c.execute(
            "SELECT status, derived_from FROM candidate_packets WHERE derived_from LIKE ?",
            (like,),
        )
        rows = c.fetchall()
    return [
        row["status"]
        for row in rows
        if _derived_inbox_file(row["derived_from"]) == inbox_file
    ]


def maybe_archive_for_candidate(db, config, candidate_id: int) -> Optional[str]:
    """B1 drain-on-resolution: if ``candidate_id`` was sourced from an inbox
    file AND every candidate derived from that same file is now terminal, move
    the file to ``<inbox>/.archive/``. Returns the archived path or ``None``
    when nothing was archived (non-inbox candidate, siblings still proposed,
    file already gone)."""
    with db.cursor() as c:
        c.execute(
            "SELECT derived_from FROM candidate_packets WHERE candidate_id=?",
            (int(candidate_id),),
        )
        row = c.fetchone()
    if not row:
        return None
    inbox_file = _derived_inbox_file(row["derived_from"])
    if not inbox_file:
        return None
    statuses = _statuses_for_inbox_file(db, inbox_file)
    if not statuses or any(s not in TERMINAL_STATUSES for s in statuses):
        return None
    # derived_from records only the filename; find it across the known inboxes
    # (mirrors inbox_ingest's name-keyed idempotency semantics).
    for inbox in discover_inboxes(config):
        source = inbox / inbox_file
        if source.is_file():
            archived = archive_inbox_file(source)
            if archived:
                logger.info(
                    "inbox archive: %s -> %s (all derived candidates terminal)",
                    source, archived,
                )
                return archived
    return None

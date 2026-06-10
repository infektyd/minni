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

from afm_passes.inbox_ingest import (
    CONTENT_CAP,
    STOP_KINDS,
    _as_str_set,
    _content_sha1,
    _is_stop_candidate_shape,
    discover_inboxes,
)

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
    # Containment (defense-in-depth behind _derived_inbox_file's basename
    # guard): the rename target must stay inside the sibling .archive dir —
    # never let a crafted name move a file anywhere else.
    try:
        if target.resolve().parent != archive_dir.resolve():
            logger.warning("inbox archive: refusing non-contained target %s", target)
            return None
    except OSError:
        return None
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
    if not isinstance(name, str) or not name:
        return None
    # Path-traversal guard: derived_from is client-controllable (any UDS caller
    # can stage a candidate with an arbitrary blob, and resolve is permissive),
    # while the legitimate writer (inbox_ingest) only ever stores `path.name`.
    # Reject anything that is not a pure basename.
    if (
        name in {".", ".."}
        or name != os.path.basename(name)
        or "/" in name
        or os.sep in name
        or (os.altsep and os.altsep in name)
    ):
        logger.warning("inbox archive: rejecting non-basename inbox_file %r", name)
        return None
    return name


def _rows_for_inbox_file(db, inbox_file: str) -> List[dict]:
    """Every candidate row derived from ``inbox_file`` as
    ``{status, candidate_index, content_sha1, content}``. The LIKE is a cheap
    pre-filter; rows are confirmed by parsing ``derived_from``."""
    like = f'%"inbox_file": "{inbox_file}"%'
    with db.cursor() as c:
        c.execute(
            "SELECT status, content, derived_from FROM candidate_packets "
            "WHERE derived_from LIKE ?",
            (like,),
        )
        rows = c.fetchall()
    out: List[dict] = []
    for row in rows:
        if _derived_inbox_file(row["derived_from"]) != inbox_file:
            continue
        try:
            df = json.loads(row["derived_from"])
        except Exception:
            df = {}
        out.append(
            {
                "status": row["status"],
                "candidate_index": df.get("candidate_index"),
                "content_sha1": df.get("content_sha1"),
                "content": row["content"],
            }
        )
    return out


def _eligible_candidates(doc: Any) -> Optional[dict]:
    """``{index: capped_content}`` for every candidate inbox_ingest would take
    from ``doc``, or ``None`` when the doc is not a stop-candidate file at all
    (kind gate). Mirrors ``inbox_ingest._scan_inbox``."""
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    if kind not in STOP_KINDS and not (kind is None and _is_stop_candidate_shape(doc)):
        return None
    if doc.get("log_only") is True or doc.get("do_not_store") is True:
        return {}
    log_only = _as_str_set(doc.get("log_only"))
    dns = _as_str_set(doc.get("do_not_store"))
    cands = doc.get("candidates") or []
    if not isinstance(cands, list):
        return {}
    out: dict = {}
    for idx, cand in enumerate(cands):
        if not isinstance(cand, str) or not cand.strip():
            continue
        if cand in log_only or cand in dns:
            continue
        out[idx] = cand.strip()[:CONTENT_CAP]
    return out


def _matching_rows_for_file(doc: Any, rows: List[dict]) -> Optional[List[dict]]:
    """Rows that genuinely correspond to ``doc``'s candidates, or ``None``
    when the file is not archivable through this path.

    ``derived_from`` is client-controllable (any local UDS caller can stage a
    candidate naming an arbitrary ``inbox_file``), and it records only a bare
    filename — without this check a single forged terminal row could archive
    ANY agent's live, never-ingested inbox file (cross-vault name match). So a
    row only counts when it carries ingest-written provenance for THIS file's
    content: its ``candidate_index`` addresses a real eligible candidate and
    its ``content_sha1`` (or, for legacy rows without one, the row's stored
    content) matches that candidate's text. The file is archivable only when
    every eligible candidate is covered by a matching row — i.e. the DB
    provably carries all of the file's content. Non-stop-candidate files
    (handoffs, *_precompact_handoff, ...) are never archived here; they drain
    through their own TTL/ack channels."""
    eligible = _eligible_candidates(doc)
    if not eligible:
        return None  # not a stop-candidate file, or nothing ingestible in it
    matched: List[dict] = []
    covered: set = set()
    for row in rows:
        idx = row.get("candidate_index")
        if not isinstance(idx, int) or idx not in eligible:
            continue
        content = eligible[idx]
        sha = row.get("content_sha1")
        if isinstance(sha, str) and sha:
            if sha != _content_sha1(content):
                continue
        elif row.get("content") != content:
            continue
        matched.append(row)
        covered.add(idx)
    if covered != set(eligible):
        return None  # some eligible candidate has no genuine DB row -> keep
    return matched


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
    rows = _rows_for_inbox_file(db, inbox_file)
    if not rows:
        return None
    # derived_from records only the filename; find it across the known inboxes
    # (mirrors inbox_ingest's name-keyed idempotency semantics) — but archive a
    # file ONLY when the rows provably correspond to ITS content and every
    # matching row is terminal. A same-named file in another vault (or a
    # never-ingested file targeted by a forged derived_from) fails the
    # correspondence check and stays live.
    for inbox in discover_inboxes(config):
        source = inbox / inbox_file
        try:
            # Belt-and-braces containment: the joined path must stay inside
            # this inbox dir (basename guard above already rejects traversal).
            if not source.resolve().is_relative_to(inbox.resolve()):
                continue
        except OSError:
            continue
        if not source.is_file():
            continue
        try:
            doc = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            continue
        matched = _matching_rows_for_file(doc, rows)
        if matched is None:
            continue
        if any(r["status"] not in TERMINAL_STATUSES for r in matched):
            continue  # a sibling candidate from THIS copy is still live; other
            # inboxes may hold a same-named file whose rows are all terminal
        archived = archive_inbox_file(source)
        if archived:
            logger.info(
                "inbox archive: %s -> %s (all derived candidates terminal)",
                source, archived,
            )
            return archived
    return None

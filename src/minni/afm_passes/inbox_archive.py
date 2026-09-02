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
from typing import Any, Dict, List, Optional

from minni.afm_passes.inbox_ingest import (
    CONTENT_CAP,
    STOP_KINDS,
    _as_str_set,
    _canonical_principal,
    _content_sha1,
    _is_stop_candidate_shape,
    _principal_family,
    _principal_for_inbox,
    discover_inboxes,
    is_audit_echo,
)

logger = logging.getLogger("minnid")

ARCHIVE_DIRNAME = ".archive"


def _inbox_vault_slug(inbox: Path) -> str:
    """Raw ``<slug>`` from ``<slug>-vault/inbox``; empty for the daemon vault."""
    parent = Path(inbox).parent.name
    if parent.endswith("-vault"):
        return parent[: -len("-vault")]
    return ""


def _source_principal_for_archive(row) -> str:
    """Leftover vault slug if collapse rewrote ``principal`` to canonical.

    ``repair_duplicate_candidate_pairs`` stamps ``source_principal`` into
    ``derived_from`` before UPDATE agy/xai → gemini/grok-build. Without
    that, ``owner_is_alias`` is false and discover order can archive the
    remapped vault's never-ingested live file.
    """
    owner = str(row["principal"] or "")
    raw = row["derived_from"] if "derived_from" in row.keys() else None
    if not isinstance(raw, str) or not raw:
        return owner
    try:
        df = json.loads(raw)
    except Exception:
        return owner
    if not isinstance(df, dict):
        return owner
    stamped = df.get("source_principal")
    if isinstance(stamped, str) and stamped:
        return stamped
    return owner

# Every candidate_packets status except 'proposed' (the schema CHECK set,
# including the do_not_store/log_only statuses added by migration 015).
TERMINAL_STATUSES = frozenset(
    {
        "accepted",
        "rejected",
        "redacted",
        "expired",
        "merged",
        "superseded",
        "do_not_store",
        "log_only",
    }
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


def _rows_for_inbox_file(
    db, inbox_file: str, *, principal: Optional[str] = None
) -> List[dict]:
    """Candidate rows derived from ``inbox_file`` as
    ``{status, candidate_index, content_sha1, content, principal}``.

    The LIKE is a cheap pre-filter; rows are confirmed by parsing
    ``derived_from``. When ``principal`` is set, only that agent's rows
    count — principal-scoped ingest can create same-basename peers in other
    vaults; archive must not treat those as open siblings of this inbox.

    Alias family is expanded so leftover ``agy``/``xai`` rows still count as
    siblings of remapped ``gemini``/``grok-build`` fills (and vice versa).
    Exact ``principal = owner`` hid those twins after alias collapse.
    """
    like = f'%"inbox_file": "{inbox_file}"%'
    with db.cursor() as c:
        if principal is not None:
            family = _principal_family(principal)
            placeholders = ",".join("?" for _ in family)
            c.execute(
                "SELECT principal, status, content, derived_from "
                "FROM candidate_packets "
                f"WHERE COALESCE(principal, '') IN ({placeholders}) "
                "AND derived_from LIKE ?",
                (*family, like),
            )
        else:
            c.execute(
                "SELECT principal, status, content, derived_from "
                "FROM candidate_packets WHERE derived_from LIKE ?",
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
                "principal": row["principal"] if "principal" in row.keys() else None,
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
    content: its ``content_sha1`` (or, for legacy rows without one, the row's
    stored content) matches an eligible candidate's text. extras-at-next-idx
    remaps ``derived_from.candidate_index`` off the file slot, so coverage is
    by sibling content, not by enumerate key. The file is archivable only when
    every eligible candidate is covered by a matching row — i.e. the DB
    provably carries all of the file's content. Non-stop-candidate files
    (handoffs, *_precompact_handoff, ...) are never archived here; they drain
    through their own TTL/ack channels."""
    eligible = _eligible_candidates(doc)
    if not eligible:
        return None  # not a stop-candidate file, or nothing ingestible in it
    matched: List[dict] = []
    covered: set = set()
    seen: set = set()
    for idx, content in eligible.items():
        want_sha = _content_sha1(content)
        for i, row in enumerate(rows):
            sha = row.get("content_sha1")
            if isinstance(sha, str) and sha:
                if sha != want_sha:
                    continue
            elif row.get("content") != content:
                continue
            # Sibling matches this file's eligible bytes (index may have
            # remapped via extras-at-next-idx). Forged rows with a different
            # body still fail the sha/body guard above.
            covered.add(idx)
            if i not in seen:
                matched.append(row)
                seen.add(i)
            break
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
            "SELECT principal, derived_from FROM candidate_packets "
            "WHERE candidate_id=?",
            (int(candidate_id),),
        )
        row = c.fetchone()
    if not row:
        return None
    inbox_file = _derived_inbox_file(row["derived_from"])
    if not inbox_file:
        return None
    # Scope siblings to the resolved candidate's principal so multi-vault
    # same-basename peers (allowed by principal-scoped ingest) do not block
    # archive of a fully-terminal agent vault copy. Compare the alias family
    # (agy↔gemini, xai↔grok-build): leftover rows keep the raw principal while
    # ``_principal_for_inbox`` returns the canonical vault owner.
    owner_principal = str(row["principal"] or "")
    rows = _rows_for_inbox_file(db, inbox_file, principal=owner_principal)
    if not rows:
        return None
    # Only consider inboxes owned by the resolved candidate's principal.
    # Content-matching alone is insufficient: byte-identical candidates under
    # the same basename in another vault would otherwise archive the wrong
    # agent's still-live file when discover order visits them first.
    owner_canon = _canonical_principal(owner_principal)
    # Leftover alias packets (agy/xai) share a canonical owner with the
    # remapped vault (gemini/grok-build). Do not archive that remapped
    # vault's live file — it was never the leftover's source. Collapse
    # rewrite sets principal to canonical, so owner_is_alias on the
    # rewritten column is false; consult source_principal instead.
    source_principal = _source_principal_for_archive(row)
    source_is_alias = bool(source_principal) and source_principal != _canonical_principal(
        source_principal
    )

    for inbox in discover_inboxes(config):
        inbox_owner = _principal_for_inbox(inbox, fallback_principal="unknown")
        if _canonical_principal(inbox_owner or "") != owner_canon:
            continue
        if source_is_alias and _inbox_vault_slug(inbox) != source_principal:
            continue
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
            continue  # a sibling candidate from THIS principal/copy is still live
        archived = archive_inbox_file(source)
        if archived:
            logger.info(
                "inbox archive: %s -> %s (all derived candidates terminal)",
                source, archived,
            )
            return archived
    return None


# --- Inert-file sweep (2026-08 inbox pile-up) ---------------------------------
#
# Archive-on-resolution above covers files whose candidates reached the DB. A
# second cohort never gets there: stop-candidate files whose EVERY candidate is
# rejected at ingest itself (audit echo per issue #193, log_only/do_not_store
# listing, or blank). ingest's verdict is a pure function of the write-once
# file content, so it repeats identically on every tick forever — the file can
# never earn a DB row, never resolve, and never archive. 107 such files (the
# 2026-08-01 claudecode/grok-build pile) accumulated exactly this way, each
# re-surfacing in SessionStart's pending-inbox count until an operator "triaged"
# what was Minni's own telemetry all along.
#
# The rejection IS these files' terminal resolution, so they take the same exit
# as resolved files: a rename into `.archive/` (never an unlink). Files that
# are some OTHER cohort's problem are left strictly alone: `_agent_mismatch`
# drains through inbox_quarantine, non-stop kinds through their own channels,
# and any file with at least one ingestible candidate stays live for the
# ingest/resolution path.


def _inert_reason(doc: Any, inbox_principal: str) -> Optional[Dict[str, Any]]:
    """Why ``doc`` can never produce a candidate row, or ``None`` when it can.

    Mirrors ``inbox_ingest._scan_inbox``'s gates in the same order, so "inert"
    is provably "what ingest skips forever", not an independent opinion."""
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    if not isinstance(kind, str) and kind is not None:
        return None  # _malformed_kind: different bug, not drained here
    if kind not in STOP_KINDS and not (kind is None and _is_stop_candidate_shape(doc)):
        return None  # handoffs / other kinds drain through their own channels
    if doc.get("log_only") is True or doc.get("do_not_store") is True:
        return {"reason": "log_only_or_do_not_store_flag"}
    file_agent = str(doc.get("agent_id") or "").strip()
    if file_agent and _canonical_principal(file_agent) != inbox_principal:
        return None  # _agent_mismatch: inbox_quarantine's cohort
    cands = doc.get("candidates") or []
    if not isinstance(cands, list):
        return {"reason": "malformed_candidates"}
    log_only = _as_str_set(doc.get("log_only"))
    dns = _as_str_set(doc.get("do_not_store"))
    echo = listed = blank = 0
    for cand in cands:
        if not isinstance(cand, str) or not cand.strip():
            blank += 1
            continue
        if cand in log_only or cand in dns:
            listed += 1
            continue
        if is_audit_echo(cand):
            echo += 1
            continue
        return None  # at least one ingestible candidate: file stays live
    return {
        "reason": "no_ingestible_candidates",
        "candidates": len(cands),
        "audit_echo": echo,
        "log_only_or_do_not_store": listed,
        "blank": blank,
    }


def archive_inert_files(
    config,
    inboxes: Optional[List[Path]] = None,
    *,
    fallback_principal: str = "unknown",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Archive every inert stop-candidate file across ``inboxes`` (default:
    ``discover_inboxes(config)``, the same discovery ingest uses).

    Returns a summary dict. ``dry_run=True`` reports without moving anything.
    Idempotent: an archived file has left the live inbox, so a re-run finds
    nothing to do for it. NEVER unlinks — ``archive_inbox_file`` only renames
    into the sibling ``.archive/``."""
    if inboxes is None:
        inboxes = discover_inboxes(config)

    would_archive = 0
    archived_files: List[str] = []
    reasons: Dict[str, int] = {}
    for inbox in inboxes:
        inbox_principal = _principal_for_inbox(Path(inbox), fallback_principal)
        for path in sorted(Path(inbox).glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue  # unparseable: different bug, not drained here
            reason = _inert_reason(doc, inbox_principal)
            if reason is None:
                continue
            would_archive += 1
            label = str(reason.get("reason"))
            reasons[label] = reasons.get(label, 0) + 1
            if dry_run:
                continue
            target = archive_inbox_file(path)
            if target:
                archived_files.append(target)
    if archived_files:
        logger.info(
            "inbox archive: %d inert file(s) -> .archive/ (%s)",
            len(archived_files), reasons,
        )
    return {
        "inboxes": [str(p) for p in inboxes],
        "would_archive": would_archive,
        "archived": len(archived_files),
        "archived_files": archived_files,
        "reasons": reasons,
    }

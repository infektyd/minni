"""Inbox -> candidate_packets ingestion for the AFM loop.

Background
----------
Two proposal channels exist; historically only one fed the AFM consolidation
loop:

  (1) ``minni_learn`` -> INSERT INTO candidate_packets (status='proposed')
      -> the loop drains these -> durable ``learnings``. WORKS.

  (2) Stop/PreCompact hooks -> ``<vault>/inbox/*.json`` (kind
      'stop_candidates'; legacy 'codex_stop_candidates'; or kind-less with
      the stop-candidate shape). These were NEVER ingested into
      candidate_packets, so the loop never saw them and they piled up.

This module drains channel (2) into candidate_packets using the SAME canonical
insert shape the daemon uses (see ``_stage_candidate`` in minnid.py), so the
rows are picked up by the consolidation pass (which selects purely on
``status='proposed'`` ordered by ``proposed_at ASC`` — no principal filter).

It is invoked by the AFM loop at the start of each ``consolidation`` tick (see
``_afm_loop_runner`` in minnid.py) so the inbox channel stops piling up.

Safety / contract
-----------------
* Files are processed if ``kind`` is ``'stop_candidates'`` (or the legacy
  ``'codex_stop_candidates'``) OR the file is
  kind-less but matches the stop-candidate shape (a ``candidates`` list plus
  ``slug``/``last_task`` — the shape the Claude Code Stop/PreCompact hook emits
  with no ``kind`` field). Files carrying any OTHER explicit kind (handoffs,
  ``*_precompact_handoff``, ``failed_command``, ...) are NOT processed: they
  always have a ``kind`` and so never reach the kind-less branch. They ARE
  counted: ``ingest()`` reports ``skipped_by_kind`` so the gate is observable
  instead of silently dropping whole channels (B4 / audit C2).
* Candidates are attributed to the agent that owns the vault (derived from the
  ``<agent>-vault`` dir name, e.g. ``claudecode-vault`` -> ``claudecode``) when
  the file carries no explicit ``agent_id``, instead of a global fallback.
* A FILE is skipped if ``log_only`` or ``do_not_store`` is boolean ``True``
  (defensive; current files carry these as advisory string LISTS).
* A CANDIDATE string is skipped if it appears verbatim in that file's
  ``log_only`` or ``do_not_store`` list.
* IDEMPOTENT: each row carries ``derived_from`` with the source inbox file +
  candidate index; existing rows (ANY status) are detected and never
  re-inserted. Re-running is a no-op even after the loop resolves a row.
* ``derived_from.kind`` records what the source file actually declared
  (``null`` for the kind-less Claude Code shape) — Minni logic never stamps
  one agent's label onto another agent's rows.
* NEVER deletes inbox files or candidate rows. Disposal is handled separately
  by ``afm_passes.inbox_archive`` (archive-on-resolution; rename into
  ``inbox/.archive/``, never unlink).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Canonical, agent-neutral format tag for stop-candidate inbox files. The
# legacy codex-prefixed tag is still accepted for files written before the
# hooks were neutralized. `kind` identifies the FILE FORMAT, never the author —
# author identity travels via agent_id / the owning vault dir / `principal`.
STOP_KIND = "stop_candidates"
LEGACY_STOP_KIND = "codex_stop_candidates"
STOP_KINDS = frozenset({STOP_KIND, LEGACY_STOP_KIND})
CONTENT_CAP = 200000  # matches minnid.py canonical insert bound


def _content_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _as_str_set(v: Any) -> set:
    return {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()


def _file_createdat_epoch(doc: Dict[str, Any]) -> Optional[float]:
    raw = doc.get("createdAt")
    if not isinstance(raw, str):
        return None
    try:
        from datetime import datetime, timezone

        return (
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except Exception:
        return None


def discover_inboxes(config) -> List[Path]:
    """Enumerate inbox dirs to drain: every ``<home>/*-vault/inbox`` plus the
    daemon's own ``vault/inbox``. Deduped, only existing dirs returned."""
    home = getattr(config, "CANONICAL_SOVEREIGN_HOME", None)
    if not home:
        # fall back: parent of the configured vault_path
        home = str(Path(getattr(config, "vault_path", "~/.minni/vault")).expanduser().parent)
    home = os.path.expanduser(home)

    candidates = list(glob.glob(os.path.join(home, "*-vault", "inbox")))
    own = Path(getattr(config, "vault_path", os.path.join(home, "vault"))).expanduser() / "inbox"
    candidates.append(str(own))

    seen: set = set()
    out: List[Path] = []
    for c in candidates:
        p = Path(c).resolve()
        if p in seen:
            continue
        seen.add(p)
        if p.is_dir():
            out.append(p)
    return out


def _existing_keys(db, principals: set) -> set:
    """(inbox_file, candidate_index) pairs already present for the given
    principals. Matches on derived_from regardless of status so re-runs are
    no-ops even after the loop resolves a previously-ingested row."""
    keys: set = set()
    with db.cursor() as c:
        if principals:
            qmarks = ",".join("?" for _ in principals)
            c.execute(
                f"SELECT derived_from FROM candidate_packets WHERE principal IN ({qmarks})",
                tuple(principals),
            )
        else:
            c.execute("SELECT derived_from FROM candidate_packets")
        rows = c.fetchall()
    for row in rows:
        df = row["derived_from"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
        if not df:
            continue
        try:
            obj = json.loads(df)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("source") != "inbox":
            continue
        f, i = obj.get("inbox_file"), obj.get("candidate_index")
        if isinstance(f, str) and isinstance(i, int):
            keys.add((f, i))
    return keys


def _is_stop_candidate_shape(doc: Dict[str, Any]) -> bool:
    """True for kind-less inbox files matching the stop-candidate shape the
    Claude Code hook emits: a ``candidates`` list plus the ``slug``/``last_task``
    session markers. Deliberately strict so arbitrary kind-less JSON is not
    ingested. Handoff/precompact files always carry a ``kind`` and are excluded
    upstream by the ``kind not in STOP_KINDS`` check before this is consulted."""
    return (
        isinstance(doc.get("candidates"), list)
        and "slug" in doc
        and "last_task" in doc
    )


# Maps vault slug (dir name without -vault suffix) -> canonical agent_id
# Derived from _default_agent_vault aliases in minnid.py (source of truth).
# claude-code is preferred over claudecode because it is the canonical id.
_VAULT_SLUG_TO_AGENT_ID: dict[str, str] = {
    "claudecode": "claude-code",
    "codex": "codex",
    "gemini": "gemini",
    "hermes": "hermes",
    "kilocode": "kilocode",
    "openclaw": "openclaw",
    "grok-build": "grok-build",
    "grok-beta": "grok-beta",
    "grok": "grok",
}


def _principal_for_inbox(inbox: Path, fallback_principal: str) -> str:
    """Derive the owning agent from the vault dir name (``<agent>-vault/inbox``)
    so kind-less candidates are attributed to the right agent (e.g.
    ``claudecode-vault`` -> ``claude-code``) instead of the global fallback. The
    daemon's own bare ``vault/inbox`` (no ``-vault`` suffix) uses the fallback."""
    parent = inbox.parent.name
    if parent.endswith("-vault"):
        slug = parent[: -len("-vault")]
        return _VAULT_SLUG_TO_AGENT_ID.get(slug, slug) or fallback_principal
    return fallback_principal


def _scan_inbox(
    inbox: Path, fallback_principal: str,
) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Return (candidate dicts, skipped-by-kind counts) for a single inbox dir
    (no DB, no dedup yet). Skipped counts make the kind gate observable: files
    dropped because they carry a non-stop ``kind`` (handoff, failed_command,
    ``*_precompact_handoff``, ...) are tallied per kind instead of vanishing
    silently; kind-less files that fail the stop-candidate shape check are
    tallied under ``_unrecognized``."""
    out: List[Dict[str, Any]] = []
    skipped_by_kind: Dict[str, int] = {}
    inbox_principal = _principal_for_inbox(inbox, fallback_principal)
    for path in sorted(inbox.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        # Accept codex's explicitly-tagged stop-candidates AND kind-less files
        # that carry the stop-candidate shape (the Claude Code hook writes
        # candidate files with no `kind`). Any other explicit kind is excluded
        # — but counted, so the gate is observable (B4 / audit C2).
        if kind not in STOP_KINDS and not (kind is None and _is_stop_candidate_shape(doc)):
            label = kind if isinstance(kind, str) and kind else "_unrecognized"
            skipped_by_kind[label] = skipped_by_kind.get(label, 0) + 1
            continue
        if doc.get("log_only") is True or doc.get("do_not_store") is True:
            continue

        log_only_set = _as_str_set(doc.get("log_only"))
        dns_set = _as_str_set(doc.get("do_not_store"))
        ws = doc.get("workspace_id") or "default"
        principal = doc.get("agent_id") or inbox_principal
        created = _file_createdat_epoch(doc)
        proposed_at = created if created is not None else time.time()

        cands = doc.get("candidates") or []
        if not isinstance(cands, list):
            continue
        for idx, cand in enumerate(cands):
            if not isinstance(cand, str) or not cand.strip():
                continue
            if cand in log_only_set or cand in dns_set:
                continue
            content = cand.strip()[:CONTENT_CAP]
            out.append(
                {
                    "principal": principal,
                    "workspace_id": ws,
                    "content": content,
                    "inbox_file": path.name,
                    "candidate_index": idx,
                    "proposed_at": proposed_at,
                    # Record what the file actually declared; null for the
                    # kind-less Claude Code shape. Never stamp an agent-specific
                    # label onto another agent's rows.
                    "kind": kind,
                }
            )
    return out, skipped_by_kind


def ingest(db, config, inboxes: Optional[List[Path]] = None,
           fallback_principal: str = "unknown", dry_run: bool = False) -> Dict[str, Any]:
    """Ingest eligible inbox stop-candidates into candidate_packets.

    Returns a summary dict. Idempotent; respects log_only/do_not_store; never
    deletes. ``dry_run=True`` reports counts without writing.
    """
    if inboxes is None:
        inboxes = discover_inboxes(config)

    scanned: List[Dict[str, Any]] = []
    skipped_by_kind: Dict[str, int] = {}
    for inbox in inboxes:
        rows, skipped = _scan_inbox(inbox, fallback_principal)
        scanned.extend(rows)
        for label, count in skipped.items():
            skipped_by_kind[label] = skipped_by_kind.get(label, 0) + count

    principals = {r["principal"] for r in scanned}
    existing = _existing_keys(db, principals)

    to_insert: List[Dict[str, Any]] = []
    already = 0
    for r in scanned:
        key = (r["inbox_file"], r["candidate_index"])
        if key in existing:
            already += 1
            continue
        existing.add(key)  # guard within-run dup
        to_insert.append(r)

    inserted = 0
    if not dry_run and to_insert:
        with db.transaction() as c:
            for r in to_insert:
                derived_from = json.dumps(
                    {
                        "source": "inbox",
                        "inbox_file": r["inbox_file"],
                        "candidate_index": r["candidate_index"],
                        "kind": r.get("kind"),
                        "content_sha1": _content_sha1(r["content"]),
                    }
                )
                c.execute(
                    """
                    INSERT INTO candidate_packets
                    (principal, workspace_id, layer, privacy_level, content,
                     evidence_refs, derived_from, instruction_like, status, proposed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)
                    """,
                    (
                        r["principal"],
                        r["workspace_id"],
                        None,
                        None,
                        r["content"],
                        json.dumps([]),
                        derived_from,
                        0,
                        r["proposed_at"],
                    ),
                )
                inserted += 1

    return {
        "inboxes": [str(p) for p in inboxes],
        "eligible": len(scanned),
        "already_present": already,
        "would_insert": len(to_insert),
        "inserted": inserted,
        "skipped_by_kind": skipped_by_kind,
        "dry_run": dry_run,
    }

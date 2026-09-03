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
* A CANDIDATE string is skipped if it is an AUDIT ECHO — Minni's own audit log
  fed back in as a "learning" (issue #193). The Stop hook on current main no
  longer drafts from the audit tail and scrubs telemetry client-side
  (``isAuditTelemetryLine`` in plugins/minni/src/task.ts), but hook binaries are
  deployed per-agent and go stale independently, so pre-fix builds keep writing
  audit-echo candidate files into vault inboxes. This writer is the ONE shared
  choke point every inbox file passes through, so the same grammar is enforced
  here as defense in depth. Echoes are tallied as ``_audit_echo`` in
  ``skipped_by_kind`` so the drop is observable rather than silent.
* IDEMPOTENT: each row carries ``derived_from`` with the source inbox file +
  candidate index; existing rows (ANY status) whose body matches (content
  sha1) are detected and never re-inserted. A leftover occupying the same
  (canonical principal, inbox_file, candidate_index) with a *divergent*
  body extras-at-next-idx so the remapped stop-candidate still lands.
  Re-running is a no-op even after the loop resolves a row.
* ``derived_from.kind`` records what the source file actually declared
  (``null`` for the kind-less Claude Code shape) — Minni logic never stamps
  one agent's label onto another agent's rows.
* PRIVACY IS EXPLICIT AT THIS WRITER. Commit 53da3bd fixed I1/I2 by making
  unset/NULL privacy unsafe at consolidation. Stop-candidate inbox files are
  agent-drafted summaries of the agent's own session and still pass the
  downstream instruction-like, length, dedup, and quality gates, so this
  writer stamps missing file-level privacy as ``safe``. A file's explicit
  ``privacy_level`` is propagated verbatim and is never upgraded here. This is
  a source policy decision, not a gate default from NULL to safe.
* NEVER deletes inbox files or candidate rows. Disposal is handled separately
  by ``afm_passes.inbox_archive`` (archive-on-resolution; rename into
  ``inbox/.archive/``, never unlink).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from minni.safety import is_instruction_like

# Canonical, agent-neutral format tag for stop-candidate inbox files. The
# legacy codex-prefixed tag is still accepted for files written before the
# hooks were neutralized. `kind` identifies the FILE FORMAT, never the author —
# author identity travels via agent_id / the owning vault dir / `principal`.
STOP_KIND = "stop_candidates"
LEGACY_STOP_KIND = "codex_stop_candidates"
STOP_KINDS = frozenset({STOP_KIND, LEGACY_STOP_KIND})
CONTENT_CAP = 200000  # matches minnid.py canonical insert bound

# --- Audit-echo grammar (issue #193) ------------------------------------------
# A verbatim port of `isAuditTelemetryLine` in plugins/minni/src/task.ts. Keep
# the two in sync: this is the daemon-side backstop for the SAME shape, and a
# grammar that drifts here silently re-opens the feedback loop for every agent
# whose hook binary is older than the client-side fix.
#
# Two forms, matching how `recordAudit` renders a line and how a tail gets
# pasted back in:
#   * FULL header — `## [<ts>] <tool> |`. Specific enough to match ANYWHERE in
#     the blob, because the Stop drafter collapses newlines into spaces before
#     the scrub, so a pasted tail no longer begins at a line start.
#   * BARE tail line — `<tool> | <summary>`, which MUST be anchored to a line
#     start (with an optional quote/bullet prefix, since a pasted tail usually
#     arrives inside one). Two independent signals keep ordinary prose and
#     markdown tables out: the head must name a namespace that actually emits
#     audit lines, and the tail must hold exactly one `|` and not open with
#     another snake_case identifier ("agent_id | role | created_at ...").
_AUDIT_TOOL = r"(?:hook|minni|sovereign|agent|afm|handoff|team)_[a-z0-9_]+"
_AUDIT_TOOL_KNOWN = (
    r"(?:(?:hook|minni|sovereign)_[a-z0-9_]+"
    r"|afm_loop|agent_ping|handoff_sent|handoff_received)"
)
_AUDIT_LINE_PREFIX = r"[ \t]*(?:[>*\-+][ \t]*)*"

_AUDIT_HEADER_LINE = re.compile(
    r"##[ \t]+\[[^\]\n]{4,64}\][ \t]+" + _AUDIT_TOOL + r"[ \t]*\|",
    re.IGNORECASE,
)
_AUDIT_BARE_LINE = re.compile(
    r"^"
    + _AUDIT_LINE_PREFIX
    + _AUDIT_TOOL_KNOWN
    + r"[ \t]*\|(?![ \t]*[a-z0-9]+_[a-z0-9]+)[^|\n]*$",
    re.IGNORECASE | re.MULTILINE,
)


def is_audit_echo(text: str) -> bool:
    """True when ``text`` is Minni's own audit telemetry rather than a learning.

    Mirrors `isAuditTelemetryLine` (plugins/minni/src/task.ts).
    """
    if not isinstance(text, str):
        return False
    return bool(_AUDIT_HEADER_LINE.search(text) or _AUDIT_BARE_LINE.search(text))


def _content_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _is_unique_integrity_error(exc: BaseException) -> bool:
    """True only for UNIQUE / unique-index IntegrityError (idempotent hits).

    Other integrity failures (CHECK, NOT NULL, FK) must not be counted as
    ``already_present`` and silently dropped.
    """
    if not isinstance(exc, sqlite3.IntegrityError):
        return False
    msg = " ".join(str(a) for a in exc.args).lower()
    return "unique" in msg


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


def _coerce_candidate_index(raw: Any) -> Optional[int]:
    """Normalize JSON candidate_index to int (rejects bools / non-integral floats)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    idx = int(raw)
    if idx != raw:
        return None
    return idx


def _parse_file_index_from_derived(df: Any) -> Optional[Tuple[str, int]]:
    """Parse (inbox_file, candidate_index) from a derived_from JSON blob."""
    if not df:
        return None
    try:
        obj = json.loads(df) if isinstance(df, str) else df
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("source") != "inbox":
        return None
    f = obj.get("inbox_file")
    i = _coerce_candidate_index(obj.get("candidate_index"))
    if isinstance(f, str) and i is not None:
        return (f, i)
    return None


# Back-compat alias (tests / older call sites).
_parse_inbox_key_from_derived = _parse_file_index_from_derived


def _make_inbox_key(
    principal: Any, inbox_file: str, candidate_index: int
) -> Tuple[str, str, int]:
    """App-level inbox idempotency key: principal-scoped file + index.

    Issue #239 duals were double-ingest of the **same** agent vault. Scoping
    by ``principal`` (derived from ``<agent>-vault/inbox``) prevents those
    twins without blocking legitimate same-basename files in other vaults
    (see ``test_cross_vault_live_sibling_does_not_block_other_vaults_copy``).

    Vault-slug aliases (``agy``/``antigravity`` → ``gemini``, ``xai`` →
    ``grok-build``) collapse to the canonical id so a remap is a merge of
    the old row, not a new UNIQUE key.
    """
    return (_canonical_principal(principal), inbox_file, int(candidate_index))


def _parse_inbox_key(
    principal: Any, df: Any
) -> Optional[Tuple[str, str, int]]:
    """Full app key (principal, inbox_file, candidate_index) from a row."""
    fi = _parse_file_index_from_derived(df)
    if fi is None:
        return None
    return _make_inbox_key(principal, fi[0], fi[1])


def _existing_keys(db, principals: set | None = None) -> set:
    """(principal, inbox_file, candidate_index) already in candidate_packets.

    Principal-scoped: same basename in two agent vaults may both insert.
    Status-agnostic so re-runs are no-ops after resolution.

    ``principals`` optionally restricts the scan (compact_distillation per-inbox).
    Alias family is expanded so leftover ``agy``/``xai`` rows still block a
    remapped ``gemini``/``grok-build`` insert of the same inbox fill.
    """
    keys: set = set()
    with db.cursor() as c:
        if principals:
            expanded: set[str] = set()
            for p in principals:
                expanded.update(_principal_family(p))
            placeholders = ",".join("?" for _ in expanded)
            c.execute(
                f"SELECT principal, derived_from FROM candidate_packets "
                f"WHERE principal IN ({placeholders})",
                tuple(expanded),
            )
        else:
            c.execute("SELECT principal, derived_from FROM candidate_packets")
        rows = c.fetchall()
    for row in rows:
        if isinstance(row, dict) or hasattr(row, "keys"):
            principal = row["principal"]
            df = row["derived_from"]
        else:
            principal, df = row[0], row[1]
        key = _parse_inbox_key(principal, df)
        if key is not None:
            keys.add(key)
    return keys


def _packet_content_sha1(content: Any, df: Any) -> str:
    """Prefer derived_from.content_sha1; hash the body when the stamp is missing."""
    if isinstance(df, str) and df:
        try:
            obj = json.loads(df)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            raw_sha = obj.get("content_sha1")
            if isinstance(raw_sha, str) and raw_sha:
                return raw_sha
    return _content_sha1(content or "")


def _sha_set(value: Any) -> set:
    """Normalize occupancy at one slot to a set of content_sha1s."""
    if isinstance(value, (set, frozenset)):
        return set(value)
    if isinstance(value, (list, tuple)):
        return {v for v in value if isinstance(v, str) and v}
    if isinstance(value, str) and value:
        return {value}
    return set()


def _all_occupied_shas(occupied: Dict[int, Any]) -> set:
    out: set = set()
    for v in occupied.values():
        out |= _sha_set(v)
    return out


def _fills_from_packet_rows(rows) -> Dict[Tuple[str, str], Dict[int, set]]:
    """Build occupancy from ``(principal, content, derived_from)`` rows."""
    fills: Dict[Tuple[str, str], Dict[int, set]] = {}
    for row in rows:
        if isinstance(row, dict) or hasattr(row, "keys"):
            p, content, df = row["principal"], row["content"], row["derived_from"]
        else:
            p, content, df = row[0], row[1], row[2]
        key = _parse_inbox_key(p, df)
        if key is None:
            continue
        canon, inbox_file, idx = key
        fills.setdefault((canon, inbox_file), {}).setdefault(idx, set()).add(
            _packet_content_sha1(content, df)
        )
    return fills


def _existing_fills_on_cursor(
    c, principals: set | None = None
) -> Dict[Tuple[str, str], Dict[int, set]]:
    """Occupancy map loaded on ``c`` (in-txn; not the pre-scan ``_fills_for_file``)."""
    if principals:
        expanded: set[str] = set()
        for p in principals:
            expanded.update(_principal_family(p))
        placeholders = ",".join("?" for _ in expanded)
        c.execute(
            f"SELECT principal, content, derived_from FROM candidate_packets "
            f"WHERE principal IN ({placeholders})",
            tuple(expanded),
        )
    else:
        c.execute(
            "SELECT principal, content, derived_from FROM candidate_packets"
        )
    return _fills_from_packet_rows(c.fetchall())


def _existing_fills(
    db, principals: set | None = None
) -> Dict[Tuple[str, str], Dict[int, set]]:
    """``(canonical principal, inbox_file) -> {candidate_index: set(sha)}``.

    Alias-family leftover rows occupy the canonical (file, index) slot so a
    remapped vault can extras-at-next-idx when the occupying body diverges.
    Keep every sha at a slot (CASE UNIQUE may be absent) so a same-slot
    twin cannot hide from extras skip.
    """
    with db.cursor() as c:
        return _existing_fills_on_cursor(c, principals)


def _fills_for_file(db, principal: str, inbox_file: str) -> List[Tuple[int, str]]:
    """``(candidate_index, content_sha1)`` already stored for this inbox file.

    Alias-family leftover rows occupy the canonical (file, index) slot.
    Index 0 alone is not the whole fill when the stored body diverges from
    a later compact_summary section or remapped stop-candidate. Same-slot
    twins (CASE UNIQUE not installed) yield one tuple per sha.
    """
    slot = _existing_fills(db, {principal}).get(
        (_canonical_principal(principal), inbox_file), {}
    )
    out: List[Tuple[int, str]] = []
    for idx in sorted(slot):
        for sha in sorted(_sha_set(slot[idx])):
            out.append((idx, sha))
    return out


def _assign_fill_indices(
    occupied: Dict[int, Any],
    requested: List[Tuple[int, str]],
) -> List[Optional[int]]:
    """Map each ``(requested_idx, sha)`` to an insert index, or None if present.

    Missing indices keep ``requested_idx``. Occupied indices whose body
    already exists (same sha at this index or any index) are skipped.
    Occupied divergent bodies go at the next free index after every
    reserved missing slot, so extras never steal a still-free distilled
    index. Identical extra shas share one extra index (agy-vault +
    gemini-vault grouped into one requested list must not mint a twin).
    Occupancy values are a set of shas per slot so a same-slot twin is
    not last-write hidden. Mutates ``occupied``.
    """
    for idx in list(occupied):
        occupied[idx] = _sha_set(occupied[idx])
    existing_shas = _all_occupied_shas(occupied)
    assigned: List[Optional[int]] = [None] * len(requested)
    extras: List[int] = []
    for i, (idx, sha) in enumerate(requested):
        if sha in existing_shas:
            continue
        if idx not in occupied:
            occupied[idx] = {sha}
            existing_shas.add(sha)
            assigned[i] = idx
        else:
            extras.append(i)
            # Claim the sha now so a later identical extra (agy-vault +
            # gemini-vault grouped into one requested list) does not mint
            # a twin at next_idx+1. UNIQUE is (canon, file, idx).
            existing_shas.add(sha)
    if extras:
        next_idx = (max(occupied) if occupied else -1) + 1
        for i in extras:
            _idx, sha = requested[i]
            if sha in _all_occupied_shas(occupied):
                continue
            occupied[next_idx] = {sha}
            existing_shas.add(sha)
            assigned[i] = next_idx
            next_idx += 1
    return assigned


def _existing_keys_for_on_cursor(c, wanted: set) -> set:
    """Return which of ``wanted`` keys already exist — narrow scan under txn.

    ``wanted`` entries are ``(principal, inbox_file, candidate_index)``.
    Principals are canonicalized (``agy`` → ``gemini``) so the
    ``key[0] == principal`` check matches leftover alias-family rows.
    Avoids full-table scan under ``BEGIN IMMEDIATE``.
    """
    if not wanted:
        return set()
    # Group by (canonical principal, inbox_file) → indices
    by_scope: Dict[Tuple[str, str], set] = {}
    for principal, inbox_file, idx in wanted:
        canon, inbox_file, idx = _make_inbox_key(principal, inbox_file, idx)
        by_scope.setdefault((canon, inbox_file), set()).add(idx)
    found: set = set()
    for (principal, inbox_file), indices in by_scope.items():
        family = _principal_family(principal)
        placeholders = ",".join("?" for _ in family)
        c.execute(
            f"""
            SELECT principal, derived_from FROM candidate_packets
            WHERE principal IN ({placeholders})
              AND derived_from IS NOT NULL
              AND json_extract(derived_from, '$.source') = 'inbox'
              AND json_extract(derived_from, '$.inbox_file') = ?
            """,
            (*family, inbox_file),
        )
        for row in c.fetchall():
            if isinstance(row, dict) or hasattr(row, "keys"):
                p, df = row["principal"], row["derived_from"]
            else:
                p, df = row[0], row[1]
            key = _parse_inbox_key(p, df)
            if (
                key is not None
                and key[0] == principal
                and key[1] == inbox_file
                and key[2] in indices
            ):
                found.add(key)
    return found


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
#
# Every agent in tools/author_principals.py AGENT_VAULT_DIRS must appear here.
# The two maps drifted once: `cursor` was declared there and omitted here, and
# because a missing slug makes vault_ingest skip the vault entirely rather than
# fail, cursor-vault silently accumulated 141 wiki pages that recall could never
# see. A skip is quiet; keep the maps in sync.
#
# This table is mirrored VERBATIM in plugins/minni/src/hook-utils.ts and
# scripts/inbox_cleanup.py. Both mirrors then drifted the same way -- they kept
# missing `cursor` long after it landed here. test_all_three_vault_slug_maps_agree
# (tests/test_vault_ingest.py) now compares all three; edit them together.
_VAULT_SLUG_TO_AGENT_ID: dict[str, str] = {
    "claudecode": "claude-code",
    "claude-science": "claude-science",
    "codex": "codex",
    "cursor": "cursor",
    "gemini": "gemini",
    "agy": "gemini",
    "antigravity": "gemini",
    "hermes": "hermes",
    "kilocode": "kilocode",
    "openclaw": "openclaw",
    "grok-build": "grok-build",
    "grok-beta": "grok-build",
    "grok": "grok-build",
    "xai": "grok-build",
}


def _canonical_principal(principal: Any) -> str:
    """Map a vault slug / leftover packet principal onto its canonical id.

    ``agy``/``antigravity`` → ``gemini``; ``xai``/``grok``/``grok-beta`` →
    ``grok-build``. Unknown values pass through so a genuine other-agent
    row stays a distinct UNIQUE key.
    """
    raw = str(principal or "")
    return _VAULT_SLUG_TO_AGENT_ID.get(raw, raw)


def _principal_family(principal: Any) -> tuple[str, ...]:
    """All principals that share identity with ``principal`` (including itself).

    Inbox UNIQUE is (principal, inbox_file, candidate_index). Remapping a
    vault slug without this family turns leftover ``agy``/``xai`` rows into
    a *new* key beside ``gemini``/``grok-build`` and dual-inserts #239.
    """
    raw = str(principal or "")
    canon = _canonical_principal(raw)
    family = {raw, canon}
    for slug, agent_id in _VAULT_SLUG_TO_AGENT_ID.items():
        if agent_id == canon:
            family.add(slug)
    return tuple(sorted(family))


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


def _alias_source_principal_for_inbox(
    inbox: Path, canonical_principal: str
) -> Optional[str]:
    """Raw vault slug when ingest remaps an alias vault to a canonical id.

    ``agy-vault`` stores principal=gemini; without ``source_principal``
    archive treats the row as a gemini-vault fill and gemini-first discover
    archives never-ingested ``gemini-vault/inbox``. Canonical vaults
    (slug == principal) return None.
    """
    parent = Path(inbox).parent.name
    if not parent.endswith("-vault"):
        return None
    slug = parent[: -len("-vault")]
    if slug and slug != canonical_principal:
        return slug
    return None


def _scan_inbox(
    inbox: Path, fallback_principal: str,
) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Return (candidate dicts, skipped-by-kind counts) for a single inbox dir
    (no DB, no dedup yet). Skipped counts make the kind gate observable: files
    dropped because they carry a non-stop ``kind`` (handoff, failed_command,
    ``*_precompact_handoff``, ...) are tallied per kind instead of vanishing
    silently; kind-less files that fail the stop-candidate shape check are
    tallied under ``_unrecognized``; individual audit-echo candidates (issue
    #193) under ``_audit_echo``."""
    out: List[Dict[str, Any]] = []
    skipped_by_kind: Dict[str, int] = {}
    inbox_principal = _principal_for_inbox(inbox, fallback_principal)
    source_principal = _alias_source_principal_for_inbox(inbox, inbox_principal)
    for path in sorted(inbox.glob("*.json")):
        # An unreadable or non-object payload used to be dropped with a bare
        # `continue`, incrementing nothing — so it was invisible to every
        # counter AND to any drain gated on those counters (Bugbot, #305).
        # Count it like every other excluded file: the drop is observable
        # rather than silent, which is this module's stated contract.
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            skipped_by_kind["_unparseable"] = skipped_by_kind.get("_unparseable", 0) + 1
            continue
        if not isinstance(doc, dict):
            skipped_by_kind["_unparseable"] = skipped_by_kind.get("_unparseable", 0) + 1
            continue
        kind = doc.get("kind")
        # `kind` must be a hashable, comparable value (str or None) before it
        # can be tested against STOP_KINDS (a set) — `x in <set>` raises
        # TypeError for unhashable types (list/dict), which would otherwise
        # abort the whole ingest() pass and starve every other file behind a
        # single malformed one (I3 security fix). Treat malformed kind like
        # any other excluded kind: skip and count it, keep scanning.
        if not isinstance(kind, str) and kind is not None:
            skipped_by_kind["_malformed_kind"] = skipped_by_kind.get("_malformed_kind", 0) + 1
            continue
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
        # Explicit non-empty privacy is preserved. Missing, null, or blank
        # values are treated as "writer decision: safe" — never re-introduce
        # SQL NULL (that is what permanently parked the backlog after 53da3bd).
        raw_privacy = doc.get("privacy_level", "safe")
        if raw_privacy is None or (
            isinstance(raw_privacy, str) and not raw_privacy.strip()
        ):
            privacy_level = "safe"
        else:
            privacy_level = str(raw_privacy).strip()
        file_agent = str(doc.get("agent_id") or "").strip()
        if file_agent and _canonical_principal(file_agent) != inbox_principal:
            skipped_by_kind["_agent_mismatch"] = skipped_by_kind.get("_agent_mismatch", 0) + 1
            continue
        principal = inbox_principal
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
            # Issue #193: a stale hook build drafting from the audit tail turns
            # every session stop into a proposal quoting Minni's own bookkeeping.
            # Drop it here — counted, never inserted — so a bookkeeping-only
            # session yields zero candidate_packets no matter which binary wrote
            # the file.
            if is_audit_echo(cand):
                skipped_by_kind["_audit_echo"] = skipped_by_kind.get("_audit_echo", 0) + 1
                continue
            content = cand.strip()[:CONTENT_CAP]
            row = {
                "principal": principal,
                "workspace_id": ws,
                "privacy_level": privacy_level,
                "content": content,
                "inbox_file": path.name,
                "candidate_index": idx,
                "proposed_at": proposed_at,
                # Record what the file actually declared; null for the
                # kind-less Claude Code shape. Never stamp an agent-specific
                # label onto another agent's rows.
                "kind": kind,
            }
            if source_principal:
                row["source_principal"] = source_principal
            out.append(row)
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

    occupancy = _existing_fills(db)

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in scanned:
        file_key = (_canonical_principal(r["principal"]), r["inbox_file"])
        grouped.setdefault(file_key, []).append(r)

    to_insert: List[Dict[str, Any]] = []
    already = 0
    for file_key, rows in grouped.items():
        slot = occupancy.setdefault(file_key, {})
        requested = [
            (int(r["candidate_index"]), _content_sha1(r["content"])) for r in rows
        ]
        assigned = _assign_fill_indices(slot, requested)
        for r, new_idx in zip(rows, assigned):
            if new_idx is None:
                already += 1
                continue
            if new_idx != r["candidate_index"]:
                r = dict(r)
                r["candidate_index"] = new_idx
            to_insert.append(r)

    inserted = 0
    if not dry_run and to_insert:
        with db.transaction() as c:
            # Issue #239: re-load only the keys we intend to insert (not the
            # full table) under BEGIN IMMEDIATE so concurrent ingest cannot
            # both pass the pre-txn check and create twins. UNIQUE swallow
            # remains the last backstop if the operator applied the index.
            # Key is principal-scoped so multi-vault same basenames coexist.
            wanted = {
                _make_inbox_key(r["principal"], r["inbox_file"], r["candidate_index"])
                for r in to_insert
            }
            txn_existing = _existing_keys_for_on_cursor(c, wanted)

            for r in to_insert:
                key = _make_inbox_key(
                    r["principal"], r["inbox_file"], r["candidate_index"]
                )
                if key in txn_existing:
                    already += 1
                    continue
                derived_obj: Dict[str, Any] = {
                    "source": "inbox",
                    "inbox_file": r["inbox_file"],
                    "candidate_index": r["candidate_index"],
                    "kind": r.get("kind"),
                    "content_sha1": _content_sha1(r["content"]),
                }
                source_principal = r.get("source_principal")
                if source_principal:
                    derived_obj["source_principal"] = source_principal
                derived_from = json.dumps(derived_obj)
                try:
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
                            r["privacy_level"],
                            r["content"],
                            json.dumps([]),
                            derived_from,
                            1 if is_instruction_like(r["content"]) else 0,
                            r["proposed_at"],
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    # Unique-index collision (post-#239 repair) or rare race:
                    # treat as already_present rather than aborting the batch.
                    # Re-raise CHECK/NOT NULL/FK integrity failures.
                    if not _is_unique_integrity_error(exc):
                        raise
                    already += 1
                    continue
                txn_existing.add(key)
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

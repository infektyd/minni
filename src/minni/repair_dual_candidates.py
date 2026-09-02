"""Issue #239: dual-resolution candidate_packets + virtual ``_durable`` index hygiene.

Background
----------
A one-time backfill-era double-ingest left 1,616 exact-duplicate
``candidate_packets`` rows (same ``inbox_file`` + ``candidate_index`` +
``content_sha1``). Consolidation then resolved nearly all of them both ways:
the first twin was ``accepted`` into a durable learning; the second was
``rejected`` as ``duplicate of existing learning``. That second decision is
correct *given* the double-ingest — but governance surfaces still see two
contradictory terminal rows for byte-identical content.

Separately, store-time semantic indexing writes synthetic document paths under
``…/_durable/<agent>__<digest>.md``. Those paths are **virtual identities**
(content lives in ``vault_fts`` / ``chunk_embeddings``; no file is written).
Stat-checking them as "dangling" is a false positive; true orphans are rows
with no FTS content (and for non-virtual paths, missing files).

Winner / collapse rule (applied by ``repair_duplicate_candidate_pairs``)
------------------------------------------------------------------------
1. If any row is ``accepted`` → keep the lowest-id accepted; hard-delete only
   non-``accepted`` twins (the real #239 promote-then-dedup shape). Never
   rewrites ``learnings``. Dual-``accepted`` (no non-accepted loser) is
   ``needs_operator`` — never auto-deleted; surfaces loudly for UNIQUE.
2. If any row is still ``proposed`` and none are ``accepted`` → **do not
   delete** when mixed with terminal; report ``needs_operator`` (must not let
   ``rejected`` win over an open proposed twin and block re-ingest).
3. All-``proposed``: consult active ``afm_review`` markers (same fence as
   consolidation). Prefer an **unfenced** proposed as winner so collapse
   cannot strand the only processable twin behind a fence. All-terminal
   groups collapse under lowest ``candidate_id``.
4. Losers are **deleted** after an audit row in ``consolidation_actions``
   (when present). Learnings, FTS, and embeddings are never touched.

Collapse scope (byte-identical only)
------------------------------------
Hard-delete only runs for twins that share the same
``(inbox_file, candidate_index, content_sha1)`` — the #239 dual-resolution
shape. App-key groups with **divergent** ``content_sha1`` are reported as
``divergent_content_groups`` and left untouched (prefer-accepted is a
governance signal, not a delete license for different bodies).

Concurrency
-----------
The dry-run plan is advisory. On ``--apply``, each group is **re-loaded and
re-winnered inside** ``BEGIN IMMEDIATE`` immediately before delete. Rows whose
current status is ``accepted`` are never deleted (hard guard). Stop the AFM
daemon / writers before ``--apply`` when possible.

FK hygiene
----------
Before deleting a loser, ``contradiction_log.resolution_id`` pointing at that
id is set to NULL (FK from migration 009; ``PRAGMA foreign_keys=ON``).

Inbox unique index
------------------
``ensure_inbox_dedup_index`` is **operator-only** (this CLI / ``run_full_repair``),
not a normal migration. Schema rebuilds of ``candidate_packets`` (e.g. 015)
drop it; re-run the repair CLI after cleanup to recreate.

This module is idempotent and safe to re-run. Default is dry-run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("minni.repair_dual_candidates")

# Reporting preference only (divergent samples / choose_winner). Hard-delete
# eligibility is decided by ``collapse_decision`` — rejected must NOT beat an
# open proposed twin (see issue #239 formal RC).
_STATUS_RANK: Dict[str, int] = {
    "accepted": 100,
    "merged": 40,
    "superseded": 35,
    "redacted": 30,
    "do_not_store": 25,
    "log_only": 25,
    "expired": 20,
    "rejected": 10,
    "proposed": 0,
}

_PROPOSED_STATUS = "proposed"
_ACCEPTED_STATUS = "accepted"

REPAIR_ACTION_TYPE = "issue239_dual_resolve"
VIRTUAL_DURABLE_MARKER = "/_durable/"
# Non-filesystem document identities (writeback learning nodes, future schemes).
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
# App-level inbox idempotency key (matches inbox_ingest / compact_distillation).
INBOX_DEDUP_INDEX = "idx_candidate_packets_inbox_key_unique"
# Legacy weaker index that included content_sha1; dropped on ensure.
LEGACY_INBOX_DEDUP_INDEX = "idx_candidate_packets_inbox_sha1_unique"


def _parse_derived(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _file_index_key(derived: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    """Return (inbox_file, candidate_index) for inbox-sourced rows."""
    if derived.get("source") != "inbox":
        return None
    inbox_file = derived.get("inbox_file")
    idx = derived.get("candidate_index")
    if not isinstance(inbox_file, str) or not inbox_file:
        return None
    if isinstance(idx, bool) or not isinstance(idx, (int, float)):
        return None
    idx_i = int(idx)
    if idx_i != idx:  # reject non-integral floats
        return None
    return (inbox_file, idx_i)


def _sql_quote_str(value: str) -> str:
    """Single-quote a SQL string literal (map keys, not user input)."""
    return "'" + str(value).replace("'", "''") + "'"


def _canonical_principal_sql_expr(column: str = "principal") -> str:
    """SQLite expression matching ``inbox_ingest._canonical_principal``.

    Operator UNIQUE must key the canonical fill so leftover ``agy``/``xai``
    rows collide with remapped ``gemini``/``grok-build`` inserts. Raw
    ``principal`` is a different tuple and cannot backstop #239 duals.
    """
    from minni.afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID

    remaps = [
        (slug, agent)
        for slug, agent in sorted(_VAULT_SLUG_TO_AGENT_ID.items())
        if slug != agent
    ]
    if not remaps:
        return column
    whens = " ".join(
        f"WHEN {_sql_quote_str(slug)} THEN {_sql_quote_str(agent)}"
        for slug, agent in remaps
    )
    return f"CASE {column} {whens} ELSE {column} END"


def _inbox_dedup_index_sql_is_current(sql: Any) -> bool:
    """True when sqlite_master SQL keys canonical fill, not raw principal."""
    sql_s = " ".join(str(sql or "").split()).lower()
    expr = " ".join(_canonical_principal_sql_expr("principal").split()).lower()
    return expr in sql_s


def _inbox_key(
    principal: Any, derived: Dict[str, Any]
) -> Optional[Tuple[str, str, int]]:
    """App key: (principal, inbox_file, candidate_index).

    Matches ``inbox_ingest`` / ``compact_distillation`` principal-scoped
    idempotency. Same basename in two agent vaults is not a dual.
    Vault-slug aliases (``agy``→``gemini``, ``xai``→``grok-build``) collapse
    to the canonical id so a remap is the same fill, not a new key.
    ``content_sha1`` is not part of the app key.
    """
    from minni.afm_passes.inbox_ingest import _canonical_principal

    fi = _file_index_key(derived)
    if fi is None:
        return None
    return (_canonical_principal(principal), fi[0], fi[1])


def _stamp_source_principal(derived_from: Any, leftover_principal: str) -> Optional[str]:
    """Return derived_from JSON with leftover slug preserved, or None.

    Collapse rewrites ``principal`` to the canonical host; archive uses
    ``source_principal`` to refuse the remapped vault's live file.
    """
    if not leftover_principal:
        return None
    if not isinstance(derived_from, str) or not derived_from:
        return None
    try:
        df = json.loads(derived_from)
    except Exception:
        return None
    if not isinstance(df, dict):
        return None
    df.setdefault("source_principal", leftover_principal)
    return json.dumps(df)


def _content_sha1_of(derived: Dict[str, Any]) -> Optional[str]:
    """Normalize content_sha1 from derived_from (None if missing/empty)."""
    sha = derived.get("content_sha1")
    if sha is None:
        return None
    if isinstance(sha, str) and sha.strip():
        return sha.strip().lower()
    return None


def _digest_content(content: Any) -> Optional[str]:
    """SHA1 hex of the content column (same algorithm as inbox_ingest)."""
    if content is None:
        return None
    if not isinstance(content, str):
        try:
            content = str(content)
        except Exception:
            return None
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def _collapse_digest(
    derived: Dict[str, Any], content: Any = None
) -> Optional[str]:
    """Byte-identity digest for collapse: content column is authoritative.

    When content is present, always key on ``sha1(content)`` so a stale/copied
    ``derived_from.content_sha1`` cannot group divergent bodies as twins.
    Stored sha is audit metadata only; used only when content is unavailable.
    """
    content_digest = _digest_content(content)
    if content_digest is not None:
        return content_digest
    return _content_sha1_of(derived)


def _collapse_key(
    principal: Any,
    derived: Dict[str, Any],
    content: Any = None,
) -> Optional[Tuple[str, str, int, Optional[str]]]:
    """Byte-identical collapse key: principal-scoped app key + content digest."""
    app = _inbox_key(principal, derived)
    if app is None:
        return None
    return (app[0], app[1], app[2], _collapse_digest(derived, content))


def status_rank(status: Optional[str]) -> int:
    return _STATUS_RANK.get(str(status or "").strip().lower(), 1)


def _norm_status(status: Any) -> str:
    return str(status or "").strip().lower()


def choose_winner(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Reporting preference: higher status_rank first, then lower candidate_id.

    **Not** the hard-delete eligibility rule. Use ``collapse_decision`` before
    deleting so rejected cannot beat an open proposed twin.
    """
    if not rows:
        raise ValueError("choose_winner requires at least one row")
    return min(
        rows,
        key=lambda r: (-status_rank(r.get("status")), int(r["candidate_id"])),
    )


def collapse_decision(
    rows: Sequence[Dict[str, Any]],
    *,
    fenced_ids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    """Decide whether a byte-identical dual group may hard-delete losers.

    Returns dict with:
      * ``action``: ``"collapse"`` | ``"needs_operator"``
      * ``winner``: winning row when action is collapse (else None)
      * ``reason``: short machine-readable cause
      * ``losers``: rows that would be deleted under collapse (empty if not)

    Rules:
      1. Any ``accepted`` → keep lowest-id accepted; delete non-accepted.
         Dual-accepted (only accepted twins, no non-accepted loser) →
         ``needs_operator`` / ``dual_accepted`` (no auto-delete path).
      2. Any ``proposed`` mixed with terminal and no ``accepted`` →
         needs-operator (no delete).
      3. All-proposed → prefer unfenced (no active ``afm_review``) as winner
         so the only processable twin is never deleted behind a fence;
         otherwise lowest candidate_id. All-terminal → lowest candidate_id.

    ``fenced_ids``: candidate_ids with an active (non-superseded) ``afm_review``
    consolidation_actions row — same fence consolidation uses to skip review.
    """
    if not rows:
        raise ValueError("collapse_decision requires at least one row")
    by_status: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_status[_norm_status(r.get("status"))].append(r)

    accepted = by_status.get(_ACCEPTED_STATUS) or []
    proposed = by_status.get(_PROPOSED_STATUS) or []
    fenced: set = set()
    if fenced_ids:
        for x in fenced_ids:
            try:
                fenced.add(int(x))
            except (TypeError, ValueError):
                continue

    if accepted:
        winner = min(accepted, key=lambda r: int(r["candidate_id"]))
        win_id = int(winner["candidate_id"])
        extra_accepted = [
            r for r in accepted if int(r["candidate_id"]) != win_id
        ]
        losers = [
            r
            for r in rows
            if int(r["candidate_id"]) != win_id
            and _norm_status(r.get("status")) != _ACCEPTED_STATUS
        ]
        # Dual/multi accepted with nothing safe to delete — operator path.
        # (Hard guard never deletes accepted; empty losers would be a no-op
        # that still blocks UNIQUE without a loud signal.)
        if not losers and extra_accepted:
            return {
                "action": "needs_operator",
                "winner": None,
                "reason": "dual_accepted",
                "losers": [],
                "extra_accepted": list(accepted),
            }
        return {
            "action": "collapse",
            "winner": winner,
            "reason": "accepted_present",
            "losers": losers,
            "extra_accepted": extra_accepted,
        }

    if proposed:
        # Mixed proposed + any terminal non-accepted → operator must decide.
        non_proposed = [
            r for r in rows if _norm_status(r.get("status")) != _PROPOSED_STATUS
        ]
        if non_proposed:
            return {
                "action": "needs_operator",
                "winner": None,
                "reason": "proposed_with_terminal",
                "losers": [],
                "extra_accepted": [],
            }
        # All proposed: prefer unfenced so we do not strand the only twin
        # consolidation would still process behind an afm_review fence.
        unfenced = [
            r for r in rows if int(r["candidate_id"]) not in fenced
        ]
        if unfenced:
            winner = min(unfenced, key=lambda r: int(r["candidate_id"]))
            any_fenced = any(int(r["candidate_id"]) in fenced for r in rows)
            reason = (
                "all_proposed_prefer_unfenced" if any_fenced else "all_proposed"
            )
        else:
            # All fenced equally — lowest id (same as historical rule).
            winner = min(rows, key=lambda r: int(r["candidate_id"]))
            reason = "all_proposed"
        win_id = int(winner["candidate_id"])
        return {
            "action": "collapse",
            "winner": winner,
            "reason": reason,
            "losers": [r for r in rows if int(r["candidate_id"]) != win_id],
            "extra_accepted": [],
        }

    # All terminal, no accepted: lowest id.
    winner = min(rows, key=lambda r: int(r["candidate_id"]))
    win_id = int(winner["candidate_id"])
    return {
        "action": "collapse",
        "winner": winner,
        "reason": "all_terminal",
        "losers": [r for r in rows if int(r["candidate_id"]) != win_id],
        "extra_accepted": [],
    }


def _active_afm_review_ids_on_cursor(c) -> set:
    """Candidate ids with a non-superseded ``afm_review`` marker (claim column)."""
    try:
        c.execute(
            """
            SELECT claim FROM consolidation_actions
            WHERE action_type = 'afm_review'
              AND COALESCE(status, '') != 'superseded'
            """
        )
    except sqlite3.Error as exc:
        # Only a missing table is excused (pre-014). Empty set on lock/schema
        # looks like nobody is fenced and collapse deletes the unfenced twin.
        if "no such table" not in str(exc).lower():
            raise
        return set()
    out: set = set()
    for row in c.fetchall():
        claim = row["claim"] if hasattr(row, "keys") else row[0]
        if claim is None:
            continue
        try:
            out.add(int(str(claim).strip()))
        except (TypeError, ValueError):
            continue
    return out


def _active_afm_review_ids(db) -> set:
    """Load active afm_review fences; empty if table missing."""
    if not _table_exists(db, "consolidation_actions"):
        return set()
    with db.cursor() as c:
        return _active_afm_review_ids_on_cursor(c)


def _iter_inbox_candidates(db) -> List[Dict[str, Any]]:
    """Load inbox-sourced candidate rows with parsed collapse metadata."""
    items: List[Dict[str, Any]] = []
    with db.cursor() as c:
        c.execute(
            """
            SELECT candidate_id, principal, status, content, derived_from,
                   proposed_at, resolved_at, resolved_by, resolution_reason
            FROM candidate_packets
            WHERE derived_from IS NOT NULL
            """
        )
        for row in c.fetchall():
            item = dict(row)
            derived = _parse_derived(item.get("derived_from"))
            if not derived:
                continue
            ckey = _collapse_key(
                item.get("principal"), derived, item.get("content")
            )
            if ckey is None:
                continue
            item["_key"] = ckey
            item["_app_key"] = (ckey[0], ckey[1], ckey[2])
            item["_content_sha1"] = ckey[3]
            items.append(item)
    return items


def _group_key_dict(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ckey = rows[0]["_key"]
    return {
        "principal": ckey[0],
        "inbox_file": ckey[1],
        "candidate_index": ckey[2],
        "content_sha1": ckey[3],
    }


def _group_summary(
    rows: Sequence[Dict[str, Any]],
    *,
    fenced_ids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    """Summarize a byte-identical dual group with collapse eligibility."""
    decision = collapse_decision(rows, fenced_ids=fenced_ids)
    statuses = sorted({str(r.get("status")) for r in rows})
    base = {
        "key": _group_key_dict(rows),
        "statuses": statuses,
        "row_count": len(rows),
        "candidate_ids": sorted(int(r["candidate_id"]) for r in rows),
        "collapse_action": decision["action"],
        "collapse_reason": decision["reason"],
    }
    if decision["action"] == "collapse" and decision["winner"] is not None:
        winner = decision["winner"]
        losers = decision["losers"]
        base.update(
            {
                "winner_id": int(winner["candidate_id"]),
                "winner_status": winner.get("status"),
                "loser_ids": [int(r["candidate_id"]) for r in losers],
            }
        )
    else:
        base.update(
            {
                "winner_id": None,
                "winner_status": None,
                "loser_ids": [],
            }
        )
    return base


def _byte_identical_row_groups(db) -> List[List[Dict[str, Any]]]:
    groups: Dict[
        Tuple[str, str, int, Optional[str]], List[Dict[str, Any]]
    ] = defaultdict(list)
    for item in _iter_inbox_candidates(db):
        groups[item["_key"]].append(item)
    return [rows for rows in groups.values() if len(rows) >= 2]


def find_duplicate_candidate_groups(db) -> List[Dict[str, Any]]:
    """Group inbox candidates by (inbox_file, candidate_index, content digest).

    Returns only **byte-identical collapsible** groups with 2+ rows — the #239
    dual shape that hard-delete may act on. Groups that need an operator
    (proposed + terminal, dual accepted) are reported via
    ``find_needs_operator_groups`` and are not hard-deleted.
    """
    fenced = _active_afm_review_ids(db)
    out: List[Dict[str, Any]] = []
    for rows in _byte_identical_row_groups(db):
        summary = _group_summary(rows, fenced_ids=fenced)
        if summary["collapse_action"] != "collapse":
            continue
        # Skip groups where collapse would delete nothing.
        # Empty loser_ids alone is enough — groups always have row_count >= 2
        # from _byte_identical_row_groups, so a row_count>1 conjunct was dead
        # (comparison binds tighter than not).
        if not summary["loser_ids"]:
            continue
        out.append(summary)
    out.sort(key=lambda g: (g["winner_id"] is None, g["winner_id"] or 0))
    return out


def find_needs_operator_groups(db) -> List[Dict[str, Any]]:
    """Byte-identical duals that must not auto-delete.

    Covers proposed+terminal (no accepted) and dual-accepted (no safe loser).
    """
    fenced = _active_afm_review_ids(db)
    out: List[Dict[str, Any]] = []
    for rows in _byte_identical_row_groups(db):
        summary = _group_summary(rows, fenced_ids=fenced)
        if summary["collapse_action"] == "needs_operator":
            out.append(summary)
    out.sort(
        key=lambda g: (
            g["key"].get("principal", ""),
            g["key"]["inbox_file"],
            g["key"]["candidate_index"],
            g["candidate_ids"][0] if g.get("candidate_ids") else 0,
        )
    )
    return out


def find_divergent_content_groups(db) -> List[Dict[str, Any]]:
    """App-key groups whose rows do not share a single content_sha1.

    These are **not** collapsed by default: different bodies under the same
    inbox key are left for a separate operator policy (not #239 duals).
    """
    by_app: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = defaultdict(list)
    for item in _iter_inbox_candidates(db):
        by_app[item["_app_key"]].append(item)

    out: List[Dict[str, Any]] = []
    for app_key, rows in by_app.items():
        if len(rows) < 2:
            continue
        shas = {r.get("_content_sha1") for r in rows}
        if len(shas) <= 1:
            continue
        # Prefer accepted for reporting only — no delete.
        preferred = choose_winner(rows)
        out.append(
            {
                "key": {
                    "principal": app_key[0],
                    "inbox_file": app_key[1],
                    "candidate_index": app_key[2],
                },
                "content_sha1s": sorted(s or "" for s in shas),
                "row_count": len(rows),
                "candidate_ids": sorted(int(r["candidate_id"]) for r in rows),
                "preferred_id": int(preferred["candidate_id"]),
                "preferred_status": preferred.get("status"),
                "statuses": sorted({str(r.get("status")) for r in rows}),
            }
        )
    out.sort(key=lambda g: g["preferred_id"])
    return out


def find_app_key_collisions(db) -> List[Dict[str, Any]]:
    """App-key groups with COUNT>1 — blocks the principal-scoped unique index.

    Stronger than ``find_duplicate_candidate_groups``: includes both
    byte-identical duals **and** divergent-content peers under the same
    ``(principal, inbox_file, candidate_index)``. Used by
    ``ensure_inbox_dedup_index`` so CREATE UNIQUE never falls through to a
    confusing IntegrityError.
    """
    by_app: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = defaultdict(list)
    for item in _iter_inbox_candidates(db):
        by_app[item["_app_key"]].append(item)

    out: List[Dict[str, Any]] = []
    for app_key, rows in by_app.items():
        if len(rows) < 2:
            continue
        shas = sorted({(r.get("_content_sha1") or "") for r in rows})
        out.append(
            {
                "key": {
                    "principal": app_key[0],
                    "inbox_file": app_key[1],
                    "candidate_index": app_key[2],
                },
                "row_count": len(rows),
                "candidate_ids": sorted(int(r["candidate_id"]) for r in rows),
                "content_sha1s": shas,
                "statuses": sorted({str(r.get("status")) for r in rows}),
                "byte_identical": len(shas) <= 1,
            }
        )
    out.sort(
        key=lambda g: (
            g["key"].get("principal", ""),
            g["key"]["inbox_file"],
            g["key"]["candidate_index"],
            g["candidate_ids"][0] if g["candidate_ids"] else 0,
        )
    )
    return out


def _load_collapse_group_on_cursor(
    c, key: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Re-read byte-identical twins for one collapse key under an open txn.

    Always scopes by principal family (canonical id plus vault-slug aliases)
    so a leftover ``agy``/``xai`` twin of a remapped ``gemini``/``grok-build``
    row is the same fill. A same-basename peer under another agent still
    cannot be hard-deleted.
    """
    from minni.afm_passes.inbox_ingest import _principal_family

    principal = str(key.get("principal") or "")
    inbox_file = key.get("inbox_file")
    candidate_index = key.get("candidate_index")
    content_sha1 = key.get("content_sha1")
    if not isinstance(inbox_file, str) or candidate_index is None:
        return []
    family = _principal_family(principal)
    placeholders = ",".join("?" for _ in family)
    # COALESCE so empty-string principal matches rows with '' or NULL.
    c.execute(
        f"""
        SELECT candidate_id, principal, status, content, derived_from,
               proposed_at, resolved_at, resolved_by, resolution_reason
        FROM candidate_packets
        WHERE COALESCE(principal, '') IN ({placeholders})
          AND derived_from IS NOT NULL
          AND json_extract(derived_from, '$.source') = 'inbox'
          AND json_extract(derived_from, '$.inbox_file') = ?
          AND CAST(json_extract(derived_from, '$.candidate_index') AS INTEGER) = ?
        """,
        (*family, inbox_file, int(candidate_index)),
    )
    want_sha = (
        content_sha1.strip().lower()
        if isinstance(content_sha1, str) and content_sha1.strip()
        else None
    )
    rows: List[Dict[str, Any]] = []
    for row in c.fetchall():
        item = dict(row)
        derived = _parse_derived(item.get("derived_from"))
        if not derived:
            continue
        ckey = _collapse_key(
            item.get("principal"), derived, item.get("content")
        )
        if ckey is None:
            continue
        # Defense-in-depth: never accept a row from another principal.
        if ckey[0] != principal:
            continue
        row_sha = ckey[3]
        if row_sha != want_sha:
            continue
        item["_key"] = ckey
        item["_content_sha1"] = row_sha
        rows.append(item)
    return rows


def _table_exists(db, name: str) -> bool:
    """Probe via auto-commit cursor — never call inside ``db.transaction()``.

    ``db.cursor()`` commits on exit; using it mid-transaction would end the
    outer ``BEGIN IMMEDIATE`` early and commit deletes piecewise.
    """
    with db.cursor() as c:
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        )
        return c.fetchone() is not None


def _table_exists_on_cursor(c, name: str) -> bool:
    """Probe using an open transaction cursor (no commit)."""
    c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    )
    return c.fetchone() is not None


def _audit_drop(
    c,
    loser_id: int,
    winner_id: int,
    winner_status: str,
    now: float,
    *,
    loser: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort audit row; no-op if consolidation_actions is missing.

    Detail carries a compact JSON snapshot of the deleted twin (status, reason,
    content digest, principal) so forensics survive the hard DELETE.
    """
    snapshot: Dict[str, Any] = {
        "loser_id": loser_id,
        "kept_id": winner_id,
        "kept_status": winner_status,
        "rule": "accepted|all_terminal|all_proposed>lowest_id; never proposed+terminal",
    }
    if loser is not None:
        content = loser.get("content")
        digest = loser.get("_content_sha1") or _digest_content(content)
        snapshot.update(
            {
                "loser_status": loser.get("status"),
                "loser_reason": loser.get("resolution_reason"),
                "loser_principal": loser.get("principal"),
                "content_sha1": digest,
            }
        )
    try:
        detail = json.dumps(snapshot, separators=(",", ":"), default=str)
    except Exception:
        detail = (
            f"deleted dual twin #{loser_id}; kept #{winner_id} "
            f"(status={winner_status}); rule=accepted|all_terminal|all_proposed"
        )
    try:
        c.execute(
            """
            INSERT INTO consolidation_actions
            (action_type, claim, category, status, detail, created_at)
            VALUES (?, ?, 'general', 'applied', ?, ?)
            """,
            (
                REPAIR_ACTION_TYPE,
                str(loser_id),
                detail[:1000],
                now,
            ),
        )
    except Exception as exc:
        logger.debug("audit insert skipped for loser %s: %s", loser_id, exc)


def _null_contradiction_resolution(c, loser_id: int) -> None:
    """Clear contradiction_log.resolution_id FK before deleting a loser.

    Migration 009 defines
    ``FOREIGN KEY (resolution_id) REFERENCES candidate_packets(candidate_id)``
    with no ON DELETE SET NULL; with ``PRAGMA foreign_keys=ON`` a bare DELETE
    of a referenced candidate aborts the whole repair transaction.
    """
    try:
        c.execute(
            "UPDATE contradiction_log SET resolution_id = NULL WHERE resolution_id = ?",
            (loser_id,),
        )
    except Exception as exc:
        # Table missing on partial schemas — non-fatal.
        logger.debug(
            "contradiction_log resolution_id null-out skipped for %s: %s",
            loser_id,
            exc,
        )


def repair_duplicate_candidate_pairs(
    db, *, dry_run: bool = True, limit: Optional[int] = None
) -> Dict[str, Any]:
    """Collapse **byte-identical** dual inbox candidate groups to one row each.

    Never touches ``learnings``. Idempotent: a second run finds zero groups.

    On apply, each group is re-loaded and re-winnered **inside** the write
    transaction so concurrent consolidation cannot leave a stale plan that
    deletes the twin that just became ``accepted``. Rows currently
    ``accepted`` are never hard-deleted.
    """
    groups = find_duplicate_candidate_groups(db)
    needs_operator = find_needs_operator_groups(db)
    divergent = find_divergent_content_groups(db)
    if limit is not None:
        groups = groups[: max(0, int(limit))]

    plan = []
    for g in groups:
        plan.append(
            {
                "keep": g["winner_id"],
                "keep_status": g["winner_status"],
                "delete": g["loser_ids"],
                "key": g["key"],
                "statuses": g["statuses"],
                "collapse_reason": g.get("collapse_reason"),
            }
        )

    deleted = 0
    groups_applied = 0
    groups_skipped_stale = 0
    groups_needs_operator_live = 0
    skipped_accepted_guard = 0
    winner_replanned = 0
    fk_nulled = 0

    dual_accepted = [
        g for g in needs_operator if g.get("collapse_reason") == "dual_accepted"
    ]

    if not dry_run and plan:
        now = time.time()
        from minni.afm_passes.inbox_ingest import _canonical_principal
        # Probe once outside the write loop so we never open db.cursor()
        # (which commits) while BEGIN IMMEDIATE is held.
        has_audit = _table_exists(db, "consolidation_actions")
        has_contradiction = _table_exists(db, "contradiction_log")
        with db.transaction() as c:
            # Re-probe on the txn cursor as a belt-and-suspenders check
            # without committing mid-batch.
            if not has_audit:
                has_audit = _table_exists_on_cursor(c, "consolidation_actions")
            if not has_contradiction:
                has_contradiction = _table_exists_on_cursor(c, "contradiction_log")
            # Fence set for all-proposed prefer-unfenced (same as consolidation).
            live_fenced = (
                _active_afm_review_ids_on_cursor(c) if has_audit else set()
            )
            for item in plan:
                # Re-validate under the write lock — never trust the pre-txn
                # plan for deletes (concurrent AFM can change status between
                # plan and BEGIN IMMEDIATE).
                live_rows = _load_collapse_group_on_cursor(c, item["key"])
                if len(live_rows) < 2:
                    groups_skipped_stale += 1
                    continue
                decision = collapse_decision(live_rows, fenced_ids=live_fenced)
                if decision["action"] != "collapse" or decision["winner"] is None:
                    groups_needs_operator_live += 1
                    continue
                live_winner = decision["winner"]
                live_keep = int(live_winner["candidate_id"])
                live_status = str(live_winner.get("status") or "")
                if item.get("keep") is not None and live_keep != int(item["keep"]):
                    winner_replanned += 1
                live_losers = list(decision["losers"])
                group_deleted = 0
                for loser in live_losers:
                    loser_id = int(loser["candidate_id"])
                    loser_status = _norm_status(loser.get("status"))
                    # Hard guard: never delete an accepted twin (provenance).
                    if loser_status == _ACCEPTED_STATUS:
                        skipped_accepted_guard += 1
                        continue
                    if has_contradiction:
                        _null_contradiction_resolution(c, loser_id)
                        fk_nulled += 1
                    if has_audit:
                        _audit_drop(
                            c,
                            loser_id,
                            live_keep,
                            live_status,
                            now,
                            loser=loser,
                        )
                    c.execute(
                        "DELETE FROM candidate_packets WHERE candidate_id=?",
                        (loser_id,),
                    )
                    deleted += 1
                    group_deleted += 1
                if group_deleted:
                    groups_applied += 1
                    # Leftover agy/xai winners keep the raw principal; list/
                    # resolve match that column, so rewrite to the canonical
                    # id after losers are gone (UNIQUE CASE already agrees).
                    # Stamp the leftover slug into derived_from so archive
                    # can still refuse the remapped vault after this UPDATE
                    # makes owner_is_alias false.
                    winner_principal = str(live_winner.get("principal") or "")
                    canon = _canonical_principal(winner_principal)
                    if canon and canon != winner_principal:
                        new_derived = _stamp_source_principal(
                            live_winner.get("derived_from"), winner_principal
                        )
                        if new_derived is not None:
                            c.execute(
                                "UPDATE candidate_packets SET principal=?, "
                                "derived_from=? WHERE candidate_id=?",
                                (canon, new_derived, live_keep),
                            )
                        else:
                            c.execute(
                                "UPDATE candidate_packets SET principal=? "
                                "WHERE candidate_id=?",
                                (canon, live_keep),
                            )
                elif live_losers:
                    # All losers were accepted-guarded — group unresolved.
                    groups_skipped_stale += 1
                elif decision.get("extra_accepted"):
                    skipped_accepted_guard += len(decision["extra_accepted"])
                    groups_skipped_stale += 1

    return {
        "dry_run": dry_run,
        "groups_found": len(groups),
        "would_delete": sum(len(p["delete"]) for p in plan),
        "deleted": deleted if not dry_run else 0,
        "groups_applied": groups_applied if not dry_run else 0,
        "groups_skipped_stale": groups_skipped_stale if not dry_run else 0,
        "groups_needs_operator_live": (
            groups_needs_operator_live if not dry_run else 0
        ),
        "needs_operator_groups": len(needs_operator),
        "needs_operator_sample": [
            {
                "key": g["key"],
                "statuses": g["statuses"],
                "candidate_ids": g.get("candidate_ids", [])[:8],
                "reason": g.get("collapse_reason"),
            }
            for g in needs_operator[:5]
        ],
        "dual_accepted_groups": len(dual_accepted),
        "dual_accepted_sample": [
            {
                "key": g["key"],
                "statuses": g["statuses"],
                "candidate_ids": g.get("candidate_ids", [])[:8],
                "reason": g.get("collapse_reason"),
            }
            for g in dual_accepted[:5]
        ],
        "winner_replanned": winner_replanned if not dry_run else 0,
        "skipped_accepted_guard": skipped_accepted_guard if not dry_run else 0,
        "fk_resolution_nulled": fk_nulled if not dry_run else 0,
        "divergent_content_groups": len(divergent),
        "divergent_sample": divergent[:5],
        "collapse_scope": (
            "byte-identical only (inbox_file, candidate_index, sha1(content)); "
            "divergent content under the same app key is reported, not deleted; "
            "proposed+terminal and dual-accepted are needs-operator, not deleted; "
            "all-proposed prefers unfenced (no active afm_review) as winner"
        ),
        "winner_rule": (
            "if any accepted → keep accepted, delete non-accepted; "
            "dual-accepted → needs-operator (no auto-delete); "
            "if any proposed and none accepted → needs-operator when mixed "
            "with terminal; all-proposed → prefer unfenced over afm_review "
            "fence, else lowest candidate_id; all-terminal → lowest id; "
            "re-validated inside write txn; never delete status=accepted"
        ),
        "learnings_touched": False,
        "sample": plan[:5],
        "operator_note": (
            "Stop AFM/daemon writers before --apply when possible. "
            "Inbox unique index is operator-only (not a migration); re-run "
            "this repair after candidate_packets schema rebuilds. "
            "needs_operator groups (proposed+terminal, dual-accepted) require "
            "manual resolution before unique-index creation can proceed."
        ),
    }


def ensure_inbox_dedup_index(db) -> Dict[str, Any]:
    """Create a partial unique index matching app-level inbox idempotency.

    Key is ``(canonical principal, inbox_file, candidate_index)`` for
    ``source='inbox'`` — the same key ``inbox_ingest`` /
    ``compact_distillation`` use. Vault-slug aliases (``agy``→``gemini``,
    ``xai``→``grok-build``) are folded in SQL so leftover alias rows collide
    with remapped inserts. ``content_sha1`` is not part of the constraint.

    Requires principal-scoped (file, index) duplicates to already be collapsed
    (call repair first). Migrates off the legacy weaker
    ``…_inbox_sha1_unique``, any pre-principal ``…_inbox_key_unique``
    (file, index only) index, and any raw-``principal`` ``…_inbox_key_unique``
    that would still admit alias twins.

    **Operator-only, not a migration.** Schema rebuilds of
    ``candidate_packets`` drop this index; re-run
    ``scripts/repair_issue_239.py --apply`` after cleanup to recreate it.
    """
    index_name = INBOX_DEDUP_INDEX
    with db.cursor() as c:
        c.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=? LIMIT 1",
            (index_name,),
        )
        row = c.fetchone()
        if row:
            sql = row["sql"] if hasattr(row, "keys") else row[0]
            if _inbox_dedup_index_sql_is_current(sql):
                c.execute(f"DROP INDEX IF EXISTS {LEGACY_INBOX_DEDUP_INDEX}")
                return {"status": "exists", "index": index_name}
            # Stale global (file, index) or raw-principal index exists — do
            # NOT drop until we know canonical-fill CREATE can succeed
            # (collision preflight).

    collisions = find_app_key_collisions(db)
    if collisions:
        return {
            "status": "blocked_by_duplicates",
            "index": index_name,
            "duplicate_groups": len(collisions),
            "app_key_collisions": len(collisions),
            "byte_identical_groups": sum(
                1 for g in collisions if g.get("byte_identical")
            ),
            "divergent_groups": sum(
                1 for g in collisions if not g.get("byte_identical")
            ),
            "sample": [
                {
                    "key": g["key"],
                    "row_count": g["row_count"],
                    "candidate_ids": g["candidate_ids"][:8],
                    "content_sha1s": g["content_sha1s"][:8],
                    "byte_identical": g["byte_identical"],
                }
                for g in collisions[:5]
            ],
            "note": (
                "Existing UNIQUE index left in place until collisions clear; "
                "stale global (file,index) or raw-principal indexes are only "
                "replaced on CREATE."
            ),
        }

    try:
        canon_principal = _canonical_principal_sql_expr("principal")
        with db.transaction() as c:
            # Drop legacy + any stale index inside the same txn as CREATE
            # so we never leave zero uniqueness backstop on a failed upgrade.
            c.execute(f"DROP INDEX IF EXISTS {LEGACY_INBOX_DEDUP_INDEX}")
            c.execute(f"DROP INDEX IF EXISTS {index_name}")
            c.execute(
                f"""
                CREATE UNIQUE INDEX {index_name}
                ON candidate_packets (
                    {canon_principal},
                    json_extract(derived_from, '$.inbox_file'),
                    CAST(json_extract(derived_from, '$.candidate_index') AS INTEGER)
                )
                WHERE json_extract(derived_from, '$.source') = 'inbox'
                  AND json_extract(derived_from, '$.inbox_file') IS NOT NULL
                  AND json_extract(derived_from, '$.candidate_index') IS NOT NULL
                """
            )
        return {"status": "created", "index": index_name}
    except Exception as exc:
        logger.warning("failed to create inbox dedup index: %s", exc)
        return {"status": "error", "index": index_name, "error": str(exc)}


def is_virtual_durable_path(path: str) -> bool:
    """True for store-time synthetic durable index identities (no disk file)."""
    if not path:
        return False
    # Normalize so Windows and doubled separators still match.
    norm = path.replace("\\", "/")
    return VIRTUAL_DURABLE_MARKER in norm


def is_virtual_identity_path(
    path: str, page_type: Optional[str] = None
) -> bool:
    """True for non-filesystem document identities that must not be disk-pruned.

    Covers:
      * virtual ``_durable`` store-time paths
      * URI identities such as ``learning://<id>`` (writeback graph nodes)
      * rows with ``page_type='learning'`` (synthetic learning docs)
    """
    if is_virtual_durable_path(path):
        return True
    if page_type is not None and str(page_type).strip().lower() == "learning":
        return True
    if path and _URI_SCHEME_RE.match(path):
        return True
    return False


def _normalize_vault_roots(
    vault_roots: Optional[Sequence[str]] = None,
) -> List[str]:
    """Return absolute, existing vault root directories."""
    roots: List[str] = []
    seen: set = set()
    for raw in vault_roots or ():
        if not raw:
            continue
        expanded = os.path.abspath(os.path.expanduser(str(raw)))
        if expanded in seen:
            continue
        seen.add(expanded)
        if os.path.isdir(expanded):
            roots.append(expanded)
    return roots


def resolve_document_path(
    path: str, vault_roots: Optional[Sequence[str]] = None
) -> Optional[str]:
    """Resolve a documents.path to an absolute filesystem path if it exists.

    Absolute paths are used as-is. Relative paths (vault-bridge style
    identities like ``wiki/decisions/foo.md``) are tried under each
    configured vault root. Returns the first existing file path, or None
    if no candidate exists on disk.
    """
    if not path:
        return None
    if os.path.isabs(path):
        return path if os.path.isfile(path) else None
    # Relative identity: try each vault root.
    norm = path.replace("\\", "/").lstrip("/")
    if not norm or ".." in norm.split("/"):
        return None
    for root in _normalize_vault_roots(vault_roots):
        candidate = os.path.join(root, *norm.split("/"))
        if os.path.isfile(candidate):
            return candidate
    # Also accept CWD-relative existence (legacy absolute-miswritten-as-rel).
    if os.path.isfile(path):
        return os.path.abspath(path)
    return None


def _doc_ids_with_fts(db, doc_ids: Sequence[int]) -> set:
    """Return the subset of doc_ids that still have vault_fts content."""
    if not doc_ids:
        return set()
    found: set = set()
    # Chunk to stay under SQLite variable limits on huge DBs.
    ids = [int(x) for x in doc_ids]
    with db.cursor() as c:
        for i in range(0, len(ids), 400):
            chunk = ids[i : i + 400]
            placeholders = ",".join("?" * len(chunk))
            c.execute(
                f"SELECT DISTINCT doc_id FROM vault_fts WHERE doc_id IN ({placeholders})",
                chunk,
            )
            for row in c.fetchall():
                found.add(int(row["doc_id"] if hasattr(row, "keys") else row[0]))
    return found


def _doc_ids_with_chunks(db, doc_ids: Sequence[int]) -> set:
    """Return the subset of doc_ids that still have chunk_embeddings rows."""
    if not doc_ids:
        return set()
    found: set = set()
    ids = [int(x) for x in doc_ids]
    with db.cursor() as c:
        for i in range(0, len(ids), 400):
            chunk = ids[i : i + 400]
            placeholders = ",".join("?" * len(chunk))
            try:
                c.execute(
                    f"SELECT DISTINCT doc_id FROM chunk_embeddings "
                    f"WHERE doc_id IN ({placeholders})",
                    chunk,
                )
            except Exception:
                # Table may be missing on partial schemas / mid-migration.
                return found
            for row in c.fetchall():
                found.add(int(row["doc_id"] if hasattr(row, "keys") else row[0]))
    return found


def find_missing_document_rows(
    db,
    *,
    include_virtual_durable: bool = False,
    vault_roots: Optional[Sequence[str]] = None,
    force_prune_indexed: bool = False,
) -> List[Dict[str, Any]]:
    """Document rows whose path is missing on disk after vault-root resolution.

    By default **excludes** virtual identities (``_durable``, ``learning://``,
    other ``scheme://`` URIs, ``page_type='learning'``). Set
    ``include_virtual_durable=True`` only for diagnostics.

    Relative ``documents.path`` values (vault bridge) are resolved against
    ``vault_roots`` before the existence check.

    FTS-backed and chunk-backed rows (relative **or** absolute) are **never**
    classified missing unless ``force_prune_indexed=True``. That prevents an
    operator from wiping the last recall copy when a vault moves, NFS blips,
    or home prefix changes — the file may be gone but the index still holds
    the content.
    """
    roots = _normalize_vault_roots(vault_roots)
    missing: List[Dict[str, Any]] = []
    with db.cursor() as c:
        c.execute(
            "SELECT doc_id, path, agent, page_status, page_type FROM documents"
        )
        rows = [dict(r) for r in c.fetchall()]

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        path = row.get("path") or ""
        page_type = row.get("page_type")
        if is_virtual_identity_path(path, page_type) and not include_virtual_durable:
            continue
        if not path:
            continue
        if resolve_document_path(path, roots) is not None:
            continue
        candidates.append(row)

    if not candidates:
        return []

    # Protect recallable rows: FTS/chunk content is the last remaining copy
    # when the on-disk file is gone (home move, NFS unmount, wrong vault root).
    fts_backed = _doc_ids_with_fts(db, [r["doc_id"] for r in candidates])
    chunk_backed = _doc_ids_with_chunks(db, [r["doc_id"] for r in candidates])
    for row in candidates:
        path = row.get("path") or ""
        is_rel = not os.path.isabs(path)
        doc_id = int(row["doc_id"])
        if not force_prune_indexed and (
            doc_id in fts_backed or doc_id in chunk_backed
        ):
            # Still recallable via FTS/chunks — refuse to prune unless the
            # operator explicitly opts into --force-prune-indexed.
            continue
        if is_rel and not roots:
            # No vault roots + relative path: cannot prove absence.
            continue
        missing.append(row)
    return missing


def find_orphan_virtual_durable(db) -> List[Dict[str, Any]]:
    """Virtual ``_durable`` rows with neither FTS nor chunks — true index garbage.

    Mirrors non-virtual missing-path protection: a row is only an orphan when
    **both** lexical (``vault_fts``) and semantic (``chunk_embeddings``) recall
    are absent. Chunk-only virtual rows (partial/legacy index damage) are kept.
    """
    candidates: List[Dict[str, Any]] = []
    with db.cursor() as c:
        # Escape '_' so SQLite LIKE does not treat it as a single-char wildcard
        # (is_virtual_durable_path uses a Python substring of '/_durable/').
        c.execute(
            r"""
            SELECT d.doc_id, d.path, d.agent, d.page_status
            FROM documents d
            WHERE d.path LIKE '%/\_durable/%' ESCAPE '\'
            """
        )
        candidates = [dict(r) for r in c.fetchall()]
    if not candidates:
        return []
    doc_ids = [int(r["doc_id"]) for r in candidates]
    fts_backed = _doc_ids_with_fts(db, doc_ids)
    chunk_backed = _doc_ids_with_chunks(db, doc_ids)
    return [
        r
        for r in candidates
        if int(r["doc_id"]) not in fts_backed and int(r["doc_id"]) not in chunk_backed
    ]


def _purge_semantic_side_effects(
    chunk_ids: Sequence[int],
    *,
    faiss_index: Any = None,
) -> Dict[str, Any]:
    """Mirror ``RetrievalEngine.purge_durable_document`` FAISS/rerank cleanup.

    Best-effort and fail-open: SQLite is already the source of truth after
    prune; stale warm-FAISS / rerank cache only delays correctness until the
    next rebuild (disk FAISS is checksum-gated against chunk_embeddings).
    """
    ids = [int(x) for x in chunk_ids if x is not None]
    result: Dict[str, Any] = {
        "chunk_ids": len(ids),
        "rerank_invalidated": False,
        "faiss_removed": 0,
        "faiss_status": "skipped_no_index",
    }
    if not ids:
        result["faiss_status"] = "noop"
        return result

    try:
        from minni.rerank_cache import invalidate_chunks

        invalidate_chunks(ids)
        result["rerank_invalidated"] = True
    except Exception as exc:
        logger.debug("prune: rerank invalidation skipped: %s", exc)

    if faiss_index is None:
        return result

    removed = 0
    try:
        remove_fn = getattr(faiss_index, "remove", None)
        if remove_fn is None:
            result["faiss_status"] = "skipped_no_remove"
            return result
        # FAISSIndex.remove(chunk_id: int); backends.faiss_* take List[int].
        # Probe with a single id; fall back to bulk list on TypeError.
        if hasattr(faiss_index, "_reverse_map"):
            for cid in ids:
                try:
                    remove_fn(cid)
                    removed += 1
                except Exception:
                    pass
        else:
            try:
                remove_fn(list(ids))
                removed = len(ids)
            except TypeError:
                for cid in ids:
                    try:
                        remove_fn(cid)
                        removed += 1
                    except Exception:
                        pass
        result["faiss_removed"] = removed
        result["faiss_status"] = "ok" if removed else "noop"
        # Persist tombstones when the index supports it (optional).
        save = getattr(faiss_index, "save_to_disk", None) or getattr(
            faiss_index, "save_index", None
        )
        if save is not None and removed:
            try:
                save()
            except TypeError:
                try:
                    save(None)
                except Exception as exc:
                    logger.debug("prune: FAISS save skipped: %s", exc)
            except Exception as exc:
                logger.debug("prune: FAISS save skipped: %s", exc)
    except Exception as exc:
        logger.debug("prune: live FAISS remove skipped: %s", exc)
        result["faiss_status"] = f"error:{exc}"
    return result


def prune_document_rows(
    db,
    doc_ids: Iterable[int],
    *,
    dry_run: bool = True,
    faiss_index: Any = None,
) -> Dict[str, Any]:
    """Delete documents + vault_fts + chunk_embeddings for the given doc_ids.

    Also tombstones matching chunk_ids out of the live FAISS index (when
    ``faiss_index`` is provided) and invalidates the rerank cache — the same
    side-effect path as ``RetrievalEngine.purge_durable_document``. Without a
    live index, SQLite deletes still change the FAISS disk-cache checksum so
    the next cold load rebuilds from remaining ``chunk_embeddings``.

    Does not touch ``learnings``. Idempotent for already-absent ids.
    """
    ids = sorted({int(x) for x in doc_ids})
    if not ids:
        return {
            "dry_run": dry_run,
            "requested": 0,
            "deleted": 0,
            "semantic": {"chunk_ids": 0, "faiss_status": "noop"},
        }

    if dry_run:
        return {
            "dry_run": True,
            "requested": len(ids),
            "deleted": 0,
            "doc_ids": ids,
            "semantic": {"chunk_ids": 0, "faiss_status": "dry_run"},
        }

    deleted = 0
    collected_chunk_ids: List[int] = []
    with db.transaction() as c:
        for doc_id in ids:
            c.execute("SELECT doc_id FROM documents WHERE doc_id=?", (doc_id,))
            if not c.fetchone():
                continue
            try:
                for row in c.execute(
                    "SELECT chunk_id FROM chunk_embeddings WHERE doc_id=?",
                    (doc_id,),
                ).fetchall():
                    cid = row["chunk_id"] if hasattr(row, "keys") else row[0]
                    collected_chunk_ids.append(int(cid))
            except Exception:
                # chunk_embeddings may be missing on partial schemas.
                pass
            c.execute("DELETE FROM vault_fts WHERE doc_id=?", (doc_id,))
            c.execute("DELETE FROM chunk_embeddings WHERE doc_id=?", (doc_id,))
            try:
                c.execute(
                    "DELETE FROM memory_links WHERE source_doc_id=? OR target_doc_id=?",
                    (doc_id, doc_id),
                )
            except Exception:
                pass  # table may not exist on older schemas
            c.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
            deleted += 1

    # Outside the txn: live-index maintenance is best-effort (parity with
    # purge_durable_document).
    semantic = _purge_semantic_side_effects(
        collected_chunk_ids, faiss_index=faiss_index
    )
    return {
        "dry_run": False,
        "requested": len(ids),
        "deleted": deleted,
        "semantic": semantic,
    }


def repair_index_disk_divergence(
    db,
    *,
    dry_run: bool = True,
    vault_roots: Optional[Sequence[str]] = None,
    force_prune_indexed: bool = False,
) -> Dict[str, Any]:
    """Prune safe-to-remove index rows; report virtual durable separately.

    Safe to remove:
      * non-virtual document paths missing on disk after vault-root resolution
        **and** without FTS/chunk content (unless ``force_prune_indexed``)
      * virtual ``_durable`` rows with neither ``vault_fts`` nor chunks

    NOT removed (by design):
      * virtual ``_durable`` / ``learning://`` / URI / ``page_type=learning``
        identities
      * any path (relative **or** absolute) that still has FTS or chunk content
        — unless ``force_prune_indexed=True``
    """
    missing_real = find_missing_document_rows(
        db,
        include_virtual_durable=False,
        vault_roots=vault_roots,
        force_prune_indexed=force_prune_indexed,
    )
    orphan_virtual = find_orphan_virtual_durable(db)
    # Healthy = virtual durable path that is not an orphan (has FTS and/or chunks).
    virtual_healthy = 0
    with db.cursor() as c:
        c.execute(
            r"""
            SELECT COUNT(*) AS n FROM documents d
            WHERE d.path LIKE '%/\_durable/%' ESCAPE '\'
            """
        )
        virtual_total = int(c.fetchone()["n"])
    virtual_healthy = max(0, virtual_total - len(orphan_virtual))

    to_prune = [r["doc_id"] for r in missing_real] + [
        r["doc_id"] for r in orphan_virtual
    ]
    prune_result = prune_document_rows(db, to_prune, dry_run=dry_run)

    return {
        "dry_run": dry_run,
        "missing_on_disk_non_virtual": len(missing_real),
        "orphan_virtual_durable": len(orphan_virtual),
        "healthy_virtual_durable_kept": virtual_healthy,
        "force_prune_indexed": force_prune_indexed,
        "virtual_durable_note": (
            "paths under /_durable/ and URI identities (learning://, etc.) "
            "are synthetic; missing files are expected. FTS/chunk-backed "
            "rows (relative or absolute) are never pruned unless "
            "--force-prune-indexed is set."
        ),
        "prune": prune_result,
        "sample_missing": [
            {"doc_id": r["doc_id"], "path": r["path"]} for r in missing_real[:5]
        ],
        "sample_orphan_virtual": [
            {"doc_id": r["doc_id"], "path": r["path"]} for r in orphan_virtual[:5]
        ],
    }


def _dry_run_inbox_dedup_index_status(
    db, *, dual_plan: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Preflight UNIQUE index without CREATE — exists / would_create / would_block.

    When ``dual_plan`` is provided (from a dry-run dual repair), residual
    collisions exclude ``loser_ids`` the plan would delete so dry-run matches
    post-apply index installability.
    """
    index_name = INBOX_DEDUP_INDEX
    with db.cursor() as c:
        c.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=? LIMIT 1",
            (index_name,),
        )
        row = c.fetchone()
        if row:
            sql = row["sql"] if hasattr(row, "keys") else row[0]
            if _inbox_dedup_index_sql_is_current(sql):
                return {"status": "exists", "index": index_name, "dry_run": True}
            # Stale global (file, index) or raw-principal index — would be
            # replaced on apply. Fall through to collision preflight.
    loser_ids: set = set()
    if dual_plan:
        # repair_duplicate_candidate_pairs dry-run exposes plan in ``sample``
        # (and full list is only partial); also accept ``plan`` if present.
        for g in dual_plan.get("plan") or dual_plan.get("sample") or []:
            for lid in g.get("delete") or g.get("loser_ids") or []:
                try:
                    loser_ids.add(int(lid))
                except (TypeError, ValueError):
                    continue
        # When sample is truncated, still use groups_found/would_delete only as
        # a heuristic: rebuild from find_duplicate if sample is incomplete.
        would_delete = int(dual_plan.get("would_delete") or 0)
        if would_delete and len(loser_ids) < would_delete:
            # Full plan not in summary — re-enumerate collapsible losers.
            for g in find_duplicate_candidate_groups(db):
                for lid in g.get("loser_ids") or []:
                    try:
                        loser_ids.add(int(lid))
                    except (TypeError, ValueError):
                        continue

    collisions = find_app_key_collisions(db)
    residual: List[Dict[str, Any]] = []
    for g in collisions:
        remaining = [
            cid for cid in g["candidate_ids"] if int(cid) not in loser_ids
        ]
        if len(remaining) >= 2:
            residual.append({**g, "candidate_ids": remaining, "row_count": len(remaining)})
    if residual:
        return {
            "status": "would_block",
            "index": index_name,
            "dry_run": True,
            "post_plan": bool(dual_plan),
            "app_key_collisions": len(residual),
            "pre_plan_collisions": len(collisions),
            "byte_identical_groups": sum(
                1 for g in residual if g.get("byte_identical")
            ),
            "divergent_groups": sum(
                1 for g in residual if not g.get("byte_identical")
            ),
            "sample": [
                {
                    "key": g["key"],
                    "row_count": g["row_count"],
                    "candidate_ids": g["candidate_ids"][:8],
                    "content_sha1s": g["content_sha1s"][:8],
                    "byte_identical": g["byte_identical"],
                }
                for g in residual[:5]
            ],
        }
    return {
        "status": "would_create",
        "index": index_name,
        "dry_run": True,
        "post_plan": bool(dual_plan),
        "pre_plan_collisions": len(collisions),
    }


def run_full_repair(
    db,
    *,
    dry_run: bool = True,
    create_index: bool = True,
    prune_index: bool = False,
    force_prune_indexed: bool = False,
    vault_roots: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Run dual-candidate repair (+ optional unique index / index prune).

    Default is dual-candidates only. Destructive document-index pruning
    requires ``prune_index=True`` (CLI: ``--prune-index``) so operators cannot
    accidentally wipe relative-path or offline-vault index rows while fixing
    dual-resolution twins. Even with prune enabled, FTS/chunk-backed rows are
    kept unless ``force_prune_indexed=True``.
    """
    dual = repair_duplicate_candidate_pairs(db, dry_run=dry_run)
    if prune_index:
        index: Dict[str, Any] = repair_index_disk_divergence(
            db,
            dry_run=dry_run,
            vault_roots=vault_roots,
            force_prune_indexed=force_prune_indexed,
        )
    else:
        index = {
            "dry_run": dry_run,
            "skipped": True,
            "reason": "prune_index not requested; pass prune_index=True / --prune-index",
            "missing_on_disk_non_virtual": 0,
            "orphan_virtual_durable": 0,
            "healthy_virtual_durable_kept": 0,
            "force_prune_indexed": force_prune_indexed,
            "virtual_durable_note": (
                "index prune skipped by default; dual-candidate repair does "
                "not touch documents/vault_fts/chunk_embeddings"
            ),
            "prune": {"dry_run": dry_run, "requested": 0, "deleted": 0, "skipped": True},
            "sample_missing": [],
            "sample_orphan_virtual": [],
        }
    index_status: Dict[str, Any] = {"status": "skipped"}
    if create_index and not dry_run:
        index_status = ensure_inbox_dedup_index(db)
    elif create_index and dry_run:
        # Preflight without mutating: project residual collisions *after*
        # the dual plan would delete losers so dry-run matches apply order.
        index_status = _dry_run_inbox_dedup_index_status(db, dual_plan=dual)
    return {
        "dual_candidates": dual,
        "index_disk": index,
        "inbox_dedup_index": index_status,
        "prune_index": prune_index,
        "force_prune_indexed": force_prune_indexed,
    }

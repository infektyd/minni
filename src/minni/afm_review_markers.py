"""Lifecycle for ``afm_review`` markers and the proposed-candidate queue (M5).

An ``afm_review`` row in ``consolidation_actions`` fences a candidate: while
one is active (``status != 'superseded'``) the consolidation drain refuses to
re-examine that candidate. The marker is keyed by ``claim = str(candidate_id)``.

The defect this module closes: every resolve path moved the candidate out of
``proposed`` without ever superseding its marker, so the fence outlived the
thing it fenced — 480 of 495 markers pointed at already-resolved candidates.
Nothing superseded them and nothing reported the count, so the drift was
invisible.

Two halves, deliberately separate:

* ``supersede_afm_review`` is called by the resolve paths so new orphans stop
  being created. It runs on the caller's cursor inside the caller's
  transaction, so a rolled-back resolve takes the marker change with it.
* ``reconcile_orphaned_afm_review`` drains the EXISTING backlog. It is
  operator-invoked via ``scripts/reconcile_afm_review.py`` and defaults to
  dry-run, because superseding a fence changes what consolidation will
  re-examine.

``count_orphaned_afm_review`` and ``proposed_queue_stats`` exist so the two
queues are *reportable*. A depth alone cannot distinguish a healthy queue from
a parked one, which is why the proposed queue sitting at a constant 15 read as
normal for months.
"""

from __future__ import annotations

import sqlite3
import time

from minni.timestamps import parse_epoch_or_report
from typing import Any, Dict, Optional

# A marker is active until it is explicitly superseded. NULL/'' count as
# active, matching every existing fence probe (afm.py, repair_dual_candidates).
_ACTIVE_TEMPLATE = "COALESCE({col}, '') != 'superseded'"

# Statuses that mean "still queued". Everything else is a resolution, so a
# marker pointing at it is an orphan.
PENDING_STATUS = "proposed"


def _active(col: str = "status") -> str:
    """The active-marker predicate for a given (optionally qualified) column.

    Built from an explicit column name rather than by string-replacing into a
    finished SQL fragment: that trick only worked because 'superseded' happens
    not to contain the word it was replacing.
    """
    return _ACTIVE_TEMPLATE.format(col=col)


def _is_missing_table(exc: sqlite3.Error) -> bool:
    """True only for 'no such table'.

    Pre-014 installs genuinely lack ``consolidation_actions``, and that is not
    a fault. Every OTHER database error is real and must be raised rather than
    laundered into a confident zero — a silent zero from a broken query is the
    same health overstatement this module exists to remove. (It already bit
    once: a query against a non-existent column reported an empty queue while
    15 stale candidates sat in it.)
    """
    return "no such table" in str(exc).lower()


def supersede_afm_review(cursor: Any, candidate_id: Any) -> int:
    """Supersede the active ``afm_review`` marker(s) for one candidate.

    Returns how many markers were superseded (0 when there was none, the
    common case — most candidates are never fenced).
    """
    try:
        cursor.execute(
            f"""
            UPDATE consolidation_actions
            SET status = 'superseded'
            WHERE action_type = 'afm_review'
              AND claim = ?
              AND {_active()}
            """,
            (str(candidate_id),),
        )
    except sqlite3.Error as exc:
        if _is_missing_table(exc):
            return 0
        raise
    return max(int(cursor.rowcount or 0), 0)


def count_orphaned_afm_review(cursor: Any) -> int:
    """Active markers whose candidate is no longer queued (or is gone)."""
    try:
        cursor.execute(
            f"""
            SELECT COUNT(*) FROM consolidation_actions ca
            WHERE ca.action_type = 'afm_review'
              AND {_active('ca.status')}
              AND NOT EXISTS (
                    SELECT 1 FROM candidate_packets cp
                    WHERE CAST(cp.candidate_id AS TEXT) = ca.claim
                      AND cp.status = ?
              )
            """,
            (PENDING_STATUS,),
        )
    except sqlite3.Error as exc:
        if _is_missing_table(exc):
            return 0
        raise
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def reconcile_orphaned_afm_review(cursor: Any, *, dry_run: bool = True) -> Dict[str, Any]:
    """Supersede every marker whose candidate is resolved or absent.

    A marker for a still-``proposed`` candidate is a live fence and is left
    alone — disarming one would let consolidation re-examine something an
    operator deliberately parked.
    """
    orphans = count_orphaned_afm_review(cursor)
    if dry_run:
        return {"dry_run": True, "would_supersede": orphans, "superseded": 0}
    if not orphans:
        return {"dry_run": False, "would_supersede": 0, "superseded": 0}
    try:
        cursor.execute(
            f"""
            UPDATE consolidation_actions
            SET status = 'superseded'
            WHERE action_type = 'afm_review'
              AND {_active()}
              AND NOT EXISTS (
                    SELECT 1 FROM candidate_packets cp
                    WHERE CAST(cp.candidate_id AS TEXT) = consolidation_actions.claim
                      AND cp.status = ?
              )
            """,
            (PENDING_STATUS,),
        )
    except sqlite3.Error as exc:
        if not _is_missing_table(exc):
            raise
        return {"dry_run": False, "would_supersede": orphans, "superseded": 0}
    return {
        "dry_run": False,
        "would_supersede": orphans,
        "superseded": max(int(cursor.rowcount or 0), 0),
    }


def proposed_queue_stats(cursor: Any, *, stale_after_days: float = 14.0) -> Dict[str, Any]:
    """Depth, oldest age and stale count for the proposed-candidate queue.

    The age column is ``proposed_at`` (007_candidate_packets.sql) — there is no
    ``created_at`` on this table.

    Reporting only: nothing here expires or escalates a candidate. Auto-expiry
    would silently discard operator-visible proposals, so the aging policy is
    to make staleness *visible* and leave the decision to an operator.
    """
    empty: Dict[str, Any] = {
        "depth": 0,
        "oldest_age_days": None,
        "stale": 0,
        "unparseable_proposed_at": 0,
        "stale_after_days": stale_after_days,
    }
    now = time.time()
    try:
        cursor.execute(
            "SELECT COUNT(*), MIN(proposed_at) FROM candidate_packets WHERE status = ?",
            (PENDING_STATUS,),
        )
    except sqlite3.Error as exc:
        if _is_missing_table(exc):
            return empty
        raise
    row = cursor.fetchone()
    if not row or not row[0]:
        return empty
    depth = int(row[0])

    # REAL is an affinity, not a constraint: a non-numeric string stays TEXT.
    # float() on one raised ValueError straight out of the operator drain
    # (which reports before it reconciles), and `proposed_at < ?` never
    # matches a TEXT row because SQLite sorts text above every number — so a
    # poisoned timestamp both crashed the drain and hid staleness. Read the
    # ages in Python through the same parser the indexers use, and count the
    # unparseable ones instead of dropping them.
    cursor.execute(
        "SELECT proposed_at FROM candidate_packets WHERE status = ?",
        (PENDING_STATUS,),
    )
    ages: list[float] = []
    unparseable = 0
    for (raw,) in cursor.fetchall():
        parsed = parse_epoch_or_report(
            raw, field="proposed_at", source="afm_review_markers.proposed_queue_stats",
        )
        if parsed is None:
            unparseable += 1
            continue
        ages.append(max((now - float(parsed)) / 86400.0, 0.0))

    stale_days = stale_after_days
    return {
        "depth": depth,
        "oldest_age_days": max(ages) if ages else None,
        "stale": sum(1 for age in ages if age > stale_days),
        "unparseable_proposed_at": unparseable,
        "stale_after_days": stale_after_days,
    }

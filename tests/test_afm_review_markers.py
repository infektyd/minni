"""M5 (#229): an ``afm_review`` marker must not outlive the candidate it fences.

Markers are keyed by ``claim = str(candidate_id)`` and are "active" while
status != 'superseded'. Every resolve path left the candidate resolved and the
marker active, so the fence outlived its subject: 480 of 495 markers pointed
at already-resolved candidates. Nothing superseded them and nothing reported
the orphan count, so the drift was invisible.

The proposed queue has the mirror problem: a constant depth with no aging
policy — nothing expired, escalated, or reported staleness.

Every fixture here builds the schema with ``run_migrations``. The first
version of this module hand-rolled a ``candidate_packets`` with a
``created_at`` column that exists nowhere in the product; the tests passed and
the health surface reported a confident empty queue while stale candidates sat
in it. A fictional fixture is how a query against a non-existent column ships.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minni.afm_review_markers import (
    count_orphaned_afm_review,
    proposed_queue_stats,
    reconcile_orphaned_afm_review,
    supersede_afm_review,
)

DAY = 86400.0
REPO = Path(__file__).resolve().parent.parent


def _conn() -> sqlite3.Connection:
    """Real product schema, built the way the product builds it."""
    from minni.migrations import run_migrations

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    return conn


def _candidate(conn, cid: int, status: str = "proposed", *, age_days: float = 0.0):
    conn.execute(
        """INSERT INTO candidate_packets
           (candidate_id, principal, content, status, proposed_at)
           VALUES (?, 'test', 'content', ?, ?)""",
        (cid, status, time.time() - age_days * DAY),
    )


def _marker(conn, cid: int, status: str = "pending"):
    conn.execute(
        """INSERT INTO consolidation_actions
           (action_type, claim, category, status, created_at)
           VALUES ('afm_review', ?, 'general', ?, ?)""",
        (str(cid), status, time.time()),
    )


def _marker_status(conn, cid: int) -> str:
    row = conn.execute(
        "SELECT status FROM consolidation_actions WHERE action_type='afm_review' AND claim=?",
        (str(cid),),
    ).fetchone()
    return row["status"]


# ── the schema this module queries actually exists ─────────────────────────


def test_candidate_packets_has_the_age_column_we_query():
    """Guards the exact drift that shipped a fabricated `depth: 0`: the age
    column is `proposed_at`, and there is no `created_at`."""
    conn = _conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(candidate_packets)")}
    assert "proposed_at" in cols
    assert "created_at" not in cols


def test_proposed_queue_reports_a_real_backlog_not_a_zero():
    """A wrong column name raised OperationalError, which a broad
    `except sqlite3.Error: return empty` turned into a confident healthy zero."""
    conn = _conn()
    for cid in range(1, 16):
        _candidate(conn, cid, "proposed", age_days=40.0)
    stats = proposed_queue_stats(conn.cursor(), stale_after_days=14.0)
    assert stats["depth"] == 15
    assert stats["stale"] == 15
    assert stats["oldest_age_days"] == pytest.approx(40.0, abs=0.1)


def test_an_unexpected_db_error_is_not_laundered_into_zero():
    """Only a missing table is excused. Anything else must surface."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE candidate_packets (candidate_id INTEGER, status TEXT)")
    with pytest.raises(sqlite3.Error):
        proposed_queue_stats(conn.cursor())


# ── superseding on resolve ─────────────────────────────────────────────────


def test_supersede_marks_the_active_marker():
    conn = _conn()
    _candidate(conn, 1, "accepted")
    _marker(conn, 1)
    assert supersede_afm_review(conn.cursor(), 1) == 1
    assert _marker_status(conn, 1) == "superseded"


def test_supersede_is_idempotent():
    conn = _conn()
    _candidate(conn, 1, "accepted")
    _marker(conn, 1)
    supersede_afm_review(conn.cursor(), 1)
    assert supersede_afm_review(conn.cursor(), 1) == 0


def test_supersede_touches_only_its_own_candidate():
    conn = _conn()
    for cid in (1, 2):
        _candidate(conn, cid, "accepted")
        _marker(conn, cid)
    supersede_afm_review(conn.cursor(), 1)
    assert _marker_status(conn, 2) == "pending"


def test_supersede_survives_a_missing_table():
    """Pre-014 installs have no consolidation_actions; a marker sweep must not
    break the resolve that carries it."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert supersede_afm_review(conn.cursor(), 1) == 0


# ── reconcile: the existing orphan backlog ─────────────────────────────────


def test_reconcile_supersedes_markers_whose_candidate_is_resolved():
    conn = _conn()
    _candidate(conn, 1, "accepted")
    _marker(conn, 1)
    _candidate(conn, 2, "rejected")
    _marker(conn, 2)
    result = reconcile_orphaned_afm_review(conn.cursor(), dry_run=False)
    assert result["superseded"] == 2
    assert _marker_status(conn, 1) == "superseded"


def test_reconcile_leaves_markers_for_still_proposed_candidates():
    """A live fence is load-bearing — reconcile must not disarm it."""
    conn = _conn()
    _candidate(conn, 1, "proposed")
    _marker(conn, 1)
    result = reconcile_orphaned_afm_review(conn.cursor(), dry_run=False)
    assert result["superseded"] == 0
    assert _marker_status(conn, 1) == "pending"


def test_reconcile_supersedes_a_marker_with_no_candidate_at_all():
    conn = _conn()
    _marker(conn, 99)
    assert reconcile_orphaned_afm_review(conn.cursor(), dry_run=False)["superseded"] == 1


def test_reconcile_dry_run_changes_nothing():
    conn = _conn()
    _candidate(conn, 1, "accepted")
    _marker(conn, 1)
    result = reconcile_orphaned_afm_review(conn.cursor(), dry_run=True)
    assert result["would_supersede"] == 1
    assert _marker_status(conn, 1) == "pending"


def test_reconcile_is_idempotent():
    conn = _conn()
    _candidate(conn, 1, "accepted")
    _marker(conn, 1)
    reconcile_orphaned_afm_review(conn.cursor(), dry_run=False)
    assert reconcile_orphaned_afm_review(conn.cursor(), dry_run=False)["superseded"] == 0


# ── the counts are reportable, not just fixable ────────────────────────────


def test_orphan_count_is_observable():
    conn = _conn()
    _candidate(conn, 1, "accepted")
    _marker(conn, 1)
    _candidate(conn, 2, "proposed")
    _marker(conn, 2)
    assert count_orphaned_afm_review(conn.cursor()) == 1


def test_orphan_count_is_zero_on_a_healthy_system():
    conn = _conn()
    _candidate(conn, 1, "proposed")
    _marker(conn, 1)
    assert count_orphaned_afm_review(conn.cursor()) == 0


def test_orphan_count_survives_a_missing_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert count_orphaned_afm_review(conn.cursor()) == 0


def test_proposed_queue_counts_only_stale_entries():
    conn = _conn()
    _candidate(conn, 1, "proposed", age_days=30.0)
    _candidate(conn, 2, "proposed", age_days=1.0)
    stats = proposed_queue_stats(conn.cursor(), stale_after_days=14.0)
    assert stats["depth"] == 2
    assert stats["stale"] == 1


def test_proposed_queue_empty_is_honest():
    conn = _conn()
    stats = proposed_queue_stats(conn.cursor(), stale_after_days=14.0)
    assert stats["depth"] == 0
    assert stats["oldest_age_days"] is None
    assert stats["stale"] == 0


# ── end-to-end: the real resolve paths retire the fence ────────────────────


def _reset_minnid_caches() -> None:
    """minnid caches writeback/retrieval/episodic handles at module level,
    bound to whatever db_path was live when they were built. Driving the real
    handlers against a temp DB must clear them on the way in AND out, or this
    module leaves a stale handle pointed at a deleted tmp DB and the next test
    file to stage a candidate silently reads the wrong database."""
    import minni.minnid as minnid

    minnid._writeback = None
    minnid._episodic = None
    minnid._retrieval = None
    if hasattr(minnid, "_vault_retrieval_cache"):
        try:
            minnid._vault_retrieval_cache.clear()
        except Exception:
            minnid._vault_retrieval_cache = {}


def _resolve_env(tmp_path):
    import minni.config as cfg_mod
    from minni.migrations import run_migrations

    db_path = str(tmp_path / "markers.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.close()
    old = cfg_mod.DEFAULT_CONFIG.db_path
    cfg_mod.DEFAULT_CONFIG.db_path = db_path
    _reset_minnid_caches()
    return db_path, cfg_mod, old


def _add_marker(db_path: str, cid: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO consolidation_actions
           (action_type, claim, category, status, created_at)
           VALUES ('afm_review', ?, 'general', 'pending', ?)""",
        (str(cid), time.time()),
    )
    conn.commit()
    conn.close()


def _read_marker(db_path: str, cid: int) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM consolidation_actions WHERE action_type='afm_review' AND claim=?",
        (str(cid),),
    ).fetchone()
    conn.close()
    return row["status"]


def test_operator_resolve_supersedes_the_review_marker(tmp_path):
    """The real _resolve_candidate path, not a stub."""
    from minni.minnid import _resolve_candidate, _stage_candidate

    db_path, cfg_mod, old = _resolve_env(tmp_path)
    try:
        cid = _stage_candidate(
            {"content": "A candidate that carries a review fence", "workspace_id": "default"}, 1,
        )["result"]["candidate_id"]
        _add_marker(db_path, cid)

        resolved = _resolve_candidate(
            {"candidate_id": cid, "decision": "accept", "reason": "marker test"}, 2,
        )
        assert resolved["result"]["new_status"] == "accepted"
        assert _read_marker(db_path, cid) == "superseded"
    finally:
        cfg_mod.DEFAULT_CONFIG.db_path = old
        _reset_minnid_caches()


def test_resolve_leaves_another_candidates_fence_alone(tmp_path):
    from minni.minnid import _resolve_candidate, _stage_candidate

    db_path, cfg_mod, old = _resolve_env(tmp_path)
    try:
        a = _stage_candidate({"content": "First fenced candidate here", "workspace_id": "default"}, 1)
        b = _stage_candidate({"content": "Second fenced candidate here", "workspace_id": "default"}, 2)
        cid_a = a["result"]["candidate_id"]
        cid_b = b["result"]["candidate_id"]
        _add_marker(db_path, cid_a)
        _add_marker(db_path, cid_b)

        _resolve_candidate({"candidate_id": cid_a, "decision": "accept", "reason": "x"}, 3)

        assert _read_marker(db_path, cid_b) == "pending"
    finally:
        cfg_mod.DEFAULT_CONFIG.db_path = old
        _reset_minnid_caches()


def _afm_context(db_obj):
    """Minimal AFMContext: these two paths only use lazy_writeback."""
    import types

    from minni.minnid_runtime.afm import AFMContext

    wb = types.SimpleNamespace(db=db_obj, model=None)
    return AFMContext(
        make_error=lambda *a: {"error": a},
        make_response=lambda r, i=None: {"result": r},
        guard_vault_root=lambda *a, **k: None,
        lazy_writeback=lambda: wb,
        trace_ring=lambda: None,
        record_latency=lambda *a: None,
        maybe_archive_inbox_source=lambda *a, **k: None,
    )


def _db_with_fenced_candidate(tmp_path, cid: int = 1):
    """Real SovereignDB with one fenced, proposed candidate.

    Reuses the AFM loop suite's fixture so the schema is exactly what those
    code paths run against — the learnings table in particular.
    """
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_afm_loop_promotion import _make_db as _make_loop_db

    db_obj, _cfg = _make_loop_db(tmp_path)
    with db_obj.cursor() as c:
        # promote_candidate_durable writes learnings.content_hash, which no
        # migration creates (installs acquire it out-of-band; see
        # unpark_privacy_backlog, which probes for it). Out of scope for #229,
        # but the column has to exist for the accept path to run at all.
        cols = {r[1] for r in c.execute("PRAGMA table_info(learnings)")}
        if "content_hash" not in cols:
            c.execute("ALTER TABLE learnings ADD COLUMN content_hash TEXT")
        c.execute(
            """INSERT INTO candidate_packets
               (candidate_id, principal, privacy_level, content, status, proposed_at)
               VALUES (?, 'test', 'safe', 'a durable lesson worth keeping', 'proposed', ?)""",
            (cid, time.time()),
        )
        c.execute(
            """INSERT INTO consolidation_actions
               (action_type, claim, category, status, created_at)
               VALUES ('afm_review', ?, 'general', 'pending', ?)""",
            (str(cid), time.time()),
        )
    return db_obj


def _fence_status(db_obj, cid: int) -> str:
    with db_obj.cursor() as c:
        c.execute(
            "SELECT status FROM consolidation_actions WHERE action_type='afm_review' AND claim=?",
            (str(cid),),
        )
        return c.fetchone()[0]


def test_afm_accept_path_retires_the_fence(tmp_path):
    """Drives the real promote_candidate_durable. Covered only by identity
    assertions before, so a mutant deleting the call survived."""
    from minni.minnid_runtime.afm import promote_candidate_durable

    db_obj = _db_with_fenced_candidate(tmp_path, 1)
    promote_candidate_durable(1, "consolidation accepted it", _afm_context(db_obj))

    assert _fence_status(db_obj, 1) == "superseded"


def test_afm_dedup_reject_path_retires_the_fence(tmp_path):
    """Drives the real reject_candidate_dedup."""
    from minni.minnid_runtime.afm import reject_candidate_dedup

    db_obj = _db_with_fenced_candidate(tmp_path, 2)
    from minni.afm_passes.consolidation import content_hash

    content = "a durable lesson worth keeping"
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO learnings
               (agent_id, content, content_hash, created_at, status)
               VALUES ('test', ?, ?, ?, 'active')""",
            (content, content_hash(content), time.time()),
        )
    assert reject_candidate_dedup(2, _afm_context(db_obj)) is True

    assert _fence_status(db_obj, 2) == "superseded"


# ── the backlog has an operator drain path ─────────────────────────────────


def test_reconcile_cli_defaults_to_dry_run(tmp_path):
    """reconcile existed as a function with no caller, so the 480-orphan
    backlog still had no drain path."""
    from minni.migrations import run_migrations

    db_path = tmp_path / "cli.db"
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    conn.execute(
        """INSERT INTO candidate_packets
           (candidate_id, principal, content, status, proposed_at)
           VALUES (1, 'test', 'c', 'accepted', ?)""",
        (time.time(),),
    )
    conn.execute(
        """INSERT INTO consolidation_actions
           (action_type, claim, category, status, created_at)
           VALUES ('afm_review', '1', 'general', 'pending', ?)""",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    script = REPO / "scripts" / "reconcile_afm_review.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    # Pin the FLAG itself, not only the absence of a write: the commit guard
    # also suppresses the write, so asserting "still pending" alone let a
    # mutant that flips the default to --apply pass unnoticed.
    report = json.loads(proc.stdout)
    assert report["dry_run"] is True
    assert report["would_supersede"] == 1
    assert report["superseded"] == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    status = conn.execute(
        "SELECT status FROM consolidation_actions WHERE claim='1'"
    ).fetchone()["status"]
    conn.close()
    assert status == "pending", "default run must not mutate"


def test_reconcile_cli_applies_when_asked(tmp_path):
    from minni.migrations import run_migrations

    db_path = tmp_path / "cli2.db"
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    conn.execute(
        """INSERT INTO candidate_packets
           (candidate_id, principal, content, status, proposed_at)
           VALUES (1, 'test', 'c', 'accepted', ?)""",
        (time.time(),),
    )
    conn.execute(
        """INSERT INTO consolidation_actions
           (action_type, claim, category, status, created_at)
           VALUES ('afm_review', '1', 'general', 'pending', ?)""",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    script = REPO / "scripts" / "reconcile_afm_review.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--apply"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    status = conn.execute(
        "SELECT status FROM consolidation_actions WHERE claim='1'"
    ).fetchone()["status"]
    conn.close()
    assert status == "superseded"


# ── round 2: poisoned timestamps must not crash or hide staleness ──────────


def test_a_text_proposed_at_does_not_crash_the_report():
    """REAL is an affinity, not a constraint: a non-numeric string stays TEXT.
    float() on one raised straight out of the operator drain, which reports
    before it reconciles — so one poisoned row disarmed the whole drain.

    An ISO string is recovered (parse_epoch_or_report understands it); only a
    genuinely unreadable value is counted separately, never silently dropped.
    """
    conn = _conn()
    _candidate(conn, 1, "proposed", age_days=40.0)
    conn.execute(
        "UPDATE candidate_packets SET proposed_at='banana' WHERE candidate_id=1"
    )
    _candidate(conn, 2, "proposed", age_days=30.0)

    stats = proposed_queue_stats(conn.cursor(), stale_after_days=14.0)

    assert stats["depth"] == 2, "an unreadable row is still IN the queue"
    assert stats["unparseable_proposed_at"] == 1
    assert stats["stale"] == 1
    assert stats["oldest_age_days"] == pytest.approx(30.0, abs=0.1)


def test_an_iso_proposed_at_is_recovered_not_discarded():
    """SQLite sorts TEXT above every number, so `proposed_at < ?` never
    matched a TEXT row — an ISO-stamped candidate could never read as stale."""
    conn = _conn()
    _candidate(conn, 1, "proposed", age_days=0.0)
    conn.execute(
        "UPDATE candidate_packets SET proposed_at='2020-01-01T00:00:00Z' WHERE candidate_id=1"
    )

    stats = proposed_queue_stats(conn.cursor(), stale_after_days=14.0)

    assert stats["unparseable_proposed_at"] == 0
    assert stats["stale"] == 1
    assert stats["oldest_age_days"] > 365


def test_a_poisoned_timestamp_does_not_disarm_the_drain(tmp_path):
    """End-to-end through the CLI: the queue report is advisory and must never
    block the reconcile printed next to it."""
    from minni.migrations import run_migrations

    db_path = tmp_path / "poison.db"
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    conn.execute(
        """INSERT INTO candidate_packets
           (candidate_id, principal, content, status, proposed_at)
           VALUES (1, 'test', 'c', 'proposed', '2026-01-01T00:00:00Z')"""
    )
    conn.execute(
        """INSERT INTO candidate_packets
           (candidate_id, principal, content, status, proposed_at)
           VALUES (2, 'test', 'c', 'accepted', ?)""",
        (time.time(),),
    )
    conn.execute(
        """INSERT INTO consolidation_actions
           (action_type, claim, category, status, created_at)
           VALUES ('afm_review', '2', 'general', 'pending', ?)""",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "reconcile_afm_review.py"),
         "--db", str(db_path), "--apply"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    status = conn.execute(
        "SELECT status FROM consolidation_actions WHERE claim='2'"
    ).fetchone()["status"]
    conn.close()
    assert status == "superseded", "the orphan must still be drained"

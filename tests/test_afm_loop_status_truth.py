"""Audit R1: afm_loop.status must be derived, never asserted.

Until 2026-08-01 ``afm_writer.writer_status`` returned the literal
``"status": "ok"``, and ``handle_health_report`` copied that block wholesale.
The live daemon reported

    {"last_run_per_pass": {}, "drafts_pending": 1205,
     "drafts_pending_oldest": null, "status": "ok", "queue_depth": 0}

-- 1,205 drafts pending, no pass on record, ages unknown, and the one field an
operator reads said ok.
"""

from __future__ import annotations

import time

import pytest

from minni.afm_writer import (
    DRAFTS_PENDING_BACKLOG,
    STALE_INTERVAL_MULTIPLE,
    derive_loop_status,
)

DAY = 24 * 60 * 60
SCHEDULE = {
    "draft_ttl_days": 14,
    "passes": {
        "session_distillation": {"interval_seconds": DAY},
        "consolidation": {"interval_seconds": 15 * 60},
    },
}
NOW = 1_800_000_000.0


def _state(**over):
    base = {
        "last_run_per_pass": {"session_distillation": NOW - 60, "consolidation": NOW - 60},
        "last_attempt_per_pass": {},
        "drafts_pending": 0,
        "drafts_pending_oldest": None,
        "drafts_pending_undated": 0,
        "drafts_unreadable": 0,
    }
    base.update(over)
    return base


def test_healthy_loop_is_ok():
    status, reasons = derive_loop_status(_state(), schedule=SCHEDULE, now=NOW)
    assert status == "ok"
    assert reasons == []


def test_the_live_shape_that_used_to_read_ok_does_not():
    """The exact live payload from the bug report."""
    status, reasons = derive_loop_status(
        _state(last_run_per_pass={}, drafts_pending=1205, drafts_pending_oldest=None),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status != "ok"
    assert status == "backlogged"
    assert any("1205 draft(s) pending" in r for r in reasons)
    assert any("no run on record" in r for r in reasons)


def test_a_pass_silent_past_its_interval_is_stale():
    status, reasons = derive_loop_status(
        _state(last_run_per_pass={
            "session_distillation": NOW - (DAY * (STALE_INTERVAL_MULTIPLE + 1)),
            "consolidation": NOW - 60,
        }),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status == "stale"
    assert any("session_distillation" in r for r in reasons)


def test_jitter_inside_the_multiple_is_not_stale():
    """A tick landing late is not a fault; a threshold that fires on jitter is
    ignored, which is the failure mode this change exists to remove."""
    status, _ = derive_loop_status(
        _state(last_run_per_pass={
            "session_distillation": NOW - int(DAY * 1.5),
            "consolidation": NOW - 60,
        }),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status == "ok"


def test_a_pass_with_no_evidence_is_unknown_not_ok():
    status, reasons = derive_loop_status(
        _state(last_run_per_pass={"consolidation": NOW - 60}),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status == "unknown"
    assert any("session_distillation" in r for r in reasons)


def test_an_attempt_counts_as_evidence_even_with_no_drafts_written():
    """A pass that ran and found nothing to distill never lands in
    last_run_per_pass. Without last_attempt_per_pass it would read as silent
    forever, and an alarm that always fires teaches operators to ignore it."""
    status, _ = derive_loop_status(
        _state(
            last_run_per_pass={"consolidation": NOW - 60},
            last_attempt_per_pass={"session_distillation": NOW - 60},
        ),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status == "ok"


def test_backlog_depth_is_reported():
    status, reasons = derive_loop_status(
        _state(drafts_pending=DRAFTS_PENDING_BACKLOG),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status == "backlogged"
    assert any("backlog threshold" in r for r in reasons)


def test_a_draft_older_than_the_ttl_means_expiry_is_not_running():
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 30 * DAY))
    status, reasons = derive_loop_status(
        _state(drafts_pending=3, drafts_pending_oldest=old),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status == "backlogged"
    assert any("TTL" in r for r in reasons)


def test_undated_drafts_are_reported_not_ignored():
    status, reasons = derive_loop_status(
        _state(drafts_pending=5, drafts_pending_oldest=None),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status == "unknown"
    assert any("age unknown" in r for r in reasons)


def test_unreadable_vault_pages_are_reported_not_assumed_clean():
    status, reasons = derive_loop_status(_state(drafts_unreadable=2), schedule=SCHEDULE, now=NOW)
    assert status == "unknown"
    assert any("could not be read" in r for r in reasons)


def test_stale_outranks_backlogged():
    status, _ = derive_loop_status(
        _state(
            last_run_per_pass={"session_distillation": NOW - 10 * DAY, "consolidation": NOW - 60},
            drafts_pending=5000,
        ),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert status == "stale"


def test_status_reasons_never_leak_paths():
    _, reasons = derive_loop_status(
        _state(last_run_per_pass={}, drafts_pending=1205, drafts_unreadable=3),
        schedule=SCHEDULE,
        now=NOW,
    )
    assert reasons
    assert not any("/" in r and ("Users" in r or "home" in r) for r in reasons)


def test_writer_status_derives_rather_than_asserts(tmp_path, monkeypatch):
    """End to end through the real writer_status: an empty vault with a loop
    whose passes have never run must not come back ok."""
    import minni.afm_writer as afm_writer

    monkeypatch.setattr(afm_writer, "_LAST_RUN_PER_PASS", {}, raising=False)
    monkeypatch.setattr(afm_writer, "_LAST_ATTEMPT_PER_PASS", {}, raising=False)
    status = afm_writer.writer_status(str(tmp_path), schedule=SCHEDULE)
    assert status["status"] == "unknown"
    assert status["status_reasons"]
    assert "last_attempt_per_pass" in status


def test_quoted_created_dates_are_read(tmp_path, monkeypatch):
    """yaml.safe_dump writes `created: '2026-06-20T00:07:39Z'`. The old pattern
    required an unquoted value, so drafts_pending_oldest was null on a vault of
    1,210 dated drafts and the age threshold could never fire."""
    import minni.afm_writer as afm_writer

    monkeypatch.setattr(afm_writer, "_LAST_RUN_PER_PASS", {}, raising=False)
    monkeypatch.setattr(afm_writer, "_LAST_ATTEMPT_PER_PASS", {}, raising=False)
    page = tmp_path / "wiki" / "sessions" / "d.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\nstatus: draft\nagent: afm-loop\n"
        "created: '2026-06-20T00:07:39Z'\nexpires_at: '2026-07-04T00:07:39Z'\n---\n",
        encoding="utf-8",
    )

    status = afm_writer.writer_status(str(tmp_path), schedule=SCHEDULE)
    assert status["drafts_pending"] == 1
    assert status["drafts_pending_oldest"] == "2026-06-20T00:07:39Z"
    assert status["drafts_pending_undated"] == 0
    assert any("TTL" in r for r in status["status_reasons"]), status["status_reasons"]
    assert status["status"] == "backlogged"


def test_body_text_containing_created_is_not_read_as_the_date(tmp_path, monkeypatch):
    """A draft's free-form body may itself contain the substring "created:"
    (e.g. quoting another page's frontmatter). Only the draft's own frontmatter
    block may be read for its date."""
    import minni.afm_writer as afm_writer

    monkeypatch.setattr(afm_writer, "_LAST_RUN_PER_PASS", {}, raising=False)
    monkeypatch.setattr(afm_writer, "_LAST_ATTEMPT_PER_PASS", {}, raising=False)
    page = tmp_path / "wiki" / "sessions" / "d.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\nstatus: draft\nagent: afm-loop\ncreated: '2026-07-30T00:00:00Z'\n---\n"
        "See the other page's frontmatter: created: '2020-01-01T00:00:00Z'\n",
        encoding="utf-8",
    )

    status = afm_writer.writer_status(str(tmp_path), schedule=SCHEDULE)
    assert status["drafts_pending_oldest"] == "2026-07-30T00:00:00Z"


def test_unparseable_created_value_counts_as_undated(tmp_path, monkeypatch):
    """A millisecond-precision timestamp (or any value _parse_iso_utc rejects)
    must not be stuffed into drafts_pending_oldest: it would neither trigger
    the age/TTL logic (which needs a parseable value) nor the "age unknown"
    reason (which only fires when the field is None)."""
    import minni.afm_writer as afm_writer

    monkeypatch.setattr(afm_writer, "_LAST_RUN_PER_PASS", {}, raising=False)
    monkeypatch.setattr(afm_writer, "_LAST_ATTEMPT_PER_PASS", {}, raising=False)
    page = tmp_path / "wiki" / "sessions" / "d.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\nstatus: draft\nagent: afm-loop\ncreated: '2026-07-30T00:00:00.000Z'\n---\n",
        encoding="utf-8",
    )

    status = afm_writer.writer_status(str(tmp_path), schedule=SCHEDULE)
    assert status["drafts_pending_oldest"] is None
    assert status["drafts_pending_undated"] == 1
    assert any("age unknown" in r for r in status["status_reasons"]), status["status_reasons"]


def test_record_pass_attempt_moves_the_needle(monkeypatch):
    import minni.afm_writer as afm_writer

    monkeypatch.setattr(afm_writer, "_LAST_ATTEMPT_PER_PASS", {}, raising=False)
    afm_writer.record_pass_attempt("synthesis", now=NOW)
    assert afm_writer._LAST_ATTEMPT_PER_PASS["synthesis"] == NOW


@pytest.mark.parametrize("empty", [{}, {"passes": {}}])
def test_no_configured_passes_is_not_a_false_alarm(empty):
    status, reasons = derive_loop_status(_state(), schedule=empty, now=NOW)
    assert status == "ok"
    assert reasons == []


def test_schedule_defaults_to_config_when_not_passed():
    """No schedule argument must not mean 'no passes to judge against' — that
    would be a silent narrowing of what is inspected."""
    from minni.config import DEFAULT_CONFIG

    configured = set((DEFAULT_CONFIG.afm_loop_schedule or {}).get("passes") or {})
    assert configured, "config carries pass intervals; the fallback is load-bearing"
    status, reasons = derive_loop_status(
        _state(last_run_per_pass={}, last_attempt_per_pass={}), now=NOW
    )
    assert status != "ok"
    assert any(name in " ".join(reasons) for name in configured)

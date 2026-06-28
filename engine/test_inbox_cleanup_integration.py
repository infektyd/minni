"""Integration test for scripts/inbox_cleanup.py against a REAL fixture ingest
(C7 follow-up to the unit tests in test_inbox_cleanup.py, which fabricate the
candidate_packets rows by hand).

End-to-end shape:
  fixture vault inbox files
    -> afm_passes.inbox_ingest.ingest() writes REAL candidate_packets rows
       (real migrated DB, real derived_from + content_sha1 fingerprints)
    -> one row is resolved to a terminal status (the legacy backlog shape the
       operator script exists for: rows resolved before drain-on-resolution)
    -> inbox_cleanup.main() — the actual CLI command path —
       default (no flag)  => dry-run, NOTHING moves
       --apply            => archived-not-deleted, DB/file state consistent.

Fixtures/tmpdirs only; the script and the DB row assertions prove the cleanup
never deletes a file and never writes the DB. inbox_cleanup is NEVER run
against live vaults here — --home/--db always point inside tmp_path.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import inbox_cleanup  # noqa: E402
from test_inbox_ingest import _make_db, _stop_doc, _write_inbox_file  # noqa: E402


def _db_rows(db_obj):
    with db_obj.cursor() as c:
        c.execute(
            "SELECT candidate_id, principal, status, content, derived_from "
            "FROM candidate_packets ORDER BY candidate_id"
        )
        return [dict(r) for r in c.fetchall()]


def _build_ingested_fixture(tmp_path):
    """Fixture home with one vault inbox, REALLY ingested into a real DB."""
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"

    # 1) Stop file whose candidate will be RESOLVED -> ingested_resolved.
    _write_inbox_file(inbox, "2026-05-01-resolved.json", _stop_doc(["lesson fully resolved"], agent_id="claude-code"))
    # 2) Stop file whose candidate stays proposed -> ingested (rows carry it).
    _write_inbox_file(inbox, "2026-05-02-pending.json", _stop_doc(["lesson still proposed"], agent_id="claude-code"))
    # 3) Stop file written AFTER the ingest pass (not in DB) -> keep.
    # 4) Aged orphan handoff (no requires_ack) -> expired_handoff.
    _write_inbox_file(
        inbox,
        "20260420T120000Z-orphan-handoff.json",
        {"kind": "handoff", "task": "orphaned handoff"},
    )
    # 5) Fresh handoff -> keep.
    _write_inbox_file(
        inbox,
        "20260609T120000Z-fresh-handoff.json",
        {"kind": "handoff", "task": "fresh handoff"},
    )

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 2, res
    # The handoffs were skipped at the kind gate (observable, not ingested).
    assert res["skipped_by_kind"].get("handoff") == 2

    # The not-yet-ingested file arrives after the ingest tick.
    _write_inbox_file(inbox, "2026-06-09-not-ingested.json", _stop_doc(["lesson not ingested yet"], agent_id="claude-code"))

    # Resolve exactly the candidate derived from file (1) — legacy backlog
    # shape: terminal row, file still in the live inbox.
    rows = _db_rows(db_obj)
    resolved_ids = [
        r["candidate_id"]
        for r in rows
        if json.loads(r["derived_from"]).get("inbox_file") == "2026-05-01-resolved.json"
    ]
    assert len(resolved_ids) == 1
    with db_obj.cursor() as c:
        c.execute(
            "UPDATE candidate_packets SET status='accepted', resolved_at=?, "
            "resolved_by='operator-test' WHERE candidate_id=?",
            (time.time(), resolved_ids[0]),
        )
    return db_obj, cfg, inbox


# Fixed "now" inside the fixture's timeline (matches test_inbox_cleanup.NOW).
NOW = time.mktime((2026, 6, 10, 12, 0, 0, 0, 0, 0))

EXPECTED = {
    "2026-05-01-resolved.json": "ingested_resolved",
    "2026-05-02-pending.json": "ingested",
    "20260420T120000Z-orphan-handoff.json": "expired_handoff",
}


def _run_cli(tmp_path, capsys, monkeypatch, *extra):
    """Invoke the REAL CLI command path (argparse + main), frozen clock."""
    monkeypatch.setattr(inbox_cleanup.time, "time", lambda: NOW)
    rc = inbox_cleanup.main(
        ["--home", str(tmp_path), "--db", str(tmp_path / "test.db"), *extra]
    )
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_cli_default_is_dry_run_and_moves_nothing(tmp_path, capsys, monkeypatch):
    db_obj, _cfg, inbox = _build_ingested_fixture(tmp_path)
    before_files = sorted(p.name for p in inbox.glob("*.json"))
    before_rows = _db_rows(db_obj)

    rc, report = _run_cli(tmp_path, capsys, monkeypatch)
    assert rc == 0
    assert report["dry_run"] is True
    vault = report["vaults"]["claudecode-vault"]
    would = {e["file"]: e["reason"] for e in vault["would_archive"]}
    assert would == EXPECTED
    assert vault["counts"]["keep"] == 2  # fresh handoff + not-yet-ingested stop file

    # Dry run touched NOTHING: files in place, no .archive, DB rows identical.
    assert sorted(p.name for p in inbox.glob("*.json")) == before_files
    assert not (inbox / ".archive").exists()
    assert _db_rows(db_obj) == before_rows


def test_cli_apply_archives_never_deletes_and_db_state_consistent(tmp_path, capsys, monkeypatch):
    db_obj, _cfg, inbox = _build_ingested_fixture(tmp_path)
    before_files = {p.name for p in inbox.glob("*.json")}
    before_rows = _db_rows(db_obj)

    rc, report = _run_cli(tmp_path, capsys, monkeypatch, "--apply")
    assert rc == 0
    assert report["dry_run"] is False
    vault = report["vaults"]["claudecode-vault"]
    archived = {e["file"]: e["reason"] for e in vault["archived"]}
    assert archived == EXPECTED

    # Archived-not-deleted: every archived file still exists under .archive
    # with its content intact; conservation across live + archive.
    live = {p.name for p in inbox.glob("*.json")}
    assert live == before_files - set(archived)
    for name in archived:
        target = inbox / ".archive" / name
        assert target.is_file(), f"{name} must be archived, never deleted"
    assert len(live) + len(archived) == len(before_files)
    doc = json.loads((inbox / ".archive" / "2026-05-01-resolved.json").read_text())
    assert doc["candidates"] == ["lesson fully resolved"]

    # DB/file consistency: cleanup opened the DB read-only — rows unchanged;
    # the resolved row still terminal, the pending row still proposed and its
    # source file (now archived) is carried by the DB row content.
    after_rows = _db_rows(db_obj)
    assert after_rows == before_rows
    by_file = {json.loads(r["derived_from"])["inbox_file"]: r for r in after_rows}
    assert by_file["2026-05-01-resolved.json"]["status"] == "accepted"
    assert by_file["2026-05-02-pending.json"]["status"] == "proposed"
    assert by_file["2026-05-02-pending.json"]["content"] == "lesson still proposed"

    # Idempotent: a second --apply archives nothing further.
    rc2, report2 = _run_cli(tmp_path, capsys, monkeypatch, "--apply")
    assert rc2 == 0
    assert report2["vaults"]["claudecode-vault"]["archived"] == []


def test_cli_dry_run_and_apply_are_mutually_exclusive(tmp_path, capsys, monkeypatch):
    _build_ingested_fixture(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run_cli(tmp_path, capsys, monkeypatch, "--dry-run", "--apply")
    assert exc.value.code == 2  # argparse usage error, never a silent apply

"""Tests for scripts/inbox_cleanup.py — the one-time operator migration that
reconciles existing inbox files against candidate_packets.derived_from and
archives already-ingested/resolved files plus aged file handoffs (B5, audit
C2). Fixtures only — the live vaults are never touched here; the gate also
asserts the script NEVER unlinks, only renames into .archive/.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import inbox_cleanup  # noqa: E402

NOW = time.mktime((2026, 6, 10, 12, 0, 0, 0, 0, 0))


def _make_fixture_db(tmp_path, rows):
    """rows: list of (status, inbox_file, candidate_index)."""
    db_path = tmp_path / "fixture.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE candidate_packets ("
        " candidate_id INTEGER PRIMARY KEY, status TEXT, derived_from TEXT)"
    )
    for status, inbox_file, idx in rows:
        derived = json.dumps(
            {"source": "inbox", "inbox_file": inbox_file, "candidate_index": idx}
        )
        conn.execute(
            "INSERT INTO candidate_packets (status, derived_from) VALUES (?, ?)",
            (status, derived),
        )
    conn.commit()
    conn.close()
    return db_path


def _stop_doc(candidates):
    return {
        "slug": "s",
        "kind": "stop_candidates",
        "candidates": candidates,
        "log_only": [],
        "do_not_store": [],
        "last_task": "t",
    }


def _build_fixture_vault(tmp_path):
    inbox = tmp_path / "claudecode-vault" / "inbox"
    inbox.mkdir(parents=True)
    # 1. Fully resolved stop file -> ingested_resolved.
    (inbox / "2026-05-01-resolved.json").write_text(
        json.dumps(_stop_doc(["lesson one"])), encoding="utf-8"
    )
    # 2. Ingested but still proposed -> ingested (rows carry the content now).
    (inbox / "2026-05-02-ingested.json").write_text(
        json.dumps(_stop_doc(["lesson two"])), encoding="utf-8"
    )
    # 3. Not yet ingested stop file -> keep.
    (inbox / "2026-06-09-fresh.json").write_text(
        json.dumps(_stop_doc(["lesson three"])), encoding="utf-8"
    )
    # 4. The orphaned aged handoff (45 days old, requires_ack-less) -> expired_handoff.
    (inbox / "20260426T184233Z-review-auth-migration-trace-pr.json").write_text(
        json.dumps({"kind": "handoff", "task": "review auth migration trace PR"}),
        encoding="utf-8",
    )
    # 5. Fresh handoff (1 day old) -> keep.
    (inbox / "20260609T120000Z-fresh-handoff.json").write_text(
        json.dumps({"kind": "handoff", "task": "fresh"}), encoding="utf-8"
    )
    # 6. Unknown kind -> keep.
    (inbox / "2026-05-03-failed.json").write_text(
        json.dumps({"kind": "failed_command", "command": "x"}), encoding="utf-8"
    )
    db_path = _make_fixture_db(
        tmp_path,
        [
            ("accepted", "2026-05-01-resolved.json", 0),
            ("proposed", "2026-05-02-ingested.json", 0),
        ],
    )
    return inbox, db_path


def test_dry_run_reports_correct_archive_set_and_touches_nothing(tmp_path):
    inbox, db_path = _build_fixture_vault(tmp_path)
    before = sorted(p.name for p in inbox.glob("*.json"))

    report = inbox_cleanup.run_cleanup(
        home=tmp_path, db_path=db_path, apply=False, now=NOW
    )
    assert report["dry_run"] is True
    vault = report["vaults"]["claudecode-vault"]
    assert vault["counts"] == {
        "total": 6,
        "keep": 3,
        "ingested_resolved": 1,
        "ingested": 1,
        "expired_handoff": 1,
    }, vault
    would = {e["file"]: e["reason"] for e in vault["would_archive"]}
    assert would == {
        "2026-05-01-resolved.json": "ingested_resolved",
        "2026-05-02-ingested.json": "ingested",
        "20260426T184233Z-review-auth-migration-trace-pr.json": "expired_handoff",
    }
    # Dry run mutates nothing.
    assert sorted(p.name for p in inbox.glob("*.json")) == before
    assert not (inbox / ".archive").exists()


def test_apply_archives_only_renames_never_unlinks(tmp_path):
    inbox, db_path = _build_fixture_vault(tmp_path)
    before = {p.name for p in inbox.glob("*.json")}

    # Belt-and-braces gate: any unlink/remove during apply fails the test.
    real_unlink, real_remove = os.unlink, os.remove

    def _forbidden(*args, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("inbox_cleanup must never unlink/remove")

    os.unlink = _forbidden
    os.remove = _forbidden
    try:
        report = inbox_cleanup.run_cleanup(
            home=tmp_path, db_path=db_path, apply=True, now=NOW
        )
    finally:
        os.unlink, os.remove = real_unlink, real_remove

    vault = report["vaults"]["claudecode-vault"]
    archived = {e["file"] for e in vault["archived"]}
    assert archived == {
        "2026-05-01-resolved.json",
        "2026-05-02-ingested.json",
        "20260426T184233Z-review-auth-migration-trace-pr.json",
    }
    live = {p.name for p in inbox.glob("*.json")}
    assert live == before - archived
    # Every archived file still exists, name preserved, under .archive/.
    for name in archived:
        assert (inbox / ".archive" / name).is_file()
    # Conservation: nothing was deleted.
    assert len(live) + len(archived) == len(before)

    # Idempotent: a second apply archives nothing further.
    report2 = inbox_cleanup.run_cleanup(
        home=tmp_path, db_path=db_path, apply=True, now=NOW
    )
    assert report2["vaults"]["claudecode-vault"]["archived"] == []


def test_script_source_contains_no_delete_calls():
    """Static gate: the only file mutation in the script is os.replace."""
    source = (Path(SCRIPTS_DIR) / "inbox_cleanup.py").read_text(encoding="utf-8")
    for forbidden in (".unlink(", "os.remove(", "rmtree(", "send2trash"):
        assert forbidden not in source, f"forbidden call in script: {forbidden}"
    assert "os.replace(" in source


def test_parse_name_epoch_both_formats():
    import calendar

    compact = inbox_cleanup.parse_name_epoch("20260426T184233Z-review.json")
    assert compact == calendar.timegm((2026, 4, 26, 18, 42, 33))

    epoch_ms = 1781000000000  # within 2026
    day = time.strftime("%Y-%m-%d", time.gmtime(epoch_ms / 1000))

    def to_base36(n):
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        out = ""
        while n:
            n, r = divmod(n, 36)
            out = digits[r] + out
        return out or "0"

    dashed = inbox_cleanup.parse_name_epoch(f"{day}-{to_base36(epoch_ms)}-slug.json")
    assert dashed == epoch_ms / 1000.0
    assert inbox_cleanup.parse_name_epoch("garbage.json") is None

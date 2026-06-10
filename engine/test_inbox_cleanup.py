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
    # 6. Stale non-handoff explicit kind (38 days) -> unrecognized_kind: the
    #    kind gate drops it forever, so the migration archives the residue.
    (inbox / "2026-05-03-failed.json").write_text(
        json.dumps({"kind": "failed_command", "command": "x"}), encoding="utf-8"
    )
    # 7. Fresh non-handoff explicit kind (1 day) -> keep (under residue TTL).
    (inbox / "2026-06-09-precompact.json").write_text(
        json.dumps({"kind": "claude_precompact_handoff", "task": "t"}),
        encoding="utf-8",
    )
    # 8. Aged but LIVE ack-channel lease (requires_ack, future expires_at) ->
    #    keep: the daemon owns it; archiving would orphan the lease row.
    (inbox / "20260420T120000Z-live-lease.json").write_text(
        json.dumps({
            "kind": "handoff",
            "task": "long lease",
            "lease_id": "handoff-live",
            "requires_ack": True,
            "expires_at": "2026-07-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    # 9. Already-acked leftover lease packet -> acked_handoff (never 'expired').
    (inbox / "20260420T130000Z-acked-lease.json").write_text(
        json.dumps({
            "kind": "handoff",
            "task": "done lease",
            "lease_id": "handoff-acked",
            "requires_ack": True,
            "ack_status": "accepted",
            "expires_at": "2026-07-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    # 10. Stale nothing-ingestible stop file (file-level log_only, no rows) ->
    #     nothing_ingestible: inbox_ingest will never take anything from it.
    (inbox / "2026-04-28-logonly.json").write_text(
        json.dumps({**_stop_doc(["never stored"]), "log_only": True}),
        encoding="utf-8",
    )
    db_path = _make_fixture_db(
        tmp_path,
        [
            ("accepted", "2026-05-01-resolved.json", 0),
            ("proposed", "2026-05-02-ingested.json", 0),
        ],
    )
    return inbox, db_path


EXPECTED_ARCHIVE = {
    "2026-05-01-resolved.json": "ingested_resolved",
    "2026-05-02-ingested.json": "ingested",
    "20260426T184233Z-review-auth-migration-trace-pr.json": "expired_handoff",
    "2026-05-03-failed.json": "unrecognized_kind",
    "20260420T130000Z-acked-lease.json": "acked_handoff",
    "2026-04-28-logonly.json": "nothing_ingestible",
}


def test_dry_run_reports_correct_archive_set_and_touches_nothing(tmp_path):
    inbox, db_path = _build_fixture_vault(tmp_path)
    before = sorted(p.name for p in inbox.glob("*.json"))

    report = inbox_cleanup.run_cleanup(
        home=tmp_path, db_path=db_path, apply=False, now=NOW
    )
    assert report["dry_run"] is True
    vault = report["vaults"]["claudecode-vault"]
    assert vault["counts"] == {
        "total": 10,
        "keep": 4,
        "ingested_resolved": 1,
        "ingested": 1,
        "expired_handoff": 1,
        "acked_handoff": 1,
        "nothing_ingestible": 1,
        "unrecognized_kind": 1,
    }, vault
    would = {e["file"]: e["reason"] for e in vault["would_archive"]}
    assert would == EXPECTED_ARCHIVE
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
    assert archived == set(EXPECTED_ARCHIVE)
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


# ── classify_file branch coverage (review hardening) ────────────────────────
# The branches below decide whether a LIVE candidate's source file gets
# archived prematurely against the real backlog, so each one is pinned.

def _classify(tmp_path, name, doc, rows, **kw):
    inbox = tmp_path / "x-vault" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    rows_by_file = {name: [{"candidate_index": i, "status": s} for s, i in rows]}
    kw.setdefault("handoff_ttl_days", 7.0)
    kw.setdefault("now", NOW)
    return inbox_cleanup.classify_file(path, rows_by_file, **kw)


def test_classify_multi_candidate_partially_ingested_keeps(tmp_path):
    """(a) Only SOME eligible indices have rows -> keep (subset check)."""
    doc = _stop_doc(["first lesson", "second lesson"])
    reason = _classify(
        tmp_path, "2026-05-05-partial.json", doc, [("accepted", 0)]
    )
    assert reason == "keep"


def test_classify_multi_candidate_mixed_statuses_is_ingested(tmp_path):
    """(b) Fully ingested with MIXED statuses -> 'ingested', not resolved."""
    doc = _stop_doc(["first lesson", "second lesson"])
    reason = _classify(
        tmp_path,
        "2026-05-05-mixed.json",
        doc,
        [("accepted", 0), ("proposed", 1)],
    )
    assert reason == "ingested"


def test_classify_nothing_ingestible_fresh_keeps_stale_archives(tmp_path):
    """(c) eligible==[] with no rows: keep while fresh, archive once stale."""
    doc = {**_stop_doc(["never stored"]), "log_only": True}
    fresh = _classify(tmp_path, "2026-06-09-logonly.json", doc, [])
    assert fresh == "keep"
    stale = _classify(tmp_path, "2026-04-28-logonly.json", doc, [])
    assert stale == "nothing_ingestible"
    # All-blank candidates are equally never-ingestible.
    blank = {**_stop_doc([]), "candidates": ["", 42]}
    assert _classify(tmp_path, "2026-04-28-blank.json", blank, []) == "nothing_ingestible"
    # But eligible==[] WITH rows must never be swept by the residue branch.
    assert (
        _classify(tmp_path, "2026-04-28-logrows.json", doc, [("proposed", 0)])
        == "keep"
    )


def test_classify_legacy_shape_with_fully_resolved_rows(tmp_path):
    """(d) Odd/legacy non-stop shape whose derived rows are all terminal."""
    doc = {"weird": True, "payload": "legacy"}
    reason = _classify(
        tmp_path,
        "2026-05-05-legacy.json",
        doc,
        [("accepted", 0), ("rejected", 1)],
    )
    assert reason == "ingested_resolved"
    # Same shape with a live row -> keep (kind-less, no handoff branch).
    assert (
        _classify(tmp_path, "2026-05-05-legacy2.json", doc, [("proposed", 0)])
        == "keep"
    )


def test_classify_handoff_lease_predicates(tmp_path):
    """Lease-aware handoff drain: live leases keep, own-expiry drains,
    acked leftovers are 'acked_handoff' (never 'expired')."""
    base = {"kind": "handoff", "task": "t", "lease_id": "h-1", "requires_ack": True}
    # Aged file, lease still unexpired -> keep (daemon owns it).
    live = {**base, "expires_at": "2026-07-01T00:00:00Z"}
    assert _classify(tmp_path, "20260420T120000Z-live.json", live, []) == "keep"
    # Lease's own expiry passed -> expired_handoff.
    dead = {**base, "expires_at": "2026-05-01T00:00:00Z"}
    assert (
        _classify(tmp_path, "20260420T120000Z-dead.json", dead, [])
        == "expired_handoff"
    )
    # requires_ack with NO expires_at -> keep (the ack channel drains it).
    assert _classify(tmp_path, "20260420T120000Z-noexp.json", base, []) == "keep"
    # Already acked -> acked_handoff regardless of expiry.
    acked = {**base, "ack_status": "accepted", "expires_at": "2026-07-01T00:00:00Z"}
    assert (
        _classify(tmp_path, "20260420T120000Z-acked.json", acked, [])
        == "acked_handoff"
    )


def test_parse_iso_epoch():
    import calendar

    assert inbox_cleanup.parse_iso_epoch("2026-06-10T12:00:00Z") == float(
        calendar.timegm((2026, 6, 10, 12, 0, 0))
    )
    assert inbox_cleanup.parse_iso_epoch(None) is None
    assert inbox_cleanup.parse_iso_epoch("not-a-date") is None

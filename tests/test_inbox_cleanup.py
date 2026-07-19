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
    """rows: list of (status, inbox_file, candidate_index, content).
    Mirrors what inbox_ingest writes: the row carries the candidate content
    and derived_from carries its content_sha1 fingerprint."""
    db_path = tmp_path / "fixture.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE candidate_packets ("
        " candidate_id INTEGER PRIMARY KEY, status TEXT, content TEXT,"
        " derived_from TEXT)"
    )
    for status, inbox_file, idx, content in rows:
        derived = json.dumps(
            {
                "source": "inbox",
                "inbox_file": inbox_file,
                "candidate_index": idx,
                "content_sha1": inbox_cleanup._content_sha1(content),
            }
        )
        conn.execute(
            "INSERT INTO candidate_packets (status, content, derived_from)"
            " VALUES (?, ?, ?)",
            (status, content, derived),
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
            ("accepted", "2026-05-01-resolved.json", 0, "lesson one"),
            ("proposed", "2026-05-02-ingested.json", 0, "lesson two"),
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
        "agent_mismatch_quarantine": 0,
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
    """rows: (status, candidate_index) for fingerprint-less legacy rows, or
    (status, candidate_index, content) for ingest-shaped rows with sha."""
    inbox = tmp_path / "x-vault" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    row_dicts = []
    for r in rows:
        d = {"candidate_index": r[1], "status": r[0]}
        if len(r) > 2:
            d["content"] = r[2]
            d["content_sha1"] = inbox_cleanup._content_sha1(r[2])
        row_dicts.append(d)
    rows_by_file = {name: row_dicts}
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


def test_cross_vault_same_filename_only_matching_copy_archives(tmp_path):
    """Rows are keyed by bare filename with no vault scoping: the same name in
    two vaults must NOT share a fate. Only the copy whose content matches the
    ingest-written fingerprint archives; the never-ingested copy keeps."""
    name = "2026-05-01-shared.json"
    a = tmp_path / "claudecode-vault" / "inbox"
    a.mkdir(parents=True)
    b = tmp_path / "codex-vault" / "inbox"
    b.mkdir(parents=True)
    (a / name).write_text(json.dumps(_stop_doc(["alpha lesson"])), encoding="utf-8")
    (b / name).write_text(
        json.dumps(_stop_doc(["beta lesson never ingested"])), encoding="utf-8"
    )
    db_path = _make_fixture_db(tmp_path, [("accepted", name, 0, "alpha lesson")])

    report = inbox_cleanup.run_cleanup(
        home=tmp_path, db_path=db_path, apply=True, now=NOW
    )
    cc = report["vaults"]["claudecode-vault"]["counts"]
    assert cc["ingested_resolved"] == 1, cc
    assert not (a / name).exists()
    assert (a / ".archive" / name).is_file()

    cz = report["vaults"]["codex-vault"]["counts"]
    assert cz["keep"] == 1 and cz["ingested_resolved"] == 0, cz
    assert (b / name).is_file(), "un-ingested same-named copy must stay live"
    assert not (b / ".archive").exists()


def test_classify_stop_shape_requires_fingerprint_match(tmp_path):
    """A row whose fingerprint does not match the file candidate (same name,
    different content — or a forged row) never counts as ingestion."""
    doc = _stop_doc(["real lesson text"])
    assert (
        _classify(
            tmp_path,
            "2026-05-05-mismatch.json",
            doc,
            [("accepted", 0, "different content entirely")],
        )
        == "keep"
    )
    # Matching fingerprint -> archives as before.
    assert (
        _classify(
            tmp_path,
            "2026-05-05-match.json",
            doc,
            [("accepted", 0, "real lesson text")],
        )
        == "ingested_resolved"
    )


def test_classify_handoff_never_takes_legacy_resolved_branch(tmp_path):
    """Forged fully-resolved rows naming a live handoff lease must not drain
    it through the odd/legacy-shape branch; handoffs only drain via their own
    ack/expiry/TTL predicates."""
    live = {
        "kind": "handoff",
        "task": "t",
        "lease_id": "h-live",
        "requires_ack": True,
        "expires_at": "2026-07-01T00:00:00Z",
    }
    assert (
        _classify(
            tmp_path,
            "20260420T120000Z-forged.json",
            live,
            [("accepted", 0, "evil staged content")],
        )
        == "keep"
    )


def test_classify_legacy_branch_requires_content_correspondence(tmp_path):
    """The legacy ingested_resolved branch only fires when each row's stored
    content provably appears in the file (forged rows naming an arbitrary
    file fail the substring check)."""
    doc = {"weird": True, "payload": "legacy"}
    assert (
        _classify(
            tmp_path,
            "2026-05-05-legacy-forged.json",
            doc,
            [("accepted", 0, "evil forged content")],
        )
        == "keep"
    )
    assert (
        _classify(
            tmp_path,
            "2026-05-05-legacy-match.json",
            doc,
            [("accepted", 0, "legacy")],
        )
        == "ingested_resolved"
    )


def test_dry_run_and_apply_are_mutually_exclusive(tmp_path, capsys):
    """Operator safety: an explicitly requested dry run can never mutate —
    `--dry-run --apply` is an argparse error, and `--dry-run` alone reports
    without touching anything."""
    import pytest

    with pytest.raises(SystemExit) as exc:
        inbox_cleanup.main(
            ["--dry-run", "--apply", "--home", str(tmp_path), "--db", str(tmp_path / "x.db")]
        )
    assert exc.value.code == 2
    capsys.readouterr()  # discard argparse stderr

    inbox, db_path = _build_fixture_vault(tmp_path)
    before = sorted(p.name for p in inbox.glob("*.json"))
    rc = inbox_cleanup.main(["--dry-run", "--home", str(tmp_path), "--db", str(db_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert sorted(p.name for p in inbox.glob("*.json")) == before
    assert not (inbox / ".archive").exists()


def _mismatch_stop_doc(candidates, agent_id):
    doc = _stop_doc(candidates)
    doc["agent_id"] = agent_id
    return doc


def test_classify_stale_agent_mismatch_returns_quarantine_reason(tmp_path):
    """W3 (audit §4): eligible, non-empty, ZERO matching DB rows, and the
    declared agent_id disagrees with the vault-derived principal (x-vault ->
    'x') — this is the confirmed permanently-unresolvable cohort, not merely
    'not yet ingested'. Once past the residue TTL it gets a real drain reason
    instead of falling through every branch to 'keep' forever."""
    doc = _mismatch_stop_doc(["orphaned lesson"], agent_id="some-other-agent")
    reason = _classify(
        tmp_path, "2026-04-01-old-mismatch.json", doc, rows=[],
    )
    assert reason == "agent_mismatch_quarantine", reason


def test_classify_fresh_agent_mismatch_keeps(tmp_path):
    """Same mismatch, but the file is younger than the residue TTL — stays
    'keep' (grace window)."""
    doc = _mismatch_stop_doc(["orphaned lesson"], agent_id="some-other-agent")
    reason = _classify(
        tmp_path, "2026-06-09-fresh-mismatch.json", doc, rows=[],
    )
    assert reason == "keep", reason


def test_classify_agent_matching_zero_rows_stays_keep(tmp_path):
    """Scope guard: zero rows + eligible content but the agent_id MATCHES the
    vault principal ('x-vault' -> 'x') is simply fresh/not-yet-ingested, not
    the agent-mismatch cohort — must stay 'keep' even when old."""
    doc = _mismatch_stop_doc(["fine content"], agent_id="x")
    reason = _classify(
        tmp_path, "2026-04-01-old-matching.json", doc, rows=[],
    )
    assert reason == "keep", reason


def test_classify_agentless_zero_rows_stays_keep(tmp_path):
    """Scope guard: no agent_id at all (kind-less Claude Code shape) is never
    treated as an agent mismatch, regardless of age."""
    doc = _stop_doc(["fine content"])
    reason = _classify(
        tmp_path, "2026-04-01-old-agentless.json", doc, rows=[],
    )
    assert reason == "keep", reason


def test_scripts_inbox_cleanup_classifies_and_quarantines_agent_mismatch_residue(tmp_path):
    """End-to-end through run_cleanup: dry-run reports the reason but touches
    nothing; --apply MOVES (never archives, never deletes) the file into
    quarantine/ with a reason sidecar, mirroring afm_passes.inbox_quarantine's
    payload shape closely enough for health_report's by_reason aggregation to
    make sense across both sources."""
    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    doc = _mismatch_stop_doc(["orphaned lesson, permanently unresolvable"], agent_id="unknown-agent")
    (inbox / "2026-04-01-mismatch.json").write_text(json.dumps(doc), encoding="utf-8")
    db_path = tmp_path / "empty.db"  # no candidate_packets rows anywhere

    # Dry run: reason surfaces, nothing moves.
    dry = inbox_cleanup.run_cleanup(home=tmp_path, db_path=db_path, apply=False, now=NOW)
    vault = dry["vaults"]["unknown-vault"]
    assert vault["counts"]["agent_mismatch_quarantine"] == 1, vault
    would = {e["file"]: e["reason"] for e in vault["would_archive"]}
    assert would == {"2026-04-01-mismatch.json": "agent_mismatch_quarantine"}
    assert (inbox / "2026-04-01-mismatch.json").exists()
    assert not (inbox / "quarantine").exists()
    assert not (inbox / ".archive").exists()

    # Apply: the file MOVES to quarantine/ (not .archive/) with a sidecar.
    applied = inbox_cleanup.run_cleanup(home=tmp_path, db_path=db_path, apply=True, now=NOW)
    vault2 = applied["vaults"]["unknown-vault"]
    archived = {e["file"]: e["reason"] for e in vault2["archived"]}
    assert archived == {"2026-04-01-mismatch.json": "agent_mismatch_quarantine"}
    assert not (inbox / "2026-04-01-mismatch.json").exists()
    assert not (inbox / ".archive").exists(), "must never land in .archive/ — content was never captured in the DB"
    quarantined = inbox / "quarantine" / "2026-04-01-mismatch.json"
    assert quarantined.is_file()
    assert json.loads(quarantined.read_text(encoding="utf-8")) == doc

    reason_sidecar = inbox / "quarantine" / "2026-04-01-mismatch.json.reason.json"
    assert reason_sidecar.is_file()
    reason_payload = json.loads(reason_sidecar.read_text(encoding="utf-8"))
    assert reason_payload["reason"] == "_agent_mismatch"
    assert reason_payload["detected_agent_id"] == "unknown-agent"
    assert reason_payload["resolved_vault_principal"] == "unknown"

    # Idempotent: a second --apply finds nothing left to quarantine.
    again = inbox_cleanup.run_cleanup(home=tmp_path, db_path=db_path, apply=True, now=NOW)
    assert again["vaults"]["unknown-vault"]["archived"] == []


def test_parse_iso_epoch():
    import calendar

    assert inbox_cleanup.parse_iso_epoch("2026-06-10T12:00:00Z") == float(
        calendar.timegm((2026, 6, 10, 12, 0, 0))
    )
    assert inbox_cleanup.parse_iso_epoch(None) is None
    assert inbox_cleanup.parse_iso_epoch("not-a-date") is None

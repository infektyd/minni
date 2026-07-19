"""Tests for afm_passes.inbox_quarantine — the quarantine drain for
permanently-unresolvable ``_agent_mismatch`` inbox stop-candidate files
(audit §4 "Consolidation inbox residue" / W3).

Follows the test_inbox_ingest.py / test_inbox_archive.py harness pattern
(isolated tmp DB + config, no real ~/.minni touched).
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from test_inbox_ingest import _make_db, _stop_doc, _write_inbox_file  # noqa: E402


def _epoch(iso: str) -> float:
    return (
        datetime.fromisoformat(iso.replace("Z", "+00:00"))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


OLD_CREATED = "2026-01-01T00:00:00.000Z"
FRESH_CREATED = "2026-06-08T12:00:00.000Z"
# Well past the default 14-day TTL relative to OLD_CREATED.
NOW_LATE = _epoch("2026-06-01T00:00:00Z")
# A few hours after FRESH_CREATED — well inside the 14-day TTL grace window.
NOW_SOON = _epoch("2026-06-08T18:00:00Z")


def _mismatch_doc(**overrides):
    """A stop-candidate file whose declared agent_id disagrees with the
    vault-derived principal for `unknown-vault` (slug 'unknown' has no entry
    in the alias table, so it resolves to 'unknown' — the exact 59-file
    audit cohort: agent_id 'unknown-agent' != principal 'unknown')."""
    base = {"agent_id": "unknown-agent", "createdAt": OLD_CREATED}
    base.update(overrides)
    return _stop_doc(["orphaned lesson from an unresolvable agent"], **base)


# ── (1) regression pin: confirms the underlying bug still exists ───────────

def test_agent_mismatch_file_is_never_ingested_confirms_residue(tmp_path):
    """ingest()'s skip behavior is UNCHANGED by this package (fail-closed
    stays exactly as-is) — this pins the precondition the quarantine drain
    exists to clean up after."""
    from minni.afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "unknown-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _mismatch_doc())

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0, res
    assert res["skipped_by_kind"]["_agent_mismatch"] == 1, res
    with db_obj.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM candidate_packets")
        assert dict(c.fetchone())["n"] == 0


# ── (2) core new behavior ───────────────────────────────────────────────────

def test_stale_agent_mismatch_file_is_quarantined(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_stale_agent_mismatch

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _mismatch_doc())

    res = quarantine_stale_agent_mismatch(
        None, inboxes=[inbox], ttl_days=14.0, now=NOW_LATE,
    )
    assert res["quarantined"] == 1, res
    assert not (inbox / "a.json").exists(), "file must leave the live inbox"

    quarantined_path = inbox / "quarantine" / "a.json"
    assert quarantined_path.is_file(), "file must land in quarantine/, never deleted"
    doc = json.loads(quarantined_path.read_text(encoding="utf-8"))
    assert doc["candidates"] == ["orphaned lesson from an unresolvable agent"]

    reason_path = inbox / "quarantine" / "a.json.reason.json"
    assert reason_path.is_file()
    reason = json.loads(reason_path.read_text(encoding="utf-8"))
    assert reason["reason"] == "_agent_mismatch"
    assert reason["detected_agent_id"] == "unknown-agent"
    assert reason["resolved_vault_principal"] == "unknown"
    assert "quarantined_at" in reason
    assert reason["ttl_days"] == 14.0


def test_fresh_agent_mismatch_file_is_not_quarantined_yet(tmp_path):
    """A file younger than the TTL stays live — the grace window lets a
    transient/upstream-fixable condition self-correct before it is treated as
    permanent."""
    from minni.afm_passes.inbox_quarantine import quarantine_stale_agent_mismatch

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write_inbox_file(inbox, "fresh.json", _mismatch_doc(createdAt=FRESH_CREATED))

    res = quarantine_stale_agent_mismatch(
        None, inboxes=[inbox], ttl_days=14.0, now=NOW_SOON,
    )
    assert res["quarantined"] == 0, res
    assert (inbox / "fresh.json").exists()
    assert not (inbox / "quarantine").exists()


def test_quarantine_is_idempotent(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_stale_agent_mismatch

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _mismatch_doc())

    first = quarantine_stale_agent_mismatch(
        None, inboxes=[inbox], ttl_days=14.0, now=NOW_LATE,
    )
    assert first["quarantined"] == 1

    second = quarantine_stale_agent_mismatch(
        None, inboxes=[inbox], ttl_days=14.0, now=NOW_LATE,
    )
    assert second["quarantined"] == 0, second
    # No numeric-suffix collision file was created by the re-run.
    assert sorted(p.name for p in (inbox / "quarantine").glob("*.json")) == [
        "a.json", "a.json.reason.json",
    ]


def test_quarantine_never_unlinks(tmp_path, monkeypatch):
    """Belt-and-braces gate matching this codebase's existing 'NEVER deletes'
    contract tests (e.g. scripts/inbox_cleanup.py's
    test_apply_archives_only_renames_never_unlinks)."""
    import os as os_mod

    from minni.afm_passes.inbox_quarantine import quarantine_stale_agent_mismatch

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _mismatch_doc())

    def _forbidden(*a, **k):  # pragma: no cover - failure path
        raise AssertionError("inbox_quarantine must never unlink/remove")

    monkeypatch.setattr(os_mod, "unlink", _forbidden)
    monkeypatch.setattr(os_mod, "remove", _forbidden)

    res = quarantine_stale_agent_mismatch(
        None, inboxes=[inbox], ttl_days=14.0, now=NOW_LATE,
    )
    assert res["quarantined"] == 1
    assert (inbox / "quarantine" / "a.json").is_file()


def test_quarantine_traversal_is_rejected(tmp_path):
    """Mirrors inbox_archive's containment defense-in-depth: a target that
    would resolve outside the sibling quarantine/ dir is refused."""
    from minni.afm_passes.inbox_quarantine import quarantine_inbox_file

    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    weird = inbox / ".."
    assert quarantine_inbox_file(weird, {"reason": "_agent_mismatch"}) is None


def test_quarantine_scoped_to_agent_mismatch_only(tmp_path):
    """Scope guard (punch-list amendment): _malformed_kind and _unrecognized
    files are DIFFERENT bugs and must NOT be drained by this pass, even when
    stale."""
    from minni.afm_passes.inbox_quarantine import quarantine_stale_agent_mismatch

    inbox = tmp_path / "unknown-vault" / "inbox"
    # _unrecognized: kind-less junk, not the stop-candidate shape at all.
    _write_inbox_file(inbox, "junk.json", {"hello": "world"})
    # _malformed_kind: kind is a list (unhashable) — not a real stop file.
    _write_inbox_file(inbox, "poison.json", {
        "kind": ["stop_candidates"], "candidates": ["x"], "slug": "s", "last_task": "t",
    })
    # An explicit non-stop kind, aged.
    _write_inbox_file(inbox, "handoff.json", {
        "kind": "handoff", "task": "old", "createdAt": OLD_CREATED,
    })
    # A stop-candidate file whose agent_id MATCHES its vault principal (no
    # mismatch at all) — must never be touched by this drain.
    _write_inbox_file(
        inbox, "matching.json",
        _stop_doc(["fine content"], agent_id="unknown", createdAt=OLD_CREATED),
    )

    res = quarantine_stale_agent_mismatch(
        None, inboxes=[inbox], ttl_days=14.0, now=NOW_LATE,
    )
    assert res["quarantined"] == 0, res
    for name in ("junk.json", "poison.json", "handoff.json", "matching.json"):
        assert (inbox / name).exists(), name


def test_quarantine_dry_run_does_not_move(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_stale_agent_mismatch

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _mismatch_doc())

    res = quarantine_stale_agent_mismatch(
        None, inboxes=[inbox], ttl_days=14.0, now=NOW_LATE, dry_run=True,
    )
    assert res["would_quarantine"] == 1
    assert res["quarantined"] == 0
    assert (inbox / "a.json").exists()
    assert not (inbox / "quarantine").exists()


def test_quarantine_collision_gets_numeric_suffix(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_inbox_file

    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "quarantine").mkdir()
    (inbox / "quarantine" / "a.json").write_text('{"old": true}', encoding="utf-8")
    (inbox / "a.json").write_text('{"new": true}', encoding="utf-8")

    target = quarantine_inbox_file(inbox / "a.json", {"reason": "_agent_mismatch"})
    assert target == str(inbox / "quarantine" / "a.1.json")
    assert (inbox / "quarantine" / "a.json").read_text(encoding="utf-8") == '{"old": true}'
    assert json.loads((inbox / "quarantine" / "a.1.json").read_text(encoding="utf-8")) == {
        "new": True
    }
    assert (inbox / "quarantine" / "a.1.json.reason.json").is_file()


def test_quarantine_missing_file_is_quiet_noop(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_inbox_file

    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    assert quarantine_inbox_file(inbox / "gone.json", {"reason": "_agent_mismatch"}) is None


# ── (10) end-to-end through the real AFM loop tick ──────────────────────────

def test_afm_loop_quarantines_stale_agent_mismatch_and_increments_counter(tmp_path, monkeypatch):
    """Not just the module function in isolation: a stale _agent_mismatch
    file gets quarantined by an actual AFM loop tick, and the SAME global
    counter status.daemon.counters already surfaces (obs.metrics_snapshot(),
    wired at minnid.py:865 / health.py handle_status) climbs by the
    quarantined count — proving the counter is updatable from the loop
    thread, not merely read back over RPC."""
    import asyncio

    import minni.obs as obs
    from minni.minnid_runtime.afm import afm_loop_runner

    from test_afm_loop_promotion import _loop_context  # noqa: E402
    from test_afm_loop_promotion import _make_db as _make_loop_db  # noqa: E402

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_loop_db(tmp_path)
    cons_cfg = cfg.afm_loop_schedule["passes"]["consolidation"]
    cons_cfg["ingest_inbox"] = True
    cons_cfg["inbox_quarantine_ttl_days"] = 14

    inbox = tmp_path / "unknown-vault" / "inbox"
    _write_inbox_file(
        inbox, "a.json",
        _stop_doc(
            ["orphaned lesson"], agent_id="unknown-agent",
            createdAt="2020-01-01T00:00:00.000Z",
        ),
    )

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        asyncio.run(afm_loop_runner(ctx))

        assert not (inbox / "a.json").exists(), "file must leave the live inbox"
        assert (inbox / "quarantine" / "a.json").is_file()
        snap = obs.metrics_snapshot()
        assert snap.get("inbox_quarantined_total") == 1, snap
    finally:
        obs.METRICS.reset()


# ── (9) operator-visible recovery surface: handle_health_report ────────────

class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return {"max_rowid": 0, "n": 0}


class _FakeDB:
    def __init__(self, *a, **k):
        pass

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


def test_health_report_surfaces_quarantine_counts(tmp_path, monkeypatch):
    """(c) operator-visible recovery surface, mirroring the existing
    vector_backend_lag shape: handle_health_report's inbox_quarantine block
    reports count/by_reason/oldest from the filesystem quarantine dirs."""
    import minni.config as cfg_mod
    import minni.minnid as minnid
    from minni.afm_passes.inbox_quarantine import quarantine_inbox_file
    from minni.principal import EffectivePrincipal

    monkeypatch.setattr(minnid, "SovereignDB", _FakeDB)
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "CANONICAL_SOVEREIGN_HOME", str(tmp_path), raising=False
    )

    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    f = inbox / "a.json"
    f.write_text(json.dumps(_mismatch_doc()), encoding="utf-8")
    quarantine_inbox_file(f, {
        "reason": "_agent_mismatch",
        "detected_agent_id": "unknown-agent",
        "resolved_vault_principal": "unknown",
        "quarantined_at": "2026-06-01T00:00:00Z",
        "ttl_days": 14.0,
    })

    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]

    assert "redacted" not in rep
    iq = rep["inbox_quarantine"]
    assert iq["count"] == 1, iq
    assert iq["by_reason"] == {"_agent_mismatch": 1}, iq
    assert iq["oldest_quarantined_at"] == "2026-06-01T00:00:00Z"


def test_health_report_quarantine_block_survives_recovery_redaction(tmp_path, monkeypatch):
    """Aggregate-only (count/by_reason/oldest, no file paths), so this stays
    outside the sensitive-key redaction path — a pre-identity/recovery caller
    still sees it, matching vector_backend_lag's precedent."""
    import minni.config as cfg_mod
    import minni.minnid as minnid
    from minni.afm_passes.inbox_quarantine import quarantine_inbox_file

    monkeypatch.setattr(minnid, "SovereignDB", _FakeDB)
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "CANONICAL_SOVEREIGN_HOME", str(tmp_path), raising=False
    )

    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    f = inbox / "a.json"
    f.write_text(json.dumps(_mismatch_doc()), encoding="utf-8")
    quarantine_inbox_file(f, {
        "reason": "_agent_mismatch",
        "detected_agent_id": "unknown-agent",
        "resolved_vault_principal": "unknown",
        "quarantined_at": "2026-06-01T00:00:00Z",
        "ttl_days": 14.0,
    })

    rep = minnid._handle_health_report({"_recovery": True}, 1)["result"]
    assert "redacted" in rep
    assert rep["inbox_quarantine"]["count"] == 1, rep["inbox_quarantine"]

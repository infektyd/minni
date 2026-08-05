"""Tests for afm_passes.inbox_quarantine — the quarantine drain for
permanently-unresolvable ``_agent_mismatch`` inbox stop-candidate files
(audit §4 "Consolidation inbox residue" / W3).

Follows the test_inbox_ingest.py / test_inbox_archive.py harness pattern
(isolated tmp DB + config, no real ~/.minni touched).
"""

import json
import sqlite3
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


# ── M4 (#229): the AFM dead-letter cohort gets a drain and a count ─────────

import time as _time
from pathlib import Path as _Path


def _afm_file(inbox, name, *, age_days, payload=None):
    inbox = _Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    body = payload if payload is not None else {
        "trace_id": "t-1", "pass_name": "pruning", "proposals": [],
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    old = _time.time() - age_days * 86400
    os.utime(path, (old, old))
    return path


def _moved(inbox):
    return [q for q in (_Path(inbox) / "quarantine").glob("afm-*.json")
            if not q.name.endswith(".reason.json")]


def test_stale_afm_dead_letter_is_quarantined(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-pruning-2026-01-01.json", age_days=61.0)
    _afm_file(inbox, "afm-drafts-2026-01-01.json", age_days=61.0)

    result = quarantine_afm_dead_letter(None, [inbox], dry_run=False)

    assert result["quarantined"] == 2
    assert not list(inbox.glob("*.json"))
    assert len(_moved(inbox)) == 2


def test_fresh_afm_dead_letter_stays_inside_the_grace_window(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-pruning-2026-08-04.json", age_days=0.5)

    assert quarantine_afm_dead_letter(None, [inbox], dry_run=False)["quarantined"] == 0
    assert list(inbox.glob("*.json"))


def test_afm_quarantine_writes_a_reason_sidecar(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-drafts-2026-01-01.json", age_days=61.0)
    quarantine_afm_dead_letter(None, [inbox], dry_run=False)

    sidecars = list((inbox / "quarantine").glob("*.reason.json"))
    assert len(sidecars) == 1
    assert json.loads(sidecars[0].read_text())["reason"] == "_afm_dead_letter"


def test_afm_dead_letter_drain_never_deletes(tmp_path):
    """Contract: os.replace only — the payload survives for inspection."""
    from minni.afm_passes.inbox_quarantine import quarantine_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-pruning-2026-01-01.json", age_days=61.0,
              payload={"pass_name": "pruning", "proposals": [{"keep": "me"}]})
    quarantine_afm_dead_letter(None, [inbox], dry_run=False)

    assert json.loads(_moved(inbox)[0].read_text())["proposals"] == [{"keep": "me"}]


def test_afm_named_file_with_a_reader_shape_is_left_alone(tmp_path):
    """Kind-LESS on purpose: that is the branch _is_stop_candidate_shape
    guards. Such a file IS ingested, so moving it would be data loss."""
    from minni.afm_passes.inbox_quarantine import quarantine_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-drafts-2026-01-01.json", age_days=61.0,
              payload={"slug": "s", "last_task": "t", "candidates": ["c"]})

    assert quarantine_afm_dead_letter(None, [inbox], dry_run=False)["quarantined"] == 0
    assert list(inbox.glob("afm-drafts-*.json"))


def test_afm_file_with_an_explicit_kind_is_left_alone(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-drafts-2026-01-01.json", age_days=61.0,
              payload={"kind": "stop_candidates", "candidates": ["x"], "agent_id": "codex"})

    assert quarantine_afm_dead_letter(None, [inbox], dry_run=False)["quarantined"] == 0


def test_drain_is_scoped_to_the_afm_names_only(tmp_path):
    """_unrecognized is not drained as a CLASS: an unknown kind is not proof a
    file is unreadable."""
    from minni.afm_passes.inbox_quarantine import (
        count_afm_dead_letter,
        quarantine_afm_dead_letter,
    )

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "some-other-writer-2026-01-01.json", age_days=61.0,
              payload={"pass_name": "mystery", "rows": []})

    assert quarantine_afm_dead_letter(None, [inbox], dry_run=False)["quarantined"] == 0
    assert count_afm_dead_letter([inbox])["files"] == 0
    assert list(inbox.glob("some-other-writer-*.json"))


def test_afm_dead_letter_dry_run_moves_nothing(tmp_path):
    from minni.afm_passes.inbox_quarantine import quarantine_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-pruning-2026-01-01.json", age_days=61.0)

    result = quarantine_afm_dead_letter(None, [inbox], dry_run=True)
    assert result["would_quarantine"] == 1
    assert result["quarantined"] == 0
    assert list(inbox.glob("*.json"))


def test_afm_backlog_is_countable_without_draining(tmp_path):
    from minni.afm_passes.inbox_quarantine import count_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-pruning-2026-01-01.json", age_days=61.0)
    _afm_file(inbox, "afm-drafts-2026-08-04.json", age_days=0.5)

    stats = count_afm_dead_letter([inbox])
    assert stats["files"] == 2
    assert stats["oldest_age_days"] >= 60.0
    assert len(list(inbox.glob("*.json"))) == 2


def test_afm_backlog_count_is_zero_on_a_clean_inbox(tmp_path):
    from minni.afm_passes.inbox_quarantine import count_afm_dead_letter

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    stats = count_afm_dead_letter([inbox])
    assert stats["files"] == 0
    assert stats["oldest_age_days"] is None


def test_afm_backlog_count_ignores_already_quarantined_files(tmp_path):
    """quarantine/ is out of the live inbox; counting it would leave the
    backlog permanently non-zero after a successful drain."""
    from minni.afm_passes.inbox_quarantine import (
        count_afm_dead_letter,
        quarantine_afm_dead_letter,
    )

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-pruning-2026-01-01.json", age_days=61.0)
    quarantine_afm_dead_letter(None, [inbox], dry_run=False)

    assert count_afm_dead_letter([inbox])["files"] == 0


def test_afm_loop_drains_the_dead_letter_and_increments_its_counter(tmp_path, monkeypatch):
    """The drain must be WIRED, not merely importable: a mutant that
    disconnects it from afm_loop_runner otherwise passes the whole suite,
    while 'M4 now has a drain path' is the central claim."""
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
    _afm_file(inbox, "afm-pruning-2026-01-01.json", age_days=61.0)

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        asyncio.run(afm_loop_runner(ctx))

        assert not (inbox / "afm-pruning-2026-01-01.json").exists()
        assert (inbox / "quarantine" / "afm-pruning-2026-01-01.json").is_file()
        snap = obs.metrics_snapshot()
        assert snap.get("inbox_afm_dead_letter_quarantined_total") == 1, snap
    finally:
        obs.METRICS.reset()


def _real_lifecycle_db(tmp_path, monkeypatch):
    """A REAL migrated DB behind the health handler.

    The _FakeCursor used by the quarantine tests returns a dict from
    fetchone(), so count_orphaned_afm_review raised KeyError and the queue
    half of the block never actually ran — every assertion about it was
    vacuous, and mutants hard-coding the counts survived.
    """
    import minni.config as cfg_mod
    import minni.db as db_mod
    import minni.minnid as minnid

    # Build via SovereignDB, not raw run_migrations: health_report reads
    # columns the full initializer creates (contradicts_id and friends), so a
    # migrations-only fixture degrades the whole report and hides the block
    # under test.
    db_path = tmp_path / "health.db"
    cfg = cfg_mod.SovereignConfig(
        db_path=str(db_path),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "f.faiss"),
        writeback_enabled=False,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        seed = db_mod.SovereignDB(cfg)
        seed._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    with seed.cursor() as c:
        c.execute(
            """INSERT INTO candidate_packets
               (candidate_id, principal, content, status, proposed_at)
               VALUES (1, 'test', 'c', 'proposed', ?)""",
            (_time.time() - 40 * 86400,),
        )
        c.execute(
            """INSERT INTO candidate_packets
               (candidate_id, principal, content, status, proposed_at)
               VALUES (2, 'test', 'c', 'accepted', ?)""",
            (_time.time(),),
        )
        c.execute(
            """INSERT INTO consolidation_actions
               (action_type, claim, category, status, created_at)
               VALUES ('afm_review', '2', 'general', 'pending', ?)""",
            (_time.time(),),
        )
    seed.close()

    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", str(db_path), raising=False)
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "CANONICAL_SOVEREIGN_HOME", str(tmp_path), raising=False
    )
    # discover_inboxes() also returns the daemon's OWN <vault_path>/inbox, and
    # DEFAULT_CONFIG.vault_path is the real ~/.minni/vault — so without this a
    # health test reads the operator's live inbox and its result depends on
    # whatever is sitting there.
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"), raising=False
    )
    monkeypatch.setattr(db_mod, "_migrations_run", False, raising=False)
    monkeypatch.setattr(minnid, "SovereignDB", db_mod.SovereignDB)
    return db_path


def test_health_report_surfaces_the_memory_lifecycle_queues(tmp_path, monkeypatch):
    """M4/M5 (#229): each queue must appear on a health surface with a REAL
    count — the live dead-letter backlog is only visible before it is drained,
    and an orphaned fence is only visible if the DB half actually runs."""
    import minni.minnid as minnid
    from minni.principal import EffectivePrincipal

    _real_lifecycle_db(tmp_path, monkeypatch)
    inbox = tmp_path / "unknown-vault" / "inbox"
    _afm_file(inbox, "afm-pruning-2026-01-01.json", age_days=61.0)
    _afm_file(inbox, "afm-drafts-2026-01-01.json", age_days=61.0)

    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]

    ml = rep["memory_lifecycle"]
    assert ml["afm_dead_letter"]["files"] == 2, ml
    assert ml["afm_dead_letter"]["oldest_age_days"] >= 60.0, ml
    assert ml["afm_review_orphans"] == 1, ml
    assert ml["proposed_queue"]["depth"] == 1, ml
    assert ml["proposed_queue"]["stale"] == 1, ml


def test_memory_lifecycle_reports_unknown_rather_than_a_healthy_zero(tmp_path, monkeypatch):
    """A scan that never ran must not read as '0 files' / 'depth 0'. Leaving
    the zero-valued default in place is the overstatement this block exists
    to remove."""
    import minni.minnid as minnid
    from minni.principal import EffectivePrincipal

    _real_lifecycle_db(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(
        "minni.afm_passes.inbox_quarantine.count_afm_dead_letter", _boom,
    )

    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]

    dead = rep["memory_lifecycle"]["afm_dead_letter"]
    assert dead["status"] == "unknown"
    assert dead["error"] == "RuntimeError"
    assert dead["files"] is None, "a failed scan must not report zero files"
    # The independent DB half still succeeded.
    assert rep["memory_lifecycle"]["afm_review_orphans"] == 1


def test_memory_lifecycle_block_reports_no_paths(tmp_path, monkeypatch):
    """Aggregate-only is a hard contract: this block sits OUTSIDE
    _HEALTH_REPORT_SENSITIVE_KEYS, so it must never carry a filesystem path."""
    import json as _json

    import minni.minnid as minnid
    from minni.principal import EffectivePrincipal

    _real_lifecycle_db(tmp_path, monkeypatch)
    inbox = tmp_path / "unknown-vault" / "inbox"
    _afm_file(inbox, "afm-pruning-2026-01-01.json", age_days=61.0)

    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]

    blob = _json.dumps(rep["memory_lifecycle"])
    assert str(tmp_path) not in blob
    assert "afm-pruning" not in blob


def test_a_corrupt_dead_letter_file_is_counted_and_drainable(tmp_path):
    """Both writers do a non-atomic read-modify-write on the same dated file,
    so a crash mid-write leaves a truncated payload. Skipping those made them
    invisible to the count AND undrainable — permanently stuck, which is the
    exact class this drain exists to remove."""
    from minni.afm_passes.inbox_quarantine import (
        count_afm_dead_letter,
        quarantine_afm_dead_letter,
    )

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    good = _afm_file(inbox, "afm-drafts-2026-01-01.json", age_days=61.0)
    truncated = inbox / "afm-drafts-2026-01-02.json"
    truncated.write_text('{"pass_name": "drafts", "dra', encoding="utf-8")
    listish = inbox / "afm-pruning-2026-01-03.json"
    listish.write_text("[]", encoding="utf-8")
    for p in (truncated, listish):
        old = _time.time() - 61 * 86400
        os.utime(p, (old, old))

    stats = count_afm_dead_letter([inbox])
    assert stats["files"] == 3, stats
    assert stats["unreadable"] == 2, stats

    result = quarantine_afm_dead_letter(None, [inbox], dry_run=False)
    assert result["quarantined"] == 3
    assert not list(inbox.glob("*.json")), "nothing may be left stuck"
    assert good.name in {p.name for p in _moved(inbox)}

    reasons = {
        json.loads(s.read_text())["reason"]
        for s in (inbox / "quarantine").glob("*.reason.json")
    }
    assert reasons == {"_afm_dead_letter", "_afm_dead_letter_unreadable"}


def test_a_different_afm_prefixed_writer_is_left_alone(tmp_path):
    """Scoped to the two NAMES, not to the `afm-` prefix: a future afm-* writer
    that does have a reader must not be swept up by this drain."""
    from minni.afm_passes.inbox_quarantine import (
        count_afm_dead_letter,
        quarantine_afm_dead_letter,
    )

    inbox = tmp_path / "codex-vault" / "inbox"
    _afm_file(inbox, "afm-handoffs-2026-01-01.json", age_days=61.0)

    assert quarantine_afm_dead_letter(None, [inbox], dry_run=False)["quarantined"] == 0
    assert count_afm_dead_letter([inbox])["files"] == 0
    assert list(inbox.glob("afm-handoffs-*.json"))


def test_a_corrupt_non_afm_file_is_still_left_alone(tmp_path):
    """Claiming unreadable files must not widen the drain past the two names."""
    from minni.afm_passes.inbox_quarantine import (
        count_afm_dead_letter,
        quarantine_afm_dead_letter,
    )

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    other = inbox / "some-other-writer-2026-01-01.json"
    other.write_text('{"truncated', encoding="utf-8")
    old = _time.time() - 61 * 86400
    os.utime(other, (old, old))

    assert count_afm_dead_letter([inbox])["files"] == 0
    assert quarantine_afm_dead_letter(None, [inbox], dry_run=False)["quarantined"] == 0
    assert other.exists()


def test_degraded_queue_half_reports_unknown_not_zero(tmp_path, monkeypatch):
    """The DB half's failure shape needs its own pin: only the dead-letter
    twin was covered, and a mutant restoring `orphans = 0` survived."""
    import minni.minnid as minnid
    from minni.principal import EffectivePrincipal

    _real_lifecycle_db(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("queue scan failed")

    monkeypatch.setattr("minni.afm_review_markers.count_orphaned_afm_review", _boom)

    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]

    ml = rep["memory_lifecycle"]
    assert ml["afm_review_orphans"] is None, "a scan that never ran is not zero"
    assert ml["proposed_queue"]["status"] == "unknown"
    assert ml["proposed_queue"]["depth"] is None
    # The independent filesystem half still succeeded.
    assert ml["afm_dead_letter"]["files"] == 0


def test_a_corrupt_only_backlog_still_reaches_the_drain(tmp_path, monkeypatch):
    """Bugbot #305: a corrupt file hits inbox_ingest's bare `continue` and
    increments NO skip counter, so a drain gated solely on _unrecognized never
    fires. With only corrupt files left, the very files the drain was taught to
    claim became unreachable — they accumulate past TTL forever."""
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
    inbox.mkdir(parents=True)
    # ONLY corrupt files: nothing here can increment _unrecognized.
    truncated = inbox / "afm-drafts-2026-01-01.json"
    truncated.write_text('{"pass_name": "drafts", "dra', encoding="utf-8")
    listish = inbox / "afm-pruning-2026-01-02.json"
    listish.write_text("[]", encoding="utf-8")
    old = _time.time() - 61 * 86400
    for p in (truncated, listish):
        os.utime(p, (old, old))

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        asyncio.run(afm_loop_runner(ctx))

        assert not list(inbox.glob("*.json")), "corrupt files must still be drained"
        assert (inbox / "quarantine" / truncated.name).is_file()
    finally:
        obs.METRICS.reset()

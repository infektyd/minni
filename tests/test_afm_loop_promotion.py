"""Issue #119 regressions: AFM background consolidation end-to-end.

Defect 1: afm_loop_runner built wet-run params without "_principal", so
handle_daemon_compile's operator gate rejected every background dry_run=false
call and the loop silently no-oped (examined=0, success-shaped logs).

Defect 2: no migration created consolidation_actions — the consolidation pass
and the daemon's promote/dedup/review helpers read it, but only test fixtures
created it, so fresh installs failed with "no such table".
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
import types

sys.path.insert(0, os.path.dirname(__file__))

from minni.principal import EffectivePrincipal, is_operator_principal


# ── Defect 2: migration 014 creates consolidation_actions ───────────────────


def test_migration_014_creates_consolidation_actions(tmp_path):
    """Fresh DB → run_migrations → consolidation_actions exists with the
    columns the engine reads/writes (schema per the historical test fixtures)."""
    from minni.migrations import run_migrations

    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    try:
        run_migrations(conn)

        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='consolidation_actions'"
        ).fetchone()
        assert row is not None, "migration 014 must create consolidation_actions"

        cols = {r[1] for r in conn.execute("PRAGMA table_info(consolidation_actions)")}
        assert cols == {
            "action_id", "action_type", "source_event_id", "target_learning_id",
            "superseded_learning_id", "claim", "category", "confidence",
            "status", "detail", "created_at",
        }

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 14

        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=14"
        ).fetchone()
        assert applied is not None
    finally:
        conn.close()


def test_migration_014_idempotent_over_adhoc_table(tmp_path):
    """Long-lived installs that already created the table ad-hoc must migrate
    cleanly (CREATE TABLE IF NOT EXISTS)."""
    from minni.migrations import run_migrations

    conn = sqlite3.connect(str(tmp_path / "adhoc.db"))
    try:
        conn.execute(
            """
            CREATE TABLE consolidation_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                source_event_id INTEGER,
                target_learning_id INTEGER,
                superseded_learning_id INTEGER,
                claim TEXT,
                category TEXT,
                confidence REAL,
                status TEXT DEFAULT 'pending',
                detail TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        run_migrations(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 14
    finally:
        conn.close()


# ── Defect 1: the loop's synthesized principal passes the operator gate ──────


def test_loop_principal_is_daemon_internal_operator():
    from minni.minnid_runtime.afm import loop_principal

    p = loop_principal()
    assert isinstance(p, EffectivePrincipal)
    assert is_operator_principal(p), "loop principal must pass the wet-run gate"
    # Audit trail: attributed to the loop, not an operator identity.
    assert p.agent_id == "afm-loop"
    assert p.transport == "internal"


# ── Integration: MINNI_AFM_LOOP=on, one tick promotes a safe candidate ───────


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "loop.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
        afm_loop_schedule={
            "enabled": True,
            "idle_seconds": 0,
            "passes": {
                "consolidation": {
                    "interval_seconds": 3600,
                    "ingest_inbox": False,
                    "max_batches_per_tick": 5,
                },
            },
        },
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _seed_safe_candidate(db_obj, content):
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             instruction_like, status, proposed_at)
            VALUES ('codex', 'default', NULL, 'safe', ?, 0, 'proposed', ?)
            """,
            (content, time.time()),
        )
        return c.lastrowid


def _loop_context(db_obj, cfg, *, ticks):
    """Real-enough AFMContext: real guard_vault_root, real SovereignDB,
    is_running yields `ticks` True checks then False."""
    from minni.minnid_runtime import afm as afm_mod
    from minni.minnid_runtime.provenance import guard_vault_root

    remaining = {"n": ticks}

    def _is_running():
        remaining["n"] -= 1
        return remaining["n"] >= 0

    def _make_error(code, message, request_id):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _make_response(result, request_id):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    traces = []

    wb = types.SimpleNamespace(db=db_obj, model=None, config=cfg)
    ctx = afm_mod.AFMContext(
        make_error=_make_error,
        make_response=_make_response,
        guard_vault_root=guard_vault_root,
        lazy_writeback=lambda: wb,
        trace_ring=lambda: types.SimpleNamespace(
            put=lambda tid, payload, owner=None: traces.append((tid, payload, owner))
        ),
        record_latency=lambda *a, **k: None,
        maybe_archive_inbox_source=lambda *a, **k: None,
        sovereign_db=lambda *a, **k: db_obj,
        default_config=cfg,
        is_running=_is_running,
    )
    return ctx, traces


def test_afm_loop_tick_promotes_safe_proposed_candidate(tmp_path, monkeypatch):
    """MINNI_AFM_LOOP=on: seed a privacy='safe' proposed candidate, run one loop
    tick, and the candidate is promoted to a durable active learning."""
    from minni.minnid_runtime.afm import afm_loop_runner

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_db(tmp_path)
    cid = _seed_safe_candidate(
        db_obj, "Durable lesson: pin the schema version before renaming columns."
    )
    ctx, traces = _loop_context(db_obj, cfg, ticks=1)

    asyncio.run(afm_loop_runner(ctx))

    with db_obj.cursor() as c:
        c.execute("SELECT status, resolved_by FROM candidate_packets WHERE candidate_id=?", (cid,))
        cand = dict(c.fetchone())
        c.execute("SELECT status, source_query, content FROM learnings")
        learnings = [dict(r) for r in c.fetchall()]
        c.execute(
            "SELECT action_type, status FROM consolidation_actions WHERE action_type='afm_promote'"
        )
        actions = [dict(r) for r in c.fetchall()]

    assert cand["status"] == "accepted", cand
    assert cand["resolved_by"] == "afm-consolidation"
    assert len(learnings) == 1, learnings
    assert learnings[0]["status"] == "active"
    assert learnings[0]["source_query"] == "afm-consolidation"
    assert "pin the schema version" in learnings[0]["content"]
    # Audit: promote recorded in consolidation_actions (table exists via
    # migration 014 — nothing in this test created it by hand)...
    assert actions and actions[0]["status"] == "applied"
    # ...and the compile trace is owned by the daemon-internal loop principal.
    assert traces and traces[-1][2] == "afm-loop"


def test_afm_loop_tick_routes_unsafe_privacy_to_review_not_promote(tmp_path, monkeypatch):
    """The stamp must not widen the gate: non-safe privacy still goes to review,
    never auto-promotes."""
    from minni.minnid_runtime.afm import afm_loop_runner

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_db(tmp_path)
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, privacy_level, content,
             instruction_like, status, proposed_at)
            VALUES ('codex', 'default', 'sensitive', ?, 0, 'proposed', ?)
            """,
            ("A sensitive detail that must never auto-promote.", time.time()),
        )
        cid = c.lastrowid
    ctx, _ = _loop_context(db_obj, cfg, ticks=1)

    asyncio.run(afm_loop_runner(ctx))

    with db_obj.cursor() as c:
        c.execute("SELECT status FROM candidate_packets WHERE candidate_id=?", (cid,))
        assert c.fetchone()["status"] == "proposed"  # held for review, not accepted
        c.execute("SELECT COUNT(*) AS n FROM learnings")
        assert c.fetchone()["n"] == 0
        c.execute(
            "SELECT 1 FROM consolidation_actions WHERE action_type='afm_review' AND claim=?",
            (str(cid),),
        )
        assert c.fetchone() is not None


# ── Finding 1: learn-only stage_candidate cannot stamp auto-promotable privacy ─


def test_learn_only_stage_candidate_clamps_safe_privacy_to_review(monkeypatch, tmp_path):
    """A learn-capable non-operator that requests privacy_level=safe must store
    'review' so AFM consolidation cannot auto-promote past resolve_candidate."""
    import types

    import minni.minnid as minnid
    import minni.minnid_runtime.provenance as provenance
    from minni.principal import EffectivePrincipal

    db_obj, _cfg = _make_db(tmp_path)
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    monkeypatch.setattr(
        provenance,
        "resolve_effective_principal",
        lambda **_kw: EffectivePrincipal(agent_id="codex", capabilities=["learn"]),
    )

    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "stage_candidate",
            "params": {
                "agent_id": "codex",
                "content": "Lesson that a learn-only agent must not auto-promote.",
                "privacy_level": "safe",
            },
        }
    )
    assert "result" in resp, resp
    assert resp["result"]["privacy_level"] == "review"
    cid = resp["result"]["candidate_id"]
    with db_obj.cursor() as c:
        c.execute(
            "SELECT privacy_level FROM candidate_packets WHERE candidate_id=?",
            (cid,),
        )
        assert dict(c.fetchone())["privacy_level"] == "review"


def test_govern_stage_candidate_keeps_safe_privacy(monkeypatch, tmp_path):
    """Operator/govern staging may still stamp safe for trusted AFM-loop promotion."""
    import types

    import minni.minnid as minnid
    import minni.minnid_runtime.provenance as provenance
    from minni.principal import EffectivePrincipal

    db_obj, _cfg = _make_db(tmp_path)
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    monkeypatch.setattr(
        provenance,
        "resolve_effective_principal",
        lambda **_kw: EffectivePrincipal(
            agent_id="main", capabilities=["learn", "govern"]
        ),
    )

    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "stage_candidate",
            "params": {
                "agent_id": "main",
                "content": "Operator-endorsed lesson eligible for AFM promotion.",
                "privacy_level": "safe",
            },
        }
    )
    assert "result" in resp, resp
    assert resp["result"]["privacy_level"] == "safe"
    cid = resp["result"]["candidate_id"]
    with db_obj.cursor() as c:
        c.execute(
            "SELECT privacy_level FROM candidate_packets WHERE candidate_id=?",
            (cid,),
        )
        assert dict(c.fetchone())["privacy_level"] == "safe"


def test_main_learn_only_stage_candidate_clamps_safe_privacy(monkeypatch, tmp_path):
    """Finding 1: main + learn (no govern) must not keep caller privacy=safe."""
    import types

    import minni.minnid as minnid
    import minni.minnid_runtime.provenance as provenance
    from minni.principal import EffectivePrincipal

    db_obj, _cfg = _make_db(tmp_path)
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    monkeypatch.setattr(
        provenance,
        "resolve_effective_principal",
        lambda **_kw: EffectivePrincipal(agent_id="main", capabilities=["learn"]),
    )

    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "stage_candidate",
            "params": {
                "agent_id": "main",
                "content": "main learn-only must not auto-promote.",
                "privacy_level": "safe",
            },
        }
    )
    assert "result" in resp, resp
    assert resp["result"]["privacy_level"] == "review"


def test_afm_loop_does_not_promote_learn_only_clamped_candidate(tmp_path, monkeypatch):
    """End-to-end: learn-only stage with privacy=safe → review clamp → loop holds."""
    import types

    import minni.minnid as minnid
    import minni.minnid_runtime.provenance as provenance
    from minni.minnid_runtime.afm import afm_loop_runner
    from minni.principal import EffectivePrincipal

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_db(tmp_path)
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    monkeypatch.setattr(
        provenance,
        "resolve_effective_principal",
        lambda **_kw: EffectivePrincipal(agent_id="codex", capabilities=["learn"]),
    )
    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "stage_candidate",
            "params": {
                "agent_id": "codex",
                "content": "Should stay in review despite caller privacy=safe.",
                "privacy_level": "safe",
            },
        }
    )
    cid = resp["result"]["candidate_id"]
    ctx, _ = _loop_context(db_obj, cfg, ticks=1)
    asyncio.run(afm_loop_runner(ctx))

    with db_obj.cursor() as c:
        c.execute("SELECT status, privacy_level FROM candidate_packets WHERE candidate_id=?", (cid,))
        row = dict(c.fetchone())
        assert row["privacy_level"] == "review"
        assert row["status"] == "proposed"
        c.execute("SELECT COUNT(*) AS n FROM learnings")
        assert c.fetchone()["n"] == 0

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {"session_distillation": {"interval_seconds": 3600}}},
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _seed_event(db_obj):
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO episodic_events
            (agent_id, event_type, content, task_id, thread_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "codex",
                "session",
                "Built the PR-12 AFM loop. Important concept: drafts require source citations.",
                "task-12",
                "thread-12",
                json.dumps({"source": "unit-test"}),
                now,
            ),
        )
        return c.lastrowid


def test_session_distillation_dry_run_returns_drafts_without_writing(tmp_path):
    from afm_passes.session_distillation import run

    db_obj, cfg = _make_db(tmp_path)
    event_id = _seed_event(db_obj)

    result = run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["drafts"]
    assert result["drafts"][0]["status"] == "draft"
    assert result["drafts"][0]["agent"] == "afm-loop"
    assert f"episodic_events:{event_id}" in result["drafts"][0]["sources"]
    assert not (tmp_path / "vault" / "inbox").exists()


def test_daemon_compile_wet_run_writes_draft_inbox_and_trace(tmp_path, monkeypatch):
    import sovrd

    db_obj, cfg = _make_db(tmp_path)
    _seed_event(db_obj)
    monkeypatch.setattr(sovrd, "DEFAULT_CONFIG", cfg)
    monkeypatch.setattr(sovrd, "_writeback", None)
    monkeypatch.setattr(sovrd, "SovereignDB", lambda config=None: db_obj)

    resp = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "daemon.compile",
            "params": {"pass_name": "session_distillation", "vault_path": cfg.vault_path, "dry_run": False},
        }
    )

    assert "error" not in resp
    result = resp["result"]
    assert result["status"] == "ok"
    assert result["dry_run"] is False
    assert result["drafts_written"]
    page_path = tmp_path / "vault" / result["drafts_written"][0]["path"]
    text = page_path.read_text(encoding="utf-8")
    assert "status: draft" in text
    assert "agent: afm-loop" in text
    assert "trace_id:" in text
    assert "status: accepted" not in text
    inbox_files = list((tmp_path / "vault" / "inbox").glob("afm-drafts-*.json"))
    assert inbox_files
    assert "afm_loop" in (tmp_path / "vault" / "log.md").read_text(encoding="utf-8")

    trace = sovrd._dispatch({"jsonrpc": "2.0", "id": 2, "method": "trace", "params": {"trace_id": result["trace_id"]}})
    assert trace["result"]["status"] == "ok"
    assert trace["result"]["trace"]["pass_name"] == "session_distillation"


def test_daemon_endorse_accepts_draft_and_audits(tmp_path, monkeypatch):
    import sovrd

    db_obj, cfg = _make_db(tmp_path)
    _seed_event(db_obj)
    monkeypatch.setattr(sovrd, "DEFAULT_CONFIG", cfg)
    monkeypatch.setattr(sovrd, "SovereignDB", lambda config=None: db_obj)
    compile_resp = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "daemon.compile",
            "params": {"pass_name": "session_distillation", "vault_path": cfg.vault_path, "dry_run": False},
        }
    )
    page_id = compile_resp["result"]["drafts_written"][0]["page_id"]

    resp = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "daemon.endorse",
            "params": {"page_id": page_id, "decision": "accept", "vault_path": cfg.vault_path},
        }
    )

    assert resp["result"]["status"] == "accepted"
    page_path = tmp_path / "vault" / compile_resp["result"]["drafts_written"][0]["path"]
    assert "status: accepted" in page_path.read_text(encoding="utf-8")
    assert "afm_endorse" in (tmp_path / "vault" / "log.md").read_text(encoding="utf-8")


def test_compile_skips_cleanly_when_afm_loop_disabled(tmp_path, monkeypatch):
    import sovrd

    db_obj, cfg = _make_db(tmp_path)
    cfg.afm_loop_schedule["enabled"] = False
    _seed_event(db_obj)
    monkeypatch.setattr(sovrd, "DEFAULT_CONFIG", cfg)
    monkeypatch.setattr(sovrd, "SovereignDB", lambda config=None: db_obj)

    resp = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "daemon.compile",
            "params": {"pass_name": "session_distillation", "vault_path": cfg.vault_path, "dry_run": False},
        }
    )

    assert resp["result"]["status"] == "afm_unavailable"
    assert not (tmp_path / "vault" / "inbox").exists()


def test_health_report_includes_afm_loop(tmp_path, monkeypatch):
    import sovrd

    db_obj, cfg = _make_db(tmp_path)
    monkeypatch.setattr(sovrd, "DEFAULT_CONFIG", cfg)
    monkeypatch.setattr(sovrd, "SovereignDB", lambda config=None: db_obj)

    resp = sovrd._dispatch({"jsonrpc": "2.0", "id": 1, "method": "health_report", "params": {}})
    assert "error" not in resp
    afm_loop = resp["result"]["afm_loop"]
    assert set(["last_run_per_pass", "drafts_pending", "drafts_pending_oldest", "afm_latency_p95", "status"]).issubset(afm_loop)


def test_scheduler_runs_most_overdue_pass_only_when_idle(tmp_path):
    from afm_scheduler import AFMScheduler
    from config import SovereignConfig

    calls = []
    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        afm_loop_schedule={
            "enabled": True,
            "idle_seconds": 300,
            "passes": {"session_distillation": {"interval_seconds": 3600}},
        },
    )
    scheduler = AFMScheduler(cfg, lambda name: calls.append(name) or {"status": "ok"}, interval_seconds=999)
    scheduler.last_activity_ts = time.time()
    assert scheduler.tick() is None
    scheduler.last_activity_ts = time.time() - 301
    assert scheduler.tick() == {"status": "ok"}
    assert calls == ["session_distillation"]

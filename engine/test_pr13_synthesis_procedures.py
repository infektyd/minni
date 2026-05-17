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
        afm_loop_schedule={
            "enabled": True,
            "idle_seconds": 300,
            "passes": {
                "session_distillation": {"interval_seconds": 3600},
                "synthesis": {"interval_seconds": 86400, "stale_after_days": 30},
                "procedure_extraction": {"interval_seconds": 86400, "lookback_days": 90},
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


def _write_page(vault, rel, title, body, *, page_type="concept", status="accepted", tags=None, sources=None):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = tags or []
    sources = sources or [f"test:{title.lower().replace(' ', '-')}"]
    text = (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        f"status: {status}\n"
        "privacy: safe\n"
        f"updated: 2026-04-20T00:00:00Z\n"
        f"tags: [{', '.join(tags)}]\n"
        f"sources: [{', '.join(sources)}]\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def _seed_repeated_events(db_obj):
    now = time.time()
    pattern = "agent exported the vault, then checked the index, then wrote the audit note"
    with db_obj.cursor() as c:
        ids = []
        for idx in range(3):
            c.execute(
                """
                INSERT INTO episodic_events
                (agent_id, event_type, content, task_id, thread_id, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "codex",
                    "session",
                    f"In run {idx}, {pattern}.",
                    f"task-{idx}",
                    f"thread-{idx}",
                    json.dumps({"source": "unit-test"}),
                    now - idx,
                ),
            )
            ids.append(c.lastrowid)
        return ids


def test_synthesis_dry_run_returns_tag_cluster_draft_without_writing(tmp_path):
    from afm_passes.synthesis import run

    db_obj, cfg = _make_db(tmp_path)
    vault = tmp_path / "vault"
    for idx in range(3):
        _write_page(
            vault,
            f"wiki/concepts/evidence-{idx}.md",
            f"Evidence {idx}",
            f"Shared note {idx} about durable recall. [[Evidence {(idx + 1) % 3}]]",
            tags=["memory-loop", "codex"],
        )

    result = run(db_obj, cfg, vault_path=str(vault), dry_run=True, trace_id="trace-synth")

    assert result["status"] == "ok"
    assert result["pass_name"] == "synthesis"
    assert result["dry_run"] is True
    assert result["prompt_version"]
    assert result["drafts"]
    draft = result["drafts"][0]
    assert draft["kind"] == "synthesis"
    assert draft["status"] == "draft"
    assert draft["agent"] == "afm-loop"
    assert draft["trace_id"] == "trace-synth"
    assert "tag:memory-loop" in draft["sources"]
    assert sum(source.startswith("vault:wiki/concepts/evidence-") for source in draft["sources"]) == 3
    assert not (vault / "inbox").exists()


def test_procedure_extraction_dry_run_returns_repeated_pattern_draft(tmp_path):
    from afm_passes.procedure_extraction import run

    db_obj, cfg = _make_db(tmp_path)
    event_ids = _seed_repeated_events(db_obj)

    result = run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="trace-proc")

    assert result["status"] == "ok"
    assert result["pass_name"] == "procedure_extraction"
    assert result["prompt_version"]
    assert result["drafts"]
    draft = result["drafts"][0]
    assert draft["kind"] == "procedure"
    assert draft["section"] == "procedures"
    assert draft["status"] == "draft"
    assert draft["agent"] == "afm-loop"
    assert draft["trace_id"] == "trace-proc"
    assert all(f"episodic_events:{event_id}" in draft["sources"] for event_id in event_ids)
    assert "exported the vault" in draft["body"]


def test_daemon_compile_reaches_pr13_passes_and_wet_run_keeps_drafts_pending(tmp_path, monkeypatch):
    import sovrd

    db_obj, cfg = _make_db(tmp_path)
    vault = tmp_path / "vault"
    for idx in range(3):
        _write_page(
            vault,
            f"wiki/concepts/source-{idx}.md",
            f"Source {idx}",
            f"Source {idx} says review gates require human endorsement.",
            tags=["endorsement"],
        )
    _seed_repeated_events(db_obj)
    monkeypatch.setattr(sovrd, "DEFAULT_CONFIG", cfg)
    monkeypatch.setattr(sovrd, "_writeback", None)
    monkeypatch.setattr(sovrd, "SovereignDB", lambda config=None: db_obj)

    synth_resp = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "daemon.compile",
            "params": {"pass_name": "synthesis", "vault_path": str(vault), "dry_run": False},
        }
    )
    proc_resp = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "daemon.compile",
            "params": {"pass_name": "procedure_extraction", "vault_path": str(vault), "dry_run": False},
        }
    )

    for resp, expected_pass in ((synth_resp, "synthesis"), (proc_resp, "procedure_extraction")):
        assert "error" not in resp
        result = resp["result"]
        assert result["status"] == "ok"
        assert result["drafts_written"]
        page_path = vault / result["drafts_written"][0]["path"]
        text = page_path.read_text(encoding="utf-8")
        assert "status: draft" in text
        assert "agent: afm-loop" in text
        assert "trace_id:" in text
        assert "status: accepted" not in text
        trace = sovrd._dispatch({"jsonrpc": "2.0", "id": 3, "method": "trace", "params": {"trace_id": result["trace_id"]}})
        assert trace["result"]["trace"]["pass_name"] == expected_pass
        assert trace["result"]["trace"]["prompt_version"]

    audit = (vault / "log.md").read_text(encoding="utf-8")
    assert "synthesis wrote" in audit
    assert "procedure_extraction wrote" in audit

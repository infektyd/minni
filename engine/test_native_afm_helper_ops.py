"""Focused tests for the additive native AFM helper-op wiring.

Each test mocks the canonical afm_provider.invoke_native_afm boundary (the same
seam the provider chain resolves at call time) keyed by operation, so no live
model is required. All wiring here is ADDITIVE and review-only: these tests
assert the new signals appear WITHOUT changing the existing draft/decision path.
"""

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
            "passes": {"session_distillation": {"interval_seconds": 3600}},
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
                "Built the native AFM helper wiring. Important concept: ops stay review-only.",
                "task-x",
                "thread-x",
                json.dumps({"source": "unit-test"}),
                now,
            ),
        )
        return c.lastrowid


class _AFMResult:
    """Stand-in matching afm_provider.AFMResult for the fields the chain reads."""

    def __init__(self, ok, data, status="native_available", error=None, provider="native"):
        self.ok = ok
        self.data = data
        self.status = status
        self.error = error
        self.provider = provider


def _route(op_map):
    """Build a fake invoke_native_afm that returns per-operation results."""

    def fake_native(operation, payload, timeout=2.0):
        if operation in op_map:
            return op_map[operation](payload)
        return _AFMResult(ok=True, data={})

    return fake_native


# --- Task 2: session_distill emits ONE extra review-only draft -------------


def test_session_distill_emits_additive_review_draft(tmp_path, monkeypatch):
    from afm_passes.session_distillation import run

    db_obj, cfg = _make_db(tmp_path)
    event_id = _seed_event(db_obj)

    op_map = {
        "session_distill": lambda p: _AFMResult(
            ok=True,
            data={
                "title": "Helper ops stay review-only",
                "assertion": "Native AFM ops only ever propose; they never auto-accept.",
                "appliesWhen": "running the AFM session distillation pass",
                "category": "principle",
            },
        )
    }
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="trace-distill")

    distill = [
        d for d in result["drafts"]
        if d.get("prompt_version") == "native.session_distill.v1"
    ]
    assert len(distill) == 1
    draft = distill[0]
    assert draft["status"] == "draft"
    assert draft["agent"] == "afm-loop"
    assert draft["title"] == "Helper ops stay review-only"
    assert draft["sources"]  # carries citations from the deterministic drafts
    assert f"episodic_events:{event_id}" in draft["sources"]
    # Additive: the original deterministic session draft is still present.
    assert any(d.get("kind") == "session" for d in result["drafts"])
    assert result["inputs"]["session_distill_draft_count"] == 1


def test_session_distill_missing_fields_emits_no_draft(tmp_path, monkeypatch):
    from afm_passes.session_distillation import run

    db_obj, cfg = _make_db(tmp_path)
    _seed_event(db_obj)

    op_map = {
        # No title/assertion -> must not produce a draft.
        "session_distill": lambda p: _AFMResult(ok=True, data={"category": "x"}),
    }
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t")
    assert result["inputs"]["session_distill_draft_count"] == 0
    assert not [
        d for d in result["drafts"]
        if d.get("prompt_version") == "native.session_distill.v1"
    ]


# --- Task 3: entity_extract surfaces entities additively -------------------


def test_entity_extract_surfaces_entities_in_inputs(tmp_path, monkeypatch):
    from afm_passes.session_distillation import run

    db_obj, cfg = _make_db(tmp_path)
    _seed_event(db_obj)

    op_map = {
        "entity_extract": lambda p: _AFMResult(
            ok=True,
            data={
                "entities": [
                    {"name": "AFM helper", "type": "system"},
                    {"name": "AFM helper", "type": "system"},  # dedup
                    {"name": "Codex", "type": "agent"},
                    {"name": "", "type": "noise"},  # dropped
                ]
            },
        )
    }
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t")

    entities = result["inputs"]["entities"]
    names = [e["name"] for e in entities]
    assert "AFM helper" in names
    assert "Codex" in names
    assert "" not in names
    assert len(names) == len(set(n.lower() for n in names))  # deduped


# --- Task 4: contradiction advisory signal ---------------------------------


def test_contradiction_signal_emitted_against_existing_learning(tmp_path, monkeypatch):
    from afm_passes.session_distillation import run

    db_obj, cfg = _make_db(tmp_path)
    _seed_event(db_obj)
    # Seed an existing learning so the FTS lookup finds something.
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO learnings (agent_id, category, content, confidence, created_at)
            VALUES (?, 'general', ?, 0.9, ?)
            """,
            ("codex", "Native AFM helper ops must always auto-accept proposals.", time.time()),
        )

    op_map = {
        "session_distill": lambda p: _AFMResult(
            ok=True,
            data={
                "title": "Helper ops stay review-only",
                "assertion": "Native AFM ops only ever propose; they never auto-accept.",
                "appliesWhen": "the AFM pass",
                "category": "principle",
            },
        ),
        "contradiction": lambda p: _AFMResult(
            ok=True,
            data={"contradicts": True, "reason": "one says auto-accept, the other says review-only"},
        ),
    }
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t")

    signal = result["inputs"].get("contradiction")
    assert signal is not None
    assert signal["contradicts"] is True
    assert "auto-accept" in signal["reason"] or "review-only" in signal["reason"]
    assert signal["candidate_page_id"]


def test_contradiction_skipped_when_no_existing_learning(tmp_path, monkeypatch):
    from afm_passes.session_distillation import run

    db_obj, cfg = _make_db(tmp_path)
    _seed_event(db_obj)

    op_map = {
        "contradiction": lambda p: _AFMResult(ok=True, data={"contradicts": True, "reason": "x"}),
    }
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t")
    # No existing learnings -> no contradiction call fires -> no signal.
    assert result["inputs"].get("contradiction") is None


# --- Task 1: error_kind surfacing ------------------------------------------


def test_error_kind_logged_distinctly_on_native_failure(tmp_path, monkeypatch, caplog):
    import logging
    from afm_passes.session_distillation import run

    db_obj, cfg = _make_db(tmp_path)
    _seed_event(db_obj)

    op_map = {
        "compile_pass_proposals": lambda p: _AFMResult(
            ok=False,
            data={"error_kind": "context_overflow", "availability": "available"},
            status="native_error",
            error="context window exceeded",
        ),
    }
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    with caplog.at_level(logging.INFO, logger="sovereign.afm.session_distillation"):
        result = run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t")

    assert result["status"] == "ok"  # behavior otherwise unchanged
    assert any(
        "context_overflow" in rec.getMessage() and "recoverable" in rec.getMessage()
        for rec in caplog.records
    )


# --- Task 5: consolidation triage advisory (decisions unchanged) -----------


def _ensure_candidate_tables(db_obj):
    """candidate_packets lives in the numbered migrations (007); the lightweight
    SovereignDB bootstrap skips them. consolidation_actions is created lazily by
    the daemon at runtime, so create it here (DDL mirrors test_inbox_archive)."""
    from migrations import run_migrations

    run_migrations(db_obj._get_conn())
    with db_obj.cursor() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS consolidation_actions (
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


def _seed_candidate(db_obj, content, privacy="safe", instruction_like=0):
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             instruction_like, status, proposed_at)
            VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
            """,
            ("codex", "ws", "L2", privacy, content, instruction_like, now),
        )
        return c.lastrowid


def test_consolidation_triage_advisory_does_not_change_decisions(tmp_path, monkeypatch):
    from afm_passes import consolidation

    db_obj, cfg = _make_db(tmp_path)
    _ensure_candidate_tables(db_obj)
    cid = _seed_candidate(db_obj, "A plausible durable fact about the build pipeline cache.")

    op_map = {
        # Helper says reject; the deterministic gate must IGNORE this and promote.
        "triage": lambda p: _AFMResult(
            ok=True,
            data={"decision": "reject", "reason": "model dislikes it", "tool_used": True},
        ),
    }
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = consolidation.run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t")

    # Deterministic gate authoritative: candidate is promoted regardless of triage.
    assert cid in result["promote_candidate_ids"]
    advisory = result["triage_advisory"]
    assert advisory is not None
    assert advisory["decision"] == "reject"
    assert advisory["tool_used"] is True
    assert advisory["candidate_id"] == cid
    assert "advisory" in advisory["note"]


def test_consolidation_triage_advisory_none_when_afm_off(tmp_path, monkeypatch):
    from afm_passes import consolidation

    db_obj, cfg = _make_db(tmp_path)
    _ensure_candidate_tables(db_obj)
    _seed_candidate(db_obj, "Another plausible durable fact for the cache layer.")

    monkeypatch.setenv("MINNI_AFM_MODE", "off")

    result = consolidation.run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t")
    assert result["triage_advisory"] is None

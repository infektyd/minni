"""PR91-2: session_distillation must bind to a single agent, never query
globally; the CLI consolidation entrypoint must propagate --agent/MINNI_AGENT_ID.
"""

from __future__ import annotations

import os
import types

import afm_passes.session_distillation as sd


def _capture_agent(monkeypatch):
    seen = {}

    def _fake_events(db, lookback_hours, agent_id=None):
        seen["events_agent"] = agent_id
        return []

    def _fake_docs(db, lookback_hours, agent_id=None):
        seen["docs_agent"] = agent_id
        return []

    monkeypatch.setattr(sd, "_recent_events", _fake_events)
    monkeypatch.setattr(sd, "_recent_raw_docs", _fake_docs)
    return seen


def _run(monkeypatch):
    cfg = types.SimpleNamespace(afm_loop_schedule={})
    return sd.run(db=object(), config=cfg, dry_run=True, trace_id="t")


def test_unbound_run_falls_back_to_unknown_not_global(monkeypatch):
    monkeypatch.delenv("MINNI_AGENT_ID", raising=False)
    seen = _capture_agent(monkeypatch)
    _run(monkeypatch)
    # The load-bearing assertion: NOT None (which queried globally before).
    assert seen["events_agent"] == "unknown"
    assert seen["docs_agent"] == "unknown"


def test_env_agent_binds_scope(monkeypatch):
    monkeypatch.setenv("MINNI_AGENT_ID", "codex")
    seen = _capture_agent(monkeypatch)
    _run(monkeypatch)
    assert seen["events_agent"] == "codex"
    assert seen["docs_agent"] == "codex"


def test_blank_env_agent_falls_back_to_unknown(monkeypatch):
    monkeypatch.setenv("MINNI_AGENT_ID", "   ")
    seen = _capture_agent(monkeypatch)
    _run(monkeypatch)
    assert seen["events_agent"] == "unknown"


def test_cli_compile_propagates_agent_to_env(monkeypatch):
    """sovereign_memory.py compile --agent X exports MINNI_AGENT_ID before the
    pass runs (verified even when the AFM loop is disabled and it returns early)."""
    import sovereign_memory

    monkeypatch.delenv("MINNI_AGENT_ID", raising=False)
    # Force the AFM loop "disabled" path so cmd_compile prints the unavailable
    # summary and returns AFTER parsing+propagating the agent — no heavy pass run.
    monkeypatch.setattr(sovereign_memory.DEFAULT_CONFIG, "afm_loop_schedule", {}, raising=False)
    sovereign_memory.cmd_compile(["--pass", "session_distillation", "--agent", "codex", "--dry-run"])
    assert os.environ.get("MINNI_AGENT_ID") == "codex"

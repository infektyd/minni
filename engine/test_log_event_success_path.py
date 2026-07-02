"""Issue #120 regression: the log_event success path must actually write.

The `log_event` RPC handler (minnid_runtime/governance.py handle_log_event) and
the in-process `SovereignAgent.log()` helper (agent_api.py) both called
``ep.log_event(...)`` — but ``EpisodicMemory`` only defines ``add_event``.
Every caller that cleared the capability gate got:

    AttributeError: 'EpisodicMemory' object has no attribute 'log_event'

The existing binding test (test_principal_binding.py) only exercises the
identity-mismatch rejection branch of ``_handle_log_event`` and never lets the
call reach a real ``EpisodicMemory``, so the break shipped undetected. These
tests drive the SUCCESS path end-to-end against a real ``EpisodicMemory`` on a
throwaway DB and assert an event_id comes back and a row lands in
``episodic_events``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minnid
import minnid_runtime.provenance as provenance
from principal import EffectivePrincipal


def _make_db(tmp_path: Path):
    """Real SovereignDB on a throwaway path, migrations forced (same idiom as
    test_rpc_capability_enforcement._patch_db)."""
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "log-event.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _episodic_rows(db_obj, agent_id: str):
    with db_obj.cursor() as c:
        c.execute(
            "SELECT event_id, agent_id, event_type, content, task_id, thread_id "
            "FROM episodic_events WHERE agent_id = ?",
            (agent_id,),
        )
        return c.fetchall()


def test_log_event_rpc_success_path_writes_episodic_event(monkeypatch, tmp_path):
    """Full dispatch (capability gate included) with an authorized principal
    must return an event_id and persist a row — not AttributeError -32000."""
    db_obj, cfg = _make_db(tmp_path)

    from episodic import EpisodicMemory

    ep = EpisodicMemory(db_obj, cfg)
    monkeypatch.setattr(minnid, "_lazy_episodic", lambda: ep)

    # Authorized principal that clears the log_event capability gate.
    stamped = EffectivePrincipal(agent_id="claude-code", capabilities=["log_event"])
    monkeypatch.setattr(
        provenance, "resolve_effective_principal", lambda **_kw: stamped
    )

    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "log_event",
            "params": {
                "agent_id": "claude-code",
                "event_type": "finding",
                "content": "authorized-hit",
                "task_id": "task-120",
            },
        }
    )

    assert "error" not in resp, resp
    result = resp["result"]
    assert result["agent_id"] == "claude-code"
    assert isinstance(result["event_id"], int)

    rows = _episodic_rows(db_obj, "claude-code")
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == result["event_id"]
    assert row["event_type"] == "finding"
    assert row["content"] == "authorized-hit"
    assert row["task_id"] == "task-120"


def test_agent_api_log_writes_episodic_event(tmp_path):
    """SovereignAgent.log() (the second broken caller) must persist an event."""
    db_obj, cfg = _make_db(tmp_path)

    from agent_api import SovereignAgent

    agent = SovereignAgent("codex", config=cfg, db=db_obj)
    event_id = agent.log(
        event_type="task_start",
        content="issue-120 regression",
        task_id="t-1",
        metadata={"source": "test"},
    )

    assert isinstance(event_id, int)
    rows = _episodic_rows(db_obj, "codex")
    assert len(rows) == 1
    assert rows[0]["event_id"] == event_id
    assert rows[0]["event_type"] == "task_start"
    assert rows[0]["content"] == "issue-120 regression"

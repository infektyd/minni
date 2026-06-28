"""P0 principal enforcement: stamped capabilities gate DB-backed RPC paths."""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minnid
from principal import EffectivePrincipal


def _patch_db(monkeypatch, tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "cap.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    return db_obj


def _stamp(monkeypatch, agent_id, capabilities):
    principal = EffectivePrincipal(agent_id=agent_id, capabilities=list(capabilities))
    monkeypatch.setattr(minnid, "resolve_effective_principal", lambda **_kw: principal)
    return principal


def _rpc(method, params, request_id=1):
    return minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    )


def test_restricted_principal_denied_search_without_search_cap(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["learn"])
    resp = _rpc("search", {"agent_id": "codex", "query": "token"})
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "capability_denied" in err.get("message", "")
    assert "search" in err.get("message", "")


def test_restricted_principal_denied_learn_without_learn_cap(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["search"])
    resp = _rpc("learn", {"agent_id": "codex", "content": "a lesson"})
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "learn" in err.get("message", "")


def test_restricted_principal_denied_stage_candidate_without_learn_cap(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["search"])
    resp = _rpc("stage_candidate", {"agent_id": "codex", "content": "candidate"})
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "learn" in err.get("message", "")


def test_restricted_principal_denied_read_without_read_cap(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["search"])
    resp = _rpc("read", {"agent_id": "codex", "limit": 1})
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "read" in err.get("message", "")


def test_default_deny_principal_denied_search(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", [])
    resp = _rpc("search", {"agent_id": "codex", "query": "token"})
    assert resp.get("error", {}).get("code") == -32004, resp


def test_explicit_caps_allow_search(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["search"])
    resp = _rpc("search", {"agent_id": "codex", "query": "nothing-matching", "expand": False})
    assert "error" not in resp, resp


def test_cross_agent_search_denied_without_elevated_cap(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["search"])
    resp = _rpc(
        "search",
        {"agent_id": "codex", "query": "token", "cross_agent": True, "expand": False},
    )
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "cross_agent" in err.get("message", "")


def test_cross_agent_search_allowed_with_wildcard_cap(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["*"])
    resp = _rpc(
        "search",
        {"agent_id": "codex", "query": "token", "cross_agent": True, "expand": False},
    )
    assert "error" not in resp, resp


def test_ax_snapshot_store_uses_stamped_principal_not_wire_agent(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    # ax_snapshot_store is a WRITE op (PR91-6): it requires the "learn" cap, so a
    # principal that can store snapshots needs it alongside "read".
    _stamp(monkeypatch, "codex", ["read", "learn"])
    captured = {}

    class _FakeAx:
        def add_snapshot(self, **kwargs):
            captured.update(kwargs)
            return "snap-1"

    import ax_memory

    monkeypatch.setattr(ax_memory, "AXMemory", lambda db: _FakeAx())
    resp = _rpc(
        "ax_snapshot_store",
        {
            "agent_id": "victim",
            "app_name": "Finder",
            "tree_json": "{}",
        },
    )
    assert "error" not in resp, resp
    assert captured.get("agent_id") == "codex"


def test_read_only_principal_denied_ax_snapshot_store(monkeypatch, tmp_path):
    """PR91-6: a read-only principal must NOT be able to write an AX snapshot."""
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    resp = _rpc(
        "ax_snapshot_store",
        {"agent_id": "codex", "app_name": "Finder", "tree_json": "{}"},
    )
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "learn" in err.get("message", "")


def test_read_only_principal_denied_resolve_contradiction(monkeypatch, tmp_path):
    """PR91-1: resolve_contradiction mutates learnings — deny a read-only principal
    at the capability gate before the owner check is even reached."""
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    resp = _rpc(
        "resolve_contradiction",
        {"agent_id": "codex", "new_content": "resolution", "supersede_ids": [1]},
    )
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "learn" in err.get("message", "")


def test_platform_agent_without_cap_map_does_not_inherit_operator_wildcard(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "platform_agent_ids": ["codex"],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)

    from principal import resolve_effective_principal

    p = resolve_effective_principal(
        supplied_agent_id="codex", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "codex"
    assert p.capabilities == []
    assert not p.can("search")

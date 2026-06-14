"""Scoped-by-default recall for learning rows.

All state is tmp_path-backed. These tests exercise the daemon search path that
surfaces learnings alongside document recall, while keeping the shared document
layer unscoped by default.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _make_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, aliases=None):
    import db as db_mod
    import minnid
    import principal as principal_mod
    import retrieval as retrieval_mod
    from config import SovereignConfig
    from db import SovereignDB
    from retrieval import RetrievalEngine

    principals = tmp_path / "principals"
    principals.mkdir()
    raw = {
        "agent_id": "codex",
        "workspace_id": "default",
        "capabilities": ["*"],
    }
    if aliases is not None:
        raw["legacy_agent_ids"] = aliases
    f = principals / "local.json"
    f.write_text(json.dumps(raw), encoding="utf-8")
    os.chmod(f, 0o600)
    monkeypatch.setattr(principal_mod, "PRINCIPALS_DIR", principals)

    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        cfg = SovereignConfig(
            db_path=str(tmp_path / "test.db"),
            reranker_enabled=False,
            hyde_enabled=False,
        )
        db_obj = SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
    monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: None))
    monkeypatch.setattr(minnid, "_retrieval", engine)
    if hasattr(retrieval_mod, "agent_scope_for"):
        retrieval_mod.agent_scope_for.cache_clear()
    return db_obj


def _insert_learning(db_obj, agent_id: str, content: str) -> int:
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO learnings
               (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fact', ?, 1.0, ?)""",
            (agent_id, content, time.time()),
        )
        return c.lastrowid


def _search(params: dict):
    from minnid import _dispatch_sync

    return _dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "search",
            "params": params,
        }
    )


def _learning_agents(response: dict) -> list[str]:
    assert "error" not in response
    return sorted(row["agent_id"] for row in response["result"]["learnings"])


def test_default_recall_scopes_learnings_to_caller_principal(tmp_path, monkeypatch):
    db_obj = _make_runtime(tmp_path, monkeypatch)
    _insert_learning(db_obj, "codex", "scoped recall token belongs to codex")
    _insert_learning(db_obj, "claude-code", "scoped recall token belongs to claude")

    resp = _search(
        {
            "query": "scoped recall token",
            "agent_id": "codex",
            "expand": False,
        }
    )

    assert _learning_agents(resp) == ["codex"]


def test_cross_agent_true_returns_all_matching_learnings(tmp_path, monkeypatch):
    db_obj = _make_runtime(tmp_path, monkeypatch)
    _insert_learning(db_obj, "codex", "scoped recall token belongs to codex")
    _insert_learning(db_obj, "claude-code", "scoped recall token belongs to claude")

    resp = _search(
        {
            "query": "scoped recall token",
            "agent_id": "codex",
            "expand": False,
            "cross_agent": True,
        }
    )

    assert _learning_agents(resp) == ["claude-code", "codex"]


def test_no_principal_recall_keeps_unscoped_back_compat(tmp_path, monkeypatch):
    import minnid

    db_obj = _make_runtime(tmp_path, monkeypatch)
    _insert_learning(db_obj, "codex", "scoped recall token belongs to codex")
    _insert_learning(db_obj, "claude-code", "scoped recall token belongs to claude")
    monkeypatch.setattr(
        minnid,
        "resolve_effective_principal",
        lambda *, supplied_agent_id=None, transport="uds": None,
    )

    resp = _search({"query": "scoped recall token", "expand": False})

    assert _learning_agents(resp) == ["claude-code", "codex"]


def test_default_recall_includes_legacy_alias_rows_for_caller(tmp_path, monkeypatch):
    db_obj = _make_runtime(tmp_path, monkeypatch, aliases=["codex-legacy"])
    _insert_learning(db_obj, "codex", "scoped recall token belongs to codex")
    _insert_learning(db_obj, "codex-legacy", "scoped recall token belongs to old codex")
    _insert_learning(db_obj, "claude-code", "scoped recall token belongs to claude")

    resp = _search(
        {
            "query": "scoped recall token",
            "agent_id": "codex",
            "expand": False,
        }
    )

    assert _learning_agents(resp) == ["codex", "codex-legacy"]

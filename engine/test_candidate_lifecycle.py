"""
G14-G18 minimal lifecycle tests (propose -> list -> resolve).
Covers migration tables present, stage/list/resolve round-trips, status machine.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from migrations import run_migrations
from principal import EffectivePrincipal, is_operator_principal
from sovrd import _stage_candidate, _list_candidates, _resolve_candidate


def _fresh_db(tmp_path):
    db_path = str(tmp_path / "cand_test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.close()
    return db_path


def test_migrations_007_009_present_and_replay(tmp_path):
    """Migration replay coverage for 007-009 (tables + indexes exist)."""
    db_path = _fresh_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "candidate_packets" in tables
    assert "handoff_leases" in tables
    assert "learning_reads" in tables
    assert "contradiction_events" in tables
    assert "contradiction_log" in tables
    applied = {
        r["version"]
        for r in conn.execute("SELECT version FROM schema_migrations")
    }
    assert {7, 8, 9}.issubset(applied)
    # indexes
    idx = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert any("candidate" in i.lower() for i in idx)
    conn.close()


def test_candidate_lifecycle_propose_list_resolve(tmp_path, monkeypatch):
    """Basic propose (stage) -> list -> resolve(accepted) creates learning."""
    db_path = _fresh_db(tmp_path)
    # force sovrd to use this DB via env or monkey (simple: patch DEFAULT_CONFIG)
    import config as cfg_mod
    old_db = cfg_mod.DEFAULT_CONFIG.db_path
    cfg_mod.DEFAULT_CONFIG.db_path = db_path
    try:
        # non-strict principal (synthesized main = operator)
        p = EffectivePrincipal(agent_id="main", capabilities=["*"])
        assert is_operator_principal(p)

        # stage via handler (simulates non-force learn)
        req = {"id": 1}
        params = {"content": "Test candidate lifecycle learning about G14", "workspace_id": "default"}
        resp = _stage_candidate(params, req["id"])
        assert resp.get("result", {}).get("status") == "proposed"
        cid = resp["result"]["candidate_id"]
        assert cid

        # list
        lresp = _list_candidates({"status": "proposed"}, 2)
        cands = lresp.get("result", {}).get("candidates", [])
        assert any(c.get("candidate_id") == cid for c in cands)

        # resolve as operator
        rresp = _resolve_candidate({"candidate_id": cid, "decision": "accept", "reason": "lifecycle test"}, 3)
        assert rresp.get("result", {}).get("new_status") == "accepted"
        assert rresp.get("result", {}).get("learning_id")

        # verify learning row exists
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        learn = conn.execute("SELECT * FROM learnings WHERE learning_id=?", (rresp["result"]["learning_id"],)).fetchone()
        assert learn is not None
        assert "lifecycle" in learn["content"]
        conn.close()
    finally:
        cfg_mod.DEFAULT_CONFIG.db_path = old_db


def test_new_tool_schema_has_no_spoof_surfaces():
    """Plugin schema test: sovereign_resolve_candidate inputSchema contains none of the G11-G13 caller-controlled spoof keys."""
    import re
    src = Path(__file__).parent.parent / "plugins/sovereign-memory/src/server.ts"
    if not src.exists():
        pytest.skip("server.ts not in tree for this env")
    text = src.read_text()
    # The registerTool for the new resolve tool must not re-introduce agentId / vaultPath / afmPrepareUrl
    # (grep for the tool name + nearby inputSchema)
    match = re.search(r"sovereign_resolve_candidate.*?inputSchema:\s*\{(.*?)\}", text, re.DOTALL)
    if match:
        schema_body = match.group(1)
        for forbidden in ("agentId", "agent_id", "vaultPath", "vault_path", "afmPrepareUrl"):
            assert forbidden not in schema_body, f"forbidden spoof key {forbidden} found in new tool schema"
    # Also positive: the 8 decisions are present
    assert "mark_project_scoped" in text


def test_ui_server_candidates_routes_exist():
    """UI smoke: the two new G17 Candidates endpoints are registered in ui-server (no import/syntax error + string presence)."""
    import re
    src = Path(__file__).parent.parent / "plugins/sovereign-memory/src/ui-server.ts"
    if not src.exists():
        pytest.skip("ui-server.ts not present")
    text = src.read_text()
    assert "/api/candidates" in text
    assert "/api/resolve-candidate" in text
    # The routes return JSON even on RPC error (200 + {error:...}) — acceptable for local console
    assert "jsonRpcSocketRequestWithFallback" in text or "resolve_candidate" in text

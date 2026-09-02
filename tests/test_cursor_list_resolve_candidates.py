"""Cursor (and any host missing the MCP pair) can list+resolve its own
proposed candidates. Operator/owner rules stay: a platform principal with
the authored template caps may drain its own queue (list + reject/redact)
but must not self-approve into durable memory, and must not resolve another
principal's rows. Cross-principal resolve remains the explicit
resolve_candidate/govern grant (live claude-code.json), not the template.

Fixtures/tmpdirs only — live ~/.minni/minni.db is never opened.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import minni.minnid as minnid
import minni.minnid_runtime.provenance as provenance
from minni.principal import EffectivePrincipal
from minni.tools.author_principals import render_principal

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_TS = REPO_ROOT / "plugins" / "minni" / "src" / "server.ts"
LIST_MODEL_TS = REPO_ROOT / "plugins" / "minni" / "src" / "list-candidates-model.ts"

# Authored platform template caps — cursor.json, grok-build.json, and the
# claude-code template (the live claude-code.json resolve_candidate grant is
# operator-authored, not this set).
PLATFORM_TEMPLATE_CAPS = (
    "search",
    "read",
    "learn",
    "feedback",
    "log_event",
    "handoff",
    "export",
)


def _patch_db(monkeypatch, tmp_path):
    import minni.db as db_mod
    import minni.config as cfg_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "cursor-candidates.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"))
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    return db_obj


def _stamp(monkeypatch, agent_id, capabilities=PLATFORM_TEMPLATE_CAPS):
    principal = EffectivePrincipal(agent_id=agent_id, capabilities=list(capabilities))
    monkeypatch.setattr(provenance, "resolve_effective_principal", lambda **_kw: principal)
    return principal


def _stage(monkeypatch, owner, content, capabilities=PLATFORM_TEMPLATE_CAPS):
    _stamp(monkeypatch, owner, capabilities)
    resp = minnid._stage_candidate({"content": content, "workspace_id": "default"}, 1)
    assert resp.get("result", {}).get("status") == "proposed", resp
    return resp["result"]["candidate_id"]


def test_cursor_template_does_not_grant_cross_principal_resolve(tmp_path):
    """Copying live claude-code.json's resolve_candidate cap onto cursor would
    let Cursor drain *other* principals. The authored template must not."""
    rendered = render_principal("cursor", minni_home=tmp_path / ".minni")
    assert rendered["agent_id"] == "cursor"
    assert set(rendered["capabilities"]) == set(PLATFORM_TEMPLATE_CAPS)
    assert "resolve_candidate" not in rendered["capabilities"]
    assert "govern" not in rendered["capabilities"]
    assert "*" not in rendered["capabilities"]

    claude = render_principal("claude-code", minni_home=tmp_path / ".minni")
    assert "resolve_candidate" not in claude["capabilities"], (
        "template must stay ungated; live claude-code.json is an operator grant"
    )


def test_mcp_surface_advertises_list_and_resolve_without_identity_spoof():
    """Cursor MCP is the same server.ts as every host. The drain pair must be
    registered, and neither schema may take a model-supplied agent id."""
    source = SERVER_TS.read_text(encoding="utf8")
    for tool in ('"minni_list_candidates"', '"minni_resolve_candidate"'):
        start = source.find(tool)
        assert start != -1, f"{tool} is missing from plugins/minni/src/server.ts"
        next_tool = source.find("server.registerTool(", start + 1)
        block = source[start : next_tool if next_tool != -1 else None]
        schema_start = block.find("inputSchema:")
        handler_start = block.find("async")
        schema = block[schema_start:handler_start]
        assert "agent_id" not in schema and "agentId" not in schema, tool
        assert "agent_id: DEFAULT_AGENT_ID" in block, f"{tool} must stamp DEFAULT_AGENT_ID"


def test_cursor_list_requires_learn_cap(monkeypatch, tmp_path):
    """list_candidates is gated on `learn` at dispatch (RPC_CAPABILITY_REQUIREMENTS).
    Direct handler call skips that gate; MCP/UDS go through dispatch."""
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "cursor", ["search", "read"])
    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "list_candidates",
            "params": {"status": "proposed"},
        }
    )
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "learn" in err.get("message", ""), resp


def test_cursor_lists_only_its_own_proposed(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    cursor_cid = _stage(monkeypatch, "cursor", "cursor proposed learning")
    grok_cid = _stage(monkeypatch, "grok-build", "grok-build proposed learning")

    _stamp(monkeypatch, "cursor")
    listed = minnid._list_candidates({"status": "proposed"}, 2)
    rows = listed.get("result", {}).get("candidates", [])
    ids = {row["candidate_id"] for row in rows}
    assert listed["result"]["principal"] == "cursor"
    assert cursor_cid in ids
    assert grok_cid not in ids


def test_cursor_owner_may_reject_own_but_not_accept(monkeypatch, tmp_path):
    db_obj = _patch_db(monkeypatch, tmp_path)
    accept_cid = _stage(monkeypatch, "cursor", "cursor must not self-approve")
    reject_cid = _stage(monkeypatch, "cursor", "cursor may reject its own")

    _stamp(monkeypatch, "cursor")
    denied = minnid._resolve_candidate(
        {"candidate_id": accept_cid, "decision": "accept"}, 3
    )
    err = denied.get("error", {})
    assert err.get("code") == -32004, denied
    assert "operator_only" in err.get("message", ""), denied

    with db_obj.cursor() as c:
        c.execute(
            "SELECT status FROM candidate_packets WHERE candidate_id=?",
            (accept_cid,),
        )
        assert dict(c.fetchone())["status"] == "proposed"
        c.execute(
            "SELECT COUNT(*) AS n FROM learnings WHERE content LIKE '%self-approve%'"
        )
        assert int(c.fetchone()["n"]) == 0

    rejected = minnid._resolve_candidate(
        {"candidate_id": reject_cid, "decision": "reject", "reason": "drain"},
        4,
    )
    assert rejected.get("result", {}).get("new_status") == "rejected", rejected


def test_cursor_cannot_resolve_foreign_owner(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    grok_cid = _stage(monkeypatch, "grok-build", "foreign proposed learning")

    _stamp(monkeypatch, "cursor")
    resp = minnid._resolve_candidate(
        {"candidate_id": grok_cid, "decision": "reject"}, 5
    )
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "principal_mismatch" in err.get("message", ""), resp
    assert "'grok-build'" in err.get("message", ""), resp


def test_explicit_resolve_candidate_cap_still_resolves_foreign(monkeypatch, tmp_path):
    """Already-allowed operators (literal resolve_candidate) keep cross-principal
    resolve. Cursor must not gain this via the template."""
    _patch_db(monkeypatch, tmp_path)
    cursor_cid = _stage(monkeypatch, "cursor", "cursor row resolved by operator")

    _stamp(monkeypatch, "claude-code", list(PLATFORM_TEMPLATE_CAPS) + ["resolve_candidate"])
    resp = minnid._resolve_candidate(
        {"candidate_id": cursor_cid, "decision": "reject"}, 6
    )
    assert resp.get("result", {}).get("new_status") == "rejected", resp


SECRET_MARKER = "hunter2-not-a-path"
LOCAL_PATH_MARKER = "/Users/example/Projects/secret-notes.md"
REDACT_THEN_LIST_MARKER = "HIDDEN_REDACT_MARKER_xyz"


def test_list_candidates_redacts_secrets_and_paths(monkeypatch, tmp_path):
    """POLICY §2: list_candidates is a JSON-RPC envelope that crosses a
    process boundary. SELECT * used to ship raw content, evidence_refs,
    and inbox paths to MCP (and therefore to cloud models)."""
    _patch_db(monkeypatch, tmp_path)
    _stage(
        monkeypatch,
        "cursor",
        f"Remember api_key={SECRET_MARKER} lives at {LOCAL_PATH_MARKER}",
    )
    _stamp(monkeypatch, "cursor")
    listed = minnid._list_candidates({"status": "proposed"}, 8)
    blob = str(listed)
    assert SECRET_MARKER not in blob, listed
    assert "/Users/example" not in blob, listed
    rows = listed.get("result", {}).get("candidates", [])
    assert len(rows) == 1, listed
    content = rows[0].get("content", "")
    assert "api_key=[REDACTED]" in content, content
    assert "[REDACTED_PATH]" in content, content


def test_list_defaults_to_proposed_and_hides_redacted_content(monkeypatch, tmp_path):
    """Omitting status used to SELECT every status. redact/reject only
    flips status — content stays — so a naive drain re-leaked hidden
    packets. Default the drain queue to proposed."""
    _patch_db(monkeypatch, tmp_path)
    visible_cid = _stage(monkeypatch, "cursor", "cursor proposed drain item")
    hidden_cid = _stage(monkeypatch, "cursor", REDACT_THEN_LIST_MARKER)
    _stamp(monkeypatch, "cursor")
    redacted = minnid._resolve_candidate(
        {"candidate_id": hidden_cid, "decision": "redact", "reason": "scrub"},
        9,
    )
    assert redacted.get("result", {}).get("new_status") == "redacted", redacted

    listed = minnid._list_candidates({}, 10)
    result = listed.get("result", {})
    rows = result.get("candidates", [])
    ids = {row["candidate_id"] for row in rows}
    assert visible_cid in ids, listed
    assert hidden_cid not in ids, listed
    assert all(row.get("status") == "proposed" for row in rows), listed
    assert REDACT_THEN_LIST_MARKER not in str(listed), listed
    assert result.get("status") == "proposed", listed

    explicit = minnid._list_candidates({"status": "redacted"}, 11)
    explicit_rows = explicit.get("result", {}).get("candidates", [])
    assert any(row.get("candidate_id") == hidden_cid for row in explicit_rows), explicit


def test_list_truncation_is_not_silent(monkeypatch, tmp_path):
    """count was len(page) with default min(limit or 100, 500) and no
    has_more/total. Hundreds of proposed packets then looked complete.
    Tests must fail if truncation is silent."""
    _patch_db(monkeypatch, tmp_path)
    for i in range(3):
        _stage(monkeypatch, "cursor", f"cursor drain item {i} unique")
    _stamp(monkeypatch, "cursor")

    page = minnid._list_candidates({"status": "proposed", "limit": 2}, 12)
    result = page.get("result", {})
    assert result.get("count") == 2, page
    assert result.get("total") == 3, page
    assert result.get("has_more") is True, page
    assert result.get("limit") == 2, page
    assert len(result.get("candidates", [])) == 2, page

    full = minnid._list_candidates({"status": "proposed", "limit": 10}, 13)
    full_result = full.get("result", {})
    assert full_result.get("count") == 3, full
    assert full_result.get("total") == 3, full
    assert full_result.get("has_more") is False, full


def test_mcp_list_does_not_stringify_raw_daemon_packet():
    """Cursor MCP sends tool output to cloud models. The handler must
    redact + project before returning, default the drain queue to
    proposed, and refuse redacted/rejected content."""
    source = SERVER_TS.read_text(encoding="utf8")
    start = source.find('"minni_list_candidates"')
    assert start != -1
    next_tool = source.find("server.registerTool(", start + 1)
    block = source[start : next_tool if next_tool != -1 else None]
    assert "modelListCandidatesPayload" in block
    assert "drainStatusForModel" in block
    assert "JSON.stringify(rpc," not in block
    helper = LIST_MODEL_TS.read_text(encoding="utf8")
    assert "redacted" in helper
    assert "rejected" in helper
    assert "proposed" in helper

    schema_start = block.find("inputSchema:")
    handler_start = block.find("async")
    schema = block[schema_start:handler_start]
    assert 'z.enum(["proposed"])' in schema, schema
    assert "z.string()" not in schema, schema

    fail_start = helper.find("ok === false")
    assert fail_start != -1, helper
    fail_return = helper.find("return redactLocalValue", fail_start)
    assert fail_return != -1, helper[fail_start : fail_start + 200]
    fail_end = helper.find(") as Record", fail_return)
    fail_block = helper[fail_return:fail_end]
    assert "ok: false" in fail_block, fail_block
    assert "total: 0" not in fail_block, fail_block
    assert "has_more: false" not in fail_block, fail_block
    assert "count: 0" not in fail_block, fail_block
    assert "candidates: []" not in fail_block, fail_block

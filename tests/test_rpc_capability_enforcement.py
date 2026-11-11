"""P0 principal enforcement: stamped capabilities gate DB-backed RPC paths."""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minni.minnid as minnid
import minni.minnid_runtime.provenance as provenance
from minni.principal import EffectivePrincipal


def _patch_db(monkeypatch, tmp_path):
    import minni.db as db_mod
    from minni.config import MinniConfig

    cfg = MinniConfig(db_path=str(tmp_path / "cap.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.MinniDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    return db_obj


def _stamp(monkeypatch, agent_id, capabilities):
    principal = EffectivePrincipal(agent_id=agent_id, capabilities=list(capabilities))
    monkeypatch.setattr(provenance, "resolve_effective_principal", lambda **_kw: principal)
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


def test_learn_uses_stamped_principal_not_wire_agent(monkeypatch, tmp_path):
    db_obj = _patch_db(monkeypatch, tmp_path)
    captured = {}

    def _fake_detect_contradictions(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        minnid,
        "_lazy_writeback",
        lambda: types.SimpleNamespace(
            db=db_obj, detect_contradictions=_fake_detect_contradictions
        ),
    )
    # learn is a WRITE op requiring the "learn" cap; stamped principal overrides wire agent_id.
    _stamp(monkeypatch, "codex", ["read", "learn"])
    resp = _rpc(
        "learn",
        {
            "agent_id": "victim",
            "content": "stamped principal overrides wire agent_id on learn",
        },
    )
    assert "error" not in resp, resp
    result = resp.get("result", {})
    assert result.get("principal") == "codex"
    assert captured.get("agent_id") == "codex"
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT principal FROM candidate_packets WHERE candidate_id = ?",
            (result["candidate_id"],),
        ).fetchone()
    assert row is not None
    assert row["principal"] == "codex"


def test_read_only_principal_denied_learn(monkeypatch, tmp_path):
    """PR91-6: a read-only principal must NOT be able to write via learn."""
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    resp = _rpc(
        "learn",
        {"agent_id": "codex", "content": "read-only principal write attempt"},
    )
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "learn" in err.get("message", "")
    with db_obj.cursor() as c:
        count = c.execute("SELECT COUNT(*) FROM candidate_packets").fetchone()[0]
    assert count == 0


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

    from minni.principal import resolve_effective_principal

    p = resolve_effective_principal(
        supplied_agent_id="codex", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "codex"
    assert p.capabilities == []
    assert not p.can("search")


def test_platform_agent_does_not_inherit_operator_vault_roots(tmp_path: Path):
    """Finding 6 residual: platform agents must not copy operator allowed_vault_roots."""
    principals = tmp_path / "principals"
    principals.mkdir()
    operator_root = tmp_path / "operator-vault"
    operator_root.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(operator_root)],
                "platform_agent_ids": ["codex"],
                "platform_agent_capabilities": {"codex": ["search", "read"]},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)

    from minni.principal import resolve_effective_principal

    p = resolve_effective_principal(
        supplied_agent_id="codex", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "codex"
    assert p.capabilities == ["search", "read"]
    assert str(operator_root.resolve()) not in p.allowed_vault_roots
    assert not p.allows_vault_root(operator_root)


def test_platform_agent_vault_roots_from_explicit_map(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    operator_root = tmp_path / "operator-vault"
    platform_root = tmp_path / "codex-vault"
    operator_root.mkdir()
    platform_root.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(operator_root)],
                "platform_agent_ids": ["codex"],
                "platform_agent_capabilities": {"codex": ["search"]},
                "platform_agent_vault_roots": {"codex": [str(platform_root)]},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)

    from minni.principal import resolve_effective_principal

    p = resolve_effective_principal(
        supplied_agent_id="codex", transport="uds", principals_dir=principals
    )
    assert p.allowed_vault_roots == [str(platform_root.resolve())]
    assert p.allows_vault_root(platform_root)
    assert not p.allows_vault_root(operator_root)


def test_platform_agent_empty_or_malformed_vault_roots_fail_closed(tmp_path: Path):
    """Finding 6: explicit [] or a string map value must not vacuous-allow."""
    principals = tmp_path / "principals"
    principals.mkdir()
    target = tmp_path / "etc"
    target.mkdir()

    from minni.principal import resolve_effective_principal

    for roots_value in ([], "/tmp/codex", [""], None):
        f = principals / "local.json"
        payload = {
            "agent_id": "main",
            "capabilities": ["*"],
            "platform_agent_ids": ["codex"],
            "platform_agent_capabilities": {"codex": ["search", "read"]},
            "platform_agent_vault_roots": {"codex": roots_value},
        }
        f.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(f, 0o600)
        p = resolve_effective_principal(
            supplied_agent_id="codex", transport="uds", principals_dir=principals
        )
        assert not p.allows_vault_root(target), roots_value
        assert "/.minni-platform-no-vault-roots" in p.allowed_vault_roots or not p.allows_vault_root(
            "/"
        )


def test_platform_agent_missing_roots_map_keeps_own_vault(tmp_path: Path, monkeypatch):
    """PR167 review: strict installs predating platform_agent_vault_roots must
    keep pathed access to the platform agent's OWN vault (never the
    operator's). The canonical installer mapping is authoritative
    (claude-code -> claudecode-vault); arbitrary literal-id roots stay denied."""
    principals = tmp_path / "principals"
    principals.mkdir()
    minni_home = tmp_path / "minni-home"
    (minni_home / "claude-code-vault").mkdir(parents=True)
    (minni_home / "claudecode-vault").mkdir(parents=True)
    operator_root = tmp_path / "operator-vault"
    operator_root.mkdir()
    monkeypatch.setenv("MINNI_HOME", str(minni_home))

    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(operator_root)],
                "platform_agent_ids": ["claude-code"],
                "platform_agent_capabilities": {"claude-code": ["search", "read"]},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)

    from minni.principal import resolve_effective_principal

    p = resolve_effective_principal(
        supplied_agent_id="claude-code", transport="uds", principals_dir=principals
    )
    assert not p.allows_vault_root(minni_home / "claude-code-vault")
    assert p.allows_vault_root(minni_home / "claudecode-vault")
    assert not p.allows_vault_root(operator_root)

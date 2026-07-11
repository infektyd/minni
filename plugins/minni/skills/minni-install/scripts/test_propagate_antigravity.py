"""Tests for the `antigravity` propagation target.

The antigravity target wires the shared Gemini/Antigravity MCP view files and the
two permission-grant files so the `minni` server is delivered at Claude-Code
parity across the three Antigravity surfaces (CLI `agy`, IDE, antigravity).

These exercise the pure-ish helpers against tmp fixtures that mirror the real
view/permission shapes, so we never touch the live `~/.gemini` tree under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402


def test_materialize_json_config_replaces_symlink_with_regular_copy(tmp_path):
    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    surface = tmp_path / "surface.json"
    surface.symlink_to(shared)
    assert propagate.materialize_json_config(surface)
    assert surface.exists() and not surface.is_symlink()
    assert json.loads(surface.read_text()) == json.loads(shared.read_text())


def test_stamp_agy_hook_env_is_platform_specific_and_shell_safe(tmp_path):
    hooks = {"hooks": {"PreToolUse": [{"hooks": [{
        "type": "command", "command": "node '/Plugin Path/gemini-hook.js' PreToolUse", "timeout": 10,
    }]}]}}
    stamped = propagate.stamp_agy_hook_env(
        hooks, "gemini", tmp_path / "Gemini Vault", tmp_path / "run/minnid.sock",
        tmp_path / "Repo With Spaces",
    )
    command = stamped["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "MINNI_GEMINI_AGENT_ID=gemini" in command
    assert "MINNI_GEMINI_VAULT_PATH=" in command and "Gemini Vault" in command
    assert "MINNI_GEMINI_WORKSPACE_ID='workspace-repo with spaces'" in command
    assert "MINNI_SOCKET_PATH=" in command
    assert "MINNI_CLAUDECODE_" not in command


def test_render_agy_hooks_quotes_dist_with_spaces_and_apostrophe(tmp_path):
    install = tmp_path / "Agent's Plugins" / "minni"
    hooks = {"hooks": {"PreToolUse": [{"hooks": [{
        "type": "command",
        "command": "node __MINNI_GEMINI_DIST__/gemini-hook.js PreToolUse",
        "timeout": 10,
    }]}]}}
    rendered = json.loads(propagate.render_agy_hooks(
        hooks, install, "gemini", tmp_path / "vault", tmp_path / "socket",
        Path("workspace-unknown"),
    ))
    command = rendered["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert propagate.AGY_DIST_TOKEN not in command
    assert "Agent" in command and "Plugins" in command
    # shlex can parse the final command back into an intact script path.
    import shlex
    argv = shlex.split(command)
    node_index = argv.index("node")
    assert argv[node_index + 1] == str(install / "dist/gemini-hook.js")


def _server(tmp: Path) -> Path:
    return tmp / ".gemini" / "extensions" / "minni" / "dist" / "server.js"


def test_minni_entry_shape(tmp_path):
    sp = _server(tmp_path)
    e = propagate.gemini_minni_entry(sp, "gemini", Path("/v"), Path("/s"), Path("/w"))
    assert e["command"] == "node"
    assert e["args"] == [str(sp)]
    assert e["cwd"] == str(sp.parent.parent)
    assert e["env"]["MINNI_AGENT_ID"] == "gemini"
    assert e["env"]["MINNI_VAULT_PATH"] == "/v"
    assert "$typeName" not in e


def test_minni_entry_type_name_is_first_key(tmp_path):
    e = propagate.gemini_minni_entry(
        _server(tmp_path), "gemini", Path("/v"), Path("/s"), Path("/w"),
        type_name=propagate.GEMINI_IDE_TYPE_NAME,
    )
    assert e["$typeName"] == propagate.GEMINI_IDE_TYPE_NAME
    assert next(iter(e)) == "$typeName"


def test_write_view_entry_inherits_typename_and_drops_legacy(tmp_path):
    view = tmp_path / "ide_view.json"
    view.write_text(json.dumps({"mcpServers": {
        "other": {"$typeName": propagate.GEMINI_IDE_TYPE_NAME, "command": "x"},
        "sovereign-memory": {"command": "old"},
    }}), encoding="utf-8")
    propagate.write_view_entry(view, _server(tmp_path), "gemini", Path("/v"), Path("/s"), Path("/w"))
    data = json.loads(view.read_text())
    assert "sovereign-memory" not in data["mcpServers"]
    assert data["mcpServers"]["minni"]["$typeName"] == propagate.GEMINI_IDE_TYPE_NAME
    # sibling untouched
    assert data["mcpServers"]["other"] == {"$typeName": propagate.GEMINI_IDE_TYPE_NAME, "command": "x"}


def test_write_view_entry_no_typename_when_no_siblings_have_it(tmp_path):
    view = tmp_path / "plain_view.json"
    view.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8")
    propagate.write_view_entry(view, _server(tmp_path), "gemini", Path("/v"), Path("/s"), Path("/w"))
    data = json.loads(view.read_text())
    assert "$typeName" not in data["mcpServers"]["minni"]


def test_write_view_entry_is_idempotent(tmp_path):
    view = tmp_path / "v.json"
    view.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    propagate.write_view_entry(view, _server(tmp_path), "gemini", Path("/v"), Path("/s"), Path("/w"))
    first = view.read_text()
    propagate.write_view_entry(view, _server(tmp_path), "gemini", Path("/v"), Path("/s"), Path("/w"))
    assert view.read_text() == first


def test_write_view_entry_missing_file_is_noop(tmp_path):
    view = tmp_path / "absent.json"
    propagate.write_view_entry(view, _server(tmp_path), "gemini", Path("/v"), Path("/s"), Path("/w"))
    assert not view.exists()


def test_ensure_permission_grant_adds_when_missing(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"permissions": {"allow": ["command(ls)"]}}), encoding="utf-8")
    propagate.ensure_permission_grant(p, ["permissions", "allow"])
    allow = json.loads(p.read_text())["permissions"]["allow"]
    # X1: per-tool read-only grants, not the blanket wildcard.
    assert "mcp(minni/*)" not in allow
    for grant in propagate.MINNI_READONLY_GRANTS:
        assert grant in allow
    assert "command(ls)" in allow


def test_ensure_permission_grant_removes_legacy_and_is_idempotent(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"globalPermissionGrants": {"allow": [
        "mcp(sovereign-memory/*)", "sovereign_tools", "keep(this)",
    ]}}), encoding="utf-8")
    propagate.ensure_permission_grant(p, ["globalPermissionGrants", "allow"])
    allow = json.loads(p.read_text())["globalPermissionGrants"]["allow"]
    assert "mcp(sovereign-memory/*)" not in allow
    assert "sovereign_tools" not in allow
    assert "keep(this)" in allow
    assert allow.count("mcp(minni/minni_recall)") == 1
    assert "mcp(minni/*)" not in allow
    # second run must not duplicate or otherwise change the file
    before = p.read_text()
    propagate.ensure_permission_grant(p, ["globalPermissionGrants", "allow"])
    assert p.read_text() == before


def test_ensure_permission_grant_reuses_nested_owner(tmp_path):
    # Antigravity config.json nests grants under userSettings; a shallow key_path
    # must reuse the existing nested allow-list, not append a top-level block.
    # X1: an existing blanket wildcard is narrowed to the per-tool set.
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"userSettings": {"globalPermissionGrants": {"allow": [
        "command(ls)", "mcp(minni/*)",
    ]}}}), encoding="utf-8")
    propagate.ensure_permission_grant(p, ["globalPermissionGrants", "allow"])
    data = json.loads(p.read_text())
    assert list(data.keys()) == ["userSettings"]  # no divergent top-level block
    allow = data["userSettings"]["globalPermissionGrants"]["allow"]
    assert "mcp(minni/*)" not in allow
    assert allow.count("mcp(minni/minni_recall)") == 1
    assert "command(ls)" in allow


def test_ensure_permission_grant_creates_when_absent(tmp_path):
    p = tmp_path / "fresh.json"
    p.write_text(json.dumps({"other": 1}), encoding="utf-8")
    propagate.ensure_permission_grant(p, ["permissions", "allow"])
    data = json.loads(p.read_text())
    assert data["permissions"]["allow"] == list(propagate.MINNI_READONLY_GRANTS)
    assert data["other"] == 1


def test_ensure_permission_grant_missing_file_is_noop(tmp_path):
    p = tmp_path / "nope.json"
    propagate.ensure_permission_grant(p, ["permissions", "allow"])
    assert not p.exists()


def test_antigravity_platform_spec_uses_gemini_agent():
    spec = propagate.platform_spec("antigravity", Path.home() / "Projects" / "minni")
    assert spec["agent"] == "gemini"
    assert spec["config_kind"] == "antigravity"
    assert str(spec["install"]).endswith(".gemini/extensions/minni")


def test_antigravity_aliases_resolve():
    assert propagate.canonical_platform("agy") == "antigravity"
    assert propagate.canonical_platform("antigravity-cli") == "antigravity"
    assert propagate.canonical_platform("antigravity-ide") == "antigravity"


# --- X3: --agent slug validation -------------------------------------------

def test_valid_agent_id_accepts_safe_slugs():
    for good in ("codex", "claude-code", "a", "agent-1", "x" * 64):
        assert propagate.valid_agent_id(good) == good


def test_valid_agent_id_rejects_traversal_and_injection():
    import argparse

    import pytest

    for bad in (
        "../evil",
        "..",
        "a/b",
        "a\\b",
        "Agent",            # uppercase
        "-leading-hyphen",  # must start alnum
        "with space",
        "quote\"inject",
        "line\ninject",     # X3: newline TOML section injection vector
        "line\rinject",
        "x" * 65,           # too long
        "",
    ):
        with pytest.raises(argparse.ArgumentTypeError):
            propagate.valid_agent_id(bad)


# --- X2: preserved MINNI_* identity validation ------------------------------

def test_validate_preserved_identity_replaces_mismatched_keys():
    agent = "codex"
    expected_vault = str(propagate.vault_for(agent))
    expected_socket = str(propagate.DEFAULT_SOCKET)
    # An existing config with a stale/planted vault + socket + wrong agent id.
    ex_env = {
        "MINNI_AGENT_ID": "attacker",
        "MINNI_VAULT_PATH": "/tmp/evil-vault",
        "MINNI_SOCKET_PATH": "/tmp/evil.sock",
        "MINNI_WORKSPACE_ID": "ws-keep",  # non-identity key must pass through
    }
    out = propagate._validate_preserved_identity(ex_env, agent)
    assert out["MINNI_AGENT_ID"] == agent
    assert out["MINNI_VAULT_PATH"] == expected_vault
    assert out["MINNI_SOCKET_PATH"] == expected_socket
    # Non-identity wiring is preserved verbatim.
    assert out["MINNI_WORKSPACE_ID"] == "ws-keep"


def test_validate_preserved_identity_keeps_canonical_values():
    agent = "codex"
    ex_env = {
        "MINNI_AGENT_ID": agent,
        "MINNI_VAULT_PATH": str(propagate.vault_for(agent)),
        "MINNI_SOCKET_PATH": str(propagate.DEFAULT_SOCKET),
    }
    out = propagate._validate_preserved_identity(dict(ex_env), agent)
    assert out == ex_env


def test_vault_path_is_safe_rejects_symlink(tmp_path, monkeypatch):
    # Point the canonical vault at a path we control, then make it a symlink:
    # a symlinked vault must be rejected even when the string matches.
    agent = "codex"
    real = tmp_path / "real-vault"
    real.mkdir()
    link = tmp_path / "codex-vault"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(propagate, "vault_for", lambda a: link)
    assert propagate._vault_path_is_safe(str(link), agent) is False


# --- X1: read-only allow-list must exclude write/export tools ----------------

def test_x1_readonly_grant_set_excludes_write_tools():
    # The blanket wildcard would auto-allow write/export tools; the per-tool set
    # must contain ONLY read-only tools and never those write/export names.
    forbidden = {
        "minni_learn",
        "minni_vault_write",
        "minni_export_pack",
        "minni_compile_vault",
        "minni_resolve_candidate",
        "minni_negotiate_handoff",
        "minni_ack_handoff",
        "minni_ping_agent_request",
        "minni_ping_agent_decide",
        "minni_prepare_outcome",
        "minni_plan_update",
        "minni_plan_create",
    }
    assert not (set(propagate.MINNI_READONLY_TOOLS) & forbidden)
    assert propagate.MINNI_WILDCARD_GRANT not in propagate.MINNI_READONLY_GRANTS
    assert all(g.startswith("mcp(minni/minni_") for g in propagate.MINNI_READONLY_GRANTS)

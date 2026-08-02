"""§9.5: propagate.py vs minni wire writers produce equivalent claude-code MCP entries."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from minni.wire.writers import update_claude_config, load_json, mcp_json

REPO = Path(__file__).resolve().parent.parent
PROPAGATE = (
    REPO / "plugins" / "minni" / "skills" / "minni-install" / "scripts" / "propagate.py"
)


def _load_propagate():
    spec = importlib.util.spec_from_file_location("propagate_mod", PROPAGATE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["propagate_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_claude_mcp_entry_parity(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    propagate = _load_propagate()

    server = tmp_path / "plugin" / "0.2.0" / "dist" / "server.js"
    server.parent.mkdir(parents=True)
    server.write_text("// stub", encoding="utf-8")
    vault = tmp_path / ".minni" / "claudecode-vault"
    vault.mkdir(parents=True)
    socket = tmp_path / ".minni" / "run" / "minnid.sock"
    socket.parent.mkdir(parents=True)
    workspace = tmp_path / "Projects" / "Minni"

    propagate.update_claude_config(
        server, "claude-code", vault, socket, workspace,
    )
    claude_path = tmp_path / ".claude.json"
    propagate_entry = load_json(claude_path)["mcpServers"]["minni"]

    claude_path.unlink()
    update_claude_config(server, "claude-code", vault, socket, workspace)
    wire_entry = load_json(claude_path)["mcpServers"]["minni"]

    assert wire_entry == propagate_entry

    manifest = mcp_json(server, "claude-code", vault, socket, workspace)
    assert manifest["mcpServers"]["minni"]["args"] == [str(server)]

def test_wire_mcp_json_mirrors_codex_hook_env(tmp_path):
    """Wire-primary codex must stamp MINNI_CODEX_* like propagate (hooks
    read only those keys; custom vault otherwise splits identity)."""
    server = tmp_path / "plugin" / "0.4.1" / "dist" / "server.js"
    server.parent.mkdir(parents=True)
    server.write_text("// stub", encoding="utf-8")
    vault = tmp_path / "custom-codex-vault"
    vault.mkdir()
    socket = tmp_path / "minnid.sock"
    workspace = tmp_path / "ws"
    env = mcp_json(server, "codex", vault, socket, workspace)["mcpServers"]["minni"]["env"]
    assert env["MINNI_CODEX_VAULT_PATH"] == env["MINNI_VAULT_PATH"] == str(vault)
    assert env["MINNI_CODEX_AGENT_ID"] == "codex"
    assert env["MINNI_CODEX_WORKSPACE_ID"] == env["MINNI_WORKSPACE_ID"]


def test_wire_mcp_json_does_not_mirror_codex_env_for_other_agents(tmp_path):
    server = tmp_path / "dist" / "server.js"
    server.parent.mkdir(parents=True)
    server.write_text("//", encoding="utf-8")
    env = mcp_json(
        server, "claude-code", tmp_path / "v", tmp_path / "s", tmp_path / "w",
    )["mcpServers"]["minni"]["env"]
    assert "MINNI_CODEX_VAULT_PATH" not in env


def test_wire_toml_codex_mirrors_hook_env(tmp_path, monkeypatch):
    from minni.wire.writers import update_toml_mcp_config
    import tomllib

    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("# empty\n", encoding="utf-8")
    server = tmp_path / "dist" / "server.js"
    server.parent.mkdir(parents=True)
    server.write_text("//", encoding="utf-8")
    vault = tmp_path / "custom-vault"
    vault.mkdir()
    update_toml_mcp_config(
        path, server, "codex", vault, tmp_path / "sock", tmp_path / "ws",
        explicit_workspace=True,
    )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    env = data["mcp_servers"]["minni"]["env"]
    assert env["MINNI_CODEX_VAULT_PATH"] == str(vault)
    assert env["MINNI_CODEX_AGENT_ID"] == "codex"

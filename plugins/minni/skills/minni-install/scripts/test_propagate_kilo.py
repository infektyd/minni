"""Tests for the `kilocode` propagation target.

Kilo (a fork of SST opencode) validates `~/.config/kilo/kilo.json` against a
STRICT schema: `Config.McpLocal` names the environment map `environment`, and
an unrecognized key fails the whole file. Writing Claude/Codex's `env` spelling
makes Kilo abort with `ConfigInvalidError` and refuse to start at all -- it
takes down the entire CLI, not just Minni.

Verified against kilocode 7.1.0:
    key "env"         -> Error: Configuration is invalid at .../kilo.json
    key "environment" -> starts clean

These run against tmp fixtures so we never touch the live `~/.config/kilo` tree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402


# Mirrors Kilo's `Config.McpLocal` (strict): the env map is `environment`.
KILO_MCP_LOCAL_KEYS = {"type", "command", "enabled", "environment", "timeout"}


def _write_kilo_config(tmp_path, monkeypatch) -> dict:
    """Run update_kilo_config against a tmp HOME and return the written JSON."""
    monkeypatch.setenv("HOME", str(tmp_path))
    propagate.update_kilo_config(
        server_path=tmp_path / "dist" / "server.js",
        agent="kilocode",
        vault=Path("/v"),
        socket_path=Path("/s"),
        workspace=Path("/w"),
    )
    return json.loads((tmp_path / ".config" / "kilo" / "kilo.json").read_text())


def test_uses_environment_not_env(tmp_path, monkeypatch):
    """Regression: `env` is rejected by Kilo's strict schema and bricks the CLI."""
    server = _write_kilo_config(tmp_path, monkeypatch)["mcp"]["minni"]

    assert "environment" in server, "Kilo's McpLocal env map is `environment`"
    assert "env" not in server, (
        "`env` is Claude/Codex spelling; Kilo's strict schema rejects it and "
        "refuses to start the entire CLI"
    )
    assert server["environment"]["MINNI_AGENT_ID"] == "kilocode"
    assert server["environment"]["MINNI_VAULT_PATH"] == "/v"


def test_emits_no_keys_outside_kilo_schema(tmp_path, monkeypatch):
    """Any unrecognized key fails the whole config, so stay inside the schema."""
    server = _write_kilo_config(tmp_path, monkeypatch)["mcp"]["minni"]

    unknown = set(server) - KILO_MCP_LOCAL_KEYS
    assert not unknown, f"keys outside Kilo's strict McpLocal schema: {sorted(unknown)}"


def test_command_is_argv_list(tmp_path, monkeypatch):
    """Kilo's McpLocal.command is a string[] argv, not Claude's {command, args}."""
    server = _write_kilo_config(tmp_path, monkeypatch)["mcp"]["minni"]

    assert isinstance(server["command"], list)
    assert server["command"][0] == "node"
    assert "args" not in server, "`args` is Claude's shape; Kilo folds argv into `command`"


def test_preserves_other_mcp_servers(tmp_path, monkeypatch):
    """Installing Minni must not clobber a user's existing MCP entries."""
    cfg = tmp_path / ".config" / "kilo"
    cfg.mkdir(parents=True)
    (cfg / "kilo.json").write_text(
        json.dumps({"mcp": {"context7": {"type": "remote", "url": "https://x", "enabled": True}}})
    )

    data = _write_kilo_config(tmp_path, monkeypatch)

    assert "context7" in data["mcp"], "existing MCP servers must survive install"
    assert "minni" in data["mcp"]

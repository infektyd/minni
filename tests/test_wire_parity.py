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
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


def _server(tmp: Path) -> Path:
    return tmp / ".gemini" / "extensions" / "minni" / "dist" / "server.js"


def test_minni_entry_shape(tmp_path):
    sp = _server(tmp_path)
    e = propagate.gemini_minni_entry(sp, "gemini", Path("/v"), Path("/s"), Path("/w"))
    assert e["args"] == ["node", str(sp)]
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
    assert "mcp(minni/*)" in allow
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
    assert allow.count("mcp(minni/*)") == 1
    # second run must not duplicate or otherwise change the file
    before = p.read_text()
    propagate.ensure_permission_grant(p, ["globalPermissionGrants", "allow"])
    assert p.read_text() == before


def test_ensure_permission_grant_reuses_nested_owner(tmp_path):
    # Antigravity config.json nests grants under userSettings; a shallow key_path
    # must reuse the existing nested allow-list, not append a top-level block.
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"userSettings": {"globalPermissionGrants": {"allow": [
        "command(ls)", "mcp(minni/*)",
    ]}}}), encoding="utf-8")
    propagate.ensure_permission_grant(p, ["globalPermissionGrants", "allow"])
    data = json.loads(p.read_text())
    assert list(data.keys()) == ["userSettings"]  # no divergent top-level block
    allow = data["userSettings"]["globalPermissionGrants"]["allow"]
    assert allow.count("mcp(minni/*)") == 1
    assert "command(ls)" in allow


def test_ensure_permission_grant_creates_when_absent(tmp_path):
    p = tmp_path / "fresh.json"
    p.write_text(json.dumps({"other": 1}), encoding="utf-8")
    propagate.ensure_permission_grant(p, ["permissions", "allow"])
    data = json.loads(p.read_text())
    assert data["permissions"]["allow"] == ["mcp(minni/*)"]
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

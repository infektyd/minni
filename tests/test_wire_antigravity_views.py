"""Bulk Antigravity refresh must not activate views the operator never enabled."""

import importlib.util
import json
import sys
from pathlib import Path

from minni.wire.writers import update_antigravity_config


def _load_propagate():
    root = Path(__file__).resolve().parents[1]
    path = root / "plugins/minni/skills/minni-install/scripts/propagate.py"
    spec = importlib.util.spec_from_file_location("propagate_antigravity_views", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _install_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin" / "0.2.0"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "server.js").write_text("// stub\n", encoding="utf-8")
    return root


def _view(home: Path, relative: str, servers: dict) -> Path:
    target = home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return target


def test_existing_only_refreshes_configured_view_and_leaves_other_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install_root = _install_root(tmp_path)
    configured = _view(
        tmp_path, ".gemini/config/mcp_config.json",
        {"minni": {"command": "node", "args": ["/old/dist/server.js"]}, "other": {"command": "keep"}},
    )
    unconfigured = _view(
        tmp_path, ".gemini/antigravity/mcp_config.json",
        {"other": {"command": "keep"}},
    )
    untouched = unconfigured.read_bytes()
    result = update_antigravity_config(
        install_root, "antigravity", tmp_path / "vault",
        tmp_path / "sock", tmp_path / "ws", existing_only=True,
    )
    assert str(configured) in result["views_written"]
    assert str(unconfigured) in result["views_skipped_unconfigured"]
    assert unconfigured.read_bytes() == untouched
    refreshed = json.loads(configured.read_text())["mcpServers"]
    assert refreshed["minni"]["args"][-1] == str(install_root / "dist" / "server.js")
    assert refreshed["other"] == {"command": "keep"}


def test_explicit_wire_keeps_full_surface_write(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install_root = _install_root(tmp_path)
    _view(tmp_path, ".gemini/config/mcp_config.json", {"other": {"command": "keep"}})
    fresh = _view(tmp_path, ".gemini/antigravity/mcp_config.json", {"other": {"command": "keep"}})
    result = update_antigravity_config(
        install_root, "antigravity", tmp_path / "vault",
        tmp_path / "sock", tmp_path / "ws",
    )
    assert str(fresh) in result["views_written"]
    assert "minni" in json.loads(fresh.read_text())["mcpServers"]


def test_existing_only_legacy_binding_counts_as_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    install_root = _install_root(tmp_path)
    legacy = _view(
        tmp_path, ".gemini/config/mcp_config.json",
        {"sovereign-memory": {"command": "node", "args": ["/old/dist/server.js"]}},
    )
    result = update_antigravity_config(
        install_root, "antigravity", tmp_path / "vault",
        tmp_path / "sock", tmp_path / "ws", existing_only=True,
    )
    assert str(legacy) in result["views_written"]
    servers = json.loads(legacy.read_text())["mcpServers"]
    assert "sovereign-memory" not in servers and "minni" in servers


def test_propagate_existing_only_does_not_activate_unconfigured_view(tmp_path, monkeypatch):
    """Standalone installer mirror: --existing-only leaves other views alone."""
    propagate = _load_propagate()
    try:
        monkeypatch.setenv("HOME", str(tmp_path))
        install_root = _install_root(tmp_path)
        configured = _view(
            tmp_path, ".gemini/config/mcp_config.json",
            {"minni": {"command": "node", "args": ["/old/dist/server.js"]}},
        )
        unconfigured = _view(
            tmp_path, ".gemini/antigravity/mcp_config.json",
            {"other": {"command": "keep"}},
        )
        untouched = unconfigured.read_bytes()
        result = propagate.update_antigravity_config(
            install_root, "antigravity", tmp_path / "vault",
            tmp_path / "sock", tmp_path / "ws", existing_only=True,
        )
        assert str(configured) in result["views_written"]
        assert str(unconfigured) in result["views_skipped_unconfigured"]
        assert unconfigured.read_bytes() == untouched
        assert "minni" not in json.loads(unconfigured.read_text())["mcpServers"]
    finally:
        sys.modules.pop("propagate_antigravity_views", None)


def test_propagate_explicit_install_remains_intentional(tmp_path, monkeypatch):
    propagate = _load_propagate()
    try:
        monkeypatch.setenv("HOME", str(tmp_path))
        install_root = _install_root(tmp_path)
        fresh = _view(tmp_path, ".gemini/antigravity/mcp_config.json", {"other": {"command": "keep"}})
        result = propagate.update_antigravity_config(
            install_root, "antigravity", tmp_path / "vault",
            tmp_path / "sock", tmp_path / "ws",
        )
        assert str(fresh) in result["views_written"]
        assert "minni" in json.loads(fresh.read_text())["mcpServers"]
    finally:
        sys.modules.pop("propagate_antigravity_views", None)

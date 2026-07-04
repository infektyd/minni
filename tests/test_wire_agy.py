"""Unit tests for update_agy_plugin_hooks in minni.wire.writers."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from minni.wire.writers import update_agy_plugin_hooks

HOOKS_TEMPLATE = {
    "_comment": "test template",
    "hooks": {
        "PreToolUse": [
            {"hooks": [{"type": "command", "command": "node __MINNI_GEMINI_DIST__/gemini-hook.js PreToolUse"}]}
        ],
    },
}


def _install_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin" / "0.2.0"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "hooks-gemini.json").write_text(
        json.dumps(HOOKS_TEMPLATE), encoding="utf-8",
    )
    return root


def _fake_agy(tmp_path: Path) -> Path:
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agy.chmod(agy.stat().st_mode | stat.S_IXUSR)
    return agy


def test_agy_missing_graceful_skip(tmp_path, monkeypatch):
    install_root = _install_root(tmp_path)
    monkeypatch.setattr("minni.wire.writers.shutil.which", lambda _name: None)
    result = update_agy_plugin_hooks(install_root)
    assert result["installed"] is False
    assert "agy CLI not found" in str(result["reason"])


def test_agy_present_succeeds(tmp_path, monkeypatch):
    install_root = _install_root(tmp_path)
    agy = _fake_agy(tmp_path)
    plugins_dir = tmp_path / "gemini-config-plugins"
    installed_hooks = plugins_dir / "minni" / "hooks.json"
    installed_hooks.parent.mkdir(parents=True)
    installed_hooks.write_text(
        json.dumps({
            "hooks": {
                "PreToolUse": [
                    {
                        "hooks": [{
                            "type": "command",
                            "command": f"node {install_root / 'dist'}/gemini-hook.js PreToolUse",
                        }],
                    },
                ],
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr("minni.wire.writers.AGY_PLUGINS_DIR", str(plugins_dir))
    monkeypatch.setattr(
        "minni.wire.writers.shutil.which",
        lambda name: str(agy) if name == "agy" else None,
    )
    monkeypatch.setattr(
        "minni.wire.writers.subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})(),
    )
    result = update_agy_plugin_hooks(install_root)
    assert result["installed"] is True
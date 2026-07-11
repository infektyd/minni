from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402


def test_cursor_config_is_portable_and_preserves_unrelated_entries(tmp_path):
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}))
    (cursor / "hooks.json").write_text(json.dumps({
        "version": 1,
        "hooks": {
            "sessionStart": [
                {"command": "node /opt/other/start.js"},
                {"command": "node /work/minni-tools/unrelated.js"},
                {"command": "node ~/.cursor/plugins/minni@minni/dist/hook.js SessionStart"},
            ]
        },
    }))
    install = cursor / "plugins" / "local" / "minni"
    server = install / "dist" / "server.js"
    propagate.update_cursor_config(
        install, server, "cursor", tmp_path / ".minni/cursor-vault",
        tmp_path / ".minni/run/minnid.sock", tmp_path / "workspace", home=tmp_path,
        explicit_workspace=True,
    )
    mcp = json.loads((cursor / "mcp.json").read_text())
    assert mcp["mcpServers"]["other"] == {"command": "other"}
    assert mcp["mcpServers"]["minni"]["env"]["MINNI_AGENT_ID"] == "cursor"
    assert mcp["mcpServers"]["minni"]["env"]["MINNI_VAULT_PATH"].endswith("cursor-vault")
    hooks = json.loads((cursor / "hooks.json").read_text())["hooks"]
    assert hooks["sessionStart"][0] == {"command": "node /opt/other/start.js"}
    assert hooks["sessionStart"][1] == {"command": "node /work/minni-tools/unrelated.js"}
    for event in propagate.CURSOR_HOOK_EVENTS:
        assert not any("minni" in e.get("command", "") for e in hooks[event]
                       if e.get("command") != "node /work/minni-tools/unrelated.js")
    installed = json.loads((install / "hooks/hooks-cursor.json").read_text())["hooks"]
    for event in propagate.CURSOR_HOOK_EVENTS:
        assert len(installed[event]) == 1
        command = installed[event][0]["command"]
        assert str(install) in command
        assert "dist/hook.js" not in command
        assert "MINNI_CURSOR_AGENT_ID=cursor" in command
        assert "MINNI_CURSOR_VAULT_PATH=" in command
        assert "MINNI_CURSOR_WORKSPACE_ID=workspace-workspace" in command


def test_cursor_update_is_idempotent(tmp_path):
    install = tmp_path / ".cursor/plugins/local/minni"
    args = (install, install / "dist/server.js", "cursor", tmp_path / ".minni/cursor-vault",
            tmp_path / ".minni/run/minnid.sock", tmp_path / "workspace")
    propagate.update_cursor_config(*args, home=tmp_path)
    first = (install / "hooks/hooks-cursor.json").read_text()
    propagate.update_cursor_config(*args, home=tmp_path)
    assert (install / "hooks/hooks-cursor.json").read_text() == first


def test_cursor_hook_command_quotes_install_paths_with_spaces(tmp_path):
    entry = propagate.cursor_hook_entry(tmp_path / "Cursor Plugins" / "minni", "sessionStart")
    assert "'" in entry["command"]
    assert "Cursor Plugins" in entry["command"]


def test_cursor_hook_command_stamps_installer_overrides(tmp_path):
    entry = propagate.cursor_hook_entry(
        tmp_path / "plugin", "stop", "cursor-alt", tmp_path / "alt vault", tmp_path / "Repo",
    )
    command = entry["command"]
    assert "MINNI_CURSOR_AGENT_ID=cursor-alt" in command
    assert "MINNI_CURSOR_VAULT_PATH=" in command and "alt vault" in command
    assert "MINNI_CURSOR_WORKSPACE_ID=workspace-repo" in command


def test_cursor_flagless_update_preserves_existing_workspace_and_fresh_fails_closed(tmp_path):
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install = cursor / "plugins/local/minni"
    server = install / "dist/server.js"
    mcp = {"mcpServers": {"minni": {"env": {"MINNI_WORKSPACE_ID": "workspace-existing"}}}}
    (cursor / "mcp.json").write_text(json.dumps(mcp))
    propagate.update_cursor_config(
        install, server, "cursor", tmp_path / ".minni/cursor-vault",
        tmp_path / ".minni/run/minnid.sock", Path("workspace-unknown"), home=tmp_path,
    )
    updated = json.loads((cursor / "mcp.json").read_text())
    assert updated["mcpServers"]["minni"]["env"]["MINNI_WORKSPACE_ID"] == "workspace-existing"
    command = json.loads((install / "hooks/hooks-cursor.json").read_text())["hooks"]["sessionStart"][0]["command"]
    assert "MINNI_CURSOR_WORKSPACE_ID=workspace-existing" in command

    fresh = tmp_path / "fresh"
    propagate.update_cursor_config(
        fresh / ".cursor/plugins/local/minni", fresh / ".cursor/plugins/local/minni/dist/server.js",
        "cursor", fresh / ".minni/cursor-vault", fresh / ".minni/run/minnid.sock",
        Path("workspace-unknown"), home=fresh,
    )
    fresh_mcp = json.loads((fresh / ".cursor/mcp.json").read_text())
    assert fresh_mcp["mcpServers"]["minni"]["env"]["MINNI_WORKSPACE_ID"] == "workspace-unknown"


def test_cursor_migrates_only_recognized_legacy_install(tmp_path):
    cursor = tmp_path / ".cursor"
    legacy = cursor / "plugins/minni@minni"
    legacy.mkdir(parents=True)
    (legacy / "package.json").write_text(json.dumps({"name": "minni-multi-plugin"}))
    install = cursor / "plugins/local/minni"
    propagate.update_cursor_config(
        install, install / "dist/server.js", "cursor", tmp_path / "vault", tmp_path / "socket",
        Path("workspace-unknown"), home=tmp_path,
    )
    assert not legacy.exists()
    archived = list((cursor / "legacy-plugins").glob("minni@minni-pre-native*"))
    assert len(archived) == 1
    assert not archived[0].is_symlink()

    unrelated = cursor / "plugins/minni@minni"
    unrelated.mkdir(parents=True)
    (unrelated / "package.json").write_text(json.dumps({"name": "not-minni"}))
    assert propagate.migrate_legacy_cursor_install(cursor, install) is None
    assert unrelated.exists()

"""Tests for the Grok Build and Cursor hook install paths.

Both were entirely absent before: `hooks-grok.json` and `hooks-cursor.json` sat
in the repo as templates that nothing installed. On Grok the only working hooks
on any machine were hand-written outside version control and would not survive a
clean install; on Cursor `grep -ci cursor` over propagate.py returned two hits,
both `conn.cursor()` from sqlite.

The placeholder assertions are the load-bearing ones. Neither
`${GROK_PLUGIN_ROOT}` nor `${CURSOR_PLUGIN_ROOT}` expands in the user-level
config roots these functions write to -- an unstamped placeholder means the hook
command resolves to a bare `node /dist/...` and silently never runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402

REPO_HOOKS = Path(__file__).resolve().parents[3] / "hooks"


def _install_root(tmp_path: Path) -> Path:
    """A fake install root carrying the real hook templates."""
    root = tmp_path / "install"
    (root / "hooks").mkdir(parents=True)
    for name in ("hooks-grok.json", "hooks-cursor.json"):
        (root / "hooks" / name).write_text((REPO_HOOKS / name).read_text())
    return root


# --- Grok Build ------------------------------------------------------------


def test_grok_hooks_installed_with_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _install_root(tmp_path)

    result = propagate.update_grok_hooks(root)

    assert result["installed"] is True
    written = Path(result["path"])
    assert written == tmp_path / ".grok/hooks/minni.json"

    raw = written.read_text()
    assert "${GROK_PLUGIN_ROOT}" not in raw, (
        "~/.grok/hooks/ is not a plugin root, so GROK_PLUGIN_ROOT is never "
        "injected there; an unstamped placeholder yields `node /dist/...`"
    )
    assert "PLUGIN_ROOT" not in raw
    assert str(root) in raw

    data = json.loads(raw)
    for event in ("SessionStart", "UserPromptSubmit", "PreCompact", "Stop"):
        assert event in data["hooks"], event


def test_grok_stop_gets_the_long_timeout(tmp_path, monkeypatch):
    """Stop runs prepareOutcome (daemon + AFM); Grok's own default here is 600s.

    Hook timeouts are fail-OPEN, so an under-provisioned Stop silently drops
    learn candidates with no error surfaced anywhere.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _install_root(tmp_path)

    propagate.update_grok_hooks(root)
    data = json.loads((tmp_path / ".grok/hooks/minni.json").read_text())

    stop = data["hooks"]["Stop"][0]["hooks"][0]
    assert stop["timeout"] >= 600


def test_grok_rules_file_carries_boot_hydration(tmp_path, monkeypatch):
    """Hooks cannot hydrate on Grok, so the rules file must do it instead."""
    monkeypatch.setenv("HOME", str(tmp_path))

    result = propagate.write_grok_rules()

    written = Path(result["path"])
    assert written == tmp_path / ".grok/rules/minni.md"
    body = written.read_text()
    # It must name the tool; Grok reaches MCP tools by qualified name.
    assert "minni__minni_recall" in body
    # It is billed into EVERY Grok session on the machine -- keep it small.
    assert len(body) < 1200, "rules file is charged to every session; keep it short"


# --- Cursor ----------------------------------------------------------------


def test_cursor_hooks_installed_with_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _install_root(tmp_path)

    result = propagate.update_cursor_hooks(root)

    assert result["installed"] is True
    raw = (tmp_path / ".cursor/hooks.json").read_text()
    assert "${CURSOR_PLUGIN_ROOT}" not in raw, (
        "CURSOR_PLUGIN_ROOT expansion is undocumented AND plugin-scoped; it does "
        "not apply to the user-level hooks.json"
    )
    assert str(root) in raw

    data = json.loads(raw)
    assert data["version"] == 1
    assert "sessionStart" in data["hooks"]


def test_cursor_install_preserves_the_users_own_hooks(tmp_path, monkeypatch):
    """A user's hooks.json is theirs; installing Minni must not clobber it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _install_root(tmp_path)

    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    (cursor / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "sessionStart": [{"command": "/usr/local/bin/my-own-hook"}],
                    "afterFileEdit": [{"command": "/usr/local/bin/formatter"}],
                },
            }
        )
    )

    propagate.update_cursor_hooks(root)
    data = json.loads((cursor / "hooks.json").read_text())

    commands = json.dumps(data)
    assert "/usr/local/bin/my-own-hook" in commands, "user's own hook was clobbered"
    assert "afterFileEdit" in data["hooks"], "unrelated event was dropped"
    assert any(str(root) in json.dumps(e) for e in data["hooks"]["sessionStart"])


def test_cursor_reinstall_is_idempotent(tmp_path, monkeypatch):
    """Re-running the installer must not stack duplicate Minni entries."""
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _install_root(tmp_path)

    propagate.update_cursor_hooks(root)
    propagate.update_cursor_hooks(root)

    data = json.loads((tmp_path / ".cursor/hooks.json").read_text())
    mine = [e for e in data["hooks"]["sessionStart"] if str(root) in json.dumps(e)]
    assert len(mine) == 1, f"expected one Minni sessionStart entry, got {len(mine)}"


# --- Claude Desktop ---------------------------------------------------------


def _desktop_cfg(tmp_path: Path) -> Path:
    return tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"


def test_claude_desktop_registers_under_the_shared_identity(tmp_path, monkeypatch):
    """Desktop and Code are one person on one machine: same agent, same vault.

    Claude Desktop's config tree is fully disjoint from ~/.claude/, so this is a
    separate write -- but the identity is deliberately shared, the way the three
    Antigravity surfaces share one `gemini` identity and vault.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    _desktop_cfg(tmp_path).parent.mkdir(parents=True)

    result = propagate.update_claude_desktop_config(
        server_path=tmp_path / "dist" / "server.js",
        agent="claude-code",
        vault=Path("/v"),
        socket_path=Path("/s"),
        workspace=Path("/w"),
    )

    assert result["installed"] is True
    server = json.loads(_desktop_cfg(tmp_path).read_text())["mcpServers"]["minni"]
    assert server["env"]["MINNI_AGENT_ID"] == "claude-code"
    assert server["env"]["MINNI_VAULT_PATH"] == "/v"


def test_claude_desktop_preserves_other_servers_and_keys(tmp_path, monkeypatch):
    """The file also holds preferences and cowork paths -- never clobber them."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _desktop_cfg(tmp_path)
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {"other": {"command": "node", "args": ["/x.js"]}},
                "preferences": {"theme": "dark"},
                "coworkUserFilesPath": "/some/path",
            }
        )
    )

    propagate.update_claude_desktop_config(
        tmp_path / "dist" / "server.js", "claude-code", Path("/v"), Path("/s"), Path("/w")
    )

    data = json.loads(cfg.read_text())
    assert "other" in data["mcpServers"], "another MCP server was clobbered"
    assert data["preferences"] == {"theme": "dark"}, "unrelated top-level key lost"
    assert data["coworkUserFilesPath"] == "/some/path"
    assert "minni" in data["mcpServers"]


def test_claude_desktop_absent_is_reported_not_crashed(tmp_path, monkeypatch):
    """A machine without Claude Desktop must not fail the whole install."""
    monkeypatch.setenv("HOME", str(tmp_path))

    result = propagate.update_claude_desktop_config(
        tmp_path / "dist" / "server.js", "claude-code", Path("/v"), Path("/s"), Path("/w")
    )

    assert result["installed"] is False
    assert "not installed" in str(result["reason"])

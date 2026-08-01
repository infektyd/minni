"""Claude Code plugin-surface registration and the one-time adoption cutover.

Covers the invariants the design leans on: the registration is idempotent to the
byte, it never damages other plugins' entries, GC can see it, and the cutover
refuses to delete a tree anything still points into.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minni.wire.claude_plugin import (
    ClaudePluginError,
    adopt_claude_code,
    installed_plugins_path,
    known_marketplaces_path,
    legacy_cache_root,
    register_claude_plugin,
    remove_legacy_cache,
    repoint_claude_desktop,
    retire_claude_marketplace,
)
from minni.wire.gc import run_gc
from minni.wire.wired import wired_record


@pytest.fixture
def home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _install_tree(home: Path, version: str) -> Path:
    root = home / ".minni" / "plugin" / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / "hooks").mkdir()
    (root / "dist").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "minni", "version": version}), encoding="utf-8",
    )
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    (root / "dist" / "server.js").write_text("// stub\n", encoding="utf-8")
    (root / "payload-manifest.json").write_text(
        json.dumps({"schema": 1, "version": version, "git_sha": "abc123"}),
        encoding="utf-8",
    )
    return root


def _write_wired(home: Path, install_root: Path, version: str) -> None:
    base = home / ".minni" / "plugin"
    base.mkdir(parents=True, exist_ok=True)
    (base / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "generation": 1,
            "wires": [{
                "platform": "claude-code",
                "config_path": str(home / ".claude.json"),
                "install_root": str(install_root),
                "version": version,
                "workspace": None,
                "wired_at": "2026-08-01T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )


def _registry(home: Path) -> dict:
    return json.loads(installed_plugins_path().read_text(encoding="utf-8"))


# --- registration -----------------------------------------------------------


def test_register_creates_entry_when_registry_absent(home):
    root = _install_tree(home, "0.4.0+git.abc1234")
    result = register_claude_plugin(root, "0.4.0+git.abc1234", git_sha="abc123")

    assert result["changed"] is True
    assert result["created"] is True
    entry = _registry(home)["plugins"]["minni@minni"][0]
    assert entry["installPath"] == str(root)
    assert entry["scope"] == "user"
    assert entry["gitCommitSha"] == "abc123"
    assert _registry(home)["version"] == 2


def test_register_is_byte_identical_on_repeat(home):
    """A re-wire of an unchanged version must not even move lastUpdated."""
    root = _install_tree(home, "0.4.0")
    register_claude_plugin(root, "0.4.0", git_sha="abc123")
    first = installed_plugins_path().read_bytes()

    result = register_claude_plugin(root, "0.4.0", git_sha="abc123")

    assert result["changed"] is False
    assert installed_plugins_path().read_bytes() == first


def test_register_preserves_other_plugins_and_scopes(home):
    path = installed_plugins_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {
            "superpowers@official": [{"scope": "user", "installPath": "/sp", "version": "6.2.0"}],
            "minni@minni": [
                {"scope": "project", "installPath": "/proj", "version": "0.1.0"},
                {"scope": "user", "installPath": "/old", "version": "0.3.0",
                 "installedAt": "2026-07-11T16:14:57.558Z"},
            ],
        },
    }), encoding="utf-8")
    root = _install_tree(home, "0.4.0")

    register_claude_plugin(root, "0.4.0", git_sha="abc123")

    data = _registry(home)
    assert data["plugins"]["superpowers@official"][0]["installPath"] == "/sp"
    entries = data["plugins"]["minni@minni"]
    project = [e for e in entries if e["scope"] == "project"][0]
    assert project["installPath"] == "/proj", "another scope was clobbered"
    user = [e for e in entries if e["scope"] == "user"][0]
    assert user["installPath"] == str(root)
    assert user["installedAt"] == "2026-07-11T16:14:57.558Z", "installedAt must survive"
    assert user["lastUpdated"] != user["installedAt"]


def test_register_drops_unvouched_git_sha(home):
    """A sha we cannot vouch for describes some other tree."""
    root = _install_tree(home, "0.4.0")
    register_claude_plugin(root, "0.4.0", git_sha="abc123")
    register_claude_plugin(root, "0.4.1", git_sha="unknown")

    entry = _registry(home)["plugins"]["minni@minni"][0]
    assert "gitCommitSha" not in entry


def test_register_refuses_to_overwrite_corrupt_registry(home):
    path = installed_plugins_path()
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    root = _install_tree(home, "0.4.0")

    with pytest.raises(ClaudePluginError, match="not valid JSON"):
        register_claude_plugin(root, "0.4.0")
    assert path.read_text(encoding="utf-8") == "{ not json", "corrupt file was rewritten"


def test_register_dry_run_writes_nothing(home):
    root = _install_tree(home, "0.4.0")
    result = register_claude_plugin(root, "0.4.0", dry_run=True)

    assert result["changed"] is True
    assert not installed_plugins_path().exists()


# --- GC reference tracking --------------------------------------------------


def test_gc_retains_the_registered_tree_without_a_wired_record(home):
    """The registration alone must protect its tree.

    wired.json is not enough: a wire run that fails verification stamps configs
    and returns before upsert_wire, so the registry can be the only reference.
    """
    root = _install_tree(home, "0.2.0")
    for stale in ("0.3.0", "0.4.0"):
        _install_tree(home, stale)
    register_claude_plugin(root, "0.2.0")

    result = run_gc(prune=True, stdin_is_tty=False)

    assert root.is_dir(), "GC collected the tree Claude Code loads hooks from"
    assert str(root) not in result.pruned
    assert str(root) in result.retained_in_use


# --- marketplace / desktop / legacy cache -----------------------------------


def test_retire_marketplace_removes_only_the_minni_entry(home):
    path = known_marketplaces_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "minni": {"installLocation": "/stale/worktree"},
        "claude-plugins-official": {"installLocation": "/official"},
    }), encoding="utf-8")

    result = retire_claude_marketplace()

    assert result["changed"] is True
    assert result["removed_source"] == "/stale/worktree"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "minni" not in data
    assert "claude-plugins-official" in data


def test_retire_marketplace_is_idempotent(home):
    path = known_marketplaces_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"other": {}}), encoding="utf-8")

    assert retire_claude_marketplace()["changed"] is False


def test_repoint_desktop_moves_only_args_zero(home):
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "mcpServers": {
            "minni": {
                "command": "node",
                "args": ["/old/cache/dist/server.js"],
                "env": {"MINNI_WORKSPACE_ID": "workspace-minni"},
            },
            "other": {"command": "node", "args": ["/x.js"]},
        },
        "preferences": {"theme": "dark"},
    }), encoding="utf-8")
    root = _install_tree(home, "0.4.0")

    result = repoint_claude_desktop(root)

    assert result["changed"] is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["minni"]["args"] == [str(root / "dist" / "server.js")]
    assert data["mcpServers"]["minni"]["env"]["MINNI_WORKSPACE_ID"] == "workspace-minni"
    assert data["mcpServers"]["other"]["args"] == ["/x.js"]
    assert data["preferences"] == {"theme": "dark"}
    assert repoint_claude_desktop(root)["changed"] is False


def test_repoint_desktop_noop_when_not_installed(home):
    root = _install_tree(home, "0.4.0")
    assert repoint_claude_desktop(root)["changed"] is False


def test_remove_legacy_cache_refuses_when_install_root_is_inside(home):
    cache = legacy_cache_root() / "minni" / "0.3.0"
    cache.mkdir(parents=True)

    with pytest.raises(ClaudePluginError, match="is inside it"):
        remove_legacy_cache(cache)
    assert cache.is_dir()


def test_remove_legacy_cache_reports_versions(home):
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")

    result = remove_legacy_cache(root)

    assert result["changed"] is True
    assert result["removed_versions"] == [str(legacy_cache_root() / "minni" / "0.3.0")]
    assert not legacy_cache_root().exists()


# --- adopt ------------------------------------------------------------------


def test_adopt_requires_a_wired_claude_code(home):
    with pytest.raises(ClaudePluginError, match="not wired yet"):
        adopt_claude_code(apply=True)


def test_adopt_rejects_a_root_that_is_not_a_plugin_tree(home):
    root = home / ".minni" / "plugin" / "0.4.0"
    root.mkdir(parents=True)
    _write_wired(home, root, "0.4.0")

    with pytest.raises(ClaudePluginError, match="not a plugin tree"):
        adopt_claude_code(apply=True)


def test_adopt_dry_run_writes_nothing(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)

    result = adopt_claude_code()

    assert result["applied"] is False
    assert result["steps"]["register"]["changed"] is True
    assert not installed_plugins_path().exists()
    assert (legacy_cache_root() / "minni" / "0.3.0").is_dir()


def test_adopt_apply_performs_every_step(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    marketplaces = known_marketplaces_path()
    marketplaces.write_text(json.dumps({"minni": {"installLocation": "/stale"}}), encoding="utf-8")

    result = adopt_claude_code(apply=True)

    assert result["applied"] is True
    assert _registry(home)["plugins"]["minni@minni"][0]["installPath"] == str(root)
    assert _registry(home)["plugins"]["minni@minni"][0]["gitCommitSha"] == "abc123"
    assert "minni" not in json.loads(marketplaces.read_text(encoding="utf-8"))
    assert not legacy_cache_root().exists()


def test_adopt_keep_legacy_cache_leaves_the_tree(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)

    result = adopt_claude_code(apply=True, keep_legacy_cache=True)

    assert result["steps"]["legacy_cache"]["changed"] is False
    assert (legacy_cache_root() / "minni" / "0.3.0").is_dir()


def test_adopt_is_idempotent(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    adopt_claude_code(apply=True)

    second = adopt_claude_code(apply=True)

    assert second["steps"]["register"]["changed"] is False
    assert second["steps"]["marketplace"]["changed"] is False
    assert second["steps"]["legacy_cache"]["changed"] is False


# --- wired.json reader ------------------------------------------------------


def test_wired_record_returns_the_platform_entry(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")

    assert wired_record("claude-code")["install_root"] == str(root)
    assert wired_record("codex") is None

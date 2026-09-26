"""Claude Code plugin-surface registration and the one-time adoption cutover.

Covers the invariants the design leans on: the registration is idempotent to the
byte, it never damages other plugins' entries, GC can see it, and the cutover
refuses to delete a tree anything still points into.
"""

from __future__ import annotations

import errno
import json
import unicodedata
from pathlib import Path

import pytest

from minni.wire import claude_plugin
from minni.wire.claude_plugin import (
    ClaudePluginError,
    adopt_claude_code,
    claude_adopt_pending,
    claude_settings_path,
    ensure_claude_marketplace,
    local_marketplace_root,
    follow_claude_desktop,
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


def _raise_enospc(path, data):
    raise OSError(errno.ENOSPC, "No space left on device")


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


def test_register_write_failure_is_a_claude_plugin_error(home, monkeypatch):
    """Callers catch only ClaudePluginError; a raw OSError is a traceback."""
    root = _install_tree(home, "0.4.0")
    monkeypatch.setattr(claude_plugin, "_atomic_write_json", _raise_enospc)

    with pytest.raises(ClaudePluginError, match="cannot update"):
        register_claude_plugin(root, "0.4.0")


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


def test_retire_marketplace_write_failure_is_a_claude_plugin_error(home, monkeypatch):
    path = known_marketplaces_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"minni": {"installLocation": "/stale"}}), encoding="utf-8")
    monkeypatch.setattr(claude_plugin, "_atomic_write_json", _raise_enospc)

    with pytest.raises(ClaudePluginError, match="cannot update"):
        retire_claude_marketplace()


def _legacy_server(version: str = "0.3.0") -> str:
    return str(legacy_cache_root() / "minni" / version / "dist" / "server.js")


def _write_desktop(home: Path, minni_entry: dict) -> Path:
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "mcpServers": {
            "minni": minni_entry,
            "other": {"command": "node", "args": ["/x.js"]},
        },
        "preferences": {"theme": "dark"},
    }), encoding="utf-8")
    return cfg


def test_repoint_desktop_moves_the_cache_arg(home):
    cfg = _write_desktop(home, {
        "command": "node",
        "args": [_legacy_server()],
        "env": {"MINNI_WORKSPACE_ID": "workspace-minni"},
    })
    root = _install_tree(home, "0.4.0")

    result = repoint_claude_desktop(root)

    assert result["changed"] is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["minni"]["args"] == [str(root / "dist" / "server.js")]
    assert data["mcpServers"]["minni"]["env"]["MINNI_WORKSPACE_ID"] == "workspace-minni"
    assert data["mcpServers"]["other"]["args"] == ["/x.js"]
    assert data["preferences"] == {"theme": "dark"}
    assert repoint_claude_desktop(root)["changed"] is False


def test_repoint_desktop_preserves_flags_before_the_path(home):
    """The rewrite targets the arg that points into the cache, not args[0].

    Replacing args[0] blindly destroys the flag *and* keeps the doomed cache
    path as an argument, which the cache deletion would then invalidate.
    """
    cfg = _write_desktop(home, {
        "command": "node",
        "args": ["--inspect", _legacy_server()],
    })
    root = _install_tree(home, "0.4.0")

    result = repoint_claude_desktop(root)

    assert result["changed"] is True
    assert result["replaced"] == [_legacy_server()]
    args = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["minni"]["args"]
    assert args == ["--inspect", str(root / "dist" / "server.js")]


def test_repoint_desktop_moves_only_the_server_argument(home):
    """A sibling path under the same tree is not a second server pointer."""
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    cfg = _write_desktop(home, {
        "command": "node",
        "args": [_legacy_server(), "--config", str(
            legacy_cache_root() / "minni" / "0.3.0" / "cfg.json",
        )],
    })
    root = _install_tree(home, "0.4.0")

    result = repoint_claude_desktop(root)

    assert result["replaced"] == [_legacy_server()]
    args = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["minni"]["args"]
    assert args[1] == "--config"
    assert args[2].endswith("cfg.json"), "a non-server path was rewritten to server.js"
    # ...and the leftover cache path must still block the deletion.
    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)


def test_repoint_desktop_leaves_unrelated_paths_alone(home):
    cfg = _write_desktop(home, {"command": "node", "args": ["/elsewhere/server.js"]})
    root = _install_tree(home, "0.4.0")

    result = repoint_claude_desktop(root)

    assert result["changed"] is False
    args = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["minni"]["args"]
    assert args == ["/elsewhere/server.js"]


def test_follow_desktop_moves_an_adopted_entry_to_the_new_version(home):
    """A later wire must not strand Desktop on a version GC will prune."""
    old = _install_tree(home, "0.4.0")
    cfg = _write_desktop(home, {
        "command": "node", "args": [str(old / "dist" / "server.js")],
    })
    new = _install_tree(home, "0.4.1")

    result = follow_claude_desktop(new)

    assert result["changed"] is True
    args = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["minni"]["args"]
    assert args == [str(new / "dist" / "server.js")]
    assert follow_claude_desktop(new)["changed"] is False


def test_follow_desktop_is_a_noop_before_adoption(home):
    """Un-adopted machines still point into the cache; wiring must not touch it."""
    cfg = _write_desktop(home, {"command": "node", "args": [_legacy_server()]})
    root = _install_tree(home, "0.4.0")

    assert follow_claude_desktop(root)["changed"] is False
    args = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["minni"]["args"]
    assert args == [_legacy_server()]


def test_gc_retains_a_version_only_claude_desktop_references(home):
    """Desktop's config is a real reference; GC pruning it would break Desktop."""
    root = _install_tree(home, "0.2.0")
    for stale in ("0.3.0", "0.4.0"):
        _install_tree(home, stale)
    _write_desktop(home, {"command": "node", "args": [str(root / "dist" / "server.js")]})

    result = run_gc(prune=True, stdin_is_tty=False)

    assert root.is_dir(), "GC collected the tree Claude Desktop launches from"
    assert str(root) not in result.pruned


def test_adopt_pending_tracks_the_marketplace_and_cache(home):
    assert claude_adopt_pending() is False

    path = known_marketplaces_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"minni": {"installLocation": "/stale"}}), encoding="utf-8")
    assert claude_adopt_pending() is True

    path.write_text(json.dumps({}), encoding="utf-8")
    assert claude_adopt_pending() is False

    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    assert claude_adopt_pending() is True


def test_repoint_desktop_refuses_a_command_inside_the_cache(home):
    _write_desktop(home, {
        "command": str(legacy_cache_root() / "minni" / "0.3.0" / "bin" / "launcher"),
        "args": [],
    })
    root = _install_tree(home, "0.4.0")

    with pytest.raises(ClaudePluginError, match="points into"):
        repoint_claude_desktop(root)


def test_repoint_desktop_write_failure_is_a_claude_plugin_error(home, monkeypatch):
    _write_desktop(home, {"command": "node", "args": [_legacy_server()]})
    root = _install_tree(home, "0.4.0")
    monkeypatch.setattr(claude_plugin, "_atomic_write_json", _raise_enospc)

    with pytest.raises(ClaudePluginError, match="cannot update"):
        repoint_claude_desktop(root)


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


def test_remove_legacy_cache_refuses_a_project_scope_referrer(home):
    """A non-user-scope entry still pointing into the cache blocks the delete.

    Adopt only rewrites the user-scope entry; deleting the tree out from under
    any other scope leaves that registration dangling.
    """
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    stale = str(legacy_cache_root() / "minni" / "0.3.0")
    path = installed_plugins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {"minni@minni": [{"scope": "project", "installPath": stale}]},
    }), encoding="utf-8")

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)
    assert (legacy_cache_root() / "minni" / "0.3.0").is_dir()


@pytest.mark.parametrize("relative", [".claude.json", ".claude/settings.json"])
def test_remove_legacy_cache_refuses_referrers_in_other_configs(home, relative):
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    cfg = home / relative
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps({"anything": {"nested": [str(legacy_cache_root() / "minni")]}}),
        encoding="utf-8",
    )

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)


def test_remove_legacy_cache_refuses_a_desktop_referrer(home):
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    _write_desktop(home, {"command": "node", "args": ["--inspect", _legacy_server()]})

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)


def test_remove_legacy_cache_spares_a_sibling_plugin(home):
    """Deletion is scoped to cache/minni/minni, not the whole marketplace dir."""
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    sibling = legacy_cache_root() / "otherplugin" / "1.0.0"
    sibling.mkdir(parents=True)
    root = _install_tree(home, "0.4.0")

    result = remove_legacy_cache(root)

    assert result["changed"] is True
    assert sibling.is_dir()
    assert result["siblings_kept"] == [str(legacy_cache_root() / "otherplugin")]
    assert not (legacy_cache_root() / "minni").exists()


def test_remove_legacy_cache_refuses_a_hook_command_string(home):
    """Claude Code hooks are shell command strings, not argv arrays.

    The cache path is embedded in a larger string there, so structured
    path-matching alone would clear the deletion and orphan the hook.
    """
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    hook = legacy_cache_root() / "minni" / "0.3.0" / "dist" / "hook.js"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": f"node {hook} SessionStart"}]},
        ]},
    }), encoding="utf-8")

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)
    assert (legacy_cache_root() / "minni" / "0.3.0").is_dir()


def test_remove_legacy_cache_gate_survives_a_non_ascii_home(tmp_path, monkeypatch):
    """json.dumps escapes non-ASCII by default, which would silently kill the gate.

    On a HOME like /Users/Hakan the blob would hold "\\u00e5" while the needle
    holds the literal character, so the substring layer would match nothing at
    all — a safety check that is off and says nothing.
    """
    fake_home = tmp_path / "hemkatalog-aao-åäö"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(fake_home, "0.4.0")
    hook = legacy_cache_root() / "minni" / "0.3.0" / "dist" / "hook.js"
    cfg = fake_home / ".claude.json"
    cfg.write_text(json.dumps({"h": f"node {hook} SessionStart"}), encoding="utf-8")

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)


def test_remove_legacy_cache_gate_respects_path_boundaries(home):
    """A bare substring would reintroduce the .../cache/minnix bug.

    An unrelated marketplace sharing the prefix must not block the cutover.
    """
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    unrelated = home / ".claude" / "plugins" / "cache" / "minni-tools" / "acme" / "1.0"
    path = installed_plugins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {"acme@minni-tools": [{"scope": "user", "installPath": str(unrelated)}]},
    }), encoding="utf-8")

    assert remove_legacy_cache(root)["changed"] is True


def test_remove_legacy_cache_gate_ignores_tilde_prose(home):
    """~/.claude.json holds prompt history; the repo's own docs name this path.

    Treating a tilde spelling as a referrer would poison that file permanently
    and leave --keep-legacy-cache as the only exit, i.e. the cutover could never
    complete. The absolute-path needle covers the real on-disk case.
    """
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({
        "history": ["we should delete ~/.claude/plugins/cache/minni after the cutover"],
    }), encoding="utf-8")

    assert remove_legacy_cache(root)["changed"] is True


def test_remove_legacy_cache_refusal_names_the_field_but_leaks_no_secrets(home):
    """The embedded-string branch fires exactly where people inline tokens.

    It must point at the offending field without copying the surrounding text
    into stderr, CI logs and bug reports.
    """
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    hook = legacy_cache_root() / "minni" / "0.3.0" / "dist" / "hook.js"
    cfg = home / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [
            # Stands in for the credential people really do inline here.
            {"command": f"--auth CANARY-4f9a2b7c node {hook} SessionStart"},
        ]}]},
    }), encoding="utf-8")

    with pytest.raises(ClaudePluginError) as exc:
        remove_legacy_cache(root)
    message = str(exc.value)
    assert "hooks.SessionStart[0].hooks[0].command" in message, "must name the field"
    assert "CANARY-4f9a2b7c" not in message, "refusal echoed the surrounding text"


def test_remove_legacy_cache_refusal_never_echoes_an_argument_string(home):
    """Path() accepts spaces, so a command string passes _is_under whole.

    Echoing the "path" would leak through the branch that looks safe.
    """
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    hook = legacy_cache_root() / "minni" / "0.3.0" / "dist" / "hook.js"
    cfg = home / ".claude.json"
    cfg.write_text(
        json.dumps({"cmd": f"{hook} --auth CANARY-9d3f1a"}), encoding="utf-8",
    )

    with pytest.raises(ClaudePluginError) as exc:
        remove_legacy_cache(root)
    assert "CANARY-9d3f1a" not in str(exc.value), "refusal echoed an argument string"


def test_remove_legacy_cache_catches_a_dotdot_detour_above_the_root(home):
    """The kernel resolves this straight back into the cache; relative_to does not."""
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    detour = (
        home / ".claude" / "plugins" / ".." / "plugins" / "cache"
        / "minni" / "minni" / "0.3.0"
    )
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({"x": {"installPath": str(detour)}}), encoding="utf-8")

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)


def test_remove_legacy_cache_never_echoes_config_values(home):
    """No separator trick should get config text into the message."""
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    hook = legacy_cache_root() / "minni" / "0.3.0" / "dist" / "hook.js"
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({
        "a": f"{hook}?auth=CANARY-NOSPACE",
        "b": f"{hook};export=CANARY-SEMI",
        "c": f"{hook} --auth CANARY-SPACE",
    }), encoding="utf-8")

    with pytest.raises(ClaudePluginError) as exc:
        remove_legacy_cache(root)
    assert "CANARY" not in str(exc.value), "refusal echoed config text"
    assert "a mentions" in str(exc.value), "must still name the field"


def test_remove_legacy_cache_scans_dict_keys(home):
    """~/.claude.json keys its projects map by directory, so a path can be a key."""
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    stale = str(legacy_cache_root() / "minni" / "0.3.0")
    cfg = home / ".claude.json"
    cfg.write_text(
        json.dumps({"projects": {stale: {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)


def test_remove_legacy_cache_catches_an_nfd_spelled_reference(tmp_path, monkeypatch):
    """macOS treats NFC and NFD as the same file; string comparison does not."""
    fake_home = tmp_path / unicodedata.normalize("NFC", "hem-åäö")
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(fake_home, "0.4.0")
    nfd = unicodedata.normalize("NFD", str(legacy_cache_root() / "minni" / "0.3.0"))
    cfg = fake_home / ".claude.json"
    cfg.write_text(json.dumps({"x": {"installPath": nfd}}), encoding="utf-8")

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        remove_legacy_cache(root)


def test_remove_legacy_cache_reports_strays_it_deletes(home):
    """rmtree takes loose files too; the report must not omit them."""
    target = legacy_cache_root() / "minni"
    (target / "0.3.0").mkdir(parents=True)
    (target / "stray.tar.gz").write_text("x", encoding="utf-8")
    root = _install_tree(home, "0.4.0")

    result = remove_legacy_cache(root)

    assert str(target / "stray.tar.gz") in result["removed_versions"]
    assert str(target / "0.3.0") in result["removed_versions"]
    assert not target.exists()


def test_register_refuses_a_non_list_entry_container(home):
    path = installed_plugins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {"minni@minni": {"scope": "user", "installPath": "/old"}},
    }), encoding="utf-8")
    root = _install_tree(home, "0.4.0")

    with pytest.raises(ClaudePluginError, match="is not a list"):
        register_claude_plugin(root, "0.4.0")
    assert json.loads(path.read_text(encoding="utf-8"))["plugins"]["minni@minni"] == {
        "scope": "user", "installPath": "/old",
    }


def test_remove_legacy_cache_refuses_an_unreadable_config(home):
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    root = _install_tree(home, "0.4.0")
    path = installed_plugins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ClaudePluginError, match="cannot verify"):
        remove_legacy_cache(root)


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
    assert result["steps"]["local_marketplace"]["settings"]["changed"] is True
    assert not local_marketplace_root().exists()
    assert not claude_settings_path().exists()


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


def test_adopt_dry_run_tolerates_the_registration_it_will_rewrite(home):
    """The pre-adopt user-scope entry points into the cache; that is not a blocker.

    Scanning the on-disk file would refuse on every un-adopted machine, so the
    dry run must scan the documents adopt is about to write.
    """
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    stale = str(legacy_cache_root() / "minni" / "0.3.0")
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    path = installed_plugins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {"minni@minni": [{"scope": "user", "installPath": stale}]},
    }), encoding="utf-8")

    result = adopt_claude_code()

    assert result["steps"]["legacy_cache"]["changed"] is True
    assert (legacy_cache_root() / "minni" / "0.3.0").is_dir()
    assert adopt_claude_code(apply=True)["applied"] is True
    assert not (legacy_cache_root() / "minni").exists()


def test_adopt_dry_run_scans_a_symlinked_settings_as_rewritten(home):
    """pending[] is keyed on the scanned path, not the resolved target.

    With ~/.claude/settings.json symlinked into dotfiles, keying the override
    on the resolved target leaves legacy_cache_referrers reading the stale
    on-disk doc: the dry run refuses "still referenced by" while --apply
    writes first and proceeds.
    """
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    target = home / "dotfiles" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "extraKnownMarketplaces": {
            "minni": {
                "source": {"source": "directory", "path": str(legacy_cache_root())},
            },
        },
    }), encoding="utf-8")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.symlink_to(target)

    result = adopt_claude_code()

    assert result["steps"]["legacy_cache"]["changed"] is True
    assert (legacy_cache_root() / "minni").is_dir()
    applied = adopt_claude_code(apply=True)
    assert applied["applied"] is True
    assert settings.is_symlink()
    assert not (legacy_cache_root() / "minni").exists()


def test_adopt_refuses_when_a_foreign_scope_still_points_into_the_cache(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    stale = str(legacy_cache_root() / "minni" / "0.3.0")
    (legacy_cache_root() / "minni" / "0.3.0").mkdir(parents=True)
    path = installed_plugins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {"minni@minni": [{"scope": "project", "installPath": stale}]},
    }), encoding="utf-8")

    with pytest.raises(ClaudePluginError, match="still referenced by"):
        adopt_claude_code()


def test_register_preserves_entries_it_does_not_understand(home):
    root = _install_tree(home, "0.4.0")
    path = installed_plugins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 2,
        "plugins": {"minni@minni": ["a-string-entry", {"scope": "project", "installPath": "/p"}]},
    }), encoding="utf-8")

    register_claude_plugin(root, "0.4.0")

    entries = _registry(home)["plugins"]["minni@minni"]
    assert "a-string-entry" in entries
    assert {"scope": "project", "installPath": "/p"} in entries


# --- wired.json reader ------------------------------------------------------


def test_wired_record_returns_the_platform_entry(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")

    assert wired_record("claude-code")["install_root"] == str(root)
    assert wired_record("codex") is None


def test_dry_run_wired_bookkeeping_writes_nothing(tmp_path, monkeypatch):
    """Dry-run must not touch HOME — the mkdir + O_CREAT on wired.lock were
    themselves writes, measured as a stray ~/.minni/plugin/wired.lock left by
    a dry-run `wire all` on a payload-present source tree. This pins the
    contract independent of whether the checkout has a staged payload (the
    original catch only manifested on dogfood trees)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from minni.wire.wired import WireRecord, retire_platform, upsert_wire

    record = WireRecord(
        platform="claude-code",
        config_path=str(tmp_path / ".claude.json"),
        install_root=str(tmp_path / ".minni/plugin/9.9.9"),
        version="9.9.9",
        workspace=None,
        wired_at="2026-08-06T00:00:00Z",
    )
    data, warning = upsert_wire(record, dry_run=True)
    assert warning is None
    assert data["generation"] == 1
    assert [w["platform"] for w in data["wires"]] == ["claude-code"]

    data, removed = retire_platform("claude-code", dry_run=True)
    assert removed == 0  # nothing durable existed to remove
    assert not list(tmp_path.iterdir()), (
        "dry-run wired bookkeeping wrote into HOME"
    )


# --- local stub marketplace (Claude Code >= 2.1.282) ------------------------


def _settings(home: Path) -> dict:
    return json.loads(claude_settings_path().read_text(encoding="utf-8"))


def _stub_manifest() -> dict:
    path = local_marketplace_root() / ".claude-plugin" / "marketplace.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_marketplace_fresh_install_creates_stub_and_settings(home):
    root = _install_tree(home, "0.4.0")

    result = ensure_claude_marketplace(root, "0.4.0")

    stub = local_marketplace_root()
    assert stub == home / ".claude" / "local-marketplaces" / "minni"
    assert _stub_manifest() == {
        "name": "minni",
        "owner": {"name": "Minni"},
        "plugins": [{"name": "minni", "version": "0.4.0", "source": "./plugins/minni-0.4.0"}],
    }
    payload = stub / "plugins" / "minni-0.4.0"
    assert payload.is_dir() and not payload.is_symlink()
    assert (payload / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8") == (
        root / ".claude-plugin" / "plugin.json"
    ).read_text(encoding="utf-8")
    assert _settings(home)["extraKnownMarketplaces"]["minni"]["source"] == {
        "source": "directory", "path": str(stub),
    }
    assert result["settings"]["changed"] is True
    assert result["settings"]["backup"] is None  # nothing existed to back up
    # installPath is not the stub's business: registration keeps the wire tree.
    assert not installed_plugins_path().exists()


def test_marketplace_repairs_a_stale_settings_path_and_keeps_other_keys(home):
    root = _install_tree(home, "0.4.0")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    original = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "enabledPlugins": {"minni@minni": True, "other@elsewhere": True},
        "extraKnownMarketplaces": {
            "minni": {
                "source": {"source": "directory", "path": str(home / "deleted" / "worktree")},
                "autoUpdate": False,
            },
            "other": {"source": {"source": "github", "repo": "example/other"}},
        },
    }
    settings.write_text(json.dumps(original), encoding="utf-8")

    result = ensure_claude_marketplace(root, "0.4.0")

    data = _settings(home)
    assert data["extraKnownMarketplaces"]["minni"] == {
        "source": {"source": "directory", "path": str(local_marketplace_root())},
        "autoUpdate": False,
    }
    assert data["extraKnownMarketplaces"]["other"] == original["extraKnownMarketplaces"]["other"]
    for key in ("model", "permissions", "enabledPlugins"):
        assert data[key] == original[key]
    backup = Path(result["settings"]["backup"])
    assert backup.parent == settings.parent
    assert backup.name.startswith("settings.json.minni-backup-")
    assert json.loads(backup.read_text(encoding="utf-8")) == original


def test_marketplace_concurrent_settings_edit_leaves_no_backup(home, monkeypatch):
    root = _install_tree(home, "0.4.0")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"extraKnownMarketplaces": {"minni": {"source": "old"}}}))
    concurrent_edit = b'{"operator_edit": true}'
    original_read_bytes = Path.read_bytes
    original_write_bytes = Path.write_bytes
    settings_reads = 0

    def read_bytes(path):
        nonlocal settings_reads
        if path == settings:
            settings_reads += 1
            if settings_reads == 2:
                original_write_bytes(path, concurrent_edit)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    with pytest.raises(ClaudePluginError, match="changed while it was being updated"):
        ensure_claude_marketplace(root, "0.4.0")

    assert settings.read_bytes() == concurrent_edit
    assert list(settings.parent.glob("settings.json.minni-backup-*")) == []


def test_marketplace_settings_edit_after_parse_uses_parsed_bytes_as_baseline(home, monkeypatch):
    root = _install_tree(home, "0.4.0")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"extraKnownMarketplaces": {"minni": {"source": "old"}}}))
    concurrent_edit = b'{"operator_edit": true}'
    original_read_bytes = Path.read_bytes
    original_write_bytes = Path.write_bytes
    replaced_after_parse = False

    def read_bytes(path):
        nonlocal replaced_after_parse
        data = original_read_bytes(path)
        if path == settings and not replaced_after_parse:
            replaced_after_parse = True
            original_write_bytes(path, concurrent_edit)
        return data

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    with pytest.raises(ClaudePluginError, match="changed while it was being updated"):
        ensure_claude_marketplace(root, "0.4.0")

    assert replaced_after_parse is True
    assert original_read_bytes(settings) == concurrent_edit
    assert list(settings.parent.glob("settings.json.minni-backup-*")) == []


def test_marketplace_failed_settings_write_leaves_no_backup(home, monkeypatch):
    root = _install_tree(home, "0.4.0")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    original = {"extraKnownMarketplaces": {"minni": {"source": "old"}}}
    settings.write_text(json.dumps(original), encoding="utf-8")
    real_write = claude_plugin._atomic_write_json

    def fail_settings_write(path, data):
        if path == settings:
            raise OSError("simulated write failure")
        real_write(path, data)

    monkeypatch.setattr(claude_plugin, "_atomic_write_json", fail_settings_write)

    with pytest.raises(ClaudePluginError):
        ensure_claude_marketplace(root, "0.4.0")

    assert json.loads(settings.read_text(encoding="utf-8")) == original
    assert list(settings.parent.glob("settings.json.minni-backup-*")) == []


def test_marketplace_rerun_is_idempotent(home):
    root = _install_tree(home, "0.4.0")
    ensure_claude_marketplace(root, "0.4.0")
    settings_before = claude_settings_path().read_bytes()
    manifest = local_marketplace_root() / ".claude-plugin" / "marketplace.json"
    manifest_mtime = manifest.stat().st_mtime_ns

    second = ensure_claude_marketplace(root, "0.4.0")

    assert second["local_marketplace"]["changed"] is False
    assert second["settings"]["changed"] is False
    assert claude_settings_path().read_bytes() == settings_before
    assert manifest.stat().st_mtime_ns == manifest_mtime
    backups = list(claude_settings_path().parent.glob("settings.json.minni-backup-*"))
    assert backups == []


def test_marketplace_new_version_replaces_and_prunes_old_copies(home):
    old = _install_tree(home, "0.4.0")
    ensure_claude_marketplace(old, "0.4.0")
    new = _install_tree(home, "0.4.1")

    result = ensure_claude_marketplace(new, "0.4.1")

    plugins = local_marketplace_root() / "plugins"
    assert sorted(p.name for p in plugins.iterdir()) == ["minni-0.4.1"]
    assert result["local_marketplace"]["pruned"] == [str(plugins / "minni-0.4.0")]
    assert _stub_manifest()["plugins"][0]["source"] == "./plugins/minni-0.4.1"
    # Settings already named the stub; a version bump does not rewrite them.
    assert result["settings"]["changed"] is False


def test_marketplace_refreshes_a_copy_that_drifted_from_the_payload(home):
    root = _install_tree(home, "0.4.0")
    ensure_claude_marketplace(root, "0.4.0")
    copy = local_marketplace_root() / "plugins" / "minni-0.4.0" / "dist" / "server.js"
    copy.write_text("// tampered\n", encoding="utf-8")

    result = ensure_claude_marketplace(root, "0.4.0")

    assert result["local_marketplace"]["copied"] is True
    assert copy.read_text(encoding="utf-8") == "// stub\n"


def test_marketplace_refreshes_a_copy_when_the_source_tree_drifted(home):
    root = _install_tree(home, "0.4.0")
    ensure_claude_marketplace(root, "0.4.0")
    (root / "dist" / "server.js").write_text("// rebuilt\n", encoding="utf-8")

    result = ensure_claude_marketplace(root, "0.4.0")

    copy = local_marketplace_root() / "plugins" / "minni-0.4.0" / "dist" / "server.js"
    assert result["local_marketplace"]["copied"] is True
    assert copy.read_text(encoding="utf-8") == "// rebuilt\n"


def test_marketplace_dry_run_writes_nothing(home):
    root = _install_tree(home, "0.4.0")

    result = ensure_claude_marketplace(root, "0.4.0", dry_run=True)

    assert result["local_marketplace"]["changed"] is True
    assert result["settings"]["changed"] is True
    assert not local_marketplace_root().exists()
    assert not claude_settings_path().exists()


def test_marketplace_scan_of_an_unreadable_plugins_dir_is_a_claude_error(home, monkeypatch):
    root = _install_tree(home, "0.4.0")
    plugins_dir = local_marketplace_root() / "plugins"
    plugins_dir.mkdir(parents=True)
    real_iterdir = Path.iterdir

    def denied(path):
        if path == plugins_dir:
            raise PermissionError(13, "Permission denied")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)

    with pytest.raises(ClaudePluginError, match="cannot inspect local marketplace"):
        ensure_claude_marketplace(root, "0.4.0")


def test_marketplace_rewrites_a_non_utf8_manifest(home):
    root = _install_tree(home, "0.4.0")
    manifest = local_marketplace_root() / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"\xff\xfe\x00 not utf-8")

    result = ensure_claude_marketplace(root, "0.4.0")

    assert result["local_marketplace"]["manifest_written"] is True
    assert _stub_manifest()["plugins"][0]["source"] == "./plugins/minni-0.4.0"


def test_marketplace_refuses_a_symlinked_stub(home, tmp_path):
    root = _install_tree(home, "0.4.0")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    local_marketplace_root().parent.mkdir(parents=True)
    local_marketplace_root().symlink_to(elsewhere)

    with pytest.raises(ClaudePluginError):
        ensure_claude_marketplace(root, "0.4.0")
    assert not any(elsewhere.iterdir())


def test_marketplace_refuses_corrupt_settings(home):
    root = _install_tree(home, "0.4.0")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{not json", encoding="utf-8")

    with pytest.raises(ClaudePluginError):
        ensure_claude_marketplace(root, "0.4.0")
    assert settings.read_text(encoding="utf-8") == "{not json"


def test_marketplace_refuses_non_utf8_settings(home):
    """Invalid UTF-8 is the same refusal as corrupt JSON, not a traceback."""
    root = _install_tree(home, "0.4.0")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_bytes(b'\xff\xfe"broken"')

    with pytest.raises(ClaudePluginError, match="not valid UTF-8"):
        ensure_claude_marketplace(root, "0.4.0")
    assert settings.read_bytes() == b'\xff\xfe"broken"'


def test_adopt_refuses_a_non_utf8_payload_manifest(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    (root / "payload-manifest.json").write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(ClaudePluginError, match="cannot read"):
        adopt_claude_code()


def test_marketplace_writes_through_a_symlinked_settings(home, tmp_path):
    root = _install_tree(home, "0.4.0")
    target = home / "dotfiles" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True)
    settings.symlink_to(target)

    result = ensure_claude_marketplace(root, "0.4.0")

    assert settings.is_symlink()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["extraKnownMarketplaces"]["minni"]["source"] == {
        "source": "directory", "path": str(local_marketplace_root()),
    }
    assert data["model"] == "opus"
    backup = Path(result["settings"]["backup"])
    assert backup.parent == target.parent
    assert json.loads(backup.read_text(encoding="utf-8")) == {"model": "opus"}


def test_marketplace_refuses_a_looping_settings_symlink(home):
    root = _install_tree(home, "0.4.0")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True)
    settings.symlink_to(settings.name)

    with pytest.raises(ClaudePluginError, match="unresolvable symlink"):
        ensure_claude_marketplace(root, "0.4.0")

    assert settings.is_symlink()


def test_marketplace_refuses_a_settings_symlink_outside_home(home, tmp_path):
    root = _install_tree(home, "0.4.0")
    outside = tmp_path / "elsewhere" / "settings.json"
    outside.parent.mkdir(parents=True)
    outside.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    settings = claude_settings_path()
    settings.parent.mkdir(parents=True)
    settings.symlink_to(outside)

    with pytest.raises(ClaudePluginError, match="outside the home directory"):
        ensure_claude_marketplace(root, "0.4.0")

    assert settings.is_symlink()
    assert json.loads(outside.read_text(encoding="utf-8")) == {"model": "opus"}


def test_stub_marketplace_entry_is_not_retired_or_pending(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")
    adopt_claude_code(apply=True)
    # Claude Code records the directory marketplace it resolved.
    marketplaces = known_marketplaces_path()
    marketplaces.write_text(json.dumps({
        "minni": {
            "source": {"source": "directory", "path": str(local_marketplace_root())},
            "installLocation": str(local_marketplace_root()),
        },
    }), encoding="utf-8")

    assert claude_adopt_pending() is False
    assert retire_claude_marketplace()["changed"] is False
    assert "minni" in json.loads(marketplaces.read_text(encoding="utf-8"))


def test_adopt_apply_sets_up_the_stub_marketplace(home):
    root = _install_tree(home, "0.4.0")
    _write_wired(home, root, "0.4.0")

    result = adopt_claude_code(apply=True)

    step = result["steps"]["local_marketplace"]
    assert step["local_marketplace"]["changed"] is True
    assert (local_marketplace_root() / "plugins" / "minni-0.4.0").is_dir()
    assert _settings(home)["extraKnownMarketplaces"]["minni"]["source"]["path"] == str(
        local_marketplace_root()
    )
    assert _registry(home)["plugins"]["minni@minni"][0]["installPath"] == str(root)

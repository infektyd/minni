"""Shared active-wire resolution used by honesty + check_versions + check_deployments.

A half-written install root (dir without payload-manifest) must not be active
for checkers while invisible to deploy honesty (or the reverse).
"""

from __future__ import annotations

import json
from pathlib import Path

from minni.wire.active_roots import (
    active_wire_plugin_roots_ordered,
    active_wire_plugin_state,
)


def _stamp(root: Path, *, version: str = "0.4.1") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "payload-manifest.json").write_text(
        json.dumps({"version": version, "git_sha": "a" * 40}),
        encoding="utf-8",
    )


def test_half_written_root_not_active(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Host config roots must exist or platforms are dropped as retired.
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    (home / ".codex").mkdir()
    plugin = home / ".minni" / "plugin"
    half = plugin / "0.4.1+git.half"
    half.mkdir(parents=True)  # dir only — no payload-manifest
    full = plugin / "0.4.1+git.full"
    _stamp(full)
    (plugin / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [
                {
                    "platform": "claude-code",
                    "install_root": str(half),
                    "wired_at": "2026-08-02T02:00:00Z",
                },
                {
                    "platform": "codex",
                    "install_root": str(full),
                    "wired_at": "2026-08-02T01:00:00Z",
                },
            ],
        }),
        encoding="utf-8",
    )
    roots, platforms = active_wire_plugin_state(home)
    assert full.resolve() in roots
    assert half.resolve() not in roots
    assert platforms == {"codex"}
    ordered = active_wire_plugin_roots_ordered(home)
    assert all(how.startswith("wired.json:") for _, how in ordered)
    assert half.resolve() not in {r for r, _ in ordered}


def test_shared_root_preserves_all_platforms(tmp_path, monkeypatch):
    """wire all shares a payload; cache retirement still needs every host."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".grok").mkdir()
    (home / ".config/kilo").mkdir(parents=True)
    plugin = home / ".minni/plugin"
    root = plugin / "shared"
    _stamp(root)
    expected = {"claude-code", "codex", "grok", "kilocode"}
    (plugin / "wired.json").write_text(json.dumps({
        "wires": [
            {"platform": platform, "install_root": str(root),
             "wired_at": "2026-09-04T00:00:00Z"}
            for platform in sorted(expected)
        ],
    }), encoding="utf-8")
    roots, platforms = active_wire_plugin_state(home)
    assert roots == {root.resolve()}
    assert platforms == expected
    # Status should continue reading/counting the shared payload only once.
    assert active_wire_plugin_roots_ordered(home) == [
        (root.resolve(), "wired.json:claude-code"),
    ]


def test_latest_per_platform_and_current_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    plugin = home / ".minni" / "plugin"
    older = plugin / "0.3.0"
    newer = plugin / "0.4.1"
    _stamp(older, version="0.3.0")
    _stamp(newer, version="0.4.1")
    (plugin / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [
                {
                    "platform": "claude-code",
                    "install_root": str(older),
                    "wired_at": "2026-07-01T00:00:00Z",
                },
                {
                    "platform": "claude-code",
                    "install_root": str(newer),
                    "wired_at": "2026-08-01T00:00:00Z",
                },
            ],
        }),
        encoding="utf-8",
    )
    roots, platforms = active_wire_plugin_state(home)
    assert roots == {newer.resolve()}
    assert platforms == {"claude-code"}

    # No usable wire records → current with manifest.
    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    plugin2 = empty_home / ".minni" / "plugin"
    release = plugin2 / "0.4.1"
    _stamp(release)
    current = plugin2 / "current"
    current.symlink_to(release)
    roots2, platforms2 = active_wire_plugin_state(empty_home)
    assert release.resolve() in roots2
    assert platforms2 == set()
    ordered2 = active_wire_plugin_roots_ordered(empty_home)
    assert ordered2[0][1] == "current"


def test_scripts_import_same_helper(tmp_path, monkeypatch):
    """check_versions / check_deployments must not re-implement is_dir-only logic."""
    import importlib.util
    from pathlib import Path as P

    repo = P(__file__).resolve().parent.parent
    for name, rel in (
        ("check_versions", "scripts/check_versions.py"),
        ("check_deployments", "scripts/check_deployments.py"),
    ):
        path = repo / rel
        spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        home = tmp_path / name
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        (home / ".claude.json").write_text("{}", encoding="utf-8")
        (home / ".codex").mkdir()
        half = home / ".minni" / "plugin" / "half"
        half.mkdir(parents=True)
        full = home / ".minni" / "plugin" / "full"
        _stamp(full)
        (home / ".minni" / "plugin" / "wired.json").write_text(
            json.dumps({
                "schema": 1,
                "wires": [
                    {"platform": "claude-code", "install_root": str(half),
                     "wired_at": "2026-08-02T02:00:00Z"},
                    {"platform": "codex", "install_root": str(full),
                     "wired_at": "2026-08-02T01:00:00Z"},
                ],
            }),
            encoding="utf-8",
        )
        roots, platforms = mod._active_wire_plugin_state(home)
        assert full.resolve() in roots
        assert half.resolve() not in roots
        assert platforms == {"codex"}


def test_fallback_prefers_newer_version_dir_over_lagging_current(tmp_path, monkeypatch):
    """Pre-wire (no wired.json): lagging current must not hide a fresher +git dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    plugin = home / ".minni" / "plugin"
    old = plugin / "0.3.0"
    _stamp(old, version="0.3.0")
    (plugin / "current").symlink_to(old)
    import time
    time.sleep(0.02)
    fresh = plugin / "0.4.1+git.deadbeef"
    _stamp(fresh, version="0.4.1+git.deadbeef")
    # No wired.json: pre-wire / release-era layout only.
    ordered = active_wire_plugin_roots_ordered(home)
    assert ordered, ordered
    root, how = ordered[0]
    assert root == fresh.resolve(), (root, how, ordered)
    assert how == "version-dir scan"


def test_empty_wires_after_retire_does_not_reanimate_version_dirs(
    tmp_path, monkeypatch,
):
    """Parsed wires:[] (retirement) must not promote historical plugin dirs.

    Re-animating abandoned trees after the last wire host is gone bricks
    make sync-root on propagate-only machines (cursor/antigravity leftovers).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    plugin = home / ".minni" / "plugin"
    old = plugin / "0.4.0"
    _stamp(old, version="0.4.0")
    (plugin / "current").symlink_to(old)
    (plugin / "wired.json").write_text(
        json.dumps({"schema": 1, "wires": []}), encoding="utf-8",
    )
    ordered = active_wire_plugin_roots_ordered(home)
    assert ordered == [], ordered
    roots, platforms = active_wire_plugin_state(home)
    assert roots == set()
    assert platforms == set()


def test_zombie_platform_does_not_fallback_to_version_dir(tmp_path, monkeypatch):
    """Host root gone + retired filter must yield empty, not version-dir scan."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # No ~/.codex — platform is zombie.
    plugin = home / ".minni" / "plugin"
    ver = plugin / "0.4.0"
    _stamp(ver, version="0.4.0")
    (plugin / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [{
                "platform": "codex",
                "install_root": str(ver),
                "wired_at": "2020-01-01T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )
    ordered = active_wire_plugin_roots_ordered(home)
    assert ordered == [], ordered

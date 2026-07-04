"""Unit tests for minni.wire.gc reference-aware pruning."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from minni.wire.gc import run_gc
from minni.wire.manifest import PayloadManifest, sha256_file


def _seed_manifest(version_dir: Path, version: str) -> None:
    server = version_dir / "dist" / "server.js"
    server.parent.mkdir(parents=True, exist_ok=True)
    server.write_text("// stub\n", encoding="utf-8")
    files = {"dist/server.js": sha256_file(server)}
    manifest = PayloadManifest(
        schema=1,
        version=version,
        git_sha="abc",
        built_at="2026-07-04T00:00:00Z",
        node_engine=">=20",
        files=files,
    )
    (version_dir / "payload-manifest.json").write_text(
        json.dumps({
            "schema": 1,
            "version": version,
            "git_sha": "abc",
            "built_at": "2026-07-04T00:00:00Z",
            "node_engine": ">=20",
            "files": files,
        }),
        encoding="utf-8",
    )


@pytest.fixture
def gc_base(tmp_path, monkeypatch):
    base = tmp_path / "plugin"
    base.mkdir()
    monkeypatch.setattr("minni.wire.gc.plugin_base", lambda: base)
    monkeypatch.setattr("minni.wire.wired.plugin_base", lambda: base)
    return base


def test_gc_prune_stale_version_dir(gc_base):
    stale = gc_base / "0.1.0+git.abc"
    _seed_manifest(stale, "0.1.0+git.abc")
    result = run_gc(prune=True, stdin_is_tty=False)
    assert str(stale) in result.pruned
    assert not stale.exists()


def test_gc_top_two_release_retention(gc_base):
    for ver in ("0.1.0", "0.2.0", "0.3.0"):
        _seed_manifest(gc_base / ver, ver)
    result = run_gc(prune=True, stdin_is_tty=False)
    assert (gc_base / "0.3.0").exists()
    assert (gc_base / "0.2.0").exists()
    assert not (gc_base / "0.1.0").exists()
    assert str(gc_base / "0.1.0") in result.pruned


def test_gc_retains_config_referenced_version(gc_base, monkeypatch, tmp_path):
    old = gc_base / "0.1.0"
    _seed_manifest(old, "0.1.0")
    for ver in ("0.2.0", "0.3.0"):
        _seed_manifest(gc_base / ver, ver)
    config = tmp_path / "config.toml"
    config.write_text(f'server = "{old / "dist" / "server.js"}"\n', encoding="utf-8")
    monkeypatch.setattr(
        "minni.wire.gc.default_config_scan_paths",
        lambda: {"codex": config},
    )
    result = run_gc(prune=True, stdin_is_tty=False)
    assert old.exists()
    assert str(old) not in result.pruned


def test_gc_prune_true_force_without_tty(gc_base):
    stale = gc_base / "0.0.1+git.dead"
    _seed_manifest(stale, "0.0.1+git.dead")
    result = run_gc(prune=True, stdin_is_tty=False)
    assert not stale.exists()
    assert str(stale) in result.pruned


def test_gc_prune_false_skips_even_on_tty(gc_base, monkeypatch):
    stale = gc_base / "0.0.2+git.beef"
    _seed_manifest(stale, "0.0.2+git.beef")
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    result = run_gc(prune=False, stdin_is_tty=True)
    assert stale.exists()
    assert result.pruned == []


def test_gc_prune_none_tty_prompt_no(monkeypatch, gc_base):
    stale = gc_base / "0.0.3+git.cafe"
    _seed_manifest(stale, "0.0.3+git.cafe")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    result = run_gc(prune=None, stdin_is_tty=True)
    assert stale.exists()
    assert result.pruned == []


def test_gc_prune_none_tty_prompt_yes(monkeypatch, gc_base):
    stale = gc_base / "0.0.4+git.babe"
    _seed_manifest(stale, "0.0.4+git.babe")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    result = run_gc(prune=None, stdin_is_tty=True)
    assert not stale.exists()
    assert str(stale) in result.pruned


def test_gc_dry_run_no_prompt(monkeypatch, gc_base):
    stale = gc_base / "0.0.5+git.face"
    _seed_manifest(stale, "0.0.5+git.face")
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    result = run_gc(prune=None, stdin_is_tty=True, dry_run=True)
    assert stale.exists()
    assert str(stale) in result.would_prune
    assert result.pruned == []


def test_gc_cleans_quarantine_dirs(gc_base):
    quarantine = gc_base / ".quarantine-0.2.0-20260704T000000Z"
    quarantine.mkdir()
    (quarantine / "leftover.txt").write_text("x", encoding="utf-8")
    result = run_gc(prune=True, stdin_is_tty=False)
    assert not quarantine.exists()
    assert str(quarantine) in result.pruned
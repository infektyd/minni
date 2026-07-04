"""Regression tests for review findings that survived the convergence rounds.

Covers: release-only `current` symlink gate, the rename-race recovery branch in
_install_locked, manifest missing-file detection, TOML control-character
escaping, GC config-scan protection for dev/local version dirs, and wired.json
flock behavior under real concurrent writers.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import tomllib
from pathlib import Path

import pytest

from minni.wire.gc import run_gc
from minni.wire.install import HashMismatchError, install_payload
from minni.wire.manifest import (
    PayloadManifest,
    is_local_version,
    sha256_file,
    verify_manifest_hashes,
)
from minni.wire.wired import make_record, upsert_wire
from minni.wire.writers import _toml_basic_str


def _payload(tmp_path: Path, version: str) -> tuple[Path, PayloadManifest]:
    src = tmp_path / f"payload-{version}"
    dist = src / "dist"
    dist.mkdir(parents=True)
    server = dist / "server.js"
    server.write_text(f"// {version}\n", encoding="utf-8")
    files = {"dist/server.js": sha256_file(server)}
    manifest = PayloadManifest(
        schema=1,
        version=version,
        git_sha="abc",
        built_at="2026-07-04T00:00:00Z",
        node_engine=">=20",
        files=files,
    )
    (src / "payload-manifest.json").write_text(
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
    return src, manifest


@pytest.fixture
def home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


# --- release-only `current` symlink gate ---


def test_is_local_version():
    assert is_local_version("0.3.0+git.abc1234")
    assert is_local_version("0.3.0+git.abc1234.dirty")
    assert not is_local_version("0.3.0")


def test_current_symlink_created_for_release(home, tmp_path):
    src, manifest = _payload(tmp_path, "0.3.0")
    install_payload(src, "0.3.0", manifest)
    current = home / ".minni" / "plugin" / "current"
    assert current.is_symlink()
    assert os.readlink(current) == "0.3.0"


def test_current_symlink_never_moved_by_dev_build(home, tmp_path):
    release_src, release_manifest = _payload(tmp_path, "0.3.0")
    install_payload(release_src, "0.3.0", release_manifest)
    dev = "0.3.0+git.abc1234"
    dev_src, dev_manifest = _payload(tmp_path, dev)
    install_payload(dev_src, dev, dev_manifest)
    current = home / ".minni" / "plugin" / "current"
    # Dev build installed, but current still points at the release.
    assert (home / ".minni" / "plugin" / dev).is_dir()
    assert os.readlink(current) == "0.3.0"


def test_current_symlink_absent_when_only_dev_installed(home, tmp_path):
    dev = "0.1.0+git.dead.dirty"
    src, manifest = _payload(tmp_path, dev)
    install_payload(src, dev, manifest)
    assert not (home / ".minni" / "plugin" / "current").exists()


# --- rename-race recovery branch (§4.4 step 4 belt-and-braces) ---


def _racy_rename(create_target_from: str, monkeypatch):
    """Make the first rename(stage, target) simulate losing the race."""
    real_rename = os.rename
    fired = {"done": False}

    def fake_rename(src, dst):
        if not fired["done"] and ".staging-" in str(src):
            fired["done"] = True
            # The "other process" wins: target appears with its own content.
            import shutil

            shutil.copytree(create_target_from, dst)
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(dst))
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", fake_rename)


def test_rename_race_matching_hashes_idempotent_skip(home, tmp_path, monkeypatch):
    src, manifest = _payload(tmp_path, "0.3.0")
    _racy_rename(str(src), monkeypatch)
    result = install_payload(src, "0.3.0", manifest)
    assert result.skipped
    target = home / ".minni" / "plugin" / "0.3.0"
    assert target.is_dir()
    # Staging dir cleaned up.
    leftovers = [
        p for p in target.parent.iterdir() if p.name.startswith(".staging-")
    ]
    assert leftovers == []


def test_rename_race_mismatched_hashes_hard_error(home, tmp_path, monkeypatch):
    src, manifest = _payload(tmp_path, "0.3.0")
    other_src, _ = _payload(tmp_path, "0.3.0-other")
    # The winner installed different content under the same version dir.
    _racy_rename(str(other_src), monkeypatch)
    with pytest.raises(HashMismatchError):
        install_payload(src, "0.3.0", manifest)


# --- manifest missing-file branch ---


def test_verify_manifest_hashes_missing_file(tmp_path):
    src, manifest = _payload(tmp_path, "0.3.0")
    (src / "dist" / "server.js").unlink()
    errors = verify_manifest_hashes(manifest, src)
    assert any("missing file: dist/server.js" in e for e in errors)


# --- TOML control-character escaping ---


@pytest.mark.parametrize(
    "raw",
    [
        'plain"quote\\back',
        "line1\nline2",
        "tab\there",
        "cr\rhere",
        "bell\x07null\x00",
        'break"\n[mcp_servers.evil]\ncmd = "x',
    ],
)
def test_toml_basic_str_round_trips(raw):
    doc = f'value = "{_toml_basic_str(raw)}"\n'
    parsed = tomllib.loads(doc)
    assert parsed == {"value": raw}


# --- GC config-scan protection covers dev/local dirs ---


def test_gc_config_scan_protects_dev_dir(home, tmp_path, monkeypatch):
    base = home / ".minni" / "plugin"
    dev = base / "0.1.0+git.abc"
    src, manifest = _payload(tmp_path, "0.1.0+git.abc")
    install_payload(src, "0.1.0+git.abc", manifest)
    assert dev.is_dir()
    # Referenced by a stamped config, but absent from wired.json.
    config = tmp_path / "config.toml"
    config.write_text(
        f'args = ["{dev / "dist" / "server.js"}"]\n', encoding="utf-8",
    )
    monkeypatch.setattr(
        "minni.wire.gc.default_config_scan_paths", lambda: {"codex": config},
    )
    result = run_gc(prune=True, stdin_is_tty=False)
    assert dev.exists()
    assert str(dev) not in result.pruned


# --- wired.json flock under real concurrent writers ---


def test_wired_upsert_concurrent_writers_no_lost_update(home):
    base = home / ".minni" / "plugin"
    n = 8
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            record = make_record(
                platform=f"platform-{idx}",
                config_path=Path(f"~/.config-{idx}.json"),
                install_root=base / "0.3.0",
                version="0.3.0",
                workspace=None,
            )
            upsert_wire(record)
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    data = json.loads((base / "wired.json").read_text(encoding="utf-8"))
    platforms = {w["platform"] for w in data["wires"]}
    assert platforms == {f"platform-{i}" for i in range(n)}
    assert data["generation"] == n

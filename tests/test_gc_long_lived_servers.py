"""Regression tests for §4.5 (P1-3, N4, N8): Retention & Safe GC vs Long-Lived Servers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from minni.wire.manifest import PayloadManifest, sha256_file

REPO_ROOT = Path(__file__).resolve().parent.parent


def _seed_version(
    plugin_dir: Path,
    version: str,
    built_at: str,
    git_sha: str,
    *,
    copy_real_server: bool = False,
) -> Path:
    vdir = plugin_dir / version
    dist = vdir / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    server = dist / "server.js"
    real_dist = REPO_ROOT / "plugins" / "minni" / "dist"
    if copy_real_server and (real_dist / "server.js").is_file():
        shutil.copytree(real_dist, dist, dirs_exist_ok=True)
        nm_src = REPO_ROOT / "plugins" / "minni" / "node_modules"
        if nm_src.exists() and not (vdir / "node_modules").exists():
            os.symlink(str(nm_src.resolve()), vdir / "node_modules")
        pkg_src = REPO_ROOT / "plugins" / "minni" / "package.json"
        if pkg_src.is_file():
            shutil.copy2(pkg_src, vdir / "package.json")
    else:
        server.write_text("// stub server\n", encoding="utf-8")

    files = {"dist/server.js": sha256_file(server)}
    manifest = PayloadManifest(
        schema=1,
        version=version,
        git_sha=git_sha,
        built_at=built_at,
        node_engine=">=20",
        files=files,
    )
    (vdir / "payload-manifest.json").write_text(
        json.dumps({
            "schema": manifest.schema,
            "version": manifest.version,
            "git_sha": manifest.git_sha,
            "built_at": manifest.built_at,
            "node_engine": manifest.node_engine,
            "files": manifest.files,
        }) + "\n",
        encoding="utf-8",
    )
    return vdir


def test_gc_protects_running_server_via_open_fd_and_bounded_lstart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.5 & §6.2 (P1-3, N4, N8): Keep open FD + lease across 3+ flips, and bounded lstart for legacy PIDs."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    from minni.wire.containers import flip_current_pointer
    from minni.wire.gc import (
        bounded_lstart_protected_versions,
        collect_garbage,
        inspect_running_mcp_servers,
    )

    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)

    # Verify server.ts source has no fs.flock or flockSync (N4) and has __minniSelfPinFd
    server_ts = (REPO_ROOT / "plugins" / "minni" / "src" / "server.ts").read_text(encoding="utf-8")
    assert "flock" not in server_ts
    assert "__minniSelfPinFd" in server_ts

    v1 = _seed_version(
        plugin_dir,
        "0.6.0+git.11111111",
        "2026-10-31T10:00:00Z",
        "11111111",
        copy_real_server=True,
    )
    flip_current_pointer("0.6.0+git.11111111", home=home)

    # Spawn a real server.js via current/dist/server.js
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["MINNI_AGENT_ID"] = "claude-code"
    env["MINNI_VAULT_PATH"] = str(home / ".minni" / "claudecode-vault")
    proc = subprocess.Popen(
        ["node", str(plugin_dir / "current" / "dist" / "server.js")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(home),
    )
    try:
        lease_file = home / ".minni" / "run" / "mcp-servers" / f"{proc.pid}.json"
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not lease_file.is_file():
            if proc.poll() is not None:
                stderr_out = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                pytest.fail(f"server.js exited early ({proc.returncode}): {stderr_out}")
            time.sleep(0.05)
        assert lease_file.is_file(), "server.js must write ~/.minni/run/mcp-servers/<pid>.json lease"

        # Flip current through v2, v3, v4 with keep_last=2
        _seed_version(plugin_dir, "0.6.0+git.22222222", "2026-10-31T11:00:00Z", "22222222")
        flip_current_pointer("0.6.0+git.22222222", home=home)
        _seed_version(plugin_dir, "0.6.0+git.33333333", "2026-10-31T12:00:00Z", "33333333")
        flip_current_pointer("0.6.0+git.33333333", home=home)
        _seed_version(plugin_dir, "0.6.0+git.44444444", "2026-10-31T13:00:00Z", "44444444")
        flip_current_pointer("0.6.0+git.44444444", home=home)

        gc_res = collect_garbage(home=home, keep_last=2, prune=True)
        # v1 is protected by open FD + lease; v3 and v4 are protected by keep_last=2 + current; v2 is pruned!
        assert v1.is_dir(), "Running v1 server directory must be protected from GC"
        assert (plugin_dir / "0.6.0+git.33333333").is_dir()
        assert (plugin_dir / "0.6.0+git.44444444").is_dir()
        assert not (plugin_dir / "0.6.0+git.22222222").exists()
        assert str(plugin_dir / "0.6.0+git.22222222") in gc_res.pruned

        # Inspect running servers report
        running = inspect_running_mcp_servers(
            target_sha="44444444",
            target_version="0.6.0+git.44444444",
            home=home,
        )
        matching = [r for r in running if r["pid"] == proc.pid]
        assert len(matching) == 1
        assert matching[0]["stale"] is True
        assert matching[0]["backing_dir_exists"] is True
        assert matching[0]["loaded_sha"] == "11111111"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    # Part 2: Bounded per-PID launch-window match for legacy unpinned PIDs (N8)
    home2 = tmp_path / "home2"
    plugin2 = home2 / ".minni" / "plugin"
    plugin2.mkdir(parents=True)
    _seed_version(plugin2, "0.6.0+git.v0", "2026-10-31T08:00:00Z", "0000")
    _seed_version(plugin2, "0.6.0+git.v1", "2026-10-31T09:00:00Z", "1111")
    _seed_version(plugin2, "0.6.0+git.v2", "2026-10-31T11:57:00Z", "2222")
    _seed_version(plugin2, "0.6.0+git.v3", "2026-10-31T12:00:00Z", "3333")
    _seed_version(plugin2, "0.6.0+git.v4", "2026-10-31T13:00:00Z", "4444")
    flip_current_pointer("0.6.0+git.v4", home=home2)

    # Simulate a legacy unpinned PID started at 2026-10-31T12:00:10Z (10s after v3, 3m10s after v2)
    lstart_protected = bounded_lstart_protected_versions(
        plugin2,
        unpinned_lstarts=[1793448010.0],  # 2026-10-31T12:00:10Z
    )
    assert lstart_protected == {"0.6.0+git.v3", "0.6.0+git.v2"}
    assert "0.6.0+git.v0" not in lstart_protected
    assert "0.6.0+git.v1" not in lstart_protected

    gc_res2 = collect_garbage(
        home=home2,
        keep_last=2,
        prune=True,
        legacy_unpinned_lstarts=[1793448010.0],
    )
    assert (plugin2 / "0.6.0+git.v4").is_dir()
    assert (plugin2 / "0.6.0+git.v3").is_dir()
    assert (plugin2 / "0.6.0+git.v2").is_dir()
    assert not (plugin2 / "0.6.0+git.v0").exists()
    assert not (plugin2 / "0.6.0+git.v1").exists()
    assert str(plugin2 / "0.6.0+git.v0") in gc_res2.pruned
    assert str(plugin2 / "0.6.0+git.v1") in gc_res2.pruned

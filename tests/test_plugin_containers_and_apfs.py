"""Regression tests for §4.1: Stable Launch Pointer (~/.minni/plugin/current) & Real Per-Host Plugin Containers."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from minni.wire.manifest import PayloadManifest, sha256_file

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_propagate():
    spec = importlib.util.spec_from_file_location(
        "_propagate_4_1",
        REPO_ROOT / "plugins" / "minni" / "skills" / "minni-install" / "scripts" / "propagate.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_payload_src(tmp_path: Path, version: str = "0.6.0+git.af0c04f7") -> tuple[Path, PayloadManifest]:
    src = tmp_path / "payload_src"
    (src / "dist").mkdir(parents=True)
    (src / "skills").mkdir()
    (src / "commands").mkdir()
    (src / "hooks").mkdir()
    (src / ".claude-plugin").mkdir()
    server = src / "dist" / "server.js"
    server.write_text("// server ok\n", encoding="utf-8")
    (src / "skills" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (src / "commands" / "status.toml").write_text("prompt = 'status'\n", encoding="utf-8")
    (src / "hooks" / "hooks.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    mcp = src / ".mcp.json"
    mcp.write_text('{"mcpServers": {"minni": {"env": {"MINNI_AGENT_ID": "grok-build"}}}}\n', encoding="utf-8")
    cplugin = src / ".claude-plugin" / "plugin.json"
    cplugin.write_text(
        json.dumps({"name": "minni", "version": version, "mcpServers": {"minni": {"command": "node"}}}) + "\n",
        encoding="utf-8",
    )
    files = {
        "dist/server.js": sha256_file(server),
        ".mcp.json": sha256_file(mcp),
        ".claude-plugin/plugin.json": sha256_file(cplugin),
    }
    manifest = PayloadManifest(
        schema=1,
        version=version,
        git_sha="af0c04f711111111111111111111111111111111",
        built_at="2026-10-31T00:00:00Z",
        node_engine=">=20",
        files=files,
    )
    (src / "payload-manifest.json").write_text(
        json.dumps({
            "schema": 1,
            "version": version,
            "git_sha": manifest.git_sha,
            "built_at": manifest.built_at,
            "node_engine": manifest.node_engine,
            "files": files,
        }) + "\n",
        encoding="utf-8",
    )
    return src, manifest


def test_containers_use_expanded_paths_scratch_probe_ui_gate_and_rename_aside_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.1 & §6.2 (P1-1, P1-2, N2, N3, N7, R4-1, R4-3): Staged payload, containers, rename-aside swap, APFS samestat."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    from minni.wire.containers import (
        archive_container_prev_subtrees,
        converge_host_container,
        flip_current_pointer,
        resolve_antigravity_container_dir,
        resolve_verified_plugin_root,
        rollback_container_subtrees,
        rollback_current_pointer,
        stage_scratch_containers,
    )
    from minni.wire.install import install_plugin_payload

    # 1. Verify install_plugin_payload omits .mcp.json and strips mcpServers in staged copy ONLY (N2)
    src, manifest = _make_payload_src(tmp_path)
    res = install_plugin_payload(src, manifest.version, manifest, update_current=False)
    staged_root = res.install_root
    assert (staged_root / ".mcp.json").exists() is False
    staged_cplugin = json.loads((staged_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "mcpServers" not in staged_cplugin
    # Repo source files remain untouched with mcpServers intact
    repo_mcp = json.loads((REPO_ROOT / "plugins" / "minni" / ".mcp.json").read_text(encoding="utf-8"))
    repo_cplugin = json.loads(
        (REPO_ROOT / "plugins" / "minni" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert "mcpServers" in repo_mcp
    assert "mcpServers" in repo_cplugin

    # 2. Pre-existing real directory at ~/.minni/plugin/current converted via rename-aside and rolled back via unlink+rename (R4-1)
    current_path = home / ".minni" / "plugin" / "current"
    current_path.mkdir(parents=True)
    (current_path / "legacy.txt").write_text("legacy-real-dir\n", encoding="utf-8")

    flip_state = flip_current_pointer(manifest.version, home=home)
    assert current_path.is_symlink()
    assert flip_state.prev_dir_path is not None and flip_state.prev_dir_path.is_dir()
    # Rollback restores real directory without ENOTDIR
    rollback_current_pointer(flip_state)
    assert not current_path.is_symlink() and current_path.is_dir()
    assert (current_path / "legacy.txt").read_text(encoding="utf-8") == "legacy-real-dir\n"

    # Flip again for container tests and verify resolve_verified_plugin_root (§4.1 #6)
    flip_state2 = flip_current_pointer(manifest.version, home=home)
    verified_root = resolve_verified_plugin_root(current_path, expected_base=home / ".minni" / "plugin")
    assert verified_root == staged_root.resolve()

    # 3. Scratch containers before touching live containers (N3)
    scratch_root, scratch_containers = stage_scratch_containers(
        staged_root, home=home, base_semver="0.6.0",
        container_modes={"antigravity": "symlink_subtrees", "cursor": "staged_copy"},
    )
    assert (scratch_containers["antigravity"] / "dist").is_symlink()
    assert not (scratch_containers["cursor"] / "dist").is_symlink()
    assert (scratch_containers["cursor"] / "dist" / "server.js").is_file()

    # 4. Converge real per-host containers via rename-aside -> symlink / staged_copy (P1-1, N3, N7, R4-3)
    ag_dir = home / ".agents" / "plugins" / "Minni@Minni"
    (ag_dir / "dist").mkdir(parents=True)
    (ag_dir / "dist" / "old.js").write_text("old\n", encoding="utf-8")

    ag_swaps = converge_host_container(
        "antigravity",
        home=home,
        base_semver="0.6.0",
        container_mode="symlink_subtrees",
        target_version_root=staged_root,
    )
    ag_mcp_raw = (ag_dir / ".mcp.json").read_text(encoding="utf-8")
    ag_ext_raw = (ag_dir / "gemini-extension.json").read_text(encoding="utf-8")
    assert "~" not in ag_mcp_raw and "+git." not in ag_mcp_raw
    assert "~" not in ag_ext_raw and "+git." not in ag_ext_raw
    ag_mcp = json.loads(ag_mcp_raw)
    assert ag_mcp["mcpServers"]["minni"]["env"]["MINNI_AGENT_ID"] == "gemini"
    assert "MINNI_WORKSPACE_ID" not in ag_mcp["mcpServers"]["minni"]["env"]
    assert "cwd" not in ag_mcp["mcpServers"]["minni"]
    assert (ag_dir / "dist").is_symlink()
    # Prior real dist directory is held as dist.prev.<pid> until post-flip verification passes (N3)
    assert any(s.prev_dir is not None and s.prev_dir.is_dir() for s in ag_swaps)

    # Test rollback of container subtrees restores real dist/
    rollback_container_subtrees(ag_swaps)
    assert not (ag_dir / "dist").is_symlink() and (ag_dir / "dist" / "old.js").is_file()

    # Re-converge and archive prev subtrees
    ag_swaps2 = converge_host_container(
        "antigravity",
        home=home,
        base_semver="0.6.0",
        container_mode="symlink_subtrees",
        target_version_root=staged_root,
    )
    archive_dir = home / ".minni" / "archive" / "test-ts"
    archive_container_prev_subtrees(ag_swaps2, archive_dir)
    assert (archive_dir / "Minni@Minni" / "dist" / "old.js").is_file()

    # Cursor container in staged_copy fallback mode (R4-3)
    cu_dir = home / ".cursor" / "plugins" / "local" / "minni"
    (cu_dir / "dist").mkdir(parents=True)
    cu_swaps = converge_host_container(
        "cursor",
        home=home,
        base_semver="0.6.0",
        container_mode="staged_copy",
        target_version_root=staged_root,
    )
    cu_mcp_raw = (cu_dir / ".mcp.json").read_text(encoding="utf-8")
    assert "~" not in cu_mcp_raw and "+git." not in cu_mcp_raw
    cu_mcp = json.loads(cu_mcp_raw)
    assert cu_mcp["mcpServers"]["minni"]["env"]["MINNI_AGENT_ID"] == "cursor"
    assert not (cu_dir / "dist").is_symlink() and (cu_dir / "dist" / "server.js").is_file()
    archive_container_prev_subtrees(cu_swaps, archive_dir)

    # 5. APFS case-insensitivity samestat check (P1-2)
    resolved_dir, alias_created = resolve_antigravity_container_dir(home)
    lower_dir = home / ".agents" / "plugins" / "minni@minni"
    if lower_dir.exists() and os.path.samestat(os.stat(ag_dir), os.stat(lower_dir)):
        assert alias_created is False


def test_strict_symlink_validation_and_propagate_d3_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.1 #6 (D3): Dangling current symlink is rejected and propagate.py does not hex-sort +git.<sha>."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    from minni.wire.containers import resolve_verified_plugin_root

    base = home / ".minni" / "plugin"
    base.mkdir(parents=True)
    dangling = base / "current"
    os.symlink("0.6.0+git.deadbeef", dangling)

    with pytest.raises(ValueError, match="dangling|does not exist"):
        resolve_verified_plugin_root(dangling, expected_base=base)

    propagate = _load_propagate()
    assert not hasattr(propagate, "max_present_version"), "max_present_version hex-SHA sort must be deleted (§4.1 #6)"

"""Unit tests for manifest helpers used by --from-repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minni.wire.from_repo import self_check_manifest
from minni.wire.manifest import PayloadManifest, dev_version, sha256_file, verify_manifest_hashes


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_dev_version_clean_suffix(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('version = "0.3.0"\n', encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "init")

    version = dev_version(repo)
    short = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=repo, text=True,
    ).strip()
    assert version == f"0.3.0+git.{short}"
    assert not version.endswith(".dirty")


def test_dev_version_dirty_suffix(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('version = "0.3.0"\n', encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "init")
    (repo / "dirty.txt").write_text("x\n", encoding="utf-8")

    version = dev_version(repo)
    assert version.endswith(".dirty")


def _manifest(**kwargs) -> PayloadManifest:
    defaults = {
        "schema": 1,
        "version": "0.3.0",
        "git_sha": "abc",
        "built_at": "2026-07-04T00:00:00Z",
        "node_engine": ">=20",
        "files": {"dist/server.js": "sha256:deadbeef"},
    }
    defaults.update(kwargs)
    return PayloadManifest(**defaults)


def test_self_check_manifest_schema_mismatch():
    with pytest.raises(ValueError, match="schema mismatch"):
        self_check_manifest(_manifest(schema=2))


def test_self_check_manifest_empty_files():
    with pytest.raises(ValueError, match="missing version or files"):
        self_check_manifest(_manifest(files={}))


def test_self_check_manifest_empty_version():
    with pytest.raises(ValueError, match="missing version or files"):
        self_check_manifest(_manifest(version=""))


def test_self_check_manifest_valid():
    self_check_manifest(_manifest())


def _build_manifest_tree(tmp_path: Path) -> tuple[Path, PayloadManifest]:
    root = tmp_path / "payload"
    dist = root / "dist"
    dist.mkdir(parents=True)
    server = dist / "server.js"
    server.write_text("ok\n", encoding="utf-8")
    files = {
        "dist/server.js": sha256_file(server),
    }
    manifest = PayloadManifest(
        schema=1,
        version="0.2.0",
        git_sha="abc",
        built_at="2026-07-04T00:00:00Z",
        node_engine=">=20",
        files=files,
    )
    return root, manifest


def test_verify_manifest_hashes_success(tmp_path):
    root, manifest = _build_manifest_tree(tmp_path)
    assert verify_manifest_hashes(manifest, root) == []


def test_verify_manifest_hashes_mismatch(tmp_path):
    root, manifest = _build_manifest_tree(tmp_path)
    (root / "dist" / "server.js").write_text("tampered\n", encoding="utf-8")
    errors = verify_manifest_hashes(manifest, root)
    assert any("hash mismatch: dist/server.js" in e for e in errors)


def test_staged_payload_omits_mcp_json_and_strips_claude_plugin_mcp_servers_while_preserving_repo_source(
    tmp_path, monkeypatch,
):
    import json
    from minni.wire.install import install_payload

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    root, manifest = _build_manifest_tree(tmp_path)
    (root / ".mcp.json").write_text('{"mcpServers": {"minni": {"command": "node"}}}\n', encoding="utf-8")
    cplugin_dir = root / ".claude-plugin"
    cplugin_dir.mkdir(parents=True, exist_ok=True)
    (cplugin_dir / "plugin.json").write_text(
        json.dumps({"name": "minni", "version": "0.2.0", "mcpServers": {"minni": {"command": "node"}}}) + "\n",
        encoding="utf-8",
    )

    res = install_payload(root, "0.2.0", manifest, update_current=False)
    staged_root = res.install_root
    assert (staged_root / ".mcp.json").exists() is False
    assert "mcpServers" not in json.loads(
        (staged_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    repo_root = Path(__file__).resolve().parent.parent
    assert "mcpServers" in json.loads(
        (repo_root / "plugins" / "minni" / ".mcp.json").read_text(encoding="utf-8")
    )
    assert "mcpServers" in json.loads(
        (repo_root / "plugins" / "minni" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
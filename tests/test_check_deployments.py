"""Audit R1: check_deployments must see the drift a symlinked dist/ hides.

The regression pinned here is structural, not cosmetic. Every deployment on a
real machine symlinks dist/ at the source dist and copies everything else. The
old check saw the symlink, concluded the deployment "cannot drift by
construction", and skipped it -- while four of those same deployments were
running hooks.json files that differed from source.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_deployments.py"
SOURCE = REPO / "plugins" / "minni"


def _isolated_repo_root(tmp_path: Path) -> Path:
    """A repo root whose plugins/ is the real one but whose src/ is empty.

    Keeps the in-repo payload deployment (src/minni/plugin_payload/dist, present
    only after `make stage-payload`) out of these fixtures, so the tests measure
    the fake $HOME and nothing else.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "plugins").symlink_to(REPO / "plugins")
    return root


def _run(*args, home: Path, repo_root: Path):
    env = {
        **os.environ,
        "MINNI_CHECK_DEPLOYMENTS_HOME": str(home),
        "MINNI_CHECK_DEPLOYMENTS_REPO_ROOT": str(repo_root),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )


def _deployment(home: Path, *, link_dist_to: Path) -> Path:
    """A deployment shaped like the real ones: symlinked dist/, copied rest."""
    root = home / ".config" / "kilo" / "plugins" / "minni"
    root.mkdir(parents=True)
    (root / "dist").symlink_to(link_dist_to)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, root / sub)
    return root


def _copytree(src: Path, dst: Path) -> None:
    import shutil

    shutil.copytree(src, dst)


def test_faithful_copy_behind_a_symlinked_dist_is_clean(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _deployment(home, link_dist_to=SOURCE / "dist")
    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 0, proc.stdout
    assert "LINKED" in proc.stdout
    assert "DRIFT" not in proc.stdout


def test_hooks_drift_behind_a_symlinked_dist_is_reported(tmp_path):
    """The measured 2026-08-01 condition: dist/ linked, hooks.json stale."""
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    hooks = root / "hooks" / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"stale": True}}), encoding="utf-8")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    # LINKED is still stated -- it is a true fact about dist/ -- but it no
    # longer decides the deployment's verdict.
    assert "LINKED" in proc.stdout
    assert "DRIFT" in proc.stdout
    assert "hooks/hooks.json" in proc.stdout


def test_missing_manifest_file_is_reported_as_missing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    (root / ".claude-plugin" / "plugin.json").unlink()

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert ".claude-plugin/plugin.json (missing)" in proc.stdout


def test_manifest_version_alone_is_not_content_drift(tmp_path):
    """Version is check_versions' job. A wire-installed tree stamps its own
    PEP440-local version, and reporting that here would bury real drift."""
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    manifest = root / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = "0.4.0+git.deadbee"
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 0, proc.stdout
    assert "DRIFT" not in proc.stdout


def test_manifest_content_drift_is_still_caught(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    manifest = root / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["description"] = "an older description"
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert ".claude-plugin/plugin.json" in proc.stdout


def test_cursor_local_tree_is_discovered(tmp_path):
    """~/.cursor/plugins/local/minni is a real copy with no build-manifest. It
    was not in the discovery globs at all, so it reported nothing, ever."""
    home = tmp_path / "home"
    root = home / ".cursor" / "plugins" / "local" / "minni"
    (root / "dist").mkdir(parents=True)
    _copytree(SOURCE / "hooks", root / "hooks")
    (root / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert ".cursor/plugins/local/minni" in proc.stdout
    assert "UNKNOWN" in proc.stdout  # no build-manifest: vintage unestablished
    assert "hooks/hooks.json" in proc.stdout


def test_unreadable_file_is_reported_not_assumed_to_match(tmp_path):
    if os.geteuid() == 0:  # pragma: no cover - root ignores mode bits
        import pytest

        pytest.skip("root can read anything")
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    target = root / "hooks" / "hooks.json"
    target.chmod(0o000)
    try:
        proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    finally:
        target.chmod(0o644)
    assert proc.returncode == 1, proc.stdout
    assert "UNREADABLE" in proc.stdout


def test_editor_droppings_are_not_drift(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    (root / "hooks" / ".DS_Store").write_bytes(b"\x00")
    (root / "skills" / "__pycache__").mkdir(exist_ok=True)
    (root / "skills" / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\x00")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 0, proc.stdout


def test_deployment_globs_cover_the_known_trees():
    """check_versions derives its deployment roots from this list, so a tree
    added to one check is added to both."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_cd", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    globs = set(module.DEPLOYMENT_GLOBS)
    assert ".cursor/plugins/local/minni/dist" in globs
    assert all(g.endswith("/dist") for g in globs), (
        "check_versions strips a trailing /dist to reach the plugin root"
    )

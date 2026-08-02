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


def _run(*args, home: Path, repo_root: Path, extra_env: dict | None = None):
    env = {
        **os.environ,
        "MINNI_CHECK_DEPLOYMENTS_HOME": str(home),
        "MINNI_CHECK_DEPLOYMENTS_REPO_ROOT": str(repo_root),
    }
    if extra_env:
        env.update(extra_env)
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


def _artifact_dist(root: Path, *, git_sha: str = "unknown") -> None:
    """Replace a deployment's dist with a real built-artifact-shaped copy.

    The isolated repo root is not a git repository, so source HEAD reads
    "unknown"; a manifest stamped the same way reports OK vintage, which is
    what these fixtures need as a clean baseline.
    """
    if (root / "dist").is_symlink() or (root / "dist").exists():
        import shutil

        if (root / "dist").is_symlink():
            (root / "dist").unlink()
        else:
            shutil.rmtree(root / "dist")
    (root / "dist").mkdir()
    (root / "dist" / "server.js").write_text("// built\n", encoding="utf-8")
    (root / "dist" / "build-manifest.json").write_text(
        json.dumps({"git_sha": git_sha, "built_at": "2026-08-01T00:00:00Z"}),
        encoding="utf-8",
    )


def test_faithful_copy_with_artifact_dist_is_clean(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    _artifact_dist(root)
    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 0, proc.stdout
    assert "DRIFT" not in proc.stdout
    assert "WORKTREE" not in proc.stdout


def test_dist_symlinked_at_working_tree_fails_strict(tmp_path):
    """D14 (#234): a dist symlinked at the repo working tree executes
    uncommitted state and has no reproducible version. The old check printed a
    reassuring LINKED and passed it."""
    home = tmp_path / "home"
    home.mkdir()
    _deployment(home, link_dist_to=SOURCE / "dist")
    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "WORKTREE" in proc.stdout
    assert "LINKED" not in proc.stdout


def test_dist_symlinked_to_frozen_artifact_under_repo_is_not_worktree(tmp_path):
    """Round-2 Low: prefix-matching any path under the repo flagged a dist
    symlink to release-artifacts/.../dist as WORKTREE. Only the live
    plugins/minni/dist working tree is the defect."""
    home = tmp_path / "home"
    home.mkdir()
    repo = _isolated_repo_root(tmp_path)
    frozen = repo / "release-artifacts" / "0.4.1" / "dist"
    frozen.mkdir(parents=True)
    (frozen / "server.js").write_text("// frozen\n", encoding="utf-8")
    (frozen / "build-manifest.json").write_text(
        json.dumps({"git_sha": "unknown", "built_at": "2026-08-01T00:00:00Z"}),
        encoding="utf-8",
    )
    root = _deployment(home, link_dist_to=frozen)
    # Faithful non-dist copies so content check is clean.
    proc = _run("--strict", home=home, repo_root=repo)
    assert "WORKTREE" not in proc.stdout, proc.stdout
    # May still be DRIFT/UNKNOWN depending on hooks fidelity; the pin is WORKTREE.


def test_hooks_drift_behind_a_symlinked_dist_is_reported(tmp_path):
    """The measured 2026-08-01 condition: dist/ linked, hooks.json stale."""
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    hooks = root / "hooks" / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"stale": True}}), encoding="utf-8")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    # The worktree link is stated AND the content drift is still reported —
    # neither hides the other.
    assert "WORKTREE" in proc.stdout
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
    _artifact_dist(root)
    manifest = root / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = "0.4.0+git.deadbee"
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 0, proc.stdout
    assert "DRIFT" not in proc.stdout


def test_hooks_schema_version_is_not_normalized_away(tmp_path):
    """hooks-cursor.json's top-level "version" is Cursor's hooks-schema
    version, not plugin semver -- check_versions.py never reads it. Stripping
    it the same way plugin-manifest versions are stripped would re-blind a
    real content difference, exactly the failure this slice exists to end."""
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    manifest = root / "hooks" / "hooks-cursor.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = data.get("version", 1) + 1
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "hooks/hooks-cursor.json" in proc.stdout


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
    _artifact_dist(root)
    (root / "hooks" / ".DS_Store").write_bytes(b"\x00")
    (root / "skills" / "__pycache__").mkdir(exist_ok=True)
    (root / "skills" / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\x00")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 0, proc.stdout


def test_source_unreadable_is_not_charged_to_the_deployment(tmp_path):
    """A file this tool cannot read on the *source* side is a tool/checkout
    defect, not evidence the deployment drifted. It must still fail the run
    loudly (never silently treated as clean) but must not be attributed to a
    deployment whose own copy is perfectly readable."""
    if os.geteuid() == 0:  # pragma: no cover - root ignores mode bits
        import pytest

        pytest.skip("root can read anything")
    home = tmp_path / "home"
    home.mkdir()
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    repo_root = _isolated_repo_root(tmp_path)
    source_file = repo_root / "plugins" / "minni" / "hooks" / "hooks.json"
    # The isolated repo root symlinks plugins/ back to the real tree, so make
    # the source file unreadable on the real tree and restore it afterward.
    real_source_file = SOURCE / "hooks" / "hooks.json"
    real_source_file.chmod(0o000)
    try:
        proc = _run("--strict", home=home, repo_root=repo_root)
    finally:
        real_source_file.chmod(0o644)
    assert proc.returncode == 1, proc.stdout
    assert "SOURCE UNREADABLE" in proc.stdout
    # The deployment's own copy is fine and must not be reported UNREADABLE.
    label = str(root).replace(str(home), "~")
    for line in proc.stdout.splitlines():
        if label in line:
            assert "UNREADABLE" not in line, proc.stdout


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


def _write_mcp(root: Path, *, agent: str, server: Path, helper: Path | None = None):
    env = {"MINNI_AGENT_ID": agent}
    if helper is not None:
        env["MINNI_AFM_NATIVE_HELPER"] = str(helper)
    (root / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(server)],
                    "cwd": str(root),
                    "env": env,
                }
            }
        }),
        encoding="utf-8",
    )


def _agents_tree_deployment(home: Path) -> Path:
    """A deployment in the shared agents tree (the D14 live location)."""
    root = home / ".agents" / "plugins" / "minni@minni"
    root.mkdir(parents=True)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, root / sub)
    _artifact_dist(root)
    return root


def test_wrong_agent_stamp_is_reported(tmp_path):
    """D14 (#234): the shared agents-tree deployment stamped `agent=cursor`
    attributes activity to the wrong agent; nothing used to flag it."""
    home = tmp_path / "home"
    home.mkdir()
    root = _agents_tree_deployment(home)
    _write_mcp(root, agent="cursor", server=root / "dist" / "server.js")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "BADCONFIG" in proc.stdout
    assert "agent stamp 'cursor'" in proc.stdout


def test_correct_agent_stamp_passes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = _agents_tree_deployment(home)
    _write_mcp(root, agent="gemini", server=root / "dist" / "server.js")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 0, proc.stdout
    assert "BADCONFIG" not in proc.stdout


def test_dead_helper_path_is_reported(tmp_path):
    """D14 (#234): a helper path recorded in a deployed config that does not
    exist must be detected here, not found by hand."""
    home = tmp_path / "home"
    home.mkdir()
    root = _agents_tree_deployment(home)
    _write_mcp(
        root,
        agent="gemini",
        server=root / "dist" / "server.js",
        helper=home / "nonexistent" / "native_afm_helper",
    )

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "BADCONFIG" in proc.stdout
    assert "dead path" in proc.stdout
    assert "MINNI_AFM_NATIVE_HELPER" in proc.stdout


def test_dead_server_path_is_reported(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = _agents_tree_deployment(home)
    _write_mcp(root, agent="gemini", server=root / "dist" / "gone.js")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "dead path" in proc.stdout


def test_malformed_env_object_is_reported_not_crashed(tmp_path):
    """Round-6 Low: env as a non-dict must not AttributeError the whole scan."""
    home = tmp_path / "home"
    home.mkdir()
    root = _agents_tree_deployment(home)
    (root / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(root / "dist" / "server.js")],
                    "cwd": str(root),
                    "env": "gemini",
                }
            }
        }),
        encoding="utf-8",
    )
    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "env is not an object" in proc.stdout


def test_missing_agent_stamp_is_reported(tmp_path):
    """Round-1 finding: a wiped/partial .mcp.json (minni entry, no
    MINNI_AGENT_ID) used to pass the agent check silently."""
    home = tmp_path / "home"
    home.mkdir()
    root = _agents_tree_deployment(home)
    (root / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(root / "dist" / "server.js")],
                    "cwd": str(root),
                    "env": {},
                }
            }
        }),
        encoding="utf-8",
    )

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "BADCONFIG" in proc.stdout
    assert "no MINNI_AGENT_ID" in proc.stdout


def test_unparseable_mcp_json_is_reported(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = _agents_tree_deployment(home)
    (root / ".mcp.json").write_text("{not json", encoding="utf-8")

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "BADCONFIG" in proc.stdout


def test_inactive_wire_version_dist_is_skipped_not_strict_failed(tmp_path):
    """Round-7 High: historical ~/.minni/plugin/<old>/dist must not fail
    --strict after a fresher wire (same active-set logic as check_versions)."""
    home = tmp_path / "home"
    home.mkdir()
    plugin = home / ".minni" / "plugin"
    old = plugin / "0.3.0"
    old.mkdir(parents=True)
    (old / "dist").mkdir()
    (old / "dist" / "server.js").write_text("// old\n", encoding="utf-8")
    (old / "dist" / "build-manifest.json").write_text(
        json.dumps({"git_sha": "0" * 40, "built_at": "2026-07-01T00:00:00Z"}),
        encoding="utf-8",
    )
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, old / sub)

    fresh = plugin / "0.4.1+git.abc1234"
    root = _deployment(home, link_dist_to=SOURCE / "dist")
    # Point _deployment at kilo path; also create active wire under plugin/
    # Use artifact dist under fresh versioned wire root.
    import shutil
    if fresh.exists():
        shutil.rmtree(fresh)
    fresh.mkdir(parents=True)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, fresh / sub)
    _artifact_dist(fresh)
    (fresh / "payload-manifest.json").write_text(
        json.dumps({"version": "0.4.1+git.abc1234", "git_sha": "a" * 40}),
        encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [{
                "platform": "claude-code",
                "install_root": str(fresh),
                "wired_at": "2026-08-02T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )
    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert "skipped (not an active wire install" in proc.stdout, proc.stdout
    # Old tree must not force STALE failure by itself.
    # Fresh kilo deployment may still DRIFT/UNKNOWN; pin is the skip note + no crash.
    assert "0.3.0" in proc.stdout


def test_legacy_marketplace_cache_skipped_when_wire_active(tmp_path):
    """Round-8 High: leftover ~/.claude|codex/plugins/cache trees must not
    fail --strict after wire-primary adoption (sync-root never refreshes them)."""
    home = tmp_path / "home"
    home.mkdir()
    plugin = home / ".minni" / "plugin"
    fresh = plugin / "0.4.1+git.deadbeef"
    fresh.mkdir(parents=True)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, fresh / sub)
    _artifact_dist(fresh)
    (fresh / "payload-manifest.json").write_text(
        json.dumps({"version": "0.4.1+git.deadbeef", "git_sha": "a" * 40}),
        encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [{
                "platform": "claude-code",
                "install_root": str(fresh),
                "wired_at": "2026-08-02T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )
    # Stale marketplace cache left from pre-wire era.
    cache_root = (
        home / ".claude" / "plugins" / "cache" / "minni" / "minni" / "0.3.0"
    )
    cache_root.mkdir(parents=True)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, cache_root / sub)
    (cache_root / "dist").mkdir()
    (cache_root / "dist" / "server.js").write_text("// old cache\n", encoding="utf-8")
    (cache_root / "dist" / "build-manifest.json").write_text(
        json.dumps({"git_sha": "f" * 40, "built_at": "2026-07-01T00:00:00Z"}),
        encoding="utf-8",
    )

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    assert "legacy marketplace cache" in proc.stdout, proc.stdout
    assert "0.3.0" in proc.stdout
    # Active wire + skipped cache only: --strict must stay green.
    assert proc.returncode == 0, proc.stdout
    assert "STALE" not in proc.stdout


def test_repo_plugin_payload_skipped_when_env_set(tmp_path):
    """sync-root must not gate on leftover stage-payload under plugin_payload."""
    home = tmp_path / "home"
    home.mkdir()
    # Real-shaped repo root with a stale in-repo payload dist (would STALE).
    root = tmp_path / "repo"
    root.mkdir()
    (root / "plugins").symlink_to(REPO / "plugins")
    payload = root / "src" / "minni" / "plugin_payload"
    payload.mkdir(parents=True)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, payload / sub)
    (payload / "dist").mkdir()
    (payload / "dist" / "server.js").write_text("// staged\n", encoding="utf-8")
    (payload / "dist" / "build-manifest.json").write_text(
        json.dumps({"git_sha": "a" * 40, "built_at": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )

    marker = "src/minni/plugin_payload"
    # Without skip: in-repo payload is discovered (may STALE/DRIFT).
    bare = _run("--strict", home=home, repo_root=root)
    assert marker in bare.stdout, bare.stdout

    # With skip (what update_root sets): not discovered at all.
    skipped = _run(
        "--strict",
        home=home,
        repo_root=root,
        extra_env={"MINNI_CHECK_DEPLOYMENTS_SKIP_REPO": "1"},
    )
    assert marker not in skipped.stdout, skipped.stdout
    assert skipped.returncode == 0, skipped.stdout


def test_marketplace_cache_skip_is_per_platform(tmp_path):
    """Only the platform that is wire-active should silence its marketplace cache.

    claude-code wire alone must not skip a still-live Codex cache (mid-migration).
    """
    home = tmp_path / "home"
    home.mkdir()
    plugin = home / ".minni" / "plugin"
    fresh = plugin / "0.4.1+git.deadbeef"
    fresh.mkdir(parents=True)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, fresh / sub)
    _artifact_dist(fresh)
    (fresh / "payload-manifest.json").write_text(
        json.dumps({"version": "0.4.1+git.deadbeef", "git_sha": "a" * 40}),
        encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [{
                "platform": "claude-code",
                "install_root": str(fresh),
                "wired_at": "2026-08-02T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )
    # Stale Codex marketplace cache — still the live path if Codex was never wired.
    codex_cache = (
        home / ".codex" / "plugins" / "cache" / "minni" / "minni" / "0.2.9"
    )
    codex_cache.mkdir(parents=True)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, codex_cache / sub)
    (codex_cache / "dist").mkdir()
    (codex_cache / "dist" / "server.js").write_text("// codex cache\n", encoding="utf-8")
    (codex_cache / "dist" / "build-manifest.json").write_text(
        json.dumps({"git_sha": "b" * 40, "built_at": "2026-06-01T00:00:00Z"}),
        encoding="utf-8",
    )

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    # Must still judge the codex cache (not skip it under claude-only wire).
    assert "legacy marketplace cache for codex" not in proc.stdout, proc.stdout
    assert "0.2.9" in proc.stdout
    assert "STALE" in proc.stdout or proc.returncode != 0, proc.stdout


def test_plugin_current_skipped_when_wire_active(tmp_path):
    """Release-era plugin/current must not fail --strict after wire-primary."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    plugin = home / ".minni" / "plugin"
    fresh = plugin / "0.4.1+git.deadbeef"
    fresh.mkdir(parents=True)
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, fresh / sub)
    _artifact_dist(fresh)
    (fresh / "payload-manifest.json").write_text(
        json.dumps({"git_sha": "unknown", "version": "0.4.1+git.deadbeef"}),
        encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [{
                "platform": "claude-code",
                "install_root": str(fresh),
                "wired_at": "2026-08-02T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )
    # Lagging release current symlink target.
    old = plugin / "0.3.0"
    old.mkdir()
    for sub in ("hooks", "commands", "skills", ".claude-plugin", ".codex-plugin",
                ".cursor-plugin", ".kilocode-plugin", ".gemini-plugin"):
        src = SOURCE / sub
        if src.is_dir():
            _copytree(src, old / sub)
    (old / "dist").mkdir()
    (old / "dist" / "server.js").write_text("// old\n", encoding="utf-8")
    (old / "dist" / "build-manifest.json").write_text(
        json.dumps({"git_sha": "c" * 40, "built_at": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    cur = plugin / "current"
    cur.symlink_to(old, target_is_directory=True)

    proc = _run("--strict", home=home, repo_root=_isolated_repo_root(tmp_path))
    # current is skipped either as legacy plugin/current or (when symlink
    # resolves into a historical version dir) as inactive wire install.
    assert "plugin/current" in proc.stdout and "skipped" in proc.stdout, proc.stdout
    assert "STALE" not in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stdout

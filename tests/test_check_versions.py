"""§8 / §9.6: check-versions script passes and catches stale literals.

Extended 2026-08-01 (audit R1): the script grew an installed layer and a
deployed layer, and marketplace.json joined the repo layer. The regression the
new tests pin is the one that let a three-way split (marketplace 0.1.0, deployed
0.3.0, repo 0.4.1) report "all versions agree at 0.4.1".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_versions.py"


def _run(*args, env_extra=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO,
        env={**os.environ, **(env_extra or {})},
        capture_output=True,
        text=True,
    )


def test_check_versions_passes_on_current_tree():
    # --repo-only: the repo layer is the only one whose truth lives in this
    # commit. The installed/deployed layers describe the machine, and a machine
    # with a stale deployment must fail the tool, not the test suite.
    proc = _run("--repo-only")
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_check_versions_fails_on_injected_literal(tmp_path):
    src = REPO / "plugins" / "minni" / "skills" / "minni-install" / "scripts" / "propagate.py"
    copy = tmp_path / "propagate.py"
    text = src.read_text(encoding="utf-8")
    text += '\nSTALE = "~/.minni/plugin/0.9.9/dist/cli.js"\n'
    copy.write_text(text, encoding="utf-8")

    proc = _run("--repo-only", env_extra={"MINNI_CHECK_VERSIONS_PROPAGATE": str(copy)})
    assert proc.returncode == 1
    assert "0.9.9" in (proc.stdout + proc.stderr)


def test_repo_layer_catches_a_drifted_marketplace(tmp_path):
    """The split that survived: marketplace.json nests its version under
    plugins[].version, so the flat manifest reader saw nothing at all and the
    file sat three minor versions behind while the check printed 'all agree'."""
    src = REPO / ".claude-plugin" / "marketplace.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    for entry in data["plugins"]:
        entry["version"] = "0.1.0"
    copy = tmp_path / "marketplace.json"
    copy.write_text(json.dumps(data), encoding="utf-8")

    proc = _run("--repo-only", env_extra={"MINNI_CHECK_VERSIONS_MARKETPLACE": str(copy)})
    assert proc.returncode == 1, proc.stdout
    assert "0.1.0" in (proc.stdout + proc.stderr)
    assert "plugins[minni].version" in (proc.stdout + proc.stderr)


def test_marketplace_version_agrees_with_pyproject():
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == "minni")
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    import re

    canonical = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject, re.M).group(1)
    assert entry["version"] == canonical, (
        "marketplace.json is the manifest a fresh `/plugin install` reads; "
        "it drifted to 0.1.0 while the repo was at 0.4.1 and check-versions could not see it"
    )


def _fake_home_with_deployment(tmp_path: Path, version: str) -> Path:
    home = tmp_path / "home"
    root = home / ".config" / "kilo" / "plugins" / "minni"
    (root / "dist").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "minni", "version": version}), encoding="utf-8"
    )
    return home


def test_deployed_layer_fails_on_a_stale_deployment(tmp_path):
    home = _fake_home_with_deployment(tmp_path, "0.3.0")
    proc = _run(env_extra={"MINNI_CHECK_VERSIONS_HOME": str(home)})
    assert proc.returncode == 1, proc.stdout
    assert "deployed:" in (proc.stdout + proc.stderr)
    assert "0.3.0" in (proc.stdout + proc.stderr)


def _canonical() -> str:
    import re

    return re.search(
        r'^version\s*=\s*["\']([^"\']+)["\']',
        (REPO / "pyproject.toml").read_text(encoding="utf-8"),
        re.M,
    ).group(1)


def test_deployed_layer_passes_when_the_deployment_agrees(tmp_path):
    canonical = _canonical()
    home = _fake_home_with_deployment(tmp_path, canonical)
    # Only the deployed layer is under test here. The installed layer reads
    # whatever `minni` version happens to be on this interpreter's sys.path,
    # which this repo's own dev install may or may not match at any given
    # moment -- a real disagreement there must fail the *installed*-layer test
    # (see test_installed_layer_fails_on_a_mismatch below), not this one. The
    # override holds the installed layer at a known-agreeing value rather than
    # skipping its inspection, so this run still genuinely checks all three
    # layers -- it is just no longer at the mercy of the ambient environment.
    proc = _run(
        env_extra={
            "MINNI_CHECK_VERSIONS_HOME": str(home),
            "MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE": canonical,
        }
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "(agrees)" in proc.stdout


def test_installed_layer_fails_on_a_mismatch(tmp_path):
    canonical = _canonical()
    home = _fake_home_with_deployment(tmp_path, canonical)
    proc = _run(
        env_extra={
            "MINNI_CHECK_VERSIONS_HOME": str(home),
            "MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE": "0.2.0",
        }
    )
    assert proc.returncode == 1, proc.stdout
    assert "installed:" in (proc.stdout + proc.stderr)
    assert "0.2.0" in (proc.stdout + proc.stderr)


def test_manifestless_deployment_is_uninspectable_not_clean(tmp_path):
    """A tree we cannot establish a version for must fail loudly. Treating it as
    clean is the exact failure this whole slice exists to remove."""
    home = tmp_path / "home"
    (home / ".config" / "kilo" / "plugins" / "minni" / "dist").mkdir(parents=True)
    proc = _run(env_extra={"MINNI_CHECK_VERSIONS_HOME": str(home)})
    assert proc.returncode == 1, proc.stdout
    assert "UNINSPECTABLE" in (proc.stdout + proc.stderr)


def test_unparseable_deployed_manifest_is_uninspectable(tmp_path):
    home = tmp_path / "home"
    root = home / ".config" / "kilo" / "plugins" / "minni"
    (root / "dist").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
    proc = _run(env_extra={"MINNI_CHECK_VERSIONS_HOME": str(home)})
    assert proc.returncode == 1, proc.stdout
    assert "UNINSPECTABLE" in (proc.stdout + proc.stderr)


def test_no_deployments_is_reported_but_not_a_failure(tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    # Same reasoning as test_deployed_layer_passes_when_the_deployment_agrees:
    # this test is about the deployed layer being empty, not about whatever
    # `minni` version is installed in the interpreter running the suite, so
    # the installed layer is held at a known-agreeing value rather than left
    # to the ambient environment.
    proc = _run(
        env_extra={
            "MINNI_CHECK_VERSIONS_HOME": str(home),
            "MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE": _canonical(),
        }
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "no deployments discovered" in proc.stdout


def test_deployed_layer_accepts_from_repo_local_version(tmp_path):
    """Round-2 High: --from-repo / sync-root stamps 0.4.1+git.<short>. Exact
    equality against the public pyproject version made a successful redeploy
    fail its own verify step."""
    canonical = _canonical()
    local = f"{canonical}+git.abc1234"
    home = _fake_home_with_deployment(tmp_path, local)
    proc = _run(
        env_extra={
            "MINNI_CHECK_VERSIONS_HOME": str(home),
            "MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE": canonical,
        }
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "public version agrees" in proc.stdout or "(agrees)" in proc.stdout
    assert "0.3.0" not in proc.stdout


def test_deployed_layer_still_fails_on_public_drift_with_local_suffix(tmp_path):
    """Local suffix must not paper over a real public-version drift."""
    home = _fake_home_with_deployment(tmp_path, "0.3.0+git.deadbeef")
    proc = _run(
        env_extra={
            "MINNI_CHECK_VERSIONS_HOME": str(home),
            "MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE": _canonical(),
        }
    )
    assert proc.returncode == 1, proc.stdout
    assert "0.3.0+git.deadbeef" in (proc.stdout + proc.stderr)


def test_inactive_wire_version_dir_is_skipped_not_failed(tmp_path):
    """Round-2 compounder: non-interactive wire leaves historical
    ~/.minni/plugin/<old>/ trees; only active wired/current roots are judged."""
    canonical = _canonical()
    home = tmp_path / "home"
    plugin = home / ".minni" / "plugin"
    old = plugin / "0.3.0"
    old.mkdir(parents=True)
    (old / "dist").mkdir()
    (old / ".claude-plugin").mkdir()
    (old / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "minni", "version": "0.3.0"}), encoding="utf-8",
    )
    fresh_ver = f"{canonical}+git.abc1234"
    fresh = plugin / fresh_ver
    fresh.mkdir(parents=True)
    (fresh / "dist").mkdir()
    (fresh / ".claude-plugin").mkdir()
    (fresh / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "minni", "version": fresh_ver}), encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [{
                "platform": "claude-code",
                "install_root": str(fresh),
                "version": fresh_ver,
                "wired_at": "2026-08-02T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )
    proc = _run(
        env_extra={
            "MINNI_CHECK_VERSIONS_HOME": str(home),
            "MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE": canonical,
        }
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "skipped (not an active wire install" in proc.stdout
    assert "public version agrees" in proc.stdout or fresh_ver in proc.stdout

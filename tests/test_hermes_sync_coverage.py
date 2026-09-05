"""Hermes sync validation never rewrites host configuration."""

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from minni.wire.hermes import inspect_hermes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("minni.wire.hermes.shutil.which", lambda _: "/bin/fixture")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("GIT_")}

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], env=env, stderr=subprocess.DEVNULL, text=True
        ).strip()

    git("init")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    (repo / ".gitignore").write_text("plugins/\n")
    git("add", ".gitignore")
    git("commit", "-m", "fixture")
    sha = git("rev-parse", "HEAD")
    server = repo / "plugins/minni/dist/server.js"
    server.parent.mkdir(parents=True)
    server.write_text("fixture server")
    (server.parent / "build-manifest.json").write_text(json.dumps({"git_sha": sha, "git_dirty": False}))
    payload = tmp_path / ".minni/plugin/0.5.0+git.fixture"
    (payload / "dist").mkdir(parents=True)
    (payload / "dist/server.js").write_bytes(server.read_bytes())
    (payload / "payload-manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "version": payload.name,
                "git_sha": sha,
                "files": {"dist/server.js": "sha256:" + hashlib.sha256(server.read_bytes()).hexdigest()},
            }
        )
    )
    config = tmp_path / ".hermes/config.yaml"
    config.parent.mkdir()
    config.write_text(
        f"# preserve comments\nmcp_servers:\n  minni:\n    enabled: true\n    command: node\n    args: [{server}]\n    env: {{MINNI_AGENT_ID: hermes, CUSTOM: secret-fixture}}\n  other: {{command: unrelated}}\n"
    )
    return repo, payload, config, server


def test_unregistered_source_verified_bytes_unchanged_and_reload_distinct(setup):
    repo, payload, config, _ = setup
    before = config.read_bytes()
    first = inspect_hermes(repo=repo, new_root=payload)
    assert first.get("artifact") == "verified", first
    assert first["runtime"] == "not_probed" and first["reload_required"]
    assert inspect_hermes(repo=repo, new_root=payload) == first
    assert config.read_bytes() == before
    assert "secret-fixture" not in json.dumps(first)


@pytest.mark.parametrize(
    "mode",
    [
        "packaged",
        "different_checkout",
        "missing_payload",
        "changed_server",
        "stale_build",
        "dirty_source",
        "malformed",
        "duplicate",
        "bad_env",
        "symlink",
    ],
)
def test_incomplete_preserves_config(setup, mode):
    repo, payload, config, server = setup
    if mode == "packaged":
        repo = None
    elif mode == "different_checkout":
        repo = repo.parent
    elif mode == "missing_payload":
        payload = None
    elif mode == "changed_server":
        server.write_text("changed")
    elif mode == "stale_build":
        (server.parent / "build-manifest.json").write_text("{}")
    elif mode == "dirty_source":
        (repo / "untracked").write_text("dirty")
    elif mode == "malformed":
        config.write_text("[]")
    elif mode == "duplicate":
        config.write_text("mcp_servers: {}\nmcp_servers: {}\n")
    elif mode == "bad_env":
        config.write_text(config.read_text().replace("CUSTOM: secret-fixture", "CUSTOM: 42"))
    elif mode == "symlink":
        dest = config.with_suffix(".original")
        config.rename(dest)
        config.symlink_to(dest)
    before = config.read_bytes()
    result = inspect_hermes(repo=repo, new_root=payload)
    assert result["exit_code"] == 1 and result["status"] == "incomplete"
    assert config.read_bytes() == before


@pytest.mark.parametrize("mode", ["absent", "disabled", "no_binding", "unavailable"])
def test_no_activation(setup, monkeypatch, mode):
    repo, payload, config, _ = setup
    if mode == "absent":
        config.unlink()
    elif mode == "disabled":
        config.write_text(config.read_text().replace("enabled: true", "enabled: false"))
    elif mode == "no_binding":
        config.write_text("mcp_servers: {}")
    else:
        monkeypatch.setattr("minni.wire.hermes.shutil.which", lambda _: None)
    before = config.read_bytes() if config.exists() else None
    assert inspect_hermes(repo=repo, new_root=payload)["skipped"]
    assert (config.read_bytes() if config.exists() else None) == before


def test_dry_run_does_not_claim_built(setup):
    repo, _, config, server = setup
    server.unlink()
    result = inspect_hermes(repo=repo, dry_run=True)
    assert result["artifact"] == "not_validated" and result["exit_code"] == 0


@pytest.mark.parametrize("packaged", [False, True])
def test_fleet_propagates_hermes_result(setup, monkeypatch, packaged):
    from minni import fleet_sync as fleet

    repo, payload, _, _ = setup
    monkeypatch.setattr(
        fleet, "_detect_install_kind", lambda: ("packaged", None) if packaged else ("editable-checkout", repo)
    )
    monkeypatch.setattr(fleet, "_run_wire", lambda **kw: {"name": "wire_all", "exit_code": 0, "install_root": payload})
    monkeypatch.setattr(fleet, "_audit_deploy_symlinks", lambda *a, **kw: {"name": "audit", "exit_code": 0})
    result = fleet.run_fleet_sync(propagate_hosts=False, restart_daemon=False)
    step = next(s for s in result.steps if s["name"] == "hermes_source_binding")
    assert result.ok is (not packaged)
    assert step["status"] == ("incomplete" if packaged else "artifact_current")
    if not packaged:
        assert any("/reload-mcp" in action for action in result.next_actions)


def test_update_root_hermes_stage_propagates_failure(tmp_path):
    """Execute the real shell stage, with a recording Python command stub."""
    script = (Path(__file__).resolve().parents[1] / "scripts/update_root.sh").read_text()
    stage = script.split("# Validate existing Hermes source binding;", 1)[1].split(
        'if [ "$DRY_RUN" != 1 ]; then rm', 1
    )[0]
    stage = stage[stage.index("\n") :]
    record = tmp_path / "argv"
    stub = tmp_path / "python-stub"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD"\nexit 1\n')
    stub.chmod(0o755)
    import os

    env = {
        **os.environ,
        "RECORD": str(record),
        "DRY_RUN": "0",
        "VENV_PY": str(stub),
        "REPO": str(tmp_path / "repo with spaces"),
        "_WIRE_JSON": str(tmp_path / "wire.json"),
    }
    run = subprocess.run(
        ["bash", "-c", "REDEPLOY_EXIT=0\n" + stage + '\nexit "$REDEPLOY_EXIT"'], env=env, capture_output=True, text=True
    )
    assert run.returncode == 1
    assert record.read_text().splitlines() == [
        "-m",
        "minni.wire.hermes",
        "--repo",
        env["REPO"],
        "--wire-report",
        env["_WIRE_JSON"],
    ]

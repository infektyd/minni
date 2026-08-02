"""GA1-3/GA5-1: the daemon status reports when the RUNNING code is stale.

The signal must be truthful, not a guess: stale=True only on evidence (the
checkout's HEAD moved since the daemon captured its start state), stale=None
with a reason whenever the comparison is unmeasurable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minni.minnid_runtime import deploy_honesty


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(repo), text=True,
    ).strip()


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "one")
    return repo


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(deploy_honesty, "_START_STATE", None)
    monkeypatch.setattr(deploy_honesty, "_HEAD_CACHE", {})


def test_matching_head_is_not_stale(checkout, monkeypatch):
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["install_kind"] == "editable-checkout"
    assert out["stale"] is False
    assert out["started_git_sha"] == out["current_git_sha"]


def test_moved_head_is_reported_stale(checkout, monkeypatch):
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    (checkout / "f.txt").write_text("two\n", encoding="utf-8")
    _git(checkout, "commit", "-aqm", "two")
    out = deploy_honesty.deploy_status()
    assert out["stale"] is True
    assert "stale" in out["reason"]
    assert out["started_git_sha"] != out["current_git_sha"]


def test_dirty_start_is_named_not_guessed(checkout, monkeypatch):
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    (checkout / "f.txt").write_text("uncommitted\n", encoding="utf-8")
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["stale"] is False
    assert out["started_dirty"] is True
    assert "dirty" in out["reason"]


def test_wheel_install_reports_unmeasurable(monkeypatch):
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: None)
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["install_kind"] == "wheel"
    assert out["stale"] is None
    assert "wheel" in out["reason"]


def test_plugin_dist_staleness(checkout, monkeypatch, tmp_path):
    home = tmp_path / "home"
    plugin = home / ".minni" / "plugin" / "0.4.1"
    plugin.mkdir(parents=True)
    (home / ".minni" / "plugin" / "current").symlink_to(plugin)
    head = _git(checkout, "rev-parse", "HEAD")
    (plugin / "payload-manifest.json").write_text(
        '{"git_sha": "%s"}' % head, encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["stale"] is False

    (checkout / "f.txt").write_text("three\n", encoding="utf-8")
    _git(checkout, "commit", "-aqm", "three")
    monkeypatch.setattr(deploy_honesty, "_HEAD_CACHE", {})
    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["stale"] is True
    assert "sync-root" in out["plugin_dist"]["reason"] or "wire" in out["plugin_dist"]["reason"]


def test_deploy_status_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("git exploded")

    monkeypatch.setattr(deploy_honesty, "_source_checkout", boom)
    monkeypatch.setattr(deploy_honesty, "_START_STATE", None)
    out = deploy_honesty.deploy_status()
    assert out["stale"] is None
    assert "error" in out


def test_status_surface_carries_deploy_block(monkeypatch, tmp_path):
    """handle_status must actually emit the deploy block."""
    from minni.minnid_runtime.health import HealthContext, handle_status

    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: None)

    context = HealthContext(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}},
        make_response=lambda result, rid: {"result": result},
        guard_vault_root=lambda *a, **k: None,
        latency_snapshot=lambda: {},
        metrics_snapshot=lambda: {},
        afm_loop_enabled=lambda cfg: False,
    )
    resp = handle_status({"vault": str(tmp_path)}, 1, context)
    deploy = resp["result"]["daemon"]["deploy"]
    assert "stale" in deploy
    assert deploy["install_kind"] == "wheel"

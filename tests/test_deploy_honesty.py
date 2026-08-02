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


def _matching_plugin(home: Path, head: str) -> None:
    """Install a wire payload whose manifest matches *head* (process+plugin green)."""
    import json as _json

    root = home / ".minni" / "plugin" / "0.4.1"
    root.mkdir(parents=True)
    (root / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": head, "version": "0.4.1"}), encoding="utf-8",
    )
    (home / ".minni" / "plugin" / "wired.json").write_text(
        _json.dumps({
            "schema": 1,
            "wires": [{
                "platform": "claude-code",
                "install_root": str(root),
                "wired_at": "2026-08-02T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )


def test_matching_head_is_not_stale(checkout, monkeypatch, tmp_path):
    home = tmp_path / "home"
    head = _git(checkout, "rev-parse", "HEAD")
    _matching_plugin(home, head)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["install_kind"] == "editable-checkout"
    assert out["stale"] is False
    assert out["plugin_dist"]["stale"] is False
    assert out["started_git_sha"] == out["current_git_sha"]


def test_moved_head_is_reported_stale(checkout, monkeypatch, tmp_path):
    home = tmp_path / "home"
    head = _git(checkout, "rev-parse", "HEAD")
    _matching_plugin(home, head)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    (checkout / "f.txt").write_text("two\n", encoding="utf-8")
    _git(checkout, "commit", "-aqm", "two")
    out = deploy_honesty.deploy_status()
    assert out["stale"] is True
    assert "stale" in out["reason"] or "stale" in out.get("plugin_dist", {}).get("reason", "")
    assert out["started_git_sha"] != out["current_git_sha"]


def test_dirty_start_is_named_not_guessed(checkout, monkeypatch, tmp_path):
    home = tmp_path / "home"
    head = _git(checkout, "rev-parse", "HEAD")
    _matching_plugin(home, head)
    monkeypatch.setenv("HOME", str(home))
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


def test_plugin_dist_tracks_wired_local_payload_without_current(
    checkout, monkeypatch, tmp_path,
):
    """Round-1 High: --from-repo / sync-root installs local (+git.*) versions
    and update_current_symlink deliberately never moves `current` for those.
    The honesty signal must resolve the payload wire RECORDED, not go blind
    (stale=None forever) because `current` is absent."""
    import json as _json

    home = tmp_path / "home"
    head = _git(checkout, "rev-parse", "HEAD")
    local_ver = f"0.4.1+git.{head[:7]}"
    root = home / ".minni" / "plugin" / local_ver
    root.mkdir(parents=True)
    (root / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": head, "version": local_ver}), encoding="utf-8",
    )
    (home / ".minni" / "plugin" / "wired.json").write_text(
        _json.dumps({
            "schema": 1, "generation": 1,
            "wires": [{
                "platform": "claude-code",
                "install_root": str(root),
                "version": local_ver,
                "wired_at": "2026-08-02T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()

    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["resolved_via"].startswith("wired.json")
    assert out["plugin_dist"]["stale"] is False
    assert out["plugin_dist"]["dist_version"] == local_ver

    (checkout / "f.txt").write_text("moved\n", encoding="utf-8")
    _git(checkout, "commit", "-aqm", "moved")
    monkeypatch.setattr(deploy_honesty, "_HEAD_CACHE", {})
    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["stale"] is True


def test_plugin_dist_prefers_wired_record_over_zombie_current(
    checkout, monkeypatch, tmp_path,
):
    """Round-2 High: when wired.json has valid latest-per-platform roots,
    a leftover release `current` symlink must NOT join the active set.
    Local (+git.*) installs never move current; always-including it would
    report stale=True forever and brick make sync-root after --from-repo."""
    import json as _json

    home = tmp_path / "home"
    head = _git(checkout, "rev-parse", "HEAD")
    plugin = home / ".minni" / "plugin"

    old = plugin / "0.4.0"
    old.mkdir(parents=True)
    (old / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": "0" * 40, "version": "0.4.0"}), encoding="utf-8",
    )
    (plugin / "current").symlink_to(old)

    local_ver = f"0.4.1+git.{head[:7]}"
    fresh = plugin / local_ver
    fresh.mkdir()
    (fresh / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": head, "version": local_ver}), encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        _json.dumps({
            "schema": 1, "generation": 2,
            "wires": [
                {"platform": "claude-code", "install_root": str(old),
                 "version": "0.4.0", "wired_at": "2026-07-01T00:00:00Z"},
                {"platform": "claude-code", "install_root": str(fresh),
                 "version": local_ver, "wired_at": "2026-08-02T00:00:00Z"},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()

    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["stale"] is False, out["plugin_dist"]
    assert out["plugin_dist"]["dist_version"] == local_ver
    assert "current" not in out["plugin_dist"]["resolved_via"]
    assert out["plugin_dist"]["active_roots"] == 1


def test_plugin_dist_stale_if_any_platform_root_lags(checkout, monkeypatch, tmp_path):
    """Latest-per-platform: judging only the global-newest root hides a lagging
    peer still executing an older payload after a partial rewire."""
    import json as _json

    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    (home / ".codex").mkdir()
    plugin = home / ".minni" / "plugin"
    head = _git(checkout, "rev-parse", "HEAD")
    old = plugin / "0.4.1+git.oldroot"
    new = plugin / "0.4.1+git.newroot"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": "0" * 40, "version": "0.4.1+git.oldroot"}),
        encoding="utf-8",
    )
    (new / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": head, "version": "0.4.1+git.newroot"}),
        encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        _json.dumps({
            "schema": 1,
            "wires": [
                {"platform": "claude-code", "install_root": str(old),
                 "wired_at": "2026-07-01T00:00:00Z"},
                {"platform": "codex", "install_root": str(new),
                 "wired_at": "2026-08-02T00:00:00Z"},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["stale"] is True
    assert out["plugin_dist"]["active_roots"] == 2
    assert any("oldroot" in x for x in out["plugin_dist"].get("lagging", []))
    assert out["stale"] is True

def test_top_level_stale_rolls_up_plugin_dist(checkout, monkeypatch, tmp_path):
    """Round-5 Med: top-level deploy.stale must be true when plugin_dist is
    stale even if the daemon process still matches start HEAD."""
    import json as _json

    home = tmp_path / "home"
    head = _git(checkout, "rev-parse", "HEAD")
    # Process matches HEAD (not process-stale) but plugin lags.
    old_sha = "0" * 40
    root = home / ".minni" / "plugin" / "0.4.0"
    root.mkdir(parents=True)
    (root / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": old_sha, "version": "0.4.0"}), encoding="utf-8",
    )
    (home / ".minni" / "plugin" / "wired.json").write_text(
        _json.dumps({
            "schema": 1,
            "wires": [{
                "platform": "claude-code",
                "install_root": str(root),
                "wired_at": "2026-08-02T00:00:00Z",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["stale"] is True
    assert out["stale"] is True, out
    assert "plugin" in (out.get("reason") or "").lower() or "dist" in (out.get("reason") or "").lower()


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


def test_plugin_dist_unknown_first_plus_lagging_is_stale(checkout, monkeypatch, tmp_path):
    """Known lag must not be masked by a peer with unknown git_sha."""
    import json as _json

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gemini").mkdir()
    (home / ".codex").mkdir()
    plugin = home / ".minni" / "plugin"
    head = _git(checkout, "rev-parse", "HEAD")
    unk = plugin / "0.4.1+git.unknown"
    lag = plugin / "0.4.0"
    unk.mkdir(parents=True)
    lag.mkdir(parents=True)
    (unk / "payload-manifest.json").write_text(
        _json.dumps({"version": "0.4.1+git.unknown"}), encoding="utf-8",
    )
    (lag / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": "0" * 40, "version": "0.4.0"}), encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        _json.dumps({
            "schema": 1,
            "wires": [
                {"platform": "antigravity", "install_root": str(unk),
                 "wired_at": "2026-08-02T00:00:00Z"},
                {"platform": "codex", "install_root": str(lag),
                 "wired_at": "2026-08-02T01:00:00Z"},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["stale"] is True, out["plugin_dist"]
    assert out["stale"] is True, out


def test_plugin_dist_unreadable_peer_keeps_lag_evidence(checkout, monkeypatch, tmp_path):
    """Unreadable peer must not discard lag already proven on another root."""
    import json as _json

    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    (home / ".codex").mkdir()
    plugin = home / ".minni" / "plugin"
    head = _git(checkout, "rev-parse", "HEAD")
    lag = plugin / "0.4.0"
    bad = plugin / "0.4.1+git.broken"
    lag.mkdir(parents=True)
    bad.mkdir(parents=True)
    (lag / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": "0" * 40, "version": "0.4.0"}), encoding="utf-8",
    )
    (bad / "payload-manifest.json").write_text("{not-json", encoding="utf-8")
    (plugin / "wired.json").write_text(
        _json.dumps({
            "schema": 1,
            "wires": [
                {"platform": "claude-code", "install_root": str(lag),
                 "wired_at": "2026-08-02T00:00:00Z"},
                {"platform": "codex", "install_root": str(bad),
                 "wired_at": "2026-08-02T01:00:00Z"},
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: checkout)
    deploy_honesty.capture_start_state()
    out = deploy_honesty.deploy_status()
    assert out["plugin_dist"]["stale"] is True, out["plugin_dist"]
    assert out["plugin_dist"].get("lagging"), out["plugin_dist"]
    assert out["stale"] is True


def test_source_checkout_rejects_unrelated_git_ancestor(tmp_path, monkeypatch):
    """Wheel under a project venv must not inherit myapp's .git as Minni."""
    import types
    from minni.minnid_runtime import deploy_honesty

    # Fake package path: <app>/.venv/lib/pythonX/site-packages/minni/__init__.py
    app = tmp_path / "myapp"
    app.mkdir()
    (app / ".git").mkdir()
    pkg = app / ".venv" / "lib" / "python3.12" / "site-packages" / "minni"
    pkg.mkdir(parents=True)
    init = pkg / "__init__.py"
    init.write_text("# fake wheel\n", encoding="utf-8")
    fake = types.ModuleType("minni")
    fake.__file__ = str(init)
    monkeypatch.setitem(__import__("sys").modules, "minni", fake)
    assert deploy_honesty._source_checkout() is None


def test_source_checkout_accepts_editable_src_layout(tmp_path, monkeypatch):
    """Standard editable layout <repo>/src/minni + pyproject name=minni."""
    import types
    from minni.minnid_runtime import deploy_honesty

    repo = tmp_path / "minni-src"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "minni"\n', encoding="utf-8",
    )
    pkg = repo / "src" / "minni"
    pkg.mkdir(parents=True)
    init = pkg / "__init__.py"
    init.write_text("# editable\n", encoding="utf-8")
    fake = types.ModuleType("minni")
    fake.__file__ = str(init)
    monkeypatch.setitem(__import__("sys").modules, "minni", fake)
    assert deploy_honesty._source_checkout() == repo.resolve()


def test_zombie_wire_platform_without_config_root_not_active(tmp_path, monkeypatch):
    """codex wired but ~/.codex gone must not keep lagging root active."""
    import json as _json
    from minni.wire.active_roots import active_wire_plugin_roots_ordered
    from minni.minnid_runtime import deploy_honesty

    home = tmp_path / "home"
    home.mkdir()
    plugin = home / ".minni" / "plugin"
    old = plugin / "0.4.0"
    old.mkdir(parents=True)
    (old / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": "0" * 40, "version": "0.4.0"}), encoding="utf-8",
    )
    # claude-code needs HOME as config root; create a dummy .claude.json
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    fresh = plugin / "0.4.1+git.bbbbbbb"
    fresh.mkdir(parents=True)
    head = "a" * 40
    (fresh / "payload-manifest.json").write_text(
        _json.dumps({"git_sha": head, "version": "0.4.1"}), encoding="utf-8",
    )
    (plugin / "wired.json").write_text(
        _json.dumps({
            "schema": 1,
            "wires": [
                {
                    "platform": "codex",
                    "install_root": str(old),
                    "wired_at": "2026-08-01T00:00:00Z",
                },
                {
                    "platform": "claude-code",
                    "install_root": str(fresh),
                    "wired_at": "2026-08-02T00:00:00Z",
                },
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    # No ~/.codex → codex config root missing
    ordered = active_wire_plugin_roots_ordered(home)
    platforms = [how for _r, how in ordered]
    assert any("claude-code" in h for h in platforms), ordered
    assert not any("codex" in h for h in platforms), ordered

    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: None)
    # Use plugin dist path via real active roots + fake head
    monkeypatch.setattr(
        deploy_honesty, "_active_payload_roots",
        lambda: active_wire_plugin_roots_ordered(home),
    )
    out = deploy_honesty._plugin_dist_status(head)
    assert out["stale"] is False, out


def test_no_plugin_payload_is_stale_false_not_null(monkeypatch):
    """Measurable absence of a wire payload must not roll up to null stale."""
    from pathlib import Path

    from minni.minnid_runtime import deploy_honesty

    monkeypatch.setattr(deploy_honesty, "_active_payload_roots", lambda: [])
    out = deploy_honesty._plugin_dist_status("abc")
    assert out["stale"] is False
    assert "no wire-managed" in out["reason"]

    # Rollup: process clean + no payload → top-level stale false (not null).
    monkeypatch.setattr(
        deploy_honesty,
        "_START_STATE",
        {
            "install_kind": "editable-checkout",
            "checkout": "/tmp/not-used",
            "git_sha": "abc",
            "git_dirty": False,
            "captured_at": 0.0,
        },
    )
    monkeypatch.setattr(deploy_honesty, "_current_head", lambda _c: "abc")
    monkeypatch.setattr(deploy_honesty, "_source_checkout", lambda: Path("/tmp/not-used"))
    full = deploy_honesty.deploy_status()
    assert full["plugin_dist"]["stale"] is False
    assert full["stale"] is False, full

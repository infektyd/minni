"""Unit tests for minni.wire.preflight."""

from __future__ import annotations

from minni.wire import platform as wire_platform
from minni.wire.preflight import check_config_root, check_node, parse_node_version, preflight_platform


def test_parse_node_version_valid():
    assert parse_node_version("v20.11.0") == (20, 11, 0)
    assert parse_node_version("18.2.0") == (18, 2, 0)


def test_parse_node_version_garbage():
    assert parse_node_version("not-a-version") is None
    assert parse_node_version("") is None


def test_check_node_too_old(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        "minni.wire.preflight.shutil.which", lambda _: "/usr/bin/node",
    )
    monkeypatch.setattr(
        "minni.wire.preflight.subprocess.check_output",
        lambda *a, **k: "v18.2.0\n",
    )
    ok, msg = check_node(min_version=20)
    assert ok is False
    assert "older than 20" in msg


def test_check_node_unparseable_version(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        "minni.wire.preflight.shutil.which", lambda _: "/usr/bin/node",
    )
    monkeypatch.setattr(
        "minni.wire.preflight.subprocess.check_output",
        lambda *a, **k: "garbage-output\n",
    )
    ok, msg = check_node()
    assert ok is False
    assert "cannot parse node version" in msg


def test_check_config_root_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    codex_root = home / ".codex"
    monkeypatch.setenv("HOME", str(home))
    ok, msg = check_config_root("codex")
    assert ok is False
    assert "no config root found for codex" in msg
    assert str(codex_root) in msg


def test_preflight_platform_config_root_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "minni.wire.preflight.check_node", lambda min_version=20: (True, "v22.0.0"),
    )
    errors = preflight_platform("kilocode")
    assert len(errors) == 1
    assert "no config root found for kilocode" in errors[0]


def test_check_config_root_present(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    codex_root = home / ".codex"
    codex_root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ok, msg = check_config_root("codex")
    assert ok is True
    assert msg == ""


def test_config_root_candidates_follow_current_home(tmp_path, monkeypatch):
    # Regression: candidates must be computed per call, not at import time —
    # CI sandboxes set HOME long after the module is imported.
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    for home in (home_a, home_b):
        (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_a))
    assert wire_platform.config_root_candidates()["codex"] == (home_a / ".codex",)
    monkeypatch.setenv("HOME", str(home_b))
    assert wire_platform.config_root_candidates()["codex"] == (home_b / ".codex",)

def test_missing_config_root_only_is_skipped_status_contract():
    """Pure "no config root" preflight must finalize as skip, not fail.

    Combined with at least one wired platform, overall status stays ok so
    partial-fleet hosts can complete wire all / sync-root.
    """
    from minni.wire.output import PlatformResult, WireOutput

    errors = [
        "no config root found for kilocode (probed: /x); "
        "create the platform config or use --install-root"
    ]
    assert all("no config root found" in e for e in errors)
    out = WireOutput(status="ok")
    out.results.append(PlatformResult("claude-code", "wired", reason="ok"))
    out.results.append(
        PlatformResult("kilocode", "skipped", reason="; ".join(errors)),
    )
    out.finalize_status(dry_run=False)
    assert out.status == "ok"
    assert out.emit() == 0


def test_config_root_exists_respects_home_arg(tmp_path, monkeypatch):
    # Probe the *passed* home, not ambient $HOME.
    alien = tmp_path / "alien"
    alien.mkdir()
    (alien / ".codex").mkdir()
    # Ambient HOME has no codex
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(empty))
    ok, _ = wire_platform.config_root_exists("codex", home=alien)
    assert ok is True
    ok2, _ = wire_platform.config_root_exists("codex", home=empty)
    assert ok2 is False
    # active_roots must use the same home binding
    from minni.wire.active_roots import active_wire_plugin_roots_ordered
    import json
    plugin = alien / ".minni" / "plugin"
    root = plugin / "0.4.0"
    root.mkdir(parents=True)
    (root / "payload-manifest.json").write_text(json.dumps({"git_sha": "a"*40}), encoding="utf-8")
    (plugin / "wired.json").write_text(json.dumps({"schema":1,"wires":[{"platform":"codex","install_root":str(root),"wired_at":"2026-08-02T00:00:00Z"}]}), encoding="utf-8")
    ordered = active_wire_plugin_roots_ordered(alien)
    assert any("codex" in how for _r, how in ordered), ordered
    # same wired under empty home probe would drop codex
    plugin2 = empty / ".minni" / "plugin"
    root2 = plugin2 / "0.4.0"
    root2.mkdir(parents=True)
    (root2 / "payload-manifest.json").write_text(json.dumps({"git_sha": "a"*40}), encoding="utf-8")
    (plugin2 / "wired.json").write_text(json.dumps({"schema":1,"wires":[{"platform":"codex","install_root":str(root2),"wired_at":"2026-08-02T00:00:00Z"}]}), encoding="utf-8")
    ordered2 = active_wire_plugin_roots_ordered(empty)
    assert not any("codex" in how for _r, how in ordered2), ordered2

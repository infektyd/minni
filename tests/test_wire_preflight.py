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
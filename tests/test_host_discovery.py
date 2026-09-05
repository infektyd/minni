"""Optional host discovery never treats configuration as executable presence."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from minni.wire.host_discovery import discover_host, host_decision


def probe(home, platform, **kwargs):
    return host_decision(platform, home=home, path=str(home / "bin"), app_roots=(home / "Applications",), **kwargs)


def executable(home, name):
    target = home / "bin" / name
    target.parent.mkdir(exist_ok=True)
    target.write_text("#!/bin/sh\nexit 91\n")
    target.chmod(0o700)
    return target


def config(home, relative, text):
    target = home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


@pytest.mark.parametrize("platform", ["codex", "claude-code", "kilocode", "gemini", "antigravity", "grok", "cursor"])
def test_empty_home_skips_without_mutation(tmp_path, platform):
    before = list(tmp_path.rglob("*"))
    for bulk in (False, True):
        result = probe(tmp_path, platform, bulk=bulk)
        assert result["status"] == "skipped"
        assert not result["eligible"]
        assert result["host"]["runtime"] == "not_probed"
    assert list(tmp_path.rglob("*")) == before


def test_stale_configuration_does_not_activate_missing_host(tmp_path):
    target = config(tmp_path, ".claude.json", '{"mcpServers":{"minni":{"command":"node"}}}')
    before = target.read_bytes()
    result = probe(tmp_path, "claude-code")
    assert result["host"]["configured"] is True
    assert result["host"]["config_present"]
    assert not result["eligible"]
    assert target.read_bytes() == before


def test_explicit_initializes_available_host_but_bulk_requires_binding(tmp_path):
    executable(tmp_path, "codex")
    assert probe(tmp_path, "codex")["eligible"]
    assert not probe(tmp_path, "codex", bulk=True)["eligible"]
    config(tmp_path, ".codex/config.toml", '[mcp_servers.minni]\ncommand="node"\nenabled=true\n')
    assert probe(tmp_path, "codex", bulk=True)["eligible"]


def test_gui_codex_and_bundled_cli_without_path(tmp_path):
    app = tmp_path / "Applications/Codex.app"
    (app / "Contents/Resources").mkdir(parents=True)
    (app / "Contents/Info.plist").write_text("fixture")
    cli = app / "Contents/Resources/codex"
    cli.write_text("not executed")
    cli.chmod(0o700)
    result = probe(tmp_path, "codex")
    assert result["eligible"]
    assert result["host"]["executables"] == (str(cli),)
    assert result["host"]["applications"] == (str(app),)


def test_empty_app_directory_is_not_installation(tmp_path):
    (tmp_path / "Applications/Codex.app").mkdir(parents=True)
    assert not probe(tmp_path, "codex")["eligible"]


def test_shared_gemini_config_does_not_make_two_hosts_available(tmp_path):
    executable(tmp_path, "agy")
    config(tmp_path, ".gemini/config/mcp_config.json", '{"mcpServers":{"minni":{"command":"node"}}}')
    assert probe(tmp_path, "antigravity", bulk=True)["eligible"]
    assert not probe(tmp_path, "gemini")["eligible"]


def test_malformed_config_is_unknown_and_error_does_not_echo_secrets(tmp_path):
    executable(tmp_path, "grok")
    config(tmp_path, ".grok/config.toml", 'credential = "sensitive fixture\n')
    result = probe(tmp_path, "grok", bulk=True)
    assert not result["eligible"]
    assert result["host"]["configured"] is None
    assert "sensitive" not in json.dumps(result)
    assert result["status"] == "failed"
    assert probe(tmp_path, "grok")["status"] == "failed"
    (tmp_path / "bin/grok").unlink()
    assert probe(tmp_path, "grok")["status"] == "failed"


def test_generic_is_explicit_headless_exemption(tmp_path):
    assert probe(tmp_path, "generic")["eligible"]
    assert not probe(tmp_path, "generic", bulk=True)["eligible"]
    assert not probe(tmp_path, "unrecognized")["eligible"]


def test_standalone_mirror_is_identical_and_importable(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = root / "src/minni/wire/host_discovery.py"
    mirror = root / "plugins/minni/skills/minni-install/scripts/host_discovery.py"
    assert mirror.read_bytes() == source.read_bytes()
    spec = importlib.util.spec_from_file_location("standalone_host_discovery", mirror)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert module.host_decision("codex", home=tmp_path, path="", app_roots=())["eligible"] is False
    finally:
        sys.modules.pop(spec.name, None)


def test_claude_root_probe_does_not_count_home_itself(tmp_path):
    from minni.wire.platform import config_root_exists
    assert not config_root_exists("claude-code", tmp_path)[0]
    (tmp_path / ".claude").mkdir()
    assert config_root_exists("claude-code", tmp_path)[0]


@pytest.mark.parametrize("platform,command,relative,key", [
    ("codex", "codex", ".codex/config.toml", "mcp_servers"),
    ("kilocode", "kilo", ".config/kilo/kilo.json", "mcp"),
    ("cursor", "cursor", ".cursor/mcp.json", "mcpServers"),
])
@pytest.mark.parametrize("flag,value", [("enabled", False), ("disabled", True)])
def test_bulk_preserves_disabled_binding_but_explicit_can_enable(tmp_path, platform, command, relative, key, flag, value):
    executable(tmp_path, command)
    if relative.endswith("toml"):
        text = f'[{key}.minni]\n{flag}={str(value).lower()}\n'
    else:
        text = json.dumps({key: {"minni": {flag: value}}})
    target = config(tmp_path, relative, text)
    before = target.read_bytes()
    result = probe(tmp_path, platform, bulk=True)
    assert result["status"] == "skipped"
    assert result["host"]["configured"] is True
    assert result["host"]["binding_disabled"] is True
    assert "disabled" in result["reason"]
    assert probe(tmp_path, platform)["eligible"]
    assert target.read_bytes() == before


def test_disabled_view_is_not_overruled_by_other_enabled_view(tmp_path):
    executable(tmp_path, "agy")
    for relative, enabled in [(".gemini/config/mcp_config.json", True),
                              (".gemini/antigravity/mcp_config.json", False)]:
        config(tmp_path, relative, json.dumps({"mcpServers": {"minni": {"enabled": enabled}}}))
    assert not probe(tmp_path, "antigravity", bulk=True)["eligible"]
    assert probe(tmp_path, "antigravity")["eligible"]


def test_unrelated_disabled_server_does_not_disable_minni(tmp_path):
    executable(tmp_path, "kilo")
    config(tmp_path, ".config/kilo/kilo.json", json.dumps({"mcp": {
        "minni": {"enabled": True, "disabled": False}, "other": {"enabled": False}}}))
    assert probe(tmp_path, "kilocode", bulk=True)["eligible"]


def test_disabled_boolean_binding_is_preserved(tmp_path):
    executable(tmp_path, "kilo")
    config(tmp_path, ".config/kilo/kilo.json", '{"mcp":{"minni":false}}')
    assert probe(tmp_path, "kilocode", bulk=True)["host"]["binding_disabled"]
    assert not probe(tmp_path, "kilocode", bulk=True)["eligible"]


@pytest.mark.parametrize("payload", [
    {"mcpServers": []}, {"mcpServers": None},
    {"mcpServers": {"minni": "sensitive invalid binding"}},
    {"mcpServers": {"minni": True}},
])
def test_invalid_mcp_structure_fails_without_rewriting(tmp_path, payload):
    executable(tmp_path, "cursor")
    target = config(tmp_path, ".cursor/mcp.json", json.dumps(payload))
    before = target.read_bytes()
    for bulk in (False, True):
        result = probe(tmp_path, "cursor", bulk=bulk)
        assert result["status"] == "failed"
        assert not result["eligible"]
        assert "sensitive" not in json.dumps(result)
        assert target.read_bytes() == before


def test_cursor_agent_with_propagated_binding_is_bulk_eligible(tmp_path):
    executable(tmp_path, 'cursor-agent')
    config(tmp_path, '.cursor/plugins/local/minni/.mcp.json', '{"mcpServers":{"minni":{"command":"node"}}}')
    assert probe(tmp_path, 'cursor', bulk=True)['eligible'] is True


@pytest.mark.parametrize('location', ['user', 'homebrew', 'local'])
def test_restricted_ambient_path_finds_real_known_launcher(tmp_path, monkeypatch, location):
    from minni.wire import host_discovery as discovery
    monkeypatch.setenv('PATH', '')
    system = (tmp_path / 'opt/homebrew/bin', tmp_path / 'usr/local/bin')
    monkeypatch.setattr(discovery, '_SYSTEM_LAUNCHER_ROOTS', system)
    root = {'user': tmp_path / '.local/bin', 'homebrew': system[0], 'local': system[1]}[location]
    root.mkdir(parents=True)
    launcher = root / 'grok'
    marker = tmp_path / 'host-executed'
    launcher.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
    launcher.chmod(0o700)
    config(tmp_path, '.grok/config.toml', '[mcp_servers.minni]\n')
    result = host_decision('grok', home=tmp_path, bulk=True, app_roots=())
    assert result['eligible'] is True
    assert str(launcher) in result['host']['executables']
    assert not marker.exists()
    launcher.chmod(0o600)
    assert host_decision('grok', home=tmp_path, bulk=True, app_roots=())['eligible'] is False
    launcher.unlink()
    launcher.symlink_to(root / 'removed-cli')
    assert host_decision('grok', home=tmp_path, bulk=True, app_roots=())['eligible'] is False

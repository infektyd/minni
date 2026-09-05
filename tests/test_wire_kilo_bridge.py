"""Native Kilo installation is versioned, reversible, and actually executable."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from minni.wire import kilo
from minni.wire.flow import run_wire
from tests.test_wire_integration import (
    _args, _build_payload, _patch_payload, wire_env as _wire_env,
)


@pytest.fixture
def wire_env(request):
    return request.getfixturevalue("_wire_env")


def _wire(env, monkeypatch, capsys):
    _patch_payload(env, monkeypatch)
    assert run_wire(_args("kilocode", env[0], no_prune=True)) == 0
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["kilo_bridge"]["installed"] is True
    assert result["kilo_bridge"]["host_delivery"] == "not_verified"
    return result


def test_first_wire_update_repeat_preserve_unrelated_host_state(wire_env, monkeypatch, capsys):
    home, payload, _ = wire_env
    config = home / ".config/kilo/kilo.json"
    config.write_text(json.dumps({"theme": "mine", "mcp": {"other": {"enabled": False}}}))
    plugin_dir = config.parent / "plugin"
    plugin_dir.mkdir()
    other = plugin_dir / "other.js"
    other.write_text("// untouched unrelated plugin\n")
    first = _wire(wire_env, monkeypatch, capsys)
    bridge = Path(first["kilo_bridge"]["bridge_path"])
    before = bridge.read_bytes()
    assert first["kilo_bridge"]["hook_entry"] in before.decode()
    assert bridge.stat().st_mode & 0o777 == 0o600
    assert first["server_path"].replace("server.js", "kilocode-hook.js") == first["kilo_bridge"]["hook_entry"]
    assert '"MINNI_KILOCODE_AGENT_ID": "kilocode"' in before.decode()

    payload.rename(home / "old-payload")
    newer, manifest = _build_payload(home, "0.2.1")
    second = _wire((home, newer, manifest), monkeypatch, capsys)
    updated = bridge.read_bytes()
    assert second["kilo_bridge"]["hook_entry"] in updated.decode()
    assert first["kilo_bridge"]["hook_entry"] not in updated.decode()
    _wire((home, newer, manifest), monkeypatch, capsys)
    assert bridge.read_bytes() == updated
    actual_config = json.loads(config.read_text())
    assert actual_config["theme"] == "mine"
    assert actual_config["mcp"]["other"] == {"enabled": False}
    assert actual_config["mcp"]["minni"]["command"][1] == second["server_path"]
    assert other.read_text() == "// untouched unrelated plugin\n"
    assert not list(plugin_dir.glob("*.tmp"))


def test_missing_bridge_payload_fails_wire_without_changing_host_config(wire_env, monkeypatch, capsys):
    home, payload, _ = wire_env
    config = home / ".config/kilo/kilo.json"
    before = config.read_bytes()
    (payload / "kilo/minni-plugin.js").unlink()
    _patch_payload(wire_env, monkeypatch)
    assert run_wire(_args("kilocode", home, no_prune=True)) == 1
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["status"] == "failed"
    assert "missing its native bridge" in result["reason"]
    assert config.read_bytes() == before
    assert not (config.parent / "plugin/minni.js").exists()


@pytest.fixture
def local_install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    payload, _ = _build_payload(home)
    config = home / ".config/kilo/kilo.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"theme":"unchanged"}\n')
    return home, payload, config


def _install(home, payload):
    return kilo.install_kilo_bridge(payload, "kilocode", home / "vault", home / "socket", home / "project")


@pytest.mark.parametrize("existing", [False, True])
def test_failed_mcp_write_restores_bridge_and_config(local_install, monkeypatch, existing):
    home, payload, config = local_install
    bridge = config.parent / "plugin/minni.js"
    if existing:
        _install(home, payload)
    before = config.read_bytes()
    old_bridge = bridge.read_bytes() if bridge.exists() else None

    def partial_write(*_args):
        config.write_text("partial")
        raise OSError("injected config write failure")

    monkeypatch.setattr(kilo, "update_kilo_config", partial_write)
    with pytest.raises(OSError, match="injected config write failure"):
        _install(home, payload)
    assert config.read_bytes() == before
    assert (bridge.read_bytes() if bridge.exists() else None) == old_bridge


@pytest.mark.parametrize("content", [
    "// user-owned different plugin",
    "export const agent = process.env.MINNI_KILOCODE_AGENT_ID;",
    "export const hook = '__MINNI_KILO_HOOK_SCRIPT__';",
])
def test_unrecognized_or_symlinked_bridge_is_preserved(local_install, content):
    home, payload, config = local_install
    bridge = config.parent / "plugin/minni.js"
    bridge.parent.mkdir()
    bridge.write_text(content)
    before_config = config.read_bytes()
    with pytest.raises(ValueError, match="unrecognized plugin"):
        _install(home, payload)
    assert bridge.read_text() == content
    assert config.read_bytes() == before_config
    bridge.unlink()
    bridge.symlink_to(config)
    before = config.read_bytes()
    with pytest.raises(ValueError, match="symlinked"):
        _install(home, payload)
    assert bridge.is_symlink() and config.read_bytes() == before


def test_recognized_legacy_bridge_is_repointed(local_install):
    home, payload, config = local_install
    bridge = config.parent / "plugin/minni.js"
    bridge.parent.mkdir()
    # Old installers shipped the actual template without the managed header.
    bridge.write_bytes((payload / "kilo/minni-plugin.js").read_bytes())
    _, metadata = _install(home, payload)
    assert bridge.read_text().startswith("// Managed by minni wire kilocode.\n")
    assert metadata["hook_entry"] in bridge.read_text()


def test_installed_native_bridge_executes_stamped_hook_with_isolated_binding(local_install):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for native bridge acceptance")
    home, payload, _ = local_install
    hook = payload / "dist/kilocode-hook.js"
    # Real child execution, but only an isolated fixture hook (no daemon/vault).
    hook.write_text('''process.stdin.resume();
process.stdin.on("end", () => {
 console.log(JSON.stringify({hookSpecificOutput:{additionalContext:
  JSON.stringify({event:process.argv[2], agent:process.env.MINNI_KILOCODE_AGENT_ID,
   vault:process.env.MINNI_KILOCODE_VAULT_PATH, socket:process.env.MINNI_SOCKET_PATH})}}));
});
''')
    _, metadata = _install(home, payload)
    # .mjs gives Node20 the same ESM interpretation Kilo/Bun uses for plugins.
    module = home / "installed-bridge.mjs"
    module.write_bytes(Path(metadata["bridge_path"]).read_bytes())
    script = '''const {default: plugin} = await import(process.argv[1]);
const hooks = await plugin({directory: "workspace-fixture", client: {}});
const out = {context: []};
await hooks["experimental.session.compacting"]({sessionID: "fixture"}, out);
console.log(JSON.stringify(out));'''
    env = {**os.environ, "HOME": str(home), "MINNI_HOME": str(home / ".minni"),
           "MINNI_SOCKET_PATH": str(home / "wrong-socket"), "MINNI_KILOCODE_AGENT_ID": "wrong-agent"}
    response = subprocess.run([node, "--input-type=module", "-e", script, module.as_uri()],
                              cwd=home, env=env, text=True, capture_output=True, timeout=10, check=True)
    context = json.loads(response.stdout)["context"]
    assert len(context) == 1
    binding = json.loads(context[0])
    assert binding == {"event": "PreCompact", "agent": "kilocode",
                       "vault": str(home / "vault"), "socket": str(home / "socket")}


def test_failed_bridge_write_does_not_change_mcp(local_install, monkeypatch):
    home, payload, config = local_install
    _install(home, payload)
    bridge = config.parent / "plugin/minni.js"
    before = config.read_bytes(), bridge.read_bytes()
    original = kilo._replace_bytes
    failed = False

    def fail_once(path, content, mode=0o600):
        nonlocal failed
        if path == bridge and not failed:
            failed = True
            raise OSError("injected bridge failure")
        return original(path, content, mode)

    monkeypatch.setattr(kilo, "_replace_bytes", fail_once)
    with pytest.raises(OSError, match="injected bridge failure"):
        _install(home, payload)
    assert (config.read_bytes(), bridge.read_bytes()) == before


def test_failed_config_readback_restores_host_state(local_install, monkeypatch):
    home, payload, config = local_install
    _install(home, payload)
    bridge = config.parent / "plugin/minni.js"
    before = config.read_bytes(), bridge.read_bytes()

    def wrong_pointer(*_args):
        config.write_text('{"mcp":{"minni":{"command":["node","/wrong"]}}}')

    monkeypatch.setattr(kilo, "update_kilo_config", wrong_pointer)
    with pytest.raises(OSError, match="MCP readback"):
        _install(home, payload)
    assert (config.read_bytes(), bridge.read_bytes()) == before


def test_malformed_config_is_untouched_and_does_not_create_bridge(local_install):
    home, payload, config = local_install
    config.write_text('{"mcp":[]}')
    before = config.read_bytes()
    with pytest.raises(ValueError, match="object-valued"):
        _install(home, payload)
    assert config.read_bytes() == before
    assert not (config.parent / "plugin/minni.js").exists()


def test_explicit_available_fresh_host_creates_native_binding(wire_env, monkeypatch, capsys):
    home = wire_env[0]
    shutil.rmtree(home / '.config/kilo')
    result = _wire(wire_env, monkeypatch, capsys)
    assert Path(result['kilo_bridge']['bridge_path']).is_file()


@pytest.mark.parametrize('prior_bridge', ['absent', 'managed', 'independent'])
def test_bulk_preserves_native_activation_intent(wire_env, monkeypatch, capsys, prior_bridge):
    home, payload, _ = wire_env
    bridge = home / '.config/kilo/plugin/minni.js'
    if prior_bridge == 'managed':
        _install(home, payload)
    elif prior_bridge == 'independent':
        bridge.parent.mkdir()
        bridge.write_text('export const agent = process.env.MINNI_KILOCODE_AGENT_ID;')
    before = bridge.read_bytes() if bridge.exists() else None
    _patch_payload(wire_env, monkeypatch)
    assert run_wire(_args('all', home, no_prune=True)) == 0
    result = next(row for row in json.loads(capsys.readouterr().out)['results'] if row['platform'] == 'kilocode')
    assert result['status'] == 'wired'
    if prior_bridge == 'managed':
        assert result['kilo_bridge']['installed'] is True
        assert result['server_path'].replace('server.js', 'kilocode-hook.js') in bridge.read_text()
    else:
        assert result['kilo_bridge']['installed'] is False
        assert 'explicit wire' in result['kilo_bridge']['reason']
        assert (bridge.read_bytes() if bridge.exists() else None) == before


def test_bulk_unavailable_hosts_skip_before_node_payload_or_bootstrap(wire_env, monkeypatch, capsys):
    home = wire_env[0]
    monkeypatch.setenv('PATH', '')
    shutil.rmtree(home / 'Applications')

    def forbidden(*args, **kwargs):
        pytest.fail('unavailable fleet reached a build, Node check, or write')

    monkeypatch.setattr('minni.wire.preflight.check_node', forbidden)
    monkeypatch.setattr('minni.wire.flow.payload_tree', forbidden)
    monkeypatch.setattr('minni.wire.flow.bootstrap_vault', forbidden)
    assert run_wire(_args('all', home)) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'skipped'
    assert not (home / '.minni').exists()


def test_bulk_mixed_absent_broken_and_ready_hosts_continue(wire_env, monkeypatch, capsys):
    home = wire_env[0]
    (home.parent / 'kilo').unlink()
    broken = home / '.grok/config.toml'
    broken.write_text('invalid = [')
    _patch_payload(wire_env, monkeypatch)
    assert run_wire(_args('all', home, no_prune=True)) == 1
    output = json.loads(capsys.readouterr().out)
    rows = {row['platform']: row for row in output['results']}
    assert rows['kilocode']['status'] == 'skipped'
    assert rows['grok']['status'] == 'failed'
    assert rows['claude-code']['status'] == 'wired'
    assert broken.read_text() == 'invalid = ['
    assert not (home / '.config/kilo/plugin/minni.js').exists()

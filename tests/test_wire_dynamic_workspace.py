"""Codex global wiring preserves deliberate pins, with an explicit dynamic reset."""
import json
import tomllib
from argparse import Namespace

import pytest

from minni.minni_cli import main
from minni.wire import flow
from minni.wire.platform import platform_spec
from minni.wire.writers import update_toml_mcp_config


@pytest.fixture
def surface(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr('minni.wire.writers.DEFAULT_SOCKET', str(tmp_path / '.minni/run/minnid.sock'))
    root = tmp_path / '.minni/plugin/test'
    (root / 'dist').mkdir(parents=True)
    (root / 'dist/server.js').write_text('// fixture')
    monkeypatch.setattr(flow, 'native_afm_env', lambda repo: {})
    monkeypatch.setattr(flow, 'bootstrap_vault', lambda agent: tmp_path / '.minni' / f'{agent}-vault')

    def wire(platform='codex', workspace=None, dynamic=False):
        return flow._wire_platform(platform_spec(platform), root, 'test',
            socket=tmp_path / '.minni/run/minnid.sock', workspace=workspace,
            repo_root=None, explicit_workspace=workspace is not None, dry_run=False,
            dynamic_workspace=dynamic)

    return tmp_path, root, wire


def env(path):
    return tomllib.loads(path.read_text())['mcp_servers']['minni']['env']


def test_new_codex_is_dynamic_and_idempotent(surface):
    home, root, wire = surface
    path, _ = wire()
    first = path.read_text()
    for values in (env(path), json.loads((root / '.mcp.json').read_text())['mcpServers']['minni']['env']):
        assert 'MINNI_WORKSPACE_ID' not in values
        assert 'MINNI_CODEX_WORKSPACE_ID' not in values
        assert values['MINNI_AGENT_ID'] == values['MINNI_CODEX_AGENT_ID'] == 'codex'
    wire()
    assert path.read_text() == first


@pytest.mark.parametrize('key', ['MINNI_WORKSPACE_ID', 'MINNI_CODEX_WORKSPACE_ID'])
def test_existing_pin_preserved_then_reset_without_losing_other_env(surface, key):
    home, root, wire = surface
    config = home / '.codex/config.toml'
    config.parent.mkdir()
    helper = home / 'native-helper'
    helper.write_text('fixture')
    config.write_text('[mcp_servers.minni.env]\n' +
        f'{key} = "workspace-deliberate"\nMINNI_AGENT_ID = "codex"\nCUSTOM_OPTION = "keep"\n' +
        f'MINNI_AFM_NATIVE_HELPER = "{helper}"\nMINNI_AFM_PROVIDER_MODE = "native"\n')
    wire()
    before = env(config)
    assert before['MINNI_WORKSPACE_ID'] == before['MINNI_CODEX_WORKSPACE_ID'] == 'workspace-deliberate'
    wire(dynamic=True)
    after = env(config)
    assert 'MINNI_WORKSPACE_ID' not in after and 'MINNI_CODEX_WORKSPACE_ID' not in after
    assert after == {k: v for k, v in before.items() if k not in {'MINNI_WORKSPACE_ID', 'MINNI_CODEX_WORKSPACE_ID'}}
    wire()
    assert env(config) == after


def test_explicit_pin_overrides_preserved_pin_and_keeps_custom_env(surface):
    home, root, wire = surface
    wire(workspace=home / 'old')
    config = home / '.codex/config.toml'
    config.write_text(config.read_text() + 'CUSTOM_OPTION = "keep"\n')
    wire(workspace=home / 'new')
    assert env(config)['MINNI_WORKSPACE_ID'] == env(config)['MINNI_CODEX_WORKSPACE_ID'] == 'workspace-new'
    assert env(config)['CUSTOM_OPTION'] == 'keep'


def test_shared_root_does_not_transplant_other_host_workspace(surface):
    home, root, wire = surface
    wire('grok', workspace=home / 'grok-project')
    grok_path = home / '.grok/config.toml'
    original = grok_path.read_text()
    wire()
    stamp = json.loads((root / '.mcp.json').read_text())['mcpServers']['minni']['env']
    assert 'MINNI_WORKSPACE_ID' not in stamp
    assert 'MINNI_CODEX_WORKSPACE_ID' not in stamp
    assert grok_path.read_text() == original
    wire('grok')
    assert env(grok_path)['MINNI_WORKSPACE_ID'] == 'workspace-grok-project'
    assert env(home / '.codex/config.toml').get('MINNI_WORKSPACE_ID') is None


def test_shared_codex_stamp_reset_preserves_custom_env(surface):
    home, root, wire = surface
    wire(workspace=home / 'old')
    stamp_path = root / '.mcp.json'
    stamp = json.loads(stamp_path.read_text())
    stamp['mcpServers']['minni']['env']['CUSTOM_OPTION'] = 'keep'
    stamp_path.write_text(json.dumps(stamp))
    wire(dynamic=True)
    values = json.loads(stamp_path.read_text())['mcpServers']['minni']['env']
    assert values['CUSTOM_OPTION'] == 'keep'
    assert not any(k in values for k in ('MINNI_WORKSPACE_ID', 'MINNI_CODEX_WORKSPACE_ID'))


def test_cli_rejects_conflicting_workspace_modes():
    with pytest.raises(SystemExit) as exc:
        main(['wire', 'codex', '--workspace', '/project', '--dynamic-workspace'])
    assert exc.value.code == 2


@pytest.mark.parametrize('platform,workspace', [('all', None), ('grok', None), ('codex', '/project')])
def test_direct_flow_rejects_invalid_reset_before_install(platform, workspace, monkeypatch):
    monkeypatch.setattr(flow, 'payload_tree', lambda **kwargs: pytest.fail('must reject before any install'))
    args = Namespace(platform=platform, workspace=workspace, dynamic_workspace=True,
                     dry_run=False, verify_payload=False, prune=False, no_prune=False)
    assert flow.run_wire(args) == 2


def test_codex_dynamic_refuses_corrupt_toml_before_rewrite(surface):
    home, root, wire = surface
    path = home / 'bad.toml'
    path.write_text('[broken')
    with pytest.raises(ValueError, match='cannot parse existing TOML'):
        update_toml_mcp_config(path, root / 'dist/server.js', 'codex', home / 'vault', home / 'sock', None,
                               dynamic_workspace=True)
    assert path.read_text() == '[broken'


def test_custom_quoted_keys_and_other_server_survive_reset(surface):
    home, root, wire = surface
    config = home / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text('[mcp_servers.other]\ncommand = "untouched"\n'
        '[mcp_servers.minni.env]\n"CUSTOM.KEY" = "dot"\n"CUSTOM KEY" = "space"\n'
        '"CUSTOM\\nKEY" = "line"\nMINNI_WORKSPACE_ID = "workspace-old"\n')
    wire(dynamic=True)
    data = tomllib.loads(config.read_text())
    assert data['mcp_servers']['other'] == {'command': 'untouched'}
    values = data['mcp_servers']['minni']['env']
    assert values['CUSTOM.KEY'] == 'dot'
    assert values['CUSTOM KEY'] == 'space'
    assert values['CUSTOM\nKEY'] == 'line'
    wire()
    assert tomllib.loads(config.read_text()) == data


@pytest.mark.parametrize('value', ['true', '42', '["a"]', '{nested="value"}'])
def test_invalid_host_env_type_fails_before_shared_stamp_write(surface, value):
    home, root, wire = surface
    config = home / '.codex/config.toml'
    config.parent.mkdir()
    original = f'[mcp_servers.minni.env]\nCUSTOM = {value}\n'
    config.write_text(original)
    with pytest.raises(ValueError):
        wire(dynamic=True)
    assert config.read_text() == original
    assert not (root / '.mcp.json').exists()


@pytest.mark.parametrize('identity', [{'MINNI_CODEX_AGENT_ID': 'codex'}, {}])
def test_legacy_shared_codex_pin_is_not_mistaken_for_other_host(surface, identity):
    home, root, wire = surface
    stamp_path = root / '.mcp.json'
    stamp_path.write_text(json.dumps({'mcpServers': {'minni': {'env': {
        **identity, 'MINNI_CODEX_WORKSPACE_ID': 'workspace-legacy',
    }}}}))
    wire()
    values = json.loads(stamp_path.read_text())['mcpServers']['minni']['env']
    assert values['MINNI_WORKSPACE_ID'] == values['MINNI_CODEX_WORKSPACE_ID'] == 'workspace-legacy'
    wire(dynamic=True)
    values = json.loads(stamp_path.read_text())['mcpServers']['minni']['env']
    assert 'MINNI_WORKSPACE_ID' not in values and 'MINNI_CODEX_WORKSPACE_ID' not in values

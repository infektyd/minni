"""Optional-host decisions precede build/bootstrap and survive mixed fleets."""
from argparse import Namespace
import json
from pathlib import Path

import pytest

from tests.test_wire_propagate_honesty import _load_propagate


@pytest.fixture
def surface(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    commands = tmp_path / 'bin'
    commands.mkdir()
    monkeypatch.setenv('PATH', str(commands))
    module = _load_propagate()
    # Use actual standalone discovery, excluding the operator's application dirs.
    module.platform_update_decision('cursor')
    import sys
    discovery = sys.modules['_minni_propagate_host_discovery']
    original = discovery.discover_host
    monkeypatch.setattr(discovery, 'discover_host', lambda platform, **kw: original(
        platform, **{**kw, 'app_roots': (home / 'Applications',), 'launcher_roots': ()},
    ))
    args = Namespace(repo=str(tmp_path / 'repo'), platform='all', existing_only=False,
                     no_build=False, agent=None, install_root=None, workspace=None,
                     socket=str(home / 'socket'))
    return home, commands, module, args


def _executable(commands, name):
    path = commands / name
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)


def _config(home, relative, text):
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize('leftover', [False, True])
def test_unavailable_fleet_does_not_build_or_bootstrap(surface, monkeypatch, capsys, leftover):
    home, _, module, args = surface
    if leftover:
        _config(home, '.cursor/mcp.json', '{"mcpServers":{"minni":{}}}')
        _config(home, '.gemini/config/mcp_config.json', '{"mcpServers":{"minni":{}}}')

    def forbidden(*args, **kwargs):
        pytest.fail('unavailable host reached build/bootstrap/copy')

    for operation in ('plugin_source', 'bootstrap_vault', 'copy_tree', 'run'):
        monkeypatch.setattr(module, operation, forbidden)
    assert module.update_plugin(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'skipped'
    assert not (home / '.minni').exists()


def test_existing_only_does_not_activate_present_unconfigured_host(surface, monkeypatch, capsys):
    _, commands, module, args = surface
    _executable(commands, 'cursor')
    args.platform = 'cursor'
    args.existing_only = True
    monkeypatch.setattr(module, 'plugin_source', lambda *_: pytest.fail('new integration activated'))
    assert module.update_plugin(args) == 0
    result = json.loads(capsys.readouterr().out)['results'][0]
    assert result['status'] == 'skipped'
    assert 'explicit' in result['reason']


def test_malformed_host_failure_does_not_stop_other_host_update(surface, monkeypatch, capsys):
    home, commands, module, args = surface
    _executable(commands, 'agy')
    _executable(commands, 'cursor')
    broken = _config(home, '.gemini/config/mcp_config.json', '{bad')
    _config(home, '.cursor/mcp.json', '{"mcpServers":{"minni":{}}}')
    source = Path(args.repo) / 'plugins/minni'
    source.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(module, 'run', lambda command, **kw: calls.append(command))
    monkeypatch.setattr(module, 'bootstrap_vault', lambda *_: None)
    monkeypatch.setattr(module, 'copy_tree', lambda source, target: target.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(module, 'update_cursor_hooks', lambda *_, **kw: {'installed': True})
    monkeypatch.setattr(module, 'native_afm_env', lambda *_: {})
    assert module.update_plugin(args) == 1
    output = json.loads(capsys.readouterr().out)
    results = {row['platform']: row for row in output['results']}
    assert results['antigravity']['status'] == 'failed'
    assert results['cursor']['status'] == 'updated'
    assert output['status'] == 'partial'
    assert broken.read_text() == '{bad'
    assert sum(command == ['npm', 'run', 'build'] for command in calls) == 1
    assert args.no_build is False


@pytest.mark.parametrize('state', ['absent', 'unrelated', 'disabled'])
def test_bulk_native_hooks_preserve_absent_unrelated_disabled(surface, state):
    home, _, module, _ = surface
    path = home / '.cursor/hooks.json'
    if state != 'absent':
        data = {'hooks': {'beforeSubmitPrompt': [{'command': 'echo keep'}]}}
        if state == 'disabled':
            data = {'disabled': True, 'hooks': {'beforeSubmitPrompt': [
                {'command': 'node /old/dist/cursor-hook.js UserPromptSubmit'}]}}
        _config(home, '.cursor/hooks.json', json.dumps(data))
    before = path.read_bytes() if path.exists() else None
    result = module.update_cursor_hooks(home / 'new', existing_only=True)
    assert result['skipped'] is True
    assert (path.read_bytes() if path.exists() else None) == before
    assert not module._cursor_wrapper_path().exists()


@pytest.mark.parametrize('wrapper_state', ['missing', 'custom', 'arbitrary'])
def test_bulk_cursor_wrapper_never_created_or_overwritten(surface, wrapper_state):
    home, _, module, _ = surface
    wrapper = module._cursor_wrapper_path()
    command = module.CURSOR_WRAPPER_REL + ' UserPromptSubmit'
    if wrapper_state == 'custom':
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('# user custom wrapper\n')
    if wrapper_state == 'arbitrary':
        foreign = home / 'elsewhere/minni-cursor.sh'
        foreign.parent.mkdir()
        foreign.write_text('# unrelated executable\n')
        command = str(foreign) + ' UserPromptSubmit'
    path = _config(home, '.cursor/hooks.json', json.dumps({
        'hooks': {'beforeSubmitPrompt': [{'command': command}]}}))
    before = path.read_bytes(), wrapper.read_bytes() if wrapper.exists() else None
    module.update_cursor_hooks(home / 'new', existing_only=True)
    assert (path.read_bytes(), wrapper.read_bytes() if wrapper.exists() else None) == before
    if wrapper_state == 'arbitrary':
        assert foreign.read_text() == '# unrelated executable\n'


def test_refresh_only_actual_hook_commands_keeps_metadata_and_event_shape(surface):
    home, _, module, _ = surface
    old = 'node /old/dist/cursor-hook.js UserPromptSubmit'
    data = {'metadata': {'command': old}, 'hooks': {'beforeSubmitPrompt': [
        {'metadata': {'command': old}, 'command': old},
        {'command': 'echo untouched'},
        {'matcher': '*', 'hooks': [{'type': 'command', 'command': old}]}]}}
    path = _config(home, '.cursor/hooks.json', json.dumps(data))
    result = module.update_cursor_hooks(home / 'new', existing_only=True)
    assert result['installed'] is True
    actual = json.loads(path.read_text())
    assert actual['metadata'] == data['metadata']
    rows = actual['hooks']['beforeSubmitPrompt']
    assert rows[0]['metadata'] == data['hooks']['beforeSubmitPrompt'][0]['metadata']
    assert rows[0]['command'] == f'node {home}/new/dist/cursor-hook.js UserPromptSubmit'
    assert rows[1] == {'command': 'echo untouched'}
    assert rows[2]['matcher'] == '*'
    assert rows[2]['hooks'][0]['command'] == rows[0]['command']
    assert list(actual['hooks']) == ['beforeSubmitPrompt']


@pytest.mark.parametrize('suffix', ['>file', ';echo next', '&&echo next', '|cat'])
def test_attached_shell_operators_refuse_before_any_native_rewrite(surface, suffix):
    home, _, module, _ = surface
    path = _config(home, '.cursor/hooks.json', json.dumps({'hooks': {
        'beforeSubmitPrompt': [{'command': 'node /old/dist/cursor-hook.js UserPromptSubmit' + suffix}]}}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='native hook configuration unreadable or unsupported'):
        module.update_cursor_hooks(home / 'new', existing_only=True)
    assert path.read_bytes() == before
    assert not module._cursor_wrapper_path().exists()


def test_native_malformed_command_refuses_before_build_bootstrap(surface, monkeypatch, capsys):
    home, commands, module, args = surface
    _executable(commands, 'cursor')
    args.platform = 'cursor'
    args.existing_only = True
    _config(home, '.cursor/mcp.json', '{"mcpServers":{"minni":{}}}')
    path = _config(home, '.cursor/hooks.json', json.dumps({'hooks': {
        'beforeSubmitPrompt': [{'command': 'node /old/dist/cursor-hook.js>file'}]}}))
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('malformed native hook reached build/bootstrap/copy')
    for operation in ('run', 'bootstrap_vault', 'copy_tree'):
        monkeypatch.setattr(module, operation, forbidden)
    assert module.update_plugin(args) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['results'][0]['status'] == 'failed'
    assert path.read_bytes() == before


def test_grok_paired_preflight_keeps_hooks_when_rules_unreadable(surface, monkeypatch):
    from minni.fleet_sync import _restamp_grok_hooks
    home, commands, module, _ = surface
    _executable(commands, 'grok')
    _config(home, '.grok/config.toml', '[mcp_servers.minni]\n')
    root = home / '.minni/plugin/new'
    root.mkdir(parents=True)
    _config(home, '.minni/plugin/wired.json', json.dumps({'wires': [
        {'platform': 'grok', 'install_root': str(root), 'wired_at': '2026-09-05'}]}))
    hooks = _config(home, '.grok/hooks/minni.json', json.dumps({'hooks': {
        'SessionStart': [{'command': 'node /old/dist/grok-hook.js SessionStart'}]}}))
    rules = home / '.grok/rules/minni.md'
    rules.parent.mkdir()
    rules.write_bytes(b'\xff\xfeinvalid utf8')
    before = hooks.read_bytes(), rules.read_bytes()
    monkeypatch.setattr('minni.fleet_sync._propagate_py', lambda _: Path(module.__file__))
    result = _restamp_grok_hooks(None, dry_run=False)
    assert result['exit_code'] == 1
    assert (hooks.read_bytes(), rules.read_bytes()) == before


@pytest.mark.parametrize('platform,relative,key,entrypoint', [
    ('grok', '.grok/hooks/minni.json', 'hooks', 'grok-hook.js'),
    ('antigravity', '.gemini/config/plugins/minni/hooks.json', 'minni', 'gemini-hook.js'),
])
def test_other_native_hosts_refresh_existing_event_only_without_activation(
    surface, monkeypatch, platform, relative, key, entrypoint,
):
    home, _, module, _ = surface
    update = module.update_grok_hooks if platform == 'grok' else module.update_agy_plugin_hooks
    assert update(home / 'new', existing_only=True)['skipped'] is True
    assert not (home / relative).exists()
    path = _config(home, relative, json.dumps({key: {'SessionStart': [
        {'command': f'node /old/dist/{entrypoint} SessionStart'}, {'command': 'echo mine'}]}}))
    monkeypatch.setattr(module, 'run', lambda *a, **kw: pytest.fail('bulk installed or enabled a native plugin'))
    result = update(home / 'new', existing_only=True)
    assert result['installed'] is True
    rows = json.loads(path.read_text())[key]
    assert list(rows) == ['SessionStart']
    assert rows['SessionStart'][0]['command'] == f'node {home}/new/dist/{entrypoint} SessionStart'
    assert rows['SessionStart'][1] == {'command': 'echo mine'}


def test_unrelated_disabled_metadata_does_not_block_active_owned_hook(surface):
    home, _, module, _ = surface
    data = {'metadata': {'enabled': False}, 'hooks': {'SessionStart': [
        {'disabled': True, 'command': 'echo unrelated'},
        {'command': 'node /old/dist/cursor-hook.js SessionStart'},
        {'disabled': True, 'command': 'node /disabled/dist/cursor-hook.js SessionStart'}]}}
    target = _config(home, '.cursor/hooks.json', json.dumps(data))
    assert module.update_cursor_hooks(home / 'new', existing_only=True)['installed'] is True
    actual = json.loads(target.read_text())
    assert actual['metadata'] == data['metadata']
    rows = actual['hooks']['SessionStart']
    assert rows[0] == data['hooks']['SessionStart'][0]
    assert rows[2] == data['hooks']['SessionStart'][2]
    assert '/new/dist/' in rows[1]['command']


@pytest.mark.parametrize('custom', [False, True])
def test_historical_grok_body_refreshes_but_custom_body_is_preserved(surface, custom):
    home, _, module, _ = surface
    old = module.LEGACY_GROK_RULES_BODY + ('\nUser addition\n' if custom else '')
    target = _config(home, '.grok/rules/minni.md', old)
    module.preflight_grok_native(home / 'new')
    assert target.read_text() == old  # preflight is read-only
    result = module.write_grok_rules(existing_only=True)
    assert target.read_text() == (old if custom else module.GROK_RULES_BODY)
    assert result.get('skipped', False) is custom


def test_legacy_grok_body_matches_recorded_shipped_version(surface):
    import hashlib
    _, _, module, _ = surface
    # Independently extracted from git 1f126a81^; no Git history needed in CI.
    assert hashlib.sha256(module.LEGACY_GROK_RULES_BODY.encode()).hexdigest() == (
        'e5e8dde4b78a8ca1496028959a3ab65dfce17d627ea99f656a971a0743f9257e')

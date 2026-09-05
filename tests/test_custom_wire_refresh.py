"""Custom MCP refresh preserves host intent and confines all writes to temp HOME."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from minni.wire import custom_refresh as refresh
from minni.wire.manifest import sha256_file


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(refresh.shutil, 'which', lambda command: '/bin/' + command)
    base = tmp_path / '.minni/plugin'
    def payload(version):
        root = base / version
        (root / 'dist').mkdir(parents=True)
        (root / 'dist/server.js').write_text('console.log("fixture")')
        (root / 'payload-manifest.json').write_text(json.dumps({
            'schema': 1, 'version': version,
            'files': {'dist/server.js': sha256_file(root / 'dist/server.js')},
        }))
        return root
    old, new = payload('0.5.0+git.11111111'), payload('0.5.0+git.22222222')
    records = []
    def host(name='muse', **overrides):
        config = tmp_path / refresh._CUSTOM[name]
        config.parent.mkdir(parents=True, exist_ok=True)
        entry = {'command': 'node', 'args': [str(old / 'dist/server.js')],
                 'cwd': str(old),
                 'env': {'MINNI_AGENT_ID': name, 'MINNI_WORKSPACE_ID': 'custom-project', 'CUSTOM': 'preserve'},
                 'enabled': True, **overrides}
        config.write_text(json.dumps({'mcpServers': {'minni': entry, 'other': {'command': 'keep'}}, 'custom': [1, 2]}))
        records.append({'platform': name, 'config_path': str(config), 'install_root': str(old),
                        'version': old.name, 'workspace': 'registry-workspace', 'wired_at': 'old'})
        (base / 'wired.json').write_text(json.dumps({'schema': 1, 'generation': 0, 'wires': records}))
        return config
    return base, old, new, host


def test_refresh_preserves_configuration_and_registry_and_is_idempotent(setup):
    base, old, new, host = setup
    config = host()
    original = json.loads(config.read_text())
    result = refresh.refresh_custom_wires(new_root=setup[2])
    assert result['exit_code'] == 0
    row = result['results'][0]
    assert row['status'] == 'refreshed'
    assert json.loads(Path(row['backup']).read_text()) == original
    expected = original.copy()
    expected['mcpServers']['minni']['args'] = [str(new / 'dist/server.js')]
    expected['mcpServers']['minni']['cwd'] = str(new)
    assert json.loads(config.read_text()) == expected
    wire = json.loads((base / 'wired.json').read_text())['wires'][0]
    assert wire['install_root'] == str(new)
    assert wire['workspace'] == 'registry-workspace'
    before = config.read_bytes(), (base / 'wired.json').read_bytes()
    assert refresh.refresh_custom_wires(new_root=setup[2])['results'][0]['status'] == 'skipped'
    assert before == (config.read_bytes(), (base / 'wired.json').read_bytes())


@pytest.mark.parametrize('changes', [{'enabled': False}, {'disabled': True}, {'args': ['--inspect', 'other.js']}, {'command': 'custom-node'}, {'type': 'http'}, {'transport': 'http'}])
def test_disabled_or_unsupported_binding_is_preserved(setup, changes):
    base, _, _, host = setup
    config = host(**changes)
    before = config.read_bytes(), (base / 'wired.json').read_bytes()
    assert refresh.refresh_custom_wires(new_root=setup[2])['results'][0]['status'] == 'skipped'
    assert before == (config.read_bytes(), (base / 'wired.json').read_bytes())


def test_absent_executable_and_unknown_host_skip(setup, monkeypatch):
    base, _, _, host = setup
    config = host()
    monkeypatch.setattr(refresh.shutil, 'which', lambda command: None)
    assert refresh.refresh_custom_wires(new_root=setup[2])['results'][0]['status'] == 'skipped'
    data = json.loads((base / 'wired.json').read_text())
    data['wires'][0]['platform'] = 'unsupported'
    (base / 'wired.json').write_text(json.dumps(data))
    assert 'unsupported' in refresh.refresh_custom_wires(new_root=setup[2])['results'][0]['reason']


def test_malformed_host_fails_without_preventing_other_refresh(setup):
    _, _, new, host = setup
    bad, good = host('muse'), host('devin')
    bad.write_text('{ broken')
    result = refresh.refresh_custom_wires(new_root=setup[2])
    assert result['exit_code'] == 1
    assert [r['status'] for r in result['results']] == ['failed', 'refreshed']
    assert json.loads(good.read_text())['mcpServers']['minni']['args'] == [str(new / 'dist/server.js')]
    assert bad.read_text() == '{ broken'


def test_registry_failure_rolls_back_config(setup, monkeypatch):
    base, _, _, host = setup
    config = host()
    before = config.read_bytes(), (base / 'wired.json').read_bytes()
    def fail(*args, **kwargs):
        # Config write and readback happened before registry publication.
        assert config.read_bytes() != before[0]
        raise OSError('injected registry failure')
    monkeypatch.setattr(refresh, 'upsert_wire', fail)
    assert refresh.refresh_custom_wires(new_root=setup[2])['exit_code'] == 1
    assert before == (config.read_bytes(), (base / 'wired.json').read_bytes())


@pytest.mark.parametrize('damage', ['old_hash', 'new_hash', 'new_symlink'])
def test_unverified_payload_fails_without_config_write(setup, damage):
    base, old, new, host = setup
    config = host()
    before = config.read_bytes(), (base / 'wired.json').read_bytes()
    target = old if damage == 'old_hash' else new
    server = target / 'dist/server.js'
    if damage == 'new_symlink':
        server.unlink()
        server.symlink_to(old / 'dist/server.js')
    else:
        server.write_text('tampered')
    assert refresh.refresh_custom_wires(new_root=setup[2])['exit_code'] == 1
    assert before == (config.read_bytes(), (base / 'wired.json').read_bytes())


def test_dry_run_no_writes_and_unrelated_cwd_preserved(setup):
    base, _, _, host = setup
    config = host(cwd='/some/project')
    before = {p: p.read_bytes() for p in base.parent.parent.rglob('*') if p.is_file()}
    assert refresh.refresh_custom_wires(dry_run=True, new_root=setup[2])['results'][0]['status'] == 'dry-run'
    assert before == {p: p.read_bytes() for p in base.parent.parent.rglob('*') if p.is_file()}
    refresh.refresh_custom_wires(new_root=setup[2])
    assert json.loads(config.read_text())['mcpServers']['minni']['cwd'] == '/some/project'


def test_sync_defers_gc_and_includes_custom_failure(setup, monkeypatch):
    from minni import fleet_sync
    _, _, _, host = setup
    host().write_text('{ malformed')
    monkeypatch.setattr(fleet_sync, '_detect_install_kind', lambda: ('packaged', None))
    observed = []
    def wire(**kwargs):
        observed.append(kwargs)
        return {'name': 'wire_all', 'exit_code': 0}
    monkeypatch.setattr(fleet_sync, '_run_wire', wire)
    monkeypatch.setattr(fleet_sync, '_audit_deploy_symlinks', lambda *a, **kw: {'name': 'audit', 'exit_code': 0})
    result = fleet_sync.run_fleet_sync(prune=True, restart_daemon=False, propagate_hosts=False)
    assert observed[0]['prune'] is False
    assert result.ok is False
    assert any(s['name'] == 'payload_gc' and s['skipped'] for s in result.steps)
    assert any(s['name'] == 'custom_mcp_refresh' and s['exit_code'] == 1 for s in result.steps)


def test_refresh_caps_sibling_backups(setup):
    _, _, new, host = setup
    config = host()
    original = json.loads(config.read_text())
    leftovers = [config.parent / f'{config.name}.minni-backup-stale{i}' for i in range(3)]
    for path in leftovers:
        path.write_text('stale')
    result = refresh.refresh_custom_wires(new_root=new)
    assert result['results'][0]['status'] == 'refreshed'
    backups = list(config.parent.glob(f'{config.name}.minni-backup-*'))
    assert len(backups) == refresh._BACKUP_KEEP
    kept = Path(result['results'][0]['backup'])
    assert kept.resolve() in {path.resolve() for path in backups}
    assert json.loads(kept.read_text()) == original
    assert all(not path.exists() for path in leftovers)


def test_full_update_preserves_old_payloads_and_refreshes_custom_bindings(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts/update_root.sh'
    git_env = {**os.environ, 'GIT_BYPASS': '1'}

    def git(cwd, *args):
        return subprocess.check_output(['git', *args], cwd=str(cwd), text=True, env=git_env).strip()

    seed = tmp_path / 'seed'
    seed.mkdir()
    git(seed, 'init', '-q', '-b', 'main')
    git(seed, 'config', 'user.email', 't@example.invalid')
    git(seed, 'config', 'user.name', 't')
    (seed / 'f.txt').write_text('one\n')
    (seed / '.gitignore').write_text('.venv/\n')
    git(seed, 'add', 'f.txt', '.gitignore')
    git(seed, 'commit', '-qm', 'one')
    origin = tmp_path / 'origin.git'
    subprocess.check_call(['git', 'clone', '-q', '--bare', str(seed), str(origin)], env=git_env)
    clone = tmp_path / 'clone'
    subprocess.check_call(['git', 'clone', '-q', str(origin), str(clone)], env=git_env)
    git(clone, 'config', 'user.email', 't@example.invalid')
    git(clone, 'config', 'user.name', 't')

    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'npm').write_text('#!/bin/sh\nexit 0\n')
    (bindir / 'npm').chmod(0o755)
    (bindir / 'launchctl').write_text('#!/bin/sh\nexit 1\n')
    (bindir / 'launchctl').chmod(0o755)

    log = tmp_path / 'python-argv.log'
    venv_py = clone / '.venv' / 'bin' / 'python'
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text(
        '#!/usr/bin/env python3\n'
        'import json, sys\n'
        f'open({str(log)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
        'mod = sys.argv[sys.argv.index("-m") + 1] if "-m" in sys.argv else ""\n'
        'if mod == "minni.minni_cli":\n'
        '    print(json.dumps({"schema": 1, "status": "success", "results": [],'
        '"install_root": "/tmp/0.5.0+git.abcd", "payload_version": "0.5.0+git.abcd"}))\n'
        'elif mod == "minni.wire.custom_refresh":\n'
        '    print(json.dumps({"name": "custom_mcp_refresh", "exit_code": 0, "results": []}))\n'
    )
    venv_py.chmod(0o755)

    env = {
        **git_env,
        'PATH': f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}',
        'HOME': str(tmp_path / 'home'),
        'MINNI_SYNC_LOCKDIR': str(tmp_path / 'lock'),
    }
    (tmp_path / 'home').mkdir()

    live = subprocess.run(['bash', str(script), '--repo', str(clone)], capture_output=True, text=True, env=env)
    recorded = log.read_text() if log.exists() else ''
    assert '-m minni.minni_cli wire all --from-repo' in recorded, live.stdout + live.stderr + recorded
    assert '--no-prune --force-reinstall' in recorded
    assert '--prune --force-reinstall' not in recorded
    assert '-m minni.wire.custom_refresh --wire-report' in recorded

    dry = subprocess.run(
        ['bash', str(script), '--repo', str(clone), '--dry-run'],
        capture_output=True, text=True, env=env,
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert '-m minni.minni_cli wire all' in dry.stdout
    assert '--no-prune --force-reinstall' in dry.stdout
    assert 'minni.wire.custom_refresh --dry-run' in dry.stdout
    assert 'custom_refresh --wire-report' not in dry.stdout
    assert (log.read_text() if log.exists() else '') == recorded


def test_atomic_config_write_failure_leaves_registry_unchanged(setup, monkeypatch):
    base, _, _, host = setup
    config = host()
    before = config.read_bytes(), (base / 'wired.json').read_bytes()
    def fail(*args, **kwargs):
        raise OSError('injected write failure')
    monkeypatch.setattr(refresh, '_replace', fail)
    assert refresh.refresh_custom_wires(new_root=setup[2])['exit_code'] == 1
    assert before == (config.read_bytes(), (base / 'wired.json').read_bytes())


def test_readback_failure_rolls_back_before_registry_publication(setup, monkeypatch):
    base, _, new, host = setup
    config = host()
    before = config.read_bytes(), (base / 'wired.json').read_bytes()
    read = Path.read_bytes
    failed = False
    def readback(path):
        nonlocal failed
        content = read(path)
        if path == config and str(new).encode() in content and not failed:
            failed = True
            raise OSError('injected readback failure')
        return content
    monkeypatch.setattr(Path, 'read_bytes', readback)
    assert refresh.refresh_custom_wires(new_root=setup[2])['exit_code'] == 1
    assert failed
    assert before == (config.read_bytes(), (base / 'wired.json').read_bytes())


def test_false_entry_is_disabled(setup):
    _, _, _, host = setup
    config = host()
    config.write_text(json.dumps({'mcpServers': {'minni': False}}))
    assert refresh.refresh_custom_wires(new_root=setup[2])['results'][0]['status'] == 'skipped'
    assert json.loads(config.read_text())['mcpServers']['minni'] is False


def test_unverified_version_shaped_cwd_preserved_with_note(setup):
    base, _, _, host = setup
    cwd = str(base / '0.5.0+git.not-a-payload')
    config = host(cwd=cwd)
    result = refresh.refresh_custom_wires(new_root=setup[2])
    assert result['results'][0]['status'] == 'refreshed'
    assert 'cwd preserved' in result['results'][0]['notes'][0]
    assert json.loads(config.read_text())['mcpServers']['minni']['cwd'] == cwd


@pytest.mark.parametrize("change", ["changed", "vanished", "duplicate"])
def test_concurrent_registry_publication_preserved_and_config_rolled_back(setup, monkeypatch, change):
    base, _, _, host = setup
    config = host()
    before = config.read_bytes()
    publish = refresh.upsert_wire
    concurrent = None
    def interleave(record, **kwargs):
        nonlocal concurrent
        data = json.loads((base / 'wired.json').read_text())
        if change == 'changed':
            data['wires'][0]['workspace'] = 'newer-workspace'
        elif change == 'vanished':
            data['wires'] = []
        else:
            data['wires'].append(data['wires'][0].copy())
        concurrent = json.dumps(data).encode()
        (base / 'wired.json').write_bytes(concurrent)
        return publish(record, **kwargs)
    monkeypatch.setattr(refresh, 'upsert_wire', interleave)
    assert refresh.refresh_custom_wires(new_root=setup[2])['exit_code'] == 1
    assert config.read_bytes() == before
    assert (base / 'wired.json').read_bytes() == concurrent


def test_changed_snapshot_rejected_before_config_write(setup):
    base, _, new, host = setup
    config = host()
    registry = base / 'wired.json'
    data = json.loads(registry.read_text())
    snapshot = data['wires'][0].copy()
    data['wires'][0]['workspace'] = 'newer-workspace'
    registry.write_text(json.dumps(data))
    before = config.read_bytes()
    with pytest.raises(ValueError, match='changed during validation'):
        refresh._refresh(snapshot, new, dry_run=False)
    assert config.read_bytes() == before
    assert not list(config.parent.glob('*.minni-backup-*'))


def test_real_wire_output_schema_selects_root_without_current_symlink(setup):
    from minni.wire.output import WireOutput
    base, _, new, host = setup
    host()
    assert not (base / 'current').exists()
    report = WireOutput(status='success', payload_version=new.name, install_root=str(new))
    import contextlib
    import io
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        report.emit()
    target = refresh.wire_report_root(captured.getvalue())
    assert target == new
    assert refresh.refresh_custom_wires(new_root=target)['results'][0]['status'] == 'refreshed'


def test_missing_target_never_guesses_newest_directory(setup):
    _, _, _, host = setup
    config = host()
    before = config.read_bytes()
    assert refresh.refresh_custom_wires()['exit_code'] == 1
    assert config.read_bytes() == before


def test_fleet_wire_step_carries_actual_report_root(setup, monkeypatch):
    from minni import fleet_sync
    from minni.wire import flow
    from minni.wire.output import WireOutput
    _, _, new, _ = setup
    def run_wire(_args):
        print('build output before report')
        return WireOutput(status='success', payload_version=new.name, install_root=str(new)).emit()
    monkeypatch.setattr(flow, 'run_wire', run_wire)
    step = fleet_sync._run_wire(from_repo=None, force_reinstall=False, prune=False, dry_run=False)
    assert step['install_root'] == str(new)


@pytest.mark.parametrize('extra', [
    {'schema': 1, 'status': 'failed', 'results': [], 'install_root': None, 'payload_version': None},
    {'schema': 1, 'status': 'success', 'results': [], 'install_root': '/different/0.5.0', 'payload_version': '0.5.0'},
])
def test_conflicting_wire_documents_do_not_select_target(setup, extra):
    _, _, new, _ = setup
    document = {'schema': 1, 'status': 'success', 'results': [],
                'install_root': str(new), 'payload_version': new.name}
    assert refresh.wire_report_root(json.dumps(document) + json.dumps(extra)) is None


def test_dry_run_absent_planned_target_is_not_validated(setup):
    base, _, _, host = setup
    config = host()
    before = config.read_bytes()
    target = base / '0.5.0+git.planned'
    result = refresh.refresh_custom_wires(dry_run=True, new_root=target)
    assert result['exit_code'] == 0
    assert result['results'][0]['target_validation'] == 'not_validated'
    assert config.read_bytes() == before
    assert not target.exists()


@pytest.mark.parametrize('damage', ['config', 'old_hash', 'new_hash', 'version'])
def test_dry_run_does_not_hide_known_validation_failures(setup, damage):
    base, old, new, host = setup
    config = host()
    target = base / '0.5.0+git.planned'
    if damage == 'config':
        config.write_text('{ invalid')
    elif damage == 'old_hash':
        (old / 'dist/server.js').write_text('tampered')
    elif damage == 'new_hash':
        (new / 'dist/server.js').write_text('tampered')
        target = new
    else:
        path = new / 'payload-manifest.json'
        manifest = json.loads(path.read_text())
        manifest['version'] = '9.9.9'
        path.write_text(json.dumps(manifest))
        target = new
    before = config.read_bytes()
    assert refresh.refresh_custom_wires(dry_run=True, new_root=target)['exit_code'] == 1
    assert config.read_bytes() == before


def test_version_mismatch_rejected_on_apply(setup):
    _, _, new, host = setup
    config = host()
    before = config.read_bytes()
    path = new / 'payload-manifest.json'
    manifest = json.loads(path.read_text())
    manifest['version'] = '9.9.9'
    path.write_text(json.dumps(manifest))
    assert refresh.refresh_custom_wires(new_root=new)['exit_code'] == 1
    assert config.read_bytes() == before

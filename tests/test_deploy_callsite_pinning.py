"""Call-site pinning tests for scripts/update_root.sh and src/minni/fleet_sync.py (§4.3, §6.2)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import conftest as _conftest
from minni.fleet_sync import run_fleet_sync
from minni.wire.paths import user_home
from minni.wire.registry import resolve_home

REPO = Path(__file__).resolve().parent.parent


def test_hermetic_home_guard_never_resolves_real_home() -> None:
    """Guard test (P1-6 Step 0): fails if any home/XDG/Minni resolver returns the real home."""
    real_home = _conftest._PRE_FIXTURE_REAL_HOME
    assert real_home and os.path.isabs(real_home)

    resolved_candidates = {
        "Path.home()": os.path.realpath(str(Path.home())),
        "expanduser('~')": os.path.realpath(os.path.expanduser("~")),
        "user_home()": os.path.realpath(str(user_home())),
        "resolve_home()": os.path.realpath(str(resolve_home())),
        "env[HOME]": os.path.realpath(os.environ.get("HOME", "")),
        "env[MINNI_HOME]": os.path.realpath(os.environ.get("MINNI_HOME", "")),
        "env[XDG_CONFIG_HOME]": os.path.realpath(os.environ.get("XDG_CONFIG_HOME", "")),
        "env[XDG_DATA_HOME]": os.path.realpath(os.environ.get("XDG_DATA_HOME", "")),
        "env[XDG_CACHE_HOME]": os.path.realpath(os.environ.get("XDG_CACHE_HOME", "")),
        "env[XDG_STATE_HOME]": os.path.realpath(os.environ.get("XDG_STATE_HOME", "")),
    }

    for label, resolved in resolved_candidates.items():
        assert resolved != real_home, f"{label} resolved to real HOME ({real_home})"
        assert not resolved.startswith(real_home + os.sep), (
            f"{label} ({resolved}) resolved inside real HOME ({real_home})"
        )


def _seed_full_9_host_fleet(home: Path) -> tuple[str, str, str]:
    import json
    from minni.wire.manifest import sha256_file

    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    v1 = "0.6.0+git.11111111"
    v2 = "0.6.0+git.22222222"
    v2_sha = "2" * 40

    tools_json = json.dumps([{"name": f"minni_tool_{i}"} for i in range(44)])
    server_code = (
        "import readline from 'node:readline';\n"
        f"const TOOLS = {tools_json};\n"
        "const rl = readline.createInterface({ input: process.stdin });\n"
        "rl.on('line', (line) => {\n"
        "  if (!line.trim()) return;\n"
        "  const msg = JSON.parse(line);\n"
        "  if (msg.method === 'initialize') {\n"
        "    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result: { protocolVersion: '2024-11-05', capabilities: {}, serverInfo: { name: 'minni', version: '0.6.0' } } }) + '\\n');\n"
        "  } else if (msg.method === 'tools/list') {\n"
        "    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result: { tools: TOOLS } }) + '\\n');\n"
        "    process.exit(0);\n"
        "  }\n"
        "});\n"
    )
    for ver, sha in ((v1, "1" * 40), (v2, v2_sha)):
        vdir = plugin_dir / ver
        (vdir / "dist").mkdir(parents=True, exist_ok=True)
        (vdir / "dist" / "server.js").write_text(server_code, encoding="utf-8")
        for hname in ("hook.js", "codex-hook.js", "grok-hook.js", "gemini-hook.js", "cursor-hook.js", "kilocode-hook.js", "cli.js"):
            (vdir / "dist" / hname).write_text("process.stdout.write(JSON.stringify({ continue: true }) + '\\n');\n", encoding="utf-8")
        for sub in ("skills", "commands", "hooks"):
            (vdir / sub).mkdir(parents=True, exist_ok=True)
            (vdir / sub / "marker.txt").write_text(ver, encoding="utf-8")
        (vdir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (vdir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "minni", "version": "0.6.0"}), encoding="utf-8")
        (vdir / "package.json").write_text(json.dumps({"name": "minni-multi-plugin", "version": "0.6.0", "type": "module"}), encoding="utf-8")
        (vdir / "payload-manifest.json").write_text(
            json.dumps({
                "schema": 1,
                "version": ver,
                "git_sha": sha,
                "built_at": "2026-10-31T12:00:00Z",
                "node_engine": ">=20",
                "files": {"dist/server.js": sha256_file(vdir / "dist" / "server.js")},
            }),
            encoding="utf-8",
        )

    stale_srv = str(plugin_dir / v1 / "dist" / "server.js")

    # 1. claude-code
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"minni": {"command": "node", "args": [stale_srv], "env": {"MINNI_AGENT_ID": "claude-code", "MINNI_VAULT_PATH": "~/.minni/claudecode-vault"}}}}),
        encoding="utf-8",
    )
    # 2. claude-desktop
    cd_cfg = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    cd_cfg.parent.mkdir(parents=True, exist_ok=True)
    cd_cfg.write_text(
        json.dumps({"mcpServers": {"minni": {"command": "node", "args": [stale_srv], "env": {"MINNI_AGENT_ID": "claude-code", "MINNI_VAULT_PATH": "~/.minni/claudecode-vault"}}}}),
        encoding="utf-8",
    )
    # 3. codex
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text(
        f'[mcp_servers.minni]\ncommand = "node"\nargs = ["{stale_srv}"]\n\n[mcp_servers.minni.env]\nMINNI_AGENT_ID = "codex"\nMINNI_VAULT_PATH = "~/.minni/codex-vault"\n',
        encoding="utf-8",
    )
    # 4. grok-build
    (home / ".grok").mkdir(parents=True, exist_ok=True)
    (home / ".grok" / "config.toml").write_text(
        f'[mcp_servers.minni]\ncommand = "node"\nargs = ["{stale_srv}"]\n\n[mcp_servers.minni.env]\nMINNI_AGENT_ID = "grok-build"\nMINNI_VAULT_PATH = "~/.minni/grok-build-vault"\n',
        encoding="utf-8",
    )
    # 5. antigravity
    (home / ".agents" / "plugins").mkdir(parents=True, exist_ok=True)
    (home / ".gemini").mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"mcpServers": {"minni": {"command": "node", "args": [stale_srv], "env": {"MINNI_AGENT_ID": "gemini", "MINNI_VAULT_PATH": "~/.minni/gemini-vault"}}}}),
        encoding="utf-8",
    )
    # 6. cursor
    (home / ".cursor" / "plugins" / "local" / "minni").mkdir(parents=True, exist_ok=True)
    (home / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"minni": {"command": "node", "args": [stale_srv], "env": {"MINNI_AGENT_ID": "cursor", "MINNI_VAULT_PATH": "~/.minni/cursor-vault"}}}}),
        encoding="utf-8",
    )
    # 7. kilocode
    (home / ".config" / "kilo").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "kilo" / "kilo.json").write_text(
        json.dumps({"mcp": {"minni": {"type": "local", "command": ["node", stale_srv], "enabled": True, "environment": {"MINNI_AGENT_ID": "kilocode", "MINNI_VAULT_PATH": "~/.minni/kilocode-vault"}}}}),
        encoding="utf-8",
    )
    # 8. devin
    (home / ".config" / "devin").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "devin" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"minni": {"command": "node", "args": [stale_srv], "env": {"MINNI_AGENT_ID": "devin", "MINNI_VAULT_PATH": "~/.minni/devin-vault"}}}}),
        encoding="utf-8",
    )
    # 9. muse
    (home / ".config" / "muse").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "muse" / "settings.json").write_text(
        json.dumps({"mcpServers": {"minni": {"command": "node", "args": [stale_srv], "env": {"MINNI_AGENT_ID": "muse", "MINNI_VAULT_PATH": "~/.minni/muse-vault"}}}}),
        encoding="utf-8",
    )
    return v1, v2, v2_sha


def test_update_root_and_fleet_sync_invoke_atomic_cutover_and_behavioral_probe(
    tmp_path: Path, monkeypatch
) -> None:
    import minni.wire.cutover as cutover_mod
    from minni.wire.health import audit_host_propagation_health
    from minni.wire.registry import HOST_SURFACE_CATALOG

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("MINNI_HOME", str(fake_home / ".minni"))

    v1, v2, v2_sha = _seed_full_9_host_fleet(fake_home)
    v2_root = str(fake_home / ".minni" / "plugin" / v2)

    probe_calls: list[Path] = []
    orig_probe = cutover_mod.run_behavioral_probe

    def _spy_probe(version_dir: Path, **kw):
        probe_calls.append(version_dir)
        return orig_probe(version_dir, **kw)

    monkeypatch.setattr(cutover_mod, "run_behavioral_probe", _spy_probe)
    monkeypatch.setattr(cutover_mod, "_detect_running_rewrite_hosts", lambda: set())

    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0, "status": "ok", "install_root": v2_root}),
        patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0}),
        patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0}),
        patch("minni.fleet_sync._restamp_grok_hooks", return_value={"name": "grok_hooks_rules", "exit_code": 0}),
        patch("minni.fleet_sync._audit_deploy_symlinks", return_value={"name": "deploy_symlink_audit", "exit_code": 0}),
    ):
        res = run_fleet_sync(dry_run=False)

    assert res.ok is True, res.steps
    assert len(probe_calls) >= 3  # target vdir + 2 scratch containers

    # Verify cutover ran BEFORE custom_mcp_refresh
    step_names = [s.get("name") for s in res.steps]
    assert step_names.index("reconcile_all_hosts") < step_names.index("custom_mcp_refresh")

    reconcile_step = next(s for s in res.steps if s.get("name") == "reconcile_all_hosts")
    updated_hosts = {item.split(":", 1)[0] for item in reconcile_step.get("updated_surfaces", [])}
    assert updated_hosts == set(HOST_SURFACE_CATALOG.keys())

    health = audit_host_propagation_health(home=fake_home, expected_git_sha=v2_sha)
    assert health.ok is True, health.to_dict()


def test_lockfile_and_running_process_guards_defer_with_exit_3_before_any_byte_change(
    tmp_path: Path, monkeypatch
) -> None:
    from minni.wire.cutover import execute_atomic_cutover

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("MINNI_HOME", str(fake_home / ".minni"))

    _, v2, v2_sha = _seed_full_9_host_fleet(fake_home)

    # Create active lockfiles for claude-code, grok-build, and muse
    (fake_home / ".claude.json.tmp.99999").write_text("in-flight", encoding="utf-8")
    (fake_home / ".grok" / ".config.toml.lock").write_text("locked", encoding="utf-8")
    (fake_home / ".config" / "muse" / ".settings.json.lock").write_text("locked", encoding="utf-8")

    protected_paths = {
        "claude-code": fake_home / ".claude.json",
        "grok-build": fake_home / ".grok" / "config.toml",
        "muse": fake_home / ".config" / "muse" / "settings.json",
        "devin": fake_home / ".config" / "devin" / "mcp_config.json",
    }
    before_bytes = {h: p.read_bytes() for h, p in protected_paths.items()}

    outcome = execute_atomic_cutover(
        home=fake_home,
        target_version=v2,
        target_git_sha=v2_sha,
        running_hosts={"devin"},
    )
    assert outcome.exit_code == 3
    assert outcome.status == "DEFERRED_RUNNING_HOST_DELTA"
    for h, p in protected_paths.items():
        assert h in outcome.deferred_running_hosts
        assert str(p) in outcome.message
        assert p.read_bytes() == before_bytes[h]


def test_fleet_sync_and_update_root_stop_on_cutover_exit_1_or_3_without_continuing(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess
    import minni.wire.cutover as cutover_mod

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("MINNI_HOME", str(fake_home / ".minni"))

    _, v2, _ = _seed_full_9_host_fleet(fake_home)
    v2_root = str(fake_home / ".minni" / "plugin" / v2)

    # 1. fleet_sync with running host (exit 3) MUST stop before propagate!
    monkeypatch.setattr(cutover_mod, "_detect_running_rewrite_hosts", lambda: {"muse"})
    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0, "status": "ok", "install_root": v2_root}),
        patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0}) as mock_prop_3,
        patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0}),
        patch("minni.fleet_sync._restamp_grok_hooks", return_value={"name": "grok_hooks_rules", "exit_code": 0}),
        patch("minni.fleet_sync._audit_deploy_symlinks", return_value={"name": "deploy_symlink_audit", "exit_code": 0}),
    ):
        res3 = run_fleet_sync(dry_run=False)
    assert res3.ok is False
    mock_prop_3.assert_not_called()
    rec_step3 = next(s for s in res3.steps if s.get("name") == "reconcile_all_hosts")
    assert rec_step3["exit_code"] == 3

    # 2. fleet_sync with unparseable config (exit 1) MUST stop before propagate!
    monkeypatch.setattr(cutover_mod, "_detect_running_rewrite_hosts", lambda: set())
    (fake_home / ".codex" / "config.toml").write_text("[mcp_servers.minni\nbroken =", encoding="utf-8")
    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0, "status": "ok", "install_root": v2_root}),
        patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0}) as mock_prop_1,
        patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0}),
        patch("minni.fleet_sync._restamp_grok_hooks", return_value={"name": "grok_hooks_rules", "exit_code": 0}),
        patch("minni.fleet_sync._audit_deploy_symlinks", return_value={"name": "deploy_symlink_audit", "exit_code": 0}),
    ):
        res1 = run_fleet_sync(dry_run=False)
    assert res1.ok is False
    mock_prop_1.assert_not_called()
    rec_step1 = next(s for s in res1.steps if s.get("name") == "reconcile_all_hosts")
    assert rec_step1["exit_code"] == 1

    # 3. scripts/update_root.sh must exit 3 on cutover exit 3 and exit 1 on cutover exit 1 without "continuing"
    update_root_text = (REPO / "scripts" / "update_root.sh").read_text(encoding="utf-8")
    assert "atomic cutover / per-host reconcile exited $_CUTOVER_RC — continuing" not in update_root_text
    assert "exit 3" in update_root_text

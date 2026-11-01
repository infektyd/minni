"""Tests for §4.3 7-step atomic cutover, pre-flip probes, CAS rollback, and exit code 3."""

from __future__ import annotations

import json
import os
from pathlib import Path

from minni.wire.cutover import CutoverOutcome, execute_atomic_cutover, reconcile_all_hosts
from minni.wire.manifest import sha256_file


def _seed_plugin_version(base: Path, ver: str, sha: str, *, broken_server: bool = False) -> Path:
    vdir = base / ver
    (vdir / "dist").mkdir(parents=True, exist_ok=True)
    if broken_server:
        (vdir / "dist" / "server.js").write_text("process.exit(42);\n", encoding="utf-8")
    else:
        # Minimal stdio MCP responder for initialize + tools/list (>= 40 tools)
        tools_json = json.dumps([{"name": f"minni_tool_{i}"} for i in range(44)])
        (vdir / "dist" / "server.js").write_text(
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
            "});\n",
            encoding="utf-8",
        )
    (vdir / "dist" / "hook.js").write_text(
        "process.stdout.write(JSON.stringify({ continue: true }) + '\\n');\n",
        encoding="utf-8",
    )
    (vdir / "dist" / "minni-afm-helper").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (vdir / "dist" / "minni-afm-helper").chmod(0o755)
    for sub in ("skills", "commands", "hooks"):
        (vdir / sub).mkdir(parents=True, exist_ok=True)
        (vdir / sub / "marker.txt").write_text(ver, encoding="utf-8")
    (vdir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (vdir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "minni", "version": "0.6.0"}),
        encoding="utf-8",
    )
    (vdir / "package.json").write_text(
        json.dumps({"name": "minni-multi-plugin", "version": "0.6.0", "type": "module"}),
        encoding="utf-8",
    )
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
    return vdir


def test_cutover_preflip_abort_real_dir_current_rollback_cas_restore_and_exit_3(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)

    # 1. Seed a pre-existing real directory at `current` (P2-6) + old version v1
    v1_ver = "0.6.0+git.11111111"
    v1_sha = "1" * 40
    _seed_plugin_version(plugin_dir, v1_ver, v1_sha)

    real_current = plugin_dir / "current"
    real_current.mkdir(parents=True)
    (real_current / "legacy_marker.txt").write_text("real-dir-current", encoding="utf-8")

    # Seed an existing Cursor container with real dist/ directory
    cursor_container = home / ".cursor" / "plugins" / "local" / "minni"
    (cursor_container / "dist").mkdir(parents=True)
    (cursor_container / "dist" / "old.txt").write_text("old-cursor-dist", encoding="utf-8")

    # Part A: Pre-flip behavioral probe failure aborts BEFORE flipping `current` or touching live containers (R4-2)
    broken_ver = "0.6.0+git.bad00000"
    _seed_plugin_version(plugin_dir, broken_ver, "b" * 40, broken_server=True)
    abort_res: CutoverOutcome = execute_atomic_cutover(
        home=home,
        target_version=broken_ver,
        target_git_sha="b" * 40,
    )
    assert abort_res.exit_code == 1
    assert abort_res.aborted_pre_flip is True
    # `current` is STILL the untouched real directory!
    assert not real_current.is_symlink()
    assert (real_current / "legacy_marker.txt").read_text(encoding="utf-8") == "real-dir-current"
    # Scratch `.probe.` dirs cleaned up
    assert not list((home / ".cursor" / "plugins" / "local").glob(".minni.probe.*"))

    # Part B & C: Step 4 converts real-dir `current` -> symlink and swaps container subtrees;
    # Step 5 updates `~/.claude.json`; concurrent mutation happens before Step 6 fails ->
    # Step 6 rolls back `current` to real directory via unlink+rename (no ENOTDIR, R4-1),
    # restores `<item>.prev.<pid>`, and performs key-scoped CAS restore on `~/.claude.json` (N5).
    claude_json = home / ".claude.json"
    old_minni_entry = {
        "command": "node",
        "args": [str(plugin_dir / v1_ver / "dist" / "server.js")],
        "env": {
            "MINNI_AGENT_ID": "claude-code",
            "MINNI_WORKSPACE_ID": "legacy-pin",
            "MINNI_AFM_NATIVE_HELPER": str(plugin_dir / v1_ver / "dist" / "minni-afm-helper"),
        },
    }
    claude_json.write_text(
        json.dumps({"mcpServers": {"minni": old_minni_entry}, "oauthAccount": "initial"}),
        encoding="utf-8",
    )

    v2_ver = "0.6.0+git.22222222"
    v2_sha = "2" * 40
    _seed_plugin_version(plugin_dir, v2_ver, v2_sha)

    def _concurrent_claude_mutation_then_fail(_home: Path) -> bool:
        # Simulate live Claude Code writing to ~/.claude.json after Step 5
        doc = json.loads(claude_json.read_text(encoding="utf-8"))
        doc["oauthAccount"] = "concurrently-updated-by-live-session"
        doc["numStartups"] = 99
        claude_json.write_text(json.dumps(doc), encoding="utf-8")
        return False  # Trigger Step 6 rollback!

    rb_res = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        post_flip_verifier=_concurrent_claude_mutation_then_fail,
    )
    assert rb_res.exit_code == 1
    assert rb_res.rolled_back is True
    # `current` restored to real directory without ENOTDIR!
    assert real_current.is_dir() and not real_current.is_symlink()
    assert (real_current / "legacy_marker.txt").read_text(encoding="utf-8") == "real-dir-current"
    # Cursor container `dist` restored to real directory with old.txt!
    assert (cursor_container / "dist").is_dir() and not (cursor_container / "dist").is_symlink()
    assert (cursor_container / "dist" / "old.txt").read_text(encoding="utf-8") == "old-cursor-dist"
    # `~/.claude.json` was NOT overwritten on rollback CAS mismatch; live bytes stay intact and conflict is archived!
    restored_claude = json.loads(claude_json.read_text(encoding="utf-8"))
    assert restored_claude["oauthAccount"] == "concurrently-updated-by-live-session"
    assert restored_claude["numStartups"] == 99
    assert rb_res.cas_conflicts
    assert any("rollback-conflict" in c for c in rb_res.cas_conflicts)

    # Part D: Running rewrite-on-run host (`muse`) with `delta != 0` defers write and exits 3 (R4-4)
    muse_cfg = home / ".muse" / "config.json"
    muse_cfg.parent.mkdir(parents=True, exist_ok=True)
    muse_original = json.dumps({
        "mcpServers": {
            "minni": {
                "command": "node",
                "args": [str(plugin_dir / v1_ver / "dist" / "server.js")],
                "env": {
                    "MINNI_AGENT_ID": "muse",
                    "MINNI_WORKSPACE_ID": "stale-pin",
                    "MINNI_AFM_NATIVE_HELPER": str(plugin_dir / v1_ver / "dist" / "minni-afm-helper"),
                },
            }
        }
    })
    muse_cfg.write_text(muse_original, encoding="utf-8")

    # Run cutover with muse reported as running
    res_deferred = reconcile_all_hosts(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        dry_run=False,
        running_hosts={"muse"},
    )
    assert res_deferred.exit_code == 3
    assert "muse" in res_deferred.deferred_running_hosts
    # muse config was NOT touched while running
    assert muse_cfg.read_text(encoding="utf-8") == muse_original
    # Meanwhile `current` and `~/.claude.json` converged to v2 / current!
    assert real_current.is_symlink()
    assert os.readlink(real_current) == v2_ver
    converged_claude = json.loads(claude_json.read_text(encoding="utf-8"))
    assert converged_claude["mcpServers"]["minni"]["args"] == [
        str(plugin_dir / "current" / "dist" / "server.js")
    ]
    assert "MINNI_WORKSPACE_ID" not in converged_claude["mcpServers"]["minni"]["env"]
    # And v1_ver was protected from GC because deferred muse still references it!
    assert (plugin_dir / v1_ver).is_dir()


def test_step5_reconciles_all_catalog_surface_kinds_and_fails_closed_on_unparseable_or_residual_delta(
    tmp_path: Path,
) -> None:
    import tomllib
    from minni.wire.health import audit_host_propagation_health

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)

    v1_ver = "0.6.0+git.11111111"
    v2_ver = "0.6.0+git.22222222"
    v2_sha = "2" * 40
    _seed_plugin_version(plugin_dir, v1_ver, "1" * 40)
    v2_dir = _seed_plugin_version(plugin_dir, v2_ver, v2_sha)
    for hook_name in ("codex-hook.js", "grok-hook.js", "gemini-hook.js", "cursor-hook.js", "kilocode-hook.js"):
        (v2_dir / "dist" / hook_name).write_text("process.exit(0);\n", encoding="utf-8")
    (v2_dir / "dist" / "cli.js").write_text("process.exit(0);\n", encoding="utf-8")

    # Seed non-mcp_json catalog surfaces with stale v1_ver / ~ paths
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        f'[mcp_servers.minni]\ncommand = "node"\nargs = ["{plugin_dir / v1_ver / "dist/server.js"}"]\n'
        f'cwd = "{plugin_dir / v1_ver}"\n\n'
        f'[mcp_servers.minni.env]\nMINNI_AGENT_ID = "codex"\n'
        f'MINNI_VAULT_PATH = "~/.minni/codex-vault"\nMINNI_WORKSPACE_ID = "stale"\n',
        encoding="utf-8",
    )
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"command": f"node {plugin_dir / v1_ver / 'dist/codex-hook.js'}"}]}}),
        encoding="utf-8",
    )
    codex_skill = codex_dir / "skills" / "astra-minni" / "SKILL.md"
    codex_skill.parent.mkdir(parents=True)
    codex_skill.write_text(f"Run `{plugin_dir / v1_ver / 'dist/cli.js'}`\n", encoding="utf-8")

    grok_dir = home / ".grok"
    (grok_dir / "hooks").mkdir(parents=True)
    (grok_dir / "rules").mkdir(parents=True)
    (grok_dir / "config.toml").write_text(
        f'[mcp_servers.minni]\ncommand = "node"\nargs = ["{plugin_dir / v1_ver / "dist/server.js"}"]\n\n'
        f'[mcp_servers.minni.env]\nMINNI_AGENT_ID = "grok-build"\nMINNI_VAULT_PATH = "~/.minni/grok-build-vault"\n',
        encoding="utf-8",
    )
    (grok_dir / "hooks" / "minni.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"command": f"node {plugin_dir / v1_ver / 'dist/grok-hook.js'}"}]}}),
        encoding="utf-8",
    )
    (grok_dir / "rules" / "minni.md").write_text(
        f"CLI at {plugin_dir / v1_ver / 'dist/cli.js'}\n",
        encoding="utf-8",
    )

    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    minni_mcp = local_bin / "minni-mcp"
    minni_mcp.write_text(f"#!/usr/bin/env bash\nnode {plugin_dir / v1_ver / 'dist/server.js'} \"$@\"\n", encoding="utf-8")
    minni_mcp.chmod(0o755)

    cursor_hooks = home / ".cursor" / "hooks"
    cursor_hooks.mkdir(parents=True)
    (home / ".cursor" / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"command": f"{plugin_dir / v1_ver / 'hooks/minni-cursor.sh'}"}]}}),
        encoding="utf-8",
    )
    cursor_sh = cursor_hooks / "minni-cursor.sh"
    cursor_sh.write_text(
        f"#!/usr/bin/env bash\nnode {plugin_dir / v1_ver / 'dist/cursor-hook.js'} \"$@\"\n",
        encoding="utf-8",
    )
    cursor_sh.chmod(0o755)

    devin_scripts = home / ".config" / "devin" / "scripts"
    devin_scripts.mkdir(parents=True)
    (home / ".config" / "devin" / "config.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"command": "~/.config/devin/scripts/minni-hook.sh"}]}}),
        encoding="utf-8",
    )
    devin_sh = devin_scripts / "minni-hook.sh"
    devin_sh.write_text(f"#!/usr/bin/env bash\nnode {plugin_dir / v1_ver / 'dist/hook.js'} \"$@\"\n", encoding="utf-8")
    devin_sh.chmod(0o755)

    kilo_plugin = home / ".config" / "kilo" / "plugin"
    kilo_plugin.mkdir(parents=True)
    (kilo_plugin / "minni.js").write_text(
        "// Managed by minni wire kilocode.\n"
        f'const __MINNI_KILO_HOOK_SCRIPT__ = "{plugin_dir / v1_ver / "dist/kilocode-hook.js"}";\n',
        encoding="utf-8",
    )

    ag_views = home / ".agents" / "mcp-servers" / "views"
    ag_views.mkdir(parents=True)
    view_file = ag_views / ".gemini__config__mcp_config.json.json"
    view_file.write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(plugin_dir / v1_ver / "dist/server.js")],
                    "env": {"MINNI_AGENT_ID": "gemini", "MINNI_VAULT_PATH": "~/.minni/gemini-vault"},
                }
            }
        }),
        encoding="utf-8",
    )
    ag_hooks = home / ".gemini" / "config" / "plugins" / "minni"
    ag_hooks.mkdir(parents=True)
    (ag_hooks / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"command": f"node {plugin_dir / v1_ver / 'dist/gemini-hook.js'}"}]}}),
        encoding="utf-8",
    )
    ag_cmds = home / ".gemini" / "commands"
    ag_cmds.mkdir(parents=True)
    os.symlink(str(plugin_dir / v1_ver / "commands"), ag_cmds / "minni")

    res = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert res.exit_code == 0, res.message

    codex_toml = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
    assert codex_toml["mcp_servers"]["minni"]["args"] == [str(plugin_dir / "current" / "dist" / "server.js")]
    assert "cwd" not in codex_toml["mcp_servers"]["minni"]
    assert "MINNI_WORKSPACE_ID" not in codex_toml["mcp_servers"]["minni"]["env"]
    assert codex_toml["mcp_servers"]["minni"]["env"]["MINNI_VAULT_PATH"] == str(home / ".minni" / "codex-vault")
    assert "exec node" in (codex_dir / "hooks.json").read_text(encoding="utf-8")
    assert v1_ver not in (codex_dir / "hooks.json").read_text(encoding="utf-8")
    assert v1_ver not in codex_skill.read_text(encoding="utf-8")
    assert v1_ver not in (grok_dir / "config.toml").read_text(encoding="utf-8")
    assert "exec node" in (grok_dir / "hooks" / "minni.json").read_text(encoding="utf-8")
    assert v1_ver not in (grok_dir / "rules" / "minni.md").read_text(encoding="utf-8")
    assert "exec node" in minni_mcp.read_text(encoding="utf-8")
    assert v1_ver not in minni_mcp.read_text(encoding="utf-8")
    assert "exec node" in cursor_sh.read_text(encoding="utf-8")
    assert v1_ver not in cursor_sh.read_text(encoding="utf-8")
    assert "~/" not in (home / ".config" / "devin" / "config.json").read_text(encoding="utf-8")
    assert "exec node" in devin_sh.read_text(encoding="utf-8")
    assert v1_ver not in (kilo_plugin / "minni.js").read_text(encoding="utf-8")
    assert v1_ver not in view_file.read_text(encoding="utf-8")
    assert os.readlink(ag_cmds / "minni") == str(plugin_dir / "current" / "commands")

    health = audit_host_propagation_health(home=home, expected_git_sha=v2_sha)
    assert health.ok is True, health.to_dict()

    # Corrupting a surface (unparseable TOML) MUST fail closed (exit 1 + rollback) naming the host
    (codex_dir / "config.toml").write_text("[mcp_servers.minni\nbroken =", encoding="utf-8")
    bad_res = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert bad_res.exit_code == 1
    assert bad_res.rolled_back is True
    assert "codex" in bad_res.message


def test_forward_write_cas_and_step7a_rollback_cas_protect_concurrent_edits_on_all_hosts(
    tmp_path: Path, monkeypatch
) -> None:
    import minni.wire.cutover as cutover_mod

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)
    v1_ver = "0.6.0+git.11111111"
    v2_ver = "0.6.0+git.22222222"
    v2_sha = "2" * 40
    _seed_plugin_version(plugin_dir, v1_ver, "1" * 40)
    _seed_plugin_version(plugin_dir, v2_ver, v2_sha)

    kilo_cfg = home / ".config" / "kilo" / "kilo.json"
    kilo_cfg.parent.mkdir(parents=True)
    kilo_cfg.write_text(
        json.dumps({
            "mcp": {
                "minni": {
                    "type": "local",
                    "command": ["node", str(plugin_dir / v1_ver / "dist" / "server.js")],
                    "enabled": True,
                    "environment": {
                        "MINNI_AGENT_ID": "kilocode",
                        "MINNI_VAULT_PATH": str(home / ".minni" / "kilocode-vault"),
                    },
                }
            },
            "theme": "initial",
        }),
        encoding="utf-8",
    )

    # 1. Step 7a rollback CAS on a host with rewrite_on_run=False / cas_on_delta:
    # If kilo.json is modified concurrently after Step 5 and Step 6 fails, rollback MUST NOT clobber "theme"
    def _concurrent_kilo_edit_then_fail(_home: Path) -> bool:
        doc = json.loads(kilo_cfg.read_text(encoding="utf-8"))
        doc["theme"] = "user-edited-during-cutover"
        kilo_cfg.write_text(json.dumps(doc), encoding="utf-8")
        return False

    rb_res = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
        post_flip_verifier=_concurrent_kilo_edit_then_fail,
    )
    assert rb_res.exit_code == 1
    assert rb_res.rolled_back is True
    live_kilo = json.loads(kilo_cfg.read_text(encoding="utf-8"))
    assert live_kilo["theme"] == "user-edited-during-cutover"
    assert rb_res.cas_conflicts

    # 2. Forward-write CAS: if kilo.json changes between pre-diff read and Step 5 replace,
    # cutover MUST abort, roll back, and leave the concurrent edit in kilo.json untouched!
    kilo_cfg.write_text(
        json.dumps({
            "mcp": {
                "minni": {
                    "type": "local",
                    "command": ["node", str(plugin_dir / v1_ver / "dist" / "server.js")],
                    "enabled": True,
                    "environment": {
                        "MINNI_AGENT_ID": "kilocode",
                        "MINNI_VAULT_PATH": str(home / ".minni" / "kilocode-vault"),
                    },
                }
            },
            "theme": "before-forward-cas",
        }),
        encoding="utf-8",
    )
    orig_archive = cutover_mod._archive_file_before_write

    def _mutate_during_forward_write(h: Path, p: Path, ts: str):
        res_arch = orig_archive(h, p, ts)
        if p == kilo_cfg:
            doc = json.loads(kilo_cfg.read_text(encoding="utf-8"))
            doc["theme"] = "concurrent-forward-edit"
            kilo_cfg.write_text(json.dumps(doc), encoding="utf-8")
        return res_arch

    monkeypatch.setattr(cutover_mod, "_archive_file_before_write", _mutate_during_forward_write)
    fw_res = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert fw_res.exit_code == 1
    assert fw_res.rolled_back is True
    assert fw_res.cas_conflicts
    assert json.loads(kilo_cfg.read_text(encoding="utf-8"))["theme"] == "concurrent-forward-edit"


def test_preflip_probe_fails_on_timeout_strips_parent_env_secrets_and_probes_scratch_containers(
    tmp_path: Path, monkeypatch
) -> None:
    import minni.wire.cutover as cutover_mod
    from minni.wire.cutover import run_behavioral_probe

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)

    # 1. Timeout in server.js MUST fail (never returncode=0)
    hang_ver = "0.6.0+git.hang0000"
    hang_dir = _seed_plugin_version(plugin_dir, hang_ver, "a" * 40)
    (hang_dir / "dist" / "server.js").write_text("setInterval(() => {}, 10000);\n", encoding="utf-8")
    ok, msg = run_behavioral_probe(hang_dir, timeout=0.3)
    assert ok is False
    assert "timed out" in msg.lower()

    # 2. Parent environment secrets MUST NOT leak into probe subprocess (P3-12)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "top-secret-aws-key")
    sec_ver = "0.6.0+git.sec00000"
    sec_dir = _seed_plugin_version(plugin_dir, sec_ver, "c" * 40)
    orig_server = (sec_dir / "dist" / "server.js").read_text(encoding="utf-8")
    (sec_dir / "dist" / "server.js").write_text(
        "if (process.env.AWS_SECRET_ACCESS_KEY) { process.exit(99); }\n" + orig_server,
        encoding="utf-8",
    )
    ok_sec, msg_sec = run_behavioral_probe(sec_dir)
    assert ok_sec is True, msg_sec

    # 3. Scratch container probe: if scratch container server.js fails MCP handshake, abort pre-flip!
    orig_stage = cutover_mod.stage_scratch_containers

    def _stage_with_broken_cursor_scratch(vdir: Path, **kw):
        sroot, sdirs = orig_stage(vdir, **kw)
        cu_dist = sdirs["cursor"] / "dist"
        if cu_dist.is_symlink():
            cu_dist.unlink()
            cu_dist.mkdir(parents=True)
        (cu_dist / "server.js").write_text("process.exit(55);\n", encoding="utf-8")
        return sroot, sdirs

    monkeypatch.setattr(cutover_mod, "stage_scratch_containers", _stage_with_broken_cursor_scratch)
    sc_res = execute_atomic_cutover(
        home=home,
        target_version=sec_ver,
        target_git_sha="c" * 40,
        running_hosts=set(),
    )
    assert sc_res.exit_code == 1
    assert sc_res.aborted_pre_flip is True


def test_claude_installed_plugins_writes_base_semver_and_is_zero_delta_idempotent(
    tmp_path: Path,
) -> None:
    from minni.wire.health import audit_host_propagation_health

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)
    (home / ".claude" / "plugins").mkdir(parents=True)

    v2_ver = "0.6.0+git.22222222"
    v2_sha = "2" * 40
    _seed_plugin_version(plugin_dir, v2_ver, v2_sha)

    res1 = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert res1.exit_code == 0
    inst_path = home / ".claude" / "plugins" / "installed_plugins.json"
    doc1 = json.loads(inst_path.read_text(encoding="utf-8"))
    entry1 = doc1["plugins"]["minni@minni-local"][0]
    assert entry1["version"] == "0.6.0"
    assert entry1["gitCommitSha"] == v2_sha

    health = audit_host_propagation_health(home=home, expected_git_sha=v2_sha)
    assert health.ok is True, health.to_dict()

    bytes1 = inst_path.read_bytes()
    mtime1 = inst_path.stat().st_mtime_ns

    res2 = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert res2.exit_code == 0
    assert inst_path.read_bytes() == bytes1
    assert inst_path.stat().st_mtime_ns == mtime1


def test_r3_p1_1_plugin_container_only_and_missing_minni_key_reconciled_and_audited(
    tmp_path: Path,
) -> None:
    """P1-1:
    1. A host with only a `plugin_container` surface (e.g. cursor container) is scored in
       `updated_surfaces` / `skipped_surfaces` and audited by `audit_host_propagation_health`.
    2. An existing host config file (`mcp_json`, `mcp_view_mirrors`, `mcp_toml`) where `"minni"`
       was deleted/absent is flagged as drifted by `audit_host_propagation_health` and converged
       (never skipped) by `execute_atomic_cutover`.
    """
    from minni.wire.health import audit_host_propagation_health

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)
    v1_ver = "0.6.0+git.11111111"
    v2_ver = "0.6.0+git.22222222"
    v2_sha = "2" * 40
    _seed_plugin_version(plugin_dir, v1_ver, "1" * 40)
    _seed_plugin_version(plugin_dir, v2_ver, v2_sha)
    (plugin_dir / "current").symlink_to(plugin_dir / v2_ver)

    # 1. Host with ONLY a plugin_container surface (cursor container with stale v1 dist)
    cursor_container = home / ".cursor" / "plugins" / "local" / "minni"
    (cursor_container / "dist").mkdir(parents=True)
    (cursor_container / "dist" / "server.js").write_text("// stale v1 container\n", encoding="utf-8")

    # Health check must flag drifted plugin_container before cutover
    health_pre = audit_host_propagation_health(home=home, expected_git_sha=v2_sha)
    assert health_pre.ok is False
    assert any(d["host"] == "cursor" and "plugins/local/minni" in d["path"] for d in health_pre.drifted_surfaces)

    res_container = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert res_container.exit_code == 0, res_container.message
    assert any("cursor:" in s and "plugins/local/minni" in s for s in res_container.updated_surfaces)
    health_post_container = audit_host_propagation_health(home=home, expected_git_sha=v2_sha)
    assert health_post_container.ok is True, health_post_container.to_dict()

    # 2. Existing host configs where "minni" key is absent must be flagged by health and converged by cutover
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {}, "oauthAccount": "keep"}), encoding="utf-8")
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    health_missing_key = audit_host_propagation_health(home=home, expected_git_sha=v2_sha)
    assert health_missing_key.ok is False
    drifted_hosts = {d["host"] for d in health_missing_key.drifted_surfaces}
    assert "claude-code" in drifted_hosts
    assert "codex" in drifted_hosts

    res_missing_key = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert res_missing_key.exit_code == 0, res_missing_key.message
    assert any("claude-code:" in s for s in res_missing_key.updated_surfaces)
    assert any("codex:" in s for s in res_missing_key.updated_surfaces)
    claude_doc = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert "minni" in claude_doc["mcpServers"]
    assert claude_doc["oauthAccount"] == "keep"
    assert "[mcp_servers.minni]" in (home / ".codex" / "config.toml").read_text(encoding="utf-8")


def test_r3_p1_2_running_or_locked_cursor_defers_before_step4_and_gc_retains_tilde_old_version(
    tmp_path: Path,
) -> None:
    """P1-2, New P1 #2, New P1 #3:
    - Running/locked `cursor` with a container or config delta defers BEFORE Step 4 (exit 3,
      `DEFERRED_RUNNING_HOST_DELTA`), leaving `~/.cursor/plugins/local/minni` and `~/.cursor/mcp.json`
      completely untouched.
    - When a deferred host config references an old version via tilde (`~/.minni/plugin/<old_ver>/...`),
      Step 7 GC retains `<old_ver>` even when `keep_last=2` is exceeded by newer versions.
    """
    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)

    v_old = "0.5.0+git.00000000"
    v_mid1 = "0.6.0+git.11111111"
    v_mid2 = "0.6.0+git.22222222"
    v_new = "0.6.0+git.33333333"
    v_new_sha = "3" * 40
    _seed_plugin_version(plugin_dir, v_old, "0" * 40)
    _seed_plugin_version(plugin_dir, v_mid1, "1" * 40)
    _seed_plugin_version(plugin_dir, v_mid2, "2" * 40)
    _seed_plugin_version(plugin_dir, v_new, v_new_sha)
    (plugin_dir / "current").symlink_to(plugin_dir / v_mid2)

    # Seed Cursor container and ~/.cursor/mcp.json referencing v_old via ~ tilde path
    cursor_container = home / ".cursor" / "plugins" / "local" / "minni"
    (cursor_container / "dist").mkdir(parents=True)
    (cursor_container / "dist" / "server.js").write_text("// old cursor container\n", encoding="utf-8")
    cursor_mcp = home / ".cursor" / "mcp.json"
    cursor_mcp_orig = json.dumps({
        "mcpServers": {
            "minni": {
                "command": "node",
                "args": [f"~/.minni/plugin/{v_old}/dist/server.js"],
                "env": {
                    "MINNI_AGENT_ID": "cursor",
                    "MINNI_VAULT_PATH": "~/.minni/cursor-vault",
                },
            }
        }
    })
    cursor_mcp.write_text(cursor_mcp_orig, encoding="utf-8")

    # Run cutover with cursor in running_hosts
    res = execute_atomic_cutover(
        home=home,
        target_version=v_new,
        target_git_sha=v_new_sha,
        running_hosts={"cursor"},
    )
    assert res.exit_code == 3
    assert res.status == "DEFERRED_RUNNING_HOST_DELTA"
    assert "cursor" in res.deferred_running_hosts
    # Step 4 MUST NOT have mutated Cursor's container while Cursor was running!
    assert (cursor_container / "dist" / "server.js").read_text(encoding="utf-8") == "// old cursor container\n"
    assert cursor_mcp.read_text(encoding="utf-8") == cursor_mcp_orig
    # Step 7 GC MUST retain v_old even though keep_last=2 (v_new, v_mid2) would otherwise prune v_old!
    assert (plugin_dir / v_old).is_dir(), f"v_old was pruned: {res.gc_result}"


def test_r3_p1_3_rollback_never_rewrites_on_conflict_and_forward_cas_covers_all_surfaces(
    tmp_path: Path, monkeypatch
) -> None:
    """P1-3, New P2 #1, New P3 #1:
    - Step 7a rollback leaves live bytes untouched on CAS mismatch (including mcp_json on claude-code).
    - Forward CAS protects commands_dir symlink replacement against concurrent modification.
    """
    import minni.wire.cutover as cutover_mod

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)
    v1_ver = "0.6.0+git.11111111"
    v2_ver = "0.6.0+git.22222222"
    v2_sha = "2" * 40
    _seed_plugin_version(plugin_dir, v1_ver, "1" * 40)
    _seed_plugin_version(plugin_dir, v2_ver, v2_sha)

    claude_json = home / ".claude.json"
    claude_json.write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(plugin_dir / v1_ver / "dist" / "server.js")],
                    "env": {
                        "MINNI_AGENT_ID": "claude-code",
                        "MINNI_VAULT_PATH": str(home / ".minni" / "claudecode-vault"),
                    },
                }
            }
        }),
        encoding="utf-8",
    )

    concurrent_live_payload = '{"mcpServers":{"minni":{"custom":"live-user-edit"}},"concurrent":true}\n'

    def _mutate_claude_then_fail(_home: Path) -> bool:
        claude_json.write_text(concurrent_live_payload, encoding="utf-8")
        return False

    rb_res = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
        post_flip_verifier=_mutate_claude_then_fail,
    )
    assert rb_res.exit_code == 1
    assert rb_res.rolled_back is True
    assert rb_res.cas_conflicts
    # Live file MUST be byte-identical to concurrent_live_payload (never re-merged or overwritten on conflict!)
    assert claude_json.read_text(encoding="utf-8") == concurrent_live_payload

    # Forward CAS on commands_dir symlink: if symlink target changes concurrently before replace, abort & roll back
    ag_cmds = home / ".gemini" / "commands"
    ag_cmds.mkdir(parents=True)
    cmd_link = ag_cmds / "minni"
    os.symlink(str(plugin_dir / v1_ver / "commands"), cmd_link)

    real_check_lock = cutover_mod._check_host_lockfile

    def _mutate_symlink_during_check(h: Path, host: str, spath: Path, **kw):
        res_lock = real_check_lock(h, host, spath, **kw)
        if spath == cmd_link and os.readlink(cmd_link) == str(plugin_dir / v1_ver / "commands"):
            cmd_link.unlink()
            os.symlink("/concurrent/user/commands", cmd_link)
        return res_lock

    monkeypatch.setattr(cutover_mod, "_check_host_lockfile", _mutate_symlink_during_check)
    sym_res = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert sym_res.exit_code == 1
    assert sym_res.rolled_back is True
    assert os.readlink(cmd_link) == "/concurrent/user/commands"


def test_r3_p1_4_probe_fixed_path_lang_and_catalog_wrapper_launcher_probe(
    tmp_path: Path, monkeypatch
) -> None:
    """P1-4, New P3 #2:
    - `_minimal_probe_env` uses a fixed PATH and `LANG=C.UTF-8` rather than inheriting parent PATH/LANG.
    - Step 3 pre-flip probe invokes catalog host wrapper launchers (`~/.agents/bin/mcp-env-run`) when present
      and aborts pre-flip if the launcher fails.
    """
    from minni.wire.cutover import _minimal_probe_env

    monkeypatch.setenv("LANG", "POISONED_LOCALE.INVALID")
    monkeypatch.setenv("PATH", f"/poisoned/ attacker/path:{os.environ.get('PATH', '')}")
    probe_env = _minimal_probe_env(tmp_path / "probe_home")
    assert probe_env["LANG"] == "C.UTF-8"
    assert "/poisoned/ attacker/path" not in probe_env["PATH"]

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)
    v2_ver = "0.6.0+git.22222222"
    v2_sha = "2" * 40
    _seed_plugin_version(plugin_dir, v2_ver, v2_sha)

    # Seed a broken antigravity wrapper `~/.agents/bin/mcp-env-run`
    wrapper = home / ".agents" / "bin" / "mcp-env-run"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
    wrapper.chmod(0o755)
    (home / ".gemini").mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"mcpServers": {"minni": {"command": "node", "args": []}}}),
        encoding="utf-8",
    )

    res = execute_atomic_cutover(
        home=home,
        target_version=v2_ver,
        target_git_sha=v2_sha,
        running_hosts=set(),
    )
    assert res.exit_code == 1
    assert res.aborted_pre_flip is True


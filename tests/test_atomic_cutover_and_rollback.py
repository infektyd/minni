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
    # `~/.claude.json` preserved concurrent `oauthAccount` while restoring `mcpServers.minni`!
    restored_claude = json.loads(claude_json.read_text(encoding="utf-8"))
    assert restored_claude["oauthAccount"] == "concurrently-updated-by-live-session"
    assert restored_claude["numStartups"] == 99
    assert restored_claude["mcpServers"]["minni"] == old_minni_entry

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

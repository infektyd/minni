"""Tests for §4.7 unified drift health check, installed_plugins.json SHA tracking, and uncatalogued .minni/plugin scanner."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from minni.wire.cutover import execute_atomic_cutover
from minni.wire.health import audit_host_propagation_health, scan_for_uncatalogued_plugin_refs
from tests.test_atomic_cutover_and_rollback import _seed_plugin_version


def test_installed_plugins_tracks_sha_expanded_paths_and_scanner_catches_rogue_file(
    tmp_path: Path, capsys
) -> None:
    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)

    ver = "0.6.0+git.cafebabe"
    sha = "c" * 40
    _seed_plugin_version(plugin_dir, ver, sha)

    # Seed ~/.claude/plugins/installed_plugins.json with stale version/SHA and ~ path
    claude_plugins = home / ".claude" / "plugins"
    claude_plugins.mkdir(parents=True)
    inst_path = claude_plugins / "installed_plugins.json"
    inst_path.write_text(
        json.dumps({
            "version": 2,
            "plugins": {
                "minni@minni-local": [
                    {
                        "scope": "user",
                        "installPath": "~/.claude/plugins/marketplaces/minni-local/plugins/minni",
                        "projectPath": "~/.claude/plugins/marketplaces/minni-local",
                        "version": "0.5.0+git.old",
                        "gitCommitSha": "0" * 40,
                    }
                ]
            },
        }),
        encoding="utf-8",
    )

    # Seed ~/.claude.json
    (home / ".claude.json").write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(plugin_dir / ver / "dist" / "server.js")],
                    "env": {
                        "MINNI_AGENT_ID": "claude-code",
                        "MINNI_VAULT_PATH": str(home / ".minni" / "claudecode-vault"),
                    },
                }
            }
        }),
        encoding="utf-8",
    )

    outcome = execute_atomic_cutover(
        home=home,
        target_version=ver,
        target_git_sha=sha,
        dry_run=False,
        running_hosts=set(),
    )
    assert outcome.exit_code == 0

    # 1. Verify installed_plugins.json tracks base semver version, 40-char gitCommitSha, and expanded paths (no ~)
    inst_doc = json.loads(inst_path.read_text(encoding="utf-8"))
    entry = inst_doc["plugins"]["minni@minni-local"][0]
    assert entry["version"] == "0.6.0"
    assert entry["gitCommitSha"] == sha
    assert "~" not in entry["installPath"]
    assert "~" not in entry["projectPath"]
    assert entry["installPath"].startswith(str(home))

    # 2. Clean converged home has zero uncatalogued refs and healthy audit
    assert scan_for_uncatalogued_plugin_refs(home) == []
    report_clean = audit_host_propagation_health(home=home, expected_git_sha=sha)
    assert report_clean.ok is True
    assert report_clean.uncatalogued_refs == []
    assert report_clean.drifted_surfaces == []

    # 3. Plant a rogue uncatalogued config file and an excluded history.jsonl file
    rogue_cfg = home / ".config" / "muse" / "rogue-extra-mcp.json"
    rogue_cfg.parent.mkdir(parents=True, exist_ok=True)
    rogue_cfg.write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(plugin_dir / "0.5.0+git.deadbeef" / "dist" / "server.js")],
                }
            }
        }),
        encoding="utf-8",
    )
    history_jsonl = home / ".claude" / "history.jsonl"
    history_jsonl.write_text(
        f'{{"text": "inspected {plugin_dir}/0.5.0+git.deadbeef/dist/server.js"}}\n',
        encoding="utf-8",
    )

    rogue_hits = scan_for_uncatalogued_plugin_refs(home)
    assert any(Path(h["path"]) == rogue_cfg for h in rogue_hits)
    assert all(Path(h["path"]) != history_jsonl for h in rogue_hits)

    report_dirty = audit_host_propagation_health(home=home, expected_git_sha=sha)
    assert report_dirty.ok is False
    assert any(Path(h["path"]) == rogue_cfg for h in report_dirty.uncatalogued_refs)

    # 4. Verify minni status surfaces daemon["deploy"] staleness and running_stale_servers (P1-4)
    from minni.minni_cli import cmd_status

    fake_status = {
        "daemon": {
            "version": "0.6.0",
            "uptime_seconds": 120,
            "requests_served": 5,
            "latencies": {},
            "deploy": {
                "stale": True,
                "reason": "daemon started from older commit",
                "running_stale_servers": [
                    {"pid": 4242, "ppid": 100, "version": "0.5.0+git.old", "git_sha": "11111111"}
                ],
            },
        },
        "engine": {"db_ok": True, "faiss_ok": True, "stats": {}},
    }
    with patch("minni.minni_cli._rpc", return_value=fake_status):
        rc = cmd_status(Namespace(socket=str(home / "minnid.sock")))
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "STALE" in out_text
    assert "4242" in out_text


def test_doctor_and_check_scripts_fail_closed_on_daemon_stale_and_host_propagation_drift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """P1-7: minni doctor must fail (exit 1) on daemon deploy lag and on
    audit_host_propagation_health drift, and both check_deployments.py and
    check_versions.py must be driven by audit_host_propagation_health.
    """
    import importlib.util
    import sys

    import minni.minni_cli as minni_cli
    from minni.minnid_runtime import deploy_honesty

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)
    ver = "0.6.0+git.cafebabe"
    sha = "c" * 40
    _seed_plugin_version(plugin_dir, ver, sha)
    (plugin_dir / "current").symlink_to(plugin_dir / ver)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))

    sock_dir = tmp_path / "sockdir"
    sock_dir.mkdir()
    sock_dir.chmod(0o700)
    sock = sock_dir / "minnid.sock"
    sock.touch()
    sock.chmod(0o600)

    # 1. Daemon RPC reports stale=True while local deploy_status() reports stale=False -> doctor must fail (rc == 1)
    monkeypatch.setattr(
        deploy_honesty,
        "deploy_status",
        lambda: {"stale": False, "plugin_dist": {"stale": False}},
    )

    def _rpc_stale(sock_path, method, params=None, timeout=30.0):
        if method == "status":
            return {
                "daemon": {
                    "version": "0.6.0",
                    "uptime_seconds": 10,
                    "requests_served": 1,
                    "deploy": {
                        "stale": True,
                        "reason": "daemon started from older commit 11111111",
                    },
                },
                "engine": {"db_ok": True, "faiss_ok": True, "stats": {}},
            }
        return {"results": [], "count": 0}

    monkeypatch.setattr(minni_cli, "_rpc", _rpc_stale)
    rc_stale = minni_cli.main(["--socket", str(sock), "doctor"])
    out_stale = capsys.readouterr().out
    assert rc_stale == 1, out_stale
    assert "[FAIL] fleet:" in out_stale
    assert "11111111" in out_stale

    # 2. Daemon RPC reports stale=False, but host surface (~/.claude.json) has drift -> doctor must fail (rc == 1)
    (home / ".claude.json").write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(plugin_dir / "0.5.0+git.oldsha" / "dist" / "server.js")],
                    "env": {
                        "MINNI_AGENT_ID": "claude-code",
                        "MINNI_VAULT_PATH": str(home / ".minni" / "claudecode-vault"),
                        "MINNI_WORKSPACE_ID": "forbidden-static-id",
                    },
                }
            }
        }),
        encoding="utf-8",
    )

    def _rpc_clean(sock_path, method, params=None, timeout=30.0):
        if method == "status":
            return {
                "daemon": {
                    "version": "0.6.0",
                    "uptime_seconds": 10,
                    "requests_served": 1,
                    "deploy": {"stale": False, "plugin_dist": {"stale": False}},
                },
                "engine": {"db_ok": True, "faiss_ok": True, "stats": {}},
            }
        return {"results": [], "count": 0}

    monkeypatch.setattr(minni_cli, "_rpc", _rpc_clean)
    rc_drift = minni_cli.main(["--socket", str(sock), "doctor"])
    out_drift = capsys.readouterr().out
    assert rc_drift == 1, out_drift
    assert "[FAIL] fleet:" in out_drift
    assert "claude_code" in out_drift or ".claude.json" in out_drift

    # 3. Both check_deployments.py and check_versions.py must report the drifted host via audit_host_propagation_health
    repo_root = Path(__file__).resolve().parent.parent
    cd_spec = importlib.util.spec_from_file_location(
        "_check_deployments_p1_7", repo_root / "scripts" / "check_deployments.py"
    )
    cd_mod = importlib.util.module_from_spec(cd_spec)
    cd_spec.loader.exec_module(cd_mod)
    cd_problems, _cd_notes = cd_mod.check_wire_launch_bindings(home)
    assert any("claude_code" in p or ".claude.json" in p for p in cd_problems), cd_problems

    monkeypatch.setenv("MINNI_CHECK_VERSIONS_HOME", str(home))
    cv_spec = importlib.util.spec_from_file_location(
        "_check_versions_p1_7", repo_root / "scripts" / "check_versions.py"
    )
    cv_mod = importlib.util.module_from_spec(cv_spec)
    cv_spec.loader.exec_module(cv_mod)
    cv_mismatches, _cv_notes = cv_mod.check_deployed("0.6.0")
    assert any("claude_code" in m or ".claude.json" in m for m in cv_mismatches), cv_mismatches

    # 4. New P1 #4: minni doctor must fail closed (rc == 1, [FAIL] fleet:) if audit_host_propagation_health raises
    def _boom_health(*args, **kwargs):
        raise RuntimeError("corrupt health state")

    with patch("minni.wire.health.audit_host_propagation_health", side_effect=_boom_health):
        rc_exc = minni_cli.main(["--socket", str(sock), "doctor"])
    out_exc = capsys.readouterr().out
    assert rc_exc == 1, out_exc
    assert "[FAIL] fleet:" in out_exc

    # 5. New P2 #3: check_deployments.py must fail on legacy v1 wired.json when a newer payload is staged in ~/.minni/plugin
    home_v1 = tmp_path / "home_v1_stale"
    pdir_v1 = home_v1 / ".minni" / "plugin"
    pdir_v1.mkdir(parents=True)
    old_ver = "0.5.0+git.11111111"
    new_ver = "0.6.0+git.22222222"
    _seed_plugin_version(pdir_v1, old_ver, "1" * 40)
    _seed_plugin_version(pdir_v1, new_ver, "2" * 40)
    old_root = pdir_v1 / old_ver
    claude_cfg = home_v1 / ".claude.json"
    claude_cfg.write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(old_root / "dist" / "server.js")],
                    "env": {
                        "MINNI_AGENT_ID": "claude-code",
                        "MINNI_VAULT_PATH": "~/.minni/claudecode-vault",
                    },
                }
            }
        }),
        encoding="utf-8",
    )
    (pdir_v1 / "wired.json").write_text(
        json.dumps({
            "schema": 1,
            "wires": [
                {
                    "platform": "claude-code",
                    "agent": "claude-code",
                    "workspace": "global",
                    "config_path": str(claude_cfg),
                    "install_root": str(old_root),
                    "version": old_ver,
                    "wired_at": "2026-10-31T00:00:00Z",
                }
            ],
        }),
        encoding="utf-8",
    )
    v1_problems, _ = cd_mod.check_wire_launch_bindings(home_v1)
    assert any("versioned_sha_in_args" in p or "entry_or_args_mismatch" in p for p in v1_problems), v1_problems

    # 6. R4 New P1 #1: check_deployments.py must NOT skip plugin_container directory drift!
    home_c = tmp_path / "home_container_drift"
    pdir_c = home_c / ".minni" / "plugin"
    pdir_c.mkdir(parents=True)
    _seed_plugin_version(pdir_c, new_ver, "2" * 40)
    (pdir_c / "current").symlink_to(pdir_c / new_ver)
    cursor_c = home_c / ".cursor" / "plugins" / "local" / "minni"
    (cursor_c / "dist").mkdir(parents=True)
    (cursor_c / "dist" / "server.js").write_text("// stale container server.js\n", encoding="utf-8")
    (home_c / ".cursor" / "mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "minni": {
                    "command": "node",
                    "args": [str(pdir_c / "current" / "dist" / "server.js")],
                    "env": {
                        "MINNI_AGENT_ID": "cursor",
                        "MINNI_VAULT_PATH": str(home_c / ".minni" / "cursor-vault"),
                    },
                }
            }
        }),
        encoding="utf-8",
    )
    container_problems, _ = cd_mod.check_wire_launch_bindings(home_c)
    assert any(
        "cursor: host propagation drift" in p and "container_dist_server_js_mismatch" in p
        for p in container_problems
    ), container_problems


def test_uncatalogued_scanner_ignores_backups_and_conversations_but_fails_unknown_configs(
    tmp_path: Path,
) -> None:
    """R6 Item 2: Backup, history, conversation, file-history, and worker workspace files
    referencing .minni/plugin are NOT drift; unknown config-shaped files at host config depths ARE.
    """
    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)
    ver = "0.6.0+git.cafebabe"
    sha = "c" * 40
    _seed_plugin_version(plugin_dir, ver, sha)
    (plugin_dir / "current").symlink_to(plugin_dir / ver)

    stale_ref = f"node {plugin_dir}/0.5.0+git.deadbeef/dist/server.js"

    # Seed excluded history, backup, conversation, session, and worker workspace stores referencing .minni/plugin
    noise_files = [
        home / ".gemini" / "antigravity" / "brain" / "conv-1" / ".system_generated" / "messages" / "m1.json",
        home / ".gemini" / "antigravity-cli" / "brain" / "conv-2" / "scratch" / "bench.mjs",
        home / ".gemini" / "antigravity-ide" / "brain" / "conv-3" / "notes.md",
        home / ".gemini" / "antigravity-backup" / "mcp_config.json",
        home / ".gemini" / "tmp" / "user" / "chats" / "session-1.json",
        home / ".agents" / "backups" / "20260520-085707" / ".gemini__config__mcp_config.json",
        home / ".codex" / "muse-code-worker" / "e3fd8d37" / "workspace" / "eval" / "public_repo.json",
        home / ".codex" / "grok-build-peer" / "970c9701" / "workspace" / "contract.md",
        home / ".codex" / "backups" / "20260925" / "config.toml",
        home / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions" / "a" / "b" / "local_1.json",
        home / "Library" / "Application Support" / "Claude" / "claude-code-sessions" / "a" / "b" / "local_2.json",
        home / ".claude" / "file-history" / "fc405a96" / "5782a9a8@v24",
        home / ".claude" / "backups" / "2026-09-24" / "installed_plugins.json",
        home / ".grok" / "memory-v2" / "workspaces" / "w1" / "topics" / "topic.md",
        home / ".grok" / "scratch" / "audit" / "ledger.md",
        home / ".cursor" / "plans" / "rewrite.plan.md",
        home / ".cursor" / "legacy-plugins" / "minni-old" / "skills" / "s" / "SKILL.md",
    ]
    for nf in noise_files:
        nf.parent.mkdir(parents=True, exist_ok=True)
        nf.write_text(json.dumps({"ref": stale_ref}), encoding="utf-8")

    # Converge the catalogued surfaces on home so only uncatalogued refs could fail health
    cut_res = execute_atomic_cutover(
        home=home,
        target_version=ver,
        target_git_sha=sha,
        dry_run=False,
        running_hosts=set(),
    )
    assert cut_res.exit_code == 0

    assert scan_for_uncatalogued_plugin_refs(home) == []
    clean_report = audit_host_propagation_health(home=home, expected_git_sha=sha)
    assert clean_report.ok is True, clean_report.to_dict()
    assert clean_report.uncatalogued_refs == []

    # Now plant unknown config-shaped files at host config depths -> MUST fail closed
    unknown_json = home / ".gemini" / "unknown_mcp_config.json"
    unknown_toml = home / ".codex" / "rogue_config.toml"
    unknown_skill = home / ".codex" / "skills" / "rogue-minni-skill" / "SKILL.md"
    for uf in (unknown_json, unknown_toml, unknown_skill):
        uf.parent.mkdir(parents=True, exist_ok=True)
        uf.write_text(f"# config referencing {stale_ref}\n", encoding="utf-8")

    hits = scan_for_uncatalogued_plugin_refs(home)
    hit_paths = {Path(h["path"]) for h in hits}
    assert unknown_json in hit_paths, hits
    assert unknown_toml in hit_paths, hits
    assert unknown_skill in hit_paths, hits
    for nf in noise_files:
        assert nf not in hit_paths

    dirty_report = audit_host_propagation_health(home=home, expected_git_sha=sha)
    assert dirty_report.ok is False
    assert {Path(h["path"]) for h in dirty_report.uncatalogued_refs} >= {
        unknown_json,
        unknown_toml,
        unknown_skill,
    }


def test_r7_scanner_denylist_narrowing_symlinks_and_unreadable_or_oversized_findings(
    tmp_path: Path,
) -> None:
    """R7 Input 1 (P1 & P3):
    - P1: Scanner deny-list only denies data-store subtrees (brain, conversations, file-history,
      backups, muse-code-worker, session stores); keeps mcp_config.json siblings (~/.gemini/antigravity,
      ~/.gemini/antigravity-ide), workflows/, and projects/ scannable; follows symlinks that resolve to
      config-shaped files.
    - P3: Unreadable config files and config files over the size cap are reported as findings, not silent skips.
    """
    import os
    from minni.wire.health import _MAX_SCAN_FILE_BYTES

    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)
    ver = "0.6.0+git.cafebabe"
    sha = "c" * 40
    _seed_plugin_version(plugin_dir, ver, sha)
    (plugin_dir / "current").symlink_to(plugin_dir / ver)

    cut_res = execute_atomic_cutover(
        home=home,
        target_version=ver,
        target_git_sha=sha,
        dry_run=False,
        running_hosts=set(),
    )
    assert cut_res.exit_code == 0

    stale_ref = f"node {plugin_dir}/0.5.0+git.deadbeef/dist/server.js"

    # 1. Previously hidden config roots (~/.gemini/antigravity, -ide, workflows, projects) are now scannable
    ag_sibling = home / ".gemini" / "antigravity" / "extra_mcp_config.json"
    ag_ide_sibling = home / ".gemini" / "antigravity-ide" / "extra_mcp_config.json"
    agents_workflow = home / ".agents" / "workflows" / "plan.md"
    grok_workflow = home / ".grok" / "workflows" / "deploy.md"
    claude_project_cfg = home / ".claude" / "projects" / "demo" / "mcp.json"

    for p in (ag_sibling, ag_ide_sibling, agents_workflow, grok_workflow, claude_project_cfg):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'{{"ref": "{stale_ref}"}}\n', encoding="utf-8")

    # 2. Symlink resolving to an external config-shaped file referencing .minni/plugin is followed
    external_dir = tmp_path / "external_configs"
    external_dir.mkdir(parents=True, exist_ok=True)
    external_cfg = external_dir / "shared_mcp.json"
    external_cfg.write_text(f'{{"mcpServers": {{"minni": "{stale_ref}"}}}}\n', encoding="utf-8")
    symlink_cfg = home / ".gemini" / "antigravity" / "linked_mcp.json"
    os.symlink(external_cfg, symlink_cfg)

    # Symlink to a directory containing a config-shaped file is also followed (cycle-safe)
    external_subdir = external_dir / "linked_workflows"
    external_subdir.mkdir(parents=True, exist_ok=True)
    linked_wf_file = external_subdir / "sync.md"
    linked_wf_file.write_text(f"Run `{stale_ref}`\n", encoding="utf-8")
    symlink_dir = home / ".agents" / "workflows" / "external"
    os.symlink(external_subdir, symlink_dir)

    # 3. Unreadable config-shaped file and oversized config-shaped file (> 2 MiB) are reported as findings
    unreadable_cfg = home / ".codex" / "unreadable_config.toml"
    unreadable_cfg.parent.mkdir(parents=True, exist_ok=True)
    unreadable_cfg.write_text("# secret\n", encoding="utf-8")
    unreadable_cfg.chmod(0o000)

    oversized_cfg = home / ".gemini" / "oversized_mcp.json"
    oversized_cfg.parent.mkdir(parents=True, exist_ok=True)
    oversized_cfg.write_bytes(b" " * (_MAX_SCAN_FILE_BYTES + 1024))

    try:
        findings = scan_for_uncatalogued_plugin_refs(home)
        by_path = {Path(f["path"]): f for f in findings}

        for expected_p in (
            ag_sibling,
            ag_ide_sibling,
            agents_workflow,
            grok_workflow,
            claude_project_cfg,
            symlink_cfg,
            symlink_dir / "sync.md",
        ):
            assert expected_p in by_path, f"Missing expected scan hit for {expected_p}: {findings}"
            assert by_path[expected_p]["reason"] == "uncatalogued_file_references_minni_plugin"

        assert unreadable_cfg in by_path, f"Missing unreadable config finding: {findings}"
        assert by_path[unreadable_cfg]["reason"] == "unreadable_config_file"

        assert oversized_cfg in by_path, f"Missing oversized config finding: {findings}"
        assert by_path[oversized_cfg]["reason"] == "config_file_exceeds_size_cap"

        report = audit_host_propagation_health(home=home, expected_git_sha=sha)
        assert report.ok is False
    finally:
        unreadable_cfg.chmod(0o600)

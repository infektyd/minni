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

    # 1. Verify installed_plugins.json tracks version, 40-char gitCommitSha, and expanded paths (no ~)
    inst_doc = json.loads(inst_path.read_text(encoding="utf-8"))
    entry = inst_doc["plugins"]["minni@minni-local"][0]
    assert entry["version"] == ver
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

"""Tests for Claude Desktop surface reconciliation and running-host guard (P1-5, P2-3, N6, R4-4)."""

from __future__ import annotations

import json
from pathlib import Path

from minni.wire.cutover import reconcile_all_hosts
from tests.test_atomic_cutover_and_rollback import _seed_plugin_version


def test_claude_desktop_zero_delta_exits_0_and_running_delta_exits_nonzero(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugin_dir = home / ".minni" / "plugin"
    plugin_dir.mkdir(parents=True)

    ver = "0.6.0+git.abc12345"
    sha = "a" * 40
    _seed_plugin_version(plugin_dir, ver, sha)
    (plugin_dir / "current").symlink_to(ver)

    desktop_cfg = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    desktop_cfg.parent.mkdir(parents=True, exist_ok=True)
    valid_doc = {
        "mcpServers": {
            "minni": {
                "command": "/opt/homebrew/bin/node",
                "args": [
                    str(home / ".agents" / "plugins" / "Minni@Minni" / "dist" / "server.js")
                ],
                "env": {
                    "MINNI_AGENT_ID": "claude-code",
                    "MINNI_VAULT_PATH": str(home / ".minni" / "claudecode-vault"),
                    "MINNI_SOCKET_PATH": str(home / ".minni" / "run" / "minnid.sock"),
                    "MINNI_AFM_BRIDGE_URL": "http://127.0.0.1:11437/v1/chat/completions",
                    "MINNI_AFM_MODEL": "afm-local",
                    "MINNI_AFM_PROVIDER_MODE": "native",
                    "MINNI_AFM_NATIVE_HELPER": str(
                        plugin_dir / "current" / "dist" / "minni-afm-helper"
                    ),
                },
            }
        },
        "preferences": {"theme": "dark"},
    }
    desktop_cfg.write_text(json.dumps(valid_doc, indent=2), encoding="utf-8")
    mtime_before = desktop_cfg.stat().st_mtime_ns

    # 1. With delta == 0, reconcile returns exit 0 and performs zero writes even while Claude.app runs (P1-5)
    outcome_ok = reconcile_all_hosts(
        home=home,
        target_version=ver,
        target_git_sha=sha,
        dry_run=False,
        running_hosts={"claude-desktop"},
    )
    assert outcome_ok.exit_code == 0
    assert desktop_cfg.stat().st_mtime_ns == mtime_before

    # 2. Inject forbidden MINNI_WORKSPACE_ID while Claude.app is running -> zero writes and exit code 3 (R4-4)
    drifted_doc = json.loads(desktop_cfg.read_text(encoding="utf-8"))
    drifted_doc["mcpServers"]["minni"]["env"]["MINNI_WORKSPACE_ID"] = "minni"
    drifted_raw = json.dumps(drifted_doc, indent=2)
    desktop_cfg.write_text(drifted_raw, encoding="utf-8")

    outcome_deferred = reconcile_all_hosts(
        home=home,
        target_version=ver,
        target_git_sha=sha,
        dry_run=False,
        running_hosts={"claude-desktop"},
    )
    assert outcome_deferred.exit_code == 3
    assert "claude-desktop" in outcome_deferred.deferred_running_hosts
    assert desktop_cfg.read_text(encoding="utf-8") == drifted_raw

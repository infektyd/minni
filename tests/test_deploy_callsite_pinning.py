"""Call-site pinning tests for scripts/update_root.sh and src/minni/fleet_sync.py (§4.3, §6.2)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from minni.fleet_sync import run_fleet_sync

REPO = Path(__file__).resolve().parent.parent


def test_update_root_and_fleet_sync_invoke_atomic_cutover_and_behavioral_probe() -> None:
    update_root_text = (REPO / "scripts" / "update_root.sh").read_text(encoding="utf-8")
    assert "minni.wire.cutover" in update_root_text

    fleet_sync_text = (REPO / "src" / "minni" / "fleet_sync.py").read_text(encoding="utf-8")
    assert "_reconcile_all_hosts" in fleet_sync_text
    assert "reconcile_all_hosts" in fleet_sync_text

    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0, "status": "ok"}),
        patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0}),
        patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0}),
        patch("minni.fleet_sync._restamp_grok_hooks", return_value={"name": "grok_hooks_rules", "exit_code": 0}),
        patch("minni.fleet_sync._audit_deploy_symlinks", return_value={"name": "deploy_symlink_audit", "exit_code": 0}),
        patch("minni.fleet_sync._reconcile_all_hosts", return_value={"name": "reconcile_all_hosts", "exit_code": 0}) as mock_reconcile,
    ):
        res = run_fleet_sync(dry_run=False)
        assert res.ok is True
        mock_reconcile.assert_called_once()

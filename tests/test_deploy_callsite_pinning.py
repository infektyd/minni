"""Call-site pinning tests for scripts/update_root.sh and src/minni/fleet_sync.py (§4.3, §6.2)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import conftest as _conftest
from minni.fleet_sync import run_fleet_sync
from minni.wire.paths import default_home
from minni.wire.registry import resolve_home

REPO = Path(__file__).resolve().parent.parent


def test_hermetic_home_guard_never_resolves_real_home() -> None:
    """Guard test (P1-6 Step 0): fails if any home/XDG/Minni resolver returns the real home."""
    real_home = _conftest._PRE_FIXTURE_REAL_HOME
    assert real_home and os.path.isabs(real_home)

    resolved_candidates = {
        "Path.home()": os.path.realpath(str(Path.home())),
        "expanduser('~')": os.path.realpath(os.path.expanduser("~")),
        "default_home()": os.path.realpath(str(default_home())),
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


def test_update_root_and_fleet_sync_invoke_atomic_cutover_and_behavioral_probe(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("MINNI_HOME", str(fake_home / ".minni"))

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

"""Product fleet sync — keep hosts on the current install."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minni.fleet_sync import (
    SyncResult,
    run_fleet_sync,
)


def test_sync_result_to_dict():
    r = SyncResult(ok=True, install_kind="packaged", message="ok", next_actions=["a"])
    d = r.to_dict()
    assert d["ok"] is True
    assert d["install_kind"] == "packaged"
    assert d["next_actions"] == ["a"]


@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0})
@patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_packaged_sync_wires_and_propagates(mock_kind, mock_wire, mock_prop, mock_kick):
    result = run_fleet_sync(dry_run=False)
    assert result.ok
    assert result.install_kind == "packaged"
    mock_wire.assert_called_once()
    assert mock_wire.call_args.kwargs["from_repo"] is None
    assert mock_wire.call_args.kwargs["force_reinstall"] is True
    assert mock_prop.call_count == 2  # antigravity + cursor
    mock_kick.assert_called_once()


@patch("minni.fleet_sync._kickstart_daemon")
@patch("minni.fleet_sync._run_propagate")
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch(
    "minni.fleet_sync._detect_install_kind",
    return_value=("editable-checkout", Path("/tmp/minni-checkout")),
)
def test_editable_sync_uses_from_repo(mock_kind, mock_wire, mock_prop, mock_kick):
    mock_prop.return_value = {"name": "propagate:x", "exit_code": 0}
    mock_kick.return_value = {"name": "restart_daemon", "exit_code": 0}
    result = run_fleet_sync()
    assert result.ok
    assert mock_wire.call_args.kwargs["from_repo"] == Path("/tmp/minni-checkout")
    assert any("sync --full" in a for a in result.next_actions)


@patch("minni.fleet_sync.subprocess.run")
@patch(
    "minni.fleet_sync._detect_install_kind",
    return_value=("editable-checkout", Path("/tmp/minni-checkout")),
)
def test_full_requires_update_root_script(mock_kind, mock_run, tmp_path):
    # checkout without script → fail
    result = run_fleet_sync(full=True)
    assert result.ok is False
    assert "update_root.sh missing" in result.message or "missing" in result.message.lower()


@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_full_rejected_on_packaged(mock_kind):
    result = run_fleet_sync(full=True)
    assert result.ok is False
    assert "editable" in result.message.lower() or "pipx" in result.message.lower()


@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0, "skipped": True})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_wire_only_skips_propagate(mock_kind, mock_wire, mock_kick):
    result = run_fleet_sync(propagate_hosts=False)
    assert result.ok
    assert all(not s.get("name", "").startswith("propagate") for s in result.steps)

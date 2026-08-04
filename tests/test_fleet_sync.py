"""Product fleet sync — keep hosts on the current install."""

from __future__ import annotations

import dis
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minni.fleet_sync import (
    SyncResult,
    _restamp_grok_hooks,
    _run_wire,
    _wire_status,
    run_fleet_sync,
)

OK_GROK = {"name": "grok_hooks_rules", "exit_code": 0}


def test_sync_result_to_dict():
    r = SyncResult(ok=True, install_kind="packaged", message="ok", next_actions=["a"])
    d = r.to_dict()
    assert d["ok"] is True
    assert d["install_kind"] == "packaged"
    assert d["next_actions"] == ["a"]


@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0})
@patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_packaged_sync_wires_and_propagates(mock_kind, mock_wire, mock_prop, mock_kick, mock_grok):
    result = run_fleet_sync(dry_run=False)
    assert result.ok
    assert result.install_kind == "packaged"
    mock_wire.assert_called_once()
    assert mock_wire.call_args.kwargs["from_repo"] is None
    assert mock_wire.call_args.kwargs["force_reinstall"] is True
    assert mock_prop.call_count == 2  # antigravity + cursor
    mock_kick.assert_called_once()


@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon")
@patch("minni.fleet_sync._run_propagate")
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch(
    "minni.fleet_sync._detect_install_kind",
    return_value=("editable-checkout", Path("/tmp/minni-checkout")),
)
def test_editable_sync_uses_from_repo(mock_kind, mock_wire, mock_prop, mock_kick, mock_grok):
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


@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0, "skipped": True})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_wire_only_skips_propagate(mock_kind, mock_wire, mock_kick, mock_grok):
    result = run_fleet_sync(propagate_hosts=False)
    assert result.ok
    assert all(not s.get("name", "").startswith("propagate") for s in result.steps)


# ── D-fleet honesty: a nonzero step must never report ok ─────────────────────
#
# The pre-fix verdict laundered failures: wire exit 1 was hard-coded "partial
# skip = fine" and the propagate filter discarded exit 1 entirely, so a fleet
# redeploy that wired nothing still printed ok=true and exited 0.


def _fleet(**patches):
    """Run the default fleet path with every step stubbed, overriding some."""
    steps = {
        "_run_wire": {"name": "wire_all", "exit_code": 0, "status": "ok"},
        "_run_propagate": {"name": "propagate:x", "exit_code": 0},
        "_kickstart_daemon": {"name": "restart_daemon", "exit_code": 0},
        "_restamp_grok_hooks": dict(OK_GROK),
    }
    steps.update(patches)
    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value=steps["_run_wire"]),
        patch("minni.fleet_sync._run_propagate", return_value=steps["_run_propagate"]),
        patch("minni.fleet_sync._kickstart_daemon", return_value=steps["_kickstart_daemon"]),
        patch("minni.fleet_sync._restamp_grok_hooks", return_value=steps["_restamp_grok_hooks"]),
    ):
        return run_fleet_sync()


def test_wire_failure_is_not_ok():
    result = _fleet(_run_wire={"name": "wire_all", "exit_code": 1, "status": "failed"})
    assert result.ok is False
    assert "wire_all" in result.message


def test_wire_partial_is_not_ok():
    result = _fleet(_run_wire={"name": "wire_all", "exit_code": 1, "status": "partial"})
    assert result.ok is False
    assert "wire_all" in result.message


def test_wire_usage_error_is_not_ok():
    result = _fleet(_run_wire={"name": "wire_all", "exit_code": 2, "status": "failed"})
    assert result.ok is False


def test_wire_skipped_stays_ok_like_update_root():
    """status=skipped means no wire-managed hosts here — propagate still owns
    antigravity/cursor, so this is not a redeploy failure (update_root.sh
    makes the same call)."""
    result = _fleet(
        _run_wire={
            "name": "wire_all",
            "exit_code": 1,
            "status": "skipped",
            "skipped": True,
            "reason": "no wire-managed host surfaces",
        },
    )
    assert result.ok is True


def test_propagate_failure_is_not_ok():
    result = _fleet(_run_propagate={"name": "propagate:cursor", "exit_code": 1})
    assert result.ok is False
    assert "propagate:cursor" in result.message


def test_propagate_missing_script_is_not_ok():
    result = _fleet(_run_propagate={"name": "propagate:cursor", "exit_code": 2, "error": "x"})
    assert result.ok is False


def test_daemon_restart_failure_is_not_ok():
    result = _fleet(_kickstart_daemon={"name": "restart_daemon", "exit_code": 1})
    assert result.ok is False
    assert "restart_daemon" in result.message


def test_grok_restamp_failure_is_not_ok():
    result = _fleet(_restamp_grok_hooks={"name": "grok_hooks_rules", "exit_code": 1})
    assert result.ok is False
    assert "grok_hooks_rules" in result.message


def test_every_failed_step_is_named_in_the_verdict():
    result = _fleet(
        _run_wire={"name": "wire_all", "exit_code": 1, "status": "failed"},
        _run_propagate={"name": "propagate:cursor", "exit_code": 1},
        _kickstart_daemon={"name": "restart_daemon", "exit_code": 1},
        _restamp_grok_hooks={"name": "grok_hooks_rules", "exit_code": 1},
    )
    assert result.ok is False
    for name in ("wire_all", "propagate:cursor", "restart_daemon", "grok_hooks_rules"):
        assert name in result.message
    assert any("minni sync" in a for a in result.next_actions)


def test_cli_sync_exits_nonzero_when_a_step_failed(capsys):
    """minni_cli maps ok → exit code; a failed fleet redeploy must exit 1."""
    from argparse import Namespace

    from minni.minni_cli import cmd_sync

    failed = SyncResult(
        ok=False,
        install_kind="packaged",
        steps=[{"name": "wire_all", "exit_code": 1}],
        message="fleet sync failed",
    )
    with patch("minni.fleet_sync.run_fleet_sync", return_value=failed):
        assert cmd_sync(Namespace()) == 1
    payload = json.loads(capsys.readouterr().out.split("\n\n")[0])
    assert payload["ok"] is False


# ── Grok hooks/rules re-stamp (propagate-only surface) ───────────────────────

FAKE_PROPAGATE = '''
CALLS = []


def update_grok_hooks(install_root):
    CALLS.append(("hooks", str(install_root)))
    return {"installed": INSTALLED, "path": "hooks.json"}


def write_grok_rules():
    CALLS.append(("rules", None))
    return {"installed": INSTALLED, "path": "rules.md"}
'''


def _fake_propagate(tmp_path: Path, *, installed: bool = True) -> Path:
    script = tmp_path / "fake_propagate.py"
    script.write_text(f"INSTALLED = {installed!r}\n{FAKE_PROPAGATE}", encoding="utf-8")
    return script


def _grok_home(tmp_path: Path, monkeypatch, *, wired: bool = True) -> Path:
    """Temp HOME with a wire install root — never the operator's real HOME."""
    home = tmp_path / "home"
    root = home / ".minni" / "plugin" / "9.9.9"
    root.mkdir(parents=True)
    if wired:
        (home / ".minni" / "plugin" / "wired.json").write_text(
            json.dumps({"wires": [
                {"platform": "codex", "install_root": str(root), "wired_at": "2026-01-02"},
                {"platform": "grok", "install_root": str(root), "wired_at": "2026-01-01"},
            ]}),
            encoding="utf-8",
        )
    monkeypatch.setenv("HOME", str(home))
    return root


def test_grok_restamp_uses_the_grok_wire_root(tmp_path, monkeypatch):
    root = _grok_home(tmp_path, monkeypatch)
    with patch("minni.fleet_sync._propagate_py", return_value=_fake_propagate(tmp_path)):
        step = _restamp_grok_hooks(None, dry_run=False)
    assert step["exit_code"] == 0
    assert step["install_root"] == str(root)


def test_grok_restamp_failure_reports_nonzero(tmp_path, monkeypatch):
    _grok_home(tmp_path, monkeypatch)
    with patch(
        "minni.fleet_sync._propagate_py",
        return_value=_fake_propagate(tmp_path, installed=False),
    ):
        step = _restamp_grok_hooks(None, dry_run=False)
    assert step["exit_code"] == 1
    assert not step.get("skipped")


def test_grok_restamp_without_wire_root_skips_rather_than_fails(tmp_path, monkeypatch):
    home = tmp_path / "bare-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with patch("minni.fleet_sync._propagate_py", return_value=_fake_propagate(tmp_path)):
        step = _restamp_grok_hooks(None, dry_run=False)
    assert step["exit_code"] == 0
    assert step["skipped"] is True


def test_grok_restamp_missing_propagate_is_a_failure(tmp_path, monkeypatch):
    _grok_home(tmp_path, monkeypatch)
    with patch("minni.fleet_sync._propagate_py", return_value=None):
        step = _restamp_grok_hooks(None, dry_run=False)
    assert step["exit_code"] != 0
    assert not step.get("skipped")


def test_grok_restamp_dry_run_touches_nothing(tmp_path, monkeypatch):
    _grok_home(tmp_path, monkeypatch)
    with patch("minni.fleet_sync._propagate_py") as locate:
        step = _restamp_grok_hooks(None, dry_run=True)
    locate.assert_not_called()
    assert step["skipped"] is True
    assert step["exit_code"] == 0


@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0})
@patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_default_sync_restamps_grok(mock_kind, mock_wire, mock_prop, mock_kick, mock_grok):
    """`minni sync` omitted this and left five dead Grok hooks behind when GC
    pruned the versioned tree the old manifest pointed at."""
    result = run_fleet_sync()
    mock_grok.assert_called_once()
    assert any(s.get("name") == "grok_hooks_rules" for s in result.steps)


# ── wire JSON status parsing (mirrors update_root.sh) ────────────────────────


def test_wire_status_parses_pretty_emit():
    doc = json.dumps(
        {"schema": 1, "status": "partial", "results": [], "gc": {}}, indent=2,
    )
    assert _wire_status(doc) == "partial"


def test_wire_status_ignores_leading_noise_and_nested_objects():
    doc = "npm warn deprecated\n" + json.dumps(
        {"schema": 1, "status": "skipped", "results": [{"platform": "grok"}], "gc": {}},
        indent=2,
    )
    assert _wire_status(doc) == "skipped"


def test_wire_status_unparseable_is_none():
    assert _wire_status("not json at all") is None


def test_unparseable_wire_output_is_not_ok():
    """A nonzero wire whose status cannot be read is a failure, not a skip."""
    result = _fleet(_run_wire={"name": "wire_all", "exit_code": 1, "status": None})
    assert result.ok is False


# ── update_root.sh must not run unbounded (launchd timer drives it) ──────────


@patch("minni.fleet_sync.subprocess.run")
@patch("minni.fleet_sync._detect_install_kind")
def test_full_sync_passes_a_timeout(mock_kind, mock_run, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "update_root.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    mock_kind.return_value = ("editable-checkout", checkout)
    mock_run.return_value = MagicMock(returncode=0)
    result = run_fleet_sync(full=True)
    assert result.ok
    timeout = mock_run.call_args.kwargs.get("timeout")
    assert isinstance(timeout, (int, float)) and timeout > 0


@patch("minni.fleet_sync.subprocess.run")
@patch("minni.fleet_sync._detect_install_kind")
def test_full_sync_timeout_is_reported_as_failure(mock_kind, mock_run, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "update_root.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    mock_kind.return_value = ("editable-checkout", checkout)
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="update_root.sh", timeout=1)
    result = run_fleet_sync(full=True)
    assert result.ok is False
    assert "timed out" in result.message.lower()


# ── #275 class regression: the real run_wire call path ──────────────────────


def test_wire_namespace_covers_every_attribute_run_wire_reads():
    """#275 shipped a Namespace missing `socket`; every test stubbed _run_wire
    so nothing caught it. Read the attributes off run_wire's own bytecode."""
    from minni.wire.flow import run_wire

    reads = set()
    prev = None
    for ins in dis.get_instructions(run_wire):
        if (
            ins.opname == "LOAD_ATTR"
            and prev is not None
            and prev.opname in ("LOAD_FAST", "LOAD_FAST_BORROW")
            and prev.argval == "args"
        ):
            reads.add(ins.argval)
        prev = ins
    assert reads, "could not read run_wire's arg attribute access"

    seen = {}

    def _record(ns):
        seen.update(vars(ns))
        return 0

    with patch("minni.wire.flow.run_wire", _record):
        _run_wire(from_repo=None, force_reinstall=True, prune=True, dry_run=True)
    missing = reads - set(seen)
    assert not missing, f"Namespace missing run_wire attributes: {sorted(missing)}"


def test_run_wire_drives_the_real_flow_without_attribute_errors(tmp_path, monkeypatch):
    """Call the real run_wire (dry-run, temp HOME) — a missing Namespace attr
    raises AttributeError here instead of only on the operator's machine."""
    monkeypatch.setenv("HOME", str(tmp_path))
    step = _run_wire(from_repo=None, force_reinstall=True, prune=True, dry_run=True)
    assert step["name"] == "wire_all"
    assert isinstance(step["exit_code"], int)
    # No bundled payload in a source checkout: wire refuses loudly, and that
    # nonzero must not be laundered into a skip.
    if step["exit_code"] != 0:
        assert step.get("status") != "ok"
    assert not list(tmp_path.iterdir()), "dry-run wire must not write to HOME"

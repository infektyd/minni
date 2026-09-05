"""Product fleet sync — keep hosts on the current install."""

from __future__ import annotations

import dis
import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minni.fleet_sync import (
    SyncResult,
    _audit_deploy_symlinks,
    _kickstart_daemon,
    _minnid_is_live,
    _restamp_grok_hooks,
    _run_propagate,
    _run_wire,
    _step_failed,
    _wire_status,
    _worktree_linked_labels,
    run_fleet_sync,
)

OK_GROK = {"name": "grok_hooks_rules", "exit_code": 0}
OK_AUDIT = {"name": "deploy_symlink_audit", "exit_code": 0}

REPO = Path(__file__).resolve().parent.parent


def test_sync_result_to_dict():
    r = SyncResult(ok=True, install_kind="packaged", message="ok", next_actions=["a"])
    d = r.to_dict()
    assert d["ok"] is True
    assert d["install_kind"] == "packaged"
    assert d["next_actions"] == ["a"]


@patch("minni.fleet_sync._audit_deploy_symlinks", return_value=OK_AUDIT)
@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0})
@patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_packaged_sync_wires_and_propagates(
    mock_kind, mock_wire, mock_prop, mock_kick, mock_grok, mock_audit,
):
    result = run_fleet_sync(dry_run=False)
    assert result.ok
    assert result.install_kind == "packaged"
    mock_wire.assert_called_once()
    assert mock_wire.call_args.kwargs["from_repo"] is None
    assert mock_wire.call_args.kwargs["force_reinstall"] is True
    assert mock_prop.call_count == 2  # antigravity + cursor
    mock_kick.assert_called_once()


@patch("minni.fleet_sync._audit_deploy_symlinks", return_value=OK_AUDIT)
@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon")
@patch("minni.fleet_sync._run_propagate")
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch(
    "minni.fleet_sync._detect_install_kind",
    return_value=("editable-checkout", Path("/tmp/minni-checkout")),
)
def test_editable_sync_uses_from_repo(
    mock_kind, mock_wire, mock_prop, mock_kick, mock_grok, mock_audit,
):
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


@patch("minni.fleet_sync._audit_deploy_symlinks", return_value=OK_AUDIT)
@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0, "skipped": True})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_wire_only_skips_propagate(mock_kind, mock_wire, mock_kick, mock_grok, mock_audit):
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
        "_audit_deploy_symlinks": dict(OK_AUDIT),
    }
    steps.update(patches)
    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value=steps["_run_wire"]),
        patch("minni.fleet_sync._run_propagate", return_value=steps["_run_propagate"]),
        patch("minni.fleet_sync._kickstart_daemon", return_value=steps["_kickstart_daemon"]),
        patch("minni.fleet_sync._restamp_grok_hooks", return_value=steps["_restamp_grok_hooks"]),
        patch(
            "minni.fleet_sync._audit_deploy_symlinks",
            return_value=steps["_audit_deploy_symlinks"],
        ),
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


def preflight_grok_native(install_root):
    pass


def update_grok_hooks(install_root, *, existing_only=False):
    assert existing_only
    CALLS.append(("hooks", str(install_root)))
    return {"installed": INSTALLED, "path": "hooks.json"}


def write_grok_rules(*, existing_only=False):
    assert existing_only
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
    # Codex is wired NEWER, to a different root: a global max over wired_at
    # would stamp grok's hooks with codex's dist paths.
    codex_root = home / ".minni" / "plugin" / "9.9.10"
    codex_root.mkdir(parents=True)
    if wired:
        (home / ".minni" / "plugin" / "wired.json").write_text(
            json.dumps({"wires": [
                {"platform": "codex", "install_root": str(codex_root), "wired_at": "2026-02-02"},
                {"platform": "grok", "install_root": str(root), "wired_at": "2026-01-01"},
            ]}),
            encoding="utf-8",
        )
    monkeypatch.setenv("HOME", str(home))
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    (home / ".grok").mkdir()
    (home / ".grok/config.toml").write_text("[mcp_servers.minni]\n")
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


@patch("minni.fleet_sync._audit_deploy_symlinks", return_value=OK_AUDIT)
@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0})
@patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_default_sync_restamps_grok(
    mock_kind, mock_wire, mock_prop, mock_kick, mock_grok, mock_audit,
):
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


def test_wire_status_ignores_a_non_wire_json_object():
    """Only a WireOutput-shaped document may set the status — an unrelated
    JSON object carrying a `status` key must not be mistaken for one."""
    assert _wire_status(json.dumps({"status": "skipped", "tool": "npm"})) is None


def test_wire_status_survives_output_after_the_document():
    doc = json.dumps({"schema": 1, "status": "failed", "results": [], "gc": {}})
    assert _wire_status(doc + "\n[wire] warning: trailing note\n") == "failed"


def test_wire_status_unparseable_is_none():
    assert _wire_status("not json at all") is None


def test_unparseable_wire_output_is_not_ok():
    """A nonzero wire whose status cannot be read is a failure, not a skip."""
    result = _fleet(_run_wire={"name": "wire_all", "exit_code": 1, "status": None})
    assert result.ok is False


# ── update_root.sh must not run unbounded (launchd timer drives it) ──────────


def _full_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "update_root.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    return checkout


@patch("minni.fleet_sync.subprocess.Popen")
@patch("minni.fleet_sync._detect_install_kind")
def test_full_sync_passes_a_timeout(mock_kind, mock_popen, tmp_path):
    mock_kind.return_value = ("editable-checkout", _full_checkout(tmp_path))
    mock_popen.return_value = MagicMock(**{"wait.return_value": 0, "pid": 4242})
    result = run_fleet_sync(full=True)
    assert result.ok
    timeout = mock_popen.return_value.wait.call_args.kwargs.get("timeout")
    assert isinstance(timeout, (int, float)) and timeout > 0


@patch("minni.fleet_sync.subprocess.Popen")
@patch("minni.fleet_sync._detect_install_kind")
def test_full_sync_runs_update_root_in_its_own_process_group(mock_kind, mock_popen, tmp_path):
    """So a timeout can kill git/pip/npm grandchildren too, instead of
    orphaning a blocked `git pull` on .git/index.lock."""
    mock_kind.return_value = ("editable-checkout", _full_checkout(tmp_path))
    mock_popen.return_value = MagicMock(**{"wait.return_value": 0, "pid": 4242})
    run_fleet_sync(full=True)
    assert mock_popen.call_args.kwargs.get("start_new_session") is True


@patch("minni.fleet_sync.os.killpg")
@patch("minni.fleet_sync.os.getpgid", return_value=4242)
@patch("minni.fleet_sync.subprocess.Popen")
@patch("minni.fleet_sync._detect_install_kind")
def test_full_sync_timeout_is_reported_as_failure(
    mock_kind, mock_popen, mock_getpgid, mock_killpg, tmp_path,
):
    mock_kind.return_value = ("editable-checkout", _full_checkout(tmp_path))
    mock_popen.return_value = MagicMock(pid=4242, **{
        "wait.side_effect": subprocess.TimeoutExpired(cmd="update_root.sh", timeout=1),
    })
    result = run_fleet_sync(full=True)
    assert result.ok is False
    assert "timed out" in result.message.lower()
    mock_killpg.assert_called_once_with(4242, signal.SIGKILL)


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


# ── Product standard: deploy by copy; a symlinked dist is a failure ─────────
#
# Operator standard 2026-08-04: every deployed artifact is a built, versioned
# COPY. A dist symlinked at the repo working tree executes uncommitted state
# and has no reproducible version, so `git stash` silently changes live
# behavior. scripts/check_deployments.py already classifies these WORKTREE;
# sync's verdict has to honor that judgment instead of reporting success.

SAMPLE_REPORT = """source HEAD: abc12345  (/repo)

  OK         dist abc12345                                            ~/.codex/plugins/minni
  WORKTREE   dist -> repo working tree (executes uncommitted state)    ~/.cursor/plugins/local/minni

2 deployment(s): 0 stale dist, 0 with content drift, 0 unknown vintage, 0 partly unreadable, 1 dist symlinked at the working tree, 0 with .mcp.json problems.
"""


def test_worktree_labels_read_only_worktree_rows():
    assert _worktree_linked_labels(SAMPLE_REPORT) == ["~/.cursor/plugins/local/minni"]


def test_worktree_labels_ignore_the_summary_sentence():
    """The tally line also says 'dist symlinked at the working tree'."""
    clean = SAMPLE_REPORT.replace(
        "  WORKTREE   dist -> repo working tree (executes uncommitted state)"
        "    ~/.cursor/plugins/local/minni\n",
        "",
    )
    assert _worktree_linked_labels(clean) == []


def _deployment_home(tmp_path: Path, monkeypatch, *, link_to: Path | None) -> Path:
    """Temp HOME carrying one cursor deployment — copy or worktree symlink."""
    home = tmp_path / "home"
    dest = home / ".cursor" / "plugins" / "local" / "minni"
    dest.mkdir(parents=True)
    if link_to is not None:
        (dest / "dist").symlink_to(link_to)
    else:
        (dest / "dist").mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_symlinked_dist_target_is_a_failure(tmp_path, monkeypatch):
    """Drives the real check_deployments.py against a real symlinked dist."""
    _deployment_home(tmp_path, monkeypatch, link_to=REPO / "plugins" / "minni" / "dist")
    step = _audit_deploy_symlinks(REPO, dry_run=False)
    assert step["exit_code"] == 1
    assert not step.get("skipped")
    assert step["worktree_linked"] == ["~/.cursor/plugins/local/minni"]


def test_copied_dist_target_passes(tmp_path, monkeypatch):
    """A real copied dist is not flagged, even though it is stale/unknown —
    sync audits symlinks here, not vintage."""
    _deployment_home(tmp_path, monkeypatch, link_to=None)
    step = _audit_deploy_symlinks(REPO, dry_run=False)
    assert step["exit_code"] == 0
    assert step["worktree_linked"] == []


def test_deploy_audit_reports_inconclusive_as_failure(tmp_path, monkeypatch):
    """A crashed audit must not read as a clean bill of health."""
    broken = tmp_path / "repo"
    (broken / "scripts").mkdir(parents=True)
    (broken / "scripts" / "check_deployments.py").write_text(
        "import sys\nsys.exit(3)\n", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    step = _audit_deploy_symlinks(broken, dry_run=False)
    assert step["exit_code"] == 1
    assert not step.get("skipped")
    assert "inconclusive" in str(step.get("error", "")).lower()


def test_deploy_audit_without_the_script_skips_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    step = _audit_deploy_symlinks(tmp_path / "no-repo", dry_run=False)
    assert step["skipped"] is True
    assert step["exit_code"] == 0
    assert step.get("reason")


def test_deploy_audit_dry_run_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch("minni.fleet_sync.subprocess.run") as run:
        step = _audit_deploy_symlinks(REPO, dry_run=True)
    run.assert_not_called()
    assert step["skipped"] is True


def test_symlinked_dist_makes_the_whole_sync_not_ok():
    """The pin: a symlinked dist target fails the verdict — not skipped,
    not merely warned."""
    result = _fleet(
        _audit_deploy_symlinks={
            "name": "deploy_symlink_audit",
            "exit_code": 1,
            "worktree_linked": ["~/.cursor/plugins/local/minni"],
        },
    )
    assert result.ok is False
    assert "deploy_symlink_audit" in result.message
    assert any("symlink" in a.lower() or "copy" in a.lower() for a in result.next_actions)


@patch("minni.fleet_sync._audit_deploy_symlinks", return_value=OK_AUDIT)
@patch("minni.fleet_sync._restamp_grok_hooks", return_value=OK_GROK)
@patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0})
@patch("minni.fleet_sync._run_propagate", return_value={"name": "propagate:x", "exit_code": 0})
@patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0})
@patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None))
def test_default_sync_audits_deploy_symlinks(
    mock_kind, mock_wire, mock_prop, mock_kick, mock_grok, mock_audit,
):
    result = run_fleet_sync()
    mock_audit.assert_called_once()
    assert any(s.get("name") == "deploy_symlink_audit" for s in result.steps)


# ── _run_wire's own skip decision (only status=skipped earns the pass) ──────
#
# This is the single load-bearing line of the wire fix: without direct
# coverage, inverting it to "any nonzero wire run is a skip" — the exact
# pre-fix bug — leaves the suite green.


def _wire_emitting(status: str, code: int):
    """Stand in for wire: print a real WireOutput document, return `code`."""
    def _run(_ns):
        print(json.dumps(
            {"schema": 1, "status": status, "results": [], "gc": {}}, indent=2,
        ))
        return code
    return _run


def test_run_wire_marks_only_a_skipped_status_as_skipped(capsys):
    with patch("minni.wire.flow.run_wire", _wire_emitting("skipped", 1)):
        step = _run_wire(from_repo=None, force_reinstall=True, prune=True, dry_run=False)
    assert step["exit_code"] == 1
    assert step["status"] == "skipped"
    assert step["skipped"] is True
    assert not _step_failed(step)


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_run_wire_does_not_launder_a_failure_into_a_skip(status):
    with patch("minni.wire.flow.run_wire", _wire_emitting(status, 1)):
        step = _run_wire(from_repo=None, force_reinstall=True, prune=True, dry_run=False)
    assert step["exit_code"] == 1
    assert step["status"] == status
    assert not step.get("skipped")
    assert _step_failed(step)


def test_run_wire_tees_the_report_to_stdout(capsys):
    """Capturing the JSON must not swallow the operator's copy of it."""
    with patch("minni.wire.flow.run_wire", _wire_emitting("ok", 0)):
        _run_wire(from_repo=None, force_reinstall=True, prune=True, dry_run=False)
    assert '"status": "ok"' in capsys.readouterr().out


def test_run_wire_without_a_readable_report_is_not_a_skip():
    """A nonzero wire that printed nothing parseable is a failure."""
    with patch("minni.wire.flow.run_wire", lambda _ns: 1):
        step = _run_wire(from_repo=None, force_reinstall=True, prune=True, dry_run=False)
    assert step["status"] is None
    assert not step.get("skipped")
    assert _step_failed(step)


# ── the launchd unit is optional; `minni up` installs are not failures ──────


def _launchctl(returncode: int, stderr: str = ""):
    return MagicMock(returncode=returncode, stdout="", stderr=stderr)


@patch("minni.fleet_sync.py_platform.system", return_value="Darwin")
def test_daemon_not_launchd_managed_is_a_skip(mock_sys):
    """launchctl exits 113 'Could not find service' when the optional unit is
    not loaded — that is a `minni up` install, not a redeploy failure."""
    with (
        patch("minni.fleet_sync.subprocess.run", return_value=_launchctl(
            113, 'Could not find service "com.minni.minnid" in domain for user gui: 501',
        )),
        # Never probe the real daemon from a unit test.
        patch("minni.fleet_sync._minnid_is_live", return_value=True),
    ):
        step = _kickstart_daemon()
    assert step["skipped"] is True
    assert step["exit_code"] == 0
    assert not _step_failed(step)


@patch("minni.fleet_sync.py_platform.system", return_value="Darwin")
def test_daemon_kickstart_failure_still_fails(mock_sys):
    with patch("minni.fleet_sync.subprocess.run", return_value=_launchctl(1, "boom")):
        step = _kickstart_daemon()
    assert not step.get("skipped")
    assert _step_failed(step)


# ── verdict trust boundary ─────────────────────────────────────────────────


def test_a_step_without_an_exit_code_counts_as_failed():
    """A step dict that never established success must not pass by default."""
    assert _step_failed({"name": "mystery", "error": "boom"}) is True
    assert _step_failed({"name": "mystery", "skipped": True}) is False


def test_grok_restamp_survives_a_propagate_contract_change(tmp_path, monkeypatch):
    """A propagate.py returning something unexpected must fail the step, not
    crash `minni sync` with an AttributeError."""
    _grok_home(tmp_path, monkeypatch)
    script = tmp_path / "bad_propagate.py"
    script.write_text(
        "def update_grok_hooks(install_root):\n    return None\n\n"
        "def write_grok_rules():\n    return None\n",
        encoding="utf-8",
    )
    with patch("minni.fleet_sync._propagate_py", return_value=script):
        step = _restamp_grok_hooks(None, dry_run=False)
    assert step["exit_code"] == 1
    assert step.get("error")


def test_wire_only_leaves_propagate_owned_grok_alone():
    """--wire-only means propagate-managed surfaces are not touched, and Grok
    hooks/rules are one of them."""
    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0}),
        patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0}),
        patch("minni.fleet_sync._audit_deploy_symlinks", return_value=dict(OK_AUDIT)),
        patch("minni.fleet_sync._restamp_grok_hooks") as grok,
    ):
        result = run_fleet_sync(propagate_hosts=False)
    grok.assert_not_called()
    assert result.ok


def test_deploy_audit_reports_what_it_did_not_judge(tmp_path, monkeypatch):
    """A clean audit states its blind spots rather than implying total cover."""
    home = tmp_path / "home"
    (home / ".minni" / "plugin" / "current").mkdir(parents=True)
    (home / ".minni" / "plugin" / "current" / "dist").mkdir()
    monkeypatch.setenv("HOME", str(home))
    step = _audit_deploy_symlinks(REPO, dry_run=False)
    assert step["exit_code"] == 0
    assert any("plugin/current" in n for n in step["not_judged"])


# ── round-3 hardening: fail-closed everywhere the verdict can be fooled ─────


def test_wire_status_refuses_two_disagreeing_documents():
    """A decoy WireOutput embedded in noise must not overwrite the real
    status — ambiguous is unreadable, and unreadable is a failure."""
    real = json.dumps({"schema": 1, "status": "failed", "results": [], "gc": {}})
    decoy = json.dumps({"schema": 1, "status": "skipped", "results": [], "gc": {}})
    assert _wire_status(f"{real}\nnpm ERR! cache {decoy} (retrying)\n") is None


def test_a_decoy_skipped_document_cannot_launder_a_failed_wire():
    real = json.dumps({"schema": 1, "status": "failed", "results": [], "gc": {}})
    decoy = json.dumps({"schema": 1, "status": "skipped", "results": [], "gc": {}})

    def _run(_ns):
        print(f"{real}\nnpm ERR! {decoy} (retrying)")
        return 1

    with patch("minni.wire.flow.run_wire", _run):
        step = _run_wire(from_repo=None, force_reinstall=True, prune=True, dry_run=False)
    assert not step.get("skipped")
    assert _step_failed(step)


def test_wire_status_accepts_the_same_document_seen_twice():
    doc = json.dumps({"schema": 1, "status": "skipped", "results": [], "gc": {}})
    assert _wire_status(f"{doc}\n{doc}\n") == "skipped"


def test_run_wire_turns_an_exception_into_a_failed_step():
    """`minni sync` owes the launchd timer a JSON verdict, not a traceback."""
    def _boom(_ns):
        raise RuntimeError("wire blew up")

    with patch("minni.wire.flow.run_wire", _boom):
        step = _run_wire(from_repo=None, force_reinstall=True, prune=True, dry_run=False)
    assert step["exit_code"] == 1
    assert "wire blew up" in step["error"]
    assert _step_failed(step)


@patch("minni.fleet_sync.py_platform.system", return_value="Darwin")
def test_daemon_not_loaded_and_not_live_is_a_failure(mock_sys):
    """An unloaded launchd unit with no daemon answering is a real failure —
    the same 113 a `minni up` install produces, told apart by liveness."""
    with (
        patch("minni.fleet_sync.subprocess.run", return_value=_launchctl(
            113, 'Could not find service "com.minni.minnid" in domain for user gui: 501',
        )),
        patch("minni.fleet_sync._minnid_is_live", return_value=False),
    ):
        step = _kickstart_daemon()
    assert _step_failed(step)


@patch("minni.fleet_sync.py_platform.system", return_value="Darwin")
def test_daemon_113_for_a_different_service_is_not_laundered(mock_sys):
    """Only minnid's own absence excuses the restart."""
    with (
        patch("minni.fleet_sync.subprocess.run", return_value=_launchctl(
            113, 'Could not find service "com.other.thing" in domain for user gui: 501',
        )),
        patch("minni.fleet_sync._minnid_is_live", return_value=True),
    ):
        step = _kickstart_daemon()
    assert _step_failed(step)


@patch("minni.fleet_sync.py_platform.system", return_value="Darwin")
def test_daemon_failure_with_a_misleading_stderr_still_fails(mock_sys):
    """A non-113 failure must not be excused by stderr wording alone."""
    with (
        patch("minni.fleet_sync.subprocess.run", return_value=_launchctl(
            5, 'Could not find service "com.minni.minnid"',
        )),
        patch("minni.fleet_sync._minnid_is_live", return_value=True),
    ):
        step = _kickstart_daemon()
    assert _step_failed(step)


def test_manual_restart_reaches_the_operator():
    """A skipped restart is only honest if the human is told to do it."""
    result = _fleet(_kickstart_daemon={
        "name": "restart_daemon",
        "exit_code": 0,
        "skipped": True,
        "reason": "minnid is not launchd-managed and is live",
        "manual_restart": True,
    })
    assert result.ok
    assert any("minni down && minni up" in a for a in result.next_actions)


def test_an_all_skipped_run_does_not_claim_a_redeploy():
    skipped = lambda name: {"name": name, "exit_code": 0, "skipped": True}  # noqa: E731
    result = _fleet(
        _run_wire={"name": "wire_all", "exit_code": 1, "status": "skipped", "skipped": True},
        _run_propagate=skipped("propagate:x"),
        _kickstart_daemon=skipped("restart_daemon"),
        _restamp_grok_hooks=skipped("grok_hooks_rules"),
        _audit_deploy_symlinks=skipped("deploy_symlink_audit"),
    )
    assert result.ok
    assert "redeployed" not in result.message
    assert "every step skipped" in result.message


def test_the_message_counts_what_actually_ran():
    result = _fleet()
    assert "step(s) applied" in result.message


def test_audit_blind_spots_reach_the_operator():
    result = _fleet(_audit_deploy_symlinks={
        "name": "deploy_symlink_audit",
        "exit_code": 0,
        "worktree_linked": [],
        "not_judged": ["~/.minni/plugin/current: skipped (legacy path)"],
    })
    assert any("not_judged" in a for a in result.next_actions)


def test_audit_fails_when_the_tally_exceeds_what_it_could_read():
    """A row-scrape that misses what check_deployments counted is fail-open;
    an unreadable audit must fail instead."""
    report = (
        "  WEIRDROW  something unparseable\n\n"
        "1 deployment(s): 0 stale dist, 0 with content drift, 0 unknown vintage, "
        "0 partly unreadable, 1 dist symlinked at the working tree, "
        "0 with .mcp.json problems.\n"
    )
    proc = MagicMock(returncode=1, stdout=report, stderr="")
    with patch("minni.fleet_sync._check_deployments_py", return_value=Path("x.py")), \
         patch("minni.fleet_sync.subprocess.run", return_value=proc):
        step = _audit_deploy_symlinks(REPO, dry_run=False)
    assert step["exit_code"] == 1
    assert "inconclusive" in step["error"]


def test_audit_discloses_the_repo_payload_exclusion(tmp_path, monkeypatch):
    """fleet_sync itself excludes the staged payload; check_deployments emits
    no NOTE for that, so the blind-spot list has to name it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    step = _audit_deploy_symlinks(REPO, dry_run=False)
    assert any("plugin_payload" in n for n in step["not_judged"])


def test_a_broken_deploy_honesty_import_is_not_silently_packaged():
    """Swallowing this would wire from the stale bundled payload and skip the
    deploy audit, with every step exiting 0."""
    import minni.fleet_sync as fs

    with patch.object(fs.importlib.util, "find_spec", return_value=object()), \
         patch.dict("sys.modules", {"minni.minnid_runtime.deploy_honesty": None}):
        with pytest.raises(ImportError):
            fs._source_checkout()


# ── coverage the round-2 mutants proved missing ────────────────────────────


@patch("minni.fleet_sync.subprocess.Popen")
@patch("minni.fleet_sync._detect_install_kind")
def test_full_sync_nonzero_update_root_fails(mock_kind, mock_popen, tmp_path):
    """The unattended launchd path: update_root.sh exiting nonzero is a
    failed sync."""
    mock_kind.return_value = ("editable-checkout", _full_checkout(tmp_path))
    mock_popen.return_value = MagicMock(**{"wait.return_value": 9, "pid": 1})
    result = run_fleet_sync(full=True)
    assert result.ok is False


def test_run_propagate_plumbs_a_real_nonzero_exit(tmp_path, monkeypatch):
    """The pre-fix laundering bug transplanted to propagate: a cursor failure
    must arrive as a nonzero step."""
    monkeypatch.setenv("HOME", str(tmp_path))
    executable = tmp_path / "cursor"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text('{"mcpServers":{"minni":{}}}')
    script = tmp_path / "propagate.py"
    script.write_text("import sys\nassert '--existing-only' in sys.argv\nsys.exit(7)\n", encoding="utf-8")
    with patch("minni.fleet_sync._propagate_py", return_value=script):
        step = _run_propagate("cursor", None, dry_run=False)
    assert step["exit_code"] == 7
    assert _step_failed(step)


def test_dry_run_never_touches_propagate_managed_hosts():
    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0}),
        patch("minni.fleet_sync._run_propagate") as prop,
    ):
        result = run_fleet_sync(dry_run=True)
    prop.assert_not_called()
    assert result.ok


@pytest.mark.parametrize(
    ("hooks_ok", "rules_ok"), [(True, False), (False, True), (False, False)],
)
def test_grok_restamp_needs_both_hooks_and_rules(tmp_path, monkeypatch, hooks_ok, rules_ok):
    _grok_home(tmp_path, monkeypatch)
    script = tmp_path / f"prop_{hooks_ok}_{rules_ok}.py"
    script.write_text(
        f"def update_grok_hooks(install_root):\n    return {{'installed': {hooks_ok!r}}}\n\n"
        f"def write_grok_rules():\n    return {{'installed': {rules_ok!r}}}\n",
        encoding="utf-8",
    )
    with patch("minni.fleet_sync._propagate_py", return_value=script):
        step = _restamp_grok_hooks(None, dry_run=False)
    assert step["exit_code"] == 1


def test_wire_status_requires_the_schema_field():
    """`results` + `status` alone is not a WireOutput."""
    assert _wire_status(json.dumps({"status": "skipped", "results": []})) is None


# ── liveness probe: a wedged daemon is not a live one ───────────────────────


@pytest.fixture
def short_home(monkeypatch):
    """A HOME short enough for an AF_UNIX path (~104 char limit)."""
    import shutil as _shutil
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="mn", dir="/tmp"))
    monkeypatch.setenv("HOME", str(home))
    try:
        yield home
    finally:
        _shutil.rmtree(home, ignore_errors=True)


def _bind_socket(home: Path, *, accepting: bool):
    """Bind a fake minnid socket under a temp HOME. Returns the server."""
    import socket as _socket
    import threading

    run = home / ".minni" / "run"
    run.mkdir(parents=True, exist_ok=True)
    srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.bind(str(run / "minnid.sock"))
    srv.listen(5)
    if accepting:
        def _serve():
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.recv(8192)
                    conn.sendall(b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n')
            except OSError:
                pass
        threading.Thread(target=_serve, daemon=True).start()
    return srv


def test_liveness_true_only_for_a_responding_daemon(short_home):
    srv = _bind_socket(short_home, accepting=True)
    try:
        assert _minnid_is_live(timeout=5.0) is True
    finally:
        srv.close()


def test_liveness_false_for_a_wedged_daemon(short_home):
    """Listening but never accepting: connect() succeeds, so a bare connect
    would call this healthy. The round-trip must not."""
    srv = _bind_socket(short_home, accepting=False)
    try:
        assert _minnid_is_live(timeout=0.5) is False
    finally:
        srv.close()


def test_liveness_false_without_a_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _minnid_is_live(timeout=0.5) is False


def test_liveness_false_for_a_stale_socket_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    run = tmp_path / ".minni" / "run"
    run.mkdir(parents=True)
    (run / "minnid.sock").write_text("", encoding="utf-8")
    assert _minnid_is_live(timeout=0.5) is False


@patch("minni.fleet_sync.py_platform.system", return_value="Linux")
def test_non_macos_skip_still_tells_the_operator_to_restart(mock_sys):
    step = _kickstart_daemon()
    assert step["skipped"] is True
    assert step["manual_restart"] is True


def test_a_broken_install_returns_a_verdict_not_a_traceback():
    """The launchd timer reads JSON; a half-installed package must not hand
    it a stack trace."""
    with patch(
        "minni.fleet_sync._detect_install_kind",
        side_effect=ModuleNotFoundError("No module named 'minni.minnid_runtime'"),
    ):
        result = run_fleet_sync()
    assert result.ok is False
    assert "install kind" in result.message


def test_wire_only_still_audits_deploy_symlinks():
    """The deploy-by-copy standard is not a propagate-managed surface."""
    with (
        patch("minni.fleet_sync._detect_install_kind", return_value=("packaged", None)),
        patch("minni.fleet_sync._run_wire", return_value={"name": "wire_all", "exit_code": 0}),
        patch("minni.fleet_sync._kickstart_daemon", return_value={"name": "restart_daemon", "exit_code": 0}),
        patch("minni.fleet_sync._audit_deploy_symlinks", return_value=dict(OK_AUDIT)) as audit,
    ):
        run_fleet_sync(propagate_hosts=False)
    audit.assert_called_once()


def test_audit_excludes_the_staged_repo_payload(tmp_path, monkeypatch):
    """Parity with update_root.sh: the staged payload is a release artifact,
    not a live host surface."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch("minni.fleet_sync.subprocess.run") as run:
        run.return_value = MagicMock(
            returncode=0, stdout="No deployments discovered.\n", stderr="",
        )
        _audit_deploy_symlinks(REPO, dry_run=False)
    assert run.call_args.kwargs["env"]["MINNI_CHECK_DEPLOYMENTS_SKIP_REPO"] == "1"


@pytest.mark.parametrize('leftover_config', [False, True])
def test_optional_grok_skips_before_loading_propagation(tmp_path, monkeypatch, leftover_config):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PATH', '')
    if leftover_config:
        (tmp_path / '.grok').mkdir()
        (tmp_path / '.grok/config.toml').write_text('[mcp_servers.minni]\n')
    with patch('minni.fleet_sync._propagate_py') as locate:
        result = _restamp_grok_hooks(None, dry_run=False)
    locate.assert_not_called()
    assert result['skipped'] is True
    assert result['exit_code'] == 0
    assert not (tmp_path / '.minni').exists()


def test_malformed_grok_config_is_visible_without_loading_propagation(tmp_path, monkeypatch):
    root = _grok_home(tmp_path, monkeypatch)
    config = tmp_path / 'home/.grok/config.toml'
    config.write_text('bad = [')
    with patch('minni.fleet_sync._propagate_py') as locate:
        result = _restamp_grok_hooks(None, dry_run=False)
    locate.assert_not_called()
    assert result['status'] == 'failed'
    assert _step_failed(result)
    assert config.read_text() == 'bad = ['
    assert root.is_dir()

"""Tests for the propagate.py verify ok-predicate (B6ii, audit C4).

The old predicate passed vacuously: when the daemon socket was missing the
``daemon_read_has_*`` keys were simply absent (only ``daemon_read_error`` was
set), and ``all()`` over the present keys reported ok=True. The honest
predicate requires every daemon-read key to be present AND True, and any
``*_error`` key forces ok=False.
"""

import sys
from pathlib import Path

SCRIPTS_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "minni" / "skills" / "minni-propagation" / "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import propagate  # noqa: E402


def _good_checks():
    return {
        "agent_api_returncode": 0,
        "agent_api_has_identity": True,
        "agent_api_has_map_rule": True,
        "agent_api_no_personality": True,
        "daemon_read_has_identity": True,
        "daemon_read_has_map_rule": True,
    }


def test_verify_ok_all_required_true():
    assert propagate.verify_ok(_good_checks()) is True


def test_missing_daemon_socket_reports_not_ok():
    """B6ii gate: socket missing -> daemon_read_error set, daemon keys absent
    -> ok must be False (previously passed vacuously)."""
    checks = _good_checks()
    del checks["daemon_read_has_identity"]
    del checks["daemon_read_has_map_rule"]
    checks["daemon_read_error"] = "socket missing: /tmp/nope.sock"
    assert propagate.verify_ok(checks) is False


def test_absent_daemon_keys_without_error_still_not_ok():
    checks = _good_checks()
    del checks["daemon_read_has_identity"]
    del checks["daemon_read_has_map_rule"]
    assert propagate.verify_ok(checks) is False


def test_any_error_key_forces_not_ok():
    checks = _good_checks()
    checks["daemon_read_error"] = "connection refused"
    assert propagate.verify_ok(checks) is False


def test_false_required_check_not_ok():
    checks = _good_checks()
    checks["daemon_read_has_map_rule"] = False
    assert propagate.verify_ok(checks) is False


# ── verify() COMMAND-path coverage (C9 follow-up) ────────────────────────────
# The predicate above is pure; these exercise the actual command function
# (agent_api subprocess probe + daemon socket read + JSON report + exit code).
# Hermetic: subprocess.run and socket_rpc are faked, the socket path lives in
# tmp_path — no live engine, daemon, or ~/.minni state is touched.

import argparse  # noqa: E402
import json  # noqa: E402
import types  # noqa: E402


def _identity_text(agent: str) -> str:
    return (
        f"## Agent Identity: {agent.title()}\n"
        "Minni gives hosted agents a map of prior work.\n"
        "It does not define personality.\n"
    )


def _fake_agent_api(monkeypatch, agent: str, returncode: int = 0):
    calls = []

    def fake_run(cmd, cwd=None, text=None, capture_output=True, check=False):
        calls.append(cmd)
        return types.SimpleNamespace(
            returncode=returncode, stdout=_identity_text(agent), stderr=""
        )

    monkeypatch.setattr(propagate.subprocess, "run", fake_run)
    return calls


def test_verify_command_ok_path(tmp_path, capsys, monkeypatch):
    """Socket present + healthy agent_api + daemon read -> ok, exit 0."""
    sock = tmp_path / "minnid.sock"
    sock.write_text("")  # verify only checks existence before socket_rpc
    calls = _fake_agent_api(monkeypatch, "codex")
    monkeypatch.setattr(
        propagate,
        "socket_rpc",
        lambda *_a, **_k: {"result": {"context": _identity_text("codex")}},
    )

    args = argparse.Namespace(agent="codex", workspace=str(tmp_path), socket=str(sock))
    rc = propagate.verify(args)
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["ok"] is True
    assert report["checks"]["agent_api_has_identity"] is True
    assert report["checks"]["daemon_read_has_identity"] is True
    assert report["checks"]["daemon_read_has_map_rule"] is True
    # The agent_api probe actually ran (command path, not just the predicate).
    assert calls and "--identity" in calls[0]


def test_verify_command_socket_missing_exits_1(tmp_path, capsys, monkeypatch):
    """Missing socket -> daemon_read_error + ok False + exit 1 (B6ii gate,
    end-to-end through the command function)."""
    _fake_agent_api(monkeypatch, "codex")
    args = argparse.Namespace(
        agent="codex", workspace=str(tmp_path), socket=str(tmp_path / "nope.sock")
    )
    rc = propagate.verify(args)
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["ok"] is False
    assert "socket missing" in report["checks"]["daemon_read_error"]
    assert "daemon_read_has_identity" not in report["checks"]


def test_verify_command_socket_rpc_failure_exits_1(tmp_path, capsys, monkeypatch):
    """socket exists but the RPC blows up -> daemon_read_error + exit 1."""
    sock = tmp_path / "minnid.sock"
    sock.write_text("")
    _fake_agent_api(monkeypatch, "codex")

    def boom(*_a, **_k):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(propagate, "socket_rpc", boom)
    args = argparse.Namespace(agent="codex", workspace=str(tmp_path), socket=str(sock))
    rc = propagate.verify(args)
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["ok"] is False
    assert "connection refused" in report["checks"]["daemon_read_error"]


def test_verify_command_via_main_argparse_wiring(tmp_path, capsys, monkeypatch):
    """`propagate.py --socket ... verify --agent ... --workspace ...` reaches
    verify() through main()'s subparser wiring and propagates the exit code."""
    _fake_agent_api(monkeypatch, "codex")
    monkeypatch.setattr(
        "sys.argv",
        [
            "propagate.py",
            "--socket", str(tmp_path / "nope.sock"),
            "verify",
            "--agent", "codex",
            "--workspace", str(tmp_path),
        ],
    )
    rc = propagate.main()
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["ok"] is False
    assert "socket missing" in report["checks"]["daemon_read_error"]

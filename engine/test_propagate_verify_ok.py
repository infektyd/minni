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

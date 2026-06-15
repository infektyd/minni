"""Provenance gate core tests.

The gate lives at minnid dispatch: resolve provenance once, route known callers
through, and send unresolved callers to recovery instead of a silent/default
identity path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minnid  # type: ignore
import principal  # type: ignore
from principal import EffectivePrincipal, IdentityMismatchError


def test_recover_machine_packet_is_never_silent_or_default():
    packet = minnid.recover(
        reason="identity_mismatch",
        caller_ctx={"method": "search", "supplied_agent_id": "mystery"},
        render_mode="machine",
    )

    assert packet["status"] == "recovery_required"
    assert packet["ok"] is False
    assert packet["reason"] == "identity_mismatch"
    assert packet["identity"] is None
    assert packet["route"]["zone"] == "pre_identity"
    assert "status" in packet["route"]["allowed_methods"]
    assert "stamp" in " ".join(packet["remediation"]).lower()
    assert json.dumps(packet)
    assert "unknown-agent" not in json.dumps(packet)


def test_recover_human_render_mode_explains_next_step():
    message = minnid.recover(
        reason="missing_provenance",
        caller_ctx={"method": "learn"},
        render_mode="human",
    )

    assert isinstance(message, str)
    assert "missing_provenance" in message
    assert "learn" in message
    assert "status" in message
    assert "unknown-agent" not in message


def test_resolve_provenance_returns_specific_known_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    principals = tmp_path / "principals"
    principals.mkdir()
    codex = principals / "codex.json"
    codex.write_text(
        json.dumps(
            {
                "agent_id": "codex",
                "workspace_id": "default",
                "capabilities": ["search", "read"],
                "allowed_vault_roots": [str(tmp_path / "codex-vault")],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(codex, 0o600)

    original_resolve = principal.resolve_effective_principal

    def _patched_resolve(*, supplied_agent_id=None, transport="uds", principals_dir=None, operator_context=False):
        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=principals_dir or principals,
            operator_context=operator_context,
        )

    monkeypatch.setattr(minnid, "resolve_effective_principal", _patched_resolve)

    result = minnid.resolve_provenance(
        {"jsonrpc": "2.0", "id": 1, "method": "search", "params": {"agent_id": "codex"}}
    )

    assert result.principal is not None
    assert result.principal.agent_id == "codex"
    assert result.recovery is None


def test_dispatch_routes_unresolved_identity_to_recovery_before_handler(monkeypatch: pytest.MonkeyPatch):
    def _boom_resolve(**_kwargs):
        raise IdentityMismatchError("mystery", "main", "no registered principal")

    def _handler_should_not_run(_params, _request_id):
        raise AssertionError("handler ran before provenance recovery")

    monkeypatch.setattr(minnid, "resolve_effective_principal", _boom_resolve)
    monkeypatch.setitem(minnid._METHODS, "gate_test", _handler_should_not_run)

    response = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": "gate-1",
            "method": "gate_test",
            "params": {"agent_id": "mystery"},
        }
    )

    assert "error" not in response
    result = response["result"]
    assert result["status"] == "recovery_required"
    assert result["reason"] == "identity_mismatch"
    assert result["caller"]["method"] == "gate_test"
    assert result["identity"] is None


def test_gate_shared_reports_resolved_principal_before_shared_operation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        minnid,
        "resolve_effective_principal",
        lambda **_kwargs: EffectivePrincipal(
            agent_id="codex",
            workspace_id="workspace-minni",
            capabilities=["*"],
        ),
    )

    response = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": "shared-1",
            "method": "gate.shared",
            "params": {"agent_id": "codex", "operation": "plan.update"},
        }
    )

    assert "error" not in response
    assert response["result"]["gate"] == "minnid"
    assert response["result"]["principal"] == "codex"
    assert response["result"]["operation"] == "plan.update"


def test_reload_runtime_config_clears_agent_scope_cache(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    local = principals / "local.json"
    local.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "platform_agent_ids": ["codex"],
                "legacy_agent_ids": ["old-codex"],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(local, 0o600)

    first = principal.agent_scope_for("codex", principals_dir=principals)
    assert first == ["codex", "old-codex"]

    local.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "platform_agent_ids": ["codex"],
                "legacy_agent_ids": ["old-codex", "new-codex"],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(local, 0o600)

    assert principal.agent_scope_for("codex", principals_dir=principals) == first
    minnid._reload_runtime_config()
    assert principal.agent_scope_for("codex", principals_dir=principals) == [
        "codex",
        "old-codex",
        "new-codex",
    ]

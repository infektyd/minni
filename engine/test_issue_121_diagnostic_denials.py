"""Issue #121: fresh-install default-denies must be diagnostic, never a bare -32004.

Two distinct default-deny roots in principal.py must stay fail-closed but
fail LOUD at the dispatch capability gate:

- Root A (unknown identity): a supplied non-reserved agent_id with no
  principals/<id>.json gets the structured recovery route (the same shape
  gate.shared already returns), telling the operator to author the file.
- Root B (reserved id): a wire claim of "main"/"operator" without operator
  context gets a distinct reserved_agent_id diagnostic, not a message
  byte-identical to a genuinely capability-less agent.

Correctly-provisioned callers (deny_reason is None) keep the exact prior
wire behavior, including the bare capability_denied for missing grants.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minnid
import minnid_runtime.provenance as provenance
import principal
from principal import EffectivePrincipal, resolve_effective_principal


def _use_principals_dir(monkeypatch, principals: Path):
    """Route the real resolver at a hermetic principals dir for dispatch tests."""
    original = principal.resolve_effective_principal

    def _patched(*, supplied_agent_id=None, transport="uds", principals_dir=None, operator_context=False):
        return original(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=principals_dir or principals,
            operator_context=operator_context,
        )

    monkeypatch.setattr(provenance, "resolve_effective_principal", _patched)


def _rpc(method, params, request_id=1):
    return minnid._dispatch_sync(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )


def _author(principals: Path, agent_id: str, capabilities: list[str]):
    f = principals / f"{agent_id}.json"
    f.write_text(
        json.dumps({"agent_id": agent_id, "capabilities": capabilities}),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)
    return f


# ── resolver: deny_reason carries WHY the stamp is default-deny ─────────────

def test_fresh_install_fileless_supplied_id_denies_with_unknown_identity_reason(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    p = resolve_effective_principal(
        supplied_agent_id="claude-code", transport="uds", principals_dir=principals
    )
    # Fail-closed outcome unchanged (Root A)...
    assert p.agent_id == "claude-code"
    assert p.capabilities == []
    assert p.allowed_vault_roots == []
    # ...but the stamp now says why, so dispatch can report it.
    assert p.deny_reason == "unknown_identity"


def test_strict_install_fileless_supplied_id_also_carries_unknown_identity(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    _author(principals, "main", ["*"])
    (principals / "main.json").rename(principals / "local.json")
    p = resolve_effective_principal(
        supplied_agent_id="gemini", transport="uds", principals_dir=principals
    )
    assert p.capabilities == []
    assert p.deny_reason == "unknown_identity"


@pytest.mark.parametrize("reserved", ["main", "operator"])
def test_reserved_id_denies_with_reserved_reason(tmp_path: Path, monkeypatch, reserved: str):
    monkeypatch.delenv("MINNI_LOCAL_OPERATOR", raising=False)
    principals = tmp_path / "principals"
    principals.mkdir()
    p = resolve_effective_principal(
        supplied_agent_id=reserved, transport="uds", principals_dir=principals
    )
    assert p.agent_id == reserved
    assert p.capabilities == []
    assert p.deny_reason == "reserved_agent_id"


def test_reserved_id_with_local_operator_env_resolves_operator(tmp_path: Path, monkeypatch):
    """MINNI_LOCAL_OPERATOR is the daemon-env (never wire) operator signal
    principal.py:289 already trusts — honoring it for a reserved-id claim is
    the sanctioned escape hatch, and stays out of reach of wire callers."""
    monkeypatch.setenv("MINNI_LOCAL_OPERATOR", "1")
    principals = tmp_path / "principals"
    principals.mkdir()
    p = resolve_effective_principal(
        supplied_agent_id="main", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "main"
    assert p.capabilities == ["*"]
    assert p.deny_reason is None


def test_reserved_operator_alias_with_local_operator_env_resolves_operator(
    tmp_path: Path, monkeypatch
):
    """PR #132 review (P2): under MINNI_LOCAL_OPERATOR the reserved alias
    'operator' must normalize to the stamped local operator ('main' on a
    fresh install) instead of raising IdentityMismatchError('operator' !=
    'main') — otherwise the escape hatch is unusable for that alias."""
    monkeypatch.setenv("MINNI_LOCAL_OPERATOR", "1")
    principals = tmp_path / "principals"
    principals.mkdir()
    p = resolve_effective_principal(
        supplied_agent_id="operator", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "main"
    assert p.capabilities == ["*"]
    assert p.deny_reason is None


def test_reserved_operator_alias_with_local_operator_env_strict_main_json(
    tmp_path: Path, monkeypatch
):
    """Same alias normalization against an authored operator file: a claim of
    'operator' under MINNI_LOCAL_OPERATOR resolves to the main.json stamp."""
    monkeypatch.setenv("MINNI_LOCAL_OPERATOR", "1")
    principals = tmp_path / "principals"
    principals.mkdir()
    _author(principals, "main", ["*"])
    p = resolve_effective_principal(
        supplied_agent_id="operator", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "main"
    assert p.capabilities == ["*"]
    assert p.deny_reason is None


def test_authored_principal_has_no_deny_reason(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    _author(principals, "codex", ["search"])
    p = resolve_effective_principal(
        supplied_agent_id="codex", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "codex"
    assert p.capabilities == ["search"]
    assert p.deny_reason is None


# ── dispatch: Root A → structured recovery route, not bare -32004 ───────────

def test_fresh_install_gated_method_returns_recovery_route(monkeypatch, tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    _use_principals_dir(monkeypatch, principals)

    resp = _rpc("search", {"agent_id": "claude-code", "query": "anything"})

    assert "error" not in resp, resp
    result = resp["result"]
    assert result["status"] == "recovery_required"
    assert result["reason"] == "unknown_identity"
    assert result["caller"]["method"] == "search"
    assert result["caller"]["supplied_agent_id"] == "claude-code"
    assert result["route"]["zone"] == "pre_identity"
    remediation = " ".join(result["remediation"])
    assert "author_principals" in remediation
    assert "0600" in remediation


def test_fresh_install_handoff_returns_recovery_route(monkeypatch, tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    _use_principals_dir(monkeypatch, principals)

    resp = _rpc(
        "daemon.handoff",
        {"from_agent": "claude-code", "to_agent": "codex", "packet": {}},
    )

    assert "error" not in resp, resp
    result = resp["result"]
    assert result["status"] == "recovery_required"
    assert result["reason"] == "unknown_identity"
    assert result["caller"]["supplied_agent_id"] == "claude-code"


# ── dispatch: Root B → distinct reserved-id diagnostic ──────────────────────

def test_reserved_agent_id_gets_distinct_diagnostic(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MINNI_LOCAL_OPERATOR", raising=False)
    principals = tmp_path / "principals"
    principals.mkdir()
    _use_principals_dir(monkeypatch, principals)

    resp = _rpc("search", {"agent_id": "main", "query": "anything"})

    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    msg = err.get("message", "")
    assert "reserved_agent_id" in msg
    assert "omit agent_id" in msg
    assert "MINNI_LOCAL_OPERATOR" in msg


def test_reserved_from_agent_handoff_gets_distinct_diagnostic(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MINNI_LOCAL_OPERATOR", raising=False)
    principals = tmp_path / "principals"
    principals.mkdir()
    _use_principals_dir(monkeypatch, principals)

    resp = _rpc(
        "daemon.handoff",
        {"from_agent": "operator", "to_agent": "codex", "packet": {}},
    )

    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "reserved_agent_id" in err.get("message", "")
    assert "'operator'" in err.get("message", "")


def test_gate_shared_reserved_id_gets_distinct_diagnostic(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MINNI_LOCAL_OPERATOR", raising=False)
    principals = tmp_path / "principals"
    principals.mkdir()
    _use_principals_dir(monkeypatch, principals)

    resp = _rpc("gate.shared", {"agent_id": "main", "operation": "plan.create"})

    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "reserved_agent_id" in err.get("message", "")


# ── provisioned callers keep the exact prior deny shape ─────────────────────

def test_provisioned_caller_missing_grant_keeps_bare_capability_denied(monkeypatch, tmp_path: Path):
    """A caller with an authored principal but without the method's grant must
    keep the byte-identical -32004 capability_denied (no recovery route)."""
    principals = tmp_path / "principals"
    principals.mkdir()
    _author(principals, "codex", ["search"])
    _use_principals_dir(monkeypatch, principals)

    resp = _rpc(
        "daemon.handoff",
        {"from_agent": "codex", "to_agent": "claude-code", "packet": {}},
    )

    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "capability_denied" in err.get("message", "")
    assert "reserved_agent_id" not in err.get("message", "")


# ── authoring the principal file unlocks the flow end-to-end ────────────────

def _patch_db(monkeypatch, tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "issue121.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    return db_obj


def _handoff_params():
    return {
        "from_agent": "codex",
        "to_agent": "claude-code",
        "packet": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "kind": "handoff",
            "task": "Review auth migration",
            "envelope": '<sovereign:context event="Handoff">plain context</sovereign:context>',
            "wikilink_refs": [],
            "trace_id": "trace-issue-121",
        },
    }


def test_authoring_principal_file_unlocks_handoff_end_to_end(monkeypatch, tmp_path: Path):
    _patch_db(monkeypatch, tmp_path)
    principals = tmp_path / "principals"
    principals.mkdir()
    _use_principals_dir(monkeypatch, principals)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )

    # Fresh install: the gated handoff is denied with the recovery route.
    denied = _rpc("daemon.handoff", _handoff_params())
    assert "error" not in denied, denied
    assert denied["result"]["status"] == "recovery_required"
    assert denied["result"]["reason"] == "unknown_identity"

    # Operator authors principals/codex.json (the documented remediation)...
    _author(principals, "codex", ["handoff"])

    # ...and the same call now succeeds end-to-end.
    ok = _rpc("daemon.handoff", _handoff_params(), request_id=2)
    assert "error" not in ok, ok
    assert ok["result"]["status"] == "ok"
    assert ok["result"]["delivered"] is True
    assert len(list((recipient / "inbox").glob("*.json"))) == 1


def test_deny_reason_default_is_none_for_plain_principals():
    p = EffectivePrincipal(agent_id="codex", capabilities=["*"])
    assert p.deny_reason is None

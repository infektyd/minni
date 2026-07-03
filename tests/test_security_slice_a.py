"""Slice A security regressions: capability/principal gates (P1, P3).

P2/P5 (self-owned candidate self-approval) is covered in test_rpc_authz.py
(test_resolve_candidate_owner_with_restricted_caps_may_reject_not_accept and
test_resolve_candidate_owner_with_operator_cap_can_accept).
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.principal import (
    EffectivePrincipal,
    from_local_transport,
    is_operator_principal,
    resolve_effective_principal,
)


# ── P1: daemon.compile dry_run=false requires operator ──────────────────────


def _afm_context(monkeypatch, tmp_path, *, submit_called):
    """Build an AFMContext whose write side effects record if reached."""
    from minni.minnid_runtime import afm as afm_mod

    # afm_loop must be "enabled" for compile to get past the availability guard
    # and reach the write path we are gating.
    cfg = types.SimpleNamespace(
        vault_path=str(tmp_path / "vault"),
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
        db_path=str(tmp_path / "compile.db"),
    )

    def _guard_vault_root(params, vault_path, request_id, label="vault"):
        return None  # not under test here

    def _make_error(code, message, request_id):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _make_response(result, request_id):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # If the write path is ever reached, submit_drafts would be imported; flip a flag.
    def _fake_run(db, config, **kwargs):
        submit_called["run"] = True
        return {"drafts": [], "drafts_written": []}

    ctx = afm_mod.AFMContext(
        make_error=_make_error,
        make_response=_make_response,
        guard_vault_root=_guard_vault_root,
        lazy_writeback=lambda: types.SimpleNamespace(),
        trace_ring=lambda: types.SimpleNamespace(put=lambda *a, **k: None),
        record_latency=lambda *a, **k: None,
        maybe_archive_inbox_source=lambda *a, **k: None,
        sovereign_db=lambda *a, **k: types.SimpleNamespace(close=lambda: None),
        default_config=cfg,
    )
    return ctx, afm_mod


def test_daemon_compile_dry_run_false_denied_for_read_principal(monkeypatch, tmp_path):
    """P1: a read-only principal running daemon.compile with dry_run=false is
    denied with operator_only BEFORE any pass runs / draft is submitted."""
    submit_called = {"run": False}
    ctx, afm_mod = _afm_context(monkeypatch, tmp_path, submit_called=submit_called)

    read_principal = EffectivePrincipal(agent_id="codex", capabilities=["read"])
    assert not is_operator_principal(read_principal)

    resp = afm_mod.handle_daemon_compile(
        {
            "pass_name": "consolidation",
            "dry_run": False,
            "_principal": read_principal,
        },
        1,
        ctx,
    )
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "operator_only" in err.get("message", ""), resp
    # The gate must fire before the pass runner ever executes.
    assert submit_called["run"] is False


def test_daemon_compile_dry_run_true_allowed_for_read_principal(monkeypatch, tmp_path):
    """P1: dry_run=true must still work for a read-only principal (preview)."""
    submit_called = {"run": False}
    ctx, afm_mod = _afm_context(monkeypatch, tmp_path, submit_called=submit_called)

    read_principal = EffectivePrincipal(agent_id="codex", capabilities=["read"])
    resp = afm_mod.handle_daemon_compile(
        {
            "pass_name": "consolidation",
            "dry_run": True,
            "_principal": read_principal,
        },
        2,
        ctx,
    )
    # No operator_only denial on the dry-run path.
    assert "operator_only" not in str(resp)


def test_daemon_compile_dry_run_false_allowed_for_operator(monkeypatch, tmp_path):
    """P1: an operator principal passes the dry_run=false gate."""
    submit_called = {"run": False}
    ctx, afm_mod = _afm_context(monkeypatch, tmp_path, submit_called=submit_called)

    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    assert is_operator_principal(op)
    resp = afm_mod.handle_daemon_compile(
        {
            "pass_name": "consolidation",
            "dry_run": False,
            "_principal": op,
        },
        3,
        ctx,
    )
    assert "operator_only" not in str(resp)


# ── P3: no-agent-id caller not silently elevated to wide-open operator ───────


def test_strict_install_no_operator_file_synthesizes_non_operator_main(monkeypatch, tmp_path):
    """P3: in a strict install (per-agent principal files exist but no canonical
    operator/main/local/default file), a no-agent-id caller must NOT be stamped
    as a wide-open operator 'main'. Any local process could otherwise claim full
    governance on a shared multi-agent daemon."""
    monkeypatch.delenv("MINNI_LOCAL_OPERATOR", raising=False)
    pdir = tmp_path / "principals_strict"
    pdir.mkdir()
    # Only a per-agent file (NOT a canonical operator name) exists → strict mode.
    (pdir / "codex.json").write_text('{"agent_id": "codex", "capabilities": ["learn"]}')
    os.chmod(pdir / "codex.json", 0o600)

    p = resolve_effective_principal(
        supplied_agent_id=None, transport="uds", principals_dir=pdir
    )
    assert p.agent_id == "main"
    assert not is_operator_principal(p), (
        "synthesized main in a strict install without an authored operator file "
        "must not be an operator"
    )
    assert p.capabilities == []


def test_fresh_install_no_files_still_wide_open_operator(monkeypatch, tmp_path):
    """P3 regression guard: a genuinely fresh install (NO principal files at all)
    keeps the zero-config single-user wide-open operator main."""
    monkeypatch.delenv("MINNI_LOCAL_OPERATOR", raising=False)
    pdir = tmp_path / "principals_fresh"
    pdir.mkdir()

    p = resolve_effective_principal(
        supplied_agent_id=None, transport="uds", principals_dir=pdir
    )
    assert p.agent_id == "main"
    assert is_operator_principal(p)


def test_local_operator_env_reasserts_operator_in_strict_install(monkeypatch, tmp_path):
    """P3 escape hatch: the operator-controlled MINNI_LOCAL_OPERATOR env signal
    re-enables the wide-open synthesized main in a strict install."""
    monkeypatch.setenv("MINNI_LOCAL_OPERATOR", "1")
    pdir = tmp_path / "principals_optin"
    pdir.mkdir()
    (pdir / "codex.json").write_text('{"agent_id": "codex", "capabilities": ["learn"]}')
    os.chmod(pdir / "codex.json", 0o600)

    p = resolve_effective_principal(
        supplied_agent_id=None, transport="uds", principals_dir=pdir
    )
    assert p.agent_id == "main"
    assert is_operator_principal(p)

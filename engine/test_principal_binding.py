"""
G11 — EffectivePrincipal server-stamp binding test (SEC-002 keystone, RCM-003 updated).

Proves that caller-supplied agent_id is never authoritative:
- resolve_effective_principal returns a stamped EffectivePrincipal.
- When no principals/*.json present (fresh): ONLY fixed "main" is synthesized; any other supplied -> IdentityMismatchError.
- When strict principal config present: stamped value wins; mismatch (or non-main on fresh) -> IdentityMismatchError
  and every public RPC handler returns structured -32000 identity_mismatch WITHOUT engine work.

Covers per RCM-003: (a) strict+file pass, (b) no-principals+"main" pass, (c) no-principals+"other" raises.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make the engine/ directory importable exactly like the other test_pr*.py files
sys.path.insert(0, str(Path(__file__).parent))

import principal  # type: ignore
from principal import (
    EffectivePrincipal,
    IdentityMismatchError,
    resolve_effective_principal,
    from_local_transport,
    make_mismatch_error,
)
import minnid  # type: ignore  # for the _handle_* entry points


def test_effective_principal_dataclass_fields():
    p = EffectivePrincipal(
        agent_id="hermes",
        workspace_id="ws-42",
        transport="uds",
        capabilities=["search", "learn"],
        allowed_vault_roots=["/tmp/vault"],
    )
    assert p.agent_id == "hermes"
    assert p.workspace_id == "ws-42"
    assert p.can("search")
    assert p.can("foo") is False
    assert p.allows_vault_root("/tmp/vault/sub/note.md")
    assert not p.allows_vault_root("/etc/passwd")


def test_resolve_fresh_install_only_main_accepted(tmp_path: Path):
    """RCM-003: no principals/*.json -> synthesize ONLY "main"; other supplied raises (covers a/b/c).
    Hermetic: uses explicit empty principals_dir (no global PRINCIPALS_DIR reliance).
    """
    principals = tmp_path / "principals"
    principals.mkdir()  # empty -> fresh synthesize mode for "main" only

    # (b) no-principal + no supplied id is the explicit local/operator path.
    p = resolve_effective_principal(supplied_agent_id=None, transport="uds", principals_dir=principals)
    assert isinstance(p, EffectivePrincipal)
    assert p.agent_id == "main"
    assert "*" in p.capabilities

    # (c) no-principal + valid agent claim -> default-deny caller scope, not operator.
    p2 = resolve_effective_principal(
        supplied_agent_id="claude-code", transport="uds", principals_dir=principals
    )
    assert p2.agent_id == "claude-code"
    assert p2.capabilities == []
    assert p2.allowed_vault_roots == []
    assert not p2.can("read")

    # (a) strict mode with principal file still passes (existing test covers details)


def test_resolve_non_strict_default_main_when_no_supplied(tmp_path: Path):
    """Hermetic variant (global PRINCIPALS_DIR avoided)."""
    principals = tmp_path / "principals"
    principals.mkdir()
    p = resolve_effective_principal(supplied_agent_id=None, transport="uds", principals_dir=principals)
    assert p.agent_id == "main"


def test_resolve_strict_rejects_mismatch(tmp_path: Path):
    """With a strict principal file, mismatched supplied raises and is not synthesized."""
    principals = tmp_path / "principals"
    principals.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "operator-prime",
                "workspace_id": "prod",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(tmp_path)],
                "legacy_agent_ids": ["main", "hermes"],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)  # satisfy strict 0600 hard requirement (Bug 4)

    # Good supplied via legacy alias -> stamped wins (no raise)
    p = resolve_effective_principal(
        supplied_agent_id="hermes", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "operator-prime"
    assert p.workspace_id == "prod"

    # Direct match
    p2 = resolve_effective_principal(
        supplied_agent_id="operator-prime", transport="uds", principals_dir=principals
    )
    assert p2.agent_id == "operator-prime"

    # Mismatch (not in aliases) -> hard error
    with pytest.raises(IdentityMismatchError) as exc:
        resolve_effective_principal(
            supplied_agent_id="spoofed-evil", transport="uds", principals_dir=principals
        )
    assert "spoofed-evil" in str(exc.value)
    assert "operator-prime" in str(exc.value)


def test_resolve_strict_platform_agent_gets_own_stamp(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "platform_agent_ids": ["codex"],
                "platform_agent_capabilities": {
                    "codex": ["search", "read", "learn"],
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)

    p = resolve_effective_principal(
        supplied_agent_id="codex", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "codex"
    assert p.can("read")
    assert not p.can("resolve_candidate")


def test_resolve_strict_platform_agent_empty_caps_is_honoured(tmp_path: Path):
    """An explicit empty capability list must NOT fall back to the broader principal
    caps (capability-escalation guard): `[]` means "deliberately no capabilities"."""
    principals = tmp_path / "principals"
    principals.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "platform_agent_ids": ["codex"],
                "platform_agent_capabilities": {
                    "codex": [],
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)

    p = resolve_effective_principal(
        supplied_agent_id="codex", transport="uds", principals_dir=principals
    )
    assert p.agent_id == "codex"
    assert p.capabilities == []
    assert not p.can("read")
    assert not p.can("search")


def test_resolve_matching_per_agent_principal_file_wins(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    (principals / "local.json").write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(tmp_path / "operator-vault")],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(principals / "local.json", 0o600)
    codex_root = tmp_path / "codex-vault"
    shared_root = tmp_path / "shared"
    (principals / "codex.json").write_text(
        json.dumps(
            {
                "agent_id": "codex",
                "workspace_id": "default",
                "capabilities": ["search", "read", "learn"],
                "allowed_vault_roots": [str(codex_root), str(shared_root)],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(principals / "codex.json", 0o600)

    p = resolve_effective_principal(
        supplied_agent_id="codex", transport="uds", principals_dir=principals
    )

    assert p.agent_id == "codex"
    assert p.workspace_id == "default"
    assert p.capabilities == ["search", "read", "learn"]
    assert p.allowed_vault_roots == [str(codex_root.resolve()), str(shared_root.resolve())]
    assert p.allows_vault_root(codex_root / "wiki" / "note.md")
    assert not p.allows_vault_root(tmp_path / "operator-vault" / "wiki" / "note.md")


def test_strict_install_without_operator_file_synthesizes_non_operator_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """P3: a strict install (some principals/*.json exists) that never authored a
    canonical operator/local/default/main file must NOT let a no-agent-id caller
    synthesize a wide-open ``main`` with ``capabilities=["*"]``. Previously
    ``from_local_transport`` always fell through to the wide-open synthesis
    whenever no canonical file matched, regardless of strict mode -- so any
    local process on a shared multi-agent daemon got full governance for free,
    with zero authentication. Only a non-canonical per-agent file (e.g.
    codex.json) is present here, so strict=True but no operator/main file
    exists; the no-agent-id resolution must come back default-deny (no caps,
    no vault roots) and NOT is_operator_principal.
    """
    monkeypatch.delenv("MINNI_LOCAL_OPERATOR", raising=False)
    principals = tmp_path / "principals"
    principals.mkdir()
    (principals / "codex.json").write_text(
        json.dumps({"agent_id": "codex", "capabilities": ["search", "read"]}),
        encoding="utf-8",
    )
    os.chmod(principals / "codex.json", 0o600)

    p = resolve_effective_principal(
        supplied_agent_id=None, transport="uds", principals_dir=principals
    )

    assert p.agent_id == "main"
    assert p.capabilities == []
    assert p.allowed_vault_roots == []
    assert not principal.is_operator_principal(p)


def test_strict_install_local_operator_env_signal_restores_wide_open_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """P3 escape hatch: the operator-controlled (never wire-controlled)
    MINNI_LOCAL_OPERATOR env signal re-enables the wide-open synthesized
    ``main`` in an otherwise-strict install with no authored operator file --
    for the legitimate single-user case of "I added a per-agent file but the
    no-agent-id caller (e.g. this same local daemon operator) should still
    have full access"."""
    monkeypatch.setenv("MINNI_LOCAL_OPERATOR", "1")
    principals = tmp_path / "principals"
    principals.mkdir()
    (principals / "codex.json").write_text(
        json.dumps({"agent_id": "codex", "capabilities": ["search", "read"]}),
        encoding="utf-8",
    )
    os.chmod(principals / "codex.json", 0o600)

    p = resolve_effective_principal(
        supplied_agent_id=None, transport="uds", principals_dir=principals
    )

    assert p.agent_id == "main"
    assert p.capabilities == ["*"]
    assert principal.is_operator_principal(p)


def test_resolve_unknown_fileless_agent_is_default_deny_not_operator(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    (principals / "local.json").write_text(
        json.dumps({"agent_id": "main", "capabilities": ["*"]}),
        encoding="utf-8",
    )
    os.chmod(principals / "local.json", 0o600)

    p = resolve_effective_principal(
        supplied_agent_id="gemini", transport="uds", principals_dir=principals
    )

    assert p.agent_id == "gemini"
    assert p.capabilities == []
    assert p.allowed_vault_roots == []
    assert not p.can("search")
    assert not p.allows_vault_root(tmp_path / "gemini-vault")
    assert not principal.is_operator_principal(p)


def test_agent_claiming_main_is_deny_without_operator_context(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    (principals / "main.json").write_text(
        json.dumps({"agent_id": "main", "capabilities": ["*"]}),
        encoding="utf-8",
    )
    os.chmod(principals / "main.json", 0o600)

    p = resolve_effective_principal(
        supplied_agent_id="main", transport="uds", principals_dir=principals
    )

    assert p.agent_id == "main"
    assert p.capabilities == []
    assert p.allowed_vault_roots == []
    assert not p.allows_vault_root(tmp_path)
    assert not principal.is_operator_principal(p)


def test_operator_context_can_resolve_main_principal(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    (principals / "main.json").write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(tmp_path / "operator-vault")],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(principals / "main.json", 0o600)

    p = resolve_effective_principal(
        supplied_agent_id="main",
        transport="uds",
        principals_dir=principals,
        operator_context=True,
    )

    assert p.agent_id == "main"
    assert p.capabilities == ["*"]
    assert principal.is_operator_principal(p)


def test_per_agent_principal_file_agent_id_mismatch_raises(tmp_path: Path):
    principals = tmp_path / "principals"
    principals.mkdir()
    (principals / "codex.json").write_text(
        json.dumps({"agent_id": "gemini", "capabilities": ["read"]}),
        encoding="utf-8",
    )
    os.chmod(principals / "codex.json", 0o600)

    with pytest.raises(IdentityMismatchError) as exc:
        resolve_effective_principal(
            supplied_agent_id="codex", transport="uds", principals_dir=principals
        )
    assert "codex" in str(exc.value)
    assert "gemini" in str(exc.value)


def test_strict_principal_bad_permissions_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    principals = tmp_path / "principals"
    principals.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "local",
                "workspace_id": "prod",
                "capabilities": ["search"],
                "allowed_vault_roots": [str(tmp_path)],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o644)

    def _deny_chmod(*_args, **_kwargs):
        raise PermissionError("chmod unavailable")

    monkeypatch.setattr(principal.os, "chmod", _deny_chmod)

    with pytest.raises(RuntimeError, match="must be 0600"):
        resolve_effective_principal(
            supplied_agent_id="local", transport="uds", principals_dir=principals
        )


def test_make_mismatch_error_shape():
    err = make_mismatch_error("bad", "good", request_id=42)
    assert err["id"] == 42
    assert err["error"]["code"] == -32000
    assert "identity_mismatch" in err["error"]["message"]


# --- RPC handler negative tests (every path that previously honored caller agent_id) ---


def _call_handler_with_mismatch(handler, supplied: str = "evil-spoof"):
    """Invoke a minnid _handle_* and assert it returns identity_mismatch error without side effects."""
    params = {
        "agent_id": supplied,
        "query": "test",
        "content": "test",
        "event_type": "test",
    }
    # Provide minimal required fields for the specific handler
    if handler.__name__ == "_handle_search":
        params = {"agent_id": supplied, "query": "test query"}
    elif handler.__name__ == "_handle_feedback":
        params = {"agent_id": supplied, "query": "q", "result_id": 1, "useful": True}
    elif handler.__name__ == "_handle_read":
        params = {"agent_id": supplied, "limit": 1}
    elif handler.__name__ == "_handle_learn":
        params = {"agent_id": supplied, "content": "x", "title": "t"}
    elif handler.__name__ == "_handle_log_event":
        params = {"agent_id": supplied, "event_type": "note", "content": "c"}
    elif handler.__name__ == "_handle_resolve_contradiction":
        # Non-empty supersede_ids: the empty list is now a -32602 validation
        # error BEFORE principal resolution; identity mismatch must still fire
        # on a well-formed request.
        params = {"agent_id": supplied, "new_content": "new", "supersede_ids": [1]}
    elif handler.__name__ == "_handle_daemon_handoff":
        # G11: handoff now guarded on from_agent claim (Bug 3 / "every MCP/RPC path")
        params = {"from_agent": supplied, "to_agent": "recipient", "packet": {}}
    elif handler.__name__ == "_handle_sm_export_pack":
        params = {"agent_id": supplied, "query": "test query for export", "budget_tokens": 100, "cache_key": "t"}
    elif handler.__name__ == "_handle_list_pending_handoffs":
        params = {"agent_id": supplied}
    elif handler.__name__ == "_handle_subscribe_contradictions":
        params = {"agent_id": supplied, "since_ts": 0}

    resp = handler(params, request_id="test-1")
    assert "error" in resp, f"{handler.__name__} did not return error on mismatch"
    assert resp["error"]["code"] == -32000
    assert "identity_mismatch" in resp["error"]["message"]
    return resp


def test_all_rpc_paths_deny_mismatched_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Every public write/read path that accepted agent_id must now deny spoofing when strict."""
    principals = tmp_path / "principals"
    principals.mkdir()
    f = principals / "default.json"
    f.write_text(
        json.dumps({"agent_id": "canonical", "legacy_agent_ids": []}),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)  # satisfy strict 0600 hard requirement (Bug 4)

    # Patch the module-level resolve to use our temp dir for these calls
    # (handlers call resolve_effective_principal() with no principals_dir arg)
    original_resolve = principal.resolve_effective_principal

    def _patched_resolve(
        *, supplied_agent_id=None, transport="uds", principals_dir=None
    ):
        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=principals_dir or principals,
        )

    # Use pytest monkeypatch fixture (project idiom, auto-restore, scope-safe)
    monkeypatch.setattr(principal, "resolve_effective_principal", _patched_resolve)
    monkeypatch.setattr(minnid, "resolve_effective_principal", _patched_resolve)
    handlers = [
        minnid._handle_search,
        minnid._handle_feedback,
        minnid._handle_read,
        minnid._handle_learn,
        minnid._handle_log_event,
        minnid._handle_resolve_contradiction,
        minnid._handle_daemon_handoff,  # now stamped + denies mismatch (G11)
        minnid._handle_sm_export_pack,  # G11 critical: now guarded (was direct agent_id bypass)
        minnid._handle_list_pending_handoffs,  # G11 critical: now guarded
        minnid._handle_subscribe_contradictions,  # G11 critical: now guarded
    ]
    for h in handlers:
        _call_handler_with_mismatch(h, supplied="not-canonical")


def test_happy_path_with_strict_principal_uses_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When match (or non-operator legacy alias), handler proceeds with the *stamped* agent_id."""
    principals = tmp_path / "principals"
    principals.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
                {
                    "agent_id": "stamped-one",
                    "workspace_id": "g11-test",
                    "legacy_agent_ids": ["stamped-legacy"],
                }
            ),
            encoding="utf-8",
        )
    os.chmod(f, 0o600)  # satisfy strict 0600 hard requirement (Bug 4)

    original_resolve = principal.resolve_effective_principal

    def _patched(*, supplied_agent_id=None, transport="uds", principals_dir=None):
        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=principals_dir or principals,
        )

    monkeypatch.setattr(principal, "resolve_effective_principal", _patched)
    # Supply a non-operator legacy alias -> must get stamped "stamped-one" back.
    p = resolve_effective_principal(supplied_agent_id="stamped-legacy", principals_dir=principals)
    assert p.agent_id == "stamped-one"
    assert p.workspace_id == "g11-test"

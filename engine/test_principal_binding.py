"""
G11 — EffectivePrincipal server-stamp binding test (SEC-002 keystone).

Proves that caller-supplied agent_id is never authoritative:
- resolve_effective_principal returns a stamped EffectivePrincipal.
- When no principals/*.json present: back-compat synthesis (supplied or "main").
- When strict principal config present: stamped value wins; mismatch -> IdentityMismatchError
  and every public RPC handler (_handle_search, _handle_learn, _handle_read, etc.)
  returns a structured -32000 identity_mismatch error WITHOUT proceeding to engine work.

This is the only test that exercises the G11 guard. All other engine tests continue
to run in non-strict mode (no principals/ files) so the tree stays green.
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
import sovrd  # type: ignore  # for the _handle_* entry points


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


def test_resolve_non_strict_backcompat_synthesizes_supplied():
    """No principals/ files -> supplied value is accepted (rollout safety)."""
    p = resolve_effective_principal(supplied_agent_id="claude-code", transport="uds")
    assert isinstance(p, EffectivePrincipal)
    assert p.agent_id == "claude-code"
    assert p.transport == "uds"
    assert "*" in p.capabilities


def test_resolve_non_strict_default_main_when_no_supplied():
    p = resolve_effective_principal(supplied_agent_id=None, transport="uds")
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
    """Invoke a sovrd _handle_* and assert it returns identity_mismatch error without side effects."""
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
        params = {"agent_id": supplied, "new_content": "new", "supersede_ids": []}
    elif handler.__name__ == "_handle_daemon_handoff":
        # G11: handoff now guarded on from_agent claim (Bug 3 / "every MCP/RPC path")
        params = {"from_agent": supplied, "to_agent": "recipient", "packet": {}}

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
    monkeypatch.setattr(sovrd, "resolve_effective_principal", _patched_resolve)
    handlers = [
        sovrd._handle_search,
        sovrd._handle_feedback,
        sovrd._handle_read,
        sovrd._handle_learn,
        sovrd._handle_log_event,
        sovrd._handle_resolve_contradiction,
        sovrd._handle_daemon_handoff,  # now stamped + denies mismatch (G11)
    ]
    for h in handlers:
        _call_handler_with_mismatch(h, supplied="not-canonical")


def test_happy_path_with_strict_principal_uses_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When match (or legacy alias), handler proceeds with the *stamped* agent_id."""
    principals = tmp_path / "principals"
    principals.mkdir()
    f = principals / "local.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": "stamped-one",
                "workspace_id": "g11-test",
                "legacy_agent_ids": ["main"],
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
    # Supply legacy alias "main" -> must get stamped "stamped-one" back
    p = resolve_effective_principal(supplied_agent_id="main", principals_dir=principals)
    assert p.agent_id == "stamped-one"
    assert p.workspace_id == "g11-test"

#!/usr/bin/env python3
"""G19: Targeted tests for centralized can_read_document gate (TDD - must fail before impl).

Covers matrix from design doc + query:
- same-agent allowed
- shared-wiki (page_type=wiki/handoff or agent=wiki:*) visible when authorized (in vault root, not blocked)
- private/foreign-agent docs denied
- operator principal override (broader read within allowed vaults, still respects blocked)
- workspace scoping (ws mismatch denied unless '*' semantics)
- vault root containment via principal.allows_vault_root
- blocked always denied
- unknown agent treated as shared/safe in context

All read surfaces (retrieval, expand, handoff, agent_api, sovrd) must route through this.
"""

import pytest
from pathlib import Path

from minni.principal import (
    EffectivePrincipal,
    can_read_document,
    is_operator_principal,
    resolve_effective_principal,
)


def _p(agent: str = "main", ws: str = "default", roots=None, caps=None) -> EffectivePrincipal:
    return EffectivePrincipal(
        agent_id=agent,
        workspace_id=ws,
        capabilities=caps or ["*"],
        allowed_vault_roots=roots or ["/tmp/test-vault"],
    )


def test_same_agent_allowed():
    p = _p("main")
    meta = {
        "agent": "main",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/kb/note.md",
        "page_status": "accepted",
        "workspace_id": "default",
    }
    assert can_read_document(p, "default", meta) is True


def test_shared_wiki_authorized():
    p = _p("main")
    meta = {
        "agent": "wiki:meta",
        "page_type": "wiki",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/shared.md",
        "page_status": "accepted",
    }
    assert can_read_document(p, "default", meta) is True

    meta2 = {"agent": "handoff-bot", "page_type": "handoff", "privacy_level": "safe", "path": "/tmp/test-vault/wiki/handoffs/x.md"}
    assert can_read_document(p, "default", meta2) is True


def test_private_shared_wiki_denied_for_foreign_non_operator():
    p = _p("hermes", caps=["search", "recall"])
    meta = {
        "agent": "wiki:meta",
        "page_type": "wiki",
        "privacy_level": "private",
        "path": "/tmp/test-vault/wiki/private-shared.md",
        "page_status": "accepted",
    }
    assert can_read_document(p, "default", meta) is False


def test_foreign_private_denied():
    # Non-operator limited principal (no * cap, not main/operator id) must be denied foreign private
    p = _p("hermes", caps=["search", "recall"])
    meta = {
        "agent": "other-agent",
        "privacy_level": "private",
        "path": "/tmp/test-vault/other/secret.md",
        "page_status": "accepted",
    }
    assert can_read_document(p, "default", meta) is False


def test_private_local_only_denied_for_foreign():
    p = _p("hermes", caps=["search", "recall"])
    meta = {"agent": "other", "privacy_level": "local-only", "path": "/tmp/test-vault/other/local.md"}
    assert can_read_document(p, "default", meta) is False


def test_blocked_always_denied():
    p = _p("main")
    meta = {"agent": "main", "privacy_level": "blocked", "path": "/tmp/test-vault/kb/blocked.md"}
    assert can_read_document(p, "default", meta) is False


def test_foreign_safe_session_denied_for_non_operator():
    """Finding 10: session pages are agent-scoped even when privacy=safe."""
    p = _p("codex", caps=["search", "read"])
    meta = {
        "agent": "other-agent",
        "page_type": "session",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/sessions/other.md",
        "page_status": "accepted",
    }
    assert can_read_document(p, "default", meta) is False
    meta_legacy = {
        "agent": "wiki:session",
        "page_type": "session",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/sessions/legacy.md",
        "page_status": "accepted",
    }
    assert can_read_document(p, "default", meta_legacy) is False
    # Unattributed / unknown agent must not fall through the legacy unknown grant.
    meta_unknown = {
        "agent": "unknown",
        "page_type": "session",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/sessions/unattributed.md",
        "page_status": "accepted",
    }
    assert can_read_document(p, "default", meta_unknown) is False
    meta_missing_agent = {
        "page_type": "session",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/sessions/missing-agent.md",
        "page_status": "accepted",
    }
    assert can_read_document(p, "default", meta_missing_agent) is False


def test_principal_named_unknown_cannot_read_unattributed_sessions():
    """Finding 10 residual: principal id 'unknown' must not same-agent-match
    the legacy unknown sentinel on session pages."""
    p = _p("unknown", caps=["search", "read"])
    assert is_operator_principal(p) is False
    meta = {
        "agent": "unknown",
        "page_type": "session",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/sessions/unattributed.md",
    }
    assert can_read_document(p, "default", meta) is False
    # Non-session unknown docs remain visible to a capable principal.
    meta_note = {
        "agent": "unknown",
        "page_type": "concept",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/concepts/shared.md",
    }
    assert can_read_document(p, "default", meta_note) is True


def test_operator_can_read_foreign_within_vault():
    # Operator (govern cap or main) gets broader visibility for governance/audit within roots
    p = _p("operator", caps=["govern"])
    assert is_operator_principal(p) is True
    meta = {
        "agent": "other",
        "privacy_level": "private",
        "path": "/tmp/test-vault/kb/foreign-but-in-root.md",
    }
    # Still gated by vault root + not blocked
    assert can_read_document(p, "default", meta) is True


def test_vault_root_denied_even_for_same_agent():
    p = _p("main", roots=["/tmp/test-vault/allowed-only"])
    meta = {"agent": "main", "privacy_level": "safe", "path": "/tmp/test-vault/outside/escape.md"}
    assert can_read_document(p, "default", meta) is False


def test_workspace_mismatch_denied():
    p = _p("main", ws="ws1")
    meta = {"agent": "main", "privacy_level": "safe", "path": "/tmp/test-vault/kb/x.md", "workspace_id": "ws2"}
    assert can_read_document(p, "ws1", meta) is False


def test_workspace_star_or_match_allowed():
    p = _p("main", ws="ws1")
    meta = {"agent": "main", "privacy_level": "safe", "path": "/tmp/test-vault/kb/x.md", "workspace_id": "ws2"}
    # '*' in doc or call means cross-ws shared (future G28)
    assert can_read_document(p, "*", meta) is True
    meta["workspace_id"] = "*"
    assert can_read_document(p, "ws1", meta) is True


def test_unknown_agent_treated_lenient_for_safe():
    p = _p("main")
    meta = {"agent": "unknown", "privacy_level": "safe", "path": "/tmp/test-vault/kb/legacy.md"}
    assert can_read_document(p, "default", meta) is True


def test_can_read_document_is_centralized_and_deterministic():
    p = _p()
    meta = {"agent": "main", "privacy_level": "safe", "path": "/tmp/test-vault/kb/x.md"}
    assert can_read_document(p, "default", meta) is True
    assert can_read_document(p, "default", None) is False
    assert can_read_document(None, "default", meta) is False  # type: ignore


# Integration smoke: resolve_effective + gate (no DB)
def test_resolve_and_gate_roundtrip():
    # Non-strict synthesis path (dirty tree default)
    p = resolve_effective_principal(supplied_agent_id="main", transport="test")
    meta = {"agent": "main", "privacy_level": "safe", "path": str(Path.home())}  # home may be allowed or not
    # Just ensure no crash and bool result
    res = can_read_document(p, p.workspace_id, meta)
    assert isinstance(res, bool)

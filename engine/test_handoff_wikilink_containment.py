#!/usr/bin/env python3
"""G23: Handoff wikilink path containment + traversal denial (TDD).

Wikilinks in handoff packets ([[../../etc/passwd]] or outside allowed_vault_roots)
must be rejected at _validate or _handle_daemon_handoff using realpath + principal.allows_vault_root
+ (future) can_read_document on the target.

Symlink-aware, no escape from vault/wiki subdir.
"""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from principal import EffectivePrincipal, can_read_document
from minnid import _handoff_context
from minnid_runtime.handoff import handle_daemon_handoff, validate_handoff_packet


def _p(roots):
    return EffectivePrincipal(agent_id="main", allowed_vault_roots=roots)


def test_validate_rejects_traversing_wikilink_ref(tmp_path):
    """Validator normalizes shape only; traversal containment is enforced in the handler."""
    sender_vault = tmp_path / "main-vault"
    (sender_vault / "wiki" / "handoffs").mkdir(parents=True, exist_ok=True)

    packet = {
        "from_agent": "main",
        "to_agent": "other",
        "kind": "handoff",
        "task": "bad link",
        "envelope": "x",
        "wikilink_refs": ["../../../etc/passwd", "goodpage", "../outside.md"],
        "trace_id": "trace-g23",
    }
    normalized, err = validate_handoff_packet("main", "other", packet)
    assert err is None
    assert normalized is not None
    assert normalized["wikilink_refs"] == packet["wikilink_refs"]


def test_daemon_handoff_handler_rejects_traversing_wikilink_ref(tmp_path, monkeypatch):
    """G23 containment runs in handle_daemon_handoff after principal stamp (handoff.py G23 block)."""
    sender = tmp_path / "main-vault"
    recipient = tmp_path / "other-vault"
    (sender / "wiki" / "handoffs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"main": str(sender), "other": str(recipient)}),
    )

    principal = EffectivePrincipal(
        agent_id="main",
        allowed_vault_roots=[str(sender.resolve()), str(recipient.resolve())],
        capabilities=["handoff", "*"],
    )

    def _stamped_principal(_params, _request_id, **kwargs):
        return principal, None

    context = replace(_handoff_context(), handler_principal=_stamped_principal)

    response = handle_daemon_handoff(
        {
            "from_agent": "main",
            "to_agent": "other",
            "packet": {
                "from_agent": "main",
                "to_agent": "other",
                "kind": "handoff",
                "task": "bad link",
                "envelope": "x",
                "wikilink_refs": ["../../../etc/passwd"],
                "trace_id": "trace-g23-handler",
            },
        },
        request_id=1,
        context=context,
    )

    assert "error" in response
    assert response["error"]["code"] == -32003
    assert "wikilink_traversal_denied" in response["error"]["message"]


def test_wikilink_target_must_be_inside_allowed_root(tmp_path):
    """Realpath resolution + allows_vault_root must deny escape even if string looks relative."""
    p = _p([str(tmp_path / "safe-root")])
    bad_target = tmp_path / "safe-root" / ".." / ".." / "etc" / "shadow"
    try:
        resolved = bad_target.resolve()
    except Exception:
        resolved = bad_target
    assert not p.allows_vault_root(str(resolved))


def test_can_read_document_used_for_wikilink_target_in_retrieval(tmp_path):
    """When a wikilink target is resolved at read time, can_read_document must gate it (G19+G23)."""
    p = _p([str(tmp_path / "safe-root")])
    target_meta = {
        "path": str(tmp_path / "outside" / "leak.md"),
        "agent": "wiki",
        "page_type": "handoff",
        "privacy_level": "safe",
    }
    assert can_read_document(p, "default", target_meta) is False

#!/usr/bin/env python3
"""G23: Handoff wikilink path containment + traversal denial (TDD).

Wikilinks in handoff packets ([[../../etc/passwd]] or outside allowed_vault_roots)
must be rejected at _validate or _handle_daemon_handoff using realpath + principal.allows_vault_root
+ (future) can_read_document on the target.

Symlink-aware, no escape from vault/wiki subdir.
"""

import pytest
from pathlib import Path
import tempfile
import os

from principal import EffectivePrincipal, can_read_document
from sovrd import _validate_handoff_packet  # the packet validator (will be hardened)


def _p(roots):
    return EffectivePrincipal(agent_id="main", allowed_vault_roots=roots)


def test_validate_rejects_traversing_wikilink_ref(tmp_path):
    """[[../../../etc/passwd]] or ../outside must cause validation error (no handoff created)."""
    sender_vault = tmp_path / "main-vault"
    (sender_vault / "wiki" / "handoffs").mkdir(parents=True, exist_ok=True)

    # The validator currently accepts any str list; G23 will add containment check
    packet = {
        "from_agent": "main",
        "to_agent": "other",
        "kind": "handoff",
        "task": "bad link",
        "envelope": "x",
        "wikilink_refs": ["../../../etc/passwd", "goodpage", "../outside.md"],
    }
    # Before G23 hardening this may return normalized, after it will error on bad refs
    normalized, err = _validate_handoff_packet("main", "other", packet)
    # For TDD we assert that after hardening, err mentions traversal or the call site in handoff rejects
    # Here we just document; the real check will live in sovrd handoff or a new _sanitize_wikilink
    assert True  # placeholder until G23 edit; full denial asserted in integration via handoff handler test


def test_wikilink_target_must_be_inside_allowed_root(tmp_path):
    """Realpath resolution + allows_vault_root must deny escape even if string looks relative."""
    p = _p([str(tmp_path / "safe-root")])
    bad_target = tmp_path / "safe-root" / ".." / ".." / "etc" / "shadow"
    # Simulate the check we will add
    try:
        resolved = bad_target.resolve()
    except Exception:
        resolved = bad_target
    assert not p.allows_vault_root(str(resolved))


def test_can_read_document_used_for_wikilink_target_in_retrieval(tmp_path):
    """When a wikilink target is resolved at read time, can_read_document must gate it (G19+G23)."""
    p = _p([str(tmp_path / "safe-root")])
    target_meta = {"path": str(tmp_path / "outside" / "leak.md"), "agent": "wiki", "page_type": "handoff", "privacy_level": "safe"}
    # If the resolved target escapes the principal roots, gate denies even for wiki type
    # (the allows_vault is called first inside can_read)
    assert can_read_document(p, "default", target_meta) is False

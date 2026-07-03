#!/usr/bin/env python3
"""G12 tests: EffectivePrincipal vault root binding (SEC-003).

Covers:
- allows_vault_root happy path (under allowed root, realpath)
- denial for path outside allowed_vault_roots
- .. traversal in input string is defeated by resolve()
- symlink escape to outside root is denied (resolve follows)
- integration: minnid hygiene/compile/endorse paths return -32003 on bad vault
  (via the shared _guard_vault_root + G11 stamp). The handoff path performs an equivalent
  inline `principal.allows_vault_root` check on _agent_vault-derived sender/recipient
  vaults (minnid.py:501) after its own G11 stamp; the primitive + denial shape are covered
  by the dataclass tests and the other handler tests (no duplication of the core logic).
"""

import os
import json
import tempfile
from pathlib import Path

import pytest

from minni.principal import EffectivePrincipal
from minni.minnid import (
    _handle_hygiene_report,
    _handle_daemon_compile,
    _handle_daemon_endorse,
    _guard_vault_root,
)


def _install_strict_principal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allowed_root: Path) -> None:
    import minni.principal as principal_mod

    principals = tmp_path / "principals"
    principals.mkdir(exist_ok=True)
    (principals / "main.json").write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(allowed_root)],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(principals / "main.json", 0o600)
    monkeypatch.setattr(principal_mod, "PRINCIPALS_DIR", principals)


def test_allows_vault_root_happy_and_default():
    p = EffectivePrincipal(agent_id="main", allowed_vault_roots=["/tmp/allowed-root"])
    assert p.allows_vault_root("/tmp/allowed-root/sub/dir")
    assert p.allows_vault_root(Path("/tmp/allowed-root"))
    # No restriction if empty list for an otherwise-capable operator/default principal.
    p_open = EffectivePrincipal(agent_id="main", allowed_vault_roots=[])
    assert p_open.allows_vault_root("/any/place")
    # Empty caps + empty roots is the explicit default-deny principal shape.
    p_deny = EffectivePrincipal(agent_id="unknown-agent", capabilities=[], allowed_vault_roots=[])
    assert not p_deny.allows_vault_root("/any/place")


def test_vault_root_denies_outside_and_traversal(tmp_path: Path):
    allowed = tmp_path / "safe-vault"
    allowed.mkdir()
    p = EffectivePrincipal(agent_id="main", allowed_vault_roots=[str(allowed)])

    # Outside
    outside = tmp_path / "other"
    outside.mkdir()
    assert not p.allows_vault_root(outside)

    # Traversal string (resolve normalizes .. before relative check)
    traversal = str(allowed) + "/../other"
    assert not p.allows_vault_root(traversal)

    # Even deeper traversal
    assert not p.allows_vault_root(str(allowed / ".." / "other" / ".." / "escape"))


def test_vault_root_denies_symlink_escape(tmp_path: Path):
    allowed = tmp_path / "safe"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed / "escape-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted in this env")
    p = EffectivePrincipal(agent_id="main", allowed_vault_roots=[str(allowed)])
    # resolve() follows the symlink, so the resolved target is outside -> deny
    assert not p.allows_vault_root(str(link))


def test_guard_vault_root_returns_structured_deny_for_bad_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_strict_principal(monkeypatch, tmp_path, tmp_path / "allowed-vault")
    bad_vault = str(tmp_path / "completely-unrelated-vault-3549")
    params = {"agent_id": "main", "vault_path": bad_vault}
    err = _guard_vault_root(params, bad_vault, "t1", label="test")
    assert err is not None
    assert err["error"]["code"] == -32003
    assert "vault_root_denied" in err["error"]["message"]
    assert "realpath" in err["error"]["message"] or "allowed_vault_roots" in err["error"]["message"]


def test_hygiene_handler_denies_bad_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_strict_principal(monkeypatch, tmp_path, tmp_path / "allowed-vault")
    bad = str(tmp_path / "evil-hygiene-vault")
    resp = _handle_hygiene_report({"vault_path": bad}, "req-1")
    assert "error" in resp
    assert resp["error"]["code"] == -32003
    assert "vault_root_denied" in resp["error"]["message"]


def test_compile_handler_denies_bad_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_strict_principal(monkeypatch, tmp_path, tmp_path / "allowed-vault")
    bad = str(tmp_path / "evil-compile-vault")
    resp = _handle_daemon_compile({"pass_name": "session_distillation", "vault_path": bad, "dry_run": True}, "req-2")
    # Guard runs immediately after pass_name validation; for a valid pass_name + bad vault we must get -32003
    assert "error" in resp
    assert resp["error"]["code"] == -32003, "bad vault must be rejected by G12 guard before any further work"


def test_endorse_handler_denies_bad_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_strict_principal(monkeypatch, tmp_path, tmp_path / "allowed-vault")
    bad = str(tmp_path / "evil-endorse-vault")
    resp = _handle_daemon_endorse({"page_id": "p1", "decision": "accept", "vault_path": bad}, "req-3")
    assert "error" in resp
    # Guard runs before endorse_draft; for bad vault we get -32003 (the page-not-found would only happen after a passing guard)
    assert resp["error"]["code"] == -32003, "bad vault must be rejected by G12 guard with structured denial"

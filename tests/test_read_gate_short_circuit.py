"""Cheap foreign-session denial must never bypass containment for possible grants."""
from pathlib import Path

import pytest

from minni.principal import EffectivePrincipal, can_read_document


@pytest.mark.parametrize("owner,kind", [("foreign", "session"), ("unknown", "session"),
                                        ("wiki:session", "wiki"), ("session:foreign", "wiki")])
@pytest.mark.parametrize("caller", ["codex", "unknown"])
def test_foreign_sessions_do_not_resolve_paths(monkeypatch, owner, kind, caller):
    principal = EffectivePrincipal(agent_id=caller, capabilities=["read"], allowed_vault_roots=["/vault"])
    monkeypatch.setattr(Path, "resolve", lambda *_a, **_k: pytest.fail("definitive denial must avoid filesystem work"))
    assert not can_read_document(principal, "default", {
        "agent": owner, "page_type": kind, "path": "/vault/note.md", "privacy_level": "safe"})


@pytest.mark.parametrize("caller,caps,owner,kind,expected", [
    ("codex", ["read"], "codex", "session", True),
    ("main", ["read"], "foreign", "session", True),
    ("reviewer", ["govern"], "foreign", "session", True),
    ("codex", ["read"], "foreign", "session", False),
    ("unknown", ["read"], "unknown", "session", False),
    ("unknown", ["read"], "unknown", "wiki", True),
    ("wiki:session", ["read"], "wiki:session", "wiki", True),
    ("session:mine", ["read"], "session:mine", "session", True),
    ("codex", ["read"], "foreign", "decision", True),
])
@pytest.mark.parametrize("location", ["inside", "traversal", "symlink"])
def test_possible_grants_still_require_actual_containment(tmp_path, caller, caps, owner, kind, expected, location):
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    path = {"inside": root / "note.md", "traversal": root / ".." / "outside" / "note.md",
            "symlink": root / "escape" / "note.md"}[location]
    principal = EffectivePrincipal(agent_id=caller, capabilities=caps, allowed_vault_roots=[str(root)])
    row = {"agent": owner, "page_type": kind, "path": str(path), "privacy_level": "safe"}
    assert can_read_document(principal, "default", row) == (expected and location == "inside")
    row["privacy_level"] = "blocked"
    assert not can_read_document(principal, "default", row)
    row.update(privacy_level="safe", workspace_id="other")
    assert not can_read_document(principal, "default", row)

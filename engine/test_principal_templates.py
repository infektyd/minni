"""Per-agent principal templates and dry-run authoring script."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))


def test_principal_templates_render_and_load_with_resolver(tmp_path: Path):
    from principal import resolve_effective_principal
    from tools.author_principals import AGENT_VAULT_DIRS, render_principal

    principals = tmp_path / "principals"
    principals.mkdir()
    minni_home = tmp_path / ".minni"
    shared_root = minni_home / "shared"

    for agent_id, vault_dir in AGENT_VAULT_DIRS.items():
        rendered = render_principal(agent_id, minni_home=minni_home)
        assert rendered["agent_id"] == agent_id
        assert rendered["workspace_id"] == "default"
        assert set(rendered["capabilities"]) == {
            "search",
            "read",
            "learn",
            "feedback",
            "log_event",
            "handoff",
            "export",
        }
        assert rendered["allowed_vault_roots"] == [
            str(minni_home / vault_dir),
            str(shared_root),
        ]

        principal_file = principals / f"{agent_id}.json"
        principal_file.write_text(render_principal(agent_id, minni_home=minni_home, as_json=True), encoding="utf-8")
        os.chmod(principal_file, 0o600)

        p = resolve_effective_principal(
            supplied_agent_id=agent_id,
            transport="uds",
            principals_dir=principals,
        )
        assert p.agent_id == agent_id
        assert p.can("read")
        assert p.allows_vault_root(minni_home / vault_dir / "wiki" / "note.md")
        assert p.allows_vault_root(shared_root / "wiki" / "handoff.md")
        assert not p.allows_vault_root(minni_home / "other-vault" / "wiki" / "note.md")


def test_author_principals_dry_run_default_does_not_write(tmp_path: Path):
    from tools.author_principals import AGENT_VAULT_DIRS, author_principals

    minni_home = tmp_path / ".minni"
    principals = tmp_path / "principals"

    results = author_principals(minni_home=minni_home, principals_dir=principals)

    assert len(results) == len(AGENT_VAULT_DIRS)
    assert all(item["dry_run"] is True for item in results)
    assert not principals.exists()


def test_author_principals_apply_writes_0600_in_target_dir(tmp_path: Path):
    from tools.author_principals import AGENT_VAULT_DIRS, author_principals

    minni_home = tmp_path / ".minni"
    principals = tmp_path / "principals"

    results = author_principals(minni_home=minni_home, principals_dir=principals, apply=True)

    assert len(results) == len(AGENT_VAULT_DIRS)
    assert principals.is_dir()
    assert oct(principals.stat().st_mode & 0o777) == "0o700"
    for item in results:
        path = Path(item["path"])
        assert path.is_file()
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert item["dry_run"] is False

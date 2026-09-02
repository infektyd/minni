"""Tests for `layer1/` seeding during vault bootstrap.

The Minni Layer 1 contract says every agent vault carries `layer1/core.md` +
`layer1/budget.md`: a small, agent-curated, read-first-on-wake workspace under a
strict <4096 token budget. Nothing in the installer ever created them, so only
hand-seeded vaults had one and the doctrine referenced artifacts that nothing
produced.

These files are agent-owned living state edited during every distill, so seeding
must never overwrite an existing file.

These run against tmp fixtures so we never touch a live `~/.minni` vault.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402

LAYER1_FILES = ("core.md", "budget.md")


@pytest.fixture
def vault(tmp_path, monkeypatch, capsys) -> Path:
    """Bootstrap `test-agent`'s vault under a tmp HOME and return its path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.delenv("MINNI_WORKSPACE_ID", raising=False)
    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None))
    capsys.readouterr()
    return tmp_path / ".minni" / "test-agent-vault"


def test_templates_exist_in_repo():
    """The templates must ship in-repo, not be hand-authored per vault."""
    for name in LAYER1_FILES:
        assert (propagate.LAYER1_TEMPLATE_DIR / name).is_file(), name


def test_bootstrap_seeds_the_identity_workspace(vault):
    """Regression: bootstrap created inbox/wiki/... but never layer1/."""
    for name in LAYER1_FILES:
        assert (vault / "layer1" / name).is_file(), f"layer1/{name} not seeded"


def test_core_carries_the_identity_parameters(vault):
    core = (vault / "layer1" / "core.md").read_text()

    assert "test-agent" in core
    assert str(vault) in core, "vault path not substituted"
    assert str(propagate.DEFAULT_SOCKET) in core, "socket path not substituted"
    assert "identity:test-agent" in core
    assert "{{" not in core, "unsubstituted template placeholder"


def test_core_states_the_generic_contract(vault):
    core = (vault / "layer1" / "core.md").read_text()

    # The three load-bearing clauses of the contract.
    assert "durable" in core.lower()
    assert "budget.md" in core
    assert "hosted_agent_envelope" in core
    assert "subordinate to the host runtime" in core


def test_budget_states_the_4096_ceiling_and_curation_rights(vault):
    budget = (vault / "layer1" / "budget.md").read_text()

    assert "4096" in budget
    assert "test-agent" in budget, "curation rights must name the owning agent"
    assert "{{" not in budget


def test_seeded_layer1_fits_the_budget(vault):
    """A seed that already blows the 4096-token budget would be self-defeating."""
    chars = sum((vault / "layer1" / name).stat().st_size for name in LAYER1_FILES)
    assert chars // 4 < 4096, f"seeded layer1/ is ~{chars // 4} tokens"


def test_workspace_flag_is_recorded(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)

    propagate.bootstrap_vault(
        argparse.Namespace(agent="test-agent", workspace="~/Projects/Example")
    )
    capsys.readouterr()

    core = (tmp_path / ".minni" / "test-agent-vault" / "layer1" / "core.md").read_text()
    assert "~/Projects/Example" in core


def test_workspace_falls_back_to_env_then_placeholder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.setenv("MINNI_WORKSPACE_ID", "workspace-fromenv")

    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None))
    capsys.readouterr()

    core = (tmp_path / ".minni" / "test-agent-vault" / "layer1" / "core.md").read_text()
    assert "workspace-fromenv" in core


def test_seeding_never_overwrites_agent_curation(vault, capsys):
    """layer1/ is living state the agent rewrites; a re-run must not clobber it."""
    edits = {name: f"AGENT-CURATED {name}\n" for name in LAYER1_FILES}
    for name, body in edits.items():
        (vault / "layer1" / name).write_text(body)

    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None))
    report = json.loads(capsys.readouterr().out)["layer1"]

    for name, body in edits.items():
        assert (vault / "layer1" / name).read_text() == body, f"{name} clobbered"
    assert report["created"] == []
    assert sorted(report["kept"]) == sorted(LAYER1_FILES)


def test_seeding_backfills_only_the_missing_file(vault, capsys):
    (vault / "layer1" / "budget.md").unlink()

    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None))
    report = json.loads(capsys.readouterr().out)["layer1"]

    assert report["created"] == ["budget.md"]
    assert report["kept"] == ["core.md"]


def test_bootstrapped_vault_reports_layer1_present_to_the_distill_gauges(vault):
    """Seeding order matters: gauges are written after layer1/ exists."""
    assert "layer1/core.md present" in (vault / "distill" / "gauges.md").read_text()


def test_missing_templates_do_not_break_bootstrap(tmp_path, monkeypatch, capsys):
    """A stripped install tree must degrade visibly, not abort vault bootstrap."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.setattr(propagate, "LAYER1_TEMPLATE_DIR", tmp_path / "absent")

    assert propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None)) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "ok"
    assert report["layer1"]["status"] == "skipped"
    assert "template dir missing" in report["layer1"]["reason"]


_PLUGIN_LOG = "# Minni Log\n\n## [2026-09-01T00:00:00Z] plugin | audit\n\n"
_PLUGIN_INDEX = "# Minni Index\n\n- [[wiki/entities/peer]]\n"
_RACE_AUDIT = (
    "## [2026-09-01T00:00:00Z] plugin | audit | unique-payload-do-not-clobber\n\n"
)
_RACE_INDEX = "- [[wiki/entities/peer]] unique-index-do-not-clobber\n"


def test_bootstrap_preserves_existing_log_and_index(tmp_path, monkeypatch, capsys):
    """Exclusive create must not wipe a plugin stamp already at log.md/index.md."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.delenv("MINNI_WORKSPACE_ID", raising=False)
    vault = tmp_path / ".minni" / "test-agent-vault"
    vault.mkdir(parents=True)
    (vault / "log.md").write_text(_PLUGIN_LOG, encoding="utf-8")
    (vault / "index.md").write_text(_PLUGIN_INDEX, encoding="utf-8")
    assert propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None)) == 0
    capsys.readouterr()
    assert (vault / "log.md").read_text(encoding="utf-8") == _PLUGIN_LOG
    assert (vault / "index.md").read_text(encoding="utf-8") == _PLUGIN_INDEX


def test_bootstrap_does_not_clobber_append_after_exclusive_create(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.delenv("MINNI_WORKSPACE_ID", raising=False)
    vault = tmp_path / ".minni" / "test-agent-vault"
    vault.mkdir(parents=True)
    orig_os_open = os.open

    def append_after_create(path) -> None:
        try:
            name = Path(os.fsdecode(path)).name
        except (TypeError, ValueError, OSError):
            return
        if name not in {"log.md", "index.md"}:
            return
        payload = _RACE_AUDIT if name == "log.md" else _RACE_INDEX
        extra = orig_os_open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(extra, payload.encode("utf-8"))
        finally:
            os.close(extra)

    def racing_os_open(path, flags, *args, **kwargs):
        fd = orig_os_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            append_after_create(path)
        return fd

    monkeypatch.setattr(os, "open", racing_os_open)
    assert propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None)) == 0
    capsys.readouterr()
    assert _RACE_AUDIT in (vault / "log.md").read_text(encoding="utf-8")
    assert _RACE_INDEX in (vault / "index.md").read_text(encoding="utf-8")


def test_bootstrap_refuses_schema_agents_symlink_into_shop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.delenv("MINNI_WORKSPACE_ID", raising=False)
    vault = tmp_path / ".minni" / "test-agent-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir(parents=True)
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "schema").mkdir()
    (vault / "schema" / "AGENTS.md").symlink_to(shop / "keep.md")
    with pytest.raises((OSError, SystemExit)):
        propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None))
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_bootstrap_refuses_schema_dir_symlink_into_shop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.delenv("MINNI_WORKSPACE_ID", raising=False)
    vault = tmp_path / ".minni" / "test-agent-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir(parents=True)
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "schema").symlink_to(shop)
    with pytest.raises((OSError, SystemExit)):
        propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None))
    assert not (shop / "AGENTS.md").exists()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_bootstrap_refuses_wiki_dir_symlink_into_shop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.delenv("MINNI_WORKSPACE_ID", raising=False)
    vault = tmp_path / ".minni" / "test-agent-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir(parents=True)
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "wiki").symlink_to(shop)
    with pytest.raises((OSError, SystemExit)):
        propagate.bootstrap_vault(argparse.Namespace(agent="test-agent", workspace=None))
    assert not (shop / "sessions").exists()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"

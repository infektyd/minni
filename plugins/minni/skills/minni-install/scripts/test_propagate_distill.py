"""Tests for `distill/` seeding during vault bootstrap.

The Minni Distill Ritual V1 (see the `minni` SKILL, "Minni Distill Ritual V1"
and "Gauges / Live Context Meter") tells the agent to read `distill/gauges.md`
FIRST at any wind-down signal, and reads its explicit|auto|disabled toggle from
`distill/mode`. Nothing in the installer ever created those files, so vaults
bootstrapped by `propagate.py` ran the ritual blind against a missing meter.

`mode` and `gauges.md` are living state that operators and the agent edit, so
seeding must never overwrite an existing file.

These run against tmp fixtures so we never touch a live `~/.minni` vault.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402

DISTILL_FILES = ("mode", "gauges.md", "ritual.md")


@pytest.fixture
def vault(tmp_path, monkeypatch, capsys) -> Path:
    """Bootstrap `test-agent`'s vault under a tmp HOME and return its path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent"))
    capsys.readouterr()
    return tmp_path / ".minni" / "test-agent-vault"


def test_templates_exist_in_repo():
    """The templates must ship in-repo, not from a machine-local ~/.agents path."""
    for name in DISTILL_FILES:
        assert (propagate.DISTILL_TEMPLATE_DIR / name).is_file(), name


def test_bootstrap_seeds_all_ritual_artifacts(vault):
    """Regression: bootstrap used to create inbox/wiki/... but never distill/."""
    for name in DISTILL_FILES:
        assert (vault / "distill" / name).is_file(), f"distill/{name} not seeded"


def test_mode_defaults_to_explicit_on_line_one(vault):
    """The parser takes line 1 only; comments must not leak into the value."""
    lines = (vault / "distill" / "mode").read_text().splitlines()
    assert lines[0] == "explicit"
    assert all(line.startswith("#") for line in lines[1:] if line.strip())


def test_gauges_carry_schema_sections_and_agent_id(vault):
    gauges = (vault / "distill" / "gauges.md").read_text()

    assert gauges.startswith("---\n")
    assert "agent: test-agent" in gauges
    assert "mode: explicit" in gauges
    for section in (
        "## Pressure Signals",
        "## Layer 1 Reference",
        "## Recent Burst",
        "## Decision Aids",
    ):
        assert section in gauges, section
    # A freshly seeded meter must read honestly, not imply a burst happened.
    assert "pressure_level: low" in gauges
    assert 'recommended: "no action"' in gauges
    assert "{{" not in gauges, "unsubstituted template placeholder"


def test_ritual_points_at_canonical_skill(vault):
    ritual = (vault / "distill" / "ritual.md").read_text()

    assert "test-agent" in ritual
    assert "skills/minni/SKILL.md" in ritual, "must defer to the canonical section"
    assert "{{" not in ritual


def test_gauges_reference_layer1_when_present(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    vault = tmp_path / ".minni" / "test-agent-vault"
    (vault / "layer1").mkdir(parents=True)
    (vault / "layer1" / "core.md").write_text("# identity\n")

    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent"))
    capsys.readouterr()

    gauges = (vault / "distill" / "gauges.md").read_text()
    assert "layer1/core.md present" in gauges


def test_gauges_report_missing_layer1_honestly(tmp_path, monkeypatch, capsys):
    """With Layer 1 seeding unavailable, the meter must not claim identity exists."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.setattr(propagate, "LAYER1_TEMPLATE_DIR", tmp_path / "absent")

    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent"))
    capsys.readouterr()

    vault = tmp_path / ".minni" / "test-agent-vault"
    assert 'identity_present: "not seeded"' in (vault / "distill" / "gauges.md").read_text()


def test_seeding_never_overwrites_operator_edits(vault, capsys):
    """`mode` and `gauges.md` are operator-owned; ritual.md accumulates traces."""
    edits = {name: f"OPERATOR EDIT {name}\n" for name in DISTILL_FILES}
    for name, body in edits.items():
        (vault / "distill" / name).write_text(body)

    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent"))
    report = json.loads(capsys.readouterr().out)["distill"]

    for name, body in edits.items():
        assert (vault / "distill" / name).read_text() == body, f"{name} clobbered"
    assert report["created"] == []
    assert sorted(report["kept"]) == sorted(DISTILL_FILES)


def test_seeding_backfills_only_the_missing_file(vault, capsys):
    (vault / "distill" / "gauges.md").unlink()

    propagate.bootstrap_vault(argparse.Namespace(agent="test-agent"))
    report = json.loads(capsys.readouterr().out)["distill"]

    assert report["created"] == ["gauges.md"]
    assert sorted(report["kept"]) == ["mode", "ritual.md"]


def test_missing_templates_do_not_break_bootstrap(tmp_path, monkeypatch, capsys):
    """A stripped install tree must degrade visibly, not abort vault bootstrap."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINNI_VAULT_PATH", raising=False)
    monkeypatch.setattr(propagate, "DISTILL_TEMPLATE_DIR", tmp_path / "absent")

    assert propagate.bootstrap_vault(argparse.Namespace(agent="test-agent")) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "ok"
    assert report["distill"]["status"] == "skipped"
    assert "template dir missing" in report["distill"]["reason"]

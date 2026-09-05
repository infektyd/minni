"""Tests for decoupled Typed Memory Graph configuration flags (P1.2).

Verifies:
- Default values: graph_classification_enabled=True, graph_expansion_enabled=False.
- Decoupling: each flag can be toggled independently without affecting the other.
- Environment variable overrides: MINNI_GRAPH_CLASSIFICATION_ENABLED and MINNI_GRAPH_EXPANSION_ENABLED.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.config import SovereignConfig


def test_sovereign_config_graph_defaults(monkeypatch):
    """Verify default values in SovereignConfig."""
    monkeypatch.delenv("MINNI_GRAPH_CLASSIFICATION_ENABLED", raising=False)
    monkeypatch.delenv("MINNI_GRAPH_EXPANSION_ENABLED", raising=False)

    cfg = SovereignConfig()
    assert cfg.graph_classification_enabled is True
    assert cfg.graph_expansion_enabled is False


def test_sovereign_config_graph_decoupling_via_constructor():
    """Verify flags can be set independently via constructor args."""
    cfg1 = SovereignConfig(graph_classification_enabled=False, graph_expansion_enabled=False)
    assert cfg1.graph_classification_enabled is False
    assert cfg1.graph_expansion_enabled is False

    cfg2 = SovereignConfig(graph_classification_enabled=True, graph_expansion_enabled=True)
    assert cfg2.graph_classification_enabled is True
    assert cfg2.graph_expansion_enabled is True

    cfg3 = SovereignConfig(graph_classification_enabled=False, graph_expansion_enabled=True)
    assert cfg3.graph_classification_enabled is False
    assert cfg3.graph_expansion_enabled is True


@pytest.mark.parametrize(
    "env_val,expected",
    [
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_sovereign_config_graph_classification_env_overrides(monkeypatch, env_val, expected):
    """Verify MINNI_GRAPH_CLASSIFICATION_ENABLED overrides default."""
    monkeypatch.setenv("MINNI_GRAPH_CLASSIFICATION_ENABLED", env_val)
    cfg = SovereignConfig()
    assert cfg.graph_classification_enabled is expected


@pytest.mark.parametrize(
    "env_val,expected",
    [
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
    ],
)
def test_sovereign_config_graph_expansion_env_overrides(monkeypatch, env_val, expected):
    """Verify MINNI_GRAPH_EXPANSION_ENABLED overrides default."""
    monkeypatch.setenv("MINNI_GRAPH_EXPANSION_ENABLED", env_val)
    cfg = SovereignConfig()
    assert cfg.graph_expansion_enabled is expected

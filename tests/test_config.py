"""Focused tests for MinniConfig field defaults."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def test_afm_input_budget_tokens_default():
    from minni.config import MinniConfig

    cfg = MinniConfig()
    assert cfg.afm_input_budget_tokens == 3200


def test_afm_input_budget_tokens_env_override(monkeypatch):
    from minni.config import MinniConfig

    monkeypatch.setenv("MINNI_AFM_INPUT_BUDGET_TOKENS", "1234")
    cfg = MinniConfig()
    assert cfg.afm_input_budget_tokens == 1234


def test_afm_input_budget_tokens_ignores_invalid_env(monkeypatch):
    from minni.config import MinniConfig

    monkeypatch.setenv("MINNI_AFM_INPUT_BUDGET_TOKENS", "not-a-number")
    try:
        cfg = MinniConfig()
    except ValueError:
        # int() on garbage must not blow up config construction
        raise AssertionError("invalid env value must fall back, not raise")
    assert cfg.afm_input_budget_tokens == 3200

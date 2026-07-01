"""Focused tests for SovereignConfig field defaults."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def test_afm_input_budget_tokens_default():
    from config import SovereignConfig

    cfg = SovereignConfig()
    assert cfg.afm_input_budget_tokens == 3200


def test_afm_input_budget_tokens_env_override(monkeypatch):
    from config import SovereignConfig

    monkeypatch.setenv("MINNI_AFM_INPUT_BUDGET_TOKENS", "1234")
    cfg = SovereignConfig()
    assert cfg.afm_input_budget_tokens == 1234


def test_afm_input_budget_tokens_ignores_invalid_env(monkeypatch):
    from config import SovereignConfig

    monkeypatch.setenv("MINNI_AFM_INPUT_BUDGET_TOKENS", "not-a-number")
    try:
        cfg = SovereignConfig()
    except ValueError:
        # int() on garbage must not blow up config construction
        raise AssertionError("invalid env value must fall back, not raise")
    assert cfg.afm_input_budget_tokens == 3200

"""Focused tests for SovereignConfig field defaults."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def test_afm_input_budget_tokens_default():
    from config import SovereignConfig

    cfg = SovereignConfig()
    assert cfg.afm_input_budget_tokens == 3200

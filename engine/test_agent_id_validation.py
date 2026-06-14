"""Tests for agent_id validation and inbox vault slug mapping."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def test_principal_for_inbox_vault_mapping():
    from afm_passes.inbox_ingest import _principal_for_inbox

    assert _principal_for_inbox(Path("/home/claudecode-vault/inbox"), "fallback") == "claude-code"
    assert _principal_for_inbox(Path("/home/grok-build-vault/inbox"), "fallback") == "grok-build"
    assert _principal_for_inbox(Path("/home/codex-vault/inbox"), "fallback") == "codex"
    assert _principal_for_inbox(Path("/home/unknown-vault/inbox"), "fallback") == "unknown"
    assert _principal_for_inbox(Path("/home/vault/inbox"), "fallback") == "fallback"


def test_validate_agent_id_accepts_valid():
    from principal import validate_agent_id

    for agent_id in (
        "claude-code",
        "codex",
        "grok-build",
        "main",
        "afm-loop",
        "grok.beta",
        "agent_1",
        "a",
    ):
        assert validate_agent_id(agent_id) == agent_id


def test_validate_agent_id_rejects_invalid():
    from principal import validate_agent_id

    with pytest.raises(ValueError):
        validate_agent_id("Discord bot tags for...")

    with pytest.raises(ValueError):
        validate_agent_id("")

    with pytest.raises(ValueError):
        validate_agent_id("Claude-Code")

    with pytest.raises(ValueError):
        validate_agent_id("a" * 65)

    assert validate_agent_id("a" * 64) == "a" * 64

    with pytest.raises(ValueError):
        validate_agent_id("-bad")

    with pytest.raises(ValueError):
        validate_agent_id("bad!")
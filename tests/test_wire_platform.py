"""Unit tests for platform_spec validation."""

from __future__ import annotations

import pytest

from minni.wire.platform import platform_spec


def test_platform_spec_rejects_traversal_agent():
    with pytest.raises(ValueError, match="invalid --agent"):
        platform_spec(
            "generic",
            install_root="/tmp/agent",
            agent="../../../../tmp/pwn",
        )


def test_platform_spec_accepts_simple_agent(tmp_path):
    root = tmp_path / "agent"
    root.mkdir()
    spec = platform_spec("generic", install_root=str(root), agent="testagent")
    assert spec.agent == "testagent"
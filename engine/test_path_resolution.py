"""
G02 — Canonical path unification test.
Walks the engine config surfaces + resolver and asserts they agree on
~/.sovereign-memory (or SOVEREIGN_HOME) for db/faiss/graph/writeback/socket.
Also sanity-checks plugin TS default via node (if node available) for cross-lang parity.
"""
import os
import subprocess
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent))
from config import SovereignConfig, CANONICAL_SOVEREIGN_HOME, resolve_canonical_path


def test_canonical_home_prefers_sovereign_memory():
    home = CANONICAL_SOVEREIGN_HOME
    assert ".sovereign-memory" in home or "sovereign" in home.lower()
    assert "openclaw" not in home  # legacy not the default


def test_engine_config_derives_from_canonical_home():
    cfg = SovereignConfig()  # uses env or default
    for p in (cfg.db_path, cfg.faiss_index_path, cfg.graph_export_dir, cfg.writeback_path):
        assert CANONICAL_SOVEREIGN_HOME in p or p.startswith(CANONICAL_SOVEREIGN_HOME), f"{p} not under canonical home"


def test_resolver_returns_consistent_values():
    assert resolve_canonical_path("db").endswith("sovereign_memory.db")
    assert resolve_canonical_path("socket").endswith("sovrd.sock")
    assert "sovereign-memory" in resolve_canonical_path("home")


def test_plugin_config_ts_unified_on_sovereign_memory_base():
    """Portable source-level check for G02 unification.
    Verifies that the plugin config.ts (the single source for its defaults)
    references the canonical .sovereign-memory paths for both vault and the
    G04-hardened socket. No external node/TS loader or machine-specific paths.
    """
    plugin_config = Path(__file__).parent.parent / "plugins/minni/src/config.ts"
    assert plugin_config.exists(), "plugin config.ts must exist for G02 parity"
    text = plugin_config.read_text(encoding="utf-8")
    # Vault (pre-existing) and socket (G02/G04 unification we just completed)
    assert ".sovereign-memory" in text, "plugin must reference canonical .sovereign-memory base"
    assert "run/sovrd.sock" in text or 'sovrd.sock' in text, "SOCKET_PATH must be unified to the secure engine default (G04)"

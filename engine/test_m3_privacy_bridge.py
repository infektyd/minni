"""M-3: Durable-store semantic indexing must honor YAML frontmatter privacy floor.

_index_durable_learning previously hard-coded privacy_level='safe', bypassing
frontmatter declarations like privacy: private. These tests verify the bridge
to VaultIndexer._extract_frontmatter fixes that gap.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minnid
import config as cfg_mod
from migrations import run_migrations
from principal import EffectivePrincipal, can_read_document


_DOC_PRIVATE = (
    "---\n"
    "agent: alice\n"
    "status: accepted\n"
    "privacy: private\n"
    "layer: knowledge\n"
    "---\n"
    "# Private Knowledge\n\n"
    "This document contains sensitive private information that should never "
    "surface in cross-agent or safe-scoped recall. It covers personal "
    "configuration details and private operational procedures that are "
    "agent-specific and must remain hidden from other principals.\n"
)

_DOC_NO_FM = (
    "# Plain Knowledge\n\n"
    "This document has no YAML frontmatter block at all. It is stored through "
    "the durable learn path and should receive VaultIndexer's default metadata "
    "for absent frontmatter, with page_status overridden to accepted because "
    "durable store constitutes governance acceptance of the content.\n"
)


@pytest.fixture
def temp_engine(tmp_path, monkeypatch):
    """Point the whole engine at a fresh temp DB + temp vault, reset singletons."""
    db_path = str(tmp_path / "m3_privacy.db")
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", db_path, raising=False)
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"), raising=False
    )
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG,
        "writeback_path",
        str(tmp_path / "learnings"),
        raising=False,
    )
    monkeypatch.setattr(minnid, "_retrieval", None, raising=False)
    monkeypatch.setattr(minnid, "_writeback", None, raising=False)
    monkeypatch.setattr(minnid, "_episodic", None, raising=False)

    from db import SovereignDB

    _seed_db = SovereignDB()
    run_migrations(_seed_db._get_conn())

    yield db_path, tmp_path
    minnid._retrieval = None
    minnid._writeback = None
    minnid._episodic = None


def _alice():
    return EffectivePrincipal(agent_id="alice", capabilities=["*"])


def _bob():
    # Non-operator cross-agent principal (no * cap) — foreign private must deny.
    return EffectivePrincipal(agent_id="bob", capabilities=["search", "recall"])


def _durable_doc_row(conn):
    return conn.execute(
        "SELECT path, agent, privacy_level, page_status, layer "
        "FROM documents WHERE path LIKE '%_durable%' "
        "ORDER BY doc_id DESC LIMIT 1"
    ).fetchone()


def test_frontmatter_privacy_level_honored_not_overridden_to_safe(temp_engine):
    """Private frontmatter must land in documents.privacy_level, not hard-coded safe."""
    db_path, _ = temp_engine
    resp = minnid._handle_learn(
        {"content": _DOC_PRIVATE, "force": True, "_principal": _alice()}, 1
    )
    assert resp["result"]["status"] == "ok"

    conn = sqlite3.connect(db_path)
    row = _durable_doc_row(conn)
    conn.close()
    assert row is not None, "durable store did not create a documents row"
    assert row[2] == "private", (
        f"expected privacy_level='private' from frontmatter, got {row[2]!r}"
    )


def test_cross_agent_cannot_read_private_doc_same_agent_can(temp_engine):
    """Privacy floor: foreign principal denied, owning agent allowed."""
    db_path, _ = temp_engine
    resp = minnid._handle_learn(
        {"content": _DOC_PRIVATE, "force": True, "_principal": _alice()}, 1
    )
    assert resp["result"]["status"] == "ok"

    conn = sqlite3.connect(db_path)
    row = _durable_doc_row(conn)
    conn.close()
    assert row is not None

    meta = {
        "agent": row[1],
        "privacy_level": row[2],
        "page_status": row[3],
        "path": row[0],
    }
    assert meta["privacy_level"] == "private"
    assert meta["agent"] == "alice"

    assert can_read_document(_bob(), "default", meta) is False
    assert can_read_document(_alice(), "default", meta) is True


def test_no_frontmatter_falls_back_to_vault_indexer_defaults(temp_engine):
    """Absent frontmatter → VaultIndexer defaults + durable-store accepted override."""
    db_path, _ = temp_engine
    resp = minnid._handle_learn(
        {"content": _DOC_NO_FM, "force": True, "_principal": _alice()}, 1
    )
    assert resp["result"]["status"] == "ok"

    conn = sqlite3.connect(db_path)
    row = _durable_doc_row(conn)
    conn.close()
    assert row is not None
    path, agent, privacy_level, page_status, layer = row

    assert privacy_level == "safe", (
        f"no-frontmatter default privacy should be 'safe', got {privacy_level!r}"
    )
    assert page_status == "accepted", (
        f"durable store should override default candidate → accepted, got {page_status!r}"
    )
    assert layer == "knowledge"
    # Caller agent_id when frontmatter has no agent declaration.
    assert agent == "alice"
    assert "_durable" in path
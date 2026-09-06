"""P1 slice tests: canonical learning nodes + atomic coordinated commit.

Covers (model-free, no live stores):
- canonical documents row + learning_documents join on store_learning, with
  production durable_metadata attribution (never invented values)
- reuse/stamp of a pre-existing projection row at the durable path
  (memory_kind NULL): no UNIQUE failure, ownership/privacy preserved
- private-owner reuse keeps the restricted privacy level
- N:1 idempotency (same agent+content shares the node; uri tracks latest)
- agent isolation (same content, different agents -> different nodes)
- rollback (failure mid-commit leaves no partial durable state)
- baseline tolerance (typed schema absent -> baseline-only, no canonical)
- canonical edge source (no alias orphan when canonical is available)
- atomicity through the actual minnid learn handler (force path)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import numpy as np


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    db_path = str(tmp_path / "test.db")
    cfg = SovereignConfig(
        db_path=db_path, writeback_enabled=False, writeback_path=str(tmp_path)
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _unit_vec(dim=384):
    v = np.ones(dim, dtype=np.float32)
    return v / np.linalg.norm(v)


class _FakeModel:
    def encode(self, text, **kwargs):
        return _unit_vec()


def _make_writeback(tmp_path):
    import minni.writeback as wb_mod

    db_obj, cfg = _make_db(tmp_path)
    wb = wb_mod.WriteBackMemory(db_obj, cfg)
    original = wb_mod.WriteBackMemory.model.fget
    wb_mod.WriteBackMemory.model = property(lambda self: _FakeModel())
    try:
        yield wb, db_obj
    finally:
        wb_mod.WriteBackMemory.model = property(original)


@pytest.fixture()
def wb_store(tmp_path):
    yield from _make_writeback(tmp_path)


def _rows(db_obj, sql, params=()):
    with db_obj.cursor() as c:
        return [tuple(r) for r in c.execute(sql, params).fetchall()]


def _canonical_doc(db_obj, doc_id):
    return _rows(
        db_obj,
        "SELECT agent, sigil, page_status, privacy_level, page_type, layer,"
        " memory_kind, memory_uri FROM documents WHERE doc_id = ?",
        (doc_id,),
    )


def test_canonical_node_uses_durable_metadata_attribution(wb_store):
    """Plain content gets production defaults; plain agent id, not invented."""
    wb, db_obj = wb_store
    lid = wb.store_learning(
        agent_id="a1", content="canonical attribution content", category="fact"
    )
    joins = _rows(
        db_obj, "SELECT learning_id, doc_id FROM learning_documents WHERE learning_id = ?", (lid,)
    )
    assert len(joins) == 1
    assert _canonical_doc(db_obj, joins[0][1]) == [
        ("a1", "❓", "accepted", "safe", "learning", "knowledge", "learning", f"learning://{lid}")
    ]


def test_frontmatter_privacy_flows_to_canonical_node(wb_store):
    """A private frontmatter page keeps its privacy level on the node."""
    wb, db_obj = wb_store
    content = "---\nprivacy: private\n---\nPrivate canonical content"
    lid = wb.store_learning(agent_id="a1", content=content, category="fact")
    joins = _rows(
        db_obj, "SELECT doc_id FROM learning_documents WHERE learning_id = ?", (lid,)
    )
    assert len(joins) == 1
    doc = _canonical_doc(db_obj, joins[0][0])[0]
    assert doc[0] == "a1"
    assert doc[3] == "private"


def test_existing_projection_row_reused_not_duplicated(wb_store):
    """Parent repro: pre-graph row at the durable path must stamp, not crash."""
    from minni.durable_projection import durable_doc_path

    wb, db_obj = wb_store
    content = "projection first content"
    path = durable_doc_path("a1", "", db_obj.config.vault_path, content)
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at, page_status,
                privacy_level, page_type, layer)
               VALUES (?, 'a1', '❓', 0.0, 0.0, 'accepted', 'safe', 'learning',
                       'knowledge')"""
        , (path,))
    before = _rows(db_obj, "SELECT memory_kind FROM documents WHERE path = ?", (path,))
    assert before == [(None,)]

    lid = wb.store_learning(agent_id="a1", content=content, category="fact")
    after = _rows(
        db_obj,
        "SELECT doc_id, agent, page_status, privacy_level, memory_kind, memory_uri"
        " FROM documents WHERE path = ?",
        (path,),
    )
    assert len(after) == 1
    doc_id, agent, status, privacy, kind, uri = after[0]
    assert (agent, status, privacy, kind, uri) == (
        "a1", "accepted", "safe", "learning", f"learning://{lid}",
    )
    assert _rows(
        db_obj, "SELECT learning_id, doc_id FROM learning_documents WHERE learning_id = ?", (lid,)
    ) == [(lid, doc_id)]


def test_private_owner_projection_keeps_restricted_privacy(wb_store):
    """Stamping a private row must not loosen its privacy level."""
    from minni.durable_projection import durable_doc_path

    wb, db_obj = wb_store
    content = "private owner content"
    path = durable_doc_path("a1", "", db_obj.config.vault_path, content)
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at, page_status,
                privacy_level, page_type, layer)
               VALUES (?, 'a1', '❓', 0.0, 0.0, 'accepted', 'private',
                       'learning', 'knowledge')""",
            (path,),
        )
    lid = wb.store_learning(agent_id="a1", content=content, category="fact")
    rows = _rows(
        db_obj,
        "SELECT privacy_level, memory_kind, memory_uri FROM documents WHERE path = ?",
        (path,),
    )
    assert rows == [("private", "learning", f"learning://{lid}")]
    assert len(_rows(db_obj, "SELECT * FROM learning_documents WHERE learning_id = ?", (lid,))) == 1


def test_same_agent_content_shares_node_and_uri_tracks_latest(wb_store):
    wb, db_obj = wb_store
    lid1 = wb.store_learning(agent_id="a1", content="shared content here", category="fact")
    lid2 = wb.store_learning(agent_id="a1", content="shared content here", category="fact")
    assert lid2 != lid1
    joins = _rows(db_obj, "SELECT DISTINCT doc_id FROM learning_documents")
    assert len(joins) == 1
    assert len(_rows(db_obj, "SELECT * FROM learning_documents")) == 2
    uri = _rows(db_obj, "SELECT memory_uri FROM documents WHERE doc_id = ?", (joins[0][0],))
    assert uri == [(f"learning://{lid2}",)]


def test_different_agents_get_isolated_nodes(wb_store):
    wb, db_obj = wb_store
    wb.store_learning(agent_id="a1", content="same words", category="fact")
    wb.store_learning(agent_id="b2", content="same words", category="fact")
    docs = _rows(
        db_obj, "SELECT agent FROM documents WHERE memory_kind = 'learning' ORDER BY doc_id"
    )
    assert docs == [("a1",), ("b2",)]
    assert len(_rows(db_obj, "SELECT DISTINCT doc_id FROM learning_documents")) == 2


def test_mid_commit_failure_rolls_back_everything(wb_store, monkeypatch):
    import minni.writeback as wb_mod

    wb, db_obj = wb_store

    def _boom(*args, **kwargs):
        raise RuntimeError("induced canonical failure")

    monkeypatch.setattr(wb_mod, "ensure_canonical_learning_node", _boom)
    with pytest.raises(RuntimeError, match="induced canonical failure"):
        wb.store_learning(agent_id="a1", content="doomed content", category="fact")
    assert _rows(db_obj, "SELECT learning_id FROM learnings") == []
    assert _rows(db_obj, "SELECT doc_id FROM documents WHERE memory_kind = 'learning'") == []
    assert _rows(db_obj, "SELECT * FROM learning_documents") == []
    assert _rows(db_obj, "SELECT * FROM memory_links") == []


def test_baseline_schema_stores_learning_without_canonical(wb_store):
    wb, db_obj = wb_store
    with db_obj.cursor() as c:
        c.execute("DROP INDEX IF EXISTS idx_learning_documents_doc_id")
        c.execute("DROP TABLE learning_documents")
    lid = wb.store_learning(agent_id="a1", content="baseline content", category="fact")
    assert lid is not None
    assert _rows(db_obj, "SELECT learning_id FROM learnings WHERE learning_id = ?", (lid,)) == [
        (lid,)
    ]
    tables = {r[0] for r in _rows(db_obj, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "learning_documents" not in tables


def test_schema_read_io_error_propagates_not_baseline():
    """A real schema-read failure must not masquerade as an absent schema."""
    import sqlite3

    from minni.graph_commit import (
        ensure_canonical_learning_node,
        graph_node_schema_present,
    )

    class _FailingCursor:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        graph_node_schema_present(_FailingCursor())
    # Through ensure: the error surfaces instead of a silent baseline skip.
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        ensure_canonical_learning_node(
            _FailingCursor(), learning_id=1, agent_id="a1",
            content="io failure content", vault_path="vault", created_at=0.0,
        )


def test_canonical_edge_source_creates_no_alias(wb_store):
    """With a canonical node, derived_from edges source from it (no orphan)."""
    wb, db_obj = wb_store
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, agent) VALUES ('/wiki/source.md', 'wiki:concept')"
        )
        evidence_id = c.lastrowid
    lid = wb.store_learning(
        agent_id="a1", content="canonical edge content", category="fact",
        evidence_doc_ids=[evidence_id],
    )
    assert _rows(db_obj, "SELECT doc_id FROM documents WHERE path = ?", (f"learning://{lid}",)) == []
    canon = _rows(
        db_obj, "SELECT doc_id FROM learning_documents WHERE learning_id = ?", (lid,)
    )
    assert len(canon) == 1
    edges = _rows(
        db_obj,
        "SELECT source_doc_id, target_doc_id, link_type FROM memory_links"
        " WHERE link_type = 'derived_from'",
    )
    assert edges == [(canon[0][0], evidence_id, "derived_from")]


def test_handler_force_path_is_atomic_on_canonical_failure(tmp_path, monkeypatch):
    """Bounded atomicity through the actual minnid learn handler (force)."""
    import minni.minnid as minnid
    import minni.writeback as wb_mod
    from minni.minnid_runtime import governance as gov_mod
    from minni.writeback import WriteBackMemory

    db_obj, cfg = _make_db(tmp_path)
    monkeypatch.setattr(minnid, "_writeback", WriteBackMemory(db_obj, cfg))
    original_prop = wb_mod.WriteBackMemory.model.fget
    wb_mod.WriteBackMemory.model = property(lambda self: _FakeModel())
    # The force path binds ensure at governance import time: patch there.
    monkeypatch.setattr(
        gov_mod, "ensure_canonical_learning_node", lambda *a, **k: 1 / 0
    )
    try:
        resp = minnid._dispatch_sync(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "learn",
                "params": {
                    "content": "handler atomicity content",
                    "agent_id": "codex",
                    "force": True,
                },
            }
        )
    finally:
        wb_mod.WriteBackMemory.model = property(original_prop)
    assert "error" in resp
    assert _rows(db_obj, "SELECT learning_id FROM learnings") == []
    assert _rows(db_obj, "SELECT * FROM learning_documents") == []

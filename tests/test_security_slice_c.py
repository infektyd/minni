"""Slice C security regressions: lifecycle/indexing integrity (M2, M4, M5, M6)."""

from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import MinniConfig
    from minni.db import MinniDB

    cfg = MinniConfig(db_path=str(tmp_path / "slicec.db"), reranker_enabled=False)
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = MinniDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


# ── M2: durable-learn synthetic doc must not adopt model page_type ───────────


def test_durable_learn_ignores_frontmatter_page_type(monkeypatch, tmp_path):
    """M2: a learn with `type: wiki` frontmatter must NOT produce a cross-visible
    synthetic doc. can_read_document treats page_type in {wiki,...} as
    cross-agent-readable, so the durable synthetic doc must pin a non-cross-
    visible page_type ('learning'), ignoring the model-supplied value."""
    import minni.minnid as minnid
    import types

    db_obj, cfg = _make_db(tmp_path)

    captured = {}

    class _FakeEngine:
        config = types.SimpleNamespace(vault_path=str(tmp_path / "vault"))

        def index_durable_document(self, **kwargs):
            captured.update(kwargs)
            return {"status": "ok", "doc_id": 1, "chunks": 1}

    monkeypatch.setattr(minnid, "_lazy_retrieval", lambda: _FakeEngine())
    # Point DEFAULT_CONFIG db at the store's db so the singleton path is taken.
    import minni.config as cfg_mod
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)

    content = "---\nagent: codex\ntype: wiki\nprivacy: safe\n---\n# note\nbody text here.\n"
    minnid._index_durable_learning("codex", content, key="learning:1", db=db_obj)

    assert captured.get("page_type") == "learning", captured


# ── M4: superseding a learning purges its synthetic doc ─────────────────────


def test_purge_durable_document_removes_all_index_rows(tmp_path):
    """M4: purge_durable_document drops the documents/FTS/chunk rows for a
    synthetic doc so a superseded learning stops surfacing in doc search."""
    from minni.faiss_index import FAISSIndex
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, FAISSIndex(cfg))

    now = time.time()
    path = os.path.join(cfg.vault_path, "_durable", "codex__deadbeef.md")
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at, page_status,
                privacy_level, page_type)
               VALUES (?, 'codex', '?', ?, ?, 'accepted', 'safe', 'learning')""",
            (path, now, now),
        )
        doc_id = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, heading_context, computed_at)
               VALUES (?, 0, 'superseded content', ?, '', ?)""",
            (doc_id, np.zeros(384, dtype=np.float32).tobytes(), now),
        )
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, 'superseded content', 'codex', '?')",
            (doc_id, path),
        )

    result = engine.purge_durable_document(path)
    assert result["status"] == "ok", result

    with db_obj.cursor() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM documents WHERE doc_id=?", (doc_id,)).fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id=?", (doc_id,)).fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) AS n FROM vault_fts WHERE doc_id=?", (doc_id,)).fetchone()["n"] == 0


# ── M5: rejected/expired learnings must not match FTS/semantic search ────────


def _seed_learning(db_obj, agent, content, status, emb=None):
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO learnings
               (agent_id, category, content, confidence, embedding, created_at, status)
               VALUES (?, 'general', ?, 1.0, ?, ?, ?)""",
            (agent, content, emb, now, status),
        )
        return c.lastrowid


def test_search_learnings_excludes_rejected_and_expired(tmp_path):
    """M5: search_learnings previously filtered only on superseded_by IS NULL, so
    a rejected/expired learning (status set, superseded_by NULL) still matched
    FTS. It must now be excluded."""
    from minni.faiss_index import FAISSIndex
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, FAISSIndex(cfg))

    _seed_learning(db_obj, "codex", "quantum tunneling notes active", status="active")
    _seed_learning(db_obj, "codex", "quantum tunneling notes rejected", status="rejected")
    _seed_learning(db_obj, "codex", "quantum tunneling notes expired", status="expired")

    results = engine.search_learnings("quantum", agent_id="codex", limit=10)
    contents = {r["content"] for r in results}
    assert "quantum tunneling notes active" in contents
    assert "quantum tunneling notes rejected" not in contents
    assert "quantum tunneling notes expired" not in contents


def test_graph_contradiction_shortlist_excludes_rejected(tmp_path):
    """M5/parity: typed graph edge preparation must not surface or link against
    a rejected learning."""
    from minni.graph_commit import ensure_canonical_learning_node
    from minni.minnid_runtime.graph_adapters import prepare_governed_promotion
    from minni.principal import EffectivePrincipal

    db_obj, cfg = _make_db(tmp_path)
    emb = np.ones(384, dtype=np.float32)
    emb = emb / np.linalg.norm(emb)
    rej_lid = _seed_learning(
        db_obj,
        "codex",
        "Always route internal daemon traffic through port 8080.",
        status="rejected",
        emb=emb.tobytes(),
    )
    with db_obj.transaction() as c:
        doc_id = ensure_canonical_learning_node(
            c,
            learning_id=rej_lid,
            agent_id="codex",
            content="Always route internal daemon traffic through port 8080.",
            vault_path=cfg.vault_path,
            created_at=time.time(),
            workspace_id="default",
        )
        c.execute("UPDATE documents SET page_status = 'rejected' WHERE doc_id = ?", (doc_id,))

    principal = EffectivePrincipal(
        agent_id="codex", workspace_id="default", capabilities=["read", "learn"]
    )
    plan = prepare_governed_promotion(
        db_obj,
        author_agent="codex",
        workspace_id="default",
        caller_principal=principal,
        content="Never route internal daemon traffic through port 8080.",
        category="correction",
        vault_path=cfg.vault_path,
    )
    assert plan.fallback_edges is None


# ── M6: vault frontmatter cannot self-assign the identity layer ─────────────


def test_indexer_strips_identity_prefix_from_frontmatter_agent():
    """M6: on-disk markdown `agent: identity:codex` must NOT resolve to the
    trusted identity recall layer — the prefix is stripped at extraction."""
    from minni.indexer import VaultIndexer

    content = "---\nagent: identity:codex\nprivacy: safe\n---\n# forged identity\nbody.\n"
    meta = VaultIndexer._extract_frontmatter(content)
    assert meta["agent"] == "codex", meta
    assert meta["layer"] != "identity", meta


def test_indexer_legit_agent_still_knowledge_layer():
    """M6 regression guard: a normal agent frontmatter is unaffected."""
    from minni.indexer import VaultIndexer

    content = "---\nagent: codex\nprivacy: safe\n---\n# note\nbody.\n"
    meta = VaultIndexer._extract_frontmatter(content)
    assert meta["agent"] == "codex"
    assert meta["layer"] == "knowledge"

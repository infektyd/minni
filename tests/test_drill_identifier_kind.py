#!/usr/bin/env python3
"""Drill identifier-kind disambiguation (indexed-source lookup ambiguity).

Bug: ``reference_ids_for_engine`` resolves source/path to a document doc_id,
but ``expand_result`` interpreted that integer as chunk_id FIRST; a collision
with another document's chunk caused the wrong result, then
``reference_matches`` rejected the requested existing doc as missing.

Fix: ``expand_result(..., id_kind=...)`` restricts the namespace ("doc" for
source/path/wikilink and explicit doc_id references, "chunk" for explicit
chunk_id, legacy "auto" chunk-first fallback for bare result_id).

All cases run against a REAL SQLite fixture (SovereignDB + RetrievalEngine):
no live vault, no network, no model calls.

Limitation kept explicit: indexed chunks are retrieval excerpts, NOT
original-file line fidelity — assertions compare doc/source identity and
body text, never source line numbers.
"""

import time
from types import SimpleNamespace

import numpy as np
import pytest

from minni.config import SovereignConfig
from minni.db import SovereignDB
from minni.principal import EffectivePrincipal
from minni.retrieval import RetrievalEngine
from minni.minnid_runtime import recall as recall_mod


def _make_db(tmp_path, name):
    import minni.db as db_mod

    db_path = str(tmp_path / name)
    cfg = SovereignConfig(db_path=db_path)
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _seed(conn, path, agent, texts, privacy="safe", page_type="session"):
    now = time.time()
    conn.execute(
        """INSERT INTO documents
           (path, agent, sigil, last_modified, indexed_at, privacy_level, page_type, page_status)
           VALUES (?, ?, 'T', ?, ?, ?, ?, 'accepted')""",
        (path, agent, now, now, privacy, page_type),
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    emb = np.zeros(384, dtype="float32").tobytes()
    for index, text in enumerate(texts):
        conn.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, model_name, computed_at)
               VALUES (?, ?, ?, ?, 'test', ?)""",
            (doc_id, index, text, emb, now),
        )
    conn.commit()
    return doc_id


@pytest.fixture()
def collision(tmp_path):
    """other doc (2 chunks) + target doc: target doc_id collides with other's chunk."""
    db_obj, cfg = _make_db(tmp_path, "drill_kind.db")
    conn = db_obj._get_conn()
    other = _seed(conn, "/tmp/v/other.md", "codex", ["other chunk zero", "other chunk one"])
    target = _seed(conn, "/tmp/v/target.md", "codex", ["TARGET BODY TEXT"])
    private = _seed(
        conn, "/tmp/v/secret.md", "gemini", ["gemini private body"],
        privacy="private", page_type="knowledge",
    )
    chunks = [
        (row["chunk_id"], row["doc_id"])
        for row in conn.execute(
            "SELECT chunk_id, doc_id FROM chunk_embeddings ORDER BY chunk_id"
        )
    ]
    assert target == 2, "fixture needs target doc_id to collide with other's chunk_id=2"
    assert chunks == [(1, 1), (2, 1), (3, 2), (4, 3)]
    eng = RetrievalEngine(db_obj, cfg)
    principal = EffectivePrincipal(
        agent_id="codex",
        capabilities=["search", "read"],
        allowed_vault_roots=["/tmp/v"],
    )
    context = recall_mod.RecallContext(
        make_error=lambda code, msg, req: {"error": {"code": code, "message": msg}},
        make_response=lambda res, req: {"result": res},
        handler_principal=lambda params, req: (principal, None),
        lazy_retrieval=lambda: eng,
        agent_vault_retrieval=lambda agent_id: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: SimpleNamespace(add=lambda *a, **k: None),
        record_latency=lambda *a: None,
        default_config=SimpleNamespace(db_path=cfg.db_path),
    )
    return SimpleNamespace(
        engine=eng, principal=principal, context=context,
        other=other, target=target, private=private,
    )


def _drill(fix, reference):
    return recall_mod.expand_reference(
        reference,
        depth="chunk",
        principal=fix.principal,
        agent_id="codex",
        shared_engine=fix.engine,
        context=fix.context,
    )


def test_source_reference_returns_target_despite_chunk_collision(collision):
    result = _drill(collision, {"source": "/tmp/v/target.md"})
    assert result is not None, "existing indexed doc must not be reported missing"
    assert result["doc_id"] == collision.target
    assert result["source"] == "/tmp/v/target.md"


def test_explicit_doc_id_returns_target_despite_chunk_collision(collision):
    result = _drill(collision, {"doc_id": collision.target})
    assert result is not None
    assert result["doc_id"] == collision.target
    assert result["source"] == "/tmp/v/target.md"


def test_explicit_chunk_id_still_resolves_owning_chunk(collision):
    result = _drill(collision, {"chunk_id": 2})
    assert result is not None
    assert result["chunk_id"] == 2
    assert result["doc_id"] == collision.other


def test_bare_result_id_keeps_legacy_chunk_first(collision):
    result = collision.engine.expand_result(
        2, depth="chunk", update_access=False, principal=collision.principal
    )
    assert result is not None
    assert (result["doc_id"], result["chunk_id"]) == (collision.other, 2)


def test_id_kind_doc_and_chunk_direct(collision):
    doc = collision.engine.expand_result(
        2, depth="chunk", update_access=False,
        principal=collision.principal, id_kind="doc",
    )
    assert doc is not None and doc["doc_id"] == collision.target
    chunk = collision.engine.expand_result(
        2, depth="chunk", update_access=False,
        principal=collision.principal, id_kind="chunk",
    )
    assert chunk is not None and chunk["chunk_id"] == 2


def test_id_kind_rejects_unknown_value(collision):
    with pytest.raises(ValueError):
        collision.engine.expand_result(2, id_kind="nope")


def test_unauthorized_private_target_denied(collision):
    assert _drill(collision, {"source": "/tmp/v/secret.md"}) is None
    assert _drill(collision, {"doc_id": collision.private}) is None


def test_nonexistent_target_fails_honestly(collision):
    assert _drill(collision, {"source": "/tmp/v/nope.md"}) is None
    assert _drill(collision, {"doc_id": 99999}) is None
    assert _drill(collision, {"chunk_id": 99999}) is None
    assert (
        collision.engine.expand_result(
            99999, depth="chunk", update_access=False, id_kind="doc"
        )
        is None
    )


def test_reference_id_kind_mapping():
    assert recall_mod.reference_id_kind({"chunk_id": 2}) == "chunk"
    assert recall_mod.reference_id_kind({"doc_id": 2}) == "doc"
    assert recall_mod.reference_id_kind({"result_id": 2}) == "auto"
    assert recall_mod.reference_id_kind({"source": "/tmp/v/x.md"}) == "doc"
    assert recall_mod.reference_id_kind({"path": "/tmp/v/x.md"}) == "doc"
    assert recall_mod.reference_id_kind({"wikilink": "[[x]]"}) == "doc"
    # Most-specific identifier wins, matching reference_ids_for_engine's chain.
    assert recall_mod.reference_id_kind({"doc_id": 2, "chunk_id": 3}) == "chunk"

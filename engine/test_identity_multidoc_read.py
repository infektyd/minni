"""_handle_read must concatenate multiple whole-document identity rows in path order."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import db as db_mod
    from config import SovereignConfig

    db_path = str(tmp_path / "test.db")
    cfg = SovereignConfig(db_path=db_path)

    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    return db_obj, cfg


def _seed_identity_doc(
    conn,
    identity_path: str,
    chunk_text: str,
    agent_id: str = "codex",
) -> int:
    import numpy as np

    now = time.time()
    conn.execute(
        """INSERT INTO documents
           (path, agent, sigil, last_modified, indexed_at, whole_document, layer)
           VALUES (?, ?, ?, ?, ?, 1, 'identity')""",
        (identity_path, f"identity:{agent_id}", "🤖", now, now),
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    emb_bytes = np.zeros(384, dtype="float32").tobytes()
    conn.execute(
        """INSERT INTO chunk_embeddings
           (doc_id, chunk_index, chunk_text, embedding, model_name, computed_at)
           VALUES (?, 0, ?, ?, 'test', ?)""",
        (doc_id, chunk_text, emb_bytes, now),
    )
    conn.commit()
    return doc_id


def test_handle_read_concatenates_identity_docs_in_path_order(tmp_path, monkeypatch):
    import minnid
    from principal import EffectivePrincipal

    db_obj, _cfg = _make_db(tmp_path)
    conn = db_obj._get_conn()

    envelope_path = "vault/identities/codex/CODEX_HOSTED_AGENT_ENVELOPE.md"
    shelf_path = "vault/identities/codex/CODEX_LAYER1_SHELF.md"
    _seed_identity_doc(conn, envelope_path, "envelope body")
    _seed_identity_doc(conn, shelf_path, "shelf body")

    monkeypatch.setattr(minnid, "SovereignDB", lambda: db_obj)
    monkeypatch.setattr(
        minnid,
        "resolve_effective_principal",
        lambda **_kw: EffectivePrincipal(agent_id="codex", transport="uds"),
    )

    resp = minnid._handle_read({"agent_id": "codex"}, "test-req-1")
    context = resp["result"]["context"]

    assert "## Agent Identity: Codex" in context
    assert "### CODEX_HOSTED_AGENT_ENVELOPE" in context
    assert "### CODEX_LAYER1_SHELF" in context
    assert "envelope body" in context
    assert "shelf body" in context
    assert context.index("CODEX_HOSTED_AGENT_ENVELOPE") < context.index("CODEX_LAYER1_SHELF")
    assert "## Prior Context" not in context
    assert "## Learnings" not in context
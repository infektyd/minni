"""#229 M6: a completed plan keeps its documents row but must not be recalled.

The exclusion retracts the page's payload and keeps the row, so wikilinks and
doc_id references survive an accepted -> complete -> accepted round trip. That
moves the H6 guarantee ("a model-driven plan completion must not self-promote
into recallable memory") from the indexer to recall — so recall has to be the
thing that is pinned. The payload being absent is NOT the proof: this test
force-inserts a full FTS payload for the excluded row, so it fails for the
right reason if the status filter is ever removed.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "recall.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "f.faiss"),
        writeback_enabled=False,
        reranker_enabled=False,
        hyde_enabled=False,
    )
    old = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old
    return db_obj, cfg


def _insert_doc(db_obj, path: str, status: str, body: str) -> int:
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at, page_status,
                privacy_level, page_type, layer)
               VALUES (?, 'wiki:artifact', 'vault', ?, ?, ?, 'safe', 'artifact', 'knowledge')""",
            (path, now, now, status),
        )
        doc_id = int(c.lastrowid)
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
            "VALUES (?, ?, ?, 'wiki:artifact', 'vault')",
            (doc_id, path, body),
        )
    return doc_id


def test_retrieve_excludes_a_completed_plan_that_still_has_a_row(tmp_path, monkeypatch):
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    # Pure local FTS: no embedding model, no FAISS, no reranker/HyDE — the
    # same harness the correction-reinjection suite uses.
    monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: None))
    engine = RetrievalEngine(db_obj, cfg, faiss_index=None)

    marker = "zzmarker plancontent unique token"
    _insert_doc(db_obj, "/vault/wiki/plan-done.md", "complete", marker)
    _insert_doc(db_obj, "/vault/wiki/live-note.md", "accepted", marker)

    results = engine.retrieve(marker, limit=10)
    # retrieve() reports the document under "source"; keep "path" too so the
    # assertion survives a shape change rather than silently passing.
    paths = [f"{r.get('source') or ''}{r.get('path') or ''}" for r in results]

    assert any("live-note" in p for p in paths), (
        "the control document must be retrievable, or this test proves nothing"
    )
    assert not any("plan-done" in p for p in paths), (
        "a completed plan must not be recallable even with a payload present"
    )


def test_the_excluded_row_is_still_present_in_documents(tmp_path):
    """Complement: the guarantee is 'not recalled', NOT 'deleted' — deleting
    the row would CASCADE memory_links and break doc_id identity."""
    db_obj, _cfg = _make_db(tmp_path)
    doc_id = _insert_doc(db_obj, "/vault/wiki/plan-done.md", "complete", "body text")

    with db_obj.cursor() as c:
        c.execute("SELECT page_status FROM documents WHERE doc_id=?", (doc_id,))
        assert c.fetchone()[0] == "complete"

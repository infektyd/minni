import os
import sqlite3
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "test.db"), vector_backends=["faiss-disk"])
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


class FakeBackend:
    name = "fake"
    dim = 384

    def __init__(self):
        self.items = []

    def upsert(self, items):
        self.items.extend(items)

    def remove(self, chunk_ids):
        self.items = [item for item in self.items if item.chunk_id not in set(chunk_ids)]

    def search(self, query_vec, k, filter=None):
        from vector_backend import VectorHit

        return [VectorHit(chunk_id=item.chunk_id, doc_id=item.doc_id, score=1.0, backend=self.name)
                for item in self.items[:k]]

    def stats(self):
        return {"name": self.name, "dim": self.dim, "vector_count": len(self.items)}


def test_protocol_dataclasses_and_multi_backend_rrf():
    from backends.multi import MultiBackend
    from vector_backend import VectorHit, VectorItem

    vec = np.ones(384, dtype=np.float32)
    item = VectorItem(chunk_id=1, doc_id=10, vector=vec, metadata={"agent": "codex"})
    assert item.metadata["agent"] == "codex"

    class BackendA(FakeBackend):
        name = "a"

        def search(self, query_vec, k, filter=None):
            return [
                VectorHit(chunk_id=1, doc_id=10, score=0.9, backend=self.name),
                VectorHit(chunk_id=2, doc_id=20, score=0.8, backend=self.name),
            ]

    class BackendB(FakeBackend):
        name = "b"

        def search(self, query_vec, k, filter=None):
            return [
                VectorHit(chunk_id=2, doc_id=20, score=0.95, backend=self.name),
                VectorHit(chunk_id=3, doc_id=30, score=0.7, backend=self.name),
            ]

    merged = MultiBackend([BackendA(), BackendB()]).search(vec, k=3)
    assert [hit.chunk_id for hit in merged] == [2, 1, 3]
    assert merged[0].backend in {"a", "b"}


def test_migration_003_creates_vector_backends_table(tmp_path):
    from migrations import run_migrations

    db_path = tmp_path / "migration.db"
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(vector_backends)").fetchall()}
    conn.close()

    assert {"name", "last_synced_chunk_rowid", "last_synced_at", "vector_count", "status"} <= cols


def test_vector_sync_upserts_new_chunks_and_tracks_state(tmp_path):
    from vector_sync import get_backend_state, sync_backend

    db_obj, _cfg = _make_db(tmp_path)
    now = time.time()
    vec = np.ones(384, dtype=np.float32)
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
            ("wiki/test.md", now),
        )
        doc_id = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, computed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, 0, "hello", vec.tobytes(), now),
        )

    backend = FakeBackend()
    result = sync_backend(backend, db_obj)
    state = get_backend_state("fake", db_obj)
    db_obj.close()

    assert result["status"] == "ok"
    assert result["upserted"] == 1
    assert len(backend.items) == 1
    assert state["vector_count"] == 1
    assert state["status"] == "ok"


def test_vector_sync_removes_deleted_chunks_when_dirty(tmp_path):
    """NEW-02: a deleted chunk's vector must leave the backend on the next sync.

    The incremental cursor only sees INSERTs (chunk_id > last_synced), so a
    delete leaves a stale vector in FAISS forever unless the dirty sync
    reconciles removals. Regression test for index-to-DB deletion drift.
    """
    from vector_sync import get_backend_state, mark_dirty, sync_backend

    db_obj, _cfg = _make_db(tmp_path)
    now = time.time()
    vec = np.ones(384, dtype=np.float32)
    with db_obj.cursor() as c:
        c.execute("INSERT INTO documents (path, indexed_at) VALUES (?, ?)", ("wiki/a.md", now))
        doc_id = c.lastrowid
        for i in range(2):
            c.execute(
                """INSERT INTO chunk_embeddings
                   (doc_id, chunk_index, chunk_text, embedding, computed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (doc_id, i, f"chunk{i}", vec.tobytes(), now),
            )

    backend = FakeBackend()
    sync_backend(backend, db_obj)
    assert {it.chunk_id for it in backend.items} == {1, 2}

    # Delete chunk 2 (mirrors a real deletion that calls mark_dirty).
    with db_obj.cursor() as c:
        c.execute("DELETE FROM chunk_embeddings WHERE chunk_id = 2")
    mark_dirty("fake", db_obj)

    result = sync_backend(backend, db_obj)
    state = get_backend_state("fake", db_obj)
    db_obj.close()

    remaining = {it.chunk_id for it in backend.items}
    assert 2 not in remaining, "deleted chunk's vector must be removed from the backend"
    assert remaining == {1}
    assert result["status"] == "ok"
    assert state["status"] == "ok"


def test_backend_resolver_preserves_default_bit_identical_path():
    from config import SovereignConfig
    from minnid import _resolve_backend

    assert _resolve_backend("auto", SovereignConfig(vector_backends=["faiss-disk"])) is None
    assert _resolve_backend(None, SovereignConfig(vector_backends=["faiss-disk"])) is None
    assert _resolve_backend("faiss-mem", SovereignConfig()) == "faiss-mem"
    assert _resolve_backend("auto", SovereignConfig(vector_backends=["faiss-disk", "faiss-mem"])) == [
        "faiss-disk",
        "faiss-mem",
    ]


def _fresh_engine(tmp_path, name):
    from db import SovereignDB
    from config import SovereignConfig
    from retrieval import RetrievalEngine

    cfg = SovereignConfig(db_path=str(tmp_path / name))
    return RetrievalEngine(SovereignDB(cfg), cfg)


def test_normalize_backend_names_dedups_and_preserves_order(tmp_path):
    """R9: a caller-supplied backend list is deduped (order-preserving) before
    any backend is constructed/loaded — e.g. ["faiss-disk"]*N must collapse to
    a single entry, not build N disk-loading backends."""
    engine = _fresh_engine(tmp_path, "t1.db")

    result = engine._normalize_backend_names(["faiss-disk"] * 50)
    assert result == ["faiss-disk"]

    result2 = engine._normalize_backend_names(["faiss-mem", "faiss-disk", "faiss-mem"])
    assert result2 == ["faiss-mem", "faiss-disk"]


def test_normalize_backend_names_caps_length(tmp_path):
    """R9: a backend list beyond _MAX_BACKENDS distinct known names is
    rejected loudly, not silently truncated/fanned-out unbounded."""
    from retrieval import RetrievalEngine

    engine = _fresh_engine(tmp_path, "t2.db")
    assert engine._MAX_BACKENDS == 4

    # Exactly at the cap (the 4 known backends): allowed.
    engine._normalize_backend_names(["faiss-disk", "faiss-mem", "qdrant", "lance"])

    # One more DISTINCT known name pushes past the cap -> must raise.
    original_known = RetrievalEngine._KNOWN_BACKENDS
    RetrievalEngine._KNOWN_BACKENDS = original_known + ("fifth-backend",)
    try:
        try:
            engine._normalize_backend_names(
                ["faiss-disk", "faiss-mem", "qdrant", "lance", "fifth-backend"]
            )
            raised = None
        except ValueError as exc:
            raised = str(exc)
    finally:
        RetrievalEngine._KNOWN_BACKENDS = original_known

    assert raised is not None and "too many backends" in raised, raised


def test_normalize_backend_names_rejects_unknown_backend_loudly(tmp_path):
    """R9: an unknown backend name must raise, not be silently skipped (which
    would let a caller's typo/probe pass through unnoticed)."""
    engine = _fresh_engine(tmp_path, "t3.db")

    try:
        engine._normalize_backend_names(["faiss-disk", "totally-not-a-backend"])
        raised = None
    except ValueError as exc:
        raised = str(exc)

    assert raised is not None and "unknown backend" in raised, raised

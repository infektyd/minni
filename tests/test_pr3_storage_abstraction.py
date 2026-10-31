import os
import sqlite3
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import MinniConfig

    cfg = MinniConfig(db_path=str(tmp_path / "test.db"), vector_backends=["faiss-disk"])
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.MinniDB(cfg)
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
        from minni.vector_backend import VectorHit

        return [VectorHit(chunk_id=item.chunk_id, doc_id=item.doc_id, score=1.0, backend=self.name)
                for item in self.items[:k]]

    def stats(self):
        return {"name": self.name, "dim": self.dim, "vector_count": len(self.items)}


def test_protocol_dataclasses():
    from minni.vector_backend import VectorHit, VectorItem

    vec = np.ones(384, dtype=np.float32)
    item = VectorItem(chunk_id=1, doc_id=10, vector=vec, metadata={"agent": "codex"})
    assert item.metadata["agent"] == "codex"
    hit = VectorHit(chunk_id=1, doc_id=10, score=0.9, backend="faiss-disk")
    assert hit.backend == "faiss-disk"



def test_migration_003_creates_vector_backends_table(tmp_path):
    from minni.migrations import run_migrations

    db_path = tmp_path / "migration.db"
    conn = sqlite3.connect(db_path)
    run_migrations(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(vector_backends)").fetchall()}
    conn.close()

    assert {"name", "last_synced_chunk_rowid", "last_synced_at", "vector_count", "status"} <= cols


def test_vector_sync_upserts_new_chunks_and_tracks_state(tmp_path):
    from minni.vector_sync import get_backend_state, sync_backend

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
    from minni.vector_sync import get_backend_state, mark_dirty, sync_backend

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
    from minni.config import MinniConfig
    from minni.minnid import _resolve_backend

    assert _resolve_backend("auto", MinniConfig(vector_backends=["faiss-disk"])) is None
    assert _resolve_backend(None, MinniConfig(vector_backends=["faiss-disk"])) is None
    # R4(c) (#226): this used to assert the bare string was passed through
    # unchanged. That WAS the defect — a bare string reached the "explicit
    # backend object" branch in retrieve() and had `.search` called on a `str`,
    # which surfaced to the caller as JSON-RPC -32000 (internal error) for what
    # is a perfectly documented parameter value. A named backend is now
    # normalized to the validated single-element list form.
    # The default paths asserted above are unchanged and still bit-identical.
    assert _resolve_backend("faiss-mem", MinniConfig()) == ["faiss-mem"]
    assert _resolve_backend("auto", MinniConfig(vector_backends=["faiss-disk", "faiss-mem"])) == [
        "faiss-disk",
        "faiss-mem",
    ]


def _fresh_engine(tmp_path, name):
    from minni.db import MinniDB
    from minni.config import MinniConfig
    from minni.retrieval import RetrievalEngine

    cfg = MinniConfig(db_path=str(tmp_path / name))
    return RetrievalEngine(MinniDB(cfg), cfg)


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
    from minni.retrieval import RetrievalEngine

    engine = _fresh_engine(tmp_path, "t2.db")
    assert engine._MAX_BACKENDS == 2

    # Exactly at the cap (the 2 known backends): allowed.
    engine._normalize_backend_names(["faiss-disk", "faiss-mem"])

    # One more DISTINCT known name pushes past the cap -> must raise.
    original_known = RetrievalEngine._KNOWN_BACKENDS
    RetrievalEngine._KNOWN_BACKENDS = original_known + ("third-backend",)
    try:
        try:
            engine._normalize_backend_names(
                ["faiss-disk", "faiss-mem", "third-backend"]
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


def test_h13_vector_sync_dirty_not_lost_on_remove_failure(tmp_path):
    """H13: if backend.remove fails during a dirty sync (setting status='error'),
    the next sync_backend pass must still reconcile deletions instead of dropping
    the dirty obligation forever."""
    from minni.vector_sync import get_backend_state, mark_dirty, sync_backend

    db_obj, _cfg = _make_db(tmp_path)
    now = time.time()
    vec = np.ones(384, dtype=np.float32)
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
            ("wiki/h13.md", now),
        )
        doc_id = c.lastrowid
        for i in range(2):
            c.execute(
                """INSERT INTO chunk_embeddings
                   (doc_id, chunk_index, chunk_text, embedding, computed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (doc_id, i, f"chunk{i}", vec.tobytes(), now),
            )

    class FlakyRemoveBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.fail_remove = False

        def remove(self, chunk_ids):
            if self.fail_remove:
                raise RuntimeError("transient remove failure")
            super().remove(chunk_ids)

    backend = FlakyRemoveBackend()
    sync_backend(backend, db_obj)
    assert {it.chunk_id for it in backend.items} == {1, 2}

    with db_obj.cursor() as c:
        c.execute("DELETE FROM chunk_embeddings WHERE chunk_id = 1")
    mark_dirty("fake", db_obj)

    backend.fail_remove = True
    res_err = sync_backend(backend, db_obj)
    assert res_err["status"] == "error"
    assert get_backend_state("fake", db_obj)["status"] == "error"

    backend.fail_remove = False
    res_retry = sync_backend(backend, db_obj)
    assert res_retry["status"] == "ok"
    remaining = {it.chunk_id for it in backend.items}
    db_obj.close()
    assert 1 not in remaining, (
        f"deleted chunk 1 remained in backend after retry following remove error: {remaining}"
    )


def test_h14_concurrent_mark_dirty_during_upsert_not_clobbered(tmp_path):
    """H14: if a deletion + mark_dirty happens while sync_backend is in the
    middle of upserting new chunks on a non-dirty pass, sync_backend must
    either reconcile the deletion immediately or preserve status='dirty'."""
    from minni.vector_sync import get_backend_state, mark_dirty, sync_backend

    db_obj, _cfg = _make_db(tmp_path)
    now = time.time()
    vec = np.ones(384, dtype=np.float32)
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
            ("wiki/h14.md", now),
        )
        doc_id = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, computed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, 0, "c0", vec.tobytes(), now),
        )

    class HookedBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.on_upsert = None

        def upsert(self, items):
            super().upsert(items)
            if self.on_upsert:
                self.on_upsert()

    backend = HookedBackend()
    sync_backend(backend, db_obj)
    assert {it.chunk_id for it in backend.items} == {1}

    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, computed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, 1, "c1", vec.tobytes(), now),
        )

    def concurrent_delete():
        with db_obj.cursor() as c:
            c.execute("DELETE FROM chunk_embeddings WHERE chunk_id = 1")
        mark_dirty("fake", db_obj)

    backend.on_upsert = concurrent_delete
    sync_backend(backend, db_obj)
    backend.on_upsert = None

    state = get_backend_state("fake", db_obj)
    remaining = {it.chunk_id for it in backend.items}
    db_obj.close()
    assert state["status"] == "dirty" or 1 not in remaining, (
        f"concurrent mark_dirty was clobbered to status={state['status']!r} and deleted chunk 1 remained in {remaining}"
    )


def test_p2_1_vector_sync_crash_mid_sync_reconciles_on_next_pass(tmp_path):
    """P2-1: sync_backend sets status='syncing' before deletion reconciliation.
    If the process crashes or an uncaught error aborts mid-sync leaving
    status='syncing', both should_sync and the next sync_backend pass must treat
    'syncing' as dirty so deleted vectors are reconciled."""
    import pytest
    from minni.vector_sync import get_backend_state, mark_dirty, should_sync, sync_backend

    db_obj, _cfg = _make_db(tmp_path)
    now = time.time()
    vec = np.ones(384, dtype=np.float32)
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
            ("wiki/p2_1.md", now),
        )
        doc_id = c.lastrowid
        for i in range(2):
            c.execute(
                """INSERT INTO chunk_embeddings
                   (doc_id, chunk_index, chunk_text, embedding, computed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (doc_id, i, f"chunk{i}", vec.tobytes(), now),
            )

    class SimulatedCrash(BaseException):
        pass

    class CrashingRemoveBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.crash_on_remove = False

        def remove(self, chunk_ids):
            if self.crash_on_remove:
                raise SimulatedCrash("process died mid-reconcile")
            super().remove(chunk_ids)

    backend = CrashingRemoveBackend()
    sync_backend(backend, db_obj)
    assert {it.chunk_id for it in backend.items} == {1, 2}

    with db_obj.cursor() as c:
        c.execute("DELETE FROM chunk_embeddings WHERE chunk_id = 1")
    mark_dirty("fake", db_obj)

    backend.crash_on_remove = True
    with pytest.raises(SimulatedCrash):
        sync_backend(backend, db_obj)
    assert get_backend_state("fake", db_obj)["status"] == "syncing"
    needs_sync = should_sync("fake", db_obj)

    backend.crash_on_remove = False
    res_retry = sync_backend(backend, db_obj)
    assert res_retry["status"] == "ok"
    assert get_backend_state("fake", db_obj)["status"] == "ok"
    remaining = {it.chunk_id for it in backend.items}
    db_obj.close()
    assert 1 not in remaining, (
        f"deleted chunk 1 was skipped after mid-sync crash left status='syncing': {remaining}"
    )
    assert needs_sync is True, (
        "should_sync must treat 'syncing' (interrupted sync) as needing sync"
    )

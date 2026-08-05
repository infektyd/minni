"""#288 — every raw _get_conn() call site must leave the connection clean.

`SovereignDB.cursor()` is the auto-commit contract: it commits on success and
rolls back on exception. `_get_conn()` hands out the raw connection and opts out
of that contract *silently*. PR #279 shipped a defect of exactly this shape — a
repair that wrote through `_get_conn()` and never committed, leaving the write
invisible to every other connection AND holding the write lock against every
daemon writer until the next sweep.

The reason it survived review is structural: the suite verified what queries
COMPUTE, never whether a write COMMITTED, and a test that reads back through the
same connection sees uncommitted rows just fine.

So these tests assert the property the suite could not see, for every site:

  * `conn.in_transaction is False` after the operation, and
  * an INDEPENDENT connection can still write (the lock was released).

The audit found no write-without-commit sites remaining: every raw `_get_conn()`
consumer outside `db.py` passes the connection to `compute_db_checksum`, which
is a single SELECT. These tests exist to keep it that way — the moment one of
these paths starts writing without committing, they go red.
"""

import os
import sqlite3
import tempfile

import numpy as np
import pytest


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "contract.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _assert_connection_is_clean(conn, cfg, label):
    """The two halves of the contract: no open transaction, and no held lock."""
    assert conn.in_transaction is False, (
        f"{label} left an open transaction — its writes are invisible to every "
        "other connection and it holds the write lock until something else "
        "happens to commit"
    )
    other = sqlite3.connect(cfg.db_path, timeout=5)
    try:
        other.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES ('lock-probe', 'message', 'probe', 1.0)"
        )
        other.commit()
    except sqlite3.OperationalError as exc:  # pragma: no cover - failure path
        pytest.fail(f"{label} still holds the write lock: {exc}")
    finally:
        other.close()


class TestDbModuleContracts:
    """db.py's own sites — the contract itself."""

    def test_cursor_commits_and_releases(self, tmp_path):
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO episodic_events"
                " (agent_id, event_type, content, created_at)"
                " VALUES ('forge', 'message', 'via cursor', 1.0)"
            )
        _assert_connection_is_clean(conn, cfg, "db.cursor()")
        db_obj.close()

    def test_cursor_rolls_back_and_releases_on_exception(self, tmp_path):
        """The failure half. A rollback that did not happen is the same stalled
        write lock as a missing commit."""
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        with pytest.raises(RuntimeError):
            with db_obj.cursor() as c:
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('forge', 'message', 'doomed', 1.0)"
                )
                raise RuntimeError("boom")
        _assert_connection_is_clean(conn, cfg, "db.cursor() on exception")
        with db_obj.cursor() as c:
            assert c.execute(
                "SELECT COUNT(*) AS n FROM episodic_events WHERE content = 'doomed'"
            ).fetchone()["n"] == 0
        db_obj.close()

    def test_transaction_commits_and_releases(self, tmp_path):
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        with db_obj.transaction() as c:
            c.execute(
                "INSERT INTO episodic_events"
                " (agent_id, event_type, content, created_at)"
                " VALUES ('forge', 'message', 'via transaction', 1.0)"
            )
        _assert_connection_is_clean(conn, cfg, "db.transaction()")
        db_obj.close()

    def test_transaction_rolls_back_and_releases_on_exception(self, tmp_path):
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        with pytest.raises(RuntimeError):
            with db_obj.transaction() as c:
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('forge', 'message', 'doomed', 1.0)"
                )
                raise RuntimeError("boom")
        _assert_connection_is_clean(conn, cfg, "db.transaction() on exception")
        db_obj.close()

    def test_connect_leaves_no_open_transaction(self, tmp_path):
        """db.py:529 — connect() acquires a connection purely to trigger schema
        init and migrations. Both write; both must commit."""
        import minni.db as db_mod
        from minni.config import SovereignConfig
        from minni.db import connect

        cfg = SovereignConfig(db_path=str(tmp_path / "connect.db"))
        old_flag = db_mod._migrations_run
        db_mod._migrations_run = False
        try:
            db_obj = connect(cfg)
        finally:
            db_mod._migrations_run = old_flag

        _assert_connection_is_clean(db_obj._get_conn(), cfg, "db.connect()")
        db_obj.close()


class TestChecksumConsumersAreReadOnly:
    """Every raw _get_conn() consumer outside db.py hands the connection to
    compute_db_checksum, directly or via FAISSIndex. That is a single SELECT
    today; these pin it, so adding a write there without a commit goes red.

    Sites covered: retrieval.py:864 and :879 (_ensure_faiss_loaded),
    backends/faiss_disk.py:60 (backend construction),
    sovereign_memory.py:292 and afm_passes/vault_ingest.py:359 (save_to_disk).
    """

    def _index_with_vectors(self, cfg):
        from minni.faiss_index import FAISSIndex

        idx = FAISSIndex(cfg)
        vecs = np.zeros((2, cfg.embedding_dim), dtype="float32")
        vecs[0][0] = 1.0
        vecs[1][1] = 1.0
        idx.build_from_vectors([1, 2], vecs)
        return idx

    def test_compute_db_checksum_leaves_no_transaction(self, tmp_path):
        from minni.faiss_persist import compute_db_checksum

        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        compute_db_checksum(conn)
        _assert_connection_is_clean(conn, cfg, "compute_db_checksum")
        db_obj.close()

    def test_save_to_disk_leaves_no_transaction(self, tmp_path):
        """sovereign_memory.py:292 and vault_ingest.py:359 both do exactly
        this: save_to_disk(db_conn=db._get_conn())."""
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        self._index_with_vectors(cfg).save_to_disk(db_conn=conn)
        _assert_connection_is_clean(conn, cfg, "FAISSIndex.save_to_disk(db_conn)")
        db_obj.close()

    def test_try_load_from_disk_leaves_no_transaction(self, tmp_path):
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        idx = self._index_with_vectors(cfg)
        idx.save_to_disk(db_conn=conn)
        idx.try_load_from_disk(db_conn=conn)
        _assert_connection_is_clean(conn, cfg, "FAISSIndex.try_load_from_disk(db_conn)")
        db_obj.close()

    def test_faiss_disk_backend_construction_leaves_no_transaction(self, tmp_path):
        """backends/faiss_disk.py:60 — the disk-cache load on construction."""
        from minni.backends.faiss_disk import FaissDiskBackend

        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        FaissDiskBackend(config=cfg, db=db_obj)
        _assert_connection_is_clean(conn, cfg, "FaissDiskBackend.__init__")
        db_obj.close()

    def test_ensure_faiss_loaded_leaves_no_transaction(self, tmp_path):
        """retrieval.py:864 and :879 — the warm-start path, which takes the raw
        connection twice: once for the disk-cache load, once for the rebuild
        checksum."""
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        RetrievalEngine(db_obj, cfg)._ensure_faiss_loaded()
        _assert_connection_is_clean(conn, cfg, "_ensure_faiss_loaded")
        db_obj.close()

    def test_a_rebuild_over_real_embeddings_leaves_no_transaction(self, tmp_path):
        """The same path with rows present, so the rebuild branch actually runs
        rather than short-circuiting on an empty table."""
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, last_modified,"
                " indexed_at, access_count, decay_score, whole_document)"
                " VALUES ('/d.md', 'forge', 'F', 1.0, 1.0, 0, 1.0, 0)"
            )
            doc_id = c.lastrowid
            for i in range(2):
                vec = np.zeros(cfg.embedding_dim, dtype="float32")
                vec[i] = 1.0
                c.execute(
                    "INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text,"
                    " embedding, model_name, computed_at)"
                    " VALUES (?, ?, ?, ?, 'test', 1.0)",
                    (doc_id, i, f"chunk {i}", vec.tobytes()),
                )

        RetrievalEngine(db_obj, cfg)._ensure_faiss_loaded()
        _assert_connection_is_clean(conn, cfg, "_ensure_faiss_loaded (rebuild)")
        db_obj.close()


class TestTheContractGuardActuallyDetects:
    """Proves _assert_connection_is_clean is not vacuous: an uncommitted write
    on a raw connection must trip both halves of it."""

    def test_an_uncommitted_write_is_caught(self, tmp_path):
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        conn.execute(
            "INSERT INTO episodic_events"
            " (agent_id, event_type, content, created_at)"
            " VALUES ('forge', 'message', 'uncommitted', 1.0)"
        )

        assert conn.in_transaction is True, "precondition: the write is open"
        with pytest.raises((AssertionError, Exception)):
            _assert_connection_is_clean(conn, cfg, "deliberate leak")

        conn.rollback()
        _assert_connection_is_clean(conn, cfg, "after rollback")
        db_obj.close()

    def test_an_independent_reader_cannot_see_an_uncommitted_write(self, tmp_path):
        """The other half of why #279's defect was invisible: reading back
        through the writing connection shows the row regardless."""
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()
        conn.execute(
            "INSERT INTO episodic_events"
            " (agent_id, event_type, content, created_at)"
            " VALUES ('forge', 'message', 'uncommitted', 1.0)"
        )

        same = conn.execute(
            "SELECT COUNT(*) FROM episodic_events WHERE content = 'uncommitted'"
        ).fetchone()[0]
        other = sqlite3.connect(cfg.db_path, timeout=5)
        try:
            seen = other.execute(
                "SELECT COUNT(*) FROM episodic_events WHERE content = 'uncommitted'"
            ).fetchone()[0]
        finally:
            other.close()

        assert same == 1, "the writing connection sees its own uncommitted row"
        assert seen == 0, (
            "an independent connection must NOT see it — this asymmetry is why "
            "a same-connection read-back cannot catch a missing commit"
        )
        conn.rollback()
        db_obj.close()

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

Audit result across all ten production sites (`grep -rn "_get_conn()" src/`):

  db.py:167, 179          the contract itself (cursor / transaction)
  db.py:529               init only — schema + migrations, which commit
  minnid.py:2095          init only — same, eager startup
  minnid.py:1327          WRITE — reconcile_episodic_fts; commits explicitly
                          at minnid.py:1322 and rolls back on exception
  retrieval.py:864, 879   read-only — connection goes to compute_db_checksum
  sovereign_memory.py:292 read-only — save_to_disk(db_conn=...) → checksum
  vault_ingest.py:359     read-only — same
  faiss_disk.py:60        read-only — try_load_from_disk(db_conn=...) → checksum

So exactly one raw write site remains outside db.py, and it is correct. These
tests exist to keep it that way — the moment one of these paths writes without
committing, they go red.

Two distinct guards, because end-state cleanliness alone is NOT sufficient:

  * `_assert_connection_is_clean` — after the operation, no open transaction
    and no held lock.
  * `_fails_if_a_write_is_pending_when_a_cursor_opens` — a leak at a raw site
    can be *laundered* by any later `db.cursor()` block on the same
    thread-local connection, which commits it and leaves the end state clean.
    That is the "held until something else happens to commit" hazard above, and
    an end-state assertion cannot see it. Verified: an uncommitted INSERT at
    retrieval.py:864 passed all end-state tests before this guard existed.
"""

import sqlite3
from contextlib import contextmanager

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
    """No open transaction, and no held write lock.

    Under WAL the second check is defence-in-depth rather than an independent
    detector: a held write lock implies an open transaction, so the assert below
    fires first in every leak this suite can construct. It is kept because it
    states the consequence in the terms an operator experiences (other writers
    block), and because it stops being redundant the moment the journal mode
    changes. `test_the_lock_probe_detects_a_held_lock` exercises it directly, so
    it cannot rot into a no-op.
    """
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


@contextmanager
def _fails_if_a_write_is_pending_when_a_cursor_opens(db_obj, label):
    """Catch a raw-site leak at the moment something else would LAUNDER it.

    A write left open by a raw `_get_conn()` block is committed by the next
    `db.cursor()` on the same thread-local connection, which restores a clean
    end state and hides the leak from any after-the-fact assertion. So this
    wraps cursor()/transaction() and checks `in_transaction` on ENTRY: nothing
    should be pending when an unrelated block opens.

    Scoped to a single audited call path per test — a cursor() legitimately
    nested inside another writing cursor() would trip it.
    """
    import minni.db as db_mod

    conn = db_obj._get_conn()
    originals = {
        name: getattr(db_mod.SovereignDB, name) for name in ("cursor", "transaction")
    }
    laundered = []

    def _wrap(original):
        @contextmanager
        def wrapper(self, *args, **kwargs):
            if conn.in_transaction:
                laundered.append(True)
            with original(self, *args, **kwargs) as c:
                yield c

        return wrapper

    for name, original in originals.items():
        setattr(db_mod.SovereignDB, name, _wrap(original))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(db_mod.SovereignDB, name, original)

    assert not laundered, (
        f"{label} left a write pending when a later cursor() opened — that "
        "cursor commits it, so the end state looks clean while the raw site "
        "held the write lock across everything in between"
    )


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
    """The six read-only sites hand their connection to compute_db_checksum,
    directly or via FAISSIndex. That is a single SELECT today; these pin it, so
    adding a write there without a commit goes red.

    Sites covered: retrieval.py:864 and :879 (_ensure_faiss_loaded),
    backends/faiss_disk.py:60 (backend construction),
    sovereign_memory.py:292 and afm_passes/vault_ingest.py:359 (save_to_disk).
    The one WRITE site, minnid.py:1327, is covered by TestTheSweepWriteSite.
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
        # AssertionError specifically: `Exception` would also swallow a
        # NameError or TypeError from a broken helper and call that detection.
        with pytest.raises(AssertionError):
            _assert_connection_is_clean(conn, cfg, "deliberate leak")

        conn.rollback()
        _assert_connection_is_clean(conn, cfg, "after rollback")
        db_obj.close()

    def test_the_lock_probe_detects_a_held_lock(self, tmp_path):
        """Exercises the probe half directly, so it cannot rot into a no-op.

        Driven from a SECOND connection holding BEGIN IMMEDIATE, so the audited
        connection's own in_transaction stays False and the assert half cannot
        pre-empt the probe — which is the only way to test the probe in
        isolation under WAL.
        """
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()

        holder = sqlite3.connect(cfg.db_path, timeout=5)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO episodic_events"
            " (agent_id, event_type, content, created_at)"
            " VALUES ('holder', 'message', 'held', 1.0)"
        )
        try:
            assert conn.in_transaction is False, (
                "precondition: the assert half must not fire, so the probe is "
                "the only thing that can detect this"
            )
            with pytest.raises(BaseException) as caught:
                _assert_connection_is_clean(conn, cfg, "lock held elsewhere")
            assert "write lock" in str(caught.value)
        finally:
            holder.rollback()
            holder.close()

        # And it passes again once the lock is released.
        _assert_connection_is_clean(conn, cfg, "after the holder released")
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


class TestNoLeakIsLaunderedByALaterCursor:
    """End-state cleanliness is necessary but not sufficient. Verified: an
    uncommitted INSERT at retrieval.py:864 passed every end-state test in this
    file, because `with self.db.cursor()` at retrieval.py:885 committed it."""

    def test_ensure_faiss_loaded_holds_nothing_across_its_cursor(self, tmp_path):
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path)
        engine = RetrievalEngine(db_obj, cfg)
        with _fails_if_a_write_is_pending_when_a_cursor_opens(
            db_obj, "_ensure_faiss_loaded"
        ):
            engine._ensure_faiss_loaded()
        db_obj.close()

    def test_rebuild_path_holds_nothing_across_its_cursor(self, tmp_path):
        """With embeddings present, so the rebuild branch runs and reaches both
        raw takes before the laundering cursor()."""
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path)
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

        engine = RetrievalEngine(db_obj, cfg)
        with _fails_if_a_write_is_pending_when_a_cursor_opens(
            db_obj, "_ensure_faiss_loaded (rebuild)"
        ):
            engine._ensure_faiss_loaded()
        db_obj.close()

    def test_the_laundering_guard_actually_detects(self, tmp_path):
        """Proves the guard is not vacuous: a pending raw write followed by a
        cursor() must be reported."""
        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()

        with pytest.raises(AssertionError):
            with _fails_if_a_write_is_pending_when_a_cursor_opens(
                db_obj, "deliberate leak"
            ):
                conn.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('forge', 'message', 'pending', 1.0)"
                )
                with db_obj.cursor() as c:  # launders it
                    c.execute("SELECT 1")
        db_obj.close()


class TestTheSweepWriteSite:
    """minnid.py:1327 — the one raw _get_conn() WRITE site outside db.py. It
    commits explicitly at minnid.py:1322 and rolls back on exception; this is
    the guard for that, independent of the #279 test that first pinned it."""

    def _db_with_unindexed_events(self, tmp_path):
        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_insert")
            for i in range(3):
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('forge', 'message', ?, 1.0)",
                    (f"unindexed row {i}",),
                )
        return db_obj, cfg

    def _run_sweep(self, db_obj, cfg):
        import minni.minnid as minnid
        from minni.db import SovereignDB

        original_cfg = minnid.DEFAULT_CONFIG
        original_shared = SovereignDB.shared
        minnid.DEFAULT_CONFIG = cfg
        SovereignDB.shared = staticmethod(lambda *a, **k: db_obj)
        try:
            return minnid._backfill_sweep_once()
        finally:
            SovereignDB.shared = original_shared
            minnid.DEFAULT_CONFIG = original_cfg

    def test_the_sweep_commits_and_releases(self, tmp_path):
        db_obj, cfg = self._db_with_unindexed_events(tmp_path)
        conn = db_obj._get_conn()

        self._run_sweep(db_obj, cfg)

        _assert_connection_is_clean(conn, cfg, "_backfill_sweep_once")
        # Durability, from an independent connection: rows only the sweep's own
        # connection can see have repaired nothing.
        other = sqlite3.connect(cfg.db_path, timeout=5)
        try:
            indexed = other.execute(
                "SELECT COUNT(*) FROM episodic_fts WHERE episodic_fts MATCH 'unindexed'"
            ).fetchone()[0]
        finally:
            other.close()
        assert indexed == 3, (
            "the sweep's writes must be visible to other connections, not "
            f"pending on its own (independent reader saw {indexed})"
        )
        db_obj.close()

    def test_a_failed_sweep_releases_the_lock(self, tmp_path):
        """The rollback half — a reconcile that writes and then raises must not
        leave the lock held until the next sweep (3600s by default)."""
        import minni.episodic as episodic_mod

        db_obj, cfg = self._db_with_unindexed_events(tmp_path)
        conn = db_obj._get_conn()

        def _write_then_fail(c):
            c.execute(
                "INSERT INTO episodic_fts(event_id, agent_id, content)"
                " VALUES (424242, 'forge', 'half written')"
            )
            raise sqlite3.OperationalError("disk I/O error mid-backfill")

        original = episodic_mod.reconcile_episodic_fts
        episodic_mod.reconcile_episodic_fts = _write_then_fail
        try:
            results = self._run_sweep(db_obj, cfg)
        finally:
            episodic_mod.reconcile_episodic_fts = original

        assert "error" in results["episodic_fts"]
        _assert_connection_is_clean(conn, cfg, "_backfill_sweep_once (failed)")
        db_obj.close()

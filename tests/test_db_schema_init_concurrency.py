"""Regression coverage for process-wide SQLite schema initialization."""

import inspect
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from minni import db as db_mod
from minni.config import SovereignConfig


def _make_cfg(tmp_path):
    return SovereignConfig(
        db_path=str(tmp_path / "minni.db"),
        faiss_index_path=str(tmp_path / "minni.faiss"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
    )


def _make_db(tmp_path):
    cfg = _make_cfg(tmp_path)
    db_obj = db_mod.SovereignDB(cfg)
    db_obj._get_conn()
    return db_obj, cfg


def test_schema_initialization_is_serialized_across_db_instances(tmp_path, monkeypatch):
    cfg = SovereignConfig(
        db_path=str(tmp_path / "minni.db"),
        faiss_index_path=str(tmp_path / "minni.faiss"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
    )
    first = db_mod.SovereignDB(cfg)
    second = db_mod.SovereignDB(cfg)
    original = db_mod.SovereignDB._init_schema
    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    def observed_init(self, conn):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return original(self, conn)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(db_mod.SovereignDB, "_init_schema", observed_init)
    errors = []

    def open_and_query(db):
        try:
            with db.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM vault_fts")
                cursor.fetchone()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=open_and_query, args=(first,)),
        threading.Thread(target=open_and_query, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1


# ---------------------------------------------------------------------------
# Punch-list §2 fix (a): process-wide _schema_ready_paths — schema DDL runs
# once per db-path per process, not once per SovereignDB instance.
# ---------------------------------------------------------------------------


def test_schema_ddl_runs_once_per_process_per_path(tmp_path, monkeypatch):
    """A second SovereignDB opened on the same db-path must NOT re-run
    _init_schema. Before the fix, the per-instance ``self._schema_initialized``
    flag let every fresh instance re-run the trigger DDL against the shared
    file, bumping the SQLite schema cookie under concurrent vault_fts readers
    ("vtable constructor failed: vault_fts"). After it, the process-wide
    ``_schema_ready_paths`` set gates the DDL to exactly once per path.
    """
    cfg = _make_cfg(tmp_path)
    # Defensive: this path must start un-initialized. tmp_path is unique per
    # test and the conftest autouse fixture already resets the set, but mirror
    # the _migrated_paths reset discipline explicitly here.
    db_mod._schema_ready_paths.discard(os.path.abspath(cfg.db_path))

    calls = []
    original = db_mod.SovereignDB._init_schema

    def counting_init(self, conn):
        calls.append(os.path.abspath(self.config.db_path))
        return original(self, conn)

    monkeypatch.setattr(db_mod.SovereignDB, "_init_schema", counting_init)

    first = db_mod.SovereignDB(cfg)
    second = db_mod.SovereignDB(cfg)
    first._get_conn()
    second._get_conn()

    assert calls == [os.path.abspath(cfg.db_path)], (
        f"_init_schema must run exactly once per path per process; ran {len(calls)}x"
    )
    # The second instance still has a fully usable schema (created on-disk by
    # the first) even though it skipped _init_schema.
    with second.cursor() as c:
        c.execute("SELECT count(*) FROM vault_fts")
        c.fetchone()


def test_schema_ready_path_not_recorded_when_init_fails(tmp_path, monkeypatch):
    """Fail-loud parity with _migrated_paths: a failed _init_schema must NOT
    mark the path ready, so the next instance retries it."""
    cfg = _make_cfg(tmp_path)
    abs_path = os.path.abspath(cfg.db_path)
    db_mod._schema_ready_paths.discard(abs_path)

    boom = db_mod.SovereignDB._init_schema

    def failing_init(self, conn):
        raise RuntimeError("simulated schema init failure")

    monkeypatch.setattr(db_mod.SovereignDB, "_init_schema", failing_init)
    first = db_mod.SovereignDB(cfg)
    with pytest.raises(RuntimeError):
        first._get_conn()
    assert abs_path not in db_mod._schema_ready_paths

    # Restore the real init; a fresh instance must now succeed (path was not
    # poisoned into the ready set by the failure).
    monkeypatch.setattr(db_mod.SovereignDB, "_init_schema", boom)
    second = db_mod.SovereignDB(cfg)
    second._get_conn()
    assert abs_path in db_mod._schema_ready_paths


def test_migration_failure_does_not_poison_schema_ready_gate(tmp_path, monkeypatch):
    """A migrations-only failure caught INSIDE _init_schema must not mark the
    path schema-ready — otherwise the process-wide _schema_ready_paths gate
    silently disables the migration retry contract for the life of the process.

    _init_schema runs the migrations block in a try/except that swallows a
    transient failure as non-fatal (e.g. 'database is locked' from a competing
    first-contact process) and deliberately leaves _migrated_paths unset so the
    run is retried on the next open of this path. _init_schema then returns
    normally. If _schema_ready_paths were keyed off 'did _init_schema return'
    rather than 'did migrations succeed', that swallowed failure would poison
    the gate: every later SovereignDB the daemon/AFM loop constructs would skip
    _init_schema entirely and migrations would NEVER be retried this process
    ('no such table'-class errors). This is the exact path
    ``test_schema_ready_path_not_recorded_when_init_fails`` does NOT cover — it
    exercises _init_schema itself raising, not a migrations-only internal catch.
    """
    import minni.migrations as migrations_mod

    cfg = _make_cfg(tmp_path)
    abs_path = os.path.abspath(cfg.db_path)
    db_mod._schema_ready_paths.discard(abs_path)
    db_mod._migrated_paths.discard(abs_path)
    monkeypatch.setattr(db_mod, "_migrations_run", False, raising=False)

    real_run_migrations = migrations_mod.run_migrations
    calls = {"n": 0}

    def flaky_run_migrations(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a transient first-contact failure (competing process).
            raise sqlite3.OperationalError("database is locked")
        return real_run_migrations(conn)

    monkeypatch.setattr(migrations_mod, "run_migrations", flaky_run_migrations)

    # First instance: migrations raise, _init_schema swallows it non-fatally and
    # returns. The gate must NOT record the path (migrations did not succeed).
    first = db_mod.SovereignDB(cfg)
    first._get_conn()
    assert calls["n"] == 1, "run_migrations must have been attempted exactly once"
    assert abs_path not in db_mod._migrated_paths, (
        "a swallowed migration failure must not record _migrated_paths"
    )
    assert abs_path not in db_mod._schema_ready_paths, (
        "the process-wide schema-ready gate must NOT be poisoned by a swallowed "
        "migration failure — otherwise migrations are never retried this process"
    )

    # Second instance on the same path: must re-enter _init_schema and RETRY
    # migrations (now succeeding), then mark the path ready. Before the fix the
    # gate skipped _init_schema here and calls["n"] stayed at 1.
    second = db_mod.SovereignDB(cfg)
    second._get_conn()
    assert calls["n"] == 2, (
        "the second SovereignDB instance must retry migrations; the gate wrongly "
        "skipped _init_schema if run_migrations was not attempted again"
    )
    assert abs_path in db_mod._migrated_paths
    assert abs_path in db_mod._schema_ready_paths


# ---------------------------------------------------------------------------
# Punch-list §2 fix (b): trigger DDL is idempotent (CREATE IF NOT EXISTS),
# not DROP + CREATE which bumps the schema cookie on every init.
# ---------------------------------------------------------------------------


def test_trigger_ddl_is_idempotent_no_drop_trigger():
    src = inspect.getsource(db_mod.SovereignDB._init_schema)
    assert "DROP TRIGGER" not in src, (
        "trigger DDL still uses DROP TRIGGER; convert to CREATE TRIGGER IF NOT "
        "EXISTS — DROP+CREATE bumps the schema cookie on every init"
    )
    assert "CREATE TRIGGER IF NOT EXISTS" in src


def test_trigger_ddl_is_idempotent_at_runtime(tmp_path):
    """Re-running _init_schema on an already-initialized db must not raise
    (the whole point of IF NOT EXISTS) — proves two processes racing first
    contact on the same file cannot fail on a duplicate-trigger error."""
    db_obj, _cfg = _make_db(tmp_path)
    conn = db_obj._get_conn()
    # Directly re-run schema init against the same connection twice more.
    db_obj._init_schema(conn)
    db_obj._init_schema(conn)


# ---------------------------------------------------------------------------
# Punch-list §2 fix (c): bounded retry around the vault_fts MATCH reads.
# ---------------------------------------------------------------------------


class _CountingCursor:
    """Minimal cursor stub that raises ``error`` on the first ``fail_times``
    execute() calls (and optionally the first ``fetch_fail_times`` fetchall()
    calls), then succeeds — for exercising the retry helper directly."""

    def __init__(self, fail_times, error, fetch_fail_times=0, rows=()):
        self.calls = 0
        self.fetch_calls = 0
        self._fail_times = fail_times
        self._fetch_fail_times = fetch_fail_times
        self._error = error
        self._rows = list(rows)

    def execute(self, sql, params=None):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        return self

    def fetchall(self):
        self.fetch_calls += 1
        if self.fetch_calls <= self._fetch_fail_times:
            raise self._error
        return list(self._rows)


def test_fts_retry_recovers_from_transient_vtable_error(monkeypatch):
    from minni import retrieval as r

    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)
    cur = _CountingCursor(
        fail_times=1,
        error=sqlite3.OperationalError("vtable constructor failed: vault_fts"),
    )
    rows = r._fts_execute_with_retry(cur, "SELECT 1 WHERE vault_fts MATCH ?", ["x"])
    assert cur.calls == 2  # failed once, retried, succeeded
    assert rows == []  # helper returns the fetched rows (review r3)


def test_fts_retry_recovers_from_fetch_time_vtable_error(monkeypatch):
    """Review r3 (P2): the schema-cookie race can also surface while STEPPING
    the SELECT — i.e. during fetchall() after a successful execute() — so the
    fetch must live inside the retry window too."""
    from minni import retrieval as r

    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)
    cur = _CountingCursor(
        fail_times=0,
        fetch_fail_times=1,
        error=sqlite3.OperationalError("database schema has changed"),
        rows=[("doc-1",)],
    )
    rows = r._fts_execute_with_retry(cur, "SELECT 1 WHERE vault_fts MATCH ?", ["x"])
    assert cur.fetch_calls == 2  # fetch failed once, whole read retried
    assert rows == [("doc-1",)]


def test_fts_retry_recovers_from_schema_changed(monkeypatch):
    from minni import retrieval as r

    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)
    cur = _CountingCursor(
        fail_times=2,
        error=sqlite3.OperationalError("database schema has changed"),
    )
    r._fts_execute_with_retry(cur, "SELECT 1", [])
    assert cur.calls == 3  # two transient failures, third attempt succeeds


def test_fts_retry_fails_loud_after_budget(monkeypatch):
    from minni import retrieval as r

    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)
    cur = _CountingCursor(
        fail_times=99,
        error=sqlite3.OperationalError("vtable constructor failed: vault_fts"),
    )
    with pytest.raises(sqlite3.OperationalError, match="vtable constructor failed"):
        r._fts_execute_with_retry(cur, "SELECT 1", [])
    assert cur.calls == 3  # bounded to the retry budget, then re-raises (loud)


def test_fts_retry_does_not_mask_other_operational_errors(monkeypatch):
    from minni import retrieval as r

    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)
    cur = _CountingCursor(
        fail_times=99,
        error=sqlite3.OperationalError("no such column: bogus"),
    )
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        r._fts_execute_with_retry(cur, "SELECT 1", [])
    assert cur.calls == 1  # non-transient error raises immediately, no retry


def test_both_vault_fts_call_sites_use_retry_helper():
    """SCOPE-MISS guard: the retry must wrap BOTH vault_fts MATCH sites."""
    from minni import retrieval as r

    fts_src = inspect.getsource(r.RetrievalEngine._fts_search)
    chrono_src = inspect.getsource(r.RetrievalEngine._chronological_search)
    assert "_fts_execute_with_retry" in fts_src, "_fts_search must use the retry helper"
    assert "_fts_execute_with_retry" in chrono_src, (
        "_chronological_search must use the retry helper"
    )


class _FailOnceCursor:
    """Wraps a real sqlite3 cursor and raises a transient vtable error the
    first time a vault_fts MATCH statement runs, then delegates to the real
    cursor. Exercises the retry loop end-to-end through _fts_search."""

    def __init__(self, real, state):
        self._real = real
        self._state = state

    def execute(self, sql, params=None):
        if "vault_fts MATCH" in sql:
            self._state["match_calls"] += 1
            if self._state["match_calls"] == 1:
                raise sqlite3.OperationalError("vtable constructor failed: vault_fts")
        if params is None:
            return self._real.execute(sql)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_fts_search_recovers_from_transient_vtable_race(tmp_path, monkeypatch):
    from minni import retrieval as r
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, agent, sigil) VALUES (?, ?, ?)",
            ("wiki/x.md", "main", "X"),
        )
        did = c.lastrowid
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil)"
            " VALUES (?, ?, ?, ?, ?)",
            (did, "wiki/x.md", "aurora protocol seal timeout", "main", "X"),
        )

    monkeypatch.setattr(r.time, "sleep", lambda *_a, **_k: None)
    state = {"match_calls": 0}
    real_cursor_cm = db_obj.cursor

    @contextmanager
    def flaky_cursor_cm():
        with real_cursor_cm() as real_c:
            yield _FailOnceCursor(real_c, state)

    monkeypatch.setattr(db_obj, "cursor", flaky_cursor_cm)

    results = engine._fts_search("aurora protocol seal timeout", limit=5)

    assert state["match_calls"] >= 2, "the transient vtable error path was not exercised"
    assert any(row["path"] == "wiki/x.md" for row in results), (
        "the bounded retry did not recover the row after a transient vtable error"
    )

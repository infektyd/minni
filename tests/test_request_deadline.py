"""Cooperative SQL/request deadlines; temporary databases and no models."""
import dataclasses
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from minni.config import DEFAULT_CONFIG
from minni.db import SovereignDB, _BudgetConnection
from minni.request_deadline import RequestDeadlineExceeded, current_deadline, request_deadline


@pytest.mark.parametrize('invalid', [float('inf'), float('-inf'), float('nan'), 'bad', True, 10**400])
def test_invalid_deadline_preserves_outer_scope(invalid):
    outer = time.monotonic() + 10
    with request_deadline(outer):
        with pytest.raises(ValueError, match='finite'):
            with request_deadline(invalid):
                pytest.fail('invalid scope entered')
        assert current_deadline() == outer
    assert current_deadline() is None


def test_nonfinite_rpc_budget_uses_default():
    from minni.minnid_runtime.recall import _search_deadline_monotonic
    now = time.monotonic()
    deadline = _search_deadline_monotonic({'timeout_ms': float('inf'), '_accepted_monotonic': float('-inf')})
    assert now + 20 < deadline < now + 26


@pytest.fixture
def db(tmp_path):
    config = dataclasses.replace(DEFAULT_CONFIG, db_path=str(tmp_path / 'test.db'), vault_path=str(tmp_path / 'vault'))
    database = SovereignDB(config)
    database._get_conn()
    yield database
    database.close()


def test_expired_budget_rolls_back_and_restores_reused_connection(db):
    conn = db._get_conn()
    conn.execute('CREATE TABLE deadline_probe (value INTEGER)')
    conn.commit()
    conn.execute('PRAGMA busy_timeout=4321')
    progress = lambda: 0
    conn.set_progress_handler(progress, 17)
    with pytest.raises(RequestDeadlineExceeded):
        with request_deadline(time.monotonic() + .02):
            with db.cursor() as c:
                c.execute('INSERT INTO deadline_probe VALUES (1)')
                time.sleep(.03)
    assert current_deadline() is None
    assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 4321
    assert conn._progress_callback is progress
    assert conn._progress_steps == 17
    assert not conn.in_transaction
    assert conn.execute('SELECT COUNT(*) FROM deadline_probe').fetchone()[0] == 0
    with db.cursor() as c:
        c.execute('INSERT INTO deadline_probe VALUES (2)')
    assert conn.execute('SELECT value FROM deadline_probe').fetchone()[0] == 2


def test_busy_wait_respects_remaining_budget(db):
    other = sqlite3.connect(db.config.db_path)
    other.execute('BEGIN IMMEDIATE')
    started = time.monotonic()
    try:
        with pytest.raises((RequestDeadlineExceeded, sqlite3.OperationalError)):
            with request_deadline(started + .04):
                with db.cursor() as c:
                    c.execute("INSERT INTO score_distribution (raw_score, kind, created_at) VALUES (0.1, 'combined', 0)")
        assert time.monotonic() - started < .5
    finally:
        other.rollback()
        other.close()
    assert not db._get_conn().in_transaction
    assert db._get_conn().execute('PRAGMA busy_timeout').fetchone()[0] == 30000


def test_progress_interrupts_query_and_resets(db):
    with pytest.raises(RequestDeadlineExceeded):
        with request_deadline(time.monotonic() + .02):
            with db.cursor() as c:
                c.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n')
                c.fetchone()
    assert db._get_conn()._progress_callback is None
    assert db._get_conn().execute('SELECT 1').fetchone()[0] == 1


def test_expired_scope_does_not_open_connection(tmp_path):
    database = SovereignDB(dataclasses.replace(DEFAULT_CONFIG, db_path=str(tmp_path / 'cold.db'), vault_path=str(tmp_path / 'vault')))
    with request_deadline(time.monotonic() - 1), pytest.raises(RequestDeadlineExceeded):
        database._get_conn()
    assert not hasattr(database._local, 'conn')


def test_nested_budget_cannot_extend_outer_deadline():
    deadline = time.monotonic() + 1
    with request_deadline(deadline):
        with request_deadline(deadline + 10):
            assert current_deadline() == deadline
        assert current_deadline() == deadline
    assert current_deadline() is None


def _context(engine):
    from minni.minnid_runtime.recall import RecallContext
    return RecallContext(
        make_error=lambda code, message, rid: {'error': message},
        make_response=lambda result, rid: {'result': result},
        handler_principal=lambda params, rid: (None, None),
        lazy_retrieval=lambda: engine,
        agent_vault_retrieval=lambda agent: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: None,
        record_latency=lambda *args: None,
        lazy_episodic=None,
        default_config=dataclasses.replace(DEFAULT_CONFIG, recall_trace=False),
        logger=logging.getLogger('deadline-test'),
    )


def test_completed_ranking_survives_expiry_without_tail_work(monkeypatch):
    import minni.request_deadline as deadline_module
    from minni.minnid_runtime.recall import handle_search
    clock = {'now': 1000.0}
    monkeypatch.setattr(deadline_module.time, 'monotonic', lambda: clock['now'])
    calls = []
    def retrieve(*args, **kwargs):
        clock['now'] = 1002.0
        return [{'doc_id': 1, 'path': 'a.md', 'score': .9, 'confidence_raw': .9}]
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('post-deadline operation')
    engine = SimpleNamespace(retrieve=retrieve, search_learnings=forbidden, search_episodic=forbidden,
                             db=SimpleNamespace(cursor=forbidden), config=DEFAULT_CONFIG)
    response = handle_search({'query': 'known', 'timeout_ms': 1000, 'layers': ['episodic']}, 1, _context(engine))
    assert 'error' not in response, response
    assert response['result']['results'][0]['doc_id'] == 1
    assert 'confidence_raw' not in response['result']['results'][0]
    assert response['result']['degraded'] is True
    assert {'document_access', 'score_calibration', 'learnings', 'episodic'} <= {
        d.get('stage') for d in response['result']['degradation']
    }
    assert calls == []
    assert current_deadline() is None


def test_learning_rows_survive_tracking_expiry_and_transaction_rolls_back(db, monkeypatch):
    from minni.db import _BudgetCursor
    from minni.retrieval import RetrievalEngine
    import minni.request_deadline as deadline_module
    with db.cursor() as c:
        c.execute("INSERT INTO learnings (agent_id, content, category, created_at) VALUES ('codex', 'deadline memory', 'fact', 0)")
    engine = object.__new__(RetrievalEngine)
    engine.db = db
    clock = {'now': 1000.0}
    monkeypatch.setattr(deadline_module.time, 'monotonic', lambda: clock['now'])
    original = _BudgetCursor.execute
    def execute(cursor, sql, *args, **kwargs):
        result = original(cursor, sql, *args, **kwargs)
        if sql.lstrip().startswith('UPDATE learnings'):
            clock['now'] = 1002.0
        return result
    monkeypatch.setattr(_BudgetCursor, 'execute', execute)
    with request_deadline(1001.0):
        rows = engine.search_learnings('deadline', agent_id='codex')
    assert len(rows) == 1
    assert rows[0]['content'] == 'deadline memory'
    with db.cursor() as c:
        assert c.execute('SELECT access_count FROM learnings').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_reads').fetchone()[0] == 0


def test_bulk_scores_stop_and_keep_consistent_response_when_budget_expires(db, monkeypatch):
    import minni.request_deadline as deadline_module
    import minni.scoring as scoring
    from minni.minnid_runtime.recall import handle_search
    clock = {'now': 1000.0}
    monkeypatch.setattr(deadline_module.time, 'monotonic', lambda: clock['now'])
    calls = []
    def record(*args):
        calls.append('record')
        clock['now'] = 1002.0
    monkeypatch.setattr(scoring, 'record_score', record)
    rows = [{'path': f'{i}.md', 'score': .9, 'confidence': .2, 'confidence_raw': .9} for i in range(3)]
    engine = SimpleNamespace(retrieve=lambda *a, **k: rows, search_learnings=lambda *a, **k: [], db=db, config=DEFAULT_CONFIG)
    response = handle_search({'query': 'known', 'timeout_ms': 1000}, 1, _context(engine))
    assert 'error' not in response, response
    assert calls == ['record']
    assert all(row['confidence'] == .2 for row in response['result']['results'])
    assert any(d.get('stage') == 'score_calibration' for d in response['result']['degradation'])


def test_request_budget_covers_multiple_connections_and_resets(db, tmp_path):
    second = SovereignDB(dataclasses.replace(DEFAULT_CONFIG, db_path=str(tmp_path / 'second.db'), vault_path=str(tmp_path / 'second-vault')))
    second._get_conn()
    try:
        with request_deadline(time.monotonic() - 1):
            for database in (db, second):
                with pytest.raises(RequestDeadlineExceeded), database.cursor():
                    pytest.fail('expired cursor was yielded')
        for database in (db, second):
            with database.cursor() as c:
                assert c.execute('SELECT 1').fetchone()[0] == 1
    finally:
        second.close()


def test_schema_lock_wait_is_bounded_and_next_request_can_open(tmp_path):
    import threading
    import minni.db as database_module
    held, release = threading.Event(), threading.Event()
    def holder():
        with database_module._schema_init_lock:
            held.set()
            release.wait(1)
    worker = threading.Thread(target=holder)
    worker.start()
    assert held.wait(1)
    database = SovereignDB(dataclasses.replace(DEFAULT_CONFIG, db_path=str(tmp_path / 'blocked.db'), vault_path=str(tmp_path / 'vault')))
    started = time.monotonic()
    try:
        with request_deadline(started + .02), pytest.raises(RequestDeadlineExceeded):
            database._get_conn()
        assert time.monotonic() - started < .5
    finally:
        release.set()
        worker.join(1)
    assert not worker.is_alive()
    assert database._get_conn().execute('SELECT 1').fetchone()[0] == 1
    database.close()


def test_learning_tracking_non_deadline_failure_still_propagates(db, monkeypatch):
    from minni.db import _BudgetCursor
    from minni.retrieval import RetrievalEngine
    with db.cursor() as c:
        c.execute("INSERT INTO learnings (agent_id, content, category, created_at) VALUES ('codex', 'deadline memory', 'fact', 0)")
    engine = object.__new__(RetrievalEngine)
    engine.db = db
    original = _BudgetCursor.execute
    def execute(cursor, sql, *args, **kwargs):
        if sql.lstrip().startswith('UPDATE learnings'):
            raise RuntimeError('ordinary tracking failure')
        return original(cursor, sql, *args, **kwargs)
    monkeypatch.setattr(_BudgetCursor, 'execute', execute)
    with pytest.raises(RuntimeError, match='ordinary tracking failure'):
        engine.search_learnings('deadline', agent_id='codex')


def test_executor_scope_requires_explicit_context_propagation():
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    deadline = time.monotonic() + 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        with request_deadline(deadline):
            assert pool.submit(current_deadline).result() is None
            context = copy_context()
            assert pool.submit(context.run, current_deadline).result() == deadline
        assert pool.submit(current_deadline).result() is None


def test_bind_copied_deadline_restores_worker_and_does_not_share_context():
    """Each bind snapshots its own Context; reuse after return sees None."""
    from concurrent.futures import ThreadPoolExecutor
    from minni.request_deadline import bind_copied_deadline, run_bound

    deadline = time.monotonic() + 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        with request_deadline(deadline):
            first = bind_copied_deadline(current_deadline)
            second = bind_copied_deadline(current_deadline)
            assert first is not second
            assert pool.submit(run_bound, first).result() == deadline
            assert pool.submit(run_bound, second).result() == deadline
        assert pool.submit(current_deadline).result() is None
        assert current_deadline() is None


def _two_vault_context(engines):
    from minni.minnid_runtime.recall import RecallContext
    from minni.principal import EffectivePrincipal
    principal = EffectivePrincipal(
        agent_id="codex", workspace_id="default", capabilities=["recall"],
    )
    shared, vault_a, vault_b = engines
    return RecallContext(
        make_error=lambda code, message, rid: {"error": message},
        make_response=lambda result, rid: {"result": result},
        handler_principal=lambda params, rid: (principal, None),
        lazy_retrieval=lambda: shared,
        agent_vault_retrieval=lambda agent: (vault_a, "codex", "a.db"),
        all_vault_retrievals=lambda: [
            (vault_a, "codex", "a.db"),
            (vault_b, "cursor", "b.db"),
        ],
        trace_ring=lambda: None,
        record_latency=lambda *args: None,
        lazy_episodic=None,
        default_config=dataclasses.replace(DEFAULT_CONFIG, recall_trace=False),
        logger=logging.getLogger("deadline-fanout-test"),
    )


class _DeadlineProbeEngine:
    """Canned retrieve that records the worker's ContextVar deadline."""

    def __init__(self, name, sink, *, rows=None, on_retrieve=None):
        self.name = name
        self.sink = sink
        self.rows = rows or []
        self.on_retrieve = on_retrieve
        self.config = SimpleNamespace(embedding_model="test", recall_trace=False)
        self.last_auth_suppression = None
        self.last_vector_degraded = None
        self.last_rerank_degraded = None
        self.last_query_expand_degraded = None
        self.last_hyde_degraded = None
        self.last_trace_id = None
        self.EPISODIC_NON_MEMORY_TYPES = []
        self.db = SimpleNamespace(cursor=lambda: pytest.fail("unexpected qty db"))

    def retrieve(self, **kwargs):
        self.sink.append((self.name, current_deadline(), threading.get_ident()))
        if self.on_retrieve is not None:
            self.on_retrieve(self)
        return [dict(r) for r in self.rows]

    def search_learnings(self, *args, **kwargs):
        return []

    def search_episodic(self, *args, **kwargs):
        return []


def test_parallel_combined_legs_see_absolute_request_deadline(monkeypatch):
    """ThreadPoolExecutor legs must see the same absolute request deadline.

    ContextVar does not follow pool workers; without an independent
    copy_context snapshot per spawn, current_deadline() is None on the
    leg and SQLite stays on the unbounded legacy busy timeout.
    """
    import threading
    import minni.minnid_runtime.recall as recall_mod
    from minni.minnid_runtime.recall import handle_search

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    seen = []
    engines = (
        _DeadlineProbeEngine("shared", seen),
        _DeadlineProbeEngine("vault-a", seen),
        _DeadlineProbeEngine("vault-b", seen),
    )
    clock = {"now": 5000.0}
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])
    import minni.request_deadline as deadline_module
    monkeypatch.setattr(deadline_module.time, "monotonic", lambda: clock["now"])
    response = handle_search(
        {"query": "q", "scope": "combined", "timeout_ms": 10_000, "expand": False},
        1,
        _two_vault_context(engines),
    )
    assert "error" not in response, response
    deadlines = [item[1] for item in seen]
    assert len(deadlines) >= 2, seen
    assert None not in deadlines, seen
    assert len(set(deadlines)) == 1, seen
    parent = deadlines[0]
    assert parent == pytest.approx(5000.0 + 9.0, abs=0.01)
    assert current_deadline() is None


def test_spawned_leg_sql_lock_wait_is_bounded_and_resets(db, monkeypatch):
    """A pooled leg waiting on SQLite must use the request budget, then restore."""
    import threading
    import minni.minnid_runtime.recall as recall_mod
    from minni.minnid_runtime.recall import handle_search

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    conn = db._get_conn()
    conn.execute("PRAGMA busy_timeout=4321")
    other = sqlite3.connect(db.config.db_path)
    other.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    errors = []

    def on_retrieve(engine):
        if current_deadline() is None:
            errors.append(RuntimeError("deadline missing on worker"))
            return
        try:
            with db.cursor() as c:
                c.execute(
                    "INSERT INTO score_distribution (raw_score, kind, created_at) "
                    "VALUES (0.1, 'combined', 0)"
                )
        except Exception as exc:
            errors.append(exc)
            raise

    seen = []
    vault_a = _DeadlineProbeEngine("vault-a", seen, on_retrieve=on_retrieve)
    vault_b = _DeadlineProbeEngine("vault-b", seen)
    shared = _DeadlineProbeEngine("shared", seen)
    vault_a.db = db
    try:
        response = handle_search(
            {"query": "q", "scope": "combined", "timeout_ms": 100, "expand": False},
            1,
            _two_vault_context((shared, vault_a, vault_b)),
        )
    finally:
        other.rollback()
        other.close()
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, elapsed
    assert seen, "combined fan-out must run vault retrieve"
    assert all(item[1] is not None for item in seen), seen
    assert errors, "budgeted lock wait must expire"
    assert any(isinstance(exc, RequestDeadlineExceeded) for exc in errors)
    assert not conn.in_transaction
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
    with db.cursor() as c:
        c.execute("SELECT 1")
        assert c.fetchone()[0] == 1
    assert current_deadline() is None
    # handle_search may surface -32000 or a 200 with request degradation;
    # either is truthful. Native wait_for does not kill the worker thread.
    assert response is not None


def test_simultaneous_requests_do_not_leak_deadline_into_other_workers(monkeypatch):
    import threading
    import minni.minnid_runtime.recall as recall_mod
    from minni.minnid_runtime.recall import handle_search

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    buckets = {0: [], 1: []}

    def run(idx, timeout_ms, accepted):
        engines = (
            _DeadlineProbeEngine(f"s{idx}", buckets[idx]),
            _DeadlineProbeEngine(f"a{idx}", buckets[idx]),
            _DeadlineProbeEngine(f"b{idx}", buckets[idx]),
        )
        handle_search(
            {
                "query": "q",
                "scope": "combined",
                "timeout_ms": timeout_ms,
                "expand": False,
                "_accepted_monotonic": accepted,
            },
            idx,
            _two_vault_context(engines),
        )

    t0 = threading.Thread(target=run, args=(0, 10_000, time.monotonic()))
    t1 = threading.Thread(target=run, args=(1, 2_000, time.monotonic()))
    t0.start()
    t1.start()
    t0.join(5)
    t1.join(5)
    assert not t0.is_alive() and not t1.is_alive()
    d0 = {item[1] for item in buckets[0]}
    d1 = {item[1] for item in buckets[1]}
    assert None not in d0 and None not in d1
    assert len(d0) == 1 and len(d1) == 1
    assert d0 != d1
    assert current_deadline() is None


def test_deadline_free_legacy_pool_workers_see_none_and_keep_busy_timeout(db):
    """Ordinary calls outside a request retain configured SQLite waits.

    Connections are thread-local: a worker opens its own conn (default
    busy_timeout 30000). The submitting thread's PRAGMA is unchanged.
    """
    from concurrent.futures import ThreadPoolExecutor

    conn = db._get_conn()
    conn.execute("PRAGMA busy_timeout=4321")
    seen = []
    worker_busy = []

    def leg(_idx):
        seen.append(current_deadline())
        with db.cursor() as c:
            worker_busy.append(c.execute("PRAGMA busy_timeout").fetchone()[0])
            c.execute("SELECT 1")
        return "ok"

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="minni-leg") as pool:
        results = list(pool.map(leg, (0, 1)))
    assert results == ["ok", "ok"]
    assert seen == [None, None]
    assert worker_busy == [30000, 30000]
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
    assert current_deadline() is None

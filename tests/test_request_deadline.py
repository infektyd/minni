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


def test_expensive_sql_after_expiry_is_interrupted_and_connection_resets(db):
    """Remaining 0 must not run an unbounded recursive query, even on an open conn."""
    expensive = (
        'WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) '
        'SELECT sum(x) FROM n'
    )
    db._get_conn()
    started = time.monotonic()
    with pytest.raises(RequestDeadlineExceeded):
        with request_deadline(time.monotonic() - 1):
            with db.cursor() as c:
                c.execute(expensive)
                c.fetchone()
    assert time.monotonic() - started < 1.0
    assert not db._get_conn().in_transaction
    assert db._get_conn()._progress_callback is None
    with db.cursor() as c:
        assert c.execute('SELECT 1').fetchone()[0] == 1


def test_bookkeeping_scope_still_interrupts_expensive_sql(db):
    """allow_expired_sql is entry-only; the progress handler still bounds work."""
    from minni.request_deadline import allow_expired_sql
    expensive = (
        'WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) '
        'SELECT sum(x) FROM n'
    )
    db._get_conn()
    started = time.monotonic()
    with pytest.raises(RequestDeadlineExceeded):
        with request_deadline(time.monotonic() - 1):
            with allow_expired_sql():
                with db.cursor() as c:
                    c.execute(expensive)
                    c.fetchone()
    assert time.monotonic() - started < 2.0
    assert not db._get_conn().in_transaction
    with db.cursor() as c:
        assert c.execute('SELECT 1').fetchone()[0] == 1


def test_expired_commit_without_transaction_keeps_completed_read(db, monkeypatch):
    """Cursor-exit commit must not drop a finished SELECT after the budget expires."""
    import minni.request_deadline as deadline_module
    clock = {'now': 1000.0}
    monkeypatch.setattr(deadline_module.time, 'monotonic', lambda: clock['now'])
    db._get_conn()
    with request_deadline(1000.5):
        with db.cursor() as c:
            c.execute('SELECT 1 AS v')
            row = c.fetchone()
            clock['now'] = 1002.0
        assert row['v'] == 1


def test_completed_select_survives_real_sleep_after_fetch(db):
    """Proven read-only SELECT still succeeds when cursor-exit commit expires."""
    with request_deadline(time.monotonic() + 0.1):
        with db.cursor() as c:
            c.execute('SELECT 1 AS v')
            row = c.fetchone()
            time.sleep(0.12)
        assert row['v'] == 1
    assert not db._get_conn().in_transaction


def test_insert_returning_expired_cursor_does_not_false_succeed(db, monkeypatch):
    """INSERT ... RETURNING must not look successful after an expired rollback.

    SQLite leaves total_changes unchanged until a RETURNING statement is
    exhausted. fetchone() of the first row therefore looks like a read if
    that counter is the write heuristic, and cursor-exit rollback is
    swallowed while nothing persisted.
    """
    import minni.request_deadline as deadline_module
    with db.cursor() as c:
        c.execute('CREATE TABLE probe (v INTEGER)')
    changes_after_create = db._get_conn().total_changes
    clock = {'now': 1000.0}
    monkeypatch.setattr(deadline_module.time, 'monotonic', lambda: clock['now'])
    claimed = None
    with pytest.raises(RequestDeadlineExceeded):
        with request_deadline(1000.5):
            with db.cursor() as c:
                c.execute('INSERT INTO probe VALUES (1), (2) RETURNING v')
                claimed = tuple(c.fetchone())
                assert db._get_conn().total_changes == changes_after_create
                assert db._get_conn().in_transaction
                clock['now'] = 1002.0
    assert claimed == (1,)
    with db.cursor() as c:
        persisted = c.execute('SELECT COUNT(*) FROM probe').fetchone()[0]
    assert persisted == 0
    assert not db._get_conn().in_transaction


def test_first_eligibility_fetch_deadline_raises_instead_of_empty(tmp_path, monkeypatch):
    """A deadline on the first FTS fetch must not look like a quiet miss."""
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / 'elig.db'),
        vault_path=str(tmp_path / 'vault'),
        reranker_enabled=False,
        hyde_enabled=False,
        query_expand_default='off',
    )
    engine = RetrievalEngine(SovereignDB(cfg), cfg)

    def boom(*args, **kwargs):
        raise RequestDeadlineExceeded('first fetch')

    monkeypatch.setattr(engine, '_fts_search', boom)
    with pytest.raises(RequestDeadlineExceeded, match='first fetch'):
        engine.retrieve(
            'sockets',
            limit=5,
            budget_tokens=False,
            expand=False,
            use_hyde=False,
            deadline_monotonic=time.monotonic() + 30,
        )


def test_multi_backend_search_does_not_swallow_deadline():
    import numpy as np
    from minni.backends.multi import MultiBackend
    from minni.vector_backend import VectorHit

    class Boom:
        name = 'boom'
        dim = 8
        def search(self, *args, **kwargs):
            raise RequestDeadlineExceeded('copied context deadline')

    class Ok:
        name = 'ok'
        dim = 8
        def search(self, *args, **kwargs):
            return [VectorHit(chunk_id=1, doc_id=1, score=1.0, backend='ok')]

    multi = MultiBackend([Boom(), Ok()])
    with pytest.raises(RequestDeadlineExceeded, match='copied context deadline'):
        multi.search(np.zeros(8, dtype=np.float32), k=3)


def test_document_depth_hydration_timeout_preserves_chunk_and_reports_degradation(
    tmp_path, monkeypatch,
):
    """Document-depth _fetch_full_document timeout keeps the ranked chunk.

    FAISS disk-cache load is a different surface. This drives retrieve(depth=
    document) through the ranked-result hydration path.
    """
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.minnid_runtime.recall import _degradation_for
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / 'doc-hydrate.db'),
        faiss_index_path=str(tmp_path / 'doc-hydrate.faiss'),
        vault_path=str(tmp_path / 'vault'),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
        query_expand_default='off',
    )
    engine = RetrievalEngine(SovereignDB(cfg), cfg)
    engine.index_durable_document(
        content='# Deadline slice\n\nFTS must still find this paragraph about sockets.\n',
        path='wiki/concepts/deadline-slice.md',
        agent='claude-code',
        sigil='📄',
        privacy_level='safe',
        page_status='accepted',
        layer='knowledge',
    )

    def boom(_doc_id):
        raise RequestDeadlineExceeded('document hydrate timeout')

    monkeypatch.setattr(engine, '_fetch_full_document', boom)
    rows = engine.retrieve(
        'sockets',
        limit=5,
        budget_tokens=False,
        expand=False,
        use_hyde=False,
        depth='document',
        deadline_monotonic=time.monotonic() - 1.0,
    )
    assert rows, 'ranked chunk must survive document hydration timeout'
    row = rows[0]
    assert row.get('text'), 'chunk body must still ship'
    assert 'full_document_text' not in row
    assert row.get('depth') == 'chunk'
    assert row.get('requested_depth') == 'document'
    assert row.get('delivered_depth') == 'chunk'
    assert 'deadline' in str(row.get('document_hydration', '')).lower()
    prov = row.get('provenance') or {}
    assert prov.get('requested_depth') == 'document'
    assert prov.get('delivered_depth') == 'chunk'
    assert 'deadline' in str(prov.get('document_hydration', '')).lower()
    assert engine.last_document_hydration_degraded
    assert 'deadline' in str(engine.last_document_hydration_degraded).lower()
    assert 'skipped full document' in str(engine.last_document_hydration_degraded).lower()
    entry = _degradation_for(engine, 'c')
    assert entry.get('degraded') is True
    assert 'deadline' in str(entry.get('document_hydration_degraded', '')).lower()


def test_faiss_hydration_timeout_sets_vector_degraded(tmp_path, monkeypatch):
    """Disk-cache hydration timeout must degrade, not look like a healthy miss."""
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / 'hydrate.db'),
        vault_path=str(tmp_path / 'vault'),
        reranker_enabled=False,
        hyde_enabled=False,
    )
    engine = RetrievalEngine(SovereignDB(cfg), cfg)

    def boom(*args, **kwargs):
        raise RequestDeadlineExceeded('hydrate timeout')

    monkeypatch.setattr(engine.db, '_get_conn', boom)
    engine._ensure_faiss_loaded()
    assert engine.last_vector_degraded
    assert 'deadline' in str(engine.last_vector_degraded).lower()


def test_faiss_disk_postfilter_does_not_swallow_deadline():
    from minni.backends.faiss_disk import FaissDiskBackend

    backend = object.__new__(FaissDiskBackend)
    backend.name = 'faiss-disk'

    class _Boom:
        def cursor(self):
            raise RequestDeadlineExceeded('post-filter deadline')

    backend.db = _Boom()

    class _Index:
        count = 1

        def search(self, query, top_k=5):
            return [(1, 0.9)]

    backend._faiss = _Index()
    backend.dim = 8
    import numpy as np
    with pytest.raises(RequestDeadlineExceeded, match='post-filter deadline'):
        backend.search(np.zeros(8, dtype=np.float32), k=1)


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
    """New tails skip after expiry; completed hybrid still attempts calibration.

    search_learnings / episodic are new queries and must not run. Document
    access and score calibration for a non-poisoned ranking still attempt.
    """
    from contextlib import contextmanager
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
    @contextmanager
    def noop_cursor():
        class Cursor:
            def execute(self, *args, **kwargs):
                return self
            def fetchone(self):
                return None
        yield Cursor()
    engine = SimpleNamespace(
        retrieve=retrieve, search_learnings=forbidden,
        search_episodic=forbidden, db=SimpleNamespace(cursor=noop_cursor),
        config=DEFAULT_CONFIG,
    )
    response = handle_search({'query': 'known', 'timeout_ms': 1000, 'layers': ['episodic']}, 1, _context(engine))
    assert 'error' not in response, response
    assert response['result']['results'][0]['doc_id'] == 1
    assert 'confidence_raw' not in response['result']['results'][0]
    assert response['result']['degraded'] is True
    skipped = {d.get('stage') for d in response['result']['degradation']}
    assert {'learnings', 'episodic'} <= skipped
    assert 'document_access' not in skipped
    assert 'score_calibration' not in skipped
    assert calls == []
    assert current_deadline() is None


def test_deadline_fallback_strips_private_result_keys(monkeypatch):
    """Outer RequestDeadlineExceeded payload must not leak private carriers."""
    from contextlib import contextmanager
    import minni.minnid_runtime.recall as recall_mod
    import minni.request_deadline as deadline_module
    from minni.minnid_runtime.recall import handle_search
    clock = {'now': 1000.0}
    monkeypatch.setattr(deadline_module.time, 'monotonic', lambda: clock['now'])
    sentinel = object()
    def retrieve(*args, **kwargs):
        clock['now'] = 1002.0
        return [{
            'doc_id': 1, 'path': 'a.md', 'score': .9, 'confidence_raw': .9,
            recall_mod._QTY_ENGINE_KEY: sentinel,
            recall_mod._DEADLINE_POISONED_KEY: False,
        }]
    real_strip = recall_mod._strip_private_search_keys
    calls = {'n': 0}
    def raise_before_first_strip(rows):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RequestDeadlineExceeded('before strip')
        real_strip(rows)
    monkeypatch.setattr(recall_mod, '_strip_private_search_keys', raise_before_first_strip)
    @contextmanager
    def noop_cursor():
        class Cursor:
            def execute(self, *args, **kwargs):
                return self
            def fetchone(self):
                return None
        yield Cursor()
    engine = SimpleNamespace(
        retrieve=retrieve, search_learnings=lambda *a, **k: [],
        search_episodic=lambda *a, **k: [], db=SimpleNamespace(cursor=noop_cursor),
        config=DEFAULT_CONFIG,
    )
    response = handle_search({'query': 'known', 'timeout_ms': 1000}, 1, _context(engine))
    assert 'error' not in response, response
    row = response['result']['results'][0]
    assert row['doc_id'] == 1
    assert 'confidence_raw' not in row
    assert recall_mod._QTY_ENGINE_KEY not in row
    assert recall_mod._DEADLINE_POISONED_KEY not in row
    assert response['result']['degraded'] is True


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


def _bare_encode_engine():
    from minni.retrieval import RetrievalEngine
    engine = object.__new__(RetrievalEngine)
    engine.vector_model_down = False
    return engine


def test_request_deadline_reuses_query_embedding_across_engines(monkeypatch):
    """Serial corpus legs under one RPC deadline encode a query once."""
    import numpy as np
    import minni.models as models
    from minni.request_deadline import current_query_embed_cache

    class Embedder:
        calls = []

        def encode(self, query, **kwargs):
            self.calls.append(query)
            return np.array([1.0, 2.0], dtype=np.float32)

    model = Embedder()
    monkeypatch.setattr(models, "get_embedder", lambda: model)
    first = _bare_encode_engine()
    second = _bare_encode_engine()
    deadline = time.monotonic() + 30
    with request_deadline(deadline):
        assert current_query_embed_cache() is not None
        left = first._encode_query("same query", deadline_monotonic=deadline)
        left[0] = 99
        right = second._encode_query("same query", deadline_monotonic=deadline)
        other = first._encode_query("other query", deadline_monotonic=deadline)
        assert list(right) == [1.0, 2.0]
    assert model.calls == ["same query", "other query"]
    assert list(other) == [1.0, 2.0]
    with request_deadline(deadline):
        again = second._encode_query("same query", deadline_monotonic=deadline)
    assert again[0] == 1.0
    assert model.calls == ["same query", "other query", "same query"]


def test_query_embedding_memo_does_not_survive_deadline_or_scope_exit(monkeypatch):
    """Cached vectors must not bypass an expired retrieve deadline."""
    import numpy as np
    import minni.models as models

    class Embedder:
        calls = []

        def encode(self, query, **kwargs):
            self.calls.append(query)
            return np.array([3.0], dtype=np.float32)

    model = Embedder()
    monkeypatch.setattr(models, "get_embedder", lambda: model)
    engine = _bare_encode_engine()
    live = time.monotonic() + 30
    with request_deadline(live):
        filled = engine._encode_query("q", deadline_monotonic=live)
        assert list(filled) == [3.0]
        empty = engine._encode_query("q", deadline_monotonic=time.monotonic() - 1)
        assert empty.size == 0
    assert model.calls == ["q"]
    engine._encode_query("q")
    assert model.calls == ["q", "q"]


def test_nested_request_deadline_reuses_outer_query_embedding_memo(monkeypatch):
    import numpy as np
    import minni.models as models

    class Embedder:
        calls = []

        def encode(self, query, **kwargs):
            self.calls.append(query)
            return np.array([4.0], dtype=np.float32)

    model = Embedder()
    monkeypatch.setattr(models, "get_embedder", lambda: model)
    outer = _bare_encode_engine()
    inner = _bare_encode_engine()
    with request_deadline(time.monotonic() + 30):
        outer._encode_query("q")
        with request_deadline(time.monotonic() + 10):
            inner._encode_query("q")
    assert model.calls == ["q"]


def test_failed_encode_time_is_recorded(monkeypatch):
    import minni.models as models
    import minni.retrieval as retrieval

    class FailingEmbedder:
        def encode(self, *args, **kwargs):
            raise RuntimeError("encoder failed after work")

    monkeypatch.setattr(models, "get_embedder", lambda: FailingEmbedder())
    ticks = iter([10.0, 10.025])
    engine = _bare_encode_engine()
    with monkeypatch.context() as patch:
        patch.setattr(retrieval.time, "perf_counter", lambda: next(ticks))
        result = engine._encode_query("failed specimen")
    assert result.size == 0
    assert engine._take_encode_ms() == pytest.approx(25.0)

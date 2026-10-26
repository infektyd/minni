"""R7 retrieval-integrity tests — issue #225 plus the folded gap-audit findings."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path, **cfg_overrides):
    """(MinniDB, MinniConfig) over a temporary SQLite file."""
    import minni.db as db_mod
    from minni.config import MinniConfig

    cfg = MinniConfig(db_path=str(tmp_path / "test.db"), **cfg_overrides)
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.MinniDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _make_engine(tmp_path, **cfg_overrides):
    """RetrievalEngine against a fresh test DB, no FAISS/model loading."""
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path, **cfg_overrides)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
    return engine, db_obj, cfg


class _RecordedTask:
    """Stands in for asyncio.Task: remembers the code object it was created"""

    def __init__(self, coro, loop):
        self.code = coro.cr_code
        self.name = coro.cr_code.co_name
        coro.close()
        self._loop = loop
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        self._loop.events.append(("cancel", self.name))


class _StubLoop:
    """Records create_task calls instead of running them."""

    def __init__(self):
        self.tasks = []
        self.events = []

    def set_default_executor(self, executor):
        executor.shutdown(wait=False)

    def create_task(self, coro):
        task = _RecordedTask(coro, self)
        self.tasks.append(task)
        self.events.append(("create", task.name))
        return task

    def mark(self):
        """Index into the event log, for asserting what happened after it."""
        return len(self.events)

    def cancels_since(self, mark):
        return [name for kind, name in self.events[mark:] if kind == "cancel"]

    def run_until_complete(self, task):
        self.events.append(("run", getattr(task, "name", None)))
        return None

    def close(self):
        pass

    def task_for(self, func):
        """The recorded task created from *func*, or None."""
        for task in self.tasks:
            if task.code is func.__code__:
                return task
        return None


def _run_main_with_stub_loop(monkeypatch, tmp_path):
    """Run minnid.main() against a stub loop. Returns (minnid, loop, handlers)."""
    import asyncio as _asyncio
    import signal as _signal

    import minni.minnid as minnid
    import minni.minnid_runtime.deploy_honesty as deploy_honesty
    from minni.db import MinniDB

    loop = _StubLoop()
    handlers = {}

    monkeypatch.setattr(
        sys, "argv", ["minnid", "--socket", str(tmp_path / "minnid.sock")]
    )
    monkeypatch.setattr(_asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(_signal, "signal", lambda num, fn: handlers.__setitem__(num, fn))
    monkeypatch.setattr(minnid, "_raise_fd_ceiling", lambda *a, **k: 0)
    monkeypatch.setattr(minnid.obs, "configure_logging", lambda **k: None)
    monkeypatch.setattr(deploy_honesty, "capture_start_state", lambda: None)

    loop.live_db_attempts = []

    def _no_live_db(*args, **kwargs):
        loop.live_db_attempts.append(True)
        raise RuntimeError("test: refusing to open the live database")

    monkeypatch.setattr(MinniDB, "shared", staticmethod(_no_live_db))
    if "MINNI_MODEL_DEVICE" not in os.environ:
        monkeypatch.setenv("MINNI_MODEL_DEVICE", "")
        monkeypatch.delenv("MINNI_MODEL_DEVICE", raising=False)

    minnid.main()
    return minnid, loop, handlers


@pytest.fixture
def hermetic_principals(tmp_path, monkeypatch):
    import json

    import minni.minnid as minnid
    import minni.principal as principal

    pdir = tmp_path / "principals"
    pdir.mkdir(exist_ok=True)
    original_resolve = principal.resolve_effective_principal

    def _patched_resolve(*, supplied_agent_id=None, transport="uds",
                         principals_dir=None, operator_context=False):
        target_dir = principals_dir or pdir
        target_agent = str(supplied_agent_id or "").strip()
        if target_agent:
            fname, file_agent = f"{target_agent}.json", target_agent
        else:
            fname, file_agent = "local.json", "main"
        f = target_dir / fname
        f.write_text(json.dumps({
            "agent_id": file_agent, "workspace_id": "default",
            "capabilities": ["*"],
        }), encoding="utf-8")
        os.chmod(f, 0o600)
        op_ctx = operator_context or (
            target_agent in principal.OPERATOR_RESERVED_AGENT_IDS
        )
        return original_resolve(
            supplied_agent_id=supplied_agent_id, transport=transport,
            principals_dir=target_dir, operator_context=op_ctx,
        )

    monkeypatch.setattr(principal, "resolve_effective_principal", _patched_resolve)
    monkeypatch.setattr(minnid, "resolve_effective_principal", _patched_resolve)


def _patch_engine_and_writeback(tmp_path, monkeypatch):
    """Wire ONE test DB into both minnid singletons so dispatch tests hit the"""
    import minni.minnid as minnid
    import minni.writeback as wb_mod
    from minni.retrieval import RetrievalEngine
    from minni.writeback import WriteBackMemory

    from functools import cache
    import minni.models as models

    @cache
    def unavailable_embedder():
        return None

    unavailable_embedder()
    monkeypatch.setattr(models, "get_embedder", unavailable_embedder)
    db_obj, cfg = _make_db(tmp_path, reranker_enabled=False, hyde_enabled=False)
    wb = WriteBackMemory(db_obj, cfg)
    monkeypatch.setattr(minnid, "_writeback", wb)
    monkeypatch.setattr(wb_mod.WriteBackMemory, "model", property(lambda self: None))
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
    monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: None))
    monkeypatch.setattr(minnid, "_retrieval", engine)
    return engine, db_obj, cfg


def _dispatch(method, params):
    from minni.minnid import _dispatch_sync

    return _dispatch_sync(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )


def _add_event(db_obj, content, event_type="observation", agent="codex"):
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO episodic_events (agent_id, event_type, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (agent, event_type, content, time.time()),
        )
        return c.lastrowid


class TestEpisodicIsReachable:
    """search_episodic had zero production call sites while the `layer` enum"""

    def test_search_rpc_returns_episodic_hits(self, tmp_path, monkeypatch,
                                              hermetic_principals):
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        _add_event(db_obj, "deployment rollback completed on the edge fleet")

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        episodic = resp["result"].get("episodic")
        assert episodic, (
            "the search RPC must surface episodic events; on the old code the "
            "response had no episodic channel at all and the layer was dead"
        )
        assert "rollback" in episodic[0]["content"]
        assert resp["result"]["episodic_count"] == len(episodic)

    def test_explicit_episodic_layer_is_answered(self, tmp_path, monkeypatch,
                                                 hermetic_principals):
        """layers=['episodic'] is the exact request the enum advertises."""
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        _add_event(db_obj, "cache invalidation bug traced to a stale manifest")

        resp = _dispatch("search", {
            "query": "cache invalidation", "agent_id": "codex",
            "layers": ["episodic"], "expand": False,
        })
        assert "error" not in resp
        assert resp["result"]["episodic"], "layers=['episodic'] must not be empty"

    def test_non_episodic_layer_filter_skips_the_channel(self, tmp_path, monkeypatch,
                                                         hermetic_principals):
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        _add_event(db_obj, "deployment rollback completed")

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "layers": ["knowledge"], "expand": False,
        })
        assert "error" not in resp
        assert resp["result"]["episodic"] == [], (
            "an explicit non-episodic filter must not leak episodic hits"
        )

    def test_recall_traces_are_not_surfaced_as_memory(self, tmp_path, monkeypatch,
                                                      hermetic_principals):
        """The daemon writes a TTL'd `recall` event per search for observability."""
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        _add_event(db_obj, "deployment rollback trace", event_type="recall")
        kept = _add_event(db_obj, "deployment rollback postmortem",
                          event_type="observation")

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        ids = [e["event_id"] for e in resp["result"]["episodic"]]
        assert ids == [kept]

    def test_direct_callers_keep_every_event_type(self, tmp_path):
        """exclude_event_types defaults to None — no change for direct callers."""
        engine, db_obj, _cfg = _make_engine(tmp_path)
        _add_event(db_obj, "deployment rollback trace", event_type="recall")
        results = engine.search_episodic("deployment rollback", agent_id="codex")
        assert len(results) == 1

    def test_layer_scope_helper(self):
        from minni.minnid_runtime.recall import _episodic_layer_requested

        assert _episodic_layer_requested(None) is True
        assert _episodic_layer_requested(["episodic"]) is True
        assert _episodic_layer_requested(["knowledge", "EPISODIC"]) is True
        assert _episodic_layer_requested("episodic") is True
        assert _episodic_layer_requested(["knowledge"]) is False
        assert _episodic_layer_requested([]) is False


class TestScoreCalibrationIsWired:
    """record_score had zero production call sites, so score_distribution held"""

    def test_compute_confidence_records_when_asked(self, tmp_path):
        from minni.scoring import compute_confidence

        db_obj, _cfg = _make_db(tmp_path)
        compute_confidence(0.02, 1.0, 1.0, db=db_obj, record=True)

        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM score_distribution"
            ).fetchone()["n"]
        assert n == 1, (
            "the retrieval path must feed the calibration window; on the old "
            "code score_distribution stayed empty forever"
        )

    def test_recording_is_opt_in(self, tmp_path):
        """Speculative paths (the HyDE probe) must not inflate the window with"""
        from minni.scoring import compute_confidence

        db_obj, _cfg = _make_db(tmp_path)
        compute_confidence(0.02, 1.0, 1.0, db=db_obj)

        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM score_distribution"
            ).fetchone()["n"]
        assert n == 0

    def test_recorded_value_is_pre_calibration(self, tmp_path):
        """Recording the calibrated output would feed the distribution its own"""
        from minni.scoring import compute_confidence

        db_obj, _cfg = _make_db(tmp_path)
        for _ in range(20):
            compute_confidence(0.02, 1.0, 1.0, db=db_obj, record=True)

        returned = compute_confidence(0.02, 1.0, 1.0, db=db_obj, record=True)
        with db_obj.cursor() as c:
            last = c.execute(
                "SELECT raw_score FROM score_distribution ORDER BY id DESC LIMIT 1"
            ).fetchone()["raw_score"]
        assert abs(last - returned) > 1e-9, (
            "the stored sample must be the raw blend, not the calibrated output"
        )

    def test_retrieval_actually_records_through_the_public_path(self, tmp_path,
                                                                monkeypatch,
                                                                hermetic_principals):
        """Behavioral, not a source grep: drive the real search RPC and assert"""
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        assert resp["result"]["count"] >= 1, "need a hit for a score to exist"

        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM score_distribution"
            ).fetchone()["n"]
        assert n >= 1, (
            "a real search must feed the calibration window; on the old code "
            "score_distribution stayed empty forever"
        )


class TestCalibrationActivationIsObservable:
    """GA4-1 guardrail. Wiring record_score means the window fills during normal"""

    def _fill(self, db_obj, n):
        from minni.scoring import record_score

        for i in range(n):
            record_score(0.01 * (i + 1), "combined", db_obj)

    def test_pre_activation_confidence_is_the_raw_blend(self, tmp_path):
        """Below the threshold _calibrate must pass the raw score through."""
        from minni.scoring import _ACTIVATION_THRESHOLD, calibration_status, compute_confidence

        db_obj, _cfg = _make_db(tmp_path)
        self._fill(db_obj, _ACTIVATION_THRESHOLD - 1)

        status = calibration_status(db_obj)
        assert status["active"] is False
        assert status["confidence_basis"] == "raw_blend"
        assert status["samples_until_active"] == 1

        with_db = compute_confidence(0.02, 1.0, 1.0, db=db_obj)
        without_db = compute_confidence(0.02, 1.0, 1.0, db=None)
        assert abs(with_db - without_db) < 1e-9, (
            "below the threshold, calibration must be a no-op"
        )

    def test_post_activation_confidence_is_a_percentile(self, tmp_path):
        """At/above the threshold the SAME inputs yield a different number —"""
        from minni.scoring import _ACTIVATION_THRESHOLD, calibration_status, compute_confidence

        db_obj, _cfg = _make_db(tmp_path)
        raw_only = compute_confidence(0.02, 1.0, 1.0, db=None)

        self._fill(db_obj, _ACTIVATION_THRESHOLD)
        status = calibration_status(db_obj)
        assert status["active"] is True
        assert status["confidence_basis"] == "percentile_rank"
        assert status["samples_until_active"] == 0

        calibrated = compute_confidence(0.02, 1.0, 1.0, db=db_obj)
        assert abs(calibrated - raw_only) > 1e-9, (
            "crossing the threshold must actually change the number — if it "
            "did not, the surface would be announcing a no-op"
        )

    def test_the_surface_flips_exactly_at_the_threshold(self, tmp_path):
        """The reported boundary must be the one _calibrate actually uses; a"""
        from minni.scoring import _ACTIVATION_THRESHOLD, calibration_status

        db_obj, _cfg = _make_db(tmp_path)
        self._fill(db_obj, _ACTIVATION_THRESHOLD - 1)
        assert calibration_status(db_obj)["active"] is False
        self._fill(db_obj, 1)
        assert calibration_status(db_obj)["active"] is True

    def test_health_report_carries_the_calibration_surface(self):
        import inspect

        import minni.minnid_runtime.health as health

        src = inspect.getsource(health.handle_health_report)
        assert "score_calibration" in src
        assert "calibration_status" in src

    def test_calibration_surface_is_not_redacted_away(self):
        """Counts and labels only — it must stay readable pre-identity, like"""
        from minni.minnid_runtime.health import (
            _HEALTH_REPORT_SENSITIVE_KEYS,
            redact_health_report_for_recovery,
        )

        assert "score_calibration" not in _HEALTH_REPORT_SENSITIVE_KEYS
        report = {"score_calibration": {"active": True, "window_rows": 42}}
        assert redact_health_report_for_recovery(report)["score_calibration"] == {
            "active": True, "window_rows": 42,
        }


class _StubEmbedder:
    """Deterministic encoder — the backfill only needs a vector, not a good one."""

    def encode(self, text, **kwargs):
        import numpy as np

        vec = np.zeros(384, dtype="float32")
        vec[sum(str(text).encode("utf-8")) % 384] = 1.0
        return vec


class _ConstantStubReranker:
    def __init__(self, score: float = 1.0):
        self._score = float(score)

    def predict(self, pairs, **kwargs):
        return [self._score] * len(pairs)


class TestEmbeddingBackfillAndCoverage:
    def _stub_encoder(self, monkeypatch):
        import minni.models as models

        monkeypatch.setattr(models, "get_embedder", lambda: _StubEmbedder())

    def test_coverage_reports_the_document_vector_ratio(self, tmp_path):
        from minni.backfill import embedding_coverage

        db_obj, _cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            for i in range(4):
                c.execute(
                    "INSERT INTO documents (path, agent, sigil) VALUES (?, 'a', 'v')",
                    (f"d{i}.md",),
                )
            c.execute(
                """INSERT INTO chunk_embeddings
                   (doc_id, chunk_index, chunk_text, embedding)
                   VALUES (1, 0, 'x', X'00')"""
            )

        cov = embedding_coverage(db_obj)
        assert cov["documents_total"] == 4
        assert cov["documents_with_vectors"] == 1
        assert cov["documents_missing_vectors"] == 3
        assert abs(cov["documents_vector_ratio"] - 0.25) < 1e-9

    def test_empty_index_reports_no_ratio_rather_than_perfect(self, tmp_path):
        """Claiming 100% coverage for zero documents is exactly the"""
        from minni.backfill import embedding_coverage

        db_obj, _cfg = _make_db(tmp_path)
        cov = embedding_coverage(db_obj)
        assert cov["documents_vector_ratio"] is None
        assert cov["learnings_embedding_ratio"] is None

    def test_learning_embedding_backfill_fills_nulls(self, tmp_path, monkeypatch):
        from minni.backfill import backfill_learning_embeddings

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence,
                                          created_at, embedding)
                   VALUES ('codex', 'fix', 'websocket backoff is 500ms', 1.0, ?, NULL)""",
                (time.time(),),
            )
            lid = c.lastrowid

        stats = backfill_learning_embeddings(db_obj, cfg)
        assert stats["embedded"] == 1

        with db_obj.cursor() as c:
            emb = c.execute(
                "SELECT embedding FROM learnings WHERE learning_id = ?", (lid,)
            ).fetchone()["embedding"]
        assert emb is not None, (
            "promote_candidate_durable stores embedding=NULL on encode failure "
            "and search hard-filters on IS NOT NULL — with no backfill those "
            "learnings were permanently unreachable"
        )

    def test_document_vector_backfill_creates_chunks(self, tmp_path, monkeypatch):
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        body = "# Title\n\n" + ("the deployment rollback procedure. " * 40)
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('a.md', 'codex', 'vault', 'artifact')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        stats = backfill_document_vectors(db_obj, cfg)
        assert stats["documents"] == 1
        assert stats["chunks"] >= 1

        with db_obj.cursor() as c:
            layers = {
                r["layer"] for r in c.execute(
                    "SELECT layer FROM chunk_embeddings WHERE doc_id = ?", (doc_id,)
                ).fetchall()
            }
        assert layers == {"artifact"}, (
            "backfilled chunks must inherit the document's layer, not default "
            "to knowledge — that is the GA6-1 bug in a new place"
        )

    def test_backfill_leaves_a_document_with_no_indexed_text_visible(
        self, tmp_path, monkeypatch
    ):
        """Never fix a signal by suppressing it: an unbackfillable document is"""
        from minni.backfill import backfill_document_vectors, embedding_coverage

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('empty.md', 'codex', 'vault')"
            )

        stats = backfill_document_vectors(db_obj, cfg)
        assert stats["documents"] == 0
        assert stats["unrecoverable"] == 1
        assert embedding_coverage(db_obj)["documents_missing_vectors"] == 1, (
            "excluding it from the batch must not hide it from coverage"
        )

    def test_backfill_skips_draft_and_expired_per_unembedded_statuses(
        self, tmp_path, monkeypatch
    ):
        """grok-review tip finding 1: draft/expired must stay vectorless."""
        from minni.backfill import backfill_document_vectors, embedding_coverage
        from minni.indexer import UNEMBEDDED_STATUSES

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        body = "# Note\n\n" + ("accepted rollback procedure text. " * 40)
        draft_body = "# Draft\n\n" + ("draft only content not for faiss. " * 40)
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, page_status) "
                "VALUES ('ok.md', 'codex', 'vault', 'accepted')"
            )
            accepted_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'ok.md', ?, 'codex', 'vault')",
                (accepted_id, body),
            )
            c.execute(
                "INSERT INTO documents (path, agent, sigil, page_status) "
                "VALUES ('draft.md', 'codex', 'vault', 'draft')"
            )
            draft_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'draft.md', ?, 'codex', 'vault')",
                (draft_id, draft_body),
            )
            c.execute(
                "INSERT INTO documents (path, agent, sigil, page_status) "
                "VALUES ('expired.md', 'codex', 'vault', 'expired')"
            )
            expired_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'expired.md', ?, 'codex', 'vault')",
                (expired_id, draft_body),
            )

        assert "draft" in UNEMBEDDED_STATUSES and "expired" in UNEMBEDDED_STATUSES
        stats = backfill_document_vectors(db_obj, cfg)
        assert stats["documents"] == 1, (
            f"only the accepted/embed-eligible doc should drain; got {stats}"
        )

        with db_obj.cursor() as c:
            n_accepted = c.execute(
                "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id = ?",
                (accepted_id,),
            ).fetchone()["n"]
            n_draft = c.execute(
                "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id = ?",
                (draft_id,),
            ).fetchone()["n"]
            n_expired = c.execute(
                "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id = ?",
                (expired_id,),
            ).fetchone()["n"]
        assert n_accepted >= 1
        assert n_draft == 0
        assert n_expired == 0

        cov = embedding_coverage(db_obj)
        assert cov["documents_total"] == 1, (
            "draft/expired must not count in embed-eligible totals"
        )
        assert cov["documents_missing_vectors"] == 0
        assert cov["documents_deliberately_unembedded"] == 2
        assert abs(cov["documents_vector_ratio"] - 1.0) < 1e-9

    def test_backfill_without_an_encoder_reports_rather_than_claiming_success(
        self, tmp_path, monkeypatch
    ):
        import minni.models as models
        from minni.backfill import backfill_learning_embeddings

        monkeypatch.setattr(models, "get_embedder", lambda: None)
        db_obj, cfg = _make_db(tmp_path)
        stats = backfill_learning_embeddings(db_obj, cfg)
        assert stats["skipped_no_model"] == 1
        assert stats["embedded"] == 0

    def test_backfilled_chunks_reach_a_warm_live_index(self, tmp_path, monkeypatch):
        """grok-review finding 1: committing chunk_embeddings rows is not enough."""
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        body = "# Title\n\n" + ("the deployment rollback procedure. " * 40)
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('a.md', 'codex', 'vault')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        pushed: list = []
        backfill_document_vectors(
            db_obj, cfg,
            on_vectors=lambda ids, vecs: pushed.append((ids, vecs)),
        )
        assert pushed, "the live index must be offered the backfilled vectors"
        ids, vecs = pushed[0]
        assert len(ids) == len(vecs) and len(ids) >= 1

        with db_obj.cursor() as c:
            stored = [
                r["chunk_id"] for r in c.execute(
                    "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ? "
                    "ORDER BY chunk_index", (doc_id,)
                ).fetchall()
            ]
        assert ids == stored, (
            "handing the live index anything but the stored chunk_ids would "
            "corrupt the id mapping"
        )

    def test_warm_faiss_index_gains_backfilled_chunks_without_restart(
        self, tmp_path, monkeypatch
    ):
        """The end-to-end form of grok-review finding 1: a REAL warm FAISSIndex"""
        import numpy as np

        from minni.faiss_index import FAISSIndex
        from minni.backfill import backfill_document_vectors
        from minni.retrieval import RetrievalEngine

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)

        faiss_index = FAISSIndex(cfg)
        seed = np.zeros((1, cfg.embedding_dim), dtype="float32")
        seed[0][0] = 1.0
        faiss_index.build_from_vectors([999], seed)
        engine = RetrievalEngine(db_obj, cfg, faiss_index=faiss_index)
        assert faiss_index.count == 1

        body = "# Title\n\n" + ("the deployment rollback procedure. " * 40)
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('a.md', 'codex', 'vault')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        stats = backfill_document_vectors(
            db_obj, cfg, on_vectors=engine._refresh_live_faiss
        )
        assert stats["chunks"] >= 1
        assert faiss_index.count == 1 + stats["chunks"], (
            "the warm live index must gain the backfilled chunks; on the first "
            "cut coverage climbed while the documents stayed invisible to the "
            "semantic leg until a restart"
        )

    def test_live_refresh_failure_does_not_lose_the_backfill(
        self, tmp_path, monkeypatch
    ):
        """The rows are durably committed; only immediacy is at stake."""
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        body = "# Title\n\n" + ("the deployment rollback procedure. " * 40)
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('a.md', 'codex', 'vault')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        def _explode(_ids, _vecs):
            raise RuntimeError("live index unavailable")

        stats = backfill_document_vectors(db_obj, cfg, on_vectors=_explode)
        assert stats["documents"] == 1
        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()["n"]
        assert n >= 1, "a failed live refresh must not cost the durable rows"

    def test_unrecoverable_documents_cannot_wedge_the_drain(
        self, tmp_path, monkeypatch
    ):
        """grok-review finding 2: taking the LIMIT head unfiltered let >=limit"""
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        body = "# Title\n\n" + ("the deployment rollback procedure. " * 40)
        with db_obj.cursor() as c:
            for i in range(3):
                c.execute(
                    "INSERT INTO documents (path, agent, sigil) VALUES (?, 'c', 'v')",
                    (f"empty{i}.md",),
                )
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('good.md', 'codex', 'vault')"
            )
            good_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'good.md', ?, 'codex', 'vault')",
                (good_id, body),
            )

        stats = backfill_document_vectors(db_obj, cfg, limit=2)
        assert stats["documents"] == 1, (
            "the recoverable document must be reachable despite a backlog of "
            "unrecoverable rows ahead of it"
        )
        assert stats["unrecoverable"] == 3, (
            "unrecoverable rows must stay COUNTED, not silently dropped"
        )

    def test_unrecoverable_learnings_cannot_wedge_the_drain(
        self, tmp_path, monkeypatch
    ):
        from minni.backfill import backfill_learning_embeddings

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        now = time.time()
        with db_obj.cursor() as c:
            for i in range(3):
                c.execute(
                    """INSERT INTO learnings (agent_id, category, content,
                                              confidence, created_at, embedding)
                       VALUES ('codex', 'fix', '', 1.0, ?, NULL)""",
                    (now,),
                )
            c.execute(
                """INSERT INTO learnings (agent_id, category, content,
                                          confidence, created_at, embedding)
                   VALUES ('codex', 'fix', 'websocket backoff is 500ms', 1.0, ?, NULL)""",
                (now,),
            )

        stats = backfill_learning_embeddings(db_obj, cfg, limit=2)
        assert stats["embedded"] == 1
        assert stats["unrecoverable"] == 3

    def test_document_encode_happens_outside_the_write_transaction(self):
        """grok-review finding 3: encoding inside db.transaction() held"""
        import inspect

        from minni.backfill import backfill_document_vectors

        src = inspect.getsource(backfill_document_vectors)
        encode_pos = src.index("model.encode")
        txn_pos = src.index("with db.transaction()")
        assert encode_pos < txn_pos, (
            "all chunk vectors must be encoded before the write transaction opens"
        )

    def test_backfill_covers_every_index_like_decay_does(self, tmp_path, monkeypatch):
        """grok-review finding 5: draining only the shared index while decay"""
        import minni.backfill as backfill_mod
        import minni.index_all as index_all
        import minni.vault_index as vault_index

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        db_obj.close()

        broken = tmp_path / "broken-vault"
        broken.mkdir()
        monkeypatch.setattr(
            index_all, "discover_agent_vaults", lambda home=None: [broken]
        )

        def _explode(*_a, **_kw):
            raise RuntimeError("vault index unreadable")

        monkeypatch.setattr(vault_index, "build_vault_index_config", _explode)

        results = backfill_mod.run_backfill_all_indexes(cfg)
        assert "error" not in results["shared"], (
            "a failing vault must not cost the shared index its pass"
        )
        assert "error" in results["broken-vault"]

    def test_migration_repairs_null_layer_identity_envelopes(self, tmp_path):
        """grok-review finding 4: seed_identity skips already-seeded agents, so"""
        from minni.migrations import run_migrations

        db_obj, _cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO documents (path, agent, sigil, whole_document, layer)
                   VALUES ('SOUL.md', 'identity:codex', 'C', 1, NULL)"""
            )
            envelope = c.lastrowid
            c.execute(
                """INSERT INTO documents (path, agent, sigil, whole_document, layer)
                   VALUES ('chunked.md', 'identity:codex', 'C', 0, 'knowledge')"""
            )
            not_whole = c.lastrowid

        conn = db_obj._get_conn()
        conn.execute("DELETE FROM schema_migrations WHERE version = 17")
        conn.commit()
        run_migrations(conn)
        conn.commit()

        with db_obj.cursor() as c:
            rows = {
                r["doc_id"]: r["layer"]
                for r in c.execute("SELECT doc_id, layer FROM documents").fetchall()
            }
        assert rows[envelope] == "identity"
        assert rows[not_whole] == "knowledge"
        db_obj.close()

    def test_daemon_schedules_the_backfill(self, monkeypatch, tmp_path):
        """main() must hand _backfill_runner to create_task, and cancel it on"""
        import signal

        monkeypatch.setenv("MINNI_BACKFILL", "on")
        minnid, loop, handlers = _run_main_with_stub_loop(monkeypatch, tmp_path)

        task = loop.task_for(minnid._backfill_runner)
        assert task is not None, (
            "the degraded status was logged but no retry was queued — a log "
            f"line is not a queue (scheduled: {[t.name for t in loop.tasks]})"
        )
        assert not task.cancelled, (
            "main() returned with the backfill task already cancelled — a task "
            "queued and then killed on the next line drains nothing"
        )

        mark = loop.mark()
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert task.cancelled, "shutdown must cancel the backfill task"
        assert "_backfill_runner" in loop.cancels_since(mark), (
            "the cancel must be caused by the shutdown handler, not by main()"
        )

    def test_backfill_is_not_scheduled_when_disabled(self, monkeypatch, tmp_path):
        """The env gate is real, not decorative — the same run harness proves"""
        monkeypatch.setenv("MINNI_BACKFILL", "off")
        minnid, loop, _ = _run_main_with_stub_loop(monkeypatch, tmp_path)

        assert loop.task_for(minnid._backfill_runner) is None

    def test_health_report_declares_the_coverage_field(self):
        import inspect

        import minni.minnid_runtime.health as health

        src = inspect.getsource(health.handle_health_report)
        assert "embedding_coverage" in src, (
            "health never compared document count against vector count, so the "
            "gap was invisible to every status surface"
        )


class TestFaissWarmStartRebuild:
    """load_from_disk restored the index but set _vectors = [], and every"""

    def _index(self, tmp_path):
        from minni.config import MinniConfig
        from minni.faiss_index import FAISSIndex

        cfg = MinniConfig(db_path=str(tmp_path / "f.db"))
        return FAISSIndex(cfg)

    def _vecs(self, n, dim):
        import numpy as np

        arr = np.zeros((n, dim), dtype="float32")
        for i in range(n):
            arr[i][i % dim] = 1.0
        return arr

    def test_reconstruct_recovers_raw_vectors(self, tmp_path):
        idx = self._index(tmp_path)
        dim = idx.config.embedding_dim
        idx.build_from_vectors([10, 11, 12], self._vecs(3, dim))

        idx._vectors = []
        assert idx._reconstruct_vectors_from_index() is True
        assert len(idx._vectors) == 3

    def test_rebuild_after_a_warm_start_compacts_removals(self, tmp_path):
        idx = self._index(tmp_path)
        dim = idx.config.embedding_dim
        idx.build_from_vectors([10, 11, 12], self._vecs(3, dim))

        idx._vectors = []          # the warm-start state
        idx.remove(11)
        idx.rebuild()

        assert 11 not in idx._chunk_ids, (
            "rebuild must compact a removed chunk out of a disk-restored "
            "index; on the old code it returned without doing anything"
        )
        assert sorted(idx._chunk_ids) == [10, 12]

    def test_rebuild_reports_when_it_cannot_recover(self, tmp_path, caplog):
        """Never fix a signal by suppressing it: if reconstruction is"""
        import logging

        idx = self._index(tmp_path)
        dim = idx.config.embedding_dim
        idx.build_from_vectors([10, 11], self._vecs(2, dim))
        idx._vectors = []
        idx._index = None  # nothing left to reconstruct from

        with caplog.at_level(logging.WARNING):
            idx.rebuild()
        assert any("rebuild skipped" in r.message.lower() or
                   "rebuild skipped" in r.getMessage().lower()
                   for r in caplog.records), "the skip must be logged"


class TestLayerStamping:
    """Three writers disagreed with indexer._infer_layer: vault_ingest"""

    def test_vault_ingest_stamps_the_inferred_layer_on_disk(self, tmp_path,
                                                            monkeypatch):
        """GA6-1, behaviorally: run the real ingest over an artifact-typed page"""
        import sqlite3

        import minni.db as db_mod
        import minni.models as models
        from minni.afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID
        from minni.afm_passes.vault_ingest import run as run_vault_ingest
        from minni.config import MinniConfig
        from minni.vault_index import vault_index_paths

        monkeypatch.setattr(models, "get_embedder", lambda: _StubEmbedder())

        slug = next(iter(_VAULT_SLUG_TO_AGENT_ID))
        vault = tmp_path / f"{slug}-vault"
        wiki = vault / "wiki"
        wiki.mkdir(parents=True)

        def _page(name, page_type):
            (wiki / name).write_text(
                "\n".join([
                    "---", f"title: {name}", f"type: {page_type}",
                    "status: accepted", "privacy: safe", "---", "",
                    " ".join(f"{name}-{i}" for i in range(90)),
                ]),
                encoding="utf-8",
            )

        _page("art.md", "artifact")
        _page("note.md", "concept")

        cfg = MinniConfig(
            db_path=str(tmp_path / "shared" / "minni.db"),
            vault_path=str(tmp_path / "shared-vault"),
            writeback_enabled=False,
        )
        old = db_mod._migrations_run
        db_mod._migrations_run = False
        try:
            shared = db_mod.MinniDB(cfg)
            shared._get_conn()
        finally:
            db_mod._migrations_run = old

        run_vault_ingest(shared, cfg, vault_path=str(vault), dry_run=False)

        conn = sqlite3.connect(str(vault_index_paths(vault).db_path))
        conn.row_factory = sqlite3.Row
        try:
            layers = {
                os.path.basename(r["path"]): r["layer"]
                for r in conn.execute("SELECT path, layer FROM documents")
            }
            chunk_layers = {
                r["layer"] for r in conn.execute(
                    "SELECT DISTINCT ce.layer FROM chunk_embeddings ce "
                    "JOIN documents d ON d.doc_id = ce.doc_id "
                    "WHERE d.path LIKE '%art.md'"
                )
            }
        finally:
            conn.close()

        assert layers.get("art.md") == "artifact", (
            "an artifact-typed page must be STORED on the artifact layer; the "
            "old writer hardcoded 'knowledge' and made it unrecallable"
        )
        assert layers.get("note.md") == "knowledge"
        assert chunk_layers == {"artifact"}, (
            "chunk_embeddings must carry the same layer as their document"
        )

    def test_artifact_page_type_infers_the_artifact_layer(self):
        from minni.indexer import VaultIndexer

        assert VaultIndexer._infer_layer(agent="codex", page_type="artifact") == "artifact"
        assert VaultIndexer._infer_layer(agent="codex", page_type="concept") == "knowledge"

    def test_wiki_indexer_cannot_self_assign_identity_from_frontmatter(self):
        """wiki_indexer's agent_tag can come from untrusted page frontmatter, so"""
        from minni.indexer import VaultIndexer

        assert VaultIndexer._infer_layer(
            agent="identity:codex", page_type="concept", whole_document=0
        ) != "identity"

    def test_wiki_indexer_stamps_layer_on_disk(self, tmp_path, monkeypatch):
        """GA7-2, behaviorally: index a real artifact-typed wiki page and read"""
        import minni.models as models
        from minni.wiki_indexer import WikiIndexer

        monkeypatch.setattr(models, "get_embedder", lambda: _StubEmbedder())
        db_obj, cfg = _make_db(tmp_path)

        wiki_dir = tmp_path / "wiki" / "pages"
        wiki_dir.mkdir(parents=True)
        (wiki_dir / "art.md").write_text(
            "---\ntitle: Art\nstatus: accepted\nprivacy: safe\ntype: artifact\n---\n\n"
            + " ".join(f"art-{i}" for i in range(90)),
            encoding="utf-8",
        )

        WikiIndexer(db=db_obj, config=cfg).index_wiki(str(wiki_dir.parent))

        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT doc_id, layer FROM documents WHERE path LIKE '%art.md'"
            ).fetchone()
            assert row is not None, "the page must have been indexed"
            chunk_layers = {
                r["layer"] for r in c.execute(
                    "SELECT DISTINCT layer FROM chunk_embeddings WHERE doc_id = ?",
                    (row["doc_id"],),
                ).fetchall()
            }

        assert row["layer"] == "artifact", (
            "an artifact-typed wiki page must be stored on the artifact layer; "
            "the old writer left layer NULL"
        )
        assert chunk_layers and None not in chunk_layers, (
            "chunk_embeddings must carry a non-NULL layer (601 NULL chunks live)"
        )
        db_obj.close()

    def test_wiki_indexer_never_stores_the_identity_layer_from_frontmatter(
        self, tmp_path, monkeypatch
    ):
        """A session page's agent_tag comes from untrusted on-disk frontmatter."""
        import minni.models as models
        from minni.wiki_indexer import WikiIndexer

        monkeypatch.setattr(models, "get_embedder", lambda: _StubEmbedder())
        db_obj, cfg = _make_db(tmp_path)

        wiki_dir = tmp_path / "wiki" / "pages"
        wiki_dir.mkdir(parents=True)
        (wiki_dir / "sneaky.md").write_text(
            "---\ntitle: Sneaky\nstatus: accepted\nprivacy: safe\ntype: session\n"
            "agent: identity:codex\n---\n\n"
            + " ".join(f"sneaky-{i}" for i in range(90)),
            encoding="utf-8",
        )

        WikiIndexer(db=db_obj, config=cfg).index_wiki(str(wiki_dir.parent))

        with db_obj.cursor() as c:
            rows = c.execute(
                "SELECT layer FROM documents WHERE path LIKE '%sneaky.md'"
            ).fetchall()
        assert rows, "the page must have been indexed"
        assert all(r["layer"] != "identity" for r in rows), (
            "frontmatter must never be able to self-assign the identity layer"
        )
        db_obj.close()

    def test_artifact_layer_recall_returns_repaired_rows_end_to_end(
        self, tmp_path, monkeypatch, hermetic_principals
    ):
        """The coherence check for the artifact half: writer fix + migration"""
        import minni.db as db_mod
        from minni.migrations import run_migrations

        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        body = "the deployment rollback procedure is documented here. " * 20

        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO documents (path, agent, sigil, page_type, layer)
                   VALUES ('artifact.md', 'codex', 'vault', 'artifact', 'knowledge')"""
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'artifact.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        before = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "layers": ["artifact"], "expand": False,
        })
        assert before["result"]["count"] == 0, (
            "precondition: the mislabeled row must be invisible to artifact recall"
        )

        conn = db_obj._get_conn()
        conn.execute("DELETE FROM schema_migrations WHERE version = 17")
        conn.commit()
        run_migrations(conn)
        conn.commit()

        after = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "layers": ["artifact"], "expand": False,
        })
        assert after["result"]["count"] >= 1, (
            "after migration 017 a layers=['artifact'] recall must return the "
            "repaired document — that is what 'the day this merges' means"
        )
        assert any(r.get("doc_id") == doc_id for r in after["result"]["results"])

    def test_migration_backfills_mislabeled_artifact_rows(self, tmp_path):
        """GA6-1 backfill: the writer fix only helps re-ingested pages, and"""
        import minni.db as db_mod
        from minni.migrations import run_migrations

        db_obj, _cfg = _make_db(tmp_path)

        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO documents (path, agent, sigil, page_type, layer)
                   VALUES ('art.md', 'codex', 'vault', 'artifact', 'knowledge')"""
            )
            mislabeled = c.lastrowid
            c.execute(
                """INSERT INTO documents (path, agent, sigil, page_type, layer)
                   VALUES ('note.md', 'codex', 'vault', 'concept', 'knowledge')"""
            )
            untouched = c.lastrowid
            c.execute(
                """INSERT INTO documents (path, agent, sigil, page_type, layer)
                   VALUES ('id.md', 'identity:codex', 'vault', 'artifact', 'identity')"""
            )
            identity_doc = c.lastrowid

        conn = db_obj._get_conn()
        conn.execute("DELETE FROM schema_migrations WHERE version = 17")
        conn.commit()
        run_migrations(conn)
        conn.commit()

        with db_obj.cursor() as c:
            rows = {
                r["doc_id"]: r["layer"]
                for r in c.execute(
                    "SELECT doc_id, layer FROM documents"
                ).fetchall()
            }
        assert rows[mislabeled] == "artifact", (
            "an artifact-typed row stamped 'knowledge' must be repaired"
        )
        assert rows[untouched] == "knowledge", "non-artifact rows are untouched"
        assert rows[identity_doc] == "identity", (
            "the migration must never overwrite a non-knowledge layer"
        )
        db_obj.close()


class TestDecayIsScheduled:
    """run_decay was reachable only from the manual CLI and no launchd job"""

    def test_daemon_registers_a_decay_runner(self):
        import minni.minnid as minnid

        assert hasattr(minnid, "_decay_runner"), (
            "the daemon must schedule a decay pass; on the old code decay was "
            "reachable only from sovereign_memory's manual CLI"
        )
        assert hasattr(minnid, "_decay_enabled")
        assert hasattr(minnid, "_decay_sweep_once")

    def test_decay_runner_is_started_by_main(self, monkeypatch, tmp_path):
        """A runner nobody creates a task for is the same dead channel."""
        import signal

        monkeypatch.setenv("MINNI_DECAY", "on")
        minnid, loop, handlers = _run_main_with_stub_loop(monkeypatch, tmp_path)

        task = loop.task_for(minnid._decay_runner)
        assert task is not None, (
            "main() must create the decay task "
            f"(scheduled: {[t.name for t in loop.tasks]})"
        )
        assert not task.cancelled, (
            "main() returned with the decay task already cancelled — a task "
            "queued and then killed on the next line sweeps nothing"
        )

        mark = loop.mark()
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert task.cancelled, "shutdown must cancel the decay task"
        assert "_decay_runner" in loop.cancels_since(mark), (
            "the cancel must be caused by the shutdown handler, not by main()"
        )

    def test_main_actually_runs_the_loop(self, monkeypatch, tmp_path):
        """Creating tasks is only half of scheduling. A main() that builds every"""
        _, loop, _ = _run_main_with_stub_loop(monkeypatch, tmp_path)

        assert ("run", "_serve_unix_socket") in loop.events, (
            "main() must run the loop, not merely populate it "
            f"(events: {loop.events})"
        )

    def test_the_harness_never_reaches_the_live_database(self, monkeypatch, tmp_path):
        """main()'s eager startup migration opens MinniDB.shared inside a"""
        _, loop, _ = _run_main_with_stub_loop(monkeypatch, tmp_path)

        assert loop.live_db_attempts, (
            "main() no longer routes its startup DB open through "
            "MinniDB.shared — the live-database guard is not holding"
        )

    def test_decay_is_not_scheduled_when_disabled(self, monkeypatch, tmp_path):
        """MINNI_DECAY=off must actually suppress the task, so the positive"""
        monkeypatch.setenv("MINNI_DECAY", "off")
        minnid, loop, _ = _run_main_with_stub_loop(monkeypatch, tmp_path)

        assert loop.task_for(minnid._decay_runner) is None

    def test_decay_enabled_by_default_and_env_disablable(self, monkeypatch):
        import minni.minnid as minnid

        monkeypatch.delenv("MINNI_DECAY", raising=False)
        assert minnid._decay_enabled() is True
        monkeypatch.setenv("MINNI_DECAY", "off")
        assert minnid._decay_enabled() is False

    def test_decay_interval_defaults_daily_with_a_floor(self, monkeypatch):
        import minni.minnid as minnid

        monkeypatch.delenv("MINNI_DECAY_INTERVAL", raising=False)
        assert minnid._decay_interval() == 86400
        monkeypatch.setenv("MINNI_DECAY_INTERVAL", "5")
        assert minnid._decay_interval() == 3600, "sub-hour sweeps are pointless"
        monkeypatch.setenv("MINNI_DECAY_INTERVAL", "not-a-number")
        assert minnid._decay_interval() == 86400

    def test_a_stale_document_actually_decays(self, tmp_path):
        """The acceptance check from issue #225: a non-1.0 decay_score on an"""
        from minni.decay import MemoryDecay

        db_obj, cfg = _make_db(tmp_path)
        old = time.time() - (90 * 86400)  # 90 days, ~13 half-lives
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO documents (path, agent, sigil, indexed_at,
                                          last_accessed, access_count, decay_score)
                   VALUES ('stale.md', 'codex', 'vault', ?, ?, 0, 1.0)""",
                (old, old),
            )
            doc_id = c.lastrowid

        stats = MemoryDecay(db_obj, cfg).run_decay()
        assert stats["updated"] == 1

        with db_obj.cursor() as c:
            score = c.execute(
                "SELECT decay_score FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()["decay_score"]
        assert score < 1.0, "a 90-day-old document must not still score 1.0"

    def test_decay_pass_is_idempotent(self, tmp_path):
        """Restart-heavy machines sweep more often than planned; scores must"""
        from minni.decay import MemoryDecay

        db_obj, cfg = _make_db(tmp_path)
        old = time.time() - (30 * 86400)
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO documents (path, agent, sigil, indexed_at,
                                          last_accessed, access_count, decay_score)
                   VALUES ('stale.md', 'codex', 'vault', ?, ?, 0, 1.0)""",
                (old, old),
            )
            doc_id = c.lastrowid

        decay = MemoryDecay(db_obj, cfg)
        decay.run_decay()
        with db_obj.cursor() as c:
            first = c.execute(
                "SELECT decay_score FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()["decay_score"]
        second_stats = decay.run_decay()
        with db_obj.cursor() as c:
            second = c.execute(
                "SELECT decay_score FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()["decay_score"]

        assert second_stats["updated"] == 0, "a converged pass rewrites nothing"
        assert abs(first - second) < 1e-9

    def test_all_indexes_sweep_isolates_a_failing_vault(self, tmp_path, monkeypatch):
        """One unreadable vault index must not cost every other index its pass."""
        import minni.decay as decay_mod
        import minni.index_all as index_all
        import minni.vault_index as vault_index

        db_obj, cfg = _make_db(tmp_path)
        db_obj.close()

        broken = tmp_path / "broken-vault"
        broken.mkdir()
        monkeypatch.setattr(
            index_all, "discover_agent_vaults", lambda home=None: [broken]
        )

        def _explode(*_a, **_kw):
            raise RuntimeError("vault index unreadable")

        monkeypatch.setattr(vault_index, "build_vault_index_config", _explode)

        results = decay_mod.run_decay_all_indexes(cfg)
        assert "shared" in results
        assert "error" not in results["shared"], (
            "the shared index must still be decayed when a vault fails"
        )
        assert "error" in results["broken-vault"]


class TestDecayRerankParity:
    """decay_score only ever entered final_score (_score_merged_doc). The"""

    def test_decayed_candidate_sorts_below_fresh_at_equal_logit(self, tmp_path):
        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [
            {"doc_id": 1, "rerank_score": 2.0, "decay_score": 0.10, "page_type": "note"},
            {"doc_id": 2, "rerank_score": 2.0, "decay_score": 1.00, "page_type": "note"},
        ]
        engine._apply_rerank_score_adjustments(candidates)
        candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)

        assert [c["doc_id"] for c in candidates] == [2, 1], (
            "the fresh document must outrank the decayed one at an identical "
            "raw logit; on the old code both kept rerank_score=2.0 and decay "
            "changed nothing"
        )
        assert abs(candidates[1]["rerank_score"] - 0.2) < 1e-9

    def test_decay_attenuates_negative_logit_downward(self, tmp_path):
        """A negative logit must get MORE negative under decay. Naive"""
        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [{"doc_id": 1, "rerank_score": -1.0, "decay_score": 0.5,
                       "page_type": "note"}]
        engine._apply_decay_rerank_attenuation(candidates)
        assert candidates[0]["rerank_score"] < -1.0

    def test_zero_logit_is_pushed_below_an_undecayed_zero(self, tmp_path):
        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [
            {"doc_id": 1, "rerank_score": 0.0, "decay_score": 0.25, "page_type": "note"},
            {"doc_id": 2, "rerank_score": 0.0, "decay_score": 1.00, "page_type": "note"},
        ]
        engine._apply_decay_rerank_attenuation(candidates)
        assert candidates[0]["rerank_score"] < candidates[1]["rerank_score"]

    def test_undecayed_candidate_is_untouched(self, tmp_path):
        """Zero behaviour change for a corpus that has never been decayed."""
        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [{"doc_id": 1, "rerank_score": 1.5, "decay_score": 1.0,
                       "page_type": "note"}]
        engine._apply_decay_rerank_attenuation(candidates)
        assert candidates[0]["rerank_score"] == 1.5

    def test_missing_decay_score_defaults_to_no_attenuation(self, tmp_path):
        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [{"doc_id": 1, "rerank_score": 1.5, "page_type": "note"}]
        engine._apply_decay_rerank_attenuation(candidates)
        assert candidates[0]["rerank_score"] == 1.5

    def test_correction_decay_floor_applies_on_the_rerank_leg_too(self, tmp_path):
        """recall-F4's floor lives in _score_merged_doc. If the rerank leg does"""
        engine, _db, cfg = _make_engine(tmp_path)
        page_type = sorted(engine._correction_types)[0]
        candidates = [{"doc_id": 1, "rerank_score": 1.0, "decay_score": 0.01,
                       "page_type": page_type}]
        engine._apply_decay_rerank_attenuation(candidates)
        assert abs(candidates[0]["decay_applied"]
                   - float(cfg.correction_decay_floor)) < 1e-9

    def test_rerank_public_path_orders_by_decay(self, tmp_path):
        """Name-independent proof: drive _rerank itself with a stub"""
        engine, _db, _cfg = _make_engine(tmp_path)
        engine._reranker = _ConstantStubReranker(1.0)
        candidates = [
            {"doc_id": 1, "chunk_id": None, "chunk_text": "alpha",
             "rerank_score": None, "decay_score": 0.05, "page_type": "note"},
            {"doc_id": 2, "chunk_id": None, "chunk_text": "alpha",
             "rerank_score": None, "decay_score": 1.00, "page_type": "note"},
        ]
        ranked = engine._rerank("alpha", candidates)
        assert [c["doc_id"] for c in ranked] == [2, 1], (
            "identical cross-encoder logits must be broken by decay; the "
            "decayed doc_id=1 must not lead"
        )

    def test_decay_above_one_cannot_become_a_promotion_channel(self, tmp_path):
        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [{"doc_id": 1, "rerank_score": 1.0, "decay_score": 5.0,
                       "page_type": "note"}]
        engine._apply_decay_rerank_attenuation(candidates)
        assert candidates[0]["rerank_score"] == 1.0


class TestDecayIsAppliedOnce:
    """Round-2 finding 1: _apply_decay_rerank_attenuation mutated rerank_score"""

    def test_adjustments_preserve_the_raw_logit(self, tmp_path):
        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [
            {"doc_id": 1, "rerank_score": 2.0, "decay_score": 0.5, "page_type": "note"},
            {"doc_id": 2, "rerank_score": 2.0, "decay_score": 1.0, "page_type": "note"},
        ]
        engine._apply_rerank_score_adjustments(candidates)
        assert candidates[0]["raw_rerank_score"] == 2.0, (
            "the model-pure logit must survive attenuation; without it every "
            "downstream consumer only sees the decayed value"
        )
        assert candidates[1]["raw_rerank_score"] == 2.0
        assert abs(candidates[0]["rerank_score"] - 1.0) < 1e-9, (
            "ranking itself must still see the decayed score"
        )

    def test_rerank_public_path_carries_the_raw_logit(self, tmp_path):
        engine, _db, _cfg = _make_engine(tmp_path)
        engine._reranker = _ConstantStubReranker(2.0)
        candidates = [
            {"doc_id": 1, "chunk_id": None, "chunk_text": "alpha",
             "rerank_score": None, "decay_score": 0.5, "page_type": "note"},
        ]
        ranked = engine._rerank("alpha", candidates)
        assert ranked[0]["raw_rerank_score"] == 2.0
        assert abs(ranked[0]["rerank_score"] - 1.0) < 1e-9

    def test_confidence_math_is_single_application(self, tmp_path):
        """Equal raw logits, decay 0.5: the confidence the search path computes"""
        from minni.scoring import compute_confidence

        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [
            {"doc_id": 1, "rerank_score": 2.0, "decay_score": 0.5, "page_type": "note"},
        ]
        engine._apply_rerank_score_adjustments(candidates)
        r = candidates[0]

        got = compute_confidence(
            rrf_score=0.02,
            cross_encoder_score=r.get("raw_rerank_score", r.get("rerank_score")),
            decay_factor=r.get("decay_score"),
        )
        single = compute_confidence(
            rrf_score=0.02, cross_encoder_score=2.0, decay_factor=0.5,
        )
        double = compute_confidence(
            rrf_score=0.02, cross_encoder_score=1.0, decay_factor=0.5,
        )
        assert abs(got - single) < 1e-12
        assert got > double, (
            "single-application confidence must exceed the double-decayed one; "
            "equality here means decay leaked into the logit again"
        )

    def test_search_rpc_feeds_confidence_the_raw_logit(self, tmp_path, monkeypatch,
                                                       hermetic_principals):
        """Behavioral pin through the production search RPC: with a stub"""
        import minni.minnid as minnid
        import minni.scoring as scoring
        import minni.writeback as wb_mod
        from minni.retrieval import RetrievalEngine
        from minni.writeback import WriteBackMemory

        db_obj, cfg = _make_db(tmp_path, hyde_enabled=False)  # reranker stays ON
        wb = WriteBackMemory(db_obj, cfg)
        monkeypatch.setattr(minnid, "_writeback", wb)
        monkeypatch.setattr(wb_mod.WriteBackMemory, "model", property(lambda self: None))
        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
        monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: None))
        monkeypatch.setattr(minnid, "_retrieval", engine)
        engine._reranker = _ConstantStubReranker(2.0)

        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer, decay_score) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge', 0.5)"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        real = scoring.compute_confidence
        seen = []

        def _spy(rrf_score, cross_encoder_score, decay_factor, db=None,
                 record=False):
            seen.append((cross_encoder_score, decay_factor, record))
            return real(rrf_score, cross_encoder_score, decay_factor, db=db,
                        record=record)

        monkeypatch.setattr(scoring, "compute_confidence", _spy)

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        assert resp["result"]["count"] >= 1
        decayed = [(ce, d) for ce, d, _rec in seen if d == pytest.approx(0.5)]
        assert decayed, "the formatted result set must compute confidence"
        for ce, _d in decayed:
            assert abs(ce - 2.0) < 1e-9, (
                "confidence must get the raw logit; 1.0 here means the decayed "
                "rerank_score leaked in and decay was applied twice"
            )
        assert not any(rec for _ce, _d, rec in seen), (
            "compute_confidence(record=True) inside formatting re-pollutes the "
            "calibration window on multi-call searches"
        )


class TestVaultBackfillRefreshesWarmEngines:
    """Round-2 finding 2: on_vectors covers the shared index only. A vault"""

    def _sweep_with(self, monkeypatch, results):
        import minni.backfill as backfill_mod
        import minni.minnid as minnid

        monkeypatch.setattr(
            backfill_mod, "run_backfill_all_indexes", lambda *a, **kw: results
        )
        return minnid._backfill_sweep_once()

    def test_vault_progress_clears_the_vault_retrieval_cache(self, monkeypatch):
        import minni.minnid as minnid

        minnid._vault_retrieval_cache["stale"] = ("engine", "codex", "db")
        try:
            self._sweep_with(monkeypatch, {
                "shared": {"documents": {"documents": 0, "chunks": 0}},
                "codex-vault": {"documents": {"documents": 2, "chunks": 5}},
            })
            assert not minnid._vault_retrieval_cache, (
                "a vault that gained vectors must evict its warm cached "
                "engine; on the old code the engine early-returned in "
                "_ensure_faiss_loaded and the new rows never reached FAISS"
            )
        finally:
            minnid._vault_retrieval_cache.clear()

    def test_idle_sweep_keeps_the_warm_engines(self, monkeypatch):
        """No vault progress → no eviction; warm engines are worth keeping."""
        import minni.minnid as minnid

        sentinel = ("engine", "codex", "db")
        minnid._vault_retrieval_cache["warm"] = sentinel
        try:
            self._sweep_with(monkeypatch, {
                "shared": {"documents": {"documents": 3, "chunks": 9}},
                "codex-vault": {"documents": {"documents": 0, "chunks": 0}},
                "broken-vault": {"error": "vault index unreadable"},
            })
            assert minnid._vault_retrieval_cache.get("warm") is sentinel, (
                "shared-only progress (already covered by on_vectors) and "
                "vault errors must not churn the vault engine cache"
            )
        finally:
            minnid._vault_retrieval_cache.clear()


class TestShortDocumentBackfill:
    """Round-2 finding 3: content below the chunker's min_tokens chunked to"""

    def _stub_encoder(self, monkeypatch):
        import minni.models as models

        monkeypatch.setattr(models, "get_embedder", lambda: _StubEmbedder())

    def test_short_document_gets_a_whole_body_vector(self, tmp_path, monkeypatch):
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        short = "the lock code is 4711"
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('short.md', 'codex', 'vault')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'short.md', ?, 'codex', 'vault')",
                (doc_id, short),
            )

        stats = backfill_document_vectors(db_obj, cfg)
        assert stats["documents"] == 1
        assert stats["chunks"] == 1
        assert "skipped_no_content" not in stats
        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT chunk_text FROM chunk_embeddings WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        assert row is not None and row["chunk_text"] == short, (
            "the whole short body must be the embedded chunk, mirroring the "
            "durable-path short-content floor"
        )

    def test_short_documents_cannot_wedge_the_drain(self, tmp_path, monkeypatch):
        """Three sub-min_tokens docs ahead of a long one, limit=2: the old skip"""
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        long_body = "# Title\n\n" + ("the deployment rollback procedure. " * 40)
        with db_obj.cursor() as c:
            for i in range(3):
                c.execute(
                    "INSERT INTO documents (path, agent, sigil) VALUES (?, 'c', 'v')",
                    (f"stub{i}.md",),
                )
                c.execute(
                    "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                    "VALUES (?, ?, ?, 'c', 'v')",
                    (c.lastrowid, f"stub{i}.md", f"short decision stub {i}"),
                )
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('good.md', 'codex', 'vault')"
            )
            good_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'good.md', ?, 'codex', 'vault')",
                (good_id, long_body),
            )

        total = 0
        for _ in range(2):
            total += backfill_document_vectors(db_obj, cfg, limit=2)["documents"]
        assert total == 4
        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id = ?",
                (good_id,),
            ).fetchone()["n"]
        assert n >= 1, "the recoverable long document must be reachable"


class _PoisonEmbedder:
    """Raises for poison content — the permanent-encode-failure shape."""

    def encode(self, text, **kwargs):
        import numpy as np

        if "POISONROW" in text:
            raise RuntimeError("model reject")
        vec = np.zeros(384, dtype="float32")
        vec[len(text) % 384] = 1.0
        return vec


class TestEncodeFailureCannotWedgeTheDrain:
    """Round-3 finding 1: empty content and short docs are excluded by static"""

    def _stub_encoder(self, monkeypatch):
        import minni.models as models

        monkeypatch.setattr(models, "get_embedder", lambda: _PoisonEmbedder())

    def test_encode_failures_cannot_wedge_the_learning_drain(
        self, tmp_path, monkeypatch
    ):
        from minni.backfill import backfill_learning_embeddings

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        now = time.time()
        with db_obj.cursor() as c:
            for i in range(3):
                c.execute(
                    """INSERT INTO learnings (agent_id, category, content,
                                              confidence, created_at, embedding)
                       VALUES ('codex', 'fix', ?, 1.0, ?, NULL)""",
                    (f"POISONROW {i} always fails to encode", now),
                )
            c.execute(
                """INSERT INTO learnings (agent_id, category, content,
                                          confidence, created_at, embedding)
                   VALUES ('codex', 'fix', 'websocket backoff is 500ms', 1.0, ?, NULL)""",
                (now,),
            )

        embedded = 0
        for _ in range(2):
            embedded += backfill_learning_embeddings(db_obj, cfg, limit=2)["embedded"]
        assert embedded == 1, (
            "the recoverable learning must drain despite >=limit permanently "
            "failing rows ahead of it; the old un-ordered batch re-served the "
            "same failing head every pass"
        )

    def test_encode_failures_cannot_wedge_the_document_drain(
        self, tmp_path, monkeypatch
    ):
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            for i in range(3):
                c.execute(
                    "INSERT INTO documents (path, agent, sigil) VALUES (?, 'c', 'v')",
                    (f"bad{i}.md",),
                )
                c.execute(
                    "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                    "VALUES (?, ?, ?, 'c', 'v')",
                    (c.lastrowid, f"bad{i}.md",
                     "# Bad\n\n" + (f"POISONROW {i} corrupt payload. " * 40)),
                )
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('good.md', 'codex', 'vault')"
            )
            good_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'good.md', ?, 'codex', 'vault')",
                (good_id, "# Title\n\n" + ("the deployment rollback procedure. " * 40)),
            )

        drained = 0
        for _ in range(2):
            drained += backfill_document_vectors(db_obj, cfg, limit=2)["documents"]
        assert drained == 1
        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id = ?",
                (good_id,),
            ).fetchone()["n"]
        assert n >= 1, (
            "the recoverable document must gain vectors despite encode-raising "
            "rows occupying the head of the queue"
        )

    def test_failed_rows_are_retried_after_the_cursor_wraps(
        self, tmp_path, monkeypatch
    ):
        """Failures must be stepped past, not excluded forever — a transient"""
        from minni.backfill import backfill_learning_embeddings

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content,
                                          confidence, created_at, embedding)
                   VALUES ('codex', 'fix', 'POISONROW transient', 1.0, ?, NULL)""",
                (time.time(),),
            )

        first = backfill_learning_embeddings(db_obj, cfg, limit=2)
        assert first["failed"] == 1
        again = backfill_learning_embeddings(db_obj, cfg, limit=2)
        assert again["failed"] == 1, (
            "a failed row must come back after the wrap; permanent exclusion "
            "would turn every transient fault into a silent hole"
        )


class TestCoverageMatchesDrainEligibility:
    """Round-3 finding 2: coverage counted terminal learnings (rejected /"""

    def test_terminal_learnings_do_not_dent_the_ratio(self, tmp_path):
        from minni.backfill import embedding_coverage

        db_obj, _cfg = _make_db(tmp_path)
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content,
                                          confidence, created_at, embedding, status)
                   VALUES ('codex', 'fix', 'active learning', 1.0, ?, ?, 'active')""",
                (now, b"\x00" * 4),
            )
            c.execute(
                """INSERT INTO learnings (agent_id, category, content,
                                          confidence, created_at, embedding, status)
                   VALUES ('codex', 'fix', 'dead learning', 1.0, ?, NULL, 'rejected')""",
                (now,),
            )

        cov = embedding_coverage(db_obj)
        assert cov["learnings_total"] == 1
        assert cov["learnings_missing_embedding"] == 0, (
            "a rejected NULL-embedding learning is untouchable by backfill and "
            "invisible to recall; counting it as missing makes health lie"
        )
        assert cov["learnings_embedding_ratio"] == 1.0
        assert cov["learnings_terminal_null_embedding"] == 1, (
            "the excluded rows must stay visible as their own count"
        )


class TestCorrectionZeroLogitSurvivesDecay:
    """Round-3 finding 3: decay-first mapped a raw 0.0 logit to decay - 1.0"""

    def test_decayed_zero_logit_correction_outranks_zero_non_correction(
        self, tmp_path
    ):
        engine, _db, cfg = _make_engine(tmp_path)
        correction_type = sorted(engine._correction_types)[0]
        candidates = [
            {"doc_id": 1, "rerank_score": 0.0, "decay_score": 1.0,
             "page_type": "note"},
            {"doc_id": 2, "rerank_score": 0.0, "decay_score": 0.10,
             "page_type": correction_type},
        ]
        engine._apply_rerank_score_adjustments(candidates)
        candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        assert [c["doc_id"] for c in candidates] == [2, 1], (
            "a decayed zero-logit correction must keep its lift; decay-first "
            "turned it negative and sank it below the habitual hit"
        )
        boost = float(cfg.correction_salience_boost)
        floor = float(cfg.correction_decay_floor)
        expected = boost * max(0.10, floor)
        assert abs(candidates[0]["rerank_score"] - expected) < 1e-9, (
            "the lift must be the boosted zero attenuated once by the floored "
            "decay: boost * decay"
        )

    def test_nonzero_logits_are_unmoved_by_the_reorder(self, tmp_path):
        """Positive and negative branches must compose identically in either"""
        engine, _db, cfg = _make_engine(tmp_path)
        correction_type = sorted(engine._correction_types)[0]
        boost = float(cfg.correction_salience_boost)
        decay = max(0.6, float(cfg.correction_decay_floor))
        candidates = [
            {"doc_id": 1, "rerank_score": 2.0, "decay_score": decay,
             "page_type": correction_type},
            {"doc_id": 2, "rerank_score": -1.0, "decay_score": decay,
             "page_type": correction_type},
        ]
        engine._apply_rerank_score_adjustments(candidates)
        assert abs(candidates[0]["rerank_score"]
                   - 2.0 * decay * (1.0 + boost)) < 1e-9
        assert abs(candidates[1]["rerank_score"]
                   - (-1.0 / (decay * (1.0 + boost)))) < 1e-9


class TestCoverageCoversEveryIndex:
    """Round-3 finding 4: the drain is multi-index but health's"""

    def test_vault_coverage_reports_per_vault_counts(self, tmp_path, monkeypatch):
        import minni.index_all as index_all
        import minni.vault_index as vault_index
        from minni.backfill import vault_embedding_coverage

        vault_db, vault_cfg = _make_db(tmp_path)
        with vault_db.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('a.md', 'codex', 'vault')"
            )
        vault_db.close()

        vault = tmp_path / "codex-vault"
        vault.mkdir()
        monkeypatch.setattr(
            index_all, "discover_agent_vaults", lambda home=None: [vault]
        )
        monkeypatch.setattr(
            vault_index, "build_vault_index_config",
            lambda v, base_config=None: vault_cfg,
        )

        out = vault_embedding_coverage()
        assert out["codex-vault"]["documents_missing_vectors"] == 1

    def test_an_unreadable_vault_reports_its_own_error(self, tmp_path, monkeypatch):
        import minni.index_all as index_all
        import minni.vault_index as vault_index
        from minni.backfill import vault_embedding_coverage

        broken = tmp_path / "broken-vault"
        broken.mkdir()
        monkeypatch.setattr(
            index_all, "discover_agent_vaults", lambda home=None: [broken]
        )

        def _explode(*_a, **_kw):
            raise RuntimeError("vault index unreadable")

        monkeypatch.setattr(vault_index, "build_vault_index_config", _explode)
        out = vault_embedding_coverage()
        assert "error" in out["broken-vault"]

    def test_health_report_carries_the_vault_rollup(self):
        import inspect

        import minni.minnid_runtime.health as health

        src = inspect.getsource(health.handle_health_report)
        assert "vault_embedding_coverage" in src, (
            "health must aggregate per-vault coverage, not sample only the "
            "shared DB while the drain covers every index"
        )


class TestCalibrationWindowIsBoundaryFed:
    """Round-4 finding 1: _format_results recorded on every engine call, but"""

    def test_scope_both_records_count_rows_not_double(self, tmp_path, monkeypatch,
                                                      hermetic_principals):
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        count = resp["result"]["count"]
        assert count >= 1

        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM score_distribution"
            ).fetchone()["n"]
        assert n == count, (
            f"one search must record exactly the {count} caller-visible "
            f"result(s), not {n}; double-recording pads the window toward the "
            "activation threshold with scores no caller saw"
        )

    def test_confidence_raw_does_not_leak_into_the_response(
        self, tmp_path, monkeypatch, hermetic_principals
    ):
        """The recording carrier is popped at the boundary; the transport"""
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )
        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        results = resp["result"]["results"]
        assert results
        assert all("confidence_raw" not in r for r in results)
        assert all("confidence" in r for r in results)


class TestWarmAddAfterFailedReconstruct:
    """Round-4 finding 2: a warm-started index whose reconstruct failed has"""

    def _warm_index_without_vectors(self, tmp_path):
        import numpy as np

        from minni.faiss_index import FAISSIndex

        _db, cfg = _make_db(tmp_path)
        idx = FAISSIndex(cfg)
        seed = np.zeros((2, cfg.embedding_dim), dtype="float32")
        seed[0][0] = 1.0
        seed[1][1] = 1.0
        idx.build_from_vectors([101, 102], seed)
        idx._vectors = []
        return idx, cfg

    def test_add_after_failed_reconstruct_keeps_rebuild_honest(
        self, tmp_path, monkeypatch
    ):
        import numpy as np

        idx, cfg = self._warm_index_without_vectors(tmp_path)
        monkeypatch.setattr(idx, "_reconstruct_vectors_from_index", lambda: False)

        vec = np.zeros(cfg.embedding_dim, dtype="float32")
        vec[2] = 1.0
        idx.add(103, vec)

        assert idx._chunk_ids == [101, 102, 103]
        assert idx._vectors == [], (
            "an orphan append to _vectors defeats the GA4-4 empty-vectors "
            "rebuild guard; the honest state after a failed reconstruct is "
            "NO raw vectors"
        )
        assert idx._index.ntotal == 3
        idx.rebuild()
        assert idx._chunk_ids == [101, 102, 103]

    def test_add_recovers_vectors_when_reconstruct_succeeds(self, tmp_path):
        """A flat index CAN reconstruct — add() must heal _vectors instead of"""
        import numpy as np

        idx, cfg = self._warm_index_without_vectors(tmp_path)
        vec = np.zeros(cfg.embedding_dim, dtype="float32")
        vec[2] = 1.0
        idx.add(103, vec)
        assert len(idx._vectors) == 3, (
            "reconstructable vectors must be recovered before the append so "
            "the parallel arrays stay paired"
        )
        assert len(idx._chunk_ids) == 3
        idx.rebuild()
        assert idx._index.ntotal == 3


class TestStringLayersScopeDocuments:
    """Round-4 finding 3: layers='episodic' (bare string) enabled the episodic"""

    def _seed_doc(self, db_obj):
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

    def test_string_episodic_layer_excludes_documents(self, tmp_path, monkeypatch,
                                                      hermetic_principals):
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        self._seed_doc(db_obj)
        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "expand": False, "layers": "episodic",
        })
        assert "error" not in resp
        assert resp["result"]["count"] == 0, (
            "layers='episodic' must scope documents out, not fail open to an "
            "unfiltered document search"
        )

    def test_string_layer_still_admits_its_own_layer(self, tmp_path, monkeypatch,
                                                     hermetic_principals):
        """The string form must select the named layer, not filter everything."""
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        self._seed_doc(db_obj)
        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "expand": False, "layers": "knowledge",
        })
        assert "error" not in resp
        assert resp["result"]["count"] >= 1


class TestConfidenceSharesOneBasis:
    """Round-5 finding 1: default scope=both is multi-ENGINE. Vault hits used"""

    def test_scope_both_returns_one_calibration_basis(self, tmp_path, monkeypatch,
                                                      hermetic_principals):
        import minni.minnid as minnid
        from minni.retrieval import RetrievalEngine

        _engine, shared_db, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)

        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        vault_db, vault_cfg = _make_db(vault_dir, reranker_enabled=False,
                                       hyde_enabled=False)
        vault_engine = RetrievalEngine(vault_db, vault_cfg, faiss_index=object())

        shared_body = "the deployment rollback procedure is documented here. " * 20
        vault_body = "deployment rollback happened on the edge fleet vault. " * 20
        with shared_db.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('shared.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'shared.md', ?, 'codex', 'vault')",
                (doc_id, shared_body),
            )
            for _ in range(10):
                c.execute(
                    "INSERT INTO score_distribution (raw_score, kind, created_at) "
                    "VALUES (0.0, 'combined', ?)",
                    (time.time(),),
                )
        with vault_db.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('filler.md', 'codex', 'vault')"
            )
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('personal.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'personal.md', ?, 'codex', 'vault')",
                (doc_id, vault_body),
            )

        monkeypatch.setattr(
            minnid, "_agent_vault_retrieval",
            lambda agent_id: (vault_engine, "codex", str(vault_cfg.db_path)),
        )
        monkeypatch.setattr(minnid, "_all_vault_retrievals", lambda: [])

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        results = resp["result"]["results"]
        sources = {r.get("source") for r in results}
        assert "personal.md" in sources and "shared.md" in sources, (
            "the pin needs one vault hit and one shared hit in a single response"
        )
        confidences = [r["confidence"] for r in results]
        assert all(c is not None and c >= 0.85 for c in confidences), (
            f"every confidence must share the shared percentile basis after "
            f"activation; got {confidences} — a sub-0.85 value is a vault hit "
            "still served on the raw_blend basis while shared hits are "
            "percentile ranks"
        )
        with shared_db.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM score_distribution"
            ).fetchone()["n"]
        assert n == 10 + len(results)


class TestLayersFailClosedAtTheRoot:
    """Round-5 finding 2: the RPC edge wrap papered over _normalize_layers,"""

    def _direct_engine(self, tmp_path, monkeypatch):
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path, reranker_enabled=False,
                               hyde_enabled=False)
        monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: None))
        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )
        return engine

    def test_direct_string_layers_scope_documents(self, tmp_path, monkeypatch):
        engine = self._direct_engine(tmp_path, monkeypatch)
        assert engine.retrieve(
            query="deployment rollback", expand=False, layers="episodic",
        ) == [], (
            "a bare-string layer must scope the search at the engine root, "
            "not only behind the daemon's edge wrap"
        )
        assert engine.retrieve(
            query="deployment rollback", expand=False, layers="knowledge",
        ), "the string form must admit its own layer"

    def test_explicit_invalid_or_empty_filters_fail_closed(self, tmp_path,
                                                           monkeypatch):
        engine = self._direct_engine(tmp_path, monkeypatch)
        assert engine.retrieve(
            query="deployment rollback", expand=False, layers=["nope"],
        ) == [], (
            "an explicit filter with zero valid layers must match nothing; "
            "falling open serves the unscoped corpus under a scoped request"
        )
        assert engine.retrieve(
            query="deployment rollback", expand=False, layers=[],
        ) == []

    def test_chronological_sort_fails_closed_too(self, tmp_path, monkeypatch):
        engine = self._direct_engine(tmp_path, monkeypatch)
        assert engine.retrieve(
            query="deployment rollback", expand=False, layers=["nope"],
            sort="chronological",
        ) == []


class TestCorrectionFloorReachesConfidence:
    """Round-5 finding 3: both ranking legs floor a correction's decay"""

    def test_score_merged_doc_stamps_effective_decay(self, tmp_path):
        engine, _db, cfg = _make_engine(tmp_path)
        correction_type = sorted(engine._correction_types)[0]
        d = {"rrf_score": 0.02, "decay_score": 0.01, "page_type": correction_type}
        engine._score_merged_doc(d)
        assert d["decay_applied"] == pytest.approx(
            float(cfg.correction_decay_floor)
        )
        plain = {"rrf_score": 0.02, "decay_score": 0.3, "page_type": "note"}
        engine._score_merged_doc(plain)
        assert plain["decay_applied"] == pytest.approx(0.3)

    def test_confidence_reads_the_floored_decay_end_to_end(self, tmp_path,
                                                           monkeypatch,
                                                           hermetic_principals):
        import minni.scoring as scoring

        _engine, db_obj, cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        correction_type = sorted(_engine._correction_types)[0]
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer, decay_score, "
                "page_type) VALUES ('c.md', 'codex', 'vault', 'knowledge', 0.01, ?)",
                (correction_type,),
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'c.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        real = scoring.raw_confidence
        seen = []

        def _spy(rrf_score, cross_encoder_score, decay_factor):
            seen.append(decay_factor)
            return real(rrf_score, cross_encoder_score, decay_factor)

        monkeypatch.setattr(scoring, "raw_confidence", _spy)
        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        assert resp["result"]["count"] >= 1
        assert seen, "the format path must derive confidence_raw"
        floor = float(cfg.correction_decay_floor)
        assert all(d == pytest.approx(floor) for d in seen), (
            f"confidence must see the floored decay {floor}, not the raw 0.01 "
            "that disagrees with both ranking legs"
        )


class TestBasisIsUniformWithinOneResponse:
    """Round-6 finding 1: recording and calibrating row-by-row let the window"""

    def test_mid_response_activation_cannot_split_the_basis(
        self, tmp_path, monkeypatch, hermetic_principals
    ):
        from minni.scoring import _ACTIVATION_THRESHOLD

        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        body = "the deployment rollback procedure is documented here. "
        with db_obj.cursor() as c:
            for i in range(3):
                c.execute(
                    "INSERT INTO documents (path, agent, sigil, layer) "
                    "VALUES (?, 'codex', 'vault', 'knowledge')",
                    (f"doc{i}.md",),
                )
                doc_id = c.lastrowid
                c.execute(
                    "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                    "VALUES (?, ?, ?, 'codex', 'vault')",
                    (doc_id, f"doc{i}.md", body * (20 + i)),
                )
            for _ in range(_ACTIVATION_THRESHOLD - 2):
                c.execute(
                    "INSERT INTO score_distribution (raw_score, kind, created_at) "
                    "VALUES (0.0, 'combined', ?)",
                    (time.time(),),
                )

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        results = resp["result"]["results"]
        assert len(results) >= 3, "the pin needs enough hits to cross mid-response"
        confidences = [r["confidence"] for r in results]
        assert all(c is not None and c >= 0.8 for c in confidences), (
            f"every confidence must be calibrated against the same post-record "
            f"window; got {confidences} — a sub-0.8 value is a row that was "
            "calibrated before the window crossed the activation threshold "
            "(raw_blend) while later rows in the SAME response got percentile "
            "ranks"
        )


class TestHydeProbeIsCalibrationBlind:
    """Round-6 finding 2: the HyDE probe calibrated per-engine (db=self.db),"""

    def test_probe_confidence_never_sees_a_calibration_db(
        self, tmp_path, monkeypatch
    ):
        import minni.hyde as hyde
        import minni.scoring as scoring
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path, reranker_enabled=False,
                               hyde_enabled=True)
        monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: None))
        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        probed = []
        monkeypatch.setattr(
            hyde, "should_trigger_hyde",
            lambda results, **kw: (probed.append(len(results)), False)[1],
        )
        real = scoring.compute_confidence
        dbs_seen = []

        def _spy(rrf_score=None, cross_encoder_score=None, decay_factor=None,
                 db=None, record=False):
            dbs_seen.append(db)
            return real(rrf_score, cross_encoder_score, decay_factor, db=db,
                        record=record)

        monkeypatch.setattr(scoring, "compute_confidence", _spy)

        out = engine.retrieve(query="deployment rollback", expand=False)
        assert out, "the pin needs at least one hit for the probe to score"
        assert probed and probed[0] >= 1, "the HyDE probe must have run"
        assert dbs_seen and all(db is None for db in dbs_seen), (
            "the speculative HyDE probe must compute raw blends only; a "
            "non-None db means activation of that engine's window silently "
            "retunes when HyDE fires"
        )


class TestProvenanceReportsEffectiveDecay:
    """Round-6 finding 3: ranking, confidence, and the recorded calibration"""

    def test_provenance_decay_factor_matches_the_ranking_legs(
        self, tmp_path, monkeypatch, hermetic_principals
    ):
        _engine, db_obj, cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        correction_type = sorted(_engine._correction_types)[0]
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer, decay_score, "
                "page_type) VALUES ('c.md', 'codex', 'vault', 'knowledge', 0.01, ?)",
                (correction_type,),
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'c.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "expand": False, "depth": "chunk",
        })
        assert "error" not in resp
        results = resp["result"]["results"]
        assert results
        floor = float(cfg.correction_decay_floor)
        for r in results:
            assert r["provenance"]["decay_factor"] == pytest.approx(floor), (
                "provenance must report the effective decay every leg used, "
                "not the raw 0.01 the floor overrode"
            )

    def test_headline_decay_factor_matches_too(self, tmp_path, monkeypatch,
                                               hermetic_principals):
        _engine, db_obj, cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        correction_type = sorted(_engine._correction_types)[0]
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer, decay_score, "
                "page_type) VALUES ('c.md', 'codex', 'vault', 'knowledge', 0.01, ?)",
                (correction_type,),
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'c.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )
        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "expand": False, "depth": "headline",
        })
        assert "error" not in resp
        results = resp["result"]["results"]
        assert results
        floor = float(cfg.correction_decay_floor)
        for r in results:
            assert r["decay_factor"] == pytest.approx(floor)


class TestEveryTierCarriesTheCalibrationCarrier:
    """Round-7 finding 1: the headline branch hand-rolls its dict and dropped"""

    @pytest.mark.parametrize("depth", ["headline", "snippet", "chunk", "document"])
    def test_every_depth_tier_carries_the_calibration_carrier(self, tmp_path,
                                                              depth):
        engine, _db, _cfg = _make_engine(tmp_path)
        out = engine._apply_depth({"confidence_raw": 0.42, "confidence": 0.5},
                                  depth)
        assert out.get("confidence_raw") == pytest.approx(0.42), (
            f"depth={depth} must carry the GA4-1 carrier; a tier that drops it "
            "silently exempts itself from calibration"
        )

    def test_headline_search_feeds_the_window_and_pops_the_carrier(
        self, tmp_path, monkeypatch, hermetic_principals
    ):
        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge')"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )
        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "expand": False, "depth": "headline",
        })
        assert "error" not in resp
        results = resp["result"]["results"]
        assert results
        assert all("confidence_raw" not in r for r in results), (
            "the carrier must be popped at the boundary, not shipped"
        )
        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM score_distribution"
            ).fetchone()["n"]
        assert n == len(results), (
            "headline hits must feed the calibration window exactly like "
            "snippet hits; on the old code the tier silently recorded nothing"
        )


class TestFinalScoreClampsDecay:
    """Round-7 finding 2: _score_merged_doc stamped the clamped decay_applied"""

    def test_decay_above_one_cannot_promote_final_score(self, tmp_path):
        engine, _db, _cfg = _make_engine(tmp_path)
        d = {"rrf_score": 0.02, "decay_score": 2.0, "page_type": "note"}
        engine._score_merged_doc(d)
        assert d["decay_applied"] == pytest.approx(1.0)
        assert d["final_score"] == pytest.approx(0.02), (
            "final_score must multiply the clamped decay; the raw 2.0 turned "
            "a poison decay into a promotion channel on the non-rerank leg"
        )

    def test_negative_decay_clamps_to_zero_not_negative_score(self, tmp_path):
        engine, _db, _cfg = _make_engine(tmp_path)
        d = {"rrf_score": 0.02, "decay_score": -0.5, "page_type": "note"}
        engine._score_merged_doc(d)
        assert d["decay_applied"] == pytest.approx(0.0)
        assert d["final_score"] == pytest.approx(0.0)


class TestPostActivationEnvelopeAgrees:
    """Round-8 finding 1: formatting freezes recommended_action and rationale"""

    def test_recommended_action_and_rationale_track_calibrated_confidence(
        self, tmp_path, monkeypatch, hermetic_principals
    ):
        from minni.scoring import _ACTIVATION_THRESHOLD

        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        body = "the deployment rollback procedure is documented here. " * 20
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil, layer, decay_score) "
                "VALUES ('a.md', 'codex', 'vault', 'knowledge', 0.4)"
            )
            doc_id = c.lastrowid
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
                "VALUES (?, 'a.md', ?, 'codex', 'vault')",
                (doc_id, body),
            )
            for _ in range(_ACTIVATION_THRESHOLD):
                c.execute(
                    "INSERT INTO score_distribution (raw_score, kind, created_at) "
                    "VALUES (0.0, 'combined', ?)",
                    (time.time(),),
                )

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex",
            "expand": False, "depth": "snippet",
        })
        assert "error" not in resp
        results = resp["result"]["results"]
        assert results
        for r in results:
            conf = r.get("confidence")
            assert conf is not None and conf >= 0.8, (
                f"expected percentile-ranked confidence after activation; "
                f"got {conf}"
            )
            assert r.get("recommended_action") == "cite", (
                f"recommended_action must re-derive from calibrated "
                f"confidence={conf}; freeze-from-format left 'follow_up' "
                f"(raw < 0.2) while confidence was rewritten high"
            )
            rationale = r.get("rationale") or ""
            assert f"confidence {conf:.2f}" in rationale, (
                f"rationale must embed the calibrated confidence {conf:.2f}, "
                f"not the pre-calibration blend; got {rationale!r}"
            )


class TestEpisodicFtsBackfill:
    """PR #259 wired search_episodic into recall, but the index it searches was"""

    _PRE_TRIGGER = [
        ("task_start", "Starting: Build TurboQuant compression layer"),
        ("query", "Query: 'Research graph visualization options' → 3 results"),
        ("finding", "Successfully queried vault and retrieved relevant docs"),
        ("message", "[user] How do I fix the websocket error?"),
    ]

    def _db_with_pre_trigger_events(self, tmp_path):
        """A DB whose earliest events were written with the trigger absent —"""
        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_insert")
            for event_type, content in self._PRE_TRIGGER:
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('claude-code', ?, ?, ?)",
                    (event_type, content, time.time()),
                )
            c.execute(
                """CREATE TRIGGER trg_episodic_fts_insert
                   AFTER INSERT ON episodic_events
                   WHEN NEW.content IS NOT NULL
                   BEGIN
                       INSERT INTO episodic_fts(rowid, agent_id, content)
                       VALUES (NEW.event_id, NEW.agent_id, NEW.content);
                   END"""
            )
            c.execute(
                "INSERT INTO episodic_events"
                " (agent_id, event_type, content, created_at)"
                " VALUES ('claude-code', 'message', ?, ?)",
                ("[assistant] indexed by the live trigger", time.time()),
            )
        return db_obj, cfg

    def test_pre_trigger_events_are_unreachable_before_the_backfill(self, tmp_path):
        """The bug itself: the text is in episodic_events and MATCH finds"""
        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        with db_obj.cursor() as c:
            stored = c.execute(
                "SELECT COUNT(*) AS n FROM episodic_events"
                " WHERE content LIKE '%TurboQuant%'"
            ).fetchone()["n"]
            found = c.execute(
                "SELECT COUNT(*) AS n FROM episodic_fts"
                " WHERE episodic_fts MATCH 'TurboQuant'"
            ).fetchone()["n"]
        assert stored == 1
        assert found == 0, "precondition: the pre-trigger row must start unindexed"
        db_obj.close()

    def test_backfill_makes_pre_trigger_events_searchable(self, tmp_path):
        """Through search_episodic — the path recall actually uses — not"""
        from minni.episodic import reconcile_episodic_fts
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = self._db_with_pre_trigger_events(tmp_path)
        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

        assert engine.search_episodic("TurboQuant") == []

        result = reconcile_episodic_fts(db_obj._get_conn())
        assert result["inserted"] == len(self._PRE_TRIGGER)

        for term in ("TurboQuant", "websocket", "visualization"):
            hits = engine.search_episodic(term)
            assert hits, f"{term!r} is in episodic_events but recall cannot find it"
        db_obj.close()

    def test_backfill_is_idempotent(self, tmp_path):
        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        conn = db_obj._get_conn()
        reconcile_episodic_fts(conn)
        after_first = conn.execute("SELECT COUNT(*) FROM episodic_fts").fetchone()[0]

        assert reconcile_episodic_fts(conn)["inserted"] == 0
        assert conn.execute("SELECT COUNT(*) FROM episodic_fts").fetchone()[0] == after_first
        db_obj.close()

    def test_backfill_survives_a_null_event_id_in_the_index(self, tmp_path):
        """#311: episodic_fts is keyed by rowid (INTEGER PRIMARY KEY). Non-integer
        keys are rejected at write time, and a NULL rowid auto-assigns an
        unreferenced integer rowid that reconcile_episodic_fts sweeps as an orphan."""
        import sqlite3

        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        conn = db_obj._get_conn()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episodic_fts(rowid, agent_id, content)"
                " VALUES ('not-an-int', 'claude-code', 'invalid rowid')"
            )
        conn.execute(
            "INSERT INTO episodic_fts(rowid, agent_id, content)"
            " VALUES (NULL, 'claude-code', 'orphaned index row')"
        )

        result = reconcile_episodic_fts(conn)
        assert result["inserted"] == len(self._PRE_TRIGGER)
        assert result["removed"] == 1
        db_obj.close()

    def test_backfill_is_safe_on_a_schema_without_episodic_tables(self):
        """Migrations run against partial fixture schemas; the reconcile must"""
        import sqlite3

        from minni.episodic import reconcile_episodic_fts

        assert reconcile_episodic_fts(sqlite3.connect(":memory:")) == {
            "missing_before": 0,
            "inserted": 0,
            "removed": 0,
        }

    def test_migration_018_runs_the_backfill(self, tmp_path):
        """The repair ships as migration 018, so an existing install gets it on"""
        from minni.migrations import run_migrations

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        conn = db_obj._get_conn()
        conn.execute("DELETE FROM episodic_fts")
        conn.execute("DELETE FROM schema_migrations WHERE version = 18")
        conn.commit()

        run_migrations(conn)
        conn.commit()

        indexed = conn.execute(
            "SELECT COUNT(*) FROM episodic_fts WHERE episodic_fts MATCH 'TurboQuant'"
        ).fetchone()[0]
        assert indexed == 1, "migration 018 must index the pre-trigger events"
        db_obj.close()

    def test_coverage_report_exposes_the_episodic_gap(self, tmp_path):
        """R7 deliverable 2: embedding_coverage compared documents to vectors"""
        from minni.backfill import embedding_coverage, episodic_index_coverage
        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)

        gapped = episodic_index_coverage(db_obj)
        assert gapped["episodic_events_total"] == len(self._PRE_TRIGGER) + 1
        assert gapped["episodic_events_indexed"] == 1
        assert gapped["episodic_events_missing_index"] == len(self._PRE_TRIGGER)
        assert gapped["episodic_index_ratio"] < 1.0

        reconcile_episodic_fts(db_obj._get_conn())

        healed = episodic_index_coverage(db_obj)
        assert healed["episodic_events_missing_index"] == 0
        assert healed["episodic_index_ratio"] == 1.0

        assert "episodic_index_ratio" in embedding_coverage(db_obj)
        db_obj.close()

    def test_coverage_excludes_recall_traces_from_the_ratio(self, tmp_path):
        """Recall traces are observability rows, not memory. Folding thousands"""
        from minni.backfill import episodic_index_coverage
        from minni.retrieval import RetrievalEngine

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        with db_obj.cursor() as c:
            for i in range(5):
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('claude-code', 'recall', ?, ?)",
                    (f"trace {i}", time.time()),
                )

        coverage = episodic_index_coverage(db_obj)
        assert coverage["episodic_observability_events"] == 5
        assert coverage["episodic_events_total"] == len(self._PRE_TRIGGER) + 1, (
            "trace rows must stay out of the denominator"
        )
        db_obj.close()

    def test_the_non_memory_type_list_has_one_definition(self):
        """Identity, not equality. Asserting `== ('recall',)` would survive"""
        import minni.episodic as episodic
        from minni.retrieval import RetrievalEngine

        assert (
            RetrievalEngine.EPISODIC_NON_MEMORY_TYPES
            is episodic.NON_MEMORY_EVENT_TYPES
        ), "the search filter must READ the shared list, not copy its value"

    def test_recent_activity_filters_traces_through_the_shared_list(
        self, tmp_path, monkeypatch
    ):
        """recall.handle_read's Recent Activity query had its own hardcoded"""
        import minni.episodic as episodic
        import minni.minnid as minnid

        db_obj, _ = _make_db(tmp_path)
        now = time.time()
        with db_obj.cursor() as c:
            for event_type, content in (
                ("message", "genuine agent message"),
                ("recall", "recall trace noise"),
                ("audit_probe", "probe event text"),
            ):
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('codex', ?, ?, ?)",
                    (event_type, content, now),
                )
        monkeypatch.setattr(minnid, "MinniDB", lambda: db_obj)

        context = minnid._handle_read({"agent_id": "codex", "limit": 5}, 1)
        context = context["result"]["context"]
        assert "genuine agent message" in context
        assert "recall trace noise" not in context, "traces must stay out"
        assert "probe event text" in context

        monkeypatch.setattr(
            episodic,
            "NON_MEMORY_EVENT_TYPES",
            episodic.NON_MEMORY_EVENT_TYPES + ("audit_probe",),
        )
        context = minnid._handle_read({"agent_id": "codex", "limit": 5}, 1)
        context = context["result"]["context"]
        assert "genuine agent message" in context
        assert "probe event text" not in context, (
            "Recent Activity kept its own copy of the trace list — extending "
            "NON_MEMORY_EVENT_TYPES did not move this filter"
        )
        db_obj.close()

    def test_backfill_skips_null_content_events(self, tmp_path):
        """The INSERT's `content IS NOT NULL` must match the trigger's"""
        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        conn = db_obj._get_conn()
        conn.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES ('claude-code', 'message', NULL, ?)",
            (time.time(),),
        )

        result = reconcile_episodic_fts(conn)
        assert result["inserted"] == result["missing_before"], (
            "the count and the insert must use the same predicate"
        )
        assert result["inserted"] == len(self._PRE_TRIGGER)
        assert conn.execute(
            "SELECT COUNT(*) FROM episodic_fts WHERE content IS NULL"
        ).fetchone()[0] == 0
        db_obj.close()

    def test_backfill_no_ops_when_only_one_episodic_table_exists(self, tmp_path):
        """The half-schema case is the one the guard exists for — fixtures in"""
        import sqlite3

        from minni.episodic import reconcile_episodic_fts

        conn = sqlite3.connect(str(tmp_path / "half.db"))
        conn.execute(
            "CREATE TABLE episodic_events ("
            " event_id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT,"
            " event_type TEXT, content TEXT, created_at REAL)"
        )
        conn.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES ('a', 'message', 'text', 1.0)"
        )

        assert reconcile_episodic_fts(conn) == {
            "missing_before": 0, "inserted": 0, "removed": 0,
        }
        conn.close()

    def test_coverage_counts_orphaned_index_rows(self, tmp_path):
        """episodic_fts_orphans is the compensating control migration 018 cites"""
        from minni.backfill import episodic_index_coverage

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        assert episodic_index_coverage(db_obj)["episodic_fts_orphans"] == 0

        conn = db_obj._get_conn()
        conn.execute(
            "INSERT INTO episodic_fts(rowid, agent_id, content)"
            " VALUES (999999, 'claude-code', 'index row for a deleted event')"
        )
        conn.execute(
            "INSERT INTO episodic_fts(rowid, agent_id, content)"
            " VALUES (NULL, 'claude-code', 'index row with no id at all')"
        )

        coverage = episodic_index_coverage(db_obj)
        assert coverage["episodic_fts_orphans"] == 2, (
            "both the dangling id and the NULL id are unreachable index rows"
        )
        assert coverage["episodic_events_indexed"] == 1
        db_obj.close()

    def test_coverage_failure_reports_unknown_not_empty(self, tmp_path):
        """A broken coverage query must not read as a healthy empty log. The"""
        from minni.backfill import episodic_index_coverage

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        db_obj._get_conn().execute("DROP TABLE episodic_fts")

        coverage = episodic_index_coverage(db_obj)
        assert "episodic_error" in coverage, "the failure must be stated"
        for key in (
            "episodic_events_total",
            "episodic_events_indexed",
            "episodic_events_missing_index",
            "episodic_index_ratio",
            "episodic_observability_events",
            "episodic_fts_orphans",
        ):
            assert key in coverage, f"{key} dropped — unknown reads as empty"
            assert coverage[key] is None, f"{key} must be None on failure, not a count"
        db_obj.close()

    def test_episodic_coverage_survives_broken_document_coverage(self, tmp_path):
        """The boundary has to hold both ways. Migration 018's warning promises"""
        from minni.backfill import embedding_coverage

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        db_obj._get_conn().execute("DROP TABLE chunk_embeddings")

        coverage = embedding_coverage(db_obj)
        assert "error" in coverage, "the document-side failure must be stated"
        assert coverage["episodic_events_total"] == len(self._PRE_TRIGGER) + 1, (
            "episodic coverage must survive a document-coverage failure"
        )
        assert coverage["episodic_index_ratio"] is not None
        db_obj.close()

    def test_a_failed_backfill_does_not_freeze_the_migration_ladder(self, tmp_path):
        """_flush_batch runs all pending migrations in one transaction, so an"""
        import sqlite3

        import minni.episodic as episodic_mod
        from minni.migrations import run_migrations

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        conn = db_obj._get_conn()
        conn.execute("DELETE FROM schema_migrations WHERE version = 18")
        conn.commit()

        def _boom(_conn):
            raise sqlite3.OperationalError(
                "table episodic_fts has no column named event_id"
            )

        monkey = episodic_mod.reconcile_episodic_fts
        episodic_mod.reconcile_episodic_fts = _boom
        try:
            run_migrations(conn)
            conn.commit()
        finally:
            episodic_mod.reconcile_episodic_fts = monkey

        applied = {
            v for (v,) in conn.execute("SELECT version FROM schema_migrations")
        }
        assert 18 in applied, (
            "a failed data repair must not block the schema ladder — 019 and "
            "later would batch with it and roll back forever"
        )

        run_migrations(conn)
        conn.commit()
        still_missing = conn.execute(
            "SELECT COUNT(*) FROM episodic_fts WHERE episodic_fts MATCH 'TurboQuant'"
        ).fetchone()[0]
        assert still_missing == 0, (
            "migrations re-ran the repair — if that ever becomes true, the "
            "sweep-based retry is no longer the only healing path and this "
            "test's premise needs revisiting"
        )
        db_obj.close()

    def test_backfill_preserves_agent_scope(self, tmp_path):
        """search_episodic filters on ef.agent_id — the FTS copy, not the"""
        from minni.episodic import reconcile_episodic_fts
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_insert")
            for agent in ("claude-code", "codex"):
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES (?, 'message', ?, ?)",
                    (agent, f"{agent} says TurboQuant", time.time()),
                )

        reconcile_episodic_fts(db_obj._get_conn())
        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

        for agent in ("claude-code", "codex"):
            hits = engine.search_episodic("TurboQuant", agent_id=agent)
            assert len(hits) == 1, (
                f"agent-scoped recall — the call recall actually makes — found "
                f"{len(hits)} rows for {agent}"
            )
            assert hits[0]["agent_id"] == agent
            assert agent in hits[0]["content"], "wrong agent's row returned"
        db_obj.close()

    def test_coverage_measures_reachability_not_mere_presence(self, tmp_path):
        """An index row under the wrong agent_id is unreachable through the"""
        from minni.backfill import episodic_index_coverage

        db_obj, _ = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_insert")
            c.execute(
                "INSERT INTO episodic_events"
                " (agent_id, event_type, content, created_at)"
                " VALUES ('claude-code', 'message', 'scoped text', ?)",
                (time.time(),),
            )
            event_id = c.lastrowid
            c.execute(
                "INSERT INTO episodic_fts(rowid, agent_id, content)"
                " VALUES (?, 'someone-else', 'scoped text')",
                (event_id,),
            )

        coverage = episodic_index_coverage(db_obj)
        assert coverage["episodic_events_indexed"] == 0, (
            "an index row under the wrong agent is not coverage"
        )
        assert coverage["episodic_index_ratio"] == 0.0
        db_obj.close()

    def test_a_failed_sweep_reconcile_releases_the_write_lock(self, tmp_path):
        """The rollback on the sweep's failure path is load-bearing, not tidy"""
        import sqlite3

        import minni.episodic as episodic_mod
        import minni.minnid as minnid

        db_obj, cfg = self._db_with_pre_trigger_events(tmp_path)
        conn = db_obj._get_conn()
        conn.execute("DELETE FROM episodic_fts")
        conn.commit()

        def _write_then_fail(c):
            c.execute(
                "INSERT INTO episodic_fts(rowid, agent_id, content)"
                " VALUES (424242, 'claude-code', 'half-written row')"
            )
            raise sqlite3.OperationalError("disk I/O error mid-backfill")

        from minni.db import MinniDB

        monkey_cfg = minnid.DEFAULT_CONFIG
        shared_original = MinniDB.shared
        episodic_original = episodic_mod.reconcile_episodic_fts
        minnid.DEFAULT_CONFIG = cfg
        episodic_mod.reconcile_episodic_fts = _write_then_fail
        MinniDB.shared = staticmethod(lambda *a, **k: db_obj)
        try:
            results = minnid._backfill_sweep_once()
        finally:
            episodic_mod.reconcile_episodic_fts = episodic_original
            MinniDB.shared = shared_original
            minnid.DEFAULT_CONFIG = monkey_cfg

        assert "error" in results["episodic_fts"], "the failure must be reported"
        assert conn.in_transaction is False, (
            "the failed sweep left an open write transaction — it holds the "
            "write lock until the next sweep (3600s by default)"
        )

        other = sqlite3.connect(cfg.db_path, timeout=5)
        try:
            assert other.execute(
                "SELECT COUNT(*) FROM episodic_fts WHERE episodic_fts MATCH 'half'"
            ).fetchone()[0] == 0, "the partial write must be rolled back"
            other.execute(
                "INSERT INTO episodic_events"
                " (agent_id, event_type, content, created_at)"
                " VALUES ('probe', 'message', 'writer probe', 1.0)"
            )
            other.commit()
        finally:
            other.close()
        db_obj.close()

    def test_empty_episodic_log_reports_no_ratio_not_perfect_coverage(self, tmp_path):
        """None, not 1.0. Claiming perfect coverage over zero rows is the"""
        from minni.backfill import episodic_index_coverage

        db_obj, _ = _make_db(tmp_path)

        coverage = episodic_index_coverage(db_obj)
        assert coverage["episodic_events_total"] == 0
        assert coverage["episodic_index_ratio"] is None, (
            "an empty episodic log has no coverage to report; 1.0 would claim "
            "perfect coverage over nothing"
        )
        db_obj.close()

    def test_coverage_counts_every_agent(self, tmp_path):
        """The metric is machine-wide, not per-agent. Every fixture above uses a"""
        from minni.backfill import episodic_index_coverage
        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_insert")
            for agent, count in (("claude-code", 2), ("forge", 3)):
                for i in range(count):
                    c.execute(
                        "INSERT INTO episodic_events"
                        " (agent_id, event_type, content, created_at)"
                        " VALUES (?, 'message', ?, ?)",
                        (agent, f"{agent} memory {i}", time.time()),
                    )

        gapped = episodic_index_coverage(db_obj)
        assert gapped["episodic_events_total"] == 5, (
            "the denominator must span every agent, not just one "
            f"(got {gapped['episodic_events_total']})"
        )
        assert gapped["episodic_events_indexed"] == 0

        reconcile_episodic_fts(db_obj._get_conn())

        healed = episodic_index_coverage(db_obj)
        assert healed["episodic_events_total"] == 5
        assert healed["episodic_events_indexed"] == 5, (
            "the backfill must cover every agent, and the metric must see it"
        )
        assert healed["episodic_index_ratio"] == 1.0
        db_obj.close()

    def test_coverage_does_not_invent_a_gap_for_null_content_events(self, tmp_path):
        """A NULL-content event can never be indexed — the trigger skips it and"""
        from minni.backfill import episodic_index_coverage
        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = self._db_with_pre_trigger_events(tmp_path)
        conn = db_obj._get_conn()
        conn.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES ('claude-code', 'message', NULL, ?)",
            (time.time(),),
        )
        reconcile_episodic_fts(conn)

        coverage = episodic_index_coverage(db_obj)
        assert coverage["episodic_index_ratio"] == 1.0, (
            f"fully repaired DB must report full coverage, got {coverage}"
        )
        db_obj.close()

    def test_a_failed_migration_repair_is_retried_by_the_backfill_sweep(self, tmp_path):
        """Migration 018 swallows its own exceptions so a failed repair cannot"""
        import minni.minnid as minnid

        db_obj, cfg = self._db_with_pre_trigger_events(tmp_path)
        conn = db_obj._get_conn()
        conn.execute("DELETE FROM episodic_fts")
        conn.commit()

        monkey_cfg = minnid.DEFAULT_CONFIG
        minnid.DEFAULT_CONFIG = cfg
        try:
            from minni.db import MinniDB

            original = MinniDB.shared
            MinniDB.shared = staticmethod(lambda *a, **k: db_obj)
            try:
                minnid._backfill_sweep_once()
            finally:
                MinniDB.shared = original
        finally:
            minnid.DEFAULT_CONFIG = monkey_cfg

        assert conn.in_transaction is False, (
            "the sweep left an open write transaction — it blocks every other "
            "daemon writer until some unrelated code path happens to commit"
        )

        import sqlite3

        other = sqlite3.connect(cfg.db_path, timeout=5)
        try:
            indexed = other.execute(
                "SELECT COUNT(*) FROM episodic_fts"
                " WHERE episodic_fts MATCH 'TurboQuant'"
            ).fetchone()[0]
            assert indexed == 1, (
                "the sweep must COMMIT its reconcile — rows only the sweep's "
                "own connection can see have not repaired anything"
            )
            other.execute(
                "INSERT INTO episodic_events"
                " (agent_id, event_type, content, created_at)"
                " VALUES ('probe', 'message', 'writer probe', 1.0)"
            )
            other.commit()
        finally:
            other.close()
        db_obj.close()


class TestSchedulerRunnersActuallySweep:
    """Scheduling a coroutine that returns immediately is the same dead channel"""

    def _drive_one_pass(self, monkeypatch, runner, sweep_attr):
        """Run a single iteration of *runner*, then break out of its while-loop."""
        import asyncio

        import minni.minnid as minnid

        calls = []

        def _sweep():
            calls.append(True)
            raise asyncio.CancelledError

        async def _no_sleep(_delay):
            return None

        async def _inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(minnid, sweep_attr, _sweep)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(asyncio, "to_thread", _inline)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runner())
        return calls

    def test_backfill_runner_calls_the_sweep(self, monkeypatch):
        import minni.minnid as minnid

        calls = self._drive_one_pass(
            monkeypatch, minnid._backfill_runner, "_backfill_sweep_once"
        )
        assert calls, "_backfill_runner scheduled but never swept"

    def test_decay_runner_calls_the_sweep(self, monkeypatch):
        import minni.minnid as minnid

        calls = self._drive_one_pass(
            monkeypatch, minnid._decay_runner, "_decay_sweep_once"
        )
        assert calls, "_decay_runner scheduled but never swept"


class TestTraceReapingFollowsTheSharedList:
    """trim_recall_traces bounds the one table that grows without limit. It"""

    def _episodic(self, tmp_path):
        from minni.episodic import EpisodicMemory

        db_obj, cfg = _make_db(tmp_path)
        return EpisodicMemory(db_obj, cfg), db_obj

    def _seed(self, db_obj, age_seconds):
        now = time.time()
        with db_obj.cursor() as c:
            for event_type in ("recall", "audit_probe", "message"):
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('claude-code', ?, ?, ?)",
                    (event_type, f"{event_type} text", now - age_seconds),
                )

    def _surviving(self, db_obj):
        with db_obj.cursor() as c:
            return {
                r["event_type"]
                for r in c.execute("SELECT event_type FROM episodic_events").fetchall()
            }

    def test_traces_are_reaped_and_real_memory_is_not(self, tmp_path):
        episodic, db_obj = self._episodic(tmp_path)
        self._seed(db_obj, age_seconds=999_999)

        episodic.trim_recall_traces(max_age_seconds=60)

        survivors = self._surviving(db_obj)
        assert "recall" not in survivors, "expired traces must be reaped"
        assert "message" in survivors, "agent memory must never be reaped here"
        db_obj.close()

    def test_a_new_trace_type_is_reaped_too(self, tmp_path, monkeypatch):
        """The whole point of the shared list: adding a type must move the"""
        import minni.episodic as episodic_mod

        episodic, db_obj = self._episodic(tmp_path)
        self._seed(db_obj, age_seconds=999_999)
        monkeypatch.setattr(
            episodic_mod,
            "NON_MEMORY_EVENT_TYPES",
            episodic_mod.NON_MEMORY_EVENT_TYPES + ("audit_probe",),
        )

        episodic.trim_recall_traces(max_age_seconds=60)

        survivors = self._surviving(db_obj)
        assert "audit_probe" not in survivors, (
            "trim_recall_traces kept its own copy of the trace list — a new "
            "trace type is filtered everywhere but reaped nowhere"
        )
        assert "message" in survivors
        db_obj.close()

    def test_fresh_traces_survive_the_ttl(self, tmp_path):
        episodic, db_obj = self._episodic(tmp_path)
        self._seed(db_obj, age_seconds=0)

        episodic.trim_recall_traces(max_age_seconds=604800)

        assert "recall" in self._surviving(db_obj), "unexpired traces must stay"
        db_obj.close()


@pytest.mark.parametrize("cold", [False, True])
def test_calibration_distinguishes_unavailable_encoder_from_deadline_skip(
    tmp_path, monkeypatch, hermetic_principals, cold,
):
    """Real deadline policy excludes cold-load skips but feeds encoder-down FTS."""
    import minni.models as models
    from minni.retrieval import RetrievalEngine

    _, db_obj, _ = _patch_engine_and_writeback(tmp_path, monkeypatch)
    if cold:
        models.get_embedder.cache_clear()
    encoder_down = []
    original = RetrievalEngine._note_vector_model_down

    def note_down(engine):
        encoder_down.append(True)
        original(engine)

    monkeypatch.setattr(RetrievalEngine, "_note_vector_model_down", note_down)
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, agent, sigil, layer) "
            "VALUES ('a.md', 'codex', 'vault', 'knowledge')"
        )
        doc_id = c.lastrowid
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
            "VALUES (?, 'a.md', 'deployment rollback procedure', 'codex', 'vault')",
            (doc_id,),
        )
    response = _dispatch("search", {
        "query": "deployment rollback", "agent_id": "codex", "expand": False,
    })
    assert "error" not in response
    results = response["result"]["results"]
    assert results
    assert bool(encoder_down) is not cold
    with db_obj.cursor() as c:
        recorded = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
    assert recorded == (0 if cold else len(results))
    assert all("confidence_raw" not in row for row in results)


def test_phase5_mixin_reexports_and_monkeypatch_seams(tmp_path, monkeypatch):
    """Phase 5 seam parity: module-level re-exports and dynamic _ret.* lookups."""
    from types import SimpleNamespace
    import minni.retrieval as retrieval_mod
    from minni.retrieval import RetrievalEngine

    assert callable(getattr(retrieval_mod, "_strip_frontmatter", None))
    assert callable(getattr(retrieval_mod, "_tokenize_trie_text", None))
    assert hasattr(retrieval_mod, "sqlite3")
    assert callable(getattr(RetrievalEngine, "_apply_post_rerank_confidence_floor", None))

    engine, db_obj, _ = _make_engine(tmp_path)
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO documents (path, agent, sigil, layer) "
            "VALUES ('seam.md', 'codex', 'vault', 'knowledge')"
        )
        doc_id = c.lastrowid
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) "
            "VALUES (?, 'seam.md', '---\ntitle: Seam\n---\nseam_token_alpha', 'codex', 'vault')",
            (doc_id,),
        )

    monotonic_calls = []
    perf_calls = []
    strip_calls = []

    fake_time = SimpleNamespace(
        monotonic=lambda: (monotonic_calls.append(1), 500.0)[1],
        perf_counter=lambda: (perf_calls.append(1), float(len(perf_calls)))[1],
        time=lambda: 1700000000.0,
        sleep=lambda _s: None,
    )
    monkeypatch.setattr(retrieval_mod, "time", fake_time)
    orig_strip = retrieval_mod._strip_leading_frontmatter
    monkeypatch.setattr(
        retrieval_mod,
        "_strip_leading_frontmatter",
        lambda text: (strip_calls.append(text), orig_strip(text))[1],
    )

    engine._ensure_trie_loaded()
    assert monotonic_calls, "TrieIndexMixin._ensure_trie_loaded must resolve time via minni.retrieval"

    fts_hits = engine._fts_search("seam_token_alpha", limit=5)
    assert fts_hits and strip_calls, "FtsSearchMixin._fts_search must resolve _strip_leading_frontmatter via minni.retrieval"

    monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: _StubEmbedder()))
    monkeypatch.setattr(retrieval_mod, "should_skip_cold_model_load", lambda *_a: False)
    vec = engine._encode_query("seam_token_alpha")
    assert vec.size == 384 and len(perf_calls) >= 2, (
        "SemanticRerankMixin._encode_query must resolve time.perf_counter via minni.retrieval"
    )

    kept = engine._apply_post_rerank_confidence_floor(
        [
            {"doc_id": 1, "fts_rank": 1, "raw_rerank_score": -5.0, "similarity": 0.1},
            {"doc_id": 2, "fts_rank": None, "raw_rerank_score": -5.0, "similarity": 0.9},
        ],
        fts_results=[{"doc_id": 1}],
        limit=5,
    )
    assert [r["doc_id"] for r in kept] == [1]
    db_obj.close()


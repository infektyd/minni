"""
R7 retrieval-integrity tests — issue #225 plus the folded gap-audit findings.

Each test fails on the OLD behaviour, so reverting the corresponding fix turns
it red. Covers:

  GA4-2   decay reaches the DEFAULT (reranker) ordering, not just final_score
  #225-R1 the episodic layer is reachable from the search RPC
  GA6-1   vault_ingest infers the layer instead of hardcoding 'knowledge'
  GA7-2   seed_identity / wiki_indexer stamp layer on their INSERTs
  GA1-1   learnings with a NULL embedding are backfillable and counted
  #225-R6 the document/vector coverage ratio is surfaced in health
  GA4-1   score calibration is fed by the retrieval path (record_score wired)
  GA4-4   a FAISS index restored from disk can still rebuild (raw vectors kept)

All state is tmp_path-backed; no live ~/.minni access.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path, **cfg_overrides):
    """(SovereignDB, SovereignConfig) over a temporary SQLite file."""
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "test.db"), **cfg_overrides)
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
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


# Hermetic principal setup — same pattern as test_correction_reinjection.py, so
# the dispatch tests below exercise the real search RPC with no live ~/.minni.
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
    """Wire ONE test DB into both minnid singletons so dispatch tests hit the
    production search RPC end to end."""
    import minni.minnid as minnid
    import minni.writeback as wb_mod
    from minni.retrieval import RetrievalEngine
    from minni.writeback import WriteBackMemory

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


# ---------------------------------------------------------------------------
# #225-R1 — the episodic layer must be reachable, not merely advertised
# ---------------------------------------------------------------------------

class TestEpisodicIsReachable:
    """search_episodic had zero production call sites while the `layer` enum
    and BOOT_RECALL_LAYERS both advertised episodic. 2,178 captured events
    were unretrievable: document retrieval cannot reach episodic_events."""

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
        """The daemon writes a TTL'd `recall` event per search for observability.
        Surfacing those would make episodic search return a log of its own past
        searches instead of memory."""
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


# ---------------------------------------------------------------------------
# GA4-1 — score calibration must be fed, or it is permanently inert
# ---------------------------------------------------------------------------

class TestScoreCalibrationIsWired:
    """record_score had zero production call sites, so score_distribution held
    0 rows, _calibrate always tripped its `total < 10` guard, and calibration
    returned the raw score forever. Decision: WIRE it — the machinery is
    complete and was inert only for want of one call."""

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
        """Speculative paths (the HyDE probe) must not inflate the window with
        scores no caller ever saw."""
        from minni.scoring import compute_confidence

        db_obj, _cfg = _make_db(tmp_path)
        compute_confidence(0.02, 1.0, 1.0, db=db_obj)

        with db_obj.cursor() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM score_distribution"
            ).fetchone()["n"]
        assert n == 0

    def test_recorded_value_is_pre_calibration(self, tmp_path):
        """Recording the calibrated output would feed the distribution its own
        result and make the percentiles converge on themselves."""
        from minni.scoring import compute_confidence

        db_obj, _cfg = _make_db(tmp_path)
        # Seed enough samples that _calibrate is live (its guard is total < 10).
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
        """Behavioral, not a source grep: drive the real search RPC and assert
        score_distribution gained rows. A grep for `record=True` would stay green
        over dead code."""
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
    """GA4-1 guardrail. Wiring record_score means the window fills during normal
    retrieval, and crossing the threshold changes what `confidence` MEANS for
    every caller — raw blend before, percentile rank after. A feature that
    switches semantics on a row count with nothing observable is the same
    silent-degrade class this audit exists to remove."""

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
        """At/above the threshold the SAME inputs yield a different number —
        this is the semantic shift the surface exists to announce."""
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
        """The reported boundary must be the one _calibrate actually uses; a
        surface that disagreed with the behaviour would be worse than none."""
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
        """Counts and labels only — it must stay readable pre-identity, like
        the other aggregate liveness fields."""
        from minni.minnid_runtime.health import (
            _HEALTH_REPORT_SENSITIVE_KEYS,
            redact_health_report_for_recovery,
        )

        assert "score_calibration" not in _HEALTH_REPORT_SENSITIVE_KEYS
        report = {"score_calibration": {"active": True, "window_rows": 42}}
        assert redact_health_report_for_recovery(report)["score_calibration"] == {
            "active": True, "window_rows": 42,
        }


# ---------------------------------------------------------------------------
# #225-R6 / GA1-1 — the vector gap needs a retry AND a visible ratio
# ---------------------------------------------------------------------------

class _StubEmbedder:
    """Deterministic encoder — the backfill only needs a vector, not a good one."""

    def encode(self, text):
        import numpy as np

        vec = np.zeros(384, dtype="float32")
        vec[len(text) % 384] = 1.0
        return vec


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
            # Only one of the four has a vector.
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
        """Claiming 100% coverage for zero documents is exactly the
        health-signal overstatement this audit exists to remove."""
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
        """Never fix a signal by suppressing it: an unbackfillable document is
        counted, not quietly treated as done. After grok-review finding 2 it is
        excluded from the BATCH (so it cannot wedge the drain) but still
        counted, and still reported missing by embedding_coverage."""
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
        """grok-review finding 1: committing chunk_embeddings rows is not enough.
        _ensure_faiss_loaded early-returns while count > 0, so a WARM daemon
        never sees rows written underneath it — coverage would climb while the
        documents stayed invisible to the semantic leg until a restart."""
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

        # The ids handed out must be the real chunk_ids a search resolves.
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
        """The end-to-end form of grok-review finding 1: a REAL warm FAISSIndex
        behind a real RetrievalEngine must grow by the backfilled chunks, with
        no process restart and no cold reload."""
        import numpy as np

        from minni.faiss_index import FAISSIndex
        from minni.backfill import backfill_document_vectors
        from minni.retrieval import RetrievalEngine

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)

        # A warm index: count > 0 is exactly what makes _ensure_faiss_loaded
        # early-return and never notice new DB rows.
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
        """grok-review finding 2: taking the LIMIT head unfiltered let >=limit
        contentless documents occupy every batch forever, so recoverable
        documents behind them never entered one — a bounded drain silently
        becomes a stuck queue."""
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        body = "# Title\n\n" + ("the deployment rollback procedure. " * 40)
        with db_obj.cursor() as c:
            # Three contentless documents FIRST (lower doc_ids), then a good one.
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

        # A batch smaller than the contentless backlog: the old query would
        # have returned only the three empties and made zero progress forever.
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
        """grok-review finding 3: encoding inside db.transaction() held
        BEGIN IMMEDIATE across every model.encode call, blocking live writers
        for the encode duration rather than the INSERT duration."""
        import inspect

        from minni.backfill import backfill_document_vectors

        src = inspect.getsource(backfill_document_vectors)
        encode_pos = src.index("model.encode")
        txn_pos = src.index("with db.transaction()")
        assert encode_pos < txn_pos, (
            "all chunk vectors must be encoded before the write transaction opens"
        )

    def test_backfill_covers_every_index_like_decay_does(self, tmp_path, monkeypatch):
        """grok-review finding 5: draining only the shared index while decay
        covered every index would leave any vault-side gap undrained forever."""
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
        """grok-review finding 4: seed_identity skips already-seeded agents, so
        the writer fix never revisits an existing envelope. An envelope seeded
        after migration 004 kept layer=NULL and read back as KNOWLEDGE — the one
        layer it must never be."""
        from minni.migrations import run_migrations

        db_obj, _cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO documents (path, agent, sigil, whole_document, layer)
                   VALUES ('SOUL.md', 'identity:codex', 'C', 1, NULL)"""
            )
            envelope = c.lastrowid
            # A non-whole-document row with the same agent prefix must NOT be
            # promoted — whole_document is half the trusted rule.
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

    def test_daemon_schedules_the_backfill(self):
        import inspect

        import minni.minnid as minnid

        assert hasattr(minnid, "_backfill_runner")
        src = inspect.getsource(minnid.main)
        assert "_backfill_runner()" in src, (
            "the degraded status was logged but no retry was queued — a log "
            "line is not a queue"
        )
        assert "backfill_task.cancel()" in src

    def test_health_report_declares_the_coverage_field(self):
        import inspect

        import minni.minnid_runtime.health as health

        src = inspect.getsource(health.handle_health_report)
        assert "embedding_coverage" in src, (
            "health never compared document count against vector count, so the "
            "gap was invisible to every status surface"
        )


# ---------------------------------------------------------------------------
# GA4-4 — a disk-restored FAISS index must still be rebuildable
# ---------------------------------------------------------------------------

class TestFaissWarmStartRebuild:
    """load_from_disk restored the index but set _vectors = [], and every
    rebuild path is guarded on _vectors — so after a warm start rebuild() was
    a silent no-op and chunks removed since the snapshot stayed searchable."""

    def _index(self, tmp_path):
        from minni.config import SovereignConfig
        from minni.faiss_index import FAISSIndex

        cfg = SovereignConfig(db_path=str(tmp_path / "f.db"))
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

        # Simulate exactly what load_from_disk leaves behind: a live index with
        # id maps, and no raw vectors.
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
        """Never fix a signal by suppressing it: if reconstruction is
        impossible the skip must be visible, not silent."""
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


# ---------------------------------------------------------------------------
# GA6-1 / GA7-2 — every document writer must stamp the layer it infers
# ---------------------------------------------------------------------------

class TestLayerStamping:
    """Three writers disagreed with indexer._infer_layer: vault_ingest
    hardcoded 'knowledge', and seed_identity + wiki_indexer omitted layer
    entirely (NULL, masked by COALESCE(layer,'knowledge') on read)."""

    def test_vault_ingest_stamps_the_inferred_layer_on_disk(self, tmp_path,
                                                            monkeypatch):
        """GA6-1, behaviorally: run the real ingest over an artifact-typed page
        and read the stored rows back. A source grep for `_infer_layer` would
        stay green even if the value never reached the INSERT."""
        import sqlite3

        import minni.db as db_mod
        import minni.models as models
        from minni.afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID
        from minni.afm_passes.vault_ingest import run as run_vault_ingest
        from minni.config import SovereignConfig
        from minni.vault_index import vault_index_paths

        class _FakeEmbedder:
            def encode(self, text):
                import numpy as np

                vec = np.zeros(384, dtype="float32")
                vec[sum(text.encode("utf-8")) % 384] = 1.0
                return vec

        monkeypatch.setattr(models, "get_embedder", lambda: _FakeEmbedder())

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

        cfg = SovereignConfig(
            db_path=str(tmp_path / "shared" / "minni.db"),
            vault_path=str(tmp_path / "shared-vault"),
            writeback_enabled=False,
        )
        old = db_mod._migrations_run
        db_mod._migrations_run = False
        try:
            shared = db_mod.SovereignDB(cfg)
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
        """wiki_indexer's agent_tag can come from untrusted page frontmatter, so
        the identity layer must be unreachable there regardless of what a file
        declares."""
        from minni.indexer import VaultIndexer

        assert VaultIndexer._infer_layer(
            agent="identity:codex", page_type="concept", whole_document=0
        ) != "identity"

    def test_wiki_indexer_stamps_layer_on_disk(self, tmp_path, monkeypatch):
        """GA7-2, behaviorally: index a real artifact-typed wiki page and read
        the stored layer back. The old writer omitted the column entirely,
        leaving NULL — masked on read by COALESCE(layer, 'knowledge')."""
        import minni.models as models
        from minni.wiki_indexer import WikiIndexer

        class _FakeEmbedder:
            def encode(self, text):
                import numpy as np

                vec = np.zeros(384, dtype="float32")
                vec[sum(text.encode("utf-8")) % 384] = 1.0
                return vec

        monkeypatch.setattr(models, "get_embedder", lambda: _FakeEmbedder())
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
        """A session page's agent_tag comes from untrusted on-disk frontmatter.
        Even declaring `agent: identity:codex` must not yield an identity row."""
        import minni.models as models
        from minni.wiki_indexer import WikiIndexer

        class _FakeEmbedder:
            def encode(self, text):
                import numpy as np

                vec = np.zeros(384, dtype="float32")
                vec[sum(text.encode("utf-8")) % 384] = 1.0
                return vec

        monkeypatch.setattr(models, "get_embedder", lambda: _FakeEmbedder())
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

    def test_seed_identity_stamps_the_identity_layer(self):
        """STRUCTURAL, deliberately — and the reason is worth stating rather
        than leaving as an unexplained weaker test. seed_identity.seed_identity()
        is a top-level script bound to a module-level DB_PATH under the real
        ~/.openclaw, and it reads each agent's SOUL.md/IDENTITY.md from live
        home directories. Driving it behaviorally would write to live machine
        state, which this campaign forbids.

        So this asserts the two SQL statements carry the layer, and the
        migration test below covers the stored-row half behaviorally — an
        envelope that reaches the DB with layer NULL is repaired there.
        """
        import ast
        import inspect

        import minni.seed_identity as si

        src = inspect.getsource(si.seed_identity)
        # Parse rather than substring-match, so a stray 'identity' in a comment
        # or log line cannot make this pass over an unstamped INSERT.
        statements = [
            node.value
            for node in ast.walk(ast.parse(src.strip()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        doc_insert = [s for s in statements if "INSERT INTO documents" in s]
        chunk_insert = [s for s in statements if "INSERT INTO chunk_embeddings" in s]
        assert doc_insert and chunk_insert, "both INSERTs must still be present"
        assert all("layer" in s for s in doc_insert), (
            "identity envelopes left layer NULL and read back as KNOWLEDGE — "
            "the one layer they must never be"
        )
        assert all("layer" in s for s in chunk_insert)
        assert all("'identity'" in s for s in doc_insert)

    def test_artifact_layer_recall_returns_repaired_rows_end_to_end(
        self, tmp_path, monkeypatch, hermetic_principals
    ):
        """The coherence check for the artifact half: writer fix + migration
        must combine so that a layers=['artifact'] recall actually RETURNS a
        previously-mislabeled document. Each piece passing in isolation would
        not prove the day-one behaviour."""
        import minni.db as db_mod
        from minni.migrations import run_migrations

        _engine, db_obj, _cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        body = "the deployment rollback procedure is documented here. " * 20

        # A document as vault_ingest USED to write it: artifact-typed, but
        # stamped knowledge, hence invisible to layer-scoped artifact recall.
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

        # Apply the migration exactly as an upgrade would.
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
        """GA6-1 backfill: the writer fix only helps re-ingested pages, and
        vault_ingest skips unchanged files by mtime — so the already-stored
        rows must be repaired too."""
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

        # Reproduce a real upgrade: rows already exist, 017 has not been applied
        # to this file yet. Migrations are tracked by version, so un-record 017
        # and re-run — the same path an existing install takes.
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


# ---------------------------------------------------------------------------
# #225-R2 — decay must actually be scheduled, not CLI-only
# ---------------------------------------------------------------------------

class TestDecayIsScheduled:
    """run_decay was reachable only from the manual CLI and no launchd job
    called it, so every document sat at decay_score=1.0 against a declared
    7-day half-life."""

    def test_daemon_registers_a_decay_runner(self):
        import minni.minnid as minnid

        assert hasattr(minnid, "_decay_runner"), (
            "the daemon must schedule a decay pass; on the old code decay was "
            "reachable only from sovereign_memory's manual CLI"
        )
        assert hasattr(minnid, "_decay_enabled")
        assert hasattr(minnid, "_decay_sweep_once")

    def test_decay_runner_is_started_by_main(self):
        """A runner nobody creates a task for is the same dead channel."""
        import inspect
        import minni.minnid as minnid

        src = inspect.getsource(minnid.main)
        assert "_decay_runner()" in src, "main() must create the decay task"
        assert "decay_task.cancel()" in src, "shutdown must cancel the decay task"

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
        """The acceptance check from issue #225: a non-1.0 decay_score on an
        old document. Proves the pass does the thing, not merely that it runs."""
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
        """Restart-heavy machines sweep more often than planned; scores must
        converge, not compound."""
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
        # run_decay_all_indexes imports both helpers inside the function, so the
        # patches must land on the SOURCE modules, not on minni.decay.
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


# ---------------------------------------------------------------------------
# GA4-2 — decay must reach the reranker ordering, not only final_score
# ---------------------------------------------------------------------------

class TestDecayRerankParity:
    """decay_score only ever entered final_score (_score_merged_doc). The
    default config sorts and reports rerank_score, so a scheduled decay pass
    was invisible on the path that actually serves recall."""

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
        """A negative logit must get MORE negative under decay. Naive
        multiplication (score * decay) moves it UP toward zero — the sign bug
        the correction-boost leg already had to solve."""
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
        """recall-F4's floor lives in _score_merged_doc. If the rerank leg does
        not honour it, the two legs disagree about a correction's decay."""
        engine, _db, cfg = _make_engine(tmp_path)
        page_type = sorted(engine._correction_types)[0]
        candidates = [{"doc_id": 1, "rerank_score": 1.0, "decay_score": 0.01,
                       "page_type": page_type}]
        engine._apply_decay_rerank_attenuation(candidates)
        assert abs(candidates[0]["decay_applied"]
                   - float(cfg.correction_decay_floor)) < 1e-9

    def test_rerank_public_path_orders_by_decay(self, tmp_path):
        """Name-independent proof: drive _rerank itself with a stub
        cross-encoder that returns an identical logit for both candidates, so
        the ONLY thing that can separate them is decay. On the old code the
        order was the arbitrary input order and decay was inert."""
        engine, _db, _cfg = _make_engine(tmp_path)

        class _StubReranker:
            def predict(self, pairs):
                return [1.0] * len(pairs)

        # `reranker` is a lazy singleton property with no setter; seed its
        # backing field so no real cross-encoder is loaded.
        engine._reranker = _StubReranker()
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


# ---------------------------------------------------------------------------
# grok-review round 2 — the three reopened failure classes
# ---------------------------------------------------------------------------

class TestDecayIsAppliedOnce:
    """Round-2 finding 1: _apply_decay_rerank_attenuation mutated rerank_score
    in place, and compute_confidence then received that attenuated logit PLUS
    decay_factor — applying decay twice on the confidence/calibration/HyDE leg.
    Ranking keeps sorting on the adjusted score; confidence and provenance must
    read the model-pure raw_rerank_score."""

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

        class _StubReranker:
            def predict(self, pairs):
                return [2.0] * len(pairs)

        engine._reranker = _StubReranker()
        candidates = [
            {"doc_id": 1, "chunk_id": None, "chunk_text": "alpha",
             "rerank_score": None, "decay_score": 0.5, "page_type": "note"},
        ]
        ranked = engine._rerank("alpha", candidates)
        assert ranked[0]["raw_rerank_score"] == 2.0
        assert abs(ranked[0]["rerank_score"] - 1.0) < 1e-9

    def test_confidence_math_is_single_application(self, tmp_path):
        """Equal raw logits, decay 0.5: the confidence the search path computes
        must equal compute_confidence(raw, decay) — single application — and
        not compute_confidence(raw * decay, decay), the double-applied value
        the round-2 review measured (~0.384 vs ~0.299)."""
        from minni.scoring import compute_confidence

        engine, _db, _cfg = _make_engine(tmp_path)
        candidates = [
            {"doc_id": 1, "rerank_score": 2.0, "decay_score": 0.5, "page_type": "note"},
        ]
        engine._apply_rerank_score_adjustments(candidates)
        r = candidates[0]

        # The exact expression the two call sites now use.
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
        """Behavioral pin through the production search RPC: with a stub
        cross-encoder returning logit 2.0 and a document decayed to 0.5, the
        record=True confidence call must receive cross_encoder_score=2.0 —
        NOT the attenuated 1.0."""
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

        class _StubReranker:
            def predict(self, pairs):
                return [2.0] * len(pairs)

        engine._reranker = _StubReranker()

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
        recorded = []

        def _spy(rrf_score, cross_encoder_score, decay_factor, db=None,
                 record=False):
            if record:
                recorded.append((cross_encoder_score, decay_factor))
            return real(rrf_score, cross_encoder_score, decay_factor, db=db,
                        record=record)

        monkeypatch.setattr(scoring, "compute_confidence", _spy)

        resp = _dispatch("search", {
            "query": "deployment rollback", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        assert resp["result"]["count"] >= 1
        assert recorded, "the formatted result set must record confidence"
        ce, decay = recorded[0]
        assert abs(decay - 0.5) < 1e-9
        assert abs(ce - 2.0) < 1e-9, (
            "confidence must get the raw logit; 1.0 here means the decayed "
            "rerank_score leaked in and decay was applied twice"
        )


class TestVaultBackfillRefreshesWarmEngines:
    """Round-2 finding 2: on_vectors covers the shared index only. A vault
    engine memoized in _vault_retrieval_cache keeps its warm FAISS index, so
    backfilled vault rows stayed invisible to semantic recall until restart.
    The sweep must drop the per-vault cache when a vault gains vectors."""

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
    """Round-2 finding 3: content below the chunker's min_tokens chunked to
    nothing and was skipped — but the row still matched the batch predicate,
    so >=limit short docs re-wedged the LIMIT head (finding 2's stuck queue,
    new predicate hole) AND short memories stayed vectorless forever while
    index_durable_document embeds them as one whole-body chunk."""

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
        assert stats["skipped_no_content"] == 0
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
        """Three sub-min_tokens docs ahead of a long one, limit=2: the old skip
        left them matching every batch, so the long doc never entered one."""
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

        # Two bounded passes must drain all four rows; on the old code every
        # pass returned the same two short heads and made zero progress.
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


# ---------------------------------------------------------------------------
# grok-review round 3 — residual stuck-queue and honesty holes
# ---------------------------------------------------------------------------

class _PoisonEmbedder:
    """Raises for poison content — the permanent-encode-failure shape."""

    def encode(self, text):
        import numpy as np

        if "POISONROW" in text:
            raise RuntimeError("model reject")
        vec = np.zeros(384, dtype="float32")
        vec[len(text) % 384] = 1.0
        return vec


class TestEncodeFailureCannotWedgeTheDrain:
    """Round-3 finding 1: empty content and short docs are excluded by static
    predicates, but a row whose encode PERMANENTLY raises stays eligible and
    re-matched the head of every un-ordered LIMIT batch — the third form of
    the stuck queue. Batches are now ordered and cursor-advanced, so failures
    are stepped past and retried only after the cursor wraps."""

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
        """Failures must be stepped past, not excluded forever — a transient
        encoder fault (OOM) heals, and the wrap is what re-offers the row."""
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
        # Tail exhausted → cursor wraps to the head and re-attempts the row.
        again = backfill_learning_embeddings(db_obj, cfg, limit=2)
        assert again["failed"] == 1, (
            "a failed row must come back after the wrap; permanent exclusion "
            "would turn every transient fault into a silent hole"
        )


class TestCoverageMatchesDrainEligibility:
    """Round-3 finding 2: coverage counted terminal learnings (rejected /
    expired / superseded status, embedding NULL) that both backfill and
    semantic recall skip — a permanent phantom gap no drain could close."""

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
    """Round-3 finding 3: decay-first mapped a raw 0.0 logit to decay - 1.0
    (negative), so the boost's zero-logit special case never fired and a
    decayed zero-logit correction ranked BELOW an undecayed zero-logit
    non-correction. Boost now runs first, dispatching on the model-pure logit;
    the non-zero branches are commutative so nothing else moves."""

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
        """Positive and negative branches must compose identically in either
        order: raw * decay * (1 + boost) and raw / (decay * (1 + boost))."""
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
    """Round-3 finding 4: the drain is multi-index but health's
    embedding_coverage sampled only the shared DB — "coverage fine" could mask
    a still-gapped vault."""

    def test_vault_coverage_reports_per_vault_counts(self, tmp_path, monkeypatch):
        import minni.index_all as index_all
        import minni.vault_index as vault_index
        from minni.backfill import vault_embedding_coverage

        # A real vault-shaped DB with one vectorless document.
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

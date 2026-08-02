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

    def test_retrieval_passes_record_at_the_formatting_site(self):
        import inspect

        from minni.retrieval import RetrievalEngine

        src = inspect.getsource(RetrievalEngine.retrieve)
        assert "record=True" in src, (
            "the final formatted result set is the one site that feeds the window"
        )


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
        counted, not quietly treated as done."""
        from minni.backfill import backfill_document_vectors

        self._stub_encoder(monkeypatch)
        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO documents (path, agent, sigil) "
                "VALUES ('empty.md', 'codex', 'vault')"
            )

        stats = backfill_document_vectors(db_obj, cfg)
        assert stats["documents"] == 0
        assert stats["skipped_no_content"] == 1

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

    def test_vault_ingest_no_longer_hardcodes_knowledge(self):
        """GA6-1: the literal must be gone from the writer's parameters."""
        import inspect

        import minni.afm_passes.vault_ingest as vi

        src = inspect.getsource(vi._index_changed_pages)
        assert "_infer_layer" in src, "vault_ingest must infer the layer"
        assert '"knowledge",' not in src, (
            "a hardcoded layer='knowledge' makes every artifact-typed page "
            "invisible to layer-scoped recall"
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

    def test_wiki_indexer_stamps_layer_on_all_writes(self):
        import inspect

        import minni.wiki_indexer as wi

        src = inspect.getsource(wi.WikiIndexer)
        assert "whole_document=0" in src, (
            "the identity branch must be closed structurally, not by a strip"
        )
        assert "computed_at, layer" in src, "chunk_embeddings must carry layer"

    def test_seed_identity_stamps_the_identity_layer(self):
        import inspect

        import minni.seed_identity as si

        src = inspect.getsource(si)
        assert "'identity')" in src or "'identity'" in src
        assert "whole_document, layer" in src, (
            "identity envelopes left layer NULL and read back as KNOWLEDGE — "
            "the one layer they must never be"
        )

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

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

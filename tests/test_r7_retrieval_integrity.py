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

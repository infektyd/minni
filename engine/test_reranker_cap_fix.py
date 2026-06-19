"""Test: reranker_final_k must not cap results below the caller's limit (S-1 fix).

Before the fix (retrieval.py:1986): merged = merged[:self.config.reranker_final_k]
After the fix:                      merged = merged[:max(self.config.reranker_final_k, limit)]

When limit=10 and reranker_final_k=5, the old code structurally capped recall@10
at 0.5 — any relevant doc ranked 6-10 was silently dropped. The fix ensures
limit takes precedence when it is larger than final_k.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Dict
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_merged(n: int) -> List[Dict]:
    """Create n fake merged result dicts with distinct doc_ids."""
    return [
        {
            "doc_id": i + 1,
            "chunk_id": i + 1,
            "path": f"doc_{i + 1}.md",
            "agent": "test",
            "sigil": "❓",
            "rrf_score": 1.0 / (i + 1),
            "fts_rank": i + 1,
            "sem_rank": i + 1,
            "final_score": 1.0 / (i + 1),
            "rerank_score": 1.0 / (i + 1),
            "decay_score": 1.0,
            "page_status": "accepted",
            "privacy_level": "safe",
            "page_type": "wiki",
            "layer": "knowledge",
            "indexed_at": 0,
            "evidence_refs": "[]",
            "chunk_text": f"Content for document {i + 1}",
            "heading_context": "",
            "token_count": 10,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Unit test: the truncation respects max(final_k, limit)
# ---------------------------------------------------------------------------

def test_reranker_final_k_does_not_cap_below_limit(tmp_path):
    """A limit=10 retrieve call must return up to 10 results, not 5 (final_k)."""
    from config import SovereignConfig
    from db import SovereignDB
    from retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        vault_path=str(tmp_path / "vault/"),
        writeback_path=str(tmp_path / "learnings/"),
        graph_export_dir=str(tmp_path / "graphs/"),
        reranker_enabled=True,
        reranker_final_k=5,   # the old default that caused the bug
        hyde_enabled=False,
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)

    # Build 20 fake pre-merge candidates so the reranker has plenty to truncate.
    fake_candidates = _make_fake_merged(20)

    # Fake cross-encoder: return input unchanged (scores already set in rerank_score)
    fake_reranker = MagicMock()
    fake_reranker.predict.side_effect = lambda pairs: [1.0 / (i + 1) for i in range(len(pairs))]

    # Patch the reranker property and the lower-level search/merge methods
    # so the test only exercises the post-rerank truncation logic.
    with (
        patch.object(type(engine), "reranker", new_callable=lambda: property(lambda self: fake_reranker)),
        patch.object(engine, "_fts_search", return_value=fake_candidates[:10]),
        patch.object(engine, "_semantic_search", return_value=fake_candidates[:10]),
        patch.object(engine, "_rrf_merge", return_value=fake_candidates[:15]),
        patch.object(engine, "_filter_candidates", side_effect=lambda c, *_a, **_kw: c),
        patch.object(engine, "_apply_feedback_demotions", side_effect=lambda c, *_a, **_kw: c),
        patch.object(engine, "_chunk_index_empty", return_value=False),
        patch.object(engine, "_budget_results", side_effect=lambda c, _q: c),
    ):
        results = engine.retrieve(
            query="test query",
            limit=10,       # caller asks for 10
            depth="headline",
            expand=False,
            update_access=False,
            budget_tokens=False,
        )

    # With final_k=5 and limit=10, the fix must return up to 10.
    assert len(results) == 10, (
        f"Expected 10 results (limit=10), got {len(results)}. "
        "reranker_final_k=5 must not cap below limit."
    )


def test_reranker_final_k_still_caps_when_larger_than_limit(tmp_path):
    """When final_k > limit, the truncation keeps max(final_k, limit) candidates.

    The max(final_k, limit) truncation only prevents final_k from FLOORING the
    result count below limit. When final_k is LARGER, the reranker has already
    decided how many to keep; we don't add a second [:limit] cap here because
    token budgeting handles that downstream. This test verifies the formula
    max(20, 5) = 20 is applied (not a tighter limit=5 cap that would strip
    docs the reranker intentionally kept).
    """
    from config import SovereignConfig
    from db import SovereignDB
    from retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test2.db"),
        faiss_index_path=str(tmp_path / "test2.faiss"),
        vault_path=str(tmp_path / "vault2/"),
        writeback_path=str(tmp_path / "learnings2/"),
        graph_export_dir=str(tmp_path / "graphs2/"),
        reranker_enabled=True,
        reranker_final_k=20,  # larger than limit
        hyde_enabled=False,
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)

    fake_candidates = _make_fake_merged(20)

    fake_reranker = MagicMock()
    fake_reranker.predict.side_effect = lambda pairs: [1.0 / (i + 1) for i in range(len(pairs))]

    with (
        patch.object(type(engine), "reranker", new_callable=lambda: property(lambda self: fake_reranker)),
        patch.object(engine, "_fts_search", return_value=fake_candidates[:10]),
        patch.object(engine, "_semantic_search", return_value=fake_candidates[:10]),
        patch.object(engine, "_rrf_merge", return_value=fake_candidates[:20]),
        patch.object(engine, "_filter_candidates", side_effect=lambda c, *_a, **_kw: c),
        patch.object(engine, "_apply_feedback_demotions", side_effect=lambda c, *_a, **_kw: c),
        patch.object(engine, "_chunk_index_empty", return_value=False),
        patch.object(engine, "_budget_results", side_effect=lambda c, _q: c),
    ):
        results = engine.retrieve(
            query="test query",
            limit=5,       # caller asks for 5
            depth="headline",
            expand=False,
            update_access=False,
            budget_tokens=False,
        )

    # max(final_k=20, limit=5) = 20: the reranker kept 20, budget_tokens is off,
    # so all 20 flow through. Token budget (budget_tokens=True) would trim further.
    assert len(results) == 20, (
        f"Expected 20 results (max(final_k=20, limit=5)=20), got {len(results)}. "
        "Token budget (not a hard limit cap) should trim further when enabled."
    )


def test_reranker_disabled_returns_limit_results(tmp_path):
    """When reranker is disabled, the else-branch [:limit] must still work."""
    from config import SovereignConfig
    from db import SovereignDB
    from retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test3.db"),
        faiss_index_path=str(tmp_path / "test3.faiss"),
        vault_path=str(tmp_path / "vault3/"),
        writeback_path=str(tmp_path / "learnings3/"),
        graph_export_dir=str(tmp_path / "graphs3/"),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)

    fake_candidates = _make_fake_merged(20)

    with (
        patch.object(engine, "_fts_search", return_value=fake_candidates[:10]),
        patch.object(engine, "_semantic_search", return_value=fake_candidates[:10]),
        patch.object(engine, "_rrf_merge", return_value=fake_candidates[:15]),
        patch.object(engine, "_filter_candidates", side_effect=lambda c, *_a, **_kw: c),
        patch.object(engine, "_apply_feedback_demotions", side_effect=lambda c, *_a, **_kw: c),
        patch.object(engine, "_chunk_index_empty", return_value=False),
        patch.object(engine, "_budget_results", side_effect=lambda c, _q: c),
    ):
        results = engine.retrieve(
            query="test query",
            limit=10,
            depth="headline",
            expand=False,
            update_access=False,
            budget_tokens=False,
        )

    assert len(results) == 10, (
        f"Expected 10 results when reranker disabled, got {len(results)}."
    )

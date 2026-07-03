"""Test: retrieve() must self-label results as primary/related (S7 fix).

Every result dict now carries:
  match_kind   : "primary"  for the top-ranked result (rank 1)
                 "related"  for ranks 2..N
  related_rank : None       for the primary result
                 1..N-1     for the related results (1 = closest to primary)

These fields must be present at all depth tiers (headline, snippet, chunk).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _make_engine(tmp_path, db_suffix=""):
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / f"test{db_suffix}.db"),
        faiss_index_path=str(tmp_path / f"test{db_suffix}.faiss"),
        vault_path=str(tmp_path / "vault/"),
        writeback_path=str(tmp_path / "learnings/"),
        graph_export_dir=str(tmp_path / "graphs/"),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    return RetrievalEngine(db, cfg)


# Shared content — repeated enough to produce multiple chunks / docs
_LONG_BODY = (
    "The recall pipeline uses Reciprocal Rank Fusion to merge FTS5 and FAISS results. "
    "Each chunk carries a score derived from its position in both ranked lists. "
    "The cross-encoder reranker rescores the top-k candidates for precision. "
) * 8  # ~120 tokens × 8 ≈ 960 tokens → several chunks


def _seed(engine, *, n_docs=3):
    """Index n_docs distinct pages so we reliably get multi-result recalls."""
    for i in range(n_docs):
        engine.index_durable_document(
            content=f"# Recall Pipeline Doc {i}\n\n{_LONG_BODY}",
            path=f"wiki/recall/doc-{i}.md",
            agent="claude-code",
            sigil="📄",
            privacy_level="safe",
            page_status="accepted",
            layer="knowledge",
        )


# ---------------------------------------------------------------------------
# Core label correctness
# ---------------------------------------------------------------------------

def test_labels_present_at_snippet_depth(tmp_path):
    """depth=snippet: first result is primary, rest are related with correct rank."""
    engine = _make_engine(tmp_path, "a")
    _seed(engine)

    results = engine.retrieve(
        query="recall pipeline fusion FAISS reranker",
        limit=5,
        depth="snippet",
        expand=False,
        budget_tokens=False,
        update_access=False,
    )
    assert len(results) >= 2, f"Need >=2 results for relational labeling; got {len(results)}"

    first = results[0]
    assert first.get("match_kind") == "primary", (
        f"rank-1 result must be 'primary', got {first.get('match_kind')!r}"
    )
    assert first.get("related_rank") is None, (
        f"primary result must have related_rank=None, got {first.get('related_rank')!r}"
    )

    for i, res in enumerate(results[1:], start=1):
        assert res.get("match_kind") == "related", (
            f"rank-{i+1} result must be 'related', got {res.get('match_kind')!r}"
        )
        expected_rr = i  # related_rank is 1-based: rank-2 → related_rank=1
        assert res.get("related_rank") == expected_rr, (
            f"rank-{i+1} result must have related_rank={expected_rr}, "
            f"got {res.get('related_rank')!r}"
        )


def test_labels_present_at_headline_depth(tmp_path):
    """depth=headline: labels are present at the headline tier too."""
    engine = _make_engine(tmp_path, "b")
    _seed(engine)

    results = engine.retrieve(
        query="recall pipeline fusion FAISS reranker",
        limit=3,
        depth="headline",
        expand=False,
        budget_tokens=False,
        update_access=False,
    )
    assert results, "Expected at least 1 result"

    assert results[0].get("match_kind") == "primary"
    assert results[0].get("related_rank") is None

    for i, res in enumerate(results[1:], start=1):
        assert res.get("match_kind") == "related"
        assert res.get("related_rank") == i


def test_labels_present_at_chunk_depth(tmp_path):
    """depth=chunk: labels are present at the chunk tier too."""
    engine = _make_engine(tmp_path, "c")
    _seed(engine)

    results = engine.retrieve(
        query="recall pipeline fusion FAISS reranker",
        limit=3,
        depth="chunk",
        expand=False,
        budget_tokens=False,
        update_access=False,
    )
    assert results, "Expected at least 1 result"

    assert results[0].get("match_kind") == "primary"
    assert results[0].get("related_rank") is None

    for i, res in enumerate(results[1:], start=1):
        assert res.get("match_kind") == "related"
        assert res.get("related_rank") == i


def test_single_result_is_primary(tmp_path):
    """When only 1 result is returned it is labeled primary with related_rank=None."""
    engine = _make_engine(tmp_path, "d")
    # Only index one unique doc; limit=1
    engine.index_durable_document(
        content="# Single Doc\n\n" + _LONG_BODY,
        path="wiki/recall/single.md",
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="accepted",
        layer="knowledge",
    )

    results = engine.retrieve(
        query="recall pipeline fusion FAISS",
        limit=1,
        depth="snippet",
        expand=False,
        budget_tokens=False,
        update_access=False,
    )
    assert results, "Expected at least 1 result"
    assert results[0].get("match_kind") == "primary"
    assert results[0].get("related_rank") is None


def test_related_rank_sequence_is_contiguous(tmp_path):
    """related_rank values must form a contiguous 1, 2, 3 ... sequence."""
    engine = _make_engine(tmp_path, "e")
    _seed(engine, n_docs=5)

    results = engine.retrieve(
        query="recall pipeline fusion FAISS reranker",
        limit=8,
        depth="snippet",
        expand=False,
        budget_tokens=False,
        update_access=False,
    )
    assert len(results) >= 2, "Need multiple results for contiguity check"

    related = [r for r in results if r.get("match_kind") == "related"]
    rr_values = [r["related_rank"] for r in related]
    expected = list(range(1, len(related) + 1))
    assert rr_values == expected, (
        f"related_rank sequence must be {expected}, got {rr_values}"
    )

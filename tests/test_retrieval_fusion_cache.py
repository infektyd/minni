"""Fusion must retain the cache identity of the passage actually scored."""
from types import SimpleNamespace

import pytest

from minni.principal import EffectivePrincipal
from minni.rerank_cache import RerankCache
from minni.retrieval import RetrievalEngine


class Model:
    model_name = "fusion-test"
    version = "1"

    def __init__(self):
        self.calls = []

    def predict(self, pairs, **kwargs):
        self.calls.append(pairs)
        return [float(len(passage)) for _, passage in pairs]


def row(text, chunk_id=None):
    result = dict(doc_id=1, path="/tmp/fusion/a.md", agent="codex", sigil="T",
                  page_type="session", privacy_level="safe", page_status="accepted",
                  chunk_text=text, heading_context="", layer="knowledge")
    if chunk_id is not None:
        result["chunk_id"] = chunk_id
    return result


def engine(model, corpus="/tmp/fusion.db"):
    result = object.__new__(RetrievalEngine)
    result.config = SimpleNamespace(db_path=corpus, reranker_model="fusion-test",
        reranker_enabled=True, reranker_top_k=1, reranker_final_k=1,
        hyde_enabled=False, rrf_k=60, fts_weight=1, semantic_weight=1)
    result._reranker = model
    result._correction_types = set()
    result.db = None
    result._chunk_index_empty = lambda: False
    result._resolve_query_variants = lambda query, expand: [query]
    result._apply_feedback_demotions = lambda rows, *_args: rows
    result._apply_rerank_score_adjustments = lambda rows: None
    return result


@pytest.mark.parametrize("fts", [[], [row("full page", 999)]])
def test_semantic_passage_identity_survives_fusion(fts):
    result = engine(Model())._rrf_merge(fts, [row("selected passage", 7)], 1)[0]
    assert (result["chunk_text"], result["chunk_id"]) == ("selected passage", 7)


def test_fts_and_empty_semantic_do_not_borrow_chunk_identity():
    subject = engine(Model())
    for semantic in ([], [row("", 7)]):
        result = subject._rrf_merge([row("full page", 999)], semantic, 1)[0]
        assert result["chunk_text"] == "full page"
        assert "chunk_id" not in result


def test_replaced_passage_replaces_or_clears_identity():
    subject = engine(Model())
    for replacement, expected_id in [(row("second", 8), 8), (row("second"), None)]:
        result = subject._rrf_merge([], [row("first", 7), replacement], 1)[0]
        assert result["chunk_text"] == "second"
        assert result.get("chunk_id") == expected_id


@pytest.mark.parametrize("dual_hit", [False, True])
def test_retrieve_reuses_cache_but_changed_passage_corpus_and_invalidation_miss(monkeypatch, dual_hit):
    import minni.rerank_cache as caches
    cache = RerankCache()
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", cache)
    model = Model()
    subject = engine(model)
    passage = row("semantic passage", 7)
    subject._fts_search = lambda *_args, **_kwargs: [row("full page")] if dual_hit else []
    subject._semantic_search = lambda *_args, **_kwargs: [dict(passage)]
    principal = EffectivePrincipal(agent_id="codex", capabilities=["search", "read"],
                                   allowed_vault_roots=["/tmp/fusion"])

    def search():
        return subject.retrieve("matching", limit=1, principal=principal, workspace="default",
                                update_access=False, budget_tokens=False, depth="chunk",
                                expand=False, use_hyde=False)

    first = search()
    assert first
    repeated = search()
    for field in ("text", "source", "score", "doc_id", "chunk_id", "heading"):
        assert repeated[0][field] == first[0][field]
    assert len(model.calls) == 1
    assert model.calls[0] == [["matching", "semantic passage"]]
    passage["chunk_text"] = "changed passage"
    search()
    assert len(model.calls) == 2
    passage["heading_context"] = "changed heading"
    search()
    assert len(model.calls) == 3
    subject.config.db_path = "/tmp/other-fusion.db"
    search()
    assert len(model.calls) == 4
    assert cache.invalidate_chunks([7]) == 4
    search()
    assert len(model.calls) == 5


@pytest.mark.parametrize("primary", [[], [row("first semantic", 7)], [row("first semantic")]])
def test_multi_stream_keeps_identity_of_first_selected_semantic_passage(primary):
    subject = engine(Model())
    result = subject._rrf_merge_multi([row("full page", 999)], primary,
                                     [[row("extra semantic", 8)]], 1)[0]
    winner = primary[0] if primary else row("extra semantic", 8)
    assert result["chunk_text"] == winner["chunk_text"]
    assert result.get("chunk_id") == winner.get("chunk_id")

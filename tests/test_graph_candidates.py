"""P1 graph candidate shortlist: synthetic focused tests, no DB/model/live.

Covers: envelope denial, filter-before-limit, self-exclusion, doc dedup by
max cosine, cosine floor with finite values (NaN/Inf dropped), pair/deferred
limits, metadata-before-content ordering (no unauthorized content read),
immutability, and cross-corpus aliasing rejection.
"""

import dataclasses
import hashlib
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.graph_candidates import (
    COSINE_FLOOR,
    MAX_CHUNK_HITS,
    MAX_CLASSIFIER_PAIRS,
    MAX_SHORTLIST_DOCS,
    prepare_candidate_shortlist,
)
from minni.principal import EffectivePrincipal


def _principal(agent_id="codex"):
    # A realistic committing agent: NOT an operator ("*" would grant
    # foreign-private reads via the operator oversight rule).
    return EffectivePrincipal(agent_id=agent_id, capabilities=["learn"])


def _meta(doc_id, agent="codex", **over):
    base = {
        "store_id": "store-A",
        "doc_id": doc_id,
        "agent": agent,
        "privacy_level": "safe",
        "page_status": "accepted",
        "page_type": "learning",
        "memory_kind": "learning",
        "title": f"Learning {doc_id}",
        "path": f"/vault/_durable/{agent}__{doc_id:04d}.md",
    }
    base.update(over)
    return base


class _Store:
    """Injectable exact metadata/content accessor with call recording."""

    def __init__(self, docs, contents=None, strict_content=False):
        self.docs = dict(docs)
        self.contents = dict(contents or {})
        self.strict_content = strict_content
        self.metadata_calls = []
        self.content_calls = []

    def _text(self, doc_id):
        return self.contents.get(doc_id, f"Body text for document {doc_id}. " * 20)

    def get_metadata(self, store_id, doc_id):
        assert store_id == "store-A"
        self.metadata_calls.append(doc_id)
        doc = self.docs.get(doc_id)
        if doc is None:
            return None
        return {"content_sha256": hashlib.sha256(self._text(doc_id).encode()).hexdigest(), **doc}

    def get_content(self, store_id, doc_id):
        assert store_id == "store-A"
        self.content_calls.append(doc_id)
        if self.strict_content and doc_id not in self.contents:
            return None
        return {"store_id": store_id, "doc_id": doc_id, "content": self._text(doc_id)}


def _hits(*specs):
    """Specs of (doc_id, chunk_id, cosine) in rank order."""
    return [
        {"store_id": "store-A", "chunk_id": chunk, "doc_id": doc, "cosine": score}
        for doc, chunk, score in specs
    ]


def test_envelope_denial():
    store = _Store({})
    with pytest.raises(ValueError):
        prepare_candidate_shortlist(
        store_id="store-A",
            hits=[], principal="codex",
            get_metadata=store.get_metadata, get_content=store.get_content,
        )
    with pytest.raises(ValueError):
        prepare_candidate_shortlist(
        store_id="store-A",
            hits="not-a-sequence", principal=_principal(),
            get_metadata=store.get_metadata, get_content=store.get_content,
        )
    with pytest.raises(ValueError):
        prepare_candidate_shortlist(
        store_id="store-A",
            hits=[], principal=_principal(),
            get_metadata=None, get_content=store.get_content,
        )
    with pytest.raises(ValueError):
        prepare_candidate_shortlist(
        store_id="store-A",
            hits=[], principal=_principal(), source_doc_id="1",
            get_metadata=store.get_metadata, get_content=store.get_content,
        )
    with pytest.raises(ValueError):
        prepare_candidate_shortlist(
        store_id="store-A",
            hits=[{"store_id": "store-A", "chunk_id": 1, "doc_id": 2, "cosine": 0.9},
                  {"store_id": "store-A", "chunk_id": 1, "doc_id": 3, "cosine": 0.8}],
            principal=_principal(),
            get_metadata=store.get_metadata, get_content=store.get_content,
        )


def test_happy_path_pairs_and_counts():
    docs = {i: _meta(i) for i in (10, 20, 30)}
    store = _Store(docs)
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=_hits((10, 1, 0.9), (20, 2, 0.8), (30, 3, 0.7)),
        principal=_principal(), source_doc_id=99,
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    assert [p.doc_id for p in result.pairs] == [10, 20, 30]
    assert result.deferred == ()
    assert result.examined_docs == 3
    assert result.excluded_before_cap == 0
    first = result.pairs[0]
    assert first.pair_id == "candidate-doc-10"
    assert first.chunk_id == 1 and first.cosine == 0.9
    assert first.excerpt and first.excerpt_tokens > 0
    assert set(store.content_calls) == {10, 20, 30}


def test_filter_before_limit_self_terminal_blocked_unreadable():
    docs = {1: _meta(1)}
    for i in range(2, 2 + MAX_SHORTLIST_DOCS):
        docs[i] = _meta(i)
    docs[100] = _meta(100, privacy_level="blocked")
    docs[101] = _meta(101, page_status="rejected")
    docs[102] = _meta(102, memory_kind="wiki", page_type="wiki")
    docs[103] = _meta(103, agent="foreign", privacy_level="private")
    store = _Store(docs)
    hits = _hits(
        (100, 1000, 0.99), (101, 1001, 0.98), (102, 1002, 0.97),
        (103, 1003, 0.96), (1, 1004, 0.95),
        *((i, 1004 + i, 0.94 - i * 0.001) for i in range(2, 2 + MAX_SHORTLIST_DOCS)),
    )
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=hits, principal=_principal(), source_doc_id=1,
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    kept = [p.doc_id for p in result.pairs] + [d.doc_id for d in result.deferred]
    assert len(kept) == MAX_SHORTLIST_DOCS
    assert 1 not in kept and 100 not in kept and 101 not in kept
    assert 102 not in kept and 103 not in kept
    assert result.excluded_before_cap == 5
    # Blocked/rejected/unreadable text is never loaded.
    assert 100 not in store.content_calls and 101 not in store.content_calls
    assert 103 not in store.content_calls and 1 not in store.content_calls


def test_self_exclusion_top_hit():
    docs = {7: _meta(7), 8: _meta(8)}
    store = _Store(docs)
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=_hits((7, 70, 0.99), (8, 80, 0.5)),
        principal=_principal(), source_doc_id=7,
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    assert [p.doc_id for p in result.pairs] == [8]


def test_dedup_max_cosine_deterministic_ties():
    docs = {5: _meta(5), 6: _meta(6), 9: _meta(9)}
    store = _Store(docs)
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=_hits(
            (5, 50, 0.6), (5, 51, 0.9), (5, 52, 0.7),
            (6, 60, 0.9), (9, 90, 0.9),
        ),
        principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    by_doc = {p.doc_id: p for p in result.pairs}
    assert by_doc[5].cosine == 0.9 and by_doc[5].chunk_id == 51
    # Equal cosine breaks by doc_id ascending.
    assert [p.doc_id for p in result.pairs] == [5, 6, 9]
    assert result.examined_docs == 3


def test_floor_and_nonfinite_scores():
    docs = {1: _meta(1), 2: _meta(2)}
    store = _Store(docs)
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=[
            {"store_id": "store-A", "chunk_id": 1, "doc_id": 1, "cosine": 0.9},
            {"store_id": "store-A", "chunk_id": 2, "doc_id": 2, "cosine": COSINE_FLOOR - 0.01},
            {"store_id": "store-A", "chunk_id": 3, "doc_id": 3, "cosine": float("nan")},
            {"store_id": "store-A", "chunk_id": 4, "doc_id": 4, "cosine": float("inf")},
            {"store_id": "store-A", "chunk_id": 5, "doc_id": "6", "cosine": 0.9},
            {"store_id": "store-A", "chunk_id": True, "doc_id": 7, "cosine": 0.9},
            "garbage",
        ],
        principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    assert [p.doc_id for p in result.pairs] == [1]
    assert result.below_floor == 1
    assert result.malformed_hits == 5
    assert 3 not in store.metadata_calls and 4 not in store.metadata_calls


def test_pair_cap_with_explicit_deferred():
    docs = {i: _meta(i) for i in range(1, MAX_SHORTLIST_DOCS + 1)}
    store = _Store(docs)
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=_hits(*((i, 100 + i, 0.95 - i * 0.001)
                     for i in range(1, MAX_SHORTLIST_DOCS + 1))),
        principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    assert len(result.pairs) == MAX_CLASSIFIER_PAIRS
    assert len(result.deferred) == MAX_SHORTLIST_DOCS - MAX_CLASSIFIER_PAIRS
    assert all(p.excerpt for p in result.pairs)
    assert all(d.excerpt is None for d in result.deferred)
    # Deferred identity is retained for later revalidation; their text never loads.
    assert [d.doc_id for d in result.deferred] == list(range(9, MAX_SHORTLIST_DOCS + 1))
    assert set(store.content_calls) == set(range(1, MAX_CLASSIFIER_PAIRS + 1))
    pair_ids = [p.pair_id for p in result.pairs] + [d.pair_id for d in result.deferred]
    assert len(set(pair_ids)) == MAX_SHORTLIST_DOCS


def test_chunk_bound_and_missing_content_falls_to_deferred():
    docs = {i: _meta(i) for i in range(1, 4)}
    store = _Store(docs, contents={1: "Usable body text. " * 30}, strict_content=True)
    hits = _hits(*((i, 1000 + i, 0.9) for i in range(1, 60)))
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=hits, principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    assert result.hits_truncated is True
    assert result.examined_chunks == MAX_CHUNK_HITS
    assert [p.doc_id for p in result.pairs] == [1]
    assert [d.doc_id for d in result.deferred] == [2, 3]
    assert all(d.excerpt is None for d in result.deferred)


def test_result_immutable_and_inputs_untouched():
    docs = {1: _meta(1)}
    store = _Store(docs)
    hits = _hits((1, 10, 0.9))
    snapshot = [dict(h) for h in hits]
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=hits, principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    assert hits == snapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.pairs[0].cosine = 0.1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.examined_docs = 0  # type: ignore[misc]


def test_absent_and_foreign_metadata_silently_dropped():
    docs = {1: _meta(1), 3: _meta(3, agent="foreign", privacy_level="private")}
    store = _Store(docs)
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=_hits((1, 10, 0.9), (2, 20, 0.8), (3, 30, 0.7)),
        principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    assert [p.doc_id for p in result.pairs] == [1]
    assert result.excluded_before_cap == 2
    assert 2 not in store.content_calls and 3 not in store.content_calls
    # Empty snapshot is a valid empty shortlist, not an error.
    empty = prepare_candidate_shortlist(
        store_id="store-A",
        hits=[], principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    assert empty.pairs == () and empty.deferred == ()


def test_pairs_render_through_edge_prompt_renderer():
    """Descriptors are directly consumable by the existing edge renderer."""
    from minni.edge_inference import render_edge_inference_prompt

    store = _Store({1: _meta(1, title="Candidate title")})
    result = prepare_candidate_shortlist(
        store_id="store-A",
        hits=_hits((1, 10, 0.9)), principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content,
    )
    candidates = [{
        "pair_id": p.pair_id, "title": p.title, "excerpt": p.excerpt,
        "status": p.status, "page_type": p.page_type, "doc_id": p.doc_id,
    } for p in result.pairs]
    rendered = render_edge_inference_prompt(
        {"learning_id": 9, "title": "Source", "excerpt": "Source body."},
        candidates,
    )
    assert rendered.pair_ids == ["candidate-doc-1"]
    assert rendered.budget_exceeded is False


def test_large_sequence_reads_only_first_48_indices():
    from collections.abc import Sequence

    class PoisonTail(Sequence):
        def __init__(self):
            self.reads = []
        def __len__(self):
            return 1_000_000
        def __getitem__(self, index):
            assert isinstance(index, int) and 0 <= index < 48
            self.reads.append(index)
            return _hits((index + 1, index + 1, 0.9))[0]
        def __iter__(self):
            raise AssertionError("must not materialize or iterate the full snapshot")

    hits = PoisonTail()
    store = _Store({})
    result = prepare_candidate_shortlist(store_id="store-A", hits=hits, principal=_principal(),
        get_metadata=store.get_metadata, get_content=store.get_content)
    assert hits.reads == list(range(48))
    assert result.examined_chunks == 48 and result.hits_truncated


def test_zero_document_cap_never_calls_accessors():
    store = _Store({1: _meta(1)})
    result = prepare_candidate_shortlist(store_id="store-A", hits=_hits((1, 1, 0.9)),
        principal=_principal(), get_metadata=store.get_metadata, get_content=store.get_content,
        max_docs=0)
    assert result.pairs == result.deferred == ()
    assert store.metadata_calls == store.content_calls == []


def test_caller_cannot_lower_normative_cosine_floor():
    store = _Store({1: _meta(1), 2: _meta(2)})
    result = prepare_candidate_shortlist(store_id="store-A", hits=_hits((1, 1, 0.1), (2, 2, 0.42)),
        principal=_principal(), get_metadata=store.get_metadata, get_content=store.get_content,
        cosine_floor=0)
    assert [pair.doc_id for pair in result.pairs] == [2]
    assert store.metadata_calls == store.content_calls == [2]
    assert result.below_floor == 1


@pytest.mark.parametrize("layer,field,value", [
    ("hit", "store_id", "store-B"), ("hit", "store_id", None),
    ("metadata", "store_id", "store-B"), ("metadata", "doc_id", 999),
    ("content", "store_id", "store-B"), ("content", "doc_id", 999),
])
def test_mismatched_store_or_document_identity_fails_closed(layer, field, value):
    store = _Store({1: _meta(1)})
    hits = _hits((1, 1, 0.9))
    if layer == "hit":
        hits[0][field] = value
    def metadata(store_id, doc_id):
        result = store.get_metadata(store_id, doc_id)
        if layer == "metadata":
            result[field] = value
        return result
    def content(store_id, doc_id):
        result = store.get_content(store_id, doc_id)
        if layer == "content":
            result[field] = value
        return result
    with pytest.raises(ValueError, match="identity"):
        prepare_candidate_shortlist(store_id="store-A", hits=hits, principal=_principal(),
            get_metadata=metadata, get_content=content)
    if layer != "content":
        assert store.content_calls == []
    if layer == "hit":
        assert store.metadata_calls == []


def test_full_content_hash_detects_change_beyond_excerpt():
    text = "Common prefix. " * 1500 + "original tail"
    store = _Store({1: _meta(1)}, {1: text})
    def changed_content(store_id, doc_id):
        return {"store_id": store_id, "doc_id": doc_id, "content": text + " changed tail"}
    with pytest.raises(ValueError, match="content hash"):
        prepare_candidate_shortlist(store_id="store-A", hits=_hits((1, 1, 0.9)),
            principal=_principal(), get_metadata=store.get_metadata, get_content=changed_content)


def test_descriptor_provenance_binds_full_text_metadata_and_store():
    import json

    text = "Common prefix. " * 1500 + "original tail"
    store = _Store({1: _meta(1)}, {1: text})
    def prepare():
        return prepare_candidate_shortlist(store_id="store-A", hits=_hits((1, 1, 0.9)),
            principal=_principal(), get_metadata=store.get_metadata, get_content=store.get_content).pairs[0]
    first = prepare()
    assert first.store_id == "store-A"
    assert first.content_sha256 == hashlib.sha256(text.encode()).hexdigest()
    metadata = store.get_metadata("store-A", 1)
    assert first.metadata_sha256 == hashlib.sha256(json.dumps(metadata,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    store.contents[1] = text + " later revision"
    changed = prepare()
    assert changed.excerpt == first.excerpt
    assert changed.content_sha256 != first.content_sha256
    assert changed.evidence_sha256 != first.evidence_sha256
    store.docs[1]["privacy_level"] = "private"
    restricted = prepare()
    assert restricted.metadata_sha256 != changed.metadata_sha256
    assert restricted.evidence_sha256 != changed.evidence_sha256
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.store_id = "store-B"


def test_metadata_snapshot_is_not_mutated_by_content_accessor():
    store = _Store({1: _meta(1)})
    metadata = store.get_metadata("store-A", 1)
    def content(store_id, doc_id):
        metadata["privacy_level"] = "blocked"
        metadata["title"] = "Changed after authorization"
        return store.get_content(store_id, doc_id)
    result = prepare_candidate_shortlist(store_id="store-A", hits=_hits((1, 1, 0.9)),
        principal=_principal(), get_metadata=lambda *args: metadata, get_content=content)
    assert result.pairs[0].title == "Learning 1"


def test_deferred_provenance_is_claimed_without_reading_text():
    store = _Store({1: _meta(1)})
    result = prepare_candidate_shortlist(store_id="store-A", hits=_hits((1, 1, 0.9)),
        principal=_principal(), get_metadata=store.get_metadata, get_content=store.get_content,
        max_pairs=0)
    descriptor = result.deferred[0]
    assert descriptor.store_id == "store-A"
    assert len(descriptor.content_sha256) == len(descriptor.evidence_sha256) == 64
    assert descriptor.excerpt is None
    assert store.content_calls == []

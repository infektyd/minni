"""Unit tests for engine/afm_chunking.py — the shared chunking primitives
used to keep native AFM calls under the ~4096-token context window without
silently truncating input."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def test_estimate_native_payload_tokens_counts_json():
    from afm_chunking import estimate_native_payload_tokens

    small = estimate_native_payload_tokens({"text": "hello"})
    large = estimate_native_payload_tokens({"text": "hello " * 500})
    assert small > 0
    assert large > small


def test_split_text_returns_single_chunk_when_under_budget():
    from afm_chunking import split_text

    text = "This is a short sentence. It fits easily."
    chunks = split_text(text, chunk_tokens=2400, overlap_tokens=300)
    assert chunks == [text]


def test_split_text_splits_long_text_into_multiple_chunks():
    from afm_chunking import split_text

    # ~3000 words, well over a 50-token chunk budget.
    sentence = "The quick brown fox jumps over the lazy dog. "
    text = sentence * 300
    chunks = split_text(text, chunk_tokens=50, overlap_tokens=10)
    assert len(chunks) > 1
    # Every chunk should itself be non-empty text.
    assert all(chunk.strip() for chunk in chunks)


def test_split_text_chunks_end_on_sentence_boundaries_when_possible():
    from afm_chunking import split_text

    sentence = "Alpha bravo charlie delta echo foxtrot golf hotel. "
    text = sentence * 40
    chunks = split_text(text, chunk_tokens=30, overlap_tokens=5)
    # Every chunk but possibly the last should end with sentence punctuation
    # (sentence-snap extends the window to the next ". ").
    for chunk in chunks[:-1]:
        assert chunk.rstrip().endswith(".")


def test_split_text_produces_overlap_between_consecutive_chunks():
    from afm_chunking import split_text

    words = [f"word{i}" for i in range(300)]
    text = " ".join(words)
    chunks = split_text(text, chunk_tokens=50, overlap_tokens=15)
    assert len(chunks) > 1
    first_chunk_words = chunks[0].split()
    second_chunk_words = chunks[1].split()
    # The tail of chunk 1 should reappear at the head of chunk 2.
    overlap_candidates = set(first_chunk_words[-15:])
    assert overlap_candidates & set(second_chunk_words[:20])


def test_split_list_by_token_budget_single_group_when_small():
    from afm_chunking import split_list_by_token_budget

    items = [{"title": "a"}, {"title": "b"}]
    groups = split_list_by_token_budget(items, budget_tokens=1000, serialize=str)
    assert groups == [items]


def test_split_list_by_token_budget_splits_when_over_budget():
    from afm_chunking import split_list_by_token_budget

    items = [{"title": f"item-{i}", "body": "x" * 200} for i in range(20)]
    groups = split_list_by_token_budget(items, budget_tokens=100, serialize=str)
    assert len(groups) > 1
    # Every item must appear exactly once across all groups.
    flattened = [item for group in groups for item in group]
    assert flattened == items


def test_split_list_by_token_budget_never_returns_empty_group_list():
    from afm_chunking import split_list_by_token_budget

    groups = split_list_by_token_budget([], budget_tokens=100, serialize=str)
    assert groups == [[]]


class _FakeResult:
    """Stand-in matching model_provider.ProviderResult for the fields the
    chunking wrapper reads (same shape used by engine/test_native_afm_helper_ops.py's
    _AFMResult)."""

    def __init__(self, ok, data, status="native_available", error=None):
        self.ok = ok
        self.data = data
        self.status = status
        self.error = error


class _FakeChain:
    """Routes chain.native_op(op_name, payload, timeout) through a supplied
    callback so tests can inspect every call made, including payload shape."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def native_op(self, op_name, payload, timeout=4.0):
        self.calls.append((op_name, dict(payload)))
        return self.handler(op_name, payload)


def test_call_native_op_chunked_passthrough_when_under_budget():
    from afm_chunking import call_native_op_chunked

    chain = _FakeChain(lambda op, payload: _FakeResult(True, {"summary": "ok"}))
    results, was_chunked = call_native_op_chunked(
        chain, "neighborhood_summary", {"prompt": "short prompt"}, text_field="prompt",
    )
    assert was_chunked is False
    assert len(results) == 1
    assert results[0].data == {"summary": "ok"}
    assert len(chain.calls) == 1


def test_call_native_op_chunked_splits_when_over_budget():
    from afm_chunking import call_native_op_chunked

    long_prompt = "word " * 5000  # far over the 3200-token default budget
    chain = _FakeChain(lambda op, payload: _FakeResult(True, {"summary": "partial"}))
    results, was_chunked = call_native_op_chunked(
        chain, "neighborhood_summary", {"prompt": long_prompt}, text_field="prompt",
    )
    assert was_chunked is True
    assert len(results) > 1
    assert len(chain.calls) == len(results)
    # Every chunked call's payload text should be smaller than the original.
    for _, payload in chain.calls:
        assert len(payload["prompt"]) < len(long_prompt)


def test_call_native_op_chunked_reactive_fallback_on_context_overflow():
    from afm_chunking import call_native_op_chunked

    # Payload looks small enough to estimate as under budget, but the first
    # call trips context_overflow anyway (estimate was wrong) — the wrapper
    # must fall through to chunking rather than giving up.
    call_count = {"n": 0}

    def handler(op, payload):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResult(False, {"error_kind": "context_overflow"}, status="native_available")
        return _FakeResult(True, {"summary": "recovered"})

    chain = _FakeChain(handler)
    prompt = "word " * 5000
    results, was_chunked = call_native_op_chunked(
        chain, "neighborhood_summary", {"prompt": prompt}, text_field="prompt",
        budget_tokens=999999,  # force the proactive check to pass through
    )
    assert was_chunked is True
    assert any(r.ok for r in results)


def test_call_native_op_chunked_recursively_shrinks_on_persistent_overflow():
    from afm_chunking import call_native_op_chunked, MIN_CHUNK_TOKENS

    # Every call reports context_overflow until the chunk size has been
    # nearly halved down toward the floor a couple of times.
    seen_payload_lengths = []

    def handler(op, payload):
        seen_payload_lengths.append(len(payload["prompt"]))
        if len(seen_payload_lengths) < 3:
            return _FakeResult(False, {"error_kind": "context_overflow"})
        return _FakeResult(True, {"summary": "ok"})

    chain = _FakeChain(handler)
    prompt = "word " * 2000
    results, was_chunked = call_native_op_chunked(
        chain, "neighborhood_summary", {"prompt": prompt}, text_field="prompt",
        chunk_tokens=1900,
    )
    assert was_chunked is True
    assert any(r.ok for r in results)
    # Payload sizes should shrink across the retried calls.
    assert seen_payload_lengths == sorted(seen_payload_lengths, reverse=True)


def test_call_native_op_chunked_drops_chunk_at_floor_without_infinite_loop():
    from afm_chunking import call_native_op_chunked

    chain = _FakeChain(lambda op, payload: _FakeResult(False, {"error_kind": "context_overflow"}))
    prompt = "word " * 2000
    results, was_chunked = call_native_op_chunked(
        chain, "neighborhood_summary", {"prompt": prompt}, text_field="prompt",
    )
    # Must terminate (this call itself finishing IS the assertion) and report
    # only failures, since nothing ever succeeded.
    assert all(not r.ok for r in results)


def test_reduce_via_same_op_returns_none_when_nothing_succeeded():
    from afm_chunking import reduce_via_same_op

    chain = _FakeChain(lambda op, payload: _FakeResult(False, {}))
    chunk_results = [_FakeResult(False, {}), _FakeResult(False, {})]
    reduced = reduce_via_same_op(
        chain, "neighborhood_summary", chunk_results,
        build_reduce_payload=lambda partials: {"prompt": "x"},
        text_field="prompt",
    )
    assert reduced is None


def test_reduce_via_same_op_returns_single_result_unreduced():
    from afm_chunking import reduce_via_same_op

    chain = _FakeChain(lambda op, payload: _FakeResult(True, {"summary": "should not be called"}))
    only_result = _FakeResult(True, {"summary": "the one answer"})
    reduced = reduce_via_same_op(
        chain, "neighborhood_summary", [only_result],
        build_reduce_payload=lambda partials: {"prompt": "x"},
        text_field="prompt",
    )
    assert reduced is only_result
    assert len(chain.calls) == 0  # no reduce call needed for a single result


def test_reduce_via_same_op_calls_op_again_to_synthesize_final_result():
    from afm_chunking import reduce_via_same_op

    def handler(op, payload):
        assert "partial 1" in payload["prompt"] and "partial 2" in payload["prompt"]
        return _FakeResult(True, {"summary": "synthesized"})

    chain = _FakeChain(handler)
    chunk_results = [
        _FakeResult(True, {"summary": "partial 1"}),
        _FakeResult(True, {"summary": "partial 2"}),
    ]
    reduced = reduce_via_same_op(
        chain, "neighborhood_summary", chunk_results,
        build_reduce_payload=lambda partials: {"prompt": "\n".join(p["summary"] for p in partials)},
        text_field="prompt",
    )
    assert reduced.data == {"summary": "synthesized"}
    assert len(chain.calls) == 1


def test_reduce_via_same_op_final_safety_net_falls_back_to_first_success():
    from afm_chunking import reduce_via_same_op

    chain = _FakeChain(lambda op, payload: _FakeResult(False, {"error_kind": "context_overflow"}))
    chunk_results = [
        _FakeResult(True, {"summary": "first success"}),
        _FakeResult(True, {"summary": "second success"}),
    ]
    reduced = reduce_via_same_op(
        chain, "neighborhood_summary", chunk_results,
        build_reduce_payload=lambda partials: {"prompt": "x" * 50000},
        text_field="prompt",
    )
    assert reduced is not None
    assert reduced.data["summary"] == "first success"

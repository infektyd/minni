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


def test_call_native_op_chunked_persistent_overflow_stays_bounded_in_call_count():
    from afm_chunking import call_native_op_chunked

    # Regression test for the combinatorial blow-up bug: overlap_tokens used
    # to stay fixed at its default (300) even as chunk_tokens shrank toward
    # MIN_CHUNK_TOKENS (200) during recursive re-splitting. Once
    # chunk_tokens <= overlap_tokens, split_text's step collapsed to 1,
    # producing roughly one chunk per word — and since every overflowing
    # chunk recurses, a 20,000-word input that overflows persistently used
    # to trigger on the order of millions of chain.native_op calls.
    #
    # With overlap_tokens properly clamped relative to chunk_tokens at every
    # recursive step, the number of calls must stay small and bounded no
    # matter how large the input or how persistent the overflow.
    chain = _FakeChain(lambda op, payload: _FakeResult(False, {"error_kind": "context_overflow"}))
    prompt = "word " * 20000
    results, was_chunked = call_native_op_chunked(
        chain, "neighborhood_summary", {"prompt": prompt}, text_field="prompt",
    )
    assert was_chunked is True
    assert all(not r.ok for r in results)
    # Sane, explicit bound: with the overlap clamp, the recursive re-split
    # tree has a small constant branching factor (~2-3 pieces per split) and
    # a small constant depth (bounded by log2(chunk_tokens / MIN_CHUNK_TOKENS)
    # ~= 4-5 halvings from the 2400-token default down to the 200-token
    # floor) — so the total call count is a small bounded constant,
    # independent of the input's word count. Before the fix this same input
    # drove step to 1 and produced on the order of millions of calls; a
    # correctly bounded tree stays well under 1000 regardless of input size.
    assert len(chain.calls) < 1000


def test_reduce_via_same_op_recurses_when_reduce_pass_itself_gets_chunked():
    from afm_chunking import reduce_via_same_op

    # Drives the recursive tree-reduction branch:
    #     if len(reduced_successes) > 1: return reduce_via_same_op(...)
    #
    # Scenario: 4 initial chunk_results succeed. build_reduce_payload joins
    # their summaries into one big "reduce-level-1" prompt that is itself
    # over budget, so call_native_op_chunked chunks it into 2 pieces. Both
    # of those reduce-level-1 sub-calls succeed, producing 2 successes —
    # forcing reduce_via_same_op to recurse once more on those 2 results.
    # That final reduce-level-2 call succeeds with a single synthesized
    # result, which should be what's ultimately returned.
    # Note: a text marker like "REDUCE1" can't be used to tag every chunked
    # sub-call, because split_text's sliding window only guarantees the
    # marker survives in whichever piece happens to contain the first word —
    # later pieces are plain substrings of the long filler text. Instead,
    # discriminate purely on payload length/shape, which every piece (and
    # the final small payload) reliably differs on.
    def handler(op, payload):
        prompt = payload["prompt"]
        if prompt.startswith("FINAL "):
            return _FakeResult(True, {"summary": "FINAL synthesized result"})
        # Any other call reaching native_op is one of the reduce-level-1
        # chunked sub-calls (long filler payload split into pieces) —
        # succeed each one so call_native_op_chunked reports 2+ successes.
        return _FakeResult(True, {"summary": "reduce1 partial (len=%d)" % len(prompt)})

    chain = _FakeChain(handler)

    chunk_results = [
        _FakeResult(True, {"summary": f"chunk summary {i}"}) for i in range(4)
    ]

    call_depth = {"n": 0}

    def build_reduce_payload(partials):
        call_depth["n"] += 1
        if call_depth["n"] == 1:
            # First reduce pass: build a payload big enough to itself
            # require chunking (forces call_native_op_chunked to split it).
            joined = " ".join(p["summary"] for p in partials)
            big_prompt = joined + " filler " * 5000
            return {"prompt": big_prompt}
        # Second reduce pass: small payload over the 2 reduce-level-1
        # results — stays under budget, single direct call.
        return {"prompt": "FINAL " + " ".join(p["summary"] for p in partials)}

    reduced = reduce_via_same_op(
        chain, "neighborhood_summary", chunk_results,
        build_reduce_payload=build_reduce_payload,
        text_field="prompt",
    )

    assert reduced is not None
    assert reduced.data["summary"] == "FINAL synthesized result"
    # Confirms the recursive path was actually taken: two build_reduce_payload
    # calls (level-1 chunked reduce, then level-2 final reduce over its 2
    # successes) rather than the single-pass or safety-net fallback path.
    assert call_depth["n"] == 2


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


def test_call_native_op_and_reduce_returns_result_when_under_budget():
    from afm_chunking import call_native_op_and_reduce

    chain = _FakeChain(lambda op, payload: _FakeResult(True, {"summary": "ok"}))
    result = call_native_op_and_reduce(
        chain, "neighborhood_summary", {"prompt": "short prompt"}, text_field="prompt",
        build_reduce_payload=lambda partials: {"prompt": "joined"},
    )
    assert result is not None
    assert result.data == {"summary": "ok"}
    assert len(chain.calls) == 1


def test_call_native_op_and_reduce_logs_error_kind_when_every_chunk_fails(caplog):
    import logging

    from afm_chunking import call_native_op_and_reduce

    long_prompt = "word " * 5000  # forces chunking
    chain = _FakeChain(lambda op, payload: _FakeResult(
        False, {"error_kind": "guardrail"}, status="native_error", error="blocked",
    ))
    with caplog.at_level(logging.INFO, logger="sovereign.afm_chunking"):
        result = call_native_op_and_reduce(
            chain, "session_distill", {"text": long_prompt}, text_field="text",
            build_reduce_payload=lambda partials: {"text": "joined"},
            trace_id="trace-total-failure",
        )
    assert result is None
    # The total-failure diagnostic must surface the classified error_kind and
    # trace id — this used to be silently dropped when result was None.
    assert any(
        "session_distill" in record.message and "guardrail" in record.message
        and "trace-total-failure" in record.message
        for record in caplog.records
    )


def test_call_native_op_and_reduce_logs_and_returns_none_on_single_failed_call(caplog):
    import logging

    from afm_chunking import call_native_op_and_reduce

    chain = _FakeChain(lambda op, payload: _FakeResult(
        False, {"error_kind": "unavailable"}, status="native_error", error="down",
    ))
    with caplog.at_level(logging.INFO, logger="sovereign.afm_chunking"):
        result = call_native_op_and_reduce(
            chain, "triage", {"candidate": "short"}, text_field="candidate",
            build_reduce_payload=lambda partials: {"candidate": "joined"},
            trace_id="trace-single",
        )
    assert result is None
    assert any(
        "triage" in record.message and "trace-single" in record.message
        for record in caplog.records
    )


def test_call_native_op_and_reduce_fold_merges_chunk_data_without_reduce_call():
    from afm_chunking import call_native_op_and_reduce

    long_text = "word " * 5000  # forces chunking
    chain = _FakeChain(lambda op, payload: _FakeResult(True, {"contradicts": False, "reason": "fine"}))
    folded_inputs = []

    def fold(successes):
        folded_inputs.extend(successes)
        return {"contradicts": any(s.get("contradicts") for s in successes), "reason": "folded"}

    result = call_native_op_and_reduce(
        chain, "contradiction", {"existing": "e", "candidate": long_text},
        text_field="candidate", fold=fold,
    )
    assert result is not None
    assert result.ok is True
    assert result.data["reason"] == "folded"
    assert len(folded_inputs) == len(chain.calls)  # fold saw every chunk success
    # Deterministic fold: no extra AFM reduce call beyond the map-phase chunks.
    assert all(op == "contradiction" for op, _ in chain.calls)


def test_env_override_changes_proactive_chunk_trigger(monkeypatch):
    from afm_chunking import call_native_op_chunked

    text = "word " * 600  # ~600 tokens: under the 3200 default, over a 100 override
    chain = _FakeChain(lambda op, payload: _FakeResult(True, {"summary": "ok"}))
    _, was_chunked = call_native_op_chunked(chain, "op", {"text": text}, text_field="text")
    assert was_chunked is False

    monkeypatch.setenv("MINNI_AFM_INPUT_BUDGET_TOKENS", "100")
    chain_low = _FakeChain(lambda op, payload: _FakeResult(True, {"summary": "ok"}))
    _, was_chunked_low = call_native_op_chunked(chain_low, "op", {"text": text}, text_field="text")
    assert was_chunked_low is True
    assert len(chain_low.calls) > 1


def test_oversized_non_text_fields_fail_fast_with_distinct_log(caplog):
    import logging

    from afm_chunking import call_native_op_chunked

    chain = _FakeChain(lambda op, payload: _FakeResult(False, {"error_kind": "context_overflow"}))
    # Sidecar alone exceeds the budget; the text field is short.
    payload = {"existing": "fact " * 8000, "candidate": "One short sentence."}
    with caplog.at_level(logging.INFO, logger="sovereign.afm_chunking"):
        results, was_chunked = call_native_op_chunked(
            chain, "contradiction", payload, text_field="candidate",
        )
    # No pointless halving loop: exactly one call, clearly diagnosed.
    assert len(chain.calls) == 1
    assert was_chunked is False
    assert any("text chunking cannot help" in rec.message for rec in caplog.records)


def test_chunk_sizing_subtracts_non_text_field_tokens():
    from afm_chunking import call_native_op_chunked, estimate_native_payload_tokens

    chain = _FakeChain(lambda op, payload: _FakeResult(True, {"contradicts": False, "reason": "ok"}))
    # Moderate sidecar (~1800 tokens) + long text: chunks must be sized to the
    # remaining room so each chunked payload stays near the budget instead of
    # blowing past it by the sidecar's size.
    payload = {"existing": "fact " * 1800, "candidate": "word " * 4000}
    budget = 3200
    results, was_chunked = call_native_op_chunked(
        chain, "contradiction", payload, text_field="candidate", budget_tokens=budget,
    )
    assert was_chunked is True
    assert len(chain.calls) > 1
    for _, chunk_payload in chain.calls:
        # Word-approximation slack (sentence snap + tokenizer variance) is
        # bounded; a sidecar-blind split would exceed the budget by ~1800.
        assert estimate_native_payload_tokens(chunk_payload) <= budget * 1.25

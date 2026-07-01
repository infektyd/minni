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

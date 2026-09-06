"""Tests for batched edge classification prompt substrate and response validator (P1.2).

Verifies:
- Prompt template loading and required placeholder / label definitions.
- Token counting honesty (measured tiktoken cl100k_base vs heuristic fallback).
- AFM input token budget adherence (<= 3,200 tokens total for instructions + <= 8 pairs with excerpts <= 220 tokens).
- Hard ceiling on candidate pairs (<= 8 pairs).
- Fail-loud batch response validation matrix covering all failure modes.
"""

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.edge_inference import (
    AFM_INPUT_BUDGET_TOKENS,
    MAX_CANDIDATE_PAIRS,
    MAX_EXCERPT_TOKENS,
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
    VALID_DIRECTIONS,
    VALID_EDGE_LABELS,
    count_tokens,
    format_numbered_excerpt,
    get_edge_inference_prompt_path,
    load_edge_inference_prompt_template,
    render_edge_inference_prompt,
    truncate_to_tokens,
    validate_edge_inference_response,
)


def test_prompt_template_exists_and_contains_contract_elements():
    """Verify prompt template exists, loads, and contains all canonical edge types and placeholders."""
    path = get_edge_inference_prompt_path()
    assert path.is_file()

    template = load_edge_inference_prompt_template()
    assert "# Batched Typed Memory Graph Edge Inference (v1)" in template
    assert "{source_learning}" in template
    assert "{candidate_pairs}" in template

    for label in ("updates", "extends", "contradicts", "relates", "none"):
        assert label in template

    for direction in ("forward", "backward", "mutual", "none"):
        assert direction in template

    assert "UNTRUSTED CONTENT" in template
    assert "FAIL-LOUD SCHEMA COMPLIANCE" in template


def test_token_counting_honesty():
    """Verify token counting distinguishes measured tiktoken cl100k_base from heuristic fallback."""
    sample_text = "The quick brown fox jumps over the lazy dog."
    measured_count, is_measured = count_tokens(sample_text)
    assert is_measured is True
    assert measured_count > 0

    # Test fallback behavior when tiktoken is unavailable
    import minni.edge_inference as mod

    orig_import = __import__

    def mock_import(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("Mock missing tiktoken")
        return orig_import(name, *args, **kwargs)

    import builtins

    orig_builtin_import = builtins.__import__
    try:
        builtins.__import__ = mock_import
        fallback_count, is_fallback_measured = count_tokens(sample_text)
        assert is_fallback_measured is False
        assert fallback_count > 0
    finally:
        builtins.__import__ = orig_builtin_import


def test_truncate_to_tokens():
    """Verify text is strictly truncated to token ceiling."""
    # Generate long text of 500 repeated words
    long_text = " ".join([f"token_{i}" for i in range(500)])
    orig_count, _ = count_tokens(long_text)
    assert orig_count > 220

    truncated, count, is_measured = truncate_to_tokens(long_text, max_tokens=220)
    assert is_measured is True
    assert count <= 220
    assert len(truncated) < len(long_text)


def test_format_numbered_excerpt():
    """Verify excerpt is formatted into numbered lines with token bounds."""
    text = "Line one about SQLite architecture.\nLine two about FAISS indexing.\nLine three about AFM loop."
    formatted, lines, tokens, is_measured = format_numbered_excerpt(text, max_tokens=100)

    assert is_measured is True
    assert len(lines) == 3
    assert "[1] Line one about SQLite architecture." in formatted
    assert "[2] Line two about FAISS indexing." in formatted
    assert "[3] Line three about AFM loop." in formatted


def test_format_numbered_excerpt_preserves_capped_single_line():
    """A single-line body at the cap retains evidence after numbering."""
    text = "single line evidence " * 100
    formatted, lines, tokens, is_measured = format_numbered_excerpt(text, max_tokens=20)

    assert is_measured is True
    assert formatted.startswith("[1] single line evidence")
    # lines are raw evidence lines; numbering is exactly one prefix + raw line.
    assert len(lines) == 1
    assert formatted == f"[1] {lines[0]}"
    assert 0 < tokens <= 20


def test_format_numbered_excerpt_keeps_empty_body_uncitable():
    formatted, lines, tokens, is_measured = format_numbered_excerpt("", max_tokens=20)

    assert formatted == ""
    assert lines == []
    assert tokens == 0
    assert is_measured is True


def test_render_edge_inference_prompt_within_afm_budget():
    """Render prompt with source and 8 long candidates. Must strictly fit in <= 3,200 tokens."""
    source = {
        "learning_id": 101,
        "title": "SQLite WAL Mode and Concurrent Reads",
        "applies_when": "when database concurrency is configured",
        "created_at": "2026-07-09T12:00:00Z",
        "body": " ".join(["sqlite_wal_token"] * 400),  # deliberately oversized
    }

    # 8 candidates, each with an oversized body of 400 tokens
    candidates = [
        {
            "pair_id": f"pair_{i}",
            "doc_id": f"doc_{i}",
            "page_type": "learning",
            "status": "accepted",
            "applies_when": "general storage",
            "created_at": "2026-07-08T10:00:00Z",
            "body": f"Candidate fact {i}: " + " ".join([f"content_word_{i}"] * 350),
        }
        for i in range(1, 9)
    ]

    result = render_edge_inference_prompt(source, candidates)

    assert result.is_measured is True
    assert len(result.pair_ids) == 8
    assert result.pair_ids == [f"pair_{i}" for i in range(1, 9)]
    assert result.budget_limit == AFM_INPUT_BUDGET_TOKENS
    assert result.budget_exceeded is False

    # Each candidate excerpt must have been truncated to <= 220 tokens
    for pair_id, cand_toks in result.candidate_tokens.items():
        assert cand_toks <= 260  # excerpt tokens + line number prefixes

    # Total prompt tokens must be strictly <= 3,200
    assert result.total_tokens <= AFM_INPUT_BUDGET_TOKENS
    assert result.total_tokens > 1000  # Non-vacuity: verified real content was rendered


def test_render_caps_at_max_eight_candidate_pairs():
    """If 12 candidates are shortlisted, render must cap strictly at 8."""
    source = {"learning_id": 1, "body": "source content"}
    candidates = [{"pair_id": f"p_{i}", "body": f"candidate {i}"} for i in range(12)]

    result = render_edge_inference_prompt(source, candidates)
    assert len(result.pair_ids) == MAX_CANDIDATE_PAIRS
    assert result.pair_ids == [f"p_{i}" for i in range(8)]


def test_validate_edge_inference_response_success():
    """Verify valid batch response parses cleanly with all required fields."""
    expected_pair_ids = ["pair_1", "pair_2", "pair_3", "pair_4", "pair_5"]
    line_counts = {"pair_1": 4, "pair_2": 3, "pair_3": 5, "pair_4": 2, "pair_5": 3}

    raw_response = json.dumps(
        [
            {
                "pair_id": "pair_1",
                "label": "updates",
                "direction": "forward",
                "confidence": 0.95,
                "supporting_evidence_indices": [1, 2],
                "rationale": "Source supersedes target config with new pool size.",
            },
            {
                "pair_id": "pair_2",
                "label": "extends",
                "direction": "forward",
                "confidence": 0.88,
                "supporting_evidence_indices": [3],
                "rationale": "Source adds details on error recovery without contradiction.",
            },
            {
                "pair_id": "pair_3",
                "label": "contradicts",
                "direction": "mutual",
                "confidence": 0.92,
                "supporting_evidence_indices": [2, 4],
                "rationale": "Source states port is 8080 while target asserts 9090.",
            },
            {
                "pair_id": "pair_4",
                "label": "relates",
                "direction": "mutual",
                "confidence": 0.80,
                "supporting_evidence_indices": [1],
                "rationale": "Both relate to daemon networking.",
            },
            {
                "pair_id": "pair_5",
                "label": "none",
                "direction": "none",
                "confidence": 0.15,
                "supporting_evidence_indices": [],
                "rationale": "No semantic relationship between facts.",
            },
        ]
    )

    is_valid, edges, err = validate_edge_inference_response(
        raw_response, expected_pair_ids, line_counts_per_pair=line_counts
    )
    assert is_valid is True
    assert err is None
    assert len(edges) == 5

    assert edges[0].pair_id == "pair_1"
    assert edges[0].label == "updates"
    assert edges[0].confidence == 0.95
    assert edges[0].supporting_evidence_indices == (1, 2)


@pytest.mark.parametrize(
    "corrupt_input,expected_error_substring",
    [
        ("not json at all", "json_decode_error"),
        ('{"pair_id": "pair_1"}', "schema_error"),  # Dict instead of List
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "forward", "confidence": 0.9, "supporting_evidence_indices": [1], "rationale": "ok"}]',
            "missing_pair_ids",  # missing pair_2
        ),
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "forward", "confidence": 0.9, "supporting_evidence_indices": [1], "rationale": "ok"}, {"pair_id": "pair_unknown", "label": "none", "direction": "none", "confidence": 0.1, "supporting_evidence_indices": [], "rationale": "extra"}]',
            "unknown_pair_id",
        ),
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "forward", "confidence": 0.9, "supporting_evidence_indices": [1], "rationale": "ok"}, {"pair_id": "pair_1", "label": "none", "direction": "none", "confidence": 0.1, "supporting_evidence_indices": [], "rationale": "dup"}]',
            "duplicate_pair_id",
        ),
        (
            '[{"pair_id": "pair_1", "label": "supersedes", "direction": "forward", "confidence": 0.9, "supporting_evidence_indices": [1], "rationale": "ok"}]',
            "invalid_label",  # supersedes is not an allowed enum label (must be updates)
        ),
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "sideways", "confidence": 0.9, "supporting_evidence_indices": [1], "rationale": "ok"}]',
            "invalid_direction",
        ),
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "forward", "confidence": "high", "supporting_evidence_indices": [1], "rationale": "ok"}]',
            "invalid_confidence",
        ),
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "forward", "confidence": 1.5, "supporting_evidence_indices": [1], "rationale": "ok"}]',
            "invalid_confidence_range",
        ),
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "forward", "confidence": 0.9, "supporting_evidence_indices": [99], "rationale": "ok"}]',
            "evidence_index_out_of_range",  # line count is 3, 99 is invalid
        ),
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "forward", "confidence": 0.9, "supporting_evidence_indices": [], "rationale": "ok"}]',
            "missing_evidence",
        ),
        (
            '[{"pair_id": "pair_1", "label": "updates", "direction": "forward", "confidence": 0.9, "supporting_evidence_indices": [1], "rationale": ""}]',
            "missing_or_empty_rationale",
        ),
    ],
)
def test_validate_edge_inference_fail_loud_matrix(corrupt_input, expected_error_substring):
    """Verify non-vacuity and strict fail-loud behavior on malformed classifier output."""
    expected_pair_ids = ["pair_1"] if "missing_pair_ids" not in expected_error_substring else ["pair_1", "pair_2"]
    line_counts = {"pair_1": 3, "pair_2": 3}

    is_valid, edges, err = validate_edge_inference_response(
        corrupt_input, expected_pair_ids, line_counts_per_pair=line_counts
    )
    assert is_valid is False
    assert edges is None
    assert err is not None
    assert expected_error_substring in err


def _valid_item(pair_id="pair_1", **overrides):
    item = {
        "pair_id": pair_id,
        "label": "updates",
        "direction": "forward",
        "confidence": 0.9,
        "supporting_evidence_indices": [1],
        "rationale": "ok",
    }
    item.update(overrides)
    return item


def test_validate_rejects_bool_confidence():
    """Booleans are ints in Python; True/False must not pass as confidences."""
    for bad in (True, False):
        raw = json.dumps([_valid_item(confidence=bad)])
        is_valid, edges, err = validate_edge_inference_response(
            raw, ["pair_1"], line_counts_per_pair={"pair_1": 3}
        )
        assert is_valid is False
        assert edges is None
        assert err is not None and "invalid_confidence" in err


def test_validate_accepts_int_confidence_in_range():
    """Plain ints 0/1 are valid floats per the Output Schema range (no overreach)."""
    for good in (0, 1):
        raw = json.dumps([_valid_item(confidence=good)])
        is_valid, edges, err = validate_edge_inference_response(
            raw, ["pair_1"], line_counts_per_pair={"pair_1": 3}
        )
        assert is_valid is True, err
        assert edges is not None and edges[0].confidence == float(good)


def test_validate_rejects_unknown_and_missing_fields():
    """Each item must carry exactly the six Output Schema keys."""
    extra = _valid_item()
    extra["injected"] = "prompt"
    is_valid, _, err = validate_edge_inference_response(
        json.dumps([extra]), ["pair_1"], line_counts_per_pair={"pair_1": 3}
    )
    assert is_valid is False
    assert err is not None and "unknown_field" in err

    dropped = _valid_item()
    del dropped["rationale"]
    is_valid, _, err = validate_edge_inference_response(
        json.dumps([dropped]), ["pair_1"], line_counts_per_pair={"pair_1": 3}
    )
    assert is_valid is False
    assert err is not None and "missing_field" in err


def test_validate_rejects_duplicate_json_keys():
    """json.loads collapses repeats; a masked pair_id must fail, not last-win."""
    raw = (
        '[{"pair_id": "pair_1", "pair_id": "pair_unknown", "label": "updates", '
        '"direction": "forward", "confidence": 0.9, '
        '"supporting_evidence_indices": [1], "rationale": "ok"}]'
    )
    is_valid, edges, err = validate_edge_inference_response(
        raw, ["pair_1"], line_counts_per_pair={"pair_1": 3}
    )
    assert is_valid is False
    assert edges is None
    assert err is not None and "duplicate_field" in err


def test_validate_rejects_malformed_expected_pair_ids():
    """Caller-side ambiguity (dupes, empties, non-strings) fails the batch."""
    raw = json.dumps([_valid_item()])
    line_counts = {"pair_1": 3}
    for bad_expected, code in (
        (["pair_1", "pair_1"], "duplicate_expected_pair_id"),
        (["pair_1", ""], "invalid_expected_pair_id"),
        (["pair_1", 7], "invalid_expected_pair_id"),
        (None, "invalid_expected_pair_ids"),
    ):
        is_valid, edges, err = validate_edge_inference_response(
            raw, bad_expected, line_counts_per_pair=line_counts
        )
        assert is_valid is False
        assert edges is None
        assert err is not None and code in err


@pytest.mark.parametrize(
    "label,direction,compatible",
    [
        ("updates", "forward", True),
        ("updates", "backward", True),
        ("extends", "forward", True),
        ("extends", "backward", True),
        ("contradicts", "mutual", True),
        ("contradicts", "forward", True),  # template allows forward/backward
        ("contradicts", "backward", True),
        ("relates", "mutual", True),
        ("none", "none", True),
        ("relates", "forward", False),
        ("relates", "none", False),
        ("none", "forward", False),
        ("none", "mutual", False),
        ("updates", "none", False),
        ("updates", "mutual", False),
        ("extends", "mutual", False),
        ("contradicts", "none", False),
    ],
)
def test_validate_label_direction_compatibility(label, direction, compatible):
    """Enforce exactly the normative compatibility matrix from the prompt template."""
    raw = json.dumps([_valid_item(label=label, direction=direction)])
    is_valid, edges, err = validate_edge_inference_response(
        raw, ["pair_1"], line_counts_per_pair={"pair_1": 3}
    )
    if compatible:
        assert is_valid is True, err
        assert edges is not None and len(edges) == 1
    else:
        assert is_valid is False
        assert edges is None
        assert err is not None and "incompatible_label_direction" in err


@pytest.mark.parametrize("length,valid", [(200, True), (201, False)])
def test_rationale_matches_prompt_limit(length, valid):
    result, _, error = validate_edge_inference_response(
        [_valid_item(rationale="x" * length)],
        ["pair_1"],
        line_counts_per_pair={"pair_1": 3},
    )
    assert result is valid
    if not valid:
        assert "rationale_too_long" in error


def _fat_candidate(pair_id, words=350):
    return {
        "pair_id": pair_id,
        "doc_id": f"doc_{pair_id}",
        "body": f"Candidate {pair_id}: " + " ".join([f"content_{pair_id}"] * words),
    }


def test_render_enforces_total_budget_with_oversized_title():
    """A 16k-char title must be metadata-capped, not returned over budget."""
    source = {
        "learning_id": 7,
        "title": "T" * 16000,
        "body": " ".join(["src_token"] * 200),
    }
    candidates = [_fat_candidate(f"pair_{i}") for i in range(1, 9)]
    result = render_edge_inference_prompt(source, candidates)
    assert result.budget_exceeded is False
    assert result.total_tokens <= AFM_INPUT_BUDGET_TOKENS
    assert len(result.pair_ids) == 8
    assert result.excluded_pair_ids == []


def test_render_drops_pairs_to_fit_budget_and_reports_excluded():
    """Over-budget renders drop last pairs (re-batchable) instead of flag-only."""
    source = {"learning_id": 1, "body": " ".join(["src_token"] * 200)}
    candidates = [_fat_candidate(f"pair_{i}") for i in range(1, 9)]
    result = render_edge_inference_prompt(source, candidates, budget_limit=1200)
    assert result.budget_exceeded is False
    assert result.total_tokens <= 1200
    assert len(result.pair_ids) < 8
    assert result.pair_ids + result.excluded_pair_ids == [f"pair_{i}" for i in range(1, 9)]
    assert set(result.line_counts_per_pair) == set(result.pair_ids)


def test_numbered_excerpt_keeps_complete_raw_lines():
    """Fallback truncation must not leave a dangling prefix or numbered raw_lines."""
    with mock.patch.dict(sys.modules, {"tiktoken": None}):
        formatted, raw_lines, _, measured = format_numbered_excerpt(
            "alpha\nbeta\ngamma", max_tokens=3
        )
    assert measured is False
    # Number prefixes consume budget too: "[1] alpha\n[2] beta" exceeds the
    # 3-token cap, so whole trailing lines drop — prefixes never split.
    assert raw_lines == ["alpha"]
    assert formatted == "[1] alpha"
    assert all("[" not in line for line in raw_lines)

    with mock.patch.dict(sys.modules, {"tiktoken": None}):
        formatted, raw_lines, _, _ = format_numbered_excerpt(
            "alpha\nbeta\ngamma", max_tokens=6
        )
    assert raw_lines == ["alpha", "beta"]
    assert formatted == "[1] alpha\n[2] beta"

    with mock.patch.dict(sys.modules, {"tiktoken": None}):
        formatted, raw_lines, _, _ = format_numbered_excerpt("abcdefghij", max_tokens=1)
    assert raw_lines == ["abcd"]
    assert formatted == "[1] abcd"


def test_metadata_inside_untrusted_boundary():
    """Source and candidate metadata must sit inside untrusted-evidence markers."""
    source = {"learning_id": 1, "title": "Secret Title", "body": "hello world"}
    candidates = [{"pair_id": "p1", "body": "candidate body here"}]
    result = render_edge_inference_prompt(source, candidates)
    text = result.prompt_text
    assert UNTRUSTED_BEGIN in text and UNTRUSTED_END in text
    first_begin = text.index(UNTRUSTED_BEGIN)
    last_end = text.rindex(UNTRUSTED_END)
    assert first_begin < text.index("Secret Title") < last_end
    assert first_begin < text.index("Candidate Pair: p1") < last_end


def test_strict_parser_rejects_trailing_text_and_preamble():
    """Fragments around the array fail closed; a whole-text fence still parses."""
    good = json.dumps([_valid_item()])
    counts = {"pair_1": 3}
    is_valid, _, err = validate_edge_inference_response(
        good + " trailing junk", ["pair_1"], line_counts_per_pair=counts
    )
    assert is_valid is False and "json_decode_error" in err
    is_valid, _, err = validate_edge_inference_response(
        "note: classify this " + good, ["pair_1"], line_counts_per_pair=counts
    )
    assert is_valid is False and "json_decode_error" in err
    is_valid, edges, err = validate_edge_inference_response(
        "```json\n" + good + "\n```", ["pair_1"], line_counts_per_pair=counts
    )
    assert is_valid is True, err
    assert edges is not None and len(edges) == 1


def test_missing_malformed_line_counts_rejected():
    """Absent, incomplete, or non-positive line-count maps fail the batch."""
    good = json.dumps([_valid_item()])
    is_valid, _, err = validate_edge_inference_response(good, ["pair_1"])
    assert is_valid is False and "missing_line_counts" in (err or "")
    is_valid, _, err = validate_edge_inference_response(
        good, ["pair_1"], line_counts_per_pair={"other": 3}
    )
    assert is_valid is False and "incomplete_line_counts" in (err or "")
    for bad_counts, code in (
        ({"pair_1": -1}, "invalid_line_counts"),
        ({"pair_1": True}, "invalid_line_counts"),
        ({"pair_1": "3"}, "invalid_line_counts"),
    ):
        is_valid, _, err = validate_edge_inference_response(
            good, ["pair_1"], line_counts_per_pair=bad_counts
        )
        assert is_valid is False and code in (err or "")

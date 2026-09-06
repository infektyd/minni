"""Deterministic unit tests for EdgeClassifier adapter (P1).

Verifies:
- Pure prepare-only behavior: zero DB writes, zero network, zero live vault dependencies.
- No-candidates clean handling.
- Valid batched classification with cryptographic evidence hashes.
- Structural rejection of non-local or cloud providers.
- Provider unavailability and exception handling.
- Whole-batch fail-loud validation matrix:
  - malformed JSON
  - partial / missing pair IDs
  - duplicate pair IDs
  - unknown pair IDs
  - invalid labels or incompatible directions
  - invalid evidence line references (out of range)
- Token overflow handling.
- Explicit accounting of excluded pairs as unclassified/remaining (never falsely complete).
- Immutability of ClassificationBatchResult.
"""

from dataclasses import FrozenInstanceError
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.edge_classifier import (
    ClassificationBatchResult,
    EdgeClassifier,
    classify_learning_edges,
    compute_canonical_hash,
)
from minni.edge_inference import AFM_INPUT_BUDGET_TOKENS, MAX_CANDIDATE_PAIRS
from minni.model_provider import ChatRequest, OperationClass, ProviderChain, ProviderResult


class MockLocalProvider:
    """Deterministic in-memory local provider for hermetic testing."""

    def __init__(
        self,
        name: str = "mock_local_afm",
        tier: str = "local",
        response_data: Optional[Dict[str, Any]] = None,
        should_fail: bool = False,
        error_msg: str = "Mock provider failure",
        raise_exc: Optional[Exception] = None,
    ):
        self.name = name
        self.tier = tier
        self.response_data = response_data or {}
        self.should_fail = should_fail
        self.error_msg = error_msg
        self.raise_exc = raise_exc
        self.calls: List[ChatRequest] = []

    def supports(self, operation: OperationClass) -> bool:
        return operation == "edge_inference"

    def chat(self, request: ChatRequest, client: Optional[Any] = None) -> ProviderResult:
        self.calls.append(request)
        if self.raise_exc:
            raise self.raise_exc
        if self.should_fail:
            return ProviderResult(
                ok=False,
                data={},
                provider=self.name,
                status="error",
                error=self.error_msg,
            )
        return ProviderResult(
            ok=True,
            data=self.response_data,
            provider=self.name,
            status="ok",
            error=None,
        )


def _make_source(learning_id: int = 101) -> Dict[str, Any]:
    return {
        "learning_id": learning_id,
        "title": "SQLite WAL Concurrency",
        "applies_when": "when database operations require concurrent readers",
        "created_at": "2026-07-09T10:00:00Z",
        "body": "WAL mode allows readers to proceed without blocking writers.\nReaders read from shared memory index.\nCommit updates the wal-index.",
    }


def _make_candidates(count: int = 3) -> List[Dict[str, Any]]:
    cands = []
    for i in range(1, count + 1):
        cands.append(
            {
                "pair_id": f"pair_{i}",
                "doc_id": f"doc_{i}",
                "page_type": "learning",
                "status": "accepted",
                "applies_when": f"storage scenario {i}",
                "created_at": f"2026-07-0{min(i, 8)}T09:00:00Z",
                "body": f"Storage fact {i} details.\nSecond line of evidence for candidate {i}.",
            }
        )
    return cands


def _valid_model_response(pair_ids: List[str]) -> Dict[str, Any]:
    """Build a valid model chat response for the given pair IDs."""
    items = []
    labels = ["updates", "extends", "none", "contradicts", "relates"]
    directions = ["forward", "forward", "none", "mutual", "mutual"]
    for idx, pid in enumerate(pair_ids):
        lbl = labels[idx % len(labels)]
        dirn = directions[idx % len(directions)]
        evidence = [1] if lbl != "none" else []
        items.append(
            {
                "pair_id": pid,
                "label": lbl,
                "direction": dirn,
                "confidence": 0.90 if lbl != "none" else 0.10,
                "supporting_evidence_indices": evidence,
                "rationale": f"Valid test rationale for {pid}",
            }
        )
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(items),
                }
            }
        ]
    }


# --- Test Cases ---


def test_edge_classifier_no_candidates_returns_clean_noop():
    """When candidates is empty, classifier returns no_candidates without invoking provider."""
    source = _make_source()
    provider = MockLocalProvider()
    chain = ProviderChain([provider])

    classifier = EdgeClassifier(provider_chain=chain)
    result = classifier.classify(source, candidates=[])

    assert result.status == "no_candidates"
    assert result.ok is True
    assert result.edges == ()
    assert result.classified_pair_ids == ()
    assert result.unclassified_pair_ids == ()
    assert result.total_tokens == 0
    assert result.is_measured is True
    assert result.error is None
    assert result.evidence_hash != ""
    assert provider.calls == []


def test_edge_classifier_valid_batch_execution_and_evidence_hashes():
    """Verify successful classification, validated edges, and reproducible evidence hashes."""
    source = _make_source()
    candidates = _make_candidates(3)
    pair_ids = ["pair_1", "pair_2", "pair_3"]

    provider = MockLocalProvider(
        name="local_afm_test",
        response_data=_valid_model_response(pair_ids),
    )
    chain = ProviderChain([provider])

    classifier = EdgeClassifier(provider_chain=chain)
    result = classifier.classify(source, candidates)

    assert result.status == "ok"
    assert result.ok is True
    assert len(result.edges) == 3
    assert result.classified_pair_ids == ("pair_1", "pair_2", "pair_3")
    assert result.unclassified_pair_ids == ()
    assert result.model_id == "local_afm_test:unknown_model_revision"
    assert result.prompt_version == "edge_inference_v1"
    assert result.total_tokens > 0
    assert result.is_measured is True
    assert len(provider.calls) == 1

    # Verify edge details
    edge0 = result.edges[0]
    assert edge0.pair_id == "pair_1"
    assert edge0.label == "updates"
    assert edge0.direction == "forward"
    assert edge0.confidence == 0.90
    assert edge0.supporting_evidence_indices == (1,)

    # Revalidation Proof: Evidence hash matches deterministic formula including output_hash
    expected_recomputed_hash = hashlib.sha256(
        f"{result.source_hash}:{result.batch_candidates_hash}:{result.prompt_hash}:{result.output_hash}:{result.prompt_version}:{result.model_id}".encode(
            "utf-8"
        )
    ).hexdigest()
    assert result.evidence_hash == expected_recomputed_hash


def test_edge_classifier_rejects_nonlocal_provider_route():
    """Classifier structurally rejects any provider that is not local tier."""
    source = _make_source()
    candidates = _make_candidates(2)

    # 1. Chain with only cloud provider: chain.providers_for filters it out
    cloud_provider = MockLocalProvider(name="cloud_openai", tier="cloud")
    chain = ProviderChain([cloud_provider])

    classifier = EdgeClassifier(provider_chain=chain)
    result = classifier.classify(source, candidates)

    assert result.status == "unsupported_route"
    assert result.ok is False
    assert result.edges == ()
    assert result.unclassified_pair_ids == ("pair_1", "pair_2")
    assert "no_local_provider" in (result.error or "")
    assert cloud_provider.calls == []

    # 2. Direct provider injection with nonlocal tier: classifier tier-guard rejects it
    classifier_direct = EdgeClassifier(provider_chain=cloud_provider)
    result_direct = classifier_direct.classify(source, candidates)

    assert result_direct.status == "unsupported_route"
    assert result_direct.ok is False
    assert result_direct.edges == ()
    assert result_direct.unclassified_pair_ids == ("pair_1", "pair_2")
    assert "no_local_provider" in (result_direct.error or "")
    assert cloud_provider.calls == []


def test_edge_classifier_provider_unavailable():
    """Provider failure returns provider_unavailable and leaves candidates unclassified."""
    source = _make_source()
    candidates = _make_candidates(2)

    provider = MockLocalProvider(
        should_fail=True,
        error_msg="Local AFM service connection refused",
    )
    chain = ProviderChain([provider])

    classifier = EdgeClassifier(provider_chain=chain)
    result = classifier.classify(source, candidates)

    assert result.status == "provider_unavailable"
    assert result.ok is False
    assert result.edges == ()
    assert result.unclassified_pair_ids == ("pair_1", "pair_2")
    assert "Local AFM service connection refused" in (result.error or "")


def test_edge_classifier_provider_exception_handled_gracefully():
    """Exception raised by provider call returns provider_unavailable without unhandled crash."""
    source = _make_source()
    candidates = _make_candidates(2)

    provider = MockLocalProvider(raise_exc=RuntimeError("Unexpected socket EOF"))
    chain = ProviderChain([provider])

    classifier = EdgeClassifier(provider_chain=chain)
    result = classifier.classify(source, candidates)

    assert result.status == "provider_unavailable"
    assert result.ok is False
    assert "Unexpected socket EOF" in (result.error or "")
    assert result.unclassified_pair_ids == ("pair_1", "pair_2")


def test_edge_classifier_empty_completion_text_fails_loud():
    """Provider returns ok=True but empty completion content."""
    source = _make_source()
    candidates = _make_candidates(2)

    provider = MockLocalProvider(response_data={"choices": []})
    chain = ProviderChain([provider])

    classifier = EdgeClassifier(provider_chain=chain)
    result = classifier.classify(source, candidates)

    assert result.status == "validation_failed"
    assert result.ok is False
    assert "empty_completion" in (result.error or "")
    assert result.unclassified_pair_ids == ("pair_1", "pair_2")


@pytest.mark.parametrize(
    "corrupt_content,expected_error_keyword",
    [
        ("not valid json at all", "json_decode_error"),
        (
            json.dumps(
                [
                    {
                        "pair_id": "pair_1",
                        "label": "updates",
                        "direction": "forward",
                        "confidence": 0.9,
                        "supporting_evidence_indices": [1],
                        "rationale": "ok",
                    }
                ]
            ),
            "missing_pair_ids",  # 2 candidates submitted, only 1 returned
        ),
        (
            json.dumps(
                [
                    {
                        "pair_id": "pair_1",
                        "label": "updates",
                        "direction": "forward",
                        "confidence": 0.9,
                        "supporting_evidence_indices": [1],
                        "rationale": "ok",
                    },
                    {
                        "pair_id": "pair_1",
                        "label": "none",
                        "direction": "none",
                        "confidence": 0.1,
                        "supporting_evidence_indices": [],
                        "rationale": "dup",
                    },
                ]
            ),
            "duplicate_pair_id",
        ),
        (
            json.dumps(
                [
                    {
                        "pair_id": "pair_1",
                        "label": "updates",
                        "direction": "forward",
                        "confidence": 0.9,
                        "supporting_evidence_indices": [1],
                        "rationale": "ok",
                    },
                    {
                        "pair_id": "pair_unknown",
                        "label": "none",
                        "direction": "none",
                        "confidence": 0.1,
                        "supporting_evidence_indices": [],
                        "rationale": "unknown",
                    },
                ]
            ),
            "unknown_pair_id",
        ),
        (
            json.dumps(
                [
                    {
                        "pair_id": "pair_1",
                        "label": "invalid_label_xyz",
                        "direction": "forward",
                        "confidence": 0.9,
                        "supporting_evidence_indices": [1],
                        "rationale": "ok",
                    },
                    {
                        "pair_id": "pair_2",
                        "label": "none",
                        "direction": "none",
                        "confidence": 0.1,
                        "supporting_evidence_indices": [],
                        "rationale": "ok",
                    },
                ]
            ),
            "invalid_label",
        ),
        (
            json.dumps(
                [
                    {
                        "pair_id": "pair_1",
                        "label": "relates",
                        "direction": "forward",  # incompatible: relates requires mutual
                        "confidence": 0.8,
                        "supporting_evidence_indices": [1],
                        "rationale": "ok",
                    },
                    {
                        "pair_id": "pair_2",
                        "label": "none",
                        "direction": "none",
                        "confidence": 0.1,
                        "supporting_evidence_indices": [],
                        "rationale": "ok",
                    },
                ]
            ),
            "incompatible_label_direction",
        ),
        (
            json.dumps(
                [
                    {
                        "pair_id": "pair_1",
                        "label": "updates",
                        "direction": "forward",
                        "confidence": 0.9,
                        "supporting_evidence_indices": [999],  # out of line range
                        "rationale": "ok",
                    },
                    {
                        "pair_id": "pair_2",
                        "label": "none",
                        "direction": "none",
                        "confidence": 0.1,
                        "supporting_evidence_indices": [],
                        "rationale": "ok",
                    },
                ]
            ),
            "evidence_index_out_of_range",
        ),
    ],
)
def test_edge_classifier_whole_batch_fail_loud(corrupt_content, expected_error_keyword):
    """Any defect in model completion invalidates the whole batch (zero partial edges)."""
    source = _make_source()
    candidates = _make_candidates(2)

    provider = MockLocalProvider(response_data={"choices": [{"message": {"content": corrupt_content}}]})
    chain = ProviderChain([provider])

    classifier = EdgeClassifier(provider_chain=chain)
    result = classifier.classify(source, candidates)

    assert result.status == "validation_failed"
    assert result.ok is False
    assert result.edges == ()
    assert result.unclassified_pair_ids == ("pair_1", "pair_2")
    assert expected_error_keyword in (result.error or "")


def test_edge_classifier_token_overflow_handling():
    """When budget_limit is too small to fit the prompt, returns token_overflow without calling provider."""
    source = _make_source()
    candidates = _make_candidates(3)

    provider = MockLocalProvider()
    chain = ProviderChain([provider])

    # Impossibly small budget limit of 50 tokens
    classifier = EdgeClassifier(provider_chain=chain, budget_limit=50)
    result = classifier.classify(source, candidates)

    assert result.status == "token_overflow"
    assert result.ok is False
    assert result.edges == ()
    assert result.unclassified_pair_ids == ("pair_1", "pair_2", "pair_3")
    assert "token_overflow" in (result.error or "")
    assert provider.calls == []


def test_edge_classifier_explicit_excluded_pairs_accounting():
    """When more candidates than max_pairs exist, excluded candidates are explicitly returned as unclassified."""
    source = _make_source()
    # Provide 12 candidates
    candidates = _make_candidates(12)
    assert len(candidates) == 12

    # max_pairs = 8 (normative ceiling)
    batch_rendered_pairs = [f"pair_{i}" for i in range(1, 9)]
    provider = MockLocalProvider(response_data=_valid_model_response(batch_rendered_pairs))
    chain = ProviderChain([provider])

    classifier = EdgeClassifier(provider_chain=chain, max_pairs=8)
    result = classifier.classify(source, candidates)

    assert result.status == "ok"
    assert result.ok is True
    # Exactly 8 pairs classified
    assert len(result.edges) == 8
    assert result.classified_pair_ids == tuple(batch_rendered_pairs)

    # The 4 excluded pairs MUST be reported as unclassified
    expected_unclassified = tuple(f"pair_{i}" for i in range(9, 13))
    assert result.unclassified_pair_ids == expected_unclassified
    assert len(result.unclassified_pair_ids) == 4


def test_edge_classifier_immutability():
    """Verify result structure is frozen and immutable."""
    source = _make_source()
    candidates = _make_candidates(1)
    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1"]))
    chain = ProviderChain([provider])

    classifier = EdgeClassifier(provider_chain=chain)
    result = classifier.classify(source, candidates)

    assert isinstance(result.edges, tuple)
    assert isinstance(result.classified_pair_ids, tuple)
    assert isinstance(result.unclassified_pair_ids, tuple)

    with pytest.raises(FrozenInstanceError):
        result.status = "tampered"  # type: ignore[misc]


def test_classify_learning_edges_convenience_function():
    """Verify classify_learning_edges convenience function functions identically."""
    source = _make_source()
    candidates = _make_candidates(1)
    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1"]))
    chain = ProviderChain([provider])

    result = classify_learning_edges(source, candidates, provider_chain=chain)
    assert result.status == "ok"
    assert len(result.edges) == 1
    assert result.classified_pair_ids == ("pair_1",)


def test_edge_classifier_rejects_truncation_envelope_even_with_valid_json():
    """Whole-batch truncation envelope detection: finish_reason='length' or 'max_tokens'

    or truncated=True rejects completion even if the JSON payload is completely valid.
    Zero partial edges are accepted and no false complete is reported.
    """
    source = _make_source()
    candidates = _make_candidates(2)
    valid_content = json.dumps(
        [
            {
                "pair_id": "pair_1",
                "label": "updates",
                "direction": "forward",
                "confidence": 0.9,
                "supporting_evidence_indices": [1],
                "rationale": "valid rationale 1",
            },
            {
                "pair_id": "pair_2",
                "label": "none",
                "direction": "none",
                "confidence": 0.1,
                "supporting_evidence_indices": [],
                "rationale": "valid rationale 2",
            },
        ]
    )

    # 1. choices[0].finish_reason = 'length'
    provider_length = MockLocalProvider(
        response_data={
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": valid_content},
                }
            ]
        }
    )
    res_length = EdgeClassifier(provider_chain=ProviderChain([provider_length])).classify(source, candidates)
    assert res_length.status == "validation_failed"
    assert res_length.ok is False
    assert res_length.edges == ()
    assert res_length.unclassified_pair_ids == ("pair_1", "pair_2")
    assert "truncation_detected" in (res_length.error or "")
    assert "finish_reason='length'" in (res_length.error or "")

    # 2. choices[0].finish_reason = 'max_tokens'
    provider_max_tokens = MockLocalProvider(
        response_data={
            "choices": [
                {
                    "finish_reason": "max_tokens",
                    "message": {"content": valid_content},
                }
            ]
        }
    )
    res_max = EdgeClassifier(provider_chain=ProviderChain([provider_max_tokens])).classify(source, candidates)
    assert res_max.status == "validation_failed"
    assert res_max.ok is False
    assert res_max.edges == ()
    assert "truncation_detected" in (res_max.error or "")

    # 3. top-level truncated = True
    provider_truncated = MockLocalProvider(
        response_data={
            "truncated": True,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": valid_content},
                }
            ],
        }
    )
    res_trunc = EdgeClassifier(provider_chain=ProviderChain([provider_truncated])).classify(source, candidates)
    assert res_trunc.status == "validation_failed"
    assert res_trunc.ok is False
    assert res_trunc.edges == ()
    assert "truncated=True" in (res_trunc.error or "")


def test_edge_classifier_edges_deep_immutability():
    """Assert InferredEdge and supporting_evidence_indices cannot be mutated.

    Coercion to tuple in InferredEdge.__post_init__ guarantees that
    result.edges[0].supporting_evidence_indices.append(999) raises AttributeError.
    """
    source = _make_source()
    candidates = _make_candidates(1)
    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1"]))
    res = EdgeClassifier(provider_chain=ProviderChain([provider])).classify(source, candidates)
    assert res.status == "ok"
    assert len(res.edges) == 1
    edge = res.edges[0]

    # edges is a tuple
    assert isinstance(res.edges, tuple)
    # supporting_evidence_indices is a tuple
    assert isinstance(edge.supporting_evidence_indices, tuple)

    # Attempting to call .append() must raise AttributeError
    with pytest.raises(AttributeError):
        edge.supporting_evidence_indices.append(999)  # type: ignore[attr-defined]

    # Attempting item assignment must raise TypeError
    with pytest.raises(TypeError):
        edge.supporting_evidence_indices[0] = 999  # type: ignore[index]

    # Attempting dataclass attribute assignment must raise FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        edge.label = "extends"  # type: ignore[misc]


def test_edge_classifier_provider_fallback_provenance_and_output_binding():
    """Verify truthful provenance on fallback and cryptographic binding to output edges.

    When primary provider fails and fallback succeeds, model_id truthfully records the
    fallback provider name and revision, and evidence_hash cryptographically binds output_hash.
    """
    source = _make_source()
    candidates = _make_candidates(2)

    # Provider 1: fails
    primary_failed = MockLocalProvider(
        name="local_primary_afm",
        tier="local",
        should_fail=True,
        error_msg="primary failed",
    )
    # Provider 2: succeeds with explicit model revision
    fallback_success = MockLocalProvider(
        name="local_fallback_afm",
        tier="local",
        response_data={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            [
                                {
                                    "pair_id": "pair_1",
                                    "label": "updates",
                                    "direction": "forward",
                                    "confidence": 0.95,
                                    "supporting_evidence_indices": [1],
                                    "rationale": "fallback detected update",
                                },
                                {
                                    "pair_id": "pair_2",
                                    "label": "relates",
                                    "direction": "mutual",
                                    "confidence": 0.85,
                                    "supporting_evidence_indices": [1, 2],
                                    "rationale": "fallback detected relation",
                                },
                            ]
                        )
                    },
                }
            ],
            "model": "afm-4b-instruct-202607",
        },
    )

    chain = ProviderChain([primary_failed, fallback_success])
    classifier = EdgeClassifier(provider_chain=chain)
    res = classifier.classify(source, candidates)

    assert res.status == "ok"
    assert res.ok is True
    # Truthful provenance: records fallback provider and exact model revision
    assert res.model_id == "local_fallback_afm:afm-4b-instruct-202607"

    # Verify output_hash is computed canonically over edges
    expected_output_dicts = [e.to_dict() for e in res.edges]
    expected_output_hash = compute_canonical_hash(expected_output_dicts)
    assert res.output_hash == expected_output_hash

    # Verify evidence_hash binds source_hash, batch_candidates_hash, prompt_hash, output_hash, prompt_version, model_id
    expected_evidence_hash = hashlib.sha256(
        f"{res.source_hash}:{res.batch_candidates_hash}:{res.prompt_hash}:{res.output_hash}:{res.prompt_version}:{res.model_id}".encode(
            "utf-8"
        )
    ).hexdigest()
    assert res.evidence_hash == expected_evidence_hash


def test_edge_classifier_input_mutation_defense():
    """Verify input snapshotting prevents caller/provider mutations from altering hashes or results."""
    source = _make_source()
    candidates = _make_candidates(2)

    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1", "pair_2"]))
    classifier = EdgeClassifier(provider_chain=ProviderChain([provider]))

    res = classifier.classify(source, candidates)
    orig_source_hash = res.source_hash
    orig_candidates_hash = res.candidates_hash
    orig_evidence_hash = res.evidence_hash

    # Mutate caller objects post-classify
    source["title"] = "MUTATED_TITLE_AFTER_CLASSIFY"
    source["body"] = "MUTATED_BODY_AFTER_CLASSIFY"
    candidates[0]["body"] = "MUTATED_CANDIDATE_BODY"
    candidates.append({"pair_id": "pair_extra", "doc_id": "doc_extra"})

    # Classifier result hashes and edges remain pristine
    assert res.source_hash == orig_source_hash
    assert res.candidates_hash == orig_candidates_hash
    assert res.evidence_hash == orig_evidence_hash
    assert len(res.edges) == 2


def test_edge_classifier_rejects_nonfinite_floats_and_nonjson_fail_closed():
    """Fail-closed input validation: rejects NaN, Inf, -Inf, and non-serializable objects."""
    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1"]))
    classifier = EdgeClassifier(provider_chain=ProviderChain([provider]))

    # 1. NaN in source
    bad_source_nan = {"learning_id": 1, "title": "NaN test", "invalid_val": float("nan")}
    res_nan = classifier.classify(bad_source_nan, _make_candidates(1))
    assert res_nan.status == "validation_failed"
    assert res_nan.ok is False
    assert "non-JSON or non-finite float value" in (res_nan.error or "")

    # 2. Inf in candidates
    good_source = _make_source()
    bad_cand_inf = [{"pair_id": "pair_1", "inf_score": float("inf")}]
    res_inf = classifier.classify(good_source, bad_cand_inf)
    assert res_inf.status == "validation_failed"
    assert res_inf.ok is False
    assert "non-JSON or non-finite float value" in (res_inf.error or "")

    # 3. Non-serializable object
    bad_obj_source = {"learning_id": 1, "custom_obj": object()}
    res_obj = classifier.classify(bad_obj_source, _make_candidates(1))
    assert res_obj.status == "validation_failed"
    assert res_obj.ok is False
    assert "non-JSON or non-finite float value" in (res_obj.error or "")


def test_edge_classifier_hard_caps_enforcement():
    """Hard caps: caller cannot weaken max_pairs (<= 8) or budget_limit (<= 3,200)."""
    # Exceeding limits
    c_excess = EdgeClassifier(max_pairs=99, budget_limit=100_000)
    assert c_excess.max_pairs == MAX_CANDIDATE_PAIRS
    assert c_excess.max_pairs == 8
    assert c_excess.budget_limit == AFM_INPUT_BUDGET_TOKENS
    assert c_excess.budget_limit == 3200

    # Negative limits clamp to 0
    c_neg = EdgeClassifier(max_pairs=-5, budget_limit=-100)
    assert c_neg.max_pairs == 0
    assert c_neg.budget_limit == 0


def test_edge_classifier_secret_hygiene_sanitizes_errors_and_raw_responses():
    """Secret hygiene: sanitizes errors and raw responses via _safe_status_error and bounds lengths."""
    source = _make_source()
    candidates = _make_candidates(1)

    # Provider returns an oversized error string (> 500 chars)
    long_raw_err = "SecretTokenKey_1234567890 " * 30
    assert len(long_raw_err) > 500

    provider_err = MockLocalProvider(
        should_fail=True,
        error_msg=long_raw_err,
    )
    res_err = EdgeClassifier(provider_chain=ProviderChain([provider_err])).classify(source, candidates)
    assert res_err.status == "provider_unavailable"
    assert res_err.error is not None
    # Error message must be bounded to <= 500 chars + prefix
    assert len(res_err.error) <= 550

    # Provider returns oversized raw completion with malformed JSON
    long_raw_content = "Malformed raw response content: " + ("x" * 2000)
    provider_raw = MockLocalProvider(response_data={"choices": [{"message": {"content": long_raw_content}}]})
    res_val = EdgeClassifier(provider_chain=ProviderChain([provider_raw])).classify(source, candidates)
    assert res_val.status == "validation_failed"
    assert res_val.raw_response is not None
    # Raw response bounded to <= 1000 chars
    assert len(res_val.raw_response) <= 1000


@pytest.mark.parametrize("endpoint_attribute", ["url", "_url"])
def test_allowlisted_remote_local_tier_provider_is_never_called(monkeypatch, endpoint_attribute):
    from minni.config import check_model_target

    endpoint = "https://remote-model.example/v1/chat/completions"
    monkeypatch.setenv("MINNI_MODEL_ALLOWED_TARGETS", "remote-model.example")
    assert check_model_target(endpoint)["allowed"] is True
    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1"]))
    setattr(provider, endpoint_attribute, endpoint)
    result = EdgeClassifier(provider_chain=ProviderChain([provider])).classify(
        _make_source(), _make_candidates(1),
    )
    assert result.status == "unsupported_route"
    assert result.unclassified_pair_ids == ("pair_1",)
    assert provider.calls == []


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:11437/v1/chat/completions",
                                      "http://localhost:11437/chat", "http://[::1]:11437/chat"])
def test_loopback_endpoint_remains_supported(endpoint):
    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1"]))
    provider.url = endpoint
    result = EdgeClassifier(provider_chain=ProviderChain([provider])).classify(
        _make_source(), _make_candidates(1),
    )
    assert result.status == "ok"
    assert len(provider.calls) == 1


def test_loopback_public_url_cannot_hide_remote_private_endpoint(monkeypatch):
    monkeypatch.setenv("MINNI_MODEL_ALLOWED_TARGETS", "remote-model.example")
    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1"]))
    provider.url = "http://127.0.0.1:11437/chat"
    provider._url = "https://remote-model.example/chat"
    result = EdgeClassifier(provider_chain=ProviderChain([provider])).classify(
        _make_source(), _make_candidates(1),
    )
    assert result.status == "unsupported_route"
    assert provider.calls == []


@pytest.mark.parametrize("source", [None, [], "source", 1])
def test_invalid_source_shape_never_dispatches(source):
    provider = MockLocalProvider()
    result = EdgeClassifier(provider_chain=ProviderChain([provider])).classify(
        source, _make_candidates(1),
    )
    assert result.status == "validation_failed"
    assert result.edges == ()
    assert "invalid_source" in result.error
    assert provider.calls == []


@pytest.mark.parametrize("candidates", [None, {}, "candidates", b"bytes", [None], [[]], [1]])
def test_invalid_candidate_shapes_never_dispatch(candidates):
    provider = MockLocalProvider()
    result = EdgeClassifier(provider_chain=ProviderChain([provider])).classify(
        _make_source(), candidates,
    )
    assert result.status == "validation_failed"
    assert result.edges == ()
    assert "invalid_candidates" in result.error
    assert provider.calls == []


@pytest.mark.parametrize("max_pairs", [1, 8])
@pytest.mark.parametrize("alias_field", ["pair_id", "candidate_id"])
def test_duplicate_effective_pair_ids_never_dispatch_even_outside_cap(max_pairs, alias_field):
    candidates = _make_candidates(2)
    candidates[1].pop("pair_id")
    candidates[1][alias_field] = "pair_1"
    provider = MockLocalProvider(response_data=_valid_model_response(["pair_1"]))
    result = EdgeClassifier(
        provider_chain=ProviderChain([provider]), max_pairs=max_pairs,
    ).classify(_make_source(), candidates)
    assert result.status == "validation_failed"
    assert result.edges == ()
    assert result.classified_pair_ids == ()
    assert result.batch_candidates_hash == ""
    assert "duplicate_candidate_pair_id" in result.error
    assert provider.calls == []

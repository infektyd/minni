"""Local-only edge classifier batch execution adapter for Minni Typed Memory Graph (P1).

Provides:
- Prepare-only batch execution adapter over existing edge_inference.py renderer+validator
  and model_provider hardcoded edge_inference local-only policy.
- Strictly local provider routing: default-deny on tier (unknown tier rejected; cloud rejected),
  unconditional loopback URL boundary enforcement, filtered local dispatch.
- Bounded batch rendering (<= 8 pairs, <= 3,200 tokens total AFM input budget, hard caller clamping).
- Deep immutability: frozen dataclass, tuple edges, tuple supporting_evidence_indices.
- Fail-closed input validation and snapshotting: rejects non-finite floats and non-JSON types;
  deep-copies descriptors before hashing and execution to prevent input mutation divergence.
- Truncation envelope detection: rejects completion if finish_reason == 'length' or 'max_tokens'
  even if JSON happens to be parseable. Whole-batch fail-loud.
- Truthful provenance & output-bound evidence hashing: identifies actual successful provider and
  explicit model revision (e.g. 'afm:unknown_model_revision'); evidence_hash binds source,
  candidates, prompt, output edges, prompt version, and model_id.
- Secret hygiene: sanitizes errors and raw responses with _safe_status_error and bounds output.
- Explicit unclassified/excluded pair accounting: pairs excluded by budget or ceilings
  are explicitly returned as unclassified_pair_ids, never falsely marked complete.
- Pure prepare-only: zero database writes, zero daemon calls, zero graph traversals,
  zero auto-supersessions, zero schema migrations.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from minni.afm_provider import _safe_status_error
from minni.edge_inference import (
    AFM_INPUT_BUDGET_TOKENS,
    MAX_CANDIDATE_PAIRS,
    InferredEdge,
    PromptRenderResult,
    _pair_id_of,
    render_edge_inference_prompt,
    validate_edge_inference_response,
)
from minni.model_provider import (
    ChatRequest,
    OperationPolicy,
    ProviderChain,
    ProviderResult,
    default_provider_chain,
    _is_loopback_request_url,
)

logger = logging.getLogger("sovereign.edge_classifier")

ClassifierStatus = Literal[
    "ok",
    "no_candidates",
    "provider_unavailable",
    "validation_failed",
    "token_overflow",
    "unsupported_route",
]


def snapshot_and_validate_descriptor(data: Any, descriptor_name: str) -> Tuple[Any, Optional[str]]:
    """Validate and deep-copy descriptor as JSON-safe.

    Fail-closed:
    - Must be a dictionary or list.
    - Rejects non-finite floats (NaN, Inf, -Inf).
    - Rejects non-JSON-serializable types.
    - Deep-copies via canonical JSON parse so caller/provider mutations cannot alter it.

    Returns:
        (snapshot, None) on success
        (None, error_message) on failure
    """
    if not isinstance(data, (dict, list)):
        return None, f"invalid_{descriptor_name}: must be dict or list, got {type(data).__name__}"
    try:
        # allow_nan=False strictly rejects NaN, Inf, -Inf
        dumped = json.dumps(data, allow_nan=False)
        snapshot = json.loads(dumped)
        return snapshot, None
    except (ValueError, TypeError, OverflowError) as exc:
        return None, f"invalid_{descriptor_name}: non-JSON or non-finite float value ({exc})"


def compute_canonical_hash(data: Any) -> str:
    """Deterministic SHA-256 hash of structured descriptor data.

    Strict fail-closed: requires valid JSON-compliant data without NaN or Inf.
    """
    canonical_json = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClassificationBatchResult:
    """Immutable result of a single batched edge inference attempt."""

    status: ClassifierStatus
    edges: Tuple[InferredEdge, ...]
    classified_pair_ids: Tuple[str, ...]
    unclassified_pair_ids: Tuple[str, ...]
    evidence_hash: str
    prompt_hash: str
    source_hash: str
    candidates_hash: str
    batch_candidates_hash: str
    output_hash: str
    model_id: str
    prompt_version: str
    total_tokens: int
    is_measured: bool
    error: Optional[str] = None
    raw_response: Optional[str] = None

    def __post_init__(self) -> None:
        # Guarantee all collection fields are deeply immutable tuples
        if not isinstance(self.edges, tuple):
            object.__setattr__(self, "edges", tuple(self.edges))
        if not isinstance(self.classified_pair_ids, tuple):
            object.__setattr__(self, "classified_pair_ids", tuple(self.classified_pair_ids))
        if not isinstance(self.unclassified_pair_ids, tuple):
            object.__setattr__(self, "unclassified_pair_ids", tuple(self.unclassified_pair_ids))

    @property
    def ok(self) -> bool:
        """True only if the classification succeeded or there were no candidates."""
        return self.status in ("ok", "no_candidates")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "edges": [edge.to_dict() for edge in self.edges],
            "classified_pair_ids": list(self.classified_pair_ids),
            "unclassified_pair_ids": list(self.unclassified_pair_ids),
            "evidence_hash": self.evidence_hash,
            "prompt_hash": self.prompt_hash,
            "source_hash": self.source_hash,
            "candidates_hash": self.candidates_hash,
            "batch_candidates_hash": self.batch_candidates_hash,
            "output_hash": self.output_hash,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "total_tokens": self.total_tokens,
            "is_measured": self.is_measured,
            "error": self.error,
            "raw_response": self.raw_response,
        }


def _extract_completion_and_check_truncation(
    data: Any,
) -> Tuple[Optional[str], bool, Optional[str]]:
    """Extract completion text and check for truncation envelope.

    Returns:
        (completion_text, is_truncated, truncation_reason)
    """
    if isinstance(data, str):
        return data, False, None

    if isinstance(data, dict):
        # 1. Check top-level truncation indicators
        top_finish = data.get("finish_reason")
        if top_finish in ("length", "max_tokens"):
            return None, True, f"finish_reason='{top_finish}'"
        if data.get("truncated") is True:
            return None, True, "truncated=True"

        # 2. Check choices array
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            first = choices[0]
            choice_finish = first.get("finish_reason")
            if choice_finish in ("length", "max_tokens"):
                return None, True, f"choices[0].finish_reason='{choice_finish}'"
            msg = first.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"], False, None
            if isinstance(first.get("text"), str):
                return first["text"], False, None

        # 3. Direct content keys
        content = data.get("content")
        if isinstance(content, str):
            return content, False, None
        text = data.get("text")
        if isinstance(text, str):
            return text, False, None
        answer = data.get("answer")
        if isinstance(answer, str):
            return answer, False, None

    return None, False, None


class EdgeClassifier:
    """Local-only edge classifier batch execution adapter."""

    def __init__(
        self,
        provider_chain: Optional[Any] = None,
        timeout: float = 2.0,
        max_pairs: int = MAX_CANDIDATE_PAIRS,
        budget_limit: int = AFM_INPUT_BUDGET_TOKENS,
        encoding_name: str = "cl100k_base",
        prompt_version: str = "edge_inference_v1",
    ):
        # Hard caps: caller cannot weaken the normative ceilings
        self.max_pairs = max(0, min(int(max_pairs), MAX_CANDIDATE_PAIRS))
        self.budget_limit = max(0, min(int(budget_limit), AFM_INPUT_BUDGET_TOKENS))
        self.provider_chain = provider_chain
        self.timeout = max(0.1, float(timeout))
        self.encoding_name = encoding_name
        self.prompt_version = prompt_version

    def classify(
        self,
        source: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
        client: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> ClassificationBatchResult:
        """Classify candidate edges for a canonical source learning.

        Args:
            source: Descriptor for canonical source learning.
            candidates: Sequence of candidate descriptors.
            client: Optional bridge client (for hermetic testing/injection).
            timeout: Optional call-level timeout override in seconds.

        Returns:
            Immutable ClassificationBatchResult.
        """
        # 1. Fail-Closed Input Descriptor Validation & Snapshotting
        if not isinstance(source, dict):
            source_snapshot, src_err = None, "invalid_source: must be a dictionary"
        else:
            source_snapshot, src_err = snapshot_and_validate_descriptor(source, "source")
        if src_err is not None:
            return ClassificationBatchResult(
                status="validation_failed",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=(),
                evidence_hash="",
                prompt_hash="",
                source_hash="",
                candidates_hash="",
                batch_candidates_hash="",
                output_hash="",
                model_id="",
                prompt_version=self.prompt_version,
                total_tokens=0,
                is_measured=True,
                error=src_err,
                raw_response=None,
            )

        candidates_snapshot = None
        cands_err = None
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
            cands_err = "invalid_candidates: must be a sequence of dictionaries"
        elif any(not isinstance(candidate, dict) for candidate in candidates):
            cands_err = "invalid_candidates: every candidate must be a dictionary"
        else:
            candidates_snapshot, cands_err = snapshot_and_validate_descriptor(list(candidates), "candidates")
        if cands_err is None:
            # Validate the complete snapshot, including candidates excluded by
            # the render cap. Otherwise a repeated ID aliases an unsent
            # candidate into both completion accounting and batch provenance.
            all_candidate_pair_ids = tuple(
                _pair_id_of(c, i) for i, c in enumerate(candidates_snapshot)
            )
            if len(set(all_candidate_pair_ids)) != len(all_candidate_pair_ids):
                cands_err = "duplicate_candidate_pair_id: effective pair IDs must be unique"
        if cands_err is not None:
            return ClassificationBatchResult(
                status="validation_failed",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=(),
                evidence_hash="",
                prompt_hash="",
                source_hash=compute_canonical_hash(source_snapshot),
                candidates_hash="",
                batch_candidates_hash="",
                output_hash="",
                model_id="",
                prompt_version=self.prompt_version,
                total_tokens=0,
                is_measured=True,
                error=cands_err,
                raw_response=None,
            )

        source_hash = compute_canonical_hash(source_snapshot)
        candidates_hash = compute_canonical_hash(candidates_snapshot)

        # 2. Zero Candidates: clean no-op, no model call needed.
        if not candidates_snapshot:
            evidence_hash = hashlib.sha256(
                f"{source_hash}:{candidates_hash}::empty:{self.prompt_version}".encode("utf-8")
            ).hexdigest()
            return ClassificationBatchResult(
                status="no_candidates",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=(),
                evidence_hash=evidence_hash,
                prompt_hash="",
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash=candidates_hash,
                output_hash="",
                model_id="",
                prompt_version=self.prompt_version,
                total_tokens=0,
                is_measured=True,
                error=None,
                raw_response=None,
            )

        # 3. Local-Only Provider Gate: Filtered Local Dispatcher (Default-Deny)
        chain = self.provider_chain
        if chain is None:
            chain = default_provider_chain()

        candidate_providers: List[Any] = []
        if hasattr(chain, "providers"):
            candidate_providers = list(chain.providers)
        elif hasattr(chain, "chat"):
            candidate_providers = [chain]

        verified_local_providers: List[Any] = []
        for p in candidate_providers:
            # Default-deny on tier: tier must be explicitly 'local'
            tier = getattr(p, "tier", None)
            if tier != "local":
                continue

            # Provider must support edge_inference
            supports_fn = getattr(p, "supports", None)
            if callable(supports_fn) and not supports_fn("edge_inference"):
                continue

            # Local-only classification cannot use the ordinary remote-target
            # allowlist. Check every advertised endpoint; URL-less native
            # providers remain eligible through the explicit local-tier guard.
            endpoints = (getattr(p, "url", None), getattr(p, "_url", None))
            if any(url and not _is_loopback_request_url(str(url)) for url in endpoints):
                logger.warning(
                    "Provider %s rejected: edge inference requires a loopback endpoint",
                    getattr(p, "name", "unknown"),
                )
                continue

            verified_local_providers.append(p)

        if not verified_local_providers:
            evidence_hash = hashlib.sha256(
                f"{source_hash}:{candidates_hash}::unsupported_route:{self.prompt_version}".encode("utf-8")
            ).hexdigest()
            return ClassificationBatchResult(
                status="unsupported_route",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=all_candidate_pair_ids,
                evidence_hash=evidence_hash,
                prompt_hash="",
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash="",
                output_hash="",
                model_id="",
                prompt_version=self.prompt_version,
                total_tokens=0,
                is_measured=True,
                error="no_local_provider: no eligible verified local provider for operation 'edge_inference'",
                raw_response=None,
            )

        # Build isolated local-only dispatcher
        local_dispatcher = ProviderChain(
            providers=verified_local_providers,
            operations={"edge_inference": OperationPolicy(local_only=True)},
        )

        # 4. Render Bounded Batch Prompt (using snapshotted inputs)
        render_result: PromptRenderResult = render_edge_inference_prompt(
            source=source_snapshot,
            candidates=candidates_snapshot,
            max_pairs=self.max_pairs,
            budget_limit=self.budget_limit,
            encoding_name=self.encoding_name,
        )

        prompt_hash = hashlib.sha256(render_result.prompt_text.encode("utf-8")).hexdigest()

        # Identify which candidates are in the rendered batch
        batch_pair_ids = list(render_result.pair_ids)
        batch_pair_set = set(batch_pair_ids)

        # Explicitly track remaining/excluded candidates as unclassified
        unclassified_pair_ids = tuple(pid for pid in all_candidate_pair_ids if pid not in batch_pair_set)

        batch_candidates = [
            cand for i, cand in enumerate(candidates_snapshot) if _pair_id_of(cand, i) in batch_pair_set
        ]
        batch_candidates_hash = compute_canonical_hash(batch_candidates)

        # Check Token Overflow: fail if prompt exceeded budget
        if render_result.budget_exceeded:
            evidence_hash = hashlib.sha256(
                f"{source_hash}:{batch_candidates_hash}:{prompt_hash}::token_overflow:{self.prompt_version}".encode(
                    "utf-8"
                )
            ).hexdigest()
            return ClassificationBatchResult(
                status="token_overflow",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=all_candidate_pair_ids,
                evidence_hash=evidence_hash,
                prompt_hash=prompt_hash,
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash=batch_candidates_hash,
                output_hash="",
                model_id="",
                prompt_version=self.prompt_version,
                total_tokens=render_result.total_tokens,
                is_measured=render_result.is_measured,
                error=f"token_overflow: rendered prompt tokens ({render_result.total_tokens}) exceed budget ({self.budget_limit})",
                raw_response=None,
            )

        # 5. Dispatch Call to Filtered Local Provider
        # Note: prompt_text already contains self-contained system instructions + schema.
        # Zero redundant extra system envelope is added so total tokens match accurately.
        call_timeout = timeout if timeout is not None else self.timeout
        chat_req = ChatRequest(
            payload={
                "messages": [
                    {"role": "user", "content": render_result.prompt_text},
                ],
                "temperature": 0.0,
            },
            operation="edge_inference",
            timeout=call_timeout,
        )

        try:
            provider_res: ProviderResult = local_dispatcher.chat(chat_req, client=client)
        except Exception as exc:
            clean_err = _safe_status_error(str(exc)) or "provider_exception"
            evidence_hash = hashlib.sha256(
                f"{source_hash}:{batch_candidates_hash}:{prompt_hash}::provider_unavailable:{self.prompt_version}".encode(
                    "utf-8"
                )
            ).hexdigest()
            return ClassificationBatchResult(
                status="provider_unavailable",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=all_candidate_pair_ids,
                evidence_hash=evidence_hash,
                prompt_hash=prompt_hash,
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash=batch_candidates_hash,
                output_hash="",
                model_id="",
                prompt_version=self.prompt_version,
                total_tokens=render_result.total_tokens,
                is_measured=render_result.is_measured,
                error=f"provider_exception: {clean_err[:500]}",
                raw_response=None,
            )

        if not provider_res.ok:
            raw_err = provider_res.error or f"status_{provider_res.status}"
            clean_err = _safe_status_error(raw_err) or "provider_failure"
            evidence_hash = hashlib.sha256(
                f"{source_hash}:{batch_candidates_hash}:{prompt_hash}::provider_unavailable:{self.prompt_version}".encode(
                    "utf-8"
                )
            ).hexdigest()
            return ClassificationBatchResult(
                status="provider_unavailable",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=all_candidate_pair_ids,
                evidence_hash=evidence_hash,
                prompt_hash=prompt_hash,
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash=batch_candidates_hash,
                output_hash="",
                model_id="",
                prompt_version=self.prompt_version,
                total_tokens=render_result.total_tokens,
                is_measured=render_result.is_measured,
                error=f"provider_unavailable: {clean_err[:500]}",
                raw_response=None,
            )

        # 6. Extract Actual Successful Provider & Model Provenance
        successful_provider_name = str(getattr(provider_res, "provider", "") or "unknown_provider")
        model_revision = "unknown_model_revision"
        if isinstance(provider_res.data, dict) and provider_res.data.get("model"):
            model_revision = str(provider_res.data["model"])
        proven_model_id = f"{successful_provider_name}:{model_revision}"

        # 7. Check Truncation Envelope & Extract Completion Text
        raw_text, is_truncated, trunc_reason = _extract_completion_and_check_truncation(provider_res.data)

        sanitized_raw = None
        if raw_text is not None:
            sanitized_raw = _safe_status_error(raw_text)
            if sanitized_raw:
                sanitized_raw = sanitized_raw[:1000]

        if is_truncated:
            evidence_hash = hashlib.sha256(
                f"{source_hash}:{batch_candidates_hash}:{prompt_hash}::truncation:{self.prompt_version}:{proven_model_id}".encode(
                    "utf-8"
                )
            ).hexdigest()
            return ClassificationBatchResult(
                status="validation_failed",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=all_candidate_pair_ids,
                evidence_hash=evidence_hash,
                prompt_hash=prompt_hash,
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash=batch_candidates_hash,
                output_hash="",
                model_id=proven_model_id,
                prompt_version=self.prompt_version,
                total_tokens=render_result.total_tokens,
                is_measured=render_result.is_measured,
                error=f"truncation_detected: model completion was truncated ({trunc_reason})",
                raw_response=sanitized_raw,
            )

        if raw_text is None:
            evidence_hash = hashlib.sha256(
                f"{source_hash}:{batch_candidates_hash}:{prompt_hash}::empty_completion:{self.prompt_version}:{proven_model_id}".encode(
                    "utf-8"
                )
            ).hexdigest()
            return ClassificationBatchResult(
                status="validation_failed",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=all_candidate_pair_ids,
                evidence_hash=evidence_hash,
                prompt_hash=prompt_hash,
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash=batch_candidates_hash,
                output_hash="",
                model_id=proven_model_id,
                prompt_version=self.prompt_version,
                total_tokens=render_result.total_tokens,
                is_measured=render_result.is_measured,
                error="empty_completion: provider returned no completion text",
                raw_response=None,
            )

        # 8. Whole-Batch Fail-Loud Validation
        is_valid, validated_edges, val_err = validate_edge_inference_response(
            raw_response=raw_text,
            expected_pair_ids=batch_pair_ids,
            line_counts_per_pair=render_result.line_counts_per_pair,
        )

        if not is_valid or validated_edges is None:
            clean_val_err = _safe_status_error(val_err) or "validation_error"
            evidence_hash = hashlib.sha256(
                f"{source_hash}:{batch_candidates_hash}:{prompt_hash}::validation_failed:{self.prompt_version}:{proven_model_id}".encode(
                    "utf-8"
                )
            ).hexdigest()
            return ClassificationBatchResult(
                status="validation_failed",
                edges=(),
                classified_pair_ids=(),
                unclassified_pair_ids=all_candidate_pair_ids,
                evidence_hash=evidence_hash,
                prompt_hash=prompt_hash,
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash=batch_candidates_hash,
                output_hash="",
                model_id=proven_model_id,
                prompt_version=self.prompt_version,
                total_tokens=render_result.total_tokens,
                is_measured=render_result.is_measured,
                error=f"validation_failed: {clean_val_err[:500]}",
                raw_response=sanitized_raw,
            )

        # 9. Compute Canonical Output Hash & Evidence Hash Binding Output
        output_edges_dicts = [edge.to_dict() for edge in validated_edges]
        output_hash = compute_canonical_hash(output_edges_dicts)

        evidence_hash = hashlib.sha256(
            f"{source_hash}:{batch_candidates_hash}:{prompt_hash}:{output_hash}:{self.prompt_version}:{proven_model_id}".encode(
                "utf-8"
            )
        ).hexdigest()

        # 10. Success: return immutable validated batch
        return ClassificationBatchResult(
            status="ok",
            edges=tuple(validated_edges),
            classified_pair_ids=tuple(batch_pair_ids),
            unclassified_pair_ids=unclassified_pair_ids,
            evidence_hash=evidence_hash,
            prompt_hash=prompt_hash,
            source_hash=source_hash,
            candidates_hash=candidates_hash,
            batch_candidates_hash=batch_candidates_hash,
            output_hash=output_hash,
            model_id=proven_model_id,
            prompt_version=self.prompt_version,
            total_tokens=render_result.total_tokens,
            is_measured=render_result.is_measured,
            error=None,
            raw_response=sanitized_raw,
        )


def classify_learning_edges(
    source: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
    provider_chain: Optional[Any] = None,
    client: Optional[Any] = None,
    timeout: float = 2.0,
    max_pairs: int = MAX_CANDIDATE_PAIRS,
    budget_limit: int = AFM_INPUT_BUDGET_TOKENS,
    encoding_name: str = "cl100k_base",
    prompt_version: str = "edge_inference_v1",
) -> ClassificationBatchResult:
    """Convenience function for batched edge classification."""
    classifier = EdgeClassifier(
        provider_chain=provider_chain,
        timeout=timeout,
        max_pairs=max_pairs,
        budget_limit=budget_limit,
        encoding_name=encoding_name,
        prompt_version=prompt_version,
    )
    return classifier.classify(source=source, candidates=candidates, client=client)

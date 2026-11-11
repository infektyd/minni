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
import re
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
        edges = data.get("edges")
        if isinstance(edges, list):
            return json.dumps(edges), False, None

    return None, False, None


_HEURISTIC_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "can",
        "could", "did", "do", "does", "for", "from", "had", "has", "have", "how",
        "if", "in", "into", "is", "it", "its", "may", "might", "of", "on", "or",
        "our", "should", "so", "than", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "those", "to", "was", "we", "were",
        "what", "when", "where", "which", "while", "who", "will", "with", "would",
    }
)
_UPDATE_PREFIX_WORDS = frozenset(
    {
        "supersedes", "supersede", "superseded", "updated", "update", "updates",
        "replaces", "replace", "replaced", "obsoletes", "obsolete", "deprecates",
        "deprecated", "instead", "formerly", "previously", "contradicts",
        "contradict", "prior", "previous", "old", "earlier", "rule",
    }
)
_POSITIVE_POLARITY_WORDS = frozenset(
    {
        "always", "must", "enable", "enabled", "enables", "allow", "allowed",
        "allows", "true", "require", "required", "requires",
    }
)
_NEGATIVE_POLARITY_WORDS = frozenset(
    {
        "never", "disable", "disabled", "disables", "forbid", "forbidden",
        "forbids", "deny", "denied", "denies", "false", "avoid", "avoids",
        "prohibit", "prohibited", "prohibits", "disallow", "disallowed",
        "disallows",
    }
)
_NEGATION_PHRASE_RE = re.compile(
    r"\b(?:must\s+not|do\s+not|don't|cannot|can't|should\s+not|shouldn't|never)\b",
    re.IGNORECASE,
)
_ANCHORED_SUPERSEDE_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:update(?:d)?|supersedes?|replaces?|obsoletes?)\s*(?:[:—–-]|prior\b|previous\b|old\b|earlier\b|rule\b)"
)
_EXPLICIT_SUPERSEDE_RE = _ANCHORED_SUPERSEDE_PREFIX_RE
_INLINE_SUPERSEDES_WORD_RE = re.compile(r"(?i)\bsupersedes\b")
_EXPLICIT_CONTRADICT_RE = re.compile(
    r"(?i)(?:^\s*(?:contradicts?|conflicts?\s+with)\s*[:—–-]|\b(?:contradicts?|contrary\s+to)\b)"
)
_WORD_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.IGNORECASE)
_NUMBER_VALUE_RE = re.compile(r"\b\d+(?:\.\d+)*[a-z%]*\b", re.IGNORECASE)
_CLI_FLAG_RE = re.compile(r"(?<!\w)--[a-z0-9][a-z0-9_-]*", re.IGNORECASE)
_KV_ASSIGN_RE = re.compile(r"\b([a-z][a-z0-9_]*)=([+-]?[a-z0-9_.-]+)\b", re.IGNORECASE)


def _descriptor_text(desc: Dict[str, Any]) -> str:
    for key in ("body", "content", "excerpt"):
        val = desc.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _polarity_signature(text: str) -> Tuple[bool, bool]:
    has_neg_phrase = bool(_NEGATION_PHRASE_RE.search(text))
    scrubbed = _NEGATION_PHRASE_RE.sub(" ", text.lower())
    tokens = {t.strip("._-") for t in _WORD_TOKEN_RE.findall(scrubbed)}
    has_neg = has_neg_phrase or bool(tokens & _NEGATIVE_POLARITY_WORDS)
    has_pos = bool(tokens & _POSITIVE_POLARITY_WORDS)
    return has_pos, has_neg


def _extract_kv_map(text: str) -> Dict[str, str]:
    return {k.lower(): v.lower() for k, v in _KV_ASSIGN_RE.findall(text)}


def _is_numeric_or_version_token(tok: str) -> bool:
    return bool(
        tok.isdigit()
        or re.fullmatch(r"\d+(?:\.\d+)*[a-z%]*", tok)
        or re.fullmatch(r"v\d+(?:\.\d+)+[a-z0-9-]*", tok)
    )


def _content_and_subject_tokens(text: str) -> Tuple[set[str], set[str]]:
    raw_tokens = {
        tok.lower().strip("._-")
        for tok in _WORD_TOKEN_RE.findall(text)
        if len(tok.strip("._-")) >= 2
    }
    content_tokens = {
        t for t in raw_tokens if t and t not in _HEURISTIC_STOPWORDS
    }
    # Replace key=value with key so RHS values (e.g. wal vs delete, 5000 vs -64000)
    # do not masquerade as distinct subject entities while key names stay distinct.
    entity_source = _KV_ASSIGN_RE.sub(r"\1", text)
    raw_entity_tokens = {
        tok.lower().strip("._-")
        for tok in _WORD_TOKEN_RE.findall(entity_source)
        if len(tok.strip("._-")) >= 2
    }
    subject_tokens = {
        t
        for t in raw_entity_tokens
        if t
        and t not in _HEURISTIC_STOPWORDS
        and t not in _UPDATE_PREFIX_WORDS
        and t not in _POSITIVE_POLARITY_WORDS
        and t not in _NEGATIVE_POLARITY_WORDS
        and not _is_numeric_or_version_token(t)
    }
    return content_tokens, subject_tokens


def classify_pair_deterministic(
    source: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    pair_id: str,
    line_count: int = 1,
) -> InferredEdge:
    """Conservative deterministic fallback edge classifier for high-overlap pairs."""
    if line_count < 1:
        return InferredEdge(
            pair_id=pair_id,
            label="none",
            direction="none",
            confidence=0.05,
            supporting_evidence_indices=(),
            rationale="No citable target lines available.",
        )

    src_text = _descriptor_text(source)
    cand_text = _descriptor_text(candidate)
    if not src_text or not cand_text:
        return InferredEdge(
            pair_id=pair_id,
            label="none",
            direction="none",
            confidence=0.05,
            supporting_evidence_indices=(),
            rationale="Empty source or candidate text.",
        )

    norm_src = re.sub(r"\s+", " ", src_text.lower()).strip()
    norm_cand = re.sub(r"\s+", " ", cand_text.lower()).strip()
    if norm_src == norm_cand:
        return InferredEdge(
            pair_id=pair_id,
            label="none",
            direction="none",
            confidence=0.05,
            supporting_evidence_indices=(),
            rationale="Identical text; handled by content deduplication.",
        )

    src_meta = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    cand_meta = (
        candidate.get("target_metadata")
        if isinstance(candidate.get("target_metadata"), dict)
        else (candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {})
    )

    src_ws_raw = str(source.get("workspace_id") or src_meta.get("workspace_id") or "").strip().lower()
    cand_ws_raw = str(candidate.get("workspace_id") or cand_meta.get("workspace_id") or "").strip().lower()
    src_ws = "" if src_ws_raw in ("", "default") else src_ws_raw
    cand_ws = "" if cand_ws_raw in ("", "default") else cand_ws_raw
    if (src_ws or cand_ws) and src_ws != cand_ws:
        return InferredEdge(
            pair_id=pair_id,
            label="none",
            direction="none",
            confidence=0.05,
            supporting_evidence_indices=(),
            rationale="Distinct workspaces; cross-workspace heuristic edges suppressed.",
        )

    src_agent = str(
        source.get("agent_id")
        or source.get("agent")
        or src_meta.get("agent_id")
        or src_meta.get("agent")
        or ""
    ).strip().lower()
    cand_agent = str(
        candidate.get("agent_id")
        or candidate.get("agent")
        or cand_meta.get("agent_id")
        or cand_meta.get("agent")
        or ""
    ).strip().lower()
    same_agent = not src_agent or not cand_agent or src_agent == cand_agent

    src_cat = str(source.get("category") or src_meta.get("category") or "").strip().lower()
    cand_cat = str(candidate.get("category") or cand_meta.get("category") or "").strip().lower()
    same_category = not src_cat or not cand_cat or src_cat == cand_cat

    src_kv = _extract_kv_map(src_text)
    cand_kv = _extract_kv_map(cand_text)
    shared_kv_keys = set(src_kv) & set(cand_kv)
    if src_kv and cand_kv and not shared_kv_keys:
        return InferredEdge(
            pair_id=pair_id,
            label="none",
            direction="none",
            confidence=0.10,
            supporting_evidence_indices=(),
            rationale="Disjoint key=value configuration keys describe distinct settings.",
        )
    same_key_value_changed = any(src_kv[k] != cand_kv[k] for k in shared_kv_keys)

    src_tokens, src_entity_tokens = _content_and_subject_tokens(src_text)
    cand_tokens, cand_entity_tokens = _content_and_subject_tokens(cand_text)
    shared_entity_tokens = src_entity_tokens & cand_entity_tokens
    union_tokens = src_tokens | cand_tokens
    union_subj = src_entity_tokens | cand_entity_tokens

    both_have_unshared_entities = bool(
        src_entity_tokens - cand_entity_tokens
    ) and bool(cand_entity_tokens - src_entity_tokens)
    if both_have_unshared_entities:
        return InferredEdge(
            pair_id=pair_id,
            label="none",
            direction="none",
            confidence=0.10,
            supporting_evidence_indices=(),
            rationale="Both learnings contain unshared entity tokens describing distinct subjects.",
        )

    content_jaccard = (len(src_tokens & cand_tokens) / len(union_tokens)) if union_tokens else 0.0
    subj_jaccard = (len(shared_entity_tokens) / len(union_subj)) if union_subj else 0.0

    src_pos, src_neg = _polarity_signature(src_text)
    cand_pos, cand_neg = _polarity_signature(cand_text)
    opposing_polarity = (
        (src_pos and cand_neg and not (src_neg and cand_pos))
        or (src_neg and cand_pos and not (src_pos and cand_neg))
    )

    src_nums = {m.lower() for m in _NUMBER_VALUE_RE.findall(_UPDATE_PREFIX_RE_STRIP(src_text))}
    cand_nums = {m.lower() for m in _NUMBER_VALUE_RE.findall(_UPDATE_PREFIX_RE_STRIP(cand_text))}
    src_flags = {m.lower() for m in _CLI_FLAG_RE.findall(src_text)}
    cand_flags = {m.lower() for m in _CLI_FLAG_RE.findall(cand_text)}

    value_changed = (
        (bool(src_nums) and bool(cand_nums) and src_nums != cand_nums)
        or (bool(src_flags) and bool(cand_flags) and src_flags != cand_flags)
        or same_key_value_changed
    )

    exact_same_entities = (
        src_entity_tokens == cand_entity_tokens and len(shared_entity_tokens) >= 3
    )
    old_entities_covered_by_new = (
        cand_entity_tokens <= src_entity_tokens and len(cand_entity_tokens) >= 3
    )

    if _EXPLICIT_CONTRADICT_RE.search(src_text) and (
        subj_jaccard >= 0.50 and len(shared_entity_tokens) >= 2
    ):
        return InferredEdge(
            pair_id=pair_id,
            label="contradicts",
            direction="mutual",
            confidence=0.92,
            supporting_evidence_indices=(1,),
            rationale="Explicit contradiction marker with shared technical subject.",
        )

    has_anchored_supersede = bool(_ANCHORED_SUPERSEDE_PREFIX_RE.search(src_text))
    has_inline_supersedes = (
        bool(_INLINE_SUPERSEDES_WORD_RE.search(src_text))
        and same_agent
        and same_category
        and len(src_entity_tokens) >= 2
        and (len(shared_entity_tokens) / max(len(src_entity_tokens), 1)) >= 0.65
        and (len(shared_entity_tokens) / max(len(cand_entity_tokens), 1)) >= 0.50
    )
    if has_anchored_supersede or has_inline_supersedes:
        if same_agent and same_category and (
            exact_same_entities
            or (has_anchored_supersede and old_entities_covered_by_new)
        ):
            return InferredEdge(
                pair_id=pair_id,
                label="updates",
                direction="forward",
                confidence=0.97,
                supporting_evidence_indices=(1,),
                rationale="Explicit supersession marker updating prior rule on covered subject.",
            )
        if same_agent and same_category and len(shared_entity_tokens) >= 2 and subj_jaccard >= 0.50:
            return InferredEdge(
                pair_id=pair_id,
                label="updates",
                direction="forward",
                confidence=0.90,
                supporting_evidence_indices=(1,),
                rationale="Partial-overlap explicit update in same category; queued for review.",
            )

    high_overlap = (
        (content_jaccard >= 0.55 or subj_jaccard >= 0.55)
        and len(shared_entity_tokens) >= 2
    )
    if high_overlap:
        if opposing_polarity and (not same_agent or not same_category):
            return InferredEdge(
                pair_id=pair_id,
                label="contradicts",
                direction="mutual",
                confidence=0.91,
                supporting_evidence_indices=(1,),
                rationale="Opposing rule polarity across distinct owner or category.",
            )
        if (opposing_polarity or value_changed) and same_agent and same_category:
            if exact_same_entities:
                return InferredEdge(
                    pair_id=pair_id,
                    label="updates",
                    direction="forward",
                    confidence=0.97,
                    supporting_evidence_indices=(1,),
                    rationale="Same-owner rule revises concrete parameter or polarity on identical subject.",
                )
            return InferredEdge(
                pair_id=pair_id,
                label="updates",
                direction="forward",
                confidence=0.90,
                supporting_evidence_indices=(1,),
                rationale="Partial-overlap same-category parameter/polarity revision; queued for review.",
            )
        if opposing_polarity or value_changed:
            return InferredEdge(
                pair_id=pair_id,
                label="contradicts",
                direction="mutual",
                confidence=0.90,
                supporting_evidence_indices=(1,),
                rationale="Conflicting rule parameter or polarity across categories or owners.",
            )
        if (
            len(cand_entity_tokens) >= 2
            and len(shared_entity_tokens) / len(cand_entity_tokens) >= 0.75
            and len(src_tokens - cand_tokens) >= 2
        ):
            from minni.durable_projection import five_gram_jaccard

            if five_gram_jaccard(src_text, cand_text) >= 0.65:
                return InferredEdge(
                    pair_id=pair_id,
                    label="none",
                    direction="none",
                    confidence=0.05,
                    supporting_evidence_indices=(),
                    rationale="Near-duplicate paraphrase; handled by near-duplicate eviction.",
                )
            return InferredEdge(
                pair_id=pair_id,
                label="extends",
                direction="forward",
                confidence=0.86,
                supporting_evidence_indices=(1,),
                rationale="Additive technical detail extending existing learning on shared subject.",
            )

    return InferredEdge(
        pair_id=pair_id,
        label="none",
        direction="none",
        confidence=0.10,
        supporting_evidence_indices=(),
        rationale="No high-overlap rule revision or conflict detected.",
    )


def _UPDATE_PREFIX_RE_STRIP(text: str) -> str:
    return re.sub(
        r"(?i)\b(?:supersedes?|replaces?|formerly|previously|instead\s+of)\s+\d+(?:\.\d+)*[a-z%]*\b",
        " ",
        text,
    )


def _recoverable_afm_trip_kind(provider_res: Any) -> Optional[str]:
    status = str(getattr(provider_res, "status", "") or "").strip().lower()
    if status in {"context_overflow", "guardrail", "refusal"}:
        return status
    data = getattr(provider_res, "data", None)
    if isinstance(data, dict):
        err_kind = str(data.get("error_kind") or data.get("status") or "").strip().lower()
        if err_kind in {"context_overflow", "guardrail", "refusal"}:
            return err_kind
    err_text = str(getattr(provider_res, "error", "") or "").strip().lower()
    for token in ("context_overflow", "guardrail", "refusal"):
        if token in err_text:
            return token
    return None


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
        allow_heuristic_fallback: Optional[bool] = None,
        breaker: Optional[Any] = None,
    ):
        # Hard caps: caller cannot weaken the normative ceilings
        self.max_pairs = max(0, min(int(max_pairs), MAX_CANDIDATE_PAIRS))
        self.budget_limit = max(0, min(int(budget_limit), AFM_INPUT_BUDGET_TOKENS))
        self.provider_chain = provider_chain
        self.timeout = max(0.1, float(timeout))
        self.encoding_name = encoding_name
        self.prompt_version = prompt_version
        self.allow_heuristic_fallback = (
            provider_chain is None
            if allow_heuristic_fallback is None
            else bool(allow_heuristic_fallback)
        )
        self.breaker = breaker

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

        # 5. Dispatch Call to Filtered Local Provider (with sub-batching & deterministic fallback)
        call_timeout = timeout if timeout is not None else self.timeout
        pair_candidates_by_id: Dict[str, Dict[str, Any]] = {
            _pair_id_of(cand, i): cand
            for i, cand in enumerate(candidates_snapshot)
            if _pair_id_of(cand, i) in batch_pair_set
        }

        def _deterministic_batch_edges(
            pair_ids: Sequence[str],
            line_counts: Dict[str, int],
        ) -> List[InferredEdge]:
            return [
                classify_pair_deterministic(
                    source_snapshot,
                    pair_candidates_by_id[pid],
                    pair_id=pid,
                    line_count=int(line_counts.get(pid, 1)),
                )
                for pid in pair_ids
            ]

        def _finalize_valid_edges(
            edges_seq: Sequence[InferredEdge],
            proven_model_id: str,
            raw_resp: Optional[str] = None,
        ) -> Optional[ClassificationBatchResult]:
            raw_items = [e.to_dict() for e in edges_seq]
            ok_val, revalidated, _ = validate_edge_inference_response(
                raw_response=raw_items,
                expected_pair_ids=batch_pair_ids,
                line_counts_per_pair=render_result.line_counts_per_pair,
            )
            if not ok_val or revalidated is None:
                return None
            out_hash = compute_canonical_hash([edge.to_dict() for edge in revalidated])
            ev_hash = hashlib.sha256(
                f"{source_hash}:{batch_candidates_hash}:{prompt_hash}:{out_hash}:{self.prompt_version}:{proven_model_id}".encode(
                    "utf-8"
                )
            ).hexdigest()
            return ClassificationBatchResult(
                status="ok",
                edges=tuple(revalidated),
                classified_pair_ids=tuple(batch_pair_ids),
                unclassified_pair_ids=unclassified_pair_ids,
                evidence_hash=ev_hash,
                prompt_hash=prompt_hash,
                source_hash=source_hash,
                candidates_hash=candidates_hash,
                batch_candidates_hash=batch_candidates_hash,
                output_hash=out_hash,
                model_id=proven_model_id,
                prompt_version=self.prompt_version,
                total_tokens=render_result.total_tokens,
                is_measured=render_result.is_measured,
                error=None,
                raw_response=raw_resp,
            )

        def _invoke_single_request(
            sub_render: PromptRenderResult,
            sub_pair_ids: List[str],
        ) -> Tuple[Optional[ProviderResult], Optional[str]]:
            chat_payload = {
                "messages": [
                    {"role": "user", "content": sub_render.prompt_text},
                ],
                "temperature": 0.0,
            }
            req = ChatRequest(
                payload=chat_payload,
                operation="edge_inference",
                timeout=call_timeout,
                native_operation="edge_inference",
                native_payload={
                    "prompt": sub_render.prompt_text,
                    "expected_pair_ids": list(sub_pair_ids),
                    "payload": chat_payload,
                },
            )
            try:
                res: ProviderResult = local_dispatcher.chat(req, client=client)
            except Exception as exc:
                if self.breaker is not None and hasattr(self.breaker, "record_failure"):
                    self.breaker.record_failure()
                return None, _safe_status_error(str(exc)) or "provider_exception"

            if (
                not res.ok
                and "unsupported native afm operation" in str(res.error or "").lower()
            ):
                fallback_req = ChatRequest(
                    payload=chat_payload,
                    operation="edge_inference",
                    timeout=call_timeout,
                    native_operation="chat_completion",
                    native_payload={"payload": chat_payload},
                )
                try:
                    res = local_dispatcher.chat(fallback_req, client=client)
                except Exception as exc:
                    if self.breaker is not None and hasattr(self.breaker, "record_failure"):
                        self.breaker.record_failure()
                    return None, _safe_status_error(str(exc)) or "provider_exception"

            trip_kind = _recoverable_afm_trip_kind(res)
            if res.ok:
                if self.breaker is not None and hasattr(self.breaker, "record_success"):
                    self.breaker.record_success()
            elif trip_kind is None and str(getattr(res, "status", "")).lower() != "off":
                if self.breaker is not None and hasattr(self.breaker, "record_failure"):
                    self.breaker.record_failure()
            return res, None

        def _resolve_subbatch(
            sub_cands: List[Dict[str, Any]],
            sub_pair_ids: List[str],
        ) -> Optional[Tuple[List[InferredEdge], str]]:
            sub_render = render_edge_inference_prompt(
                source=source_snapshot,
                candidates=sub_cands,
                max_pairs=len(sub_cands),
                budget_limit=self.budget_limit,
                encoding_name=self.encoding_name,
            )
            if sub_render.budget_exceeded or list(sub_render.pair_ids) != sub_pair_ids:
                if len(sub_cands) > 1:
                    mid = len(sub_cands) // 2
                    left = _resolve_subbatch(sub_cands[:mid], sub_pair_ids[:mid])
                    right = _resolve_subbatch(sub_cands[mid:], sub_pair_ids[mid:])
                    if left is not None and right is not None:
                        model_tag = left[1] if left[1] == right[1] else "afm:subbatch_hybrid_v1"
                        return left[0] + right[0], model_tag
                return (
                    _deterministic_batch_edges(sub_pair_ids, render_result.line_counts_per_pair),
                    "deterministic:heuristic_v1",
                )

            sub_res, sub_exc = _invoke_single_request(sub_render, sub_pair_ids)
            if sub_res is None or not sub_res.ok:
                trip = _recoverable_afm_trip_kind(sub_res) if sub_res is not None else None
                if len(sub_cands) > 1 and (trip is not None or self.allow_heuristic_fallback):
                    mid = len(sub_cands) // 2
                    left = _resolve_subbatch(sub_cands[:mid], sub_pair_ids[:mid])
                    right = _resolve_subbatch(sub_cands[mid:], sub_pair_ids[mid:])
                    if left is not None and right is not None:
                        model_tag = left[1] if left[1] == right[1] else "afm:subbatch_hybrid_v1"
                        return left[0] + right[0], model_tag
                if trip is not None or self.allow_heuristic_fallback:
                    return (
                        _deterministic_batch_edges(sub_pair_ids, render_result.line_counts_per_pair),
                        "deterministic:heuristic_v1",
                    )
                return None

            sub_prov = str(getattr(sub_res, "provider", "") or "unknown_provider")
            sub_rev = "unknown_model_revision"
            if isinstance(sub_res.data, dict) and sub_res.data.get("model"):
                sub_rev = str(sub_res.data["model"])
            sub_model_id = f"{sub_prov}:{sub_rev}"

            sub_raw, sub_trunc, _ = _extract_completion_and_check_truncation(sub_res.data)
            if not sub_trunc and sub_raw is not None:
                merged_line_counts = {
                    pid: min(
                        int(sub_render.line_counts_per_pair.get(pid, 1)),
                        int(render_result.line_counts_per_pair.get(pid, 1)),
                    )
                    for pid in sub_pair_ids
                }
                ok_sub, sub_edges, _ = validate_edge_inference_response(
                    raw_response=sub_raw,
                    expected_pair_ids=sub_pair_ids,
                    line_counts_per_pair=merged_line_counts,
                )
                if ok_sub and sub_edges is not None:
                    return list(sub_edges), sub_model_id

            if len(sub_cands) > 1:
                mid = len(sub_cands) // 2
                left = _resolve_subbatch(sub_cands[:mid], sub_pair_ids[:mid])
                right = _resolve_subbatch(sub_cands[mid:], sub_pair_ids[mid:])
                if left is not None and right is not None:
                    model_tag = left[1] if left[1] == right[1] else "afm:subbatch_hybrid_v1"
                    return left[0] + right[0], model_tag
            if self.allow_heuristic_fallback:
                return (
                    _deterministic_batch_edges(sub_pair_ids, render_result.line_counts_per_pair),
                    "deterministic:heuristic_v1",
                )
            return None

        provider_res, exc_err = _invoke_single_request(render_result, batch_pair_ids)
        if exc_err is not None:
            if self.allow_heuristic_fallback and batch_pair_ids:
                det_res = _finalize_valid_edges(
                    _deterministic_batch_edges(batch_pair_ids, render_result.line_counts_per_pair),
                    "deterministic:heuristic_v1",
                )
                if det_res is not None:
                    return det_res
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
                error=f"provider_exception: {exc_err[:500]}",
                raw_response=None,
            )

        assert provider_res is not None
        if not provider_res.ok:
            trip_kind = _recoverable_afm_trip_kind(provider_res)
            if trip_kind is not None and batch_candidates:
                if len(batch_candidates) > 1:
                    mid = len(batch_candidates) // 2
                    left = _resolve_subbatch(batch_candidates[:mid], batch_pair_ids[:mid])
                    right = _resolve_subbatch(batch_candidates[mid:], batch_pair_ids[mid:])
                    if left is not None and right is not None:
                        model_tag = left[1] if left[1] == right[1] else "afm:subbatch_hybrid_v1"
                        recovered = _finalize_valid_edges(left[0] + right[0], model_tag)
                        if recovered is not None:
                            return recovered
                det_res = _finalize_valid_edges(
                    _deterministic_batch_edges(batch_pair_ids, render_result.line_counts_per_pair),
                    "deterministic:heuristic_v1",
                )
                if det_res is not None:
                    return det_res
            elif self.allow_heuristic_fallback and batch_pair_ids:
                det_res = _finalize_valid_edges(
                    _deterministic_batch_edges(batch_pair_ids, render_result.line_counts_per_pair),
                    "deterministic:heuristic_v1",
                )
                if det_res is not None:
                    return det_res

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
            if len(batch_candidates) > 1:
                mid = len(batch_candidates) // 2
                left = _resolve_subbatch(batch_candidates[:mid], batch_pair_ids[:mid])
                right = _resolve_subbatch(batch_candidates[mid:], batch_pair_ids[mid:])
                if left is not None and right is not None:
                    model_tag = left[1] if left[1] == right[1] else "afm:subbatch_hybrid_v1"
                    recovered = _finalize_valid_edges(left[0] + right[0], model_tag)
                    if recovered is not None:
                        return recovered
            if self.allow_heuristic_fallback and batch_pair_ids:
                det_res = _finalize_valid_edges(
                    _deterministic_batch_edges(batch_pair_ids, render_result.line_counts_per_pair),
                    "deterministic:heuristic_v1",
                )
                if det_res is not None:
                    return det_res
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
            if len(batch_candidates) > 1:
                mid = len(batch_candidates) // 2
                left = _resolve_subbatch(batch_candidates[:mid], batch_pair_ids[:mid])
                right = _resolve_subbatch(batch_candidates[mid:], batch_pair_ids[mid:])
                if left is not None and right is not None:
                    model_tag = left[1] if left[1] == right[1] else "afm:subbatch_hybrid_v1"
                    recovered = _finalize_valid_edges(left[0] + right[0], model_tag)
                    if recovered is not None:
                        return recovered
            if self.allow_heuristic_fallback and batch_pair_ids:
                det_res = _finalize_valid_edges(
                    _deterministic_batch_edges(batch_pair_ids, render_result.line_counts_per_pair),
                    "deterministic:heuristic_v1",
                )
                if det_res is not None:
                    return det_res
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

        # 8. Whole-Batch Fail-Loud Validation (with recursive sub-batch recovery on multi-pair batches)
        is_valid, validated_edges, val_err = validate_edge_inference_response(
            raw_response=raw_text,
            expected_pair_ids=batch_pair_ids,
            line_counts_per_pair=render_result.line_counts_per_pair,
        )

        if not is_valid or validated_edges is None:
            if len(batch_candidates) > 1:
                mid = len(batch_candidates) // 2
                left = _resolve_subbatch(batch_candidates[:mid], batch_pair_ids[:mid])
                right = _resolve_subbatch(batch_candidates[mid:], batch_pair_ids[mid:])
                if left is not None and right is not None:
                    model_tag = left[1] if left[1] == right[1] else "afm:subbatch_hybrid_v1"
                    recovered = _finalize_valid_edges(left[0] + right[0], model_tag, sanitized_raw)
                    if recovered is not None:
                        return recovered
            if self.allow_heuristic_fallback and batch_pair_ids:
                det_res = _finalize_valid_edges(
                    _deterministic_batch_edges(batch_pair_ids, render_result.line_counts_per_pair),
                    "deterministic:heuristic_v1",
                    sanitized_raw,
                )
                if det_res is not None:
                    return det_res
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
        finalized = _finalize_valid_edges(validated_edges, proven_model_id, sanitized_raw)
        assert finalized is not None
        return finalized


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

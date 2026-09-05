"""Edge inference prompt substrate and validator for Minni Typed Memory Graph.

Provides:
- Normative edge classification prompt template loader (prompts/edge_inference_v1.txt).
- Honest token accounting: distinguishes measured tiktoken cl100k_base counts from heuristic estimates.
- AFM token-budgeted prompt rendering (<= 3,200 tokens total: instructions + <= 8 pairs with excerpts <= 220 tokens each).
- Numbered excerpt preparation and untrusted content boundaries.
- Fail-loud batch response validation: missing/duplicate/unknown fields, invalid evidence refs,
  or truncation invalidate the ENTIRE batch.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

logger = logging.getLogger("sovereign.edge_inference")

# Normative AFM token budget constants (Spec §2.2, Addendum §7.2 / P1.2)
AFM_INPUT_BUDGET_TOKENS = 3200
MAX_CANDIDATE_PAIRS = 8
MAX_EXCERPT_TOKENS = 220

EdgeLabel = Literal["updates", "extends", "contradicts", "relates", "none"]
EdgeDirection = Literal["forward", "backward", "mutual", "none"]

VALID_EDGE_LABELS = frozenset({"updates", "extends", "contradicts", "relates", "none"})
VALID_DIRECTIONS = frozenset({"forward", "backward", "mutual", "none"})

# Exact per-item schema from the normative prompt template (Output Schema §).
EXPECTED_ITEM_KEYS = frozenset(
    {
        "pair_id",
        "label",
        "direction",
        "confidence",
        "supporting_evidence_indices",
        "rationale",
    }
)

# Normative label/direction compatibility from the prompt template:
# updates/extends take forward|backward; contradicts takes mutual (or
# forward|backward); relates takes mutual only; none takes none only.
COMPATIBLE_DIRECTIONS: Dict[str, frozenset] = {
    "updates": frozenset({"forward", "backward"}),
    "extends": frozenset({"forward", "backward"}),
    "contradicts": frozenset({"mutual", "forward", "backward"}),
    "relates": frozenset({"mutual"}),
    "none": frozenset({"none"}),
}


@dataclass(frozen=True)
class InferredEdge:
    pair_id: str
    label: EdgeLabel
    direction: EdgeDirection
    confidence: float
    supporting_evidence_indices: List[int]
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "label": self.label,
            "direction": self.direction,
            "confidence": self.confidence,
            "supporting_evidence_indices": list(self.supporting_evidence_indices),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class PromptRenderResult:
    prompt_text: str
    pair_ids: List[str]
    total_tokens: int
    is_measured: bool
    budget_limit: int
    budget_exceeded: bool
    source_tokens: int
    candidate_tokens: Dict[str, int]
    header_tokens: int
    line_counts_per_pair: Dict[str, int]


# --- Token Accounting with Measurement Honesty ----------------------------------


def count_tokens(text: str, encoding_name: str = "cl100k_base") -> Tuple[int, bool]:
    """Count tokens in text.

    Returns:
        (count, is_measured):
        - is_measured is True when measured via tiktoken.
        - is_measured is False when estimated via heuristic fallback (e.g. len // 4).
    """
    if not text:
        return 0, True
    try:
        import tiktoken

        enc = tiktoken.get_encoding(encoding_name)
        return len(enc.encode(text)), True
    except Exception:
        # Honest fallback heuristic: ~4 characters per token
        return max(1, math.ceil(len(text) / 4)), False


def truncate_to_tokens(
    text: str,
    max_tokens: int,
    encoding_name: str = "cl100k_base",
) -> Tuple[str, int, bool]:
    """Truncate text to at most max_tokens tokens.

    Returns:
        (truncated_text, count, is_measured)
    """
    if not text or max_tokens <= 0:
        return "", 0, True
    try:
        import tiktoken

        enc = tiktoken.get_encoding(encoding_name)
        tokens = enc.encode(text)
        if len(tokens) <= max_tokens:
            return text, len(tokens), True
        truncated = enc.decode(tokens[:max_tokens])
        return truncated, max_tokens, True
    except Exception:
        char_limit = max_tokens * 4
        if len(text) <= char_limit:
            count = max(1, math.ceil(len(text) / 4))
            return text, count, False
        truncated = text[:char_limit]
        return truncated, max_tokens, False


# --- Excerpt Numbering & Untrusted Content Sanitation ---------------------------


def format_numbered_excerpt(
    text: str,
    max_tokens: int = MAX_EXCERPT_TOKENS,
    encoding_name: str = "cl100k_base",
) -> Tuple[str, List[str], int, bool]:
    """Truncate an excerpt to max_tokens and split into numbered lines.

    Lines are numbered starting at 1 so the classifier can cite
    specific supporting evidence line indices.

    Returns:
        (formatted_numbered_block, raw_lines, token_count, is_measured)
    """
    bounded_text, _, is_measured = truncate_to_tokens(text, max_tokens, encoding_name=encoding_name)
    raw_lines = [line.strip() for line in bounded_text.splitlines() if line.strip()]
    if not raw_lines:
        raw_lines = [bounded_text.strip()] if bounded_text.strip() else ["(empty)"]

    numbered_lines = [f"[{i + 1}] {line}" for i, line in enumerate(raw_lines)]
    formatted_block = "\n".join(numbered_lines)
    # Number prefixes are part of the prompt, so enforce the cap after adding
    # them rather than only on the unnumbered body.
    if count_tokens(formatted_block, encoding_name=encoding_name)[0] > max_tokens:
        formatted_block, _, _ = truncate_to_tokens(
            formatted_block, max_tokens, encoding_name=encoding_name
        )
        # Token truncation can split the final numbered line, which would make
        # a citation appear valid even though its evidence text was omitted.
        if "\n" in formatted_block:
            formatted_block = formatted_block.rsplit("\n", 1)[0]
        else:
            prefix = "[1] "
            prefix_tokens, _ = count_tokens(prefix, encoding_name=encoding_name)
            available_tokens = max_tokens - prefix_tokens
            if available_tokens > 0:
                first_line, _, _ = truncate_to_tokens(
                    raw_lines[0], available_tokens, encoding_name=encoding_name
                )
                formatted_block = prefix + first_line
            else:
                formatted_block = ""
        raw_lines = [line for line in formatted_block.splitlines() if line.strip()]
    actual_tokens, is_measured_final = count_tokens(formatted_block, encoding_name=encoding_name)
    return formatted_block, raw_lines, actual_tokens, (is_measured and is_measured_final)


# --- Prompt Template Loading ---------------------------------------------------


def get_edge_inference_prompt_path() -> Path:
    """Resolve the location of edge_inference_v1.txt."""
    # 1. Package-relative: src/minni/prompts/edge_inference_v1.txt
    pkg_path = Path(__file__).resolve().parent / "prompts" / "edge_inference_v1.txt"
    if pkg_path.is_file():
        return pkg_path

    # 2. Workspace root relative: prompts/edge_inference_v1.txt
    root_path = Path(__file__).resolve().parents[2] / "prompts" / "edge_inference_v1.txt"
    if root_path.is_file():
        return root_path

    # Fallback to pkg_path even if not yet on disk (for error messages)
    return pkg_path


def load_edge_inference_prompt_template() -> str:
    """Load the edge inference v1 prompt template."""
    path = get_edge_inference_prompt_path()
    if not path.is_file():
        raise FileNotFoundError(f"Edge inference prompt template not found at {path}")
    return path.read_text(encoding="utf-8")


# --- Prompt Rendering ----------------------------------------------------------


def render_edge_inference_prompt(
    source: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
    max_pairs: int = MAX_CANDIDATE_PAIRS,
    max_excerpt_tokens: int = MAX_EXCERPT_TOKENS,
    budget_limit: int = AFM_INPUT_BUDGET_TOKENS,
    encoding_name: str = "cl100k_base",
) -> PromptRenderResult:
    """Render the batched edge inference prompt within the token budget.

    Args:
        source: Dict containing source learning fields:
            - learning_id or doc_id or id
            - title or name
            - body or content or excerpt
            - created_at (optional)
            - applies_when (optional)
        candidates: Sequence of candidate dicts:
            - pair_id or candidate_id or id or doc_id
            - doc_id (optional)
            - page_type (optional, default 'learning')
            - status (optional, default 'accepted')
            - body or content or excerpt
            - created_at (optional)
            - applies_when (optional)
        max_pairs: Hard ceiling on pairs sent in one batch (default 8).
        max_excerpt_tokens: Maximum tokens per excerpt (default 220).
        budget_limit: AFM token budget ceiling (default 3,200).
        encoding_name: Tokenizer encoding name (default 'cl100k_base').

    Returns:
        PromptRenderResult with prompt text, exact token metrics, and line counts.
    """
    template = load_edge_inference_prompt_template()

    # 1. Format Source Learning
    source_id = str(source.get("learning_id") or source.get("doc_id") or source.get("id") or "source")
    source_title = str(source.get("title") or source.get("name") or "Untitled")
    source_applies = str(source.get("applies_when") or "always")
    source_created = str(source.get("created_at") or "")
    source_raw_body = str(source.get("body") or source.get("content") or source.get("excerpt") or "")

    effective_max_pairs = max(0, min(max_pairs, MAX_CANDIDATE_PAIRS))
    effective_max_excerpt_tokens = max(0, min(max_excerpt_tokens, MAX_EXCERPT_TOKENS))

    source_excerpt_formatted, _, source_tokens, src_measured = format_numbered_excerpt(
        source_raw_body, max_tokens=effective_max_excerpt_tokens, encoding_name=encoding_name
    )

    source_meta_parts = [
        f"[Source Learning: {source_id} | Title: {source_title} | Applies: {source_applies}",
    ]
    if source_created:
        source_meta_parts.append(f"| Created: {source_created}")
    source_meta = " ".join(source_meta_parts) + "]"
    source_learning_text = f"{source_meta}\n[Evidence Excerpts]:\n{source_excerpt_formatted}"

    # 2. Format Candidate Pairs (strictly capped at max_pairs)
    effective_candidates = list(candidates)[:effective_max_pairs]
    candidate_blocks = []
    pair_ids: List[str] = []
    candidate_tokens_map: Dict[str, int] = {}
    line_counts_map: Dict[str, int] = {}
    all_measured = src_measured

    for idx, cand in enumerate(effective_candidates):
        pair_id = str(cand.get("pair_id") or cand.get("candidate_id") or cand.get("id") or f"pair_{idx + 1}")
        pair_ids.append(pair_id)

        target_doc_id = str(cand.get("doc_id") or cand.get("id") or pair_id)
        target_page_type = str(cand.get("page_type") or "learning")
        target_status = str(cand.get("status") or "accepted")
        target_applies = str(cand.get("applies_when") or "always")
        target_created = str(cand.get("created_at") or "")
        target_raw_body = str(cand.get("body") or cand.get("content") or cand.get("excerpt") or "")

        cand_excerpt, cand_lines, cand_tokens, cand_meas = format_numbered_excerpt(
            target_raw_body, max_tokens=effective_max_excerpt_tokens, encoding_name=encoding_name
        )
        all_measured = all_measured and cand_meas
        candidate_tokens_map[pair_id] = cand_tokens
        line_counts_map[pair_id] = len(cand_lines)

        cand_meta_parts = [
            f"--- Candidate Pair: {pair_id} | Doc: {target_doc_id} | Type: {target_page_type} | Status: {target_status} | Applies: {target_applies}",
        ]
        if target_created:
            cand_meta_parts.append(f"| Created: {target_created}")
        cand_meta = " ".join(cand_meta_parts) + " ---"
        cand_block = f"{cand_meta}\n[Evidence Excerpts]:\n{cand_excerpt}"
        candidate_blocks.append(cand_block)

    candidate_pairs_text = "\n\n".join(candidate_blocks) if candidate_blocks else "(no candidates)"

    # 3. Render Template
    rendered_prompt = template.replace("{source_learning}", source_learning_text).replace(
        "{candidate_pairs}", candidate_pairs_text
    )

    # 4. Measure Token Accounting
    total_tokens, final_measured = count_tokens(rendered_prompt, encoding_name=encoding_name)
    all_measured = all_measured and final_measured

    header_text = template.replace("{source_learning}", "").replace("{candidate_pairs}", "")
    header_tokens, _ = count_tokens(header_text, encoding_name=encoding_name)

    budget_exceeded = total_tokens > budget_limit

    return PromptRenderResult(
        prompt_text=rendered_prompt,
        pair_ids=pair_ids,
        total_tokens=total_tokens,
        is_measured=all_measured,
        budget_limit=budget_limit,
        budget_exceeded=budget_exceeded,
        source_tokens=source_tokens,
        candidate_tokens=candidate_tokens_map,
        header_tokens=header_tokens,
        line_counts_per_pair=line_counts_map,
    )


# --- Response Validator (Fail-Loud) --------------------------------------------


def _clean_json_text(raw_text: str) -> str:
    """Extract JSON string from potential markdown code fences or surrounding text."""
    text = raw_text.strip()
    # Strip markdown fences if present
    match = re.search(r"```(?:json)?\s*(\[[\s\S]*?\]|\{[\s\S]*?\})\s*```", text)
    if match:
        return match.group(1).strip()
    # If starting with [ and ending with ], return as-is
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


class _DuplicateFieldError(ValueError):
    """Raised when a JSON object in a string response repeats a key."""


def _no_duplicate_object_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """object_pairs_hook that rejects duplicate keys instead of collapsing them.

    json.loads silently keeps the last value for repeated keys, which would
    let a model mask a bad pair_id (or any field) behind a good one. The
    fail-loud contract treats duplicated fields as batch-invalid.
    """
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateFieldError(f"duplicate key {key!r} in JSON object")
        obj[key] = value
    return obj


def validate_edge_inference_response(
    raw_response: Any,
    expected_pair_ids: Sequence[str],
    line_counts_per_pair: Optional[Dict[str, int]] = None,
) -> Tuple[bool, Optional[List[InferredEdge]], Optional[str]]:
    """Strictly validate a model's batch classification response.

    Fail-Loud Contract (Spec §2.3, Addendum §7.2):
    Missing/duplicate/unknown fields, invalid evidence refs, incompatible
    label/direction pairs, or truncation invalidate the ENTIRE batch —
    partial graph commits are forbidden. Each item must carry exactly the six
    Output Schema keys; booleans are not valid confidences; label/direction
    must satisfy the normative compatibility matrix.

    Args:
        raw_response: Raw response string or parsed JSON structure.
        expected_pair_ids: Ordered list of pair_ids included in the request.
            Must contain unique non-empty strings; a malformed expectation is
            itself a caller error and fails the batch.
        line_counts_per_pair: Optional map from pair_id to number of numbered excerpt lines.

    Returns:
        (is_valid, parsed_edges, error_message):
        - If valid: (True, [InferredEdge, ...], None)
        - If invalid: (False, None, error_code_or_reason)
    """
    # 0. Caller contract: the expectation list must be unambiguous.
    try:
        expected_list = list(expected_pair_ids)
    except TypeError:
        return False, None, f"invalid_expected_pair_ids: {expected_pair_ids!r} is not a sequence"
    for eid in expected_list:
        if not isinstance(eid, str) or not eid:
            return False, None, f"invalid_expected_pair_id: expected pair ids must be non-empty strings, got {eid!r}"
    if len(set(expected_list)) != len(expected_list):
        dupes = sorted({eid for eid in expected_list if expected_list.count(eid) > 1})
        return False, None, f"duplicate_expected_pair_id: expected batch repeats {dupes}"
    expected_set = set(expected_list)

    # 1. Parse JSON if string
    parsed_items: Any = raw_response
    if isinstance(raw_response, str):
        cleaned = _clean_json_text(raw_response)
        try:
            parsed_items = json.loads(cleaned, object_pairs_hook=_no_duplicate_object_pairs)
        except _DuplicateFieldError as exc:
            return False, None, f"duplicate_field: {exc}"
        except Exception as exc:
            return False, None, f"json_decode_error: {exc}"

    if not isinstance(parsed_items, list):
        return False, None, f"schema_error: expected JSON list, got {type(parsed_items).__name__}"

    seen_pair_ids = set()
    validated_edges: List[InferredEdge] = []

    for idx, item in enumerate(parsed_items):
        if not isinstance(item, dict):
            return False, None, f"schema_error: item {idx} is not a dictionary"

        # Exact schema: no missing keys, no unknown keys.
        item_keys = set(item.keys())
        missing_keys = EXPECTED_ITEM_KEYS - item_keys
        if missing_keys:
            return (
                False,
                None,
                f"missing_field: item {idx} missing fields {sorted(missing_keys)}",
            )
        unknown_keys = item_keys - EXPECTED_ITEM_KEYS
        if unknown_keys:
            return (
                False,
                None,
                f"unknown_field: item {idx} has unexpected fields {sorted(unknown_keys)}",
            )

        # Check pair_id
        pair_id = item.get("pair_id")
        if not pair_id or not isinstance(pair_id, str):
            return False, None, f"missing_or_invalid_pair_id: item {idx} has pair_id={pair_id!r}"

        if pair_id not in expected_set:
            return False, None, f"unknown_pair_id: {pair_id} not in expected batch {expected_list}"

        if pair_id in seen_pair_ids:
            return False, None, f"duplicate_pair_id: {pair_id} appears multiple times in batch"
        seen_pair_ids.add(pair_id)

        # Check label
        label = item.get("label")
        if not isinstance(label, str) or label not in VALID_EDGE_LABELS:
            return False, None, f"invalid_label: pair {pair_id} has unsupported label {label!r}"

        # Check direction
        direction = item.get("direction")
        if not isinstance(direction, str) or direction not in VALID_DIRECTIONS:
            return False, None, f"invalid_direction: pair {pair_id} has unsupported direction {direction!r}"

        # Check normative label/direction compatibility
        if direction not in COMPATIBLE_DIRECTIONS[label]:
            return (
                False,
                None,
                f"incompatible_label_direction: pair {pair_id} label {label!r} "
                f"is incompatible with direction {direction!r}",
            )

        # Check confidence (bools are not confidences: isinstance(True, int)).
        confidence = item.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or math.isnan(confidence)
            or math.isinf(confidence)
        ):
            return False, None, f"invalid_confidence: pair {pair_id} confidence {confidence!r} is not finite float"
        confidence_float = float(confidence)
        if not (0.0 <= confidence_float <= 1.0):
            return False, None, f"invalid_confidence_range: pair {pair_id} confidence {confidence_float} not in [0, 1]"

        # Check supporting evidence indices
        evidence_indices = item.get("supporting_evidence_indices")
        if evidence_indices is None or not isinstance(evidence_indices, list):
            return False, None, f"invalid_evidence_indices: pair {pair_id} indices must be a list"
        if label != "none" and not evidence_indices:
            return (
                False,
                None,
                f"missing_evidence: pair {pair_id} non-none labels require at least one evidence index",
            )

        clean_indices: List[int] = []
        max_line_count = line_counts_per_pair.get(pair_id) if line_counts_per_pair else None

        for ref in evidence_indices:
            if not isinstance(ref, int) or isinstance(ref, bool):
                return False, None, f"invalid_evidence_index_type: pair {pair_id} index {ref!r} is not integer"
            if ref < 1:
                return False, None, f"invalid_evidence_index_value: pair {pair_id} index {ref} < 1"
            if max_line_count is not None and ref > max_line_count:
                return (
                    False,
                    None,
                    f"evidence_index_out_of_range: pair {pair_id} index {ref} > line count {max_line_count}",
                )
            clean_indices.append(ref)

        # Check rationale
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            return False, None, f"missing_or_empty_rationale: pair {pair_id} rationale missing or empty"
        if len(rationale) > 200:
            return False, None, f"rationale_too_long: pair {pair_id} rationale length {len(rationale)} > 200 chars"

        validated_edges.append(
            InferredEdge(
                pair_id=pair_id,
                label=label,
                direction=direction,
                confidence=confidence_float,
                supporting_evidence_indices=clean_indices,
                rationale=rationale.strip(),
            )
        )

    # All expected pairs must be present
    missing_pair_ids = expected_set - seen_pair_ids
    if missing_pair_ids:
        return False, None, f"missing_pair_ids: batch missing classifications for {sorted(missing_pair_ids)}"

    return True, validated_edges, None

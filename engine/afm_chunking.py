"""AFM Chunked Calling — shared primitives for calling native AFM ops with
input larger than the model's ~4096-token context window.

No knowledge of any specific pass's domain semantics lives here — reduction
is domain-specific and stays with each caller (afm_passes/session_distillation.py,
query_expand.py, afm_passes/consolidation.py), which already owns bespoke
normalization code. This module only answers: "is this payload too big, and
if so, how do I split it into pieces that fit?"

AFM calls are free (on-device, no API cost) — nothing here economizes on
call count. The only real constraint is MIN_CHUNK_TOKENS: below that floor,
further splitting cannot help, so a chunk that still overflows there is
dropped (logged) rather than infinitely re-split.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from tokens import count_tokens, get_encoder

logger = logging.getLogger("sovereign.afm_chunking")

AFM_INPUT_BUDGET_TOKENS = 3200
MIN_CHUNK_TOKENS = 200
DEFAULT_CHUNK_TOKENS = 2400
DEFAULT_OVERLAP_TOKENS = 300

# Sentence boundary regex: end-of-sentence punctuation followed by whitespace
# (mirrors chunker.py's _SENTENCE_END).
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

# How many extra words to look ahead when snapping a chunk boundary forward
# to the next sentence end.
_SENTENCE_SNAP_LOOKAHEAD_WORDS = 20


def resolve_afm_input_budget_tokens() -> int:
    """Resolve the proactive chunk-trigger budget at call time:
    MINNI_AFM_INPUT_BUDGET_TOKENS env override, then the config field
    (SovereignConfig.afm_input_budget_tokens, mirroring how
    context_budget_tokens is config-driven), then the module fallback."""
    raw = os.environ.get("MINNI_AFM_INPUT_BUDGET_TOKENS", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    try:
        from config import DEFAULT_CONFIG
        value = int(getattr(DEFAULT_CONFIG, "afm_input_budget_tokens", 0))
        if value > 0:
            return value
    except Exception:  # noqa: BLE001 - config must never break chunking
        pass
    return AFM_INPUT_BUDGET_TOKENS


def _budget_token_count(text: str) -> int:
    """Conservative token estimate for proactive AFM budget checks.

    When tiktoken is unavailable, count_tokens() falls back to whitespace
    splitting, which severely underestimates minified JSON and other dense
    serialized payloads. For chunking decisions we take the max of that
    fallback and a char/4 floor so list bin-packing stays under the cap.
    """
    counted = count_tokens(text)
    if get_encoder() is not None:
        return counted
    if not text:
        return 0
    return max(counted, max(len(text) // 4, 1))


def estimate_native_payload_tokens(payload: Dict[str, Any]) -> int:
    """Token-count the compact JSON serialization of payload — mirrors the
    Swift side's compactJSONString sizing closely enough for a proactive
    budget check (exactness isn't required; the reactive fallback in
    call_native_op_chunked catches underestimates)."""
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    return _budget_token_count(serialized)


def split_text(
    text: str,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> List[str]:
    """Token-budgeted sliding window over `text` with sentence-boundary
    snapping — the same technique as chunker.py's _sliding_window_raw /
    _snap_to_sentence, reimplemented standalone here (not via
    MarkdownChunker, which is coupled to heading parsing, code-block
    treatment, and the embedder — none of which apply to a flat AFM text
    field like a session-event blob or a candidate string).

    chunk_tokens/overlap_tokens are treated as an approximate word budget
    (mirroring chunker.py's own "512 tokens (word approximation)" comment) —
    exact token-boundary precision isn't required since the caller re-checks
    the actual serialized size before ever making an AFM call.
    """
    if not text.strip():
        return [text]
    if count_tokens(text) <= chunk_tokens:
        return [text]

    words = text.split()
    if not words:
        return [text]

    step = max(chunk_tokens - overlap_tokens, 1)
    chunks: List[str] = []
    start = 0
    n = len(words)
    while start < n:
        end = min(start + chunk_tokens, n)
        window_words = words[start:end]

        # Sentence-snap: if we're not at the end of the text, look a little
        # further ahead for the next sentence-ending punctuation and extend
        # the window to include it, so chunks end on ". "/"! "/"? " rather
        # than mid-sentence.
        if end < n:
            lookahead_words = words[end:end + _SENTENCE_SNAP_LOOKAHEAD_WORDS]
            lookahead_text = " ".join(lookahead_words)
            match = _SENTENCE_END.search(lookahead_text)
            if match:
                extra_words = lookahead_text[:match.start() + 1].split()
                window_words = window_words + extra_words
                end += len(extra_words)

        chunk_text = " ".join(window_words)
        if chunk_text.strip():
            chunks.append(chunk_text)
        if end >= n:
            break
        start += step

    return chunks or [text]


def split_list_by_token_budget(
    items: List[Any],
    budget_tokens: int,
    serialize: Callable[[Any], str] = str,
) -> List[List[Any]]:
    """Greedy bin-packing of `items` into groups whose serialized total stays
    within budget_tokens. A single item that alone exceeds budget_tokens
    still gets its own group (it cannot be split further at this layer —
    the caller decides what to do if even one item is oversized)."""
    groups: List[List[Any]] = []
    current: List[Any] = []
    current_tokens = 0
    for item in items:
        item_tokens = _budget_token_count(serialize(item))
        if current and current_tokens + item_tokens > budget_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        groups.append(current)
    return groups or [[]]


def _classify_error_kind(result: Any) -> str:
    data = result.data if isinstance(getattr(result, "data", None), dict) else {}
    return str(data.get("error_kind") or "").strip().lower()


def _effective_overlap_tokens(chunk_tokens: int, overlap_tokens: int) -> int:
    """Clamp overlap_tokens so it always stays meaningfully smaller than
    chunk_tokens, regardless of how far chunk_tokens has shrunk during
    recursive re-splitting.

    Without this clamp, a caller-supplied overlap_tokens (e.g. the default
    300) never shrinks as _chunk_and_call halves chunk_tokens toward
    MIN_CHUNK_TOKENS. Once chunk_tokens <= overlap_tokens, split_text's
    step = max(chunk_tokens - overlap_tokens, 1) collapses to 1, producing
    roughly one chunk per word instead of a small, bounded number of
    chunks — and since _chunk_and_call recurses per-chunk, that blows up
    combinatorially. Clamping to at most a quarter of chunk_tokens keeps
    step comfortably above 1 for any chunk_tokens >= MIN_CHUNK_TOKENS.
    """
    return max(min(overlap_tokens, chunk_tokens // 4), 0)


def _chunk_and_call(
    chain: Any,
    op_name: str,
    payload: Dict[str, Any],
    text_field: str,
    timeout: float,
    chunk_tokens: int,
    overlap_tokens: int,
) -> List[Any]:
    """Already committed to chunking payload[text_field]. Recurses (shrinking
    chunk_tokens) on individual chunks that still trip context_overflow,
    down to MIN_CHUNK_TOKENS — AFM calls are free, so there is no reason to
    give up early; the only real stop condition is the floor."""
    text = str(payload.get(text_field) or "")
    if not text.strip() or chunk_tokens < MIN_CHUNK_TOKENS:
        return [chain.native_op(op_name, payload, timeout=timeout)]

    effective_overlap = _effective_overlap_tokens(chunk_tokens, overlap_tokens)
    pieces = split_text(text, chunk_tokens=chunk_tokens, overlap_tokens=effective_overlap)
    if len(pieces) <= 1:
        if chunk_tokens <= MIN_CHUNK_TOKENS:
            logger.info(
                "afm native op %s: %s cannot be split below the %d-token floor; "
                "issuing a single call with the full payload",
                op_name, text_field, MIN_CHUNK_TOKENS,
            )
            return [chain.native_op(op_name, payload, timeout=timeout)]
        return _chunk_and_call(
            chain, op_name, payload, text_field, timeout,
            max(chunk_tokens // 2, MIN_CHUNK_TOKENS), overlap_tokens,
        )

    results: List[Any] = []
    for piece in pieces:
        chunk_payload = dict(payload)
        chunk_payload[text_field] = piece
        chunk_result = chain.native_op(op_name, chunk_payload, timeout=timeout)
        if (
            not chunk_result.ok
            and _classify_error_kind(chunk_result) == "context_overflow"
            and chunk_tokens > MIN_CHUNK_TOKENS
        ):
            logger.info(
                "afm native op %s: chunk still overflowed at chunk_tokens=%d, re-splitting",
                op_name, chunk_tokens,
            )
            results.extend(_chunk_and_call(
                chain, op_name, chunk_payload, text_field, timeout,
                max(chunk_tokens // 2, MIN_CHUNK_TOKENS), overlap_tokens,
            ))
        else:
            results.append(chunk_result)
    return results


def call_native_op_chunked(
    chain: Any,
    op_name: str,
    payload: Dict[str, Any],
    text_field: str,
    timeout: float = 4.0,
    budget_tokens: Optional[int] = None,
    chunk_tokens: Optional[int] = None,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> Tuple[List[Any], bool]:
    """Call chain.native_op(op_name, payload, timeout), chunking
    payload[text_field] first if the serialized payload is over budget (or
    reactively, if a single call unexpectedly trips context_overflow).

    Returns (results, was_chunked):
    - Under budget: calls chain.native_op() exactly once, byte-identical to
      today's un-chunked behavior. Returns ([result], False).
    - Over budget (proactively or reactively): splits payload[text_field]
      into N pieces via split_text and calls chain.native_op once per
      piece, sequentially. Returns (list_of_N_results, True).

    Does NOT reduce results — reduction is domain-specific; see
    reduce_via_same_op for the shared reduce-pass helper.
    """
    budget = budget_tokens if budget_tokens is not None else resolve_afm_input_budget_tokens()
    size = chunk_tokens if chunk_tokens is not None else DEFAULT_CHUNK_TOKENS

    estimated = estimate_native_payload_tokens(payload)
    if estimated <= budget:
        result = chain.native_op(op_name, payload, timeout=timeout)
        if result.ok or _classify_error_kind(result) != "context_overflow":
            return [result], False
        logger.info(
            "afm native op %s: reactive chunk trigger (estimated=%d <= budget=%d but call overflowed)",
            op_name, estimated, budget,
        )

    # Size chunks to the room actually left after the non-text fields — a
    # large sidecar (e.g. contradiction's `existing`) would otherwise make
    # every "chunked" payload still exceed the real budget and waste a
    # halving-recursion round per chunk.
    base_payload = {k: v for k, v in payload.items() if k != text_field}
    base_tokens = estimate_native_payload_tokens(base_payload)
    if base_tokens > budget:
        logger.info(
            "afm native op %s: payload over budget from non-%s fields alone "
            "(base=%d > budget=%d) — text chunking cannot help; calling once as-is",
            op_name, text_field, base_tokens, budget,
        )
        return [chain.native_op(op_name, payload, timeout=timeout)], False
    size = max(min(size, budget - base_tokens), MIN_CHUNK_TOKENS)

    return _chunk_and_call(chain, op_name, payload, text_field, timeout, size, overlap_tokens), True


def reduce_via_same_op(
    chain: Any,
    op_name: str,
    chunk_results: List[Any],
    build_reduce_payload: Callable[[List[Dict[str, Any]]], Dict[str, Any]],
    text_field: str,
    timeout: float = 4.0,
    budget_tokens: Optional[int] = None,
) -> Optional[Any]:
    """Reduce N successful per-chunk results into one, by calling op_name
    again with a payload built from the chunks' .data (build_reduce_payload
    decides the shape — e.g. {"text": joined_titles_and_assertions} for
    session_distill). No new Swift-side op is needed: the reduce call uses
    the exact same op_name as the map phase.

    If the reduce payload is itself over budget, this recurses (tree
    reduction) via call_native_op_chunked + another reduce_via_same_op pass
    over the reduced pieces — AFM calls are free, so there is no single-pass
    ceiling.

    Returns None if there are zero successful chunk_results to reduce.
    Returns the sole result unreduced if there is exactly one success (no
    reduce call needed).
    """
    successes_data = [r.data for r in chunk_results if r.ok and isinstance(r.data, dict)]
    if not successes_data:
        return None
    if len(successes_data) == 1:
        return next(r for r in chunk_results if r.ok)

    reduce_payload = build_reduce_payload(successes_data)
    results, _ = call_native_op_chunked(
        chain, op_name, reduce_payload, text_field, timeout=timeout, budget_tokens=budget_tokens,
    )
    reduced_successes = [r for r in results if r.ok]
    if len(reduced_successes) == 1:
        return reduced_successes[0]
    if len(reduced_successes) > 1:
        return reduce_via_same_op(
            chain, op_name, reduced_successes, build_reduce_payload, text_field,
            timeout=timeout, budget_tokens=budget_tokens,
        )
    # Final safety net: every reduce attempt failed — a partial answer beats
    # nothing, so fall back to the first successful chunk.
    return next((r for r in chunk_results if r.ok), None)


class _FoldedResult:
    """Result-shaped wrapper for deterministic fold outputs, so callers read
    .ok/.data uniformly whether the reduce was an AFM call or a pure fold."""
    __slots__ = ("ok", "data", "status", "error")

    def __init__(self, data: Dict[str, Any]):
        self.ok = True
        self.data = data
        self.status = None
        self.error = None


def log_native_error_kind(op: str, trace_id: str, result: Any) -> str:
    """Surface the helper's error_kind so a recoverable trip (context_overflow /
    guardrail) is no longer indistinguishable from "AFM down". Returns the
    classified kind for the caller to record; behavior otherwise unchanged.

    NOTE (PR84-2 retraction): this CLASSIFIES and LOGS the trip only — it does
    NOT chunk on context_overflow or rephrase on guardrail. The pass still
    returns empty on a trip. The #84 description's "(chunk)" / "(rephrase/skip)"
    wording describes intended future recovery, not shipped behavior; real
    recovery is tracked as separate follow-up work."""
    data = result.data if isinstance(getattr(result, "data", None), dict) else {}
    error_kind = str(data.get("error_kind") or "").strip() or "unknown"
    if error_kind in {"context_overflow", "guardrail"}:
        logger.info(
            "afm native op %s recoverable trip (%s): status=%s error=%s trace=%s",
            op, error_kind, getattr(result, "status", None), getattr(result, "error", None), trace_id,
        )
    else:
        logger.info(
            "afm native op %s unavailable (error_kind=%s): status=%s error=%s trace=%s",
            op, error_kind, getattr(result, "status", None), getattr(result, "error", None), trace_id,
        )
    return error_kind


def call_native_op_and_reduce(
    chain: Any,
    op_name: str,
    payload: Dict[str, Any],
    text_field: str,
    build_reduce_payload: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None,
    fold: Optional[Callable[[List[Dict[str, Any]]], Optional[Dict[str, Any]]]] = None,
    timeout: float = 4.0,
    budget_tokens: Optional[int] = None,
    trace_id: str = "",
) -> Optional[Any]:
    """Shared orchestration for every text-field native op call site:
    chunk-call op_name, then reduce multi-chunk results back to one.

    Reduce strategy — exactly one of:
    - fold: deterministic fold over the successful chunks' .data dicts
      (judgment ops like triage/contradiction, where re-judging the model's
      own verdicts via a second AFM call is unsound). The fold returns the
      final data dict, or None to signal no usable outcome.
    - build_reduce_payload: reduce-via-same-op (synthesis ops like
      session_distill/neighborhood_summary, where a second AFM call over the
      partials is the intended synthesis).

    Returns the final ok result, or None after logging the error_kind via
    log_native_error_kind — including when EVERY chunk failed, the case the
    per-site copies of this block used to drop without any diagnostic."""
    chunk_results, was_chunked = call_native_op_chunked(
        chain, op_name, payload, text_field=text_field,
        timeout=timeout, budget_tokens=budget_tokens,
    )
    if not was_chunked:
        result = chunk_results[0] if chunk_results else None
    elif fold is not None:
        successes = [r.data for r in chunk_results if r.ok and isinstance(r.data, dict)]
        if not successes:
            result = None
        elif len(successes) == 1:
            result = next(r for r in chunk_results if r.ok)
        else:
            folded = fold(successes)
            result = _FoldedResult(folded) if folded is not None else None
    else:
        result = reduce_via_same_op(
            chain, op_name, chunk_results, build_reduce_payload, text_field=text_field,
            timeout=timeout, budget_tokens=budget_tokens,
        )
    if result is None:
        failed = next((r for r in chunk_results if not getattr(r, "ok", False)), None)
        if failed is not None:
            log_native_error_kind(op_name, trace_id, failed)
        return None
    if not result.ok:
        log_native_error_kind(op_name, trace_id, result)
        return None
    return result

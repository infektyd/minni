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
import re
from typing import Any, Callable, Dict, List

from tokens import count_tokens

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


def estimate_native_payload_tokens(payload: Dict[str, Any]) -> int:
    """Token-count the compact JSON serialization of payload — mirrors the
    Swift side's compactJSONString sizing closely enough for a proactive
    budget check (exactness isn't required; the reactive fallback in
    call_native_op_chunked catches underestimates)."""
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    return count_tokens(serialized)


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
        item_tokens = count_tokens(serialize(item))
        if current and current_tokens + item_tokens > budget_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        groups.append(current)
    return groups or [[]]

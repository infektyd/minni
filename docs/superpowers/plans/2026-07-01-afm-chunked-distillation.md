# AFM Chunked Distillation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the hardcoded ~4096-token AFM context cap across every native-AFM call site in Minni (Python and TypeScript) by replacing silent `[:6000]`-char truncation / empty-on-overflow fallback with real chunking + reduction, so oversized input is split, distilled per-piece, and merged into one final answer instead of dropping content.

**Architecture:** Two small, dumb, reusable "call this op with arbitrarily large input" modules — `engine/afm_chunking.py` (Python) and `plugins/minni/src/afm-chunking.ts` (TypeScript), mirroring each other like `model_provider.py`/`providers.ts` already do. Each existing call site is migrated to route through these instead of its bespoke truncation constant; domain-specific reduction (how to merge N partial results into one) stays with each call site, which already owns bespoke normalization code.

**Tech Stack:** Python 3 (pytest, tiktoken via existing `engine/tokens.py`), TypeScript/Node (`node --test`, existing `plugins/minni` build via `npm run build`).

## Global Constraints

- No changes to `engine/native_afm_helper.swift` (compiled Swift binary; chunking happens entirely in the two callers, confirmed by research — see spec §1).
- `AFM_INPUT_BUDGET_TOKENS = 3200` (Python) / `AFM_INPUT_BUDGET_TOKENS = 3200` (TS) — the proactive chunk trigger, headroom below the ~4096 model context window (spec §3.5).
- `MIN_CHUNK_TOKENS = 200` — floor below which further splitting cannot help; a chunk at this floor that still overflows is dropped (logged), not infinitely re-split.
- `DEFAULT_CHUNK_TOKENS = 2400`, `DEFAULT_OVERLAP_TOKENS = 300` — initial chunk size/overlap for `split_text`.
- AFM calls are free (on-device, no API cost) — call count is never a design constraint; recursion/reduction depth is bounded only by `MIN_CHUNK_TOKENS`, not by an artificial call cap (spec §3.1, §3.2, per operator direction "afm calls are free so don't worry about the number of calls").
- Every migrated call site must remain byte-identical in behavior for inputs that are already under budget (no regression for the common case) — verified per-task by keeping existing tests green.
- Follow this repo's existing test conventions exactly: Python tests run via `pytest` from the `engine/` directory; TypeScript tests run via `npm test` (`npm run build && node --test --import ./tests/setup-env.mjs tests/*.test.mjs`) from `plugins/minni/`, importing from `../dist/*.js` (compiled output), never `../src/*.ts` directly.
- Git: this repo has a pre-commit hook (`.githooks/pre-commit`) enforcing a public-boundary guard (blocks `.gitignore`/`.gitfilters` matches) and a fast lint gate (ruff for `engine/`, eslint/typecheck for the plugin) — do not use `MINNI_SKIP_HOOKS=1`.

---

## File Structure

| File | Responsibility |
|---|---|
| `engine/config.py` | Add `afm_input_budget_tokens: int = 3200` field (modify) |
| `engine/afm_chunking.py` | New — Python chunking primitives: token estimate, text splitter, list splitter, chunked-call wrapper, tree-reduce helper (create) |
| `engine/test_afm_chunking.py` | New — unit tests for the above (create) |
| `engine/afm_passes/session_distillation.py` | Migrate `session_distill`, `entity_extract`, `contradiction`, `compile_pass_proposals` call sites; remove `[:6000]` cap in `_combined_session_text` (modify) |
| `engine/test_native_afm_helper_ops.py` | Add oversized-input test cases for the 4 ops above (modify) |
| `engine/query_expand.py` | Migrate `neighborhood_summary` call site; remove `prompt[:6000]` cap on the native path (modify) |
| `engine/test_query_expand.py` | Add oversized-input test case (modify, or create if it doesn't exist — verify during Task 9) |
| `engine/afm_passes/consolidation.py` | Migrate `triage` call site; remove `content[:6000]` cap (modify) |
| `plugins/minni/src/afm-chunking.ts` | New — TypeScript mirror of `afm_chunking.py` (create) |
| `plugins/minni/tests/afm-chunking.test.mjs` | New — unit tests for the above (create) |
| `plugins/minni/src/task.ts` | Migrate `callAfmPrepareTask` (the `prepare_task`/`prepare_outcome` call site — the exact op from the reported incident) to chunk `relevantSources` (modify) |
| `plugins/minni/tests/task.test.mjs` | Add oversized-`relevantSources` test case (modify) |

---

## Task 1: Config field for the AFM input budget

**Files:**
- Modify: `engine/config.py:104` (next to `context_budget_tokens`)
- Test: `engine/test_config.py` (verify this file exists first; if not, create it)

**Interfaces:**
- Produces: `SovereignConfig.afm_input_budget_tokens: int` (default `3200`), used by Task 2's `AFM_INPUT_BUDGET_TOKENS` constant as the module-level default (the field exists on config for operators who want to override it, mirroring how `context_budget_tokens` already works).

- [ ] **Step 1: Check whether `engine/test_config.py` exists**

Run: `ls engine/test_config.py`

If it exists, read it to match its existing style for the new test. If not, the test in Step 2 creates it.

- [ ] **Step 2: Write the failing test**

Add to `engine/test_config.py` (create with this content if the file doesn't exist, otherwise append the function):

```python
def test_afm_input_budget_tokens_default():
    from config import SovereignConfig

    cfg = SovereignConfig()
    assert cfg.afm_input_budget_tokens == 3200
```

If creating the file fresh, it needs this header first:

```python
"""Focused tests for SovereignConfig field defaults."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd engine && python3 -m pytest test_config.py::test_afm_input_budget_tokens_default -v`
Expected: FAIL with `AttributeError: 'SovereignConfig' object has no attribute 'afm_input_budget_tokens'`

- [ ] **Step 4: Add the field**

In `engine/config.py`, find this block (around line 103-105):

```python
    # Context window budgeting
    context_budget_tokens: int = 4096     # Max tokens to return in a single recall
    token_model: str = "cl100k_base"      # tiktoken encoding for counting
```

Replace it with:

```python
    # Context window budgeting
    context_budget_tokens: int = 4096     # Max tokens to return in a single recall
    token_model: str = "cl100k_base"      # tiktoken encoding for counting

    # Proactive chunk trigger for native AFM calls (engine/afm_chunking.py) —
    # headroom below the ~4096-token model context window for the Swift
    # helper's instructions string + @Generable schema guide + JSON envelope
    # overhead, none of which is visible to Python/TypeScript callers.
    afm_input_budget_tokens: int = 3200
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd engine && python3 -m pytest test_config.py::test_afm_input_budget_tokens_default -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add engine/config.py engine/test_config.py
git commit -m "afm-chunking: add afm_input_budget_tokens config field"
```

---

## Task 2: Python core primitives — token estimate, text splitter, list splitter

**Files:**
- Create: `engine/afm_chunking.py`
- Test: `engine/test_afm_chunking.py`

**Interfaces:**
- Consumes: `tokens.count_tokens(text: str) -> int` (existing, `engine/tokens.py:53`)
- Produces:
  - `AFM_INPUT_BUDGET_TOKENS: int = 3200`
  - `MIN_CHUNK_TOKENS: int = 200`
  - `DEFAULT_CHUNK_TOKENS: int = 2400`
  - `DEFAULT_OVERLAP_TOKENS: int = 300`
  - `estimate_native_payload_tokens(payload: Dict[str, Any]) -> int`
  - `split_text(text: str, chunk_tokens: int = DEFAULT_CHUNK_TOKENS, overlap_tokens: int = DEFAULT_OVERLAP_TOKENS) -> List[str]`
  - `split_list_by_token_budget(items: List[Any], budget_tokens: int, serialize: Callable[[Any], str] = str) -> List[List[Any]]`

- [ ] **Step 1: Write the failing tests**

Create `engine/test_afm_chunking.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd engine && python3 -m pytest test_afm_chunking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'afm_chunking'`

- [ ] **Step 3: Write the implementation**

Create `engine/afm_chunking.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd engine && python3 -m pytest test_afm_chunking.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add engine/afm_chunking.py engine/test_afm_chunking.py
git commit -m "afm-chunking: add Python core primitives (token estimate, text/list splitters)"
```

---

## Task 3: Python chunked-call wrapper + tree-reduce helper

**Files:**
- Modify: `engine/afm_chunking.py` (append to the file created in Task 2)
- Test: `engine/test_afm_chunking.py` (append)

**Interfaces:**
- Consumes: `estimate_native_payload_tokens`, `split_text`, `AFM_INPUT_BUDGET_TOKENS`, `MIN_CHUNK_TOKENS`, `DEFAULT_CHUNK_TOKENS`, `DEFAULT_OVERLAP_TOKENS` (Task 2). A `chain` object with `.native_op(op_name: str, payload: Dict[str, Any], timeout: float) -> ProviderResult` (the existing `model_provider.ProviderChain`/`AfmProvider`, already used by every call site as `default_provider_chain().native_op(...)`). `ProviderResult` has `.ok: bool`, `.data: Dict[str, Any]`, `.status`, `.error` (existing, `engine/model_provider.py:55-61`).
- Produces:
  - `call_native_op_chunked(chain, op_name: str, payload: Dict[str, Any], text_field: str, timeout: float = 4.0, budget_tokens: Optional[int] = None, chunk_tokens: Optional[int] = None, overlap_tokens: int = DEFAULT_OVERLAP_TOKENS) -> Tuple[List[Any], bool]` — returns `(results, was_chunked)` where `results` is a list of the same `ProviderResult`-shaped objects `chain.native_op` returns.
  - `reduce_via_same_op(chain, op_name: str, chunk_results: List[Any], build_reduce_payload: Callable[[List[Dict[str, Any]]], Dict[str, Any]], text_field: str, timeout: float = 4.0, budget_tokens: Optional[int] = None) -> Optional[Any]` — returns a single `ProviderResult`-shaped object, or `None` if there was nothing successful to reduce.

- [ ] **Step 1: Write the failing tests**

Append to `engine/test_afm_chunking.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd engine && python3 -m pytest test_afm_chunking.py -v -k "chunked or reduce"`
Expected: FAIL with `ImportError: cannot import name 'call_native_op_chunked'`

- [ ] **Step 3: Write the implementation**

Append to `engine/afm_chunking.py`:

```python
from typing import Optional, Tuple  # noqa: E402 - grouped with the rest below


def _classify_error_kind(result: Any) -> str:
    data = result.data if isinstance(getattr(result, "data", None), dict) else {}
    return str(data.get("error_kind") or "").strip().lower()


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

    pieces = split_text(text, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
    if len(pieces) <= 1:
        if chunk_tokens <= MIN_CHUNK_TOKENS:
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
    budget = budget_tokens if budget_tokens is not None else AFM_INPUT_BUDGET_TOKENS
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd engine && python3 -m pytest test_afm_chunking.py -v`
Expected: PASS (all tests, including the 7 from Task 2)

- [ ] **Step 5: Commit**

```bash
git add engine/afm_chunking.py engine/test_afm_chunking.py
git commit -m "afm-chunking: add call_native_op_chunked + reduce_via_same_op (tree reduction)"
```

---

## Task 4: Migrate `session_distill` in `session_distillation.py`

**Files:**
- Modify: `engine/afm_passes/session_distillation.py:292-345` (`_combined_session_text`, `_native_session_distill_draft`)
- Test: `engine/test_native_afm_helper_ops.py`

**Interfaces:**
- Consumes: `call_native_op_chunked`, `reduce_via_same_op` (Task 3, `engine/afm_chunking.py`)
- Produces: no change to `_native_session_distill_draft`'s public signature (`(text: str, sources: List[str], trace_id: str) -> Optional[Dict[str, Any]]`) — callers elsewhere in this file are unaffected.

- [ ] **Step 1: Write the failing test**

Append to `engine/test_native_afm_helper_ops.py` (this file already has `_make_db`, `_seed_event`, `_AFMResult`, `_route` helpers from the existing tests — reuse them):

```python
def test_session_distill_chunks_and_reduces_oversized_text(tmp_path, monkeypatch):
    from afm_passes import session_distillation

    db_obj, cfg = _make_db(tmp_path)
    # Seed enough events that _combined_session_text produces well over
    # AFM_INPUT_BUDGET_TOKENS (3200) worth of text — previously this would
    # have been silently truncated to [:6000] chars.
    now = time.time()
    with db_obj.cursor() as c:
        for i in range(80):
            c.execute(
                """
                INSERT INTO episodic_events
                (agent_id, event_type, content, task_id, thread_id, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "codex", "session",
                    f"Event {i}: " + ("substantial detail about the AFM chunking work. " * 20),
                    "task-x", "thread-x", json.dumps({"source": "unit-test"}), now,
                ),
            )

    call_count = {"n": 0}

    def fake_session_distill(payload):
        call_count["n"] += 1
        # The reduce call joins per-chunk "title: assertion (appliesWhen)"
        # lines, so its text won't contain "Event " — use that to
        # distinguish a map call from the reduce call.
        if "Event " in payload.get("text", ""):
            return _AFMResult(ok=True, data={
                "title": f"Chunk {call_count['n']}", "assertion": "partial assertion",
                "appliesWhen": "during chunking", "category": "concept",
            })
        return _AFMResult(ok=True, data={
            "title": "Synthesized final learning", "assertion": "the reduced assertion",
            "appliesWhen": "always", "category": "concept",
        })

    op_map = {"session_distill": fake_session_distill}
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = session_distillation.run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="trace-chunked")

    assert call_count["n"] > 1  # multiple map calls + one reduce call
    distill = [d for d in result["drafts"] if d.get("prompt_version") == "native.session_distill.v1"]
    assert len(distill) == 1
    assert distill[0]["title"] == "Synthesized final learning"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_session_distill_chunks_and_reduces_oversized_text -v`
Expected: FAIL — `call_count["n"]` will be `1` (today's code makes exactly one truncated call), so the `call_count["n"] > 1` assertion fails.

- [ ] **Step 3: Modify `_combined_session_text` to stop truncating**

In `engine/afm_passes/session_distillation.py`, find (around line 292-304):

```python
def _combined_session_text(events: List[Dict[str, Any]], raw_docs: List[Dict[str, Any]]) -> str:
    """Flatten the session into one text blob for the guided AFM ops, capped
    well under the helper's ~4K token budget (first ~6000 chars)."""
    lines: List[str] = []
    for event in events:
        content = (event.get("content") or "").strip()
        if content:
            lines.append(f"{event.get('event_type', 'event')}: {content}")
    for doc in raw_docs:
        path = (doc.get("path") or "").strip()
        if path:
            lines.append(f"raw document: {path}")
    return "\n".join(lines)[:6000]
```

Replace with:

```python
def _combined_session_text(events: List[Dict[str, Any]], raw_docs: List[Dict[str, Any]]) -> str:
    """Flatten the session into one text blob for the guided AFM ops.
    Unbounded — oversized input is chunked by call_native_op_chunked at the
    call site (session_distill, entity_extract) instead of being silently
    truncated here."""
    lines: List[str] = []
    for event in events:
        content = (event.get("content") or "").strip()
        if content:
            lines.append(f"{event.get('event_type', 'event')}: {content}")
    for doc in raw_docs:
        path = (doc.get("path") or "").strip()
        if path:
            lines.append(f"raw document: {path}")
    return "\n".join(lines)
```

- [ ] **Step 4: Migrate `_native_session_distill_draft`**

Find (around line 306-345):

```python
def _native_session_distill_draft(
    text: str, sources: List[str], trace_id: str
) -> Optional[Dict[str, Any]]:
    """Additive review-only draft from the guided `session_distill` op. Never
    replaces the existing compile_pass drafts; emitted alongside them."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return None
    if not text.strip() or not sources:
        return None
    result = default_provider_chain().native_op("session_distill", {"text": text}, timeout=4.0)
    if not result.ok:
        _log_native_error_kind("session_distill", trace_id, result)
        return None
    data = result.data if isinstance(result.data, dict) else {}
```

Replace with:

```python
def _build_session_distill_reduce_payload(partials: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Synthesize a compact 'text' input for a second session_distill call
    from N per-chunk DistilledLearningResult-shaped dicts — reduce-via-
    same-op, no new Swift surface needed."""
    lines = []
    for partial in partials:
        title = str(partial.get("title") or "").strip()
        assertion = str(partial.get("assertion") or "").strip()
        applies_when = str(partial.get("appliesWhen") or "").strip()
        if title or assertion:
            lines.append(f"{title}: {assertion} ({applies_when})".strip())
    return {"text": "\n".join(lines)}


def _native_session_distill_draft(
    text: str, sources: List[str], trace_id: str
) -> Optional[Dict[str, Any]]:
    """Additive review-only draft from the guided `session_distill` op. Never
    replaces the existing compile_pass drafts; emitted alongside them."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return None
    if not text.strip() or not sources:
        return None
    chain = default_provider_chain()
    chunk_results, was_chunked = call_native_op_chunked(
        chain, "session_distill", {"text": text}, text_field="text", timeout=4.0,
    )
    if was_chunked:
        result = reduce_via_same_op(
            chain, "session_distill", chunk_results, _build_session_distill_reduce_payload,
            text_field="text", timeout=4.0,
        )
    else:
        result = chunk_results[0] if chunk_results else None
    if result is None or not result.ok:
        if result is not None:
            _log_native_error_kind("session_distill", trace_id, result)
        return None
    data = result.data if isinstance(result.data, dict) else {}
```

(The rest of the function body — everything after the `data = ...` line — is unchanged.)

- [ ] **Step 5: Add the import**

At the top of `engine/afm_passes/session_distillation.py`, find:

```python
from afm_provider import resolve_afm_mode
from model_provider import default_provider_chain
```

Replace with:

```python
from afm_chunking import call_native_op_chunked, reduce_via_same_op
from afm_provider import resolve_afm_mode
from model_provider import default_provider_chain
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_session_distill_chunks_and_reduces_oversized_text -v`
Expected: PASS

- [ ] **Step 7: Run the full existing test file to check for regressions**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py -v`
Expected: PASS (all tests, including `test_session_distill_emits_additive_review_draft` and `test_session_distill_missing_fields_emits_no_draft` from before this change — under-budget text takes the exact same single-call path)

- [ ] **Step 8: Commit**

```bash
git add engine/afm_passes/session_distillation.py engine/test_native_afm_helper_ops.py
git commit -m "afm-chunking: migrate session_distill to chunked calling + AFM reduce"
```

---

## Task 5: Migrate `entity_extract` in `session_distillation.py`

**Files:**
- Modify: `engine/afm_passes/session_distillation.py:355-380` (`_native_entity_extract`)
- Test: `engine/test_native_afm_helper_ops.py`

**Interfaces:**
- Consumes: `call_native_op_chunked` (Task 3) — no `reduce_via_same_op` here; this is a list-shaped op merged deterministically (dedupe by `name.lower()`, cap 20 — matching the existing behavior exactly).
- Produces: no change to `_native_entity_extract`'s signature (`(text: str, trace_id: str) -> List[Dict[str, str]]`).

- [ ] **Step 1: Write the failing test**

Append to `engine/test_native_afm_helper_ops.py`:

```python
def test_entity_extract_merges_entities_across_chunks_deterministically(tmp_path, monkeypatch):
    from afm_passes import session_distillation

    db_obj, cfg = _make_db(tmp_path)
    now = time.time()
    with db_obj.cursor() as c:
        for i in range(80):
            c.execute(
                """
                INSERT INTO episodic_events
                (agent_id, event_type, content, task_id, thread_id, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "codex", "session",
                    f"Event {i}: " + ("substantial detail about the AFM chunking work. " * 20),
                    "task-x", "thread-x", json.dumps({"source": "unit-test"}), now,
                ),
            )

    call_count = {"n": 0}

    def fake_entity_extract(payload):
        call_count["n"] += 1
        # Each chunk call reports one distinct entity plus a duplicate
        # ("Minni") shared across chunks, to verify dedupe.
        return _AFMResult(ok=True, data={
            "entities": [
                {"name": f"Entity{call_count['n']}", "type": "concept"},
                {"name": "Minni", "type": "system"},
            ]
        })

    op_map = {"entity_extract": fake_entity_extract}
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = session_distillation.run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="trace-entities")

    assert call_count["n"] > 1
    entities = result["inputs"].get("entities") or []
    names = [e["name"] for e in entities]
    assert names.count("Minni") == 1  # deduped across chunks
    assert len(entities) <= 20  # existing cap preserved
```

- [ ] **Step 2: Check whether `entities` is currently surfaced on `result["inputs"]`**

Run: `grep -n "entities" engine/afm_passes/session_distillation.py`

Expected: this shows where `_native_entity_extract`'s return value is stored into `pass_input` (e.g. `pass_input["entities"] = ...`). If the exact key differs from `"entities"`, update the test's `result["inputs"].get("entities")` line to match the actual key before proceeding.

- [ ] **Step 3: Run test to verify it fails**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_entity_extract_merges_entities_across_chunks_deterministically -v`
Expected: FAIL — `call_count["n"]` will be `1` (today's single truncated call).

- [ ] **Step 4: Migrate `_native_entity_extract`**

Find (around line 355-380):

```python
def _native_entity_extract(text: str, trace_id: str) -> List[Dict[str, str]]:
    """Additive review-only entities for the wikilink graph seed. Proposal only:
    NEVER writes durable links."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return []
    if not text.strip():
        return []
    result = default_provider_chain().native_op("entity_extract", {"text": text}, timeout=4.0)
    if not result.ok:
        _log_native_error_kind("entity_extract", trace_id, result)
        return []
    data = result.data if isinstance(result.data, dict) else {}
    raw = data.get("entities")
    if not isinstance(raw, list):
        return []
    entities: List[Dict[str, str]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        etype = str(item.get("type") or "").strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            entities.append({"name": name, "type": etype})
    return entities[:20]
```

Replace with:

```python
def _native_entity_extract(text: str, trace_id: str) -> List[Dict[str, str]]:
    """Additive review-only entities for the wikilink graph seed. Proposal only:
    NEVER writes durable links. List-shaped: merged deterministically across
    chunks (dedupe by name.lower(), cap 20) rather than via an AFM reduce
    pass — no synthesis is needed for a flat entity list."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return []
    if not text.strip():
        return []
    chain = default_provider_chain()
    chunk_results, _ = call_native_op_chunked(chain, "entity_extract", {"text": text}, text_field="text", timeout=4.0)
    if not any(r.ok for r in chunk_results):
        _log_native_error_kind("entity_extract", trace_id, chunk_results[0])
        return []
    entities: List[Dict[str, str]] = []
    seen: set = set()
    for result in chunk_results:
        if not result.ok:
            continue
        data = result.data if isinstance(result.data, dict) else {}
        raw = data.get("entities")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            etype = str(item.get("type") or "").strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                entities.append({"name": name, "type": etype})
    return entities[:20]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_entity_extract_merges_entities_across_chunks_deterministically -v`
Expected: PASS

- [ ] **Step 6: Run the full test file to check for regressions**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py -v`
Expected: PASS (all tests, including `test_entity_extract_surfaces_entities_in_inputs`)

- [ ] **Step 7: Commit**

```bash
git add engine/afm_passes/session_distillation.py engine/test_native_afm_helper_ops.py
git commit -m "afm-chunking: migrate entity_extract to chunked calling + deterministic merge"
```

---

## Task 6: Migrate `contradiction` in `session_distillation.py`

**Files:**
- Modify: `engine/afm_passes/session_distillation.py:418-455` (`_native_contradiction_signal`)
- Test: `engine/test_native_afm_helper_ops.py`

**Interfaces:**
- Consumes: `call_native_op_chunked`, `reduce_via_same_op` (Task 3)
- Produces: no change to `_native_contradiction_signal`'s signature (`(db, drafts: List[Dict[str, Any]], trace_id: str) -> Optional[Dict[str, Any]]`).

- [ ] **Step 1: Write the failing test**

Append to `engine/test_native_afm_helper_ops.py`:

```python
def test_contradiction_signal_chunks_oversized_candidate(tmp_path, monkeypatch):
    from afm_passes import session_distillation

    db_obj, cfg = _make_db(tmp_path)
    _seed_event(db_obj)
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (content, created_at) VALUES (?, ?)",
            ("Built the native AFM helper wiring, an existing durable fact.", time.time()),
        )

    call_count = {"n": 0}

    def fake_contradiction(payload):
        call_count["n"] += 1
        return _AFMResult(ok=True, data={"contradicts": False, "reason": f"partial reason {call_count['n']}"})

    op_map = {
        "session_distill": lambda p: _AFMResult(ok=True, data={
            "title": "A" * 50, "assertion": "long assertion body. " * 400,
            "appliesWhen": "always", "category": "concept",
        }),
        "contradiction": fake_contradiction,
    }
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = session_distillation.run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="trace-contradiction")

    assert call_count["n"] > 1
    assert result["inputs"].get("contradiction") is not None
```

- [ ] **Step 2: Check the exact schema for the `learnings` table and how the pass looks up "existing"**

Run: `grep -n "CREATE TABLE.*learnings\|_similar_existing_learning" engine/db.py engine/afm_passes/session_distillation.py`

If the `learnings` table has additional required NOT NULL columns beyond `content`/`created_at`, update the `INSERT INTO learnings` statement in Step 1's test to match (check `engine/test_native_afm_helper_ops.py`'s existing `_seed_candidate`/table-seeding helpers for the correct column list, since this file already seeds similar tables elsewhere).

- [ ] **Step 3: Run test to verify it fails**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_contradiction_signal_chunks_oversized_candidate -v`
Expected: FAIL — `call_count["n"]` is `1` today.

- [ ] **Step 4: Migrate `_native_contradiction_signal`**

Find (around line 418-455):

```python
def _native_contradiction_signal(
    db, drafts: List[Dict[str, Any]], trace_id: str
) -> Optional[Dict[str, Any]]:
    """Advisory-only contradiction check: compares the strongest candidate draft
    this pass against the most similar existing learning (cheap FTS lookup). No
    durable write, no decision change — returns a {contradicts,reason} signal."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return None
    # Strongest candidate = the native session_distill / compile draft if present,
    # else the first deterministic draft. Use its body as the candidate text.
    candidate = None
    for draft in drafts:
        if draft.get("provider") == "native":
            candidate = draft
            break
    if candidate is None and drafts:
        candidate = drafts[0]
    if candidate is None:
        return None
    candidate_text = str(candidate.get("title") or "") + ". " + str(candidate.get("body") or "")
    candidate_text = candidate_text.strip()
    existing = _similar_existing_learning(db, candidate.get("title") or candidate.get("body") or "")
    if not existing:
        return None
    result = default_provider_chain().native_op(
        "contradiction", {"existing": existing, "candidate": candidate_text[:6000]}, timeout=4.0
    )
    if not result.ok:
        _log_native_error_kind("contradiction", trace_id, result)
        return None
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "candidate_page_id": candidate.get("page_id"),
        "contradicts": bool(data.get("contradicts")),
        "reason": str(data.get("reason") or "").strip(),
    }
```

Replace with:

```python
def _build_contradiction_reduce_payload(existing: str) -> Callable[[List[Dict[str, Any]]], Dict[str, Any]]:
    def build(partials: List[Dict[str, Any]]) -> Dict[str, Any]:
        reasons = [str(p.get("reason") or "").strip() for p in partials if p.get("reason")]
        return {"existing": existing, "candidate": "\n".join(r for r in reasons if r)}
    return build


def _native_contradiction_signal(
    db, drafts: List[Dict[str, Any]], trace_id: str
) -> Optional[Dict[str, Any]]:
    """Advisory-only contradiction check: compares the strongest candidate draft
    this pass against the most similar existing learning (cheap FTS lookup). No
    durable write, no decision change — returns a {contradicts,reason} signal."""
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return None
    # Strongest candidate = the native session_distill / compile draft if present,
    # else the first deterministic draft. Use its body as the candidate text.
    candidate = None
    for draft in drafts:
        if draft.get("provider") == "native":
            candidate = draft
            break
    if candidate is None and drafts:
        candidate = drafts[0]
    if candidate is None:
        return None
    candidate_text = str(candidate.get("title") or "") + ". " + str(candidate.get("body") or "")
    candidate_text = candidate_text.strip()
    existing = _similar_existing_learning(db, candidate.get("title") or candidate.get("body") or "")
    if not existing:
        return None
    chain = default_provider_chain()
    chunk_results, was_chunked = call_native_op_chunked(
        chain, "contradiction", {"existing": existing, "candidate": candidate_text},
        text_field="candidate", timeout=4.0,
    )
    if was_chunked:
        result = reduce_via_same_op(
            chain, "contradiction", chunk_results, _build_contradiction_reduce_payload(existing),
            text_field="candidate", timeout=4.0,
        )
    else:
        result = chunk_results[0] if chunk_results else None
    if result is None or not result.ok:
        if result is not None:
            _log_native_error_kind("contradiction", trace_id, result)
        return None
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "candidate_page_id": candidate.get("page_id"),
        "contradicts": bool(data.get("contradicts")),
        "reason": str(data.get("reason") or "").strip(),
    }
```

- [ ] **Step 5: Add `Callable` to the typing import**

At the top of `engine/afm_passes/session_distillation.py`, find:

```python
from typing import Any, Dict, List, Optional
```

Replace with:

```python
from typing import Any, Callable, Dict, List, Optional
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_contradiction_signal_chunks_oversized_candidate -v`
Expected: PASS

- [ ] **Step 7: Run the full test file to check for regressions**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py -v`
Expected: PASS (all tests, including `test_contradiction_signal_emitted_against_existing_learning` and `test_contradiction_skipped_when_no_existing_learning`)

- [ ] **Step 8: Commit**

```bash
git add engine/afm_passes/session_distillation.py engine/test_native_afm_helper_ops.py
git commit -m "afm-chunking: migrate contradiction to chunked calling + AFM reduce"
```

---

## Task 7: Migrate `compile_pass_proposals` in `session_distillation.py`

**Files:**
- Modify: `engine/afm_passes/session_distillation.py:257-289` (`_native_compile_drafts`)
- Test: `engine/test_native_afm_helper_ops.py`

**Interfaces:**
- Consumes: `split_list_by_token_budget`, `estimate_native_payload_tokens`, `AFM_INPUT_BUDGET_TOKENS` (Task 2)
- Produces: no change to `_native_compile_drafts`'s signature (`(pass_input: Dict[str, Any], deterministic_drafts: List[Dict[str, Any]], trace_id: str) -> List[Dict[str, Any]]`). This op is list-shaped like `entity_extract` — the chunkable field is `deterministic_drafts` (a list), not a text string, so it uses `split_list_by_token_budget` directly rather than `call_native_op_chunked` (which is text-field-only).

- [ ] **Step 1: Write the failing test**

Append to `engine/test_native_afm_helper_ops.py`:

```python
def test_compile_pass_proposals_groups_large_draft_lists_by_token_budget(tmp_path, monkeypatch):
    from afm_passes import session_distillation

    db_obj, cfg = _make_db(tmp_path)
    # Seed enough events that the deterministic pass produces many drafts —
    # today's code hard-caps deterministic_drafts[:12] before calling AFM,
    # silently dropping anything beyond the 12th; the migrated code must
    # process the FULL list across token-budgeted groups instead.
    now = time.time()
    with db_obj.cursor() as c:
        for i in range(30):
            c.execute(
                """
                INSERT INTO episodic_events
                (agent_id, event_type, content, task_id, thread_id, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "codex", "session", f"Distinct concept {i}: substantial content here.",
                    f"task-{i}", f"thread-{i}", json.dumps({"source": "unit-test"}), now,
                ),
            )

    call_count = {"n": 0}
    seen_group_sizes = []

    def fake_compile(payload):
        call_count["n"] += 1
        drafts_in = payload.get("deterministic_drafts") or []
        seen_group_sizes.append(len(drafts_in))
        sources = drafts_in[0]["sources"] if drafts_in and drafts_in[0].get("sources") else []
        return _AFMResult(ok=True, data={
            "drafts": [{
                "kind": "concept", "section": "concepts",
                "title": f"Draft from call {call_count['n']}",
                "body": "review-only body",
                "sources": sources,
            }]
        })

    op_map = {"compile_pass_proposals": fake_compile}
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = session_distillation.run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="trace-compile")

    assert result is not None
    # With 30 seeded events (deterministic drafts likely > 12), verify the
    # code no longer silently caps input at 12 — either multiple calls were
    # made, or a single call's group carries more than 12 drafts.
    assert call_count["n"] >= 1
    if call_count["n"] > 1:
        assert sum(seen_group_sizes) > 12
```

- [ ] **Step 2: Run test to verify it fails or passes trivially**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_compile_pass_proposals_groups_large_draft_lists_by_token_budget -v`

If the deterministic pass produces 12 or fewer drafts from 30 events (some events may not each produce a distinct draft — verify via `seen_group_sizes` printed with `-s`), this test may pass trivially before the code change. If so, increase the seeded event count/content size in Step 1 until `sum(seen_group_sizes)` (or the deterministic draft count) provably exceeds 12 before the migration, so the test is meaningful. Confirm this before moving on — a trivially-passing test here would hide a real regression.

- [ ] **Step 3: Migrate `_native_compile_drafts`**

Find (around line 257-289):

```python
def _native_compile_drafts(pass_input: Dict[str, Any], deterministic_drafts: List[Dict[str, Any]], trace_id: str) -> List[Dict[str, Any]]:
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return []
    allowed_sources = {
        str(source)
        for draft in deterministic_drafts
        for source in draft.get("sources", [])
        if str(source).strip()
    }
    if not allowed_sources:
        return []
    payload = {
        "pass_name": "session_distillation",
        "trace_id": trace_id,
        "inputs": pass_input,
        "deterministic_drafts": deterministic_drafts[:12],
    }
    # P2: native helper ops route through the provider chain. The op stays
    # native-only by contract (bridge mode keeps returning no drafts).
    result = default_provider_chain().native_op("compile_pass_proposals", payload, timeout=4.0)
    if not result.ok:
        _log_native_error_kind("compile_pass_proposals", trace_id, result)
        return []
    candidates = result.data.get("drafts")
    if not isinstance(candidates, list):
        return []
    normalized = [
        draft
        for draft in (_normalize_native_draft(candidate, trace_id, allowed_sources) for candidate in candidates)
        if draft is not None
    ]
    return normalized[:5]
```

Replace with:

```python
def _native_compile_drafts(pass_input: Dict[str, Any], deterministic_drafts: List[Dict[str, Any]], trace_id: str) -> List[Dict[str, Any]]:
    mode = resolve_afm_mode()
    if mode not in {"native", "auto"}:
        return []
    allowed_sources = {
        str(source)
        for draft in deterministic_drafts
        for source in draft.get("sources", [])
        if str(source).strip()
    }
    if not allowed_sources:
        return []
    chain = default_provider_chain()
    base_payload = {"pass_name": "session_distillation", "trace_id": trace_id, "inputs": pass_input}
    base_tokens = estimate_native_payload_tokens(base_payload)
    draft_group_budget = max(AFM_INPUT_BUDGET_TOKENS - base_tokens, MIN_CHUNK_TOKENS)
    # List-shaped input (deterministic_drafts): group by token budget instead
    # of the old hard cap of 12 (which silently dropped anything beyond it).
    groups = split_list_by_token_budget(
        deterministic_drafts, draft_group_budget,
        serialize=lambda draft: json.dumps(draft, default=str),
    )

    normalized: List[Dict[str, Any]] = []
    seen_titles: set = set()
    for group in groups:
        payload = dict(base_payload, deterministic_drafts=group)
        # P2: native helper ops route through the provider chain. The op stays
        # native-only by contract (bridge mode keeps returning no drafts).
        result = chain.native_op("compile_pass_proposals", payload, timeout=4.0)
        if not result.ok:
            _log_native_error_kind("compile_pass_proposals", trace_id, result)
            continue
        candidates = result.data.get("drafts") if isinstance(result.data, dict) else None
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            draft = _normalize_native_draft(candidate, trace_id, allowed_sources)
            if draft is None:
                continue
            key = draft["title"].strip().lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            normalized.append(draft)
    return normalized[:5]
```

- [ ] **Step 4: Add imports**

At the top of `engine/afm_passes/session_distillation.py`, find:

```python
from afm_chunking import call_native_op_chunked, reduce_via_same_op
from afm_provider import resolve_afm_mode
from model_provider import default_provider_chain
```

Replace with:

```python
from afm_chunking import (
    AFM_INPUT_BUDGET_TOKENS,
    MIN_CHUNK_TOKENS,
    call_native_op_chunked,
    estimate_native_payload_tokens,
    reduce_via_same_op,
    split_list_by_token_budget,
)
from afm_provider import resolve_afm_mode
from model_provider import default_provider_chain
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_compile_pass_proposals_groups_large_draft_lists_by_token_budget -v`
Expected: PASS

- [ ] **Step 6: Run the full test file to check for regressions**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py -v`
Expected: PASS (all tests, including any existing `compile_pass_proposals` coverage)

- [ ] **Step 7: Run the entire engine test suite to be sure nothing else broke**

Run: `cd engine && python3 -m pytest -q`
Expected: PASS (all tests)

- [ ] **Step 8: Commit**

```bash
git add engine/afm_passes/session_distillation.py engine/test_native_afm_helper_ops.py
git commit -m "afm-chunking: migrate compile_pass_proposals to token-budgeted list grouping"
```

---

## Task 8: Migrate `neighborhood_summary` in `query_expand.py`

**Files:**
- Modify: `engine/query_expand.py:41-76` (`summarize_with_afm`)
- Test: `engine/test_query_expand.py` (verify this exists first; if not, create it)

**Interfaces:**
- Consumes: `call_native_op_chunked`, `reduce_via_same_op` (Task 3)
- Produces: no change to `summarize_with_afm`'s signature (`(prompt: str, timeout: float = 1.5) -> Optional[str]`).

- [ ] **Step 1: Check for an existing test file and existing coverage of `summarize_with_afm`**

Run: `ls engine/test_query_expand.py; grep -n "summarize_with_afm" engine/test_query_expand.py 2>/dev/null`

If the file exists, read it fully before proceeding, and match its existing mocking style for `default_provider_chain`/`native_op`. If it doesn't exist, create it in Step 2 following the pattern below.

- [ ] **Step 2: Write the failing test**

Create or append to `engine/test_query_expand.py`:

```python
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


class _AFMResult:
    def __init__(self, ok, data, status="native_available", error=None):
        self.ok = ok
        self.data = data
        self.status = status
        self.error = error


def test_summarize_with_afm_chunks_oversized_prompt(monkeypatch):
    import query_expand

    call_count = {"n": 0}

    def fake_native(operation, payload, timeout=2.0):
        assert operation == "neighborhood_summary"
        call_count["n"] += 1
        prompt = payload.get("prompt", "")
        if "chunk marker" in prompt:
            return _AFMResult(True, {"summary": f"partial summary {call_count['n']}"})
        return _AFMResult(True, {"summary": "final synthesized summary"})

    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", fake_native)

    long_prompt = "chunk marker filler text. " * 1000  # far over budget
    summary = query_expand.summarize_with_afm(long_prompt, timeout=1.5)

    assert call_count["n"] > 1
    assert summary == "final synthesized summary"


def test_summarize_with_afm_unchanged_for_short_prompt(monkeypatch):
    import query_expand

    call_count = {"n": 0}

    def fake_native(operation, payload, timeout=2.0):
        call_count["n"] += 1
        return _AFMResult(True, {"summary": "short summary"})

    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", fake_native)

    summary = query_expand.summarize_with_afm("a short prompt", timeout=1.5)

    assert call_count["n"] == 1
    assert summary == "short summary"
```

- [ ] **Step 3: Run tests to verify the oversized-prompt test fails**

Run: `cd engine && python3 -m pytest test_query_expand.py -v -k summarize_with_afm`
Expected: `test_summarize_with_afm_chunks_oversized_prompt` FAILS (`call_count["n"] == 1` today); `test_summarize_with_afm_unchanged_for_short_prompt` may already pass.

- [ ] **Step 4: Migrate `summarize_with_afm`**

Find (around line 41-76):

```python
def summarize_with_afm(prompt: str, timeout: float = 1.5) -> Optional[str]:
    """Ask the configured AFM provider for a short summary; return None on downgrade."""
    if not prompt.strip():
        return None
    mode = resolve_afm_mode()
    if mode == "off":
        return None
    if mode in {"native", "auto"}:
        # P2: native helper ops route through the provider chain.
        native = default_provider_chain().native_op(
            "neighborhood_summary",
            {"prompt": prompt[:6000]},
            timeout=timeout,
        )
        if native.ok:
            summary = native.data.get("summary")
            return str(summary).strip() if summary else None
        if mode == "native":
            logger.debug("Native AFM neighborhood summary unavailable: %s", native.error)
            return None
    payload = {
```

Replace with:

```python
def _build_neighborhood_summary_reduce_payload(partials: List[Dict[str, Any]]) -> Dict[str, Any]:
    summaries = [str(p.get("summary") or "").strip() for p in partials if p.get("summary")]
    return {"prompt": "\n".join(s for s in summaries if s)}


def summarize_with_afm(prompt: str, timeout: float = 1.5) -> Optional[str]:
    """Ask the configured AFM provider for a short summary; return None on downgrade."""
    if not prompt.strip():
        return None
    mode = resolve_afm_mode()
    if mode == "off":
        return None
    if mode in {"native", "auto"}:
        # P2: native helper ops route through the provider chain. Oversized
        # prompts are chunked (not truncated) and, if chunked, reduced back
        # into one summary via a second neighborhood_summary call.
        chain = default_provider_chain()
        chunk_results, was_chunked = call_native_op_chunked(
            chain, "neighborhood_summary", {"prompt": prompt}, text_field="prompt", timeout=timeout,
        )
        if was_chunked:
            native = reduce_via_same_op(
                chain, "neighborhood_summary", chunk_results, _build_neighborhood_summary_reduce_payload,
                text_field="prompt", timeout=timeout,
            )
        else:
            native = chunk_results[0] if chunk_results else None
        if native is not None and native.ok:
            summary = native.data.get("summary")
            return str(summary).strip() if summary else None
        if mode == "native":
            logger.debug("Native AFM neighborhood summary unavailable: %s", native.error if native else "no result")
            return None
    payload = {
```

Note: leave the rest of the function (the bridge fallback `payload = {...}` block and everything after it, including its own `prompt[:6000]` char slice) **unchanged** — that block posts to a separate HTTP chat-completions transport with its own independent constraints, not the native AFM 4096-token context window this task addresses (see spec §2 out-of-scope note).

- [ ] **Step 5: Add the import and required typing imports**

At the top of `engine/query_expand.py`, find:

```python
from afm_provider import resolve_afm_mode
from model_provider import ChatRequest, default_provider_chain
```

Replace with:

```python
from afm_chunking import call_native_op_chunked, reduce_via_same_op
from afm_provider import resolve_afm_mode
from model_provider import ChatRequest, default_provider_chain
```

Check the existing `typing` import line in this file (run `grep -n "^from typing" engine/query_expand.py`) and ensure `Any` and `Dict` are included alongside whatever is already imported (e.g. `Iterable, List, Optional`) — add them if missing, matching this file's existing import style.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd engine && python3 -m pytest test_query_expand.py -v`
Expected: PASS (both tests)

- [ ] **Step 7: Run the entire engine test suite to check for regressions**

Run: `cd engine && python3 -m pytest -q`
Expected: PASS (all tests)

- [ ] **Step 8: Commit**

```bash
git add engine/query_expand.py engine/test_query_expand.py
git commit -m "afm-chunking: migrate neighborhood_summary to chunked calling + AFM reduce"
```

---

## Task 9: Migrate `triage` in `consolidation.py`

**Files:**
- Modify: `engine/afm_passes/consolidation.py:159-220` (`_triage_advisory`)
- Test: `engine/test_native_afm_helper_ops.py`

**Interfaces:**
- Consumes: `call_native_op_chunked`, `reduce_via_same_op` (Task 3) — imported lazily inside the function, matching this file's existing lazy-import convention.
- Produces: no change to `_triage_advisory`'s signature (`(candidates: List[Dict[str, Any]], promote_candidate_ids: List[int], trace_id: str) -> Optional[Dict[str, Any]]`).

- [ ] **Step 1: Write the failing test**

Append to `engine/test_native_afm_helper_ops.py` (this file already has `_ensure_candidate_tables`/`_seed_candidate` helpers for the existing triage tests — reuse them):

```python
def test_triage_advisory_chunks_oversized_candidate(tmp_path, monkeypatch):
    from afm_passes import consolidation

    db_obj, cfg = _make_db(tmp_path)
    _ensure_candidate_tables(db_obj)
    long_content = "A plausible durable fact about the build pipeline cache. " * 200
    cid = _seed_candidate(db_obj, long_content)

    call_count = {"n": 0}

    def fake_triage(payload):
        call_count["n"] += 1
        candidate = payload.get("candidate", "")
        if "build pipeline cache" in candidate and len(candidate) > 500:
            return _AFMResult(ok=True, data={"decision": "accept", "reason": f"partial {call_count['n']}", "tool_used": True})
        return _AFMResult(ok=True, data={"decision": "accept", "reason": "synthesized reason", "tool_used": True})

    op_map = {"triage": fake_triage}
    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("afm_provider.invoke_native_afm", _route(op_map))

    result = consolidation.run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t")

    assert call_count["n"] > 1
    assert result["triage_advisory"] is not None
    assert result["triage_advisory"]["candidate_id"] == cid
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_triage_advisory_chunks_oversized_candidate -v`
Expected: FAIL — `call_count["n"]` is `1` today (single truncated call).

- [ ] **Step 3: Migrate `_triage_advisory`**

In `engine/afm_passes/consolidation.py`, find (around line 159-220):

```python
def _triage_advisory(candidates: List[Dict[str, Any]],
                     promote_candidate_ids: List[int],
                     trace_id: str) -> Optional[Dict[str, Any]]:
    """ADVISORY-ONLY native triage signal for one representative candidate.

    Strictly informational: it is attached to the pass output and is NEVER read
    by the deterministic accept/reject/redact gate above (which stays
    deterministic by design — "never reject plausible short facts"). Native-only
    by contract; bridge/off modes no-op.
    """
    # Lazy imports keep the deterministic pass dependency-light and avoid paying
    # provider-chain import cost when AFM is off.
    try:
        from afm_provider import resolve_afm_mode
        from model_provider import default_provider_chain
    except Exception:  # noqa: BLE001 - advisory must never break consolidation
        return None
    if resolve_afm_mode() not in {"native", "auto"}:
        return None
    # Prefer a candidate the deterministic gate already chose to promote, so the
    # advisory is comparable to a real decision; else fall back to the first one.
    chosen = None
    if promote_candidate_ids:
        promote_set = set(promote_candidate_ids)
        chosen = next((c for c in candidates if c.get("candidate_id") in promote_set), None)
    if chosen is None and candidates:
        chosen = candidates[0]
    if chosen is None:
        return None
    promote_set = set(promote_candidate_ids)
    if promote_candidate_ids and chosen.get("candidate_id") not in promote_set:
        return None
    privacy = str(chosen.get("privacy_level") or "safe").strip().lower()
    if privacy not in {"safe", ""}:
        return None
    if int(chosen.get("instruction_like") or 0) == 1:
        return None
    content = (chosen.get("content") or "").strip()
    if not content:
        return None
    try:
        result = default_provider_chain().native_op(
            "triage", {"candidate": content[:6000]}, timeout=4.0
        )
    except Exception:  # noqa: BLE001 - advisory must never break consolidation
        return None
    if not result.ok:
        data = result.data if isinstance(result.data, dict) else {}
        error_kind = str(data.get("error_kind") or "").strip() or "unknown"
        logger.info(
            "afm native op triage unavailable (error_kind=%s): status=%s error=%s trace=%s",
            error_kind, result.status, result.error, trace_id,
        )
        return None
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "candidate_id": chosen.get("candidate_id"),
        "decision": str(data.get("decision") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
        "tool_used": bool(data.get("tool_used")),
        "note": "advisory only; deterministic gate decision is authoritative",
    }
```

Replace with:

```python
def _build_triage_reduce_payload(partials: List[Dict[str, Any]]) -> Dict[str, Any]:
    lines = [f"{p.get('decision', '')}: {p.get('reason', '')}" for p in partials if p.get("decision")]
    return {"candidate": "\n".join(lines)}


def _triage_advisory(candidates: List[Dict[str, Any]],
                     promote_candidate_ids: List[int],
                     trace_id: str) -> Optional[Dict[str, Any]]:
    """ADVISORY-ONLY native triage signal for one representative candidate.

    Strictly informational: it is attached to the pass output and is NEVER read
    by the deterministic accept/reject/redact gate above (which stays
    deterministic by design — "never reject plausible short facts"). Native-only
    by contract; bridge/off modes no-op.
    """
    # Lazy imports keep the deterministic pass dependency-light and avoid paying
    # provider-chain import cost when AFM is off.
    try:
        from afm_chunking import call_native_op_chunked, reduce_via_same_op
        from afm_provider import resolve_afm_mode
        from model_provider import default_provider_chain
    except Exception:  # noqa: BLE001 - advisory must never break consolidation
        return None
    if resolve_afm_mode() not in {"native", "auto"}:
        return None
    # Prefer a candidate the deterministic gate already chose to promote, so the
    # advisory is comparable to a real decision; else fall back to the first one.
    chosen = None
    if promote_candidate_ids:
        promote_set = set(promote_candidate_ids)
        chosen = next((c for c in candidates if c.get("candidate_id") in promote_set), None)
    if chosen is None and candidates:
        chosen = candidates[0]
    if chosen is None:
        return None
    promote_set = set(promote_candidate_ids)
    if promote_candidate_ids and chosen.get("candidate_id") not in promote_set:
        return None
    privacy = str(chosen.get("privacy_level") or "safe").strip().lower()
    if privacy not in {"safe", ""}:
        return None
    if int(chosen.get("instruction_like") or 0) == 1:
        return None
    content = (chosen.get("content") or "").strip()
    if not content:
        return None
    try:
        chain = default_provider_chain()
        chunk_results, was_chunked = call_native_op_chunked(
            chain, "triage", {"candidate": content}, text_field="candidate", timeout=4.0,
        )
        if was_chunked:
            result = reduce_via_same_op(
                chain, "triage", chunk_results, _build_triage_reduce_payload,
                text_field="candidate", timeout=4.0,
            )
        else:
            result = chunk_results[0] if chunk_results else None
    except Exception:  # noqa: BLE001 - advisory must never break consolidation
        return None
    if result is None or not result.ok:
        if result is not None:
            data = result.data if isinstance(result.data, dict) else {}
            error_kind = str(data.get("error_kind") or "").strip() or "unknown"
            logger.info(
                "afm native op triage unavailable (error_kind=%s): status=%s error=%s trace=%s",
                error_kind, result.status, result.error, trace_id,
            )
        return None
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "candidate_id": chosen.get("candidate_id"),
        "decision": str(data.get("decision") or "").strip(),
        "reason": str(data.get("reason") or "").strip(),
        "tool_used": bool(data.get("tool_used")),
        "note": "advisory only; deterministic gate decision is authoritative",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py::test_triage_advisory_chunks_oversized_candidate -v`
Expected: PASS

- [ ] **Step 5: Run the full test file and entire engine suite to check for regressions**

Run: `cd engine && python3 -m pytest test_native_afm_helper_ops.py -v && python3 -m pytest -q`
Expected: PASS (all tests, including `test_consolidation_triage_advisory_does_not_change_decisions` and `test_consolidation_triage_advisory_none_when_afm_off`)

- [ ] **Step 6: Commit**

```bash
git add engine/afm_passes/consolidation.py engine/test_native_afm_helper_ops.py
git commit -m "afm-chunking: migrate triage advisory to chunked calling + AFM reduce"
```

---

## Task 10: TypeScript core primitives + chunked-call wrapper + tree-reduce helper

**Files:**
- Create: `plugins/minni/src/afm-chunking.ts`
- Test: `plugins/minni/tests/afm-chunking.test.mjs`

**Interfaces:**
- Produces (all exported from `plugins/minni/src/afm-chunking.ts`, compiled to `plugins/minni/dist/afm-chunking.js`):
  - `AFM_INPUT_BUDGET_TOKENS = 3200`
  - `MIN_CHUNK_TOKENS = 200`
  - `estimateNativePayloadTokens(payload: Record<string, unknown>): number`
  - `splitListByTokenBudget<T>(items: T[], budgetTokens: number, serialize?: (item: T) => string): T[][]`
  - `interface NativeOpResult { ok: boolean; data?: Record<string, unknown>; error?: string }`
  - `callNativeOpChunked(callOp: (payload: Record<string, unknown>) => Promise<NativeOpResult>, payload: Record<string, unknown>, listField: string, budgetTokens?: number): Promise<{ results: NativeOpResult[]; wasChunked: boolean }>`
  - `reduceViaSameOp(callOp: (payload: Record<string, unknown>) => Promise<NativeOpResult>, results: NativeOpResult[], buildReducePayload: (partials: Record<string, unknown>[]) => Record<string, unknown>, listField: string, budgetTokens?: number): Promise<NativeOpResult | undefined>`

- [ ] **Step 1: Confirm the TS build/test setup before writing code**

Run: `cd plugins/minni && npm run build 2>&1 | tail -20`
Expected: build succeeds (0 errors) — confirms the toolchain works before adding new files. If it fails for unrelated pre-existing reasons, stop and report rather than proceeding on a broken baseline.

- [ ] **Step 2: Write the failing tests**

Create `plugins/minni/tests/afm-chunking.test.mjs`:

```javascript
// Unit tests for src/afm-chunking.ts — the TypeScript mirror of
// engine/afm_chunking.py. Same shared-primitive responsibility: is this
// payload too big for the ~4096-token AFM context window, and if so, how do
// we split it (list-shaped only here — relevantSources in task.ts is
// already a list, not raw text, so no text splitter is needed on this side).

import assert from "node:assert/strict";
import test from "node:test";

import {
  AFM_INPUT_BUDGET_TOKENS,
  MIN_CHUNK_TOKENS,
  callNativeOpChunked,
  estimateNativePayloadTokens,
  reduceViaSameOp,
  splitListByTokenBudget,
} from "../dist/afm-chunking.js";

test("estimateNativePayloadTokens grows with payload size", () => {
  const small = estimateNativePayloadTokens({ text: "hello" });
  const large = estimateNativePayloadTokens({ text: "hello ".repeat(500) });
  assert.ok(small > 0);
  assert.ok(large > small);
});

test("splitListByTokenBudget returns one group when small", () => {
  const items = [{ title: "a" }, { title: "b" }];
  const groups = splitListByTokenBudget(items, 1000);
  assert.deepEqual(groups, [items]);
});

test("splitListByTokenBudget splits when over budget, preserving every item once", () => {
  const items = Array.from({ length: 20 }, (_, i) => ({ title: `item-${i}`, body: "x".repeat(200) }));
  const groups = splitListByTokenBudget(items, 100);
  assert.ok(groups.length > 1);
  const flattened = groups.flat();
  assert.deepEqual(flattened, items);
});

test("splitListByTokenBudget never returns an empty group list", () => {
  const groups = splitListByTokenBudget([], 100);
  assert.deepEqual(groups, [[]]);
});

test("callNativeOpChunked passes through unchanged when under budget", async () => {
  const calls = [];
  const callOp = async (payload) => {
    calls.push(payload);
    return { ok: true, data: { brief: "ok" } };
  };
  const { results, wasChunked } = await callNativeOpChunked(
    callOp, { task: "x", relevantSources: [{ a: 1 }] }, "relevantSources",
  );
  assert.equal(wasChunked, false);
  assert.equal(results.length, 1);
  assert.equal(calls.length, 1);
});

test("callNativeOpChunked splits relevantSources when over budget", async () => {
  const bigSources = Array.from({ length: 50 }, (_, i) => ({
    relativePath: `note-${i}.md`,
    evidenceEnvelope: "x".repeat(300),
  }));
  const calls = [];
  const callOp = async (payload) => {
    calls.push(payload);
    return { ok: true, data: { brief: "partial" } };
  };
  const { results, wasChunked } = await callNativeOpChunked(
    callOp, { task: "x", relevantSources: bigSources }, "relevantSources",
  );
  assert.equal(wasChunked, true);
  assert.ok(results.length > 1);
  assert.equal(calls.length, results.length);
  for (const call of calls) {
    assert.ok(call.relevantSources.length < bigSources.length);
  }
});

test("callNativeOpChunked reactive fallback chunks after a surprise context_overflow", async () => {
  let callCount = 0;
  const bigSources = Array.from({ length: 50 }, (_, i) => ({ relativePath: `note-${i}.md` }));
  const callOp = async () => {
    callCount += 1;
    if (callCount === 1) return { ok: false, data: { error_kind: "context_overflow" } };
    return { ok: true, data: { brief: "recovered" } };
  };
  const { results, wasChunked } = await callNativeOpChunked(
    callOp, { task: "x", relevantSources: bigSources }, "relevantSources", 999999,
  );
  assert.equal(wasChunked, true);
  assert.ok(results.some((r) => r.ok));
});

test("reduceViaSameOp returns undefined when nothing succeeded", async () => {
  const callOp = async () => ({ ok: false });
  const reduced = await reduceViaSameOp(
    callOp, [{ ok: false }, { ok: false }], () => ({ relevantSources: [] }), "relevantSources",
  );
  assert.equal(reduced, undefined);
});

test("reduceViaSameOp returns the sole result unreduced", async () => {
  let called = false;
  const callOp = async () => {
    called = true;
    return { ok: true, data: { brief: "should not be called" } };
  };
  const only = { ok: true, data: { brief: "the one answer" } };
  const reduced = await reduceViaSameOp(callOp, [only], () => ({ relevantSources: [] }), "relevantSources");
  assert.equal(reduced, only);
  assert.equal(called, false);
});

test("reduceViaSameOp synthesizes a final result from partials", async () => {
  const callOp = async (payload) => {
    assert.ok(payload.partialBriefs.length === 2);
    return { ok: true, data: { brief: "synthesized" } };
  };
  const chunkResults = [
    { ok: true, data: { brief: "partial 1" } },
    { ok: true, data: { brief: "partial 2" } },
  ];
  const reduced = await reduceViaSameOp(
    callOp, chunkResults,
    (partials) => ({ partialBriefs: partials.map((p) => p.brief) }),
    "relevantSources",
  );
  assert.equal(reduced.data.brief, "synthesized");
});

test("AFM_INPUT_BUDGET_TOKENS and MIN_CHUNK_TOKENS are exported with expected defaults", () => {
  assert.equal(AFM_INPUT_BUDGET_TOKENS, 3200);
  assert.equal(MIN_CHUNK_TOKENS, 200);
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd plugins/minni && npm test 2>&1 | grep -A3 "afm-chunking"`
Expected: FAIL — `dist/afm-chunking.js` doesn't exist yet (module not found).

- [ ] **Step 4: Write the implementation**

Create `plugins/minni/src/afm-chunking.ts`:

```typescript
// AFM Chunked Calling — TypeScript mirror of engine/afm_chunking.py.
//
// Two-language mirrored boundary (same precedent as providers.ts mirroring
// model_provider.py): this module gives task.ts's prepare_task/prepare_outcome
// call site (the exact op that hit the reported 4096-token context-overflow
// incident) the same recovery the Python side gets, without inventing any
// new Swift-side operation.
//
// Only list-splitting is needed here (not a text splitter): the oversized
// field at this call site is relevantSources, already an array, not raw
// prose. AFM calls are free (on-device) — nothing here economizes on call
// count; MIN_CHUNK_TOKENS/group count is the only real floor.

export const AFM_INPUT_BUDGET_TOKENS = 3200;
export const MIN_CHUNK_TOKENS = 200;

/**
 * Estimate tokens for a payload via a words/0.75 heuristic — the same
 * fallback engine/tokens.py already uses when tiktoken is unavailable. No
 * tokenizer package is added as a dependency here since exactness isn't
 * required (callNativeOpChunked's reactive fallback catches underestimates).
 */
export function estimateNativePayloadTokens(payload: Record<string, unknown>): number {
  const serialized = JSON.stringify(payload) ?? "";
  const words = serialized.split(/\s+/).filter(Boolean).length;
  return Math.ceil(words / 0.75);
}

function estimateTextTokens(text: string): number {
  const words = text.split(/\s+/).filter(Boolean).length;
  return Math.ceil(words / 0.75);
}

/**
 * Greedy bin-packing of items into groups whose serialized total stays
 * within budgetTokens. A single item that alone exceeds budgetTokens still
 * gets its own group.
 */
export function splitListByTokenBudget<T>(
  items: T[],
  budgetTokens: number,
  serialize: (item: T) => string = (item) => JSON.stringify(item),
): T[][] {
  const groups: T[][] = [];
  let current: T[] = [];
  let currentTokens = 0;
  for (const item of items) {
    const itemTokens = estimateTextTokens(serialize(item));
    if (current.length > 0 && currentTokens + itemTokens > budgetTokens) {
      groups.push(current);
      current = [];
      currentTokens = 0;
    }
    current.push(item);
    currentTokens += itemTokens;
  }
  if (current.length > 0) groups.push(current);
  return groups.length > 0 ? groups : [[]];
}

export interface NativeOpResult {
  ok: boolean;
  data?: Record<string, unknown>;
  error?: string;
}

function errorKindOf(result: NativeOpResult): string {
  const kind = result.data?.["error_kind"];
  return typeof kind === "string" ? kind.trim().toLowerCase() : "";
}

/**
 * Call callOp(payload) once if the payload is under budget (byte-identical
 * to today's un-chunked behavior). If over budget (proactively, by
 * estimateNativePayloadTokens, or reactively after a surprise
 * context_overflow), splits payload[listField] via splitListByTokenBudget
 * and calls callOp once per group, sequentially.
 *
 * Does NOT reduce results — see reduceViaSameOp for that.
 */
export async function callNativeOpChunked(
  callOp: (payload: Record<string, unknown>) => Promise<NativeOpResult>,
  payload: Record<string, unknown>,
  listField: string,
  budgetTokens: number = AFM_INPUT_BUDGET_TOKENS,
): Promise<{ results: NativeOpResult[]; wasChunked: boolean }> {
  const estimated = estimateNativePayloadTokens(payload);
  if (estimated <= budgetTokens) {
    const result = await callOp(payload);
    if (result.ok || errorKindOf(result) !== "context_overflow") {
      return { results: [result], wasChunked: false };
    }
    // Reactive: estimate was wrong. Fall through to chunking below.
  }

  const items = Array.isArray(payload[listField]) ? (payload[listField] as unknown[]) : [];
  if (items.length <= 1) {
    // Nothing to split further — make the call once more as-is and let the
    // caller see the failure.
    return { results: [await callOp(payload)], wasChunked: false };
  }

  const { [listField]: _omitted, ...basePayload } = payload;
  const baseTokens = estimateNativePayloadTokens(basePayload);
  const groupBudget = Math.max(budgetTokens - baseTokens, MIN_CHUNK_TOKENS);
  const groups = splitListByTokenBudget(items, groupBudget);

  const results: NativeOpResult[] = [];
  for (const group of groups) {
    results.push(await callOp({ ...payload, [listField]: group }));
  }
  return { results, wasChunked: true };
}

/**
 * Reduce N successful per-chunk results into one, by calling callOp again
 * with a payload built from the chunks' .data (buildReducePayload decides
 * the shape). If the reduce payload is itself over budget, recurses (tree
 * reduction) — AFM calls are free, so there is no single-pass ceiling.
 *
 * Returns undefined if there are zero successful results to reduce. Returns
 * the sole result unreduced if there is exactly one success.
 */
export async function reduceViaSameOp(
  callOp: (payload: Record<string, unknown>) => Promise<NativeOpResult>,
  results: NativeOpResult[],
  buildReducePayload: (partials: Record<string, unknown>[]) => Record<string, unknown>,
  listField: string,
  budgetTokens: number = AFM_INPUT_BUDGET_TOKENS,
): Promise<NativeOpResult | undefined> {
  const successes = results.filter((r) => r.ok && r.data).map((r) => r.data as Record<string, unknown>);
  if (successes.length === 0) return undefined;
  if (successes.length === 1) return results.find((r) => r.ok);

  const reducePayload = buildReducePayload(successes);
  const { results: reduced } = await callNativeOpChunked(callOp, reducePayload, listField, budgetTokens);
  const reducedSuccesses = reduced.filter((r) => r.ok);
  if (reducedSuccesses.length === 1) return reducedSuccesses[0];
  if (reducedSuccesses.length > 1) {
    return reduceViaSameOp(callOp, reducedSuccesses, buildReducePayload, listField, budgetTokens);
  }
  // Final safety net: every reduce attempt failed — fall back to the first
  // successful chunk rather than nothing.
  return results.find((r) => r.ok);
}
```

- [ ] **Step 5: Build and run tests to verify they pass**

Run: `cd plugins/minni && npm test 2>&1 | grep -A3 "afm-chunking"`
Expected: PASS (all 11 tests)

- [ ] **Step 6: Commit**

```bash
git add plugins/minni/src/afm-chunking.ts plugins/minni/tests/afm-chunking.test.mjs
git commit -m "afm-chunking: add TypeScript mirror (list chunking + tree-reduce)"
```

---

## Task 11: Migrate `callAfmPrepareTask` in `task.ts`

**Files:**
- Modify: `plugins/minni/src/task.ts:756-791` (`callAfmPrepareTask`)
- Test: `plugins/minni/tests/task.test.mjs`

**Interfaces:**
- Consumes: `callNativeOpChunked`, `reduceViaSameOp`, `NativeOpResult` (Task 10, `plugins/minni/src/afm-chunking.ts`)
- Produces: no change to `callAfmPrepareTask`'s exported signature (`(url: string, payload: Record<string, unknown>) => Promise<JsonResult<Partial<PreparedTaskPacket>>>`).

- [ ] **Step 1: Read the existing test file's mocking pattern for `callAfmPrepareTask` / `defaultProviderChain`**

Run: `grep -n "callAfmPrepareTask\|defaultProviderChain\|prepareTask(" plugins/minni/tests/task.test.mjs | head -20`

Read the surrounding context of the first few matches in `plugins/minni/tests/task.test.mjs` before writing Step 2's test, so the new test's mocking approach (monkeypatching `defaultProviderChain` or injecting a fake `chat`) matches this file's existing convention exactly.

- [ ] **Step 2: Write the failing test**

Append to `plugins/minni/tests/task.test.mjs`, following whatever mocking pattern Step 1 revealed (the sketch below uses a `deps`-injection style consistent with `prepareTask(input, deps)`'s existing `PrepareTaskDeps` — adjust to match the file's actual pattern if it differs):

```javascript
test("callAfmPrepareTask chunks oversized relevantSources instead of sending them all in one native call", async () => {
  const { callAfmPrepareTask } = await import("../dist/task.js");
  const { ProviderChain } = await import("../dist/providers.js");

  const nativeCalls = [];
  // Fake native helper: records every payload's relevantSources length: the
  // first "map" calls see a slice, the final "reduce" call sees none
  // (partialBriefs instead).
  const fakeChat = async (request) => {
    nativeCalls.push(request.nativePayload);
    const sources = Array.isArray(request.nativePayload?.relevantSources)
      ? request.nativePayload.relevantSources
      : [];
    if (sources.length > 0) {
      return { ok: true, data: { brief: `partial brief ${nativeCalls.length}`, recommendedNextActions: [], risks: [] } };
    }
    return { ok: true, data: { brief: "synthesized final brief", recommendedNextActions: [], risks: [] } };
  };

  // A plain object literal implementing ModelProvider directly — NOT a
  // spread of a class instance, which would drop AfmProvider's prototype
  // methods (supports/chat/health) and make ProviderChain.chat() throw when
  // it calls provider.supports(operation).
  const fakeProvider = {
    name: "afm",
    tier: "local",
    supports: (_operation) => true,
    chat: fakeChat,
  };
  const chain = new ProviderChain([fakeProvider]);
  const bigSources = Array.from({ length: 60 }, (_, i) => ({
    relativePath: `note-${i}.md`,
    wikilink: `[[note-${i}]]`,
    evidenceEnvelope: "context text ".repeat(50),
  }));

  const result = await callAfmPrepareTask("http://127.0.0.1:11437/v1/chat/completions", {
    task: "a task",
    budgetTokens: 4096,
    profile: "default",
    provider: { provider: "native", mode: "native" },
    intent: "intent",
    constraints: [],
    currentState: [],
    relevantSources: bigSources,
    daemonLead: "",
    model: "afm-local",
  }, chain);

  assert.equal(result.ok, true);
  assert.ok(nativeCalls.length > 1, `expected multiple native calls, got ${nativeCalls.length}`);
  assert.equal(result.data.brief, "synthesized final brief");
});
```

- [ ] **Step 3: Adjust the test to match how `callAfmPrepareTask` actually resolves its provider chain**

`callAfmPrepareTask` today calls `defaultProviderChain().chat({...})` directly rather than accepting an injected chain — check this with:

Run: `grep -n "defaultProviderChain\(\)" plugins/minni/src/task.ts`

If `callAfmPrepareTask` has no way to inject a fake chain, Step 4 below adds an optional last parameter (`chain: ProviderChain = defaultProviderChain()`) so this test (and future tests) can inject a fake without needing to mock module-level state — a minimal, backward-compatible signature change (existing callers that pass only `(url, payload)` are unaffected since the new parameter has a default).

- [ ] **Step 4: Run test to verify it fails**

Run: `cd plugins/minni && npm test 2>&1 | grep -A5 "callAfmPrepareTask chunks"`
Expected: FAIL (today's code makes exactly one native call with the full 60-item `relevantSources`, so `nativeCalls.length > 1` fails — or a TypeScript compile error if the injected-chain parameter doesn't exist yet, which Step 5 adds).

- [ ] **Step 5: Migrate `callAfmPrepareTask`**

In `plugins/minni/src/task.ts`, find (around line 756-791):

```typescript
export async function callAfmPrepareTask(
  url: string,
  payload: Record<string, unknown>,
): Promise<JsonResult<Partial<PreparedTaskPacket>>> {
  const parsedUrl = new URL(url);
  const isChatCompletions = parsedUrl.pathname.endsWith("/chat/completions");
  const provider = payload.provider && typeof payload.provider === "object"
    ? (payload.provider as { provider?: AfmProvider; mode?: AfmProviderMode; requestedMode?: AfmProviderMode })
    : undefined;
  const actualProvider = provider?.provider ?? (provider?.mode === "bridge" || provider?.mode === "native" || provider?.mode === "off" ? provider.mode : undefined);
  const requestedProvider = provider?.mode ?? provider?.requestedMode;
  const transportMode: AfmProviderMode =
    actualProvider === "off" || requestedProvider === "off"
      ? "off"
      : actualProvider === "native"
        ? "native"
        : "bridge";
  const purpose = payload.purpose === "outcome" ? "prepare_outcome" : "prepare_task";
  const wirePayload = transportMode === "native"
    ? payload
    : isChatCompletions
      ? buildAfmChatPayload(payload)
      : payload;
  // P2: route through the provider chain (AFM-only chain is byte-identical to
  // the old direct callAfmJson path — enforced by the P0 golden contracts).
  const result = await defaultProviderChain().chat({
    payload: wirePayload,
    operation: "prepare",
    url,
    mode: transportMode,
    nativeOperation: purpose,
    nativePayload: payload,
  });
  return result.ok
    ? { ok: true, data: normalizeAfmResponse(result.data) }
    : { ok: false, data: normalizeAfmResponse(result.data), error: result.error };
}
```

Replace with:

```typescript
function buildPrepareReducePayload(purpose: string): (partials: Record<string, unknown>[]) => Record<string, unknown> {
  return (partials: Record<string, unknown>[]): Record<string, unknown> => {
    if (purpose === "prepare_outcome") {
      return { purpose, partialOutcomeDrafts: partials };
    }
    return {
      purpose,
      partialBriefs: partials.map((p) => ({
        brief: p.brief,
        recommendedNextActions: p.recommendedNextActions,
        risks: p.risks,
      })),
    };
  };
}

export async function callAfmPrepareTask(
  url: string,
  payload: Record<string, unknown>,
  chain: ProviderChain = defaultProviderChain(),
): Promise<JsonResult<Partial<PreparedTaskPacket>>> {
  const parsedUrl = new URL(url);
  const isChatCompletions = parsedUrl.pathname.endsWith("/chat/completions");
  const provider = payload.provider && typeof payload.provider === "object"
    ? (payload.provider as { provider?: AfmProvider; mode?: AfmProviderMode; requestedMode?: AfmProviderMode })
    : undefined;
  const actualProvider = provider?.provider ?? (provider?.mode === "bridge" || provider?.mode === "native" || provider?.mode === "off" ? provider.mode : undefined);
  const requestedProvider = provider?.mode ?? provider?.requestedMode;
  const transportMode: AfmProviderMode =
    actualProvider === "off" || requestedProvider === "off"
      ? "off"
      : actualProvider === "native"
        ? "native"
        : "bridge";
  const purpose = payload.purpose === "outcome" ? "prepare_outcome" : "prepare_task";

  const callOp = async (opPayload: Record<string, unknown>): Promise<NativeOpResult> => {
    const opWirePayload = transportMode === "native"
      ? opPayload
      : isChatCompletions
        ? buildAfmChatPayload(opPayload)
        : opPayload;
    // P2: route through the provider chain (AFM-only chain is byte-identical to
    // the old direct callAfmJson path — enforced by the P0 golden contracts).
    const result = await chain.chat({
      payload: opWirePayload,
      operation: "prepare",
      url,
      mode: transportMode,
      nativeOperation: purpose,
      nativePayload: opPayload,
    });
    return { ok: result.ok, data: result.data as Record<string, unknown> | undefined, error: result.error };
  };

  if (transportMode !== "native") {
    // Chunking only applies to the native path (the one with no size limit
    // today, per the reported incident) — the bridge/chat-completions path
    // already slices relevantSources via buildAfmChatPayload's
    // sourceLines.slice(0, 1200), a separate, already-bounded transport.
    const result = await callOp(payload);
    return result.ok
      ? { ok: true, data: normalizeAfmResponse(result.data) }
      : { ok: false, data: normalizeAfmResponse(result.data), error: result.error };
  }

  const { results, wasChunked } = await callNativeOpChunked(callOp, payload, "relevantSources");
  let finalResult: NativeOpResult | undefined;
  if (wasChunked) {
    finalResult = await reduceViaSameOp(callOp, results, buildPrepareReducePayload(purpose), "relevantSources");
  } else {
    finalResult = results[0];
  }
  if (!finalResult) {
    return { ok: false, data: normalizeAfmResponse(undefined), error: "AFM prepare returned no data." };
  }
  return finalResult.ok
    ? { ok: true, data: normalizeAfmResponse(finalResult.data) }
    : { ok: false, data: normalizeAfmResponse(finalResult.data), error: finalResult.error };
}
```

- [ ] **Step 6: Add the imports**

At the top of `plugins/minni/src/task.ts`, find (line 11):

```typescript
import { defaultProviderChain } from "./providers.js";
```

Replace with:

```typescript
import { defaultProviderChain, type ProviderChain } from "./providers.js";
import { callNativeOpChunked, reduceViaSameOp, type NativeOpResult } from "./afm-chunking.js";
```

- [ ] **Step 7: Build and run the new test to verify it passes**

Run: `cd plugins/minni && npm test 2>&1 | grep -A5 "callAfmPrepareTask chunks"`
Expected: PASS

- [ ] **Step 8: Run the entire plugin test suite to check for regressions**

Run: `cd plugins/minni && npm test`
Expected: PASS (all tests, including everything in `task.test.mjs`, `providers.test.mjs`, and the P0 golden contract tests in `afm-contract-golden.test.mjs` — this last one is the strictest check: it enforces byte-identical wire behavior for the default un-chunked path, so any regression there means the "byte-identical when under budget" requirement broke)

- [ ] **Step 9: Commit**

```bash
git add plugins/minni/src/task.ts plugins/minni/tests/task.test.mjs
git commit -m "afm-chunking: migrate callAfmPrepareTask to chunked relevantSources + AFM reduce"
```

---

## Task 12: Full-suite regression pass and final verification

**Files:** none (verification only)

**Interfaces:** none

- [ ] **Step 1: Run the complete Python engine test suite**

Run: `cd engine && python3 -m pytest -q`
Expected: PASS, 0 failures.

- [ ] **Step 2: Run the complete TypeScript plugin test suite**

Run: `cd plugins/minni && npm test`
Expected: PASS, 0 failures.

- [ ] **Step 3: Run the Python lint gate the pre-commit hook enforces**

Run: `cd engine && ruff check .` (or whatever exact ruff invocation `.githooks/pre-commit` uses — check with `grep -n "ruff" ../.githooks/pre-commit` first and match it)
Expected: no errors on the new/modified files (`afm_chunking.py`, `test_afm_chunking.py`, `session_distillation.py`, `query_expand.py`, `consolidation.py`, `test_native_afm_helper_ops.py`, `test_query_expand.py`, `config.py`, `test_config.py`).

- [ ] **Step 4: Run the TypeScript lint/typecheck gate**

Run: `cd plugins/minni && npm run build` (this is the typecheck; also confirm any separate lint script with `grep -n "\"lint\"" package.json` and run it if present)
Expected: 0 type errors, 0 lint errors on `afm-chunking.ts` and the modified `task.ts`.

- [ ] **Step 5: Grep for any remaining `[:6000]` truncation this plan was supposed to remove**

Run: `grep -rn "\[:6000\]" engine/ --include="*.py"`
Expected: zero remaining matches in the four files this plan migrated (`session_distillation.py`, `query_expand.py`'s native path, `consolidation.py`). If `query_expand.py`'s bridge-fallback `prompt[:6000]` still shows up, that is expected and correct — it's explicitly out of scope (Task 8, Step 4 note: separate HTTP transport, not the native AFM context window).

- [ ] **Step 6: Commit any final cleanup, if Steps 3-5 required fixes**

```bash
git add -A
git commit -m "afm-chunking: final lint/regression fixups"
```

(Skip this commit if Steps 1-5 all passed clean with no changes needed.)

---

## Self-Review Notes (for the plan author, not a task)

- **Spec coverage:** §3.1 (Python core module) → Tasks 1-3. §3.2 (reduction pattern table, all 6 Python ops) → Tasks 4, 6, 8, 9 (AFM-reduce ops) + Tasks 5, 7 (deterministic-merge list ops). §3.3 (call sites table) → Tasks 4-9, 11. §3.4 (TS mirror) → Tasks 10-11. §3.5 (config) → Task 1. §3.6 (error handling) → embedded in Task 3's implementation + every migration task's regression check. §3.7 (testing) → every task's own test steps + Task 12's full-suite pass.
- **Placeholder scan:** no TBD/TODO markers; every step has complete, runnable code or an exact grep/test command with expected output.
- **Type consistency:** `call_native_op_chunked(chain, op_name, payload, text_field, ...)` and `reduce_via_same_op(chain, op_name, chunk_results, build_reduce_payload, text_field, ...)` signatures are identical across Tasks 3-9 (Python) and `callNativeOpChunked`/`reduceViaSameOp` identical across Tasks 10-11 (TS). `NativeOpResult` (TS) matches the Python `ProviderResult`-shaped duck type (`ok`/`data`/`error`) used throughout.

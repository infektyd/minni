# AFM Chunked Distillation — Design Spec

- **Date:** 2026-07-01
- **Author:** claude-code (with operator)
- **Status:** Draft for review
- **Branch:** `feature/afm-chunked-distillation`
- **Scope of this doc:** the complete architecture for removing the hardcoded ~4096-token AFM context cap from every native-AFM call site in Minni, replacing silent truncation / empty-fallback with real chunking + reduction.

---

## 1. Problem

Minni's Swift native helper (`engine/native_afm_helper.swift`) wraps Apple Foundation Models, which has a hard ~4096-token context window per `LanguageModelSession.respond()` call. Today, every Python call site that can produce large input works around this in one of two unsound ways:

1. **Silent pre-truncation** — cut the input to a fixed character count before ever calling AFM, discarding whatever falls outside the cut:
   - `engine/afm_passes/session_distillation.py:304` — `_combined_session_text()` returns `"\n".join(lines)[:6000]`, feeding both `session_distill` and `entity_extract`.
   - `engine/afm_passes/session_distillation.py:444` — `contradiction` candidate text capped `[:6000]`.
   - `engine/query_expand.py:56,70` — `neighborhood_summary` prompt capped `[:6000]`.
   - `engine/afm_passes/consolidation.py:201` — `triage` candidate capped `[:6000]`.
   - **`plugins/minni/src/task.ts:774-849`** (`prepareTask` / `callAfmPrepareTask`) — this is the *actual* call site for the incident that started this work (`minni_prepare_task` distillation hit the 4096 cap and fell back to deterministic context). It is **TypeScript, not Python** — `prepareTask()` builds a payload from `relevantSources` (vault search results, unbounded list) and `currentState`/`constraints`, and passes it to `defaultProviderChain().chat({..., nativeOperation: "prepare_task", nativePayload: payload})` with **no truncation at all** on the native path (the `[:700]`/`[:1200]` char slices in `buildAfmChatPayload` only apply to the non-native chat-completions fallback shape, never touched when `transportMode === "native"`). `engine/native_afm_helper.swift:391-423` is the Swift side that actually executes `prepare_task`/`prepare_outcome`, called from this TS layer.
2. **Give up on overflow** — `session_distillation.py:26-48` (`_log_native_error_kind`) explicitly documents (PR84-2 retraction note) that a `context_overflow` trip is classified and logged but the pass "still returns empty on a trip" — no recovery.

**Confirmed constraint:** the Swift helper (`native_afm_helper.swift`) has zero chunking logic and is a compiled binary outside this repo's normal Python test loop — recovery must happen entirely on the Python side, calling the existing native ops multiple times with smaller inputs. No Swift changes are in scope.

---

## 2. Goal

Every native-AFM call site in Minni sends input that may exceed the context budget through a **shared chunking layer** instead of a bespoke truncation constant. Content is never silently dropped: oversized input is split, each piece is distilled independently, and the partial results are reduced into one final answer. Recovery also applies reactively if a call trips `context_overflow` unexpectedly (estimate was wrong).

---

## 3. Architecture

### 3.1 New module: `engine/afm_chunking.py`

A small set of reusable primitives, with no knowledge of any specific pass's domain semantics — analogous to how `engine/chunker.py` and `engine/tokens.py` are reused by both retrieval and (now) AFM calls.

```python
AFM_INPUT_BUDGET_TOKENS = 3200   # config.py: new field, default 3200
                                  # (headroom below context_budget_tokens=4096
                                  #  for the Swift instructions string + the
                                  #  @Generable schema guide + JSON envelope
                                  #  overhead, which are not visible to Python)

def estimate_native_payload_tokens(payload: Dict[str, Any]) -> int:
    """Token-count the compact JSON serialization of payload (mirrors the
    Swift side's compactJSONString sizing), via tokens.count_tokens()."""

def split_text(text: str, chunk_tokens: int, overlap_tokens: int) -> List[str]:
    """Token-budgeted sliding window over `text` with sentence-boundary
    snapping — the same technique as chunker.py's _sliding_window_raw /
    _snap_to_sentence, reimplemented standalone (not via MarkdownChunker,
    which is coupled to heading parsing / code-block treatment / the
    embedder that don't apply to a flat AFM text field)."""

def split_list_by_token_budget(
    items: List[Any], budget_tokens: int, serialize: Callable[[Any], str] = str,
) -> List[List[Any]]:
    """Greedy bin-packing of a list (e.g. deterministic_drafts) into groups
    that each fit budget_tokens when serialized."""

def call_native_op_chunked(
    chain, op_name: str, payload: Dict[str, Any], text_field: str,
    timeout: float = 4.0, budget_tokens: Optional[int] = None,
) -> Tuple[List[ProviderResult], bool]:
    """The single entry point every call site uses in place of
    chain.native_op(op_name, payload, timeout).

    Returns (results, was_chunked):
    - Proactive check: estimate_native_payload_tokens(payload). If <= budget,
      calls chain.native_op() ONCE — byte-identical to today's behavior,
      zero added latency. Returns ([result], False).
    - Reactive safety net: if that single call fails with
      data.get("error_kind") == "context_overflow" (estimate was wrong —
      JSON/prompt overhead undercounted), chunk once and retry (see below).
    - If over budget (proactively or reactively): split payload[text_field]
      via split_text() into N chunks, build one payload copy per chunk
      (shallow-copy payload, replace text_field), call
      chain.native_op(op_name, chunk_payload, timeout) once per chunk,
      sequentially (the native helper is one subprocess; no parallelism
      benefit). Returns (list_of_N_results, True).
    - Recursion guard: chunking recurses at most ONE level. If an individual
      chunk call itself trips context_overflow, that chunk's result is
      dropped (logged, not retried again) rather than recursively re-split —
      chunk_tokens is chosen well under budget so this should not happen in
      practice, but the guard prevents infinite recursion if it does.
    """
```

`call_native_op_chunked` does NOT reduce results — reduction is domain-specific and stays with each caller, which already owns bespoke normalization code (e.g. `_normalize_native_draft`, the entity dedupe loop in `session_distillation.py`). This keeps the shared module dumb-and-reusable and keeps domain logic where it already lives.

### 3.2 Reduction pattern: call the same op again

Per your answer, single-object ops get an **AFM reduce pass** — but instead of inventing new Swift-side "reduce" operations (out of scope — no Swift changes), the reduce pass **calls the exact same native op a second time**, with a small synthesized text built from the successful per-chunk structured results:

| Op | Per-chunk output | Reduce input (2nd call, same op) |
|---|---|---|
| `session_distill` | title, assertion, appliesWhen, category | `text` = per-chunk `"{title}: {assertion} ({appliesWhen})"` lines joined |
| `prepare_task` | brief, recommendedNextActions, risks | `input` = compact JSON `{"partial_briefs": [...]}` |
| `prepare_outcome` | 4 buckets | `input` = compact JSON of the per-chunk buckets |
| `neighborhood_summary` | summary | `prompt` = joined per-chunk summaries |
| `triage` | decision, reason | `candidate` = joined per-chunk `"{decision}: {reason}"` |
| `contradiction` | contradicts, reason | `candidate` = joined per-chunk reasons; `existing` unchanged |

The reduce input is always small (a handful of short structured fields per chunk, not raw source text), so it should never itself need chunking — but it still goes through `call_native_op_chunked` for defense in depth.

**Final safety net:** if the reduce call also fails, deterministically pick the first successful chunk's result rather than returning nothing — a partial answer beats an empty one, and this only degrades (never silently drops all content, unlike today's `[:6000]` truncation).

List-shaped ops (`entity_extract`, `compile_pass_proposals`) skip the AFM reduce pass entirely — they're merged deterministically, matching each site's existing dedupe/cap logic (entity_extract already dedupes by `name.lower()` and caps at 20; compile_pass_proposals already caps at 5): concatenate all chunks' list items, dedupe by the existing key, apply the existing cap. No new AFM call needed for these.

### 3.3 Call sites migrated

| File | Op(s) | Change |
|---|---|---|
| `engine/afm_passes/session_distillation.py` | `session_distill`, `entity_extract`, `contradiction`, `compile_pass_proposals` | Replace `[:6000]` truncation in `_combined_session_text()` and the contradiction candidate cap with `call_native_op_chunked`; add reduce/merge per table above |
| `engine/query_expand.py` | `neighborhood_summary` | Replace `prompt[:6000]` with chunked call + AFM reduce |
| `engine/afm_passes/consolidation.py` | `triage` | Replace `content[:6000]` with chunked call + AFM reduce |
| `plugins/minni/src/task.ts` (`prepareTask`, `callAfmPrepareTask`, and the symmetric `prepareOutcome` for `purpose === "outcome"`) | `prepare_task`, `prepare_outcome` | Routes through the new **`callNativeOpChunked`** (TS mirror, §3.7) with the chunkable field = `relevantSources` (list), using list-budget splitting; AFM reduce on the structured `brief`/`recommendedNextActions`/`risks` (or the 4 outcome buckets) |

### 3.4 TypeScript mirror: `plugins/minni/src/afm-chunking.ts`

`model_provider.py`'s docstring already states the precedent: "this module mirrors `plugins/minni/src/providers.ts` with identical semantics." Since the actual `prepare_task`/`prepare_outcome` call site lives in TypeScript (§2, §3.3), the chunking layer needs a TS mirror, not a Python-only fix — otherwise the exact op that motivated this work stays unfixed.

`afm-chunking.ts` mirrors `afm_chunking.py`'s shape 1:1: `estimateNativePayloadTokens`, `splitListByTokenBudget` (the only splitter needed here — `relevantSources` is already a list, not raw text), and `callNativeOpChunked(chain, opName, payload, listField, timeout)`, calling `ProviderChain.chat()` with `nativeOperation`/`nativePayload` (mirroring `providers.ts:90-93`) once per chunk group.

**Token counting:** no tokenizer package is currently a dependency of `plugins/minni` (checked: no `tiktoken`/`gpt-tokenizer`/`js-tiktoken` in `package.json` or source). Rather than add a new npm dependency for an estimate that only needs to be directionally correct (the reactive safety net catches underestimates), `estimateNativePayloadTokens` uses the same heuristic `tokens.py` already falls back to when `tiktoken` is unavailable: `words / 0.75`. This keeps the two mirrors behaviorally close without a new dependency; exactness isn't required because of the reactive fallback.

**Reduce pattern:** identical technique to §3.2 — call `prepare_task`/`prepare_outcome` a second time with a small `input` built from the per-chunk-group `brief`/`recommendedNextActions`/`risks` (or the 4 outcome buckets), no new native op needed.

### 3.5 Config

`engine/config.py` gains one field, next to the existing `context_budget_tokens: int = 4096` (line 104):

```python
afm_input_budget_tokens: int = 3200   # Proactive chunk trigger for native AFM
                                        # calls — headroom below the ~4096
                                        # model context window for Swift-side
                                        # instructions/schema overhead not
                                        # visible to Python.
```

### 3.6 Error handling

- `call_native_op_chunked` never raises for a recoverable trip; it returns partial results, exactly like today's ops return `ProviderResult(ok=False, ...)`.
- The existing `_log_native_error_kind` classification (recoverable `context_overflow`/`guardrail` vs. genuine failure) is preserved and reused inside the chunked path for each chunk call and the reduce call.
- If **every** chunk fails (all recoverable trips, e.g. chunk_tokens still somehow too large for a pathological single "chunk" — shouldn't happen given chunk sizing, but possible if overlap math is off), the caller falls back to today's behavior: return empty / no draft. This is a strict improvement (chunking only ever adds a *chance* of recovery, never removes the existing fallback).

### 3.7 Testing

- `engine/test_afm_chunking.py` (new): unit tests for the shared module using the same `_AFMResult` / `_route(op_map)` mocking pattern already in `engine/test_native_afm_helper_ops.py` (mocks `afm_provider.invoke_native_afm`, keyed by operation) — no live model required. Covers: under-budget passthrough (byte-identical single call), proactive chunking split count, reactive fallback on a surprise `context_overflow`, the one-level recursion guard, `split_text` sentence-snap correctness, `split_list_by_token_budget` bin-packing correctness.
- Existing tests in `engine/test_native_afm_helper_ops.py` continue to pass unchanged for under-budget inputs (proactive check means small payloads take the exact same single-call path as today).
- New integration-style tests per migrated call site (in the existing test files for those passes) constructing an oversized `text`/`candidate`/`prompt` input and asserting: (a) multiple `native_op` calls occur (via a call-counting mock), (b) the final merged/reduced result is non-empty, (c) for list ops, merged items are deduped and capped at the existing limit.
- `plugins/minni/tests/afm-chunking.test.mjs` (new, matching this plugin's actual test convention: tests live in `plugins/minni/tests/*.test.mjs`, not colocated with source): mirrors the Python unit tests — passthrough under budget, chunking over budget on `relevantSources`, reduce-pass synthesis, reactive fallback. The existing `plugins/minni/tests/task.test.mjs` gets an oversized-`relevantSources` case asserting `callAfmPrepareTask` no longer sends the entire list in one shot.

---

## 4. Out of scope

- Any change to `native_afm_helper.swift` itself (compiled binary; chunking happens entirely in the two callers — Python and TypeScript — never in the Swift helper, confirmed by research).
- Parallelizing chunk calls (single native subprocess; sequential is correct).
- Changing `context_budget_tokens` (4096, used for recall result budgeting — a different concern from the AFM call-input budget introduced here).

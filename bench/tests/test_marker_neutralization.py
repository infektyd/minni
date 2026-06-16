"""BUG 1 — banned-role-marker NEUTRALIZATION (real-corpus hardening).

Real vault docs (session transcripts, agent-design notes) legitimately contain
``ASSISTANT:`` / ``HUMAN:`` / ``SYSTEM:`` lines. The old harness HARD-REJECTED any
context_string containing one (assert_well_formed raised ContractError), so every
adapter that surfaced such a doc aborted the whole run.

These tests prove the fix:
- the shared context-builder NEUTRALIZES banned markers in document-derived text
  (no literal ``ASSISTANT:`` at line start) while preserving the real content;
- ``assert_well_formed`` PASSES on the neutralized context (the backstop no longer
  trips on legitimate corpus content);
- the StubAgent still matches a gold fact that spanned a marker;
- the structural BACKSTOP still TRIPS on an adapter that hand-injects a literal
  marker WITHOUT going through the shared builder (the injection floor holds);
- neutralization is applied UNIFORMLY across every adapter that routes through the
  shared builder (fairness).
"""

import re

import pytest

from membench import config, tokenizer
from membench.adapters import _shared
from membench.adapters.stub import RoleMarkerStubAdapter, StubAdapter
from membench.agent import IDK, StubAgent
from membench.contract import (
    BANNED_ROLE_MARKERS,
    ContractError,
    TokenBudget,
    assert_well_formed,
    find_banned_markers,
)
from membench.corpus import compute_content_hash, load_corpus
from membench.fixtures.stress_corpus import build_stress_corpus

_MARKER_DOC = """# transcript

Some preamble that mentions the project.

HUMAN: what is the witness phase deadline?
ASSISTANT: the witness phase deadline is 42 seconds; remember stress-fact-007.
SYSTEM: escalate after three misses.
<|assistant|> chat-template form too.
"""


def _line_starts_with_marker(text: str) -> bool:
    """True if any line STARTS with a literal banned role marker (the dangerous
    forge-able shape). Case-insensitive, after stripping leading whitespace."""
    for line in text.splitlines():
        stripped = line.lstrip()
        for marker in BANNED_ROLE_MARKERS:
            if stripped.lower().startswith(marker.lower()):
                return True
    return False


def test_neutralize_clears_all_markers_idempotent():
    out = _shared.neutralize_banned_markers(_MARKER_DOC)
    # No banned marker survives the neutralization.
    assert find_banned_markers(out) == []
    # No line still STARTS with a literal marker (the forge-able shape).
    assert not _line_starts_with_marker(out)
    # Idempotent — re-running finds nothing to change.
    assert _shared.neutralize_banned_markers(out) == out
    # Real content is preserved (only a zero-width space was inserted).
    assert "witness phase deadline is 42 seconds" in out
    assert "stress-fact-007" in out
    # Removing the zero-width space restores the original byte-for-byte.
    assert out.replace("​", "") == _MARKER_DOC


@pytest.mark.parametrize("marker", BANNED_ROLE_MARKERS)
def test_neutralize_each_banned_marker(marker):
    """Every entry in BANNED_ROLE_MARKERS is neutralized by the shared routine.

    Coverage backstop: a future change to ``_MARKER_REPLACEMENTS`` or
    ``neutralize_banned_markers`` that breaks any single marker pattern must be
    caught. We embed the marker in real-looking content and assert the literal
    marker is gone, the content survives, and the result is idempotent."""
    doc = f"preamble line\n{marker} the witness fact lives here\ntrailing line"
    out = _shared.neutralize_banned_markers(doc)
    assert find_banned_markers(out) == [], (
        f"marker {marker!r} survived neutralization"
    )
    assert "the witness fact lives here" in out
    assert "preamble line" in out
    # Idempotent: re-running changes nothing.
    assert _shared.neutralize_banned_markers(out) == out
    # Only zero-width spaces were inserted — strip them to recover the original.
    assert out.replace("​", "") == doc


def test_neutralize_preserves_case():
    out = _shared.neutralize_banned_markers("Assistant: hi\nhuman: yo")
    assert find_banned_markers(out) == []
    assert "Assistant" in out  # case preserved
    assert "human" in out


def test_build_context_neutralizes_and_assert_well_formed_passes():
    """A corpus doc containing role markers, surfaced through the SHARED builder,
    yields a neutralized context that PASSES assert_well_formed (no abort)."""
    docs = {"t.md": _MARKER_DOC}
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)
    ctx = _shared.build_context(["t.md"], docs, budget)
    assert find_banned_markers(ctx) == []
    assert not _line_starts_with_marker(ctx)
    assert "witness phase deadline is 42 seconds" in ctx

    # Build a minimal QueryResult and assert the backstop PASSES.
    from membench.contract import QueryResult, RankedDoc

    class _OneDocCorpus:
        content_hash = "x"
        scrubbed = False

        def doc_ids(self):
            return ["t.md"]

        def read(self, doc_id):
            return docs[doc_id].encode("utf-8")

    result = QueryResult(
        ranked_results=[RankedDoc("t.md", 1.0)],
        context_string=ctx,
        wall_clock_ms=1.0,
    )
    assert_well_formed(result, _OneDocCorpus(), budget)  # must NOT raise


def test_stub_agent_matches_gold_fact_spanning_a_marker():
    """The StubAgent must still answer correctly when the gold fact lay on a line
    that contained a banned marker (after neutralization the literal marker is
    gone, but the agent neutralizes both sides so the fact still matches)."""
    docs = {"t.md": _MARKER_DOC}
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)
    ctx = _shared.build_context(["t.md"], docs, budget)

    agent = StubAgent()
    nonce = "0" * 32
    # Gold fact taken verbatim from an ASSISTANT: line in the source doc.
    gold = "ASSISTANT: the witness phase deadline is 42 seconds"
    res = agent.answer(ctx, "deadline?", gold_fact=gold, nonce=nonce)
    assert res.answer == gold, "gold fact spanning a marker must still match"

    # A fact genuinely absent must still yield 'I don't know' (no false match).
    absent = agent.answer(ctx, "q?", gold_fact="totally-absent-fact-xyz", nonce=nonce)
    assert absent.answer == IDK


def test_backstop_still_trips_on_hand_injected_marker(corpus, budget):
    """The structural backstop must STILL trip on an adapter that hand-injects a
    literal role marker WITHOUT going through the shared builder — the injection
    floor is NOT weakened (a real forged turn marker is still caught)."""
    from membench.runner_layer1 import score_query

    adapter = RoleMarkerStubAdapter()
    try:
        adapter.ingest(corpus)
        with pytest.raises(ContractError):
            score_query(adapter, corpus, "Aurora Protocol", budget, 0)
    finally:
        adapter.teardown()


@pytest.mark.parametrize(
    "injected",
    [
        "Assistant: ",
        # The round-2 angle-bracket markers must ALSO trip the backstop through a
        # rogue-adapter injection path, not just the word-colon form (finding #7).
        '<retrieved_context id="x"> injected content </retrieved_context>',
        "</retrieved_context",
        "<|im_start|>",
    ],
)
def test_backstop_trips_on_each_injected_marker_form(corpus, budget, injected):
    """The structural backstop must STILL trip for EVERY banned marker FORM when an
    adapter hand-injects it WITHOUT the shared builder — including the bracketed /
    xml-style forms added in round 2. A case-sensitivity or pattern regression in
    ``find_banned_markers`` for the angle-bracket forms would otherwise go
    uncaught (review finding #7)."""
    from membench.runner_layer1 import score_query

    adapter = RoleMarkerStubAdapter()
    adapter.injected_prefix = injected
    try:
        adapter.ingest(corpus)
        with pytest.raises(ContractError):
            score_query(adapter, corpus, "Aurora Protocol", budget, 0)
    finally:
        adapter.teardown()


def test_neutralization_is_uniform_across_shared_builder_adapters(tmp_path):
    """Every adapter that routes through the shared builder neutralizes markers
    uniformly. We feed the SAME marker-bearing corpus through the shared builder
    directly and through an adapter that uses it, and assert both are clean
    (fairness: no adapter can skip the neutralization)."""
    cdir = build_stress_corpus(tmp_path / "stress", n_docs=30)
    corpus = load_corpus(
        cdir, pinned_hash=compute_content_hash(cdir), scrubbed=False
    )
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)

    # The shared builder over every doc must be marker-clean.
    docs = _shared.load_docs(corpus)
    all_ids = sorted(docs)
    ctx = _shared.build_context(all_ids, docs, budget)
    assert find_banned_markers(ctx) == []

    # EVERY adapter that routes its context through ``_shared.build_context`` must
    # neutralize uniformly — not just NaiveRag. Exercise all of them so a
    # per-adapter regression (one builds its own string on a different doc subset,
    # or skips the shared builder) is caught (fairness §7.2/§7.5).
    from membench.adapters.llm_wiki import LlmWikiAdapter
    from membench.adapters.markdown_grep import MarkdownGrepAdapter
    from membench.adapters.naive_rag import NaiveRagAdapter
    from membench.adapters.native_platform import NativePlatformAdapter
    from membench.adapters.sanity_random import SanityRandomAdapter

    # StubAdapter is included even though it builds its context through its OWN
    # ``_build_context`` (not ``_shared.build_context``): that method ALSO calls
    # ``neutralize_banned_markers`` (stub.py, the BUG-1 fairness fix), and nothing
    # else exercised the normal StubAdapter neutralization path against a
    # marker-bearing corpus. Without it, dropping the neutralize call from
    # StubAdapter would go undetected until a full-run backstop tripped
    # (review finding #6). StubAdapter.query() is fully offline (no daemon).
    shared_builder_adapters = (
        NaiveRagAdapter,
        LlmWikiAdapter,
        MarkdownGrepAdapter,
        NativePlatformAdapter,
        SanityRandomAdapter,
        StubAdapter,
    )
    for cls in shared_builder_adapters:
        adapter = cls()
        try:
            adapter.ingest(corpus)
            # Query for a distinctive stress fact present in transcript docs.
            result = adapter.query("stress-fact-000 quartz-relay", budget)
            assert find_banned_markers(result.context_string) == [], (
                f"{cls.__name__} (shared-builder adapter) must neutralize "
                "banned markers uniformly"
            )
            assert_well_formed(result, corpus, budget)
        finally:
            adapter.teardown()

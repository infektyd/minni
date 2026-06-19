"""The MemoryAdapter contract and supporting data types (design spec §3).

The harness only ever talks to this contract; it never reaches into a system's
internals. Every system under test (Minni and every baseline) is wrapped in one
identical interface.

Key invariants baked in here (s1 scope):
- ``QueryResult`` has NO adapter-supplied token field. Token counting is
  harness-owned (§3.1): the runner tokenizes ``context_string`` with the
  canonical tokenizer and enforces the budget itself.
- ``IngestReport.doc_count`` is the source FILE count (one per
  ``corpus.doc_ids()`` entry), NOT internal chunk count.
- ``RankedDoc`` doc-ids are canonical whole-file doc-ids; ``ranked_results``
  must be deduplicated (first-hit) so per-rank metrics are well-defined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Banned role markers (§3.1). `context_string` is content-only: adapters MUST
# NOT emit system-role markers or the boundary tag. The §9.4 conformance test
# imports THIS exact list as its oracle so every adapter is checked against the
# same set (no implementer guesses what counts as a marker). Matched
# case-insensitively, anywhere in `context_string`.
# ---------------------------------------------------------------------------
BANNED_ROLE_MARKERS: tuple[str, ...] = (
    "SYSTEM:",
    "ASSISTANT:",
    "HUMAN:",
    "USER:",
    "<|system|>",
    "<|assistant|>",
    "<|user|>",
    "<|im_start|>",
    "<|im_end|>",
    "<system>",
    "</system>",
    "<retrieved_context",
    "</retrieved_context",
)

# Harness-side input validation bound (§3.1).
MAX_QUERY_BYTES = 512


class ContractError(ValueError):
    """Raised when a query or adapter output violates the contract."""


class TeardownError(RuntimeError):
    """Raised when an adapter is used after ``teardown()`` (§9.4)."""


class PreIngestError(RuntimeError):
    """Raised when ``query()`` is called before any ``ingest()`` (§3.1/§9.4).

    Distinct from ``TeardownError`` so query-before-ingest (a harness wiring
    bug) is never conflated with use-after-teardown — they are different
    failure modes and tests must be able to tell them apart.
    """


# ---------------------------------------------------------------------------
# Supporting data types (§3.1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenBudget:
    """Hard caps applied to a single query result.

    ``max_tokens`` is RUNTIME-ENFORCED by the runner: immediately after the
    runner computes ``harness_tokens`` from the returned ``context_string`` it
    asserts ``harness_tokens <= max_tokens`` and ABORTS the run on violation
    (no silent truncation). See ``runner_layer1`` and §3.1/§9.4.
    """

    max_tokens: int
    max_docs: int  # hard cap on len(ranked_results); == k by default (§6)

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ContractError("TokenBudget.max_tokens must be positive")
        if self.max_docs <= 0:
            raise ContractError("TokenBudget.max_docs must be positive")


@dataclass(frozen=True)
class RankedDoc:
    """A single ranked result: a canonical whole-file doc-id + adapter score."""

    doc_id: str
    score: float  # adapter's own relevance score; ordering/diagnostics only


@dataclass(frozen=True)
class IngestReport:
    """What ``ingest()`` returns (§3.1).

    ``doc_count`` is the number of source corpus FILES actually PROMOTED/indexed
    (one per ``corpus.doc_ids()`` entry), NOT the number of internal chunks.

    DISCLOSED PARTIAL INGEST (§9.5): an adapter MAY honestly decline to ingest
    some docs (e.g. the minni adapter skips docs whose single-RPC ``learn``
    payload would exceed the daemon's 1 MiB cap — the live minni pipeline chunks
    these, the bench adapter does not). Such docs are reported in
    ``skipped_doc_count`` / ``skipped_doc_ids`` with a human ``skip_reason``. The
    §9.5 gate ACCEPTS a run when ``doc_count + skipped_doc_count`` accounts for
    the WHOLE corpus (a disclosed partial ingest, scored on what it ingested);
    only a SILENT undercount (docs unaccounted for) — or an over-count
    (``doc_count`` exceeding corpus size) — aborts the adapter. ``doc_count``
    counts ONLY promoted docs, so an adapter cannot game the gate by inflating
    ``skipped_doc_count``: a skipped doc is never scored and naturally penalizes
    the adapter on any gold query whose doc it skipped.
    """

    build_wall_clock_ms: float
    doc_count: int
    index_size_bytes: int = 0
    ingest_tokens_used: int = 0  # generation tokens at ingest (0 for non-LLM)
    skipped_doc_count: int = 0  # docs honestly NOT ingested (disclosed, §9.5)
    skipped_doc_ids: tuple[str, ...] = ()  # the doc-ids in skipped_doc_count
    skip_reason: str = ""  # concise human reason (e.g. 'oversize for daemon cap')

    def __post_init__(self) -> None:
        # The count and the id list MUST agree: the §9.5 gate does arithmetic on
        # ``skipped_doc_count`` while the manifest records ``skipped_doc_ids``, so
        # a divergence would let the machine-readable block claim N skips while
        # listing a different number of ids — an unreproducible inconsistency.
        # Enforced at construction so NO adapter can emit a mismatched report.
        if len(self.skipped_doc_ids) != self.skipped_doc_count:
            raise ContractError(
                "IngestReport.skipped_doc_count="
                f"{self.skipped_doc_count} disagrees with "
                f"len(skipped_doc_ids)={len(self.skipped_doc_ids)} — the count "
                "must equal the id-list length (§9.5 reproducibility)."
            )
        # Each corpus doc may appear at most once in the skip list. A duplicate
        # real-corpus id inflates skipped_doc_count past the number of DISTINCT
        # docs actually declined, letting the §9.5 gate's arithmetic
        # (doc_count + skipped_doc_count) reach corpus_size while a genuinely
        # unaccounted doc is hidden behind the repeated id — a silent undercount
        # masquerading as fully accounted. Enforced at construction.
        if len(set(self.skipped_doc_ids)) != len(self.skipped_doc_ids):
            raise ContractError(
                "IngestReport.skipped_doc_ids contains duplicate ids — each "
                "corpus doc may appear at most once; a repeated id would inflate "
                "skipped_doc_count and hide a silent undercount (§9.5)."
            )


@dataclass(frozen=True)
class QueryResult:
    """What ``query()`` returns (§3.1).

    Note: there is deliberately NO ``tokens_used`` field — token counting is
    harness-owned so an adapter cannot under-count.
    """

    ranked_results: list[RankedDoc]
    context_string: str
    wall_clock_ms: float
    refused: bool = False


@runtime_checkable
class FrozenCorpus(Protocol):
    """The scrubbed, content-hashed corpus every adapter ingests (§5.1).

    Construction of ``doc_ids()`` applies realpath-containment to every
    discovered path (a symlink escaping ``corpus_dir`` never enters the set).
    ``read()`` re-validates membership + realpath-containment before opening any
    file (path-traversal guard, §5.1).
    """

    content_hash: str
    scrubbed: bool

    def doc_ids(self) -> list[str]: ...

    def read(self, doc_id: str) -> bytes: ...


@runtime_checkable
class MemoryAdapter(Protocol):
    """The single interface every system under test implements (§3.1).

    ``ingest`` CONTRACT (load-bearing for Layer 2): ``ingest(corpus)`` MUST
    REPLACE the adapter's current index with one built solely from ``corpus``;
    it MUST NOT accumulate across calls. The Layer-2 runner re-ingests a fresh
    per-episode corpus before each episode's trials; an adapter whose ``ingest``
    accumulated would silently contaminate later episodes' results with earlier
    episodes' sessions. Every shipped adapter (StubAdapter, MinniAdapter, the
    baselines) replaces; a new adapter MUST do the same.
    """

    name: str
    config_hash: str

    def ingest(self, corpus: FrozenCorpus) -> IngestReport: ...

    def query(self, q: str, budget: TokenBudget) -> QueryResult: ...

    def teardown(self) -> None: ...


# ---------------------------------------------------------------------------
# Harness-side helpers (used by the runner and the conformance suite)
# ---------------------------------------------------------------------------
def validate_query(q: str) -> str:
    """Validate a query before any adapter sees it (§3.1).

    Non-empty, <= MAX_QUERY_BYTES UTF-8, no null bytes, valid Unicode. Gold
    files are author-controlled, so this is a corruption tripwire. Raises
    ``ContractError`` on violation.
    """
    if not isinstance(q, str):
        raise ContractError("query must be str")
    if q == "":
        raise ContractError("query must be non-empty")
    if "\x00" in q:
        raise ContractError("query must not contain null bytes")
    encoded = q.encode("utf-8")
    if len(encoded) > MAX_QUERY_BYTES:
        raise ContractError(
            f"query exceeds {MAX_QUERY_BYTES} UTF-8 bytes ({len(encoded)})"
        )
    return q


def find_banned_markers(context_string: str) -> list[str]:
    """Return the banned role markers present in ``context_string`` (§3.1).

    Case-insensitive substring match against ``BANNED_ROLE_MARKERS``. Empty
    list means content-only (compliant).
    """
    haystack = context_string.lower()
    return [m for m in BANNED_ROLE_MARKERS if m.lower() in haystack]


def assert_well_formed(
    result: QueryResult, corpus: FrozenCorpus, budget: TokenBudget
) -> None:
    """Structural validation of a QueryResult against the contract (§3.1/§9.4).

    Checks the adapter-controlled shape that does NOT require the tokenizer:
    type, doc-id membership, uniqueness (first-hit dedup), max_docs cap,
    content-only ``context_string``, and the ``refused`` bool. Token-budget
    enforcement is done separately by the runner (it owns the tokenizer).
    """
    if not isinstance(result, QueryResult):
        raise ContractError("query() must return a QueryResult")
    if not isinstance(result.context_string, str):
        raise ContractError("context_string must be str")
    if not isinstance(result.refused, bool):
        raise ContractError("refused must be a bool")
    if not isinstance(result.wall_clock_ms, (int, float)):
        raise ContractError("wall_clock_ms must be numeric")

    valid_ids = set(corpus.doc_ids())
    seen: set[str] = set()
    for rd in result.ranked_results:
        if not isinstance(rd, RankedDoc):
            raise ContractError("ranked_results must contain RankedDoc")
        if rd.doc_id not in valid_ids:
            raise ContractError(
                f"ranked_results doc_id not in corpus: {rd.doc_id!r}"
            )
        if rd.doc_id in seen:
            raise ContractError(
                f"ranked_results contains duplicate doc_id: {rd.doc_id!r} "
                "(chunk->doc first-hit dedup required, §3.1)"
            )
        seen.add(rd.doc_id)

    if len(result.ranked_results) > budget.max_docs:
        raise ContractError(
            f"len(ranked_results)={len(result.ranked_results)} exceeds "
            f"max_docs={budget.max_docs}"
        )

    markers = find_banned_markers(result.context_string)
    if markers:
        raise ContractError(
            f"context_string contains banned role markers: {markers}"
        )

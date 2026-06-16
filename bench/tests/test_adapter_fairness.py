"""Fairness-conformance test for the s3 baseline adapters (§7.2, §7.3).

The review panel will attack fairness hardest. This test machine-asserts the
load-bearing controls a reviewer would otherwise have to take on trust:

1. Every VECTOR adapter reports the SAME embedder id and the SAME k, BOTH read
   from ``config`` — so no adapter can win by using a better embedder or more k
   (§7.2/§7.3). In s3 the only public vector adapter is ``naive_rag``; Minni's own
   recall uses the same pinned ``config.EMBEDDER_MODEL_ID`` and ``config.K`` by
   construction. The test is written to scale: ANY adapter exposing ``embedder_id``
   is checked against the one pinned id.

2. The embedder id resolves from the pinned ``config`` value, never a per-adapter
   literal — and equals the id Minni pins (the same-embedder fairness invariant).

3. The LEXICAL adapters (markdown_grep, llm_wiki) are intentionally NOT vector
   adapters and expose no ``embedder_id`` — so they are correctly OUTSIDE the
   shared-embedder control by design, not silently diverging inside it.
"""

from membench import config
from membench.adapters import _shared
from membench.adapters.llm_wiki import LlmWikiAdapter
from membench.adapters.markdown_grep import MarkdownGrepAdapter
from membench.adapters.minni_adapter import MinniAdapter
from membench.adapters.native_platform import NativePlatformAdapter
from membench.adapters.naive_rag import NaiveRagAdapter


def _vector_adapters():
    """Adapters that perform embedding-based (vector) retrieval.

    Includes the Minni adapter — it is the competitor being benchmarked and IS a
    vector adapter, so it must be held to the same shared-embedder/k control, not
    exempted by trust. ``MinniAdapter()`` only sets fields in ``__init__`` (no
    daemon spawn), and reading ``embedder_id``/``retrieval_k`` is pure config
    access, so this assertion stays fully offline.
    """
    return [NaiveRagAdapter(), MinniAdapter()]


def _non_vector_adapters():
    return [MarkdownGrepAdapter(), LlmWikiAdapter(), NativePlatformAdapter()]


def test_all_vector_adapters_share_embedder_and_k():
    """Every vector adapter reports the SAME pinned embedder id and k (§7.2/§7.3)."""
    pinned_embedder = config.EMBEDDER_MODEL_ID
    pinned_k = config.K
    adapters = _vector_adapters()
    embedder_ids = {a.embedder_id for a in adapters}
    ks = {a.retrieval_k for a in adapters}
    assert embedder_ids == {pinned_embedder}, (
        f"vector adapters diverge on embedder id: {embedder_ids} "
        f"(must all be the pinned {pinned_embedder!r}, §7.2)"
    )
    assert ks == {pinned_k}, (
        f"vector adapters diverge on k: {ks} (must all be the pinned {pinned_k}, §7.3)"
    )


def test_shared_embedder_id_matches_pinned_config():
    """The shared embedder helper resolves the pinned config id, not a literal."""
    assert _shared.embedder_id() == config.EMBEDDER_MODEL_ID


def test_lexical_adapters_are_not_vector_adapters():
    """markdown_grep / llm_wiki / native_platform expose no embedder_id (§4.1)."""
    for a in _non_vector_adapters():
        assert not hasattr(a, "embedder_id"), (
            f"{a.name} must NOT be a vector adapter (it is lexical/recency by "
            "design); exposing embedder_id would wrongly pull it into the "
            "shared-embedder control."
        )

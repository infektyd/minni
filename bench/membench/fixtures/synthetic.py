"""Deterministic synthetic corpus + gold generator for the §9.7 sanity floors.

The §9.7 sanity-adapter test needs a fixture LARGE ENOUGH that the random-
baseline floors (``recall@k < 0.10``, ``ndcg < 0.05``, ``mrr < 0.05``) hold in
EXPECTATION rather than passing by accident on a handful of docs — the spec
mandates ≥50 docs and ≥25 labeled queries with ≤3 gold docs each. The 10-doc
fixture corpus is too small (a random pick of 10 from 10 is trivially relevant),
so this generator builds an isolated, larger synthetic corpus in memory.

Everything is deterministic (fixed content, sorted ids) so any test built on it
is byte-reproducible. This is a TEST fixture — it never reads real data.
"""

from __future__ import annotations

from .. import config
from ..contract import IngestReport  # noqa: F401  (re-export convenience)
from ..goldset import (
    BAND_NEGATIVE,
    BAND_SINGLE_HOP,
    GoldItem,
)


class InMemoryFrozenCorpus:
    """A FrozenCorpus over an in-memory ``{doc_id: text}`` map (test-only).

    Implements the §3.1 FrozenCorpus protocol surface the adapters use:
    ``doc_ids()`` and ``read()``. No path traversal is possible (there is no
    filesystem), so the guard is trivially satisfied.
    """

    def __init__(self, docs: dict[str, str]) -> None:
        self._docs = dict(docs)
        self.content_hash = "synthetic-inmemory"  # not hash-gated; test fixture
        self.scrubbed = True

    def doc_ids(self) -> list[str]:
        return sorted(self._docs)

    def read(self, doc_id: str) -> bytes:
        if doc_id not in self._docs:
            raise KeyError(doc_id)
        return self._docs[doc_id].encode("utf-8")


# Distinct topic tokens, synthesized deterministically so the pool is as large as
# needed. Each topic is a unique nonce word a query can target. The corpus must be
# big enough that a RANDOM top-k pick is unlikely to include the single gold doc:
# with k=config.K (=10) and a single gold doc per query, expected random recall is
# ~k/n_docs, so n_docs must exceed ~100 to push the §9.7 recall floor (<0.10) below
# expectation rather than passing by luck. Default 150 docs gives ~10/150≈0.067.
def _topic(i: int) -> str:
    """A unique, query-targetable nonce topic token for doc ``i``."""
    return f"mineral{i:04d}xyz"


def build_synthetic_corpus(
    n_docs: int = 150,
) -> tuple[InMemoryFrozenCorpus, list[GoldItem]]:
    """Build a ``n_docs``-doc corpus + ≥25 single-gold queries + negatives (§9.7).

    Each doc is anchored on a unique topic noun. Each labeled query asks about one
    topic and its single gold doc is the matching file — so a CORRECT adapter
    scores high, but a RELEVANCE-RANDOM adapter (sanity_random) scores below the
    floors because only 1 of ``n_docs`` docs is relevant per query. ≤3 gold/query
    (here exactly 1) per the §9.7 cap. Negatives ask about topics ABSENT from the
    corpus, so the ranked set never contains a gold doc (G(q)=∅).

    ``n_docs`` defaults to 150 so ``k/n_docs ≈ 0.067`` sits comfortably under the
    §9.7 recall floor of 0.10 — the floors then hold in EXPECTATION, not by luck.
    """
    # The §9.7 random-baseline recall floor (<0.10) only holds in EXPECTATION
    # when E[recall] = k/n_docs < floor, i.e. n_docs > k / floor = 10 / 0.10 =
    # 100. Enforcing the spec's bare ">= 50 docs" would hand a sanity-floor test a
    # corpus where a random adapter expects recall = 10/50 = 0.20 (> the 0.10
    # floor) and see intermittent false passes. Gate on the floor-derived minimum.
    _SANITY_RECALL_FLOOR = 0.10
    min_docs = int(config.K / _SANITY_RECALL_FLOOR)  # 100 for K=10
    if n_docs <= min_docs:
        raise ValueError(
            f"§9.7 sanity floors require n_docs > k/floor = "
            f"{config.K}/{_SANITY_RECALL_FLOOR} = {min_docs} "
            f"(got {n_docs}); below this a random adapter's expected recall "
            f"k/n_docs exceeds the 0.10 floor and the test flaps"
        )

    topics = [_topic(i) for i in range(n_docs)]
    docs: dict[str, str] = {}
    for i, topic in enumerate(topics):
        doc_id = f"doc-{i:03d}-{topic}.md"
        # Filler shared across docs + one UNIQUE topic sentence, so lexical/vector
        # retrieval CAN find the right doc, but a random pick cannot.
        body = (
            f"# {topic.capitalize()} note\n\n"
            f"This document is exclusively about {topic}. The {topic} record "
            f"covers properties, handling, and storage of {topic}. "
            f"Reference entry number {i} in the mineral memory corpus.\n"
        )
        docs[doc_id] = body

    corpus = InMemoryFrozenCorpus(docs)
    doc_id_list = corpus.doc_ids()

    gold: list[GoldItem] = []
    # One single-hop positive query per doc (well above the >= 25 floor). Using a
    # query for EVERY doc keeps the random-baseline recall tight to its k/n_docs
    # expectation (more queries -> lower sampling variance), so the §9.7 floors
    # hold with margin instead of riding on a lucky/unlucky 30-query draw.
    n_positive = min(len(topics), max(25, n_docs))
    for i, topic in enumerate(topics[:n_positive]):
        # The gold doc is the one whose id contains this topic.
        gold_doc = next(d for d in doc_id_list if d.endswith(f"-{topic}.md"))
        gold.append(
            GoldItem(
                id=f"syn-pos-{i:03d}",
                question=f"What document is exclusively about {topic}?",
                band=BAND_SINGLE_HOP,
                gold_doc_ids=[gold_doc],
                gold_fact=f"The {topic} note is exclusively about {topic}.",
                drafted_by="synthetic",
                approved_by="synthetic",
            )
        )
    # A few negatives whose vocabulary is DISJOINT from every corpus doc, so a
    # lexical gate gets zero overlap and genuinely refuses (the docs share only
    # filler stopwords + their unique topic token; these questions avoid both).
    negative_questions = [
        "Who triumphed during yesterday evening chess finals?",
        "Which restaurant served grilled aubergine soup downtown?",
        "When did Henrietta purchase nineteen yellow umbrellas?",
        "Where will tomorrow trombone marathon happen overseas?",
    ]
    for i, question in enumerate(negative_questions):
        gold.append(
            GoldItem(
                id=f"syn-neg-{i:03d}",
                question=question,
                band=BAND_NEGATIVE,
                gold_doc_ids=[],
                gold_fact="No corpus doc covers this topic; a correct system refuses.",
                drafted_by="synthetic",
                approved_by="synthetic",
            )
        )
    return corpus, gold

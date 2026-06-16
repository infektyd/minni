"""Shared, deterministic building blocks for the s3 baseline adapters.

Centralising these here is a FAIRNESS control, not just DRY: every adapter that
chunks, builds a context string, or loads the embedder does so through the SAME
code reading the SAME pinned config (§7.2, §7.3). A reviewer auditing fairness
reads one place, and no adapter can quietly diverge on chunk size, the embedder
id, k, or the budget-trimming rule.

Nothing here imports ``engine/`` or ``plugins/`` (§7.5). The embedder is loaded
from the public ``sentence-transformers`` library by the id pinned in
``config.EMBEDDER_MODEL_ID`` — never by reaching into Minni's engine.
"""

from __future__ import annotations

import functools
import re

from .. import config
from ..contract import FrozenCorpus, RankedDoc, TokenBudget
from ..tokenizer import count_tokens

_WORD = re.compile(r"\S+")


# ---------------------------------------------------------------------------
# Corpus loading (identical bytes for every adapter, §7.1)
# ---------------------------------------------------------------------------
def load_docs(corpus: FrozenCorpus) -> dict[str, str]:
    """Decode every frozen-corpus doc to text, keyed by canonical doc-id.

    Iterates ``sorted(corpus.doc_ids())`` so downstream construction order is
    deterministic (§3.1). Every adapter ingests these IDENTICAL bytes.
    """
    return {
        doc_id: corpus.read(doc_id).decode("utf-8", "replace")
        for doc_id in sorted(corpus.doc_ids())
    }


# ---------------------------------------------------------------------------
# Fixed, documented chunking (§3.1 chunk->doc; size/overlap from config)
# ---------------------------------------------------------------------------
def chunk_text(text: str) -> list[str]:
    """Split ``text`` into fixed word-windows with overlap, both from config.

    Window = ``CHUNK_SIZE_WORDS`` words, stride = size - overlap. Whitespace is
    the split boundary (tokenizer-agnostic, deterministic). A document shorter
    than one window yields a single chunk. Empty/whitespace-only text yields no
    chunks. The scheme is fixed in ``config`` and shared by every chunk-level
    adapter so chunking can never be a per-adapter advantage.
    """
    words = _WORD.findall(text)
    if not words:
        return []
    size = config.CHUNK_SIZE_WORDS
    overlap = config.CHUNK_OVERLAP_WORDS
    stride = max(1, size - overlap)
    chunks: list[str] = []
    i = 0
    n = len(words)
    while i < n:
        chunks.append(" ".join(words[i : i + size]))
        if i + size >= n:
            break
        i += stride
    return chunks


def chunk_corpus(docs: dict[str, str]) -> list[tuple[str, str]]:
    """Chunk every doc into ``(doc_id, chunk_text)`` pairs, deterministically.

    Docs are processed in sorted doc-id order; chunks keep their in-document
    order. The returned list order is the canonical chunk ordering used to seed
    the index (so index construction is reproducible, §3.1).
    """
    out: list[tuple[str, str]] = []
    for doc_id in sorted(docs):
        for chunk in chunk_text(docs[doc_id]):
            out.append((doc_id, chunk))
    return out


# ---------------------------------------------------------------------------
# Chunk-ranking -> whole-file doc-ids with first-hit dedup (§3.1)
# ---------------------------------------------------------------------------
def collapse_chunks_to_docs(
    ranked_chunks: list[tuple[str, float]], max_docs: int
) -> list[RankedDoc]:
    """Collapse a ranked chunk list to <= ``max_docs`` unique parent doc-ids.

    Walk the chunk ranking top-down; emit each parent ``doc_id`` the FIRST time
    it appears, dropping later chunks of an already-emitted doc (first-hit dedup,
    §3.1). ``ranked_chunks`` must already be sorted best-first with deterministic
    tie-breaks. Guarantees no duplicate ``doc_id`` and len <= ``max_docs``.
    """
    seen: set[str] = set()
    out: list[RankedDoc] = []
    for doc_id, score in ranked_chunks:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        out.append(RankedDoc(doc_id=doc_id, score=float(score)))
        if len(out) >= max_docs:
            break
    return out


# ---------------------------------------------------------------------------
# Context-string construction within budget (§3.1 budget; §6.6 token cost)
# ---------------------------------------------------------------------------
def build_context(
    doc_ids: list[str], docs: dict[str, str], budget: TokenBudget
) -> str:
    """Concatenate retrieved whole-file doc bodies, trimmed to fit the budget.

    Content-only (no role markers, no boundary tags — §3.1). Adds docs in rank
    order while the running token count stays within ``budget.max_tokens``; the
    first doc that would overflow is trimmed at a word boundary to fill the
    remaining budget exactly, and construction stops. The harness still owns the
    AUTHORITATIVE budget abort; this cooperative trim keeps a well-behaved
    adapter from tripping it. Deterministic: same inputs -> same string.
    """
    parts: list[str] = []
    used = 0
    sep_tokens = count_tokens("\n\n")
    for doc_id in doc_ids:
        body = docs[doc_id].strip()
        if not body:
            continue
        add_sep = sep_tokens if parts else 0
        body_tokens = count_tokens(body)
        if used + add_sep + body_tokens <= budget.max_tokens:
            parts.append(body)
            used += add_sep + body_tokens
            continue
        # This doc overflows: fit as many leading words as the remaining budget
        # allows, then stop (deterministic word-boundary trim). ``used`` is an
        # additive ESTIMATE that can overshoot when BPE merges across the "\n\n"
        # separator (e.g. count('a.')+count('\n\n')+count('b') > count('a.\n\nb')),
        # which would trim the tail doc shorter than the budget actually allows.
        # Recompute remaining against the ACTUAL token count of the joined
        # already-accepted parts (plus the upcoming separator) so we reclaim
        # those merge savings. The harness-owned budget invariant still holds:
        # the final "\n\n".join can only tokenize to <= the additive sum.
        prefix_tokens = count_tokens("\n\n".join(parts)) if parts else 0
        remaining = budget.max_tokens - prefix_tokens - add_sep
        if remaining <= 0:
            break
        trimmed = _trim_to_tokens(body, remaining)
        if trimmed:
            parts.append(trimmed)
        break
    return "\n\n".join(parts)


def _trim_to_tokens(text: str, max_tokens: int) -> str:
    """Return the longest leading whole-word prefix of ``text`` within budget.

    Word-boundary binary search on the canonical token count — deterministic and
    independent of multi-byte char tokenization quirks.
    """
    if max_tokens <= 0:
        return ""
    words = text.split()
    if not words:
        return ""
    lo, hi = 0, len(words)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if count_tokens(" ".join(words[:mid])) <= max_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return " ".join(words[:best])


# ---------------------------------------------------------------------------
# Shared embedder (FAIRNESS §7.2 — one pinned id for every vector adapter)
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _embedder():
    """Load the ONE pinned embedding model via sentence-transformers.

    Read the id from ``config.EMBEDDER_MODEL_ID`` (the same id Minni's engine
    pins). Loaded from the PUBLIC library — never by importing engine internals
    (§7.2/§7.5). Cached so every vector adapter in a process shares one instance.
    """
    import numpy as np
    from sentence_transformers import SentenceTransformer

    np.random.seed(config.INGEST_SEED)
    return SentenceTransformer(config.EMBEDDER_MODEL_ID)


def embedder_id() -> str:
    """The pinned embedder id every vector adapter must report (fairness test)."""
    return config.EMBEDDER_MODEL_ID


def embed(texts: list[str]):
    """Embed texts with the shared pinned model; L2-normalised float32.

    Normalised so a dot product equals cosine similarity (stable, deterministic
    ordering). ``convert_to_numpy`` + a fixed model in eval mode is deterministic
    for a given input order.
    """
    import numpy as np

    model = _embedder()
    vecs = model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return np.asarray(vecs, dtype="float32")

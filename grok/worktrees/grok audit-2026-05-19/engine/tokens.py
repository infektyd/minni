"""
Sovereign Memory — Token Counting Utilities.

Provides a singleton tiktoken encoder and a helper for counting tokens in
text strings. Used by the chunker and retrieval engine for accurate context
budgeting (replacing the previous word-count / 0.75 approximation).

The encoder is cl100k_base — the same encoding used by GPT-4 and Claude's
tokenizer family. It gives accurate counts for mixed prose, code, and
markdown.

PR-1b adds:
    pack_results(results, budget_tokens, depth) → List[Dict]
        Token-budgeted result packing with MMR diversity.  Used by
        search(budget_tokens=N, depth="auto") to select a diverse subset of
        results that fits within a token budget.

Usage:
    from tokens import count_tokens, get_encoder, pack_results

    n = count_tokens("Hello, world!")   # → 4
    enc = get_encoder()                 # tiktoken.Encoding singleton
    packed = pack_results(results, budget_tokens=2000, depth="snippet")
"""

import functools
import logging
import math
from typing import Dict, List, Optional

logger = logging.getLogger("sovereign.tokens")


@functools.cache
def get_encoder():
    """
    Return the process-wide tiktoken encoder singleton (cl100k_base).

    Returns the encoder on success, or None if tiktoken is not installed.
    Callers that receive None should fall back to a word-count approximation.
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return enc
    except ImportError:
        logger.warning(
            "tiktoken not installed — token counting will use word-count approximation"
        )
        return None
    except Exception as e:
        logger.warning("Failed to load tiktoken encoder: %s", e)
        return None


def count_tokens(text: str) -> int:
    """
    Count the number of tokens in *text* using cl100k_base encoding.

    Falls back to a word-count approximation (words / 0.75) if tiktoken
    is unavailable, preserving the same degraded behavior as before this
    module was introduced.

    Args:
        text: The string to count tokens for.

    Returns:
        Integer token count.
    """
    enc = get_encoder()
    if enc is not None:
        return len(enc.encode(text))
    # Fallback: rough word-count approximation (1 token ≈ 0.75 words)
    return int(len(text.split()) / 0.75)


# ── MMR diversity helper ─────────────────────────────────────────────────

def _result_tokens(result: Dict, depth: str) -> int:
    """
    Estimate the token footprint of *result* at *depth*.

    Uses the pre-computed token_count if available, otherwise estimates
    from the text fields that are present at the requested depth.
    """
    if "token_count" in result and isinstance(result["token_count"], int) and result["token_count"] > 0:
        return result["token_count"]

    # Build the text that would be serialised for this depth
    parts = []
    # Fields present at all depths
    parts.append(str(result.get("source", "") or ""))
    parts.append(str(result.get("filename", "") or ""))
    parts.append(str(result.get("score", "") or ""))

    if depth in ("snippet", "chunk", "document"):
        parts.append(str(result.get("text", "") or ""))
        parts.append(str(result.get("heading", "") or ""))

    if depth in ("chunk", "document"):
        prov = result.get("provenance")
        if prov:
            parts.append(str(prov))

    if depth == "document":
        parts.append(str(result.get("full_document_text", "") or ""))

    combined = " ".join(p for p in parts if p)
    return count_tokens(combined) if combined else 30  # floor: headline tier


def _result_text_repr(result: Dict, depth: str) -> str:
    """Return a string representation of *result* suitable for MMR similarity."""
    parts = [
        str(result.get("source", "") or ""),
        str(result.get("text", "") or ""),
        str(result.get("heading", "") or ""),
    ]
    if depth in ("chunk", "document"):
        prov = result.get("provenance") or {}
        parts.append(str(prov.get("agent_origin", "") or ""))
    return " ".join(p for p in parts if p)


def _cosine_sim_bag_of_words(a: str, b: str) -> float:
    """
    Approximate cosine similarity between two strings using bag-of-words.

    This is a lightweight MMR diversity metric that avoids loading a full
    embedding model — appropriate for post-retrieval budget packing where
    the heavy model has already run.
    """
    def word_vec(text: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for w in text.lower().split():
            counts[w] = counts.get(w, 0) + 1
        return counts

    va = word_vec(a)
    vb = word_vec(b)
    if not va or not vb:
        return 0.0

    dot = sum(va.get(w, 0) * vb.get(w, 0) for w in va)
    norm_a = math.sqrt(sum(c * c for c in va.values()))
    norm_b = math.sqrt(sum(c * c for c in vb.values()))
    denom = norm_a * norm_b
    return dot / denom if denom > 0 else 0.0


def pack_results(
    results: List[Dict],
    budget_tokens: int,
    depth: str = "snippet",
    mmr_lambda: float = 0.6,
) -> List[Dict]:
    """
    Select a diverse, token-budgeted subset of *results* using MMR.

    Maximal Marginal Relevance (MMR) balances relevance (score) against
    diversity (dissimilarity to already-selected results) to avoid returning
    near-duplicate chunks that eat up the token budget.

    Args:
        results: Ranked list of result dicts (highest-score first).
        budget_tokens: Maximum total tokens to return.
        depth: Depth tier used to estimate per-result token cost.
        mmr_lambda: Trade-off weight. 1.0 = pure relevance (no diversity),
                    0.0 = pure diversity. Default 0.6 gives moderate diversity.

    Returns:
        A subset of *results* that fits within *budget_tokens*, ordered by
        MMR selection sequence (not necessarily original rank).

    Notes:
        - If *budget_tokens* <= 0, returns all results (no budget constraint).
        - The relevance score is taken from result["score"] (0–1 range).
        - Similarity is approximated by bag-of-words cosine; no embedding model
          is loaded here.
        - Results with token_count == 0 receive a floor estimate from depth tier.
    """
    if not results:
        return []

    if budget_tokens <= 0:
        return list(results)

    candidates = list(results)  # mutable working copy
    selected: List[Dict] = []
    selected_reprs: List[str] = []
    total_tokens = 0

    while candidates:
        best_idx: Optional[int] = None
        best_mmr: float = -float("inf")

        for i, cand in enumerate(candidates):
            relevance = float(cand.get("score", 0))
            cand_repr = _result_text_repr(cand, depth)

            if selected_reprs:
                max_sim = max(
                    _cosine_sim_bag_of_words(cand_repr, sel_repr)
                    for sel_repr in selected_reprs
                )
            else:
                max_sim = 0.0

            mmr_score = mmr_lambda * relevance - (1.0 - mmr_lambda) * max_sim

            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = i

        if best_idx is None:
            break

        chosen = candidates.pop(best_idx)
        cost = _result_tokens(chosen, depth)

        if total_tokens + cost > budget_tokens and selected:
            # Would exceed budget; stop adding (still include first result even
            # if it alone exceeds budget, to guarantee at least one result)
            break

        selected.append(chosen)
        selected_reprs.append(_result_text_repr(chosen, depth))
        total_tokens += cost

    return selected

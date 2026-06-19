"""The canonical, pinned cross-adapter tokenizer (§7.8).

The canonical tokenizer for ALL token counting is ``cl100k_base`` (tiktoken),
pinned by tiktoken version in :mod:`config`. It is open, deterministic, locally
runnable by any reviewer, and identical across adapters, so token cost is an
apples-to-apples comparison rather than a per-vendor count.

The harness — never the adapter — counts tokens (§3.1). The runner calls
:func:`count_tokens` on the returned ``context_string`` and enforces the budget.
"""

from __future__ import annotations

import functools

from .config import CANONICAL_TOKENIZER_ID


@functools.lru_cache(maxsize=1)
def _encoding():
    import tiktoken

    return tiktoken.get_encoding(CANONICAL_TOKENIZER_ID)


def encode(text: str) -> list[int]:
    """Encode text with the canonical tokenizer."""
    return _encoding().encode(text)


def count_tokens(text: str) -> int:
    """Authoritative harness token count for a context string (§3.1, §6.6)."""
    return len(encode(text))

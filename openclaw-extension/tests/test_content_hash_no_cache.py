"""X7 regression: sovrd._content_hash must NOT retain full text payloads.

The prior functools.lru_cache(maxsize=10000) keyed the cache on the raw text,
retaining up to 10k full (potentially sensitive) payloads for the process
lifetime. Hashing is cheap; the cache is removed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sovrd


def test_content_hash_has_no_lru_cache():
    # An lru_cache-wrapped function exposes cache_info / cache_clear; a plain
    # function does not.
    assert not hasattr(sovrd._content_hash, "cache_info")
    assert not hasattr(sovrd._content_hash, "cache_clear")


def test_content_hash_is_deterministic_and_normalized():
    assert sovrd._content_hash("Hello  World") == sovrd._content_hash("hello world")
    assert sovrd._content_hash("a") != sovrd._content_hash("b")
    # Known SHA-256 of the normalized single char "a".
    assert sovrd._content_hash("A") == (
        "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"
    )

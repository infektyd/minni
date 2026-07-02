"""Config sanity for slice s1 (§7.7, §7.14).

Asserts the pinned fields the spec names are present with the right types, the
embedder id matches Minni's engine, and credentials are env-var NAMES (never
values) — config.py reads no secret at import time.
"""

import os

from membench import config


def test_embedder_matches_minni_engine():
    # Minni's own embedding model id (engine/config.py: embedding_model).
    assert config.EMBEDDER_MODEL_ID == "all-MiniLM-L6-v2"
    assert config.EMBEDDING_DIM == 384


def test_canonical_tokenizer_pinned():
    assert config.CANONICAL_TOKENIZER_ID == "cl100k_base"


def test_named_pins_present_and_typed():
    assert isinstance(config.K, int) and config.K == 10
    assert isinstance(config.DEFAULT_MAX_TOKENS, int)
    assert isinstance(config.N, int)
    assert config.JUDGE_MIN_SUBSET_N == 40
    assert isinstance(config.MAX_API_CALLS, int)
    assert isinstance(config.CONTEXT_LOG_TRUNCATE, int)
    assert set(config.MIN_PER_BAND) == {
        "single-hop",
        "multi-hop",
        "contradiction",
        "recency-sensitive",
        "negatives",
        "poisoned",
    }
    assert config.DETERMINISM_EXCLUDED_FIELDS == frozenset(
        {"wall_clock_ms", "build_wall_clock_ms"}
    )


def test_credentials_are_env_var_names_not_values():
    for logical, env_name in config.CREDENTIAL_ENV_VARS.items():
        assert isinstance(env_name, str)
        assert env_name.isupper() or "_" in env_name
        # The config stores the NAME, and must not have read a value at import.
        # (A real value would not look like an env-var identifier.)
        assert " " not in env_name
        assert not env_name.startswith("sk-")
        assert not env_name.startswith("Bearer ")


def test_fixture_hash_pinned():
    assert len(config.FIXTURE_CORPUS_HASH) == 64
    int(config.FIXTURE_CORPUS_HASH, 16)  # valid hex

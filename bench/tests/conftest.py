"""Shared fixtures for the membench s1 test suite."""

import pytest

from membench import config
from membench.contract import TokenBudget
from membench.corpus import load_corpus

_FIXTURE_DIR = "membench/fixtures/corpus_synthetic"


def _fixture_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / _FIXTURE_DIR


@pytest.fixture
def fixture_dir():
    return _fixture_path()


@pytest.fixture
def corpus():
    """The pinned synthetic fixture corpus, loaded with hash verification."""
    return load_corpus(_fixture_path(), pinned_hash=config.FIXTURE_CORPUS_HASH)


@pytest.fixture
def budget():
    return TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)

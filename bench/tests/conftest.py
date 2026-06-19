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
    """The pinned synthetic fixture corpus, loaded with hash verification.

    Loaded with ``scrubbed=False``: the synthetic fixture is PUBLIC and contains
    no secrets, so it has no scrub-gated snapshot manifest to verify. Real-data
    runs (s2(b)) load a frozen, scrub-gated snapshot with ``scrubbed=True``.
    """
    return load_corpus(
        _fixture_path(), pinned_hash=config.FIXTURE_CORPUS_HASH, scrubbed=False
    )


@pytest.fixture
def budget():
    return TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)


@pytest.fixture(autouse=True)
def _reset_process_global_api_counters():
    """Reset every process-global LLM call counter before AND after each test.

    The agent/judge/llm_wiki API caps are PROCESS-GLOBAL (§7.15, fix 5): a stale
    count from one test could otherwise trip another's guard or mask a real one.
    Reset around every test so the global never leaks budget across cases.
    """
    from membench import agent as _agent
    from membench import judge as _judge
    from membench.adapters import llm_wiki as _llm_wiki

    _agent._reset_api_calls()
    _judge._reset_api_calls()
    _llm_wiki._reset_api_calls()
    yield
    _agent._reset_api_calls()
    _judge._reset_api_calls()
    _llm_wiki._reset_api_calls()

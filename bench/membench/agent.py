"""Layer-2 agent interface + offline StubAgent + (gated) real LLM agent (§3.3).

The Agent is handed the adapter's retrieved ``context`` plus the episode
``question`` and produces an ``answer``. It records ``tokens_to_model`` — the
FULL prompt token count (the canonical-tokenizer count of the shared system
prompt + the question + the wrapped retrieved context), so tokens-to-model
reflects everything the model actually consumed, not just the context (§3.3).

Two implementations:

* :class:`StubAgent` — DETERMINISTIC, OFFLINE. Answers correctly iff the gold
  fact substring is present in the provided context, else "I don't know". No
  randomness, no clock-dependent output: the same (context, question, gold_fact)
  always yields the same answer, so the Layer-2 tests are not flaky. Used by
  every test.

* :class:`LLMAgent` — the REAL Anthropic-model agent, gated by
  ``config.MAX_API_CALLS`` and an env API key. It is NEVER constructed or called
  in tests; ``answer()`` resolves the key from the environment and would call the
  real client only in a real run. The offline tests forbid this path.

Token counting is harness-owned (the canonical tokenizer), exactly as for the
Layer-1 context cost — the agent does not self-report a token count.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from . import config
from .layer2_prompt import build_agent_prompt
from .tokenizer import count_tokens

IDK = "I don't know"


# ── SHARED cumulative API-call budget (§7.15, review fix 4) ──────────────────
# MAX_API_CALLS is a PROCESS-WIDE cap on CUMULATIVE LLM calls across ALL roles —
# agent + judge + llm_wiki curation. A per-role counter (the old bug) let the run
# make up to 3*MAX_API_CALLS calls before any single counter aborted. The agent
# now reserves against the ONE shared counter in membench.api_budget, so the cap
# is a true combined ceiling. These thin wrappers preserve the module-local names
# the tests already use; both delegate to the shared budget.
from . import api_budget


def _reserve_api_call(max_api_calls: int) -> None:
    """Reserve one call on the SHARED cumulative budget (delegates, fix 4)."""
    api_budget.reserve(max_api_calls, role="agent")


def _reset_api_calls() -> None:
    """Reset the SHARED cumulative call counter (test-only helper)."""
    api_budget.reset()


@dataclass(frozen=True)
class AgentResult:
    """One agent turn's output (§3.3)."""

    answer: str
    # FULL prompt tokens the model consumed: system prompt + question + wrapped
    # context, counted with the canonical tokenizer. NOT just the context — the
    # §6.7 composite uses context-only tokens separately; this is the total.
    tokens_to_model: int


class Agent(Protocol):
    """The Layer-2 agent contract: (context, question) -> answer + token count."""

    name: str

    def answer(
        self, context: str, question: str, *, gold_fact: str, nonce: str | None = None
    ) -> AgentResult:
        ...


def _count_prompt_tokens(context: str, question: str, nonce: str | None) -> int:
    """Canonical-tokenizer count of the FULL prompt handed to the model (§3.3).

    Tokenizes the exact composed prompt (shared system prompt + nonce-wrapped
    untrusted context + question) so the count reflects everything the model
    consumes. Harness-owned — neither the adapter nor the agent self-reports it.
    The runner passes the run's nonce so the count is byte-reproducible.
    """
    system, user = build_agent_prompt(context, question, nonce=nonce)
    return count_tokens(system) + count_tokens(user)


class StubAgent:
    """Deterministic offline agent for tests (§3.3).

    Contract: answers with the gold answer iff ``gold_fact`` is a substring of
    the provided ``context`` (i.e. the memory system actually retrieved the
    needed fact); otherwise answers "I don't know". This makes task success a
    pure function of whether the adapter surfaced the fact — so the Layer-2
    distributions are driven by RETRIEVAL quality, exactly what membench tests,
    with zero nondeterminism.

    ``gold_fact`` is passed by the runner from the gold label; it is NOT read
    from the question (which would leak), and the no-leak episode guard
    (episodes.check_episode) ensures it is not in the question text anyway.
    """

    name = "stub_agent"

    def answer(
        self, context: str, question: str, *, gold_fact: str, nonce: str | None = None
    ) -> AgentResult:
        tokens = _count_prompt_tokens(context, question, nonce)
        # Banned role markers in the retrieved context are NEUTRALIZED by the
        # shared context-builder (a zero-width space is inserted inside any
        # ``ASSISTANT:``/``HUMAN:``/… so a benign transcript-style corpus doc is
        # preserved without forging a turn boundary — see
        # adapters/_shared.neutralize_banned_markers). The gold fact is matched
        # against the SAME neutralized text the model sees, so a gold fact that
        # legitimately contains a marker still matches after neutralization.
        from .adapters._shared import neutralize_banned_markers

        if gold_fact and neutralize_banned_markers(gold_fact) in neutralize_banned_markers(
            context
        ):
            # The stub "answers correctly" by asserting the gold fact verbatim.
            return AgentResult(answer=gold_fact, tokens_to_model=tokens)
        return AgentResult(answer=IDK, tokens_to_model=tokens)


class LLMAgent:
    """The REAL Anthropic-model agent (gated; NEVER called in tests) (§3.3, §7.15).

    Pinned to ``config.AGENT_MODEL`` (id + family). It enforces
    ``config.MAX_API_CALLS`` and resolves the API key from the environment by
    NAME (``config.CREDENTIAL_ENV_VARS['agent_api_key']``) at call time — never
    at import, never into any artifact (§7.14). The offline test-suite forbids
    this class: it is constructed only by a real run, and ``answer()`` raises if
    no key is present, so it cannot silently make a network call.
    """

    name = "llm_agent"

    def __init__(self, *, max_api_calls: int | None = None) -> None:
        self.model_id = config.AGENT_MODEL.model_id
        self.model_family = config.AGENT_MODEL.model_family
        self.max_api_calls = (
            config.MAX_API_CALLS if max_api_calls is None else max_api_calls
        )

    def _resolve_key(self) -> str:
        env_name = config.CREDENTIAL_ENV_VARS["agent_api_key"]
        key = os.environ.get(env_name)
        if not key:
            raise RuntimeError(
                f"agent API key env var {env_name!r} is unset — the real LLM "
                "agent cannot run (this path is never exercised offline)."
            )
        return key

    def answer(
        self, context: str, question: str, *, gold_fact: str, nonce: str | None = None
    ) -> AgentResult:
        # Reserve a slot on the PROCESS-GLOBAL counter BEFORE building the prompt,
        # so N agents sharing one budget cannot collectively exceed MAX_API_CALLS
        # (the per-instance counter was the bug, §7.15).
        _reserve_api_call(self.max_api_calls)
        # SECURITY (review fix): do NOT bind the API key to a named local in this
        # stub. ``_resolve_key()`` is deliberately NOT called here — it is
        # meaningless without the real network call, and any error-reporting
        # framework that captures locals on an exception (Sentry, cgitb, logging
        # with exc_info) would otherwise expose the plaintext key from this frame
        # at the NotImplementedError below. The key is resolved at call time ONLY
        # in the real implementation, immediately before the Anthropic client call
        # (wired in the run slice, never reached by any test).
        build_agent_prompt(context, question, nonce=nonce)
        raise NotImplementedError(
            "LLMAgent.answer is the gated live path; not implemented in s5 "
            "(offline-only). Use StubAgent in tests."
        )

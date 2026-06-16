"""Layer-2 runner — agent-in-the-loop, N-trial, variance (§3.3).

For each (adapter, episode):

1. Play the episode's sessions through the adapter (ingest/accumulate the session
   content as the corpus the adapter retrieves over).
2. At the FINAL question, RETRIEVE via the adapter (one ``query()``), hand the
   retrieved context to the Agent, and capture answer + tokens-to-model +
   wall_clock_ms.
3. Score correctness/task-success with the Judge.

Run ``config.N`` TRIALS per (adapter, episode) and aggregate per-adapter:
answer-correctness, task-success, mean tokens-to-model — each with the
VARIANCE / stddev ACROSS trials. Emit canonical JSON.

Inter-turn context policy (§3.3): each turn is a fresh session — only the final
question's freshly-retrieved context reaches the agent; no prior turn's content
is carried in. Here every episode's scored turn is its final question; the
multi-session character lives in the CORPUS the adapter retrieves over.

FULLY OFFLINE: the runner is given a StubAgent + StubJudge in tests. No real LLM
or network call is reachable from any test.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from . import config
from .agent import Agent
from .contract import MemoryAdapter, TokenBudget
from .episodes import Episode
from .judge import Judge, assert_judge_publishable
from .layer2_prompt import new_nonce
from .tokenizer import count_tokens


# ---------------------------------------------------------------------------
# Per-episode corpus: the adapter retrieves over the episode's session content.
#
# An episode is multi-session; the adapter ingests one doc per session (the
# "session 1 wrote it" framing — the fact lives in an earlier session's doc,
# §3.3). This is an in-memory FrozenCorpus surface (doc_ids/read) so any
# contract-conformant adapter can ingest + query it with no filesystem.
# ---------------------------------------------------------------------------
class _EpisodeCorpus:
    """An in-memory FrozenCorpus over one episode's sessions (one doc/session)."""

    def __init__(self, episode: Episode) -> None:
        self._docs: dict[str, str] = {
            f"{episode.id}/{s.session_id}.md": s.content for s in episode.sessions
        }
        self.content_hash = f"episode-{episode.id}"
        self.scrubbed = True  # synthetic fixture content; no secrets

    def doc_ids(self) -> list[str]:
        return sorted(self._docs)

    def read(self, doc_id: str) -> bytes:
        if doc_id not in self._docs:
            raise KeyError(doc_id)
        return self._docs[doc_id].encode("utf-8")


@dataclass
class TrialResult:
    """One (adapter, episode, trial) outcome."""

    adapter: str
    episode_id: str
    trial: int
    correct: int  # judge: 1 if the answer asserts the gold fact, else 0
    success: int  # task success for this episode turn (== correct here)
    tokens_to_model: int
    ctx_tokens: int  # canonical-tokenizer count of the retrieved context only
    wall_clock_ms: float
    answer: str


def _budget() -> TokenBudget:
    return TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)


def run_episode_trial(
    adapter: MemoryAdapter,
    episode: Episode,
    agent: Agent,
    judge: Judge,
    trial: int,
    *,
    nonce: str | None = None,
) -> TrialResult:
    """Play one episode through one adapter for one trial (§3.3).

    Ingests the episode's session content, retrieves for the final question,
    hands the context to the agent, and scores with the judge. Deterministic
    given deterministic adapter/agent/judge + a fixed nonce.
    """
    corpus = _EpisodeCorpus(episode)
    ingest_report = adapter.ingest(corpus)
    if ingest_report.doc_count != len(corpus.doc_ids()):
        raise RuntimeError(
            f"adapter {adapter.name!r} ingest doc_count="
            f"{ingest_report.doc_count} != {len(corpus.doc_ids())} sessions "
            "— aborting episode (§9.5)."
        )

    start = time.perf_counter()
    result = adapter.query(episode.question, _budget())
    wall_clock_ms = (time.perf_counter() - start) * 1000.0

    context = result.context_string
    # Context-only token count (§6.6) — for the §6.7 composite denominator.
    ctx_tokens = count_tokens(context)

    agent_out = agent.answer(
        context, episode.question, gold_fact=episode.gold_fact, nonce=nonce
    )
    correct = judge.score(agent_out.answer, episode.gold_fact)
    return TrialResult(
        adapter=adapter.name,
        episode_id=episode.id,
        trial=trial,
        correct=correct,
        success=correct,  # one-turn episode: task success == answer correctness
        tokens_to_model=agent_out.tokens_to_model,
        ctx_tokens=ctx_tokens,
        wall_clock_ms=wall_clock_ms,
        answer=agent_out.answer,
    )


def _stats(values: list[float]) -> dict[str, float]:
    """Mean, population variance, and stddev of a sample (variance across trials)."""
    n = len(values)
    if n == 0:
        return {"mean": 0.0, "variance": 0.0, "stddev": 0.0, "n": 0}
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return {
        "mean": mean,
        "variance": variance,
        "stddev": math.sqrt(variance),
        "n": n,
    }


@dataclass
class AdapterLayer2Result:
    """Per-adapter Layer-2 aggregate over N trials × all episodes (§3.3)."""

    adapter: str
    n_trials: int
    n_episodes: int
    trials: list[TrialResult] = field(default_factory=list)

    def block(self) -> dict:
        """Aggregate: correctness/task-success/tokens-to-model with variance."""
        correct = [float(t.correct) for t in self.trials]
        success = [float(t.success) for t in self.trials]
        ttm = [float(t.tokens_to_model) for t in self.trials]
        ctx = [float(t.ctx_tokens) for t in self.trials]
        return {
            "adapter": self.adapter,
            "n_trials": self.n_trials,
            "n_episodes": self.n_episodes,
            "n_observations": len(self.trials),
            "answer_correctness": _stats(correct),
            "task_success": _stats(success),
            "tokens_to_model": _stats(ttm),
            "ctx_tokens": _stats(ctx),
        }


def run_layer2(
    adapters: dict[str, MemoryAdapter],
    episodes: list[Episode],
    agent: Agent,
    judge: Judge,
    *,
    n_trials: int = config.N,
    fixed_nonce: str | None = None,
) -> dict[str, AdapterLayer2Result]:
    """Run Layer 2 for every adapter over every episode, ``n_trials`` per pair.

    Returns per-adapter results (with the full trial list). ``fixed_nonce`` makes
    the composed prompts byte-reproducible across runs (tests); omit it for a
    fresh per-run nonce. Fully offline with StubAgent/StubJudge.
    """
    out: dict[str, AdapterLayer2Result] = {}
    for name, adapter in sorted(adapters.items()):
        agg = AdapterLayer2Result(
            adapter=name, n_trials=n_trials, n_episodes=len(episodes)
        )
        # try/finally guarantees teardown() even if a trial raises (e.g. the §9.5
        # ingest-doc_count abort), so a real adapter holding fs locks / handles /
        # connections never leaks them across adapters.
        try:
            for episode in episodes:
                for trial in range(n_trials):
                    nonce = fixed_nonce if fixed_nonce is not None else new_nonce()
                    agg.trials.append(
                        run_episode_trial(
                            adapter, episode, agent, judge, trial, nonce=nonce
                        )
                    )
                # NOTE: per-episode state cannot leak — run_episode_trial
                # re-ingests a fresh _EpisodeCorpus at the start of every trial.
                # Per the MemoryAdapter.ingest CONTRACT (contract.py), ingest
                # REPLACES the index (never accumulates), so episode N's results
                # are never contaminated by episode N-1's sessions. We tear the
                # adapter down ONCE after all episodes (in finally), not
                # per-episode, so a subsequent re-ingest never hits a torn-down
                # adapter.
        finally:
            adapter.teardown()
        out[name] = agg
    return out


def results_to_dict(
    results: dict[str, AdapterLayer2Result],
    *,
    calibration: "object | None" = None,
) -> dict:
    """Build the canonical Layer-2 results artifact (sorted, deterministic).

    The Layer-2 artifact carries JUDGE-SCORED numbers (answer_correctness /
    task_success). Per the load-bearing gate (§3.3 / §9.6) those numbers may NOT
    be published unless the judge cleared calibration. When ``calibration`` is
    provided it MUST be a :class:`membench.judge.CalibrationResult` whose METRICS
    independently clear the thresholds — the ``passed`` flag is NOT trusted, so a
    forged ``CalibrationResult(passed=True, cohen_kappa=0.0, ...)`` cannot bypass
    the gate. ``calibration`` is only optional so unit tests that assert structure
    can opt out explicitly.
    """
    if calibration is not None:
        from .judge import (
            JudgeGateError,
            CalibrationResult,
            _CALIBRATION_MIN_AGREEMENT,
            _CALIBRATION_MIN_KAPPA,
        )
        from . import config as _config

        # Re-validate the METRICS directly; never trust a hand-set ``passed``
        # flag. The dataclass is frozen but its constructor is public, so a
        # caller can fabricate CalibrationResult(passed=True, kappa=0.0); the
        # only safe gate re-checks n, agreement, and kappa here (§3.3/§9.6).
        if not isinstance(calibration, CalibrationResult):
            raise JudgeGateError(
                "calibration must be a CalibrationResult (§3.3/§9.6)"
            )
        if (
            calibration.n < _config.JUDGE_MIN_SUBSET_N
            or calibration.raw_agreement < _CALIBRATION_MIN_AGREEMENT
            or calibration.cohen_kappa < _CALIBRATION_MIN_KAPPA
            or not calibration.passed
        ):
            raise JudgeGateError(
                "refusing to build Layer-2 artifact: the supplied calibration "
                "did NOT pass the gate (re-checked n>=%d, agreement>=%.2f, "
                "kappa>=%.2f; passed flag is not trusted) — judge numbers may "
                "not be published (§3.3/§9.6)"
                % (
                    _config.JUDGE_MIN_SUBSET_N,
                    _CALIBRATION_MIN_AGREEMENT,
                    _CALIBRATION_MIN_KAPPA,
                )
            )
    n_trials = (
        next(iter(results.values())).n_trials if results else config.N
    )
    return {
        "layer": 2,
        "n_trials": n_trials,
        "agent_model": {
            "model_id": config.AGENT_MODEL.model_id,
            "model_family": config.AGENT_MODEL.model_family,
        },
        "judge_model": {
            "model_id": config.JUDGE_MODEL.model_id,
            "model_family": config.JUDGE_MODEL.model_family,
        },
        "adapters": {
            name: res.block() for name, res in sorted(results.items())
        },
    }


def publish_layer2_results(
    results: dict[str, AdapterLayer2Result],
    human_labels: list[int],
    judge_labels: list[int],
) -> dict:
    """Build the Layer-2 artifact ONLY after the calibration gate clears (§3.3).

    Runs :func:`assert_judge_publishable` (min-n, no-self-judge, raw-agreement,
    kappa) over the human-checked paired subset; any failure raises before any
    judge number is emitted. This is the load-bearing wiring between the gate and
    the output path: a gate-failing judge cannot produce a results artifact.
    """
    calibration = assert_judge_publishable(human_labels, judge_labels)
    return results_to_dict(results, calibration=calibration)


def canonical_json(
    results: dict[str, AdapterLayer2Result],
    *,
    calibration: "object | None" = None,
) -> str:
    """Canonical JSON for the Layer-2 results (sorted keys)."""
    return json.dumps(
        results_to_dict(results, calibration=calibration),
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    )

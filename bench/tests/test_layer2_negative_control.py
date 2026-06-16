"""Layer-2 NEGATIVE-CONTROL self-test (fix 1b) — retrieval selectivity must bite.

The headline fairness fix (fix 1): the public episode fixtures now co-ingest a
large pool of distractor sessions, so each episode has ~31 candidate session docs
of which only ONE carries the gold fact. The single establishing fact must
actually be RETRIEVED and RANKED into the budgeted top-K to land in context.

MECHANISM (corrected, review fix 1a). This is NOT a "budget pressure" control. A
top-K pick of K=10 docs is ~1750 tokens and fits the 2048-token budget fine, so
``sanity_random`` does NOT fail by being excluded from the budget. It fails by
RETRIEVAL SELECTIVITY over a LARGER CANDIDATE POOL: it ranks docs by
``sha256(question || doc_id)``, which is independent of relevance, so it surfaces
the gold session into its top-K only ~K/num_docs ≈ 10/31 ≈ 0.32 of the time in
expectation — and on the actual fixture the gold-fact SUBSTRING lands in the
budgeted top-K context even more rarely (the answer substring is not always in
the establishing session — e.g. the correction episodes), so the observed
sanity_random task-success is ≈0. Real retrieval adapters (naive_rag,
markdown_grep) rank the gold session ABOVE the noise, so they score ≈1.

ROBUSTNESS (review fix 1b). The control is NOT validated on a single lucky nonce.
``test_sanity_random_stays_low_across_seeds`` runs sanity_random over MULTIPLE
nonces and asserts its MEAN task-success stays comfortably below the expected
random ceiling ~K/num_docs, and the real adapters beat it by a wide margin that
holds across seeds. The expected random rate is stated in the assertion.

Fully offline: StubAgent (correct iff gold_fact in context) + StubJudge.
"""

import pytest

from membench import config
from membench.adapters.markdown_grep import MarkdownGrepAdapter
from membench.adapters.naive_rag import NaiveRagAdapter
from membench.adapters.sanity_random import SanityRandomAdapter
from membench.adapters.stub import StubAdapter
from membench.agent import StubAgent
from membench.episodes import load_fixture_episodes
from membench.judge import StubJudge
from membench.runner_layer2 import _EpisodeCorpus, _budget, run_layer2
from membench.tokenizer import count_tokens

_FIXED_NONCE = "deadbeef"


@pytest.fixture(scope="module")
def episodes():
    return load_fixture_episodes()


def test_fixture_has_at_least_six_episodes(episodes):
    # >= 6 fixture episodes for tests (the real run uses >= 15). (fix 1c)
    assert len(episodes) >= 6


def test_every_episode_has_a_large_candidate_pool(episodes):
    # Candidate-pool precondition (fix 1a, corrected mechanism). The negative
    # control works by RETRIEVAL SELECTIVITY over a large candidate pool, not by
    # budget exclusion. What must hold is that there are MANY candidate sessions
    # per episode (so a random top-K pick rarely hits the single gold session) —
    # i.e. num_docs >> K. We assert each episode has at least ~3x K candidate
    # sessions; the total token count merely confirms the distractors are non-
    # trivial prose (it is NOT the mechanism — a K=10 pick fits the budget fine).
    budget = config.DEFAULT_MAX_TOKENS
    for ep in episodes:
        num_docs = len(ep.sessions)
        assert num_docs >= 3 * config.K, (
            f"episode {ep.id} has only {num_docs} candidate sessions "
            f"(K={config.K}); the candidate pool is too small for the random "
            "control to be selective — fix 1 regressed."
        )
        total = sum(count_tokens(s.content) for s in ep.sessions)
        assert total > budget, (
            f"episode {ep.id} corpus is only {total} tokens (budget={budget}); "
            "distractor prose went missing — fix 1 regressed."
        )


def _task_success(factory, episodes, *, nonce=_FIXED_NONCE):
    adapters = {factory().name: factory()}
    results = run_layer2(
        adapters, episodes, StubAgent(), StubJudge(),
        n_trials=config.N, fixed_nonce=nonce,
    )
    (res,) = results.values()
    return res.block()["task_success"]["point"]


def _task_success_block(factory, episodes):
    adapters = {factory().name: factory()}
    results = run_layer2(
        adapters, episodes, StubAgent(), StubJudge(),
        n_trials=config.N, fixed_nonce=_FIXED_NONCE,
    )
    (res,) = results.values()
    return res.block()["task_success"]


# Multiple independent nonces — different question-salt -> different
# sha256(question||doc_id) orderings in sanity_random, so the random top-K pick
# changes across seeds. Asserting over ALL of them makes the control robust, not
# dependent on one lucky seed landing exactly 0.0 (review fix 1b).
_NONCES = (
    "deadbeef", "cafe1234", "0badf00d", "feedface",
    "12345678", "99999999", "abcdef01", "55aa55aa",
)

# Expected random hit ceiling. sanity_random surfaces the single gold session in
# its top-K with probability ~ K/num_docs. With K=10 and ~31 candidate docs that
# is ~0.32; the OBSERVED task-success is lower still because the gold-fact
# substring is not always in the establishing session (correction episodes). We
# assert the MEAN sanity_random rate across seeds stays well under this ceiling.
_EXPECTED_RANDOM_RATE = config.K / 31.0  # ~0.323


def test_sanity_random_is_deterministic_per_nonce(episodes):
    """Per-nonce determinism: zero within-episode (between-trial) variance.

    For a FIXED nonce the question is identical across all N trials, so the SAME
    top-K docs are selected every trial — each per-episode rate is a hard 0.0/1.0
    and the within-episode variance is EXACTLY 0.0. This documents that the
    cross-seed spread measured below comes from the SEED, not from trial flakiness.
    """
    block = _task_success_block(SanityRandomAdapter, episodes)
    rel = block["between_trial_reliability"]
    assert rel["mean_within_episode_variance"] == 0.0
    assert rel["max_within_episode_variance"] == 0.0
    assert 0.0 <= block["point"] <= 1.0


def test_sanity_random_stays_low_across_seeds(episodes):
    """Robust negative control: sanity_random << real retrievers across SEEDS.

    Runs sanity_random over MULTIPLE nonces and checks its MEAN task-success
    stays comfortably below the expected random ceiling ~K/num_docs (≈0.32), so
    the control does not rely on one lucky seed giving exactly 0.0. The real
    retrieval adapters (deterministic w.r.t. relevance, nonce-independent) must
    beat the per-seed random rate by a wide margin for EVERY seed.
    """
    random_rates = [
        _task_success(SanityRandomAdapter, episodes, nonce=n) for n in _NONCES
    ]
    mean_random = sum(random_rates) / len(random_rates)
    max_random = max(random_rates)

    # The mean random rate must sit clearly UNDER the K/num_docs ceiling. Using
    # half the ceiling as the bound leaves margin while pinning the expected rate
    # in the assertion message.
    assert mean_random <= 0.5 * _EXPECTED_RANDOM_RATE, (
        f"sanity_random mean task-success {mean_random:.3f} over {len(_NONCES)} "
        f"seeds exceeds half the expected random ceiling "
        f"(K/num_docs≈{_EXPECTED_RANDOM_RATE:.3f}); the negative control passes "
        "too easily — fix 1b regressed."
    )
    # No single seed may even approach the real adapters.
    assert max_random <= _EXPECTED_RANDOM_RATE, (
        f"a sanity_random seed scored {max_random:.3f}, at/above the expected "
        f"random ceiling K/num_docs≈{_EXPECTED_RANDOM_RATE:.3f}."
    )

    # Real retrievers are relevance-deterministic; they beat random on EVERY seed.
    real = {
        "naive_rag": _task_success(NaiveRagAdapter, episodes),
        "markdown_grep": _task_success(MarkdownGrepAdapter, episodes),
        "stub": _task_success(StubAdapter, episodes),
    }
    for name, score in real.items():
        for nonce, rrate in zip(_NONCES, random_rates):
            assert score - rrate >= 0.5, (
                f"{name}={score:.3f} vs sanity_random[{nonce}]={rrate:.3f}: gap "
                "too small — retrieval selectivity does not bite (fix 1b)."
            )


def test_threshold_adapters_do_not_falsely_refuse_positives(episodes):
    """Threshold-refusal adapters must NOT false-refuse the POSITIVE questions.

    IMPORTANT (review fix 6). naive_rag / markdown_grep can THRESHOLD-REFUSE when
    their own top retrieval score is below tau. If tau fired on an episode's
    POSITIVE establishing question (because the distractor noise diluted the
    embedding similarity), the adapter would return refused=True/empty on a
    POSITIVE -> the stub agent answers IDK -> task_success 0. That would corrupt
    the negative-control comparison: it would be measuring FALSE-REFUSAL, not
    retrieval. So we assert a ~0 false-refusal rate on the distractor episodes'
    positive questions. tau (config.REFUSAL_SCORE_THRESHOLD /
    LEXICAL_REFUSAL_MIN_HITS) is tuned to fire on clear negatives but NOT here.
    """
    for factory in (NaiveRagAdapter, MarkdownGrepAdapter):
        refusals = 0
        for ep in episodes:
            adapter = factory()
            corpus = _EpisodeCorpus(ep)
            adapter.ingest(corpus)
            result = adapter.query(ep.question, _budget())
            adapter.teardown()
            if result.refused:
                refusals += 1
        rate = refusals / len(episodes)
        assert rate == 0.0, (
            f"{factory().name} false-refused {refusals}/{len(episodes)} POSITIVE "
            f"establishing questions (rate={rate:.3f}); tau is mis-tuned and is "
            "corrupting the negative control — retune REFUSAL_SCORE_THRESHOLD / "
            "LEXICAL_REFUSAL_MIN_HITS (fix 6)."
        )

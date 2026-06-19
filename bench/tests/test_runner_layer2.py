"""Layer-2 runner: agent-in-the-loop, N-trial, variance — fully OFFLINE (§3.3)."""

import hashlib

import pytest

from membench import config
from membench.adapters.stub import MiscountStubAdapter, StubAdapter
from membench.agent import IDK, AgentResult, LLMAgent, StubAgent
from membench.contract import IngestReport, QueryResult, RankedDoc, TokenBudget
from membench.episodes import load_fixture_episodes
from membench.judge import (
    JudgeGateError,
    LLMJudge,
    StubJudge,
    judge_gate_fixture_path,
    load_paired_judgments,
)
from membench.layer2_prompt import build_agent_prompt, wrap_context
from membench.runner_layer2 import (
    AdapterLayer2Result,
    TrialResult,
    _EpisodeCorpus,
    canonical_json,
    publish_layer2_results,
    results_to_dict,
    run_episode_trial,
    run_layer2,
    significance_block,
)
from membench.tokenizer import count_tokens

_FIXED_NONCE = "0" * 32


# ---------------------------------------------------------------------------
# A deliberately blind adapter that NEVER returns the fact — proves task-success
# is driven by retrieval, and gives a contrasting per-adapter distribution.
# ---------------------------------------------------------------------------
class BlindAdapter(StubAdapter):
    name = "blind"

    def query(self, q, budget):
        # Returns empty context: the agent then answers "I don't know".
        return QueryResult(
            ranked_results=[], context_string="", wall_clock_ms=0.1, refused=False
        )


# ---------------------------------------------------------------------------
# A SEEDED stochastic adapter: per trial it either returns the fact or not, by a
# deterministic hash of (episode_id, trial_index). This produces a NON-ZERO,
# hand-checkable variance across trials so we can prove N is actually varied and
# variance is computed correctly — without any real randomness/flakiness.
#
# The hit/miss pattern is keyed on (episode_id, trial_index) where trial_index
# is a per-EPISODE counter that resets to 0 whenever a NEW episode is ingested
# (detected by a change in the corpus content_hash, which is "episode-<id>").
# Because the runner re-ingests the fresh per-episode corpus before each trial,
# the counter advances once per trial within an episode and resets at the next
# episode. The pattern is therefore STABLE under fixture reordering / additions:
# episode E's trial T always hashes the same key regardless of how many episodes
# ran before it. (Contrast: a single monotonic global call-counter would shift
# the whole pattern whenever fixtures change.)
# ---------------------------------------------------------------------------
class SeededFlakyAdapter:
    name = "seeded_flaky"

    def __init__(self):
        self.config_hash = "seeded-v1"
        self._docs = {}
        self._episode_id = None
        self._trial_index = 0

    def ingest(self, corpus) -> IngestReport:
        self._docs = {d: corpus.read(d).decode("utf-8") for d in corpus.doc_ids()}
        # Derive the episode id from the corpus content_hash ("episode-<id>").
        episode_id = getattr(corpus, "content_hash", "?")
        # Reset the per-episode trial counter on a NEW episode so the hit/miss
        # key is (episode_id, trial_index-within-episode) — stable across fixture
        # reordering. Re-ingest of the SAME episode (the next trial) advances it.
        if episode_id != self._episode_id:
            self._episode_id = episode_id
            self._trial_index = 0
        return IngestReport(build_wall_clock_ms=0.0, doc_count=len(self._docs))

    def query(self, q, budget):
        # Deterministic per (episode, trial_index): hit on even hash, miss on odd.
        key = f"{self._episode_id}:{self._trial_index}".encode()
        self._trial_index += 1
        hit = hashlib.sha256(key).digest()[0] % 2 == 0
        if hit:
            ctx = "\n\n".join(self._docs.values())
        else:
            ctx = ""  # miss
        return QueryResult(
            ranked_results=[], context_string=ctx, wall_clock_ms=0.1, refused=False
        )

    def teardown(self):
        self._docs = {}


@pytest.fixture
def episodes():
    return load_fixture_episodes()


# ── StubAgent determinism + token counting ───────────────────────────────────
def test_stub_agent_answers_iff_fact_in_context():
    a = StubAgent()
    r1 = a.answer("... thirty seconds ...", "How long?", gold_fact="thirty seconds")
    assert r1.answer == "thirty seconds"
    r2 = a.answer("nothing relevant", "How long?", gold_fact="thirty seconds")
    assert r2.answer == IDK
    assert r1.tokens_to_model > 0 and r2.tokens_to_model > 0


def test_tokens_to_model_counts_full_prompt():
    a = StubAgent()
    ctx = "thirty seconds is the seal timeout"
    q = "What is the seal timeout?"
    r = a.answer(ctx, q, gold_fact="thirty seconds", nonce=_FIXED_NONCE)
    system, user = build_agent_prompt(ctx, q, nonce=_FIXED_NONCE)
    expected = count_tokens(system) + count_tokens(user)
    # Counts the FULL prompt (system + question + wrapped context), not just ctx.
    assert r.tokens_to_model > count_tokens(ctx)
    assert r.tokens_to_model == expected
    assert r.tokens_to_model <= config.DEFAULT_MAX_TOKENS


# ── Nonce validation (prompt-injection boundary defense, §3.1) ───────────────
@pytest.mark.parametrize(
    "bad_nonce",
    [
        '0" onload="x',          # double-quote breaks out of id="{nonce}"
        '"><script>',            # tag-forging attempt
        "ABCDEF",                # uppercase hex is not the token_hex output
        "deadbeef ",             # trailing space
        "",                      # empty
        "g00d",                  # non-hex letter
    ],
)
def test_build_agent_prompt_rejects_non_hex_nonce(bad_nonce):
    # A caller-supplied nonce that is not lowercase hex could break the
    # id="{nonce}" boundary the prompt-injection defense relies on -> ValueError.
    with pytest.raises(ValueError, match="hex"):
        build_agent_prompt("ctx", "q?", nonce=bad_nonce)


def test_wrap_context_rejects_non_hex_nonce():
    # Both entry points share the guard: wrap_context rejects a double-quote nonce.
    with pytest.raises(ValueError, match="hex"):
        wrap_context("ctx", '0" id="forged')


def test_build_agent_prompt_accepts_valid_hex_nonce():
    # The legitimate token_hex-style nonce is accepted (no false rejection).
    system, user = build_agent_prompt("ctx", "q?", nonce=_FIXED_NONCE)
    assert f'id="{_FIXED_NONCE}"' in user


# ── End-to-end Layer-2 over fixture episodes ─────────────────────────────────
def test_run_layer2_correctness_and_zero_variance_for_deterministic_stub(episodes):
    adapters = {"stub": StubAdapter(), "blind": BlindAdapter()}
    results = run_layer2(
        adapters, episodes, StubAgent(), StubJudge(), n_trials=config.N,
        fixed_nonce=_FIXED_NONCE,
    )
    n_obs = config.N * len(episodes)

    stub = results["stub"].block()
    blind = results["blind"].block()

    # N actually varied: one observation per trial per episode (raw flattened).
    assert stub["n_observations"] == n_obs
    assert stub["flattened_observations"]["task_success"]["n"] == n_obs

    # The lexical StubAdapter retrieves the fact for every fixture episode ->
    # task success is 1.0 everywhere. Per-episode-aggregated point == 1.0, with
    # BOTH between-trial reliability AND per-episode dispersion at 0 (fix 2).
    assert stub["task_success"]["point"] == 1.0
    assert stub["task_success"]["per_episode_dispersion"]["variance"] == 0.0
    assert (
        stub["task_success"]["between_trial_reliability"][
            "mean_within_episode_variance"
        ]
        == 0.0
    )
    assert stub["answer_correctness"]["per_episode_dispersion"]["variance"] == 0.0
    # The blind adapter never returns the fact -> 0 success, 0 dispersion.
    assert blind["task_success"]["point"] == 0.0
    assert blind["task_success"]["per_episode_dispersion"]["variance"] == 0.0

    # Deterministic stubs -> the N TRIALS OF A GIVEN EPISODE are identical
    # (non-flaky). The OVERALL tokens_to_model variance is non-zero only because
    # episodes differ in length — that is expected, not flakiness. Assert
    # per-episode-across-trials determinism directly.
    by_ep: dict[str, list[int]] = {}
    for t in results["stub"].trials:
        by_ep.setdefault(t.episode_id, []).append(t.tokens_to_model)
    for ep_id, ttms in by_ep.items():
        assert len(ttms) == config.N
        assert len(set(ttms)) == 1, f"episode {ep_id} flaky across trials"

    # tokens-to-model counted and within budget.
    assert stub["tokens_to_model"]["point"] > 0
    for t in results["stub"].trials:
        assert t.tokens_to_model <= config.DEFAULT_MAX_TOKENS


def test_variance_is_nonzero_when_trials_actually_differ(episodes):
    # The seeded flaky adapter hits/misses per call, so task success varies across
    # trials -> non-zero variance, proving N is varied and variance is real.
    adapters = {"seeded_flaky": SeededFlakyAdapter()}
    results = run_layer2(
        adapters, episodes, StubAgent(), StubJudge(), n_trials=config.N,
        fixed_nonce=_FIXED_NONCE,
    )
    res = results["seeded_flaky"]
    block = res.block()
    # Hand-compute the per-episode aggregation (fix 2) and compare to the block.
    per_ep = res.per_episode_success_rates()  # {episode: mean over N trials}
    ep_means = [per_ep[e] for e in sorted(per_ep)]
    point = sum(ep_means) / len(ep_means)
    disp = sum((m - point) ** 2 for m in ep_means) / len(ep_means)
    assert block["task_success"]["point"] == pytest.approx(point)
    assert block["task_success"]["per_episode_dispersion"]["variance"] == pytest.approx(disp)
    # The raw flattened variance is reported SEPARATELY and explicitly labeled.
    succ = [float(t.success) for t in res.trials]
    flat_mean = sum(succ) / len(succ)
    flat_var = sum((v - flat_mean) ** 2 for v in succ) / len(succ)
    assert block["flattened_observations"]["task_success"]["variance"] == pytest.approx(flat_var)
    # The flaky adapter hits AND misses -> between-trial reliability is NON-zero
    # (trials within an episode actually differ), which is the variance the OLD
    # code mislabeled as "across trials" — now correctly its OWN field (fix 2).
    assert block["task_success"]["between_trial_reliability"]["mean_within_episode_variance"] > 0.0
    assert 0.0 < block["task_success"]["point"] < 1.0


def _trial(adapter, ep, trial, success):
    # Minimal hand-crafted TrialResult; only adapter/episode/trial/success/correct
    # matter for the per-episode aggregation under test.
    return TrialResult(
        adapter=adapter, episode_id=ep, trial=trial, correct=success,
        success=success, tokens_to_model=10, ctx_tokens=5, wall_clock_ms=0.1,
        answer="x",
    )


def test_per_episode_rates_are_per_episode_under_uneven_trial_counts():
    # Review fix 8: per_episode_success_rates() must aggregate PER EPISODE, not
    # flatten — and be correct even if episodes carry DIFFERENT trial counts.
    # ep1 has 2 trials (1 hit, 1 miss -> 0.5); ep2 has 3 trials (all hit -> 1.0).
    # A flattened mean would be 3/5 = 0.6 and is WRONG as a per-episode statistic.
    trials = [
        _trial("a", "ep1", 0, 1),
        _trial("a", "ep1", 1, 0),
        _trial("a", "ep2", 0, 1),
        _trial("a", "ep2", 1, 1),
        _trial("a", "ep2", 2, 1),
    ]
    res = AdapterLayer2Result(adapter="a", n_trials=3, n_episodes=2, trials=trials)
    rates = res.per_episode_success_rates()
    assert rates == {"ep1": pytest.approx(0.5), "ep2": pytest.approx(1.0)}
    # The headline point is the mean OF the per-episode means (each episode weighted
    # equally regardless of its trial count): (0.5 + 1.0) / 2 = 0.75, NOT 0.6.
    block = res.block()
    assert block["task_success"]["point"] == pytest.approx(0.75)
    # ep1's two differing trials -> nonzero within-episode (between-trial) variance.
    assert block["task_success"]["between_trial_reliability"][
        "mean_within_episode_variance"
    ] > 0.0
    # Review fix: uneven trial counts must be VISIBLE in the artifact, not hidden
    # behind the first sorted episode's scalar. {min,max} exposes ep1=2, ep2=3.
    assert block["task_success"]["n_trials_per_episode"] == {"min": 2, "max": 3}


def test_significance_block_end_to_end_winner_is_correct():
    # Review fix 5: the existing runner tests only assert the significance block's
    # KEYS exist. This drives the FULL wiring — per_episode_success_rates ->
    # significance_block -> pairwise content — with a KNOWN difference, and asserts
    # winner / significant / n_episodes / means, not just key presence.
    # "good" succeeds on every trial of 8 episodes; "bad" fails on every one.
    good = [_trial("good", f"ep{e}", t, 1) for e in range(8) for t in range(3)]
    bad = [_trial("bad", f"ep{e}", t, 0) for e in range(8) for t in range(3)]
    results = {
        "good": AdapterLayer2Result("good", 3, 8, good),
        "bad": AdapterLayer2Result("bad", 3, 8, bad),
    }
    block = significance_block(results, q=0.05)
    assert block["test"].startswith("wilcoxon")
    pairwise = block["pairwise"]
    assert len(pairwise) == 1
    p = pairwise[0]
    assert {p["adapter_a"], p["adapter_b"]} == {"good", "bad"}
    assert p["n_episodes"] == 8
    assert p["winner"] == "good"
    assert p["significant"] is True
    # Review fix: ground the p-value end-to-end, not just "significant is True".
    # 8 episodes, every per-episode diff = (1.0 - 0.0) = +1.0 -> all same sign,
    # so the exact two-sided Wilcoxon p is 2/2**8 = 0.0078125. With a single
    # comparison in the family, BH-FDR leaves p_adjusted == p_value.
    assert p["p_value"] == pytest.approx(2 / 2 ** 8)
    assert p["p_adjusted"] == pytest.approx(2 / 2 ** 8)
    # Means are the per-episode success rates (good=1.0, bad=0.0).
    means = {p["adapter_a"]: p["mean_a"], p["adapter_b"]: p["mean_b"]}
    assert means["good"] == pytest.approx(1.0)
    assert means["bad"] == pytest.approx(0.0)
    # CIs are reported per adapter.
    assert block["task_success_ci95"]["good"]["mean"] == pytest.approx(1.0)
    assert block["task_success_ci95"]["bad"]["mean"] == pytest.approx(0.0)


def test_significance_block_tie_yields_no_winner():
    # Symmetric negative: two adapters tied on every episode -> not significant,
    # empty winner, p_value 1.0. Proves the wiring does not fabricate a winner.
    a = [_trial("a", f"ep{e}", t, 1) for e in range(5) for t in range(2)]
    b = [_trial("b", f"ep{e}", t, 1) for e in range(5) for t in range(2)]
    results = {
        "a": AdapterLayer2Result("a", 2, 5, a),
        "b": AdapterLayer2Result("b", 2, 5, b),
    }
    p = significance_block(results)["pairwise"][0]
    assert p["n_episodes"] == 5
    assert p["significant"] is False
    assert p["winner"] == ""
    assert p["p_value"] == 1.0


def test_run_layer2_is_reproducible(episodes):
    a1 = {"stub": StubAdapter()}
    a2 = {"stub": StubAdapter()}
    r1 = run_layer2(a1, episodes, StubAgent(), StubJudge(), n_trials=3,
                    fixed_nonce=_FIXED_NONCE)
    r2 = run_layer2(a2, episodes, StubAgent(), StubJudge(), n_trials=3,
                    fixed_nonce=_FIXED_NONCE)
    assert canonical_json(r1) == canonical_json(r2)


def test_results_dict_pins_models(episodes):
    results = run_layer2({"stub": StubAdapter()}, episodes, StubAgent(),
                         StubJudge(), n_trials=2, fixed_nonce=_FIXED_NONCE)
    d = results_to_dict(results)
    assert d["agent_model"]["model_family"] == config.AGENT_MODEL.model_family
    assert d["judge_model"]["model_family"] == config.JUDGE_MODEL.model_family
    assert d["agent_model"]["model_family"] != d["judge_model"]["model_family"]
    # The artifact must report the ACTUAL n_trials used, not config.N.
    assert d["n_trials"] == 2
    assert config.N != 2  # guard: the test is meaningful only if these differ


def test_results_dict_n_trials_tracks_actual_value(episodes):
    # Vary n_trials and confirm the top-level field follows the argument, never
    # config.N, and stays consistent with each adapter block.
    for n in (1, 3):
        results = run_layer2({"stub": StubAdapter()}, episodes, StubAgent(),
                             StubJudge(), n_trials=n, fixed_nonce=_FIXED_NONCE)
        d = results_to_dict(results)
        assert d["n_trials"] == n
        assert d["adapters"]["stub"]["n_trials"] == n


# ── ctx_tokens (context-only token count) is real, not silently zero ─────────
def test_ctx_tokens_counted_for_nonempty_context(episodes):
    # The retrieving StubAdapter yields a non-empty context: ctx_tokens > 0 and
    # strictly LESS than the full prompt's tokens_to_model (context-only vs whole
    # prompt). The BlindAdapter yields empty context -> ctx_tokens == 0.
    adapters = {"stub": StubAdapter(), "blind": BlindAdapter()}
    results = run_layer2(adapters, episodes, StubAgent(), StubJudge(),
                         n_trials=1, fixed_nonce=_FIXED_NONCE)
    for t in results["stub"].trials:
        assert t.ctx_tokens > 0
        assert t.ctx_tokens < t.tokens_to_model  # context-only < full prompt
    for t in results["blind"].trials:
        assert t.ctx_tokens == 0
    assert results["stub"].block()["ctx_tokens"]["point"] > 0
    assert results["blind"].block()["ctx_tokens"]["point"] == 0.0


# ── §9.5 abort: adapter ingest doc_count must match the corpus ───────────────
def test_run_episode_trial_aborts_on_ingest_doc_count_mismatch(episodes):
    with pytest.raises(RuntimeError, match="ingest doc_count"):
        run_episode_trial(
            MiscountStubAdapter(), episodes[0], StubAgent(), StubJudge(),
            trial=0, nonce=_FIXED_NONCE,
        )


# ── run_layer2 try/finally: teardown() runs even when a trial raises ─────────
class TeardownSpyMiscountAdapter(MiscountStubAdapter):
    """Miscount adapter (raises mid-trial) that records its teardown() calls.

    Exercises the run_layer2 try/finally: a trial that raises must NOT leak the
    adapter — teardown() is still called exactly once.
    """

    name = "teardown_spy_miscount"

    def __init__(self):
        super().__init__()
        self.teardown_calls = 0
        self.ingest_count = 0
        self.teardown_after_ingest = False

    def ingest(self, corpus):
        self.ingest_count += 1
        # MiscountStubAdapter.ingest succeeds; the runner raises AFTER it on the
        # doc_count mismatch, so the trial never completes.
        return super().ingest(corpus)

    def teardown(self):
        # Record that teardown fired AFTER an ingest with no completed trial in
        # between — i.e. on the try/finally raise path, not on normal completion.
        self.teardown_after_ingest = self.ingest_count > 0
        self.teardown_calls += 1
        super().teardown()


class TeardownSpyStubAdapter(StubAdapter):
    """A non-raising StubAdapter that records teardown() calls + ingest order."""

    name = "teardown_spy_stub"

    def __init__(self):
        super().__init__()
        self.teardown_calls = 0
        self.ingested = False

    def ingest(self, corpus):
        self.ingested = True
        return super().ingest(corpus)

    def teardown(self):
        self.teardown_calls += 1
        super().teardown()


def test_run_layer2_calls_teardown_even_when_trial_raises(episodes):
    spy = TeardownSpyMiscountAdapter()
    with pytest.raises(RuntimeError, match="ingest doc_count"):
        run_layer2({"spy": spy}, episodes, StubAgent(), StubJudge(),
                   n_trials=2, fixed_nonce=_FIXED_NONCE)
    # The try/finally in run_layer2 must have torn the adapter down exactly once
    # despite the trial raising before the loop completed. Prove it was the
    # try/finally that drove it, not normal completion: the spy raised on its
    # FIRST trial (MiscountStubAdapter aborts at ingest), so no trial result was
    # ever appended — teardown ran only because finally fired on the raise path.
    assert spy.teardown_calls == 1
    # Tighten the proof that the try/finally drove teardown (nit b): the adapter
    # DID ingest (so the raise happened mid-trial, after ingest), and teardown
    # fired on that raise path — not via a no-op short-circuit before ingest.
    assert spy.ingest_count == 1  # raised on the FIRST trial's ingest mismatch
    assert spy.teardown_after_ingest is True


def test_run_layer2_tears_down_first_adapter_when_it_raises_mid_run(episodes):
    # TWO adapters: the first (sorted) raises mid-trial; the second must reveal
    # run_layer2's actual behavior. run_layer2 wraps EACH adapter in its own
    # try/finally but does NOT catch the raise, so:
    #   * the first adapter IS torn down (its finally fires), and
    #   * the exception propagates out of run_layer2 -> the run ABORTS and the
    #     second adapter is NEVER reached (never ingested, never torn down).
    # Names chosen so the RAISING adapter sorts first ("a_" < "b_").
    raiser = TeardownSpyMiscountAdapter()
    raiser.name = "a_raiser"
    second = TeardownSpyStubAdapter()
    second.name = "b_second"

    with pytest.raises(RuntimeError, match="ingest doc_count"):
        run_layer2(
            {raiser.name: raiser, second.name: second},
            episodes, StubAgent(), StubJudge(),
            n_trials=2, fixed_nonce=_FIXED_NONCE,
        )

    # First adapter torn down exactly once via its try/finally despite raising.
    assert raiser.teardown_calls == 1
    # The run aborted on the first adapter's raise: the second adapter was never
    # reached, so it was neither ingested nor torn down (no leak — it never
    # acquired anything). This documents/asserts the intended abort behavior.
    assert second.ingested is False
    assert second.teardown_calls == 0


def test_run_layer2_tears_down_all_adapters_on_clean_run(episodes):
    # Contrast case: with no raise, EVERY adapter is torn down exactly once.
    a = TeardownSpyStubAdapter()
    a.name = "a_one"
    b = TeardownSpyStubAdapter()
    b.name = "b_two"
    run_layer2(
        {a.name: a, b.name: b}, episodes, StubAgent(), StubJudge(),
        n_trials=1, fixed_nonce=_FIXED_NONCE,
    )
    assert a.teardown_calls == 1
    assert b.teardown_calls == 1


# ── Cross-episode corpus isolation: re-ingest REPLACES (episode N-1 can't leak) ─
def test_cross_episode_corpus_isolation(episodes):
    # Two episodes with DISTINCT gold facts run through ONE adapter instance. The
    # MemoryAdapter.ingest contract REPLACES the index per episode, so episode 2's
    # retrieval must NOT be able to surface episode 1's fact. We assert each
    # episode's scored trial answer matches its OWN gold fact and never the other.
    from membench.episodes import Episode, Session

    ep1 = Episode(
        id="iso-ep-1",
        band=episodes[0].band,
        sessions=[
            Session("s1", "The vault seal timeout is exactly zorptide-alpha."),
            Session("s2", "Later, someone asks about the seal timeout setting."),
        ],
        fact_session_id="s1",
        question="What is the vault seal timeout value?",
        gold_answer="It is zorptide-alpha.",
        gold_fact="zorptide-alpha",
    )
    ep2 = Episode(
        id="iso-ep-2",
        band=episodes[0].band,
        sessions=[
            Session("s1", "The backup region code is quibble-omega for the cluster."),
            Session("s2", "Later, someone asks about the backup region code."),
        ],
        fact_session_id="s1",
        question="What is the backup region code?",
        gold_answer="It is quibble-omega.",
        gold_fact="quibble-omega",
    )

    adapter = StubAdapter()  # ONE instance reused across both episodes
    results = run_layer2(
        {"stub": adapter}, [ep1, ep2], StubAgent(), StubJudge(),
        n_trials=1, fixed_nonce=_FIXED_NONCE,
    )
    by_ep = {t.episode_id: t for t in results["stub"].trials}

    # Episode 1: retrieves + asserts its own fact; episode 2's fact is absent.
    assert "zorptide-alpha" in by_ep["iso-ep-1"].answer
    assert "quibble-omega" not in by_ep["iso-ep-1"].answer
    # Episode 2: retrieves + asserts ITS fact; episode 1's fact CANNOT leak in —
    # if ingest had accumulated, ep1's session docs would still be indexed and a
    # lexical match on shared words could surface "zorptide-alpha".
    assert "quibble-omega" in by_ep["iso-ep-2"].answer
    assert "zorptide-alpha" not in by_ep["iso-ep-2"].answer
    assert by_ep["iso-ep-1"].correct == 1
    assert by_ep["iso-ep-2"].correct == 1


# ── The calibration gate is WIRED to the publish path (load-bearing) ─────────
def test_publish_blocked_by_failing_calibration_gate(episodes):
    # A gate-FAILING judge subset (the skewed-constant fixture: kappa ~ 0) must
    # block the results artifact entirely — no judge numbers may be published.
    results = run_layer2({"stub": StubAdapter()}, episodes, StubAgent(),
                         StubJudge(), n_trials=1, fixed_nonce=_FIXED_NONCE)
    human, judge = load_paired_judgments(
        judge_gate_fixture_path("skewed_constant.jsonl")
    )
    with pytest.raises(JudgeGateError, match="kappa"):
        publish_layer2_results(results, human, judge)


def test_publish_blocked_by_too_few_pairs(episodes):
    results = run_layer2({"stub": StubAdapter()}, episodes, StubAgent(),
                         StubJudge(), n_trials=1, fixed_nonce=_FIXED_NONCE)
    human, judge = load_paired_judgments(judge_gate_fixture_path("n39.jsonl"))
    with pytest.raises(JudgeGateError, match="< JUDGE_MIN_SUBSET_N"):
        publish_layer2_results(results, human, judge)


def test_publish_succeeds_on_passing_calibration_gate(episodes):
    # A passing subset (n40) lets the artifact build and pins the actual n_trials.
    results = run_layer2({"stub": StubAdapter()}, episodes, StubAgent(),
                         StubJudge(), n_trials=2, fixed_nonce=_FIXED_NONCE)
    human, judge = load_paired_judgments(judge_gate_fixture_path("n40.jsonl"))
    d = publish_layer2_results(results, human, judge)
    assert d["n_trials"] == 2
    assert d["adapters"]["stub"]["task_success"]["point"] == 1.0
    # The pre-registered significance block is present (§6.9, fix 3).
    assert d["significance"]["test"].startswith("wilcoxon")
    assert "stub" in d["significance"]["task_success_ci95"]


def test_results_to_dict_rejects_unpassed_calibration(episodes):
    # Guard the structural path too: passing a non-passed CalibrationResult is a
    # hard error so the gate cannot be bypassed by feeding a failed result.
    from membench.judge import CalibrationResult

    results = run_layer2({"stub": StubAdapter()}, episodes, StubAgent(),
                         StubJudge(), n_trials=1, fixed_nonce=_FIXED_NONCE)
    bad = CalibrationResult(n=40, raw_agreement=0.5, cohen_kappa=0.0, passed=False)
    with pytest.raises(JudgeGateError, match="did NOT pass"):
        results_to_dict(results, calibration=bad)


def test_results_to_dict_rejects_forged_passed_true_with_bad_metrics(episodes):
    # The dataclass constructor is public: a caller can hand-set passed=True on a
    # factually-failed calibration (kappa=0). results_to_dict must NOT trust the
    # flag — it re-validates n / agreement / kappa and rejects the forgery.
    from membench.judge import CalibrationResult

    results = run_layer2({"stub": StubAdapter()}, episodes, StubAgent(),
                         StubJudge(), n_trials=1, fixed_nonce=_FIXED_NONCE)
    forged = CalibrationResult(n=5, raw_agreement=0.1, cohen_kappa=0.0, passed=True)
    with pytest.raises(JudgeGateError, match="did NOT pass"):
        results_to_dict(results, calibration=forged)
    # Also block it on the canonical_json path (same gate, mirrored entry point).
    with pytest.raises(JudgeGateError, match="did NOT pass"):
        canonical_json(results, calibration=forged)


def test_canonical_json_rejects_unpassed_calibration(episodes):
    # canonical_json must enforce the SAME gate as results_to_dict: a non-passed
    # CalibrationResult (even structurally honest) blocks emission of judge numbers.
    from membench.judge import CalibrationResult

    results = run_layer2({"stub": StubAdapter()}, episodes, StubAgent(),
                         StubJudge(), n_trials=1, fixed_nonce=_FIXED_NONCE)
    bad = CalibrationResult(n=40, raw_agreement=0.5, cohen_kappa=0.0, passed=False)
    with pytest.raises(JudgeGateError, match="did NOT pass"):
        canonical_json(results, calibration=bad)


def test_episode_corpus_is_multi_session(episodes):
    ep = episodes[0]
    corpus = _EpisodeCorpus(ep)
    assert len(corpus.doc_ids()) == len(ep.sessions) >= 2


# ── NO real LLM / network call reachable in any offline run ──────────────────
def test_real_agent_never_invoked_offline(monkeypatch):
    # Forbid the real client path: the LLMAgent must not make a network call.
    # The s5 stub deliberately does NOT resolve the key (review fix: no plaintext
    # key in the exception frame), so it raises NotImplementedError before any
    # network client could be reached — the live path is simply not wired.
    monkeypatch.delenv(config.CREDENTIAL_ENV_VARS["agent_api_key"], raising=False)
    agent = LLMAgent()
    with pytest.raises(NotImplementedError):
        agent.answer("ctx", "q", gold_fact="x")


def test_real_agent_stub_does_not_resolve_key_into_a_local(monkeypatch):
    # SECURITY regression guard (review fix): the s5 LLMAgent stub must not call
    # _resolve_key() — a resolved key bound to a frame local would leak into any
    # exception-capturing logger at the NotImplementedError. Spy on _resolve_key
    # and assert it is never invoked on the offline stub path.
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["agent_api_key"], "sk-fake")
    agent = LLMAgent()
    called = {"n": 0}
    orig = agent._resolve_key

    def spy():
        called["n"] += 1
        return orig()

    monkeypatch.setattr(agent, "_resolve_key", spy)
    with pytest.raises(NotImplementedError):
        agent.answer("ctx", "q", gold_fact="x")
    assert called["n"] == 0, "stub must not resolve the API key (leak risk)"


def test_real_agent_with_key_still_does_not_call_network(monkeypatch):
    # Even WITH a key set, the live path is unimplemented in s5 — it raises
    # NotImplementedError rather than reaching any network client.
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["agent_api_key"], "sk-fake")
    agent = LLMAgent()
    with pytest.raises(NotImplementedError):
        agent.answer("ctx", "q", gold_fact="x")


def test_real_judge_never_invoked_offline(monkeypatch):
    # The live judge path is unimplemented in s5 — it raises NotImplementedError
    # before any network call, regardless of whether a key is present. (It no
    # longer resolves the key in the stub path; see the security guard below.)
    monkeypatch.delenv(config.CREDENTIAL_ENV_VARS["judge_api_key"], raising=False)
    judge = LLMJudge()
    with pytest.raises(NotImplementedError):
        judge.score("answer", "gold")


def test_real_judge_stub_does_not_resolve_key_into_a_local(monkeypatch):
    # SECURITY regression guard (review fix): the s5 LLMJudge stub must not call
    # _resolve_key() — a resolved key bound to a frame local would leak into any
    # exception-capturing logger at the NotImplementedError. Mirrors the agent
    # guard above. Spy on _resolve_key and assert it is never invoked offline.
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["judge_api_key"], "sk-fake")
    judge = LLMJudge()
    called = {"n": 0}
    orig = judge._resolve_key

    def spy():
        called["n"] += 1
        return orig()

    monkeypatch.setattr(judge, "_resolve_key", spy)
    with pytest.raises(NotImplementedError):
        judge.score("answer", "gold")
    assert called["n"] == 0, "judge stub must not resolve the API key (leak risk)"


def test_real_judge_with_key_still_does_not_call_network(monkeypatch):
    # Symmetric with test_real_agent_with_key_still_does_not_call_network: even
    # WITH a key set, the live judge path is unimplemented in s5 — it raises
    # NotImplementedError rather than reaching any network client.
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["judge_api_key"], "sk-fake")
    judge = LLMJudge()
    with pytest.raises(NotImplementedError):
        judge.score("a", "b")


def test_no_anthropic_import_reachable_from_stub_path():
    # The offline runner path must not import the anthropic SDK. Assert it is not
    # loaded after a full StubAgent/StubJudge run.
    import sys

    eps = load_fixture_episodes()
    run_layer2({"stub": StubAdapter()}, eps, StubAgent(), StubJudge(),
               n_trials=2, fixed_nonce=_FIXED_NONCE)
    assert "anthropic" not in sys.modules


def test_max_api_calls_guard_on_real_agent(monkeypatch):
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["agent_api_key"], "sk-fake")
    agent = LLMAgent(max_api_calls=0)
    with pytest.raises(RuntimeError, match="MAX_API_CALLS"):
        agent.answer("ctx", "q", gold_fact="x")


def test_max_api_calls_guard_on_real_judge(monkeypatch):
    # The API-cost guard is required on BOTH agent and judge (task scope). With a
    # key present but max_api_calls=0, the judge aborts before any network call.
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["judge_api_key"], "sk-fake")
    judge = LLMJudge(max_api_calls=0)
    with pytest.raises(RuntimeError, match="MAX_API_CALLS"):
        judge.score("a", "g")


# ── Process-global API cap: N instances SHARE one budget (fix 5, §7.15) ──────
def test_two_agents_share_one_process_global_api_budget(monkeypatch):
    # Two LLMAgent instances must NOT each get their own MAX_API_CALLS budget — a
    # per-instance counter (the bug) would let N agents make N*cap calls. With the
    # process-global counter, a cap of 1 is consumed by the FIRST agent's call and
    # the SECOND agent is refused immediately (its call would be the 2nd globally).
    from membench import agent as agent_mod

    agent_mod._reset_api_calls()
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["agent_api_key"], "sk-fake")
    a1 = LLMAgent(max_api_calls=1)
    a2 = LLMAgent(max_api_calls=1)
    # a1's first call reserves the single global slot, then hits the unimplemented
    # live path (NotImplementedError) — the slot is consumed regardless.
    with pytest.raises(NotImplementedError):
        a1.answer("ctx", "q", gold_fact="x")
    # a2 shares the SAME global counter: its call would be the 2nd, over cap=1, so
    # it aborts on the API-cost guard BEFORE reaching the live path.
    with pytest.raises(RuntimeError, match="MAX_API_CALLS"):
        a2.answer("ctx", "q", gold_fact="x")


def test_two_judges_share_one_process_global_api_budget(monkeypatch):
    # Symmetric proof for the judge: a single global slot is consumed by judge #1,
    # so judge #2 is refused — the cap is NOT per-instance (fix 5, §7.15).
    from membench import judge as judge_mod

    judge_mod._reset_api_calls()
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["judge_api_key"], "sk-fake")
    j1 = LLMJudge(max_api_calls=1)
    j2 = LLMJudge(max_api_calls=1)
    with pytest.raises(NotImplementedError):
        j1.score("a", "g")
    with pytest.raises(RuntimeError, match="MAX_API_CALLS"):
        j2.score("a", "g")


def test_agent_and_judge_share_one_cumulative_budget(monkeypatch):
    # Review fix 4: MAX_API_CALLS is a CUMULATIVE cap across ALL roles, not a
    # per-role one. The agent and the judge reserve against the SAME shared
    # counter (membench.api_budget). With a cap of 1, the agent's single call
    # consumes the only slot, so the JUDGE's call is refused — proving the agent
    # and judge do NOT each get their own MAX_API_CALLS budget.
    from membench import api_budget

    api_budget.reset()
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["agent_api_key"], "sk-fake")
    monkeypatch.setenv(config.CREDENTIAL_ENV_VARS["judge_api_key"], "sk-fake")
    agent = LLMAgent(max_api_calls=1)
    judge = LLMJudge(max_api_calls=1)
    # Agent consumes the one shared slot (then hits the unimplemented live path).
    with pytest.raises(NotImplementedError):
        agent.answer("ctx", "q", gold_fact="x")
    assert api_budget.calls_made() == 1
    # Judge shares the SAME counter — the 2nd call is over the cumulative cap.
    with pytest.raises(RuntimeError, match="MAX_API_CALLS"):
        judge.score("a", "g")

"""End-to-end Layer-1 scorer over the fixture gold set (§3.2, §6).

Runs every adapter (stub + 4 baselines + sanity_random + minni-as-stub) over the
synthetic gold set and asserts the scorecard artifact is well-formed: per-band +
overall quality metrics, both refusal rates, token_cost, latency percentiles, and
NO LLM/network call (the run completes offline). Also validates the gold fixture
against the corpus so a mislabeled gold doc-id is caught here.
"""

from pathlib import Path

import pytest

from membench import config, run_scorer
from membench.corpus import load_corpus
from membench.goldset import BANDS, load_jsonl, validate_set
from membench.metrics import REQUIRED_SCORE_FIELDS
from membench.runner_layer1 import canonical_json, render_table

_FIX = Path(__file__).resolve().parents[1] / "membench" / "fixtures"

_EXPECTED_ADAPTERS = {
    "stub",
    "naive_rag",
    "markdown_grep",
    "llm_wiki",
    "native_platform",
    "sanity_random",
    "minni",
}


def test_gold_fixture_validates_against_corpus():
    """Every gold doc-id must exist in the fixture corpus; bands well-formed."""
    corpus = load_corpus(
        _FIX / "corpus_synthetic",
        pinned_hash=config.FIXTURE_CORPUS_HASH,
        scrubbed=False,
    )
    gold = load_jsonl(_FIX / "gold_synthetic.jsonl")
    report = validate_set(gold, set(corpus.doc_ids()))
    assert report.ok, report.errors()
    # All five bands incl. negatives are exercised.
    present = {g.band for g in gold}
    assert "negative" in present
    assert len(present) >= 4  # at least 4 of 5 bands (recency/contradiction/etc.)


def test_scorer_emits_full_roster_scorecard():
    cards = run_scorer.run()
    assert set(cards["adapters"]) == _EXPECTED_ADAPTERS
    assert cards["k"] == config.K
    for name, card in cards["adapters"].items():
        overall = card["overall"]
        for field in REQUIRED_SCORE_FIELDS:
            assert field in overall, f"{name} missing {field}"
        assert "false_refusal_rate" in overall
        # token_cost population = all scored queries. Pin CONCRETE expected counts
        # (item 3): n_scored==n_positive+n_negative is vacuously true since the
        # three are a partition of the same record list. The fixture gold set has
        # exactly 11 positives and 3 negatives, so a scorer that mislabeled bands
        # or dropped items is caught here.
        # n_scored is the SUM of the two populations (NIT b): assert the
        # partition relationship, not three independent magic numbers. The
        # fixture has 11 positives + 3 negatives.
        assert overall["n_positive"] == 11, (name, overall)
        assert overall["n_negative"] == 3, (name, overall)
        assert overall["n_scored"] == overall["n_positive"] + overall["n_negative"], (
            name,
            overall,
        )
        # Quality SANITY check (item 8): the lexical stub must ACTUALLY retrieve
        # (recall > 0.5). Field-presence alone would pass an all-zeros scorer; this
        # pins the scorer to a known lexical adapter.
        #
        # NOTE: the panel also suggested asserting sanity_random recall < 0.1 here,
        # but that is provably FALSE on THIS fixture: the 10-doc corpus has K=10 >=
        # corpus size, so any adapter returning up to K docs necessarily includes
        # the gold doc -> recall == 1.0 even at random. The sanity FLOOR is only
        # meaningful on a corpus with many more than K docs; it is asserted in the
        # dedicated >=50-doc sanity test (test_sanity_adapter), not here.
        if name == "stub":
            assert overall["recall_at_k"] > 0.5, overall
        # token_cost must be POSITIVE (item 10): the harness tokenizes a non-empty
        # context for every scored query, so a 0.0 here would betray a wrong
        # population (e.g. computed over an empty list or only positives).
        assert overall["token_cost"] > 0, (
            f"{name} token_cost should be positive over all scored queries"
        )
        # latency reported but separate from the score block.
        assert "p50" in card["latency_ms"] and "p95" in card["latency_ms"]
        # per-band present for the bands that have items.
        assert card["per_band"], f"{name} has no per-band block"
        # per-band CONTENT is load-bearing (item 12): positive bands must carry
        # the full quality + false-refusal block; the negative band must carry
        # correct_refusal_rate. A refactor that hoisted these into the outer card
        # (or dropped false_refusal_rate from per-band) would otherwise pass.
        _POS_BAND_FIELDS = (
            "recall_at_k",
            "precision_at_k",
            "ndcg_at_k",
            "mrr",
            "false_refusal_rate",
            "token_cost",
            "n",
        )
        for band, block in card["per_band"].items():
            if band == "negative":
                assert "correct_refusal_rate" in block, (name, band, block)
                assert "token_cost" in block, (name, band, block)
                assert block["n"] >= 1, (name, band, block)
                # Quality metrics must be ABSENT from the negative band (item 7):
                # negatives carry G(q)=∅ and are excluded from quality math, so a
                # branch that leaked recall/precision/ndcg/mrr/false_refusal_rate
                # into the negative block (e.g. running _quality_block on it) is
                # caught here, not silently averaged in.
                for leaked in (
                    "recall_at_k",
                    "precision_at_k",
                    "ndcg_at_k",
                    "mrr",
                    "false_refusal_rate",
                ):
                    assert leaked not in block, (name, band, leaked, block)
            else:
                for bf in _POS_BAND_FIELDS:
                    assert bf in block, f"{name} band {band} missing {bf}"


def test_scorecard_hand_crafted_records(__import_check=None):
    """Unit-test scorecard() with hand-built GoldScoredRecord inputs (item 8).

    Every aggregate below is computed BY HAND from the fixture records, so a
    population error (esp. token_cost over ALL queries per §6.6, or quality
    leaking into negatives) is caught directly — no adapter/corpus in the loop.

    Fixture (k=10):
      pos1 recency gold={A} ranked=[A]      tok=100 -> rec1 prec0.1 ndcg1.0 rr1.0
      pos2 recency gold={B} ranked=[X,B]    tok=200 -> rec1 prec0.1 ndcg(1/log2 3) rr0.5
      pos3 recency gold={C} ranked=[] ref   tok=50  -> all 0 (false refusal)
      neg1 negative gold={} ranked=[] ref   tok=10  -> correct refusal
      neg2 negative gold={} ranked=[Y]      tok=20  -> NOT refused
    """
    import math

    from membench.runner_layer1 import GoldScoredRecord, scorecard

    def rec(qid, band, gold, ranked, refused, tok):
        return GoldScoredRecord(
            query_id=qid,
            band=band,
            gold_doc_ids=gold,
            adapter="hand",
            ranked_doc_ids=ranked,
            refused=refused,
            harness_tokens=tok,
            wall_clock_ms=1.0,
        )

    records = [
        rec("p1", "recency", ["A"], ["A"], False, 100),
        rec("p2", "recency", ["B"], ["X", "B"], False, 200),
        rec("p3", "recency", ["C"], [], True, 50),
        rec("n1", "negative", [], [], True, 10),
        rec("n2", "negative", [], ["Y"], False, 20),
    ]
    card = scorecard("hand", records, k=10)
    ov = card["overall"]

    # quality means over the 3 POSITIVES only.
    assert ov["recall_at_k"] == pytest.approx(2 / 3)  # (1+1+0)/3
    assert ov["precision_at_k"] == pytest.approx(0.2 / 3)  # (0.1+0.1+0)/3
    ndcg_mean = (1.0 + 1.0 / math.log2(3) + 0.0) / 3  # p1=1, p2=1/log2(3), p3=0
    assert ov["ndcg_at_k"] == pytest.approx(ndcg_mean)
    assert ov["mrr"] == pytest.approx(1.5 / 3)  # (1 + 0.5 + 0)/3

    # refusal rates: correct over 2 negatives (1 refused), false over 3 positives.
    assert ov["correct_refusal_rate"] == pytest.approx(0.5)  # 1/2
    assert ov["false_refusal_rate"] == pytest.approx(1 / 3)  # p3 only

    # token_cost over ALL 5 scored queries (§6.6): (100+200+50+10+20)/5 = 76.
    assert ov["token_cost"] == pytest.approx(76.0)

    # population counts: n_scored is the SUM of the two partitions.
    assert ov["n_positive"] == 3
    assert ov["n_negative"] == 2
    assert ov["n_scored"] == ov["n_positive"] + ov["n_negative"] == 5

    # negative band carries refusal but NO quality (item 7 at the unit level).
    neg = card["per_band"]["negative"]
    assert neg["correct_refusal_rate"] == pytest.approx(0.5)
    for leaked in ("recall_at_k", "precision_at_k", "ndcg_at_k", "mrr", "false_refusal_rate"):
        assert leaked not in neg


def test_canonical_json_is_sorted_and_parseable():
    import json

    cards = run_scorer.run()
    text = canonical_json(cards)
    # sorted keys -> re-dumping the parsed object yields the same text.
    reparsed = json.loads(text)
    assert json.dumps(reparsed, sort_keys=True, ensure_ascii=False, indent=2) == text


def test_render_table_lists_every_adapter():
    cards = run_scorer.run()
    table = render_table(cards)
    for name in _EXPECTED_ADAPTERS:
        assert name in table

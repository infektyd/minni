"""Layer-1 determinism gate + meta-test (§9.1).

(1) Two identical Layer-1 runs on the pinned corpus+gold+config produce
    BYTE-IDENTICAL scorecard JSON after stripping the timing fields.
(2) The excluded-fields constant is exactly the two §3.1 record fields and the
    score fields survive the strip (no stripping the whole record to pass).
(3) META-TEST: a fixture scorecard that adds a THIRD jittery field NOT in the
    strip set must make the determinism comparison FAIL — proving the gate
    catches unregistered nondeterminism rather than passing vacuously.
"""

from membench import config, metrics
from membench import run_scorer
from membench.runner_layer1 import canonical_json


def _stripped_json(cards) -> str:
    return canonical_json(metrics.strip_excluded_fields(cards))


# ── (1) byte-identical re-run ────────────────────────────────────────────────
def test_layer1_rerun_is_byte_identical():
    """Two identical runs -> byte-identical scorecard JSON after the strip."""
    cards_a = run_scorer.run()
    cards_b = run_scorer.run()
    assert _stripped_json(cards_a) == _stripped_json(cards_b)


# ── (2) the excluded set is small + score fields survive ─────────────────────
def test_excluded_fields_constant_is_exactly_the_two_record_fields():
    # §9.1(a): the CONTRACT-record excluded set is EXACTLY the two §3.1 fields.
    assert metrics.DETERMINISM_EXCLUDED_FIELDS == frozenset(
        {"wall_clock_ms", "build_wall_clock_ms"}
    )
    # metrics.py is the authority; config.py mirrors it (must agree).
    assert config.DETERMINISM_EXCLUDED_FIELDS == metrics.DETERMINISM_EXCLUDED_FIELDS


def test_scorecard_timing_and_strip_constants_are_exact():
    """The scorecard-timing + composed strip constants are pinned (item 11).

    A future editor adding a field to SCORECARD_TIMING_FIELDS (e.g. a new
    'build_latency_ms') without updating the scorecard — or vice versa — must
    trip a test. This asserts the constants EXACTLY and that DETERMINISM_STRIP_
    FIELDS is precisely the union, and that every timing key the scorecard emits
    is either present-and-stripped or absent.
    """
    assert metrics.SCORECARD_TIMING_FIELDS == frozenset({"latency_ms", "p50", "p95"})
    assert metrics.DETERMINISM_STRIP_FIELDS == (
        metrics.DETERMINISM_EXCLUDED_FIELDS | metrics.SCORECARD_TIMING_FIELDS
    )
    # Every key named in SCORECARD_TIMING_FIELDS must be GONE from a stripped
    # scorecard (latency_ms is the emitted container; p50/p95 are its leaves).
    cards = run_scorer.run()
    stripped = metrics.strip_excluded_fields(cards)

    def _keys(obj):
        out = set()
        if isinstance(obj, dict):
            for key, val in obj.items():
                out.add(key)
                out |= _keys(val)
        elif isinstance(obj, list):
            for item in obj:
                out |= _keys(item)
        return out

    surviving = _keys(stripped)
    for field in metrics.SCORECARD_TIMING_FIELDS:
        assert field not in surviving, f"{field!r} survived the strip"
    # And the latency container IS present pre-strip (so the strip is meaningful).
    assert "latency_ms" in _keys(cards)


def test_score_fields_survive_the_strip():
    """§9.1(b): every score field is still present AFTER the strip.

    Guards against a lazy implementer stripping the whole record to pass.
    """
    cards = run_scorer.run()
    stripped = metrics.strip_excluded_fields(cards)
    for name, card in stripped["adapters"].items():
        overall = card["overall"]
        for field in metrics.REQUIRED_SCORE_FIELDS:
            assert field in overall, f"{name}: score field {field!r} stripped away"
    # And the timing fields ARE gone (latency block + any record timing field).
    for card in stripped["adapters"].values():
        assert "latency_ms" not in card
        assert "wall_clock_ms" not in card

    # The build_wall_clock_ms / wall_clock_ms assertions above are VACUOUS on the
    # real scorecard (those fields are never serialised into it). To make the
    # strip itself load-bearing (item 3), INSERT the registered fields into a
    # fixture card, strip, and assert they were actually REMOVED — proving the
    # strip set removes them rather than merely confirming they were never added.
    seeded = {
        "adapters": {
            "x": {
                "overall": {"recall_at_k": 1.0},
                "wall_clock_ms": 1.23,
                "build_wall_clock_ms": 4.56,
                "latency_ms": {"p50": 1.0, "p95": 2.0},
                "nested": [{"build_wall_clock_ms": 7.0, "keep": 1}],
            }
        }
    }
    after = metrics.strip_excluded_fields(seeded)["adapters"]["x"]
    assert "wall_clock_ms" not in after, after
    assert "build_wall_clock_ms" not in after, after
    assert "latency_ms" not in after, after
    # A field NOT in the strip set survives, and the strip recurses into lists.
    assert after["nested"] == [{"keep": 1}], after
    assert after["overall"] == {"recall_at_k": 1.0}, after


# ── (3) META-TEST: the gate catches an unregistered jittery field ────────────
def test_meta_gate_catches_unregistered_nondeterministic_field():
    """A jittery field NOT in the strip set MUST make the comparison FAIL (§9.1).

    Two runs whose only difference is an UNREGISTERED ``run_start_epoch`` field
    (a stand-in for any nondeterministic source an implementer forgot to pin or
    register) must produce DIFFERENT stripped JSON — proving the gate detects it
    rather than silently ignoring it. A gate that only ever checks the excluded
    constant would pass this vacuously; this asserts it does NOT.
    """
    cards_a = run_scorer.run()
    cards_b = run_scorer.run()
    # Inject a jittery, unregistered field into each run with DIFFERENT values.
    cards_a["run_start_epoch"] = 1_700_000_000.111
    cards_b["run_start_epoch"] = 1_700_000_000.999
    # The strip set does NOT contain run_start_epoch, so it survives -> diff fails.
    assert _stripped_json(cards_a) != _stripped_json(cards_b)


def test_meta_registered_timing_field_does_not_break_gate():
    """A field that IS registered in the strip set is removed -> no false alarm.

    Counterpart to the meta-test: ``latency_ms`` differs run-to-run (wall clock)
    but is registered in the scorecard timing strip set, so the gate ignores it
    and the comparison still passes. Proves the gate strips registered timing
    without hiding unregistered drift.
    """
    cards_a = run_scorer.run()
    cards_b = run_scorer.run()
    # latency_ms p50/p95 almost certainly differ run-to-run, yet the gate passes.
    assert _stripped_json(cards_a) == _stripped_json(cards_b)


def test_unstripped_latency_would_break_byte_identity():
    """If latency were in the gate it WOULD flap — proven HERMETICALLY (items 2/8).

    The point is that latency is machine-dependent and excluding it from the
    byte-identity gate is load-bearing: were a reviewer to fold latency into the
    gate, run-to-run jitter would break byte-identity. Rather than rely on real
    wall-clock jitter (which can collapse to equal 4-dp values on a fast/quiet
    machine and make ``a != b`` flap), we INJECT two KNOWN-DIFFERENT latency
    blocks — the same hermetic technique the meta-test uses for run_start_epoch.

    We then assert the two complementary properties on the SAME injected cards:
    - the NARROW strip (record fields only) LEAKS the latency block -> diff fails;
    - the FULL strip (record + scorecard timing fields) removes it -> diff passes.
    """
    cards_a = run_scorer.run()
    cards_b = run_scorer.run()
    # Two runs are byte-identical after the full strip; inject a deterministic,
    # known-different latency block so the test never depends on real jitter.
    any_adapter = sorted(cards_a["adapters"])[0]
    cards_a["adapters"][any_adapter]["latency_ms"] = {"p50": 1.0, "p95": 2.0}
    cards_b["adapters"][any_adapter]["latency_ms"] = {"p50": 9.0, "p95": 9.0}

    narrow = metrics.DETERMINISM_EXCLUDED_FIELDS  # excludes latency_ms/p50/p95
    a_narrow = canonical_json(metrics.strip_excluded_fields(cards_a, narrow))
    b_narrow = canonical_json(metrics.strip_excluded_fields(cards_b, narrow))
    # latency leaks through the narrow strip -> the injected difference shows.
    assert a_narrow != b_narrow

    # The FULL strip set DOES remove latency_ms, so the same cards compare equal —
    # proving the scorecard-timing fields are what keep latency out of the gate.
    a_full = canonical_json(metrics.strip_excluded_fields(cards_a))
    b_full = canonical_json(metrics.strip_excluded_fields(cards_b))
    assert a_full == b_full

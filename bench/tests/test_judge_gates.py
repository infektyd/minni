"""Judge calibration gate + Cohen's kappa + no-self-judge tests (§3.3, §9.6)."""

import pytest

from membench import config
from membench.judge import (
    MAX_JUDGE_FIXTURE_BYTES,
    ConstantStubJudge,
    JudgeGateError,
    StubJudge,
    assert_judge_publishable,
    calibrate_judge,
    compute_cohen_kappa,
    judge_gate_fixture_path,
    load_paired_judgments,
    raw_agreement,
)


# ── Cohen's kappa unit cases (hand-verifiable) ───────────────────────────────
def test_kappa_perfect_agreement():
    h = [1, 0, 1, 0, 1, 0]
    assert compute_cohen_kappa(h, h) == pytest.approx(1.0)


def test_kappa_constant_judge_on_skewed_labels_is_zero():
    # 85% of humans say 1; a judge that ALWAYS says 1 agrees 0.85 but learns
    # nothing beyond chance -> kappa == 0.0.
    human = [1] * 34 + [0] * 6
    judge = [1] * 40
    assert raw_agreement(human, judge) == pytest.approx(0.85)
    assert compute_cohen_kappa(human, judge) == pytest.approx(0.0, abs=1e-9)


def test_kappa_length_mismatch_raises():
    with pytest.raises(ValueError):
        compute_cohen_kappa([1, 0], [1])


def test_calibrate_judge_length_mismatch_raises():
    # calibrate_judge has its OWN length-mismatch guard (not just the one inside
    # compute_cohen_kappa); pass mismatched-length lists directly -> ValueError.
    with pytest.raises(ValueError, match="same length"):
        calibrate_judge([1, 0, 1], [1, 0])


# ── The minimum-n gate (fence-post: 39 fails, 40 passes) ─────────────────────
def test_n39_fixture_fails_min_n_gate():
    human, judge = load_paired_judgments(judge_gate_fixture_path("n39.jsonl"))
    assert len(human) == 39
    # Sanity: this fixture clears agreement/kappa, so ONLY the n-gate is tested.
    res = calibrate_judge(human, judge, min_subset_n=1)
    assert res.raw_agreement >= 0.80
    assert res.cohen_kappa >= 0.60
    # The hard gate must REJECT it for n < 40.
    with pytest.raises(JudgeGateError, match="< JUDGE_MIN_SUBSET_N"):
        assert_judge_publishable(human, judge)


def test_n40_fixture_passes_min_n_gate():
    human, judge = load_paired_judgments(judge_gate_fixture_path("n40.jsonl"))
    assert len(human) == 40
    res = assert_judge_publishable(human, judge)  # no raise
    assert res.n == 40
    assert res.passed is True
    assert res.raw_agreement >= 0.80
    assert res.cohen_kappa >= 0.60


def test_too_small_fixture_fails_min_n_gate():
    human, judge = load_paired_judgments(judge_gate_fixture_path("too_small.jsonl"))
    assert len(human) == 20
    with pytest.raises(JudgeGateError, match="< JUDGE_MIN_SUBSET_N"):
        assert_judge_publishable(human, judge)


# ── The kappa gate (skewed-constant judge) ───────────────────────────────────
def test_skewed_constant_fixture_rejected_by_kappa_gate():
    human, judge = load_paired_judgments(
        judge_gate_fixture_path("skewed_constant.jsonl")
    )
    assert len(human) == 40  # clears the n-gate, so the kappa gate is under test
    res = calibrate_judge(human, judge, min_subset_n=1)
    assert res.raw_agreement == pytest.approx(0.85, abs=0.02)  # chance-inflated
    assert res.cohen_kappa == pytest.approx(0.0, abs=1e-9)
    assert res.passed is False
    # The hard gate must REJECT it via the kappa threshold, not the n threshold.
    with pytest.raises(JudgeGateError, match="kappa"):
        assert_judge_publishable(human, judge)


def test_low_raw_agreement_rejected():
    # Disagree on half -> agreement 0.5 < 0.80, even with >= 40 pairs.
    human = [i % 2 for i in range(40)]
    # Flip half the judge labels so raw agreement falls below 0.80.
    judge = [(1 - h) if i < 20 else h for i, h in enumerate(human)]
    assert len(human) == 40
    with pytest.raises(JudgeGateError, match="raw agreement"):
        assert_judge_publishable(human, judge)


# ── No-self-judge gate at config validation ──────────────────────────────────
def test_config_valid_by_default():
    config.assert_config_valid()  # opus agent vs sonnet judge -> different family


def test_self_judge_same_family_raises(monkeypatch):
    # Force the judge family to equal the agent family -> must raise.
    monkeypatch.setattr(
        config,
        "JUDGE_MODEL",
        config.ModelPin(
            model_id="claude-opus-4-8-other",
            model_family=config.AGENT_MODEL.model_family,
        ),
    )
    with pytest.raises(config.ConfigError, match="no-self-judge"):
        config.assert_config_valid()


def test_publishable_gate_blocks_self_judge(monkeypatch):
    # Even with a perfectly-calibrated subset, the same-family clash blocks publish.
    monkeypatch.setattr(
        config,
        "JUDGE_MODEL",
        config.ModelPin(
            model_id="x", model_family=config.AGENT_MODEL.model_family
        ),
    )
    human, judge = load_paired_judgments(judge_gate_fixture_path("n40.jsonl"))
    with pytest.raises(config.ConfigError, match="no-self-judge"):
        assert_judge_publishable(human, judge)


# ── StubJudge determinism ────────────────────────────────────────────────────
def test_stub_judge_is_deterministic():
    j = StubJudge()
    for _ in range(5):
        assert j.score("the answer is thirty seconds", "thirty seconds") == 1
        assert j.score("I don't know", "thirty seconds") == 0


def test_constant_stub_judge():
    j = ConstantStubJudge(1)
    assert j.score("anything", "x") == 1
    assert j.score("", "") == 1


# ── Forged-fixture gate bypass is blocked (non-binary labels rejected) ───────
def test_load_rejects_nonbinary_labels(tmp_path):
    # The documented bypass: rows with human=2/judge=2 forge raw_agreement=1.0 and
    # kappa=1.0, sneaking a kappa=0 judge past the gate. The loader must REJECT
    # any non-binary label so the gate cannot be forged via the fixture file.
    import json

    forged = tmp_path / "forged.jsonl"
    rows = [{"human": 1, "judge": 1}] * 34 + [{"human": 2, "judge": 2}] * 6
    forged.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    with pytest.raises(JudgeGateError, match="BINARY 0/1"):
        load_paired_judgments(forged)


def test_load_rejects_float_labels_no_silent_truncation(tmp_path):
    # JSON floats must NOT be silently truncated (int(0.9)==0): a continuous-score
    # fixture would otherwise be corrupted into 0/1 with no error, and kappa would
    # be computed over the corrupted data. Floats are a HARD ERROR.
    import json

    bad = tmp_path / "floats.jsonl"
    rows = [{"human": 0.9, "judge": 1.5}, {"human": 1.0, "judge": 0.0}]
    bad.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    with pytest.raises(JudgeGateError, match="not float/string"):
        load_paired_judgments(bad)


def test_load_rejects_string_labels(tmp_path):
    # JSON strings (e.g. "1") must NOT be coerced via int("1"); reject them.
    import json

    bad = tmp_path / "strings.jsonl"
    bad.write_text(
        json.dumps({"human": "1", "judge": "0"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(JudgeGateError, match="not float/string"):
        load_paired_judgments(bad)


def test_load_accepts_json_bool_labels(tmp_path):
    # JSON true/false ARE valid binary labels (isinstance(True, int) is True).
    import json

    ok = tmp_path / "bools.jsonl"
    rows = [{"human": True, "judge": True}, {"human": False, "judge": False}]
    ok.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    human, judge = load_paired_judgments(ok)
    assert human == [1, 0]
    assert judge == [1, 0]


def test_load_rejects_missing_key(tmp_path):
    import json

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"human": 1}) + "\n", encoding="utf-8")
    with pytest.raises(JudgeGateError, match="the keys"):
        load_paired_judgments(bad)


def test_load_rejects_malformed_json(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not valid json\n", encoding="utf-8")
    with pytest.raises(JudgeGateError, match="malformed JSON"):
        load_paired_judgments(bad)


def test_loader_rejects_oversize_fixture(tmp_path):
    # Mirror of episodes.test_loader_rejects_oversize_file: a judge-gate fixture
    # exceeding MAX_JUDGE_FIXTURE_BYTES must be refused BEFORE parsing (the size
    # cap bounds untrusted input). Write raw bytes so we never allocate 8MB of
    # JSON — the guard trips on st_size, before read_text/json.loads.
    big = tmp_path / "oversize.jsonl"
    big.write_bytes(b"x" * (MAX_JUDGE_FIXTURE_BYTES + 1))
    with pytest.raises(JudgeGateError, match="exceeds"):
        load_paired_judgments(big)

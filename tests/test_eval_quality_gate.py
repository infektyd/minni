"""Regression tests for the baseline-vs-candidate retrieval-quality gate.

Covers ``evaluate_quality_gate`` (the WORKFLOWS Eval Gate: +5% recall@5
with no query-class regression), the CLI report-key resolver, and backward
compatibility of the existing minnid-vs-ripgrep ``evaluate_gate``.
"""

import json

import pytest

from minni.eval.harness import _resolve_quality_keys
from minni.eval.metrics import evaluate_gate, evaluate_quality_gate


def _entry(query, score, expected=(1,), notes="exact-match"):
    return {
        "query": query,
        "expected_doc_ids": list(expected),
        "notes": notes,
        "recall_at_k": {5: score, "1": score},
    }


def _reports(baseline_entries, candidate_entries):
    return {
        "baseline": {"summary": {}, "per_query": baseline_entries},
        "candidate": {"summary": {}, "per_query": candidate_entries},
    }


class TestQualityGatePass:
    def test_passes_on_sufficient_gain_without_class_regression(self):
        reports = _reports(
            [_entry("a", 0.5, notes="exact"), _entry("b", 0.5, notes="partial")],
            [_entry("a", 0.6, notes="exact"), _entry("b", 0.6, notes="partial")],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is True
        assert gate["improvement_ok"] is True
        assert gate["regressions"] == []
        assert gate["relative_improvement"] == pytest.approx(0.2)
        assert gate["comparable_queries"] == 2
        assert gate["metric"] == "recall_at_k@5"
        assert gate["limitations"]

    def test_tie_counts_as_no_regression_but_fails_improvement_leg(self):
        reports = _reports([_entry("a", 0.5)], [_entry("a", 0.5)])
        gate = evaluate_quality_gate(reports)
        assert gate["regressions"] == []
        assert gate["improvement_ok"] is False
        assert gate["ok"] is False


class TestQualityGateFailures:
    def test_fails_when_gain_below_threshold(self):
        reports = _reports([_entry("a", 0.50)], [_entry("a", 0.51)])
        gate = evaluate_quality_gate(reports, min_relative_improvement=0.05)
        assert gate["ok"] is False
        assert gate["improvement_ok"] is False
        assert "below" in gate["reason"]

    def test_threshold_is_relative_not_absolute(self):
        below = _reports([_entry("a", 0.50)], [_entry("a", 0.524)])
        assert evaluate_quality_gate(below)["ok"] is False
        exact = _reports([_entry("a", 0.50)], [_entry("a", 0.525)])
        gate = evaluate_quality_gate(exact)
        assert gate["improvement_ok"] is True
        assert gate["ok"] is True

    def test_fails_on_class_regression_despite_overall_gain(self):
        reports = _reports(
            [
                _entry("a", 0.4, notes="exact"),
                _entry("b", 1.0, notes="partial"),
            ],
            [
                _entry("a", 1.0, notes="exact"),
                _entry("b", 0.5, notes="partial"),
            ],
        )
        gate = evaluate_quality_gate(reports)
        # Overall mean rises 0.7 -> 0.75 (+7%) but "partial" regresses.
        assert gate["improvement_ok"] is True
        assert gate["regressions"] == ["partial"]
        assert gate["ok"] is False
        assert "partial" in gate["reason"]


class TestQualityGateEdges:
    def test_zero_baseline_passes_on_strict_improvement(self):
        reports = _reports([_entry("a", 0.0)], [_entry("a", 0.5)])
        gate = evaluate_quality_gate(reports)
        assert gate["relative_improvement"] is None
        assert gate["improvement_ok"] is True
        assert gate["ok"] is True

    def test_both_zero_fails_with_no_evidence(self):
        reports = _reports([_entry("a", 0.0)], [_entry("a", 0.0)])
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert gate["improvement_ok"] is False

    def test_missing_reports_fail_explicitly(self):
        gate = evaluate_quality_gate({"baseline": {"per_query": []}})
        assert gate["ok"] is False
        assert "missing reports" in gate["reason"]
        assert gate["baseline_score"] is None

    def test_incompatible_query_sets_fail_explicitly(self):
        reports = _reports([_entry("a", 0.5)], [_entry("b", 0.6)])
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert "incompatible" in gate["reason"]

    def test_unjudged_queries_excluded_not_scored_zero(self):
        reports = _reports(
            [_entry("a", 0.5), _entry("probe", 0.0, expected=())],
            [_entry("a", 0.6), _entry("probe", 0.0, expected=())],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["comparable_queries"] == 1
        assert gate["unevaluable_queries"] == ["probe"]
        assert gate["baseline_score"] == pytest.approx(0.5)
        assert gate["ok"] is True

    def test_all_unjudged_fails_with_no_comparable_queries(self):
        reports = _reports(
            [_entry("probe", 0.0, expected=())],
            [_entry("probe", 0.0, expected=())],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert "no comparable judged queries" in gate["reason"]

    def test_whole_unjudged_class_fails_not_disappears(self):
        # Acceptance repro 1: query b is unjudged in both reports, so the
        # "semantic" class has no evidence and must fail, not pass.
        reports = _reports(
            [
                _entry("a", 0.5, expected=(1,), notes="exact"),
                _entry("b", 0.0, expected=(), notes="semantic"),
            ],
            [
                _entry("a", 0.8, expected=(1,), notes="exact"),
                _entry("b", 0.0, expected=(), notes="semantic"),
            ],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert gate["unevaluable_classes"] == ["semantic"]
        assert gate["unevaluable_queries"] == ["b"]
        assert "missing evidence" in gate["reason"]

    def test_mixed_class_probe_listed_without_failing(self):
        # An unjudged probe inside an otherwise judged class is reported
        # as unevaluable (never as passed) without voiding the class.
        reports = _reports(
            [
                _entry("a", 0.5, expected=(1,), notes="exact"),
                _entry("probe", 0.0, expected=(), notes="exact"),
            ],
            [
                _entry("a", 0.6, expected=(1,), notes="exact"),
                _entry("probe", 0.0, expected=(), notes="exact"),
            ],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is True
        assert gate["unevaluable_queries"] == ["probe"]
        assert gate["unevaluable_classes"] == []

    def test_candidate_auto_resolves_with_two_reports(self):
        reports = {
            "minnid-baseline": {"per_query": [_entry("a", 0.5)]},
            "minnid-with-expand": {"per_query": [_entry("a", 0.6)]},
        }
        gate = evaluate_quality_gate(
            reports, baseline="minnid-baseline", candidate=None
        )
        assert gate["candidate"] == "minnid-with-expand"
        assert gate["ok"] is True


class TestResolveQualityKeys:
    def test_exact_match(self):
        reports = {"baseline": {}, "candidate": {}}
        assert _resolve_quality_keys(reports, "baseline", "candidate") == (
            "baseline",
            "candidate",
        )

    def test_suffix_fallback_for_prefixed_report_names(self):
        reports = {"minnid-baseline": {}, "minnid-with-expand": {}}
        assert _resolve_quality_keys(reports, "baseline", "with-expand") == (
            "minnid-baseline",
            "minnid-with-expand",
        )

    def test_empty_candidate_auto_resolves_with_two_reports(self):
        reports = {"minnid-baseline": {}, "minnid-with-expand": {}}
        assert _resolve_quality_keys(reports, "baseline", "") == (
            "minnid-baseline",
            "minnid-with-expand",
        )

    def test_ambiguous_suffix_does_not_resolve(self):
        reports = {"a-baseline": {}, "b-baseline": {}}
        baseline_key, _ = _resolve_quality_keys(reports, "baseline", "")
        assert baseline_key == "baseline"

    def test_fp32_baseline_does_not_collide_with_baseline(self):
        reports = {"minnid-baseline": {}, "minnid-fp32-baseline": {}}
        assert _resolve_quality_keys(reports, "baseline", "fp32-baseline") == (
            "minnid-baseline",
            "minnid-fp32-baseline",
        )

    def test_lone_fp32_baseline_does_not_bind_as_baseline(self):
        reports = {"minnid-fp32-baseline": {}, "minnid-int8-quantized": {}}
        baseline_key, candidate_key = _resolve_quality_keys(
            reports, "baseline", "int8-quantized"
        )
        assert baseline_key == "baseline"
        assert candidate_key == "minnid-int8-quantized"

    def test_explicit_unknown_candidate_returned_verbatim_never_none(self):
        reports = {"baseline": {}, "candidate": {}}
        baseline_key, candidate_key = _resolve_quality_keys(
            reports, "baseline", "DOES-NOT-EXIST"
        )
        assert (baseline_key, candidate_key) == ("baseline", "DOES-NOT-EXIST")

    def test_explicit_unknown_candidate_fails_without_auto_select(self):
        reports = {
            "baseline": {"per_query": [_entry("a", 0.5)]},
            "candidate": {"per_query": [_entry("a", 0.9)]},
        }
        gate = evaluate_quality_gate(
            reports, baseline="baseline", candidate="DOES-NOT-EXIST"
        )
        assert gate["ok"] is False
        assert gate["candidate"] == "DOES-NOT-EXIST"
        assert "missing reports" in gate["reason"]


class TestStrictJudgmentIds:
    def test_lossy_float_ids_are_malformed(self):
        reports = _reports(
            [_entry("a", 0.5, expected=[1.1])],
            [_entry("a", 0.9, expected=[1.9])],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert (
            gate["incomparable_queries"][0]["issue"]
            == "malformed expected_doc_ids"
        )

    def test_identical_float_ids_still_malformed(self):
        reports = _reports(
            [_entry("a", 0.5, expected=[1.0])],
            [_entry("a", 0.9, expected=[1.0])],
        )
        assert evaluate_quality_gate(reports)["ok"] is False

    def test_bool_and_string_ids_are_malformed(self):
        for bad in ([True], (["1"])):
            reports = _reports(
                [_entry("a", 0.5, expected=bad)],
                [_entry("a", 0.9, expected=bad)],
            )
            assert evaluate_quality_gate(reports)["ok"] is False

    def test_exact_integer_ids_still_compare(self):
        reports = _reports(
            [_entry("a", 0.5, expected=[2, 1])],
            [_entry("a", 0.9, expected=[1, 2])],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is True


class TestReviewedBooleanRequired:
    def _valid_entry(self, reviewed):
        return {
            "query": "probe",
            "expected_doc_ids": [1],
            "expected_relevance": {"1": 3},
            "reviewed": reviewed,
            "notes": "exact-match",
            "answer_rubric": "Must mention the probe.",
            "privacy_expectation": "safe-public",
        }

    def test_string_reviewed_flags_rejected(self):
        from minni.eval.dataset import validate_queries

        for bad in ("false", "true", 1, None):
            report = validate_queries([self._valid_entry(bad)], min_reviewed=1)
            assert report["ok"] is False
            assert any("reviewed" in err for err in report["errors"])

    def test_boolean_true_accepted(self):
        from minni.eval.dataset import validate_queries

        report = validate_queries([self._valid_entry(True)], min_reviewed=1)
        assert report["ok"] is True


class TestComparabilityBeforeScoring:
    def test_changed_judgments_fail(self):
        # Acceptance repro 2: same query, different expectations.
        reports = _reports(
            [_entry("a", 0.5, expected=(1, 2))],
            [_entry("a", 1.0, expected=(8,))],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert gate["comparable_queries"] == 0
        assert gate["incomparable_queries"][0]["issue"] == "changed judgments"
        assert "incomparable" in gate["reason"]

    def test_missing_metric_never_defaults_to_zero(self):
        # Acceptance repro 3: baseline entry without recall_at_k.
        baseline = {
            "query": "a",
            "expected_doc_ids": [1],
            "notes": "exact-match",
        }
        reports = _reports(
            [baseline],
            [_entry("a", 1.0, expected=(1,))],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert gate["baseline_score"] is None
        assert "missing" in gate["incomparable_queries"][0]["issue"]

    def test_out_of_range_metric_fails(self):
        reports = _reports(
            [_entry("a", 0.5)],
            [_entry("a", 1.5)],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert "invalid" in gate["incomparable_queries"][0]["issue"]

    def test_non_finite_metric_fails(self):
        reports = _reports(
            [_entry("a", 0.5)],
            [_entry("a", float("nan"))],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False

    def test_changed_class_metadata_fails(self):
        reports = _reports(
            [_entry("a", 0.5, notes="exact")],
            [_entry("a", 0.6, notes="partial")],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert gate["incomparable_queries"][0]["issue"] == "changed class metadata"
        assert gate["label_mismatches"] == [
            {
                "query": "a",
                "baseline_class": "exact",
                "candidate_class": "partial",
            }
        ]

    def test_duplicate_query_identities_fail(self):
        reports = _reports(
            [_entry("a", 0.5), _entry("a", 0.6)],
            [_entry("a", 0.6)],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False
        assert "identities" in gate["reason"]

    def test_empty_query_identity_fails(self):
        reports = _reports(
            [{"query": "", "expected_doc_ids": [1], "recall_at_k": {5: 0.5}}],
            [_entry("", 0.6)],
        )
        gate = evaluate_quality_gate(reports)
        assert gate["ok"] is False


class TestInvalidGateParameters:
    def test_non_positive_k_fails(self):
        reports = _reports([_entry("a", 0.5)], [_entry("a", 0.6)])
        for bad_k in (0, -5, True):
            gate = evaluate_quality_gate(reports, k=bad_k)
            assert gate["ok"] is False
            assert "invalid k" in gate["reason"]

    def test_negative_or_non_finite_threshold_fails(self):
        reports = _reports([_entry("a", 0.5)], [_entry("a", 0.6)])
        for bad_threshold in (-0.05, float("nan"), float("inf")):
            gate = evaluate_quality_gate(
                reports, min_relative_improvement=bad_threshold
            )
            assert gate["ok"] is False
            assert "min_relative_improvement" in gate["reason"]

    def test_only_normative_recall_metric_accepted(self):
        reports = _reports([_entry("a", 0.5)], [_entry("a", 0.6)])
        for bad_metric in ("", "ndcg_at_k", "token_budget_recall_at_k", "mrr"):
            gate = evaluate_quality_gate(reports, metric=bad_metric)
            assert gate["ok"] is False
            assert "recall_at_k only" in gate["reason"]

    def test_graded_judgment_change_cannot_pass_via_ndcg(self):
        # Same IDs, changed grades ({1:1,2:3} vs {1:3,2:1}) move nDCG
        # without moving recall; the ndcg mode is rejected outright.
        baseline = [_entry("a", 0.5, expected=(1, 2))]
        baseline[0]["expected_relevance"] = {"1": 1, "2": 3}
        candidate = [_entry("a", 0.5, expected=(1, 2))]
        candidate[0]["expected_relevance"] = {"1": 3, "2": 1}
        gate = evaluate_quality_gate(
            _reports(baseline, candidate), metric="ndcg_at_k"
        )
        assert gate["ok"] is False
        assert "recall_at_k only" in gate["reason"]


class TestQualityGateCli:
    def _write_queries(self, path, queries):
        with path.open("w", encoding="utf-8") as fh:
            for entry in queries:
                fh.write(json.dumps(entry) + "\n")

    def _reviewed_entry(self, idx):
        return {
            "query": f"quality probe {idx}",
            "expected_doc_ids": [1000 + idx],
            "expected_refs": [f"docs/probe-{idx}.md"],
            "expected_relevance": {str(1000 + idx): 3},
            "reviewed": True,
            "notes": "exact-match",
            "answer_rubric": "Must mention the probe.",
            "privacy_expectation": "safe-public",
        }

    def _args(self, queries_path, **overrides):
        import argparse

        params = {
            "config": "baseline,with-expand",
            "queries": str(queries_path),
            "mock": True,
            "retrievers": "minnid",
            "gate": False,
            "quality_gate": True,
            "quality_baseline": "baseline",
            "quality_candidate": "",
            "min_improvement": 0.05,
            "quality_metric": "recall_at_k",
            "quality_k": 5,
        }
        params.update(overrides)
        return argparse.Namespace(**params)

    def _bomb_searcher(self, monkeypatch):
        calls = []

        def bomb(name, queries=None, root=None):
            calls.append(name)
            raise AssertionError("retriever work must not precede gate checks")

        monkeypatch.setattr("minni.eval.harness.make_searcher", bomb)
        return calls

    def test_unknown_candidate_exits_before_retriever_work(
        self, tmp_path, monkeypatch
    ):
        from minni.eval.harness import cmd_run

        queries_path = tmp_path / "queries.jsonl"
        self._write_queries(
            queries_path, [self._reviewed_entry(i) for i in range(300)]
        )
        monkeypatch.setattr(
            "minni.eval.harness._reports_dir", lambda: tmp_path / "reports"
        )
        calls = self._bomb_searcher(monkeypatch)

        with pytest.raises(SystemExit) as exc:
            cmd_run(
                self._args(
                    queries_path,
                    mock=False,
                    quality_candidate="DOES-NOT-EXIST",
                )
            )
        assert exc.value.code == 2
        assert calls == []
        assert not (tmp_path / "reports").exists()

    def test_non_recall_metric_exits_before_retriever_work(
        self, tmp_path, monkeypatch
    ):
        from minni.eval.harness import cmd_run

        queries_path = tmp_path / "queries.jsonl"
        self._write_queries(
            queries_path, [self._reviewed_entry(i) for i in range(300)]
        )
        monkeypatch.setattr(
            "minni.eval.harness._reports_dir", lambda: tmp_path / "reports"
        )
        calls = self._bomb_searcher(monkeypatch)

        with pytest.raises(SystemExit) as exc:
            cmd_run(
                self._args(
                    queries_path, mock=False, quality_metric="ndcg_at_k"
                )
            )
        assert exc.value.code == 2
        assert calls == []

    def test_string_reviewed_corpus_exits_before_retriever_work(
        self, tmp_path, monkeypatch
    ):
        from minni.eval.harness import cmd_run

        queries_path = tmp_path / "queries.jsonl"
        entries = [self._reviewed_entry(i) for i in range(300)]
        for entry in entries:
            entry["reviewed"] = "true"
        self._write_queries(queries_path, entries)
        monkeypatch.setattr(
            "minni.eval.harness._reports_dir", lambda: tmp_path / "reports"
        )
        calls = self._bomb_searcher(monkeypatch)

        with pytest.raises(SystemExit) as exc:
            cmd_run(self._args(queries_path, mock=False))
        assert exc.value.code == 2
        assert calls == []

    def test_unreviewed_corpus_exits_before_retriever_work(
        self, tmp_path, monkeypatch
    ):
        from minni.eval.harness import cmd_run

        queries_path = tmp_path / "queries.jsonl"
        self._write_queries(
            queries_path,
            [{"query": "unreviewed probe", "expected_doc_ids": [1]}],
        )
        monkeypatch.setattr(
            "minni.eval.harness._reports_dir", lambda: tmp_path / "reports"
        )
        calls = []

        def bomb(name, queries=None, root=None):
            calls.append(name)
            raise AssertionError("retriever work must not precede validation")

        monkeypatch.setattr("minni.eval.harness.make_searcher", bomb)

        with pytest.raises(SystemExit) as exc:
            cmd_run(self._args(queries_path, mock=False))
        assert exc.value.code == 2
        assert calls == []
        assert not (tmp_path / "reports").exists()

    def test_reviewed_corpus_tie_still_fails_gate(self, tmp_path, monkeypatch):
        from minni.eval.harness import cmd_run

        queries_path = tmp_path / "queries.jsonl"
        self._write_queries(
            queries_path, [self._reviewed_entry(i) for i in range(300)]
        )
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(
            "minni.eval.harness._reports_dir", lambda: reports_dir
        )

        with pytest.raises(SystemExit) as exc:
            cmd_run(self._args(queries_path))
        # Mock searcher recalls everything under both configs: tie 1.0->1.0
        # is below +5%, so the reviewed corpus still fails the gate.
        assert exc.value.code == 3
        gate_reports = list(reports_dir.glob("*-quality-gate.json"))
        assert len(gate_reports) == 1
        gate = json.loads(gate_reports[0].read_text())
        assert gate["ok"] is False
        assert gate["comparable_queries"] == 300
        assert gate["unevaluable_classes"] == []

    def test_reviewed_corpus_zero_threshold_passes(self, tmp_path, monkeypatch):
        from minni.eval.harness import cmd_run

        queries_path = tmp_path / "queries.jsonl"
        self._write_queries(
            queries_path, [self._reviewed_entry(i) for i in range(300)]
        )
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(
            "minni.eval.harness._reports_dir", lambda: reports_dir
        )

        cmd_run(self._args(queries_path, min_improvement=0.0))
        gate_reports = list(reports_dir.glob("*-quality-gate.json"))
        assert len(gate_reports) == 1
        assert json.loads(gate_reports[0].read_text())["ok"] is True


class TestLegacyGatePreserved:
    def test_minnid_vs_ripgrep_loss_rate_still_enforced(self):
        reports = {
            "minnid": {
                "per_query": [
                    {"query": "a", "recall_at_k": {5: 0.0}},
                    {"query": "b", "recall_at_k": {5: 1.0}},
                ]
            },
            "ripgrep": {
                "per_query": [
                    {"query": "a", "recall_at_k": {5: 1.0}},
                    {"query": "b", "recall_at_k": {5: 1.0}},
                ]
            },
        }
        gate = evaluate_gate(
            reports, primary="minnid", baseline="ripgrep", max_loss_rate=0.20
        )
        assert gate["ok"] is False
        assert gate["loss_rate"] == pytest.approx(0.5)


@pytest.mark.parametrize("overrides,bad_ids", [
    ({"quality_k": 0}, None),
    ({"quality_k": -1}, None),
    ({"quality_k": 7}, None),
    ({"min_improvement": float("nan")}, None),
    ({"min_improvement": float("inf")}, None),
    ({"min_improvement": -0.1}, None),
    ({}, [1.9]),
    ({}, [True]),
    ({}, ["1"]),
])
def test_raw_quality_inputs_rejected_before_retriever(tmp_path, monkeypatch, overrides, bad_ids):
    from minni.eval.harness import cmd_run
    fixture = TestQualityGateCli()
    rows = [fixture._reviewed_entry(i) for i in range(300)]
    if bad_ids is not None:
        rows[0]["expected_doc_ids"] = bad_ids
    path = tmp_path / "queries.jsonl"
    fixture._write_queries(path, rows)
    calls = fixture._bomb_searcher(monkeypatch)
    with pytest.raises(SystemExit) as result:
        cmd_run(fixture._args(path, mock=False, **overrides))
    assert result.value.code == 2
    assert calls == []

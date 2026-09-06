"""Focused merge-readiness corrections for bounded eval work (PR397/400).

Synthetic plumbing evidence only: every corpus here is machine-written mock
data exercising harness behavior. Nothing in this file establishes retrieval
quality or human-reviewed ground truth.
"""

import argparse
import json
import os
import stat

import pytest

from minni.eval import harness
from minni.eval.metrics import KNOWN_RETRIEVE_KWARGS, evaluate_quality_gate
from minni.eval.provenance import (
    backend_ignored_options,
    retrieval_options_provenance,
)


def _entry(query, score, expected=(1,), notes="exact-match"):
    return {
        "query": query,
        "expected_doc_ids": list(expected),
        "notes": notes,
        "recall_at_k": {5: score},
    }


def _reports(a, b):
    return {
        "baseline": {"summary": {}, "per_query": a},
        "candidate": {"summary": {}, "per_query": b},
    }


def _namespace(**overrides):
    args = argparse.Namespace(
        config="baseline",
        retrievers="mock",
        queries="",
        mock=True,
        gate=False,
        quality_gate=False,
        output_dir="",
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _bomb(monkeypatch):
    calls = []

    def bomb(name, queries=None, root=None):
        calls.append(name)
        raise AssertionError("retriever work must not precede gate checks")

    monkeypatch.setattr(harness, "make_searcher", bomb)
    return calls


def _reviewed_rows(n=300):
    return [
        {
            "query": f"synthetic probe {i}",
            "expected_doc_ids": [1000 + i],
            "expected_refs": [f"docs/synth-{i}.md"],
            "expected_relevance": {str(1000 + i): 3},
            "reviewed": True,
            "notes": "exact-match",
            "answer_rubric": "Synthetic plumbing row.",
            "privacy_expectation": "safe-public",
        }
        for i in range(n)
    ]


def _write_queries(path, rows):
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class TestDefaultDirUmaskPreflight:
    def test_fresh_default_dir_is_private_under_permissive_umask(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(harness, "repo_root", lambda: tmp_path)
        old = os.umask(0)
        try:
            d = harness._reports_dir()
        finally:
            os.umask(old)
        assert d == tmp_path / "eval" / "reports"
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_group_writable_default_dir_fails_before_retriever_work(
        self, tmp_path, monkeypatch
    ):
        default = tmp_path / "eval" / "reports"
        default.mkdir(parents=True)
        os.chmod(default, 0o775)
        monkeypatch.setattr(harness, "repo_root", lambda: tmp_path)
        query_path = tmp_path / "q.jsonl"
        _write_queries(query_path, [{"query": "a", "expected_doc_ids": [1]}])
        calls = _bomb(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            harness.cmd_run(
                _namespace(queries=str(query_path), mock=True)
            )
        assert exc.value.code == 2
        assert calls == []
        assert list(default.iterdir()) == []

    def test_explicit_shared_dir_fails_before_retriever_work(
        self, tmp_path, monkeypatch
    ):
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        query_path = tmp_path / "q.jsonl"
        _write_queries(query_path, [{"query": "a", "expected_doc_ids": [1]}])
        calls = _bomb(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            harness.cmd_run(
                _namespace(
                    queries=str(query_path), mock=True,
                    output_dir=str(shared),
                )
            )
        assert exc.value.code == 2
        assert calls == []
        assert list(shared.iterdir()) == []


class TestFixtureSingleFileOutput:
    def test_sticky_shared_parent_accepts_private_file(self, tmp_path):
        shared = tmp_path / "shared-tmp"
        shared.mkdir()
        os.chmod(shared, 0o1777)
        out = shared / "minni-fixture.json"
        harness._preflight_single_file(out)
        harness._write_private_single_file(out, '{"summary": {"ok": true}}')
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        assert json.loads(out.read_text()) == {"summary": {"ok": True}}

    def test_non_sticky_shared_parent_rejected(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        os.chmod(shared, 0o775)
        with pytest.raises(ValueError, match="sticky"):
            harness._preflight_single_file(shared / "out.json")

    def test_destination_dir_or_symlink_rejected_before_work(self, tmp_path):
        with pytest.raises(ValueError, match="not a directory"):
            harness._preflight_single_file(tmp_path / "nope" / "out.json")
        subdir = tmp_path / "sub"
        subdir.mkdir()
        with pytest.raises(ValueError, match="regular"):
            harness._preflight_single_file(subdir)
        target = tmp_path / "target"
        target.write_text("original")
        link = tmp_path / "link.json"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="regular"):
            harness._preflight_single_file(link)
        assert target.read_text() == "original"

    def test_fixture_preflights_output_before_running(self, tmp_path, monkeypatch):
        import minni.eval.fixture as fixture_mod

        calls = []
        monkeypatch.setattr(
            fixture_mod, "run_fixture",
            lambda **kwargs: calls.append(kwargs) or {"summary": {"ok": True}},
        )
        subdir = tmp_path / "sub"
        subdir.mkdir()
        with pytest.raises(SystemExit) as exc:
            harness.main(["fixture", "--output", str(subdir)])
        assert exc.value.code == 2
        assert calls == []


class TestCasefoldReportNames:
    @pytest.mark.parametrize("retrievers", ["minnid,MINNID", "mock,Mock"])
    def test_case_collisions_rejected_before_search(
        self, tmp_path, monkeypatch, retrievers
    ):
        query_path = tmp_path / "q.jsonl"
        _write_queries(query_path, [{"query": "a", "expected_doc_ids": [1]}])
        calls = _bomb(monkeypatch)
        monkeypatch.setattr(
            harness, "load_queries",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("load must not precede name checks")),
        )
        with pytest.raises(SystemExit) as exc:
            harness.cmd_run(
                _namespace(
                    queries=str(query_path), mock=True,
                    retrievers=retrievers,
                    output_dir=str(tmp_path / "out"),
                )
            )
        assert exc.value.code == 2
        assert calls == []
        assert not (tmp_path / "out").exists()


class TestGateArtifactProvenance:
    def _quality_args(self, query_path, out_dir, **overrides):
        params = {
            "config": "no-expand,with-expand",
            "queries": str(query_path),
            "mock": True,
            "retrievers": "mock",
            "gate": False,
            "quality_gate": True,
            "quality_baseline": "no-expand",
            "quality_candidate": "",
            "min_improvement": 0.0,
            "quality_metric": "recall_at_k",
            "quality_k": 5,
            "output_dir": str(out_dir),
        }
        params.update(overrides)
        return argparse.Namespace(**params)

    def test_quality_gate_artifact_carries_provenance(self, tmp_path):
        query_path = tmp_path / "q.jsonl"
        _write_queries(query_path, _reviewed_rows())
        out = tmp_path / "reports"
        with pytest.raises(SystemExit) as exc:
            harness.cmd_run(self._quality_args(query_path, out))
        # Mock plumbing never certifies: the artifact records the downgraded
        # synthetic decision alongside the preserved numeric comparison.
        assert exc.value.code == 3
        artifacts = list(out.glob("*-quality-gate.json"))
        assert len(artifacts) == 1
        report = json.loads(artifacts[0].read_text())
        assert report["evidence"] == "synthetic-plumbing"
        assert "synthetic plumbing evidence only" in report["reason"]
        prov = report["provenance"]
        assert prov["kind"] == "quality"
        assert prov["baseline"] == "mock-no-expand"
        assert prov["candidate"] == "mock-with-expand"
        assert prov["query_file"]["loaded_queries_digest"]
        assert prov["code"]["revision"]
        assert prov["corpus_snapshot"] == "mock"
        assert prov["decision"]["ok"] is False
        assert "certification" in prov and "none" in prov["certification"]

    def test_legacy_gate_artifact_carries_provenance(self, tmp_path):
        query_path = tmp_path / "q.jsonl"
        _write_queries(query_path, _reviewed_rows())
        out = tmp_path / "reports"
        harness.cmd_run(
            _namespace(
                queries=str(query_path), mock=True,
                retrievers="minnid,ripgrep", gate=True,
                output_dir=str(out),
            )
        )
        artifacts = list(out.glob("*-gate.json"))
        assert len(artifacts) == 1
        report = json.loads(artifacts[0].read_text())
        assert report["provenance"]["kind"] == "legacy-loss-rate"
        assert report["provenance"]["query_file"]["loaded_queries_digest"]
        assert report["provenance"]["corpus_snapshot"]


class TestBackendEffectiveOptions:
    def _prov(self, backend, config):
        from minni.eval.provenance import backend_envelope_options

        return retrieval_options_provenance(
            "with-expand", config,
            KNOWN_RETRIEVE_KWARGS,
            backend_ignored=backend_ignored_options(backend),
            backend_envelope=backend_envelope_options(backend),
        )

    def test_ripgrep_swallows_expand_and_update_access(self):
        prov = self._prov("rg", {"expand": True, "use_hyde": False})
        assert prov["ignored_by_backend"] == ["expand", "use_hyde"]
        assert prov["effective"] == {"limit": 10}
        assert "update_access" not in prov["effective"]

    def test_live_backend_keeps_expand_effective(self):
        prov = self._prov("minnid", {"expand": True, "use_hyde": False})
        assert prov["ignored_by_backend"] == []
        assert prov["effective"]["expand"] is True
        assert prov["effective"]["update_access"] is False

    def test_raw_context_keeps_budget_but_not_expand(self):
        prov = self._prov("raw-context", {"expand": True})
        assert prov["ignored_by_backend"] == ["expand"]
        assert "expand" not in prov["effective"]

    def test_vendor_reports_empty_envelope(self):
        prov = self._prov("vendor-memory", {"expand": True})
        assert prov["effective"] == {}
        assert prov["ignored_by_backend"] == ["expand"]

    def test_aliases_share_canonical_backend(self):
        assert backend_ignored_options("rg") == backend_ignored_options("ripgrep")
        assert backend_ignored_options("raw") == backend_ignored_options("raw-context")
        assert backend_ignored_options("vendor_memory") == backend_ignored_options("vendor-memory")


class TestMissingJudgmentsMalformed:
    SNAP = "sha256:frozen-study-corpus-v1"

    def _provenance(self):
        return {
            "corpus": {"snapshot": self.SNAP, "frozen": True},
            "principal": {"backend": "frozen-snapshot", "mock": False},
        }

    def _pair(self, query_row):
        judged_base = _entry("judged", 0.5)
        judged_cand = _entry("judged", 0.9)
        return {
            "baseline": {
                "summary": {},
                "per_query": [judged_base, dict(query_row)],
                "provenance": self._provenance(),
            },
            "candidate": {
                "summary": {},
                "per_query": [judged_cand, dict(query_row)],
                "provenance": self._provenance(),
            },
        }

    @pytest.mark.parametrize("row", [
        {"query": "unj", "notes": "exact-match", "recall_at_k": {5: 1.0}},
        {"query": "unj", "expected_doc_ids": None, "notes": "exact-match",
         "recall_at_k": {5: 1.0}},
    ])
    def test_absent_or_null_judgment_fails_instead_of_passing(self, row):
        gate = evaluate_quality_gate(self._pair(row))
        assert gate["ok"] is False
        assert gate["incomparable_queries"]
        assert gate["incomparable_queries"][0]["issue"] == (
            "malformed expected_doc_ids"
        )

    def test_explicit_empty_stays_unevaluable_probe(self):
        gate = evaluate_quality_gate(self._pair(_entry("unj", 1.0, expected=())))
        assert gate["ok"] is True
        assert gate["unevaluable_queries"] == ["unj"]


class TestHydeConstancy:
    def _quality_cli(self, query_path, config):
        return argparse.Namespace(
            config=config,
            queries=str(query_path),
            mock=False,
            retrievers="minnid",
            gate=False,
            quality_gate=True,
            quality_baseline="no-expand",
            quality_candidate="with-hyde",
            min_improvement=0.05,
            quality_metric="recall_at_k",
            quality_k=5,
        )

    def test_hyde_pair_rejected_before_search(self, tmp_path, monkeypatch):
        query_path = tmp_path / "q.jsonl"
        _write_queries(query_path, _reviewed_rows())
        calls = _bomb(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            harness.cmd_run(
                self._quality_cli(query_path, "no-expand,with-hyde")
            )
        assert exc.value.code == 2
        assert calls == []


class TestFrozenCorpusGate:
    SNAP = "sha256:frozen-study-corpus-v1"

    def _report(self, score, corpus=None, principal=None, mock=None):
        provenance = {}
        if corpus is not None:
            provenance["corpus"] = corpus
        if principal is not None:
            provenance["principal"] = principal
        if mock is not None:
            provenance["mock"] = mock
        report = {"summary": {}, "per_query": [_entry("a", score)]}
        if provenance:
            report["provenance"] = provenance
        return report

    def _live_report(self, score):
        return self._report(
            score,
            corpus={"snapshot": "unknown", "frozen": False},
            principal={"backend": "live-mutable DEFAULT_CONFIG"},
        )

    def _frozen_report(self, score, snapshot=None):
        return self._report(
            score,
            corpus={"snapshot": self.SNAP if snapshot is None else snapshot,
                     "frozen": True},
            principal={"backend": "frozen-snapshot", "mock": False},
        )

    def _mock_report(self, score):
        return self._report(
            score,
            corpus={"snapshot": "mock", "frozen": False},
            principal={"backend": "mock", "mock": True},
            mock=True,
        )

    def test_live_mutable_unknown_snapshot_cannot_certify(self):
        gate = evaluate_quality_gate({
            "baseline": self._live_report(0.5),
            "candidate": self._live_report(0.9),
        })
        assert gate["ok"] is False
        assert "corpus identity" in gate["reason"]
        assert gate["comparable_queries"] == 1

    def test_mock_evidence_compares_but_never_certifies(self):
        gate = evaluate_quality_gate({
            "baseline": self._mock_report(0.5),
            "candidate": self._mock_report(0.9),
        })
        assert gate["ok"] is False
        assert gate["evidence"] == "synthetic-plumbing"
        assert "synthetic plumbing evidence only" in gate["reason"]
        # Numeric comparison is preserved for plumbing, not discarded.
        assert gate["comparable_queries"] == 1
        assert gate["baseline_score"] == pytest.approx(0.5)
        assert gate["candidate_score"] == pytest.approx(0.9)
        assert gate["improvement_ok"] is True

    def test_recorded_frozen_snapshot_passes_compatibly(self):
        gate = evaluate_quality_gate({
            "baseline": self._frozen_report(0.5),
            "candidate": self._frozen_report(0.9),
        })
        assert gate["ok"] is True
        assert gate["evidence"] == "frozen-corpus"

    def test_absent_provenance_is_not_synthetic(self):
        gate = evaluate_quality_gate({
            "baseline": {"summary": {}, "per_query": [_entry("a", 0.5)]},
            "candidate": {"summary": {}, "per_query": [_entry("a", 0.9)]},
        })
        assert gate["ok"] is False
        assert "corpus identity" in gate["reason"]

    @pytest.mark.parametrize("corpus", [
        {"frozen": True},
        {"snapshot": None, "frozen": True},
        {"snapshot": "", "frozen": True},
        {"snapshot": "unknown", "frozen": True},
        {"snapshot": "unknown", "frozen": False},
        {"snapshot": "sha256:abc123"},
        {"snapshot": "sha256:abc123", "frozen": "yes"},
    ])
    def test_malformed_snapshot_identity_fails(self, corpus):
        gate = evaluate_quality_gate({
            "baseline": self._report(0.5, corpus=corpus),
            "candidate": self._report(0.9, corpus=corpus),
        })
        assert gate["ok"] is False

    def test_frozen_true_without_identity_fails(self):
        gate = evaluate_quality_gate({
            "baseline": self._report(0.5, corpus={"frozen": True}),
            "candidate": self._report(0.9, corpus={"frozen": True}),
        })
        assert gate["ok"] is False
        assert "corpus identity" in gate["reason"]

    def test_mismatched_snapshots_fail(self):
        gate = evaluate_quality_gate({
            "baseline": self._frozen_report(0.5, "sha256:corpus-a"),
            "candidate": self._frozen_report(0.9, "sha256:corpus-b"),
        })
        assert gate["ok"] is False
        assert "mismatch" in gate["reason"]

    def test_mixed_synthetic_and_frozen_fails(self):
        gate = evaluate_quality_gate({
            "baseline": self._mock_report(0.5),
            "candidate": self._frozen_report(0.9),
        })
        assert gate["ok"] is False
        assert "incomparable corpus evidence kinds" in gate["reason"]

    def test_non_dict_provenance_blocks_fail_closed(self):
        gate = evaluate_quality_gate({
            "baseline": self._report(0.5, corpus="sha256:abc"),
            "candidate": self._frozen_report(0.9),
        })
        assert gate["ok"] is False

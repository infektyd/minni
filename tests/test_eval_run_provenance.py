"""Focused provenance tests for legacy recall-comparison runs.

Mocked only: no real retrieval, no model loads, no live database access.
Run with ``PYTHONPATH=src:. python3 -m pytest tests/test_eval_run_provenance.py``.
"""

import argparse
import hashlib
import json
import sys
import types
from pathlib import Path

from minni.eval import harness
from minni.eval.dataset import load_queries
from minni.eval.metrics import KNOWN_RETRIEVE_KWARGS
from minni.eval.provenance import (
    RUN_CAVEATS,
    build_report_provenance,
    canonical_queries_digest,
    code_provenance,
    corpus_provenance,
    environment_provenance,
    principal_provenance,
    query_file_provenance,
    retrieval_options_provenance,
)


def _write_queries(path: Path) -> None:
    rows = [
        {"query": "how do I migrate auth", "expected_doc_ids": [11, 12]},
        {"query": "rotate deploy keys", "expected_doc_ids": [13]},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


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


class TestQueryFileProvenance:
    def test_digests_distinguish_scored_rows_from_observed_bytes(self, tmp_path):
        path = tmp_path / "q.jsonl"
        _write_queries(path)
        loaded = load_queries(path)
        prov = query_file_provenance(path, path, loaded)
        assert prov["loaded_queries_digest"] == canonical_queries_digest(loaded)
        assert prov["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert prov["file_status"] == "hashed"
        assert "unverified" in prov["correspondence"]

    def test_mutation_between_load_and_metadata_keeps_scored_digest(self, tmp_path):
        path = tmp_path / "q.jsonl"
        _write_queries(path)
        loaded = load_queries(path)
        scored_digest = canonical_queries_digest(loaded)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"query": "injected later", "expected_doc_ids": [99]}) + "\n")
        prov = query_file_provenance(path, path, loaded)
        assert prov["loaded_queries_digest"] == scored_digest
        assert prov["loaded_queries_digest"] != canonical_queries_digest(load_queries(path))
        assert prov["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_missing_file_marks_unknown_without_crash(self, tmp_path):
        missing = tmp_path / "absent.jsonl"
        prov = query_file_provenance(missing, missing, [])
        assert prov["file_sha256"] is None
        assert prov["file_status"] == "missing"
        assert prov["loaded_queries_digest"] == canonical_queries_digest([])


class TestRetrievalOptionsProvenance:
    def test_requested_vs_effective_with_unknown_ignored(self):
        prov = retrieval_options_provenance(
            "with-expand", {"expand": True, "bogus_flag": 1}, KNOWN_RETRIEVE_KWARGS
        )
        assert prov["requested"] == {"expand": True, "bogus_flag": 1}
        assert prov["effective"]["expand"] is True
        assert prov["effective"]["limit"] == 10
        assert prov["effective"]["update_access"] is False
        assert prov["ignored_unknown"] == ["bogus_flag"]
        assert "bogus_flag" not in prov["effective"]


class TestPrincipalAndCorpusHonesty:
    def test_mock_is_marked_mock(self):
        principal = principal_provenance("mock", is_mock=True)
        assert principal["mock"] is True
        assert "plumbing only" in principal["note"]
        assert corpus_provenance(is_mock=True, retriever_name="mock")["snapshot"] == "mock"

    def test_live_backend_is_never_frozen_or_snapshot(self):
        for name in ("minnid", "sovrd", "baseline"):
            principal = principal_provenance(name, is_mock=False)
            assert principal["mock"] is False
            assert principal["frozen"] is False
            assert principal["snapshot"] == "unknown"
            assert "not a frozen snapshot" in principal["note"]
            corpus = corpus_provenance(is_mock=False, retriever_name=name)
            assert corpus["snapshot"] == "unknown"
            assert corpus["frozen"] is False
            assert "Live mutable database" in corpus["note"]

    def test_non_database_backends_are_not_described_as_live_db(self):
        file_corpus = corpus_provenance(is_mock=False, retriever_name="ripgrep")
        assert file_corpus["snapshot"] == "working-tree-files"
        assert "No database involved" in file_corpus["note"]
        raw_corpus = corpus_provenance(is_mock=False, retriever_name="raw-context")
        assert raw_corpus["snapshot"] == "working-tree-files"
        vendor_corpus = corpus_provenance(is_mock=False, retriever_name="vendor")
        assert vendor_corpus["snapshot"] == "unconfigured-placeholder"
        assert "Live mutable database" not in json.dumps(
            [file_corpus, raw_corpus, vendor_corpus]
        )


class TestSharedProvenanceShapes:
    def test_code_provenance_never_invents_a_digest(self):
        prov = code_provenance()
        assert "revision" in prov and "dirty" in prov
        assert prov["dirty"] in (True, False, None)
        if prov["revision"] != "unknown":
            assert len(prov["revision"]) == 40

    def test_environment_provenance_has_no_credentials(self):
        prov = environment_provenance()
        blob = json.dumps(prov).lower()
        assert "config" in prov and "dependencies" in prov
        assert "token" not in blob and "secret" not in blob and "password" not in blob

    def test_config_failure_reason_withholds_details(self, monkeypatch):
        secret = "sk-test-secret-abc123-from-env"
        mod = types.ModuleType("minni.config")

        def _getattr(name):
            raise ValueError(f"config exploded: {secret}")

        mod.__getattr__ = _getattr
        monkeypatch.setitem(sys.modules, "minni.config", mod)
        prov = environment_provenance()
        assert prov["config"]["available"] is False
        assert secret not in json.dumps(prov)
        assert prov["config"]["reason"] == "ValueError (details withheld)"

    def test_model_names_are_configured_defaults_and_timing_is_explained(self):
        prov = environment_provenance()
        if prov["config"]["available"]:
            assert "not observed inference" in prov["config"]["model_names_note"]
        assert any("constructor" in caveat for caveat in RUN_CAVEATS)


class TestCmdRunProvenance:
    def test_mock_run_writes_provenance_to_selected_output_dir(
        self, tmp_path, monkeypatch
    ):
        query_path = tmp_path / "queries.jsonl"
        _write_queries(query_path)
        out_dir = tmp_path / "private-reports"
        monkeypatch.chdir(tmp_path)
        harness.cmd_run(
            _namespace(queries=str(query_path), output_dir=str(out_dir))
        )
        payloads = sorted(out_dir.glob("*-mock.json"))
        assert payloads, "mock JSON report must land in the selected output dir"
        report = json.loads(payloads[0].read_text(encoding="utf-8"))
        prov = report["provenance"]
        assert prov["mock"] is True
        assert prov["retriever"] == "mock"
        assert prov["query_file"]["file_sha256"] == hashlib.sha256(
            query_path.read_bytes()
        ).hexdigest()
        assert prov["query_file"]["loaded_queries_digest"] == (
            canonical_queries_digest(load_queries(query_path))
        )
        assert "unverified" in prov["query_file"]["correspondence"]
        assert prov["run_order"] == ["mock"]
        assert prov["run_index"] == 0
        assert prov["timing_caveats"], "cache/timing caveats must be recorded"
        assert prov["human_review"] == "not-established"
        assert prov["corpus"]["snapshot"] == "mock"
        comparisons = sorted(out_dir.glob("*-comparison.md"))
        assert comparisons
        assert "Run Provenance" in comparisons[0].read_text(encoding="utf-8")

    def test_mock_retriever_without_mock_flag_still_summarized_as_mock(
        self, tmp_path, monkeypatch
    ):
        query_path = tmp_path / "queries.jsonl"
        _write_queries(query_path)
        out_dir = tmp_path / "private-reports"
        monkeypatch.chdir(tmp_path)
        harness.cmd_run(
            _namespace(queries=str(query_path), output_dir=str(out_dir), mock=False)
        )
        payloads = sorted(out_dir.glob("*-mock.json"))
        assert payloads
        report = json.loads(payloads[0].read_text(encoding="utf-8"))
        assert report["provenance"]["mock"] is True
        comparison = sorted(out_dir.glob("*-comparison.md"))[0].read_text(
            encoding="utf-8"
        )
        assert "Mock run: True" in comparison
        assert "`mock`" in comparison

    def test_default_output_dir_is_preserved(self):
        assert harness._resolve_reports_dir("").name == "reports"

    def test_run_order_matches_report_sequence(self):
        prov = build_report_provenance(
            query={"file_sha256": "abc", "file_status": "hashed"},
            code={"revision": "unknown", "dirty": None},
            retrieval={"requested": {}, "effective": {}},
            principal={"mock": True},
            corpus={"snapshot": "mock"},
            environment={},
            retriever_name="mock",
            run_index=1,
            run_order=["a", "mock"],
            started_iso="2026-09-05T00:00:00+00:00",
            mock=True,
        )
        assert prov["run_order"] == ["a", "mock"]
        assert prov["run_index"] == 1
        assert "certification" in prov and "none" in prov["certification"]

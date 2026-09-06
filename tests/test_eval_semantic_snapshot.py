"""Frozen semantic snapshot runner: isolation, ranking, and provenance.

Deterministic injected embedder only — no model inference, no network, no
live database, no live vault. Proves the semantic leg:

- constructs and searches over the disposable snapshot only (a failing
  live-DB/engine/model constructor is never reached);
- ranks by cosine similarity through the engine embedding interface;
- filters authorization-blocked and lifecycle-excluded rows BEFORE the
  output limit, never by expected_eligible judgment labels (a
  contradictory labeled-false but policy-readable row is still returned
  so the evaluator can observe the error);
- fails closed with no model, on expand/HyDE options, and on generation
  replacement at the same path;
- carries separate backend/config/provenance from the lexical baseline
  while inheriting the mandatory snapshot+manifest query binding.

Run with ``PYTHONPATH=src:. <venv>/bin/python -m pytest
tests/test_eval_semantic_snapshot.py``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from minni.eval import semantic_snapshot
from minni.eval.harness import _require_snapshot_query_binding, make_searcher
from minni.eval.provenance import corpus_provenance, principal_provenance
from minni.eval.semantic_snapshot import (
    SEMANTIC_BACKEND,
    SnapshotSemanticSearcher,
)
from minni.eval.study_snapshot import (
    StudySnapshotError,
    canonical_identity,
    check_materialized,
    manifest_digest_for,
    materialize_snapshot_db,
    prepare_snapshot,
    verify_snapshot,
)


class WordBinEmbedder:
    """Deterministic bag-of-words test double for the encode interface."""

    dim = 32

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        rows = []
        for text in texts:
            vec = np.zeros(self.dim)
            for word in str(text).lower().split():
                vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dim] += 1.0
            rows.append(vec)
        return np.array(rows)


def _record(source_doc_id, store, path, text, eligible=True, **overrides):
    row = {
        "source_doc_id": source_doc_id,
        "store": store,
        "artifact_path": path,
        "text": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "content_kind": "original",
        "review_state": "machine_proposed",
        "human_reviewed": False,
        "agent": "hans",
        "privacy_level": "private",
        "origin": "day-to-day cross-project memory",
        "expected_eligible": eligible,
    }
    row.update(overrides)
    return row


def _packet(records):
    packet = {
        "packet_version": "minni-study-export-v1",
        "principal": {"agent_id": "hans", "capabilities": ["search", "read"]},
        "store": {"store_id": "store-a", "origin": "day-to-day cross-project memories"},
        "source": {"origin": "day-to-day cross-project memories"},
        "authorization": {"claimed": "operator-authorized-study-export"},
        "records": records,
    }
    identity = canonical_identity(packet)
    packet["manifest"] = {"manifest_digest": manifest_digest_for(records, identity)}
    return packet


def _two_record_packet():
    return _packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes"),
        _record("doc-b1", "store-b", "project-b/launch.md", "beta launch notes"),
    ])


def _prepared(tmp_path, packet=None):
    dest = tmp_path / "snapshot"
    prepare_snapshot(packet or _two_record_packet(), dest)
    materialize_snapshot_db(dest)
    return dest


def test_semantic_ranks_most_similar_first(tmp_path):
    dest = _prepared(tmp_path)
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    assert searcher.backend == SEMANTIC_BACKEND
    results = searcher.search("alpha launch", limit=5)
    assert [r["doc_id"] for r in results] == [1, 2]
    top = results[0]
    assert top["retriever"] == SEMANTIC_BACKEND
    assert top["provenance"]["backend"] == SEMANTIC_BACKEND
    assert top["provenance"]["snapshot_id"] == searcher.snapshot_id
    assert top["provenance"]["semantic_only"] is True
    assert top["provenance"].get("lexical_only") is False


def test_filter_before_limit_uses_authorization_not_judgments(tmp_path):
    dest = _prepared(tmp_path, _packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes",
                privacy_level="blocked"),
        _record("doc-b1", "store-b", "project-b/launch.md", "beta launch notes"),
        _record("doc-c1", "store-c", "project-c/launch.md", "gamma launch notes"),
    ]))
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    # Query text is closest to the blocked doc; limit=1 must still serve a
    # policy-readable row. The block comes from real authorization metadata
    # (privacy_level=blocked denied by can_read_document), never from the
    # expected_eligible judgment labels.
    results = searcher.search("alpha launch notes", limit=1)
    assert len(results) == 1
    assert results[0]["doc_id"] != 1


def test_contradictory_judgment_labeled_false_still_returned(tmp_path):
    dest = _prepared(tmp_path, _packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes",
                eligible=False),
        _record("doc-b1", "store-b", "project-b/launch.md", "beta launch notes"),
    ]))
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    # The judgment says ineligible, but the policy metadata (own agent,
    # private, candidate) is readable: retrieval must return the row anyway
    # so the evaluator can observe the scoring error. Judgments score;
    # they never authorize. No answer leakage: the judgment label is never
    # consulted on the retrieval path.
    results = searcher.search("alpha launch notes", limit=5)
    assert [r["doc_id"] for r in results] == [1, 2]
    assert 1 in searcher.forbidden_doc_ids


def test_lifecycle_excluded_statuses_never_served(tmp_path):
    dest = _prepared(tmp_path, _packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes",
                page_status="superseded"),
        _record("doc-b1", "store-b", "project-b/launch.md", "beta launch notes"),
    ]))
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    results = searcher.search("alpha launch notes", limit=5)
    assert [r["doc_id"] for r in results] == [2]


def test_no_model_fails_closed_without_lexical_degrade(tmp_path, monkeypatch):
    dest = _prepared(tmp_path)
    monkeypatch.setattr(semantic_snapshot, "_default_embedder",
                        lambda: (None, "missing-model"))
    with pytest.raises(ValueError, match="refusing to degrade to lexical"):
        SnapshotSemanticSearcher(dest)


def test_rejects_expand_hyde_and_rerank_options(tmp_path):
    dest = _prepared(tmp_path)
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    for kwargs in ({"expand": True}, {"expand": "on"}, {"use_hyde": True},
                   {"hyde": True}, {"hybrid": True}, {"rerank": True},
                   {"use_reranker": True}, {"rerank_top_k": 5},
                   {"cross_encoder": True}):
        with pytest.raises(ValueError, match="semantic-only baseline"):
            searcher.search("alpha", **kwargs)
    # Explicitly-off values stay allowed; the claim is no rerank, and none
    # is performed.
    assert searcher.search("alpha", expand="off", rerank=False,
                           rerank_top_k=0, use_hyde=None)
    assert searcher.search("   ") == []


def test_degenerate_embedder_output_fails(tmp_path):
    dest = _prepared(tmp_path)

    class ZeroEmbedder:
        def encode(self, texts):
            return np.zeros((len(texts), 8))

    with pytest.raises(ValueError, match="zero vector"):
        SnapshotSemanticSearcher(dest, embedder=ZeroEmbedder(),
                                 embedder_name="test-zero")


def test_generation_replacement_fails_closed(tmp_path):
    import os
    import subprocess
    import sys

    home = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), home)
    materialize_snapshot_db(home)
    searcher = SnapshotSemanticSearcher(home, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    old_id = searcher.snapshot_id
    assert searcher.search("alpha", limit=5)

    # Replacement generation B is built fresh AT the same path in a FRESH
    # PROCESS: SovereignDB keeps process-wide per-path schema state, so an
    # in-process rebuild at a reused path would skip schema init — a
    # schema-cache hazard a real replacement never hits.
    b_packet = _packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes"),
        _record("doc-b1", "store-b", "project-b/launch.md", "completely newword text"),
    ])
    (tmp_path / "packet_b.json").write_text(json.dumps(b_packet))
    driver = tmp_path / "materialize_b.py"
    driver.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from minni.eval.study_snapshot import prepare_snapshot, materialize_snapshot_db\n"
        "packet = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "dest = Path(sys.argv[2])\n"
        "prepare_snapshot(packet, dest)\n"
        "info = materialize_snapshot_db(dest)\n"
        "print(info['snapshot_id'])\n"
    )
    shutil.rmtree(home)
    proc = subprocess.run(
        [sys.executable, str(driver), str(tmp_path / "packet_b.json"), str(home)],
        capture_output=True, text=True, timeout=120,
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "PYTHONPATH": "src"},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert verify_snapshot(home)["manifest"]["snapshot_id"] != old_id
    with pytest.raises(StudySnapshotError, match="changed since searcher initialization"):
        searcher.search("alpha", limit=5)


def test_no_live_db_engine_or_model_on_search(tmp_path, monkeypatch):
    from minni import db as db_mod
    from minni import retrieval as retrieval_mod

    def no_database(_self, *_args, **_kwargs):
        pytest.fail("semantic search must not construct a database")

    def no_engine(_self, *_args, **_kwargs):
        pytest.fail("semantic search must not construct a retrieval engine")

    def no_default_loader():
        pytest.fail("injected embedder must not trigger the default loader")

    dest = _prepared(tmp_path)
    monkeypatch.setattr(db_mod.SovereignDB, "__init__", no_database)
    monkeypatch.setattr(retrieval_mod.RetrievalEngine, "__init__", no_engine)
    monkeypatch.setattr(semantic_snapshot, "_default_embedder", no_default_loader)
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    first = searcher.search("alpha launch", limit=5)
    assert [r["doc_id"] for r in first] == [1, 2]
    # Deterministic: a repeat search ranks identically.
    assert searcher.search("alpha launch", limit=5) == first


def test_embedding_provenance_marks_injected_model(tmp_path):
    import hashlib as _hashlib

    dest = _prepared(tmp_path)
    manifest = json.loads((dest / "snapshot.json").read_text())
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    prov = searcher.embedding_provenance
    # Actual object identity, never the caller-supplied label: the injected
    # double must not be marked as the real model.
    assert prov["model"] == (
        f"{WordBinEmbedder.__module__}.{WordBinEmbedder.__qualname__}")
    assert prov["model"] != "test-wordbin"
    assert prov["caller_label"] == "test-wordbin"
    assert prov["revision"] == "unknown"
    assert prov["artifact"] == "unknown"
    assert prov["encoding"]["max_seq_length"] == "unknown"
    assert prov["dim"] == WordBinEmbedder.dim
    expected_digest = _hashlib.sha256(
        np.ascontiguousarray(searcher._matrix, dtype=np.float64).tobytes()
    ).hexdigest()
    assert prov["vector_sha256"] == expected_digest
    assert len(prov["vector_sha256"]) == 64
    assert prov["injected"] is True
    assert prov["vector_count"] == 2
    assert prov["snapshot_id"] == manifest["snapshot_id"]
    assert prov["manifest_digest"] == manifest["manifest_digest"]


def test_embedding_provenance_records_exposed_model_fields(tmp_path):
    class VersionedEmbedder(WordBinEmbedder):
        revision = "abc123"
        model_name_or_path = "/models/fake-embedder"
        max_seq_length = 512

    dest = _prepared(tmp_path)
    searcher = SnapshotSemanticSearcher(dest, embedder=VersionedEmbedder(),
                                        embedder_name="test-versioned")
    prov = searcher.embedding_provenance
    assert prov["revision"] == "abc123"
    assert prov["artifact"] == "/models/fake-embedder"
    assert prov["encoding"]["max_seq_length"] == "512"
    assert prov["injected"] is True


def test_query_binding_gate_covers_semantic_searcher(tmp_path):
    dest = _prepared(tmp_path)
    manifest = json.loads((dest / "snapshot.json").read_text())
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    bound = [{
        "query": "alpha launch",
        "expected_doc_ids": [1],
        "snapshot_id": manifest["snapshot_id"],
        "manifest_digest": manifest["manifest_digest"],
    }]
    _require_snapshot_query_binding(searcher, bound)
    stale = [dict(bound[0], snapshot_id="study-f" + "0" * 59)]
    with pytest.raises(ValueError, match="binding mismatch"):
        _require_snapshot_query_binding(searcher, stale)


def test_corpus_and_principal_provenance_are_semantic_separate(tmp_path):
    dest = _prepared(tmp_path)
    searcher = SnapshotSemanticSearcher(dest, embedder=WordBinEmbedder(),
                                        embedder_name="test-wordbin")
    corpus = corpus_provenance(
        is_mock=False, retriever_name="snapshot-semantic",
        snapshot_id=searcher.snapshot_id,
        manifest_digest=searcher.manifest_digest,
        model=searcher.embedding_provenance,
    )
    assert corpus["frozen"] is True
    assert corpus["snapshot"] == searcher.snapshot_id
    assert corpus["embedding"]["model"] == searcher.embedding_provenance["model"]
    assert corpus["embedding"]["caller_label"] == "test-wordbin"
    assert corpus["embedding"]["revision"] == "unknown"
    assert corpus["embedding"]["vector_sha256"] == (
        searcher.embedding_provenance["vector_sha256"])
    assert corpus["embedding"]["injected"] is True
    assert "plumbing only" in corpus["note"]
    principal = principal_provenance(
        "snapshot-semantic", is_mock=False, principal=searcher._principal)
    assert principal["backend"] == "snapshot-semantic"
    assert principal["scope"] == (
        "prepared snapshot vault only (least-privilege study principal)")
    unknown = corpus_provenance(is_mock=False, retriever_name="snapshot-semantic")
    assert unknown["snapshot"] == "unknown"
    assert unknown["frozen"] is False


def test_make_searcher_routes_semantic_without_model_load(tmp_path, monkeypatch):
    dest = _prepared(tmp_path)
    monkeypatch.setattr(semantic_snapshot, "_default_embedder",
                        lambda: (WordBinEmbedder(), "test-wordbin"))
    searcher = make_searcher("snapshot-semantic", [], root=dest)
    assert isinstance(searcher, SnapshotSemanticSearcher)
    assert searcher.embedding_provenance["injected"] is False
    # Loader-resolved identity, not the injected object: the default path
    # reports the actual resolved model name.
    assert searcher.embedding_provenance["model"] == "test-wordbin"
    assert searcher.embedding_provenance["caller_label"] is None
    assert searcher.search("alpha launch", limit=5)[0]["doc_id"] == 1
    with pytest.raises(ValueError, match="prepared snapshot directory"):
        make_searcher("snapshot-semantic", [])

"""Bounded study-snapshot foundation: rejection, determinism, isolation.

Proves, without models, network, or the live database:

- tampered manifests, duplicate cross-store identities, duplicate content,
  unsafe artifact paths, and malformed packets are rejected;
- opaque study-ID remapping is deterministic;
- preparation calls no model and no live-database constructor;
- materialization keeps every DB/index/vault path disposable and never the
  live DEFAULT_CONFIG;
- machine_proposed review state is never human_reviewed.

Run with ``PYTHONPATH=src:. <venv>/bin/python -m pytest
tests/test_eval_study_snapshot.py``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import stat
import time
from pathlib import Path

import pytest

from minni.eval import study_snapshot
from minni.eval.study_snapshot import (
    StudySnapshotError,
    deterministic_remapping,
    manifest_digest_for,
    materialize_snapshot_db,
    prepare_snapshot,
    snapshot_id_for,
    validate_export_packet,
)


def _record(source_doc_id="doc-a1", store="store-a", path="project-a/note.md",
            text="alpha launch notes", kind="original", eligible=True, **overrides):
    row = {
        "source_doc_id": source_doc_id,
        "store": store,
        "artifact_path": path,
        "text": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "content_kind": kind,
        "review_state": "machine_proposed",
        "human_reviewed": False,
        "agent": "hans",
        "privacy_level": "private",
        "origin": "day-to-day cross-project memory",
        "expected_eligible": eligible,
    }
    if kind == "excerpt":
        row["source_locator"] = overrides.pop("source_locator", "vault:original#1-3")
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
        "manifest": {"manifest_digest": manifest_digest_for(records)},
    }
    return packet


def _two_record_packet():
    return _packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes"),
        _record("doc-b1", "store-b", "project-b/launch.md", "beta launch notes", eligible=True),
    ])


def test_valid_packet_reports_machine_proposed_not_human_reviewed():
    records = validate_export_packet(_two_record_packet())
    assert len(records) == 2
    for row in records:
        assert row["review_state"] == "machine_proposed"
        assert row["human_reviewed"] is False


def test_rejects_tampered_manifest():
    # Flip an annotation (content digest stays valid) so the failure under
    # test is manifest integrity, not the content check.
    packet = _two_record_packet()
    packet["records"][0]["expected_eligible"] = False
    with pytest.raises(StudySnapshotError, match="manifest_digest"):
        validate_export_packet(packet)


def test_rejects_tampered_content_digest():
    packet = _two_record_packet()
    packet["records"][0]["content_sha256"] = "0" * 64
    with pytest.raises(StudySnapshotError, match="content_sha256"):
        validate_export_packet(packet)


def test_rejects_duplicate_cross_store_identity():
    packet = _packet([
        _record("same-id", "store-a", "project-a/a.md", "text one here"),
        _record("same-id", "store-b", "project-b/b.md", "text two here"),
    ])
    with pytest.raises(StudySnapshotError, match="duplicate source identity"):
        validate_export_packet(packet)


def test_rejects_duplicate_content_digest():
    packet = _packet([
        _record("doc-a1", "store-a", "project-a/a.md", "identical bytes here"),
        _record("doc-b1", "store-b", "project-b/b.md", "identical bytes here"),
    ])
    with pytest.raises(StudySnapshotError, match="duplicate content digest"):
        validate_export_packet(packet)


@pytest.mark.parametrize("bad_path", [
    "/absolute/note.md",
    "../escape/note.md",
    "project-a/../escape.md",
    "project-a/note.txt",
    "project-a\\note.md",
    "",
])
def test_rejects_unsafe_artifact_path(bad_path):
    # Path checks run before the manifest check, so any packet carrying an
    # unsafe artifact path is rejected regardless of its digest.
    packet = _packet([_record("doc-a1", "store-a", bad_path, "some text here")])
    with pytest.raises(StudySnapshotError):
        validate_export_packet(packet)


def test_rejects_human_reviewed_claim():
    packet = _two_record_packet()
    packet["records"][0]["review_state"] = "human_reviewed"
    packet["records"][0]["human_reviewed"] = True
    with pytest.raises(StudySnapshotError, match="never human-reviewed"):
        validate_export_packet(packet)


def test_rejects_missing_excerpt_label_and_locator():
    packet = _two_record_packet()
    del packet["records"][0]["content_kind"]
    with pytest.raises(StudySnapshotError, match="content_kind"):
        validate_export_packet(packet)
    packet = _two_record_packet()
    packet["records"][0]["content_kind"] = "excerpt"
    with pytest.raises(StudySnapshotError, match="source_locator"):
        validate_export_packet(packet)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(packet_version="v9"),
    lambda p: p.update(records=[]),
    lambda p: p["records"][0].update(expected_eligible="yes"),
    lambda p: p["records"][0].update(agent=""),
    lambda p: p["authorization"].update(claimed=""),
    lambda p: p.update(principal={"agent_id": ""}),
])
def test_rejects_malformed_packets(mutation):
    packet = _two_record_packet()
    mutation(packet)
    with pytest.raises(StudySnapshotError):
        validate_export_packet(packet)


def test_deterministic_remapping_is_stable_and_opaque():
    records = validate_export_packet(_two_record_packet())
    first = deterministic_remapping(records)
    second = deterministic_remapping(list(reversed(records)))
    assert first == second
    assert sorted(first) == ["study-0001", "study-0002"]
    # Sorted by (store, source_doc_id): store-a/doc-a1 comes first.
    assert first["study-0001"]["source_doc_id"] == "doc-a1"
    assert first["study-0002"]["source_doc_id"] == "doc-b1"
    for study_id, row in first.items():
        assert "doc-a1" not in study_id and "doc-b1" not in study_id
        # Cross-project eligibility, ownership, privacy, and origin survive.
        assert row["expected_eligible"] is True
        assert row["agent"] == "hans" and row["privacy_level"] == "private"
        assert row["origin"] == "day-to-day cross-project memory"


def test_snapshot_id_derives_from_manifest_only():
    digest = "ab" * 32
    assert snapshot_id_for(digest) == "study-abababababababab"
    assert "live" not in snapshot_id_for(digest)


def test_preparation_calls_no_model_or_live_database(tmp_path, monkeypatch):
    from minni import db as db_mod
    from minni import retrieval as retrieval_mod

    def no_database(_self, *_args, **_kwargs):
        pytest.fail("preparation must not construct a database")

    def no_model(_self):
        pytest.fail("preparation must not load a model")

    monkeypatch.setattr(db_mod.SovereignDB, "__init__", no_database)
    monkeypatch.setattr(retrieval_mod.RetrievalEngine, "model", property(no_model))

    dest = tmp_path / "snapshot"
    manifest = prepare_snapshot(_two_record_packet(), dest)
    assert manifest["snapshot_id"].startswith("study-")
    assert manifest["human_reviewed"] is False
    assert manifest["review_state"] == "machine_proposed"
    assert "not independently verified" in manifest["principal"]["provenance_note"]

    mode = stat.S_IMODE(dest.stat().st_mode)
    assert mode == 0o700
    for name in ("snapshot.json", "mapping.json", "vault/project-a/launch.md"):
        target = dest / name
        assert target.is_file() and not target.is_symlink()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_prepare_snapshot_is_deterministic(tmp_path):
    first = prepare_snapshot(_two_record_packet(), tmp_path / "one")
    second = prepare_snapshot(_two_record_packet(), tmp_path / "two")
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["manifest_digest"] == second["manifest_digest"]
    assert (tmp_path / "one" / "mapping.json").read_text() == \
        (tmp_path / "two" / "mapping.json").read_text()


def test_materialize_keeps_everything_disposable_and_off_live_config(tmp_path, monkeypatch):
    from minni import config as config_mod
    from minni import db as db_mod

    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)

    live_config = config_mod.DEFAULT_CONFIG
    opened = []

    original = db_mod.SovereignDB.__init__

    def track_database(self, config):
        opened.append(config)
        original(self, config)

    monkeypatch.setattr(db_mod.SovereignDB, "__init__", track_database)

    result = materialize_snapshot_db(dest)
    assert len(opened) == 1
    config = opened[0]
    assert config is not live_config
    for attr in ("db_path", "vault_path", "faiss_index_path", "graph_export_dir"):
        assert str(Path(getattr(config, attr)).resolve()).startswith(str(dest.resolve())), attr
    assert config.writeback_enabled is False
    assert result["snapshot_id"].startswith("study-")
    assert sorted(result["document_ids"]) == ["study-0001", "study-0002"]
    assert Path(result["db_path"]).is_file()


def test_snapshot_searcher_never_uses_live_default_config(tmp_path, monkeypatch):
    from minni import config as config_mod
    from minni import db as db_mod
    from minni import retrieval as retrieval_mod
    from minni.eval.retrievers import SnapshotSearcher, make_searcher

    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    materialize_snapshot_db(dest)

    live_config = config_mod.DEFAULT_CONFIG
    seen = []

    def fake_db_init(self, config):
        seen.append(("db", config))

    def fake_engine_init(self, db, config=None, **kwargs):
        seen.append(("engine", config))
        self.db, self.config = db, config

    monkeypatch.setattr(db_mod.SovereignDB, "__init__", fake_db_init)
    monkeypatch.setattr(retrieval_mod.RetrievalEngine, "__init__", fake_engine_init)

    searcher = make_searcher("snapshot", root=dest)
    assert isinstance(searcher, SnapshotSearcher)
    object.__new__(SnapshotSearcher)  # device under test supports pure-mock setup
    searcher._ensure_engine()
    assert seen, "expected isolated constructors to be recorded"
    for _kind, config in seen:
        assert config is not live_config
        for attr in ("db_path", "vault_path", "faiss_index_path", "graph_export_dir"):
            assert str(Path(getattr(config, attr)).resolve()).startswith(str(dest.resolve())), attr

    with pytest.raises(ValueError, match="prepared snapshot directory"):
        make_searcher("snapshot")


def test_snapshot_end_to_end_lexical_without_models(tmp_path, monkeypatch):
    from minni import retrieval as retrieval_mod
    from minni.eval.retrievers import SnapshotSearcher

    def no_model(_self):
        pytest.fail("snapshot lexical search must not load a model")

    monkeypatch.setattr(retrieval_mod.RetrievalEngine, "model", property(no_model))

    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    materialize_snapshot_db(dest)

    searcher = SnapshotSearcher(dest)
    assert searcher.snapshot_id.startswith("study-")
    # Expired deadline exercises the FTS path with the real engine and real
    # disposable DB, exactly like the fixture's lexical profile.
    results = searcher.search("launch", limit=5, deadline_monotonic=time.monotonic())
    assert {r["source"].split("vault/")[-1] for r in results} >= {
        "project-a/launch.md", "project-b/launch.md",
    }
    assert searcher._principal.allowed_vault_roots == [str(dest / "vault")]


def test_snapshot_provenance_labels_are_honest():
    from minni.eval.provenance import corpus_provenance, principal_provenance

    corpus = corpus_provenance(is_mock=False, retriever_name="snapshot")
    assert corpus["snapshot"] == "study-frozen" and corpus["frozen"] is True
    assert "never" in corpus["note"] and "live corpus" in corpus["note"]
    principal = principal_provenance("snapshot", is_mock=False)
    assert principal["supplied"] is True
    assert "not independently verified" in principal["note"]

    assert "live" in study_snapshot.__doc__.lower()


def test_prepare_rejects_existing_non_private_destination(tmp_path):
    dest = tmp_path / "snapshot"
    dest.mkdir()
    dest.chmod(0o755)  # chmod, not mkdir: mkdir mode is masked by the process umask
    with pytest.raises(StudySnapshotError, match="private"):
        prepare_snapshot(_two_record_packet(), dest)


def test_packet_without_records_is_malformed():
    packet = _two_record_packet()
    packet["records"] = "not-a-list"
    with pytest.raises(StudySnapshotError, match="non-empty list"):
        validate_export_packet(packet)
    assert copy.deepcopy(packet) is not None

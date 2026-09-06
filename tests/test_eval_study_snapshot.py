"""Bounded study-snapshot foundation: rejection, determinism, isolation.

Proves, without models, network, live memories, or the live database:

- source identity is the (store, source_doc_id) tuple; the same document
  number in two stores names two documents, while a repeated tuple is
  rejected;
- identical bytes under separate ownership are retained and linked through
  a shared content group, never silently conflated;
- the snapshot digest binds canonical source/principal/authorization
  metadata AND lifecycle fields;
- hard count/text/total-byte limits fire before expensive work;
- frozen files and metadata re-validate before every materialization and
  search (symlinks, tampered bytes, inconsistent mappings, stale-output
  mixing all fail);
- original lifecycle/privacy/review provenance is preserved separately from
  the study machine judgment, which is never human-reviewed;
- private outputs and the disposable backend cannot target live/default
  paths, and authorization stays a supplied claim, not authentication proof.

Run with ``PYTHONPATH=src:. <venv>/bin/python -m pytest
tests/test_eval_study_snapshot.py``.
"""

from __future__ import annotations

import hashlib
import json
import stat
import time
from pathlib import Path

import pytest

from minni.eval import study_snapshot
from minni.eval.study_snapshot import (
    MAX_RECORDS,
    MAX_TEXT_CHARS,
    MAX_TOTAL_TEXT_CHARS,
    StudySnapshotError,
    canonical_identity,
    check_materialized,
    content_group_for,
    content_groups_for,
    deterministic_remapping,
    manifest_digest_for,
    materialize_snapshot_db,
    prepare_snapshot,
    snapshot_id_for,
    source_identity_key,
    validate_export_packet,
    verify_snapshot,
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


def _packet(records, *, principal=None, authorization=None):
    packet = {
        "packet_version": "minni-study-export-v1",
        "principal": principal or {"agent_id": "hans", "capabilities": ["search", "read"]},
        "store": {"store_id": "store-a", "origin": "day-to-day cross-project memories"},
        "source": {"origin": "day-to-day cross-project memories"},
        "authorization": authorization or {"claimed": "operator-authorized-study-export"},
        "records": records,
    }
    identity = canonical_identity(packet)
    packet["manifest"] = {"manifest_digest": manifest_digest_for(records, identity)}
    return packet


def _two_record_packet():
    return _packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes"),
        _record("doc-b1", "store-b", "project-b/launch.md", "beta launch notes", eligible=True),
    ])


def _mapping_records(dest: Path):
    return json.loads((dest / "mapping.json").read_text())["records"]


def test_valid_packet_reports_machine_proposed_not_human_reviewed():
    records = validate_export_packet(_two_record_packet())
    assert len(records) == 2
    for row in records:
        assert row["review_state"] == "machine_proposed"
        assert row["human_reviewed"] is False


def test_source_identity_is_store_tuple_not_bare_number():
    assert source_identity_key("store-a", "7") == ("store-a", "7")
    assert source_identity_key("store-a", "7") != source_identity_key("store-b", "7")
    # The same document number in two stores names two documents.
    records = validate_export_packet(_packet([
        _record("7", "store-a", "project-a/a.md", "text one here"),
        _record("7", "store-b", "project-b/b.md", "text two here"),
    ]))
    assert {(row["store"], row["source_doc_id"]) for row in records} == {
        ("store-a", "7"), ("store-b", "7")}


def test_rejects_duplicate_store_tuple_identity():
    packet = _packet([
        _record("same-id", "store-a", "project-a/a.md", "text one here"),
        _record("same-id", "store-a", "project-b/b.md", "text two here"),
    ])
    with pytest.raises(StudySnapshotError, match="duplicate source identity"):
        validate_export_packet(packet)


def test_rejects_duplicate_artifact_path():
    packet = _packet([
        _record("doc-a1", "store-a", "project-a/a.md", "text one"),
        _record("doc-b1", "store-b", "project-a/a.md", "text two"),
    ])
    with pytest.raises(StudySnapshotError, match="duplicate artifact path"):
        validate_export_packet(packet)


def test_duplicate_content_is_retained_and_linked_not_conflated(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_packet([
        _record("doc-a1", "store-a", "project-a/a.md", "identical bytes here"),
        _record("doc-b1", "store-b", "project-b/b.md", "identical bytes here"),
    ]), dest)
    mapping = _mapping_records(dest)
    groups = content_groups_for(mapping)
    assert len(groups) == 1
    group, members = next(iter(groups.items()))
    assert sorted(members) == ["study-0001", "study-0002"]
    assert mapping["study-0001"]["content_group"] == group == mapping["study-0002"]["content_group"]
    # Separate ownership survives: two identities, two vault files, one group.
    assert (mapping["study-0001"]["store"], mapping["study-0001"]["source_doc_id"]) != \
        (mapping["study-0002"]["store"], mapping["study-0002"]["source_doc_id"])
    assert group == content_group_for(mapping["study-0001"]["content_sha256"])

    verified = verify_snapshot(dest)
    assert verified["manifest"]["content_groups"] == groups
    materialized = materialize_snapshot_db(dest)
    assert sorted(materialized["document_ids"]) == ["study-0001", "study-0002"]
    assert sorted(check_materialized(dest)["document_ids"]) == ["study-0001", "study-0002"]

    envelope = json.loads((dest / "mapping.json").read_text())
    envelope["records"]["study-0002"]["content_group"] = "content-forged"
    (dest / "mapping.json").write_text(json.dumps(envelope))
    with pytest.raises(StudySnapshotError, match="content group"):
        verify_snapshot(dest)


def test_digest_binds_identity_and_lifecycle_not_just_text():
    base = _two_record_packet()
    validate_export_packet(base)

    tampered_principal = _two_record_packet()
    tampered_principal["principal"] = {"agent_id": "mallory", "capabilities": ["search"]}
    with pytest.raises(StudySnapshotError, match="manifest_digest"):
        validate_export_packet(tampered_principal)

    tampered_auth = _two_record_packet()
    tampered_auth["authorization"] = {"claimed": "self-granted"}
    with pytest.raises(StudySnapshotError, match="manifest_digest"):
        validate_export_packet(tampered_auth)

    tampered_lifecycle = _two_record_packet()
    tampered_lifecycle["records"][0]["page_status"] = "superseded"
    with pytest.raises(StudySnapshotError, match="manifest_digest"):
        validate_export_packet(tampered_lifecycle)

    tampered_kind = _two_record_packet()
    tampered_kind["records"][0]["content_kind"] = "excerpt"
    tampered_kind["records"][0]["source_locator"] = "vault:x#1-2"
    with pytest.raises(StudySnapshotError, match="manifest_digest"):
        validate_export_packet(tampered_kind)


def test_rejects_tampered_manifest():
    packet = _two_record_packet()
    packet["records"][0]["expected_eligible"] = False
    with pytest.raises(StudySnapshotError, match="manifest_digest"):
        validate_export_packet(packet)


def test_rejects_tampered_content_digest():
    packet = _two_record_packet()
    packet["records"][0]["content_sha256"] = "0" * 64
    with pytest.raises(StudySnapshotError, match="content_sha256"):
        validate_export_packet(packet)


def test_rejects_unknown_record_fields():
    packet = _two_record_packet()
    packet["records"][0]["owner_note"] = "unbound side channel"
    with pytest.raises(StudySnapshotError, match="unknown fields"):
        validate_export_packet(packet)


def test_hard_limits_fire_before_expensive_work():
    too_many = [_record(f"doc-{i}", "store-a", f"project-a/{i}.md", f"text {i}")
                for i in range(MAX_RECORDS + 1)]
    with pytest.raises(StudySnapshotError, match="at most"):
        validate_export_packet(_packet(too_many))

    oversized = "x" * (MAX_TEXT_CHARS + 1)
    with pytest.raises(StudySnapshotError, match="at most"):
        validate_export_packet(_packet([_record("big", "store-a", "project-a/big.md", oversized)]))

    chunk = "y" * 90_000  # under the per-record cap so the total cap fires
    needed = MAX_TOTAL_TEXT_CHARS // len(chunk) + 1
    heavy = [_record(f"doc-{i}", "store-a", f"project-a/{i}.md", chunk + str(i))
             for i in range(needed)]
    with pytest.raises(StudySnapshotError, match="exceeds"):
        validate_export_packet(_packet(heavy))


@pytest.mark.parametrize("bad_path", [
    "/absolute/note.md",
    "../escape/note.md",
    "project-a/../escape.md",
    "project-a/note.txt",
    "project-a\\note.md",
    "",
])
def test_rejects_unsafe_artifact_path(bad_path):
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


def test_excerpt_round_trip_preserves_locator_and_binds_it(tmp_path):
    packet = _packet([
        _record("excerpt-1", "store-a", "project-a/excerpt.md", "excerpt bytes",
                kind="excerpt", source_locator="vault:original#10-12"),
    ])
    dest = tmp_path / "snapshot"
    prepare_snapshot(packet, dest)
    verify_snapshot(dest)
    materialize_snapshot_db(dest)
    check_materialized(dest)

    mapping = _mapping_records(dest)
    assert mapping["study-0001"]["source_provenance"]["source_locator"] == \
        "vault:original#10-12"

    packet["records"][0]["source_locator"] = "vault:original#13-15"
    with pytest.raises(StudySnapshotError, match="manifest_digest"):
        validate_export_packet(packet)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(packet_version="v9"),
    lambda p: p.update(records=[]),
    lambda p: p["records"][0].update(expected_eligible="yes"),
    lambda p: p["records"][0].update(agent=""),
    lambda p: p["authorization"].update(claimed=""),
    lambda p: p.update(principal={"agent_id": ""}),
    lambda p: p["records"][0].update(source_detail={"nested": {"deep": 1}}),
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
    assert first["study-0001"]["source_doc_id"] == "doc-a1"
    assert first["study-0002"]["source_doc_id"] == "doc-b1"
    for study_id, row in first.items():
        assert "doc-a1" not in study_id and "doc-b1" not in study_id
        # Study machine judgment stays separate from original provenance.
        assert row["study_judgment"] == {
            "review_state": "machine_proposed", "human_reviewed": False,
            "expected_eligible": True,
        }
        assert row["source_provenance"]["agent"] == "hans"
        assert row["source_provenance"]["privacy_level"] == "private"
        assert row["source_provenance"]["origin"] == "day-to-day cross-project memory"
        assert row["source_provenance"]["page_status"] == "candidate"
        assert row["expected_eligible"] is True  # flat mirror matches judgment


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
    assert "not authentication proof" in manifest["principal"]["provenance_note"]
    assert manifest["identity"]["authorization"] == {"claimed": "operator-authorized-study-export"}

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


def test_prepare_refuses_live_destination(tmp_path, monkeypatch):
    fake_live = tmp_path / "live-vault"
    fake_live.mkdir()
    monkeypatch.setattr(study_snapshot, "_live_path_set", lambda: {str(fake_live)})
    with pytest.raises(StudySnapshotError, match="live/default path"):
        prepare_snapshot(_two_record_packet(), fake_live / "study")
    with pytest.raises(StudySnapshotError, match="live/default path"):
        prepare_snapshot(_two_record_packet(), tmp_path)  # live path inside dest


def test_verify_rejects_symlink_tamper_and_mapping_drift(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    verified = verify_snapshot(dest)
    assert verified["manifest"]["record_count"] == 2

    tampered = dest / "vault" / "project-a" / "launch.md"
    tampered.write_text("edited bytes")
    with pytest.raises(StudySnapshotError, match="tamper"):
        verify_snapshot(dest)


def test_verify_rejects_symlinked_vault_file(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    target = dest / "vault" / "project-a" / "launch.md"
    text = target.read_text()
    target.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text(text)
    target.symlink_to(outside)
    with pytest.raises(StudySnapshotError, match="symlink"):
        verify_snapshot(dest)


def test_verify_rejects_unmapped_vault_file_and_mixed_mapping(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    (dest / "vault" / "project-a" / "stowaway.md").write_text("stale record")
    with pytest.raises(StudySnapshotError, match="unmapped"):
        verify_snapshot(dest)

    other = tmp_path / "other"
    prepare_snapshot(_packet([
        _record("doc-x", "store-a", "project-a/x.md", "different bytes here"),
    ]), other)
    envelope = json.loads((dest / "mapping.json").read_text())
    foreign = json.loads((other / "mapping.json").read_text())
    envelope["records"]["study-0001"] = foreign["records"]["study-0001"]
    (dest / "mapping.json").write_text(json.dumps(envelope))
    # Bring the grafted record's bytes along so the failure under test is
    # output mixing (digest mismatch), not a missing file.
    (dest / "vault" / "project-a" / "launch.md").unlink()
    (dest / "vault" / "project-a" / "stowaway.md").unlink()
    grafted = dest / "vault" / "project-a" / "x.md"
    grafted.write_text((other / "vault" / "project-a" / "x.md").read_text())
    with pytest.raises(StudySnapshotError, match="mix|tamper|digest"):
        verify_snapshot(dest)


def test_materialize_preserves_original_lifecycle_and_refuses_rerun(tmp_path, monkeypatch):
    from minni import config as config_mod
    from minni import db as db_mod

    dest = tmp_path / "snapshot"
    packet = _packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes",
                page_status="superseded", page_type="decision"),
        _record("doc-b1", "store-b", "project-b/launch.md", "beta launch notes",
                privacy_level="safe"),
    ])
    prepare_snapshot(packet, dest)

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
    assert sorted(result["document_ids"]) == ["study-0001", "study-0002"]

    import sqlite3
    rows = sqlite3.connect(result["db_path"]).execute(
        "SELECT agent, privacy_level, page_status, page_type FROM documents ORDER BY doc_id"
    ).fetchall()
    assert rows[0] == ("hans", "private", "superseded", "decision")
    assert rows[1] == ("hans", "safe", "candidate", "unknown")

    materialized = check_materialized(dest)
    assert materialized["manifest_digest"] == result["manifest_digest"]
    with pytest.raises(StudySnapshotError, match="once per prepared directory"):
        materialize_snapshot_db(dest)


def test_snapshot_search_constructs_no_engine_db_or_model(tmp_path, monkeypatch):
    from minni import db as db_mod
    from minni import retrieval as retrieval_mod
    from minni.eval.retrievers import SnapshotSearcher, make_searcher

    def no_database(_self, *_args, **_kwargs):
        pytest.fail("snapshot search must not construct a database")

    def no_engine(_self, *_args, **_kwargs):
        pytest.fail("snapshot search must not construct a retrieval engine")

    def no_model(_self):
        pytest.fail("snapshot search must not load a model")

    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    # Materialization legitimately builds the disposable isolated DB; the
    # fail-closed guards below cover searcher construction and search only.
    materialize_snapshot_db(dest)
    monkeypatch.setattr(db_mod.SovereignDB, "__init__", no_database)
    monkeypatch.setattr(retrieval_mod.RetrievalEngine, "__init__", no_engine)
    monkeypatch.setattr(retrieval_mod.RetrievalEngine, "model", property(no_model))

    searcher = make_searcher("snapshot", root=dest)
    assert isinstance(searcher, SnapshotSearcher)
    assert searcher.snapshot_id.startswith("study-")
    # Known relevant rows come back with no engine, DB handle, or model.
    results = searcher.search("alpha launch", limit=5)
    assert [r["source"].split("vault/")[-1] for r in results] == ["project-a/launch.md"]
    assert results[0]["provenance"]["snapshot_id"] == searcher.snapshot_id
    assert results[0]["provenance"]["lexical_only"] is True
    # Caller deadlines are ignored, never forwarded: expiry semantics,
    # present or future, cannot empty snapshot results.
    expired = searcher.search("alpha launch", limit=5, deadline_monotonic=0.0)
    assert [r["doc_id"] for r in expired] == [r["doc_id"] for r in results]
    assert searcher._principal.allowed_vault_roots == [str(dest / "vault")]

    with pytest.raises(ValueError, match="prepared snapshot directory"):
        make_searcher("snapshot")

def test_snapshot_searcher_revalidates_before_every_search(tmp_path, monkeypatch):
    import sqlite3

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
    results = searcher.search("launch", limit=5, deadline_monotonic=time.monotonic())
    assert {r["source"].split("vault/")[-1] for r in results} >= {
        "project-a/launch.md", "project-b/launch.md",
    }
    assert searcher._principal.allowed_vault_roots == [str(dest / "vault")]

    connection = sqlite3.connect(dest / "study.db")
    connection.execute("UPDATE documents SET agent='mallory' WHERE doc_id=1")
    connection.execute("UPDATE vault_fts SET content='evil content' WHERE doc_id=1")
    connection.commit()
    connection.close()
    with pytest.raises(StudySnapshotError, match="tamper"):
        searcher.search("launch", limit=5)

    # Tampering after open fails the next search instead of serving bad bytes.
    (dest / "vault" / "project-b" / "launch.md").write_text("edited after open")
    with pytest.raises(StudySnapshotError, match="tamper"):
        searcher.search("launch", limit=5)


def test_snapshot_provenance_labels_are_honest():
    from minni.eval.provenance import corpus_provenance, principal_provenance

    # Fail closed: no verified snapshot ID means unknown, never frozen.
    missing = corpus_provenance(is_mock=False, retriever_name="snapshot")
    assert missing["snapshot"] == "unknown" and missing["frozen"] is False
    unknown = corpus_provenance(is_mock=False, retriever_name="snapshot", snapshot_id="unknown")
    assert unknown["snapshot"] == "unknown" and unknown["frozen"] is False
    corpus = corpus_provenance(
        is_mock=False, retriever_name="snapshot", snapshot_id="study-abc123",
        manifest_digest="digest-abc123")
    assert corpus["snapshot"] == "study-abc123" and corpus["frozen"] is True
    assert corpus["manifest_digest"] == "digest-abc123"
    assert "live corpus" in corpus["note"]
    from minni.principal import EffectivePrincipal

    uninitialized = principal_provenance("snapshot", is_mock=False)
    assert uninitialized["supplied"] is True
    assert "not initialized" in uninitialized["note"]
    principal = principal_provenance(
        "snapshot", is_mock=False,
        principal=EffectivePrincipal(
            agent_id="hans", capabilities=["search", "read"],
            allowed_vault_roots=["/private/snap/study-01/vault"]),
    )
    assert principal["supplied"] is True
    assert principal["agent_id"] == "hans"
    assert principal["allowed_vault_roots"] == ["/private/snap/study-01/vault"]

    assert "live" in study_snapshot.__doc__.lower()


def test_snapshot_provenance_preserves_manifest_identity():
    from minni.eval.provenance import corpus_provenance

    corpus = corpus_provenance(
        is_mock=False,
        retriever_name="snapshot",
        snapshot_id="study-0123456789abcdef",
        manifest_digest="0123456789abcdef" * 4,
    )

    assert corpus["snapshot"] == "study-0123456789abcdef"
    assert corpus["manifest_digest"] == "0123456789abcdef" * 4
    assert corpus["frozen"] is True


def test_prepare_rejects_existing_non_private_destination(tmp_path):
    dest = tmp_path / "snapshot"
    dest.mkdir()
    dest.chmod(0o755)  # chmod, not mkdir: mkdir mode is masked by the process umask
    with pytest.raises(StudySnapshotError, match="private"):
        prepare_snapshot(_two_record_packet(), dest)


def test_source_detail_is_preserved_verbatim_and_bound(tmp_path):
    packet = _packet([
        _record("doc-a1", "store-a", "project-a/a.md", "text one here",
                source_detail={"collector": "parent-export", "batch": 3}),
    ])
    dest = tmp_path / "snapshot"
    manifest = prepare_snapshot(packet, dest)
    mapping = _mapping_records(dest)
    assert mapping["study-0001"]["source_provenance"]["source_detail"] == {
        "collector": "parent-export", "batch": 3}
    assert mapping["study-0001"]["study_judgment"]["review_state"] == "machine_proposed"
    assert manifest["human_reviewed"] is False
    verify_snapshot(dest)


def test_packet_without_records_is_malformed():
    packet = _two_record_packet()
    packet["records"] = "not-a-list"
    with pytest.raises(StudySnapshotError, match="non-empty list"):
        validate_export_packet(packet)


def _rewrite_manifest(dest: Path, mutate):
    manifest = json.loads((dest / "snapshot.json").read_text())
    mutate(manifest)
    (dest / "snapshot.json").write_text(json.dumps(manifest))


def test_verify_rejects_edited_principal_mirror_and_identity_block(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)

    tampered = tmp_path / "tampered"
    prepare_snapshot(_two_record_packet(), tampered)
    _rewrite_manifest(tampered, lambda m: m["principal"].update(agent_id="changed-principal"))
    with pytest.raises(StudySnapshotError, match="principal mirror"):
        verify_snapshot(tampered)

    forged = tmp_path / "forged"
    prepare_snapshot(_two_record_packet(), forged)
    _rewrite_manifest(forged, lambda m: m["identity"]["principal"].update(agent_id="changed-principal"))
    with pytest.raises(StudySnapshotError, match="mirror|tamper"):
        verify_snapshot(forged)

    consistent = tmp_path / "consistent"
    prepare_snapshot(_two_record_packet(), consistent)

    def forge_both(manifest):
        manifest["identity"]["principal"]["agent_id"] = "changed-principal"
        manifest["principal"]["agent_id"] = "changed-principal"

    _rewrite_manifest(consistent, forge_both)
    with pytest.raises(StudySnapshotError, match="tamper"):
        verify_snapshot(consistent)

    assert verify_snapshot(dest)["manifest"]["snapshot_id"].startswith("study-")


def test_verify_rejects_invented_snapshot_id(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    _rewrite_manifest(dest, lambda m: m.update(snapshot_id="invented-snapshot"))
    with pytest.raises(StudySnapshotError, match="snapshot ID|snapshot_id|invented"):
        verify_snapshot(dest)


def test_verify_rejects_symlinked_vault_ancestor(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    moved = tmp_path / "loose-a"
    (dest / "vault" / "project-a").rename(moved)
    (dest / "vault" / "project-a").symlink_to(moved, target_is_directory=True)
    with pytest.raises(StudySnapshotError, match="symlink"):
        verify_snapshot(dest)


def test_verify_rejects_symlinked_snapshot_root(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    link = tmp_path / "link"
    link.symlink_to(dest, target_is_directory=True)
    with pytest.raises(StudySnapshotError, match="symlink"):
        verify_snapshot(link)


def test_check_materialized_rejects_altered_sqlite(tmp_path):
    import sqlite3

    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    dest_info = materialize_snapshot_db(dest)

    edited = tmp_path / "edited"
    prepare_snapshot(_two_record_packet(), edited)
    materialize_snapshot_db(edited)
    connection = sqlite3.connect(edited / "study.db")
    connection.execute("UPDATE documents SET agent='mallory' WHERE doc_id=1")
    connection.execute("UPDATE vault_fts SET content='evil content' WHERE doc_id=1")
    connection.commit()
    connection.close()
    with pytest.raises(StudySnapshotError, match="tamper"):
        check_materialized(edited)

    swapped = tmp_path / "swapped"
    prepare_snapshot(_two_record_packet(), swapped)
    info = materialize_snapshot_db(swapped)
    materialized = json.loads((swapped / "materialized.json").read_text())
    ids = sorted(info["document_ids"].values())
    materialized["document_ids"] = {
        study_id: ids[(position + 1) % len(ids)]
        for position, study_id in enumerate(sorted(info["document_ids"]))
    }
    (swapped / "materialized.json").write_text(json.dumps(materialized))
    with pytest.raises(StudySnapshotError, match="tamper|match|cover"):
        check_materialized(swapped)

    assert check_materialized(dest)["manifest_digest"] == dest_info["manifest_digest"]
    assert sorted(check_materialized(dest)["document_ids"]) == ["study-0001", "study-0002"]


def test_searcher_consumes_bound_identity_not_mirror(tmp_path):
    from minni.eval.retrievers import SnapshotSearcher

    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    materialize_snapshot_db(dest)
    _rewrite_manifest(dest, lambda m: m["principal"].update(agent_id="changed-principal"))
    with pytest.raises(ValueError, match="frozen validation|principal mirror"):
        SnapshotSearcher(dest)


@pytest.mark.parametrize("bad_path", ["a/./n.md", "a//n.md", "a/n.md/", "./n.md"])
def test_rejects_canonical_path_aliases(bad_path):
    packet = _packet([_record("doc-a1", "store-a", bad_path, "some text here")])
    with pytest.raises(StudySnapshotError, match="canonical|artifact_path"):
        validate_export_packet(packet)


def test_rejects_nonfinite_unbounded_and_oversized_metadata():
    packet = _two_record_packet()
    packet["records"][0]["source_detail"] = {"score": float("nan")}
    with pytest.raises(StudySnapshotError, match="finite"):
        validate_export_packet(packet)

    packet = _two_record_packet()
    packet["records"][0]["source_detail"] = {"blob": "x" * 5_000}
    with pytest.raises(StudySnapshotError, match="at most"):
        validate_export_packet(packet)

    packet = _two_record_packet()
    packet["principal"]["capabilities"] = ["search"] * 40
    with pytest.raises(StudySnapshotError, match="at most"):
        validate_export_packet(packet)

    packet = _two_record_packet()
    packet["principal"]["agent_id"] = "h" * 200
    with pytest.raises(StudySnapshotError, match="exceeds"):
        validate_export_packet(packet)


def test_verify_rejects_non_strict_json_constants(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    text = (dest / "mapping.json").read_text()
    (dest / "mapping.json").write_text(text.replace('"expected_eligible": true', '"expected_eligible": NaN', 1))
    with pytest.raises(StudySnapshotError, match="strict|tamper|valid JSON"):
        verify_snapshot(dest)


def test_check_materialized_rejects_orphan_fts_and_leaves_no_wal(tmp_path):
    import sqlite3

    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    materialize_snapshot_db(dest)

    orphan = tmp_path / "orphan"
    prepare_snapshot(_two_record_packet(), orphan)
    materialize_snapshot_db(orphan)
    connection = sqlite3.connect(orphan / "study.db")
    connection.execute(
        "INSERT INTO vault_fts(doc_id,path,content,agent,sigil) VALUES(?,?,?,?,?)",
        (9999, "nowhere", "orphan retrieval content", "mallory", "T"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(StudySnapshotError, match="orphan FTS"):
        check_materialized(orphan)

    # The verification handle is actually read-only: the database bytes are
    # identical afterwards (SQLite may still touch WAL sidecars on read; the
    # logical content never changes), and a write through the same URI fails.
    import hashlib
    import sqlite3
    from urllib.parse import quote as _quote

    digest_before = hashlib.sha256((dest / "study.db").read_bytes()).hexdigest()
    assert check_materialized(dest)["snapshot_id"].startswith("study-")
    assert hashlib.sha256((dest / "study.db").read_bytes()).hexdigest() == digest_before
    uri = "file:" + _quote(str(dest / "study.db"), safe="/:") + "?mode=ro"
    handle = sqlite3.connect(uri, uri=True)
    try:
        handle.execute("PRAGMA query_only = ON")
        with pytest.raises(sqlite3.OperationalError):
            handle.execute("INSERT INTO documents(path) VALUES('x')")
    finally:
        handle.close()
    assert hashlib.sha256((dest / "study.db").read_bytes()).hexdigest() == digest_before


def test_verify_rejects_duplicate_json_keys(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    text = (dest / "snapshot.json").read_text()
    forged = text.replace('"snapshot_version": "minni-study-snapshot-v1",',
                          '"snapshot_version": "minni-study-snapshot-v1", '
                          '"snapshot_version": "forged",', 1)
    (dest / "snapshot.json").write_text(forged)
    with pytest.raises(StudySnapshotError, match="duplicate key"):
        verify_snapshot(dest)


def test_check_materialized_rejects_bool_and_duplicate_ids(tmp_path):
    dest = tmp_path / "snapshot"
    prepare_snapshot(_two_record_packet(), dest)
    materialize_snapshot_db(dest)

    boolean = tmp_path / "boolean"
    prepare_snapshot(_two_record_packet(), boolean)
    materialize_snapshot_db(boolean)
    materialized = json.loads((boolean / "materialized.json").read_text())
    materialized["document_ids"]["study-0001"] = True
    (boolean / "materialized.json").write_text(json.dumps(materialized))
    with pytest.raises(StudySnapshotError, match="positive integer"):
        check_materialized(boolean)

    duplicate = tmp_path / "duplicate"
    prepare_snapshot(_two_record_packet(), duplicate)
    info = materialize_snapshot_db(duplicate)
    materialized = json.loads((duplicate / "materialized.json").read_text())
    first_id = info["document_ids"]["study-0001"]
    materialized["document_ids"] = {"study-0001": first_id, "study-0002": first_id}
    (duplicate / "materialized.json").write_text(json.dumps(materialized))
    with pytest.raises(StudySnapshotError, match="two study records"):
        check_materialized(duplicate)


def test_prepare_preflights_nonempty_destination(tmp_path):
    dest = tmp_path / "snapshot"
    first = _two_record_packet()
    prepare_snapshot(first, dest)
    before = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())

    second = _packet([
        _record("doc-9", "store-a", "project-a/other.md", "second packet text"),
    ])
    with pytest.raises(StudySnapshotError, match="not empty|fresh directory"):
        prepare_snapshot(second, dest)
    # The later rejection left no mixed second-packet bytes behind.
    after = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert after == before
    assert verify_snapshot(dest)["manifest"]["manifest_digest"] == first["manifest"]["manifest_digest"]


def test_page_status_uses_genuine_engine_vocabulary():
    from minni.eval.study_snapshot import DEFAULT_PAGE_STATUS, PAGE_STATUSES

    assert DEFAULT_PAGE_STATUS == "candidate"
    assert "draft" in PAGE_STATUSES and "active" not in PAGE_STATUSES
    packet = _two_record_packet()
    packet["records"][0]["page_status"] = "active"
    with pytest.raises(StudySnapshotError, match="page_status"):
        validate_export_packet(packet)
    allowed = _packet([
        _record("doc-a1", "store-a", "project-a/a.md", "text one here", page_status="draft"),
    ])
    assert validate_export_packet(allowed)[0]["page_status"] == "draft"


def test_page_status_vocabulary_matches_engine():
    from minni.eval.study_snapshot import PAGE_STATUSES
    from minni.wiki_indexer import WikiFrontmatter

    assert PAGE_STATUSES == set(WikiFrontmatter.VALID_STATUSES)


def test_snapshot_sanitize_matches_engine_fts():
    from minni.eval.retrievers import _sanitize_snapshot_query
    from minni.retrieval import RetrievalEngine

    for query in ["alpha launch", "cross-project recall!", "  spaced   out  ",
                  "c++ and c#", "well-known fact"]:
        assert _sanitize_snapshot_query(query) == RetrievalEngine._sanitize_fts_query(query)


def test_draft_records_excluded_from_default_snapshot_search(tmp_path):
    from minni.eval.retrievers import SnapshotSearcher

    dest = tmp_path / "snapshot"
    prepare_snapshot(_packet([
        _record("doc-a1", "store-a", "project-a/launch.md", "alpha launch notes",
                page_status="accepted"),
        _record("doc-b1", "store-b", "project-b/draft.md", "alpha draft notes",
                page_status="draft"),
    ]), dest)
    materialize_snapshot_db(dest)
    results = SnapshotSearcher(dest).search("alpha", limit=5)
    assert [r["source"].split("vault/")[-1] for r in results] == ["project-a/launch.md"]

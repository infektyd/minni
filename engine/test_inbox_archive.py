"""Tests for afm_passes.inbox_archive — B1 drain-on-resolution (audit C2):
once every candidate derived from an inbox file is terminal, the source file
moves to <inbox>/.archive/ (rename only, never unlink).

Follows the test_inbox_ingest.py harness (isolated tmp DB + config).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from test_inbox_ingest import _make_db, _stop_doc, _write_inbox_file


def _set_status(db_obj, status, principal="codex"):
    with db_obj.cursor() as c:
        c.execute(
            "UPDATE candidate_packets SET status=? WHERE principal=?",
            (status, principal),
        )


def _candidate_ids(db_obj, principal="codex"):
    with db_obj.cursor() as c:
        c.execute(
            "SELECT candidate_id FROM candidate_packets WHERE principal=?",
            (principal,),
        )
        return [dict(r)["candidate_id"] for r in c.fetchall()]


def test_resolved_candidate_archives_source_file(tmp_path, monkeypatch):
    """B1 gate: a resolved candidate's inbox file leaves the live inbox and
    lands in .archive (filename preserved, content intact)."""
    from afm_passes.inbox_archive import maybe_archive_for_candidate
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    monkeypatch.setattr(cfg, "CANONICAL_SOVEREIGN_HOME", str(tmp_path), raising=False)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _stop_doc(["a durable lesson worth keeping"]))

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1
    (cid,) = _candidate_ids(db_obj)

    # Still proposed -> must NOT archive.
    assert maybe_archive_for_candidate(db_obj, cfg, cid) is None
    assert (inbox / "a.json").exists()

    # Terminal -> archives.
    _set_status(db_obj, "accepted")
    archived = maybe_archive_for_candidate(db_obj, cfg, cid)
    assert archived is not None
    assert not (inbox / "a.json").exists(), "file must leave the live inbox"
    archived_path = inbox / ".archive" / "a.json"
    assert str(archived_path) == archived
    assert archived_path.is_file(), "file must land in .archive, never deleted"
    doc = json.loads(archived_path.read_text(encoding="utf-8"))
    assert doc["candidates"] == ["a durable lesson worth keeping"]

    # Idempotent: second call is a quiet no-op.
    assert maybe_archive_for_candidate(db_obj, cfg, cid) is None


def test_partial_resolution_keeps_file(tmp_path, monkeypatch):
    """A file spawning multiple candidates stays live until ALL are terminal."""
    from afm_passes.inbox_archive import maybe_archive_for_candidate
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    monkeypatch.setattr(cfg, "CANONICAL_SOVEREIGN_HOME", str(tmp_path), raising=False)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(
        inbox, "multi.json", _stop_doc(["first lesson here", "second lesson here"])
    )
    ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    cid_a, cid_b = sorted(_candidate_ids(db_obj))

    with db_obj.cursor() as c:
        c.execute(
            "UPDATE candidate_packets SET status='accepted' WHERE candidate_id=?",
            (cid_a,),
        )
    assert maybe_archive_for_candidate(db_obj, cfg, cid_a) is None
    assert (inbox / "multi.json").exists(), "sibling still proposed -> keep file"

    with db_obj.cursor() as c:
        c.execute(
            "UPDATE candidate_packets SET status='rejected' WHERE candidate_id=?",
            (cid_b,),
        )
    assert maybe_archive_for_candidate(db_obj, cfg, cid_b) is not None
    assert not (inbox / "multi.json").exists()
    assert (inbox / ".archive" / "multi.json").is_file()


def test_non_inbox_candidate_is_noop(tmp_path, monkeypatch):
    """Candidates not sourced from an inbox file never trigger file moves."""
    from afm_passes.inbox_archive import maybe_archive_for_candidate

    db_obj, cfg = _make_db(tmp_path)
    monkeypatch.setattr(cfg, "CANONICAL_SOVEREIGN_HOME", str(tmp_path), raising=False)
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, content, evidence_refs, derived_from,
             instruction_like, status, proposed_at)
            VALUES ('codex', 'default', 'a learn-tool candidate', '[]', '{}',
                    0, 'accepted', 1.0)
            """,
        )
        cid = c.lastrowid
    assert maybe_archive_for_candidate(db_obj, cfg, cid) is None
    # Unknown candidate id is also a quiet no-op.
    assert maybe_archive_for_candidate(db_obj, cfg, 999999) is None


def test_archive_collision_gets_suffix(tmp_path):
    """archive_inbox_file never overwrites an existing archived file."""
    from afm_passes.inbox_archive import archive_inbox_file

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".archive").mkdir()
    (inbox / ".archive" / "a.json").write_text("{\"old\": true}", encoding="utf-8")
    (inbox / "a.json").write_text("{\"new\": true}", encoding="utf-8")

    archived = archive_inbox_file(inbox / "a.json")
    assert archived == str(inbox / ".archive" / "a.1.json")
    assert (inbox / ".archive" / "a.json").read_text(encoding="utf-8") == "{\"old\": true}"
    assert json.loads((inbox / ".archive" / "a.1.json").read_text(encoding="utf-8")) == {"new": True}


def test_resolve_candidate_rpc_archives_inbox_source(tmp_path, monkeypatch):
    """End-to-end B1: operator resolve via the minnid handler moves the
    candidate's source inbox file into .archive (terminal state reached
    through the governance path, not just the module helper)."""
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "via-rpc.json", _stop_doc(["resolved through rpc"]))
    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1
    (cid,) = _candidate_ids(db_obj)

    import config as cfg_mod
    from minnid import _resolve_candidate

    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"))

    resp = _resolve_candidate(
        {"candidate_id": cid, "decision": "accept", "reason": "B1 archive test"}, 1
    )
    assert resp.get("result", {}).get("new_status") == "accepted", resp
    assert not (inbox / "via-rpc.json").exists(), "file must leave the live inbox"
    assert (inbox / ".archive" / "via-rpc.json").is_file()

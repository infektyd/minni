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


def test_traversal_inbox_file_is_rejected(tmp_path, monkeypatch):
    """Security: derived_from.inbox_file is client-controllable (staged via
    UDS, resolve is permissive). A traversal value must never move a file
    outside the inbox — only pure basenames are honored."""
    from afm_passes.inbox_archive import (
        _derived_inbox_file,
        archive_inbox_file,
        maybe_archive_for_candidate,
    )

    # Unit guard: anything that is not a pure basename is rejected.
    def df(name):
        return _derived_inbox_file(json.dumps({"source": "inbox", "inbox_file": name}))

    assert df("a.json") == "a.json"
    for evil in ("../../../evil.json", "..", ".", "sub/evil.json", "/etc/passwd"):
        assert df(evil) is None, evil

    # End-to-end: a staged candidate carrying a traversal blob resolves to a
    # no-op; the target file outside the inbox stays put.
    db_obj, cfg = _make_db(tmp_path)
    monkeypatch.setattr(cfg, "CANONICAL_SOVEREIGN_HOME", str(tmp_path), raising=False)
    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("{\"keep\": true}", encoding="utf-8")
    rel = os.path.relpath(victim, inbox)  # ../../victim.json
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, content, evidence_refs, derived_from,
             instruction_like, status, proposed_at)
            VALUES ('codex', 'default', 'evil', '[]', ?, 0, 'accepted', 1.0)
            """,
            (json.dumps({"source": "inbox", "inbox_file": rel, "candidate_index": 0}),),
        )
        cid = c.lastrowid
    assert maybe_archive_for_candidate(db_obj, cfg, cid) is None
    assert victim.is_file(), "traversal target must never move"
    assert not (tmp_path / ".archive").exists()

    # Defense-in-depth: archive_inbox_file refuses a non-contained target.
    weird = inbox / ".."
    assert archive_inbox_file(weird) is None


def _patched_writeback(monkeypatch, db_obj, cfg):
    import types

    import config as cfg_mod
    import minnid

    # The consolidation paths stamp consolidation_actions (a migrations table
    # not in the base test schema); create it so they can run in isolation.
    with db_obj.cursor() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS consolidation_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                source_event_id INTEGER,
                target_learning_id INTEGER,
                superseded_learning_id INTEGER,
                claim TEXT,
                category TEXT,
                confidence REAL,
                status TEXT DEFAULT 'pending',
                detail TEXT,
                created_at REAL NOT NULL
            )
            """
        )
    # content_hash arrives via a later migration; backfill for the base schema.
    try:
        with db_obj.cursor() as c:
            c.execute("ALTER TABLE learnings ADD COLUMN content_hash TEXT")
    except Exception:
        pass
    wb = types.SimpleNamespace(db=db_obj, config=cfg, model=None)
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: wb)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", cfg.vault_path)
    return wb


def test_consolidation_promote_archives_inbox_source(tmp_path, monkeypatch):
    """B1 via the consolidation terminal path: _promote_candidate_durable on
    an inbox-sourced candidate moves the source file into .archive (wb.db
    handle, not the RPC path)."""
    from afm_passes.inbox_ingest import ingest

    import minnid

    db_obj, cfg = _make_db(tmp_path)
    _patched_writeback(monkeypatch, db_obj, cfg)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "promote.json", _stop_doc(["promoted via consolidation"]))
    assert ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)["inserted"] == 1
    (cid,) = _candidate_ids(db_obj)

    lid = minnid._promote_candidate_durable(cid, reason="test promote")
    assert lid is not None
    assert not (inbox / "promote.json").exists(), "file must leave the live inbox"
    assert (inbox / ".archive" / "promote.json").is_file()


def test_consolidation_dedup_reject_archives_inbox_source(tmp_path, monkeypatch):
    """B1 via the other consolidation terminal path: _reject_candidate_dedup."""
    from afm_passes.inbox_ingest import ingest

    import minnid

    db_obj, cfg = _make_db(tmp_path)
    _patched_writeback(monkeypatch, db_obj, cfg)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "dedup.json", _stop_doc(["rejected as duplicate"]))
    assert ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)["inserted"] == 1
    (cid,) = _candidate_ids(db_obj)

    assert minnid._reject_candidate_dedup(cid) is True
    assert not (inbox / "dedup.json").exists(), "file must leave the live inbox"
    assert (inbox / ".archive" / "dedup.json").is_file()

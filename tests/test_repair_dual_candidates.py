"""Issue #239: dual-resolution candidate repair + virtual _durable hygiene."""

from __future__ import annotations

import json
import os
import time

import pytest


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _insert_candidate(
    db,
    *,
    content: str,
    status: str,
    inbox_file: str = "a.json",
    candidate_index: int = 0,
    principal: str = "codex",
    content_sha1: str | None = None,
    resolved_by: str = "afm-consolidation",
):
    import hashlib

    sha = content_sha1 or hashlib.sha1(content.encode("utf-8")).hexdigest()
    derived = json.dumps(
        {
            "source": "inbox",
            "inbox_file": inbox_file,
            "candidate_index": candidate_index,
            "kind": None,
            "content_sha1": sha,
        }
    )
    now = time.time()
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             evidence_refs, derived_from, instruction_like, status, proposed_at,
             resolved_at, resolved_by, resolution_reason)
            VALUES (?, 'default', NULL, 'safe', ?, '[]', ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                principal,
                content,
                derived,
                status,
                now,
                now if status != "proposed" else None,
                resolved_by if status != "proposed" else None,
                (
                    "duplicate of existing learning"
                    if status == "rejected"
                    else "afm-consolidation"
                ),
            ),
        )
        return c.lastrowid


def test_choose_winner_prefers_accepted_over_rejected():
    from minni.repair_dual_candidates import choose_winner

    winner = choose_winner(
        [
            {"candidate_id": 10, "status": "rejected"},
            {"candidate_id": 2, "status": "accepted"},
        ]
    )
    assert winner["candidate_id"] == 2


def test_choose_winner_tie_break_lowest_id():
    from minni.repair_dual_candidates import choose_winner

    winner = choose_winner(
        [
            {"candidate_id": 9, "status": "rejected"},
            {"candidate_id": 3, "status": "rejected"},
        ]
    )
    assert winner["candidate_id"] == 3


def test_repair_keeps_accepted_deletes_rejected_twin_leaves_learnings(tmp_path):
    from minni.repair_dual_candidates import (
        find_duplicate_candidate_groups,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    content = "a durable fact about dual-resolution repair"
    accepted_id = _insert_candidate(db, content=content, status="accepted")
    rejected_id = _insert_candidate(db, content=content, status="rejected")

    # Learning that the accepted twin produced — must survive repair.
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO learnings (agent_id, category, content, created_at)
            VALUES ('codex', 'general', ?, ?)
            """,
            (content, time.time()),
        )
        learning_id = c.lastrowid

    groups = find_duplicate_candidate_groups(db)
    assert len(groups) == 1
    assert groups[0]["winner_id"] == accepted_id
    assert rejected_id in groups[0]["loser_ids"]

    dry = repair_duplicate_candidate_pairs(db, dry_run=True)
    assert dry["groups_found"] == 1
    assert dry["would_delete"] == 1
    assert dry["deleted"] == 0
    assert dry["learnings_touched"] is False

    applied = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert applied["deleted"] == 1

    with db.cursor() as c:
        c.execute("SELECT candidate_id, status FROM candidate_packets")
        rows = [dict(r) for r in c.fetchall()]
        c.execute("SELECT learning_id, content FROM learnings")
        learnings = [dict(r) for r in c.fetchall()]

    assert len(rows) == 1
    assert rows[0]["candidate_id"] == accepted_id
    assert rows[0]["status"] == "accepted"
    assert len(learnings) == 1
    assert learnings[0]["learning_id"] == learning_id
    assert learnings[0]["content"] == content

    # Idempotent
    again = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert again["groups_found"] == 0
    assert again["deleted"] == 0


def test_ensure_inbox_dedup_index_blocks_reinsert(tmp_path):
    from minni.repair_dual_candidates import (
        ensure_inbox_dedup_index,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    content = "unique key after repair"
    _insert_candidate(db, content=content, status="accepted")
    _insert_candidate(db, content=content, status="rejected")

    blocked = ensure_inbox_dedup_index(db)
    assert blocked["status"] == "blocked_by_duplicates"

    repair_duplicate_candidate_pairs(db, dry_run=False)
    created = ensure_inbox_dedup_index(db)
    assert created["status"] == "created"
    exists = ensure_inbox_dedup_index(db)
    assert exists["status"] == "exists"

    # Second insert with same inbox key must fail the unique index.
    with pytest.raises(Exception):
        _insert_candidate(db, content=content, status="proposed")


def test_virtual_durable_not_flagged_missing_when_fts_present(tmp_path):
    from minni.repair_dual_candidates import (
        find_missing_document_rows,
        find_orphan_virtual_durable,
        is_virtual_durable_path,
        repair_index_disk_divergence,
    )

    db, cfg = _make_db(tmp_path)
    virtual_path = os.path.join(cfg.vault_path, "_durable", "codex__abc123.md")
    assert is_virtual_durable_path(virtual_path)
    assert not os.path.isfile(virtual_path)

    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, sigil, last_modified, indexed_at, page_status,
             privacy_level, page_type, layer, whole_document)
            VALUES (?, 'codex', '❓', 0, 0, 'accepted', 'safe', 'learning',
                    'knowledge', 0)
            """,
            (virtual_path,),
        )
        doc_id = c.lastrowid
        c.execute(
            """
            INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
            VALUES (?, ?, 'indexed durable learning body', 'codex', '❓')
            """,
            (doc_id, virtual_path),
        )

    # Default finder excludes virtual durable (not a real file requirement).
    assert find_missing_document_rows(db) == []
    assert find_orphan_virtual_durable(db) == []

    result = repair_index_disk_divergence(db, dry_run=False)
    assert result["healthy_virtual_durable_kept"] == 1
    assert result["prune"]["deleted"] == 0

    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM documents")
        assert dict(c.fetchone())["n"] == 1


def test_prune_orphan_virtual_durable_without_fts(tmp_path):
    from minni.repair_dual_candidates import repair_index_disk_divergence

    db, cfg = _make_db(tmp_path)
    orphan_path = os.path.join(cfg.vault_path, "_durable", "main__dead.md")
    real_missing = str(tmp_path / "vault" / "wiki" / "gone.md")

    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'main', 0, 0, 'accepted', 'safe')
            """,
            (orphan_path,),
        )
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'main', 0, 0, 'accepted', 'safe')
            """,
            (real_missing,),
        )

    result = repair_index_disk_divergence(db, dry_run=False)
    assert result["orphan_virtual_durable"] == 1
    assert result["missing_on_disk_non_virtual"] == 1
    assert result["prune"]["deleted"] == 2

    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM documents")
        assert dict(c.fetchone())["n"] == 0


def test_ingest_skips_key_present_under_other_principal(tmp_path):
    """Prevention: global key check so dual-ingest cannot recur across principals."""
    from minni.afm_passes.inbox_ingest import ingest

    db, cfg = _make_db(tmp_path)
    content = "shared lesson should insert once only"
    _insert_candidate(
        db,
        content=content,
        status="accepted",
        inbox_file="shared.json",
        principal="claude-code",
    )

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "shared.json").write_text(
        json.dumps(
            {
                "slug": "s",
                "createdAt": "2026-06-06T12:00:00.000Z",
                "kind": "codex_stop_candidates",
                "agent_id": "codex",
                "workspace_id": "default",
                "candidates": [content],
                "log_only": [],
                "expires": [],
                "do_not_store": [],
                "last_task": "t",
            }
        ),
        encoding="utf-8",
    )

    res = ingest(db, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0
    assert res["already_present"] >= 1
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM candidate_packets")
        assert dict(c.fetchone())["n"] == 1

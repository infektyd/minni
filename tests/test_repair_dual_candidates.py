"""Issue #239: dual-resolution candidate repair + virtual _durable hygiene."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

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

    result = repair_index_disk_divergence(
        db, dry_run=False, vault_roots=[cfg.vault_path]
    )
    assert result["orphan_virtual_durable"] == 1
    assert result["missing_on_disk_non_virtual"] == 1
    assert result["prune"]["deleted"] == 2

    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM documents")
        assert dict(c.fetchone())["n"] == 0


def test_relative_path_with_fts_not_pruned_wrong_cwd(tmp_path, monkeypatch):
    """High #1: relative documents.path + FTS must survive wrong-CWD prune."""
    from minni.repair_dual_candidates import (
        find_missing_document_rows,
        repair_index_disk_divergence,
        resolve_document_path,
    )

    db, cfg = _make_db(tmp_path)
    rel_path = "wiki/decisions/dual-write-mode.md"
    vault = Path(cfg.vault_path)
    real_file = vault / "wiki" / "decisions" / "dual-write-mode.md"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("# dual write\n", encoding="utf-8")

    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'codex', 0, 0, 'accepted', 'safe')
            """,
            (rel_path,),
        )
        doc_id = c.lastrowid
        c.execute(
            """
            INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
            VALUES (?, ?, 'dual write mode body', 'codex', '❓')
            """,
            (doc_id, rel_path),
        )

    # Resolve works when vault roots are provided.
    assert resolve_document_path(rel_path, [cfg.vault_path]) == str(real_file)

    # Simulate operator running from an unrelated CWD with no vault roots:
    # bare isfile(rel) is false, but FTS-backed relative must not be missing.
    other = tmp_path / "other-cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    assert find_missing_document_rows(db, vault_roots=None) == []
    assert find_missing_document_rows(db, vault_roots=[cfg.vault_path]) == []

    result = repair_index_disk_divergence(
        db, dry_run=False, vault_roots=[cfg.vault_path]
    )
    assert result["missing_on_disk_non_virtual"] == 0
    assert result["prune"]["deleted"] == 0
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM documents")
        assert dict(c.fetchone())["n"] == 1
        c.execute("SELECT COUNT(*) AS n FROM vault_fts")
        assert dict(c.fetchone())["n"] == 1


def test_relative_path_without_fts_prunable_when_vault_checked(tmp_path):
    """Relative path with no FTS and no file under vault roots → missing."""
    from minni.repair_dual_candidates import find_missing_document_rows

    db, cfg = _make_db(tmp_path)
    Path(cfg.vault_path).mkdir(parents=True, exist_ok=True)
    rel_path = "wiki/ghost.md"
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'codex', 0, 0, 'accepted', 'safe')
            """,
            (rel_path,),
        )
    # No vault roots → refuse to classify as missing (cannot prove absence).
    assert find_missing_document_rows(db, vault_roots=None) == []
    # With vault roots, resolved absolute is missing and no FTS → missing.
    missing = find_missing_document_rows(db, vault_roots=[cfg.vault_path])
    assert len(missing) == 1
    assert missing[0]["path"] == rel_path


def test_run_full_repair_default_skips_index_prune(tmp_path):
    """High #2: --apply dual-only; index prune requires prune_index=True."""
    from minni.repair_dual_candidates import run_full_repair

    db, cfg = _make_db(tmp_path)
    content = "twin content"
    _insert_candidate(db, content=content, status="accepted")
    _insert_candidate(db, content=content, status="rejected")
    gone = str(tmp_path / "vault" / "wiki" / "really-gone.md")
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'main', 0, 0, 'accepted', 'safe')
            """,
            (gone,),
        )

    default = run_full_repair(db, dry_run=False, create_index=False)
    assert default["prune_index"] is False
    assert default["index_disk"].get("skipped") is True
    assert default["dual_candidates"]["deleted"] == 1
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM documents")
        assert dict(c.fetchone())["n"] == 1  # not pruned

    pruned = run_full_repair(
        db,
        dry_run=False,
        create_index=False,
        prune_index=True,
        vault_roots=[cfg.vault_path],
    )
    assert pruned["prune_index"] is True
    assert pruned["index_disk"].get("skipped") is not True
    assert pruned["index_disk"]["missing_on_disk_non_virtual"] == 1
    assert pruned["index_disk"]["prune"]["deleted"] == 1


def test_repair_dual_does_not_commit_via_cursor_mid_txn(tmp_path, monkeypatch):
    """Medium #3: never call db.cursor() (auto-commit) inside the write txn."""
    from minni import repair_dual_candidates as mod

    db, _cfg = _make_db(tmp_path)
    content = "audit twin"
    _insert_candidate(db, content=content, status="accepted")
    _insert_candidate(db, content=content, status="rejected")

    state = {"in_txn": False, "cursor_in_txn": 0}
    real_cursor = db.cursor
    real_transaction = db.transaction

    @contextlib.contextmanager
    def tracked_transaction():
        state["in_txn"] = True
        try:
            with real_transaction() as c:
                yield c
        finally:
            state["in_txn"] = False

    def guarded_cursor():
        if state["in_txn"]:
            state["cursor_in_txn"] += 1
            raise AssertionError("db.cursor() called inside write transaction")
        return real_cursor()

    monkeypatch.setattr(db, "transaction", tracked_transaction)
    monkeypatch.setattr(db, "cursor", guarded_cursor)

    result = mod.repair_duplicate_candidate_pairs(db, dry_run=False)
    assert result["deleted"] == 1
    assert state["cursor_in_txn"] == 0


def test_distill_in_txn_recheck_skips_existing_key(tmp_path, monkeypatch):
    """Medium #4: compact_distillation mirrors ingest UNIQUE / in-txn recheck."""
    from minni.afm_passes import compact_distillation as cd

    db, cfg = _make_db(tmp_path)
    content = "Compaction summary — Key technical concepts: race on bootout"

    # Pre-seed a twin key as if another process already inserted.
    _insert_candidate(
        db,
        content=content,
        status="proposed",
        inbox_file="compact-1.json",
        candidate_index=0,
        principal="codex",
    )

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "compact-1.json").write_text(
        json.dumps(
            {
                "kind": "compact_summary",
                "agent_id": "codex",
                "workspace_id": "default",
                "summary_id": "s1",
                "platform": "codex",
                "summary_text": (
                    "1. Key technical concepts:\n"
                    "race on bootout after launchctl error 5\n\n"
                    "2. All user messages:\n"
                    "please fix it\n"
                ),
            }
        ),
        encoding="utf-8",
    )

    # Force no AFM so deterministic fallback is used.
    monkeypatch.setattr(cd, "resolve_afm_mode", lambda: "off")
    monkeypatch.setattr(cd, "default_provider_chain", lambda: None)

    # Bypass file-level short-circuit by clearing pre-scan existing keys for
    # this file? The pre-scan uses _existing_keys which will see our seed and
    # skip the whole file. That is correct prevention for the common path.
    # Exercise the in-txn UNIQUE path by monkeypatching _existing_keys to
    # return empty so the insert path runs, then UNIQUE/recheck must hold.
    monkeypatch.setattr(cd, "_existing_keys", lambda db, principals=None: set())

    res = cd.distill(db, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM candidate_packets")
        assert dict(c.fetchone())["n"] == 1


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

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
    omit_content_sha1: bool = False,
):
    import hashlib

    derived_obj: dict = {
        "source": "inbox",
        "inbox_file": inbox_file,
        "candidate_index": candidate_index,
        "kind": None,
    }
    if not omit_content_sha1:
        sha = content_sha1 or hashlib.sha1(content.encode("utf-8")).hexdigest()
        derived_obj["content_sha1"] = sha
    derived = json.dumps(derived_obj)
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


def test_collapse_decision_rejected_does_not_beat_proposed():
    """High: rejected rank must not hard-delete an open proposed twin."""
    from minni.repair_dual_candidates import collapse_decision

    decision = collapse_decision(
        [
            {"candidate_id": 100, "status": "rejected"},
            {"candidate_id": 101, "status": "proposed"},
        ]
    )
    assert decision["action"] == "needs_operator"
    assert decision["reason"] == "proposed_with_terminal"
    assert decision["losers"] == []
    assert decision["winner"] is None


def test_collapse_decision_accepted_deletes_non_accepted():
    from minni.repair_dual_candidates import collapse_decision

    decision = collapse_decision(
        [
            {"candidate_id": 10, "status": "rejected"},
            {"candidate_id": 2, "status": "accepted"},
            {"candidate_id": 11, "status": "proposed"},
        ]
    )
    assert decision["action"] == "collapse"
    assert decision["winner"]["candidate_id"] == 2
    loser_ids = {r["candidate_id"] for r in decision["losers"]}
    assert loser_ids == {10, 11}


def test_collapse_decision_all_proposed_lowest_id():
    from minni.repair_dual_candidates import collapse_decision

    decision = collapse_decision(
        [
            {"candidate_id": 9, "status": "proposed"},
            {"candidate_id": 3, "status": "proposed"},
        ]
    )
    assert decision["action"] == "collapse"
    assert decision["reason"] == "all_proposed"
    assert decision["winner"]["candidate_id"] == 3


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
    import sqlite3

    from minni.repair_dual_candidates import (
        INBOX_DEDUP_INDEX,
        ensure_inbox_dedup_index,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    content = "unique key after repair"
    _insert_candidate(db, content=content, status="accepted")
    _insert_candidate(db, content=content, status="rejected")

    blocked = ensure_inbox_dedup_index(db)
    assert blocked["status"] == "blocked_by_duplicates"
    assert blocked["duplicate_groups"] >= 1
    assert blocked.get("sample")

    repair_duplicate_candidate_pairs(db, dry_run=False)
    created = ensure_inbox_dedup_index(db)
    assert created["status"] == "created"
    assert created["index"] == INBOX_DEDUP_INDEX
    exists = ensure_inbox_dedup_index(db)
    assert exists["status"] == "exists"

    # Second insert with same inbox key must fail the unique index.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_candidate(db, content=content, status="proposed")

    # Same (file, index) with a *different* content_sha1 is also blocked.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_candidate(
            db,
            content="different body same key",
            status="proposed",
            content_sha1="deadbeef" * 5,
        )


def test_ensure_inbox_dedup_index_blocks_divergent_app_key(tmp_path):
    """Medium #1: app-key peers with different content_sha1 block unique index.

    Dual repair leaves divergent peers alone; ensure must still return
    blocked_by_duplicates (not fall through to CREATE UNIQUE IntegrityError).
    """
    from minni.repair_dual_candidates import (
        ensure_inbox_dedup_index,
        find_app_key_collisions,
        find_duplicate_candidate_groups,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    _insert_candidate(
        db,
        content="body alpha",
        status="accepted",
        inbox_file="div.json",
        candidate_index=0,
    )
    _insert_candidate(
        db,
        content="body beta different",
        status="rejected",
        inbox_file="div.json",
        candidate_index=0,
    )

    # No byte-identical duals — repair is a no-op.
    assert find_duplicate_candidate_groups(db) == []
    repair_duplicate_candidate_pairs(db, dry_run=False)
    collisions = find_app_key_collisions(db)
    assert len(collisions) == 1
    assert collisions[0]["byte_identical"] is False

    blocked = ensure_inbox_dedup_index(db)
    assert blocked["status"] == "blocked_by_duplicates"
    assert blocked["duplicate_groups"] == 1
    assert blocked.get("divergent_groups", 0) >= 1
    assert blocked.get("sample")
    assert blocked["sample"][0]["key"]["inbox_file"] == "div.json"
    # Must not have created the index.
    assert blocked["status"] != "created"
    with db.cursor() as c:
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_candidate_packets_inbox_key_unique'"
        )
        assert c.fetchone() is None


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


def test_learning_uri_survives_prune_index(tmp_path):
    """High #1: learning:// synthetic docs must not be pruned as missing-on-disk."""
    from minni.repair_dual_candidates import (
        find_missing_document_rows,
        is_virtual_identity_path,
        repair_index_disk_divergence,
        run_full_repair,
    )

    db, cfg = _make_db(tmp_path)
    vault = Path(cfg.vault_path)
    vault.mkdir(parents=True, exist_ok=True)
    evidence_file = vault / "wiki" / "evidence.md"
    evidence_file.parent.mkdir(parents=True)
    evidence_file.write_text("evidence body\n", encoding="utf-8")
    learning_path = "learning://42"
    assert is_virtual_identity_path(learning_path)
    assert is_virtual_identity_path(learning_path, page_type="learning")

    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, sigil, last_modified, indexed_at, page_status,
             privacy_level, page_type, layer, whole_document)
            VALUES (?, 'learning:codex', 'L', 0, 0, 'accepted', 'safe',
                    'learning', 'knowledge', 0)
            """,
            (learning_path,),
        )
        learning_doc_id = c.lastrowid
        # memory_links off the learning identity (writeback graph edges).
        # Evidence path is on-disk so only learning:// is the prune subject.
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'codex', 0, 0, 'accepted', 'safe')
            """,
            (str(evidence_file),),
        )
        evidence_id = c.lastrowid
        c.execute(
            """
            INSERT INTO memory_links
            (source_doc_id, target_doc_id, link_type, weight, created_at)
            VALUES (?, ?, 'derived_from', 1.0, 0)
            """,
            (learning_doc_id, evidence_id),
        )

    # No FTS by design for learning:// graph nodes — still must not be missing.
    assert find_missing_document_rows(db, vault_roots=[cfg.vault_path]) == []

    result = repair_index_disk_divergence(
        db, dry_run=False, vault_roots=[cfg.vault_path]
    )
    assert result["missing_on_disk_non_virtual"] == 0
    assert result["prune"]["deleted"] == 0

    full = run_full_repair(
        db,
        dry_run=False,
        create_index=False,
        prune_index=True,
        vault_roots=[cfg.vault_path],
    )
    assert full["index_disk"]["prune"]["deleted"] == 0

    with db.cursor() as c:
        c.execute("SELECT path FROM documents WHERE path=?", (learning_path,))
        assert c.fetchone() is not None
        c.execute(
            "SELECT COUNT(*) AS n FROM memory_links WHERE source_doc_id=?",
            (learning_doc_id,),
        )
        assert dict(c.fetchone())["n"] == 1


def test_absolute_fts_backed_path_survives_missing_file(tmp_path):
    """High #2: absolute FTS-backed vault paths survive home/NFS move."""
    from minni.repair_dual_candidates import (
        find_missing_document_rows,
        repair_index_disk_divergence,
    )

    db, cfg = _make_db(tmp_path)
    # Absolute path that never existed on disk (simulates moved home prefix).
    abs_path = str(tmp_path / "old-home" / ".minni" / "codex-vault" / "wiki" / "foo.md")
    assert not os.path.isfile(abs_path)

    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'codex', 0, 0, 'accepted', 'safe')
            """,
            (abs_path,),
        )
        doc_id = c.lastrowid
        c.execute(
            """
            INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
            VALUES (?, ?, 'last remaining recall body', 'codex', '❓')
            """,
            (doc_id, abs_path),
        )

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

    # Explicit force may prune recallable rows when the operator insists.
    forced_missing = find_missing_document_rows(
        db, vault_roots=[cfg.vault_path], force_prune_indexed=True
    )
    assert len(forced_missing) == 1
    forced = repair_index_disk_divergence(
        db,
        dry_run=False,
        vault_roots=[cfg.vault_path],
        force_prune_indexed=True,
    )
    assert forced["missing_on_disk_non_virtual"] == 1
    assert forced["prune"]["deleted"] == 1


def test_absolute_chunk_backed_path_survives_missing_file(tmp_path):
    """High #2: chunk-backed absolute paths also protected without FTS."""
    from minni.repair_dual_candidates import find_missing_document_rows

    db, cfg = _make_db(tmp_path)
    abs_path = str(tmp_path / "moved" / "wiki" / "chunked.md")
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'codex', 0, 0, 'accepted', 'safe')
            """,
            (abs_path,),
        )
        doc_id = c.lastrowid
        c.execute(
            """
            INSERT INTO chunk_embeddings
            (doc_id, chunk_index, chunk_text, embedding, computed_at)
            VALUES (?, 0, 'chunk body', X'00', 0)
            """,
            (doc_id,),
        )

    assert find_missing_document_rows(db, vault_roots=[cfg.vault_path]) == []


def test_ingest_mid_batch_unique_still_commits_later(tmp_path, monkeypatch):
    """Medium #4: IntegrityError on mid-batch UNIQUE does not abort later inserts."""
    import sqlite3

    from minni.afm_passes import inbox_ingest as ii
    from minni.repair_dual_candidates import ensure_inbox_dedup_index

    db, cfg = _make_db(tmp_path)
    ensure_inbox_dedup_index(db)

    # Pre-seed key (file=a.json, index=0) so the first insert hits UNIQUE.
    _insert_candidate(
        db,
        content="pre-existing twin",
        status="accepted",
        inbox_file="a.json",
        candidate_index=0,
        principal="codex",
    )

    # Force the insert path to attempt both keys (bypass pre-txn existing set).
    monkeypatch.setattr(ii, "_existing_keys", lambda db, principals=None: set())

    # Craft to_insert-equivalent via a synthetic scan: two candidates in one file.
    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.json").write_text(
        json.dumps(
            {
                "slug": "s",
                "createdAt": "2026-06-06T12:00:00.000Z",
                "kind": "codex_stop_candidates",
                "agent_id": "codex",
                "workspace_id": "default",
                "candidates": [
                    "pre-existing twin",  # index 0 → UNIQUE / already present
                    "brand new second lesson",  # index 1 → must still insert
                ],
                "log_only": [],
                "expires": [],
                "do_not_store": [],
                "last_task": "t",
            }
        ),
        encoding="utf-8",
    )

    res = ii.ingest(db, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1
    assert res["already_present"] >= 1
    with db.cursor() as c:
        c.execute("SELECT content FROM candidate_packets ORDER BY candidate_id")
        contents = [dict(r)["content"] for r in c.fetchall()]
    assert "brand new second lesson" in contents
    assert len(contents) == 2

    # Sanity: IntegrityError is the type raised by the unique index itself.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_candidate(
            db,
            content="another clash",
            status="proposed",
            inbox_file="a.json",
            candidate_index=1,
        )


def test_in_txn_revalidate_keeps_accepted_promoted_under_stale_plan(
    tmp_path, monkeypatch
):
    """High #1: concurrent accept of planned 'loser' must not be deleted.

    Plan outside txn sees both proposed → keep lowest id. Between plan and
    apply, consolidation promotes the higher id to accepted. Apply must
    re-choose under BEGIN IMMEDIATE and keep the accepted twin.
    """
    from minni import repair_dual_candidates as mod

    db, _cfg = _make_db(tmp_path)
    content = "race content same body"
    low_id = _insert_candidate(db, content=content, status="proposed")
    high_id = _insert_candidate(db, content=content, status="proposed")
    assert low_id < high_id

    # Capture the pre-txn plan shape (lowest id wins while both proposed).
    groups = mod.find_duplicate_candidate_groups(db)
    assert len(groups) == 1
    assert groups[0]["winner_id"] == low_id
    assert high_id in groups[0]["loser_ids"]

    real_find = mod.find_duplicate_candidate_groups

    def find_then_promote(db_arg):
        groups_out = real_find(db_arg)
        # Simulate concurrent consolidation AFTER plan, BEFORE write txn:
        # promote the planned loser (high_id) and reject the planned winner.
        with db_arg.transaction() as c:
            c.execute(
                """
                UPDATE candidate_packets
                SET status='accepted', resolved_at=?, resolved_by='afm-consolidation',
                    resolution_reason='afm-consolidation'
                WHERE candidate_id=?
                """,
                (time.time(), high_id),
            )
            c.execute(
                """
                UPDATE candidate_packets
                SET status='rejected', resolved_at=?, resolved_by='afm-consolidation',
                    resolution_reason='duplicate of existing learning'
                WHERE candidate_id=?
                """,
                (time.time(), low_id),
            )
        return groups_out

    monkeypatch.setattr(mod, "find_duplicate_candidate_groups", find_then_promote)

    result = mod.repair_duplicate_candidate_pairs(db, dry_run=False)
    assert result["deleted"] == 1
    assert result["winner_replanned"] >= 1
    with db.cursor() as c:
        c.execute(
            "SELECT candidate_id, status FROM candidate_packets ORDER BY candidate_id"
        )
        rows = [dict(r) for r in c.fetchall()]
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == high_id
    assert rows[0]["status"] == "accepted"


def test_never_deletes_accepted_when_two_accepted(tmp_path):
    """Hard guard: status=accepted is never hard-deleted as a loser."""
    from minni.repair_dual_candidates import repair_duplicate_candidate_pairs

    db, _cfg = _make_db(tmp_path)
    content = "two accepted twins"
    a = _insert_candidate(db, content=content, status="accepted")
    b = _insert_candidate(db, content=content, status="accepted")
    assert a < b

    result = repair_duplicate_candidate_pairs(db, dry_run=False)
    # collapse keeps lowest accepted; extra accepted is never deleted.
    assert result["deleted"] == 0
    with db.cursor() as c:
        c.execute("SELECT candidate_id FROM candidate_packets ORDER BY candidate_id")
        ids = [dict(r)["candidate_id"] for r in c.fetchall()]
    assert ids == [a, b]


def test_rejected_plus_proposed_needs_operator_no_delete(tmp_path):
    """High RC: rejected must not hard-delete open proposed (blocks re-ingest)."""
    from minni.repair_dual_candidates import (
        find_duplicate_candidate_groups,
        find_needs_operator_groups,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    content = "operator rejected one twin; other still open"
    rej = _insert_candidate(db, content=content, status="rejected")
    prop = _insert_candidate(db, content=content, status="proposed")
    assert rej < prop

    # Not a collapsible dual — must surface as needs-operator.
    assert find_duplicate_candidate_groups(db) == []
    needs = find_needs_operator_groups(db)
    assert len(needs) == 1
    assert set(needs[0]["statuses"]) == {"proposed", "rejected"}
    assert set(needs[0]["candidate_ids"]) == {rej, prop}

    dry = repair_duplicate_candidate_pairs(db, dry_run=True)
    assert dry["groups_found"] == 0
    assert dry["would_delete"] == 0
    assert dry["needs_operator_groups"] == 1
    assert dry["needs_operator_sample"]

    applied = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert applied["deleted"] == 0
    assert applied["needs_operator_groups"] == 1
    with db.cursor() as c:
        c.execute(
            "SELECT candidate_id, status FROM candidate_packets ORDER BY candidate_id"
        )
        rows = [dict(r) for r in c.fetchall()]
    assert len(rows) == 2
    assert {rows[0]["candidate_id"], rows[1]["candidate_id"]} == {rej, prop}
    by_id = {r["candidate_id"]: r["status"] for r in rows}
    assert by_id[rej] == "rejected"
    assert by_id[prop] == "proposed"


def test_divergent_content_same_app_key_not_deleted(tmp_path):
    """Medium #2: different content_sha1 under same app key is not collapsed."""
    from minni.repair_dual_candidates import (
        find_divergent_content_groups,
        find_duplicate_candidate_groups,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    id_a = _insert_candidate(
        db,
        content="body alpha",
        status="accepted",
        inbox_file="same.json",
        candidate_index=0,
    )
    id_b = _insert_candidate(
        db,
        content="body beta completely different",
        status="rejected",
        inbox_file="same.json",
        candidate_index=0,
    )

    # Not a byte-identical dual group.
    assert find_duplicate_candidate_groups(db) == []
    divergent = find_divergent_content_groups(db)
    assert len(divergent) == 1
    assert set(divergent[0]["candidate_ids"]) == {id_a, id_b}

    result = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert result["groups_found"] == 0
    assert result["deleted"] == 0
    assert result["divergent_content_groups"] == 1
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM candidate_packets")
        assert dict(c.fetchone())["n"] == 2


def test_byte_identical_still_collapsed_when_app_peers_exist(tmp_path):
    """Same-sha twins collapse; a third divergent peer under the key is kept."""
    from minni.repair_dual_candidates import repair_duplicate_candidate_pairs

    db, _cfg = _make_db(tmp_path)
    body = "exact twin body"
    acc = _insert_candidate(
        db, content=body, status="accepted", inbox_file="mix.json", candidate_index=1
    )
    rej = _insert_candidate(
        db, content=body, status="rejected", inbox_file="mix.json", candidate_index=1
    )
    other = _insert_candidate(
        db,
        content="different body same key",
        status="proposed",
        inbox_file="mix.json",
        candidate_index=1,
    )

    result = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert result["deleted"] == 1
    assert result["divergent_content_groups"] >= 1
    with db.cursor() as c:
        c.execute(
            "SELECT candidate_id, status FROM candidate_packets ORDER BY candidate_id"
        )
        rows = {dict(r)["candidate_id"]: dict(r)["status"] for r in c.fetchall()}
    assert rej not in rows
    assert rows[acc] == "accepted"
    assert other in rows


def test_contradiction_log_fk_nulled_before_delete(tmp_path):
    """Medium #3: resolution_id FK must not blow the repair transaction."""
    from minni.repair_dual_candidates import repair_duplicate_candidate_pairs

    db, _cfg = _make_db(tmp_path)
    content = "fk twin body"
    keep_id = _insert_candidate(db, content=content, status="accepted")
    lose_id = _insert_candidate(db, content=content, status="rejected")

    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO contradiction_log
            (memory_a_id, memory_b_id, detected_at, detection_method, resolution_id)
            VALUES (?, ?, ?, 'test', ?)
            """,
            (1, 2, time.time(), lose_id),
        )

    result = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert result["deleted"] == 1
    assert result["fk_resolution_nulled"] >= 1

    with db.cursor() as c:
        c.execute("SELECT candidate_id FROM candidate_packets")
        ids = [dict(r)["candidate_id"] for r in c.fetchall()]
        c.execute(
            "SELECT resolution_id FROM contradiction_log WHERE memory_a_id=1"
        )
        row = dict(c.fetchone())
    assert ids == [keep_id]
    assert row["resolution_id"] is None


def test_is_unique_integrity_error_narrow():
    """Low #4: only UNIQUE IntegrityError is treated as already_present."""
    import sqlite3

    from minni.afm_passes.inbox_ingest import _is_unique_integrity_error

    assert _is_unique_integrity_error(
        sqlite3.IntegrityError("UNIQUE constraint failed: index")
    )
    assert _is_unique_integrity_error(
        sqlite3.IntegrityError("unique constraint failed: candidate_packets")
    )
    assert not _is_unique_integrity_error(
        sqlite3.IntegrityError("CHECK constraint failed: status")
    )
    assert not _is_unique_integrity_error(
        sqlite3.IntegrityError("NOT NULL constraint failed: content")
    )
    assert not _is_unique_integrity_error(ValueError("nope"))


def test_missing_sha_different_bodies_not_collapsed(tmp_path):
    """Medium #2: absent content_sha1 digests from content; different bodies stay.

    Legacy / non-ingest writers may omit derived_from.content_sha1. Collapse
    must derive the digest from the content column so distinct bodies are
    reported divergent and never hard-deleted.
    """
    from minni.repair_dual_candidates import (
        find_divergent_content_groups,
        find_duplicate_candidate_groups,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    id_a = _insert_candidate(
        db,
        content="legacy body alpha — unique text",
        status="accepted",
        inbox_file="legacy.json",
        candidate_index=0,
        omit_content_sha1=True,
    )
    id_b = _insert_candidate(
        db,
        content="legacy body beta — different entirely",
        status="rejected",
        inbox_file="legacy.json",
        candidate_index=0,
        omit_content_sha1=True,
    )

    assert find_duplicate_candidate_groups(db) == []
    divergent = find_divergent_content_groups(db)
    assert len(divergent) == 1
    assert set(divergent[0]["candidate_ids"]) == {id_a, id_b}
    # Digests derived from content are distinct non-empty strings.
    shas = [s for s in divergent[0]["content_sha1s"] if s]
    assert len(shas) == 2

    result = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert result["deleted"] == 0
    assert result["groups_found"] == 0
    assert result["divergent_content_groups"] == 1
    with db.cursor() as c:
        c.execute("SELECT candidate_id FROM candidate_packets ORDER BY candidate_id")
        ids = [dict(r)["candidate_id"] for r in c.fetchall()]
    assert ids == [id_a, id_b]


def test_missing_sha_identical_bodies_still_collapsed(tmp_path):
    """Medium #2 green path: no stored sha, same body → digest match → collapse."""
    from minni.repair_dual_candidates import (
        find_duplicate_candidate_groups,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    body = "legacy twin with no content_sha1 field"
    keep = _insert_candidate(
        db,
        content=body,
        status="accepted",
        inbox_file="legacy-same.json",
        candidate_index=1,
        omit_content_sha1=True,
    )
    lose = _insert_candidate(
        db,
        content=body,
        status="rejected",
        inbox_file="legacy-same.json",
        candidate_index=1,
        omit_content_sha1=True,
    )

    groups = find_duplicate_candidate_groups(db)
    assert len(groups) == 1
    assert groups[0]["winner_id"] == keep
    assert lose in groups[0]["loser_ids"]
    # Collapse key digest is non-None (derived from content).
    assert groups[0]["key"]["content_sha1"]

    result = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert result["deleted"] == 1
    with db.cursor() as c:
        c.execute("SELECT candidate_id FROM candidate_packets")
        ids = [dict(r)["candidate_id"] for r in c.fetchall()]
    assert ids == [keep]


def test_stale_stored_sha_divergent_bodies_not_collapsed(tmp_path):
    """Medium: present-but-wrong content_sha1 must not group different bodies."""
    import hashlib

    from minni.repair_dual_candidates import (
        find_divergent_content_groups,
        find_duplicate_candidate_groups,
        repair_duplicate_candidate_pairs,
    )

    db, _cfg = _make_db(tmp_path)
    # Same stale/copied metadata sha for two different bodies.
    fake_sha = hashlib.sha1(b"unrelated body").hexdigest()
    id_a = _insert_candidate(
        db,
        content="real body alpha",
        status="accepted",
        inbox_file="stale-sha.json",
        candidate_index=0,
        content_sha1=fake_sha,
    )
    id_b = _insert_candidate(
        db,
        content="real body beta totally different",
        status="rejected",
        inbox_file="stale-sha.json",
        candidate_index=0,
        content_sha1=fake_sha,
    )

    assert find_duplicate_candidate_groups(db) == []
    divergent = find_divergent_content_groups(db)
    assert len(divergent) == 1
    assert set(divergent[0]["candidate_ids"]) == {id_a, id_b}

    result = repair_duplicate_candidate_pairs(db, dry_run=False)
    assert result["deleted"] == 0
    assert result["groups_found"] == 0
    assert result["divergent_content_groups"] == 1
    with db.cursor() as c:
        c.execute("SELECT candidate_id FROM candidate_packets ORDER BY candidate_id")
        ids = [dict(r)["candidate_id"] for r in c.fetchall()]
    assert ids == [id_a, id_b]


def test_virtual_durable_chunk_only_not_orphan(tmp_path):
    """Medium: virtual _durable with chunks but no FTS is not pruned as orphan."""
    from minni.repair_dual_candidates import (
        find_orphan_virtual_durable,
        repair_index_disk_divergence,
    )

    db, cfg = _make_db(tmp_path)
    path = os.path.join(cfg.vault_path, "_durable", "codex__chunkonly.md")
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'codex', 0, 0, 'accepted', 'safe')
            """,
            (path,),
        )
        doc_id = c.lastrowid
        c.execute(
            """
            INSERT INTO chunk_embeddings
            (doc_id, chunk_index, chunk_text, embedding, computed_at)
            VALUES (?, 0, 'semantic only body', X'00', 0)
            """,
            (doc_id,),
        )

    assert find_orphan_virtual_durable(db) == []
    result = repair_index_disk_divergence(db, dry_run=False)
    assert result["orphan_virtual_durable"] == 0
    assert result["prune"]["deleted"] == 0
    assert result["healthy_virtual_durable_kept"] == 1
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM documents")
        assert dict(c.fetchone())["n"] == 1
        c.execute("SELECT COUNT(*) AS n FROM chunk_embeddings")
        assert dict(c.fetchone())["n"] == 1


def test_prune_document_rows_invalidates_faiss_and_rerank(tmp_path):
    """Medium #3: prune drops SQLite and tombstones FAISS + rerank cache."""
    from minni.repair_dual_candidates import prune_document_rows
    from minni.rerank_cache import GLOBAL_RERANK_CACHE, invalidate_chunks

    class _FakeFaiss:
        """Minimal FAISSIndex stand-in (avoids faiss/numpy env import issues)."""

        def __init__(self):
            self._reverse_map: dict[int, int] = {}
            self.removed: list[int] = []

        def add(self, chunk_id: int) -> None:
            self._reverse_map[chunk_id] = len(self._reverse_map)

        def remove(self, chunk_id: int) -> None:
            self._reverse_map.pop(chunk_id, None)
            self.removed.append(chunk_id)

    db, _cfg = _make_db(tmp_path)
    faiss = _FakeFaiss()
    path = str(tmp_path / "vault" / "wiki" / "orphan.md")
    now = time.time()
    # 384 float32 zeros without importing numpy (env may have a broken numpy).
    empty_emb = b"\x00" * (384 * 4)
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'main', 0, 0, 'accepted', 'safe')
            """,
            (path,),
        )
        doc_id = c.lastrowid
        c.execute(
            """
            INSERT INTO chunk_embeddings
            (doc_id, chunk_index, chunk_text, embedding, heading_context, computed_at)
            VALUES (?, 0, 'stale chunk', ?, '', ?)
            """,
            (doc_id, empty_emb, now),
        )
        chunk_id = c.lastrowid
        c.execute(
            """
            INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
            VALUES (?, ?, 'stale chunk', 'main', '?')
            """,
            (doc_id, path),
        )

    faiss.add(chunk_id)
    assert chunk_id in faiss._reverse_map

    # Seed rerank cache so we can observe invalidation.
    try:
        if hasattr(GLOBAL_RERANK_CACHE, "_by_chunk"):
            GLOBAL_RERANK_CACHE._by_chunk[int(chunk_id)] = object()
        elif hasattr(GLOBAL_RERANK_CACHE, "_cache"):
            GLOBAL_RERANK_CACHE._cache[int(chunk_id)] = {"dummy": True}
    except Exception:
        pass

    result = prune_document_rows(
        db, [doc_id], dry_run=False, faiss_index=faiss
    )
    assert result["deleted"] == 1
    assert result["semantic"]["chunk_ids"] == 1
    assert result["semantic"]["faiss_status"] == "ok"
    assert result["semantic"]["faiss_removed"] == 1
    assert result["semantic"]["rerank_invalidated"] is True
    assert chunk_id not in faiss._reverse_map
    assert chunk_id in faiss.removed

    # invalidate_chunks is the same path purge_durable_document uses.
    assert callable(invalidate_chunks)

    with db.cursor() as c:
        assert c.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()["n"] == 0
        assert c.execute(
            "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id=?", (doc_id,)
        ).fetchone()["n"] == 0
        assert c.execute(
            "SELECT COUNT(*) AS n FROM vault_fts WHERE doc_id=?", (doc_id,)
        ).fetchone()["n"] == 0

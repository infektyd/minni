"""Tests for Rev 6 Self-Cleaning Memory Substrate (Pillars 1-5)."""

from __future__ import annotations

import json
import struct
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from minni.afm_passes.consolidation import (
    dedup_active_learnings,
    expire_and_compact_candidates,
    run as run_consolidation_pass,
)
from minni.config import DEFAULT_CONFIG, MinniConfig
from minni.db import MinniDB
from minni.decay import MemoryDecay, aggregate_use_ledger
from minni.durable_projection import (
    durable_doc_path,
    evict_learning_to_cold_archive,
    find_near_duplicate_active_learning,
    five_gram_jaccard,
)
from minni.episodic import EpisodicMemory
from minni.indexer import (
    UNEMBEDDED_STATUSES,
    VaultIndexer,
    extract_explicit_frontmatter_status,
    is_vault_path_excluded,
)
from minni.minnid_runtime.governance import (
    GovernanceContext,
    auto_accept_blockers,
    non_durable_promotion_blocker,
    resolve_candidate,
    stage_candidate,
)
from minni.minnid_runtime.recall import RecallContext, _handle_search, handle_read
from minni.minnid_runtime.vault_index import VaultIndexContext, handle_vault_index_doc
from minni.retrieval import RetrievalEngine
from minni.writeback import WriteBackMemory


def _make_principal(
    agent_id: str = "main",
    *,
    auto_accept_own: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        role="operator",
        workspace_id="default",
        allowed_scopes=("*",),
        capabilities=["*"],
        session_id="sess-test",
        transport="uds",
        auto_accept_own=auto_accept_own,
    )


def _make_isolated_db(tmp_path: Path) -> tuple[MinniDB, MinniConfig, Path]:
    vault_dir = tmp_path / "test-vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "sovereign.db"
    cfg = MinniConfig(
        db_path=str(db_path),
        vault_path=str(vault_dir),
        embedding_dim=4,
    )
    db = MinniDB(cfg)
    return db, cfg, vault_dir


def _pack_vec(vals: list[float]) -> bytes:
    return struct.pack(f"{len(vals)}f", *vals)


def _make_governance_context(
    db: MinniDB,
    cfg: MinniConfig,
    *,
    principal: SimpleNamespace | None = None,
    archived_cids: list[int] | None = None,
) -> GovernanceContext:
    wb = WriteBackMemory.__new__(WriteBackMemory)
    wb.db = db
    wb.config = cfg
    wb._model = SimpleNamespace(
        encode=lambda text, **kw: SimpleNamespace(
            astype=lambda _dt: SimpleNamespace(tobytes=lambda: _pack_vec([0.5, 0.5, 0.5, 0.5]))
        )
    )
    wb._write_to_disk = lambda *a, **kw: None
    ep = EpisodicMemory(db, cfg)
    eff_principal = principal or _make_principal("claude-code", auto_accept_own=True)
    return GovernanceContext(
        make_error=lambda code, msg, req_id, data=None: {
            "error": {"code": code, "message": msg, "data": data},
            "id": req_id,
        },
        make_response=lambda res, req_id: {"result": res, "id": req_id},
        handler_principal=lambda params, req_id: (eff_principal, None),
        lazy_writeback=lambda: wb,
        lazy_episodic=lambda: ep,
        record_latency=lambda name, dur: None,
        index_durable_learning=lambda *a, **kw: True,
        maybe_archive_inbox_source=lambda _db, cid: (
            archived_cids.append(int(cid)) if archived_cids is not None else None
        ),
        sovereign_db=lambda *a, **kw: db,
        increment_request_count=lambda: None,
    )


def _make_recall_context(
    db: MinniDB,
    cfg: MinniConfig,
    engine: Any,
    *,
    principal: SimpleNamespace | None = None,
) -> RecallContext:
    eff_principal = principal or _make_principal("main")
    ep = EpisodicMemory(db, cfg)
    return RecallContext(
        make_error=lambda code, msg, req_id, data=None: {
            "error": {"code": code, "message": msg, "data": data},
            "id": req_id,
        },
        make_response=lambda res, req_id: {"result": res, "id": req_id},
        handler_principal=lambda params, req_id: (eff_principal, None),
        lazy_retrieval=lambda: engine,
        agent_vault_retrieval=lambda aid: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: deque(maxlen=32),
        record_latency=lambda name, dur: None,
        increment_request_count=lambda: None,
        lazy_episodic=lambda: ep,
        sovereign_db=lambda *a, **kw: db,
        default_config=cfg,
        can_read_document=lambda *a, **kw: True,
    )


# ---------------------------------------------------------------------------
# Pillar 1: Harvest Demotion & Write-Time Consolidation
# ---------------------------------------------------------------------------


def test_pillar1_harvest_and_chatter_blockers():
    # Harvest sources without explicit invariant markers are blocked from promotion
    b1 = non_durable_promotion_blocker(
        content="Always verify database migrations with strict schema checks.",
        category="architecture",
        source="harvest",
    )
    assert b1 is not None and b1.startswith("non_durable:")

    b2 = non_durable_promotion_blocker(
        content="Always verify database migrations with strict schema checks.",
        category="architecture",
        origin="subagent_harvest",
    )
    assert b2 is not None and b2.startswith("non_durable:")

    blockers = auto_accept_blockers(
        "Always verify database migrations with strict schema checks.",
        False,
        category="architecture",
        source="session_harvest",
    )
    assert any(b.startswith("non_durable:") for b in blockers)

    # Preference without explicit invariant markers is blocked
    b_pref = non_durable_promotion_blocker(
        content="Prefers dark mode in local preview windows when working late.",
        category="preference",
    )
    assert b_pref is not None and b_pref.startswith("non_durable:")

    # Preference WITH explicit invariant marker is allowed
    assert (
        non_durable_promotion_blocker(
            content="Operator rule: never run git stash across shared worktrees; invariant.",
            category="preference",
        )
        is None
    )

    # Sprint chatter is blocked as noise even under architecture/correction categories
    assert (
        non_durable_promotion_blocker(
            content="Merged PR #312 and fixed test_foo in branch feat/cleanup.",
            category="architecture",
        )
        == "non_durable:noise"
    )
    assert (
        non_durable_promotion_blocker(
            content="Phase 2 is complete: 42/42 tests passed, 0 failed.",
            category="decision",
        )
        == "non_durable:noise"
    )


def test_pillar1_stage_candidate_blocks_harvest_and_evicts_near_duplicates(tmp_path: Path):
    db, cfg, _ = _make_isolated_db(tmp_path)
    ctx = _make_governance_context(db, cfg)

    try:
        # 1. Harvest candidate is forced to status='proposed' even when auto_accept_own=True
        res_harvest = stage_candidate(
            {
                "content": "SQLite WAL connections set busy_timeout to 5000ms on concurrent writers.",
                "category": "architecture",
                "source": "jev_harvest",
            },
            request_id=1,
            context=ctx,
        )
        assert res_harvest["result"]["status"] == "proposed"
        assert any(
            b.startswith("non_durable:")
            for b in (res_harvest["result"].get("auto_accept_withheld") or [])
        )

        # 2. First durable auto-accepted learning writes to learnings
        text_v1 = (
            "All SQLite connections in minnid must enable WAL mode and set "
            "busy_timeout to 5000 milliseconds before executing write transactions."
        )
        res_v1 = stage_candidate(
            {
                "content": text_v1,
                "category": "architecture",
                "source": "manual",
            },
            request_id=2,
            context=ctx,
        )
        assert res_v1["result"]["status"] == "accepted"
        lid_v1 = res_v1["result"]["learning_id"]
        assert lid_v1 is not None

        # 3. Near-duplicate v2 (5-gram Jaccard >= 0.65) auto-accepts and cold-evicts v1
        text_v2 = (
            "All SQLite connections in minnid must enable WAL mode and set "
            "busy_timeout to 5000 milliseconds before executing any write transaction."
        )
        assert five_gram_jaccard(text_v1, text_v2) >= 0.65

        res_v2 = stage_candidate(
            {
                "content": text_v2,
                "category": "architecture",
                "source": "manual",
            },
            request_id=3,
            context=ctx,
        )
        assert res_v2["result"]["status"] == "accepted"
        lid_v2 = res_v2["result"]["learning_id"]
        assert lid_v2 != lid_v1

        with db.cursor() as c:
            rem_v1 = c.execute(
                "SELECT COUNT(*) FROM learnings WHERE learning_id = ?", (lid_v1,)
            ).fetchone()[0]
            rem_v2 = c.execute(
                "SELECT COUNT(*) FROM learnings WHERE learning_id = ?", (lid_v2,)
            ).fetchone()[0]
        assert rem_v1 == 0
        assert rem_v2 == 1

        archive_files = list((tmp_path / "_archive").glob("tombstones-*.jsonl"))
        assert len(archive_files) == 1
        tombstones = [
            json.loads(line)
            for line in archive_files[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(t.get("learning_id") == lid_v1 for t in tombstones)
    finally:
        db.close()


def test_pillar1_cosine_near_duplicate_detection(tmp_path: Path):
    db, cfg, _ = _make_isolated_db(tmp_path)
    try:
        emb_a = _pack_vec([1.0, 0.0, 0.0, 0.0])
        emb_b = _pack_vec([0.95, 0.05, 0.0, 0.0])  # cosine > 0.99
        with db.transaction() as c:
            c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, embedding)
                   VALUES (?, ?, ?, 1.0, ?, 'active', ?)""",
                ("claude-code", "architecture", "First phrasing of invariant A", time.time(), emb_a),
            )
            lid_a = int(c.lastrowid)

        matches = find_near_duplicate_active_learning(
            db,
            agent_id="claude-code",
            category="architecture",
            content="Completely different surface words for invariant A",
            embedding=emb_b,
        )
        assert lid_a in matches
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pillar 2: Terminal Purge, Cold Archive & Candidate Compaction
# ---------------------------------------------------------------------------


def test_pillar2_evict_learning_atomic_purge_and_reject_cleanup(tmp_path: Path):
    db, cfg, vault_dir = _make_isolated_db(tmp_path)
    archived_cids: list[int] = []
    ctx = _make_governance_context(
        db,
        cfg,
        principal=_make_principal("claude-code", auto_accept_own=False),
        archived_cids=archived_cids,
    )
    engine = SimpleNamespace(db=db, config=cfg, _faiss=None)

    try:
        content = "Never store raw API tokens in public repository fixtures; invariant."
        with db.transaction() as c:
            c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status)
                   VALUES (?, ?, ?, 1.0, ?, 'active')""",
                ("claude-code", "security", content, time.time()),
            )
            lid = int(c.lastrowid)
            c.execute(
                "INSERT INTO learnings_fts(learning_id, agent_id, content, category) VALUES (?, ?, ?, ?)",
                (lid, "claude-code", content, "security"),
            )
            doc_path = Path(
                durable_doc_path("claude-code", f"learning:{lid}", str(vault_dir), content)
            )
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text(f"# Durable {lid}\n{content}\n", encoding="utf-8")

            c.execute(
                """INSERT INTO documents
                   (path, agent, sigil, indexed_at, page_status)
                   VALUES (?, 'claude-code', '#[[sec]]', ?, 'accepted')""",
                (str(doc_path), time.time()),
            )
            doc_id = int(c.lastrowid)
            c.execute(
                "INSERT INTO learning_documents (learning_id, doc_id) VALUES (?, ?)",
                (lid, doc_id),
            )
            c.execute(
                "INSERT INTO vault_fts(doc_id, path, content, agent, sigil) VALUES (?, ?, ?, 'claude-code', '#[[sec]]')",
                (doc_id, str(doc_path), content),
            )
            c.execute(
                """INSERT INTO chunk_embeddings
                   (doc_id, chunk_index, chunk_text, embedding, model_name)
                   VALUES (?, 0, ?, ?, 'bge')""",
                (doc_id, content, _pack_vec([0.1, 0.2, 0.3, 0.4])),
            )

        assert doc_path.exists()

        # Evict to cold archive
        ev = evict_learning_to_cold_archive(
            db,
            engine,
            lid,
            "unit_test_purge",
            db_path=cfg.db_path,
            vault_path=str(vault_dir),
        )
        assert ev["evicted"] is True
        assert not doc_path.exists()

        with db.cursor() as c:
            assert c.execute("SELECT COUNT(*) FROM learnings WHERE learning_id = ?", (lid,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM learnings_fts WHERE learning_id = ?", (lid,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM learning_documents WHERE learning_id = ?", (lid,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM vault_fts WHERE doc_id = ?", (doc_id,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM chunk_embeddings WHERE doc_id = ?", (doc_id,)).fetchone()[0] == 0

        # Also verify resolve_candidate(decision="reject") supersedes afm_review and archives inbox source
        staged = stage_candidate(
            {"content": "Candidate to reject cleanly without leaving orphaned markers."},
            request_id=10,
            context=ctx,
        )
        cid_rej = int(staged["result"]["candidate_id"])
        with db.transaction() as c:
            c.execute(
                """INSERT INTO consolidation_actions
                   (action_type, claim, category, status, detail, created_at)
                   VALUES ('afm_review', ?, 'general', 'pending', 'test fence', ?)""",
                (str(cid_rej), time.time()),
            )
        rej_res = resolve_candidate(
            {"candidate_id": cid_rej, "decision": "reject", "reason": "not durable"},
            request_id=11,
            context=ctx,
        )
        assert rej_res["result"]["new_status"] == "rejected"
        assert cid_rej in archived_cids
        with db.cursor() as c:
            pending_fences = c.execute(
                """SELECT COUNT(*) FROM consolidation_actions
                   WHERE action_type='afm_review' AND claim=? AND status='pending'""",
                (str(cid_rej),),
            ).fetchone()[0]
        assert pending_fences == 0
    finally:
        db.close()


def test_pillar2_dedup_active_learnings_and_candidate_compaction(tmp_path: Path):
    db, cfg, _ = _make_isolated_db(tmp_path)
    now = time.time()

    try:
        with db.transaction() as c:
            # Insert two near-duplicate active learnings and one low-signal boot learning
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                   VALUES ('main', 'architecture',
                           'Daemon Unix socket RPC handler must validate principal scope before executing mutations.',
                           0.8, ?, 'active')""",
                (now - 100,),
            )
            lid_old = int(c.lastrowid)
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                   VALUES ('main', 'architecture',
                           'Daemon Unix socket RPC handler must validate principal scope before executing any mutation.',
                           0.95, ?, 'active')""",
                (now - 10,),
            )
            lid_new = int(c.lastrowid)
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                   VALUES ('main', 'general', 'Executed prompt: run consolidation pass now', 0.5, ?, 'active')""",
                (now - 50,),
            )
            lid_low = int(c.lastrowid)

            # Insert terminal candidate_packets: one 10 days old, one 1 day old, one expired
            c.execute(
                """INSERT INTO candidate_packets
                   (principal, content, status, proposed_at, resolved_at, resolved_by)
                   VALUES ('main', 'Old rejected payload to compact',
                           'rejected', ?, ?, 'operator')""",
                (now - 10 * 86400, now - 9 * 86400),
            )
            cid_old = int(c.lastrowid)

            c.execute(
                """INSERT INTO candidate_packets
                   (principal, content, status, proposed_at, resolved_at, resolved_by)
                   VALUES ('main', 'Recent rejected payload to keep',
                           'rejected', ?, ?, 'operator')""",
                (now - 86400, now - 3600),
            )
            cid_recent = int(c.lastrowid)

            c.execute(
                """INSERT INTO candidate_packets
                   (principal, content, status, proposed_at)
                   VALUES ('main', 'Expired candidate payload',
                           'expired', ?)""",
                (now - 10 * 86400,),
            )
            cid_exp = int(c.lastrowid)

        dedup_res = dedup_active_learnings(db)
        assert dedup_res["superseded_duplicates"] >= 1
        assert dedup_res["pruned_low_signal"] >= 1

        with db.cursor() as c:
            rem_ids = {
                r["learning_id"]
                for r in c.execute("SELECT learning_id FROM learnings").fetchall()
            }
        assert lid_new in rem_ids
        assert lid_old not in rem_ids
        assert lid_low not in rem_ids

        # Expire and compact candidates
        archived_cids: list[int] = []
        comp_res = expire_and_compact_candidates(
            db,
            now=now,
            config=cfg,
            archive_callback=lambda _db, cid: archived_cids.append(int(cid)),
        )
        assert comp_res["expired_cleaned"] >= 1
        assert comp_res["truncated"] >= 1
        assert cid_exp in archived_cids

        with db.cursor() as c:
            row_old = c.execute(
                "SELECT content, status, resolved_by FROM candidate_packets WHERE candidate_id = ?",
                (cid_old,),
            ).fetchone()
            row_recent = c.execute(
                "SELECT content FROM candidate_packets WHERE candidate_id = ?",
                (cid_recent,),
            ).fetchone()
        assert row_old["content"] == ""
        assert row_old["status"] == "rejected"
        assert row_old["resolved_by"] == "operator"
        assert row_recent["content"] == "Recent rejected payload to keep"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pillar 3: Vault Indexer Path & Status Allowlist
# ---------------------------------------------------------------------------


def test_pillar3_path_and_status_allowlist_across_indexers(tmp_path: Path):
    for required_status in (
        "complete",
        "completed",
        "archived",
        "obsolete",
        "deprecated",
        "rejected",
        "superseded",
        "candidate",
        "expired",
        "draft",
    ):
        assert required_status in UNEMBEDDED_STATUSES

    db, cfg, vault_dir = _make_isolated_db(tmp_path)
    try:
        # Excluded directories / files
        for excluded_rel in (
            "wiki/sessions/20261027-sess.md",
            "wiki/tasks/task1.md",
            "wiki/handoffs/handoff1.md",
            "inbox/cand.md",
            "archive/old.md",
            "quarantine/q.md",
            "logs/run.md",
            ".runtime/ledger.md",
            ".obsidian/workspace.md",
            "raw/dump.md",
            "notes/debug.log.md",
            "notes/session-compact-1.md",
        ):
            p = vault_dir / excluded_rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("---\nstatus: active\n---\n# Excluded\nBody text", encoding="utf-8")
            excluded, _reason = is_vault_path_excluded(p, vault_dir)
            assert excluded is True, f"Expected {excluded_rel} to be excluded"

        # Allowed directories (_durable/ and layer1/)
        durable_note = vault_dir / "_durable" / "101-invariant.md"
        durable_note.parent.mkdir(parents=True, exist_ok=True)
        durable_note.write_text("---\nstatus: accepted\n---\n# Durable\nValid note", encoding="utf-8")
        assert is_vault_path_excluded(durable_note, vault_dir)[0] is False

        layer1_note = vault_dir / "layer1" / "core.md"
        layer1_note.parent.mkdir(parents=True, exist_ok=True)
        layer1_note.write_text("# Identity Core\nAlways loaded whole.", encoding="utf-8")
        assert is_vault_path_excluded(layer1_note, vault_dir)[0] is False

        # Note with excluded frontmatter status (draft)
        draft_rel = "knowledge/draft_spec.md"
        draft_note = vault_dir / draft_rel
        draft_note.parent.mkdir(parents=True, exist_ok=True)
        draft_text = "---\nstatus: draft\n---\n# Draft Spec\nDo not index"
        draft_note.write_text(draft_text, encoding="utf-8")
        assert extract_explicit_frontmatter_status(draft_text) == "draft"

        # handle_vault_index_doc rejects excluded paths and draft status, and retracts existing rows
        with db.transaction() as c:
            c.execute(
                """INSERT INTO documents (path, agent, sigil, indexed_at, page_status)
                   VALUES (?, 'main', 'sig', ?, 'active')""",
                (str(draft_note.resolve()), time.time()),
            )
            pre_doc_id = int(c.lastrowid)
            c.execute(
                "INSERT INTO vault_fts(doc_id, path, content, agent, sigil) VALUES (?, ?, 'c', 'main', 's')",
                (pre_doc_id, str(draft_note.resolve())),
            )

        v_ctx = VaultIndexContext(
            make_error=lambda code, msg, req_id, data=None: {"error": msg, "id": req_id},
            make_response=lambda res, req_id: {"result": res, "id": req_id},
            make_mismatch_error=lambda w, s, r: {"error": "mismatch", "id": r},
            handler_principal=lambda p, r: (_make_principal("main"), None),
            guard_vault_root=lambda *a, **kw: None,
            lazy_retrieval=lambda: SimpleNamespace(db=db, config=cfg, _faiss=None),
            agent_vault=lambda aid: (vault_dir, True),
            record_latency=lambda *a, **kw: None,
            increment_request_count=lambda: None,
        )

        res_draft = handle_vault_index_doc(
            {"path": draft_rel, "content": draft_text, "agent": "main"},
            request_id=10,
            context=v_ctx,
        )
        assert res_draft["result"]["indexed"] is False
        assert res_draft["result"]["skipped"] is True

        with db.cursor() as c:
            assert c.execute("SELECT COUNT(*) FROM documents WHERE doc_id = ?", (pre_doc_id,)).fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM vault_fts WHERE doc_id = ?", (pre_doc_id,)).fetchone()[0] == 0

        res_inbox = handle_vault_index_doc(
            {"path": "inbox/cand.md", "content": "# Inbox note\nBody", "agent": "main"},
            request_id=11,
            context=v_ctx,
        )
        assert res_inbox["result"]["indexed"] is False
        assert res_inbox["result"]["reason"] == "excluded_path"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pillar 4: Recall-Trace Feedback Loop Elimination
# ---------------------------------------------------------------------------


def test_pillar4_recall_trace_default_false_and_zero_episodic_writes(tmp_path: Path):
    assert DEFAULT_CONFIG.recall_trace is False
    assert MinniConfig().recall_trace is False

    db, cfg, _ = _make_isolated_db(tmp_path)
    try:
        engine = MagicMock()
        engine.db = db
        engine.config = cfg
        engine._is_large_corpus.return_value = False
        engine.retrieve.return_value = [
            {
                "doc_id": 1,
                "path": "/tmp/doc1.md",
                "agent": "main",
                "sigil": "#[[doc1]]",
                "chunk_text": "Relevant evidence text",
                "score": 0.9,
            }
        ]
        engine.search_learnings.return_value = []
        engine.search_episodic.return_value = []

        r_ctx = _make_recall_context(db, cfg, engine)
        resp = _handle_search({"query": "evidence text"}, request_id=1, context=r_ctx)
        assert "result" in resp

        with db.cursor() as c:
            ep_count = c.execute("SELECT COUNT(*) FROM episodic_events").fetchone()[0]
            ep_fts_count = c.execute("SELECT COUNT(*) FROM episodic_fts").fetchone()[0]
        assert ep_count == 0
        assert ep_fts_count == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pillar 5: Truthful Access Accounting & Use-Ledger Driven Decay
# ---------------------------------------------------------------------------


def test_pillar5_single_access_increment_and_passive_search(tmp_path: Path):
    db, cfg, vault_dir = _make_isolated_db(tmp_path)
    try:
        with db.transaction() as c:
            c.execute(
                """INSERT INTO documents
                   (doc_id, path, agent, sigil, indexed_at, access_count, decay_score)
                   VALUES (10, ?, 'main', '#[[doc10]]', ?, 0, 1.0)""",
                (str(vault_dir / "doc10.md"), time.time()),
            )
            c.execute(
                """INSERT INTO learnings
                   (learning_id, agent_id, category, content, confidence, created_at, access_count, status)
                   VALUES (20, 'main', 'architecture', 'Strict access count invariant test', 1.0, ?, 0, 'active')""",
                (time.time(),),
            )
            c.execute(
                "INSERT INTO learnings_fts(learning_id, agent_id, content, category) VALUES (20, 'main', 'Strict access count invariant test', 'architecture')"
            )

        engine = RetrievalEngine.__new__(RetrievalEngine)
        engine.db = db
        engine.config = cfg

        # Multiple results referencing the same doc_id=10 in one retrieve() call
        dup_results = [
            {"doc_id": 10, "chunk_index": 0, "score": 0.9},
            {"doc_id": 10, "chunk_index": 1, "score": 0.8},
            {"doc_id": 10, "chunk_index": 2, "score": 0.7, "source": "graph_expansion"},
        ]
        engine._record_retrieved_access(dup_results)

        with db.cursor() as c:
            ac = c.execute("SELECT access_count FROM documents WHERE doc_id = 10").fetchone()[0]
        assert ac == 1

        # Passive search (passive=True or update_access=False) must NOT increment access_count
        mock_engine = MagicMock()
        mock_engine.db = db
        mock_engine.config = cfg
        mock_engine._is_large_corpus.return_value = False
        mock_engine.retrieve.return_value = [
            {
                "doc_id": 10,
                "path": str(vault_dir / "doc10.md"),
                "agent": "main",
                "sigil": "#[[doc10]]",
                "chunk_text": "Strict access count invariant test",
                "score": 0.9,
            }
        ]
        mock_engine.search_learnings.side_effect = (
            lambda q, **kw: RetrievalEngine.search_learnings(engine, q, **kw)
        )

        r_ctx = _make_recall_context(db, cfg, mock_engine)
        _handle_search(
            {"query": "Strict access count", "passive": True},
            request_id=50,
            context=r_ctx,
        )
        with db.cursor() as c:
            doc_ac = c.execute("SELECT access_count FROM documents WHERE doc_id = 10").fetchone()[0]
            learn_ac = c.execute("SELECT access_count FROM learnings WHERE learning_id = 20").fetchone()[0]
        assert doc_ac == 1
        assert learn_ac == 0
    finally:
        db.close()


def test_pillar5_use_ledger_demotion_and_eviction(tmp_path: Path):
    db, cfg, vault_dir = _make_isolated_db(tmp_path)
    now = time.time()

    try:
        with db.transaction() as c:
            # Learning 1: 15 surfaces across 5 sessions, 0 uses -> 0.5x demotion
            c.execute(
                """INSERT INTO learnings
                   (learning_id, agent_id, category, content, confidence, created_at, status)
                   VALUES (1, 'main', 'architecture', 'Demoted architecture note alpha', 1.0, ?, 'active')""",
                (now,),
            )
            c.execute(
                "INSERT INTO learnings_fts(learning_id, agent_id, content, category) VALUES (1, 'main', 'Demoted architecture note alpha', 'architecture')"
            )
            # Learning 2: 25 surfaces across 8 sessions, 0 uses -> cold-archive eviction
            c.execute(
                """INSERT INTO learnings
                   (learning_id, agent_id, category, content, confidence, created_at, status)
                   VALUES (2, 'main', 'architecture', 'Evicted architecture note beta', 1.0, ?, 'active')""",
                (now,),
            )
            c.execute(
                "INSERT INTO learnings_fts(learning_id, agent_id, content, category) VALUES (2, 'main', 'Evicted architecture note beta', 'architecture')"
            )
            # Learning 3: 25 surfaces across 8 sessions, 1 use -> protected (used!)
            c.execute(
                """INSERT INTO learnings
                   (learning_id, agent_id, category, content, confidence, created_at, status)
                   VALUES (3, 'main', 'architecture', 'Used architecture note alpha beta', 1.0, ?, 'active')""",
                (now,),
            )
            c.execute(
                "INSERT INTO learnings_fts(learning_id, agent_id, content, category) VALUES (3, 'main', 'Used architecture note alpha beta', 'architecture')"
            )
            # Document 100: identity document (whole_document=1) with 30 surfaces, 0 uses -> protected!
            c.execute(
                """INSERT INTO documents
                   (doc_id, path, agent, sigil, indexed_at, decay_score, whole_document)
                   VALUES (100, ?, 'identity:main', '#[[core]]', ?, 1.0, 1)""",
                (str(vault_dir / "layer1" / "core.md"), now),
            )
            # Document 101: regular document with 15 surfaces across 5 sessions, 0 uses -> 0.5x demoted
            c.execute(
                """INSERT INTO documents
                   (doc_id, path, agent, sigil, indexed_at, decay_score, whole_document)
                   VALUES (101, ?, 'main', '#[[doc101]]', ?, 1.0, 0)""",
                (str(vault_dir / "knowledge" / "doc101.md"), now),
            )

        # Write use-ledger JSONL files under <vault>/.runtime/use-ledger/main/
        # exercising exposure, surfaced/judged, and single-event schemas
        ledger_dir = vault_dir / ".runtime" / "use-ledger" / "main"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []

        # Learning 1 & Doc 101: 15 surfaces across 5 sessions (3 per session), 0 uses
        for s_idx in range(5):
            for _ in range(3):
                lines.append(
                    json.dumps(
                        {
                            "kind": "exposure",
                            "sessionKey": f"sess-{s_idx}",
                            "surfaced": ["learn:1", "doc:101"],
                            "used": [],
                        }
                    )
                )

        # Learning 2 & Identity Doc 100: 25 surfaces across 8 sessions, 0 uses
        for s_idx in range(8):
            count = 4 if s_idx == 0 else 3  # 4 + 7*3 = 25
            for _ in range(count):
                lines.append(
                    json.dumps(
                        {
                            "kind": "surfaced",
                            "sessionId": f"sess-{s_idx}",
                            "items": [{"id": "learn:2"}, {"id": "doc:100"}],
                        }
                    )
                )

        # Learning 3: 25 surfaces across 8 sessions + 1 judged use
        for s_idx in range(8):
            count = 4 if s_idx == 0 else 3
            for _ in range(count):
                lines.append(
                    json.dumps(
                        {
                            "kind": "surfaced",
                            "sessionId": f"sess-{s_idx}",
                            "items": [{"id": "learn:3"}],
                        }
                    )
                )
        lines.append(
            json.dumps(
                {
                    "kind": "judged",
                    "sessionId": "sess-0",
                    "verdicts": [{"id": "learn:3", "used": True}],
                }
            )
        )

        (ledger_dir / "sessions.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        ledger_map = aggregate_use_ledger(vault_dir)
        assert ledger_map["learning:1"].should_demote is True
        assert ledger_map["learning:2"].should_evict is True
        assert ledger_map["learning:3"].should_evict is False
        assert ledger_map["learning:3"].should_demote is False

        decay_stats = MemoryDecay(db, cfg).run_decay()
        assert decay_stats["demoted"] >= 2
        assert decay_stats["evicted"] >= 1

        with db.cursor() as c:
            l1 = c.execute("SELECT confidence FROM learnings WHERE learning_id = 1").fetchone()
            l2 = c.execute("SELECT learning_id FROM learnings WHERE learning_id = 2").fetchone()
            l3 = c.execute("SELECT confidence FROM learnings WHERE learning_id = 3").fetchone()
            d100 = c.execute("SELECT decay_score FROM documents WHERE doc_id = 100").fetchone()
            d101 = c.execute("SELECT decay_score FROM documents WHERE doc_id = 101").fetchone()

        assert l1 is not None and pytest.approx(float(l1["confidence"]), rel=1e-2) == 0.5
        assert l2 is None  # Cold-evicted!
        assert l3 is not None and pytest.approx(float(l3["confidence"]), rel=1e-2) == 1.0
        assert d100 is not None and pytest.approx(float(d100["decay_score"]), rel=1e-2) == 1.0
        assert d101 is not None and pytest.approx(float(d101["decay_score"]), rel=1e-2) == 0.5

        # Also verify handle_read applies 0.5x confidence demotion and filters sprint chatter
        with db.transaction() as c:
            c.execute(
                """INSERT INTO learnings
                   (learning_id, agent_id, category, content, confidence, created_at, status)
                   VALUES (4, 'main', 'architecture', 'Merged PR #999 a1b2c3d4 in sprint 5', 1.0, ?, 'active')""",
                (now + 10,),
            )
        r_ctx = _make_recall_context(db, cfg, MagicMock())
        read_res = handle_read({"agent_id": "main"}, request_id=99, context=r_ctx)
        recent = read_res["result"]["recent_learnings"]
        recent_ids = [item["learning_id"] for item in recent]
        assert 4 not in recent_ids  # Sprint chatter filtered out as noise
        assert 2 not in recent_ids  # Cold-evicted
        assert 3 in recent_ids and 1 in recent_ids
        # Non-demoted learning 3 ranks before demoted learning 1
        assert recent_ids.index(3) < recent_ids.index(1)
        l1_entry = next(item for item in recent if item["learning_id"] == 1)
        assert "(conf=0.5)" in l1_entry["line"]
    finally:
        db.close()


def test_pillar1_consolidation_run_rejects_non_durable_harvest_and_chatter(tmp_path: Path):
    db, cfg, _ = _make_isolated_db(tmp_path)
    now = time.time()
    try:
        with db.transaction() as c:
            # Candidate 1: harvest-sourced without invariant -> rejected by consolidation.run
            c.execute(
                """INSERT INTO candidate_packets
                   (principal, privacy_level, content, derived_from, status, proposed_at)
                   VALUES ('main', 'safe',
                           'Checked the deployment status and everything looked green today.',
                           ?, 'proposed', ?)""",
                (json.dumps({"source": "jev_harvest", "category": "general"}), now),
            )
            cid_harvest = int(c.lastrowid)

            # Candidate 2: sprint chatter -> rejected by consolidation.run
            c.execute(
                """INSERT INTO candidate_packets
                   (principal, privacy_level, content, derived_from, status, proposed_at)
                   VALUES ('main', 'safe',
                           'Workstream B is complete and 52/52 tests passed.',
                           ?, 'proposed', ?)""",
                (json.dumps({"source": "manual", "category": "architecture"}), now),
            )
            cid_chatter = int(c.lastrowid)

            # Candidate 3: genuine durable architecture rule -> promoted by consolidation.run
            c.execute(
                """INSERT INTO candidate_packets
                   (principal, privacy_level, content, derived_from, status, proposed_at)
                   VALUES ('main', 'safe',
                           'SQLite WAL mode must never be disabled on shared daemon databases; invariant.',
                           ?, 'proposed', ?)""",
                (json.dumps({"source": "manual", "category": "architecture"}), now),
            )
            cid_valid = int(c.lastrowid)

        res = run_consolidation_pass(db=db, config=cfg, dry_run=True)
        assert cid_harvest in res["reject_candidate_ids"]
        assert cid_chatter in res["reject_candidate_ids"]
        assert cid_valid in res["promote_candidate_ids"]
    finally:
        db.close()


def test_pillar3_vault_and_wiki_indexers_enforce_allowlist(tmp_path: Path, monkeypatch):
    from minni.wiki_indexer import WikiIndexer

    db, cfg, vault_dir = _make_isolated_db(tmp_path)
    try:
        stub_model = SimpleNamespace(
            encode=lambda text, **kw: SimpleNamespace(
                astype=lambda _dt: SimpleNamespace(
                    tobytes=lambda: _pack_vec([0.1, 0.2, 0.3, 0.4])
                )
            )
        )
        monkeypatch.setattr(VaultIndexer, "model", property(lambda self: stub_model))
        monkeypatch.setattr(WikiIndexer, "model", property(lambda self: stub_model))

        # Create excluded and allowed files in vault_dir
        (vault_dir / "inbox").mkdir(parents=True, exist_ok=True)
        (vault_dir / "inbox" / "staged.md").write_text("# Staged\nShould not index", encoding="utf-8")

        (vault_dir / "wiki" / "sessions").mkdir(parents=True, exist_ok=True)
        (vault_dir / "wiki" / "sessions" / "20261027-chat.md").write_text("# Session\nSkip", encoding="utf-8")

        (vault_dir / "wiki" / "artifacts").mkdir(parents=True, exist_ok=True)
        (vault_dir / "wiki" / "artifacts" / "unaccepted.md").write_text(
            "---\nstatus: draft\n---\n# Draft Artifact\nSkip", encoding="utf-8"
        )
        accepted_art = vault_dir / "wiki" / "artifacts" / "accepted_spec.md"
        accepted_art.write_text(
            "---\nstatus: accepted\n---\n# Accepted Artifact\nIndex this valid artifact body.",
            encoding="utf-8",
        )

        vi = VaultIndexer(db, cfg)
        vi.index_vault()

        wi = WikiIndexer(db, cfg)
        wi.index_wiki(str(vault_dir / "wiki"))

        with db.cursor() as c:
            indexed_paths = [
                r["path"] for r in c.execute("SELECT path FROM documents").fetchall()
            ]
        assert any("accepted_spec.md" in p for p in indexed_paths)
        assert not any("staged.md" in p for p in indexed_paths)
        assert not any("20261027-chat.md" in p for p in indexed_paths)
        assert not any("unaccepted.md" in p for p in indexed_paths)
    finally:
        db.close()


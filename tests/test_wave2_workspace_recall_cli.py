"""Wave 2 verification suite:
1. Migration 025 idempotency & backfill on fresh + pre-populated DBs (BUG-02, BUG-08, BUG-15).
2. workspace_id persistence across every write/promotion/index path (BUG-08).
3. Recall post-rerank + HyDE confidence floors, Repo-First 3-tier ranking, and
   can_read_document non-blackout on _handle_search (BUG-01, BUG-07, BUG-09a).
4. Blank post-frontmatter <EVIDENCE> stripping across retrieval, FTS, and trie (BUG-09b).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import minni.config as cfg_mod
import minni.minnid as minnid
from minni.config import MinniConfig, is_concrete_project_workspace, normalize_workspace_scope
from minni.db import MinniDB
from minni.graph_coordinator import LearningFields, _insert_learning
from minni.indexer import VaultIndexer
from minni.migrations import run_migrations
from minni.principal import EffectivePrincipal, can_read_document
from minni.retrieval import RetrievalEngine
from minni.writeback import WriteBackMemory


def _make_config(tmp_path: Path) -> MinniConfig:
    vault = tmp_path / "vault"
    learnings_dir = tmp_path / "learnings"
    vault.mkdir(parents=True, exist_ok=True)
    learnings_dir.mkdir(parents=True, exist_ok=True)
    return MinniConfig(
        db_path=str(tmp_path / "minni.db"),
        vault_path=str(vault),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_path=str(learnings_dir),
    )


# ---------------------------------------------------------------------------
# 1. Migration 025 Idempotency & Backfill (BUG-02, BUG-08, BUG-15)
# ---------------------------------------------------------------------------


def test_migration_025_fresh_db_has_workspace_columns_and_indexes(tmp_path: Path):
    cfg = _make_config(tmp_path)
    db = MinniDB(cfg)
    try:
        with db.cursor() as cur:
            doc_cols = {
                r["name"]: r for r in cur.execute("PRAGMA table_info(documents)").fetchall()
            }
            learn_cols = {
                r["name"]: r for r in cur.execute("PRAGMA table_info(learnings)").fetchall()
            }
            indexes = {
                r["name"]
                for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            versions = {
                r["version"]
                for r in cur.execute("SELECT version FROM schema_migrations").fetchall()
            }

        assert "workspace_id" in doc_cols
        assert "workspace_id" in learn_cols
        assert "idx_documents_workspace" in indexes
        assert "idx_learn_workspace" in indexes
        assert 25 in versions
    finally:
        db.close()


def test_migration_025_existing_db_with_documents_workspace_id_backfills_idempotently(
    tmp_path: Path,
):
    """Simulate the live ~/.minni/minni.db state:
    - migrations 001..024 applied, `documents.workspace_id` ALREADY exists (`TEXT DEFAULT ''`)
    - `learnings.workspace_id` does NOT exist yet
    - accepted `candidate_packets` and `learning_documents` rows hold concrete workspace_ids
    Running `run_migrations(conn)` twice + reopening `MinniDB` must raise zero
    duplicate-column errors and backfill `learnings.workspace_id`.
    """
    cfg = _make_config(tmp_path)
    seed_db = MinniDB(cfg)
    seed_db._get_conn()
    seed_db.close()

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Rebuild `learnings` WITHOUT `workspace_id` and unstamp migration 25 while
        # keeping `documents.workspace_id` already present — exact live DB shape!
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            PRAGMA legacy_alter_table = ON;
            DROP INDEX IF EXISTS idx_learn_workspace;
            DROP TRIGGER IF EXISTS trg_learnings_fts_ai;
            DROP TRIGGER IF EXISTS trg_learnings_fts_au;
            DROP TRIGGER IF EXISTS trg_learnings_fts_ad;
            ALTER TABLE learnings RENAME TO _learnings_with_ws;
            CREATE TABLE learnings (
                learning_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                content TEXT NOT NULL,
                source_doc_ids TEXT,
                source_query TEXT,
                confidence REAL DEFAULT 1.0,
                embedding BLOB,
                created_at REAL NOT NULL,
                access_count INTEGER DEFAULT 0,
                last_accessed REAL,
                superseded_by INTEGER,
                status TEXT DEFAULT 'active',
                content_hash TEXT
            );
            DROP TABLE _learnings_with_ws;
            DELETE FROM schema_migrations WHERE version = 25;
            PRAGMA legacy_alter_table = OFF;
            PRAGMA foreign_keys = ON;
            """
        )
        learn_cols_before = {
            r["name"] for r in conn.execute("PRAGMA table_info(learnings)").fetchall()
        }
        doc_cols_before = {
            r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        assert "workspace_id" not in learn_cols_before
        assert "workspace_id" in doc_cols_before

        now = time.time()
        conn.execute(
            "INSERT INTO learnings (learning_id, agent_id, category, content, created_at) "
            "VALUES (1, 'gemini', 'architecture', 'Use strict Swift 6 concurrency gates', ?)",
            (now - 100,),
        )
        conn.execute(
            "INSERT INTO learnings (learning_id, agent_id, category, content, created_at) "
            "VALUES (2, 'codex', 'fix', 'Keep FTS5 triggers synced on rebuild', ?)",
            (now - 90,),
        )
        conn.execute(
            "INSERT INTO learnings (learning_id, agent_id, category, content, created_at) "
            "VALUES (3, 'main', 'general', 'Global operator preference note', ?)",
            (now - 80,),
        )
        conn.execute(
            """INSERT INTO candidate_packets
               (principal, workspace_id, category, content, status, proposed_at, resolved_at)
               VALUES ('gemini', 'workspace-praxis', 'architecture',
                       'Use strict Swift 6 concurrency gates', 'accepted', ?, ?)""",
            (now - 105, now - 100),
        )
        conn.execute(
            """INSERT INTO documents
               (doc_id, path, agent, indexed_at, page_status, privacy_level, workspace_id)
               VALUES (10, '/tmp/fts-sync.md', 'codex', ?, 'accepted', 'safe', 'workspace-minni')""",
            (now - 90,),
        )
        conn.execute(
            "INSERT INTO learning_documents (learning_id, doc_id, created_at) "
            "VALUES (2, 10, ?)",
            (now - 90,),
        )
        conn.commit()

        # Run migration 025 twice: zero duplicate column error!
        run_migrations(conn)
        run_migrations(conn)

        rows = {
            r["learning_id"]: r["workspace_id"]
            for r in conn.execute(
                "SELECT learning_id, workspace_id FROM learnings ORDER BY learning_id"
            ).fetchall()
        }
        assert rows[1] == "workspace-praxis"
        assert rows[2] == "workspace-minni"
        assert rows[3] == "default"
    finally:
        conn.close()

    # Reopening MinniDB over the migrated DB must also succeed cleanly
    mdb = MinniDB(cfg)
    mdb._get_conn()
    mdb.close()


# ---------------------------------------------------------------------------
# 2. workspace_id Persistence Across Write / Promotion / Index Paths (BUG-08)
# ---------------------------------------------------------------------------


def test_workspace_id_persisted_across_governance_afm_writeback_graph_and_indexer(
    tmp_path: Path, monkeypatch
):
    cfg = _make_config(tmp_path)
    principals_dir = tmp_path / "principals"
    principals_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    principals_dir.chmod(0o700)
    # Register both operator `main` and autonomous `auto-bot` (`auto_accept_own: True`)
    main_file = principals_dir / "main.json"
    main_file.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(cfg.vault_path)],
            }
        ),
        encoding="utf-8",
    )
    main_file.chmod(0o600)
    auto_bot_file = principals_dir / "auto-bot.json"
    auto_bot_file.write_text(
        json.dumps(
            {
                "agent_id": "auto-bot",
                "capabilities": ["learn", "recall", "stage_candidate"],
                "allowed_vault_roots": [str(cfg.vault_path)],
                "auto_accept_own": True,
            }
        ),
        encoding="utf-8",
    )
    auto_bot_file.chmod(0o600)
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    monkeypatch.setattr("minni.principal.PRINCIPALS_DIR", principals_dir)
    monkeypatch.setattr(
        WriteBackMemory,
        "model",
        property(
            lambda _self: SimpleNamespace(
                encode=lambda _text, show_progress_bar=False: np.zeros(8, dtype=np.float32)
            )
        ),
    )

    old_db_path = cfg_mod.DEFAULT_CONFIG.db_path
    old_vault_path = cfg_mod.DEFAULT_CONFIG.vault_path
    old_faiss_path = cfg_mod.DEFAULT_CONFIG.faiss_index_path
    old_wb_path = cfg_mod.DEFAULT_CONFIG.writeback_path
    old_wb = minnid._writeback
    old_ret = minnid._retrieval

    cfg_mod.DEFAULT_CONFIG.db_path = cfg.db_path
    cfg_mod.DEFAULT_CONFIG.vault_path = cfg.vault_path
    cfg_mod.DEFAULT_CONFIG.faiss_index_path = cfg.faiss_index_path
    cfg_mod.DEFAULT_CONFIG.writeback_path = cfg.writeback_path
    minnid._writeback = None
    minnid._retrieval = None

    db = MinniDB.shared(cfg_mod.DEFAULT_CONFIG)
    wb = WriteBackMemory(db, cfg_mod.DEFAULT_CONFIG)
    minnid._writeback = wb

    engine = RetrievalEngine(db, cfg_mod.DEFAULT_CONFIG)
    engine._embed_texts = lambda _texts: np.zeros((len(_texts), 384), dtype=np.float32)  # type: ignore[attr-defined]
    minnid._retrieval = engine

    indexed_durable: list[tuple[str, str]] = []
    orig_index_durable = minnid._index_durable_learning

    def _spy_index_durable(
        agent_id: str,
        content: str,
        key: str,
        db=minnid._UNSET,
        workspace_id: str = "",
    ) -> bool:
        indexed_durable.append((str(key), workspace_id))
        engine.index_durable_document(
            content=content,
            path=f"{cfg.vault_path}/_durable/{key.replace(':', '_')}.md",
            agent=agent_id,
            page_type="learning",
            workspace_id=workspace_id,
        )
        return True

    monkeypatch.setattr(minnid, "_index_durable_learning", _spy_index_durable)

    try:
        # (a) stage_candidate with operator-configured auto_accept_own=True
        resp_auto = minnid._stage_candidate(
            {
                "agent_id": "auto-bot",
                "content": "Decision: Auto-accepted alpha storage invariant for Wave 2",
                "category": "architecture",
                "privacy_level": "safe",
                "workspace_id": "workspace-alpha",
            },
            1,
        )
        assert resp_auto["result"]["status"] == "accepted", resp_auto
        lid_auto = int(resp_auto["result"]["learning_id"])

        # (b) stage_candidate (proposed) -> resolve_candidate (accept)
        resp_prop = minnid._stage_candidate(
            {
                "content": "Proposed beta invariant promoted through resolve_candidate",
                "category": "decision",
                "workspace_id": "workspace-beta",
            },
            2,
        )
        assert resp_prop["result"]["status"] == "proposed", resp_prop
        cid_beta = int(resp_prop["result"]["candidate_id"])

        resp_res = minnid._resolve_candidate(
            {
                "candidate_id": cid_beta,
                "decision": "accept",
                "reason": "wave2 test",
            },
            3,
        )
        assert resp_res["result"]["new_status"] == "accepted", resp_res
        lid_resolved = int(resp_res["result"]["learning_id"])

        # (c) handle_learn with force=True + handle_resolve_contradiction inheriting workspace_id
        resp_force = minnid._handle_learn(
            {
                "content": "Force-stored gamma operational rule for Wave 2",
                "category": "fix",
                "workspace_id": "workspace-gamma",
                "force": True,
            },
            4,
        )
        assert "result" in resp_force, resp_force
        lid_force = int(resp_force["result"]["learning_id"])

        resp_contra = minnid._handle_resolve_contradiction(
            {
                "supersede_ids": [lid_force],
                "new_content": "Updated gamma operational rule superseding prior rule",
                "category": "fix",
            },
            41,
        )
        assert "result" in resp_contra, resp_contra
        lid_contra = int(resp_contra["result"]["new_learning_id"])

        # (d) afm._promote_candidate_durable preserves both workspace_id AND category
        resp_afm_cand = minnid._stage_candidate(
            {
                "content": "AFM durable promotion preserves workspace_id and category",
                "category": "architecture",
                "workspace_id": "workspace-afm",
                "privacy_level": "safe",
            },
            5,
        )
        cid_afm = int(resp_afm_cand["result"]["candidate_id"])
        with db.transaction() as cur:
            cur.execute(
                "UPDATE candidate_packets SET privacy_level = 'safe' WHERE candidate_id = ?",
                (cid_afm,),
            )
        lid_afm = minnid._promote_candidate_durable(cid_afm, reason="afm test")
        assert lid_afm is not None
        lid_afm = int(lid_afm)

        # (e) WriteBackMemory.store_learning + YAML frontmatter + recall_learnings
        lid_wb = wb.store_learning(
            agent_id="main",
            content="Direct writeback delta learning with frontmatter",
            category="pattern",
            workspace_id="workspace-delta",
        )
        recalled_wb = wb.recall_learnings("Direct writeback delta learning", limit=5)
        matched_wb = [r for r in recalled_wb if r["learning_id"] == lid_wb]
        assert matched_wb and matched_wb[0]["workspace_id"] == "workspace-delta"

        md_files = list(Path(cfg.writeback_path).rglob("*.md"))
        assert any("workspace_id: workspace-delta" in f.read_text("utf-8") for f in md_files)
        assert any(
            "workspace_id: workspace-afm" in f.read_text("utf-8")
            and "category: architecture" in f.read_text("utf-8")
            for f in md_files
        )

        # (f) graph_coordinator._insert_learning
        with db.transaction() as cur:
            lid_graph = _insert_learning(
                cur,
                agent_id="main",
                category="architecture",
                content="Graph coordinator workspace-epsilon learning",
                embedding=None,
                now=time.time(),
                fields=LearningFields(confidence=0.95),
                workspace_id="workspace-epsilon",
            )

        with db.cursor() as cur:
            stored_ws = {
                r["learning_id"]: r["workspace_id"]
                for r in cur.execute(
                    "SELECT learning_id, workspace_id FROM learnings"
                ).fetchall()
            }

        assert stored_ws[lid_auto] == "workspace-alpha"
        assert stored_ws[lid_resolved] == "workspace-beta"
        assert stored_ws[lid_force] == "workspace-gamma"
        assert stored_ws[lid_contra] == "workspace-gamma"
        assert stored_ws[lid_afm] == "workspace-afm"
        assert stored_ws[lid_wb] == "workspace-delta"
        assert stored_ws[lid_graph] == "workspace-epsilon"
        assert (f"learning:{lid_auto}", "workspace-alpha") in indexed_durable
        assert (f"learning:{lid_resolved}", "workspace-beta") in indexed_durable
        assert (f"learning:{lid_afm}", "workspace-afm") in indexed_durable

        # (g) VaultIndexer extracts workspace_id from YAML frontmatter onto documents
        note_path = Path(cfg.vault_path) / "wiki" / "decisions" / "frontmatter-ws.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\ntitle: Frontmatter Scoped Note\nstatus: accepted\nprivacy_level: safe\nworkspace_id: workspace-zeta\n---\n\nBody text for zeta.\n",
            encoding="utf-8",
        )
        indexer = VaultIndexer(db, cfg_mod.DEFAULT_CONFIG, embed=False)
        indexer.index_vault()

        with db.cursor() as cur:
            doc_row = cur.execute(
                "SELECT doc_id, workspace_id FROM documents WHERE path = ?",
                (str(note_path),),
            ).fetchone()
        assert doc_row is not None
        assert doc_row["workspace_id"] == "workspace-zeta"

        # (h) RetrievalEngine.index_durable_document & _hydrate_durable_learning_docs
        dur_path = f"{cfg.vault_path}/_durable/learning_{lid_auto}.md"
        with db.transaction() as cur:
            dur_doc = cur.execute(
                "SELECT doc_id, workspace_id FROM documents WHERE path = ?",
                (dur_path,),
            ).fetchone()
            assert dur_doc is not None
            assert dur_doc["workspace_id"] == "workspace-alpha"
            cur.execute(
                "INSERT OR IGNORE INTO learning_documents (learning_id, doc_id, created_at) "
                "VALUES (?, ?, ?)",
                (lid_auto, int(dur_doc["doc_id"]), time.time()),
            )

        # Even if a legacy _durable/ row had empty workspace_id, _hydrate_durable_learning_docs
        # propagates workspace_id from the backing `learnings` row!
        sample_rows = [
            {
                "doc_id": int(dur_doc["doc_id"]),
                "path": dur_path,
                "source": dur_path,
                "workspace_id": "",
            }
        ]
        hydrated = engine._hydrate_durable_learning_docs(sample_rows, sample_rows)
        assert hydrated[0]["workspace_id"] == "workspace-alpha"
    finally:
        monkeypatch.setattr(minnid, "_index_durable_learning", orig_index_durable)
        minnid._writeback = old_wb
        minnid._retrieval = old_ret
        cfg_mod.DEFAULT_CONFIG.db_path = old_db_path
        cfg_mod.DEFAULT_CONFIG.vault_path = old_vault_path
        cfg_mod.DEFAULT_CONFIG.faiss_index_path = old_faiss_path
        cfg_mod.DEFAULT_CONFIG.writeback_path = old_wb_path


# ---------------------------------------------------------------------------
# 3. Confidence Floor, HyDE Floor, Repo-First Ranking & Non-Blackout
#    (BUG-01, BUG-07, BUG-09a)
# ---------------------------------------------------------------------------


def test_post_rerank_and_hyde_confidence_floors_drop_negative_logit_noise(
    tmp_path: Path, monkeypatch
):
    cfg = _make_config(tmp_path)
    db = MinniDB(cfg)
    try:
        engine = RetrievalEngine(db, cfg)
        candidates = [
            # BUG-01 exact shape: single-word FTS overlap (`fts_rank` is NOT None)
            # with strongly negative cross-encoder logit (-9.5 / -16.65) MUST be dropped!
            {
                "doc_id": 1,
                "fts_rank": 1,
                "raw_rerank_score": -9.5,
                "similarity": 0.48,
                "workspace_id": "workspace-alpha",
            },
            {
                "doc_id": 2,
                "fts_rank": 2,
                "raw_rerank_score": -16.65,
                "similarity": 0.52,
                "workspace_id": "workspace-alpha",
            },
            # Semantic-only low-logit + low-similarity noise MUST be dropped
            {
                "doc_id": 3,
                "fts_rank": None,
                "raw_rerank_score": -2.8,
                "similarity": 0.28,
                "workspace_id": "workspace-alpha",
            },
            # Borderline valid FTS + acceptable rerank score (-2.2) survives
            {
                "doc_id": 4,
                "fts_rank": 3,
                "raw_rerank_score": -2.2,
                "similarity": 0.35,
                "workspace_id": "workspace-alpha",
            },
            # Tier 3 explicit OTHER repo (`workspace-beta` != `workspace-alpha`)
            # with negative logit (-0.7) is dropped, while >= 0.0 (+1.4) survives!
            {
                "doc_id": 5,
                "fts_rank": 1,
                "raw_rerank_score": -0.7,
                "similarity": 0.62,
                "workspace_id": "workspace-beta",
            },
            {
                "doc_id": 6,
                "fts_rank": 1,
                "raw_rerank_score": 1.4,
                "similarity": 0.78,
                "workspace_id": "workspace-beta",
            },
        ]

        kept = engine._apply_post_rerank_confidence_floor(
            candidates,
            fts_results=[],
            limit=10,
            query_workspace="workspace-alpha",
        )
        assert [r["doc_id"] for r in kept] == [4, 6]

        # Verify HyDE (`_run_hyde_enrichment`) also applies `_apply_post_rerank_confidence_floor`
        # before RRF merging so negative-logit HyDE candidates never sneak past!
        cfg.hyde_enabled = True
        monkeypatch.setattr(
            RetrievalEngine,
            "reranker",
            property(lambda _self: True),
        )
        monkeypatch.setattr(
            "minni.hyde.generate_hypothetical_answer",
            lambda _q, config=None: "hypothetical answer document",
        )
        engine._semantic_search = lambda _q, _limit, **_kw: [  # type: ignore[method-assign]
            {
                "doc_id": 101,
                "fts_rank": 1,
                "similarity": 0.22,
                "score": 0.22,
                "workspace_id": "workspace-alpha",
            },
            {
                "doc_id": 102,
                "fts_rank": 1,
                "similarity": 0.75,
                "score": 0.75,
                "workspace_id": "workspace-alpha",
            },
        ]
        engine._fts_search = lambda _q, _limit, **_kw: []  # type: ignore[method-assign]
        engine._rrf_merge = lambda fts, sem, k=60, **_kw: list(fts) + list(sem)  # type: ignore[method-assign]

        def _fake_rerank(_q: str, items: list[dict], **_kw) -> list[dict]:
            out = []
            for item in items:
                c = dict(item)
                c["raw_rerank_score"] = -9.5 if c["doc_id"] == 101 else 2.1
                c["rerank_score"] = c["raw_rerank_score"]
                out.append(c)
            return out

        engine._rerank = _fake_rerank  # type: ignore[method-assign]
        merged = engine._run_hyde_enrichment(
            query="migration 025 schema",
            merged=[
                {
                    "doc_id": 100,
                    "raw_rerank_score": -3.5,
                    "rerank_score": -3.5,
                    "workspace_id": "workspace-alpha",
                }
            ],
            limit=5,
            rerank_k=5,
            skip_list=[],
            document_agent_filter=None,
            deadline_monotonic=None,
            trace={"hyde": {}},
            eligible_fn=lambda rows: list(rows),
            collect_eligible_fn=lambda fn, k, **_kw: fn(k),
            query_workspace="workspace-alpha",
        )
        merged_ids = [r["doc_id"] for r in merged]
        assert 101 not in merged_ids
        assert 102 in merged_ids
    finally:
        db.close()


def test_repo_first_ranking_and_search_rpc_non_blackout(tmp_path: Path, monkeypatch):
    """Prove end-to-end on minnid._handle_search + VaultIndexer + search_learnings:
    - Querying with `workspace_id='workspace-alpha'` leaves `principal_for_documents`
      unpinned so `global` and `default` docs are NOT blacked out by `can_read_document`.
    - `workspace-alpha` hits outrank `global`/`default` hits with identical match text.
    - Weak `workspace-beta` cross-repo hits are dropped while strong multi-token cross-repo
      hits still surface behind current-repo hits.
    - `result['workspace_id']` echoes `'workspace-alpha'`.
    """
    assert normalize_workspace_scope("/Users/example/Projects/Minni") == "workspace-minni"
    assert is_concrete_project_workspace("workspace-alpha") is True
    assert is_concrete_project_workspace("default") is False
    assert is_concrete_project_workspace("global") is False
    assert is_concrete_project_workspace("workspace-unknown") is False

    peer = EffectivePrincipal(
        agent_id="codex",
        capabilities=["recall"],
        allowed_vault_roots=[str(tmp_path / "vault")],
    )
    sample_doc = {
        "path": str(tmp_path / "vault" / "wiki" / "note.md"),
        "agent": "codex",
        "privacy_level": "safe",
        "page_status": "accepted",
        "workspace_id": "workspace-alpha",
    }
    global_doc = {**sample_doc, "workspace_id": "global"}
    beta_doc = {**sample_doc, "workspace_id": "workspace-beta"}

    # Cross-ws call wildcard ('*') allows reading workspace-tagged docs without blackout,
    # while direct 'default' gate calls stay strict against stamped foreign workspaces.
    assert can_read_document(peer, "*", sample_doc) is True
    assert can_read_document(peer, "default", sample_doc) is False
    # Pinned call workspace ('workspace-alpha') allows global docs and denies mismatched foreign ws
    assert can_read_document(peer, "workspace-alpha", global_doc) is True
    assert can_read_document(peer, "workspace-alpha", beta_doc) is False

    cfg = _make_config(tmp_path)
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))

    old_db_path = cfg_mod.DEFAULT_CONFIG.db_path
    old_vault_path = cfg_mod.DEFAULT_CONFIG.vault_path
    old_faiss_path = cfg_mod.DEFAULT_CONFIG.faiss_index_path
    old_wb_path = cfg_mod.DEFAULT_CONFIG.writeback_path
    old_wb = minnid._writeback
    old_ret = minnid._retrieval

    cfg_mod.DEFAULT_CONFIG.db_path = cfg.db_path
    cfg_mod.DEFAULT_CONFIG.vault_path = cfg.vault_path
    cfg_mod.DEFAULT_CONFIG.faiss_index_path = cfg.faiss_index_path
    cfg_mod.DEFAULT_CONFIG.writeback_path = cfg.writeback_path
    minnid._writeback = None
    minnid._retrieval = None

    db = MinniDB.shared(cfg_mod.DEFAULT_CONFIG)
    wb = WriteBackMemory(db, cfg_mod.DEFAULT_CONFIG)
    engine = RetrievalEngine(db, cfg_mod.DEFAULT_CONFIG)
    minnid._writeback = wb
    minnid._retrieval = engine

    try:
        indexer = VaultIndexer(db, cfg_mod.DEFAULT_CONFIG, embed=False)
        wiki_dir = Path(cfg.vault_path) / "wiki" / "decisions"
        wiki_dir.mkdir(parents=True, exist_ok=True)

        notes = [
            (
                "global-rule.md",
                "global",
                "Migration idempotency verification gate protocol for sqlite columns.",
            ),
            (
                "alpha-rule.md",
                "workspace-alpha",
                "Migration idempotency verification gate protocol for sqlite columns.",
            ),
            (
                "beta-weak.md",
                "workspace-beta",
                "Unrelated gardening notes mentioning sqlite once in passing.",
            ),
            (
                "beta-strong.md",
                "workspace-beta",
                "Migration idempotency verification gate protocol for sqlite columns across beta.",
            ),
        ]
        for fname, ws_id, body in notes:
            f = wiki_dir / fname
            f.write_text(
                f"---\ntitle: {fname}\nstatus: accepted\nprivacy_level: safe\nworkspace_id: {ws_id}\n---\n\n{body}\n",
                encoding="utf-8",
            )
        indexer.index_vault()

        now = time.time()
        with db.transaction() as cur:
            cur.execute(
                """INSERT INTO learnings
                   (learning_id, agent_id, category, content, confidence, created_at, status, workspace_id)
                   VALUES
                   (11, 'main', 'architecture', 'Migration idempotency verification gate rule', 0.9, ?, 'active', 'default'),
                   (12, 'main', 'architecture', 'Migration idempotency verification gate rule', 0.9, ?, 'active', 'workspace-alpha'),
                   (13, 'main', 'general', 'Unrelated beta note mentioning sqlite once', 0.4, ?, 'active', 'workspace-beta')""",
                (now, now, now),
            )

        learn_hits = engine.search_learnings(
            "migration idempotency verification gate sqlite",
            limit=5,
            query_workspace="workspace-alpha",
        )
        learn_ids = [h["learning_id"] for h in learn_hits]
        assert learn_ids[0] == 12
        assert 11 in learn_ids
        assert 13 not in learn_ids

        rpc_resp = minnid._handle_search(
            {
                "query": "migration idempotency verification gate protocol sqlite columns",
                "mode": "fast",
                "workspace_id": "workspace-alpha",
                "limit": 5,
            },
            1,
        )
        assert "result" in rpc_resp, rpc_resp
        payload = rpc_resp["result"]
        assert payload["workspace_id"] == "workspace-alpha"
        hit_blobs = [
            f"{r.get('wikilink', '')} {r.get('source', '')} {r.get('text', '')}"
            for r in payload["results"]
        ]
        assert hit_blobs, f"Expected document hits, got {payload}"
        assert "alpha-rule" in hit_blobs[0]
        assert any("global-rule" in b for b in hit_blobs)
        assert not any("beta-weak" in b for b in hit_blobs)
        assert any("beta-strong" in b for b in hit_blobs)
    finally:
        minnid._writeback = old_wb
        minnid._retrieval = old_ret
        cfg_mod.DEFAULT_CONFIG.db_path = old_db_path
        cfg_mod.DEFAULT_CONFIG.vault_path = old_vault_path
        cfg_mod.DEFAULT_CONFIG.faiss_index_path = old_faiss_path
        cfg_mod.DEFAULT_CONFIG.writeback_path = old_wb_path


# ---------------------------------------------------------------------------
# 4. Blank Post-Frontmatter <EVIDENCE> Stripping (BUG-09b)
# ---------------------------------------------------------------------------


def test_frontmatter_only_notes_never_emit_blank_evidence_blocks(tmp_path: Path):
    cfg = _make_config(tmp_path)
    db = MinniDB(cfg)
    try:
        indexer = VaultIndexer(db, cfg, embed=False)
        wiki_dir = Path(cfg.vault_path) / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)

        blank_file = wiki_dir / "empty-note.md"
        real_file = wiki_dir / "real-note.md"
        blank_file.write_text(
            "---\ntitle: Shared Token Header\nstatus: accepted\nprivacy_level: safe\nworkspace_id: workspace-alpha\n---\n   \n\n",
            encoding="utf-8",
        )
        real_file.write_text(
            "---\ntitle: Shared Token Header\nstatus: accepted\nprivacy_level: safe\nworkspace_id: workspace-alpha\n---\n\nReal body content for shared token header.\n",
            encoding="utf-8",
        )
        indexer.index_vault()

        with db.cursor() as cur:
            blank_row = cur.execute(
                "SELECT doc_id FROM documents WHERE path = ?", (str(blank_file),)
            ).fetchone()

        engine = RetrievalEngine(db, cfg)
        for depth in ("headline", "snippet", "chunk", "document"):
            res = engine.retrieve(
                "Shared Token Header",
                limit=5,
                mode="fast",
                depth=depth,
                query_workspace="workspace-alpha",
            )
            assert len(res) == 1, f"depth={depth} should drop empty-note.md, got {res}"
            assert "empty-note" not in str(res[0].get("source") or res[0].get("wikilink") or "")
            text_val = res[0].get("text")
            if isinstance(text_val, str) and "<EVIDENCE" in text_val:
                inner = text_val.split(">", 1)[1].rsplit("</EVIDENCE>", 1)[0]
                assert inner.strip() != "", f"Blank <EVIDENCE> emitted at depth={depth}: {text_val!r}"

        if blank_row is not None:
            expanded = engine.expand_result(
                int(blank_row["doc_id"]), depth="document", id_kind="doc"
            )
            assert expanded is None
    finally:
        db.close()

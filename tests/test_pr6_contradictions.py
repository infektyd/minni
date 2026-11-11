"""
PR-6 Tests — pytest suite.

Covers:
  6.1  Migration 005: columns exist as nullable after migration
  6.2  detect_contradictions: high-cosine matches found, low-cosine ignored
  6.3  detect_contradictions: graceful failure when model unavailable
  6.4  _handle_learn (force=False): blocks on contradiction
  6.5  _handle_learn (force=True): writes through regardless of contradictions
  6.6  _handle_learn (contradicts_id): writes through, skips detection
  6.7  _handle_learn backward compatibility: existing calls without new fields still work
  6.8  _handle_resolve_contradiction: atomic write + supersede
  6.9  _handle_resolve_contradiction: rollback on mid-transaction error
  6.10 config.contradiction_threshold: readable, default 0.85
  6.11 _extract_assertion: first-sentence extraction
  6.12 Full workflow: seed → detect → resolve (integration)
"""

import json
import os
import sqlite3
import sys
import time

import pytest
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

# Hermetic principal setup for dispatch tests (operator_context for wire "main").
@pytest.fixture(autouse=True)
def setup_hermetic_principals(tmp_path, monkeypatch):
    import minni.principal as principal
    import minni.minnid as minnid

    pdir = tmp_path / "principals"
    pdir.mkdir(exist_ok=True)
    original_resolve = principal.resolve_effective_principal

    def _patched_resolve(
        *,
        supplied_agent_id=None,
        transport="uds",
        principals_dir=None,
        operator_context=False,
    ):
        target_dir = principals_dir or pdir
        if supplied_agent_id is None:
            f = target_dir / "local.json"
            f.write_text(
                json.dumps(
                    {
                        "agent_id": "main",
                        "workspace_id": "default",
                        "capabilities": ["*"],
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(f, 0o600)
            return original_resolve(
                supplied_agent_id=None,
                transport=transport,
                principals_dir=target_dir,
            )

        target_agent = str(supplied_agent_id).strip()
        if not target_agent:
            return original_resolve(
                supplied_agent_id=None,
                transport=transport,
                principals_dir=target_dir,
            )

        f = target_dir / f"{target_agent}.json"
        f.write_text(
            json.dumps(
                {
                    "agent_id": target_agent,
                    "workspace_id": "default",
                    "capabilities": ["*"],
                }
            ),
            encoding="utf-8",
        )
        os.chmod(f, 0o600)

        op_ctx = operator_context or target_agent in principal.OPERATOR_RESERVED_AGENT_IDS
        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=target_dir,
            operator_context=op_ctx,
        )

    monkeypatch.setattr(principal, "resolve_effective_principal", _patched_resolve)
    monkeypatch.setattr(minnid, "resolve_effective_principal", _patched_resolve)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    """Return (MinniDB, MinniConfig) backed by a temporary SQLite file."""
    import minni.db as db_mod
    from minni.config import MinniConfig

    db_path = str(tmp_path / "test.db")
    cfg = MinniConfig(db_path=db_path)

    # Reset the module-level guard so migrations run freshly for each test DB.
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.MinniDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    return db_obj, cfg


def _make_writeback(tmp_path):
    """Return a WriteBackMemory instance against a fresh test DB."""
    from minni.writeback import WriteBackMemory
    db_obj, cfg = _make_db(tmp_path)
    return WriteBackMemory(db_obj, cfg), db_obj, cfg


def _fixed_embedding(text: str, dim: int = 384) -> np.ndarray:
    """Return a deterministic unit-norm vector for testing without real models."""
    rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _nearly_identical_embedding(base: np.ndarray, noise: float = 0.01) -> np.ndarray:
    """Return an embedding very close to base (cosine > 0.99)."""
    v = base + np.random.default_rng(42).standard_normal(len(base)).astype(np.float32) * noise
    return v / np.linalg.norm(v)


def _orthogonal_embedding(base: np.ndarray) -> np.ndarray:
    """Return an embedding orthogonal to base (cosine ≈ 0)."""
    dim = len(base)
    # Gram–Schmidt: pick a random vector, subtract projection onto base
    rng = np.random.default_rng(99)
    other = rng.standard_normal(dim).astype(np.float32)
    other -= np.dot(other, base) * base
    return other / np.linalg.norm(other)


# ---------------------------------------------------------------------------
# 6.1  Migration 005: columns added to learnings
# ---------------------------------------------------------------------------

class TestMigration005:

    def test_migration_applies_and_columns_exist(self, tmp_path):
        """Migration 005 adds assertion, applies_when, evidence_doc_ids,
        contradicts_id, and status columns to learnings."""
        from minni.migrations import run_migrations

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)

        # Minimal schema for migration 005 to run against
        conn.execute("""
            CREATE TABLE IF NOT EXISTS learnings (
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
                superseded_by INTEGER
            )
        """)
        conn.commit()

        run_migrations(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(learnings)").fetchall()}
        conn.close()

        assert "assertion" in cols, "assertion column missing"
        assert "applies_when" in cols, "applies_when column missing"
        assert "evidence_doc_ids" in cols, "evidence_doc_ids column missing"
        assert "contradicts_id" in cols, "contradicts_id column missing"
        assert "status" in cols, "status column missing"

    def test_new_columns_are_nullable(self, tmp_path):
        """The new columns accept NULL (no NOT NULL constraint)."""
        db_obj, cfg = _make_db(tmp_path)

        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, created_at)
                VALUES ('test', 'general', 'some content', ?)
            """, (now,))
            lid = c.lastrowid

            row = c.execute(
                "SELECT assertion, applies_when, evidence_doc_ids, contradicts_id, status "
                "FROM learnings WHERE learning_id = ?", (lid,)
            ).fetchone()

        assert row["assertion"] is None
        assert row["applies_when"] is None
        assert row["evidence_doc_ids"] is None
        assert row["contradicts_id"] is None
        # status has a default
        # (may be None if column was added via migration with no default override)

    def test_migration_idempotent(self, tmp_path):
        """Running migration 005 twice does not raise (duplicate column ignored)."""
        from minni.migrations import run_migrations

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS learnings (
                learning_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()

        run_migrations(conn)
        # Second run must not raise
        run_migrations(conn)
        conn.close()

    def test_schema_migrations_table_records_005(self, tmp_path):
        """schema_migrations tracks migration 005 by version number."""
        db_obj, _ = _make_db(tmp_path)
        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT version FROM schema_migrations WHERE version = 5"
            ).fetchone()
        assert row is not None, "Migration 005 not recorded in schema_migrations"


# ---------------------------------------------------------------------------
# 6.2 & 6.3  Typed Graph Contradiction & Supersession Shortlist (Wave 3)
# ---------------------------------------------------------------------------

class TestTypedGraphContradictions:

    def _seed_canonical_learning(
        self, db_obj, cfg, agent_id, content, *, category="fact", status="active", superseded_by=None
    ):
        from minni.graph_commit import ensure_canonical_learning_node

        now = time.time()
        with db_obj.transaction() as c:
            c.execute(
                """
                INSERT INTO learnings
                (agent_id, category, content, confidence, created_at, status, superseded_by)
                VALUES (?, ?, ?, 1.0, ?, ?, ?)
                """,
                (agent_id, category, content, now, status, superseded_by),
            )
            lid = c.lastrowid
            doc_id = ensure_canonical_learning_node(
                c,
                learning_id=lid,
                agent_id=agent_id,
                content=content,
                vault_path=cfg.vault_path,
                created_at=now,
                workspace_id="default",
            )
            if status in ("superseded", "rejected", "expired") or superseded_by is not None:
                c.execute(
                    "UPDATE documents SET page_status = ? WHERE doc_id = ?",
                    ("superseded" if superseded_by is not None else status, doc_id),
                )
        return lid, doc_id

    def test_opposing_polarity_classified_as_contradicts(self, tmp_path, monkeypatch):
        """Opposing polarity across categories emits a typed `contradicts` edge."""
        from minni.minnid_runtime.graph_adapters import prepare_governed_promotion
        from minni.principal import EffectivePrincipal

        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")
        db_obj, cfg = _make_db(tmp_path)
        self._seed_canonical_learning(
            db_obj, cfg, "agent1",
            "Always route internal auth service calls through JWT bearer tokens.",
            category="fact",
        )
        principal = EffectivePrincipal(
            agent_id="agent1", workspace_id="default", capabilities=["read", "learn"]
        )
        plan = prepare_governed_promotion(
            db_obj,
            author_agent="agent1",
            workspace_id="default",
            caller_principal=principal,
            content="Never route internal auth service calls through JWT bearer tokens.",
            category="correction",
            vault_path=cfg.vault_path,
        )
        assert plan.fallback_edges is not None
        validated, _info, _snaps = plan.fallback_edges
        assert len(validated) == 1
        _desc, label, _dir, conf = validated[0]
        assert label == "contradicts"
        assert conf >= 0.88

    def test_unrelated_topic_emits_no_edges(self, tmp_path, monkeypatch):
        """Unrelated learnings emit zero typed graph edges."""
        from minni.minnid_runtime.graph_adapters import prepare_governed_promotion
        from minni.principal import EffectivePrincipal

        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")
        db_obj, cfg = _make_db(tmp_path)
        self._seed_canonical_learning(
            db_obj, cfg, "agent1",
            "PostgreSQL connection pools default to 20 workers on macOS.",
            category="fact",
        )
        principal = EffectivePrincipal(
            agent_id="agent1", workspace_id="default", capabilities=["read", "learn"]
        )
        plan = prepare_governed_promotion(
            db_obj,
            author_agent="agent1",
            workspace_id="default",
            caller_principal=principal,
            content="SwiftUI preview rendering requires Xcode 27 headless simulator.",
            category="fact",
            vault_path=cfg.vault_path,
        )
        assert plan.fallback_edges is None

    def test_superseded_learnings_excluded(self, tmp_path, monkeypatch):
        """Learnings with superseded_by IS NOT NULL are excluded from graph shortlist."""
        from minni.minnid_runtime.graph_adapters import prepare_governed_promotion
        from minni.principal import EffectivePrincipal

        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")
        db_obj, cfg = _make_db(tmp_path)
        new_lid, _ = self._seed_canonical_learning(
            db_obj, cfg, "agent1", "Current active replacement rule for deployments.", category="fact"
        )
        self._seed_canonical_learning(
            db_obj, cfg, "agent1",
            "Always route internal auth service calls through JWT bearer tokens.",
            category="fact",
            superseded_by=new_lid,
        )
        principal = EffectivePrincipal(
            agent_id="agent1", workspace_id="default", capabilities=["read", "learn"]
        )
        plan = prepare_governed_promotion(
            db_obj,
            author_agent="agent1",
            workspace_id="default",
            caller_principal=principal,
            content="Never route internal auth service calls through JWT bearer tokens.",
            category="correction",
            vault_path=cfg.vault_path,
        )
        assert plan.fallback_edges is None

    def test_status_superseded_excluded(self, tmp_path, monkeypatch):
        """Learnings with status='superseded' are excluded even if superseded_by IS NULL."""
        from minni.minnid_runtime.graph_adapters import prepare_governed_promotion
        from minni.principal import EffectivePrincipal

        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")
        db_obj, cfg = _make_db(tmp_path)
        self._seed_canonical_learning(
            db_obj, cfg, "agent1",
            "Always route internal auth service calls through JWT bearer tokens.",
            category="fact",
            status="superseded",
        )
        principal = EffectivePrincipal(
            agent_id="agent1", workspace_id="default", capabilities=["read", "learn"]
        )
        plan = prepare_governed_promotion(
            db_obj,
            author_agent="agent1",
            workspace_id="default",
            caller_principal=principal,
            content="Never route internal auth service calls through JWT bearer tokens.",
            category="correction",
            vault_path=cfg.vault_path,
        )
        assert plan.fallback_edges is None


class TestTypedGraphGraceful:

    def test_graceful_when_afm_disabled_and_embedder_none(self, tmp_path, monkeypatch):
        """Typed graph preparation degrades cleanly without blocking when AFM and embedder are disabled."""
        from minni.minnid_runtime.graph_adapters import prepare_governed_promotion
        from minni.principal import EffectivePrincipal

        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")
        db_obj, cfg = _make_db(tmp_path)
        principal = EffectivePrincipal(
            agent_id="agent1", workspace_id="default", capabilities=["read", "learn"]
        )
        plan = prepare_governed_promotion(
            db_obj,
            author_agent="agent1",
            workspace_id="default",
            caller_principal=principal,
            content="Standalone durable rule with no prior graph nodes.",
            category="fact",
            vault_path=cfg.vault_path,
        )
        assert plan.adapter is None
        assert plan.fallback_edges is None


# ---------------------------------------------------------------------------
# 6.4–6.7  _handle_learn (JSON-RPC handler)
# ---------------------------------------------------------------------------

class TestHandleLearn:
    """Tests for _handle_learn via JSON-RPC dispatch."""

    def _dispatch(self, method, params):
        """Invoke the JSON-RPC _dispatch function directly."""
        from minni.minnid import _dispatch_sync as _dispatch
        resp = _dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        })
        return resp

    def _patch_writeback(self, tmp_path, monkeypatch):
        """Make the global _writeback singleton point at a fresh test DB."""
        import minni.minnid as minnid
        from minni.writeback import WriteBackMemory

        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)
        monkeypatch.setattr(minnid, "_writeback", wb)
        return wb, db_obj, cfg

    def test_backward_compat_no_new_fields(self, tmp_path, monkeypatch):
        """Existing learn() calls without force stage proposal packets under G16."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        # No model → no contradiction detection, should just write
        import minni.writeback as wb_mod
        original_prop = wb_mod.WriteBackMemory.model.fget
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("learn", {
                "content": "WebSocket reconnection needs 500ms backoff",
                "agent_id": "main",
                "category": "fix",
            })
        finally:
            wb_mod.WriteBackMemory.model = property(original_prop)

        assert "error" not in resp, f"Unexpected error: {resp}"
        result = resp["result"]
        assert result["status"] == "proposed"
        assert "candidate_id" in result
        assert "learning_id" not in result
        assert result["principal"] == "main"

    def test_learn_content_required(self, tmp_path, monkeypatch):
        """learn() returns error when content is missing."""
        self._patch_writeback(tmp_path, monkeypatch)
        resp = self._dispatch("learn", {"agent_id": "test"})
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_learn_force_false_stages_proposal_without_legacy_cosine_block(self, tmp_path, monkeypatch):
        """Wave 3: learn(force=False) stages a candidate packet directly without
        spawning legacy_unclassified cosine contradiction_log rows."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)
        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")

        base_emb = _fixed_embedding("The auth system uses JWT tokens")
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """
                INSERT INTO learnings (agent_id, category, content, confidence,
                                       embedding, created_at, status)
                VALUES ('test', 'fact', 'The auth system uses JWT tokens', 1.0, ?, ?, 'active')
                """,
                (base_emb.tobytes(), now),
            )

        resp = self._dispatch(
            "learn",
            {
                "content": "The auth system uses session cookies, not JWT",
                "agent_id": "test",
            },
        )
        assert "error" not in resp, f"Unexpected error: {resp}"
        result = resp["result"]
        assert result["status"] == "proposed"
        with db_obj.cursor() as c:
            legacy_rows = c.execute(
                "SELECT COUNT(*) FROM contradiction_log WHERE detection_method = 'cosine'"
            ).fetchone()[0]
        assert legacy_rows == 0

    def test_learn_does_not_leak_cross_agent_contradictions(self, tmp_path, monkeypatch):
        """R5: a learn(force=False) call owned by 'test' never leaks another
        agent's ('agent1') durable learning content."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)
        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")

        base_emb = _fixed_embedding("The auth system uses JWT tokens")
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """
                INSERT INTO learnings (agent_id, category, content, confidence,
                                       embedding, created_at, status)
                VALUES ('agent1', 'fact', 'The auth system uses JWT tokens', 1.0, ?, ?, 'active')
                """,
                (base_emb.tobytes(), now),
            )

        resp = self._dispatch(
            "learn",
            {
                "content": "The auth system uses session cookies, not JWT",
                "agent_id": "test",
            },
        )
        assert "error" not in resp, f"Unexpected error: {resp}"
        result = resp["result"]
        assert result["status"] == "proposed", result
        assert "agent1" not in json.dumps(result)

    def test_learn_force_true_writes_through(self, tmp_path, monkeypatch):
        """learn(force=True) writes even when contradictions exist."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        base_emb = _fixed_embedding("JWT auth system")
        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence,
                                       embedding, created_at, status)
                VALUES ('agent1', 'fact', 'JWT auth system', 1.0, ?, ?, 'active')
            """, (base_emb.tobytes(), now))

        close_emb = _nearly_identical_embedding(base_emb, noise=0.001)

        import minni.writeback as wb_mod
        class _FakeModel:
            def encode(self, text, **kwargs):
                return close_emb
        wb_mod.WriteBackMemory.model = property(lambda self: _FakeModel())
        try:
            resp = self._dispatch("learn", {
                "content": "Auth uses session cookies now",
                "agent_id": "test",
                "force": True,
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        assert "error" not in resp, f"Unexpected error: {resp}"
        result = resp["result"]
        assert result["status"] == "ok"
        assert "learning_id" in result

    def test_learn_with_contradicts_id_writes_through(self, tmp_path, monkeypatch):
        """learn(contradicts_id=X) bypasses detection but still stages unless force=true."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        # Insert a real learning to reference as the contradicts_id
        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                VALUES ('agent1', 'fact', 'original learning', 1.0, ?, 'active')
            """, (now,))
            old_id = c.lastrowid

        # No model needed — detection is bypassed when contradicts_id is provided
        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("learn", {
                "content": "Auth now uses session cookies",
                "agent_id": "test",
                "contradicts_id": old_id,  # explicit contradiction bypasses detection
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        assert "error" not in resp, f"Unexpected error: {resp}"
        result = resp["result"]
        assert result["status"] == "proposed"
        assert "candidate_id" in result

    def test_learn_stores_assertion_field(self, tmp_path, monkeypatch):
        """learn() stores the assertion field in the DB when provided."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("learn", {
                "content": "Auth uses JWT. More context follows.",
                "assertion": "Auth system: JWT",
                "agent_id": "test",
                "force": True,
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        lid = resp["result"]["learning_id"]
        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT assertion FROM learnings WHERE learning_id = ?", (lid,)
            ).fetchone()
        assert row["assertion"] == "Auth system: JWT"

    def test_learn_no_contradiction_when_no_model(self, tmp_path, monkeypatch):
        """With no model, detection is skipped and the default path stages a proposal."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("learn", {
                "content": "Some new fact",
                "agent_id": "test",
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        result = resp["result"]
        assert result["status"] == "proposed"
        assert "candidate_id" in result


# ---------------------------------------------------------------------------
# 6.8–6.9  _handle_resolve_contradiction
# ---------------------------------------------------------------------------

class TestHandleResolveContradiction:

    def _seed_learning(self, db_obj, content, agent_id="test"):
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence,
                                       created_at, status)
                VALUES (?, 'fact', ?, 1.0, ?, 'active')
            """, (agent_id, content, time.time()))
            return c.lastrowid

    def _dispatch(self, method, params):
        from minni.minnid import _dispatch_sync as _dispatch
        return _dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        })

    def _patch_writeback(self, tmp_path, monkeypatch):
        import minni.minnid as minnid
        from minni.writeback import WriteBackMemory
        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)
        monkeypatch.setattr(minnid, "_writeback", wb)
        return wb, db_obj, cfg

    def test_resolve_writes_new_and_supersedes_old(self, tmp_path, monkeypatch):
        """resolve_contradiction writes new learning and marks old ones superseded."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        # Seed two old learnings
        now = time.time()
        old_ids = []
        with db_obj.cursor() as c:
            for i in range(2):
                c.execute("""
                    INSERT INTO learnings (agent_id, category, content, confidence,
                                           created_at, status)
                    VALUES ('test', 'fact', ?, 1.0, ?, 'active')
                """, (f"old learning {i}", now))
                old_ids.append(c.lastrowid)

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("resolve_contradiction", {
                "new_content": "Resolved: auth uses session cookies since April 2026",
                "supersede_ids": old_ids,
                "agent_id": "test",
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        assert "error" not in resp, f"Unexpected error: {resp}"
        result = resp["result"]
        assert result["status"] == "ok"
        assert "new_learning_id" in result
        new_lid = result["new_learning_id"]
        assert result["superseded"] == old_ids

        # Verify DB state
        with db_obj.cursor() as c:
            for oid in old_ids:
                row = c.execute(
                    "SELECT superseded_by, status FROM learnings WHERE learning_id = ?",
                    (oid,)
                ).fetchone()
                assert row["superseded_by"] == new_lid, f"Old learning {oid} not superseded"
                assert row["status"] == "superseded", f"Old learning {oid} status not superseded"

            new_row = c.execute(
                "SELECT content, status FROM learnings WHERE learning_id = ?",
                (new_lid,)
            ).fetchone()
            assert "session cookies" in new_row["content"]
            assert new_row["status"] == "active"

            events = c.execute(
                "SELECT superseded_learning_id, new_learning_id, originating_agent FROM contradiction_events ORDER BY event_id"
            ).fetchall()
            assert [(row["superseded_learning_id"], row["new_learning_id"], row["originating_agent"]) for row in events] == [
                (old_ids[0], new_lid, "test"),
                (old_ids[1], new_lid, "test"),
            ]

    def test_resolve_supersede_purges_old_synthetic_doc(self, tmp_path, monkeypatch):
        """M4: resolve_contradiction's supersede must purge the OLD learning's
        synthetic durable doc (documents/vault_fts/chunk_embeddings) via the
        same content-hashed path _index_durable_learning would have indexed it
        under -- otherwise a superseded learning's stale content keeps
        surfacing in semantic/lexical doc search even though the learnings
        table correctly marks it superseded."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        import minni.minnid as minnid
        monkeypatch.setattr(minnid, "_writeback", wb)
        monkeypatch.setattr(minnid, "_retrieval", None, raising=False)
        import minni.config as cfg_mod
        monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path, raising=False)
        monkeypatch.setattr(
            cfg_mod.DEFAULT_CONFIG, "vault_path", cfg.vault_path, raising=False
        )

        old_content = "Old superseded fact about the auth flow before April 2026"
        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence,
                                       created_at, status)
                VALUES ('test', 'fact', ?, 1.0, ?, 'active')
            """, (old_content, now))
            old_id = c.lastrowid

        # Index the OLD learning's synthetic doc exactly like the durable-store
        # path would have, so there is something real for supersede to purge.
        minnid._index_durable_learning(
            "test", old_content, key=f"learning:{old_id}", db=db_obj
        )
        durable_path = minnid._durable_doc_path(
            "test", f"learning:{old_id}", vault_path=cfg.vault_path, content=old_content
        )
        with db_obj.cursor() as c:
            pre = c.execute(
                "SELECT doc_id FROM documents WHERE path = ?", (durable_path,)
            ).fetchone()
        assert pre is not None, "test setup: synthetic doc was not indexed"
        pre_doc_id = pre["doc_id"]

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("resolve_contradiction", {
                "new_content": "New fact: auth uses session cookies since April 2026",
                "supersede_ids": [old_id],
                "agent_id": "test",
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        assert "error" not in resp, f"Unexpected error: {resp}"
        assert resp["result"]["status"] == "ok"

        with db_obj.cursor() as c:
            post_doc = c.execute(
                "SELECT doc_id FROM documents WHERE doc_id = ?", (pre_doc_id,)
            ).fetchone()
            post_chunks = c.execute(
                "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE doc_id = ?",
                (pre_doc_id,),
            ).fetchone()
            post_fts = c.execute(
                "SELECT COUNT(*) AS n FROM vault_fts WHERE doc_id = ?", (pre_doc_id,)
            ).fetchone()

        assert post_doc is None, "superseded learning's synthetic doc row must be purged"
        assert post_chunks["n"] == 0, "superseded learning's chunks must be purged"
        assert post_fts["n"] == 0, "superseded learning's FTS row must be purged"

    def test_subscribe_contradictions_returns_events_for_read_learnings(self, tmp_path, monkeypatch):
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                VALUES ('test', 'fact', 'old learning', 1.0, ?, 'active')
            """, (now,))
            old_id = c.lastrowid
            c.execute("""
                INSERT INTO learning_reads (learning_id, agent_id, read_at, source)
                VALUES (?, 'codex', ?, 'unit-test')
            """, (old_id, now))

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resolved = self._dispatch("resolve_contradiction", {
                "new_content": "new learning",
                "supersede_ids": [old_id],
                "agent_id": "test",
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        assert resolved["result"]["status"] == "ok"
        subscribed = self._dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "since_ts": now - 1,
        })

        assert "error" not in subscribed
        events = subscribed["result"]["events"]
        assert len(events) == 1
        assert events[0]["superseded_learning_id"] == old_id

    def test_subscribe_contradictions_never_leaks_global_event_count(self, tmp_path, monkeypatch):
        """R10: subscribe_contradictions previously returned an UNSCOPED global
        `SELECT COUNT(*) FROM contradiction_events` alongside the caller's own
        (correctly scoped) events -- leaking cross-agent contradiction volume
        to any identified caller. The response's "checked" diagnostics must
        contain ONLY agent-scoped counts, and the agent-scoped count must not
        include another agent's contradiction events.
        """
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        now = time.time()
        with db_obj.cursor() as c:
            # "codex" has one read+superseded learning (its own event).
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                VALUES ('test', 'fact', 'codex-relevant learning', 1.0, ?, 'active')
            """, (now,))
            codex_learning_id = c.lastrowid
            c.execute("""
                INSERT INTO learning_reads (learning_id, agent_id, read_at, source)
                VALUES (?, 'codex', ?, 'unit-test')
            """, (codex_learning_id, now))

            # A DIFFERENT agent ("peer") has its own unrelated read+superseded
            # learning -- codex must never see or be counted against this.
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                VALUES ('test', 'fact', 'peer-relevant learning', 1.0, ?, 'active')
            """, (now,))
            peer_learning_id = c.lastrowid
            c.execute("""
                INSERT INTO learning_reads (learning_id, agent_id, read_at, source)
                VALUES (?, 'peer', ?, 'unit-test')
            """, (peer_learning_id, now))

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            for lid in (codex_learning_id, peer_learning_id):
                resp = self._dispatch("resolve_contradiction", {
                    "new_content": f"resolution for learning {lid}",
                    "supersede_ids": [lid],
                    "agent_id": "test",
                })
                assert resp["result"]["status"] == "ok"
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        subscribed = self._dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "since_ts": now - 1,
        })

        assert "error" not in subscribed
        checked = subscribed["result"]["checked"]

        # No key anywhere in "checked" may carry the unscoped, cross-agent
        # total (2 events exist globally; codex's own scope is 1).
        for key, value in checked.items():
            if isinstance(value, int):
                assert value != 2, (
                    f"checked[{key!r}]={value} looks like the unscoped global "
                    "contradiction_events count leaking cross-agent volume"
                )
        assert checked["contradiction_events_for_agent_reads"] == 1
        assert checked["learning_reads_for_agent"] == 1

        # And the events list itself must never include peer's event.
        superseded_ids = {e["superseded_learning_id"] for e in subscribed["result"]["events"]}
        assert peer_learning_id not in superseded_ids
        assert superseded_ids == {codex_learning_id}

    def test_resolve_new_content_required(self, tmp_path, monkeypatch):
        """resolve_contradiction returns error when new_content is missing."""
        self._patch_writeback(tmp_path, monkeypatch)
        resp = self._dispatch("resolve_contradiction", {
            "supersede_ids": [1],
            "agent_id": "test",
        })
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_resolve_supersede_ids_must_be_list(self, tmp_path, monkeypatch):
        """resolve_contradiction returns error when supersede_ids is not a list."""
        self._patch_writeback(tmp_path, monkeypatch)
        resp = self._dispatch("resolve_contradiction", {
            "new_content": "some content",
            "supersede_ids": 42,
        })
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_resolve_empty_supersede_ids(self, tmp_path, monkeypatch):
        """Empty supersede_ids is rejected (review panel): it would skip the
        per-id ownership loop and insert an unsupervised learning that
        bypasses the staging workflow."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        resp = self._dispatch("resolve_contradiction", {
            "new_content": "standalone learning",
            "supersede_ids": [],
            "agent_id": "test",
        })

        assert "error" in resp
        assert resp["error"]["code"] == -32602
        with db_obj.cursor() as c:
            count = c.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
        assert count == 0, "rejected empty-list resolution must not write a learning"

    def test_resolve_atomicity_new_learning_written(self, tmp_path, monkeypatch):
        """On a successful resolution, the new learning exists in DB."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)
        old_id = self._seed_learning(db_obj, "stale fact to supersede")

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("resolve_contradiction", {
                "new_content": "confirmed resolution content",
                "supersede_ids": [old_id],
                "agent_id": "test",
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        new_lid = resp["result"]["new_learning_id"]
        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT content FROM learnings WHERE learning_id = ?", (new_lid,)
            ).fetchone()
        assert row is not None
        assert "confirmed resolution content" in row["content"]

    def test_resolve_stores_structured_fields(self, tmp_path, monkeypatch):
        """resolve_contradiction stores assertion, applies_when, evidence_doc_ids."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)
        old_id = self._seed_learning(db_obj, "outdated auth fact")

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("resolve_contradiction", {
                "new_content": "Auth migrated to session cookies",
                "supersede_ids": [old_id],
                "agent_id": "test",
                "assertion": "auth: session-cookies",
                "applies_when": "production env",
                "evidence_doc_ids": [101, 202],
            })
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        new_lid = resp["result"]["new_learning_id"]
        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT assertion, applies_when, evidence_doc_ids FROM learnings WHERE learning_id = ?",
                (new_lid,)
            ).fetchone()
        assert row["assertion"] == "auth: session-cookies"
        assert row["applies_when"] == "production env"
        parsed = json.loads(row["evidence_doc_ids"])
        assert parsed == [101, 202]


# ---------------------------------------------------------------------------
# 6.10  Config: contradiction_threshold
# ---------------------------------------------------------------------------

class TestConfig:

    def test_default_threshold_is_0_85(self):
        from minni.config import MinniConfig
        cfg = MinniConfig()
        assert cfg.contradiction_threshold == 0.85

    def test_threshold_overridable(self):
        from minni.config import MinniConfig
        cfg = MinniConfig(contradiction_threshold=0.70)
        assert cfg.contradiction_threshold == 0.70

    def test_default_config_has_threshold(self):
        from minni.config import DEFAULT_CONFIG
        assert hasattr(DEFAULT_CONFIG, "contradiction_threshold")
        assert isinstance(DEFAULT_CONFIG.contradiction_threshold, float)


# ---------------------------------------------------------------------------
# 6.11  _extract_assertion
# ---------------------------------------------------------------------------

class TestExtractAssertion:

    def test_short_content_returned_as_is(self):
        from minni.minnid import _extract_assertion
        s = "Auth uses JWT."
        assert _extract_assertion(s) == s

    def test_long_content_returns_first_sentence(self):
        from minni.minnid import _extract_assertion
        # Make sure the string is longer than 120 chars so truncation triggers
        long = ("The auth system uses JWT tokens. " +
                "This was decided in March 2026 after evaluating several alternatives including "
                "OAuth2 bearer tokens and opaque session identifiers.")
        assert len(long) > 120, f"Test string too short: {len(long)}"
        result = _extract_assertion(long)
        assert result.endswith(".")
        assert len(result) < len(long)
        assert "JWT" in result

    def test_exactly_120_chars_returned_as_is(self):
        from minni.minnid import _extract_assertion
        s = "x" * 120
        assert _extract_assertion(s) == s

    def test_no_sentence_terminator_truncates_at_120(self):
        from minni.minnid import _extract_assertion
        s = "x" * 200  # no sentence terminator
        result = _extract_assertion(s)
        assert len(result) <= 120

    def test_strips_whitespace(self):
        from minni.minnid import _extract_assertion
        s = "  Auth uses JWT.  "
        result = _extract_assertion(s)
        assert not result.startswith(" ")


# ---------------------------------------------------------------------------
# 6.12  Full workflow: seed → detect → resolve (integration)
# ---------------------------------------------------------------------------

class TestFullWorkflow:
    """
    End-to-end integration: simulates the documented verification scenario
    from the spec without requiring a real sentence-transformer model.
    """

    def _dispatch(self, method, params):
        from minni.minnid import _dispatch_sync as _dispatch
        return _dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        })

    def _patch_writeback(self, tmp_path, monkeypatch):
        import minni.minnid as minnid
        from minni.writeback import WriteBackMemory
        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)
        monkeypatch.setattr(minnid, "_writeback", wb)
        return wb, db_obj, cfg

    def test_seed_detect_resolve(self, tmp_path, monkeypatch):
        """
        Wave 3 typed graph contradiction & resolution workflow:
        1. Seed a learning with force=True.
        2. Store an opposing-polarity learning with force=True -> typed
           `contradicts` edge in memory_links + unresolved row in contradiction_log.
        3. resolve_contradiction() writes the resolution + supersedes old.
        """
        import minni.minnid as minnid
        import minni.writeback as wb_mod

        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)
        monkeypatch.setattr(minnid, "MinniDB", lambda *a, **k: db_obj)
        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")
        monkeypatch.setattr(wb_mod.WriteBackMemory, "model", property(lambda self: None))

        resp1 = self._dispatch(
            "learn",
            {
                "content": "Always authenticate internal API calls with JWT bearer tokens.",
                "agent_id": "test",
                "category": "fact",
                "force": True,
            },
        )
        assert resp1["result"]["status"] == "ok", resp1
        seed_id = resp1["result"]["learning_id"]

        resp2 = self._dispatch(
            "learn",
            {
                "content": "Never authenticate internal API calls with JWT bearer tokens.",
                "agent_id": "test",
                "category": "correction",
                "force": True,
            },
        )
        assert resp2["result"]["status"] == "ok", resp2

        with db_obj.cursor() as c:
            edges = c.execute(
                "SELECT link_type FROM memory_links WHERE link_type = 'contradicts'"
            ).fetchall()
            logs = c.execute(
                "SELECT detection_method, resolution_status FROM contradiction_log"
            ).fetchall()
        assert len(edges) >= 1
        assert len(logs) == 1
        assert logs[0]["detection_method"] == "graph_classifier"
        assert logs[0]["resolution_status"] == "unresolved"

        resp3 = self._dispatch(
            "resolve_contradiction",
            {
                "new_content": "Internal API calls migrated from JWT to session cookies in April 2026.",
                "supersede_ids": [seed_id],
                "agent_id": "test",
            },
        )
        assert resp3["result"]["status"] == "ok", resp3
        assert seed_id in resp3["result"]["superseded"]

        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT status, superseded_by FROM learnings WHERE learning_id = ?",
                (seed_id,),
            ).fetchone()
        assert row["status"] == "superseded"
        assert row["superseded_by"] == resp3["result"]["new_learning_id"]

    def test_backward_compat_regression(self, tmp_path, monkeypatch):
        """
        Regression: existing learn() calls (no new fields, no force, no contradiction)
        now complete as G16 proposal packets without durable writes.
        """
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        import minni.writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            for i, content in enumerate([
                "WebSocket reconnection needs a 500ms backoff before retry",
                "Always use ECDSA over RSA for new certificates",
                "Database migrations must be idempotent",
            ]):
                resp = self._dispatch("learn", {"content": content, "agent_id": "main"})
                assert "error" not in resp, f"Call {i} errored: {resp}"
                result = resp["result"]
                assert result["status"] == "proposed", f"Call {i} not proposed: {result}"
                assert isinstance(result["candidate_id"], int)
                assert result["candidate_id"] > 0
                assert "learning_id" not in result
        finally:
            from minni.models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

    def test_contradiction_log_rows_written_g18(self, tmp_path, monkeypatch):
        """G18 + Wave 3: typed graph contradiction commits write unresolved graph_classifier rows."""
        import minni.minnid as minnid
        import minni.writeback as wb_mod

        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)
        monkeypatch.setattr(minnid, "MinniDB", lambda *a, **k: db_obj)
        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")
        monkeypatch.setattr(wb_mod.WriteBackMemory, "model", property(lambda self: None))

        r1 = self._dispatch(
            "learn",
            {
                "content": "Always enable WAL synchronous NORMAL on SQLite stores.",
                "agent_id": "main",
                "category": "fact",
                "force": True,
            },
        )
        assert r1["result"]["status"] == "ok", r1
        r2 = self._dispatch(
            "learn",
            {
                "content": "Never enable WAL synchronous NORMAL on SQLite stores.",
                "agent_id": "main",
                "category": "correction",
                "force": True,
            },
        )
        assert r2["result"]["status"] == "ok", r2
        new_lid = r2["result"]["learning_id"]

        with db_obj.cursor() as c:
            rows = c.execute(
                """
                SELECT memory_a_id, detected_at, detection_method, resolution_status
                FROM contradiction_log
                WHERE memory_a_id = ?
                """,
                (new_lid,),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["detected_at"] is not None
            assert rows[0]["detection_method"] == "graph_classifier"
            assert rows[0]["resolution_status"] == "unresolved"


class TestContradictionLogResolutionStatusShapes:
    """Typed graph contradiction rows stamp graph_classifier + unresolved (never legacy_unclassified)."""

    def test_typed_graph_contradiction_never_emits_legacy_unclassified(self, tmp_path, monkeypatch):
        import minni.minnid as minnid
        import minni.writeback as wb_mod
        from minni.writeback import WriteBackMemory

        monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "disabled")
        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)
        monkeypatch.setattr(minnid, "_writeback", wb)
        monkeypatch.setattr(minnid, "MinniDB", lambda *a, **k: db_obj)
        monkeypatch.setattr(wb_mod.WriteBackMemory, "model", property(lambda self: None))

        minnid._dispatch_sync(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "learn",
                "params": {
                    "content": "Always retry transient RPC timeouts up to 3 times.",
                    "agent_id": "main",
                    "category": "fact",
                    "force": True,
                },
            }
        )
        minnid._dispatch_sync(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "learn",
                "params": {
                    "content": "Never retry transient RPC timeouts up to 3 times.",
                    "agent_id": "main",
                    "category": "correction",
                    "force": True,
                },
            }
        )
        with db_obj.cursor() as c:
            rows = c.execute(
                "SELECT detection_method, resolution_status FROM contradiction_log"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["detection_method"] == "graph_classifier"
        assert rows[0]["resolution_status"] == "unresolved"

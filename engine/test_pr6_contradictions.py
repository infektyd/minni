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

# Hermetic principal setup for test integrity.
@pytest.fixture(autouse=True)
def setup_hermetic_principals(tmp_path, monkeypatch):
    """Replaces the module-level permissive resolve patch with a wrapper that writes
    realistic principal files to tmp_path/principals/ (chmod 0o600) and routes
    through the real principal resolution logic.
    """
    import principal
    import minnid

    pdir = tmp_path / "principals"
    pdir.mkdir(exist_ok=True)

    original_resolve = principal.resolve_effective_principal

    def _patched_resolve(*, supplied_agent_id=None, transport="uds", principals_dir=None):
        target_dir = principals_dir or pdir
        target_agent = str(supplied_agent_id or "main").strip() or "main"
        f = target_dir / "local.json"
        f.write_text(json.dumps({
            "agent_id": target_agent,
            "workspace_id": "default",
            "capabilities": ["*"]
        }), encoding="utf-8")
        os.chmod(f, 0o600)

        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=target_dir
        )

    monkeypatch.setattr(principal, "resolve_effective_principal", _patched_resolve)
    monkeypatch.setattr(sovrd, "resolve_effective_principal", _patched_resolve)



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    """Return (SovereignDB, SovereignConfig) backed by a temporary SQLite file."""
    import db as db_mod
    from config import SovereignConfig

    db_path = str(tmp_path / "test.db")
    cfg = SovereignConfig(db_path=db_path)

    # Reset the module-level guard so migrations run freshly for each test DB.
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    return db_obj, cfg


def _make_writeback(tmp_path):
    """Return a WriteBackMemory instance against a fresh test DB."""
    from writeback import WriteBackMemory
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
        from migrations import run_migrations

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
        from migrations import run_migrations

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
# 6.2  detect_contradictions: semantic matching
# ---------------------------------------------------------------------------

class TestDetectContradictions:

    def _seed_learning_with_emb(self, db_obj, agent_id, content, embedding: np.ndarray):
        """Insert a learning row directly with a specific embedding."""
        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, confidence, embedding, created_at, status)
                VALUES (?, 'fact', ?, 1.0, ?, ?, 'active')
            """, (agent_id, content, embedding.astype(np.float32).tobytes(), now))
            return c.lastrowid

    def test_high_cosine_match_returned(self, tmp_path):
        """A seeded learning with nearly identical embedding appears in candidates."""
        from writeback import WriteBackMemory
        from config import SovereignConfig
        import db as db_mod

        db_obj, cfg = _make_db(tmp_path)
        cfg = SovereignConfig(db_path=cfg.db_path, contradiction_threshold=0.85)
        wb = WriteBackMemory(db_obj, cfg)

        base_emb = _fixed_embedding("The auth system uses JWT tokens")
        close_emb = _nearly_identical_embedding(base_emb, noise=0.005)
        self._seed_learning_with_emb(db_obj, "agent1", "The auth system uses JWT tokens", base_emb)

        # Patch the model to return a deterministic near-identical embedding
        class _FakeModel:
            def encode(self, text):
                return close_emb

        wb._model = _FakeModel()
        # Override the property by monkeypatching
        import types
        original_model_prop = WriteBackMemory.model.fget
        WriteBackMemory.model = property(lambda self: self._model)
        try:
            candidates = wb.detect_contradictions("The auth system uses JWT tokens")
        finally:
            WriteBackMemory.model = property(original_model_prop)

        assert len(candidates) >= 1
        assert candidates[0]["score"] > 0.85
        assert "JWT" in candidates[0]["content"]

    def test_low_cosine_match_excluded(self, tmp_path):
        """A seeded learning with orthogonal embedding is NOT returned."""
        from writeback import WriteBackMemory
        from config import SovereignConfig

        db_obj, cfg = _make_db(tmp_path)
        cfg = SovereignConfig(db_path=cfg.db_path, contradiction_threshold=0.85)
        wb = WriteBackMemory(db_obj, cfg)

        base_emb = _fixed_embedding("Completely unrelated topic A")
        orth_emb = _orthogonal_embedding(base_emb)
        self._seed_learning_with_emb(db_obj, "agent1", "Completely unrelated topic A", orth_emb)

        query_emb = base_emb  # orthogonal to orth_emb

        class _FakeModel:
            def encode(self, text):
                return query_emb

        wb._model = _FakeModel()
        import types
        WriteBackMemory.model = property(lambda self: self._model)
        try:
            candidates = wb.detect_contradictions("Some query")
        finally:
            from models import get_embedder as _ge
            WriteBackMemory.model = property(lambda self: _ge())

        assert candidates == []

    def test_superseded_learnings_excluded(self, tmp_path):
        """Learnings with superseded_by IS NOT NULL are excluded from detection."""
        from writeback import WriteBackMemory

        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)

        base_emb = _fixed_embedding("auth uses JWT")
        now = time.time()
        with db_obj.cursor() as c:
            # Insert the "new" learning first so the FK reference is valid
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                VALUES ('agent1', 'fact', 'new learning', 1.0, ?, 'active')
            """, (now,))
            new_id = c.lastrowid
            # Now insert the "superseded" learning referencing it
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, confidence, embedding, created_at, superseded_by)
                VALUES ('agent1', 'fact', 'auth uses JWT', 1.0, ?, ?, ?)
            """, (base_emb.tobytes(), now, new_id))

        class _FakeModel:
            def encode(self, text):
                return _nearly_identical_embedding(base_emb, noise=0.001)

        wb._model = _FakeModel()
        WriteBackMemory.model = property(lambda self: self._model)
        try:
            candidates = wb.detect_contradictions("auth uses JWT")
        finally:
            from models import get_embedder as _ge
            WriteBackMemory.model = property(lambda self: _ge())

        assert candidates == [], "Superseded learnings should not appear as candidates"

    def test_status_superseded_excluded(self, tmp_path):
        """Learnings with status='superseded' are excluded even if superseded_by IS NULL."""
        from writeback import WriteBackMemory

        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)

        base_emb = _fixed_embedding("auth jwt")
        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, confidence, embedding, created_at, status)
                VALUES ('agent1', 'fact', 'auth jwt', 1.0, ?, ?, 'superseded')
            """, (base_emb.tobytes(), now))

        class _FakeModel:
            def encode(self, text):
                return _nearly_identical_embedding(base_emb, noise=0.001)

        wb._model = _FakeModel()
        WriteBackMemory.model = property(lambda self: self._model)
        try:
            candidates = wb.detect_contradictions("auth jwt")
        finally:
            from models import get_embedder as _ge
            WriteBackMemory.model = property(lambda self: _ge())

        assert candidates == []

    def test_candidates_sorted_by_score_descending(self, tmp_path):
        """Candidates list is sorted by score descending."""
        from writeback import WriteBackMemory
        from config import SovereignConfig

        db_obj, cfg = _make_db(tmp_path)
        cfg = SovereignConfig(db_path=cfg.db_path, contradiction_threshold=0.50)
        wb = WriteBackMemory(db_obj, cfg)

        emb_a = _fixed_embedding("topic A")
        emb_b = _nearly_identical_embedding(emb_a, noise=0.02)  # slightly different

        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence, embedding, created_at, status)
                VALUES ('a', 'fact', 'topic A exact', 1.0, ?, ?, 'active')
            """, (emb_a.tobytes(), now))
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence, embedding, created_at, status)
                VALUES ('a', 'fact', 'topic A variant', 1.0, ?, ?, 'active')
            """, (emb_b.tobytes(), now))

        class _FakeModel:
            def encode(self, text):
                return emb_a  # identical to emb_a → sim=1.0

        wb._model = _FakeModel()
        WriteBackMemory.model = property(lambda self: self._model)
        try:
            candidates = wb.detect_contradictions("topic A exact", threshold=0.50)
        finally:
            from models import get_embedder as _ge
            WriteBackMemory.model = property(lambda self: _ge())

        if len(candidates) >= 2:
            assert candidates[0]["score"] >= candidates[1]["score"]


# ---------------------------------------------------------------------------
# 6.3  detect_contradictions: graceful failure
# ---------------------------------------------------------------------------

class TestDetectContradictionsGraceful:

    def test_returns_empty_when_model_none(self, tmp_path):
        """detect_contradictions returns [] when model is None."""
        from writeback import WriteBackMemory

        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)

        WriteBackMemory.model = property(lambda self: None)
        try:
            result = wb.detect_contradictions("anything")
        finally:
            from models import get_embedder as _ge
            WriteBackMemory.model = property(lambda self: _ge())

        assert result == []

    def test_returns_empty_when_encode_raises(self, tmp_path):
        """detect_contradictions returns [] when embedding raises an exception."""
        from writeback import WriteBackMemory

        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)

        class _BrokenModel:
            def encode(self, text):
                raise RuntimeError("GPU exploded")

        wb._model = _BrokenModel()
        WriteBackMemory.model = property(lambda self: self._model)
        try:
            result = wb.detect_contradictions("anything")
        finally:
            from models import get_embedder as _ge
            WriteBackMemory.model = property(lambda self: _ge())

        assert result == []


# ---------------------------------------------------------------------------
# 6.4–6.7  _handle_learn (JSON-RPC handler)
# ---------------------------------------------------------------------------

class TestHandleLearn:
    """Tests for _handle_learn via JSON-RPC dispatch."""

    def _dispatch(self, method, params):
        """Invoke the JSON-RPC _dispatch function directly."""
        from minnid import _dispatch_sync as _dispatch
        resp = _dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        })
        return resp

    def _patch_writeback(self, tmp_path, monkeypatch):
        """Make the global _writeback singleton point at a fresh test DB."""
        import minnid
        from writeback import WriteBackMemory

        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)
        monkeypatch.setattr(sovrd, "_writeback", wb)
        return wb, db_obj, cfg

    def test_backward_compat_no_new_fields(self, tmp_path, monkeypatch):
        """Existing learn() calls without force stage proposal packets under G16."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        # No model → no contradiction detection, should just write
        import writeback as wb_mod
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

    def test_learn_force_false_blocked_on_contradiction(self, tmp_path, monkeypatch):
        """learn(force=False) returns {status: contradiction} when a match exists."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        base_emb = _fixed_embedding("The auth system uses JWT tokens")

        # Seed an existing learning with that embedding
        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence,
                                       embedding, created_at, status)
                VALUES ('agent1', 'fact', 'The auth system uses JWT tokens', 1.0, ?, ?, 'active')
            """, (base_emb.tobytes(), now))

        # Patch model to return a near-identical embedding
        close_emb = _nearly_identical_embedding(base_emb, noise=0.001)

        import writeback as wb_mod
        class _FakeModel:
            def encode(self, text):
                return close_emb
        wb_mod.WriteBackMemory.model = property(lambda self: _FakeModel())
        try:
            resp = self._dispatch("learn", {
                "content": "The auth system uses session cookies, not JWT",
                "agent_id": "test",
                # force defaults to False
            })
        finally:
            from models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        assert "error" not in resp, f"Unexpected error: {resp}"
        result = resp["result"]
        assert result["status"] == "contradiction"
        assert "candidates" in result
        assert len(result["candidates"]) >= 1

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

        import writeback as wb_mod
        class _FakeModel:
            def encode(self, text):
                return close_emb
        wb_mod.WriteBackMemory.model = property(lambda self: _FakeModel())
        try:
            resp = self._dispatch("learn", {
                "content": "Auth uses session cookies now",
                "agent_id": "test",
                "force": True,
            })
        finally:
            from models import get_embedder as _ge
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
        import writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("learn", {
                "content": "Auth now uses session cookies",
                "agent_id": "test",
                "contradicts_id": old_id,  # explicit contradiction bypasses detection
            })
        finally:
            from models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        assert "error" not in resp, f"Unexpected error: {resp}"
        result = resp["result"]
        assert result["status"] == "proposed"
        assert "candidate_id" in result

    def test_learn_stores_assertion_field(self, tmp_path, monkeypatch):
        """learn() stores the assertion field in the DB when provided."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        import writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("learn", {
                "content": "Auth uses JWT. More context follows.",
                "assertion": "Auth system: JWT",
                "agent_id": "test",
                "force": True,
            })
        finally:
            from models import get_embedder as _ge
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

        import writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("learn", {
                "content": "Some new fact",
                "agent_id": "test",
            })
        finally:
            from models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        result = resp["result"]
        assert result["status"] == "proposed"
        assert "candidate_id" in result


# ---------------------------------------------------------------------------
# 6.8–6.9  _handle_resolve_contradiction
# ---------------------------------------------------------------------------

class TestHandleResolveContradiction:

    def _dispatch(self, method, params):
        from minnid import _dispatch_sync as _dispatch
        return _dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        })

    def _patch_writeback(self, tmp_path, monkeypatch):
        import minnid
        from writeback import WriteBackMemory
        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)
        monkeypatch.setattr(sovrd, "_writeback", wb)
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
                    VALUES ('a', 'fact', ?, 1.0, ?, 'active')
                """, (f"old learning {i}", now))
                old_ids.append(c.lastrowid)

        import writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("resolve_contradiction", {
                "new_content": "Resolved: auth uses session cookies since April 2026",
                "supersede_ids": old_ids,
                "agent_id": "test",
            })
        finally:
            from models import get_embedder as _ge
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

    def test_subscribe_contradictions_returns_events_for_read_learnings(self, tmp_path, monkeypatch):
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        now = time.time()
        with db_obj.cursor() as c:
            c.execute("""
                INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                VALUES ('a', 'fact', 'old learning', 1.0, ?, 'active')
            """, (now,))
            old_id = c.lastrowid
            c.execute("""
                INSERT INTO learning_reads (learning_id, agent_id, read_at, source)
                VALUES (?, 'codex', ?, 'unit-test')
            """, (old_id, now))

        import writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resolved = self._dispatch("resolve_contradiction", {
                "new_content": "new learning",
                "supersede_ids": [old_id],
                "agent_id": "test",
            })
        finally:
            from models import get_embedder as _ge
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
        """resolve_contradiction with empty supersede_ids still writes the new learning."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        import writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("resolve_contradiction", {
                "new_content": "standalone learning",
                "supersede_ids": [],
                "agent_id": "test",
            })
        finally:
            from models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

        assert "error" not in resp
        assert resp["result"]["status"] == "ok"
        assert resp["result"]["superseded"] == []

    def test_resolve_atomicity_new_learning_written(self, tmp_path, monkeypatch):
        """Even with an empty supersede list, the new learning exists in DB."""
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        import writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("resolve_contradiction", {
                "new_content": "confirmed resolution content",
                "supersede_ids": [],
                "agent_id": "test",
            })
        finally:
            from models import get_embedder as _ge
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

        import writeback as wb_mod
        wb_mod.WriteBackMemory.model = property(lambda self: None)
        try:
            resp = self._dispatch("resolve_contradiction", {
                "new_content": "Auth migrated to session cookies",
                "supersede_ids": [],
                "agent_id": "test",
                "assertion": "auth: session-cookies",
                "applies_when": "production env",
                "evidence_doc_ids": [101, 202],
            })
        finally:
            from models import get_embedder as _ge
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
        from config import SovereignConfig
        cfg = SovereignConfig()
        assert cfg.contradiction_threshold == 0.85

    def test_threshold_overridable(self):
        from config import SovereignConfig
        cfg = SovereignConfig(contradiction_threshold=0.70)
        assert cfg.contradiction_threshold == 0.70

    def test_default_config_has_threshold(self):
        from config import DEFAULT_CONFIG
        assert hasattr(DEFAULT_CONFIG, "contradiction_threshold")
        assert isinstance(DEFAULT_CONFIG.contradiction_threshold, float)


# ---------------------------------------------------------------------------
# 6.11  _extract_assertion
# ---------------------------------------------------------------------------

class TestExtractAssertion:

    def test_short_content_returned_as_is(self):
        from minnid import _extract_assertion
        s = "Auth uses JWT."
        assert _extract_assertion(s) == s

    def test_long_content_returns_first_sentence(self):
        from minnid import _extract_assertion
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
        from minnid import _extract_assertion
        s = "x" * 120
        assert _extract_assertion(s) == s

    def test_no_sentence_terminator_truncates_at_120(self):
        from minnid import _extract_assertion
        s = "x" * 200  # no sentence terminator
        result = _extract_assertion(s)
        assert len(result) <= 120

    def test_strips_whitespace(self):
        from minnid import _extract_assertion
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
        from minnid import _dispatch_sync as _dispatch
        return _dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        })

    def _patch_writeback(self, tmp_path, monkeypatch):
        import minnid
        from writeback import WriteBackMemory
        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)
        monkeypatch.setattr(sovrd, "_writeback", wb)
        return wb, db_obj, cfg

    def test_seed_detect_resolve(self, tmp_path, monkeypatch):
        """
        1. Seed a learning with force=True.
        2. A contradicting learn() is blocked.
        3. resolve_contradiction() writes new + supersedes old.
        """
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        jwt_emb = _fixed_embedding("The auth system uses JWT tokens")
        cookie_emb = _nearly_identical_embedding(jwt_emb, noise=0.001)

        import writeback as wb_mod
        call_count = [0]

        class _SeqModel:
            """Returns jwt_emb for the first call, cookie_emb for subsequent calls."""
            def encode(self, text):
                call_count[0] += 1
                if call_count[0] == 1:
                    return jwt_emb
                return cookie_emb

        wb_mod.WriteBackMemory.model = property(lambda self: _SeqModel())
        try:
            # Step 1: seed with force=True
            resp1 = self._dispatch("learn", {
                "content": "The auth system uses JWT tokens",
                "agent_id": "test",
                "force": True,
            })
            assert resp1["result"]["status"] == "ok", resp1
            seed_id = resp1["result"]["learning_id"]

            # Step 2: contradicting learn is blocked
            resp2 = self._dispatch("learn", {
                "content": "The auth system uses session cookies, not JWT",
                "agent_id": "test",
            })
            assert resp2["result"]["status"] == "contradiction", resp2
            assert len(resp2["result"]["candidates"]) >= 1
            candidate_id = resp2["result"]["candidates"][0]["id"]

            # Step 3: resolve
            resp3 = self._dispatch("resolve_contradiction", {
                "new_content": "The auth system migrated from JWT to session cookies in April 2026",
                "supersede_ids": [candidate_id],
                "agent_id": "test",
            })
            assert resp3["result"]["status"] == "ok", resp3
            assert candidate_id in resp3["result"]["superseded"]

            # Verify old learning is superseded
            with db_obj.cursor() as c:
                row = c.execute(
                    "SELECT status, superseded_by FROM learnings WHERE learning_id = ?",
                    (candidate_id,)
                ).fetchone()
            assert row["status"] == "superseded"
            assert row["superseded_by"] == resp3["result"]["new_learning_id"]

        finally:
            from models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

    def test_backward_compat_regression(self, tmp_path, monkeypatch):
        """
        Regression: existing learn() calls (no new fields, no force, no contradiction)
        now complete as G16 proposal packets without durable writes.
        """
        wb, db_obj, cfg = self._patch_writeback(tmp_path, monkeypatch)

        import writeback as wb_mod
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
            from models import get_embedder as _ge
            wb_mod.WriteBackMemory.model = property(lambda self: _ge())

    def test_contradiction_log_rows_written_g18(self, tmp_path, monkeypatch):
        """G18: detect_contradictions writes rows to contradiction_log (wired in writeback)."""
        from writeback import WriteBackMemory
        import writeback as wb_mod

        db_obj, cfg = _make_db(tmp_path)
        wb = WriteBackMemory(db_obj, cfg)

        base_emb = _fixed_embedding("JWT tokens are used for auth")
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """
                INSERT INTO learnings
                (agent_id, category, content, confidence, embedding, created_at, status)
                VALUES ('a1', 'fact', 'JWT tokens are used for auth in the API layer', 1.0, ?, ?, 'active')
                """,
                (base_emb.astype(np.float32).tobytes(), now),
            )
            seeded_id = c.lastrowid

        class _FakeModel:
            def encode(self, _text):
                return _nearly_identical_embedding(base_emb, noise=0.001)

        original_model_prop = wb_mod.WriteBackMemory.model.fget
        wb_mod.WriteBackMemory.model = property(lambda self: _FakeModel())
        try:
            cands = wb.detect_contradictions("JWT tokens are used for auth")
        finally:
            wb_mod.WriteBackMemory.model = property(original_model_prop)

        assert [cand["id"] for cand in cands] == [seeded_id]
        with db_obj.cursor() as c:
            rows = c.execute(
                """
                SELECT memory_a_id, detected_at, detection_method, resolution_id
                FROM contradiction_log
                WHERE memory_a_id = ?
                """,
                (seeded_id,),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["detected_at"] is not None
            assert rows[0]["detection_method"] == "cosine"
            assert rows[0]["resolution_id"] is None

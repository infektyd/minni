"""Tests for Issue #437 (learnings deduplication, prompt-severing guard,
boot envelope filtering) and Issue #388 (embedding / cross_encoder latency
telemetry recording).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def setup_hermetic_principals(tmp_path, monkeypatch):
    import minni.minnid as minnid
    import minni.principal as principal

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
        target_agent = str(supplied_agent_id or "main").strip() or "main"
        fname = "local.json" if supplied_agent_id is None else f"{target_agent}.json"
        f = target_dir / fname
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


def _make_db(tmp_path, monkeypatch):
    import minni.config as cfg_mod
    import minni.db as db_mod
    import minni.minnid as minnid
    from minni.config import MinniConfig
    from minni.writeback import WriteBackMemory

    db_path = str(tmp_path / "test_437.db")
    cfg = MinniConfig(
        db_path=db_path,
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
    )
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", db_path, raising=False)
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.MinniDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    wb = WriteBackMemory(db_obj, cfg)
    monkeypatch.setattr(minnid, "_writeback", wb)
    monkeypatch.setattr(minnid, "MinniDB", lambda *a, **k: db_mod.MinniDB(cfg))
    return db_obj, wb, cfg


def test_resolve_candidate_accept_deduplicates_identical_and_whitespace_variants(
    tmp_path, monkeypatch
):
    """Accepting two candidates with identical or whitespace-normalized content
    creates only ONE active row in learnings and returns the existing learning_id."""
    import minni.minnid as minnid
    import minni.writeback as wb_mod

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    monkeypatch.setattr(wb_mod.WriteBackMemory, "model", property(lambda self: None))
    monkeypatch.setattr(minnid, "_index_durable_learning", lambda *a, **k: True)

    s1 = minnid._stage_candidate(
        {
            "content": "Always verify disk headroom with df -h / before starting a build.",
            "agent_id": "main",
            "category": "architecture",
        },
        1,
    )
    cid1 = s1["result"]["candidate_id"]

    s2 = minnid._stage_candidate(
        {
            "content": "  Always   verify disk headroom with df -h / before starting a build. \n",
            "agent_id": "main",
            "category": "architecture",
        },
        2,
    )
    cid2 = s2["result"]["candidate_id"]

    r1 = minnid._resolve_candidate(
        {"candidate_id": cid1, "decision": "accept", "agent_id": "main"}, 3
    )
    r2 = minnid._resolve_candidate(
        {"candidate_id": cid2, "decision": "accept", "agent_id": "main"}, 4
    )

    assert r1["result"]["new_status"] == "accepted"
    assert r2["result"]["new_status"] == "accepted"
    assert r2["result"]["learning_id"] != r1["result"]["learning_id"]

    with db_obj.cursor() as c:
        rows = c.execute(
            "SELECT learning_id, status FROM learnings "
            "WHERE agent_id = 'main' AND COALESCE(status, 'active') = 'active' "
            "AND superseded_by IS NULL"
        ).fetchall()
        sup_row = c.execute(
            "SELECT superseded_by, status FROM learnings WHERE learning_id = ?",
            (r1["result"]["learning_id"],),
        ).fetchone()
    assert len(rows) == 1
    assert rows[0]["learning_id"] == r2["result"]["learning_id"]
    assert sup_row["superseded_by"] == r2["result"]["learning_id"]
    assert sup_row["status"] == "superseded"

    # Also verify that when content_hash column exists, resolve_candidate populates it
    from minni.minnid_runtime.governance import ensure_content_hash_column

    assert ensure_content_hash_column(db_obj) is True
    s3 = minnid._stage_candidate(
        {
            "content": "Always verify disk headroom with df -h / before starting a build.",
            "agent_id": "main",
            "category": "architecture",
        },
        5,
    )
    r3 = minnid._resolve_candidate(
        {
            "candidate_id": s3["result"]["candidate_id"],
            "decision": "accept",
            "agent_id": "main",
        },
        6,
    )
    with db_obj.cursor() as c:
        active_rows = c.execute(
            "SELECT learning_id, content_hash FROM learnings "
            "WHERE agent_id = 'main' AND COALESCE(status, 'active') = 'active' "
            "AND superseded_by IS NULL"
        ).fetchall()
    assert len(active_rows) == 1
    assert active_rows[0]["learning_id"] == r3["result"]["learning_id"]
    assert active_rows[0]["content_hash"] is not None


def test_handle_learn_force_true_deduplicates_active_learning(tmp_path, monkeypatch):
    """handle_learn(force=True) with duplicate content for the same agent_id
    returns the existing learning_id with deduplicated=True instead of inserting
    a duplicate active row."""
    import minni.minnid as minnid
    import minni.writeback as wb_mod

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    monkeypatch.setattr(wb_mod.WriteBackMemory, "model", property(lambda self: None))
    monkeypatch.setattr(minnid, "_index_durable_learning", lambda *a, **k: True)

    text = "SQLite WAL mode requires shared-memory access on the same host filesystem."
    resp1 = minnid._handle_learn(
        {"content": text, "agent_id": "main", "category": "fact", "force": True}, 1
    )
    resp2 = minnid._handle_learn(
        {
            "content": "  sqlite wal mode requires shared-memory access on the same host filesystem. ",
            "agent_id": "main",
            "category": "fact",
            "force": True,
        },
        2,
    )

    assert resp1["result"]["status"] == "ok"
    assert resp2["result"]["status"] == "ok"
    assert resp2["result"]["learning_id"] == resp1["result"]["learning_id"]
    assert resp2["result"].get("deduplicated") is True

    with db_obj.cursor() as c:
        count = c.execute(
            "SELECT COUNT(*) AS n FROM learnings WHERE agent_id = 'main' "
            "AND COALESCE(status, 'active') = 'active' AND superseded_by IS NULL"
        ).fetchone()["n"]
    assert count == 1


def test_quality_and_auto_accept_blockers_reject_prompt_echo_and_test_output():
    """_quality_blockers and auto_accept_blockers withhold raw prompt echoes and
    raw test-runner summary lines."""
    from minni.afm_passes.consolidation import (
        _quality_blockers,
        is_low_signal_boot_learning,
    )
    from minni.minnid_runtime.governance import auto_accept_blockers

    for prompt_sample in (
        "Executed prompt: Run the full test suite and report any failures.",
        "  User prompt: Fix the flaky test in recall.py and commit.",
        "System prompt: You are an autonomous coding assistant.",
    ):
        qb = _quality_blockers(prompt_sample)
        assert "raw prompt echo" in qb, f"Expected raw prompt echo blocker for {prompt_sample!r}"
        ab = auto_accept_blockers(prompt_sample, instruction_like=False, requested_privacy="safe")
        assert "raw prompt echo" in ab
        assert is_low_signal_boot_learning(prompt_sample) is True

    for test_sample in (
        "================= 14 passed in 0.42s =================",
        "Ran 8 tests in 0.12s",
        "Test Suites: 5 passed, 5 total",
        "12 passed, 1 skipped in 1.23s",
    ):
        qb = _quality_blockers(test_sample)
        assert "raw test output telemetry" in qb, (
            f"Expected raw test output telemetry blocker for {test_sample!r}"
        )
        ab = auto_accept_blockers(test_sample, instruction_like=False, requested_privacy="safe")
        assert "raw test output telemetry" in ab
        assert is_low_signal_boot_learning(test_sample) is True

    valid_learning = "Ensure content_hash is indexed on learnings to keep duplicate checks O(log N)."
    assert _quality_blockers(valid_learning) == []
    assert is_low_signal_boot_learning(valid_learning) is False


def test_handle_read_boot_envelope_deduplicates_and_filters_low_signal(
    tmp_path, monkeypatch
):
    """minnid._handle_read deduplicates historical duplicate rows, excludes
    non-active rows, and filters out low-signal prompt/audit/test echoes."""
    import minni.minnid as minnid

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    now = time.time()

    with db_obj.cursor() as c:
        # 1. Duplicate active rows (should render only once)
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'fact', 'Use WAL mode for concurrent readers.', 1.0, ?, 'active')",
            (now - 50,),
        )
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'fact', '  use wal mode for concurrent readers. ', 1.0, ?, 'active')",
            (now - 40,),
        )
        # 2. Non-active rows (superseded / rejected — should be excluded even if superseded_by is NULL)
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'fact', 'Obsolete status-superseded learning.', 1.0, ?, 'superseded')",
            (now - 30,),
        )
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'fact', 'Rejected learning row.', 1.0, ?, 'rejected')",
            (now - 25,),
        )
        # 3. Low-signal rows (prompt echo, test runner output, audit echo)
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'general', 'Executed prompt: check git status and run pytest', 1.0, ?, 'active')",
            (now - 20,),
        )
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'general', '=== 42 passed in 0.88s ===', 1.0, ?, 'active')",
            (now - 15,),
        )
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'general', '## [2026-10-16T12:00:00Z] minni_recall | ok', 1.0, ?, 'active')",
            (now - 10,),
        )
        # 4. Another distinct valid learning
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'architecture', 'Always bound cross-encoder batch size on CPU.', 1.0, ?, 'active')",
            (now - 5,),
        )

    resp = minnid._handle_read({"agent_id": "main"}, 1)
    ctx_text = resp["result"]["context"]

    assert "Always bound cross-encoder batch size on CPU." in ctx_text
    assert ctx_text.lower().count("use wal mode for concurrent readers.") == 1
    assert "Obsolete status-superseded learning." not in ctx_text
    assert "Rejected learning row." not in ctx_text
    assert "Executed prompt:" not in ctx_text
    assert "42 passed in 0.88s" not in ctx_text
    assert "minni_recall | ok" not in ctx_text


def test_dedup_active_learnings_supersedes_duplicates_and_prunes_low_signal(
    tmp_path, monkeypatch
):
    """dedup_active_learnings supersedes older duplicate active rows (keeping
    the newest learning_id) and prunes low-signal rows."""
    from minni.afm_passes.consolidation import dedup_active_learnings

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    now = time.time()

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'fact', 'Keep transactions short under BEGIN IMMEDIATE.', 1.0, ?, 'active')",
            (now - 30,),
        )
        older_id = c.lastrowid
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'fact', '  keep transactions short under begin immediate. ', 1.0, ?, 'active')",
            (now - 10,),
        )
        newer_id = c.lastrowid
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'general', 'Executed prompt: run consolidation pass now', 1.0, ?, 'active')",
            (now - 5,),
        )
        low_sig_id = c.lastrowid

    stats = dedup_active_learnings(db_obj)
    assert stats["superseded_duplicates"] == 1
    assert stats["pruned_low_signal"] == 1
    assert stats["examined"] == 3

    with db_obj.cursor() as c:
        rows = {
            r["learning_id"]: dict(r)
            for r in c.execute(
                "SELECT learning_id, status, superseded_by FROM learnings"
            ).fetchall()
        }
    assert rows[newer_id]["status"] == "active"
    assert rows[newer_id]["superseded_by"] is None
    assert rows[older_id]["status"] == "superseded"
    assert rows[older_id]["superseded_by"] == newer_id
    assert rows[low_sig_id]["status"] in ("superseded", "rejected")


def test_encode_query_and_rerank_record_daemon_latencies(tmp_path, monkeypatch):
    """_encode_query records 'embedding' latency and _rerank records
    'cross_encoder' latency into minnid._latencies (#388)."""
    import minni.minnid as minnid
    import minni.models as models_mod
    from minni.retrieval import RetrievalEngine

    db_obj, _wb, cfg = _make_db(tmp_path, monkeypatch)
    engine = RetrievalEngine(db_obj, cfg)

    class _FakeEmbedder:
        def encode(self, text, **kwargs):
            return np.ones(384, dtype=np.float32)

    class _FakeCrossEncoder:
        def predict(self, pairs, **kwargs):
            return [0.9 for _ in pairs]

    monkeypatch.setattr(models_mod, "get_embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(type(engine), "model", property(lambda self: _FakeEmbedder()))
    monkeypatch.setattr(type(engine), "reranker", property(lambda self: _FakeCrossEncoder()))

    minnid._latencies["embedding"].clear()
    minnid._latencies["cross_encoder"].clear()

    vec = engine._encode_query("latency telemetry test query")
    assert vec.size == 384
    assert len(minnid._latencies["embedding"]) == 1

    candidates = [
        {"doc_id": 1, "chunk_id": 10, "chunk_text": "first passage", "rrf_score": 0.5},
        {"doc_id": 2, "chunk_id": 20, "chunk_text": "second passage", "rrf_score": 0.4},
    ]
    monkeypatch.setattr(models_mod, "cross_encoder_unlocked_predict_safe", lambda: True)
    engine._rerank("latency telemetry test query", candidates)
    assert len(minnid._latencies["cross_encoder"]) == 1

    monkeypatch.setattr(models_mod, "cross_encoder_unlocked_predict_safe", lambda: False)
    engine._rerank("latency telemetry test query locked", candidates)
    assert len(minnid._latencies["cross_encoder"]) == 2


def test_cross_agent_isolation_and_empty_inputs(tmp_path, monkeypatch):
    """Deduplication is strictly per-agent (never merges across agents) and
    is_low_signal_boot_learning handles empty/None inputs safely."""
    import minni.minnid as minnid
    import minni.writeback as wb_mod
    from minni.afm_passes.consolidation import (
        dedup_active_learnings,
        is_low_signal_boot_learning,
    )

    assert is_low_signal_boot_learning("") is True
    assert is_low_signal_boot_learning("   \n\t  ") is True
    assert is_low_signal_boot_learning(None) is True  # type: ignore[arg-type]

    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    monkeypatch.setattr(wb_mod.WriteBackMemory, "model", property(lambda self: None))
    monkeypatch.setattr(minnid, "_index_durable_learning", lambda *a, **k: True)

    shared_text = "Shared fact across two distinct principals must remain separate per owner."
    r_main = minnid._handle_learn(
        {"content": shared_text, "agent_id": "main", "category": "fact", "force": True}, 1
    )
    r_other = minnid._handle_learn(
        {"content": shared_text, "agent_id": "operator", "category": "fact", "force": True}, 2
    )
    assert r_main["result"]["learning_id"] != r_other["result"]["learning_id"]
    assert r_other["result"].get("deduplicated") is not True

    stats = dedup_active_learnings(db_obj)
    assert stats["superseded_duplicates"] == 0
    assert stats["pruned_low_signal"] == 0
    assert stats["examined"] == 2


def test_tuple_cursor_and_minimal_schema_fallbacks(tmp_path, monkeypatch):
    """find_active_learning_by_hash and has_active_learning_hash work with a
    plain sqlite3.Cursor (row_factory=None) with or without content_hash column,
    and handle_read degrades cleanly when confidence is NULL."""
    import minni.minnid as minnid
    from minni.afm_passes.consolidation import (
        content_hash,
        find_active_learning_by_hash,
        has_active_learning_hash,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE learnings ("
        "learning_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "agent_id TEXT, category TEXT, content TEXT, "
        "created_at REAL, content_hash TEXT"
        ")"
    )
    text = "Plain tuple cursor compatibility check for active learning hash."
    h = content_hash(text)
    conn.execute(
        "INSERT INTO learnings (agent_id, category, content, created_at, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("main", "fact", text, 1.0, h),
    )
    conn.execute(
        "INSERT INTO learnings (agent_id, category, content, created_at, content_hash) "
        "VALUES (?, ?, ?, ?, NULL)",
        ("main", "fact", "Second learning with NULL content_hash", 2.0),
    )
    cur = conn.cursor()
    found = find_active_learning_by_hash(cur, h, "main")
    assert found is not None
    assert found["learning_id"] == 1
    assert found["category"] == "fact"
    assert has_active_learning_hash(cur, h, "main") is True

    h2 = content_hash("Second learning with NULL content_hash")
    found_null = find_active_learning_by_hash(cur, h2, "main")
    assert found_null is not None
    assert found_null["learning_id"] == 2
    conn.close()

    # Also test plain tuple cursor without content_hash column at all
    conn2 = sqlite3.connect(":memory:")
    conn2.execute(
        "CREATE TABLE learnings ("
        "learning_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "agent_id TEXT, category TEXT, content TEXT, created_at REAL"
        ")"
    )
    conn2.execute(
        "INSERT INTO learnings (agent_id, category, content, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("main", "fact", text, 1.0),
    )
    cur2 = conn2.cursor()
    found_no_col = find_active_learning_by_hash(cur2, h, "main")
    assert found_no_col is not None
    assert found_no_col["learning_id"] == 1
    conn2.close()

    # Also verify handle_read formats NULL confidence without TypeError
    db_obj, _wb, _cfg = _make_db(tmp_path, monkeypatch)
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at, status) "
            "VALUES ('main', 'fact', 'Row with NULL confidence must still render.', NULL, ?, 'active')",
            (time.time(),),
        )
    resp = minnid._handle_read({"agent_id": "main"}, 1)
    assert "Row with NULL confidence must still render." in resp["result"]["context"]
    assert "(conf=1.0)" in resp["result"]["context"]




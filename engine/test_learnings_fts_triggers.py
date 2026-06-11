"""Tests for learnings_fts UPDATE/DELETE sync triggers."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def test_learnings_fts_update_syncs(tmp_path):
    db_obj, _cfg = _make_db(tmp_path)

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, created_at) VALUES (?, ?, ?, ?)",
            ("agent1", "general", "some content", 1000.0),
        )
        lid = c.lastrowid

    with db_obj.cursor() as c:
        c.execute(
            "UPDATE learnings SET agent_id=? WHERE learning_id=?",
            ("agent2", lid),
        )

    with db_obj.cursor() as c:
        c.execute("SELECT * FROM learnings_fts WHERE learning_id=?", (lid,))
        row = c.fetchone()

    assert row is not None
    assert dict(row)["agent_id"] == "agent2"


def test_learnings_fts_delete_syncs(tmp_path):
    db_obj, _cfg = _make_db(tmp_path)

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, created_at) VALUES (?, ?, ?, ?)",
            ("agent1", "general", "some content", 1000.0),
        )
        lid = c.lastrowid

    with db_obj.cursor() as c:
        c.execute("DELETE FROM learnings WHERE learning_id=?", (lid,))

    with db_obj.cursor() as c:
        c.execute("SELECT * FROM learnings_fts WHERE learning_id=?", (lid,))
        row = c.fetchone()

    assert row is None
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def test_empty_document_index_returns_without_loading_embedder(tmp_path, monkeypatch):
    import db as db_mod
    from config import SovereignConfig
    from retrieval import RetrievalEngine

    cfg = SovereignConfig(db_path=str(tmp_path / "empty.db"))
    old_flag = db_mod._migrations_run
    old_paths = db_mod._migrated_paths.copy()
    db_mod._migrations_run = False
    db_mod._migrated_paths = set()
    try:
        db = db_mod.SovereignDB(cfg)
        db._get_conn()
    finally:
        db_mod._migrations_run = old_flag
        db_mod._migrated_paths = old_paths

    def fail_model_load(_self):
        raise AssertionError("empty-index recall should not load the embedder")

    monkeypatch.setattr(
        RetrievalEngine,
        "model",
        property(fail_model_load),
        raising=False,
    )

    engine = RetrievalEngine(db, cfg)
    assert engine.retrieve("smoke test recall", limit=1, budget_tokens=False) == []

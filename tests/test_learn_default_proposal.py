"""G16: learn() without force returns proposal status (not durable learning_id)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.minnid import _handle_learn


def test_learn_default_is_proposal_not_durable(tmp_path, monkeypatch):
    """Non-force learn stages a candidate (status=proposed) instead of writing learning."""
    import minni.config as cfg
    old = cfg.DEFAULT_CONFIG.db_path
    cfg.DEFAULT_CONFIG.db_path = str(tmp_path / "proposal.db")
    try:
        from minni.migrations import run_migrations
        from minni.db import SovereignDB
        db = SovereignDB()
        run_migrations(db._get_conn())

        resp = _handle_learn({"content": "Proposal default test G16", "agent_id": "main"}, 42)
        res = resp.get("result") or resp
        assert res.get("status") == "proposed"
        assert "candidate_id" in res
        assert "learning_id" not in res  # no durable id on default path
    finally:
        cfg.DEFAULT_CONFIG.db_path = old

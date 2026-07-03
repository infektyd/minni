"""G16: force=true learn requires operator principal or returns operator_only error."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.minnid import _handle_learn


def test_force_learn_operator_gate(tmp_path, monkeypatch):
    """G16: force=true on learn() is denied with -32004 for non-operator principal (strict principals dir)."""
    import json
    from pathlib import Path
    import minni.config as cfg_mod
    import minni.principal as pr_mod

    pdir = tmp_path / "principals_force"
    pdir.mkdir()
    # Limited non-operator (canonical name so it gets loaded as stamped)
    principal_file = pdir / "local.json"
    principal_file.write_text(json.dumps({"agent_id": "local", "capabilities": ["search", "learn"]}))
    os.chmod(principal_file, 0o600)

    old_db = cfg_mod.DEFAULT_CONFIG.db_path
    old_pr = pr_mod.PRINCIPALS_DIR
    cfg_mod.DEFAULT_CONFIG.db_path = str(tmp_path / "force_gate.db")
    pr_mod.PRINCIPALS_DIR = pdir
    try:
        from minni.migrations import run_migrations
        from minni.db import SovereignDB
        db = SovereignDB()
        run_migrations(db._get_conn())

        # Call as non-operator (stamped "local" with limited caps)
        resp = _handle_learn({"content": "force attempt by limited local principal", "agent_id": "local", "force": True}, 7)
        err = resp.get("error", {})
        assert err.get("code") == -32004
        assert "operator_only" in str(err.get("message", "")).lower()

        # Verify no durable learning row was created
        with db.cursor() as c:
            cnt = c.execute("SELECT COUNT(*) FROM learnings WHERE content LIKE '%force attempt by limited%'").fetchone()[0]
            assert cnt == 0
    finally:
        cfg_mod.DEFAULT_CONFIG.db_path = old_db
        pr_mod.PRINCIPALS_DIR = old_pr

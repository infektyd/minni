"""M1 (security): migration 013 must not silently reattribute genuine
operator 'main' rows to 'claude-code' on a fresh/un-migrated install.

The original migration 013 SQL unconditionally rewrote
candidate_packets.principal and learnings.agent_id from 'main' to
'claude-code'. That was correct for the specific already-migrated database
it was written against (operator-asserted legacy alias), but wrong in
general: applying it to any un-migrated DB would silently fold a genuine
operator 'main' principal's memory into 'claude-code'.

The fix gates the rewrite behind the MINNI_LEGACY_MAIN_IS_CLAUDE_CODE env
flag in migrations.py (_apply_migration_013_legacy_main_rewrite), applied
as a Python step after the 013 SQL script runs (a .sql migration can't read
env). This test seeds a DB with genuine 'main' rows and confirms:
  (a) without the flag, migration 013 leaves 'main' rows untouched;
  (b) with the flag set, the legacy-alias rewrite still applies (regression
      guard for the intended, opt-in behavior);
  (c) the run is idempotent either way.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))


def _fresh_undermigrated_db(tmp_path, seed_main_rows=True):
    """A DB with the base schema plus every migration through 012 applied,
    but migration 013 NOT yet applied — i.e. a fresh/un-migrated install
    scenario, ready for genuine 'main' rows to be seeded and then 013 run
    against them."""
    import minni.config as cfg_mod
    import minni.db as db_mod
    from minni.migrations import _load_migration_files

    all_versions = [v for v, _ in _load_migration_files()]
    assert 13 in all_versions

    cfg = cfg_mod.SovereignConfig(
        db_path=str(tmp_path / "fresh.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
    )

    # Run migrations normally (via SovereignDB init) up through 012 by
    # temporarily hiding 013 from the loader, so the schema is fully
    # up-to-date except for the 013 rewrite under test.
    import minni.migrations as mig_mod
    real_loader = mig_mod._load_migration_files
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        mig_mod._load_migration_files = lambda: [
            (v, p) for v, p in real_loader() if v != 13
        ]
        db_obj = db_mod.SovereignDB(cfg)
        conn = db_obj._get_conn()
    finally:
        mig_mod._load_migration_files = real_loader
        db_mod._migrations_run = old_flag
    conn.row_factory = sqlite3.Row

    if seed_main_rows:
        conn.execute(
            "INSERT INTO candidate_packets "
            "(principal, workspace_id, layer, privacy_level, content, "
            " evidence_refs, derived_from, instruction_like, status, proposed_at) "
            "VALUES ('main', 'default', NULL, 'safe', 'genuine operator memory', "
            " '[]', NULL, 0, 'proposed', 1.0)"
        )
        conn.execute(
            "INSERT INTO learnings (agent_id, content, category, "
            " confidence, created_at) "
            "VALUES ('main', 'genuine operator learning', 'note', "
            " 0.9, 1.0)"
        )
        conn.commit()

    return conn


def test_migration_013_does_not_reattribute_genuine_main_rows_by_default(tmp_path, monkeypatch):
    from minni.migrations import run_migrations

    monkeypatch.delenv("MINNI_LEGACY_MAIN_IS_CLAUDE_CODE", raising=False)
    conn = _fresh_undermigrated_db(tmp_path)

    run_migrations(conn)  # now applies 013

    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert 13 in applied

    cp_principals = {r["principal"] for r in conn.execute("SELECT principal FROM candidate_packets")}
    learning_agents = {r["agent_id"] for r in conn.execute("SELECT agent_id FROM learnings")}
    assert "main" in cp_principals, "genuine operator 'main' candidate row must survive un-flagged 013"
    assert "claude-code" not in cp_principals
    assert "main" in learning_agents, "genuine operator 'main' learning must survive un-flagged 013"
    assert "claude-code" not in learning_agents
    conn.close()


def test_migration_013_rewrites_main_when_legacy_alias_flag_set(tmp_path, monkeypatch):
    from minni.migrations import run_migrations

    monkeypatch.setenv("MINNI_LEGACY_MAIN_IS_CLAUDE_CODE", "1")
    conn = _fresh_undermigrated_db(tmp_path)

    run_migrations(conn)

    cp_principals = {r["principal"] for r in conn.execute("SELECT principal FROM candidate_packets")}
    learning_agents = {r["agent_id"] for r in conn.execute("SELECT agent_id FROM learnings")}
    assert "main" not in cp_principals
    assert "claude-code" in cp_principals
    assert "main" not in learning_agents
    assert "claude-code" in learning_agents
    conn.close()


def test_migration_013_claudecode_slug_always_folds(tmp_path, monkeypatch):
    """The claudecode -> claude-code fold is not the disputed alias (it's
    never a valid distinct operator principal) so it must always apply,
    flag or no flag."""
    from minni.migrations import run_migrations

    monkeypatch.delenv("MINNI_LEGACY_MAIN_IS_CLAUDE_CODE", raising=False)
    conn = _fresh_undermigrated_db(tmp_path, seed_main_rows=False)
    conn.execute(
        "INSERT INTO candidate_packets "
        "(principal, workspace_id, layer, privacy_level, content, "
        " evidence_refs, derived_from, instruction_like, status, proposed_at) "
        "VALUES ('claudecode', 'default', NULL, 'safe', 'legacy slug row', "
        " '[]', NULL, 0, 'proposed', 1.0)"
    )
    conn.commit()

    run_migrations(conn)

    cp_principals = {r["principal"] for r in conn.execute("SELECT principal FROM candidate_packets")}
    assert "claudecode" not in cp_principals
    assert "claude-code" in cp_principals
    conn.close()

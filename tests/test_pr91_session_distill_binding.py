"""PR91-2: session_distillation must bind to a single agent, never query
globally; the CLI consolidation entrypoint must propagate --agent/MINNI_AGENT_ID.
"""

from __future__ import annotations

import os
import time
import types

import minni.afm_passes.session_distillation as sd


def _capture_agent(monkeypatch):
    seen = {}

    def _fake_events(db, lookback_hours, agent_id=None):
        seen["events_agent"] = agent_id
        return []

    def _fake_docs(db, lookback_hours, agent_id=None, vault_path=None):
        seen["docs_agent"] = agent_id
        seen["docs_vault"] = vault_path
        return []

    monkeypatch.setattr(sd, "_recent_events", _fake_events)
    monkeypatch.setattr(sd, "_recent_raw_docs", _fake_docs)
    return seen


def _run(monkeypatch):
    cfg = types.SimpleNamespace(afm_loop_schedule={})
    return sd.run(db=object(), config=cfg, dry_run=True, trace_id="t")


def test_unbound_run_falls_back_to_unknown_not_global(monkeypatch):
    monkeypatch.delenv("MINNI_AGENT_ID", raising=False)
    seen = _capture_agent(monkeypatch)
    _run(monkeypatch)
    # The load-bearing assertion: NOT None (which queried globally before).
    assert seen["events_agent"] == "unknown"
    assert seen["docs_agent"] == "unknown"


def test_env_agent_binds_scope(monkeypatch):
    monkeypatch.setenv("MINNI_AGENT_ID", "codex")
    seen = _capture_agent(monkeypatch)
    _run(monkeypatch)
    assert seen["events_agent"] == "codex"
    assert seen["docs_agent"] == "codex"


def test_blank_env_agent_falls_back_to_unknown(monkeypatch):
    monkeypatch.setenv("MINNI_AGENT_ID", "   ")
    seen = _capture_agent(monkeypatch)
    _run(monkeypatch)
    assert seen["events_agent"] == "unknown"


def test_cli_compile_propagates_agent_to_env(monkeypatch):
    """sovereign_memory.py compile --agent X exports MINNI_AGENT_ID before the
    pass runs (verified even when the AFM loop is disabled and it returns early)."""
    import minni.sovereign_memory as sovereign_memory

    monkeypatch.delenv("MINNI_AGENT_ID", raising=False)
    # Force the AFM loop "disabled" path so cmd_compile prints the unavailable
    # summary and returns AFTER parsing+propagating the agent — no heavy pass run.
    monkeypatch.setattr(sovereign_memory.DEFAULT_CONFIG, "afm_loop_schedule", {}, raising=False)
    sovereign_memory.cmd_compile(["--pass", "session_distillation", "--agent", "codex", "--dry-run"])
    assert os.environ.get("MINNI_AGENT_ID") == "codex"


def test_agent_id_kwarg_binds_ahead_of_env(monkeypatch):
    monkeypatch.setenv("MINNI_AGENT_ID", "env-agent")
    seen = _capture_agent(monkeypatch)
    cfg = types.SimpleNamespace(afm_loop_schedule={})
    sd.run(db=object(), config=cfg, dry_run=True, trace_id="t", agent_id="stamped-agent")
    assert seen["events_agent"] == "stamped-agent"
    assert seen["docs_agent"] == "stamped-agent"


def test_session_distill_inputs_redact_event_content(tmp_path, monkeypatch):
    """Finding 9: MCP/model-visible inputs must not include episodic event bodies."""
    import minni.db as db_mod
    from minni.config import SovereignConfig

    monkeypatch.delenv("MINNI_AGENT_ID", raising=False)
    cfg = SovereignConfig(db_path=str(tmp_path / "sd.db"), afm_loop_schedule={})
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    now = __import__("time").time()
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO episodic_events
            (agent_id, event_type, content, created_at)
            VALUES ('codex', 'session', 'SECRET session payload must not leak', ?)
            """,
            (now,),
        )
    result = sd.run(
        db_obj, cfg, dry_run=True, trace_id="redact", agent_id="codex"
    )
    events = result["inputs"]["events"]
    assert events, events
    assert "content" not in events[0]
    assert events[0]["content_redacted"] is True
    assert events[0]["content_chars"] > 0
    dumped = __import__("json").dumps(result["inputs"])
    assert "SECRET session payload" not in dumped


def test_recent_raw_docs_does_not_return_foreign_agent_raw_paths(tmp_path):
    """Finding 9: bound raw-doc query must not OR-match other agents' rows."""
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "raw.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    now = __import__("time").time()
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, sigil, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'victim', '?', ?, ?, 'accepted', 'safe')
            """,
            ("victim-vault/raw/secret-project-name.md", now, now),
        )
        c.execute(
            """
            INSERT INTO documents
            (path, agent, sigil, last_modified, indexed_at, page_status, privacy_level)
            VALUES (?, 'attacker', '?', ?, ?, 'accepted', 'safe')
            """,
            ("attacker-vault/raw/own-note.md", now, now),
        )
    rows = sd._recent_raw_docs(db_obj, lookback_hours=24, agent_id="attacker")
    paths = [r["path"] for r in rows]
    assert paths == ["attacker-vault/raw/own-note.md"]


def test_raw_docs_include_own_vault_unknown_but_not_foreign(tmp_path):
    """PR167 review: frontmatter-less raw notes are indexed agent='unknown';
    they must stay visible to their own vault's distillation (vault-path
    containment) without re-opening other vaults' unattributed raw docs."""
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "raw.db"), afm_loop_schedule={})
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    own_vault = tmp_path / "codex-vault"
    foreign_vault = tmp_path / "other-vault"
    now = time.time()
    rows = [
        (str(own_vault / "raw" / "own-unknown.md"), "unknown"),
        (str(own_vault / "raw" / "own-stamped.md"), "codex"),
        (str(foreign_vault / "raw" / "foreign-unknown.md"), "unknown"),
        (str(foreign_vault / "raw" / "foreign-stamped.md"), "gemini"),
    ]
    with db_obj.cursor() as c:
        for path, agent in rows:
            c.execute(
                "INSERT INTO documents (path, agent, indexed_at, last_modified) VALUES (?, ?, ?, ?)",
                (path, agent, now, now),
            )

    docs = sd._recent_raw_docs(
        db_obj, lookback_hours=24, agent_id="codex", vault_path=str(own_vault)
    )
    paths = {d["path"] for d in docs}
    assert str(own_vault / "raw" / "own-unknown.md") in paths
    assert str(own_vault / "raw" / "own-stamped.md") in paths
    assert str(foreign_vault / "raw" / "foreign-unknown.md") not in paths
    assert str(foreign_vault / "raw" / "foreign-stamped.md") not in paths

    # Without a vault path, unknown raw docs stay excluded (fail closed).
    docs_no_vault = sd._recent_raw_docs(db_obj, lookback_hours=24, agent_id="codex")
    paths_no_vault = {d["path"] for d in docs_no_vault}
    assert str(own_vault / "raw" / "own-unknown.md") not in paths_no_vault

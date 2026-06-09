"""Tests for afm_passes.inbox_ingest — draining inbox stop-candidates into
candidate_packets so the AFM consolidation loop sees them.

Follows the test_pr12_afm_loop.py harness pattern (isolated tmp DB + config).
"""

import json
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


def _write_inbox_file(inbox, name, doc):
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_text(json.dumps(doc), encoding="utf-8")


def _stop_doc(candidates, **overrides):
    doc = {
        "slug": "s",
        "createdAt": "2026-06-06T12:00:00.000Z",
        "kind": "codex_stop_candidates",
        "agent_id": "codex",
        "workspace_id": "/work/proj",
        "candidates": candidates,
        "log_only": [],
        "expires": [],
        "do_not_store": [],
        "last_task": "t",
    }
    doc.update(overrides)
    return doc


def _count_proposed(db_obj, principal="codex"):
    with db_obj.cursor() as c:
        c.execute(
            "SELECT COUNT(*) AS n FROM candidate_packets WHERE principal=? AND status='proposed'",
            (principal,),
        )
        return dict(c.fetchone())["n"]


def test_ingest_inserts_eligible_candidates_as_proposed(tmp_path):
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _stop_doc(["learn this fact about builds"]))

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1, res
    assert _count_proposed(db_obj) == 1

    with db_obj.cursor() as c:
        c.execute("SELECT * FROM candidate_packets WHERE principal='codex'")
        row = dict(c.fetchone())
    assert row["status"] == "proposed"
    assert row["workspace_id"] == "/work/proj"
    assert row["instruction_like"] == 0
    df = json.loads(row["derived_from"])
    assert df["source"] == "inbox" and df["inbox_file"] == "a.json"


def test_ingest_is_idempotent(tmp_path):
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _stop_doc(["a durable lesson here"]))

    first = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    second = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["already_present"] == 1
    assert _count_proposed(db_obj) == 1


def test_ingest_idempotent_after_status_change(tmp_path):
    """Re-run must be a no-op even after the loop resolves the row."""
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _stop_doc(["promoted lesson text"]))

    ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    with db_obj.cursor() as c:
        c.execute("UPDATE candidate_packets SET status='accepted' WHERE principal='codex'")

    again = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert again["inserted"] == 0
    assert again["already_present"] == 1


def test_ingest_respects_bool_flags_and_list_membership(tmp_path):
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"

    # boolean-true flag => whole file skipped
    _write_inbox_file(inbox, "bool.json",
                      _stop_doc(["should be skipped"], log_only=True))
    # candidate string listed in log_only / do_not_store => that candidate skipped
    _write_inbox_file(inbox, "list.json", _stop_doc(
        ["keep me", "drop via log_only", "drop via dns"],
        log_only=["drop via log_only"],
        do_not_store=["drop via dns"],
    ))
    # empty / blank candidates skipped
    _write_inbox_file(inbox, "empty.json", _stop_doc(["", "   "]))

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1, res
    with db_obj.cursor() as c:
        c.execute("SELECT content FROM candidate_packets WHERE principal='codex'")
        contents = [dict(r)["content"] for r in c.fetchall()]
    assert contents == ["keep me"]


def test_ingest_ignores_non_stop_kinds(tmp_path):
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "handoff.json", {
        "kind": "codex_precompact_handoff",
        "agent_id": "codex",
        "candidates": ["should not be ingested"],
    })

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0
    assert _count_proposed(db_obj) == 0


def _cc_stop_doc(candidates, **overrides):
    """Claude Code hook shape: NO `kind` field, no `agent_id`; carries the
    `slug`/`last_task` session markers and a `candidates` list."""
    doc = {
        "slug": "f373b502",
        "createdAt": "2026-06-08T12:00:00.000Z",
        "candidates": candidates,
        "log_only": [],
        "expires": [],
        "do_not_store": [],
        "last_task": "f373b502",
    }
    doc.update(overrides)
    return doc


def test_ingests_kindless_claude_code_shape_attributed_to_vault_agent(tmp_path):
    """Kind-less CC stop-candidate files must be ingested and attributed to the
    agent that owns the vault (claudecode-vault -> 'claudecode'), not 'codex'."""
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(inbox, "cc.json", _cc_stop_doc(["a durable claude code lesson"]))

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1, res
    assert _count_proposed(db_obj, principal="claudecode") == 1
    assert _count_proposed(db_obj, principal="codex") == 0
    with db_obj.cursor() as c:
        c.execute("SELECT derived_from FROM candidate_packets WHERE principal='claudecode'")
        df = json.loads(dict(c.fetchone())["derived_from"])
    assert df["source"] == "inbox" and df["inbox_file"] == "cc.json"
    # Provenance must record what the file declared — kind-less CC files must
    # NOT be stamped with the codex-specific kind (model-agnostic logic).
    assert df["kind"] is None


def test_ingests_neutral_stop_candidates_kind(tmp_path):
    """The canonical agent-neutral kind 'stop_candidates' must be accepted
    (hooks are being migrated off the legacy codex-prefixed tag)."""
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "gemini-vault" / "inbox"
    doc = _cc_stop_doc(["a neutral-kind lesson"], kind="stop_candidates")
    _write_inbox_file(inbox, "neutral.json", doc)

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1, res
    assert _count_proposed(db_obj, principal="gemini") == 1
    with db_obj.cursor() as c:
        c.execute("SELECT derived_from FROM candidate_packets WHERE principal='gemini'")
        df = json.loads(dict(c.fetchone())["derived_from"])
    assert df["kind"] == "stop_candidates"


def test_kindless_without_stop_shape_is_ignored(tmp_path):
    """Kind-less JSON that lacks the stop-candidate shape must NOT be ingested."""
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    # has candidates but missing slug/last_task -> not the stop-candidate shape
    _write_inbox_file(inbox, "weird.json", {"candidates": ["nope"], "foo": "bar"})

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0, res
    assert _count_proposed(db_obj, principal="claudecode") == 0


def test_dry_run_writes_nothing(tmp_path):
    from afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _stop_doc(["dry run candidate"]))

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=True)
    assert res["would_insert"] == 1
    assert res["inserted"] == 0
    assert _count_proposed(db_obj) == 0


def test_ingested_rows_are_selected_by_consolidation_pass(tmp_path):
    """The rows this module inserts must be picked up by the consolidation pass,
    which selects purely on status='proposed' (no principal filter)."""
    from afm_passes.inbox_ingest import ingest
    from afm_passes.consolidation import run as consolidation_run

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "a.json",
                      _stop_doc(["a clear durable lesson worth promoting"]))

    # consolidation pass reads consolidation_actions (a migrations table not in
    # the base schema); create it so the pass can run in isolation.
    with db_obj.cursor() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS consolidation_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                source_event_id INTEGER,
                target_learning_id INTEGER,
                superseded_learning_id INTEGER,
                claim TEXT,
                category TEXT,
                confidence REAL,
                status TEXT DEFAULT 'pending',
                detail TEXT,
                created_at REAL NOT NULL
            )
            """
        )

    ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    result = consolidation_run(db_obj, cfg, vault_path=cfg.vault_path, dry_run=True)
    assert result["summary"]["total_proposed"] >= 1
    assert result["summary"]["examined"] >= 1

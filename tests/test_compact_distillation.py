"""Tests for afm_passes.compact_distillation — distilling raw compaction
summaries (inbox kind 'compact_summary', harvested by the platform hooks)
into proposed candidate_packets on the AFM-loop consolidation tick.

Follows the test_inbox_ingest.py harness pattern (isolated tmp DB + config).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


SUMMARY_BODY = (
    "1. Primary Request and Intent:\n"
    "   User asked to fix the daemon fd leak and deploy the fix everywhere.\n"
    "2. Key Technical Concepts:\n"
    "   SovereignDB caches one sqlite connection per thread; the RPC pool\n"
    "   multiplies open fds past the launchd soft limit.\n"
    "3. Errors and fixes:\n"
    "   launchctl bootstrap error 5 right after bootout is a teardown race;\n"
    "   sleeping two seconds and retrying succeeds. See /Users/someone/x.log\n"
    "4. All user messages:\n"
    "   - push the work\n"
    "   - merge it and deploy\n"
    "5. Pending Tasks:\n"
    "   None.\n"
)


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

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


def _summary_doc(**overrides):
    doc = {
        "slug": "s",
        "createdAt": "2026-07-30T12:00:00.000Z",
        "kind": "compact_summary",
        "agent_id": "codex",
        "workspace_id": "/work/proj",
        "summary_text": SUMMARY_BODY,
        "summary_id": "uuid-1",
        "platform": "codex",
        "session_id": "sess-1",
    }
    doc.update(overrides)
    return doc


def _proposed_rows(db_obj, principal="codex"):
    with db_obj.cursor() as c:
        c.execute(
            "SELECT content, derived_from, status FROM candidate_packets WHERE principal = ?",
            (principal,),
        )
        return [dict(r) if hasattr(r, "keys") else {"content": r[0], "derived_from": r[1], "status": r[2]} for r in c.fetchall()]


def test_deterministic_distill_proposes_sectioned_candidates(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "2026-07-30-abc-sess-1.json", _summary_doc())

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 3  # mechanics sections (user messages, pending) excluded
    assert res["afm_sections"] == 0
    rows = _proposed_rows(db_obj)
    assert len(rows) == 3
    joined = "\n".join(r["content"] for r in rows)
    assert "teardown race" in joined
    assert "All user messages" not in joined
    assert "Pending Tasks" not in joined
    # Raw local paths never reach the proposal queue.
    assert "/Users/someone" not in joined
    assert "[local-path]" in joined
    for r in rows:
        assert r["status"] == "proposed"
        df = json.loads(r["derived_from"])
        assert df["source"] == "inbox"
        assert df["channel"] == "compact_distillation"
        assert df["kind"] == "compact_summary"
        assert df["inbox_file"] == "2026-07-30-abc-sess-1.json"
        assert df["afm_distilled"] is False


def test_idempotent_across_runs(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "f.json", _summary_doc())

    first = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    second = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert first["inserted"] == 3
    assert second["inserted"] == 0
    assert second["files_already_done"] == 1
    assert len(_proposed_rows(db_obj)) == 3


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "f.json", _summary_doc())

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=True)
    assert res["would_insert"] == 3
    assert res["inserted"] == 0
    assert _proposed_rows(db_obj) == []
    # And the wet run afterwards still inserts.
    assert distill(db_obj, cfg, inboxes=[inbox], dry_run=False)["inserted"] == 3


def test_skips_agent_mismatch_and_empty_and_foreign_kinds(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "mismatch.json", _summary_doc(agent_id="grok-build"))
    _write_inbox_file(inbox, "empty.json", _summary_doc(summary_text="   "))
    _write_inbox_file(inbox, "stop.json", {"kind": "stop_candidates", "candidates": ["x"]})

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0
    assert res["skipped"] == {"_agent_mismatch": 1, "_empty_summary": 1}
    assert res["files_scanned"] == 2  # foreign kinds are not compact files at all


def test_unsectioned_summary_falls_back_to_single_candidate(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(
        inbox, "flat.json",
        _summary_doc(summary_text="One paragraph of genuinely useful session findings about the migration."),
    )

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1
    (row,) = _proposed_rows(db_obj)
    assert row["content"].startswith("Compaction summary — Session summary:")


def test_afm_path_uses_session_distill_with_fallback(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    class FakeResult:
        def __init__(self, ok, data):
            self.ok = ok
            self.data = data

    class FakeChain:
        def __init__(self):
            self.calls = []

        def native_op(self, operation, payload, timeout=2.0):
            self.calls.append((operation, payload["text"][:40]))
            # Distill only the first section; miss the rest → fallback.
            if "Primary Request" in payload["text"]:
                return FakeResult(True, {
                    "title": "FD leak fix",
                    "assertion": "Cap RPC workers and share DB instances per path",
                    "appliesWhen": "daemon under multi-agent load",
                })
            return FakeResult(False, {})

    chain = FakeChain()
    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "native")
    monkeypatch.setattr(mod, "default_provider_chain", lambda: chain)
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "f.json", _summary_doc())

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["afm_mode"] == "native"
    assert res["afm_sections"] == 1
    assert all(op == "session_distill" for op, _ in chain.calls)
    rows = _proposed_rows(db_obj)
    afm_rows = [r for r in rows if json.loads(r["derived_from"])["afm_distilled"]]
    assert len(afm_rows) == 1
    assert afm_rows[0]["content"] == (
        "FD leak fix: Cap RPC workers and share DB instances per path "
        "(applies when: daemon under multi-agent load)"
    )
    # Missed sections still arrive via the deterministic fallback.
    assert len(rows) == 3


def test_derived_from_matches_inbox_archive_lifecycle_key(tmp_path, monkeypatch):
    """Once every distilled candidate resolves, inbox_archive must recognize
    the source file — the shape contract that makes archival free."""
    from minni.afm_passes.compact_distillation import distill
    from minni.afm_passes.inbox_archive import _derived_inbox_file
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "arch.json", _summary_doc())
    distill(db_obj, cfg, inboxes=[inbox], dry_run=False)

    for row in _proposed_rows(db_obj):
        assert _derived_inbox_file(row["derived_from"]) == "arch.json"

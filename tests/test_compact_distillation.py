"""Tests for afm_passes.compact_distillation — distilling raw compaction
summaries (inbox kind 'compact_summary', harvested by the platform hooks)
into proposed candidate_packets on the AFM-loop consolidation tick, with
audience routing: only knowledge-bearing sections become SHARED candidates,
everything else stays personal in the agent's own vault session note.

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
        "summary_sha1": "abcdef1234567890",
    }
    doc.update(overrides)
    return doc


def _session_notes(tmp_path, vault="codex-vault"):
    d = tmp_path / vault / "wiki" / "sessions"
    return sorted(d.glob("*.md")) if d.is_dir() else []


def _proposed_rows(db_obj, principal="codex"):
    with db_obj.cursor() as c:
        c.execute(
            "SELECT content, derived_from, status FROM candidate_packets WHERE principal = ?",
            (principal,),
        )
        return [dict(r) if hasattr(r, "keys") else {"content": r[0], "derived_from": r[1], "status": r[2]} for r in c.fetchall()]


def test_deterministic_distill_proposes_shared_sections_only(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "2026-07-30-abc-sess-1.json", _summary_doc())

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    # Shared: Key Technical Concepts + Errors and fixes. Personal: Primary
    # Request and Intent. Excluded from both: all user messages, pending tasks.
    assert res["inserted"] == 2
    assert res["shared_candidates"] == 2
    assert res["personal_sections"] == 1
    assert res["afm_sections"] == 0
    rows = _proposed_rows(db_obj)
    assert len(rows) == 2
    joined = "\n".join(r["content"] for r in rows)
    assert "teardown race" in joined
    assert "SovereignDB caches one sqlite connection" in joined
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
        assert df["audience"] == "shared"
        assert df["inbox_file"] == "2026-07-30-abc-sess-1.json"
        assert df["afm_distilled"] is False


def test_personal_sections_never_reach_candidate_packets(tmp_path, monkeypatch):
    """The live regression this routing fixes: session-personal narration was
    landing in the shared pool and getting auto-accepted."""
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "f.json", _summary_doc(summary_text=(
        "1. Primary Request and Intent:\n"
        "   User asked to fix the daemon fd leak and deploy the fix everywhere.\n"
        "2. Files and Code Sections:\n"
        "   The codebase is in a clean state; account switching is per repo.\n"
        "3. Errors and fixes:\n"
        "   launchctl bootstrap error 5 right after bootout is a teardown race.\n"
    )))

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1
    assert res["personal_sections"] == 2
    joined = "\n".join(r["content"] for r in _proposed_rows(db_obj))
    assert "teardown race" in joined
    assert "clean state" not in joined
    assert "account switching" not in joined
    assert "User asked to fix" not in joined
    # …but the personal content is preserved for this agent alone.
    (note,) = _session_notes(tmp_path)
    body = note.read_text(encoding="utf-8")
    assert "clean state" in body
    assert "account switching" in body


def test_idempotent_across_runs(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "f.json", _summary_doc())

    first = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    second = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert first["inserted"] == 2
    assert first["vault_notes_written"] == 1
    assert first["archived_with_shared"] == 1
    assert second["inserted"] == 0
    assert second["vault_notes_written"] == 0
    # The file was archived once its candidates were inserted, so the second
    # run finds nothing to scan at all (not a same-file "already done" skip).
    assert second["files_scanned"] == 0
    assert second["files_already_done"] == 0
    assert len(_proposed_rows(db_obj)) == 2
    assert len(_session_notes(tmp_path)) == 1


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "f.json", _summary_doc())

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=True)
    assert res["would_insert"] == 2
    assert res["inserted"] == 0
    assert res["vault_notes_written"] == 0
    assert _proposed_rows(db_obj) == []
    assert _session_notes(tmp_path) == []
    # And the wet run afterwards still inserts.
    assert distill(db_obj, cfg, inboxes=[inbox], dry_run=False)["inserted"] == 2


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


FLAT_SUMMARY = "One paragraph of genuinely useful session findings about the migration."


def test_unsectioned_summary_without_afm_stays_personal(tmp_path, monkeypatch):
    """No AFM means no crisp assertion, so the whole-body fallback is personal:
    vault note only, and the file is archived because nothing keys idempotency."""
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "flat.json", _summary_doc(summary_text=FLAT_SUMMARY))

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0
    assert res["personal_sections"] == 1
    assert res["vault_notes_written"] == 1
    assert _proposed_rows(db_obj) == []
    (note,) = _session_notes(tmp_path)
    assert FLAT_SUMMARY in note.read_text(encoding="utf-8")


def test_unsectioned_summary_upgraded_to_shared_when_afm_distills(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    class FakeResult:
        def __init__(self, ok, data):
            self.ok = ok
            self.data = data

    class FakeChain:
        def native_op(self, operation, payload, timeout=2.0):
            return FakeResult(True, {
                "title": "Migration findings",
                "assertion": "Run the schema migration before restarting the daemon",
            })

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "native")
    monkeypatch.setattr(mod, "default_provider_chain", FakeChain)
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "flat.json", _summary_doc(summary_text=FLAT_SUMMARY))

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1
    assert res["personal_sections"] == 0
    assert res["archived_zero_shared"] == 0
    assert res["archived_with_shared"] == 1
    (row,) = _proposed_rows(db_obj)
    assert row["content"] == (
        "Migration findings: Run the schema migration before restarting the daemon"
    )
    assert json.loads(row["derived_from"])["audience"] == "shared"
    # Candidate rows now carry the idempotency key, so the file is archived
    # immediately rather than waiting on the resolve-time drain lifecycle.
    assert not (inbox / "flat.json").exists()
    assert (inbox / ".archive" / "flat.json").is_file()


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
            # Distill only the first shared section; miss the rest → fallback.
            if "Key Technical Concepts" in payload["text"]:
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
    # AFM is never spent on personal sections.
    assert not any("Primary Request" in text for _, text in chain.calls)
    rows = _proposed_rows(db_obj)
    afm_rows = [r for r in rows if json.loads(r["derived_from"])["afm_distilled"]]
    assert len(afm_rows) == 1
    assert afm_rows[0]["content"] == (
        "FD leak fix: Cap RPC workers and share DB instances per path "
        "(applies when: daemon under multi-agent load)"
    )
    # Missed shared sections still arrive via the deterministic fallback.
    assert len(rows) == 2


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


def test_session_note_location_content_and_idempotency(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "note.json", _summary_doc())

    distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    (note,) = _session_notes(tmp_path)
    # Name is derived from the document's own timestamp/ids, never wall clock.
    assert note.name == "20260730-compact-sess-1-abcdef12.md"
    assert note.parent == tmp_path / "codex-vault" / "wiki" / "sessions"
    body = note.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "type: session" in body
    assert "platform: codex" in body
    assert "session_id: sess-1" in body
    assert "summary_sha1: abcdef1234567890" in body
    assert "audience: personal" in body
    assert "source: compact_distillation:note.json" in body
    assert "created: '2026-07-30T12:00:00.000Z'" in body
    # FULL body, including the sections that never became candidates…
    assert "User asked to fix the daemon fd leak" in body
    assert "push the work" in body
    assert "teardown race" in body
    # …still redacted.
    assert "/Users/someone" not in body
    assert "[local-path]" in body

    mtime = note.stat().st_mtime_ns
    again = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert again["vault_notes_written"] == 0
    assert len(_session_notes(tmp_path)) == 1
    assert note.stat().st_mtime_ns == mtime


def test_zero_shared_file_is_archived_not_deleted(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "personal.json", _summary_doc(summary_text=(
        "1. Primary Request and Intent:\n"
        "   User asked me to keep working through the backlog.\n"
        "2. Current Work:\n"
        "   Halfway through the third item.\n"
    )))

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0
    assert res["vault_notes_written"] == 1
    assert res["archived_zero_shared"] == 1
    assert res["skipped"] == {"_no_candidates": 1}
    assert not (inbox / "personal.json").exists()
    assert (inbox / ".archive" / "personal.json").is_file()

    # Nothing left to rescan on the next tick.
    second = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert second["files_scanned"] == 0
    assert second["vault_notes_written"] == 0
    assert second["archived_zero_shared"] == 0


def test_file_with_shared_candidates_is_archived_after_insert(tmp_path, monkeypatch):
    """The gap this fix closes: a file that DOES yield shared candidates must
    be archived once those candidates are durably inserted, exactly like the
    zero-shared path — otherwise it sits in the inbox forever, re-scanned and
    skipped every tick by the (path.name, 0) idempotency key with no lifecycle
    event ever draining it (consolidation auto-accepts these candidates
    without going through the resolve-time drain-on-resolution path)."""
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "shared.json", _summary_doc())

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 2
    assert res["shared_candidates"] == 2
    assert res["archived_zero_shared"] == 0
    assert res["archived_with_shared"] == 1
    assert not (inbox / "shared.json").exists()
    assert (inbox / ".archive" / "shared.json").is_file()

    # Candidate rows persist across the archive; idempotency stays keyed on
    # them, not on file presence — a re-run inserts nothing new and finds
    # nothing left to scan.
    second = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert second["files_scanned"] == 0
    assert second["inserted"] == 0
    assert second["archived_with_shared"] == 0
    assert len(_proposed_rows(db_obj)) == 2


def test_legacy_stuck_file_is_swept_on_next_tick(tmp_path, monkeypatch):
    """Regression coverage for the live gap: a file processed by a pre-fix
    daemon build already has its candidate_index=0 row in the DB (so it hits
    the file-level idempotency branch, not the fresh insert-then-archive
    path) but was never archived. The very next tick after this fix ships
    must sweep it — this is how the two already-stuck live inbox files drain
    without any hand-edit to the vault."""
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "stuck.json", _summary_doc())

    # Simulate the pre-fix daemon: candidates land, file is never archived.
    first = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert first["inserted"] == 2
    # Undo this fix's own archival to reproduce the pre-fix stuck state.
    (inbox / ".archive" / "stuck.json").rename(inbox / "stuck.json")
    assert (inbox / "stuck.json").is_file()

    swept = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert swept["files_already_done"] == 1
    assert swept["inserted"] == 0
    assert swept["archived_with_shared"] == 1
    assert not (inbox / "stuck.json").exists()
    assert (inbox / ".archive" / "stuck.json").is_file()
    # No duplicate rows were ever inserted.
    assert len(_proposed_rows(db_obj)) == 2


def test_dry_run_does_not_archive_file_with_shared_candidates(tmp_path, monkeypatch):
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "shared.json", _summary_doc())

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=True)
    assert res["would_insert"] == 2
    assert res["archived_with_shared"] == 0
    assert (inbox / "shared.json").is_file()
    assert not (inbox / ".archive").exists()

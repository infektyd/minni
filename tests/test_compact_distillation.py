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
    # AFM-9 (#230): `_other_kind` is new. A foreign-kind file used to be dropped
    # on a bare `continue` with no counter at all, which is what made "how much
    # distillation input is being discarded?" unanswerable from any surface.
    # Foreign kinds are a legitimate skip, not a fault — but they are now
    # counted rather than invisible.
    assert res["skipped"] == {
        "_agent_mismatch": 1,
        "_empty_summary": 1,
        "_other_kind": 1,
    }
    assert res["files_scanned"] == 2  # foreign kinds are not compact files at all


def test_slug_alias_stamped_agent_id_is_not_compact_mismatch(tmp_path, monkeypatch):
    """Parity with test_slug_alias_stamped_agent_id_is_not_mismatch:
    leftover agent_id=agy in agy-vault (canonical gemini) is usable.
    Both classify_unusable_compact_file and distill must agree — a raw
    file_agent != principal compare would skip/quarantine the leftover."""
    from minni.afm_passes.compact_distillation import (
        classify_unusable_compact_file,
        distill,
    )
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "agy-vault" / "inbox"
    _write_inbox_file(inbox, "stamped.json", _summary_doc(agent_id="agy"))

    assert classify_unusable_compact_file(inbox / "stamped.json", "gemini") is None

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["skipped"].get("_agent_mismatch", 0) == 0, res
    assert res["inserted"] > 0, res


def test_distill_in_txn_canonicalizes_alias_wanted_against_leftover_gemini(
    tmp_path, monkeypatch
):
    """In-txn compact keys must use _make_inbox_key like inbox_ingest.

    Leftover candidate_packets.principal='gemini' for session.json index 0;
    distill(..., fallback_principal='agy') on a non-*-vault inbox so
    _principal_for_inbox leaves principal='agy'; kind-less agent_id skips
    the mismatch gate; monkeypatch-empty _existing_keys forces the in-txn
    path. Family scan finds the gemini row, but wanted (agy, file, 0) misses
    because _parse_inbox_key always returns canonical key[0]. Without the
    operator CASE UNIQUE that INSERT would mint an agy twin.
    """
    import time

    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    monkeypatch.setattr(mod, "default_provider_chain", lambda: None)
    monkeypatch.setattr(mod, "_existing_keys", lambda db, principals=None: set())
    monkeypatch.setattr(mod, "_fills_for_file", lambda db, principal, inbox_file: [])

    db_obj, cfg = _make_db(tmp_path)
    leftover = (
        "Compaction summary — Key technical concepts: "
        "race on bootout after launchctl error 5"
    )
    derived = json.dumps(
        {
            "source": "inbox",
            "inbox_file": "session.json",
            "candidate_index": 0,
            "kind": "compact_summary",
        }
    )
    with db_obj.transaction() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             evidence_refs, derived_from, instruction_like, status, proposed_at)
            VALUES ('gemini', 'default', NULL, 'safe', ?, '[]', ?, 0, 'proposed', ?)
            """,
            (leftover, derived, time.time()),
        )

    inbox = tmp_path / "inbox"
    _write_inbox_file(
        inbox,
        "session.json",
        _summary_doc(
            agent_id="",
            summary_text=(
                "1. Key technical concepts:\n"
                "race on bootout after launchctl error 5\n\n"
                "2. All user messages:\n"
                "please fix it\n"
            ),
        ),
    )

    res = distill(
        db_obj, cfg, inboxes=[inbox], fallback_principal="agy", dry_run=False
    )
    assert res["inserted"] == 0, res
    with db_obj.cursor() as c:
        c.execute(
            "SELECT principal FROM candidate_packets ORDER BY candidate_id"
        )
        principals = [dict(r)["principal"] for r in c.fetchall()]
    assert principals == ["gemini"], principals


THREE_SHARED_BODY = (
    "1. Key Technical Concepts:\n"
    "   SovereignDB caches one sqlite connection per thread so the RPC pool\n"
    "   multiplies open fds past the launchd soft limit.\n"
    "2. Errors and fixes:\n"
    "   launchctl bootstrap error 5 right after bootout is a teardown race;\n"
    "   sleeping two seconds and retrying succeeds.\n"
    "3. Decisions:\n"
    "   Keep the unique inbox key canonical so leftover agy rows merge.\n"
)


def test_leftover_alias_index0_does_not_archive_compact_without_extra_fills(
    tmp_path, monkeypatch
):
    """Leftover agy index 0 must not treat a later 3-section compact_summary
    as fully received. File-key0 UNIQUE/family skip used to archive the live
    file before indices 1–2 were inserted.
    """
    import time

    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    leftover = (
        "Key technical concepts: leftover alias fill occupying index 0 only"
    )
    derived = json.dumps(
        {
            "source": "inbox",
            "channel": "compact_distillation",
            "inbox_file": "session.json",
            "candidate_index": 0,
            "kind": "compact_summary",
        }
    )
    with db_obj.transaction() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             evidence_refs, derived_from, instruction_like, status, proposed_at)
            VALUES ('agy', 'default', NULL, 'safe', ?, '[]', ?, 0, 'proposed', ?)
            """,
            (leftover, derived, time.time()),
        )

    inbox = tmp_path / "agy-vault" / "inbox"
    _write_inbox_file(
        inbox,
        "session.json",
        _summary_doc(agent_id="agy", summary_text=THREE_SHARED_BODY),
    )

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    # Occupied leftover 0 is not compact section 0: missing=[1,2] still
    # extras-at-next-idx the divergent qty (SovereignDB…) so it is not
    # archived unmerged. expected_keys must not treat leftover 0 as the fill.
    assert res["inserted"] == 3, res
    with db_obj.cursor() as c:
        c.execute(
            "SELECT content, principal, derived_from FROM candidate_packets "
            "ORDER BY candidate_id"
        )
        rows = [dict(r) for r in c.fetchall()]
    by_idx = {
        json.loads(r["derived_from"]).get("candidate_index"): r for r in rows
    }
    assert sorted(by_idx) == [0, 1, 2, 3], by_idx
    assert leftover in by_idx[0]["content"]
    assert by_idx[0]["principal"] == "agy"
    qty = [r for r in rows if "SovereignDB caches" in r["content"]]
    assert qty, [r["content"] for r in rows]
    qty_idx = json.loads(qty[0]["derived_from"]).get("candidate_index")
    assert qty_idx not in {0}, qty_idx
    assert {r["principal"] for r in rows[1:]} == {"gemini"}
    assert not (inbox / "session.json").exists()
    assert (inbox / ".archive" / "session.json").is_file()


def test_unique_skip_does_not_archive_compact_file_before_insert(
    tmp_path, monkeypatch
):
    """In-txn UNIQUE/key skip of leftover index 0 used to treat the hit as
    already_present without comparing content_sha1, then archive because
    expected_keys ⊆ durable_keys as index tuples (leftover 0 covers slot 0
    even when distilled section-0 qty never inserted).

    Emptying _fills_for_file disables the pre-scan occupancy extras path so
    this pin is the INSERT txn itself: compare sha, extras-at-next-idx when
    divergent, and archive only once this file's distilled shas are in
    candidate_packets. Leftover 0 + THREE_SHARED_BODY section 0 body must
    land at a new index before the live file is renamed into .archive.
    """
    import time

    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    monkeypatch.setattr(mod, "_existing_keys", lambda db, principals=None: set())
    # Force the in-txn UNIQUE skip of leftover index 0 rather than the
    # pre-scan extras path (which consults _fills_for_file occupancy).
    monkeypatch.setattr(mod, "_fills_for_file", lambda db, principal, inbox_file: [])

    db_obj, cfg = _make_db(tmp_path)
    leftover = "Key technical concepts: in-txn unique skip of leftover index 0"
    derived = json.dumps(
        {
            "source": "inbox",
            "inbox_file": "session.json",
            "candidate_index": 0,
            "kind": "compact_summary",
        }
    )
    with db_obj.transaction() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             evidence_refs, derived_from, instruction_like, status, proposed_at)
            VALUES ('agy', 'default', NULL, 'safe', ?, '[]', ?, 0, 'proposed', ?)
            """,
            (leftover, derived, time.time()),
        )

    inbox = tmp_path / "agy-vault" / "inbox"
    _write_inbox_file(
        inbox,
        "session.json",
        _summary_doc(agent_id="agy", summary_text=THREE_SHARED_BODY),
    )

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 3, res
    with db_obj.cursor() as c:
        c.execute(
            "SELECT content, principal, derived_from FROM candidate_packets "
            "ORDER BY candidate_id"
        )
        rows = [dict(r) for r in c.fetchall()]
    by_idx = {
        json.loads(r["derived_from"]).get("candidate_index"): r for r in rows
    }
    assert sorted(by_idx) == [0, 1, 2, 3], by_idx
    assert leftover in by_idx[0]["content"]
    qty = [r for r in rows if "SovereignDB caches" in r["content"]]
    assert qty, [r["content"] for r in rows]
    qty_idx = json.loads(qty[0]["derived_from"]).get("candidate_index")
    assert qty_idx not in {0}, qty_idx
    assert (inbox / ".archive" / "session.json").is_file()
    assert not (inbox / "session.json").exists()


ONE_SHARED_BODY = (
    "1. Key Technical Concepts:\n"
    "   Compact extra fill that leftover index 0 must not swallow.\n"
    "2. All user messages:\n"
    "   please ignore this personal narration\n"
)


def test_leftover_alias_index0_does_not_archive_one_section_compact_without_merge(
    tmp_path, monkeypatch
):
    """Leftover agy index 0 with a different body must not treat a later
    compact_summary that distills to a SINGLE shared section as fully
    received. (principal, inbox_file, candidate_index) UNIQUE has no
    content_sha1, so missing=[] used to archive immediately and never
    insert the extra fill. Merge the divergent body; do not archive on
    UNIQUE skip of leftover index 0.
    """
    import time

    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    leftover = (
        "Key technical concepts: leftover alias fill occupying index 0 only"
    )
    derived = json.dumps(
        {
            "source": "inbox",
            "channel": "compact_distillation",
            "inbox_file": "session.json",
            "candidate_index": 0,
            "kind": "compact_summary",
        }
    )
    with db_obj.transaction() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             evidence_refs, derived_from, instruction_like, status, proposed_at)
            VALUES ('agy', 'default', NULL, 'safe', ?, '[]', ?, 0, 'proposed', ?)
            """,
            (leftover, derived, time.time()),
        )

    inbox = tmp_path / "agy-vault" / "inbox"
    _write_inbox_file(
        inbox,
        "session.json",
        _summary_doc(agent_id="agy", summary_text=ONE_SHARED_BODY),
    )

    res = distill(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1, res
    with db_obj.cursor() as c:
        c.execute(
            "SELECT content, principal, derived_from FROM candidate_packets "
            "ORDER BY candidate_id"
        )
        rows = [dict(r) for r in c.fetchall()]
    contents = [r["content"] for r in rows]
    assert leftover in contents
    extra = [
        r
        for r in rows
        if "Compact extra fill that leftover index 0 must not swallow" in r["content"]
    ]
    assert extra, contents
    assert {r["principal"] for r in extra} == {"gemini"}
    extra_indices = {
        json.loads(r["derived_from"]).get("candidate_index") for r in extra
    }
    assert extra_indices.isdisjoint({0}), extra_indices
    # Archive only after the extra fill landed — UNIQUE skip of leftover
    # index 0 must not retire the live file unmerged.
    assert not (inbox / "session.json").exists()
    assert (inbox / ".archive" / "session.json").is_file()


AGY_ONLY_COMPACT = (
    "1. Key Technical Concepts:\n"
    "   AGY-ONLY compact qty that gemini-vault must not UNIQUE-swallow.\n"
    "2. All user messages:\n"
    "   personal agy narration\n"
)

GEMINI_ONLY_COMPACT = (
    "1. Key Technical Concepts:\n"
    "   GEMINI-ONLY compact qty that must land as an extra fill.\n"
    "2. All user messages:\n"
    "   personal gemini narration\n"
)


def test_second_alias_vault_compact_is_not_unique_skipped_and_archived(
    tmp_path, monkeypatch
):
    """agy-vault and gemini-vault both canonicalize to gemini.

    distill() used to reload _existing_keys per inbox while buffering
    to_insert until after every vault was scanned, so the same
    (principal, inbox_file, candidate_index) was queued twice. INSERT
    kept the first body, UNIQUE-swallowed the second, and archive
    retired the unmerged second vault file. One occupancy map over the
    whole scan extras-at-next-idx the divergent second body.
    """
    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    agy_inbox = tmp_path / "agy-vault" / "inbox"
    gemini_inbox = tmp_path / "gemini-vault" / "inbox"
    _write_inbox_file(
        agy_inbox,
        "session.json",
        _summary_doc(agent_id="agy", summary_text=AGY_ONLY_COMPACT),
    )
    _write_inbox_file(
        gemini_inbox,
        "session.json",
        _summary_doc(agent_id="gemini", summary_text=GEMINI_ONLY_COMPACT),
    )

    res = distill(
        db_obj, cfg, inboxes=[agy_inbox, gemini_inbox], dry_run=False
    )
    assert res["inserted"] == 2, res
    with db_obj.cursor() as c:
        c.execute(
            "SELECT content, principal, derived_from FROM candidate_packets "
            "ORDER BY candidate_id"
        )
        rows = [dict(r) for r in c.fetchall()]
    joined = "\n".join(r["content"] for r in rows)
    assert "AGY-ONLY compact qty" in joined, joined
    assert "GEMINI-ONLY compact qty" in joined, joined
    indices = sorted(
        json.loads(r["derived_from"]).get("candidate_index") for r in rows
    )
    assert indices == [0, 1], (indices, rows)
    assert {r["principal"] for r in rows} == {"gemini"}
    # Neither live file may be archived until its body actually landed.
    assert not (agy_inbox / "session.json").exists()
    assert (agy_inbox / ".archive" / "session.json").is_file()
    assert not (gemini_inbox / "session.json").exists()
    assert (gemini_inbox / ".archive" / "session.json").is_file()


IDENTICAL_D_COMPACT = (
    "1. Key Technical Concepts:\n"
    "   Identical D body shared by agy-vault and gemini-vault session.json.\n"
    "2. All user messages:\n"
    "   personal narration both vaults share\n"
)


def test_second_alias_identical_body_does_not_archive_before_insert(
    tmp_path, monkeypatch
):
    """identical-body D in agy-vault and gemini-vault session.json.

    Leftover occupies 0; first vault extras D into to_insert. Occupancy is
    the in-memory map mutated by that first file, so the second vault gets
    assigned=[None] / insert_slots=[] and used to archive immediately —
    before the INSERT txn. A crash (or UNIQUE skip) then loses D and the
    second vault file. Archive only after durable_keys confirm the extra
    committed.
    """
    import time

    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    leftover = "Key technical concepts: leftover occupying index 0, not D"
    derived = json.dumps(
        {
            "source": "inbox",
            "channel": "compact_distillation",
            "inbox_file": "session.json",
            "candidate_index": 0,
            "kind": "compact_summary",
        }
    )
    with db_obj.transaction() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             evidence_refs, derived_from, instruction_like, status, proposed_at)
            VALUES ('agy', 'default', NULL, 'safe', ?, '[]', ?, 0, 'proposed', ?)
            """,
            (leftover, derived, time.time()),
        )

    agy_inbox = tmp_path / "agy-vault" / "inbox"
    gemini_inbox = tmp_path / "gemini-vault" / "inbox"
    _write_inbox_file(
        agy_inbox,
        "session.json",
        _summary_doc(agent_id="agy", summary_text=IDENTICAL_D_COMPACT),
    )
    _write_inbox_file(
        gemini_inbox,
        "session.json",
        _summary_doc(agent_id="gemini", summary_text=IDENTICAL_D_COMPACT),
    )

    def crash_txn(*_a, **_k):
        raise RuntimeError("crash before INSERT")

    monkeypatch.setattr(db_obj, "transaction", crash_txn)
    try:
        distill(
            db_obj, cfg, inboxes=[agy_inbox, gemini_inbox], dry_run=False
        )
        raise AssertionError("distill must not swallow the crash before INSERT")
    except RuntimeError as exc:
        assert "crash before INSERT" in str(exc)

    # Second vault must still be live — occupancy skip is not durable.
    assert (gemini_inbox / "session.json").is_file()
    assert not (gemini_inbox / ".archive" / "session.json").exists()
    assert (agy_inbox / "session.json").is_file()
    assert not (agy_inbox / ".archive" / "session.json").exists()
    with db_obj.cursor() as c:
        c.execute("SELECT content FROM candidate_packets")
        contents = [dict(r)["content"] for r in c.fetchall()]
    assert leftover in contents
    assert all("Identical D body" not in body for body in contents), contents


def test_second_alias_identical_body_archives_after_extra_commits(
    tmp_path, monkeypatch
):
    """Same leftover + identical D in both alias vaults: after INSERT lands
    the extra, both files archive. inserted==1, leftover 0 + extra 1.
    """
    import time

    from minni.afm_passes.compact_distillation import distill
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    db_obj, cfg = _make_db(tmp_path)
    leftover = "Key technical concepts: leftover occupying index 0, not D"
    derived = json.dumps(
        {
            "source": "inbox",
            "channel": "compact_distillation",
            "inbox_file": "session.json",
            "candidate_index": 0,
            "kind": "compact_summary",
        }
    )
    with db_obj.transaction() as c:
        c.execute(
            """
            INSERT INTO candidate_packets
            (principal, workspace_id, layer, privacy_level, content,
             evidence_refs, derived_from, instruction_like, status, proposed_at)
            VALUES ('agy', 'default', NULL, 'safe', ?, '[]', ?, 0, 'proposed', ?)
            """,
            (leftover, derived, time.time()),
        )

    agy_inbox = tmp_path / "agy-vault" / "inbox"
    gemini_inbox = tmp_path / "gemini-vault" / "inbox"
    _write_inbox_file(
        agy_inbox,
        "session.json",
        _summary_doc(agent_id="agy", summary_text=IDENTICAL_D_COMPACT),
    )
    _write_inbox_file(
        gemini_inbox,
        "session.json",
        _summary_doc(agent_id="gemini", summary_text=IDENTICAL_D_COMPACT),
    )

    res = distill(
        db_obj, cfg, inboxes=[agy_inbox, gemini_inbox], dry_run=False
    )
    assert res["inserted"] == 1, res
    with db_obj.cursor() as c:
        c.execute(
            "SELECT content, derived_from FROM candidate_packets "
            "ORDER BY candidate_id"
        )
        rows = [dict(r) for r in c.fetchall()]
    indices = sorted(
        json.loads(r["derived_from"]).get("candidate_index") for r in rows
    )
    assert indices == [0, 1], (indices, rows)
    joined = "\n".join(r["content"] for r in rows)
    assert leftover in joined
    assert "Identical D body" in joined
    assert not (agy_inbox / "session.json").exists()
    assert (agy_inbox / ".archive" / "session.json").is_file()
    assert not (gemini_inbox / "session.json").exists()
    assert (gemini_inbox / ".archive" / "session.json").is_file()


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


# ── #307: the pass counts its drops; the AFM caller must not discard them ───


def test_afm_loop_surfaces_compact_distillation_drops(tmp_path, monkeypatch):
    """compact_distillation counts _unreadable/_malformed correctly (AFM-9,
    #230), but the AFM loop only logged when work LANDED and never read
    _dc["skipped"] — no counter, nothing on any health surface. A writer that
    starts emitting corrupt payloads was discoverable only by grepping the
    daemon log for a warning string."""
    import asyncio
    import json as _json
    import os as _os
    import sys as _sys
    import time as _time

    import minni.obs as obs
    from minni.minnid_runtime.afm import afm_loop_runner

    _sys.path.insert(0, _os.path.dirname(__file__))
    from test_afm_loop_promotion import _loop_context  # noqa: E402
    from test_afm_loop_promotion import _make_db as _make_loop_db  # noqa: E402

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_loop_db(tmp_path)
    cons_cfg = cfg.afm_loop_schedule["passes"]["consolidation"]
    cons_cfg["distill_compact_summaries"] = True

    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "compact-truncated.json").write_text(
        '{"kind": "compact_summary", "summ', encoding="utf-8",
    )
    (inbox / "compact-listish.json").write_text("[]", encoding="utf-8")
    old = _time.time() - 3600
    for p in inbox.glob("*.json"):
        _os.utime(p, (old, old))

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        asyncio.run(afm_loop_runner(ctx))

        snap = obs.metrics_snapshot()
        assert snap.get("compact_distillation_dropped_total") == 2, snap
    finally:
        obs.METRICS.reset()
        del _json


def test_health_reports_a_live_unusable_file_count_not_a_tick_total(tmp_path, monkeypatch):
    """The counter measures drop EVENTS: a dropped file is never archived, so
    it is re-dropped every tick and a cumulative total is files x ticks
    (~96/day/file at a 900s interval). Health must report the FILE count, or
    it overstates corruption — the same health-overstatement class this fix
    exists to close, relocated from the log to the surface."""
    import minni.obs as obs

    from test_inbox_quarantine import _real_lifecycle_db  # noqa: E402

    import minni.minnid as minnid
    from minni.principal import EffectivePrincipal

    _real_lifecycle_db(tmp_path, monkeypatch)
    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "c1.json").write_text('{"kind": "compact_summary", "su', encoding="utf-8")
    (inbox / "c2.json").write_text("[]", encoding="utf-8")

    obs.METRICS.reset()
    try:
        # A counter inflated by many ticks must NOT move the health number.
        obs.incr("compact_distillation_dropped_total", 96)
        op = EffectivePrincipal(agent_id="main", capabilities=["*"])
        rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]
        ml = rep["memory_lifecycle"]
        assert ml["compact_distillation_unusable"]["files"] == 2, ml
    finally:
        obs.METRICS.reset()


def test_unusable_count_clears_when_the_file_is_removed(tmp_path):
    """Self-clearing is the property a cumulative counter cannot have."""
    from minni.afm_passes.compact_distillation import count_unusable_compact_files

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    bad = inbox / "c1.json"
    bad.write_text("{oops", encoding="utf-8")
    assert count_unusable_compact_files([inbox], fallback_principal="unknown")["files"] == 1

    bad.unlink()
    assert count_unusable_compact_files([inbox], fallback_principal="unknown")["files"] == 0


def test_unusable_count_ignores_readable_files(tmp_path):
    from minni.afm_passes.compact_distillation import count_unusable_compact_files

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    # summary_TEXT is the field the pass reads; a file carrying only
    # "summary" has no usable content and is correctly counted (#336).
    (inbox / "ok.json").write_text(
        json.dumps({
            "kind": "compact_summary",
            "agent_id": "codex",
            "summary_text": "a real summary",
        }),
        encoding="utf-8",
    )
    assert count_unusable_compact_files([inbox], fallback_principal="codex")["files"] == 0


def test_health_unusable_block_degrades_to_unknown_not_zero(tmp_path, monkeypatch):
    from test_inbox_quarantine import _real_lifecycle_db  # noqa: E402

    import minni.minnid as minnid
    from minni.principal import EffectivePrincipal

    _real_lifecycle_db(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(
        "minni.afm_passes.compact_distillation.count_unusable_compact_files", _boom,
    )
    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]

    block = rep["memory_lifecycle"]["compact_distillation_unusable"]
    assert block["status"] == "unknown"
    assert block["error"] == "RuntimeError"
    assert block["files"] is None, "a failed scan must not report zero files"


def test_health_report_does_not_consume_the_status_delta_baseline(tmp_path, monkeypatch):
    """metrics_snapshot, never metrics_delta_snapshot: consuming the baseline
    here would suppress a rising-error flag before handle_status showed it."""
    import minni.obs as obs

    from test_inbox_quarantine import _real_lifecycle_db  # noqa: E402

    import minni.minnid as minnid
    from minni.principal import EffectivePrincipal

    _real_lifecycle_db(tmp_path, monkeypatch)
    obs.METRICS.reset()
    try:
        obs.incr("compact_distillation_dropped_total", 5)
        op = EffectivePrincipal(agent_id="main", capabilities=["*"])
        minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)
        delta = obs.metrics_delta_snapshot()
        entry = delta.get("compact_distillation_dropped_total") or {}
        assert entry.get("delta") == 5, (
            f"health_report must not have advanced the delta baseline: {delta}"
        )
    finally:
        obs.METRICS.reset()


def test_a_tick_that_only_dropped_files_still_logs(tmp_path, monkeypatch, caplog):
    """The old caller logged only when work LANDED, so a tick that only
    dropped files said nothing at all from that branch."""
    import asyncio
    import logging
    import os as _os
    import sys as _sys

    import minni.obs as obs
    from minni.minnid_runtime.afm import afm_loop_runner

    _sys.path.insert(0, _os.path.dirname(__file__))
    from test_afm_loop_promotion import _loop_context  # noqa: E402
    from test_afm_loop_promotion import _make_db as _make_loop_db  # noqa: E402

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_loop_db(tmp_path)
    cfg.afm_loop_schedule["passes"]["consolidation"]["distill_compact_summaries"] = True
    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "c1.json").write_text("{oops", encoding="utf-8")

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        with caplog.at_level(logging.WARNING):
            asyncio.run(afm_loop_runner(ctx))
        assert any(
            "compact distillation dropped" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]
    finally:
        obs.METRICS.reset()


def test_routine_other_kind_routing_is_not_counted_as_a_drop(tmp_path, monkeypatch):
    """_other_kind is ROUTING, not loss: other kinds legitimately share this
    inbox and are drained by their own pass (and the kind-less dead letters
    among them are already surfaced by memory_lifecycle.afm_dead_letter).
    Counting it would manufacture an alarm for healthy traffic."""
    import asyncio
    import os as _os
    import sys as _sys

    import minni.obs as obs
    from minni.minnid_runtime.afm import afm_loop_runner

    _sys.path.insert(0, _os.path.dirname(__file__))
    from test_afm_loop_promotion import _loop_context  # noqa: E402
    from test_afm_loop_promotion import _make_db as _make_loop_db  # noqa: E402

    monkeypatch.setenv("MINNI_AFM_LOOP", "on")
    monkeypatch.delenv("MINNI_AFM_MODE", raising=False)
    monkeypatch.delenv("MINNI_AFM_PROVIDER_MODE", raising=False)

    db_obj, cfg = _make_loop_db(tmp_path)
    cfg.afm_loop_schedule["passes"]["consolidation"]["distill_compact_summaries"] = True
    inbox = tmp_path / "unknown-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "handoff.json").write_text(
        json.dumps({"kind": "handoff", "task": "t"}), encoding="utf-8",
    )

    obs.METRICS.reset()
    try:
        ctx, _traces = _loop_context(db_obj, cfg, ticks=1)
        asyncio.run(afm_loop_runner(ctx))
        snap = obs.metrics_snapshot()
        assert not snap.get("compact_distillation_dropped_total"), snap
    finally:
        obs.METRICS.reset()

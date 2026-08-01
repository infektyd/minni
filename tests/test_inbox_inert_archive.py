"""Tests for afm_passes.inbox_archive.archive_inert_files — the sweep for
stop-candidate inbox files whose every candidate ingest rejects (audit echo /
log_only / do_not_store / blank) and that therefore can never resolve or
archive through the DB-row lifecycle (2026-08-01 pile-up: 107 echo-only files
re-surfacing in every SessionStart pending-inbox count).

Follows the test_inbox_ingest.py / test_inbox_quarantine.py harness pattern
(isolated tmp DB + config, no real ~/.minni touched).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from test_inbox_ingest import _make_db, _stop_doc, _write_inbox_file  # noqa: E402

# The exact shape from the pile: kind-less Claude Code stop file whose single
# candidate is a session-id-prefixed slice of Minni's own audit tail.
ECHO_CANDIDATE = (
    "6a5ba70f-70b3-44e5-8a78-2e21d774cd71: ## [2026-08-01T01:44:08.721Z] "
    'hook_stop | stop 6a5ba70f ```json { "candidates": 1 } ``` '
    "## [2026-08-01T02:53:13.360Z] hook_user_prompt_submit | make it so"
)


def _echo_doc(**overrides):
    doc = _stop_doc([ECHO_CANDIDATE])
    # Kind-less Claude Code shape: no kind, no agent_id stamp.
    doc.pop("kind")
    doc.pop("agent_id")
    doc.update(overrides)
    return doc


# ── (1) regression pin: confirms the underlying residue still exists ────────

def test_echo_only_file_is_never_ingested_confirms_residue(tmp_path):
    """ingest()'s skip behavior is UNCHANGED by this package — this pins the
    precondition the inert sweep exists to clean up after."""
    from minni.afm_passes.inbox_ingest import ingest

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _echo_doc())

    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 0, res
    assert res["skipped_by_kind"]["_audit_echo"] == 1, res
    with db_obj.cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM candidate_packets")
        assert dict(c.fetchone())["n"] == 0


# ── (2) core new behavior ───────────────────────────────────────────────────

def test_echo_only_file_is_archived(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _echo_doc())

    res = archive_inert_files(cfg, inboxes=[inbox])
    assert res["archived"] == 1, res
    assert res["reasons"] == {"no_ingestible_candidates": 1}, res
    assert not (inbox / "a.json").exists()
    archived = inbox / ".archive" / "a.json"
    assert archived.is_file()
    # Never unlinks: content is preserved verbatim.
    assert json.loads(archived.read_text())["candidates"] == [ECHO_CANDIDATE]


def test_file_with_real_candidate_stays_live(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(
        inbox, "mixed.json",
        _echo_doc(candidates=[ECHO_CANDIDATE, "Use WAL mode for SQLite."]),
    )

    res = archive_inert_files(cfg, inboxes=[inbox])
    assert res["archived"] == 0, res
    assert (inbox / "mixed.json").exists()


def test_blank_and_listed_candidates_count_as_inert(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(
        inbox, "listed.json",
        _echo_doc(
            candidates=["", "   ", "keep me out", ECHO_CANDIDATE],
            log_only=["keep me out"],
        ),
    )

    res = archive_inert_files(cfg, inboxes=[inbox])
    assert res["archived"] == 1, res
    assert not (inbox / "listed.json").exists()


def test_empty_candidates_file_is_archived(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(inbox, "empty.json", _echo_doc(candidates=[]))

    res = archive_inert_files(cfg, inboxes=[inbox])
    assert res["archived"] == 1, res


# ── (3) other cohorts are strictly out of scope ─────────────────────────────

def test_agent_mismatch_file_is_left_for_quarantine(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "unknown-vault" / "inbox"
    _write_inbox_file(
        inbox, "mm.json",
        _stop_doc([ECHO_CANDIDATE], agent_id="unknown-agent"),
    )

    res = archive_inert_files(cfg, inboxes=[inbox])
    assert res["archived"] == 0, res
    assert (inbox / "mm.json").exists()


def test_non_stop_kind_is_untouched(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(
        inbox, "handoff.json",
        {"kind": "handoff", "candidates": [ECHO_CANDIDATE], "slug": "s",
         "last_task": "t"},
    )

    res = archive_inert_files(cfg, inboxes=[inbox])
    assert res["archived"] == 0, res
    assert (inbox / "handoff.json").exists()


def test_unparseable_file_is_untouched(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "broken.json").write_text("{not json", encoding="utf-8")

    res = archive_inert_files(cfg, inboxes=[inbox])
    assert res["archived"] == 0, res
    assert (inbox / "broken.json").exists()


# ── (4) mechanics ───────────────────────────────────────────────────────────

def test_dry_run_reports_without_moving(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _echo_doc())

    res = archive_inert_files(cfg, inboxes=[inbox], dry_run=True)
    assert res["would_archive"] == 1, res
    assert res["archived"] == 0, res
    assert (inbox / "a.json").exists()


def test_sweep_is_idempotent(tmp_path):
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "claudecode-vault" / "inbox"
    _write_inbox_file(inbox, "a.json", _echo_doc())

    first = archive_inert_files(cfg, inboxes=[inbox])
    second = archive_inert_files(cfg, inboxes=[inbox])
    assert first["archived"] == 1
    assert second["archived"] == 0
    assert second["would_archive"] == 0


def test_explicit_stop_kind_echo_file_is_archived(tmp_path):
    """The codex-tagged variant of the same residue (kind + agent_id stamped,
    agent MATCHING its vault) archives too — the grok-build 25-file cohort."""
    from minni.afm_passes.inbox_archive import archive_inert_files

    _, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "c.json", _stop_doc([ECHO_CANDIDATE]))

    res = archive_inert_files(cfg, inboxes=[inbox])
    assert res["archived"] == 1, res

"""Tests for `minni.watch` — the stdlib-only, read-only audit/episodic tailer
that backs `minni watch` (PACKAGING_PLAN.md §3: no engine imports, offset/URI
read-only access only).

Covers:
  A. parse_audit_entries() against the real `## [ISO] tool | summary` +
     optional ```json details block format written by
     plugins/minni/src/vault.ts recordAudit().
  B. AuditTailer offset tailing + rotation (truncate-then-append) handling.
  C. EpisodicPoller against a minimal on-disk sqlite episodic_events table,
     including read-only-ness of its own connection.
  D. discover_vault_logs()/agent_id_for_vault() vault-dir discovery + the
     claude/claudecode -> claude-code alias.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import minni.watch as watch


# ---------------------------------------------------------------------------
# A. parse_audit_entries
# ---------------------------------------------------------------------------

def test_parse_audit_entries_multiple_with_and_without_details():
    text = (
        "## [2026-07-15T14:52:03.123Z] tool_alpha | did a thing\n\n"
        "```json\n"
        '{\n  "query": "hello",\n  "hits": 3\n}\n'
        "```\n\n"
        "## [2026-07-15T14:53:10.000Z] tool_beta | did another thing, no details\n\n"
    )
    events = watch.parse_audit_entries(text, "myagent")
    assert len(events) == 2

    first, second = events
    assert first.ts == "2026-07-15T14:52:03.123Z"
    assert first.tool == "tool_alpha"
    assert first.summary == "did a thing"
    assert first.agent == "myagent"
    assert first.details == {"query": "hello", "hits": 3}

    assert second.ts == "2026-07-15T14:53:10.000Z"
    assert second.tool == "tool_beta"
    assert second.summary == "did another thing, no details"
    assert second.details is None


def test_parse_audit_entries_tolerates_partial_leading_entry():
    """The tailer may hand the parser text that starts mid-entry (after an
    offset seek that lands inside a previous ```json block or summary line).
    The parser must not raise and must not fabricate a bogus event from the
    orphaned fragment; it should simply pick up at the next real header."""
    text = (
        '  "leftover": "fragment from a prior entry"\n'
        "}\n"
        "```\n\n"
        "## [2026-07-15T15:00:00.000Z] tool_gamma | clean entry after the fragment\n\n"
    )
    events = watch.parse_audit_entries(text, "agent-x")
    assert len(events) == 1
    assert events[0].tool == "tool_gamma"
    assert events[0].summary == "clean entry after the fragment"


def test_parse_audit_entries_single_line_summary_contract():
    """Summaries are single-line by contract (vault.ts escapeAuditField
    collapses \\n/\\r to literal backslash sequences before writing), so a
    summary containing literal backslash-n must survive as literal text, not
    be treated as a real newline / additional entry."""
    text = (
        "## [2026-07-15T16:00:00.000Z] tool_delta | line one\\nline two continues\n\n"
    )
    events = watch.parse_audit_entries(text, "agent-y")
    assert len(events) == 1
    assert events[0].summary == "line one\\nline two continues"
    assert "\n" not in events[0].summary


def test_parse_audit_entries_bad_json_details_yields_none_not_raise():
    text = (
        "## [2026-07-15T16:05:00.000Z] tool_eps | malformed details\n\n"
        "```json\n"
        "{not valid json\n"
        "```\n\n"
    )
    events = watch.parse_audit_entries(text, "agent-z")
    assert len(events) == 1
    assert events[0].details is None


def test_parse_audit_entries_empty_text():
    assert watch.parse_audit_entries("", "agent") == []


# ---------------------------------------------------------------------------
# B. AuditTailer
# ---------------------------------------------------------------------------

def _entry(ts: str, tool: str, summary: str) -> str:
    return f"## [{ts}] {tool} | {summary}\n\n"


def test_audit_tailer_seed_then_poll_only_new(tmp_path):
    log = tmp_path / "log.md"
    log.write_text(_entry("2026-07-15T10:00:00.000Z", "t1", "first"))

    tailer = watch.AuditTailer(log, "agent-a")
    backlog = tailer.seed()
    assert [e.tool for e in backlog] == ["t1"]

    # No new bytes yet -> poll returns nothing.
    assert tailer.poll() == []

    with open(log, "a") as fh:
        fh.write(_entry("2026-07-15T10:01:00.000Z", "t2", "second"))

    fresh = tailer.poll()
    assert [e.tool for e in fresh] == ["t2"]
    # Polling again with no further writes yields nothing new.
    assert tailer.poll() == []


def test_audit_tailer_rotation_resets_offset_without_raising(tmp_path):
    log = tmp_path / "log.md"
    log.write_text(
        _entry("2026-07-15T10:00:00.000Z", "t1", "first")
        + _entry("2026-07-15T10:01:00.000Z", "t2", "second")
    )
    tailer = watch.AuditTailer(log, "agent-a")
    tailer.seed()
    assert tailer.offset == log.stat().st_size

    # Simulate the plugin's 5MB rotation: truncate back to a small header,
    # then append a fresh entry.
    log.write_text(_entry("2026-07-15T11:00:00.000Z", "t3", "post-rotation"))
    assert log.stat().st_size < tailer.offset

    fresh = tailer.poll()
    assert [e.tool for e in fresh] == ["t3"]
    assert tailer.offset == log.stat().st_size


def test_audit_tailer_poll_missing_file_returns_empty(tmp_path):
    tailer = watch.AuditTailer(tmp_path / "nope.md", "agent-a")
    assert tailer.poll() == []


# ---------------------------------------------------------------------------
# C. EpisodicPoller
# ---------------------------------------------------------------------------

def _make_episodic_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE episodic_events ("
        "event_id INTEGER PRIMARY KEY, agent_id TEXT, event_type TEXT, "
        "content TEXT, created_at REAL)"
    )
    conn.executemany(
        "INSERT INTO episodic_events "
        "(event_id, agent_id, event_type, content, created_at) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_episodic_poller_seed_backlog_and_poll_ordering(tmp_path):
    db_path = tmp_path / "minni.db"
    now = time.time()
    rows = [
        (1, "agent-a", "recall", "first", now),
        (2, "agent-a", "recall", "second", now + 1),
        (3, "agent-b", "learn", "third", now + 2),
    ]
    _make_episodic_db(db_path, rows)

    poller = watch.EpisodicPoller(db_path)
    backlog = poller.seed(backlog=2)
    # seed(2) should return only the most recent 2, in ascending order.
    assert [e.summary for e in backlog] == ["second", "third"]
    assert poller.last_event_id == 3

    # No new rows -> poll() is empty.
    assert poller.poll() == []

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO episodic_events "
        "(event_id, agent_id, event_type, content, created_at) VALUES (?,?,?,?,?)",
        (4, "agent-a", "recall", "fourth", now + 3),
    )
    conn.commit()
    conn.close()

    fresh = poller.poll()
    assert [e.summary for e in fresh] == ["fourth"]
    assert poller.last_event_id == 4


def test_episodic_poller_seed_full_backlog_order(tmp_path):
    db_path = tmp_path / "minni.db"
    now = time.time()
    rows = [(i, "agent-a", "recall", f"entry-{i}", now + i) for i in range(1, 6)]
    _make_episodic_db(db_path, rows)

    poller = watch.EpisodicPoller(db_path)
    backlog = poller.seed(backlog=10)
    assert [e.summary for e in backlog] == [f"entry-{i}" for i in range(1, 6)]
    assert poller.last_event_id == 5


def test_episodic_poller_missing_db_returns_empty(tmp_path):
    poller = watch.EpisodicPoller(tmp_path / "does-not-exist.db")
    assert poller.seed(backlog=5) == []
    assert poller.poll() == []


def test_episodic_poller_connection_is_read_only(tmp_path):
    """EpisodicPoller opens sqlite in ?mode=ro URI mode; a write attempt
    through that same connection style must fail, and polling must never
    mutate the underlying file."""
    db_path = tmp_path / "minni.db"
    now = time.time()
    _make_episodic_db(db_path, [(1, "agent-a", "recall", "only", now)])
    before = db_path.read_bytes()

    poller = watch.EpisodicPoller(db_path)
    poller.seed(backlog=5)
    poller.poll()

    after = db_path.read_bytes()
    assert before == after, "EpisodicPoller must never mutate episodic_events"

    ro_conn = poller._connect()
    assert ro_conn is not None
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute(
                "INSERT INTO episodic_events "
                "(event_id, agent_id, event_type, content, created_at) "
                "VALUES (99, 'x', 'y', 'z', 0)"
            )
    finally:
        ro_conn.close()


# ---------------------------------------------------------------------------
# D. discover_vault_logs / agent_id_for_vault
# ---------------------------------------------------------------------------

def test_agent_id_for_vault_basic_and_claude_aliases():
    assert watch.agent_id_for_vault(Path("/home/x/.minni/foo-vault")) == "foo"
    assert watch.agent_id_for_vault(Path("/home/x/.minni/claudecode-vault")) == "claude-code"
    assert watch.agent_id_for_vault(Path("/home/x/.minni/claude-vault")) == "claude-code"
    # Non-vault-suffixed dir: basename passes through unchanged.
    assert watch.agent_id_for_vault(Path("/home/x/.minni/not-a-vault-dir")) == "not-a-vault-dir"


def test_discover_vault_logs_maps_vault_dirs_and_skips_non_vaults(tmp_path):
    (tmp_path / "foo-vault").mkdir()
    (tmp_path / "claudecode-vault").mkdir()
    (tmp_path / "not-a-vault").write_text("just a file, not a directory")
    (tmp_path / "some-other-dir").mkdir()  # doesn't end in -vault

    mapping = watch.discover_vault_logs(tmp_path)
    by_agent = {agent: path for path, agent in mapping.items()}

    assert set(by_agent) == {"foo", "claude-code"}
    assert by_agent["foo"] == tmp_path / "foo-vault" / "log.md"
    assert by_agent["claude-code"] == tmp_path / "claudecode-vault" / "log.md"


def test_discover_vault_logs_missing_home_returns_empty(tmp_path):
    assert watch.discover_vault_logs(tmp_path / "does-not-exist") == {}


# ---------------------------------------------------------------------------
# format_event / event_to_json (small surface, cheap to cover here)
# ---------------------------------------------------------------------------

def test_event_to_json_roundtrip_includes_details_only_when_present():
    ev = watch.WatchEvent(ts="2026-07-15T10:00:00Z", agent="a", tool="t",
                          summary="s", source="plugin", details={"hits": 1})
    payload = json.loads(watch.event_to_json(ev))
    assert payload == {"ts": ev.ts, "agent": "a", "tool": "t", "summary": "s",
                       "source": "plugin", "details": {"hits": 1}}

    ev_no_details = watch.WatchEvent(ts="2026-07-15T10:00:00Z", agent="a",
                                     tool="t", summary="s", source="daemon")
    payload_no_details = json.loads(watch.event_to_json(ev_no_details))
    assert "details" not in payload_no_details


def test_format_event_plain_text_includes_summary_and_detail_keys():
    ev = watch.WatchEvent(ts="2026-07-15T10:00:00Z", agent="a", tool="search",
                          summary="did stuff", source="plugin",
                          details={"query": "q", "hits": 2, "irrelevant": "x"})
    line = watch.format_event(ev)
    assert "did stuff" in line
    assert "query=q" in line
    assert "hits=2" in line
    assert "irrelevant" not in line

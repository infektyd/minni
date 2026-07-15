"""Regression tests for the observability review findings (PR #166).

Each test pins one confirmed finding from the adversarial review of the
`minni watch` / recall-trace surface:

1. `minni watch` must honor MINNI_DB_PATH (daemon does; watch hardcoded
   MINNI_HOME/minni.db).
2. Audit-log rotation must not drop entries appended between the last poll
   and the rotation (they live in log.1.md after the plugin rotates).
3. MINNI_AGENT_VAULTS custom mappings must win over basename inference so
   `--agent` filtering matches the plugin's own audit attribution.
4. The recall trace must not semantically bind threads (add_event's
   auto-bind mutates thread_doc_links and runs a FAISS search — an
   observability write must be inert).
5. AFM session distillation must not ingest recall-trace events as session
   signal.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time

import pytest

from minni.watch import AuditTailer, discover_vault_logs, episodic_db_path


def _entry(ts: str, tool: str, summary: str) -> str:
    return f"## [{ts}] {tool} | {summary}\n\n"


# ── 1. MINNI_DB_PATH ──────────────────────────────────────────────────────


def test_episodic_db_path_defaults_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("MINNI_DB_PATH", raising=False)
    assert episodic_db_path(tmp_path) == tmp_path / "minni.db"


def test_episodic_db_path_honors_env(tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere" / "custom.db"
    monkeypatch.setenv("MINNI_DB_PATH", str(custom))
    assert episodic_db_path(tmp_path) == custom


# ── 2. rotation must not drop unread entries ──────────────────────────────


def test_rotation_preserves_entries_written_before_rotation(tmp_path):
    log = tmp_path / "log.md"
    log.write_text("# Log\n\n" + _entry("2026-07-15T10:00:00.000Z", "a", "one"))
    tailer = AuditTailer(log, "codex")
    assert [e.summary for e in tailer.seed()] == ["one"]

    # Entry "two" lands after our last poll…
    with open(log, "a") as fh:
        fh.write(_entry("2026-07-15T10:01:00.000Z", "b", "two"))
    # …then the plugin rotates: log.md -> log.1.md, fresh log.md gets "three".
    log.rename(tmp_path / "log.1.md")
    log.write_text("# Log\n\n" + _entry("2026-07-15T10:02:00.000Z", "c", "three"))

    summaries = [e.summary for e in tailer.poll()]
    assert summaries == ["two", "three"]


# ── 3. MINNI_AGENT_VAULTS mapping ─────────────────────────────────────────


def test_discover_vault_logs_honors_agent_vaults_mapping(tmp_path, monkeypatch):
    (tmp_path / "weird-vault").mkdir()
    custom = tmp_path / "not-a-standard-dir"
    custom.mkdir()
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps({
        "custom-agent": str(custom),
        "renamed-agent": str(tmp_path / "weird-vault"),
    }))
    logs = discover_vault_logs(tmp_path)
    assert logs[custom / "log.md"] == "custom-agent"
    # The mapping wins over basename inference ("weird").
    assert logs[tmp_path / "weird-vault" / "log.md"] == "renamed-agent"


def test_discover_vault_logs_ignores_malformed_mapping(tmp_path, monkeypatch):
    (tmp_path / "codex-vault").mkdir()
    monkeypatch.setenv("MINNI_AGENT_VAULTS", "{not json")
    logs = discover_vault_logs(tmp_path)
    assert logs[tmp_path / "codex-vault" / "log.md"] == "codex"


# ── 4. recall trace must not thread-bind ──────────────────────────────────


class _StubCursor:
    lastrowid = 1

    def execute(self, *_args):
        return self


class _StubDB:
    @contextlib.contextmanager
    def cursor(self):
        yield _StubCursor()


def _make_episodic(monkeypatch):
    from minni.episodic import EpisodicMemory

    memory = EpisodicMemory.__new__(EpisodicMemory)
    memory.db = _StubDB()
    memory.config = None
    calls = []
    monkeypatch.setattr(
        memory, "_semantic_thread_bind",
        lambda thread_id, content: calls.append((thread_id, content)))
    return memory, calls


def test_add_event_bind_thread_false_skips_semantic_bind(monkeypatch):
    memory, calls = _make_episodic(monkeypatch)
    memory.add_event("codex", "recall", "recall trace", thread_id="sess-1",
                     bind_thread=False)
    assert calls == []


def test_add_event_default_still_binds(monkeypatch):
    memory, calls = _make_episodic(monkeypatch)
    memory.add_event("codex", "message", "real content", thread_id="sess-1")
    assert calls == [("sess-1", "real content")]


def test_recall_trace_passes_bind_thread_false():
    from minni.minnid_runtime import recall as recall_mod

    recorded = {}

    class _FakeEpisodic:
        def add_event(self, **kwargs):
            recorded.update(kwargs)
            return 1

    class _FakePrincipal:
        agent_id = "codex"
        workspace_id = "default"

    class _FakeEngine:
        last_trace_id = None

        def retrieve(self, **_kwargs):
            return [{"score": 0.9}]

        def search_learnings(self, *_a, **_k):
            return []

    class _Cfg:
        recall_trace = True
        vector_backends = ["faiss-disk"]

    context = recall_mod.RecallContext(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}},
        make_response=lambda result, rid: {"result": result},
        handler_principal=lambda params, rid: (_FakePrincipal(), None),
        lazy_retrieval=lambda: _FakeEngine(),
        agent_vault_retrieval=lambda agent_id: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: None,
        record_latency=lambda name, seconds: None,
        lazy_episodic=lambda: _FakeEpisodic(),
        default_config=_Cfg(),
    )
    response = recall_mod.handle_search(
        {"query": "q", "session_id": "sess-9"}, 1, context)
    assert "result" in response
    assert recorded["thread_id"] == "sess-9"
    assert recorded["bind_thread"] is False


# ── 5. distillation must exclude recall traces ────────────────────────────


class _SqliteDB:
    def __init__(self, path):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS episodic_events ("
            "event_id INTEGER PRIMARY KEY, agent_id TEXT, event_type TEXT,"
            " content TEXT, task_id TEXT, thread_id TEXT, metadata TEXT,"
            " created_at REAL)")

    @contextlib.contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        finally:
            cur.close()


def test_recent_events_excludes_recall_traces(tmp_path):
    pytest.importorskip("minni.afm_passes.session_distillation")
    from minni.afm_passes.session_distillation import _recent_events

    db = _SqliteDB(tmp_path / "events.db")
    now = time.time()
    with db.cursor() as c:
        c.execute("INSERT INTO episodic_events (agent_id, event_type, content,"
                  " created_at) VALUES (?,?,?,?)",
                  ("codex", "message", "real signal", now))
        c.execute("INSERT INTO episodic_events (agent_id, event_type, content,"
                  " created_at) VALUES (?,?,?,?)",
                  ("codex", "recall", 'recall "q" — 3 hits, top 0.90', now))

    for agent_filter in (None, "codex"):
        events = _recent_events(db, lookback_hours=24, agent_id=agent_filter)
        types = {row["event_type"] for row in events}
        assert "recall" not in types
        assert "message" in types


# ── round 2: adversarial-review findings ──────────────────────────────────


def test_recall_trace_content_is_redacted():
    """The durable trace persists query text; secrets must be scrubbed the
    same way the ephemeral trace-ring path scrubs its responses."""
    from minni.minnid_runtime import recall as recall_mod

    recorded = {}

    class _FakeEpisodic:
        def add_event(self, **kwargs):
            recorded.update(kwargs)
            return 1

    class _FakePrincipal:
        agent_id = "codex"
        workspace_id = "default"

    class _FakeEngine:
        last_trace_id = None

        def retrieve(self, **_kwargs):
            return []

        def search_learnings(self, *_a, **_k):
            return []

    class _Cfg:
        recall_trace = True
        vector_backends = ["faiss-disk"]

    context = recall_mod.RecallContext(
        make_error=lambda code, msg, rid: {"error": {"code": code}},
        make_response=lambda result, rid: {"result": result},
        handler_principal=lambda params, rid: (_FakePrincipal(), None),
        lazy_retrieval=lambda: _FakeEngine(),
        agent_vault_retrieval=lambda agent_id: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: None,
        record_latency=lambda name, seconds: None,
        lazy_episodic=lambda: _FakeEpisodic(),
        default_config=_Cfg(),
    )
    recall_mod.handle_search(
        {"query": "find the api_key=sk-supersecret123 config"}, 1, context)
    assert "sk-supersecret123" not in recorded["content"]
    assert "[REDACTED]" in recorded["content"]


def test_handle_read_recent_activity_excludes_recall_traces():
    """handle_read's Recent Activity (LIMIT 5) must not be crowded out by
    recall-trace rows. Pinned at the SQL level: the episodic_events query in
    handle_read must filter event_type != 'recall'."""
    import inspect

    from minni.minnid_runtime import recall as recall_mod

    source = inspect.getsource(recall_mod.handle_read)
    start = source.index("FROM episodic_events")
    window = source[start:start + 200]
    assert "event_type != 'recall'" in window


def test_format_event_strips_terminal_control_chars():
    from minni.watch import WatchEvent, format_event

    event = WatchEvent(ts="2026-07-15T10:00:00.000Z", agent="codex",
                       tool="minni_recall",
                       summary="\x1b[2J\x1b[31mevil\x07\x9b1m summary",
                       source="plugin",
                       details={"query": "\x1b]0;owned\x07q"})
    line = format_event(event)
    assert "\x1b" not in line and "\x07" not in line and "\x9b" not in line
    assert "evil" in line


def test_discover_vault_logs_skips_symlink_escape(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "evil-vault").symlink_to(outside)
    (home / "codex-vault").mkdir()
    logs = discover_vault_logs(home)
    assert home / "evil-vault" / "log.md" not in logs
    assert home / "codex-vault" / "log.md" in logs


def test_event_sort_key_orders_mixed_timestamp_formats():
    """Plugin timestamps are millisecond 'Z'-suffixed; daemon timestamps are
    microsecond '+00:00'. Lexicographic ordering inverts them; the sort key
    must parse."""
    from minni.watch import WatchEvent, event_sort_key

    earlier = WatchEvent(ts="2026-07-15T10:00:00.400Z", agent="a", tool="t",
                         summary="plugin first", source="plugin")
    later = WatchEvent(ts="2026-07-15T10:00:00.400500+00:00", agent="a",
                       tool="t", summary="daemon second", source="daemon")
    assert event_sort_key(earlier) < event_sort_key(later)
    # Lexicographic comparison would order these the other way around.
    assert earlier.ts > later.ts


def test_watch_rejects_non_positive_interval(tmp_path, monkeypatch, capsys):
    from minni import minni_cli

    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    assert minni_cli.main(["watch", "--interval", "0"]) == 2
    assert minni_cli.main(["watch", "--interval", "-1"]) == 2
    assert "interval" in capsys.readouterr().err

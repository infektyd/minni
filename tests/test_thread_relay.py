"""G3 daemon relay: cursors only, fail-closed, not graph state, no fake spawn."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

from minni import thread_relay
from minni.thread_relay import (
    GROK_WORKER_START,
    HOOKS_POLL_MINNI_THREAD_EVENTS,
    HOST_INJECTION_TABLE,
    RELAY_IS_GRAPH_STATE,
    SPAWNED,
    advance_cursor,
    attempt_immediate_wake,
    confirm_delivery,
    persist_cursor,
    read_pending_attention,
    rebuild_queue_from_journal,
    store_holds_graph_state,
)


EVENTS = (
    {
        "seq": 1,
        "kind": "slice.assigned",
        "actor": "orchestrator-g3",
        "at": "2026-08-20T12:00:00.000Z",
        "slice_id": "alpha",
    },
    {
        "seq": 2,
        "kind": "slice.completed",
        "actor": "worker-a",
        "at": "2026-08-20T12:01:00.000Z",
        "slice_id": "alpha",
    },
    {
        "seq": 3,
        "kind": "slice.completed",
        "actor": "worker-b",
        "at": "2026-08-20T12:02:00.000Z",
        "slice_id": "beta",
    },
)


def test_store_is_not_graph_state_and_does_not_spawn():
    assert RELAY_IS_GRAPH_STATE is False
    assert SPAWNED is False
    assert GROK_WORKER_START is None
    assert HOOKS_POLL_MINNI_THREAD_EVENTS is False
    assert store_holds_graph_state({"graph": False}) is False
    assert store_holds_graph_state({"graph": False, "slices": []}) is True
    source = inspect.getsource(thread_relay)
    for key in ("slices", "depends_on", "claim_token", "evidence"):
        assert f'"{key}"' not in source.split("GRAPH_KEYS", 1)[0]


def test_journal_seq_rebuilds_queue_and_omits_raw_evidence():
    queue = rebuild_queue_from_journal("plan-g3-relay", EVENTS, last_delivered_seq=1)
    assert [item["seq"] for item in queue] == [2, 3]
    assert "evidence" not in queue[0]["delta"]
    assert "Remember to" not in queue[0]["delta"]
    assert queue[0]["delta"].startswith("worker-a slice.completed plan=plan-g3-relay")


def test_fail_closed_cursor_is_monotonic():
    assert advance_cursor(1, 3, False) == 1
    assert advance_cursor(1, 0, True) == 1
    assert advance_cursor(1, 1, True) == 1
    assert advance_cursor(1, 3, True) == 3


def test_sqlite_cursor_survives_and_does_not_advance_on_failed_wake(tmp_path: Path):
    sql = Path(__file__).resolve().parents[1] / "src/minni/migrations/020_thread_delivery_cursors.sql"
    conn = sqlite3.connect(tmp_path / "relay.db")
    conn.executescript(sql.read_text(encoding="utf-8"))
    persist_cursor(conn, "orchestrator-g3", "plan-g3-relay", 0, "2026-08-20T12:00:00.000Z")
    behind = confirm_delivery(
        conn, "orchestrator-g3", "plan-g3-relay", 3, False, "2026-08-20T12:03:00.000Z"
    )
    assert behind == 0
    advanced = confirm_delivery(
        conn, "orchestrator-g3", "plan-g3-relay", 2, True, "2026-08-20T12:04:00.000Z"
    )
    assert advanced == 2
    pending = read_pending_attention("plan-g3-relay", EVENTS, advanced)
    assert pending["hooksPollThreadEvents"] is False
    assert pending["spawned"] is False
    assert [item["seq"] for item in pending["notifications"]] == [3]
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(thread_delivery_cursors)").fetchall()
    }
    assert cols == {"subscriber_id", "plan_id", "last_delivered_seq", "updated_at"}


def test_host_table_and_immediate_wake_are_fail_closed():
    assert HOST_INJECTION_TABLE["agy"]["stop"] == "rejects"
    assert HOST_INJECTION_TABLE["agy"]["wake"] == "deferred"
    assert HOST_INJECTION_TABLE["grok"]["sessionStart"] == "ignored"
    assert HOST_INJECTION_TABLE["grok"]["stop"] == "injects"
    assert HOST_INJECTION_TABLE["grok"]["grokWorkerStart"] is None
    assert HOST_INJECTION_TABLE["codex"]["wake"] == "unsupported"
    assert HOST_INJECTION_TABLE["cursor"]["wake"] == "unsupported"
    for host in ("agy", "grok", "codex", "cursor"):
        result = attempt_immediate_wake(host)
        assert result["spawned"] is False
        assert result["cursorAdvanced"] is False
        assert result["wetImmediate"] is False
        assert result["grokWorkerStart"] is None
        assert result["mode"] != "immediate"

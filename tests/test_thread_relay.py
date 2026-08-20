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
    SQLITE_020_LIVE,
    SPAWNED,
    advance_cursor,
    attempt_immediate_wake,
    confirm_delivery,
    confirm_delivery_store,
    empty_relay_store,
    host_hook_injects,
    ingest_journal_events,
    ingest_journal_into_vault,
    ingest_plan_journal_from_disk,
    persist_cursor,
    read_pending_attention,
    rebuild_queue_from_journal,
    relay_store_path,
    seed_relay_subscribers,
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
    assert SQLITE_020_LIVE is False
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


def test_confirm_true_only_when_host_hook_injects():
    assert host_hook_injects("grok", "sessionStart") is False
    assert host_hook_injects("grok", "promptSubmit") is False
    assert host_hook_injects("grok", "stop") is True
    assert host_hook_injects("agy", "sessionStart") is True
    assert host_hook_injects("agy", "stop") is False
    assert host_hook_injects("codex", "stop") is False
    assert host_hook_injects("cursor", "sessionStart") is False
    assert host_hook_injects("unknown", "sessionStart") is False


def test_a_confirming_seq_5_does_not_drop_b_pending_2_to_5():
    plan_id = "plan-g3-relay"
    seqs = [
        {
            "seq": seq,
            "kind": "slice.completed",
            "actor": "orchestrator-g3",
            "at": f"2026-08-20T12:0{seq}:00.000Z",
            "slice_id": "alpha",
        }
        for seq in range(1, 6)
    ]
    store = empty_relay_store()
    store["cursors"] = [
        {"subscriber_id": "subscriber-a", "plan_id": plan_id, "last_delivered_seq": 0},
        {"subscriber_id": "subscriber-b", "plan_id": plan_id, "last_delivered_seq": 1},
    ]
    store = ingest_journal_events(store, plan_id, seqs, ["subscriber-a", "subscriber-b"])
    b_before = [item["seq"] for item in store["pending"] if item["subscriber_id"] == "subscriber-b"]
    assert b_before == [2, 3, 4, 5]
    after = confirm_delivery_store(store, "subscriber-a", plan_id, 5, True)
    a_cursor = next(c for c in after["cursors"] if c["subscriber_id"] == "subscriber-a")
    b_cursor = next(c for c in after["cursors"] if c["subscriber_id"] == "subscriber-b")
    assert a_cursor["last_delivered_seq"] == 5
    assert b_cursor["last_delivered_seq"] == 1
    b_after = [item["seq"] for item in after["pending"] if item["subscriber_id"] == "subscriber-b"]
    assert b_after == [2, 3, 4, 5]


def test_production_ingest_writes_cursors_json(tmp_path: Path):
    plan_id = "plan-g3-relay"
    journal = tmp_path / "wiki" / "artifacts" / f"{plan_id}.log.md"
    journal.parent.mkdir(parents=True)
    payload = {
        "thread_event_batch": [
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
        ]
    }
    journal.write_text("# Minni Plan Journal\n\n## events\n" + __import__("json").dumps(payload) + "\n", encoding="utf-8")
    store = ingest_plan_journal_from_disk(tmp_path, plan_id, ["orchestrator-g3"])
    assert store["graph"] is False
    assert relay_store_path(tmp_path).is_file()
    seqs = [item["seq"] for item in store["pending"] if item["subscriber_id"] == "orchestrator-g3"]
    assert seqs == [1, 2]
    again = ingest_journal_into_vault(tmp_path, plan_id, EVENTS, ["orchestrator-g3"])
    assert [item["seq"] for item in again["pending"]] == [1, 2, 3]
    source = inspect.getsource(thread_relay)
    assert "def ingest_journal_into_vault" in source
    assert "def ingest_plan_journal_from_disk" in source


def test_old_writer_only_seed_misses_orchestrator_new_seed_notifies():
    worker = "worker-g3"
    landed = (
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
            "actor": worker,
            "at": "2026-08-20T12:01:00.000Z",
            "slice_id": "alpha",
        },
    )
    old_ids = [worker]  # old seed: {append actor} ∪ existing cursors (none)
    assert old_ids == [worker]
    old_store = ingest_journal_events(empty_relay_store(), "plan-g3-relay", landed, old_ids)
    orch_old = [
        item for item in old_store["pending"] if item["subscriber_id"] == "orchestrator-g3"
    ]
    assert orch_old == [], "old seed: worker append, orchestrator reads empty"
    worker_old = [item for item in old_store["pending"] if item["subscriber_id"] == worker]
    assert worker_old, "old seed notifies the writer only"

    new_ids = seed_relay_subscribers(
        actor=worker,
        plan_id="plan-g3-relay",
        cursors=[],
        events=landed,
        extra_ids=["orchestrator-g3"],
    )
    assert "orchestrator-g3" in new_ids
    new_store = ingest_journal_events(empty_relay_store(), "plan-g3-relay", landed, new_ids)
    orch_new = [
        item for item in new_store["pending"] if item["subscriber_id"] == "orchestrator-g3"
    ]
    assert orch_new, "new seed: worker append, orchestrator has pending"
    assert any(item["actor"] == worker for item in orch_new)


def test_sqlite_020_is_not_a_production_writer():
    assert SQLITE_020_LIVE is False
    minnid = Path(__file__).resolve().parents[1] / "src/minni/minnid.py"
    source = minnid.read_text(encoding="utf-8")
    assert "thread_relay" not in source
    assert "thread_delivery_cursors" not in source
    assert "ingest_journal_into_vault" not in source
    relay = inspect.getsource(thread_relay)
    assert "SQLITE_020_LIVE = False" in relay

"""G3 daemon relay: cursors only, fail-closed, not graph state, no fake spawn."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from minni import thread_relay
from minni.thread_relay import (
    GROK_WORKER_START,
    HOOKS_POLL_MINNI_THREAD_EVENTS,
    HOST_INJECTION_TABLE,
    MAX_RELAY_CURSORS,
    RELAY_IS_GRAPH_STATE,
    SPAWNED,
    SQLITE_020_LIVE,
    _relay_store_lock,
    acknowledge_relay_through_seq,
    acknowledge_self_authored_prefix_in_vault,
    active_plan_ids_from_vault,
    advance_cursor,
    attempt_immediate_wake,
    confirm_delivery,
    confirm_delivery_store,
    discover_subscribers,
    empty_relay_store,
    ensure_session_relay_cursor,
    format_session_subscriber_id,
    host_hook_injects,
    ingest_journal_events,
    ingest_journal_into_vault,
    ingest_plan_journal_from_disk,
    load_relay_store,
    parse_journal_events_from_log,
    parse_session_subscriber_id,
    persist_cursor,
    read_pending_attention,
    read_pending_attention_from_vault,
    rebuild_queue_from_journal,
    relay_lock_path,
    relay_store_path,
    save_relay_store,
    seed_relay_subscribers,
    store_holds_graph_state,
    sweep_dead_relay_cursors,
    vault_principal_subscriber_id,
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

def test_vault_principal_is_fail_closed():
    assert vault_principal_subscriber_id("/tmp/gemini-vault") == "gemini"
    assert vault_principal_subscriber_id("/tmp/grok-build-vault") == "grok-build"
    assert vault_principal_subscriber_id("/tmp/orchestrator-g3-vault") == "orchestrator-g3"
    assert vault_principal_subscriber_id("/tmp/plain-dir") is None
    assert vault_principal_subscriber_id("/tmp/vault") is None


def test_worker_only_journal_seeds_vault_principal_or_stays_empty(tmp_path):
    worker = "worker-g3"
    landed = (
        {
            "seq": 1,
            "kind": "slice.completed",
            "actor": worker,
            "at": "2026-08-20T12:01:00.000Z",
            "slice_id": "alpha",
        },
    )
    honest = tmp_path / "orchestrator-g3-vault"
    honest.mkdir()
    assert vault_principal_subscriber_id(honest) == "orchestrator-g3"
    extra = [vault_principal_subscriber_id(honest)]
    ids = seed_relay_subscribers(
        actor=worker,
        plan_id="plan-g3-relay",
        cursors=[],
        events=landed,
        extra_ids=extra,
    )
    assert "orchestrator-g3" in ids
    store = ingest_journal_events(empty_relay_store(), "plan-g3-relay", landed, ids)
    orch = [item for item in store["pending"] if item["subscriber_id"] == "orchestrator-g3"]
    assert orch, "worker-only journal: vault principal must still see pending"
    assert any(item["actor"] == worker for item in orch)

    closed = tmp_path / "plain-dir"
    closed.mkdir()
    assert vault_principal_subscriber_id(closed) is None
    closed_ids = seed_relay_subscribers(
        actor=worker,
        plan_id="plan-g3-relay",
        cursors=[],
        events=landed,
        extra_ids=[vid for vid in [vault_principal_subscriber_id(closed)] if vid],
    )
    assert "orchestrator-g3" not in closed_ids
    closed_store = ingest_journal_events(empty_relay_store(), "plan-g3-relay", landed, closed_ids)
    orch_closed = [
        item for item in closed_store["pending"] if item["subscriber_id"] == "orchestrator-g3"
    ]
    assert orch_closed == [], "no honest vault principal: empty is correct"
    worker_pending = [item for item in closed_store["pending"] if item["subscriber_id"] == worker]
    assert worker_pending, "writer is still seeded as the append actor"


def test_class_a_claude_code_host_table_and_acknowledge_through_seq(tmp_path: Path):
    assert host_hook_injects("claude-code", "sessionStart") is True
    assert host_hook_injects("claude-code", "promptSubmit") is True
    assert host_hook_injects("claude-code", "stop") is False
    assert host_hook_injects("claude", "sessionStart") is True
    assert host_hook_injects("claude", "stop") is False

    plan_id = "plan-g3-relay"
    ingest_journal_into_vault(tmp_path, plan_id, EVENTS, ["claude-code"])
    ack_fn = getattr(thread_relay, "acknowledge_relay_through_seq", None)
    assert callable(ack_fn)
    updated = ack_fn(tmp_path, plan_id, 3, ["claude-code"])
    cursor = next(c for c in updated["cursors"] if c["subscriber_id"] == "claude-code")
    assert cursor["last_delivered_seq"] == 3
    assert [p for p in updated["pending"] if p["subscriber_id"] == "claude-code"] == []


def test_session_scoped_subscriber_id_and_cursor_fork():
    fmt = thread_relay.format_session_subscriber_id
    parse = thread_relay.parse_subscriber_principal
    fork = thread_relay.fork_session_cursor

    key_a = "a1b2c3d4e5f60718"
    assert fmt("claude-code", key_a) == f"claude-code:{key_a}"
    assert fmt("claude-code", "default") == "claude-code"
    assert fmt("claude-code", "../../bad") == "claude-code"
    assert parse(f"claude-code:{key_a}") == ("claude-code", key_a)
    assert parse("claude-code") == ("claude-code", None)

    store = empty_relay_store()
    store["cursors"].append(
        {"subscriber_id": "claude-code", "plan_id": "plan-1", "last_delivered_seq": 4}
    )
    forked = fork(store, f"claude-code:{key_a}", "plan-1")
    assert forked["last_delivered_seq"] == 4
    assert isinstance(forked.get("updated_at"), str) and forked["updated_at"].endswith("Z")
    seeded = seed_relay_subscribers(
        actor="claude-code",
        actor_session_key=key_a,
        plan_id="plan-1",
    )
    assert f"claude-code:{key_a}" in seeded
    assert "claude-code" in seeded


def test_parse_actor_session_key_variants_and_session_subscriber_id():
    key_hex = "a1b2c3d4e5f60718"
    key_pid = "pid-host_42-alpha"
    assert parse_session_subscriber_id(f"claude-code:{key_hex}") == ("claude-code", key_hex)
    assert parse_session_subscriber_id(f"claude-code:{key_pid}") == ("claude-code", key_pid)
    assert parse_session_subscriber_id("claude-code:default") == ("claude-code:default", None)
    assert format_session_subscriber_id("claude-code", key_pid) == f"claude-code:{key_pid}"

    raw_log = "\n".join(
        [
            json.dumps(
                {
                    "seq": 1,
                    "kind": "scar_added",
                    "actor": "claude-code",
                    "actor_session_key": key_hex,
                    "at": "2026-11-10T10:00:00.000Z",
                }
            ),
            json.dumps(
                {
                    "seq": 2,
                    "kind": "replan",
                    "actor": "claude-code",
                    "actor_session_key": key_pid,
                    "at": "2026-11-10T10:01:00.000Z",
                }
            ),
            json.dumps(
                {
                    "seq": 3,
                    "kind": "scar_added",
                    "actor": "claude-code",
                    "actor_session_key": "default",
                    "at": "2026-11-10T10:02:00.000Z",
                }
            ),
            json.dumps(
                {
                    "seq": 4,
                    "kind": "scar_added",
                    "actor": "claude-code",
                    "actor_session_key": "../bad:inject",
                    "at": "2026-11-10T10:03:00.000Z",
                }
            ),
        ]
    )
    events = parse_journal_events_from_log(raw_log)
    assert [ev.get("actor_session_key") for ev in events] == [
        key_hex,
        key_pid,
        "default",
        None,
    ]


def test_two_sessions_independent_worker_delivery_q2_sibling_scar_and_bare_watermark(
    tmp_path: Path,
):
    vault = tmp_path / "claudecode-vault"
    vault.mkdir()
    plan_id = "plan-msp-shared"
    key_a = "1111222233334444"
    key_b = "aaaabbbbccccdddd"
    sub_a = format_session_subscriber_id("claude-code", key_a)
    sub_b = format_session_subscriber_id("claude-code", key_b)

    ensure_session_relay_cursor(vault, plan_id, sub_a)
    ensure_session_relay_cursor(vault, plan_id, sub_b)

    # 1. Worker codex completes slice alpha (seq=1)
    events_step_1 = [
        {
            "seq": 1,
            "kind": "slice_completed",
            "actor": "codex",
            "at": "2026-11-10T12:00:00.000Z",
            "slice_id": "alpha",
        }
    ]
    ingest_journal_into_vault(vault, plan_id, events_step_1, ["claude-code", sub_a, sub_b])

    att_a1 = read_pending_attention_from_vault(vault, sub_a, plan_id)
    att_b1 = read_pending_attention_from_vault(vault, sub_b, plan_id)
    assert [n["seq"] for n in att_a1["notifications"]] == [1]
    assert [n["seq"] for n in att_b1["notifications"]] == [1]

    # keyA confirms seq=1; keyB MUST still receive seq=1 and bare claude-code watermark stays 0
    after_a_ack = confirm_delivery(vault, sub_a, plan_id, 1, True)
    cursor_a = next(c for c in after_a_ack["cursors"] if c["subscriber_id"] == sub_a)
    cursor_b = next(c for c in after_a_ack["cursors"] if c["subscriber_id"] == sub_b)
    cursor_bare = next(c for c in after_a_ack["cursors"] if c["subscriber_id"] == "claude-code")
    assert cursor_a["last_delivered_seq"] == 1
    assert cursor_b["last_delivered_seq"] == 0
    assert cursor_bare["last_delivered_seq"] == 0
    assert cursor_a["updated_at"].endswith("Z")

    assert read_pending_attention_from_vault(vault, sub_a, plan_id)["notifications"] == []
    att_b_after_a = read_pending_attention_from_vault(vault, sub_b, plan_id)
    assert [n["seq"] for n in att_b_after_a["notifications"]] == [1]

    # keyB confirms seq=1; bare claude-code watermark now advances to min(1, 1) == 1
    after_b_ack = confirm_delivery(vault, sub_b, plan_id, 1, True)
    cursor_bare_2 = next(c for c in after_b_ack["cursors"] if c["subscriber_id"] == "claude-code")
    assert cursor_bare_2["last_delivered_seq"] == 1
    assert [
        p for p in after_b_ack["pending"] if p["subscriber_id"] == "claude-code"
    ] == []

    # 2. Q2 = Option A: keyA authors a scar at seq=2; auto-acks for keyA while delivering to keyB
    events_step_2 = [
        *events_step_1,
        {
            "seq": 2,
            "kind": "scar_added",
            "actor": "claude-code",
            "actor_session_key": key_a,
            "at": "2026-11-10T12:02:00.000Z",
        },
    ]
    ingest_journal_into_vault(vault, plan_id, events_step_2, ["claude-code", sub_a, sub_b])
    after_self_ack = acknowledge_self_authored_prefix_in_vault(
        vault, plan_id, ["claude-code"], session_key=key_a
    )
    cursor_a_scar = next(c for c in after_self_ack["cursors"] if c["subscriber_id"] == sub_a)
    cursor_b_scar = next(c for c in after_self_ack["cursors"] if c["subscriber_id"] == sub_b)
    cursor_bare_scar = next(
        c for c in after_self_ack["cursors"] if c["subscriber_id"] == "claude-code"
    )
    assert cursor_a_scar["last_delivered_seq"] == 2
    assert cursor_b_scar["last_delivered_seq"] == 1
    assert cursor_bare_scar["last_delivered_seq"] == 1
    assert read_pending_attention_from_vault(vault, sub_a, plan_id)["notifications"] == []
    att_b_scar = read_pending_attention_from_vault(vault, sub_b, plan_id)
    assert [n["kind"] for n in att_b_scar["notifications"]] == ["scar_added"]

    # 3. Sibling guard in acknowledge_relay_through_seq + self-ack prefix stop on sibling event:
    # Before keyB confirms seq=2, keyB authors its own scar at seq=3.
    # keyB's auto-ack MUST stop at seq=2 (authored by keyA) so seq=2 is NOT lost for keyB,
    # while seq=3 is not self-notified to keyB and IS delivered to keyA!
    events_step_3 = [
        *events_step_2,
        {
            "seq": 3,
            "kind": "scar_added",
            "actor": "claude-code",
            "actor_session_key": key_b,
            "at": "2026-11-10T12:03:00.000Z",
        },
    ]
    ingest_journal_into_vault(vault, plan_id, events_step_3, ["claude-code", sub_a, sub_b])
    after_b_self = acknowledge_self_authored_prefix_in_vault(
        vault, plan_id, ["claude-code"], session_key=key_b
    )
    cursor_b_still = next(c for c in after_b_self["cursors"] if c["subscriber_id"] == sub_b)
    assert cursor_b_still["last_delivered_seq"] == 1
    att_b_mixed = read_pending_attention_from_vault(vault, sub_b, plan_id)
    assert [n["seq"] for n in att_b_mixed["notifications"]] == [2]
    att_a_from_b = read_pending_attention_from_vault(vault, sub_a, plan_id)
    assert [n["seq"] for n in att_a_from_b["notifications"]] == [3]

    # Confirming seq=3 via acknowledge_relay_through_seq for keyA leaves bare watermark at 1 until keyB catches up
    ack_a = acknowledge_relay_through_seq(
        vault, plan_id, 3, ["claude-code"], session_key=key_a
    )
    assert (
        next(c for c in ack_a["cursors"] if c["subscriber_id"] == "claude-code")[
            "last_delivered_seq"
        ]
        == 1
    )
    ack_b = acknowledge_relay_through_seq(
        vault, plan_id, 3, ["claude-code"], session_key=key_b
    )
    assert (
        next(c for c in ack_b["cursors"] if c["subscriber_id"] == "claude-code")[
            "last_delivered_seq"
        ]
        == 3
    )


def test_relay_store_lock_fails_closed_breaks_dead_pid_and_serializes_threads(
    tmp_path: Path,
):
    vault = tmp_path / "claudecode-vault"
    vault.mkdir()
    lock_dir = relay_lock_path(vault)
    lock_dir.parent.mkdir(parents=True, exist_ok=True)

    # 1. Live PID holding cursors.lock -> raises TimeoutError (never fails open)
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "acquiredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TimeoutError):
        with _relay_store_lock(vault, timeout_s=0.08):
            pass
    assert lock_dir.is_dir(), "live owner lock must never be broken on timeout"
    (lock_dir / "owner.json").unlink()
    lock_dir.rmdir()

    # 2. Dead PID (99999999) holding cursors.lock -> broken cleanly and re-acquired
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text(
        json.dumps(
            {
                "pid": 99999999,
                "acquiredAt": "2026-11-10T00:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )
    with _relay_store_lock(vault, timeout_s=0.5) as acquired_path:
        assert acquired_path == lock_dir
        owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
    assert not lock_dir.exists()

    # 3. Unreadable/missing owner.json older than 10.0s -> broken cleanly
    lock_dir.mkdir()
    stale_epoch = time.time() - 15.0
    os.utime(lock_dir, (stale_epoch, stale_epoch))
    with _relay_store_lock(vault, timeout_s=0.5):
        assert lock_dir.is_dir()
    assert not lock_dir.exists()

    # 4. Concurrent multi-threaded RMW updates under _relay_store_lock lose zero cursors
    plan_id = "plan-msp-concurrent"
    keys = [f"{i:016x}" for i in range(1, 9)]
    events = [
        {
            "seq": seq,
            "kind": "slice_completed",
            "actor": "codex",
            "at": f"2026-11-10T12:0{seq}:00.000Z",
            "slice_id": "s1",
        }
        for seq in range(1, 5)
    ]
    subs = ["claude-code", *[format_session_subscriber_id("claude-code", k) for k in keys]]
    ingest_journal_into_vault(vault, plan_id, events, subs)

    def _worker_ack(idx_and_key: tuple[int, str]) -> None:
        idx, sk = idx_and_key
        sub = format_session_subscriber_id("claude-code", sk)
        confirm_delivery(vault, sub, plan_id, (idx % 4) + 1, True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_worker_ack, enumerate(keys)))

    final_store = load_relay_store(vault)
    for idx, sk in enumerate(keys):
        sub = format_session_subscriber_id("claude-code", sk)
        cur = next(c for c in final_store["cursors"] if c["subscriber_id"] == sub)
        assert cur["last_delivered_seq"] == (idx % 4) + 1
    bare_cur = next(c for c in final_store["cursors"] if c["subscriber_id"] == "claude-code")
    assert bare_cur["last_delivered_seq"] == 1


def test_per_session_active_thread_subscriber_discovery(tmp_path: Path):
    vault = tmp_path / "claudecode-vault"
    artifacts = vault / "wiki" / "artifacts"
    session_dir = vault / ".runtime" / "active-thread.d"
    artifacts.mkdir(parents=True)
    session_dir.mkdir(parents=True)

    plan_window_a = "plan-msp-window-a"
    plan_window_b = "plan-msp-window-b"
    key_a = "aaaa1111bbbb2222"
    key_b = "cccc3333dddd4444"
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # _active_plan.json points ONLY to window A's plan, while window B's plan lives ONLY in active-thread.d
    (artifacts / "_active_plan.json").write_text(
        json.dumps(
            {
                "plan_id": plan_window_a,
                "set_at": now_iso,
                "owner_session_key": key_a,
                "owner_host_pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
    (session_dir / f"{key_b}.json").write_text(
        json.dumps(
            {
                "plan_id": plan_window_b,
                "agent_id": "claude-code",
                "host_pid": os.getpid(),
                "updated_at": now_iso,
            }
        ),
        encoding="utf-8",
    )

    active_plans = active_plan_ids_from_vault(vault)
    assert plan_window_a in active_plans
    assert plan_window_b in active_plans

    subs_b = discover_subscribers(vault, plan_window_b, actor="codex")
    assert f"claude-code:{key_b}" in subs_b
    assert "claude-code" in subs_b

    # Ingesting worker journal event into plan_window_b automatically discovers claude-code:<keyB>
    store = ingest_journal_into_vault(
        vault,
        plan_window_b,
        [
            {
                "seq": 1,
                "kind": "slice_completed",
                "actor": "codex",
                "at": now_iso,
                "slice_id": "s1",
            }
        ],
        ["codex"],
    )
    pending_b = read_pending_attention(store, f"claude-code:{key_b}", plan_window_b)
    assert [n["seq"] for n in pending_b["notifications"]] == [1]


def test_sweep_dead_relay_cursors_stamps_updated_at_and_protects_bare_and_live_cursors(
    tmp_path: Path,
):
    vault = tmp_path / "claudecode-vault"
    artifacts = vault / "wiki" / "artifacts"
    artifacts.mkdir(parents=True)
    plan_active = "plan-msp-active"
    plan_terminal = "plan-msp-done"

    # Write real plan artifacts on disk (including terminal plan) to prove GC never deletes them
    note_terminal = artifacts / f"{plan_terminal}.md"
    log_terminal = artifacts / f"{plan_terminal}.log.md"
    hist_terminal = artifacts / f"{plan_terminal}.history.jsonl"
    note_terminal.write_text("---\nplan_id: plan-msp-done\nstatus: done\n---\n", encoding="utf-8")
    log_terminal.write_text("# log\n", encoding="utf-8")
    hist_terminal.write_text("{}\n", encoding="utf-8")

    now_dt = datetime(2026, 11, 10, 18, 0, 0, tzinfo=timezone.utc)
    now_ms = now_dt.timestamp() * 1000.0
    old_iso = (now_dt - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    recent_iso = (now_dt - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")

    live_key = "1111aaaa2222bbbb"
    expired_empty_key = "3333cccc4444dddd"
    expired_pending_key = "5555eeee6666ffff"
    terminal_empty_key = "7777000088889999"
    legacy_unstamped_key = "aaaa9999bbbb0000"

    store = empty_relay_store()
    store["cursors"] = [
        # Bare <agent> cursor with old updated_at -> MUST NEVER be evicted
        {
            "subscriber_id": "claude-code",
            "plan_id": plan_active,
            "last_delivered_seq": 2,
            "updated_at": old_iso,
        },
        # Live session cursor -> MUST NEVER be evicted even if updated_at is old
        {
            "subscriber_id": f"claude-code:{live_key}",
            "plan_id": plan_active,
            "last_delivered_seq": 2,
            "updated_at": old_iso,
        },
        # Expired (>24h) dead session with empty pending -> MUST be pruned
        {
            "subscriber_id": f"claude-code:{expired_empty_key}",
            "plan_id": plan_active,
            "last_delivered_seq": 2,
            "updated_at": old_iso,
        },
        # Expired (>24h) session that still has unacked pending -> preserved until acked or session-swept
        {
            "subscriber_id": f"claude-code:{expired_pending_key}",
            "plan_id": plan_active,
            "last_delivered_seq": 1,
            "updated_at": old_iso,
        },
        # Recent cursor on terminal plan with empty pending -> MUST be pruned
        {
            "subscriber_id": f"claude-code:{terminal_empty_key}",
            "plan_id": plan_terminal,
            "last_delivered_seq": 1,
            "updated_at": recent_iso,
        },
        # Legacy cursor missing updated_at -> stamped with updated_at and kept
        {
            "subscriber_id": f"claude-code:{legacy_unstamped_key}",
            "plan_id": plan_active,
            "last_delivered_seq": 2,
        },
    ]
    store["pending"] = [
        {
            "seq": 2,
            "plan_id": plan_active,
            "subscriber_id": f"claude-code:{expired_pending_key}",
            "actor": "codex",
            "kind": "slice_completed",
            "delta": f"codex slice_completed plan={plan_active} seq=2",
            "at": recent_iso,
        }
    ]
    save_relay_store(vault, store)

    res = sweep_dead_relay_cursors(
        vault,
        now_ms=now_ms,
        live_session_keys={live_key},
        max_cursors=MAX_RELAY_CURSORS,
    )
    assert res["pruned_cursors"] == 2
    after = load_relay_store(vault)
    remaining_ids = {c["subscriber_id"] for c in after["cursors"]}
    assert "claude-code" in remaining_ids
    assert f"claude-code:{live_key}" in remaining_ids
    assert f"claude-code:{expired_pending_key}" in remaining_ids
    assert f"claude-code:{legacy_unstamped_key}" in remaining_ids
    assert f"claude-code:{expired_empty_key}" not in remaining_ids
    assert f"claude-code:{terminal_empty_key}" not in remaining_ids

    stamped = next(
        c
        for c in after["cursors"]
        if c["subscriber_id"] == f"claude-code:{legacy_unstamped_key}"
    )
    assert isinstance(stamped.get("updated_at"), str) and stamped["updated_at"].endswith("Z")

    # Cap enforcement: max_cursors=2 MUST NEVER evict bare <agent> or live_session_keys
    cap_res = sweep_dead_relay_cursors(
        vault,
        now_ms=now_ms,
        live_session_keys={live_key},
        max_cursors=2,
    )
    assert cap_res["pruned_cursors"] == 2
    capped = load_relay_store(vault)
    capped_ids = {c["subscriber_id"] for c in capped["cursors"]}
    assert capped_ids == {"claude-code", f"claude-code:{live_key}"}

    # Plan artifacts on disk are untouched
    assert note_terminal.is_file()
    assert log_terminal.is_file()
    assert hist_terminal.is_file()


def test_symlink_plant_under_runtime_is_rejected_and_legacy_bare_self_ack_works(
    tmp_path: Path,
):
    vault = tmp_path / "claudecode-vault"
    vault.mkdir()
    plan_id = "plan-msp-legacy"

    # Legacy single-session bare <agent> prefix ack stops at worker event
    ingest_journal_into_vault(
        vault,
        plan_id,
        [
            {
                "seq": 1,
                "kind": "slice_assigned",
                "actor": "claude-code",
                "at": "2026-11-10T10:00:00.000Z",
                "slice_id": "s1",
            },
            {
                "seq": 2,
                "kind": "slice_completed",
                "actor": "codex",
                "at": "2026-11-10T10:01:00.000Z",
                "slice_id": "s1",
            },
        ],
        ["claude-code"],
    )
    acked = acknowledge_self_authored_prefix_in_vault(vault, plan_id, ["claude-code"])
    bare_cur = next(c for c in acked["cursors"] if c["subscriber_id"] == "claude-code")
    assert bare_cur["last_delivered_seq"] == 1
    assert [p["seq"] for p in acked["pending"] if p["subscriber_id"] == "claude-code"] == [2]

    # Symlink plant at <vault>/.runtime/thread-relay is rejected fail-closed
    bad_vault = tmp_path / "symlinked-vault"
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (bad_vault / ".runtime").mkdir(parents=True)
    (bad_vault / ".runtime" / "thread-relay").symlink_to(outside)
    with pytest.raises(ValueError, match="Refusing symlink"):
        with _relay_store_lock(bad_vault):
            pass
    with pytest.raises(ValueError, match="Refusing symlink"):
        save_relay_store(bad_vault, empty_relay_store())

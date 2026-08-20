"""G3 notification relay — daemon delivery cursors, not graph state.

Journal seq rebuilds the pending queue. A failed wake leaves the cursor
behind the event. The cursor advances monotonically on successful
delivery only.

This module stores subscriber/plan delivery cursors. It does not store
slices, dependencies, claims, evidence, or any other Thread graph field.
Hooks read pending attention from the rebuilt queue; they do not poll
``minni_thread_events``.

Immediate wake is unsupported: no host has wet-tested it. G2 in-session
``minni_thread_worker_update`` complete is not a spawn. ``GROK_WORKER_START``
stays None. Default agy install stays CANNOT. Codex stays UNPROVEN.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

GROK_WORKER_START = None
HOOKS_POLL_MINNI_THREAD_EVENTS = False
RELAY_IS_GRAPH_STATE = False
SPAWNED = False

GRAPH_KEYS = frozenset(
    {
        "slices",
        "dependencies",
        "depends_on",
        "claims",
        "claim_token",
        "evidence",
        "gate",
        "assigned_to",
        "generation",
    }
)

HOST_INJECTION_TABLE: dict[str, dict[str, Any]] = {
    "agy": {
        "host": "agy",
        "sessionStart": "injects",
        "promptSubmit": "injects",
        "stop": "rejects",
        "wire": "SessionStart + PreInvocation injectSteps",
        "wake": "deferred",
        "wetImmediate": False,
        "spawned": False,
        "grokWorkerStart": None,
    },
    "grok": {
        "host": "grok",
        "sessionStart": "ignored",
        "promptSubmit": "ignored",
        "stop": "injects",
        "wire": "Stop injects; SessionStart/UPS stdout ignored; boot ~/.grok/rules",
        "wake": "deferred",
        "wetImmediate": False,
        "spawned": False,
        "grokWorkerStart": None,
    },
    "codex": {
        "host": "codex",
        "sessionStart": "injects",
        "promptSubmit": "injects",
        "stop": "cannot",
        "wire": "SessionStart + UserPromptSubmit; Stop cannot; UNPROVEN",
        "wake": "unsupported",
        "wetImmediate": False,
        "spawned": False,
        "grokWorkerStart": None,
    },
    "cursor": {
        "host": "cursor",
        "sessionStart": "out",
        "promptSubmit": "out",
        "stop": "out",
        "wire": "out",
        "wake": "unsupported",
        "wetImmediate": False,
        "spawned": False,
        "grokWorkerStart": None,
    },
}

CURSOR_SELECT = (
    "SELECT last_delivered_seq FROM thread_delivery_cursors "
    "WHERE subscriber_id = ? AND plan_id = ?"
)
CURSOR_UPSERT = (
    "INSERT INTO thread_delivery_cursors "
    "(subscriber_id, plan_id, last_delivered_seq, updated_at) "
    "VALUES (?, ?, ?, ?) "
    "ON CONFLICT(subscriber_id, plan_id) DO UPDATE SET "
    "last_delivered_seq = excluded.last_delivered_seq, "
    "updated_at = excluded.updated_at"
)


def store_holds_graph_state(payload: Mapping[str, Any]) -> bool:
    if payload.get("graph") is not False:
        return True
    return any(key in payload for key in GRAPH_KEYS)


def format_state_delta(event: Mapping[str, Any], plan_id: str) -> str:
    slice_id = event.get("slice_id")
    slice_bit = f" slice={slice_id}" if slice_id else ""
    return (
        f"{event['actor']} {event['kind']} plan={plan_id}{slice_bit} "
        f"seq={event['seq']}"
    )


def rebuild_queue_from_journal(
    plan_id: str,
    events: Sequence[Mapping[str, Any]],
    last_delivered_seq: int,
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: int(item["seq"])):
        seq = int(event["seq"])
        if seq <= last_delivered_seq:
            continue
        item = {
            "seq": seq,
            "plan_id": plan_id,
            "actor": event["actor"],
            "kind": event["kind"],
            "delta": format_state_delta(event, plan_id),
            "at": event["at"],
        }
        if event.get("slice_id"):
            item["slice_id"] = event["slice_id"]
        pending.append(item)
    return pending


def advance_cursor(
    last_delivered_seq: int,
    delivered_through_seq: int,
    delivery_succeeded: bool,
) -> int:
    if not delivery_succeeded:
        return last_delivered_seq
    if delivered_through_seq <= last_delivered_seq:
        return last_delivered_seq
    return delivered_through_seq


def attempt_immediate_wake(host: str) -> dict[str, Any]:
    row = HOST_INJECTION_TABLE[host]
    return {
        "host": host,
        "mode": row["wake"],
        "spawned": False,
        "cursorAdvanced": False,
        "grokWorkerStart": GROK_WORKER_START,
        "wetImmediate": False,
    }


def read_pending_attention(
    plan_id: str,
    events: Sequence[Mapping[str, Any]],
    last_delivered_seq: int,
) -> dict[str, Any]:
    notifications = rebuild_queue_from_journal(plan_id, events, last_delivered_seq)
    return {
        "notifications": notifications,
        "text": "\n".join(item["delta"] for item in notifications),
        "hooksPollThreadEvents": HOOKS_POLL_MINNI_THREAD_EVENTS,
        "spawned": False,
    }


def load_cursor(conn: Any, subscriber_id: str, plan_id: str) -> int:
    row = conn.execute(CURSOR_SELECT, (subscriber_id, plan_id)).fetchone()
    if row is None:
        return 0
    return int(row[0])


def persist_cursor(
    conn: Any,
    subscriber_id: str,
    plan_id: str,
    last_delivered_seq: int,
    updated_at: str,
) -> None:
    conn.execute(
        CURSOR_UPSERT,
        (subscriber_id, plan_id, last_delivered_seq, updated_at),
    )


def confirm_delivery(
    conn: Any,
    subscriber_id: str,
    plan_id: str,
    delivered_through_seq: int,
    delivery_succeeded: bool,
    updated_at: str,
) -> int:
    current = load_cursor(conn, subscriber_id, plan_id)
    advanced = advance_cursor(current, delivered_through_seq, delivery_succeeded)
    persist_cursor(conn, subscriber_id, plan_id, advanced, updated_at)
    return advanced

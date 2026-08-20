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

import json
from pathlib import Path

from typing import Any, Iterable, Mapping, Sequence

GROK_WORKER_START = None
HOOKS_POLL_MINNI_THREAD_EVENTS = False
RELAY_IS_GRAPH_STATE = False
SPAWNED = False
# SQLite 020 thread_delivery_cursors is unused in production.
# Live store is plugin .runtime/thread-relay/cursors.json on journal append.
# minnid does not call ingest_journal_into_vault.
SQLITE_020_LIVE = False

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

def host_hook_injects(host: str, hook: str) -> bool:
    """Confirm delivery=true only when this host's wire for this hook injects."""
    row = HOST_INJECTION_TABLE.get(host)
    if row is None:
        return False
    return row.get(hook) == "injects"


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
    subscriber_id: str | None = None,
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
        if subscriber_id is not None:
            item["subscriber_id"] = subscriber_id
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


RELAY_STORE_VERSION = 1


def empty_relay_store() -> dict[str, Any]:
    return {"version": RELAY_STORE_VERSION, "graph": False, "cursors": [], "pending": []}


def relay_store_path(vault_path: str | Path) -> Path:
    return Path(vault_path) / ".runtime" / "thread-relay" / "cursors.json"


def plan_journal_path(vault_path: str | Path, plan_id: str) -> Path:
    return Path(vault_path) / "wiki" / "artifacts" / f"{plan_id}.log.md"


def parse_journal_events_from_log(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        trimmed = line.strip()
        if not trimmed.startswith("{"):
            continue
        try:
            parsed = json.loads(trimmed)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        items = parsed.get("thread_event_batch")
        if not isinstance(items, list):
            items = []
        if isinstance(parsed.get("seq"), int) and isinstance(parsed.get("kind"), str):
            items = [*items, parsed]
        for row in items:
            if not isinstance(row, dict):
                continue
            if not isinstance(row.get("seq"), int) or not isinstance(row.get("kind"), str):
                continue
            event = {
                "seq": row["seq"],
                "kind": row["kind"],
                "actor": row.get("actor") if isinstance(row.get("actor"), str) else "",
                "at": row.get("at") if isinstance(row.get("at"), str) else "",
            }
            if isinstance(row.get("slice_id"), str) and row["slice_id"]:
                event["slice_id"] = row["slice_id"]
            events.append(event)
    events.sort(key=lambda item: int(item["seq"]))
    return events


def _cursor_for(store: Mapping[str, Any], subscriber_id: str, plan_id: str) -> dict[str, Any]:
    for cursor in store.get("cursors") or []:
        if cursor.get("subscriber_id") == subscriber_id and cursor.get("plan_id") == plan_id:
            return dict(cursor)
    return {"subscriber_id": subscriber_id, "plan_id": plan_id, "last_delivered_seq": 0}


def seed_relay_subscribers(
    *,
    actor: str | None = None,
    plan_id: str,
    cursors: Sequence[Mapping[str, Any]] | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
    extra_ids: Sequence[str] | None = None,
) -> list[str]:
    """Seed subscribers from landed journal actors, not only the writer.

    Old seed was ``{current append actor} ∪ existing cursors for that plan``.
    That notifies the writer on a cursor-less store and leaves the
    already-working orchestrator empty.
    """
    ids: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            ids.add(value.strip())

    add(actor)
    for cursor in cursors or ():
        if cursor.get("plan_id") == plan_id:
            add(cursor.get("subscriber_id"))
    for event in events or ():
        add(event.get("actor"))
    for extra in extra_ids or ():
        add(extra)
    return list(ids)


def ingest_journal_events(
    store: Mapping[str, Any],
    plan_id: str,
    events: Sequence[Mapping[str, Any]],
    subscriber_ids: Sequence[str],
) -> dict[str, Any]:
    """Rebuild per-subscriber pending from journal seq. Not graph state."""
    next_store = empty_relay_store()
    next_store["cursors"] = [dict(cursor) for cursor in store.get("cursors") or []]
    next_store["pending"] = [
        dict(item) for item in store.get("pending") or [] if item.get("plan_id") != plan_id
    ]
    seen: set[str] = set()
    for subscriber_id in subscriber_ids:
        seen.add(subscriber_id)
        cursor = _cursor_for(next_store, subscriber_id, plan_id)
        if not any(
            row.get("subscriber_id") == subscriber_id and row.get("plan_id") == plan_id
            for row in next_store["cursors"]
        ):
            next_store["cursors"].append(cursor)
        next_store["pending"].extend(
            rebuild_queue_from_journal(
                plan_id, events, int(cursor["last_delivered_seq"]), subscriber_id
            )
        )
    for cursor in next_store["cursors"]:
        if cursor.get("plan_id") == plan_id and cursor.get("subscriber_id") not in seen:
            next_store["pending"].extend(
                rebuild_queue_from_journal(
                    plan_id,
                    events,
                    int(cursor["last_delivered_seq"]),
                    str(cursor["subscriber_id"]),
                )
            )
    next_store["pending"].sort(
        key=lambda item: (
            int(item["seq"]),
            str(item.get("subscriber_id") or ""),
            str(item.get("plan_id") or ""),
        )
    )
    return next_store


def confirm_delivery_store(
    store: Mapping[str, Any],
    subscriber_id: str,
    plan_id: str,
    delivered_through_seq: int,
    delivery_succeeded: bool,
) -> dict[str, Any]:
    """Advance one subscriber's cursor. Prune only that subscriber's pending."""
    current = _cursor_for(store, subscriber_id, plan_id)
    advanced_seq = advance_cursor(
        int(current["last_delivered_seq"]), delivered_through_seq, delivery_succeeded
    )
    advanced = {**current, "last_delivered_seq": advanced_seq}
    next_store = empty_relay_store()
    next_store["cursors"] = [
        dict(cursor)
        for cursor in store.get("cursors") or []
        if not (cursor.get("subscriber_id") == subscriber_id and cursor.get("plan_id") == plan_id)
    ]
    next_store["cursors"].append(advanced)
    next_store["pending"] = []
    for item in store.get("pending") or []:
        if item.get("subscriber_id") != subscriber_id:
            next_store["pending"].append(dict(item))
            continue
        if item.get("plan_id") != plan_id:
            next_store["pending"].append(dict(item))
            continue
        if int(item["seq"]) > advanced_seq:
            next_store["pending"].append(dict(item))
    return next_store


def load_relay_store(vault_path: str | Path) -> dict[str, Any]:
    path = relay_store_path(vault_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_relay_store()
    if not isinstance(raw, dict) or raw.get("version") != RELAY_STORE_VERSION or raw.get("graph") is not False:
        return empty_relay_store()
    return {
        "version": RELAY_STORE_VERSION,
        "graph": False,
        "cursors": list(raw["cursors"]) if isinstance(raw.get("cursors"), list) else [],
        "pending": list(raw["pending"]) if isinstance(raw.get("pending"), list) else [],
    }


def save_relay_store(vault_path: str | Path, store: Mapping[str, Any]) -> None:
    if store_holds_graph_state(store):
        raise ValueError("thread relay refuses graph state")
    path = relay_store_path(vault_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(store)
    payload["graph"] = False
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def ingest_journal_into_vault(
    vault_path: str | Path,
    plan_id: str,
    events: Sequence[Mapping[str, Any]],
    subscriber_ids: Sequence[str],
    conn: Any = None,
    updated_at: str = "",
) -> dict[str, Any]:
    """Library ingest into cursors.json. Production writer is plugin journal append.

    minnid does not call this. SQLite 020 is unused (SQLITE_020_LIVE is False).
    Optional conn is a leftover helper, not a live daemon path.
    Hooks read cursors.json. They do not poll minni_thread_events.
    """
    store = ingest_journal_events(
        load_relay_store(vault_path), plan_id, events, subscriber_ids
    )
    save_relay_store(vault_path, store)
    if conn is not None:
        stamp = updated_at or "1970-01-01T00:00:00.000Z"
        for subscriber_id in subscriber_ids:
            if load_cursor(conn, subscriber_id, plan_id) == 0:
                persist_cursor(conn, subscriber_id, plan_id, 0, stamp)
    return store


def ingest_plan_journal_from_disk(
    vault_path: str | Path,
    plan_id: str,
    subscriber_ids: Sequence[str],
    conn: Any = None,
    updated_at: str = "",
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    path = plan_journal_path(vault_path, plan_id)
    try:
        events = parse_journal_events_from_log(path.read_text(encoding="utf-8"))
    except OSError:
        events = []
    return ingest_journal_into_vault(
        vault_path, plan_id, events, subscriber_ids, conn=conn, updated_at=updated_at
    )

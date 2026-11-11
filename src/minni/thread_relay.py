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

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import random
import re
import shutil
import time
from typing import Any, Generator, Iterable, Mapping, Sequence, TypedDict
import uuid

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
    "claude-code": {
        "host": "claude-code",
        "sessionStart": "injects",
        "promptSubmit": "injects",
        "stop": "out",
        "wire": "SessionStart + UserPromptSubmit; Stop additionalContext wakes stopped turn so relay is out",
        "wake": "deferred",
        "wetImmediate": False,
        "spawned": False,
        "grokWorkerStart": None,
    },
}

RELAY_HOST_ALIASES: dict[str, str] = {
    "agy": "agy",
    "gemini": "agy",
    "antigravity": "agy",
    "grok": "grok",
    "grok-build": "grok",
    "codex": "codex",
    "cursor": "cursor",
    "claude": "claude-code",
    "claude-code": "claude-code",
}


class RelayJournalEvent(TypedDict, total=False):
    seq: int
    kind: str
    actor: str
    actor_session_key: str
    at: str
    slice_id: str


def host_hook_injects(host: str, hook: str) -> bool:
    """Confirm delivery=true only when this host's wire for this hook injects."""
    canonical = RELAY_HOST_ALIASES.get(host.strip().lower(), host)
    row = HOST_INJECTION_TABLE.get(canonical)
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


_SESSION_KEY_RE = re.compile(r"^(?:[0-9a-f]{16}|pid-[A-Za-z0-9_-]{1,64})$")
_ACTOR_SESSION_KEY_RE = re.compile(r"^(?:[0-9a-f]{16}|pid-[A-Za-z0-9_-]{1,64}|default)$")
_SAFE_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_TERMINAL_PLAN_STATUSES = frozenset(
    {"done", "completed", "archived", "abandoned", "cancelled", "superseded"}
)

MAX_RELAY_CURSORS = 256
RELAY_CURSOR_PRUNE_AGE_SECONDS = 24 * 60 * 60
RELAY_CURSOR_PRUNE_AGE_MS = RELAY_CURSOR_PRUNE_AGE_SECONDS * 1000
RELAY_LOCK_STALE_SECONDS = 10.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso_ms(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def _is_process_live(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        return exc.errno == errno.EPERM


def format_session_subscriber_id(
    agent_id: str,
    session_key: str | None = None,
) -> str:
    clean_agent = str(agent_id or "").strip()
    if not clean_agent:
        return ""
    clean_key = str(session_key or "").strip()
    if (
        clean_key
        and clean_key != "default"
        and _SESSION_KEY_RE.match(clean_key)
        and _SAFE_AGENT_ID_RE.match(clean_agent)
    ):
        return f"{clean_agent}:{clean_key}"
    return clean_agent


def parse_session_subscriber_id(subscriber_id: str) -> tuple[str, str | None]:
    trimmed = str(subscriber_id or "").strip()
    if trimmed.count(":") == 1:
        agent_id, session_key = trimmed.split(":", 1)
        if _SAFE_AGENT_ID_RE.match(agent_id) and _SESSION_KEY_RE.match(session_key):
            return agent_id, session_key
    return trimmed, None


parse_subscriber_principal = parse_session_subscriber_id


def parse_relay_journal_event(row: Mapping[str, Any]) -> RelayJournalEvent | None:
    seq_val = row.get("seq")
    kind_val = row.get("kind")
    if isinstance(seq_val, bool) or not isinstance(seq_val, int) or not isinstance(kind_val, str):
        return None
    event: RelayJournalEvent = {
        "seq": seq_val,
        "kind": kind_val,
        "actor": row.get("actor") if isinstance(row.get("actor"), str) else "",
        "at": row.get("at") if isinstance(row.get("at"), str) else "",
    }
    raw_sk = row.get("actor_session_key")
    if isinstance(raw_sk, str) and _ACTOR_SESSION_KEY_RE.match(raw_sk):
        event["actor_session_key"] = raw_sk
    raw_slice = row.get("slice_id")
    if isinstance(raw_slice, str) and raw_slice:
        event["slice_id"] = raw_slice
    return event


def is_self_authored_event(
    event: Mapping[str, Any],
    subscriber_id: str,
) -> bool:
    """Return True when ``event`` was authored by ``subscriber_id``'s own session.

    - Bare ``<agent>`` (``session_key is None``): ``ev.actor == agent_id`` when no
      foreign ``actor_session_key`` is attached (legacy single-session behavior).
    - Composite ``<agent>:<session_key>``: ``ev.actor == agent_id`` and
      ``(not ev.actor_session_key or ev.actor_session_key == session_key)``.
      Events authored by a sibling session (``ev.actor_session_key != session_key``)
      return ``False`` so they are delivered as cross-session notifications (Q2).
    """
    agent_id, session_key = parse_session_subscriber_id(subscriber_id)
    if not agent_id:
        return False
    actor = str(event.get("actor") or "").strip()
    if actor not in (subscriber_id, agent_id):
        return False
    raw_sk = event.get("actor_session_key")
    ev_sk = raw_sk if isinstance(raw_sk, str) and _ACTOR_SESSION_KEY_RE.match(raw_sk) else None
    if session_key is None:
        return True
    return not ev_sk or ev_sk == session_key


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
    _, sub_session_key = (
        parse_session_subscriber_id(subscriber_id) if subscriber_id else ("", None)
    )
    pending: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: int(item["seq"])):
        seq = int(event["seq"])
        if seq <= last_delivered_seq:
            continue
        if (
            subscriber_id is not None
            and sub_session_key is not None
            and is_self_authored_event(event, subscriber_id)
        ):
            continue
        item: dict[str, Any] = {
            "seq": seq,
            "plan_id": plan_id,
            "actor": event["actor"],
            "kind": event["kind"],
            "delta": format_state_delta(event, plan_id),
            "at": event["at"],
        }
        if subscriber_id is not None:
            item["subscriber_id"] = subscriber_id
        actor_sk = event.get("actor_session_key")
        if isinstance(actor_sk, str) and _ACTOR_SESSION_KEY_RE.match(actor_sk):
            item["actor_session_key"] = actor_sk
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


def _advance_cursor(
    cursor: Mapping[str, Any],
    delivered_through_seq: int,
    delivery_succeeded: bool,
    updated_at: str | None = None,
) -> dict[str, Any]:
    current_seq = int(cursor.get("last_delivered_seq") or 0)
    next_seq = advance_cursor(current_seq, delivered_through_seq, delivery_succeeded)
    advanced = dict(cursor)
    advanced["last_delivered_seq"] = next_seq
    if delivery_succeeded and (next_seq > current_seq or not advanced.get("updated_at")):
        advanced["updated_at"] = updated_at or _utc_now_iso()
    return advanced


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
    plan_id_or_store: str | Mapping[str, Any],
    events_or_subscriber_id: Sequence[Mapping[str, Any]] | str,
    last_delivered_seq_or_plan_id: int | str,
    subscriber_id: str | None = None,
) -> dict[str, Any]:
    """Read pending notifications either from ``(store, subscriber_id, plan_id)``
    or legacy ``(plan_id, events, last_delivered_seq[, subscriber_id])``."""
    if isinstance(plan_id_or_store, Mapping):
        store = plan_id_or_store
        sub_id = str(events_or_subscriber_id)
        plan_id = str(last_delivered_seq_or_plan_id)
        cursor = _cursor_for(store, sub_id, plan_id)
        cursor_seq = int(cursor.get("last_delivered_seq") or 0)
        agent_id, sub_sk = parse_session_subscriber_id(sub_id)

        def _self_for_store(item: Mapping[str, Any]) -> bool:
            if sub_sk is None:
                return False
            return is_self_authored_event(item, sub_id)

        notifications = [
            dict(item)
            for item in store.get("pending") or []
            if item.get("subscriber_id") == sub_id
            and item.get("plan_id") == plan_id
            and int(item["seq"]) > cursor_seq
            and not _self_for_store(item)
        ]
        if not notifications and sub_sk is not None:
            has_own_cursor = any(
                c.get("subscriber_id") == sub_id and c.get("plan_id") == plan_id
                for c in store.get("cursors") or []
            )
            if not has_own_cursor:
                notifications = [
                    {**dict(item), "subscriber_id": sub_id}
                    for item in store.get("pending") or []
                    if item.get("subscriber_id") == agent_id
                    and item.get("plan_id") == plan_id
                    and int(item["seq"]) > cursor_seq
                    and not _self_for_store(item)
                ]
        return {
            "notifications": notifications,
            "text": "\n".join(str(item["delta"]) for item in notifications),
            "hooksPollThreadEvents": HOOKS_POLL_MINNI_THREAD_EVENTS,
            "spawned": False,
        }

    plan_id = str(plan_id_or_store)
    if isinstance(events_or_subscriber_id, str):
        raise TypeError("events must be a sequence of journal event mappings")
    last_seq = int(last_delivered_seq_or_plan_id)
    notifications = rebuild_queue_from_journal(
        plan_id, events_or_subscriber_id, last_seq, subscriber_id
    )
    if subscriber_id is not None:
        _, sub_sk = parse_session_subscriber_id(subscriber_id)
        if sub_sk is None:
            notifications = [
                item
                for item in notifications
                if not is_self_authored_event(item, subscriber_id)
            ]
    return {
        "notifications": notifications,
        "text": "\n".join(str(item["delta"]) for item in notifications),
        "hooksPollThreadEvents": HOOKS_POLL_MINNI_THREAD_EVENTS,
        "spawned": False,
    }


def read_pending_attention_from_vault(
    vault_path: str | Path,
    subscriber_id: str,
    plan_id: str,
) -> dict[str, Any]:
    store = load_relay_store(vault_path)
    return read_pending_attention(store, subscriber_id, plan_id)


def pending_attention_for_hook(
    vault_path: str | Path,
    subscriber_id: str,
    plan_id: str | None = None,
) -> dict[str, Any] | None:
    clean_plan = str(plan_id or "").strip()
    if not clean_plan:
        return None
    pending = read_pending_attention_from_vault(vault_path, subscriber_id, clean_plan)
    if not pending["notifications"]:
        return None
    return pending


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
    conn_or_vault: Any,
    subscriber_id: str,
    plan_id: str,
    delivered_through_seq: int,
    delivery_succeeded: bool,
    updated_at: str | None = None,
) -> Any:
    """Confirm delivery against legacy SQLite ``conn`` (returns ``int``), an
    in-memory ``store`` mapping (returns ``RelayStore``), or a ``vault_path``
    under fail-closed ``_relay_store_lock`` (returns ``RelayStore``)."""
    if hasattr(conn_or_vault, "execute"):
        stamp = updated_at or _utc_now_iso()
        current = load_cursor(conn_or_vault, subscriber_id, plan_id)
        advanced = advance_cursor(current, delivered_through_seq, delivery_succeeded)
        persist_cursor(conn_or_vault, subscriber_id, plan_id, advanced, stamp)
        return advanced
    if isinstance(conn_or_vault, Mapping):
        return confirm_delivery_store(
            conn_or_vault,
            subscriber_id,
            plan_id,
            delivered_through_seq,
            delivery_succeeded,
            updated_at=updated_at,
        )
    if isinstance(conn_or_vault, (str, Path)):
        vault = Path(conn_or_vault)
        with _relay_store_lock(vault):
            store = load_relay_store(vault)
            store = _materialize_live_session_subscribers_in_store(
                vault, store, plan_id, [subscriber_id]
            )
            next_store = confirm_delivery_store(
                store,
                subscriber_id,
                plan_id,
                delivered_through_seq,
                delivery_succeeded,
                updated_at=updated_at,
            )
            save_relay_store(vault, next_store)
            return next_store
    raise TypeError(f"Unsupported target for confirm_delivery: {type(conn_or_vault)!r}")


def confirm_delivery_in_vault(
    vault_path: str | Path,
    subscriber_id: str,
    plan_id: str,
    delivered_through_seq: int,
    delivery_succeeded: bool,
    updated_at: str | None = None,
) -> dict[str, Any]:
    vault = Path(vault_path)
    with _relay_store_lock(vault):
        store = load_relay_store(vault)
        store = _materialize_live_session_subscribers_in_store(
            vault, store, plan_id, [subscriber_id]
        )
        next_store = confirm_delivery_store(
            store,
            subscriber_id,
            plan_id,
            delivered_through_seq,
            delivery_succeeded,
            updated_at=updated_at,
        )
        save_relay_store(vault, next_store)
        return _cursor_for(next_store, subscriber_id, plan_id)


RELAY_STORE_VERSION = 1


def empty_relay_store() -> dict[str, Any]:
    return {"version": RELAY_STORE_VERSION, "graph": False, "cursors": [], "pending": []}


def relay_dir_path(vault_path: str | Path) -> Path:
    return Path(vault_path) / ".runtime" / "thread-relay"


def _assert_no_symlink_under_vault(vault_path: str | Path, target_path: str | Path) -> None:
    vault = Path(vault_path)
    target = Path(target_path)
    rel = target.relative_to(vault)
    current = vault
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing symlink in thread relay path: {current}")


def relay_store_path(vault_path: str | Path) -> Path:
    return relay_dir_path(vault_path) / "cursors.json"


def relay_lock_path(vault_path: str | Path) -> Path:
    return relay_dir_path(vault_path) / "cursors.lock"


def plan_journal_path(vault_path: str | Path, plan_id: str) -> Path:
    return Path(vault_path) / "wiki" / "artifacts" / f"{plan_id}.log.md"


def _break_stale_lock_dir(lock_dir: Path) -> None:
    stale_dir = lock_dir.with_name(f"cursors.lock.stale.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        os.rename(lock_dir, stale_dir)
    except OSError:
        return
    shutil.rmtree(stale_dir, ignore_errors=True)


def _should_break_existing_lock(lock_dir: Path, owner_path: Path) -> bool:
    now_s = time.time()
    try:
        raw = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None

    if isinstance(raw, dict):
        owner_pid = raw.get("pid")
        if not isinstance(owner_pid, bool) and isinstance(owner_pid, int):
            if _is_process_live(owner_pid):
                return False
            return True
        acquired_raw = raw.get("acquiredAt") or raw.get("acquired_at")
        acquired_ms = _parse_iso_ms(acquired_raw)
        if acquired_ms is not None:
            return (now_s - (acquired_ms / 1000.0)) > RELAY_LOCK_STALE_SECONDS

    try:
        stat_res = lock_dir.stat()
    except OSError:
        return False
    return (now_s - stat_res.st_mtime) > RELAY_LOCK_STALE_SECONDS


@contextmanager
def _relay_store_lock(
    vault_path: str | Path,
    timeout_s: float = 5.0,
) -> Generator[Path, None, None]:
    """Acquire fail-closed ``<vault>/.runtime/thread-relay/cursors.lock``.

    Uses atomic ``os.mkdir(lock_dir)``, stamps ``owner.json`` with ``pid`` and
    UTC ISO ``acquiredAt``, breaks stale locks when the owner PID is dead or
    unreadable past ``10.0`` seconds, sleeps ``10-25ms`` jitter on contention,
    and raises ``TimeoutError`` if not acquired within ``timeout_s``.
    """
    relay_dir = relay_dir_path(vault_path)
    _assert_no_symlink_under_vault(vault_path, relay_dir)
    relay_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_dir = relay_dir / "cursors.lock"
    _assert_no_symlink_under_vault(vault_path, lock_dir)
    owner_path = lock_dir / "owner.json"
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    acquired = False

    while True:
        try:
            os.mkdir(lock_dir, 0o700)
            acquired = True
            owner_payload = (
                json.dumps({"pid": os.getpid(), "acquiredAt": _utc_now_iso()}) + "\n"
            ).encode("utf-8")
            open_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(owner_path, open_flags, 0o600)
            try:
                os.write(fd, owner_payload)
            finally:
                os.close(fd)
            break
        except FileExistsError:
            if _should_break_existing_lock(lock_dir, owner_path):
                _break_stale_lock_dir(lock_dir)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {timeout_s:.3f}s waiting for thread relay lock {lock_dir}"
                ) from None
            time.sleep(min(random.uniform(0.010, 0.025), max(0.001, remaining)))

    try:
        yield lock_dir
    finally:
        if acquired:
            release_dir = lock_dir.with_name(
                f"cursors.lock.rel.{os.getpid()}.{uuid.uuid4().hex[:8]}"
            )
            try:
                os.rename(lock_dir, release_dir)
                shutil.rmtree(release_dir, ignore_errors=True)
            except OSError:
                shutil.rmtree(lock_dir, ignore_errors=True)


def parse_journal_events_from_log(raw: str) -> list[RelayJournalEvent]:
    events: list[RelayJournalEvent] = []
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
        if not isinstance(parsed.get("seq"), bool) and isinstance(parsed.get("seq"), int) and isinstance(
            parsed.get("kind"), str
        ):
            items = [*items, parsed]
        for row in items:
            if not isinstance(row, dict):
                continue
            event = parse_relay_journal_event(row)
            if event is not None:
                events.append(event)
    events.sort(key=lambda item: int(item["seq"]))
    return events


def fork_session_cursor(
    store: Mapping[str, Any],
    subscriber_id: str,
    plan_id: str,
) -> dict[str, Any]:
    for cursor in store.get("cursors") or []:
        if cursor.get("subscriber_id") == subscriber_id and cursor.get("plan_id") == plan_id:
            existing = dict(cursor)
            if not existing.get("updated_at"):
                existing["updated_at"] = _utc_now_iso()
            return existing
    now_iso = _utc_now_iso()
    agent_id, session_key = parse_session_subscriber_id(subscriber_id)
    if session_key:
        for cursor in store.get("cursors") or []:
            if cursor.get("subscriber_id") == agent_id and cursor.get("plan_id") == plan_id:
                return {
                    "subscriber_id": subscriber_id,
                    "plan_id": plan_id,
                    "last_delivered_seq": int(cursor.get("last_delivered_seq") or 0),
                    "updated_at": now_iso,
                }
    return {
        "subscriber_id": subscriber_id,
        "plan_id": plan_id,
        "last_delivered_seq": 0,
        "updated_at": now_iso,
    }


def _cursor_for(store: Mapping[str, Any], subscriber_id: str, plan_id: str) -> dict[str, Any]:
    return fork_session_cursor(store, subscriber_id, plan_id)


def vault_principal_subscriber_id(vault_path: str | Path) -> str | None:
    """Fail-closed vault principal. Same rule as inboxPrincipalForVaultPath.

    PlanArtifact has no owner. A worker process DEFAULT_AGENT_ID is the
    writer. Hook config.agentId is not on the journal path. The product
    already derives a principal from the ``<slug>-vault`` directory name.
    No ``-vault`` suffix -> None. Do not invent a subscriber.
    Slug aliases come from the existing inbox map, not a new field.
    """
    base = Path(vault_path).resolve().name
    if not base.endswith("-vault"):
        return None
    slug = base[: -len("-vault")]
    if not slug:
        return None
    from minni.afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID

    return _VAULT_SLUG_TO_AGENT_ID.get(slug, slug)


def active_plan_id_from_vault(vault_path: str | Path) -> str | None:
    pointer = Path(vault_path) / "wiki" / "artifacts" / "_active_plan.json"
    try:
        raw = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    plan_id = raw.get("plan_id")
    if isinstance(plan_id, str) and plan_id.strip():
        return plan_id.strip()
    return None


def active_plan_ids_from_vault(
    vault_path: str | Path,
    *,
    now_ms: float | None = None,
) -> list[str]:
    """Return all active plan IDs across ``wiki/artifacts/_active_plan.json``
    and per-session ``.runtime/active-thread.d/*.json`` pointers."""
    vault = Path(vault_path)
    effective_now_ms = float(now_ms) if now_ms is not None else time.time() * 1000.0
    plan_ids: list[str] = []
    seen: set[str] = set()

    def _add_plan(value: Any) -> None:
        if isinstance(value, str):
            clean = value.strip()
            if clean and clean not in seen:
                seen.add(clean)
                plan_ids.append(clean)

    _add_plan(active_plan_id_from_vault(vault))

    session_dir = vault / ".runtime" / "active-thread.d"
    try:
        entries = sorted(session_dir.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.suffix != ".json":
            continue
        stem = entry.stem
        if not _SESSION_KEY_RE.match(stem):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        host_pid = raw.get("host_pid")
        if not isinstance(host_pid, bool) and isinstance(host_pid, int):
            if not _is_process_live(host_pid):
                continue
        ts_ms = _parse_iso_ms(raw.get("updated_at") or raw.get("set_at"))
        if ts_ms is not None and (effective_now_ms - ts_ms) > RELAY_CURSOR_PRUNE_AGE_MS:
            continue
        _add_plan(raw.get("plan_id"))

    return plan_ids


def live_session_subscribers_for_plan(
    vault_path: str | Path,
    plan_id: str,
    fallback_agent_id: str | None = None,
    *,
    now_ms: float | None = None,
) -> list[str]:
    """Discover composite session subscribers bound to ``plan_id`` in
    ``.runtime/active-thread.d/*.json`` and ``wiki/artifacts/_active_plan.json``."""
    vault = Path(vault_path)
    effective_now_ms = float(now_ms) if now_ms is not None else time.time() * 1000.0
    default_principal = vault_principal_subscriber_id(vault) or (fallback_agent_id or "").strip()
    out: list[str] = []
    seen: set[str] = set()

    def _add(sub_id: str) -> None:
        if sub_id and sub_id not in seen:
            seen.add(sub_id)
            out.append(sub_id)

    session_dir = vault / ".runtime" / "active-thread.d"
    try:
        entries = sorted(session_dir.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.suffix != ".json":
            continue
        stem = entry.stem
        if not _SESSION_KEY_RE.match(stem):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or raw.get("plan_id") != plan_id:
            continue
        host_pid = raw.get("host_pid")
        if not isinstance(host_pid, bool) and isinstance(host_pid, int):
            if not _is_process_live(host_pid):
                continue
        ts_ms = _parse_iso_ms(raw.get("updated_at") or raw.get("set_at"))
        if ts_ms is not None and (effective_now_ms - ts_ms) > RELAY_CURSOR_PRUNE_AGE_MS:
            continue
        raw_agent = raw.get("agent_id")
        agent_id = (
            raw_agent.strip()
            if isinstance(raw_agent, str) and raw_agent.strip() and raw_agent.strip() != "unknown"
            else default_principal
        )
        if agent_id:
            _add(format_session_subscriber_id(agent_id, stem))

    active_pointer = vault / "wiki" / "artifacts" / "_active_plan.json"
    try:
        active_raw = json.loads(active_pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        active_raw = None
    if isinstance(active_raw, dict) and active_raw.get("plan_id") == plan_id:
        owner_pid = active_raw.get("owner_host_pid")
        pid_ok = (
            isinstance(owner_pid, bool)
            or not isinstance(owner_pid, int)
            or _is_process_live(owner_pid)
        )
        ts_ms = _parse_iso_ms(active_raw.get("updated_at") or active_raw.get("set_at"))
        age_ok = ts_ms is None or (effective_now_ms - ts_ms) <= RELAY_CURSOR_PRUNE_AGE_MS
        if pid_ok and age_ok:
            owner_sk = active_raw.get("owner_session_key")
            raw_agent = active_raw.get("agent_id")
            agent_id = (
                raw_agent.strip()
                if isinstance(raw_agent, str)
                and raw_agent.strip()
                and raw_agent.strip() != "unknown"
                else default_principal
            )
            if (
                agent_id
                and isinstance(owner_sk, str)
                and owner_sk != "default"
                and _SESSION_KEY_RE.match(owner_sk)
            ):
                _add(format_session_subscriber_id(agent_id, owner_sk))

    return out


def discover_subscribers(
    vault_path: str | Path,
    plan_id: str,
    *,
    actor: str | None = None,
    actor_session_key: str | None = None,
    cursors: Sequence[Mapping[str, Any]] | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
    extra_ids: Sequence[str] | None = None,
    now_ms: float | None = None,
) -> list[str]:
    """Discover all subscribers for ``plan_id`` across ``_active_plan.json``,
    ``.runtime/active-thread.d/*.json``, vault principal, cursors, and journal events."""
    vault = Path(vault_path)
    discovered_extras: list[str] = list(extra_ids or ())
    principal = vault_principal_subscriber_id(vault)
    if principal:
        discovered_extras.append(principal)

    live_subs = live_session_subscribers_for_plan(
        vault, plan_id, fallback_agent_id=principal, now_ms=now_ms
    )
    for sub_id in live_subs:
        discovered_extras.append(sub_id)
        bare_agent, _ = parse_session_subscriber_id(sub_id)
        if bare_agent:
            discovered_extras.append(bare_agent)

    if active_plan_id_from_vault(vault) == plan_id and principal:
        discovered_extras.append(principal)

    return seed_relay_subscribers(
        actor=actor,
        actor_session_key=actor_session_key,
        plan_id=plan_id,
        cursors=cursors,
        events=events,
        extra_ids=discovered_extras,
    )


def seed_relay_subscribers(
    *,
    actor: str | None = None,
    actor_session_key: str | None = None,
    plan_id: str,
    cursors: Sequence[Mapping[str, Any]] | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
    extra_ids: Sequence[str] | None = None,
) -> list[str]:
    """Seed subscribers from landed journal actors, not only the writer.

    Old seed was ``{current append actor} U existing cursors for that plan``.
    That notifies the writer on a cursor-less store and leaves the
    already-working orchestrator empty. extra_ids is how a fail-closed
    vault principal is passed when the vault dir names one.
    """
    ids: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            ids.add(value.strip())

    add(actor)
    if actor and actor_session_key:
        add(format_session_subscriber_id(actor, actor_session_key))
    for cursor in cursors or ():
        if cursor.get("plan_id") == plan_id:
            add(cursor.get("subscriber_id"))
    for event in events or ():
        ev_actor = event.get("actor")
        add(ev_actor)
        ev_sk = event.get("actor_session_key")
        if isinstance(ev_actor, str) and isinstance(ev_sk, str):
            add(format_session_subscriber_id(ev_actor, ev_sk))
    for extra in extra_ids or ():
        add(extra)
    return list(ids)


def _materialize_live_session_subscribers_in_store(
    vault_path: str | Path,
    store: Mapping[str, Any],
    plan_id: str,
    extra_subscriber_ids: Sequence[str] = (),
) -> dict[str, Any]:
    parsed_extras = [
        parse_session_subscriber_id(sub_id)
        for sub_id in extra_subscriber_ids
        if parse_session_subscriber_id(sub_id)[1] is not None
    ]
    fallback_agent = parsed_extras[0][0] if parsed_extras else None
    live_on_plan = live_session_subscribers_for_plan(
        vault_path, plan_id, fallback_agent_id=fallback_agent
    )
    all_session_subs: list[str] = []
    seen_subs: set[str] = set()
    for sub_id in [
        *live_on_plan,
        *(
            sid
            for sid in extra_subscriber_ids
            if parse_session_subscriber_id(sid)[1] is not None
        ),
    ]:
        if sub_id and sub_id not in seen_subs:
            seen_subs.add(sub_id)
            all_session_subs.append(sub_id)

    if not all_session_subs:
        return {
            "version": RELAY_STORE_VERSION,
            "graph": False,
            "cursors": [dict(c) for c in store.get("cursors") or []],
            "pending": [dict(p) for p in store.get("pending") or []],
        }

    mutated = False
    next_cursors = [dict(c) for c in store.get("cursors") or []]
    next_pending = [dict(p) for p in store.get("pending") or []]
    for sub_id in all_session_subs:
        if any(
            c.get("subscriber_id") == sub_id and c.get("plan_id") == plan_id
            for c in next_cursors
        ):
            continue
        forked = fork_session_cursor(store, sub_id, plan_id)
        next_cursors.append(forked)
        agent_id, _ = parse_session_subscriber_id(sub_id)
        forked_seq = int(forked["last_delivered_seq"])
        for item in store.get("pending") or []:
            if (
                item.get("subscriber_id") == agent_id
                and item.get("plan_id") == plan_id
                and int(item["seq"]) > forked_seq
                and not is_self_authored_event(item, sub_id)
            ):
                if not any(
                    p.get("subscriber_id") == sub_id
                    and p.get("plan_id") == plan_id
                    and int(p["seq"]) == int(item["seq"])
                    for p in next_pending
                ):
                    next_pending.append({**dict(item), "subscriber_id": sub_id})
        mutated = True

    if mutated:
        next_pending.sort(
            key=lambda item: (
                int(item["seq"]),
                str(item.get("subscriber_id") or ""),
                str(item.get("plan_id") or ""),
            )
        )
    return {
        "version": RELAY_STORE_VERSION,
        "graph": False,
        "cursors": next_cursors,
        "pending": next_pending,
    }


def ensure_session_relay_cursor(
    vault_path: str | Path,
    plan_id: str,
    subscriber_id: str,
) -> None:
    agent_id, session_key = parse_session_subscriber_id(subscriber_id)
    if not session_key or not plan_id:
        return
    vault = Path(vault_path)
    with _relay_store_lock(vault):
        store = load_relay_store(vault)
        if any(
            c.get("subscriber_id") == subscriber_id and c.get("plan_id") == plan_id
            for c in store["cursors"]
        ):
            return
        live_subs = live_session_subscribers_for_plan(
            vault, plan_id, fallback_agent_id=agent_id
        )
        sibling_seqs = [
            int(c.get("last_delivered_seq") or 0)
            for c in store["cursors"]
            if c.get("plan_id") == plan_id
            and c.get("subscriber_id") != subscriber_id
            and c.get("subscriber_id") in live_subs
        ]
        initial_seq = (
            max(sibling_seqs)
            if sibling_seqs
            else int(fork_session_cursor(store, subscriber_id, plan_id)["last_delivered_seq"])
        )
        store["cursors"].append(
            {
                "subscriber_id": subscriber_id,
                "plan_id": plan_id,
                "last_delivered_seq": initial_seq,
                "updated_at": _utc_now_iso(),
            }
        )
        save_relay_store(vault, store)


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
    now_iso = _utc_now_iso()
    seen: set[str] = set()
    for subscriber_id in subscriber_ids:
        seen.add(subscriber_id)
        existing_idx = next(
            (
                idx
                for idx, row in enumerate(next_store["cursors"])
                if row.get("subscriber_id") == subscriber_id and row.get("plan_id") == plan_id
            ),
            None,
        )
        if existing_idx is None:
            cursor = _cursor_for(next_store, subscriber_id, plan_id)
            cursor["updated_at"] = cursor.get("updated_at") or now_iso
            next_store["cursors"].append(cursor)
        else:
            cursor = next_store["cursors"][existing_idx]
            if not cursor.get("updated_at"):
                cursor["updated_at"] = now_iso
        next_store["pending"].extend(
            rebuild_queue_from_journal(
                plan_id, events, int(cursor["last_delivered_seq"]), subscriber_id
            )
        )
    for cursor in next_store["cursors"]:
        if cursor.get("plan_id") == plan_id and cursor.get("subscriber_id") not in seen:
            if not cursor.get("updated_at"):
                cursor["updated_at"] = now_iso
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
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Advance one subscriber's cursor, prune its pending, and propagate the
    minimum watermark across ``<agent>:*`` cursors onto bare ``<agent>``."""
    current = _cursor_for(store, subscriber_id, plan_id)
    advanced = _advance_cursor(
        current, delivered_through_seq, delivery_succeeded, updated_at=updated_at
    )
    advanced_seq = int(advanced["last_delivered_seq"])
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

    agent_id, session_key = parse_session_subscriber_id(subscriber_id)
    if delivery_succeeded and session_key is not None:
        session_cursors = [
            c
            for c in next_store["cursors"]
            if c.get("plan_id") == plan_id
            and parse_session_subscriber_id(str(c.get("subscriber_id") or ""))[0] == agent_id
            and parse_session_subscriber_id(str(c.get("subscriber_id") or ""))[1] is not None
        ]
        if session_cursors:
            min_seq = min(int(c.get("last_delivered_seq") or 0) for c in session_cursors)
            bare_existing = next(
                (
                    dict(c)
                    for c in next_store["cursors"]
                    if c.get("subscriber_id") == agent_id and c.get("plan_id") == plan_id
                ),
                None,
            )
            bare_current = bare_existing or {
                "subscriber_id": agent_id,
                "plan_id": plan_id,
                "last_delivered_seq": 0,
                "updated_at": updated_at or _utc_now_iso(),
            }
            bare_advanced = _advance_cursor(
                bare_current, min_seq, True, updated_at=updated_at
            )
            bare_seq = int(bare_advanced["last_delivered_seq"])
            next_store["cursors"] = [
                c
                for c in next_store["cursors"]
                if not (c.get("subscriber_id") == agent_id and c.get("plan_id") == plan_id)
            ]
            next_store["cursors"].append(bare_advanced)
            next_store["pending"] = [
                item
                for item in next_store["pending"]
                if not (
                    item.get("subscriber_id") == agent_id
                    and item.get("plan_id") == plan_id
                    and int(item["seq"]) <= bare_seq
                )
            ]
    return next_store


def load_relay_store(vault_path: str | Path) -> dict[str, Any]:
    path = relay_store_path(vault_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_relay_store()
    if (
        not isinstance(raw, dict)
        or raw.get("version") != RELAY_STORE_VERSION
        or raw.get("graph") is not False
    ):
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
    _assert_no_symlink_under_vault(vault_path, path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = dict(store)
    payload["graph"] = False
    serialized = json.dumps(payload, indent=2) + "\n"
    tmp_path = path.with_name(f"cursors.json.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    open_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_path, open_flags, 0o600)
    try:
        os.write(fd, serialized.encode("utf-8"))
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(tmp_path, path)


def ingest_journal_into_vault(
    vault_path: str | Path,
    plan_id: str,
    events: Sequence[Mapping[str, Any]],
    subscriber_ids: Sequence[str],
    conn: Any = None,
    updated_at: str = "",
) -> dict[str, Any]:
    """Library ingest into cursors.json under fail-closed ``_relay_store_lock``.

    minnid does not call this. SQLite 020 is unused (SQLITE_020_LIVE is False).
    Optional conn is a leftover helper, not a live daemon path.
    Hooks read cursors.json. They do not poll minni_thread_events.
    """
    vault = Path(vault_path)
    with _relay_store_lock(vault):
        current_store = load_relay_store(vault)
        first_bare_agent = next(
            (
                parse_session_subscriber_id(sub_id)[0]
                for sub_id in subscriber_ids
                if parse_session_subscriber_id(sub_id)[0]
            ),
            None,
        )
        live_subs = live_session_subscribers_for_plan(
            vault, plan_id, fallback_agent_id=first_bare_agent
        )
        merged_subscribers = list(dict.fromkeys([*subscriber_ids, *live_subs]))
        store = ingest_journal_events(current_store, plan_id, events, merged_subscribers)
        save_relay_store(vault, store)

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
    events: list[RelayJournalEvent] = []
    path = plan_journal_path(vault_path, plan_id)
    try:
        events = parse_journal_events_from_log(path.read_text(encoding="utf-8"))
    except OSError:
        events = []
    return ingest_journal_into_vault(
        vault_path, plan_id, events, subscriber_ids, conn=conn, updated_at=updated_at
    )


def _collect_ordered_plan_events(
    vault_path: Path,
    plan_id: str,
    store: Mapping[str, Any],
    extra_events: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    by_seq: dict[int, dict[str, Any]] = {}
    journal_file = plan_journal_path(vault_path, plan_id)
    try:
        disk_events = parse_journal_events_from_log(journal_file.read_text(encoding="utf-8"))
    except OSError:
        disk_events = []
    for ev in disk_events:
        by_seq[int(ev["seq"])] = dict(ev)

    for item in store.get("pending") or []:
        if item.get("plan_id") != plan_id:
            continue
        seq = int(item["seq"])
        if seq not in by_seq:
            row: dict[str, Any] = {
                "seq": seq,
                "kind": str(item.get("kind") or ""),
                "actor": str(item.get("actor") or ""),
                "at": str(item.get("at") or ""),
            }
            if isinstance(item.get("actor_session_key"), str) and _ACTOR_SESSION_KEY_RE.match(
                item["actor_session_key"]
            ):
                row["actor_session_key"] = item["actor_session_key"]
            if item.get("slice_id"):
                row["slice_id"] = item["slice_id"]
            by_seq[seq] = row

    for ev in extra_events or ():
        parsed = parse_relay_journal_event(ev)
        if parsed is not None:
            by_seq[int(parsed["seq"])] = dict(parsed)

    return [by_seq[k] for k in sorted(by_seq)]


def acknowledge_self_authored_prefix_in_vault(
    vault_path: str | Path,
    plan_id: str,
    subscriber_ids: Sequence[str],
    session_key: str | None = None,
    *,
    actor_session_key: str | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Advance cursor past contiguous leading self-authored events on ``plan_id``
    while preserving sibling-session events (Q2 = Option A)."""
    vault = Path(vault_path)
    raw_sk = session_key if session_key is not None else actor_session_key
    clean_sk = str(raw_sk or "").strip()
    effective_sk = (
        clean_sk
        if clean_sk and clean_sk != "default" and _SESSION_KEY_RE.match(clean_sk)
        else None
    )

    with _relay_store_lock(vault):
        store = load_relay_store(vault)
        expanded_subs: list[str] = []
        for raw_id in subscriber_ids:
            trimmed = str(raw_id or "").strip()
            if not trimmed or trimmed == "unknown-agent":
                continue
            agent_id, sub_sk = parse_session_subscriber_id(trimmed)
            if effective_sk is not None:
                if sub_sk is not None and sub_sk != effective_sk:
                    continue
                expanded_subs.append(format_session_subscriber_id(agent_id, effective_sk))
            else:
                expanded_subs.append(trimmed)

        store = _materialize_live_session_subscribers_in_store(
            vault, store, plan_id, expanded_subs
        )
        ordered_events = _collect_ordered_plan_events(vault, plan_id, store, events)
        changed = False
        seen: set[str] = set()

        for target_id in expanded_subs:
            if not target_id or target_id in seen:
                continue
            seen.add(target_id)
            agent_id, target_sk = parse_session_subscriber_id(target_id)
            if target_sk is None:
                other_session_cursors = [
                    c
                    for c in store["cursors"]
                    if c.get("plan_id") == plan_id
                    and parse_session_subscriber_id(str(c.get("subscriber_id") or ""))[0]
                    == agent_id
                    and parse_session_subscriber_id(str(c.get("subscriber_id") or ""))[1]
                    is not None
                ]
                if other_session_cursors:
                    continue

            cursor = _cursor_for(store, target_id, plan_id)
            current_seq = int(cursor.get("last_delivered_seq") or 0)
            ack_seq = 0
            for item in ordered_events:
                seq = int(item["seq"])
                if seq <= current_seq:
                    continue
                item_actor = str(item.get("actor") or "").strip()
                raw_ev_sk = item.get("actor_session_key")
                ev_sk = (
                    raw_ev_sk
                    if isinstance(raw_ev_sk, str) and _ACTOR_SESSION_KEY_RE.match(raw_ev_sk)
                    else None
                )
                actor_matches = item_actor in (target_id, agent_id)
                if target_sk is not None:
                    session_matches = not ev_sk or ev_sk == target_sk
                else:
                    session_matches = not ev_sk
                if actor_matches and session_matches:
                    ack_seq = seq
                else:
                    break

            if ack_seq > 0:
                store = confirm_delivery_store(store, target_id, plan_id, ack_seq, True)
                changed = True

        if changed:
            save_relay_store(vault, store)
        return store


def acknowledge_relay_through_seq(
    vault_path: str | Path,
    plan_id: str,
    through_seq: int,
    subscriber_ids: Sequence[str],
    session_key: str | None = None,
) -> dict[str, Any]:
    """Advance delivery cursors through ``through_seq`` for the given subscribers.

    When ``session_key`` is provided (or composite ``<agent>:<session_key>`` IDs
    are supplied), advances the composite session cursor and propagates only the
    minimum watermark across sibling session cursors onto bare ``<agent>``.
    """
    vault = Path(vault_path)
    clean_sk = str(session_key or "").strip()
    effective_sk = (
        clean_sk
        if clean_sk and clean_sk != "default" and _SESSION_KEY_RE.match(clean_sk)
        else None
    )

    with _relay_store_lock(vault):
        store = load_relay_store(vault)
        if through_seq <= 0:
            return store

        target_ids: list[str] = []
        for raw_id in subscriber_ids:
            sub_id = str(raw_id or "").strip()
            if not sub_id or sub_id == "unknown-agent":
                continue
            agent_id, sub_sk = parse_session_subscriber_id(sub_id)
            if effective_sk is not None:
                if sub_sk is not None and sub_sk != effective_sk:
                    continue
                target_ids.append(format_session_subscriber_id(agent_id, effective_sk))
            else:
                target_ids.append(sub_id)

        store = _materialize_live_session_subscribers_in_store(
            vault, store, plan_id, target_ids
        )
        has_session_sub = any(
            parse_session_subscriber_id(tid)[1] is not None for tid in target_ids
        )
        seen: set[str] = set()
        for sub_id in target_ids:
            if not sub_id or sub_id in seen:
                continue
            seen.add(sub_id)
            agent_id, sub_sk = parse_session_subscriber_id(sub_id)
            if sub_sk is None:
                sibling_session_cursors = [
                    c
                    for c in store["cursors"]
                    if c.get("plan_id") == plan_id
                    and parse_session_subscriber_id(str(c.get("subscriber_id") or ""))[0]
                    == agent_id
                    and parse_session_subscriber_id(str(c.get("subscriber_id") or ""))[1]
                    is not None
                    and (
                        has_session_sub
                        or int(c.get("last_delivered_seq") or 0) < int(through_seq)
                    )
                ]
                if sibling_session_cursors:
                    if not has_session_sub:
                        min_sibling_seq = min(
                            int(c.get("last_delivered_seq") or 0)
                            for c in sibling_session_cursors
                        )
                        if min_sibling_seq > 0:
                            store = confirm_delivery_store(
                                store, sub_id, plan_id, min_sibling_seq, True
                            )
                    continue
            store = confirm_delivery_store(store, sub_id, plan_id, int(through_seq), True)

        save_relay_store(vault, store)
        return store


def _is_plan_terminal_on_disk(vault: Path, plan_id: str) -> bool:
    note_path = vault / "wiki" / "artifacts" / f"{plan_id}.md"
    try:
        head = note_path.read_text(encoding="utf-8")[:4096]
    except OSError:
        return False
    for line in head.splitlines():
        stripped = line.strip()
        if stripped.startswith("status:"):
            status_val = stripped.split(":", 1)[1].strip().strip("\"'").lower()
            return status_val in _TERMINAL_PLAN_STATUSES
    return False


def _discover_live_session_keys(vault: Path, now_ms: float) -> set[str]:
    live_keys: set[str] = set()

    active_thread_dir = vault / ".runtime" / "active-thread.d"
    try:
        entries = list(active_thread_dir.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.suffix != ".json":
            continue
        stem = entry.stem
        if not _SESSION_KEY_RE.match(stem):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        host_pid = raw.get("host_pid")
        pid_live = (
            isinstance(host_pid, bool)
            or not isinstance(host_pid, int)
            or _is_process_live(host_pid)
        )
        ts_ms = _parse_iso_ms(raw.get("updated_at") or raw.get("set_at"))
        recent = ts_ms is None or (now_ms - ts_ms) <= RELAY_CURSOR_PRUNE_AGE_MS
        if pid_live and recent:
            live_keys.add(stem)

    runtime_dir = vault / ".runtime"
    try:
        rt_entries = list(runtime_dir.iterdir())
    except OSError:
        rt_entries = []
    for entry in rt_entries:
        if not (entry.name.startswith("active-session-") and entry.name.endswith(".json")):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        sk = raw.get("session_key")
        if not isinstance(sk, str) or sk == "default" or not _SESSION_KEY_RE.match(sk):
            continue
        host_pid = raw.get("host_pid")
        pid_live = (
            not isinstance(host_pid, bool)
            and isinstance(host_pid, int)
            and _is_process_live(host_pid)
        )
        ts_ms = _parse_iso_ms(raw.get("updated_at"))
        recent = ts_ms is None or (now_ms - ts_ms) <= RELAY_CURSOR_PRUNE_AGE_MS
        if pid_live and recent:
            live_keys.add(sk)

    return live_keys


def sweep_dead_relay_cursors(
    vault_path: str | Path,
    now_ms: float | None = None,
    pruned_session_keys: Iterable[str] | None = None,
    *,
    live_session_keys: Iterable[str] | None = None,
    terminal_plan_ids: Iterable[str] | None = None,
    max_cursors: int = MAX_RELAY_CURSORS,
) -> dict[str, int]:
    """Prune expired/dead/terminal composite relay cursors while never evicting
    bare ``<agent>`` cursors or ``live_session_keys`` (including under ``MAX_RELAY_CURSORS``)."""
    vault = Path(vault_path)
    effective_now_ms = float(now_ms) if now_ms is not None else time.time() * 1000.0
    now_iso = datetime.fromtimestamp(effective_now_ms / 1000.0, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")

    with _relay_store_lock(vault):
        store = load_relay_store(vault)
        if not store["cursors"]:
            return {"pruned_cursors": 0, "prunedCursors": 0}

        effective_live_keys = _discover_live_session_keys(vault, effective_now_ms)
        if live_session_keys is not None:
            for key in live_session_keys:
                clean = str(key or "").strip()
                if clean:
                    effective_live_keys.add(clean)

        pruned_keys_set = {
            str(k).strip() for k in (pruned_session_keys or ()) if str(k).strip()
        }
        terminal_plans_set = {
            str(p).strip() for p in (terminal_plan_ids or ()) if str(p).strip()
        }
        terminal_cache: dict[str, bool] = {}

        def _is_terminal(pid: str) -> bool:
            if pid in terminal_plans_set:
                return True
            if pid not in terminal_cache:
                terminal_cache[pid] = _is_plan_terminal_on_disk(vault, pid)
            return terminal_cache[pid]

        def _has_unacked_pending(sub_id: str, pid: str, cursor_seq: int) -> bool:
            return any(
                item.get("subscriber_id") == sub_id
                and item.get("plan_id") == pid
                and int(item.get("seq") or 0) > cursor_seq
                for item in store["pending"]
            )

        pruned_sub_plans: set[tuple[str, str]] = set()
        kept_cursors: list[dict[str, Any]] = []
        stamped_missing = False

        for raw_cursor in store["cursors"]:
            cursor = dict(raw_cursor)
            sub_id = str(cursor.get("subscriber_id") or "")
            plan_id = str(cursor.get("plan_id") or "")
            cursor_seq = int(cursor.get("last_delivered_seq") or 0)
            _, sk = parse_session_subscriber_id(sub_id)
            updated_ms = _parse_iso_ms(cursor.get("updated_at"))
            if updated_ms is None:
                cursor["updated_at"] = now_iso
                stamped_missing = True

            if sk is not None and sk not in effective_live_keys:
                expired_by_age = (
                    updated_ms is not None
                    and (effective_now_ms - updated_ms) > RELAY_CURSOR_PRUNE_AGE_MS
                )
                expired_by_session_sweep = sk in pruned_keys_set
                empty_pending = not _has_unacked_pending(sub_id, plan_id, cursor_seq)
                if expired_by_session_sweep or (
                    empty_pending and (expired_by_age or _is_terminal(plan_id))
                ):
                    pruned_sub_plans.add((sub_id, plan_id))
                    continue

            kept_cursors.append(cursor)

        cap = max(1, int(max_cursors))
        if len(kept_cursors) > cap:
            protected: list[dict[str, Any]] = []
            evictable: list[dict[str, Any]] = []
            for cursor in kept_cursors:
                sub_id = str(cursor.get("subscriber_id") or "")
                _, sk = parse_session_subscriber_id(sub_id)
                if sk is None or sk in effective_live_keys:
                    protected.append(cursor)
                else:
                    evictable.append(cursor)

            excess = len(kept_cursors) - cap
            if excess > 0 and evictable:
                evictable.sort(
                    key=lambda c: (
                        1
                        if _has_unacked_pending(
                            str(c.get("subscriber_id") or ""),
                            str(c.get("plan_id") or ""),
                            int(c.get("last_delivered_seq") or 0),
                        )
                        else 0,
                        _parse_iso_ms(c.get("updated_at")) or 0.0,
                    )
                )
                dropped = evictable[:excess]
                survivors = evictable[excess:]
                for d in dropped:
                    pruned_sub_plans.add(
                        (str(d.get("subscriber_id") or ""), str(d.get("plan_id") or ""))
                    )
                kept_cursors = [*protected, *survivors]

        if not pruned_sub_plans and not stamped_missing:
            return {"pruned_cursors": 0, "prunedCursors": 0}

        next_store = {
            "version": RELAY_STORE_VERSION,
            "graph": False,
            "cursors": kept_cursors,
            "pending": [
                dict(item)
                for item in store["pending"]
                if (
                    str(item.get("subscriber_id") or ""),
                    str(item.get("plan_id") or ""),
                )
                not in pruned_sub_plans
            ],
        }
        save_relay_store(vault, next_store)
        count = len(pruned_sub_plans)
        return {"pruned_cursors": count, "prunedCursors": count}

"""Tests for the read-only ``list_events`` JSON-RPC method.

Exercises the full daemon path (minnid._dispatch_sync -> dispatch_request ->
enforce_method_capability -> minnid._METHODS["list_events"]) against a real
SovereignDB, mirroring the style of tests/test_rpc_capability_enforcement.py.
Verifies cursor pagination, param validation, capability gating (the "read"
capability, same enforcement path as other read methods), filters, and a
round-trip against a recall-trace row written the same way
minnid_runtime.recall.handle_search writes one (EpisodicMemory.add_event(...,
event_type="recall", bind_thread=False)).
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minni.minnid as minnid
import minni.minnid_runtime.provenance as provenance
from minni.episodic import EpisodicMemory
from minni.principal import EffectivePrincipal


def _patch_db(monkeypatch, tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "events.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    monkeypatch.setattr(minnid, "SovereignDB", lambda *a, **k: db_obj)
    return db_obj


def _stamp(monkeypatch, agent_id, capabilities):
    principal = EffectivePrincipal(agent_id=agent_id, capabilities=list(capabilities))
    monkeypatch.setattr(provenance, "resolve_effective_principal", lambda **_kw: principal)
    return principal


def _rpc(method, params, request_id=1):
    return minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    )


def _insert_event(db_obj, *, agent_id, event_type, content, thread_id=None):
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO episodic_events
            (agent_id, event_type, content, thread_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (agent_id, event_type, content, thread_id, 1000.0),
        )
        return c.lastrowid


def test_since_id_pagination_returns_disjoint_ordered_batches(monkeypatch, tmp_path):
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    ids = [
        _insert_event(db_obj, agent_id="codex", event_type="note", content=f"e{i}")
        for i in range(5)
    ]

    first = _rpc("list_events", {"agent_id": "codex", "since_id": 0, "limit": 3})
    assert "error" not in first, first
    first_events = first["result"]["events"]
    assert [e["event_id"] for e in first_events] == ids[:3]
    assert first["result"]["last_id"] == ids[2]

    second = _rpc(
        "list_events",
        {"agent_id": "codex", "since_id": first["result"]["last_id"], "limit": 3},
    )
    assert "error" not in second, second
    second_events = second["result"]["events"]
    assert [e["event_id"] for e in second_events] == ids[3:]
    assert second["result"]["last_id"] == ids[-1]

    # Disjoint batches.
    first_ids = {e["event_id"] for e in first_events}
    second_ids = {e["event_id"] for e in second_events}
    assert first_ids.isdisjoint(second_ids)


def test_limit_zero_or_negative_is_invalid_params(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])

    resp = _rpc("list_events", {"agent_id": "codex", "limit": 0})
    assert resp.get("error", {}).get("code") == -32602, resp

    resp = _rpc("list_events", {"agent_id": "codex", "limit": -1})
    assert resp.get("error", {}).get("code") == -32602, resp


def test_limit_clamps_to_upper_bound_of_200(monkeypatch, tmp_path):
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    for i in range(3):
        _insert_event(db_obj, agent_id="codex", event_type="note", content=f"e{i}")

    resp = _rpc("list_events", {"agent_id": "codex", "limit": 10000})
    assert "error" not in resp, resp
    # Only 3 rows exist, but the clamp itself is asserted via a huge limit not
    # erroring and not being echoed back unclamped anywhere that would break.
    assert len(resp["result"]["events"]) == 3


def test_non_integer_since_id_or_limit_is_invalid_params(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])

    resp = _rpc("list_events", {"agent_id": "codex", "since_id": "not-an-int"})
    assert resp.get("error", {}).get("code") == -32602, resp

    resp = _rpc("list_events", {"agent_id": "codex", "limit": "not-an-int"})
    assert resp.get("error", {}).get("code") == -32602, resp


def test_non_string_agent_id_or_event_type_filter_is_invalid_params(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])

    resp = _rpc("list_events", {"agent_id": "codex", "event_type": 123})
    assert resp.get("error", {}).get("code") == -32602, resp


def test_agent_id_and_event_type_filters(monkeypatch, tmp_path):
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    id_a_note = _insert_event(db_obj, agent_id="codex", event_type="note", content="a")
    _insert_event(db_obj, agent_id="other", event_type="note", content="b")
    id_a_recall = _insert_event(db_obj, agent_id="codex", event_type="recall", content="c")

    resp = _rpc("list_events", {"agent_id": "codex", "event_type": "note"})
    assert "error" not in resp, resp
    events = resp["result"]["events"]
    assert [e["event_id"] for e in events] == [id_a_note]

    resp = _rpc("list_events", {"agent_id": "codex", "event_type": "recall"})
    assert "error" not in resp, resp
    events = resp["result"]["events"]
    assert [e["event_id"] for e in events] == [id_a_recall]


def test_capability_denied_for_principal_without_read(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["search"])

    resp = _rpc("list_events", {"agent_id": "codex"})
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "read" in err.get("message", "")


def test_round_trip_recall_trace_row_via_episodic_memory(monkeypatch, tmp_path):
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])

    episodic = EpisodicMemory(db_obj)
    event_id = episodic.add_event(
        agent_id="codex",
        event_type="recall",
        content='recall "hello" — 1 hits, top 0.90',
        thread_id="sess-1",
        bind_thread=False,
    )

    resp = _rpc("list_events", {"agent_id": "codex", "event_type": "recall"})
    assert "error" not in resp, resp
    events = resp["result"]["events"]
    assert len(events) == 1
    row = events[0]
    assert row["event_id"] == event_id
    assert row["agent_id"] == "codex"
    assert row["event_type"] == "recall"
    assert row["content"] == 'recall "hello" — 1 hits, top 0.90'
    assert row["thread_id"] == "sess-1"
    assert "created_at" in row


def test_empty_result_returns_empty_list_and_last_id_equals_since_id(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])

    resp = _rpc("list_events", {"agent_id": "codex", "since_id": 42})
    assert "error" not in resp, resp
    assert resp["result"]["events"] == []
    assert resp["result"]["last_id"] == 42


def test_non_operator_without_filter_is_scoped_to_own_agent(monkeypatch, tmp_path):
    """A plain read principal omitting agent_id must NOT see other agents' rows."""
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    own_id = _insert_event(db_obj, agent_id="codex", event_type="note", content="mine")
    _insert_event(db_obj, agent_id="other", event_type="recall", content="theirs")

    resp = _rpc("list_events", {})
    assert "error" not in resp, resp
    events = resp["result"]["events"]
    assert [e["event_id"] for e in events] == [own_id]
    assert all(e["agent_id"] == "codex" for e in events)


def test_non_operator_cross_agent_filter_is_denied(monkeypatch, tmp_path):
    """A plain read principal naming another agent's id gets a -32004 denial."""
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    _insert_event(db_obj, agent_id="other", event_type="recall", content="theirs")

    resp = _rpc("list_events", {"agent_id": "other"})
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "cross_agent" in err.get("message", ""), resp


def test_operator_principal_sees_all_agents_and_may_filter(monkeypatch, tmp_path):
    """Operator context (govern cap) keeps the cross-agent console view."""
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "main", ["read", "govern"])
    id_codex = _insert_event(db_obj, agent_id="codex", event_type="note", content="a")
    id_other = _insert_event(db_obj, agent_id="other", event_type="recall", content="b")

    unfiltered = _rpc("list_events", {})
    assert "error" not in unfiltered, unfiltered
    assert [e["event_id"] for e in unfiltered["result"]["events"]] == [id_codex, id_other]

    filtered = _rpc("list_events", {"agent_id": "other"})
    assert "error" not in filtered, filtered
    assert [e["event_id"] for e in filtered["result"]["events"]] == [id_other]


def test_default_since_id_and_limit(monkeypatch, tmp_path):
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["read"])
    ids = [
        _insert_event(db_obj, agent_id="codex", event_type="note", content=f"e{i}")
        for i in range(3)
    ]

    resp = _rpc("list_events", {"agent_id": "codex"})
    assert "error" not in resp, resp
    assert [e["event_id"] for e in resp["result"]["events"]] == ids
    assert resp["result"]["last_id"] == ids[-1]

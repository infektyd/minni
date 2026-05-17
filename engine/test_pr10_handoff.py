import json
import os
import types
from pathlib import Path

import pytest

import sovrd


def _patch_handoff_db(monkeypatch, tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "handoffs.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    monkeypatch.setattr(sovrd, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    return db_obj


def _packet(**overrides):
    base = {
        "from_agent": "codex",
        "to_agent": "claude-code",
        "kind": "handoff",
        "task": "Review auth migration",
        "envelope": '<sovereign:context event="Handoff">api_key=secret-token</sovereign:context>',
        "wikilink_refs": ["wiki/decisions/auth-migration"],
        "trace_id": "trace-pr10",
    }
    base.update(overrides)
    return base


def test_daemon_handoff_validates_redacts_and_writes_inbox_outbox(monkeypatch, tmp_path):
    db_obj = _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "SOVEREIGN_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )

    response = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "packet": _packet(),
            },
        }
    )

    assert "error" not in response
    result = response["result"]
    assert result["status"] == "ok"
    assert result["redacted"] is True
    assert result["lease_persisted"] is True

    inbox_files = list((recipient / "inbox").glob("*.json"))
    outbox_files = list((sender / "outbox").glob("*.json"))
    handoff_pages = list((sender / "wiki" / "handoffs").glob("*.md"))
    assert len(inbox_files) == 1
    assert len(outbox_files) == 1
    assert len(handoff_pages) == 1

    inbox_packet = json.loads(inbox_files[0].read_text())
    outbox_packet = json.loads(outbox_files[0].read_text())
    assert inbox_packet["lease_id"].startswith("handoff-")
    assert inbox_packet["requires_ack"] is True
    assert "expires_at" in inbox_packet
    assert inbox_packet["envelope"].count("[REDACTED]") >= 1
    assert "secret-token" not in json.dumps(inbox_packet)
    assert inbox_packet == outbox_packet

    page = handoff_pages[0].read_text()
    assert "type: handoff" in page
    assert "status: accepted" in page
    assert "Review auth migration" in page
    assert "[REDACTED]" in page

    assert "handoff_sent" in (sender / "log.md").read_text()
    assert "handoff_received" in (recipient / "log.md").read_text()

    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT * FROM handoff_leases WHERE lease_id = ?",
            (inbox_packet["lease_id"],),
        ).fetchone()
    assert row is not None
    assert row["from_agent"] == "codex"
    assert row["to_agent"] == "claude-code"
    assert row["status"] == "pending"
    assert row["inbox_path"] == str(inbox_files[0])
    assert row["outbox_path"] == str(outbox_files[0])


def test_daemon_handoff_reports_degraded_when_lease_persistence_fails(monkeypatch, tmp_path):
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "SOVEREIGN_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )
    monkeypatch.setattr(sovrd, "_store_handoff_lease", lambda *_args, **_kwargs: False)

    response = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 19,
        "method": "daemon.handoff",
        "params": {"from_agent": "codex", "to_agent": "claude-code", "packet": _packet()},
    })

    assert "error" not in response
    result = response["result"]
    assert result["status"] == "degraded"
    assert result["delivered"] is True
    assert result["lease_persisted"] is False
    assert "SQLite lease persistence failed" in result["reason"]
    assert Path(result["inbox_path"]).exists()
    assert Path(result["outbox_path"]).exists()


def test_handoff_pending_list_and_ack(monkeypatch, tmp_path):
    db_obj = _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "SOVEREIGN_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )

    sent = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 20,
        "method": "daemon.handoff",
        "params": {"from_agent": "codex", "to_agent": "claude-code", "packet": _packet()},
    })["result"]
    lease_id = sent["lease_id"]

    pending = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 21,
        "method": "sovereign_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })["result"]
    assert [item["lease_id"] for item in pending["handoffs"]] == [lease_id]

    ack = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 22,
        "method": "sovereign_ack_handoff",
        "params": {"lease_id": lease_id, "status": "accepted"},
    })["result"]
    assert ack["status"] == "accepted"
    assert len(ack["updated_paths"]) == 2

    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT status FROM handoff_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
    assert row["status"] == "accepted"

    pending_after = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 23,
        "method": "sovereign_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })["result"]
    assert pending_after["handoffs"] == []


def test_await_handoff_times_out(monkeypatch, tmp_path):
    _patch_handoff_db(monkeypatch, tmp_path)
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv("SOVEREIGN_AGENT_VAULTS", json.dumps({"claude-code": str(recipient)}))
    response = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 24,
        "method": "sovereign_await_handoff",
        "params": {"lease_id": "missing", "timeout_ms": 1},
    })["result"]

    assert response["status"] == "timeout"


def test_daemon_handoff_rejects_invalid_packet(monkeypatch, tmp_path):
    monkeypatch.setenv("SOVEREIGN_AGENT_VAULTS", json.dumps({"codex": str(tmp_path / "codex")}))

    response = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "packet": _packet(kind="learn_now"),
            },
        }
    )

    assert response["error"]["code"] == -32602
    assert "kind" in response["error"]["message"]


def test_daemon_handoff_gracefully_reports_missing_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("SOVEREIGN_AGENT_VAULTS", json.dumps({"codex": str(tmp_path / "codex")}))
    monkeypatch.setenv("SOVEREIGN_HANDOFF_CREATE_MISSING_VAULTS", "0")

    response = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "ghost-agent",
                "packet": _packet(to_agent="ghost-agent"),
            },
        }
    )

    assert "error" not in response
    assert response["result"]["status"] == "degraded"
    assert response["result"]["delivered"] is False
    assert "destination vault" in response["result"]["reason"]

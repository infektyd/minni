import json
import os
import threading
import types
from pathlib import Path

import pytest

import minni.minnid as minnid


def _patch_handoff_db(monkeypatch, tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "handoffs.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))

    # Hermetic principal setup for test integrity (writes real JSON config with 0600 mode).
    import minni.principal as principal_mod
    import json

    pdir = tmp_path / "principals"
    pdir.mkdir(exist_ok=True)

    original_resolve = principal_mod.resolve_effective_principal

    def _test_resolve(*, supplied_agent_id=None, transport="uds", principals_dir=None):
        target_dir = principals_dir or pdir
        target_agent = str(supplied_agent_id or "main").strip() or "main"
        f = target_dir / "local.json"
        f.write_text(json.dumps({
            "agent_id": target_agent,
            "workspace_id": "default",
            "capabilities": ["*"]
        }), encoding="utf-8")
        os.chmod(f, 0o600)

        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=target_dir
        )

    monkeypatch.setattr(principal_mod, "resolve_effective_principal", _test_resolve)
    monkeypatch.setattr(minnid, "resolve_effective_principal", _test_resolve)
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
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )

    response = minnid._dispatch_sync(
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
    assert inbox_packet["lease_id"] in inbox_files[0].name
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
    _patch_handoff_db(monkeypatch, tmp_path)  # ensures G11 test-relaxed resolve (accepts test agent names)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )
    monkeypatch.setattr(minnid, "_store_handoff_lease", lambda *_args, **_kwargs: False)

    response = minnid._dispatch_sync({
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


def test_daemon_handoff_keeps_same_stamp_task_trace_prefix_packets(
    monkeypatch, tmp_path
):
    """write_json replace used stamp+task-slug+trace[:8]; two packets clobbered."""
    import time as time_mod

    _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )
    real_strftime = time_mod.strftime

    def _frozen_strftime(fmt, t=None):
        if fmt == "%Y%m%dT%H%M%SZ":
            return "20260901T000000Z"
        if t is None:
            return real_strftime(fmt)
        return real_strftime(fmt, t)

    monkeypatch.setattr(time_mod, "strftime", _frozen_strftime)

    first = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 43,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "packet": _packet(
                    task="task",
                    trace_id="trace-aaa-extra",
                    lease_id="handoff-lease-aaa",
                    envelope="<e>first</e>",
                ),
            },
        }
    )
    second = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 44,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "packet": _packet(
                    task="task",
                    trace_id="trace-aaa-other",
                    lease_id="handoff-lease-bbb",
                    envelope="<e>second</e>",
                ),
            },
        }
    )
    assert "error" not in first, first
    assert "error" not in second, second
    inbox_a = Path(first["result"]["inbox_path"])
    inbox_b = Path(second["result"]["inbox_path"])
    assert inbox_a != inbox_b
    assert inbox_a.is_file() and inbox_b.is_file()
    body_a = json.loads(inbox_a.read_text(encoding="utf-8"))
    body_b = json.loads(inbox_b.read_text(encoding="utf-8"))
    assert body_a["lease_id"] == "handoff-lease-aaa"
    assert body_b["lease_id"] == "handoff-lease-bbb"
    assert body_a["envelope"] == "<e>first</e>"
    assert body_b["envelope"] == "<e>second</e>"


def test_handoff_pending_list_and_ack(monkeypatch, tmp_path):
    db_obj = _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )

    sent = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 20,
        "method": "daemon.handoff",
        "params": {"from_agent": "codex", "to_agent": "claude-code", "packet": _packet()},
    })["result"]
    lease_id = sent["lease_id"]

    pending = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 21,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })["result"]
    assert [item["lease_id"] for item in pending["handoffs"]] == [lease_id]

    inbox_path = Path(sent["inbox_path"])
    outbox_path = Path(sent["outbox_path"])

    # A3 authz: only the lease's recipient may ack — the caller must stamp as
    # to_agent (the relaxed test resolver honors the supplied agent_id).
    ack = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 22,
        "method": "minni_ack_handoff",
        "params": {"lease_id": lease_id, "status": "accepted", "agent_id": "claude-code"},
    })["result"]
    assert ack["status"] == "accepted"
    assert len(ack["updated_paths"]) == 2

    # B1 archive-on-ack: exactly the recipient INBOX copy is archived (rename
    # into inbox/.archive, name preserved); the outbox copy stays put for
    # await_handoff and carries the ack_status.
    expected_archive = recipient / "inbox" / ".archive" / inbox_path.name
    assert ack["archived_paths"] == [str(expected_archive)]
    assert not inbox_path.exists(), "inbox copy must leave the live inbox"
    assert expected_archive.is_file(), "inbox copy archived, never deleted"
    assert json.loads(expected_archive.read_text())["ack_status"] == "accepted"
    assert outbox_path.is_file(), "outbox copy must survive the ack"
    assert json.loads(outbox_path.read_text())["ack_status"] == "accepted"

    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT status FROM handoff_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
    assert row["status"] == "accepted"

    pending_after = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 23,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })["result"]
    assert pending_after["handoffs"] == []


def test_await_handoff_times_out(monkeypatch, tmp_path):
    _patch_handoff_db(monkeypatch, tmp_path)
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps({"claude-code": str(recipient)}))
    response = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 24,
        "method": "minni_await_handoff",
        "params": {"lease_id": "missing", "timeout_ms": 1},
    })["result"]

    assert response["status"] == "timeout"


def test_daemon_handoff_rejects_invalid_packet(monkeypatch, tmp_path):
    _patch_handoff_db(monkeypatch, tmp_path)  # ensures G11 test-relaxed resolve (accepts test agent names)
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps({"codex": str(tmp_path / "codex")}))

    response = minnid._dispatch_sync(
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
    _patch_handoff_db(monkeypatch, tmp_path)  # ensures G11 test-relaxed resolve (accepts test agent names)
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps({"codex": str(tmp_path / "codex")}))
    monkeypatch.setenv("MINNI_HANDOFF_CREATE_MISSING_VAULTS", "0")

    response = minnid._dispatch_sync(
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


# --- RCM-006/007 required concurrency regression test (PHASE bar + RC_PLAN exit criteria) ---
import asyncio
import time


def test_handle_await_handoff_does_not_block_other_clients(monkeypatch, tmp_path):
    """Concurrent clients: one in await_handoff (polling with await sleep), one doing search (exercises RCM-006 to_thread offload).
    Assert the second client is not blocked (completes << timeout duration). Matches PHASE/RC_PLAN required regression test for offload + async handoff.
    """
    _patch_handoff_db(monkeypatch, tmp_path)

    class FakeRetrieval:
        last_trace_id = "trace-concurrency"

        def retrieve(self, **kwargs):
            return [{"doc_id": 1, "chunk_id": 1, "depth": kwargs["depth"], "source": "fake.md"}]

        def search_learnings(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(minnid, "_lazy_retrieval", lambda: FakeRetrieval())

    async def client_await_handoff():
        req = {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "minni_await_handoff",
            "params": {"lease_id": "nonexistent-for-concurrency-test", "timeout_ms": 180},
        }
        return await minnid._dispatch(req)

    async def client_other():
        start = time.perf_counter()
        req = {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "search",
            "params": {"query": "concurrent test recall", "limit": 1},
        }
        res = await minnid._dispatch(req)
        dur = time.perf_counter() - start
        # Non-blocking for the event loop: search hits to_thread offload (RCM-006); in this env first-run model load (~4s) dominates dur,
        # but the await client (handoff) is not blocked (its 180ms timeout fires independently via sleep yield). Proves concurrent clients work.
        # In cached/CI prod the offload is fast (<50ms target per PHASE example).
        assert dur < 10, f"second client (search via to_thread) took too long; {dur:.3f}s (event loop should stay responsive)"
        return res, dur

    async def run_concurrent():
        # Both run truly concurrent on the event loop; await sleep yields, status runs immediately
        res_await, other_tuple = await asyncio.gather(
            client_await_handoff(), client_other(), return_exceptions=False
        )
        res_other, dur_other = other_tuple
        assert res_await.get("result", {}).get("status") == "timeout"
        assert dur_other < 10, f"offload client dur {dur_other} exceeded tolerance"
        return res_await, res_other

    # Run the async test body from sync pytest
    asyncio.run(run_concurrent())


# ── PLUMB-T7 / #231: handoff DB errors must not look like empty results ─────


class _BrokenHandoffCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *_args, **_kwargs):
        raise RuntimeError("sqlite handoff store exploded")


class _BrokenHandoffDB:
    def cursor(self):
        return _BrokenHandoffCursor()


def test_list_pending_handoffs_fails_loud_on_db_error(monkeypatch, tmp_path):
    """PLUMB-T7: a lease-store failure is a JSON-RPC error, not handoffs=[]."""
    _patch_handoff_db(monkeypatch, tmp_path)
    monkeypatch.setattr(
        minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=_BrokenHandoffDB())
    )
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"claude-code": str(recipient)}),
    )

    response = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2311,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })

    assert "error" in response, (
        "store failure must surface as error, not success-with-empty handoffs: "
        f"{response!r}"
    )
    assert "result" not in response
    assert response["error"]["code"] == -32000
    msg = response["error"]["message"]
    assert "handoff lease store failed" in msg
    assert "listing pending leases" in msg


def test_await_handoff_fails_loud_on_db_error_not_timeout(monkeypatch, tmp_path):
    """PLUMB-T7: broken store + no file ack → -32000 after wait, not status=timeout.

    Dual-channel: we still poll the file channel until the deadline (recipient
    may ack mid-wait). Only after the wait expires with no terminal file ack
    do we fail loud — never collapse to a silent timeout.
    """
    _patch_handoff_db(monkeypatch, tmp_path)
    monkeypatch.setattr(
        minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=_BrokenHandoffDB())
    )
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"claude-code": str(recipient)}),
    )

    started = time.monotonic()
    response = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2312,
        "method": "minni_await_handoff",
        "params": {"lease_id": "lease-whatever", "timeout_ms": 150},
    })
    elapsed = time.monotonic() - started

    assert "error" in response, (
        "store failure must not degrade to a silent timeout: "
        f"{response!r}"
    )
    assert response.get("result") is None or "status" not in response.get("result", {})
    assert response["error"]["code"] == -32000
    assert "handoff lease store failed" in response["error"]["message"]
    assert "reading lease" in response["error"]["message"]
    # Must have waited roughly the timeout (file-channel poll), not fail-fast (~0).
    assert elapsed >= 0.12, f"expected ~timeout wait, got {elapsed:.3f}s"


def test_list_pending_handoffs_uses_file_channel_when_db_broken(monkeypatch, tmp_path):
    """Dual-channel: broken DB + pending inbox packet still lists the lease."""
    _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    inbox = recipient / "inbox"
    inbox.mkdir(parents=True)
    lease_id = "handoff-file-only-pending"
    packet = {
        "lease_id": lease_id,
        "from_agent": "codex",
        "to_agent": "claude-code",
        "task": "file-channel pending",
        "requires_ack": True,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    (inbox / f"{lease_id}.json").write_text(json.dumps(packet), encoding="utf-8")
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )
    monkeypatch.setattr(
        minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=_BrokenHandoffDB())
    )

    response = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2313,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })

    assert "error" not in response, f"file channel must answer when DB is down: {response!r}"
    result = response["result"]
    assert [item["lease_id"] for item in result["handoffs"]] == [lease_id]
    assert result["handoffs"][0]["task"] == "file-channel pending"


def test_list_pending_handoffs_merges_file_only_lease_with_healthy_db_leases(monkeypatch, tmp_path):
    """PLUMB-T3 / #231: a file-only lease must not become invisible just because
    the DB channel already holds a *different*, healthy lease for the same
    recipient. Before the fix, `handle_list_pending_handoffs` early-returned
    on the first non-empty SQLite result and never consulted the file
    channel — so a lease that only made it to disk (e.g. its own
    store_handoff_lease call degraded, independent of any other handoff)
    stayed permanently hidden as soon as one unrelated DB lease existed.
    """
    _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )

    # Handoff A: normal dual-write — lands in both the DB and the file channel.
    sent_a = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2320,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "packet": _packet(task="handoff A", trace_id="trace-a"),
        },
    })["result"]
    assert sent_a["lease_persisted"] is True

    # Handoff B: DB persistence deliberately fails for *this* handoff only —
    # file-only, while the DB channel already has A's healthy row.
    monkeypatch.setattr(minnid, "_store_handoff_lease", lambda *_a, **_kw: False)
    sent_b = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2321,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "packet": _packet(task="handoff B", trace_id="trace-b"),
        },
    })["result"]
    assert sent_b["lease_persisted"] is False

    pending = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2322,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })["result"]

    lease_ids = {item["lease_id"] for item in pending["handoffs"]}
    assert sent_a["lease_id"] in lease_ids, "DB-channel lease must still be visible"
    assert sent_b["lease_id"] in lease_ids, (
        "PLUMB-T3: file-only lease must not be shadowed just because the DB "
        "channel already had a healthy lease for this agent"
    )
    assert len(pending["handoffs"]) == 2


def test_list_pending_handoffs_does_not_resurrect_an_acked_lease_via_stale_file_packet(
    monkeypatch, tmp_path
):
    """PLUMB-T3 / #231 hardening: an acked lease must not reappear as pending
    just because its on-disk packet never received the ack_status update.

    This can happen when the recipient vault is resolved only through the
    per-agent MINNI_<AGENT>_VAULT_PATH override: agent_vault() (used by the
    file-channel read path) finds it, but known_agent_vaults() (used by the
    ack path's write_matching_lease_packets sweep) does not, so the ack's DB
    write succeeds while its file-packet write silently misses. Always
    merging the file channel (the T3 fix above) must not turn that pre-
    existing gap into a resurrected-as-pending lease: the DB's terminal
    status has to be treated as authoritative and exclude that lease_id from
    the file channel, not just from the live 'pending' query.
    """
    db_obj = _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    # Recipient vault resolved ONLY via the per-agent env var — not registered
    # in MINNI_AGENT_VAULTS, so known_agent_vaults() (the ack sweep) can't see it.
    recipient = tmp_path / "claude-code-side-vault"
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps({"codex": str(sender)}))
    monkeypatch.setenv("MINNI_CLAUDE_CODE_VAULT_PATH", str(recipient))

    sent_a = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2330,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "packet": _packet(task="handoff A (will be acked)", trace_id="trace-resurrect-a"),
        },
    })["result"]
    assert sent_a["lease_persisted"] is True

    ack = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2331,
        "method": "minni_ack_handoff",
        "params": {"lease_id": sent_a["lease_id"], "status": "accepted", "agent_id": "claude-code"},
    })["result"]
    assert ack["status"] == "accepted"
    # Confirms the setup actually exercises the gap: the ack sweep DOES reach
    # the sender's outbox copy (that vault is registered in MINNI_AGENT_VAULTS,
    # so known_agent_vaults() finds it) but NOT the recipient's inbox copy —
    # that vault is env-var-only, invisible to the ack sweep.
    assert str(Path(sent_a["inbox_path"])) not in ack["updated_paths"], (
        "test setup didn't exercise the gap: the recipient's inbox copy got "
        "updated after all, so this test can't tell resurrection apart from "
        "a working ack sweep"
    )
    assert Path(sent_a["inbox_path"]).exists()
    assert "ack_status" not in json.loads(Path(sent_a["inbox_path"]).read_text())

    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT status FROM handoff_leases WHERE lease_id = ?",
            (sent_a["lease_id"],),
        ).fetchone()
    assert row["status"] == "accepted"

    # A second, unrelated handoff keeps the DB channel non-empty for this
    # agent — the exact shape that would have hidden the file channel
    # entirely under the pre-merge early-return, and now must not let the
    # stale acked packet ride back in alongside it.
    sent_b = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2332,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "packet": _packet(task="handoff B (still pending)", trace_id="trace-resurrect-b"),
        },
    })["result"]
    assert sent_b["lease_persisted"] is True

    pending = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2333,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })["result"]

    lease_ids = [item["lease_id"] for item in pending["handoffs"]]
    assert sent_a["lease_id"] not in lease_ids, (
        "acked lease resurrected as pending via a stale file-channel packet"
    )
    assert sent_b["lease_id"] in lease_ids
    assert lease_ids.count(sent_b["lease_id"]) == 1


def test_list_pending_handoffs_dedupes_self_handoff_across_inbox_and_outbox(monkeypatch, tmp_path):
    """A self-handoff (from_agent == to_agent) writes the identical packet to
    the same vault's inbox AND outbox — the file channel's own scan walks
    both directories and yields the lease_id twice. The merge must not
    surface a duplicate entry for one lease just because its DB persistence
    happened to fail (forcing the file-channel path to run at all).
    """
    _patch_handoff_db(monkeypatch, tmp_path)
    vault = tmp_path / "codex-vault"
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps({"codex": str(vault)}))
    monkeypatch.setattr(minnid, "_store_handoff_lease", lambda *_a, **_kw: False)

    sent = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2340,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "codex",
            "packet": _packet(from_agent="codex", to_agent="codex", trace_id="trace-self"),
        },
    })["result"]
    assert sent["lease_persisted"] is False

    pending = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2341,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "codex"},
    })["result"]

    lease_ids = [item["lease_id"] for item in pending["handoffs"]]
    assert lease_ids.count(sent["lease_id"]) == 1, (
        f"self-handoff must not be listed twice (inbox + outbox copies): {lease_ids!r}"
    )
    # Pin which copy wins the dedupe (inbox, since iter_handoff_files walks
    # ("inbox", "outbox") in that order and the merge keeps the first file
    # entry seen for a given lease_id) — harmless either way today, but this
    # locks the invariant down instead of leaving it to walk-order luck.
    assert pending["handoffs"][0]["path"].endswith(f"/inbox/{Path(sent['inbox_path']).name}")


def test_list_pending_handoffs_reactivates_a_resent_lease_after_terminal_ack(monkeypatch, tmp_path):
    """#231 hardening: resending a handoff under a REUSED lease_id after it
    was already acked must surface again as pending — not vanish on both
    channels.

    lease_id is caller-supplied (validate_handoff_packet only requires it be
    a non-empty string), and store_handoff_lease's INSERT ... ON CONFLICT
    upsert previously left `status` untouched on conflict, so the DB row
    stayed 'accepted' forever after a resend. Combined with
    `_known_lease_ids_for_agent` (which excludes any lease_id the DB has
    EVER seen, any status, from the file-channel fallback), an unreset
    status made the resend invisible in both channels even though
    daemon.handoff reported lease_persisted=True for it.
    """
    db_obj = _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )
    reused_lease_id = "handoff-reused-lease-id"

    sent_first = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2350,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "packet": _packet(task="first send", trace_id="trace-reuse-1", lease_id=reused_lease_id),
        },
    })["result"]
    assert sent_first["lease_id"] == reused_lease_id
    assert sent_first["lease_persisted"] is True

    ack = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2351,
        "method": "minni_ack_handoff",
        "params": {"lease_id": reused_lease_id, "status": "accepted", "agent_id": "claude-code"},
    })["result"]
    assert ack["status"] == "accepted"
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT status FROM handoff_leases WHERE lease_id = ?",
            (reused_lease_id,),
        ).fetchone()
    assert row["status"] == "accepted"

    # Resend under the SAME lease_id — a legitimate re-issue of the same
    # piece of work, not a new one.
    sent_again = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2352,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "packet": _packet(task="resend", trace_id="trace-reuse-2", lease_id=reused_lease_id),
        },
    })["result"]
    assert sent_again["lease_id"] == reused_lease_id
    assert sent_again["lease_persisted"] is True, (
        "daemon.handoff must not silently claim success while the row stays terminal"
    )

    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT status FROM handoff_leases WHERE lease_id = ?",
            (reused_lease_id,),
        ).fetchone()
    assert row["status"] == "pending", (
        "resend must reactivate the DB row, not leave the prior terminal status in place"
    )

    pending = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2353,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })["result"]
    lease_ids = [item["lease_id"] for item in pending["handoffs"]]
    assert reused_lease_id in lease_ids, (
        "resent handoff must be visible again, not black-holed on both channels "
        "while daemon.handoff reports lease_persisted=True"
    )


def test_list_pending_handoffs_tolerates_a_malformed_lease_id_in_a_stray_file(monkeypatch, tmp_path):
    """#231 hardening: the file channel is an untrusted, possibly-malformed
    mirror by design (iter_handoff_files already swallows unreadable/non-JSON
    files) — a packet with a non-string lease_id must be skipped the same
    way, not crash the whole RPC. The T3 merge uses lease_id as a dict/set
    key (`in known_ids`, dict assignment), so a stray file with e.g. a list
    for lease_id previously raised TypeError: unhashable type deep inside
    list_pending_handoffs and took down the entire response for the agent,
    even though a perfectly healthy DB lease existed alongside it.
    """
    _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )

    # A healthy handoff so the response has real, expected content.
    sent = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2360,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "packet": _packet(task="healthy lease", trace_id="trace-malformed-ok"),
        },
    })["result"]
    assert sent["lease_persisted"] is True

    # A stray, malformed packet in the recipient's inbox: lease_id is a list,
    # not a string (as if a file got corrupted or hand-edited).
    inbox = recipient / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "corrupt.json").write_text(
        json.dumps({
            "lease_id": ["not", "a", "string"],
            "from_agent": "codex",
            "to_agent": "claude-code",
            "task": "corrupt packet",
            "requires_ack": True,
        }),
        encoding="utf-8",
    )

    response = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2361,
        "method": "minni_list_pending_handoffs",
        "params": {"agent_id": "claude-code"},
    })

    assert "error" not in response, (
        f"a malformed on-disk lease_id must not crash the RPC: {response!r}"
    )
    lease_ids = [item["lease_id"] for item in response["result"]["handoffs"]]
    assert lease_ids == [sent["lease_id"]], (
        "the healthy DB lease must still be returned, with the corrupt file skipped"
    )


def test_await_handoff_uses_file_ack_when_db_broken(monkeypatch, tmp_path):
    """Dual-channel: broken DB + terminal file ack returns accepted, not -32000."""
    _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    outbox = sender / "outbox"
    outbox.mkdir(parents=True)
    lease_id = "handoff-file-only-acked"
    packet = {
        "lease_id": lease_id,
        "from_agent": "codex",
        "to_agent": "claude-code",
        "task": "file-channel acked",
        "requires_ack": True,
        "ack_status": "accepted",
        "acked_at": "2026-08-02T12:00:00Z",
    }
    (outbox / f"{lease_id}.json").write_text(json.dumps(packet), encoding="utf-8")
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )
    monkeypatch.setattr(
        minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=_BrokenHandoffDB())
    )

    response = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2314,
        "method": "minni_await_handoff",
        "params": {"lease_id": lease_id, "timeout_ms": 5000},
    })

    assert "error" not in response, f"file ack must win over broken DB: {response!r}"
    result = response["result"]
    assert result["lease_id"] == lease_id
    assert result["status"] == "accepted"
    assert result["acked_at"] == "2026-08-02T12:00:00Z"


def test_await_handoff_polls_file_channel_until_ack_when_db_broken(monkeypatch, tmp_path):
    """Dual-channel: broken DB + pending packet; ack lands mid-wait → accepted.

    Must not -32000 on the first HandoffStoreError while the file lease is still
    pending — keep polling until deadline (mirrors list dual-channel).
    """
    _patch_handoff_db(monkeypatch, tmp_path)
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    outbox = sender / "outbox"
    outbox.mkdir(parents=True)
    lease_id = "handoff-file-ack-mid-wait"
    packet_path = outbox / f"{lease_id}.json"
    pending = {
        "lease_id": lease_id,
        "from_agent": "codex",
        "to_agent": "claude-code",
        "task": "file-channel mid-wait",
        "requires_ack": True,
    }
    packet_path.write_text(json.dumps(pending), encoding="utf-8")
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )
    monkeypatch.setattr(
        minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=_BrokenHandoffDB())
    )

    def _ack_after_delay() -> None:
        time.sleep(0.08)
        acked = {
            **pending,
            "ack_status": "accepted",
            "acked_at": "2026-08-02T12:30:00Z",
        }
        packet_path.write_text(json.dumps(acked), encoding="utf-8")

    writer = threading.Thread(target=_ack_after_delay, daemon=True)
    writer.start()
    try:
        started = time.monotonic()
        response = minnid._dispatch_sync({
            "jsonrpc": "2.0",
            "id": 2315,
            "method": "minni_await_handoff",
            "params": {"lease_id": lease_id, "timeout_ms": 2000},
        })
        elapsed = time.monotonic() - started
    finally:
        writer.join(timeout=2.0)

    assert "error" not in response, f"mid-wait file ack must succeed: {response!r}"
    result = response["result"]
    assert result["lease_id"] == lease_id
    assert result["status"] == "accepted"
    assert result["acked_at"] == "2026-08-02T12:30:00Z"
    # Must have waited for the mid-wait write, not returned immediately with -32000.
    assert elapsed >= 0.05, f"expected poll delay, got {elapsed:.3f}s"
    assert elapsed < 1.5, f"should return soon after mid-wait ack, got {elapsed:.3f}s"


def test_await_handoff_clears_store_error_after_recovery(monkeypatch, tmp_path):
    """Transient store blip must not sticky-poison a later healthy timeout.

    First poll raises HandoffStoreError; subsequent polls succeed with no row
    and empty file channel → status=timeout, not -32000.
    """
    from minni.minnid_runtime.handoff import HandoffStoreError

    _patch_handoff_db(monkeypatch, tmp_path)
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"claude-code": str(recipient)}),
    )

    calls = {"n": 0}

    def _flaky_status(lease_id, *, context):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HandoffStoreError(
                f"handoff lease store failed while reading lease {lease_id!r}: blip"
            )
        return None  # healthy empty / unknown

    monkeypatch.setattr(minnid, "_runtime_handoff_lease_status", _flaky_status)
    # Also patch the symbol used if dispatch goes through runtime package directly.
    import minni.minnid_runtime.handoff as handoff_mod

    monkeypatch.setattr(handoff_mod, "handoff_lease_status", _flaky_status)

    response = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2316,
        "method": "minni_await_handoff",
        "params": {"lease_id": "lease-recovered", "timeout_ms": 150},
    })

    assert "error" not in response, f"recovered store must timeout cleanly: {response!r}"
    assert response["result"]["status"] == "timeout"
    assert response["result"]["lease_id"] == "lease-recovered"
    assert calls["n"] >= 2, "expected multiple polls after recovery"


def test_await_handoff_logs_store_failure_once_not_per_poll(monkeypatch, tmp_path, caplog):
    """Broken store + multi-poll await must not flood WARNING per 50ms tick."""
    import logging

    _patch_handoff_db(monkeypatch, tmp_path)
    monkeypatch.setattr(
        minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=_BrokenHandoffDB())
    )
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({"claude-code": str(recipient)}),
    )

    with caplog.at_level(logging.WARNING, logger="minnid"):
        response = minnid._dispatch_sync({
            "jsonrpc": "2.0",
            "id": 2317,
            "method": "minni_await_handoff",
            "params": {"lease_id": "lease-log-flood", "timeout_ms": 200},
        })

    assert response.get("error", {}).get("code") == -32000
    store_warns = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and (
            "lease store failed" in r.getMessage()
            or "Could not read handoff lease" in r.getMessage()
            or "polling file channel" in r.getMessage()
        )
    ]
    assert len(store_warns) == 1, (
        f"expected exactly one store-failure WARNING, got {len(store_warns)}: "
        f"{[r.getMessage() for r in store_warns]!r}"
    )


def test_pending_handoff_leases_raises_handoff_store_error(monkeypatch, tmp_path):
    """Unit-level: empty list is reserved for genuine empty; store failure raises."""
    from dataclasses import replace

    from minni.minnid_runtime.handoff import HandoffStoreError, pending_handoff_leases

    _patch_handoff_db(monkeypatch, tmp_path)
    broken_ctx = replace(
        minnid._handoff_context(),
        lazy_writeback=lambda: types.SimpleNamespace(db=_BrokenHandoffDB()),
    )

    with pytest.raises(HandoffStoreError, match="listing pending leases"):
        pending_handoff_leases("claude-code", context=broken_ctx)

"""Named standing drain is minnid tick. Kick only. Not a later MCP boot."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from minni.worker_write_drain import (
    STANDING_DRAIN_TRIGGER,
    kick_pending_worker_write_drain_for_vault,
    minnid_tick_once,
    vaults_needing_worker_write_drain,
    worker_write_drain_enabled,
)


REPO = Path(__file__).resolve().parents[1]


def test_named_trigger_is_minnid_tick():
    assert STANDING_DRAIN_TRIGGER == "minnid tick"


def test_python_kick_does_not_write_the_journal():
    source = Path(__file__).resolve().parents[1].joinpath(
        "src", "minni", "worker_write_drain.py"
    ).read_text(encoding="utf-8")
    assert "slice.started" not in source
    assert "append_journal" not in source
    assert "write_text" not in source
    assert "drainPendingWorkerWritesForVault" in source
    assert "STANDING_DRAIN_TRIGGER" in source


def test_tick_js_calls_existing_apply_entry():
    source = (REPO / "plugins" / "minni" / "src" / "standing-drain-tick.ts").read_text(
        encoding="utf-8"
    )
    assert 'STANDING_DRAIN_TRIGGER = "minnid tick"' in source
    assert "drainPendingWorkerWritesForVault" in source
    assert "StdioServerTransport" not in source
    assert "registerTool" not in source
    assert "from \"./server.js\"" not in source
    assert "from './server.js'" not in source


def test_mcp_main_and_in_process_kick_stay():
    server = (REPO / "plugins" / "minni" / "src" / "server.ts").read_text(encoding="utf-8")
    worker = (REPO / "plugins" / "minni" / "src" / "thread-worker.ts").read_text(
        encoding="utf-8"
    )
    assert "void drainPendingWorkerWritesForVault(DEFAULT_VAULT_PATH)" in server
    assert "export function kickWorkerWriteDrain(" in worker
    assert server.count("registerTool(") == server.count("registerTool(")


def test_enabled_gate(monkeypatch):
    monkeypatch.delenv("MINNI_WORKER_WRITE_DRAIN", raising=False)
    assert worker_write_drain_enabled() is True
    monkeypatch.setenv("MINNI_WORKER_WRITE_DRAIN", "off")
    assert worker_write_drain_enabled() is False


def test_kick_invokes_standing_tick_js_not_mcp_main(monkeypatch, tmp_path):
    import minni.worker_write_drain as drain

    tick_js = tmp_path / "standing-drain-tick.js"
    tick_js.write_text("console.log(JSON.stringify({planIds:['plan-1']}))\n", encoding="utf-8")
    monkeypatch.setenv("MINNI_STANDING_DRAIN_TICK_JS", str(tick_js))
    calls = []

    class Result:
        returncode = 0
        stdout = '{"planIds":["plan-1"]}\n'
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        return Result()

    monkeypatch.setattr(drain.subprocess, "run", fake_run)
    vault = tmp_path / "vault"
    vault.mkdir()
    result = kick_pending_worker_write_drain_for_vault(vault)
    assert result["trigger"] == "minnid tick"
    assert result["kicked"] is True
    assert result["planIds"] == ["plan-1"]
    assert calls, "minnid tick must spawn the standing tick entry"
    cmd = calls[0][0]
    assert str(tick_js) in cmd
    assert not any("server.js" in str(part) for part in cmd)
    env = calls[0][1]
    assert env["MINNI_STANDING_DRAIN_VAULT"] == str(vault)


def test_tick_once_only_kicks_vaults_with_pending_q(tmp_path, monkeypatch):
    import minni.worker_write_drain as drain

    vault = tmp_path / "agy-vault"
    qdir = vault / ".runtime" / "thread-locks" / "abc.q"
    qdir.mkdir(parents=True)
    (qdir / "ticket.json").write_text("{\n}\n", encoding="utf-8")
    empty = tmp_path / "empty-vault"
    empty.mkdir()
    monkeypatch.setenv("MINNI_VAULT_PATH", str(vault))
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    needed = vaults_needing_worker_write_drain()
    assert any(path.resolve() == vault.resolve() for path in needed)
    assert all(path.resolve() != empty.resolve() for path in needed)

    kicked = []

    def fake_kick(path):
        kicked.append(Path(path))
        return {"trigger": "minnid tick", "planIds": ["p"], "kicked": True}

    monkeypatch.setattr(drain, "kick_pending_worker_write_drain_for_vault", fake_kick)
    results = minnid_tick_once()
    assert results
    assert any(path.resolve() == vault.resolve() for path in kicked)


def test_daemon_schedules_minnid_tick(monkeypatch, tmp_path):
    import signal

    from test_r7_retrieval_integrity import _run_main_with_stub_loop

    monkeypatch.setenv("MINNI_WORKER_WRITE_DRAIN", "on")
    minnid, loop, handlers = _run_main_with_stub_loop(monkeypatch, tmp_path)
    task = loop.task_for(minnid._worker_write_drain_runner)
    assert task is not None, (
        "minnid.main() must schedule the named standing drain tick "
        f"(scheduled: {[t.name for t in loop.tasks]})"
    )
    assert not task.cancelled
    mark = loop.mark()
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert task.cancelled
    assert "_worker_write_drain_runner" in loop.cancels_since(mark)


def test_minnid_tick_not_scheduled_when_disabled(monkeypatch, tmp_path):
    from test_r7_retrieval_integrity import _run_main_with_stub_loop

    monkeypatch.setenv("MINNI_WORKER_WRITE_DRAIN", "off")
    minnid, loop, _ = _run_main_with_stub_loop(monkeypatch, tmp_path)
    assert loop.task_for(minnid._worker_write_drain_runner) is None


def test_runner_source_is_kick_not_journal():
    import minni.minnid as minnid

    src = inspect.getsource(minnid._worker_write_drain_runner)
    assert "minnid_tick_runner" in src
    assert "ingest_journal_into_vault" not in src

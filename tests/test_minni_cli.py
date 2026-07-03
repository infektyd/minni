"""Tests for the packaging-only `minni` CLI (minni_cli.py).

Model-free and daemon-free: RPC behavior is exercised against a fake JSON-RPC
Unix-socket server so the suite stays in the fast, hermetic tier.
"""

from __future__ import annotations

import json
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

import pytest

import minni.minni_cli as minni_cli


class _FakeDaemonHandler(socketserver.StreamRequestHandler):
    """Answers ping/status/search the way minnid's health surface does."""

    def handle(self):
        line = self.rfile.readline()
        req = json.loads(line.decode())
        method = req.get("method")
        responses = {
            "ping": "pong",
            "status": {"daemon": {"version": "0.1.0", "uptime_seconds": 12,
                                  "requests_served": 3},
                       "engine": {"db_ok": True, "faiss_ok": True,
                                  "stats": {"documents": 1, "learnings": 2,
                                            "events": 3}}},
            "search": {"results": [], "count": 0},
        }
        if method in responses:
            body = {"jsonrpc": "2.0", "id": req.get("id"),
                    "result": responses[method]}
        else:
            body = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32601, "message": "method not found"}}
        self.wfile.write((json.dumps(body) + "\n").encode())


class _FakeDaemon(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def fake_daemon():
    # Bind under /tmp, not pytest's tmp_path: macOS caps AF_UNIX paths at
    # ~104 bytes and tmp_path lives deep under /private/var/folders.
    run_dir = Path(tempfile.mkdtemp(prefix="minni-cli-", dir="/tmp"))
    run_dir.chmod(0o700)
    sock_path = run_dir / "minnid.sock"
    server = _FakeDaemon(str(sock_path), _FakeDaemonHandler)
    sock_path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield sock_path
    server.shutdown()
    server.server_close()
    shutil.rmtree(run_dir, ignore_errors=True)


def test_rpc_round_trip(fake_daemon):
    result = minni_cli._rpc(fake_daemon, "status")
    assert "daemon" in result and "engine" in result


def test_rpc_missing_socket_raises(tmp_path):
    with pytest.raises(minni_cli.RpcError):
        minni_cli._rpc(tmp_path / "nope.sock", "ping")


def test_rpc_daemon_error_raises(fake_daemon):
    with pytest.raises(minni_cli.RpcError, match="method not found"):
        minni_cli._rpc(fake_daemon, "no_such_method")


def test_daemon_alive(fake_daemon, tmp_path):
    assert minni_cli._daemon_alive(fake_daemon) is True
    assert minni_cli._daemon_alive(tmp_path / "nope.sock") is False


def test_models_present_reads_hf_cache_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    cached = "sentence-transformers/all-MiniLM-L6-v2"
    snap = (tmp_path / ("models--" + cached.replace("/", "--")) / "snapshots"
            / "abc123")
    snap.mkdir(parents=True)
    present, missing = minni_cli._models_present()
    assert present == [cached]
    assert set(missing) == set(minni_cli.EXPECTED_MODELS) - {cached}


def test_doctor_passes_against_healthy_daemon(fake_daemon, monkeypatch,
                                              capsys, tmp_path):
    # Pin the two environment-dependent checks so the test is hermetic: the
    # interpreter floor (CI is 3.14; local venvs may lag) and the model cache.
    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    rc = minni_cli.main(["--socket", str(fake_daemon), "doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PASS] daemon status" in out
    assert "[PASS] recall round-trip" in out
    assert "[WARN] models" in out  # empty cache warns, never fails


def test_doctor_fails_without_daemon(tmp_path, capsys):
    rc = minni_cli.main(["--socket", str(tmp_path / "nope.sock"), "doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL]" in out


def test_status_renders_plain_language(fake_daemon, capsys):
    rc = minni_cli.main(["--socket", str(fake_daemon), "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "running" in out
    assert "database: ok" in out


def test_status_unreachable_exits_nonzero(tmp_path, capsys):
    rc = minni_cli.main(["--socket", str(tmp_path / "nope.sock"), "status"])
    assert rc == 1
    assert "minni up" in capsys.readouterr().err


def test_no_command_prints_help(capsys):
    assert minni_cli.main([]) == 0
    assert "doctor" in capsys.readouterr().out

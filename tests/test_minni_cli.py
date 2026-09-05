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


def _doctor_socket(tmp_path, monkeypatch):
    """A present, correctly-permissioned socket plus hermetic env pins."""
    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    tmp_path.chmod(0o700)
    sock = tmp_path / "minnid.sock"
    sock.touch()
    sock.chmod(0o600)
    return sock


def _status_with_latencies():
    return {
        "daemon": {"version": "0.1.0", "uptime_seconds": 12,
                   "requests_served": 3,
                   "latencies": {
                       "search": {"count": 8, "p50_ms": 45600.0,
                                  "p95_ms": 72900.0},
                       "afm": {"count": 4, "p50_ms": 200.0,
                               "p95_ms": 310.0},
                       "cross_encoder": {"count": 0, "p50_ms": 0.0,
                                         "p95_ms": 0.0},
                   }},
        "engine": {"db_ok": True, "faiss_ok": True,
                   "stats": {"documents": 1, "learnings": 2, "events": 3}},
    }


def test_status_renders_latencies_with_units(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(minni_cli, "_rpc",
                        lambda sock, method, params=None, timeout=30.0:
                        _status_with_latencies())
    rc = minni_cli.main(["--socket", str(tmp_path / "s.sock"), "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "latency search: 8 samples, p50 45.6s, p95 72.9s" in out
    assert "latency afm: 4 samples, p50 200ms" in out
    # Zero-count methods are reported as unsampled, never fabricated zeros;
    # methods the daemon never reports are not invented.
    assert "latency cross_encoder: no samples yet" in out
    assert "embedding" not in out


def test_status_without_latencies_says_not_reported(monkeypatch, capsys,
                                                    tmp_path):
    monkeypatch.setattr(minni_cli, "_rpc",
                        lambda sock, method, params=None, timeout=30.0:
                        {"daemon": {}, "engine": {}})
    rc = minni_cli.main(["--socket", str(tmp_path / "s.sock"), "status"])
    assert rc == 0
    assert "latencies: not reported by this daemon" in capsys.readouterr().out


def test_classify_recall_probe_grades_honestly():
    level, detail = minni_cli._classify_recall_probe(True, "", 0.4, None)
    assert level == "pass"
    assert "0.4s" in detail
    level, detail = minni_cli._classify_recall_probe(
        True, "", 12.0, 45600.0)
    assert level == "warn"
    assert "45.6s" in detail
    assert "not healthy performance" in detail
    assert "#388" not in detail  # actionable message only, no issue jargon
    level, detail = minni_cli._classify_recall_probe(
        True, "", 0.4, None, degraded=True)
    assert level == "warn"
    assert "degraded" in detail
    level, detail = minni_cli._classify_recall_probe(
        False, "results is dict", 0.4, None)
    assert level == "fail"
    assert "unexpected shape: results is dict" in detail


def test_latency_metadata_rejects_bogus_numbers():
    block = minni_cli._format_latency_block({
        "bad": {"count": 3, "p50_ms": float("nan"), "p95_ms": float("inf")},
        "neg": {"count": 2, "p50_ms": -5.0, "p95_ms": 10.0},
        "bools": {"count": True, "p50_ms": True, "p95_ms": 10.0},
        "negcount": {"count": -2, "p50_ms": 10.0, "p95_ms": 10.0},
    })
    lowered = block.lower()
    assert "nanms" not in lowered and "nans" not in lowered
    assert "infms" not in lowered and "infs" not in lowered
    assert "n/a" in block  # unusable values render as n/a ...
    assert "unavailable" in block  # ... and bogus counts never print
    assert "True samples" not in block
    assert minni_cli._latency_p50_ms(
        {"search": {"count": 1, "p50_ms": float("nan")}}, "search") is None


def test_doctor_warns_not_passes_on_slow_recall(monkeypatch, capsys,
                                                tmp_path):
    sock = _doctor_socket(tmp_path, monkeypatch)
    monkeypatch.setattr(minni_cli, "RECALL_SLOW_WARN_S", -1.0)

    def fake_rpc(sock_path, method, params=None, timeout=30.0):
        if method == "status":
            return _status_with_latencies()
        return {"results": [], "count": 0}

    monkeypatch.setattr(minni_cli, "_rpc", fake_rpc)
    rc = minni_cli.main(["--socket", str(sock), "doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[WARN] recall round-trip" in out
    assert "[PASS] recall round-trip" not in out
    assert "warning(s)" in out


def test_doctor_recall_timeout_fails_against_probe_budget(
        monkeypatch, capsys, tmp_path):
    sock = _doctor_socket(tmp_path, monkeypatch)

    def fake_rpc(sock_path, method, params=None, timeout=30.0):
        if method == "status":
            return _status_with_latencies()
        raise minni_cli.RpcTimeoutError("request timed out after 120s")

    monkeypatch.setattr(minni_cli, "_rpc", fake_rpc)
    rc = minni_cli.main(["--socket", str(sock), "doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] recall round-trip" in out
    assert "120s probe budget" in out
    assert "p50 45.6s" in out
    assert "not necessarily wedged" in out


def test_doctor_slow_success_warns_not_fails(monkeypatch, capsys, tmp_path):
    """A 45s successful probe (the #388 shape) is WARN, never FAIL."""
    import time as time_module

    sock = _doctor_socket(tmp_path, monkeypatch)
    captured: dict = {}
    # Probe start reads 1000.0, probe end 1045.0; later callers (deploy
    # honesty) pin at the last tick rather than exhausting the script.
    ticks = [1000.0, 1045.0]
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return ticks[min(calls["n"] - 1, len(ticks) - 1)]

    monkeypatch.setattr(time_module, "monotonic", fake_monotonic)

    def fake_rpc(sock_path, method, params=None, timeout=30.0):
        if method == "status":
            return _status_with_latencies()
        captured["params"] = params
        captured["timeout"] = timeout
        return {"results": [{"id": 1}], "degraded": False}

    monkeypatch.setattr(minni_cli, "_rpc", fake_rpc)
    rc = minni_cli.main(["--socket", str(sock), "doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[WARN] recall round-trip" in out
    assert "[FAIL] recall round-trip" not in out
    assert "[PASS] recall round-trip" not in out
    assert "45.0s" in out
    # Doctor-only budget: 120s transport with matching daemon timeout_ms.
    assert captured["timeout"] == minni_cli.RECALL_PROBE_BUDGET_S == 120.0
    assert captured["params"]["timeout_ms"] == 120000


def test_doctor_fast_degraded_result_warns(monkeypatch, capsys, tmp_path):
    sock = _doctor_socket(tmp_path, monkeypatch)

    def fake_rpc(sock_path, method, params=None, timeout=30.0):
        if method == "status":
            return _status_with_latencies()
        return {"results": [], "degraded": True}

    monkeypatch.setattr(minni_cli, "_rpc", fake_rpc)
    rc = minni_cli.main(["--socket", str(sock), "doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[WARN] recall round-trip" in out
    assert "degraded" in out
    assert "[PASS] recall round-trip" not in out


def test_doctor_rejects_non_list_results(monkeypatch, capsys, tmp_path):
    sock = _doctor_socket(tmp_path, monkeypatch)

    def fake_rpc(sock_path, method, params=None, timeout=30.0):
        if method == "status":
            return _status_with_latencies()
        return {"results": {"count": 0}}

    monkeypatch.setattr(minni_cli, "_rpc", fake_rpc)
    rc = minni_cli.main(["--socket", str(sock), "doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] recall round-trip" in out
    assert "results is dict" in out


def test_doctor_other_errors_surface_verbatim(monkeypatch, capsys, tmp_path):
    """Connection-refused / daemon errors must not wear a timeout label."""
    sock = _doctor_socket(tmp_path, monkeypatch)

    def fake_rpc(sock_path, method, params=None, timeout=30.0):
        if method == "status":
            return _status_with_latencies()
        raise minni_cli.RpcError("connection refused — daemon not listening")

    monkeypatch.setattr(minni_cli, "_rpc", fake_rpc)
    rc = minni_cli.main(["--socket", str(sock), "doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] recall round-trip" in out
    assert "connection refused" in out
    assert "no answer within" not in out


def test_no_command_prints_help(capsys):
    assert minni_cli.main([]) == 0
    assert "doctor" in capsys.readouterr().out


def test_wire_from_repo_use_version_mutually_exclusive(capsys):
    rc = minni_cli.main([
        "wire", "claude-code",
        "--from-repo", "/tmp/x",
        "--use-version", "1.2.3",
    ])
    assert rc == 2
    assert (
        capsys.readouterr().err.strip()
        == "minni wire: --from-repo and --use-version are mutually exclusive"
    )


def test_wire_prune_no_prune_mutually_exclusive(capsys):
    rc = minni_cli.main(["wire", "claude-code", "--prune", "--no-prune"])
    assert rc == 2
    assert (
        capsys.readouterr().err.strip()
        == "minni wire: --prune and --no-prune are mutually exclusive"
    )


def test_wire_cli_happy_path(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_run_wire(args):
        captured["platform"] = args.platform
        captured["dry_run"] = args.dry_run
        captured["verify_payload"] = args.verify_payload
        print(json.dumps({
            "schema": 1,
            "status": "dry-run",
            "payload_version": None,
            "install_root": None,
            "results": [],
            "gc": {},
        }))
        return 0

    monkeypatch.setattr("minni.wire.flow.run_wire", fake_run_wire)
    rc = minni_cli.main(["wire", "claude-code", "--dry-run", "--verify-payload"])
    assert rc == 0
    assert captured["platform"] == "claude-code"
    assert captured["dry_run"] is True
    assert captured["verify_payload"] is True

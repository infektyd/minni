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


@pytest.mark.parametrize("operation", ["connect", "sendall", "recv"])
def test_rpc_converts_socket_timeout(monkeypatch, tmp_path, operation):
    timeout_error = minni_cli.socket.timeout("transport stalled")
    observed = {}

    class TimeoutSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            observed["closed"] = True

        def settimeout(self, value):
            observed["timeout"] = value

        def connect(self, path):
            if operation == "connect":
                raise timeout_error

        def sendall(self, data):
            if operation == "sendall":
                raise timeout_error

        def recv(self, size):
            raise timeout_error

    monkeypatch.setattr(minni_cli.socket, "socket", lambda *args: TimeoutSocket())
    with pytest.raises(minni_cli.RpcTimeoutError, match="timed out after 120s") as caught:
        minni_cli._rpc(tmp_path / "daemon.sock", "search", timeout=120.0)
    assert caught.value.__cause__ is timeout_error
    assert observed == {"timeout": 120.0, "closed": True}


def test_rpc_connection_refused_kickstart_hint(monkeypatch, tmp_path):
    class RefusedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, path):
            raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr(minni_cli.socket, "socket", lambda *args: RefusedSocket())
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(minni_cli.RpcError, match="launchctl kickstart"):
        minni_cli._rpc(tmp_path / "daemon.sock", "ping")


def test_minni_cli_preserves_stdlib_only_import_invariant():
    """Importing minni.minni_cli in a fresh interpreter must NOT pull in heavy ML modules."""
    import os
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    probe = (
        "import sys, minni.minni_cli; "
        "banned = ('torch', 'faiss', 'sentence_transformers', 'numpy', 'minni.retrieval'); "
        "loaded = [m for m in sys.modules if m == 'numpy' or any(m.startswith(b) for b in banned)]; "
        "assert not loaded, f'Forbidden imports loaded by minni_cli: {loaded}'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_cli_workspace_autodetect_precedence_and_agent_omission(monkeypatch, tmp_path, capsys):
    repo_dir = tmp_path / "My-Cool-Repo"
    nested_dir = repo_dir / "src" / "pkg"
    nested_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    no_git_dir = tmp_path / "plain_folder"
    no_git_dir.mkdir()

    monkeypatch.delenv("MINNI_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("MINNI_AGENT_ID", raising=False)

    assert minni_cli._detect_cli_workspace("Explicit-Repo", cwd=nested_dir) == "workspace-explicit-repo"
    assert minni_cli._detect_cli_workspace(None, cwd=nested_dir) == "workspace-my-cool-repo"
    assert minni_cli._detect_cli_workspace(None, cwd=no_git_dir) == "default"

    monkeypatch.setenv("MINNI_WORKSPACE_ID", "env-override")
    assert minni_cli._detect_cli_workspace(None, cwd=nested_dir) == "workspace-env-override"
    assert minni_cli._detect_cli_workspace("flag-wins", cwd=nested_dir) == "workspace-flag-wins"

    # Verify recall/learn omit agent_id unless --agent or MINNI_AGENT_ID is explicitly provided
    monkeypatch.delenv("MINNI_WORKSPACE_ID", raising=False)
    monkeypatch.chdir(nested_dir)
    captured_calls: list[tuple[str, dict]] = []

    def fake_rpc(_sock, method, params=None, timeout=30.0):
        captured_calls.append((method, dict(params or {})))
        if method == "search":
            return {"results": [], "learnings": [], "count": 0}
        if method == "learn":
            return {"status": "proposed", "candidate_id": 42}
        if method == "resolve_candidate":
            return {"status": "accepted", "learning_id": 7}
        return {}

    monkeypatch.setattr(minni_cli, "_rpc", fake_rpc)

    rc = minni_cli.main(["--socket", str(tmp_path / "s.sock"), "recall", "schema", "check"])
    assert rc == 0
    assert "No strong match found." in capsys.readouterr().out
    assert captured_calls[0][0] == "search"
    assert captured_calls[0][1]["workspace_id"] == "workspace-my-cool-repo"
    assert "agent_id" not in captured_calls[0][1]

    rc = minni_cli.main(
        [
            "--socket",
            str(tmp_path / "s.sock"),
            "learn",
            "Always backfill workspace_id cleanly",
            "--title",
            "Wave 2 Rule",
            "--category",
            "architecture",
            "--accept",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Recorded durable learning #7" in out
    assert captured_calls[1][0] == "learn"
    assert captured_calls[1][1]["content"] == "Wave 2 Rule: Always backfill workspace_id cleanly"
    assert captured_calls[1][1]["workspace_id"] == "workspace-my-cool-repo"
    assert "agent_id" not in captured_calls[1][1]
    assert captured_calls[2][0] == "resolve_candidate"
    assert captured_calls[2][1] == {
        "candidate_id": 42,
        "decision": "accept",
        "reason": "cli --accept",
    }

    # Explicit --agent DOES populate agent_id
    rc = minni_cli.main(
        ["--socket", str(tmp_path / "s.sock"), "recall", "probe", "--agent", "gemini"]
    )
    assert rc == 0
    capsys.readouterr()
    assert captured_calls[3][1]["agent_id"] == "gemini"


def test_live_minnid_subprocess_cli_learn_accept_recall_and_activity():
    """Real end-to-end subprocess test exercising `minni learn --accept`, `minni recall`,
    and `minni activity` against a live `minnid` Unix socket + post-stop SQLite fallback.
    """
    import os
    import subprocess
    import time

    repo_root = Path(__file__).resolve().parents[1]
    td = Path(tempfile.mkdtemp(prefix="minni-w2-", dir="/tmp"))
    td.chmod(0o700)
    sock_path = td / "run" / "minnid.sock"
    (td / "run").mkdir(parents=True, exist_ok=True)
    (td / "home").mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    env["HOME"] = str(td / "home")
    env["MINNI_HOME"] = str(td)
    env["MINNI_SOCKET_PATH"] = str(sock_path)
    env["MINNI_WARMUP"] = "off"
    env["MINNI_BACKFILL"] = "off"
    env["MINNI_DECAY"] = "off"
    env["MINNI_VAULT_WATCH"] = "off"
    env["MINNI_AFM_LOOP"] = "off"
    env["MINNI_AFM_PROVIDER_MODE"] = "disabled"
    env.pop("MINNI_AGENT_ID", None)
    env.pop("MINNI_WORKSPACE_ID", None)

    daemon_log = td / "daemon.log"
    daemon_proc: subprocess.Popen | None = None
    log_fh = daemon_log.open("w", encoding="utf-8")
    try:
        daemon_proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "minni.minnid", "--socket", str(sock_path)],
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(repo_root),
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if daemon_proc.poll() is not None:
                log_fh.flush()
                pytest.fail(
                    f"minnid exited early ({daemon_proc.returncode}):\n"
                    f"{daemon_log.read_text(encoding='utf-8', errors='replace')}"
                )
            if minni_cli._daemon_alive(sock_path):
                break
            time.sleep(0.05)
        else:
            log_fh.flush()
            pytest.fail(
                f"minnid failed to bind socket within 20s:\n"
                f"{daemon_log.read_text(encoding='utf-8', errors='replace')}"
            )

        def run_cli(*cli_args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "minni.minni_cli", "--socket", str(sock_path), *cli_args],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        # 1. `minni learn ... --accept` promotes durably in one command
        learn_acc = run_cli(
            "learn",
            "Always run migration 025 idempotently across existing SQLite columns",
            "--title",
            "Migration 025 Invariant",
            "--category",
            "architecture",
            "--workspace",
            "workspace-alpha",
            "--accept",
        )
        assert learn_acc.returncode == 0, learn_acc.stderr
        assert "Recorded durable learning #" in learn_acc.stdout
        assert "(workspace-alpha)" in learn_acc.stdout

        # Stage a proposed candidate in workspace-beta (unaccepted) + an accepted scar in workspace-alpha
        learn_prop = run_cli(
            "learn",
            "Pending beta proposal awaiting review",
            "--category",
            "decision",
            "--workspace",
            "workspace-beta",
        )
        assert learn_prop.returncode == 0, learn_prop.stderr
        assert "Staged candidate #" in learn_prop.stdout

        learn_scar = run_cli(
            "learn",
            "Never emit blank EVIDENCE envelopes on frontmatter-only notes",
            "--category",
            "scar",
            "--workspace",
            "workspace-alpha",
            "--accept",
        )
        assert learn_scar.returncode == 0, learn_scar.stderr

        # 2. `minni recall` finds the accepted workspace-alpha learning and prints empty state on miss
        rec_hit = run_cli(
            "recall",
            "Migration 025 Invariant SQLite columns",
            "--workspace",
            "workspace-alpha",
            "--mode",
            "fast",
        )
        assert rec_hit.returncode == 0, rec_hit.stderr
        assert "workspace-alpha" in rec_hit.stdout
        assert "Migration 025 Invariant" in rec_hit.stdout

        rec_json = run_cli(
            "recall",
            "Migration 025 Invariant SQLite columns",
            "--workspace",
            "workspace-alpha",
            "--mode",
            "fast",
            "--json",
        )
        assert rec_json.returncode == 0, rec_json.stderr
        rec_payload = json.loads(rec_json.stdout)
        assert rec_payload["workspace_id"] == "workspace-alpha"

        rec_miss = run_cli(
            "recall",
            "zyxwvutsrq_nonexistent_token_991122",
            "--workspace",
            "workspace-alpha",
            "--mode",
            "fast",
        )
        assert rec_miss.returncode == 0, rec_miss.stderr
        assert rec_miss.stdout.strip() == "No strong match found."

        # 3. Seed active Thread pointers (_active_plan.json + .runtime/active-thread.d/*.json)
        gemini_vault = td / "gemini-vault"
        plan_note = gemini_vault / "wiki" / "artifacts" / "plan-w2.md"
        plan_note.parent.mkdir(parents=True, exist_ok=True)
        plan_note.write_text(
            "---\ntitle: Wave 2 Storage Thread\nstatus: in_progress\nworkspace_id: workspace-alpha\n---\n\n# Wave 2 Storage Thread\n",
            encoding="utf-8",
        )
        (gemini_vault / "wiki" / "artifacts" / "_active_plan.json").write_text(
            json.dumps(
                {
                    "plan_id": "thread-wave2-main",
                    "notePath": "wiki/artifacts/plan-w2.md",
                    "host_pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        sess_dir = td / "codex-vault" / ".runtime" / "active-thread.d"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "0123456789abcdef.json").write_text(
            json.dumps(
                {
                    "plan_id": "thread-session-beta",
                    "title": "Beta Session Slice",
                    "status": "active",
                    "workspace_id": "workspace-beta",
                    "host_pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )

        # 4. `minni activity` against LIVE daemon
        act_live = run_cli("activity", "--hours", "24", "--json")
        assert act_live.returncode == 0, act_live.stderr
        act_data = json.loads(act_live.stdout)
        assert act_data["daemon"]["reachable"] is True
        assert act_data["pending_candidates"] == 1
        plan_ids = {t["plan_id"] for t in act_data["active_threads"]}
        assert {"thread-wave2-main", "thread-session-beta"} <= plan_ids
        assert "workspace-alpha" in act_data["by_workspace"]
        assert len(act_data["by_workspace"]["workspace-alpha"]["learnings"]) >= 2
        assert len(act_data["by_workspace"]["workspace-alpha"]["scars"]) >= 1

        # 5. Stop daemon and prove graceful read-only SQLite fallback on `minni activity`
        daemon_proc.terminate()
        daemon_proc.wait(timeout=10)
        daemon_proc = None

        act_offline = run_cli("activity", "--hours", "24")
        assert act_offline.returncode == 0, act_offline.stderr
        assert "Daemon: stopped (degraded to read-only SQLite)" in act_offline.stdout
        assert "thread-wave2-main" in act_offline.stdout
        assert "[workspace-alpha]" in act_offline.stdout
    finally:
        if daemon_proc is not None and daemon_proc.poll() is None:
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()
                daemon_proc.wait(timeout=5)
        log_fh.close()
        shutil.rmtree(td, ignore_errors=True)


def test_activity_reads_episodic_events_created_at_and_normalizes_workspace_default(
    tmp_path: Path,
) -> None:
    """Cassandra regression (BUG-20/21): episodic_events uses created_at, and workspace-default/global canonicalize."""
    import time

    from minni.config import MinniConfig
    from minni.db import MinniDB

    assert minni_cli._normalize_cli_workspace("workspace-default") == "default"
    assert minni_cli._normalize_cli_workspace("workspace-global") == "global"

    cfg = MinniConfig(
        db_path=tmp_path / "minni.db",
        vault_path=tmp_path / "vault",
        faiss_index_path=tmp_path / "faiss.index",
        writeback_path=tmp_path / "learnings",
    )
    db = MinniDB(cfg)
    now = time.time()
    with db.transaction() as c:
        c.execute(
            """INSERT INTO episodic_events (agent_id, event_type, content, metadata, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "claude-code",
                "thread_scar",
                "Never query episodic_events.timestamp when schema uses created_at",
                json.dumps({"workspace_id": "workspace-alpha"}),
                now,
            ),
        )
    db.close()

    snap = minni_cli._read_sqlite_activity(tmp_path / "minni.db", cutoff_epoch=now - 3600.0)
    assert snap.get("db_error") is None
    assert any(
        s["category"] == "thread_scar"
        and s["workspace_id"] == "workspace-alpha"
        and "created_at" in s["summary"]
        for s in snap["recent_scars"]
    )




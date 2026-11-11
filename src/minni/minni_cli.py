#!/usr/bin/env python3
"""minni — daemon lifecycle and health CLI.

The newcomer-facing entry point: drive the minnid daemon without knowing what a
Unix socket is.

    minni up        start the daemon in the background
    minni status    show daemon and engine health in plain language
    minni recall    search project-scoped and global memories
    minni learn     propose or durably record a lesson (--accept)
    minni activity  summarize daemon status, pending candidates, active threads, and recent lessons
    minni doctor    check local interpreter, socket, daemon, recall, and cache health
    minni wire      wire the plugin payload to an agent platform
    minni wire-adopt cut a platform's plugin surface over to the wire tree
    minni sync      redeploy the plugin to every wired agent (keep hosts current)
    minni watch     live tail of memory activity (audit trail + daemon events)
    minni down      stop the daemon

Packaging-only surface (PACKAGING_PLAN.md §3): this module is stdlib-only and
never imports engine internals, so it cannot change how memory is stored,
recalled, scored, or governed. `doctor` shares the status and recall
response-shape probes with scripts/repro-smoke.sh: status returns `daemon` +
`engine`, and search returns a `results` key. It does not run the smoke script's isolated-home check.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from minni import today_digest

RUN_DIR = Path.home() / ".minni" / "run"
LOG_DIR = Path.home() / ".minni" / "logs"
DEFAULT_SOCKET = RUN_DIR / "minnid.sock"
PID_FILE = RUN_DIR / "minnid.pid"
DAEMON_SCRIPT = Path(__file__).resolve().parent / "minnid.py"

# Models the engine lazily downloads on first retrieval (engine/config.py
# defaults). Sizes are approximate published weights, shown so a first run is
# never a silent multi-minute hang.
EXPECTED_MODELS = {
    "sentence-transformers/all-MiniLM-L6-v2": "~90 MB",
    "cross-encoder/ms-marco-MiniLM-L-6-v2": "~90 MB",
    "cross-encoder/nli-deberta-v3-small": "~140 MB",
}
MODELS_TOTAL_NOTE = "~320 MB, one time, cached in your HuggingFace cache"

UP_TIMEOUT_SECONDS = 60
DOWN_TIMEOUT_SECONDS = 15

# Doctor-only diagnostic budget (#388). Passed explicitly as this probe's
# socket timeout; the global _rpc default stays 30.0 for every other caller.
# The probe also passes a matching timeout_ms (an existing search param the
# daemon uses as a cooperative retrieval deadline). This does not cancel the
# worker: post-retrieval bookkeeping and noninterruptible work can outlast the
# socket timeout. A transport timeout is not evidence that server work stopped.
RECALL_PROBE_BUDGET_S = 120.0
# A successful probe slower than this is transport-ok but NOT healthy
# performance (prompt-time hook callers discard at 30s; #388 shows warm
# search can exceed it). Reported as WARN, never PASS.
RECALL_SLOW_WARN_S = 10.0


class RpcError(Exception):
    """A JSON-RPC round-trip failed (transport or daemon-reported error)."""


class RpcTimeoutError(RpcError):
    """The socket timed out waiting for a reply (transport kill, not a
    diagnosis — the daemon may still be working). Subclasses RpcError so
    existing handlers keep working; catch it first when the distinction
    matters."""


def _rpc(socket_path: Path, method: str, params: dict | None = None,
         timeout: float = 30.0) -> dict:
    """JSON-RPC 2.0 over the Unix socket. Raises RpcError instead of exiting,
    so doctor can turn failures into readable findings."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method,
               "params": params or {}}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(socket_path))
            s.sendall((json.dumps(request) + "\n").encode())
            chunks: list[bytes] = []
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in b"".join(chunks):
                    break
    except FileNotFoundError as exc:
        raise RpcError(f"socket {socket_path} does not exist") from exc
    except ConnectionRefusedError as exc:
        msg = "connection refused — daemon not listening"
        if sys.platform == "darwin":
            msg += " (if managed by launchd, try: launchctl kickstart -k gui/$(id -u)/com.minni.minnid)"
        raise RpcError(msg) from exc
    except socket.timeout as exc:
        raise RpcTimeoutError(
            f"request timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise RpcError(str(exc)) from exc

    data = b"".join(chunks)
    if not data:
        raise RpcError("empty response from daemon")
    try:
        resp = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise RpcError(f"malformed response: {exc}") from exc
    if "error" in resp:
        err = resp["error"]
        raise RpcError(f"daemon error {err.get('code', '?')}: "
                       f"{err.get('message', '')}")
    return resp.get("result", {})


def _daemon_alive(socket_path: Path) -> bool:
    try:
        _rpc(socket_path, "ping", timeout=5.0)
        return True
    except RpcError:
        return False


def _read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return pid


def _hf_cache_dir() -> Path:
    if "HF_HUB_CACHE" in os.environ:
        return Path(os.environ["HF_HUB_CACHE"])
    hf_home = Path(os.environ.get(
        "HF_HOME", Path.home() / ".cache" / "huggingface"))
    return hf_home / "hub"


def _models_present() -> tuple[list[str], list[str]]:
    """Return (present, missing) model names by checking the HF cache layout
    (hub/models--org--name directories with at least one snapshot)."""
    cache = _hf_cache_dir()
    present, missing = [], []
    for name in EXPECTED_MODELS:
        marker = cache / ("models--" + name.replace("/", "--"))
        snapshots = marker / "snapshots"
        if snapshots.is_dir() and any(snapshots.iterdir()):
            present.append(name)
        else:
            missing.append(name)
    return present, missing


# ── commands ──────────────────────────────────────────────────────────────


def cmd_up(args: argparse.Namespace) -> int:
    sock = Path(args.socket)
    if _daemon_alive(sock):
        print(f"minnid is already running (socket: {sock}).")
        return 0
    if not DAEMON_SCRIPT.exists():
        print(f"Cannot find the daemon at {DAEMON_SCRIPT}.", file=sys.stderr)
        return 1

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.chmod(0o700)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.chmod(0o700)
    log_path = LOG_DIR / "minnid.log"

    _, missing = _models_present()
    if missing:
        print(f"First run detected: the daemon downloads embedding models on "
              f"first recall ({MODELS_TOTAL_NOTE}).")

    cmd = [sys.executable, str(DAEMON_SCRIPT), "--socket", str(sock)]
    if args.foreground:
        print(f"Starting minnid in the foreground (socket: {sock}). "
              "Ctrl-C stops it.")
        return subprocess.call(cmd)

    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log,
                                start_new_session=True)
    PID_FILE.write_text(f"{proc.pid}\n")
    PID_FILE.chmod(0o600)

    deadline = time.monotonic() + UP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _daemon_alive(sock):
            print(f"minnid is up (pid {proc.pid}, socket: {sock}).")
            print(f"Logs: {log_path}")
            print("Next: `minni doctor` verifies the install end to end.")
            return 0
        if proc.poll() is not None:
            print(f"minnid exited immediately (code {proc.returncode}). "
                  f"See {log_path}", file=sys.stderr)
            return 1
        time.sleep(0.5)
    print(f"minnid did not answer within {UP_TIMEOUT_SECONDS}s. "
          f"See {log_path}", file=sys.stderr)
    return 1


def cmd_down(args: argparse.Namespace) -> int:
    sock = Path(args.socket)
    pid = _read_pid()
    if pid is None:
        if _daemon_alive(sock):
            print("A daemon is answering on the socket but was not started "
                  "by `minni up` (no PID file).\n"
                  "Stop it where it was started (Ctrl-C the `make daemon` "
                  "shell, or `launchctl bootout gui/$UID/com.minni.minnid` "
                  "if you installed the launchd unit).", file=sys.stderr)
            return 1
        print("minnid is not running.")
        PID_FILE.unlink(missing_ok=True)
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + DOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            PID_FILE.unlink(missing_ok=True)
            print(f"minnid stopped (pid {pid}).")
            return 0
        time.sleep(0.3)
    print(f"minnid (pid {pid}) did not stop within {DOWN_TIMEOUT_SECONDS}s. "
          "It may be finishing a write; retry in a moment.", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    sock = Path(args.socket)
    try:
        result = _rpc(sock, "status")
    except RpcError as exc:
        msg = f"minnid is not reachable: {exc}\nStart it with: minni up"
        if sys.platform == "darwin" and "launchctl kickstart" not in str(exc):
            msg += "\nIf managed by launchd, try: launchctl kickstart -k gui/$(id -u)/com.minni.minnid"
        print(msg, file=sys.stderr)
        return 1
    daemon = result.get("daemon", {})
    engine = result.get("engine", {})
    stats = engine.get("stats", {})
    uptime = int(daemon.get("uptime_seconds", 0))
    print(f"minnid {daemon.get('version', '?')} — running "
          f"(up {uptime // 3600}h {uptime % 3600 // 60}m, "
          f"{daemon.get('requests_served', 0)} requests served)")
    minni_home = _minni_home_dir()
    db_override = (os.environ.get("MINNI_DB_PATH") or "").strip()
    db_path = Path(db_override).expanduser() if db_override else (minni_home / "minni.db")
    if db_path.is_file():
        today_digest.refresh_today_digest_safe(
            minni_home, daemon_running=True, db_path=db_path, bypass_cache=True
        )
    try:
        overview = today_digest.collect_today_digest_data(
            minni_home, daemon_running=True, db_path=db_path
        )
    except Exception:
        overview = {}
    active_plans_cnt = int(overview.get("active_plans_count") or 0)  # type: ignore[arg-type]
    mapped_cnt = int(overview.get("mapped_projects_count") or 0)  # type: ignore[arg-type]
    conflicts_cnt = int(overview.get("conflicts_count") or 0)  # type: ignore[arg-type]
    call_text = (
        "nothing waiting on you"
        if conflicts_cnt == 0
        else f"{conflicts_cnt} unresolved conflict{'s' if conflicts_cnt != 1 else ''}"
    )
    print(f"  Active plans: {active_plans_cnt}")
    print(f"  Mapped projects: {mapped_cnt}")
    print(f"  Needs your call: {call_text}")
    print(f"  Daily digest: {minni_home / 'TODAY.md'}")
    print(f"  database: {'ok' if engine.get('db_ok') else 'NOT OK'} — "
          f"{stats.get('documents', 0)} documents, "
          f"{stats.get('learnings', 0)} learnings, "
          f"{stats.get('events', 0)} events")
    print(f"  vector index: {'ok' if engine.get('faiss_ok') else 'NOT OK'}")
    print(_format_latency_block(daemon.get("latencies", {})))
    deploy = daemon.get("deploy")
    if isinstance(deploy, dict):
        if deploy.get("stale") is True:
            print(f"  deploy: STALE — {deploy.get('reason', 'running code or plugin dist lags checkout')}")
        stale_srvs = deploy.get("running_stale_servers")
        if isinstance(stale_srvs, list) and stale_srvs:
            pids_str = ", ".join(
                f"pid={s.get('pid')} (ppid={s.get('ppid')}, ver={s.get('version')})"
                for s in stale_srvs
                if isinstance(s, dict)
            )
            print(f"  running_stale_servers: {pids_str}")
    return 0


def _check(label: str, ok: bool, detail: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    return ok


def _as_latency_ms(value: object) -> float | None:
    """Coerce a p50_ms/p95_ms value; None when missing, non-numeric,
    non-finite, negative, or bool — never print nan/inf or bogus digits."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _as_sample_count(value: object) -> int | None:
    """Coerce a sample count; None when missing, bool, negative, or not an
    integer value — a bogus count must not print as samples."""
    if isinstance(value, bool):
        return None
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if count < 0 or float(value) != count:  # type: ignore[arg-type]
        return None
    return count


def _format_latency_value(value_ms: object) -> str:
    """Render a p50_ms/p95_ms value with units, or 'n/a' when unusable."""
    value = _as_latency_ms(value_ms)
    if value is None:
        return "n/a"
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.0f}ms"


def _format_latency_block(latencies: object) -> str:
    """Render daemon.latencies ({method: {count, p50_ms, p95_ms}}) honestly.

    Only methods the daemon actually reports are shown; a method with no
    samples (e.g. cross_encoder before any rerank has run) reads 'no
    samples yet' — never a fabricated zero. Malformed rows read
    'unavailable', and a missing/unshaped payload reads as not-reported.
    """
    if not isinstance(latencies, dict) or not latencies:
        return "  latencies: not reported by this daemon"
    rows = []
    for name in sorted(latencies):
        entry = latencies[name]
        count = (_as_sample_count(entry.get("count", 0))
                 if isinstance(entry, dict) else None)
        if count is None:
            rows.append(f"  latency {name}: unavailable")
            continue
        if count == 0:
            rows.append(f"  latency {name}: no samples yet")
            continue
        rows.append(
            f"  latency {name}: {count} samples, "
            f"p50 {_format_latency_value(entry.get('p50_ms'))}, "
            f"p95 {_format_latency_value(entry.get('p95_ms'))}")
    return "  latencies (daemon rolling window):\n" + "\n".join(rows)


def _latency_p50_ms(latencies: dict, method: str) -> float | None:
    """Pull a method's p50_ms from a status latencies payload, if usable."""
    entry = latencies.get(method)
    if not isinstance(entry, dict):
        return None
    if (_as_sample_count(entry.get("count", 0)) or 0) <= 0:
        return None
    return _as_latency_ms(entry.get("p50_ms"))


def _classify_recall_probe(shape_ok: bool, shape_note: str, elapsed_s: float,
                           search_p50_ms: float | None,
                           degraded: bool = False) -> tuple[str, str]:
    """Classify the doctor recall probe without overstating success.

    Returns (level, detail) with level in {pass, warn, fail}. A transport
    success that is slow or daemon-flagged degraded is 'warn': the round
    trip worked, but that is not evidence of healthy performance (#388).
    The daemon's own search p50 (when the status call already reported it)
    is cited so a slow probe against a known-slow engine reads as
    expected-slow, not wedged.
    """
    daemon_note = ""
    if search_p50_ms is not None:
        daemon_note = (f"; daemon status already showed search p50 "
                       f"{_format_latency_value(search_p50_ms)}")
    if not shape_ok:
        return ("fail", f"unexpected shape: {shape_note}{daemon_note}")
    reasons = []
    if degraded:
        reasons.append("daemon reported the result degraded")
    if elapsed_s > RECALL_SLOW_WARN_S:
        reasons.append(f"answered in {elapsed_s:.1f}s "
                       f"(over {RECALL_SLOW_WARN_S:.0f}s)")
    if reasons:
        return ("warn", "; ".join(reasons)
                + ": transport ok but not healthy performance"
                + daemon_note)
    return ("pass",
            f"a recall round-trips through retrieval in {elapsed_s:.1f}s")


def cmd_wire(args: argparse.Namespace) -> int:
    from minni.wire.flow import run_wire
    return run_wire(args)


def cmd_sync(args: argparse.Namespace) -> int:
    """Keep every Minni-serviced agent host on this install's plugin payload.

    Fixes "main/package moved but Claude/Codex/Grok still run last week's
    server.js". See docs/install.md § Fleet sync and deploy/README.md.
    """
    from minni.fleet_sync import (
        auto_sync_status,
        install_auto_sync_agent,
        run_fleet_sync,
        uninstall_auto_sync_agent,
    )

    if getattr(args, "install_auto", False):
        result = install_auto_sync_agent()
    elif getattr(args, "uninstall_auto", False):
        result = uninstall_auto_sync_agent()
    elif getattr(args, "auto_status", False):
        result = auto_sync_status()
    else:
        result = run_fleet_sync(
            dry_run=bool(getattr(args, "dry_run", False)),
            full=bool(getattr(args, "full", False)),
            force_reinstall=not bool(getattr(args, "no_force", False)),
            prune=not bool(getattr(args, "no_prune", False)),
            restart_daemon=not bool(getattr(args, "no_restart", False)),
            propagate_hosts=not bool(getattr(args, "wire_only", False)),
        )

    print(json.dumps(result.to_dict(), indent=2))
    print(f"\n{result.message}")
    if result.next_actions:
        print("Next:")
        for line in result.next_actions:
            print(f"  - {line}")
    return 0 if result.ok else 1


def cmd_wire_adopt(args: argparse.Namespace) -> int:
    """One-time cutover of Claude Code's plugin surface onto the wire tree."""
    import json as _json

    from minni.wire.claude_plugin import ClaudePluginError, adopt_claude_code

    if args.platform != "claude-code":
        print(
            f"minni wire-adopt: unsupported platform {args.platform!r}; "
            "only claude-code needs adoption",
            file=sys.stderr,
        )
        return 2
    try:
        result = adopt_claude_code(
            apply=args.apply, keep_legacy_cache=args.keep_legacy_cache,
        )
    except ClaudePluginError as exc:
        print(f"minni wire-adopt: {exc}", file=sys.stderr)
        return 1
    print(_json.dumps(result, indent=2))
    if not args.apply:
        print(
            "\n[wire-adopt] dry run — nothing was written. Re-run with --apply.",
            file=sys.stderr,
        )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    # watch.py is stdlib-only and strictly read-only, so the packaging
    # contract of this module (no engine imports) is preserved.
    from datetime import datetime, timedelta, timezone

    from minni.watch import run_watch

    if args.interval <= 0:
        print("minni watch: --interval must be a positive number of seconds",
              file=sys.stderr)
        return 2

    since = None
    if args.since:
        raw = args.since.strip()
        match = re.fullmatch(r"(\d+)([smhd])", raw)
        if match:
            unit = {"s": "seconds", "m": "minutes",
                    "h": "hours", "d": "days"}[match.group(2)]
            since = (datetime.now(timezone.utc)
                     - timedelta(**{unit: int(match.group(1))}))
        else:
            try:
                since = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
            except ValueError:
                print(f"minni watch: cannot parse --since {raw!r} "
                      "(use e.g. 10m, 2h, or an ISO timestamp)",
                      file=sys.stderr)
                return 2
    args.since = since
    return run_watch(args)


def cmd_doctor(args: argparse.Namespace) -> int:
    sock = Path(args.socket)
    print("minni doctor — verifying the install")
    failures = 0

    # 1. Interpreter (same floor make setup enforces).
    py_ok = sys.version_info >= (3, 14)
    failures += not _check(
        "python", py_ok,
        f"{sys.version.split()[0]}"
        + ("" if py_ok else " — engine requires 3.14+ (see README Quickstart)"))

    # 2. Socket presence + permissions (SEC-001: run dir 0700, socket 0600).
    sock_ok = sock.exists()
    if sock_ok:
        sock_mode = stat.S_IMODE(sock.stat().st_mode)
        dir_mode = stat.S_IMODE(sock.parent.stat().st_mode)
        perms_ok = sock_mode == 0o600 and dir_mode == 0o700
        failures += not _check(
            "socket", perms_ok,
            f"{sock} (socket {sock_mode:03o}, dir {dir_mode:03o})"
            + ("" if perms_ok else " — expected socket 600 in dir 700"))
    else:
        failures += not _check(
            "socket", False,
            f"{sock} does not exist — daemon not running? Try: minni up")

    # 3+4. The two smoke-script probes, assertion-for-assertion
    # (scripts/repro-smoke.sh: STATUS_OK and RECALL_OK).
    status_latencies: dict = {}
    try:
        status = _rpc(sock, "status")
        status_ok = "daemon" in status and "engine" in status
        detail = ("daemon answered with daemon+engine health"
                  if status_ok else f"unexpected shape: {sorted(status)}")
        if status_ok and isinstance(status, dict):
            daemon_status = status.get("daemon", {})
            if isinstance(daemon_status, dict):
                raw_latencies = daemon_status.get("latencies", {})
                if isinstance(raw_latencies, dict):
                    status_latencies = raw_latencies
    except RpcError as exc:
        status_ok, detail = False, str(exc)
    failures += not _check("daemon status", status_ok, detail)

    # Recall probe (#388): bounded diagnostic budget, timed and graded — a
    # success that is slow or daemon-flagged degraded is WARN, never PASS,
    # so transport success is not misread as healthy performance. Only a
    # transport timeout FAILs against the probe budget (not the daemon's
    # health), and it cites the status latencies when the engine was already
    # known-slow, so saturation does not read as a wedged daemon. Other
    # RpcErrors (refused/auth/malformed) surface verbatim.
    search_p50_ms = _latency_p50_ms(status_latencies, "search")
    warns = 0
    probe_start = time.monotonic()
    try:
        found = _rpc(sock, "search",
                     {"query": "smoke test recall", "limit": 1,
                      "timeout_ms": int(RECALL_PROBE_BUDGET_S * 1000)},
                     timeout=RECALL_PROBE_BUDGET_S)
        elapsed = time.monotonic() - probe_start
        results = found.get("results") if isinstance(found, dict) else None
        shape_ok = isinstance(results, list)
        shape_note = ("" if shape_ok
                      else f"results is {type(results).__name__}")
        degraded = (isinstance(found, dict)
                    and found.get("degraded") is True)
        level, detail = _classify_recall_probe(
            shape_ok, shape_note, elapsed, search_p50_ms, degraded)
        if level == "pass":
            _check("recall round-trip", True, detail)
        elif level == "warn":
            print(f"  [WARN] recall round-trip: {detail}")
            warns += 1
        else:
            failures += not _check("recall round-trip", False, detail)
    except RpcTimeoutError as exc:
        elapsed = time.monotonic() - probe_start
        timeout_note = (
            f"no answer within the {RECALL_PROBE_BUDGET_S:.0f}s probe "
            f"budget (after {elapsed:.1f}s)")
        if search_p50_ms is not None:
            timeout_note += (
                f"; status already showed search p50 "
                f"{_format_latency_value(search_p50_ms)} — slow engine, "
                f"not necessarily wedged; compare `minni status` latencies")
        failures += not _check(
            "recall round-trip", False, f"{exc} — {timeout_note}")
    except RpcError as exc:
        failures += not _check("recall round-trip", False, str(exc))

    # 5. Embedding models (WARN, not FAIL: retrieval degrades gracefully and
    # the daemon downloads them on first recall).
    present, missing = _models_present()
    if missing:
        print(f"  [WARN] models: {len(present)}/{len(EXPECTED_MODELS)} in "
              f"cache; first recall downloads the rest ({MODELS_TOTAL_NOTE}):")
        for name in missing:
            print(f"         - {name} ({EXPECTED_MODELS[name]})")
    else:
        print(f"  [PASS] models: all {len(EXPECTED_MODELS)} embedding/rerank "
              "models cached")

    # 6. Fleet freshness & host propagation health (FAIL-CLOSED, §4.7 / P1-7):
    # Fail doctor on daemon deploy lag and on audit_host_propagation_health drift.
    try:
        from minni.minnid_runtime.deploy_honesty import deploy_status
        from minni.wire.health import audit_host_propagation_health

        local_deploy = deploy_status()
        rpc_deploy = (
            status.get("daemon", {}).get("deploy")
            if status_ok and isinstance(status, dict)
            else None
        )
        if isinstance(rpc_deploy, dict):
            deploy = dict(rpc_deploy)
            if "plugin_dist" not in deploy and isinstance(local_deploy.get("plugin_dist"), dict):
                deploy["plugin_dist"] = local_deploy["plugin_dist"]
        else:
            deploy = dict(local_deploy)
            # When the daemon status RPC did not include a deploy block (e.g. a
            # minimal test stub), do not conflate an uncommitted local worktree
            # edit with daemon lag unless start SHA or plugin_dist actually lags.
            if (
                deploy.get("stale") is True
                and deploy.get("started_dirty") is True
                and deploy.get("started_git_sha") == deploy.get("current_git_sha")
                and not (deploy.get("plugin_dist") or {}).get("stale")
            ):
                deploy["stale"] = False

        plugin = (deploy or {}).get("plugin_dist") or {}
        home_dir = Path.home()
        current_link = home_dir / ".minni" / "plugin" / "current"
        health = audit_host_propagation_health(home=home_dir)
        has_current = current_link.exists() or current_link.is_symlink()
        health_failed = bool(
            health.drifted_surfaces
            or health.uncatalogued_refs
            or health.running_stale_servers
            or (has_current and not health.current_valid)
        )
        deploy_stale = (
            deploy.get("stale") is True
            or plugin.get("stale") is True
            or bool(deploy.get("running_stale_servers"))
        )
        if deploy_stale or health_failed:
            failures += 1
            reason = deploy.get("reason") or plugin.get("reason") or "host propagation drift or deploy lag"
            print("  [FAIL] fleet: install or host surface is stale/drifted relative to this package/checkout")
            print(f"         {reason}")
            if has_current and not health.current_valid and health.current_error:
                print(f"         current: {health.current_error}")
            for surf in health.drifted_surfaces:
                print(
                    f"         drifted surface: {surf.get('host')} ({surf.get('path')}): "
                    f"{', '.join(surf.get('reasons') or [])}"
                )
            for ref in health.uncatalogued_refs:
                print(f"         uncatalogued ref: {ref.get('path')} ({ref.get('reason')})")
            for srv in health.running_stale_servers:
                print(
                    f"         stale MCP server: pid={srv.get('pid')} "
                    f"version={srv.get('version')} sha={srv.get('git_sha')}"
                )
            print("         Fix: minni sync          # redeploy plugin to all wired hosts")
            print("              minni sync --full   # editable checkout: also git pull + rebuild")
        else:
            print("  [PASS] fleet: plugin/daemon and host surfaces healthy")
    except Exception as exc:
        failures += 1
        print(f"  [FAIL] fleet: could not read deploy status ({exc})")

    if failures:
        print(f"\n{failures} check(s) failed. If the daemon is not running, "
              "start it with: minni up")
        return 1
    if warns:
        print(f"\nAll hard checks passed with {warns} warning(s) — see above; "
              "a slow recall probe is degraded performance, not a clean bill.")
        return 0
    print("\nAll checks passed — Minni is installed and answering.")
    return 0


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_EVIDENCE_OPEN_RE = re.compile(r"<EVIDENCE\b[^>]*>", re.IGNORECASE)
_EVIDENCE_CLOSE_RE = re.compile(r"</EVIDENCE>", re.IGNORECASE)
_SESSION_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_TERMINAL_THREAD_STATUSES = frozenset(
    {"completed", "complete", "done", "cancelled", "canceled", "abandoned", "archived"}
)


def _cli_term_safe(text: str) -> str:
    return _CONTROL_CHARS_RE.sub("", text)


def _minni_home_dir() -> Path:
    override = (os.environ.get("MINNI_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".minni"


def _default_socket_path() -> Path:
    sock_override = (os.environ.get("MINNI_SOCKET_PATH") or "").strip()
    if sock_override:
        return Path(sock_override).expanduser()
    if (os.environ.get("MINNI_HOME") or "").strip():
        return _minni_home_dir() / "run" / "minnid.sock"
    return DEFAULT_SOCKET


def _normalize_cli_workspace(raw: str | None) -> str:
    if not raw:
        return ""
    trimmed = raw.strip()
    if not trimmed:
        return ""
    lowered = trimmed.lower()
    if lowered in ("default", "global", "*", "workspace-unknown"):
        return lowered
    if lowered == "workspace-default":
        return "default"
    if lowered == "workspace-global":
        return "global"
    stripped = trimmed.rstrip("/\\") or trimmed
    base = os.path.basename(stripped).lower()
    if base.startswith("workspace-"):
        base = base[len("workspace-") :]
    sanitized = re.sub(r"[^a-z0-9_-]", "-", base)
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    if not sanitized or sanitized == "unknown":
        return "workspace-unknown" if lowered == "unknown" else "default"
    if sanitized in ("default", "global"):
        return sanitized
    return f"workspace-{sanitized}"


def _detect_cli_workspace(
    explicit_workspace: str | None = None,
    *,
    cwd: Path | None = None,
) -> str:
    """Resolve workspace_id: --workspace -> MINNI_WORKSPACE_ID -> git root -> 'default'."""
    if explicit_workspace is not None and explicit_workspace.strip():
        return _normalize_cli_workspace(explicit_workspace)
    env_ws = (os.environ.get("MINNI_WORKSPACE_ID") or "").strip()
    if env_ws:
        return _normalize_cli_workspace(env_ws)
    try:
        start = (cwd or Path.cwd()).resolve()
    except OSError:
        return "default"
    for candidate in (start, *start.parents):
        try:
            git_marker = candidate / ".git"
            if git_marker.exists():
                norm = _normalize_cli_workspace(candidate.name)
                return norm if norm and norm != "workspace-unknown" else "default"
        except OSError:
            continue
    return "default"


def _explicit_cli_agent(args: argparse.Namespace) -> str | None:
    """Return agent_id ONLY when explicitly passed via --agent or MINNI_AGENT_ID."""
    arg_agent = getattr(args, "agent", None)
    if isinstance(arg_agent, str) and arg_agent.strip():
        return arg_agent.strip()
    env_agent = (os.environ.get("MINNI_AGENT_ID") or "").strip()
    if env_agent:
        return env_agent
    return None


def _strip_evidence_tags(text: str) -> str:
    if not text:
        return ""
    cleaned = _EVIDENCE_OPEN_RE.sub("", text)
    cleaned = _EVIDENCE_CLOSE_RE.sub("", cleaned)
    return _cli_term_safe(re.sub(r"\s+", " ", cleaned).strip())


def cmd_recall(args: argparse.Namespace) -> int:
    sock = Path(args.socket)
    query_str = " ".join(args.query).strip()
    if not query_str:
        print("minni recall: query must not be empty", file=sys.stderr)
        return 2
    ws = _detect_cli_workspace(getattr(args, "workspace", None))
    params: dict[str, object] = {
        "query": query_str,
        "limit": max(1, int(getattr(args, "limit", 5) or 5)),
        "workspace_id": ws,
    }
    mode = getattr(args, "mode", None)
    if mode:
        params["mode"] = mode
    agent_id = _explicit_cli_agent(args)
    if agent_id:
        params["agent_id"] = agent_id

    try:
        result = _rpc(sock, "search", params)
    except RpcError as exc:
        print(f"minni recall: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0

    raw_docs = result.get("results") if isinstance(result, dict) else []
    raw_learnings = result.get("learnings") if isinstance(result, dict) else []
    cards: list[str] = []

    if isinstance(raw_docs, list):
        for item in raw_docs:
            if not isinstance(item, dict):
                continue
            snippet = _strip_evidence_tags(
                str(item.get("text") or item.get("snippet") or item.get("content") or "")
            )
            if not snippet:
                continue
            if len(snippet) > 220:
                snippet = snippet[:217] + "..."
            raw_score = item.get("score", 0.0)
            try:
                score_fmt = f"{float(raw_score):.2f}"
            except (TypeError, ValueError):
                score_fmt = "0.00"
            hit_ws = str(item.get("workspace_id") or ws or "default")
            source = _cli_term_safe(
                str(
                    item.get("wikilink")
                    or item.get("source")
                    or item.get("filename")
                    or "doc"
                )
            )
            cards.append(f"[{score_fmt}/{hit_ws}] {source}: {snippet}")

    if isinstance(raw_learnings, list):
        for item in raw_learnings:
            if not isinstance(item, dict):
                continue
            snippet = _strip_evidence_tags(str(item.get("content") or ""))
            if not snippet:
                continue
            if len(snippet) > 220:
                snippet = snippet[:217] + "..."
            raw_conf = item.get("confidence", 0.0)
            try:
                conf_fmt = f"{float(raw_conf):.2f}"
            except (TypeError, ValueError):
                conf_fmt = "0.00"
            learn_ws = str(item.get("workspace_id") or "default")
            lid = item.get("learning_id", "?")
            cat = _cli_term_safe(str(item.get("category") or "general"))
            cards.append(f"[{conf_fmt}/{learn_ws}] learning#{lid} ({cat}): {snippet}")

    if not cards:
        print("No strong match found.")
        return 0

    for card in cards:
        print(card)
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    sock = Path(args.socket)
    body = " ".join(args.content).strip()
    if not body:
        print("minni learn: content must not be empty", file=sys.stderr)
        return 2
    title = (getattr(args, "title", None) or "").strip()
    if title and not body.lower().startswith(f"{title.lower()}:"):
        content = f"{title}: {body}"
    else:
        content = body

    ws = _detect_cli_workspace(getattr(args, "workspace", None))
    category = (getattr(args, "category", None) or "general").strip() or "general"
    params: dict[str, object] = {
        "content": content,
        "category": category,
        "workspace_id": ws,
    }
    agent_id = _explicit_cli_agent(args)
    if agent_id:
        params["agent_id"] = agent_id

    try:
        learn_res = _rpc(sock, "learn", params)
    except RpcError as exc:
        print(f"minni learn: {exc}", file=sys.stderr)
        return 1

    output_payload: dict[str, object] = (
        dict(learn_res) if isinstance(learn_res, dict) else {"result": learn_res}
    )
    output_payload.setdefault("workspace_id", ws)

    if (
        getattr(args, "accept", False)
        and output_payload.get("status") == "proposed"
        and output_payload.get("candidate_id") is not None
    ):
        resolve_params: dict[str, object] = {
            "candidate_id": output_payload["candidate_id"],
            "decision": "accept",
            "reason": "cli --accept",
        }
        if agent_id:
            resolve_params["agent_id"] = agent_id
        try:
            resolve_res = _rpc(sock, "resolve_candidate", resolve_params)
        except RpcError as exc:
            print(f"minni learn (--accept): {exc}", file=sys.stderr)
            return 1
        if isinstance(resolve_res, dict):
            output_payload["resolution"] = resolve_res
            output_payload["status"] = resolve_res.get("status", "accepted")
            if resolve_res.get("learning_id") is not None:
                output_payload["learning_id"] = resolve_res["learning_id"]

    if getattr(args, "json", False):
        print(json.dumps(output_payload, indent=2))
        return 0

    status = str(output_payload.get("status") or "")
    cid = output_payload.get("candidate_id")
    lid = output_payload.get("learning_id")
    if status in ("accepted", "stored", "ok") or lid is not None:
        suffix = f" (accepted candidate #{cid})" if cid is not None else ""
        print(
            f"Recorded durable learning #{lid if lid is not None else '?'} "
            f"[{category}] ({ws}){suffix}"
        )
    elif status == "proposed":
        print(
            f"Staged candidate #{cid if cid is not None else '?'} "
            f"[{category}] ({ws}) — run with --accept or resolve_candidate to promote."
        )
    else:
        print(
            f"Learn status={status or 'unknown'} "
            f"candidate_id={cid} learning_id={lid} ({ws})"
        )
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    sock = Path(args.socket)
    raw_target = getattr(args, "path", None)
    target_path = (
        os.path.abspath(os.path.expanduser(raw_target))
        if raw_target is not None and str(raw_target).strip()
        else os.getcwd()
    )
    ws_arg = (getattr(args, "workspace", None) or "").strip() or None
    refresh = bool(getattr(args, "refresh", False))
    agent_id = _explicit_cli_agent(args)

    daemon_used = _daemon_alive(sock)
    if daemon_used:
        params: dict[str, object] = {
            "repo_path": target_path,
            "refresh": refresh,
        }
        if ws_arg:
            params["workspace_id"] = _normalize_cli_workspace(ws_arg)
        if agent_id:
            params["agent_id"] = agent_id
        try:
            result = _rpc(sock, "repo_map", params, timeout=120.0)
        except RpcError as exc:
            print(f"minni map: {exc}", file=sys.stderr)
            return 1
    else:
        try:
            from minni.repo_map import build_and_index_repo_map

            result = build_and_index_repo_map(
                target_path,
                workspace_id=_normalize_cli_workspace(ws_arg) if ws_arg else None,
                refresh=refresh,
            )
        except Exception as exc:
            print(f"minni map: {exc}", file=sys.stderr)
            return 1

    minni_home = _minni_home_dir()
    db_override = (os.environ.get("MINNI_DB_PATH") or "").strip()
    db_path = Path(db_override).expanduser() if db_override else (minni_home / "minni.db")
    if db_path.is_file() or (minni_home / "vault" / "maps").is_dir():
        today_digest.refresh_today_digest_safe(
            minni_home, daemon_running=daemon_used, db_path=db_path, bypass_cache=True
        )

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0

    ws_out = _cli_term_safe(str(result.get("workspace_id") or "default"))
    sha_out = _cli_term_safe(str(result.get("git_sha") or ""))[:12] or "(uncommitted/non-git)"
    subs_mapped = result.get("subsystems_mapped", 0)
    cards_indexed = result.get("cards_indexed", 0)
    print(f"Workspace:         {ws_out}")
    print(f"Git SHA:           {sha_out}")
    print(f"Subsystems mapped: {subs_mapped}")
    print(f"Cards indexed:     {cards_indexed}")
    subsystems = result.get("subsystems")
    if isinstance(subsystems, list) and subsystems:
        for item in subsystems:
            if not isinstance(item, dict):
                continue
            slug = _cli_term_safe(str(item.get("slug") or "?"))
            fcount = item.get("file_count", 0)
            rel = _cli_term_safe(str(item.get("card_rel_path") or ""))
            print(f"  - {slug} ({fcount} files) -> {rel}")
    return 0


def _pid_alive(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _agent_for_vault_dir(vault_dir: Path) -> str:
    name = vault_dir.name
    if name.endswith("-vault"):
        slug = name[: -len("-vault")]
        if slug in ("claudecode", "claude"):
            return "claude-code"
        return slug or "agent"
    return name or "agent"


def _parse_plan_note_metadata(note_path: Path) -> dict[str, object]:
    """Extract lightweight title/status/workspace_id/scars from a Thread markdown note."""
    info: dict[str, object] = {}
    try:
        raw = note_path.read_text(encoding="utf-8")
    except OSError:
        return info
    lines = raw.splitlines()
    idx = 0
    if lines and lines[0].strip() == "---":
        idx = 1
        while idx < len(lines):
            line = lines[idx].strip()
            if line in ("---", "..."):
                idx += 1
                break
            if ":" in line:
                key, _, val = line.partition(":")
                k = key.strip().lower()
                v = val.strip().strip("'\"")
                if k in ("title", "status", "workspace_id", "plan_id") and v:
                    info[k] = v
            idx += 1
    for line in lines[idx:]:
        stripped = line.strip()
        if stripped.startswith("# ") and "title" not in info:
            info["title"] = stripped[2:].strip()
            break
    # Check embedded JSON plan block or Scar bullet lines if present
    scars: list[dict[str, object]] = []
    json_blocks = re.findall(r"```json\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    for block in json_blocks:
        try:
            parsed = json.loads(block)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        for key in ("title", "status", "workspace_id", "plan_id"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip() and key not in info:
                info[key] = val.strip()
        raw_scars = parsed.get("scars")
        if isinstance(raw_scars, list):
            for s in raw_scars:
                if isinstance(s, dict):
                    scars.append(s)
                elif isinstance(s, str) and s.strip():
                    scars.append({"summary": s.strip()})
    if scars:
        info["scars"] = scars
    return info


def _parse_iso_epoch(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fval = float(value)
        return fval / 1000.0 if fval > 1e12 else fval
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _discover_active_threads(
    minni_home: Path,
    *,
    cutoff_epoch: float,
    workspace_filter: str | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Scan *-vault/wiki/artifacts/_active_plan.json & .runtime/active-thread.d/*.json."""
    vault_dirs: list[Path] = []
    seen_vaults: set[Path] = set()
    try:
        for child in sorted(minni_home.iterdir()):
            if child.is_dir() and child.name.endswith("-vault"):
                resolved = child.resolve()
                if resolved not in seen_vaults:
                    seen_vaults.add(resolved)
                    vault_dirs.append(child)
    except OSError:
        pass

    env_vaults_raw = (os.environ.get("MINNI_AGENT_VAULTS") or "").strip()
    if env_vaults_raw:
        try:
            env_map = json.loads(env_vaults_raw)
            if isinstance(env_map, dict):
                for vpath in env_map.values():
                    if isinstance(vpath, str) and vpath.strip():
                        p = Path(vpath).expanduser()
                        if p.is_dir() and p.resolve() not in seen_vaults:
                            seen_vaults.add(p.resolve())
                            vault_dirs.append(p)
        except ValueError:
            pass

    # Also check shared runtime active-thread.d under minni_home/.runtime/active-thread.d
    active_threads: list[dict[str, object]] = []
    recent_thread_scars: list[dict[str, object]] = []
    seen_threads: set[tuple[str, str]] = set()

    def _inspect_pointer(
        pointer_file: Path,
        vault_dir: Path | None,
        source_kind: str,
        session_key: str | None = None,
    ) -> None:
        try:
            data = json.loads(pointer_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        plan_id = str(data.get("plan_id") or data.get("thread_id") or "").strip()
        if not plan_id:
            return
        if not _pid_alive(data.get("host_pid")):
            return
        agent_id = str(
            data.get("agent_id")
            or (_agent_for_vault_dir(vault_dir) if vault_dir is not None else "main")
        )
        note_meta: dict[str, object] = {}
        note_path_raw = data.get("notePath") or data.get("note_path")
        if isinstance(note_path_raw, str) and note_path_raw.strip():
            np = Path(note_path_raw)
            containment_root = vault_dir if vault_dir is not None else minni_home
            if not np.is_absolute():
                np = containment_root / np
            if today_digest._is_safe_contained_file(np, home=containment_root):
                note_meta = _parse_plan_note_metadata(np)
        status = str(
            note_meta.get("status") or data.get("status") or "active"
        ).strip().lower()
        if status in _TERMINAL_THREAD_STATUSES:
            return
        raw_ws = str(
            data.get("workspace_id")
            or note_meta.get("workspace_id")
            or "default"
        )
        ws_norm = _normalize_cli_workspace(raw_ws) or "default"
        if workspace_filter and ws_norm != workspace_filter:
            return
        dedup_key = (agent_id, plan_id)
        if dedup_key not in seen_threads:
            seen_threads.add(dedup_key)
            entry: dict[str, object] = {
                "plan_id": plan_id,
                "agent_id": agent_id,
                "status": status,
                "workspace_id": ws_norm,
                "source": source_kind,
            }
            title = str(note_meta.get("title") or data.get("title") or "").strip()
            if title:
                entry["title"] = _cli_term_safe(title)
            updated = data.get("updated_at") or data.get("set_at")
            if isinstance(updated, str) and updated.strip():
                entry["updated_at"] = updated.strip()
            if session_key:
                entry["session_key"] = session_key
            active_threads.append(entry)

        note_scars = note_meta.get("scars")
        if isinstance(note_scars, list):
            for scar in note_scars:
                if not isinstance(scar, dict):
                    continue
                scar_ts = _parse_iso_epoch(
                    scar.get("created_at") or scar.get("ts") or scar.get("timestamp")
                )
                if scar_ts is not None and scar_ts < cutoff_epoch:
                    continue
                summary = _cli_term_safe(
                    str(
                        scar.get("lesson")
                        or scar.get("summary")
                        or scar.get("description")
                        or scar.get("what_failed")
                        or ""
                    ).strip()
                )
                if summary:
                    recent_thread_scars.append(
                        {
                            "workspace_id": ws_norm,
                            "plan_id": plan_id,
                            "agent_id": agent_id,
                            "summary": summary,
                            "created_at": scar_ts,
                        }
                    )

    for vdir in vault_dirs:
        active_ptr = vdir / "wiki" / "artifacts" / "_active_plan.json"
        if active_ptr.is_file():
            _inspect_pointer(active_ptr, vdir, "_active_plan.json")
        sess_dir = vdir / ".runtime" / "active-thread.d"
        try:
            sess_files = sorted(sess_dir.iterdir())
        except OSError:
            sess_files = []
        for sf in sess_files:
            if sf.suffix == ".json" and sf.is_file():
                _inspect_pointer(
                    sf, vdir, "active-thread.d", session_key=sf.stem
                )

    shared_sess_dir = minni_home / ".runtime" / "active-thread.d"
    try:
        shared_files = sorted(shared_sess_dir.iterdir())
    except OSError:
        shared_files = []
    for sf in shared_files:
        if sf.suffix == ".json" and sf.is_file():
            _inspect_pointer(sf, None, "active-thread.d", session_key=sf.stem)

    return active_threads, recent_thread_scars


def _read_sqlite_activity(
    db_path: Path,
    *,
    cutoff_epoch: float,
    workspace_filter: str | None = None,
) -> dict[str, object]:
    """Read-only SQLite query for stats, pending candidates, learnings, and scars."""
    out: dict[str, object] = {
        "db_available": False,
        "stats": {},
        "pending_candidates": 0,
        "pending_by_workspace": {},
        "recent_learnings": [],
        "recent_scars": [],
    }
    if not db_path.is_file():
        return out
    uri = f"file:{quote(str(db_path.resolve()))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        out["db_error"] = str(exc)
        return out

    try:
        out["db_available"] = True
        tables = {
            str(r["name"])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        stats: dict[str, int] = {}
        for tbl, key in (
            ("documents", "documents"),
            ("learnings", "learnings"),
            ("episodic_events", "events"),
        ):
            if tbl in tables:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()
                stats[key] = int(row["n"]) if row else 0
        out["stats"] = stats

        if "candidate_packets" in tables:
            cp_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(candidate_packets)").fetchall()
            }
            ws_expr = "COALESCE(workspace_id, 'default')" if "workspace_id" in cp_cols else "'default'"
            rows = conn.execute(
                f"SELECT {ws_expr} AS ws, COUNT(*) AS n "
                "FROM candidate_packets WHERE status = 'proposed' GROUP BY ws"
            ).fetchall()
            pending_by_ws: dict[str, int] = {}
            total_pending = 0
            for r in rows:
                norm_ws = _normalize_cli_workspace(str(r["ws"] or "default")) or "default"
                cnt = int(r["n"] or 0)
                if workspace_filter and norm_ws != workspace_filter:
                    continue
                pending_by_ws[norm_ws] = pending_by_ws.get(norm_ws, 0) + cnt
                total_pending += cnt
            out["pending_candidates"] = total_pending
            out["pending_by_workspace"] = pending_by_ws

        if "learnings" in tables:
            l_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(learnings)").fetchall()
            }
            doc_cols = (
                {
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(documents)").fetchall()
                }
                if "documents" in tables
                else set()
            )
            ws_col = "COALESCE(l.workspace_id, 'default')" if "workspace_id" in l_cols else "'default'"
            priv_col = "l.privacy_level" if "privacy_level" in l_cols else "NULL"
            l_filter = today_digest._learning_active_and_unblocked_sql(
                "l", tables, doc_cols, l_cols
            )
            l_rows = conn.execute(
                f"""SELECT l.learning_id, l.agent_id, l.category, l.content, l.confidence,
                           l.created_at, {ws_col} AS ws, {priv_col} AS priv
                    FROM learnings l
                    WHERE l.created_at >= ? {l_filter}
                    ORDER BY l.created_at DESC
                    LIMIT 100""",
                (cutoff_epoch,),
            ).fetchall()
            recent_learnings: list[dict[str, object]] = []
            recent_scars: list[dict[str, object]] = []
            for r in l_rows:
                raw_content = str(r["content"] or "")
                if today_digest._is_wrapper_or_blocked(raw_content, r["priv"]):
                    continue
                norm_ws = _normalize_cli_workspace(str(r["ws"] or "default")) or "default"
                if workspace_filter and norm_ws != workspace_filter:
                    continue
                cat = _cli_term_safe(str(r["category"] or "general"))
                text = _strip_evidence_tags(raw_content)
                item = {
                    "learning_id": r["learning_id"],
                    "agent_id": _cli_term_safe(str(r["agent_id"] or "main")),
                    "category": cat,
                    "content": text,
                    "confidence": round(float(r["confidence"] or 0.0), 3),
                    "created_at": r["created_at"],
                    "workspace_id": norm_ws,
                }
                if cat.lower() in ("scar", "gotcha", "correction", "error", "incident"):
                    recent_scars.append(
                        {
                            "workspace_id": norm_ws,
                            "learning_id": r["learning_id"],
                            "agent_id": item["agent_id"],
                            "category": cat,
                            "summary": text,
                            "created_at": r["created_at"],
                        }
                    )
                recent_learnings.append(item)
            out["recent_learnings"] = recent_learnings
            out["recent_scars"] = recent_scars

        if "episodic_events" in tables:
            ep_cols = {
                str(col["name"])
                for col in conn.execute("PRAGMA table_info(episodic_events)").fetchall()
            }
            ts_col = "created_at" if "created_at" in ep_cols else ("timestamp" if "timestamp" in ep_cols else None)
            e_rows = (
                conn.execute(
                    f"""SELECT event_id, agent_id, event_type, content, {ts_col} AS created_at, metadata
                       FROM episodic_events
                       WHERE {ts_col} >= ?
                         AND event_type IN ('scar', 'thread_scar', 'correction', 'error')
                       ORDER BY {ts_col} DESC
                       LIMIT 50""",
                    (cutoff_epoch,),
                ).fetchall()
                if ts_col
                else []
            )
            ep_scars = list(out["recent_scars"])  # type: ignore[arg-type]
            for r in e_rows:
                ev_ws = "default"
                raw_meta = r["metadata"]
                if isinstance(raw_meta, str) and raw_meta.strip():
                    try:
                        meta = json.loads(raw_meta)
                        if isinstance(meta, dict) and meta.get("workspace_id"):
                            ev_ws = (
                                _normalize_cli_workspace(str(meta["workspace_id"]))
                                or "default"
                            )
                    except ValueError:
                        pass
                if workspace_filter and ev_ws != workspace_filter:
                    continue
                ep_scars.append(
                    {
                        "workspace_id": ev_ws,
                        "event_id": r["event_id"],
                        "agent_id": _cli_term_safe(str(r["agent_id"] or "main")),
                        "category": str(r["event_type"]),
                        "summary": _strip_evidence_tags(str(r["content"] or "")),
                        "created_at": r["created_at"],
                    }
                )
            out["recent_scars"] = ep_scars
    except sqlite3.Error as exc:
        out["db_error"] = str(exc)
    finally:
        conn.close()
    return out


def cmd_activity(args: argparse.Namespace) -> int:
    hours = float(getattr(args, "hours", 24.0) or 24.0)
    if hours <= 0:
        print("minni activity: --hours must be positive", file=sys.stderr)
        return 2
    cutoff_epoch = time.time() - (hours * 3600.0)
    explicit_ws_arg = getattr(args, "workspace", None)
    workspace_filter = (
        _normalize_cli_workspace(explicit_ws_arg)
        if isinstance(explicit_ws_arg, str) and explicit_ws_arg.strip()
        else None
    )
    detected_ws = _detect_cli_workspace(explicit_ws_arg)

    minni_home = _minni_home_dir()
    db_override = (os.environ.get("MINNI_DB_PATH") or "").strip()
    db_path = Path(db_override).expanduser() if db_override else (minni_home / "minni.db")

    sock = Path(args.socket)
    daemon_block: dict[str, object]
    try:
        status_res = _rpc(sock, "status", timeout=5.0)
        d_info = status_res.get("daemon", {}) if isinstance(status_res, dict) else {}
        e_info = status_res.get("engine", {}) if isinstance(status_res, dict) else {}
        daemon_block = {
            "reachable": True,
            "mode": "rpc",
            "version": d_info.get("version", "?"),
            "uptime_seconds": d_info.get("uptime_seconds", 0),
            "requests_served": d_info.get("requests_served", 0),
            "db_ok": bool(e_info.get("db_ok", True)),
            "stats": e_info.get("stats", {}),
        }
    except RpcError as exc:
        daemon_block = {
            "reachable": False,
            "mode": "sqlite-readonly",
            "reason": str(exc),
        }

    sql_data = _read_sqlite_activity(
        db_path, cutoff_epoch=cutoff_epoch, workspace_filter=workspace_filter
    )
    if not daemon_block.get("reachable"):
        daemon_block["stats"] = sql_data.get("stats", {})
        daemon_block["db_available"] = sql_data.get("db_available", False)

    active_threads, thread_scars = _discover_active_threads(
        minni_home, cutoff_epoch=cutoff_epoch, workspace_filter=workspace_filter
    )

    recent_learnings: list[dict[str, object]] = list(
        sql_data.get("recent_learnings") or []  # type: ignore[arg-type]
    )
    recent_scars: list[dict[str, object]] = [
        *list(sql_data.get("recent_scars") or []),  # type: ignore[arg-type]
        *thread_scars,
    ]

    by_workspace: dict[str, dict[str, list[dict[str, object]]]] = {}
    for item in recent_learnings:
        ws_key = str(item.get("workspace_id") or "default")
        by_workspace.setdefault(ws_key, {"learnings": [], "scars": []})[
            "learnings"
        ].append(item)
    for scar in recent_scars:
        ws_key = str(scar.get("workspace_id") or "default")
        by_workspace.setdefault(ws_key, {"learnings": [], "scars": []})[
            "scars"
        ].append(scar)

    payload = {
        "window_hours": hours,
        "detected_workspace": detected_ws,
        "workspace_filter": workspace_filter,
        "daemon": daemon_block,
        "pending_candidates": sql_data.get("pending_candidates", 0),
        "pending_by_workspace": sql_data.get("pending_by_workspace", {}),
        "active_threads": active_threads,
        "by_workspace": by_workspace,
        "recent_learnings_count": len(recent_learnings),
        "recent_scars_count": len(recent_scars),
    }

    digest_path: Path | None = None
    if not getattr(args, "no_refresh", False):
        digest_path = today_digest.refresh_today_digest_safe(
            minni_home,
            daemon_running=bool(daemon_block.get("reachable")),
            db_path=db_path,
            bypass_cache=True,
        )

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0

    if daemon_block.get("reachable"):
        uptime = int(daemon_block.get("uptime_seconds") or 0)  # type: ignore[arg-type]
        print(
            f"Daemon: running (v{daemon_block.get('version', '?')}, "
            f"up {uptime // 3600}h {(uptime % 3600) // 60}m, "
            f"{daemon_block.get('requests_served', 0)} reqs)"
        )
    else:
        print("Daemon: stopped (degraded to read-only SQLite)")

    stats = daemon_block.get("stats") if isinstance(daemon_block.get("stats"), dict) else {}
    if stats:
        print(
            f"Store:  {stats.get('documents', 0)} docs, "
            f"{stats.get('learnings', 0)} learnings, "
            f"{stats.get('events', 0)} events"
        )
    print(
        f"Scope:  {workspace_filter or f'all workspaces (cwd={detected_ws})'} "
        f"| window={hours:g}h | pending candidates={payload['pending_candidates']}"
    )

    print(f"\nActive Threads ({len(active_threads)}):")
    if not active_threads:
        print("  (none)")
    else:
        for t in active_threads:
            title_part = f" — {t['title']}" if t.get("title") else ""
            print(
                f"  - [{t.get('workspace_id')}] {t.get('plan_id')} "
                f"({t.get('agent_id')}, {t.get('status')}){title_part}"
            )

    print(
        f"\nRecent Accepted Learnings & Scars (last {hours:g}h: "
        f"{len(recent_learnings)} learnings, {len(recent_scars)} scars):"
    )
    if not by_workspace:
        print("  (no recent accepted learnings or scars in window)")
    else:
        ordered_ws = sorted(
            by_workspace.keys(),
            key=lambda k: (0 if k == detected_ws else 1, k),
        )
        for ws_key in ordered_ws:
            bucket = by_workspace[ws_key]
            w_learns = bucket["learnings"]
            w_scars = bucket["scars"]
            print(
                f"  [{ws_key}] ({len(w_learns)} learnings, {len(w_scars)} scars)"
            )
            for l_item in w_learns[:10]:
                snippet = str(l_item.get("content") or "")
                if len(snippet) > 140:
                    snippet = snippet[:137] + "..."
                print(
                    f"    * learning#{l_item.get('learning_id')} "
                    f"[{l_item.get('category')}]: {snippet}"
                )
            for s_item in w_scars[:5]:
                s_text = str(s_item.get("summary") or "")
                if len(s_text) > 140:
                    s_text = s_text[:137] + "..."
                label = (
                    f"plan:{s_item['plan_id']}"
                    if s_item.get("plan_id")
                    else f"scar ({s_item.get('category', 'scar')})"
                )
                print(f"    ! {label}: {s_text}")
    if digest_path is not None:
        print(f"\nDigest updated: {digest_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="minni",
        description="Run and check the Minni memory daemon.")
    parser.add_argument("--socket", "-s", default=None,
                        help=f"Unix socket path (default: {DEFAULT_SOCKET})")
    sub = parser.add_subparsers(dest="command")

    up = sub.add_parser("up", help="start the daemon in the background")
    up.add_argument("--foreground", action="store_true",
                    help="run in this terminal instead (Ctrl-C stops it)")
    sub.add_parser("down", help="stop the daemon")
    sub.add_parser("status", help="daemon and engine health, plain language")
    sub.add_parser("doctor",
                   help="check local install health (does not run CI home-isolation checks)")

    recall_p = sub.add_parser(
        "recall",
        help="search project-scoped and global memories via the daemon")
    recall_p.add_argument("query", nargs="+", help="search query")
    recall_p.add_argument("--workspace", "-w",
                          help="workspace scope (default: auto-detected from git root)")
    recall_p.add_argument("--mode", choices=["fast", "hybrid"],
                          help="retrieval mode (fast lexical/trie or full hybrid)")
    recall_p.add_argument("--limit", "-n", type=int, default=5,
                          help="maximum results to return (default: %(default)s)")
    recall_p.add_argument("--agent", "-a",
                          help="explicit caller agent_id (default: omitted -> main)")
    recall_p.add_argument("--json", action="store_true",
                          help="print raw JSON response")

    learn_p = sub.add_parser(
        "learn",
        help="stage or durably record (--accept) a project/global lesson")
    learn_p.add_argument("content", nargs="+", help="lesson text to record")
    learn_p.add_argument("--title", "-t",
                         help="optional short title prepended to content")
    learn_p.add_argument("--category", "-c", default="general",
                         help="lesson category (default: %(default)s)")
    learn_p.add_argument("--workspace", "-w",
                         help="workspace id (default: auto-detected from git root)")
    learn_p.add_argument("--accept", action="store_true",
                         help="immediately accept a proposed candidate into durable learnings")
    learn_p.add_argument("--agent", "-a",
                         help="explicit caller agent_id (default: omitted -> main)")
    learn_p.add_argument("--json", action="store_true",
                         help="print raw JSON response")

    activity_p = sub.add_parser(
        "activity",
        help="read-only summary of daemon health, pending candidates, active threads, and recent lessons")
    activity_p.add_argument("--hours", type=float, default=24.0,
                            help="lookback window in hours (default: %(default)s)")
    activity_p.add_argument("--workspace", "-w",
                            help="filter output to a specific workspace")
    activity_p.add_argument("--no-refresh", action="store_true",
                            help="do not refresh TODAY.md")
    activity_p.add_argument("--json", action="store_true",
                            help="emit structured JSON")

    map_p = sub.add_parser(
        "map",
        help="synthesize and index Living Repo Map subsystem cards for a repository")
    map_p.add_argument("path", nargs="?",
                       help="repository path (default: git root of current directory)")
    map_p.add_argument("--workspace", "-w",
                       help="workspace id override (default: workspace-<repo-name>)")
    map_p.add_argument("--refresh", action="store_true",
                       help="force rebuild even when HEAD SHA matches cached map")
    map_p.add_argument("--agent", "-a",
                       help="explicit caller agent_id (default: omitted -> main)")
    map_p.add_argument("--json", action="store_true",
                       help="print raw JSON response")

    wire = sub.add_parser("wire", help="wire plugin payload to an agent platform")
    wire.add_argument("platform", help="codex, claude-code, kilocode, grok, gemini, "
                      "antigravity, generic, or all")
    wire.add_argument("--agent", help="agent id (required for generic)")
    workspace_mode = wire.add_mutually_exclusive_group()
    workspace_mode.add_argument("--workspace", help="workspace path for MINNI_WORKSPACE_ID")
    workspace_mode.add_argument("--dynamic-workspace", action="store_true",
                                help="Codex only: remove existing workspace pins so runtime context chooses the workspace")
    wire.add_argument("--install-root", help="override install/config root (required for generic)")
    wire.add_argument("--dry-run", action="store_true",
                      help="show actions without writing")
    wire.add_argument("--verify-payload", action="store_true",
                      help="verify payload file hashes")
    wire.add_argument("--prune", action="store_true",
                      help="prune old version dirs without prompting")
    wire.add_argument("--no-prune", action="store_true",
                      help="skip GC entirely")
    wire.add_argument("--force-reinstall", action="store_true",
                      help="quarantine hash-mismatched version dir and reinstall")
    wire.add_argument("--from-repo", metavar="PATH",
                      help="build payload from repo checkout (dev escape hatch)")
    wire.add_argument("--use-version", metavar="VER",
                      help="re-wire configs against an already-installed version dir")

    adopt = sub.add_parser(
        "wire-adopt",
        help="one-time cutover of a platform's plugin surface onto the wire tree")
    adopt.add_argument("platform", help="claude-code (the only platform needing adoption)")
    adopt.add_argument("--apply", action="store_true",
                       help="perform the cutover (default: dry run, writes nothing)")
    adopt.add_argument("--keep-legacy-cache", action="store_true",
                       help="leave ~/.claude/plugins/cache/minni in place")

    sync = sub.add_parser(
        "sync",
        help="redeploy the Minni plugin to every wired agent host "
             "(keep Claude/Codex/Grok/Kilo/Cursor current after upgrade or git pull)")
    sync.add_argument(
        "--full", action="store_true",
        help="editable checkout only: fast-forward main + rebuild + fleet "
             "redeploy (same as make sync-root / update_root.sh)")
    sync.add_argument(
        "--dry-run", action="store_true",
        help="show the plan without writing configs or reinstalling")
    sync.add_argument(
        "--wire-only", action="store_true",
        help="skip propagate-managed hosts (cursor/antigravity)")
    sync.add_argument(
        "--no-force", action="store_true",
        help="do not pass --force-reinstall to wire (default: force, so "
             "same-version rebuilds still land)")
    sync.add_argument(
        "--no-prune", action="store_true",
        help="compatibility flag: sync always retains old payloads while native/custom references may remain")
    sync.add_argument(
        "--no-restart", action="store_true",
        help="do not kickstart minnid after redeploy")
    sync.add_argument(
        "--install-auto", action="store_true",
        help="macOS: install launchd timer that runs full checkout sync "
             "every 6h (opt-in; refuses dirty trees)")
    sync.add_argument(
        "--uninstall-auto", action="store_true",
        help="macOS: remove the com.minni.sync-root launchd agent")
    sync.add_argument(
        "--auto-status", action="store_true",
        help="macOS: print whether com.minni.sync-root is loaded")

    watch = sub.add_parser(
        "watch",
        help="live tail of memory activity (audit trail + daemon events)")
    watch.add_argument("--agent", help="only show events for this agent id")
    watch.add_argument("--since", metavar="WHEN",
                       help="only show events after WHEN (e.g. 10m, 2h, or "
                       "an ISO timestamp)")
    watch.add_argument("--json", action="store_true",
                       help="emit one JSON object per event")
    watch.add_argument("--once", action="store_true",
                       help="print the current backlog and exit (no follow)")
    watch.add_argument("--interval", type=float, default=1.0,
                       help="poll interval in seconds (default: %(default)s)")

    args = parser.parse_args(argv)
    if args.socket is None:
        args.socket = str(_default_socket_path())
    if not args.command:
        parser.print_help()
        return 0

    if args.command == "wire":
        if getattr(args, "from_repo", None) and getattr(args, "use_version", None):
            print("minni wire: --from-repo and --use-version are mutually exclusive",
                  file=sys.stderr)
            return 2
        if getattr(args, "prune", False) and getattr(args, "no_prune", False):
            print("minni wire: --prune and --no-prune are mutually exclusive",
                  file=sys.stderr)
            return 2

    dispatch = {"up": cmd_up, "down": cmd_down,
                "status": cmd_status, "doctor": cmd_doctor,
                "recall": cmd_recall, "learn": cmd_learn, "activity": cmd_activity,
                "map": cmd_map,
                "wire": cmd_wire, "wire-adopt": cmd_wire_adopt,
                "sync": cmd_sync, "watch": cmd_watch}
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

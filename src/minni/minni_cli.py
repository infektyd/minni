#!/usr/bin/env python3
"""minni — daemon lifecycle and health CLI.

The newcomer-facing entry point: drive the minnid daemon without knowing what a
Unix socket is.

    minni up        start the daemon in the background
    minni status    show daemon and engine health in plain language
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
import stat
import subprocess
import sys
import time
from pathlib import Path

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
        raise RpcError("connection refused — daemon not listening") from exc
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
        print(f"minnid is not reachable: {exc}\nStart it with: minni up",
              file=sys.stderr)
        return 1
    daemon = result.get("daemon", {})
    engine = result.get("engine", {})
    stats = engine.get("stats", {})
    uptime = int(daemon.get("uptime_seconds", 0))
    print(f"minnid {daemon.get('version', '?')} — running "
          f"(up {uptime // 3600}h {uptime % 3600 // 60}m, "
          f"{daemon.get('requests_served', 0)} requests served)")
    print(f"  database: {'ok' if engine.get('db_ok') else 'NOT OK'} — "
          f"{stats.get('documents', 0)} documents, "
          f"{stats.get('learnings', 0)} learnings, "
          f"{stats.get('events', 0)} events")
    print(f"  vector index: {'ok' if engine.get('faiss_ok') else 'NOT OK'}")
    print(_format_latency_block(daemon.get("latencies", {})))
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
    samples (e.g. cross_encoder, never recorded on any path) reads 'no
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

    # 6. Fleet freshness (WARN): plugin/host lag is the #1 "I merged but agents
    # still run old hooks" footgun. Never fail doctor on it — customers may be
    # mid-session — but always name the command that fixes it.
    try:
        from minni.minnid_runtime.deploy_honesty import deploy_status
        deploy = deploy_status()
        plugin = (deploy or {}).get("plugin_dist") or {}
        if deploy.get("stale") is True or plugin.get("stale") is True:
            reason = deploy.get("reason") or plugin.get("reason") or "deploy lag"
            print(f"  [WARN] fleet: install is stale relative to this package/checkout")
            print(f"         {reason}")
            print("         Fix: minni sync          # redeploy plugin to all wired hosts")
            print("              minni sync --full   # editable checkout: also git pull + rebuild")
        else:
            print("  [PASS] fleet: plugin/daemon not reporting deploy lag")
    except Exception as exc:  # pragma: no cover — optional surface
        print(f"  [WARN] fleet: could not read deploy status ({exc})")

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="minni",
        description="Run and check the Minni memory daemon.")
    parser.add_argument("--socket", "-s", default=str(DEFAULT_SOCKET),
                        help="Unix socket path (default: %(default)s)")
    sub = parser.add_subparsers(dest="command")

    up = sub.add_parser("up", help="start the daemon in the background")
    up.add_argument("--foreground", action="store_true",
                    help="run in this terminal instead (Ctrl-C stops it)")
    sub.add_parser("down", help="stop the daemon")
    sub.add_parser("status", help="daemon and engine health, plain language")
    sub.add_parser("doctor",
                   help="check local install health (does not run CI home-isolation checks)")

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
                "status": cmd_status, "doctor": cmd_doctor, "wire": cmd_wire,
                "wire-adopt": cmd_wire_adopt, "sync": cmd_sync, "watch": cmd_watch}
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""minni — daemon lifecycle and health CLI.

The newcomer-facing entry point: drive the minnid daemon without knowing what a
Unix socket is.

    minni up        start the daemon in the background
    minni status    show daemon and engine health in plain language
    minni doctor    verify the install end to end (same probes as CI's smoke)
    minni wire      wire the plugin payload to an agent platform
    minni wire-adopt cut a platform's plugin surface over to the wire tree
    minni sync      redeploy the plugin to every wired agent (keep hosts current)
    minni watch     live tail of memory activity (audit trail + daemon events)
    minni down      stop the daemon

Packaging-only surface (PACKAGING_PLAN.md §3): this module is stdlib-only and
never imports engine internals, so it cannot change how memory is stored,
recalled, scored, or governed. `doctor` mirrors the exact assertions of
scripts/repro-smoke.sh (the CI oracle): status returns `daemon` + `engine`,
search round-trips with a `results` key.
"""

from __future__ import annotations

import argparse
import json
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


class RpcError(Exception):
    """A JSON-RPC round-trip failed (transport or daemon-reported error)."""


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
        raise RpcError(f"request timed out after {timeout:.0f}s") from exc
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
    return 0


def _check(label: str, ok: bool, detail: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    return ok


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
    try:
        status = _rpc(sock, "status")
        status_ok = "daemon" in status and "engine" in status
        detail = ("daemon answered with daemon+engine health"
                  if status_ok else f"unexpected shape: {sorted(status)}")
    except RpcError as exc:
        status_ok, detail = False, str(exc)
    failures += not _check("daemon status", status_ok, detail)

    try:
        found = _rpc(sock, "search",
                     {"query": "smoke test recall", "limit": 1})
        recall_ok = isinstance(found, dict) and "results" in found
        detail = ("a recall round-trips through retrieval"
                  if recall_ok else f"unexpected shape: {type(found).__name__}")
    except RpcError as exc:
        recall_ok, detail = False, str(exc)
    failures += not _check("recall round-trip", recall_ok, detail)

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
                   help="verify the install (same probes as CI's smoke test)")

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
        help="keep old ~/.minni/plugin version dirs")
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

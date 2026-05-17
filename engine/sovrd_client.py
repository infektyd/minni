#!/usr/bin/env python3
"""sovrd-client — Lightweight CLI client for the Sovereign Memory daemon.

Usage:
    python sovrd-client.py status
    python sovrd-client.py search "websockets"
    python sovrd-client.py read --agent hermes
    python sovrd-client.py learn "User prefers dark mode" --category preference
    python sovrd-client.py log session_start "Hermes started"
    python sovrd-client.py ping
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

DEFAULT_SOCKET = "/tmp/sovrd.sock"


def _rpc(socket_path: str, method: str, params: dict = None) -> dict:
    """Send a JSON-RPC request over a Unix domain socket."""
    if not os.path.exists(socket_path):
        print(f"Error: socksd not running — socket {socket_path} not found.\n"
              "   Start with: python sovrd.py", file=sys.stderr)
        sys.exit(1)

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }
    payload = json.dumps(request) + "\n"

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(socket_path)
        s.settimeout(30)
        s.sendall(payload.encode())

        # Read response (may span multiple recv calls)
        chunks = []
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
            # If we got a complete line, we're done for single-request
            if b"\n" in b"".join(chunks):
                break

        data = b"".join(chunks)
        s.close()

        if not data:
            print("Error: empty response from daemon.", file=sys.stderr)
            sys.exit(1)

        resp = json.loads(data.decode("utf-8"))

        if "error" in resp:
            err = resp["error"]
            print(f"Error ({err.get('code', '?')}): {err.get('message', '')}",
                  file=sys.stderr)
            sys.exit(1)

        return resp.get("result", {})

    except ConnectionRefusedError:
        print("Error: connection refused — is sovrd running?", file=sys.stderr)
        sys.exit(1)
    except socket.timeout:
        print("Error: request timed out.", file=sys.stderr)
        sys.exit(1)


def search(
    query: str,
    agent_id: str = None,
    limit: int = 5,
    depth: str = "snippet",
    socket_path: str = DEFAULT_SOCKET,
) -> list:
    """Programmatic search helper used by verification scripts."""
    params = {"query": query, "limit": limit, "depth": depth}
    if agent_id:
        params["agent_id"] = agent_id
    result = _rpc(socket_path, "search", params)
    trace_id = result.get("trace_id")
    results = result.get("results", [])
    if trace_id:
        for item in results:
            item.setdefault("trace_id", trace_id)
    return results


def feedback(
    query: str,
    result_id: int,
    useful: bool,
    agent_id: str = "main",
    socket_path: str = DEFAULT_SOCKET,
) -> dict:
    """Programmatic feedback helper."""
    return _rpc(socket_path, "feedback", {
        "query": query,
        "result_id": result_id,
        "useful": useful,
        "agent_id": agent_id,
    })


def trace(trace_id: str, socket_path: str = DEFAULT_SOCKET) -> dict:
    """Programmatic trace lookup helper."""
    result = _rpc(socket_path, "trace", {"trace_id": trace_id})
    return result.get("trace") or result


def _cmd_status(args):
    result = _rpc(args.socket, "status")
    daemon = result.get("daemon", {})
    engine = result.get("engine", {})
    stats = engine.get("stats", {})

    print("sovrd Daemon Status")
    print("─" * 40)
    print(f"  Version:      {daemon.get('version', '?')}")
    print(f"  Uptime:       {daemon.get('uptime_seconds', 0):.0f}s")
    print(f"  Requests:     {daemon.get('requests_served', 0)}")
    print(f"  Socket:       {daemon.get('socket_path', '?')}")
    print(f"  Dual-write:   {daemon.get('dual_write', False)}")
    print()
    print("Engine Health")
    print("─" * 40)
    print(f"  DB OK:        {'yes' if engine.get('db_ok') else 'no'}")
    print(f"  Documents:    {stats.get('documents', 0)}")
    print(f"  Chunks:       {stats.get('chunks', 0)}")
    print(f"  Learnings:    {stats.get('learnings', 0)}")
    print(f"  Events:       {stats.get('events', 0)}")
    print(f"  FAISS OK:     {'yes' if engine.get('faiss_ok') else 'no'}")
    print()


def _cmd_search(args):
    params = {
        "query": " ".join(args.query),
        "limit": args.limit,
    }
    if args.agent:
        params["agent_id"] = args.agent

    result = _rpc(args.socket, "search", params)

    count = result.get("count", 0)
    print(f"Search: {params['query']}  ({count} results)")
    print("─" * 50)

    for r in result.get("results", []):
        heading = r.get("heading", "")
        source = r.get("source", "")
        header = source
        if heading:
            header += f" — {heading}"
        header += f"  (score={r['score']:.3f})"
        print(f"\n{header}")
        print(r.get("text", "")[:500])
    print()


def _cmd_read(args):
    params = {"limit": args.limit}
    if args.agent:
        params["agent_id"] = args.agent

    result = _rpc(args.socket, "read", params)
    print(result.get("context", "No context."))
    print()


def _cmd_learn(args):
    params = {
        "content": " ".join(args.text),
        "category": getattr(args, "category", "general") or "general",
    }
    if args.agent:
        params["agent_id"] = args.agent

    result = _rpc(args.socket, "learn", params)
    print(f"Stored learning #{result.get('learning_id', '?')}  "
          f"[{result.get('category', '?')}]")
    if result.get("dual_write"):
        print("  (also written to MEMORY.md)")
    print()


def _cmd_log(args):
    params = {
        "event_type": args.event,
        "content": " ".join(args.text),
    }
    if args.agent:
        params["agent_id"] = args.agent

    result = _rpc(args.socket, "log_event", params)
    print(f"Event #{result.get('event_id', '?')} logged  [{args.event}]")
    print()


def _cmd_ping(args):
    result = _rpc(args.socket, "ping")
    print(result)


def main():
    parser = argparse.ArgumentParser(description="sovrd CLI client")
    parser.add_argument("--socket", "-s", default=DEFAULT_SOCKET,
                        help="Unix socket path")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Daemon and engine status")

    sp = sub.add_parser("search", aliases=["recall"], help="Search memory")
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--agent", "-a")

    sp = sub.add_parser("read", help="Agent startup context")
    sp.add_argument("--agent", "-a", default="hermes")
    sp.add_argument("--limit", type=int, default=5)

    sp = sub.add_parser("learn", help="Store a learning")
    sp.add_argument("text", nargs="+")
    sp.add_argument("--category", "-c", default="general")
    sp.add_argument("--agent", "-a")

    sp = sub.add_parser("log", help="Log an episodic event")
    sp.add_argument("event", choices=[
        "session_start", "session_end", "task_completed",
        "decision", "correction", "error",
    ])
    sp.add_argument("text", nargs="+")
    sp.add_argument("--agent", "-a")

    sub.add_parser("ping", help="Liveness check")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    dispatch = {
        "status": _cmd_status,
        "search": _cmd_search,
        "recall": _cmd_search,
        "read": _cmd_read,
        "learn": _cmd_learn,
        "log": _cmd_log,
        "ping": _cmd_ping,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()

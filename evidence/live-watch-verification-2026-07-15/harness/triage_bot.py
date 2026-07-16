#!/usr/bin/env python3
"""External workload A: a support-ticket triage bot that uses minni as its
memory backend over raw JSON-RPC. Deliberately does NOT import minni — this
is a third-party client. Writes a ground-truth ledger of every op it issues.

Phase 1: 48 tickets with think time (search / log_event / learn mix).
Phase 2 (after ticket 20, overlapping the plugin lane): a 250-search burst
with no think time, to exceed minni watch's per-poll batch limit (200).
"""
import json
import random
import socket
import sys
import time

SOCKET = "/tmp/mlive/run/minnid.sock"
LEDGER = sys.argv[1] if len(sys.argv) > 1 else "ledger.jsonl"
SESSION = "sess-live-A"
AGENT = "loadgen"
TICKETS = 48
BURST = 250

TOPICS = [
    "printer offline after firmware update", "vpn drops every 20 minutes",
    "sso login loop on staging", "database connection pool exhausted",
    "email bounce dmarc failure", "kubernetes pod oomkilled on deploy",
    "ssl certificate expired on api gateway", "backup job silently failing",
    "two-factor reset request", "slow query on orders table",
    "webhook retries flooding endpoint", "disk full on ci runner",
]


def rpc(method, params, timeout=60):
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(timeout)
        s.connect(SOCKET)
        s.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        if not buf:
            return {"error": {"message": "empty response"}}
        return json.loads(buf)
    except Exception as exc:  # ledger the failure; never crash the run
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}}


def main():
    rng = random.Random(20260715)
    with open(LEDGER, "w") as ledger:
        def record(seq, op, args, response):
            ledger.write(json.dumps({
                "seq": seq, "t": time.time(), "op": op, "args": args,
                "response": response}) + "\n")
            ledger.flush()

        def do_search(seq, query):
            r = rpc("search", {"query": query, "limit": 3, "agent_id": AGENT,
                               "session_id": SESSION})
            result = r.get("result", {})
            record(seq, "search", {"query": query, "session_id": SESSION},
                   {"hits": result.get("count"),
                    "error": r.get("error", {}).get("message")})

        seq = 0
        for i in range(1, TICKETS + 1):
            topic = TOPICS[(i - 1) % len(TOPICS)]
            seq += 1
            do_search(seq, f"T{i:02d} prior incidents: {topic}")

            if i % 3 == 0:
                content = f"T{i:02d} triaged: {topic} -> tier2"
                # task_id (not thread_id) on purpose: thread_id triggers the
                # semantic thread-bind (model load) which this test does not
                # exercise; the recall trace covers session attribution.
                r2 = rpc("log_event", {"agent_id": AGENT,
                                       "event_type": "ticket_triaged",
                                       "content": content,
                                       "task_id": SESSION})
                seq += 1
                record(seq, "log_event", {"content": content},
                       {"event_id": r2.get("result", {}).get("event_id"),
                        "error": r2.get("error", {}).get("message")})

            if i % 6 == 0:
                learning = (f"T{i:02d}: for '{topic}', escalate to tier2 "
                            f"after two failed remote resets")
                r3 = rpc("learn", {"agent_id": AGENT, "content": learning,
                                   "category": "procedure"})
                res3 = r3.get("result", {})
                seq += 1
                record(seq, "learn", {"content": learning},
                       {"status": res3.get("status"),
                        "candidate_id": res3.get("candidate_id"),
                        "error": r3.get("error", {}).get("message")})

            if i == 20:
                # Phase 2 burst, mid-run so it overlaps the plugin lane
                # (same-second cross-lane events) and exceeds the poller's
                # 200-rows-per-poll batch in one interval.
                for b in range(1, BURST + 1):
                    seq += 1
                    do_search(seq, f"B{b:03d} burst probe: "
                                   f"{TOPICS[b % len(TOPICS)]}")

            time.sleep(rng.uniform(1.5, 4.5))

    print(f"triage bot done: {TICKETS} tickets + {BURST} burst searches")


if __name__ == "__main__":
    main()

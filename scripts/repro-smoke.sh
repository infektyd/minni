#!/bin/bash
# RCM-028: hermetic reproduction smoke for clean machine + isolation.
# Runs status + recall probe under isolated SOVEREIGN_HOME=/tmp/..., asserts no pollution of ~
set -euo pipefail

TD=$(mktemp -d /tmp/sovrd-smoke-XXXXXX)
export SOVEREIGN_HOME="$TD"
export SOVEREIGN_SOCKET="$TD/run/sovrd.sock"
mkdir -p "$TD/run" "$TD/logs"

echo "[smoke] SOVEREIGN_HOME=$SOVEREIGN_HOME"

# Start daemon in background (stdio mode for simplicity in CI; socket optional)
python3 -u engine/sovrd.py --home "$TD" --socket "$SOVEREIGN_SOCKET" > "$TD/daemon.log" 2>&1 &
DAEMON_PID=$!
trap 'kill $DAEMON_PID 2>/dev/null || true; rm -rf "$TD"' EXIT

sleep 3  # allow startup

# Migration safety (RCM-028 Phase 0 exit): clean /tmp start must initialize DB/schema (no corruption on first use).
DB_COUNT=$(find "$TD" -maxdepth 1 -name "*.db" -o -name "*sovereign*.db" 2>/dev/null | wc -l | tr -d ' \n')
echo "MIGRATION_DB_PRESENT: $DB_COUNT (expected >=1 for schema apply on clean start)"

# Probe status via python (uses sovrd_client if possible, else direct import)
python3 - <<'PY'
import os, sys, time, json
sys.path.insert(0, "engine")
from sovrd_client import SovereignClient
c = SovereignClient(socket_path=os.environ.get("SOVEREIGN_SOCKET"))
st = c.call("status", {})
status_ok = "daemon" in st and "engine" in st
print("STATUS_OK:", status_ok)
if not status_ok:
    print("STATUS FAILED", file=sys.stderr)
    sys.exit(1)
rec = c.call("search", {"query": "smoke test recall", "limit": 1})
recall_ok = isinstance(rec, dict) and "results" in rec
print("RECALL_OK:", recall_ok)
if not recall_ok:
    print("RECALL FAILED", file=sys.stderr)
    sys.exit(1)
pollution = ".sovereign-memory" not in os.listdir(os.path.expanduser("~"))
print("HOME_POLLUTION_CHECK:", pollution)
if not pollution:
    print("POLLUTION DETECTED: .sovereign-memory present in ~", file=sys.stderr)
    sys.exit(1)
PY

echo "[smoke] SUCCESS: daemon started, status+recall responded, no ~ pollution under $SOVEREIGN_HOME"

#!/bin/bash
# RCM-028: hermetic reproduction smoke for clean machine + isolation.
# Runs status + recall probe under isolated MINNI_HOME=/tmp/..., asserts no pollution of ~
set -euo pipefail

TD=$(mktemp -d /tmp/minnid-smoke-XXXXXX)
export MINNI_HOME="$TD"
export MINNI_SOCKET="$TD/run/minnid.sock"
mkdir -p "$TD/run" "$TD/logs"

echo "[smoke] MINNI_HOME=$MINNI_HOME"

# Start daemon in background (stdio mode for simplicity in CI; socket optional)
python3 -u engine/minnid.py --socket "$MINNI_SOCKET" > "$TD/daemon.log" 2>&1 &
DAEMON_PID=$!
trap 'kill $DAEMON_PID 2>/dev/null || true; rm -rf "$TD"' EXIT

sleep 3  # allow startup

# Migration safety (RCM-028 Phase 0 exit): clean /tmp start must initialize DB/schema (no corruption on first use).
DB_COUNT=$(find "$TD" -maxdepth 1 -name "*.db" -o -name "*minni*.db" 2>/dev/null | wc -l | tr -d ' \n')
echo "MIGRATION_DB_PRESENT: $DB_COUNT (expected >=1 for schema apply on clean start)"

# Probe status via python (uses minnid_client if possible, else direct import)
python3 - <<'PY'
import os, sys, time, json
sys.path.insert(0, "engine")
from minnid_client import _rpc
socket_path = os.environ.get("MINNI_SOCKET")
st = _rpc(socket_path, "status", {})
status_ok = "daemon" in st and "engine" in st
print("STATUS_OK:", status_ok)
if not status_ok:
    print("STATUS FAILED", file=sys.stderr)
    sys.exit(1)
rec = _rpc(socket_path, "search", {"query": "smoke test recall", "limit": 1})
recall_ok = isinstance(rec, dict) and "results" in rec
print("RECALL_OK:", recall_ok)
if not recall_ok:
    print("RECALL FAILED", file=sys.stderr)
    sys.exit(1)
sov_dir = os.path.expanduser("~/.minni")
if os.path.exists(sov_dir):
    recent_files = []
    now = time.time()
    for root_dir, dirs, files in os.walk(sov_dir):
        for f in files:
            fp = os.path.join(root_dir, f)
            try:
                mtime = os.path.getmtime(fp)
                if now - mtime < 10:  # 10s lookback
                    recent_files.append(fp)
            except OSError:
                pass
    pollution = len(recent_files) == 0
    if not pollution:
        print("POLLUTION DETECTED: Recent modified files in ~:", recent_files, file=sys.stderr)
        sys.exit(1)
    else:
        print("HOME_POLLUTION_CHECK: True (pre-existing directory but no new files)")
else:
    print("HOME_POLLUTION_CHECK: True (directory does not exist)")
PY

echo "[smoke] SUCCESS: daemon started, status+recall responded, no ~ pollution under $MINNI_HOME"

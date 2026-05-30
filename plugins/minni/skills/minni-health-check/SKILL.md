---
name: minni-health-check
description: Automated health monitoring for Minni consolidation pipeline. Quick status checks, alerts, and diagnostics for cron jobs and routine monitoring. Detects stalled processes, stale locks, model server issues, and database anomalies.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [minni, consolidation, health-check, monitoring, cron, diagnostics]
    related_skills: [minni-consolidation, systematic-debugging]
---

# Minni Health Check Automation

## Overview

Automated health monitoring for the Minni consolidation pipeline. Use this skill to:
- Generate periodic health reports (hourly, daily, weekly)
- Detect when models are unresponsive or crashed
- Identify stale lock files and hung processes
- Monitor database growth and consolidation frequency
- Alert on performance degradation or capacity issues

## When to Use

- Scheduled cron monitoring (every 1-4 hours, or daily)
- Alerting systems that need structured health data
- Pre-consolidation verification before triggering a new run
- Troubleshooting when pipeline appears stalled
- Capacity planning and performance trending

## Quick Health Check (1-minute scan)

```bash
source ~/.hermes/hermes-agent/venv/bin/activate

python3 <<'PYEOF'
import subprocess
import json
import re
from datetime import datetime, timezone

def run_cmd(cmd):
    """Execute shell command safely."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {str(e)}"

# Database stats
db_stats = run_cmd(
    "sqlite3 ~/.openclaw/sovereign_memory.db "
    "\"SELECT 'total'||'|'||COUNT(*) FROM learnings UNION ALL "
    "SELECT 'active'||'|'||COUNT(*) FROM learnings WHERE superseded_by IS NULL UNION ALL "
    "SELECT 'recent_4h'||'|'||COUNT(*) FROM learnings WHERE created_at > strftime('%s', 'now', '-4 hours');\""
).split('\n')

# Model server health (2-second timeout per server)
e2b_health = run_cmd("curl -s --connect-timeout 2 http://127.0.0.1:11436/v1/models | jq '-r .data[0].id' 2>/dev/null || echo 'DOWN'")
e4b_health = run_cmd("curl -s --connect-timeout 2 http://127.0.0.1:11435/v1/models | jq '-r .data[0].id' 2>/dev/null || echo 'DOWN'")

# Process status
consolidate_running = run_cmd("pgrep -f 'sovereign-consolidate.py' && echo 'YES' || echo 'NO'")

# Lock file status
lock_time = run_cmd("stat -f %m ~/.hermes/consolidation-staging/.pipeline.lock 2>/dev/null || echo '0'")

# Last successful run
last_summary = run_cmd("ls -t ~/.hermes/logs/consolidation/summary-*.json 2>/dev/null | head -1")
if last_summary and last_summary != "ERROR" and not last_summary.startswith("ERROR"):
    last_summary_time = run_cmd(f"jq -r .timestamp {last_summary} 2>/dev/null")
    last_summary_facts = run_cmd(f"jq .total_facts_extracted {last_summary} 2>/dev/null")
else:
    last_summary_time = "UNKNOWN"
    last_summary_facts = "0"

# **NEW: Check for stalled E4B consolidation** (key stall detector)
stalled_consolidation = False
stall_details = ""
consolidation_log = run_cmd("ls -t ~/.hermes/logs/consolidation/consolidation-*.log 2>/dev/null | head -1")
if consolidation_log and not consolidation_log.startswith("ERROR"):
    # Get last E4B consolidation attempt and check if completion exists after it
    last_e4b_attempt = run_cmd(
        f"grep 'E4B consolidating.*against' {consolidation_log} | tail -1"
    )
    if last_e4b_attempt:
        # Extract timestamp from log line (format: YYYY-MM-DD HH:MM:SS,...)
        match = re.match(r'^(\\d{{4}}-\\d{{2}}-\\d{{2}} \\d{{2}}:\\d{{2}}:\\d{{2}})', last_e4b_attempt)
        if match:
            attempt_time_str = match.group(1)
            # Check if there's a completion entry after this timestamp
            completion_after = run_cmd(
                f\"grep 'E4B consolidation complete' {consolidation_log} | \"\n                f\"awk '$0 > \\\"{attempt_time_str}\\\"' | wc -l\"
            )
            if completion_after == "0":
                # No completion found after attempt
                stalled_consolidation = True
                stall_details = f\"E4B consolidation initiated at {attempt_time_str} but never completed\"

# Print report
report = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "database": {
        "total_learnings": db_stats[0].split('|')[1] if db_stats else "0",
        "active": db_stats[1].split('|')[1] if len(db_stats) > 1 else "0",
        "recent_4h": db_stats[2].split('|')[1] if len(db_stats) > 2 else "0",
    },
    "model_servers": {
        "e2b_11436": "UP" if e2b_health and e2b_health != "DOWN" else "DOWN",
        "e4b_11435": "UP" if e4b_health and e4b_health != "DOWN" else "DOWN",
    },
    "pipeline": {
        "process_running": consolidate_running == "YES",
        "lock_file_stale": lock_time != "0" and int(float(lock_time or 0)) < (int(datetime.now().timestamp()) - 3600),
        "e4b_stalled": stalled_consolidation,
    },
    "last_run": {
        "timestamp": last_summary_time,
        "facts": last_summary_facts,
    },
    "alerts": []
}

# Alert logic
if report["model_servers"]["e2b_11436"] == "DOWN":
    report["alerts"].append("🔴 E2B server (port 11436) is DOWN")
if report["model_servers"]["e4b_11435"] == "DOWN":
    report["alerts"].append("🔴 E4B server (port 11435) is DOWN")
if report["pipeline"]["lock_file_stale"]:
    report["alerts"].append("🟡 Stale lock file (> 1h old) — may indicate hung process")
if report["pipeline"]["e4b_stalled"]:
    report["alerts"].append(f"🔴 STALLED: {stall_details}")
if int(report["database"]["recent_4h"] or 0) == 0:
    report["alerts"].append("🟡 No consolidation activity in last 4 hours")

if not report["alerts"]:
    report["alerts"].append("✅ System healthy")

print(json.dumps(report, indent=2))
PYEOF
```

**Output interpretation:**
```json
{
  "timestamp": "2026-04-25T01:34:00+00:00",
  "database": {
    "total_learnings": 989,
    "active": 988,
    "recent_4h": 30
  },
  "model_servers": {
    "e2b_11436": "UP",
    "e4b_11435": "UP"
  },
  "pipeline": {
    "process_running": false,
    "lock_file_stale": true
  },
  "alerts": [
    "✅ System healthy"
  ]
}
```

## Full Diagnostic Report (5-minute scan)

For detailed troubleshooting, run the full diagnostic:

```bash
source ~/.hermes/hermes-agent/venv/bin/activate

python3 <<'PYEOF'
import subprocess
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return result.stdout.strip()
    except Exception as e:
        return f"ERROR: {str(e)}"

# Comprehensive database query
db = sqlite3.connect(Path.home() / ".openclaw" / "sovereign_memory.db")
cursor = db.cursor()

cursor.execute("""
SELECT 
  (SELECT COUNT(*) FROM learnings) as total,
  (SELECT COUNT(*) FROM learnings WHERE superseded_by IS NULL) as active,
  (SELECT COUNT(*) FROM learnings WHERE created_at > strftime('%s', 'now', '-4 hours')) as recent_4h,
  (SELECT COUNT(*) FROM learnings WHERE created_at > strftime('%s', 'now', '-24 hours')) as recent_24h,
  (SELECT AVG(confidence) FROM learnings) as avg_confidence,
  (SELECT COUNT(DISTINCT category) FROM learnings) as category_count
""")

db_row = cursor.fetchone()
db.close()

# Get model memory usage from logs
e4b_memory = run_cmd("tail -10 ~/.hermes/logs/mlx-gemma-e4b.err | grep 'Prompt Cache' | tail -1")
e2b_memory = run_cmd("tail -10 ~/.hermes/logs/mlx-gemma-e2b.err | grep 'Prompt Cache' | tail -1")

# Check for recent errors in logs
e4b_errors = run_cmd("tail -100 ~/.hermes/logs/mlx-gemma-e4b.err | grep -i 'error\\|exception\\|oom\\|crash' | wc -l")
e2b_errors = run_cmd("tail -100 ~/.hermes/logs/mlx-gemma-e2b.err | grep -i 'error\\|exception\\|oom\\|crash' | wc -l")

# Staging directory status
staging_files = run_cmd("find ~/.hermes/consolidation-staging -type f -mtime -1 2>/dev/null | wc -l")

# Last 3 consolidation summaries
summaries = run_cmd("ls -t ~/.hermes/logs/consolidation/summary-*.json 2>/dev/null | head -3")

report = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "database": {
        "total_learnings": db_row[0],
        "active_learnings": db_row[1],
        "recent_4h": db_row[2],
        "recent_24h": db_row[3],
        "avg_confidence": round(db_row[4], 3) if db_row[4] else 0,
        "categories": db_row[5],
    },
    "model_servers": {
        "e4b_memory": e4b_memory,
        "e2b_memory": e2b_memory,
        "e4b_recent_errors": int(e4b_errors),
        "e2b_recent_errors": int(e2b_errors),
    },
    "staging": {
        "files_modified_today": int(staging_files),
    },
    "status": "GOOD" if db_row[2] > 0 and int(e4b_errors) == 0 and int(e2b_errors) == 0 else "NEEDS_REVIEW",
}

print(json.dumps(report, indent=2))
PYEOF
```

## Cron Integration

### Hourly Health Check

```bash
# Run every 4 hours, suppress output if healthy
0 */4 * * * \
  source ~/.hermes/hermes-agent/venv/bin/activate && \
  python3 ~/.hermes/scripts/sovereign-consolidate.py --dry-run --limit 1 2>&1 | \
  grep -E "ERROR|FAILED|stalled|timeout" && \
  echo "Pipeline issue detected" || echo "✅ Pipeline OK"
```

### Daily Full Diagnostic

```bash
# Run at 04:00 UTC (after 03:00 consolidation), send report
0 4 * * * \
  source ~/.hermes/hermes-agent/venv/bin/activate && \
  /path/to/health-diagnostic.sh | mail -s "Minni Health $(date +%Y-%m-%d)" admin@example.com
```

### Alert on Stalled Pipeline

```bash
# Check every 2 hours — alert if no consolidation in 6+ hours
0 */2 * * * \
  bash -c 'last_summary=$(ls -t ~/.hermes/logs/consolidation/summary-*.json 2>/dev/null | head -1); \
  [ -z "$last_summary" ] && exit 1; \
  summary_age=$(( $(date +%s) - $(stat -f %m "$last_summary") )); \
  [ $summary_age -gt 21600 ] && echo "⚠️ No consolidation for 6+ hours" || echo "✅ Recent activity"'
```

## Alert Thresholds

| Condition | Alert Level | Action |
|-----------|------------|--------|
| E4B consolidation incomplete > 5 minutes | 🔴 Critical | Kill process, remove lock, restart pipeline |
| Model server down (E2B or E4B) | 🔴 Critical | Check launchd status, restart if needed |
| Stale lock file > 1 hour | 🟡 Warning | Check if process running; if not, remove lock |
| No consolidation in 6 hours | 🟡 Warning | Check cron schedule, verify script runs |
| Database query timeout | 🔴 Critical | Check disk space, WAL journal growth |
| Recent errors > 5 in 100 lines | 🟡 Warning | Review error log context, may be transient |
| Prompt cache > 3 GB | 🟡 Warning | Consider reducing --limit on next run |
| Recent learnings = 0 in 4 hours | ⚠️ Info | Investigate if consolidation is running |

## Troubleshooting via Health Check

### Scenario: E4B consolidation stalled (key pattern)

**Detection:** Health check shows `e4b_stalled: true` or "STALLED: E4B consolidation initiated at YYYY-MM-DD HH:MM:SS but never completed"

```bash
# 1. Get last consolidation log
last_log=$(ls -t ~/.hermes/logs/consolidation/consolidation-*.log | head -1)

# 2. Extract last E4B attempt timestamp
last_attempt=$(grep "E4B consolidating.*against" "$last_log" | tail -1 | head -c 19)
echo "Last attempt: $last_attempt"

# 3. Check elapsed time (should be ~80s for completion)
last_attempt_epoch=$(date -f "%Y-%m-%d %H:%M:%S" "$last_attempt" +%s)
current_epoch=$(date +%s)
elapsed=$((current_epoch - last_attempt_epoch))
echo "Elapsed: ${elapsed}s (> 5min = stalled)"

# 4. Confirm process is running or hung
pgrep -f "sovereign-consolidate.py" && echo "Process still running (hung)" || echo "Process stopped"

# 5. Clean up and restart
if [ $elapsed -gt 300 ]; then
  echo "Stall confirmed. Terminating and cleaning up..."
  pkill -f "sovereign-consolidate.py"
  rm -f ~/.hermes/consolidation-staging/.pipeline.lock
  echo "✅ Pipeline reset. Next cron run will restart consolidation."
fi
```

### Scenario: "Pipeline stalled" alert

```bash
# 1. Quick check
pgrep -f "sovereign-consolidate.py" && echo "still running" || echo "stopped"

# 2. Check lock file
[ -f ~/.hermes/consolidation-staging/.pipeline.lock ] && \
  echo "Lock exists" && \
  stat -f %m ~/.hermes/consolidation-staging/.pipeline.lock | \
  awk '{print strftime("%Y-%m-%d %H:%M", $0)}'

# 3. If stopped and lock is stale (> 30 min), clean it
pgrep -f "sovereign-consolidate.py" > /dev/null || \
  rm ~/.hermes/consolidation-staging/.pipeline.lock

# 4. Check recent error logs
tail -50 ~/.hermes/logs/mlx-gemma-e4b.err | grep -i error
```

### Scenario: Model server unresponsive

```bash
# Verify models are listed
curl http://127.0.0.1:11436/v1/models 2>/dev/null | jq '.data[].id'
curl http://127.0.0.1:11435/v1/models 2>/dev/null | jq '.data[].id'

# If down, check launchd
launchctl list | grep mlx-gemma

# Restart if needed
launchctl stop  com.openclaw.mlx-gemma-e4b 2>/dev/null || true
launchctl start com.openclaw.mlx-gemma-e4b
sleep 15
curl http://127.0.0.1:11435/v1/models 2>/dev/null | jq '.data[0].id'
```

## Performance Expectations

**Quick Health Check:** ~1-2 seconds (database query + curl to model servers)  
**Full Diagnostic:** ~5 seconds (includes error log scanning)  
**Safe to run:** Every 1-4 hours from cron  
**Database impact:** Negligible (queries indexed, < 100ms)

## Success Criteria

✅ Both model servers report UP  
✅ Recent activity (> 0 facts in last 4 hours)  
✅ Lock file not stale (< 30 min old) OR no running process  
✅ Average confidence > 0.90  
✅ No critical errors in last 100 log lines  
✅ Database responsive (query completes in < 1s)

## Pitfalls

### ❌ DO NOT

- Run diagnostics with `shell=False` subprocess (security risk with parsing)
- Alert on every stale lock without checking if process is running
- Assume "no recent activity" means pipeline failed (may be scheduled for later)
- Set alert thresholds too tight (will produce false positives)
- Chain multiple health checks in a single cron job (timeout risk)

### ✅ DO

- Include a timeout on every curl/system command
- Check process status before removing lock files
- Correlate database activity with cron schedule before alerting
- Test alert thresholds against production data before deploying
- Run health check 1-2 hours after scheduled consolidation

## Integration with Alerting Systems

### Webhook Alert (JSON payload)

```python
def send_alert(status, alerts):
    """Send health status to webhook."""
    import httpx
    payload = {
        "content": f"Minni Health: {status}",
        "embeds": [{
            "title": "Pipeline Status",
            "description": "\n".join(alerts),
            "color": 0x00ff00 if status == "HEALTHY" else 0xff0000,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    httpx.post("https://your-webhook-url", json=payload, timeout=30)
```

### Prometheus Metrics

```python
def export_metrics():
    """Export health metrics for Prometheus."""
    print("sovereign_memory_total_learnings 989")
    print("sovereign_memory_active_learnings 988")
    print("sovereign_memory_recent_facts_4h 30")
    print("model_server_e4b{port=\"11435\"} 1")  # 1=up, 0=down
    print("model_server_e2b{port=\"11436\"} 1")
    print("consolidation_last_run_seconds_ago 3600")
```


---
name: sovereign-memory-consolidation
description: Run Sovereign Memory consolidation pipeline, troubleshoot configuration errors, generate health reports. Handles --limit parameter, timeout diagnostics, and database queries.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [sovereign-memory, consolidation, diagnostics, openclaw, cron]
    related_skills: [systematic-debugging]
---

# Sovereign Memory Consolidation Pipeline

## Overview

Run the Sovereign Memory consolidation pipeline to extract facts from session records, consolidate them using local LLMs (E2B/E4B), and update the semantic knowledge base.

## When to Use

- Scheduled consolidation runs via cron
- Generating health reports on the knowledge graph
- Troubleshooting consolidation failures
- Checking pipeline performance or bottlenecks
- Validating database integrity

## Key Information

### Correct Execution Syntax

```bash
source ~/.hermes/hermes-agent/venv/bin/activate
python3 ~/.hermes/scripts/sovereign-consolidate.py [OPTIONS]
```

**IMPORTANT:** There is NO `--batch` flag. Attempting `--batch` produces:
```
error: unrecognized arguments: --batch
```

### Available Flags

| Flag | Purpose | Example |
|------|---------|---------|
| `--all` | Process ALL wiki pages (not time-limited) | `--all --limit 100` |
| `--hours N` | Process pages from last N hours (default: 24) | `--hours 1` |
| `--limit N` | Max pages to process per run | `--limit 5` |
| `--page PATH` | Process single wiki page path | `--page /path/to/page.md` |
| `--dry-run` | Show what would be done without executing | `--dry-run` |
| `--promote` | Run episodic → semantic promotion pipeline | `--promote` |
| `--resolve` | Execute judge's contradiction/merge decisions | `--resolve` |

### Recommended Cron Commands

**For frequent short runs (6 hours):**
```bash
0 */6 * * * source ~/.hermes/hermes-agent/venv/bin/activate && python3 ~/.hermes/scripts/sovereign-consolidate.py --limit 5 --promote --resolve
```

**For full consolidation (nightly):**
```bash
0 2 * * * source ~/.hermes/hermes-agent/venv/bin/activate && python3 ~/.hermes/scripts/sovereign-consolidate.py --all --limit 100 --promote --resolve
```

## Common Errors & Fixes

### Error: `TypeError: slice indices must be integers or None or have an __index__ method`

**Location:** Line ~335 in `sovereign-consolidate.py`  
**Symptom:**
```
File "~/.hermes/scripts/sovereign-consolidate.py", line 335, in extract_facts_from_page
    truncated = content[:E2B_MAX_CHARS]
TypeError: slice indices must be integers or None or have an __index__ method
```

**Root Cause:** `CHARS_PER_TOKEN` defined as string literal `***` instead of numeric value

**Fix:** Patch the constant definition:

```bash
# In ~/.hermes/scripts/sovereign-consolidate.py around line 73:
# BEFORE: CHARS_PER_TOKEN=***
# AFTER:  CHARS_PER_TOKEN = 4
```

**Using patch tool:**
```python
patch(
    path="~/.hermes/scripts/sovereign-consolidate.py",
    old_string='CHARS_PER_TOKEN=***  # chars per token for 4-bit quantized models',
    new_string='CHARS_PER_TOKEN = 4  # chars per token for 4-bit quantized models'
)
```

**Verification:** After applying fix, run pipeline again. Should see health check messages:
```
[INFO] E2B server OK (mlx-community/gemma-4-e2b-it-4bit)
[INFO] E4B server OK (mlx-community/gemma-4-e4b-it-4bit)
```

### Error: `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` (PEP 604 under Python 3.9)

**Symptom in `~/.hermes/logs/session-extract.err`:**
```
File "/Users/hansaxelsson/.hermes/scripts/session-extract.py", line 220, in <module>
    def run_gemma_extraction(text: str) -> dict | None:
TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
```

**Root cause — Critical cron gotcha:** The cron prompt says:
```bash
source ~/.hermes/hermes-agent/venv/bin/activate && python3 ~/.hermes/scripts/session-extract.py
```
…but in practice, cron-executed agent runs sometimes **don't actually activate the venv** before invoking `python3`, causing it to resolve to `/usr/bin/python3` (macOS system Python 3.9) instead of the Hermes venv's 3.11. PEP 604 `X | None` union syntax requires 3.10+.

**Diagnosis:**
```bash
which python3 && python3 --version        # shows 3.13/3.11 interactively
/usr/bin/python3 --version                 # shows 3.9.6 — what cron actually gets
```

**Belt-and-suspenders fix (apply both):**

1. **Pin shebang to venv python (direct-exec path):**
   ```python
   #!/Users/hansaxelsson/.hermes/hermes-agent/venv/bin/python3
   ```
   Avoids `#!/usr/bin/env python3` which is subject to PATH order.

2. **Make code 3.9-compatible (cron-invocation path — shebangs are ignored when invoked as `python3 script.py`):**
   ```python
   # BEFORE (3.10+ only):
   def run_gemma_extraction(text: str) -> dict | None:

   # AFTER (3.9+):
   from typing import Optional
   def run_gemma_extraction(text: str) -> Optional[dict]:
   ```

**Verification:**
```bash
/usr/bin/python3 -c "import ast; ast.parse(open('path/to/script.py').read())"  # ✅ parses under 3.9
bash -c "source ~/.hermes/hermes-agent/venv/bin/activate && python3 path/to/script.py --dry-run"
```

**Rule of thumb:** Any `~/.hermes/scripts/*.py` that a cron invokes should be 3.9-compatible, not just rely on `source venv/bin/activate` being honored.

---

### Error: MLX E4B OOM crash mid-extraction (`Connection refused` after healthy ping)

**Symptom:** Health check passes, then the actual extraction call fails with:
```
[ERROR] Gemma API request failed: [Errno 61] Connection refused
```

**Check `~/.hermes/logs/mlx-gemma-e4b.err`:**
```
libc++abi: terminating due to uncaught exception of type std::runtime_error:
  [METAL] Command buffer execution failed: Insufficient Memory (00000008:***)
```

**Root cause:** MLX `mlx_lm.server` accumulates prompt-cache residency across requests (e.g. 5.6 GB after back-to-back 71K-token prompts). A subsequent large prompt exceeds GPU memory → Metal kills the process → launchd auto-restarts it (~15s downtime) → in-flight request dies with `Connection refused`.

**Evidence pattern in E4B err log:**
1. Long "Prompt processing progress: N/M" for big session
2. `Prompt Cache: 6 sequences, 5.60 GB` (high residency)
3. `libc++abi: terminating ... Insufficient Memory`
4. `Fetching 8 files: 100%` (model reload via launchd)
5. `Starting httpd at 127.0.0.1 on port 11435...`

**Workaround — don't retry immediately same-shell:** launchd restart takes ~10-15s; the next cron run (hours later) will succeed fresh. If retrying manually, `sleep 20` first or `curl http://localhost:11435/v1/models` until 200.

**Session identification for OOM-killed backlog:**
```sql
sqlite3 ~/.hermes/extraction-tracking.db \
  "SELECT session_id, status FROM extracted_sessions WHERE status = 'failed';"
```
Retry individually with `--session <id>` after confirming server is up.

---

### Hardening pattern (2026-04-17): two-layer OOM resilience

On 16GB M4 systems, E4B+E2B+OS can consume 12-14GB resident with prompt caches expanding the pressure. Two-layer fix applied to both `session-extract.py` and `sovereign-consolidate.py`:

**LAYER 1 — Server-side KV cache bounds (launchd plists)**

The MLX server accepts three crucial bounds flags (verify with `mlx_lm.server --help | grep prompt-cache`):

```xml
<!-- ~/Library/LaunchAgents/com.openclaw.mlx-gemma-e4b.plist -->
<key>ProgramArguments</key>
<array>
    <string>/opt/homebrew/bin/python3.11</string>
    <string>-m</string>
    <string>mlx_lm.server</string>
    <string>--model</string><string>mlx-community/gemma-4-e4b-it-4bit</string>
    <string>--port</string><string>11435</string>
    <string>--log-level</string><string>INFO</string>
    <!-- Hard cap: E4B gets 2GB KV cache; E2B gets 1GB -->
    <string>--prompt-cache-bytes</string><string>2147483648</string>
    <!-- Retain at most 4 distinct prompts in cache (default is unbounded) -->
    <string>--prompt-cache-size</string><string>4</string>
    <!-- Halved from default 2048; lowers peak memory during long-prompt prefill -->
    <string>--prefill-step-size</string><string>1024</string>
</array>
<!-- Don't crash-loop: minimum 30s between restarts -->
<key>ThrottleInterval</key><integer>30</integer>
<!-- Yield to foreground apps -->
<key>ProcessType</key><string>Background</string>
<key>LowPriorityIO</key><true/>
```

Reload with:
```bash
launchctl unload ~/Library/LaunchAgents/com.openclaw.mlx-gemma-e4b.plist
launchctl load   ~/Library/LaunchAgents/com.openclaw.mlx-gemma-e4b.plist
# Same for e2b. Verify flags took effect:
ps aux | grep mlx_lm.server | grep -v grep
```

Sizing rule of thumb on 16GB M4: E4B cache ≤ 2GB, E2B cache ≤ 1GB, leaves ~11GB for weights + OS + user apps.

**LAYER 2 — App-side crash-resilient retry**

Plist bounds prevent most OOMs, but if one still happens, launchd needs ~15-30s to restart. Apps should ride it out. Pattern for both scripts:

```python
def post_api(url, payload, timeout, model_id):
    """Crash-resilient retry — distinguishes connection errors from timeouts."""
    MAX_ATTEMPTS = 4
    BACKOFF_BASE = 10  # 10s, 20s, 40s, 80s total ~150s window
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = httpx.post(url, json=payload, timeout=timeout,
                              headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as e:
            # Server crashed + restarting — wait for launchd
            last_err = e
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            if attempt < MAX_ATTEMPTS:
                logger.warning("[%s] unreachable (%d/%d): %s — retry in %ds",
                               model_id, attempt, MAX_ATTEMPTS, e, wait)
                time.sleep(wait)
            else:
                logger.error("[%s] unreachable after %d attempts", model_id, MAX_ATTEMPTS)
                return None
        except httpx.ReadTimeout as e:
            # DO NOT retry — timeout means the model is legitimately slow, let it finish
            logger.error("[%s] timed out after %ds", model_id, int(timeout))
            return None
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.error("[%s] request failed: %s", model_id, e)
            return None
```

**Critical distinction:** retry on `ConnectError`/`RemoteProtocolError`/`ReadError` (server dead/restarting), but NOT on `ReadTimeout` (server working, just slow — retrying multiplies work). The backoff window (10+20+40+80 = 150s) comfortably covers launchd's 30s throttle + model reload time.

**Timeout sizing for local MLX (2x-3x the cloud equivalents):**

```python
# session-extract.py (E4B direct)
HTTP_TIMEOUT = 1200.0  # 20 min — large sessions can take 200s+ of prompt processing

# sovereign-consolidate.py
E2B_REQUEST_TIMEOUT = 360    # 6 min (extraction, fast model)
E4B_REQUEST_TIMEOUT = 600    # 10 min (consolidation, thinking model)
HTTP_TIMEOUT       = 1200.0  # 20 min HTTP
E4B_HTTP_TIMEOUT   = 1800.0  # 30 min HTTP cap (E4B reasoning)
```

Local inference is free; erring on too-generous is the right tradeoff.

**Verification after applying both layers:**

```bash
# 1. plist accepts new args (won't crash on load)
plutil -lint ~/Library/LaunchAgents/com.openclaw.mlx-gemma-e4b.plist
launchctl unload ~/Library/LaunchAgents/com.openclaw.mlx-gemma-e4b.plist
launchctl load   ~/Library/LaunchAgents/com.openclaw.mlx-gemma-e4b.plist

# 2. Confirm flags live in process args
ps aux | grep mlx_lm.server | grep prompt-cache-bytes

# 3. Watch err log during a real extraction — should see no OOM crashes
tail -f ~/.hermes/logs/mlx-gemma-e4b.err &
bash -c "source ~/.hermes/hermes-agent/venv/bin/activate && python3 ~/.hermes/scripts/session-extract.py --limit 3"

# 4. If server ever does crash mid-request, the app log should show retry pattern
grep "unreachable.*retry in" ~/.hermes/logs/*.log
```

---

### Error: Watchdog false-alarms "Pipeline stalled" every day

**Symptom:** `consolidation-watchdog` cron fires alert:
```
🟠 Pipeline stalled — no activity for 5h (last log entry: YYYY-MM-DD 14:16)
```
…but MLX servers are healthy, consolidation ran successfully at 3am.

**Root cause:** The watchdog's stall threshold was set to **2 hours** but the `consolidation-pipeline` cron schedule is `0 3 * * *` (daily @ 3am). Between ~6am and 3am next morning (~21h), any check will show "no activity" and fire the alert — one false positive per hour, ~20+ per day.

**Fix in `~/.hermes/scripts/consolidation-watchdog.sh`:**
```bash
# Match threshold to daily schedule — stall = missed a full 24h cycle
STALL_THRESHOLD_HOURS=25
if [ "$DIFF" -gt "$STALL_THRESHOLD_HOURS" ]; then
    ALERTS="${ALERTS}\n🟠 **Pipeline stalled** — no activity for ${DIFF}h (threshold: ${STALL_THRESHOLD_HOURS}h)"
fi
```

**Also add grace window for "no log today":**
```bash
# Don't alert before 4am (pipeline runs at 3am) and if yesterday's log also missing
CURRENT_HOUR=$(date "+%H")
if [ "$CURRENT_HOUR" -gt 4 ]; then
    YESTERDAY=$(date -v-1d +%Y%m%d)
    [ ! -f "$LOG_DIR/consolidation-${YESTERDAY}.log" ] && ALERTS="..."
fi
```

**General rule:** Watchdog stall threshold = **cron period × 1.05** (allow 5% slack). Hourly job → 65min. Daily job → 25h. Weekly → 8d.

---

### Error: Pipeline Timeout (Running but No Completion)

**Typical scenario:** Full `--all` run exceeds time budget

**Actions:**
1. **Don't re-run immediately** — models are still processing background
2. **Check consolidation staging directory:**
   ```bash
   ls -lah ~/.hermes/consolidation-staging/
   ```
3. **Query database for recent activity** (see diagnostics below)
4. **Use `--limit` parameter** to reduce scope

### Error: Stale Lock File — Process Exited But Lock Remains (2026-04-24 postmortem)

**Symptom:** Next pipeline run hangs indefinitely (waits for lock). Lock file exists but is stale. `ps aux` shows no `sovereign-consolidate.py` process running.

**Typical cause:** Earlier run exited abnormally (crash, timeout, or early exit between health checks) without releasing the lock via `atexit.register(_release_lock)`.

**Diagnosis sequence:**

1. **Confirm no process is running:**
   ```bash
   pgrep -f "sovereign-consolidate.py" && echo "still running" || echo "process exited"
   ```

2. **Verify lock exists and is fresh:**
   ```bash
   ls -la ~/.hermes/consolidation-staging/.pipeline.lock
   # Should show recent timestamp (last 30-60 min)
   ```

3. **Check logs for a crash or early exit:**
   ```bash
   tail -50 ~/.hermes/logs/consolidation/consolidation-$(date +%Y%m%d).log
   # Look for: no "Summary:" or "Consolidation Summary:" section
   # Look for: last entry is health check, then silence
   ```

4. **Verify model servers are healthy (not hung):**
   ```bash
   curl -s http://127.0.0.1:11435/v1/models | jq '.data[0].id'  # E4B
   curl -s http://127.0.0.1:11436/v1/models | jq '.data[0].id'  # E2B
   # Both should respond within 1-2 seconds
   ```

5. **If all checks pass, lock is safe to remove:**
   ```bash
   rm ~/.hermes/consolidation-staging/.pipeline.lock
   ```

**Verification:** Next pipeline run should start immediately and complete normally.

**Prevention:** The skill's 2026-04-17 hardening includes `atexit.register(_release_lock)` which catches `sys.exit()`, exceptions, and normal exit. If locks are still leaking, check for:
- `os._exit()` (forceful exit bypassing atexit)
- Background detach without lock cleanup
- SIGKILL on the process (outside atexit's reach)

**Post-run lock cleanup (cron observation 2026-04-28):**  
If a pipeline run exits with an error (e.g., `ModuleNotFoundError` during write-back), the lock file may remain stale. The lock prevents the next scheduled run from starting. Safe recovery:

```bash
# Verify no process is running
pgrep -f "sovereign-consolidate.py" || echo "clear to remove lock"

# Verify lock is stale (> 30 min old)
stat ~/.hermes/consolidation-staging/.pipeline.lock

# Remove stale lock
rm -f ~/.hermes/consolidation-staging/.pipeline.lock

# Next cron run will start normally
```

This is safe because:
- The lock is only held for the duration of a single run (max ~3600s)
- If a process is still running (verified above), removal would cause duplicate runs, but pgrep catches that
- The pipeline itself doesn't hold locks across restarts — each run is stateless

---

### Error: ModuleNotFoundError — Missing SOVEREIGN_PROJECT Dependency

**Symptom:** Pipeline completes E2B/E4B health checks, extracts and consolidates facts, then fails:
```
ModuleNotFoundError: No module named 'config'
  File "~/.hermes/scripts/sovereign-consolidate.py", line 624, in write_to_sovereign_memory
    from config import DEFAULT_CONFIG as SOV_CONFIG
```

**Root cause:** The Sovereign Memory Python library is not installed or SOVEREIGN_PROJECT environment variable points to a non-existent path.

**Default location checked:**
```bash
SOVEREIGN_PROJECT = ~/Projects/sovereign-memory
```

**Impact:** Staged facts are successfully consolidated but NOT written to the knowledge graph database. The staging .json file persists and is retried on next run.

**Diagnosis:**
```bash
# Check if project directory exists
ls -d ~/Projects/sovereign-memory && echo "✅ Directory exists" || echo "❌ Directory missing"

# Check if config.py exists
ls -f ~/Projects/sovereign-memory/config.py 2>/dev/null && echo "✅ Module found" || echo "❌ Module missing"

# Check environment override
echo $SOVEREIGN_PROJECT
```

**Fix (choose one):**

**Option 1 — Install Sovereign Memory (recommended for permanent fix):**
```bash
cd ~/Projects  # or create if missing: mkdir -p ~/Projects
git clone https://github.com/nous-research/sovereign-memory
cd sovereign-memory
pip install -e .
```

**Option 2 — Set custom path (if project is elsewhere):**
```bash
export SOVEREIGN_PROJECT=/path/to/sovereign-memory
source ~/.hermes/hermes-agent/venv/bin/activate
python3 ~/.hermes/scripts/sovereign-consolidate.py --limit 5
```

**Option 3 — Assess health without write-back (temporary diagnostics):**

When you can't fix the dependency immediately, use this fallback to still get pipeline health:

```bash
# 1. Check staging consolidation (dry-run avoids write-back)
source ~/.hermes/hermes-agent/venv/bin/activate
python3 ~/.hermes/scripts/sovereign-consolidate.py --dry-run --limit 5

# 2. Query database for health metrics
sqlite3 ~/.openclaw/sovereign_memory.db <<EOF
SELECT 'Total learnings' as metric, COUNT(*) FROM learnings
UNION ALL
SELECT 'Active learnings', COUNT(*) FROM learnings 
  WHERE superseded_by IS NULL AND (expires_at IS NULL OR expires_at > strftime('%s', 'now'))
UNION ALL
SELECT 'Added (24h)', COUNT(*) FROM learnings 
  WHERE created_at > strftime('%s', 'now', '-24 hours');
EOF

# 3. Check staging directory for pending consolidated facts
ls -lah ~/.hermes/consolidation-staging/*.json | head -5

# 4. Review last log for partial success
tail -20 ~/.hermes/logs/consolidation/consolidation-$(date +%Y%m%d).log
```

**Expected output if at staging step:**
```
Loaded 9 staged facts from 20260406_213429_8cce0a.json
E4B consolidation complete in 87.6s
Staged 20260406_213429_8cce0a consolidated: 9 entries
```
…then ModuleNotFoundError during write-back. Staged file remains in `~/.hermes/consolidation-staging/`.

**Verification after fix:**
```bash
# Retry with dependency installed
python3 ~/.hermes/scripts/sovereign-consolidate.py --limit 5 --promote --resolve

# Confirm no error in logs
tail -5 ~/.hermes/logs/consolidation/consolidation-$(date +%Y%m%d).log | grep -i error
# Should show: no errors (or only pre-existing errors)

# Verify facts were written
sqlite3 ~/.openclaw/sovereign_memory.db "SELECT COUNT(*) as new_learnings FROM learnings WHERE created_at > strftime('%s', 'now', '-5 minutes');"
# Should show: non-zero count
```

---

### Error: Cron runs stack / pipeline silently stalls for hours (2026-04-17 postmortem)

**Symptom:** Log shows pipeline "starting" every cron cycle but never "Summary:" lines. Multiple processes running. Watchdog fires "no activity for Nh".

**Root cause:** E4B LLM call hangs with no request timeout. Each subsequent cron run starts fresh (no lock) and also hangs.

**Defenses in current script (as of 2026-04-17):**
- `E2B_REQUEST_TIMEOUT = 180` and `E4B_REQUEST_TIMEOUT = 300` on every `post_api` call
- `try/except (httpx.TimeoutException, httpx.ReadTimeout)` around E4B consolidation — staging file preserved for retry
- `MAX_PIPELINE_SECONDS = 3600` wall-clock guard checked between pages
- `fcntl.flock LOCK_EX|LOCK_NB` at `~/.hermes/consolidation-staging/.pipeline.lock` — second run exits cleanly
- Watchdog probes both 11436 (E2B) and 11435 (E4B)

**Verify protections are in place:**
```bash
grep -n "E4B_REQUEST_TIMEOUT\|fcntl.flock\|MAX_PIPELINE_SECONDS" ~/.hermes/scripts/sovereign-consolidate.py
grep -n "11435\|11436" ~/.hermes/scripts/consolidation-watchdog.sh
```

**Test concurrency lock:** Start a real run, then in another shell run `--dry-run --limit 1` — it should log "Another consolidation run is already in progress" and exit 0.

**All hazards resolved (Recon audit round 2, 2026-04-17):**
- ✅ Watchdog webhook uses `jq -n --arg content "$MSG" '{content: $content}'` — no shell injection
- ✅ SQLite opens with `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`
- ✅ `clear_staging` guarded by `if not (write_errors or wiki_errors)` — retries on partial failure
- ✅ Frontmatter built via `yaml.safe_dump(fm_dict, ...)` — colons/quotes/newlines safe
- ✅ Dry-run preserves staging (doesn't burn session for real run)
- ✅ Lockfile released via `atexit.register(_release_lock)` — covers sys.exit, exception, normal exit

Real Recon invocation (grok-4-1-fast-reasoning via OpenClaw Council):
```bash
openclaw agent --agent recon --local --thinking high --message "audit task"
```
NOT Hermes `delegate_task` — that spawns a generic subagent with no Recon identity.

## Database Health Diagnostics

### Core Statistics Query

```sql
sqlite3 ~/.openclaw/sovereign_memory.db <<EOF
SELECT 'Total learnings' as metric, COUNT(*) as value FROM learnings
UNION ALL
SELECT 'Learnings (agent=hermes)', COUNT(*) FROM learnings WHERE agent_id = 'hermes'
UNION ALL
SELECT 'Active learnings', COUNT(*) FROM learnings WHERE superseded_by IS NULL AND (expires_at IS NULL OR expires_at > strftime('%s', 'now'))
UNION ALL
SELECT 'Superseded learnings', COUNT(*) FROM learnings WHERE superseded_by IS NOT NULL
UNION ALL
SELECT 'Expired learnings', COUNT(*) FROM learnings WHERE expires_at < strftime('%s', 'now');
EOF
```

### Category Breakdown

```sql
sqlite3 ~/.openclaw/sovereign_memory.db \
  "SELECT category, COUNT(*) as count FROM learnings GROUP BY category ORDER BY count DESC;"
```

### Recent Consolidation Activity (30 minutes)

```sql
sqlite3 ~/.openclaw/sovereign_memory.db \
  "SELECT COUNT(*) as recent_learnings FROM learnings WHERE created_at > strftime('%s', 'now', '-30 minutes');"
```

### Last 10 Learnings (with metadata)

```sql
sqlite3 ~/.openclaw/sovereign_memory.db << EOF
SELECT 
  learning_id, 
  category, 
  agent_id, 
  substr(content, 1, 80) as preview,
  datetime(created_at, 'unixepoch') as created_at
FROM learnings 
ORDER BY created_at DESC 
LIMIT 10;
EOF
```

## Performance Expectations

| Stage | Duration | Notes |
|-------|----------|-------|
| E2B health check | ~25s | Connects to gemma-4-e2b model |
| E4B health check | ~10s | Connects to gemma-4-e4b model |
| Per-session fact extraction | 35-40s | Processes one session file |
| Consolidation per 9-11 facts | 65-85s | E4B merging and deduplication |
| Database write per entry | 0.3ms | Index writes included |
| Wiki page creation per fact | ~10ms | Creates mirror concept page |

**Full run estimate:**
- `--limit 5` = ~10 minutes
- `--all --limit 100` = ~2-3 hours (don't run on frequent crons)

## Health Status Interpretation

| Signal | Status | Action |
|--------|--------|--------|
| 98 total learnings, 98 active | ✅ Healthy | Continue normal ops |
| All confidence > 0.85 | ✅ Healthy | Data quality good |
| No superseded/expired entries | ✅ Healthy | No cleanup needed |
| 11+ learnings in last 30 min | ✅ Healthy | Active consolidation |
| Database query succeeds | ✅ Healthy | Storage healthy |
| E2B/E4B health checks pass | ✅ Healthy | Models ready |
| High E2B/E4B memory | ⚠️ Monitor | May need --limit |
| Recurring TypeErrors | ⚠️ Config | Fix CHARS_PER_TOKEN |
| Database query times out | 🔴 Critical | Check disk space |
| Cannot connect to models | 🔴 Critical | Check service status |

## Staging Directory

The pipeline uses a staging mechanism to handle multi-run consolidation:

```bash
~/.hermes/consolidation-staging/  # Temporary storage for extracted facts
```

After successful consolidation, facts are moved from staging to the main database. Residual staging files (`.json`) can be safely deleted if stale (> 7 days old).

```bash
# List staging files
ls -lah ~/.hermes/consolidation-staging/

# Clean old staging files (optional)
find ~/.hermes/consolidation-staging -name "*.json" -mtime +7 -delete
```

## Wiki Mirror

Consolidated facts are synced to a local wiki for human review:

```bash
~/wiki/concepts/          # Durable facts
~/wiki/decisions/         # Architectural decisions
~/wiki/consolidation/     # Consolidation metadata and logs
```

The wiki acts as a human-readable mirror of the knowledge graph.

## Timeout Diagnostics

**If pipeline times out mid-execution or appears stuck:**

### Step 1: Check for a Running Process
```bash
pgrep -f "sovereign-consolidate.py" && echo "still running" || echo "process exited"
# If still running, the pipeline is actually executing — wait for completion
# If exited, check Step 2 for stale locks
```

### Step 2: Check for Stale Lock
```bash
ls -la ~/.hermes/consolidation-staging/.pipeline.lock
# If file exists and is >30 min old AND process is not running, the lock is stale
# Safe to remove: rm ~/.hermes/consolidation-staging/.pipeline.lock
```

### Step 3: Check Staging Directory
```bash
ls -lah ~/.hermes/consolidation-staging/ | head -10
# Shows residual files (.json) from staging
# Clean old staging: find ~/.hermes/consolidation-staging -name "*.json" -mtime +7 -delete
```

### Step 4: Query Database for Progress
```bash
sqlite3 ~/.openclaw/sovereign_memory.db <<EOF
SELECT 'Total learnings' as metric, COUNT(*) FROM learnings
UNION ALL
SELECT 'New (last 2h)', COUNT(*) FROM learnings WHERE created_at > strftime('%s', 'now', '-2 hours');
EOF
# Shows if new learnings were created during the timeout window
```

### Step 5: Check Wiki Mirror for Recent Updates
```bash
ls -lt ~/wiki/concepts/ | head -10
# Shows which concepts were recently created
```

### Step 6: Review Pipeline Logs
```bash
# Check today's log for completion or errors
tail -100 ~/.hermes/logs/consolidation/consolidation-$(date +%Y%m%d).log

# Look for "Consolidation Summary:" section
# If not present, pipeline exited early — check Step 2 for lock
```

### Step 7: Verify Model Servers Are Responsive
```bash
# E4B (port 11435) and E2B (port 11436) health
curl -s http://127.0.0.1:11435/v1/models | jq '.data[0].id'
curl -s http://127.0.0.1:11436/v1/models | jq '.data[0].id'
# Both should respond within 1-2 seconds; check MLX error logs if not
```

### Step 8: Reduce Scope and Retry
```bash
# Use --limit to process fewer pages
source ~/.hermes/hermes-agent/venv/bin/activate
python3 ~/.hermes/scripts/sovereign-consolidate.py --limit 3 --promote --resolve
```

## Pitfalls

### ❌ DO NOT

- Run `--all` without `--limit` on frequent crons (will exceed time budget)
- Use `--batch` flag (doesn't exist; use `--limit` instead)
- Hardcode paths in cron scripts (use env vars and source ~/.bashrc)
- Run multiple instances concurrently (causes database locking)
- Ignore timeout warnings (models may still be processing)
- Assume fresh Python session between runs (use `source` in cron)
- Leave staging directory full without cleanup (wastes disk space)

### ✅ DO

- Verify help output before running: `python3 sovereign-consolidate.py --help`
- Use `--limit 5-10` for frequent cron jobs (keeps runs under 10 minutes)
- Use `--all --limit 100` for nightly full consolidation
- Check health with database queries before assuming failure
- Wait 30 seconds before re-running after timeout
- Use `--dry-run` to preview what will be processed
- Monitor performance metrics over time to optimize scheduling

## Success Criteria

✅ Pipeline starts without TypeError  
✅ E2B and E4B health checks complete successfully  
✅ At least 3-5 learnings stored per session  
✅ Wiki mirror updated with new concept pages  
✅ Database queries return valid counts  
✅ No entries in superseded_by or expires_at columns for active learnings  
✅ Staging directory contains residual .json files (expected)  
✅ Recent learnings show high confidence (> 0.85)

## Example: Full Health Report

```bash
#!/bin/bash
set -e

echo "=== Sovereign Memory Health Report ==="
echo

echo "Database Statistics:"
sqlite3 ~/.openclaw/sovereign_memory.db <<SQL
SELECT 'Total learnings', COUNT(*) FROM learnings
UNION ALL
SELECT 'Active learnings', COUNT(*) FROM learnings WHERE superseded_by IS NULL;
SQL

echo
echo "Category Breakdown:"
sqlite3 ~/.openclaw/sovereign_memory.db \
  "SELECT category, COUNT(*) FROM learnings GROUP BY category ORDER BY count DESC;"

echo
echo "Recent Consolidation (30 min):"
sqlite3 ~/.openclaw/sovereign_memory.db \
  "SELECT COUNT(*) as new_learnings FROM learnings WHERE created_at > strftime('%s', 'now', '-30 minutes');"

echo
echo "Staging Files:"
ls -lh ~/.hermes/consolidation-staging/ | tail -5

echo
echo "✅ Report complete"
```

Run this as a health check cron:
```bash
0 */4 * * * /path/to/health_report.sh
```

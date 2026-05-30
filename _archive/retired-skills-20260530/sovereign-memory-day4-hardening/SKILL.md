---
name: sovereign-memory-day4-hardening
description: "Production-harden a Sovereign Memory deployment: idempotent wiki imports via sha256 manifest, Layer 1 identity seeding into SQLite, launchd persistence, and OpenClaw plugin registration. Use when the daemon is running but needs to survive reboots, re-runs, and multi-agent hydration."
tags: [openclaw, sovereign-memory, launchd, idempotency, identity, layer-1, manifest, dedup]
---

# Sovereign Memory — Day 4 Hardening

Production hardening pattern for a Sovereign Memory deployment that already has the daemon (`sovrd.py`) and TS adapter working. Covers the four things that make it survive the real world: idempotent imports, per-agent identity seeding, reboot persistence, and OpenClaw plugin registration.

## When to use

- Wiki import script re-imports same files on rerun (weak dedup)
- Only `hermes` has a Layer 1 identity; other council agents boot identity-less
- `sovrd.py` runs ad-hoc, dies on reboot or crash
- Plugin builds cleanly but OpenClaw gateway doesn't load it

## Architecture reminder

- **Daemon:** `~/.openclaw/plugins/sovereign-memory/sovrd.py` on `/tmp/sovereign.sock`
- **Python API:** `~/.openclaw/sovereign-memory-v3.1/agent_api.py` (venv at `venv/bin/python`)
- **DB:** SQLite at `~/.openclaw/sovereign_memory.db` — tables: `documents`, `chunk_embeddings`, `vault_fts`, `memory_links`
- **Layer 1 = identity** (whole, loaded per-agent at boot) — `documents` rows with `agent='identity:<agent_id>'`, `whole_document=1`
- **Layer 2 = knowledge** (chunked RAG) — wiki imports, learnings, etc.

## Part 1 — Idempotent wiki imports (sha256 manifest)

### Problem with semantic-only dedup
The naive approach — recall first 80 chars, skip if score > 0.9 — fails two ways:
- Topic-similar files collide → false-positive skips
- Edits to a file change the prefix → false-negative re-imports
- No record of which wiki paths were ever ingested

### Solution: local manifest + sha256
Manifest at `~/.openclaw/plugins/sovereign-memory/.import-manifest.json`:
```json
{
  "version": 1,
  "entries": {
    "concepts/foo.md": {
      "sha256": "<hex>",
      "size": 4821,
      "mtime": 1745000000,
      "imported_at": "2026-04-18T12:33:39+00:00",
      "import_count": 2
    }
  }
}
```

### Decision logic per file
1. Compute `sha256` of FULL content (not prefix)
2. Path in manifest + sha matches → **skipped-unchanged** (zero writes)
3. Path in manifest + sha differs → **imported-updated**, tag as `[wiki-import:<path> v=<count+1>]`, increment `import_count`
4. Path NOT in manifest → **imported-new**, tag as `[wiki-import:<path> v=1]`
5. Safety net: for NEW files, run stricter semantic check (threshold 0.95) — if hit, still import but flag `skipped-semantic` for review (catches pre-manifest dupes)

### Critical: atomic manifest writes
Save manifest after EACH successful `/learn` using tmp + `os.replace()`. Otherwise a crash mid-run loses the whole record.

```python
import tempfile, os, json
def save_manifest(manifest, path):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".manifest-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f, indent=2)
        os.replace(tmp, path)  # atomic on POSIX
    except Exception:
        os.unlink(tmp)
        raise
```

### CLI env vars to support
- `DRY_RUN=1` — compute diffs, no POST, no manifest writes
- `FORCE_REIMPORT=1` — ignore manifest, reimport everything
- `MANIFEST_PATH` — override manifest location
- `WIKI_DIR`, `SOCKET_PATH`, `DEDUP_THRESHOLD` (keep existing)

### Verification pattern (MANDATORY after rewrite)
Run these three passes to prove correctness:
1. **First import** — expect `imported-new=N`
2. **Immediate rerun** — must show `skipped-unchanged=N, imported-*=0`. Proves idempotency.
3. **Touch one file + run** — must show exactly `imported-updated=1, skipped-unchanged=N-1`. Proves edit detection.

If any of the three fails, the manifest logic is broken. Don't ship it.

## Part 2 — Layer 1 identity seeding (all agents)

### Discovery order (source-of-truth hunt)
For each agent_id, check these in order and use the FIRST that exists:
1. `~/.openclaw/agents/<id>/agent/SOUL.md` + `IDENTITY.md` (canonical for council)
2. `~/wiki/agents/<id>.md` or `~/wiki/agents/<id>/`
3. `~/.hermes/profiles/<id>/SOUL.md` (canonical for Hermes profiles like Vidar)

If none exist: **skip and flag — do NOT fabricate identity text.**

### Gotcha: Hermes profile stubs
`~/.hermes/profiles/{forge,syntra,recon,pulse}/SOUL.md` are ~107-byte stubs, not real identities. The REAL identities live in `~/.openclaw/agents/<id>/agent/`. Never use the Hermes profile stubs as source for council agents.

### Storage mechanism
Sovereign Memory has NO write-identity endpoint. You write directly to SQLite:
- `documents` table: one row per identity file, `agent='identity:<agent_id>'`, `whole_document=1`, `path=<source_path>`
- `chunk_embeddings` table: full content as single chunk (`chunk_index=0`) with embedding
- `vault_fts` table: FTS5 entry for keyword hit

`agent_api.py identity_context()` is the READ path — it joins these and returns formatted markdown.

### Seed script pattern
Write `seed_identity.py` in `~/.openclaw/sovereign-memory-v3.1/` that:
1. Reads source file
2. Computes embedding via same model Sovereign uses
3. INSERTs into all three tables in a transaction
4. Round-trips: calls `identity_context(agent_id)` and verifies content matches

Keep the script — it's re-runnable when identities change.

### Round-trip verification (mandatory)
After seeding, for each agent:
```python
from agent_api import SovereignAgent
a = SovereignAgent("forge")
ctx = a.identity_context()
assert "# Soul: Forge" in ctx and "# IDENTITY" in ctx
```

### Possible surprise: hermes may not be seeded yet
The system can appear to have hermes identity working via on-disk file reads fallback, while the DB is empty. Don't trust "it's already there" — verify with a direct SQL query:
```sql
SELECT COUNT(*) FROM documents WHERE agent = 'identity:hermes';
```

## Part 3 — launchd persistence

### Plist at `~/Library/LaunchAgents/com.openclaw.sovrd.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.openclaw.sovrd</string>
    <key>ProgramArguments</key>
    <array>
        <string>~/.openclaw/sovereign-memory-v3.1/venv/bin/python</string>
        <string>~/.openclaw/plugins/sovereign-memory/sovrd.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>~/.openclaw/plugins/sovereign-memory</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>~/Library/Logs/sovrd.out.log</string>
    <key>StandardErrorPath</key>
    <string>~/Library/Logs/sovrd.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONPATH</key>
        <string>~/.openclaw/plugins/sovereign-memory/src</string>
    </dict>
</dict>
</plist>
```

### Install sequence
```bash
# 1. Kill any ad-hoc sovrd
pgrep -f "python.*sovrd.py" | xargs kill -TERM

# 2. Remove stale socket
rm -f /tmp/sovereign.sock

# 3. Validate plist
plutil -lint ~/Library/LaunchAgents/com.openclaw.sovrd.plist

# 4. Bootstrap (modern syntax)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.sovrd.plist
# Fallback: launchctl load ~/Library/LaunchAgents/com.openclaw.sovrd.plist

# 5. Verify running
launchctl list | grep sovrd
curl --unix-socket /tmp/sovereign.sock http://localhost/health
```

### Crash-recovery proof (MANDATORY)
```bash
PID=$(pgrep -f "python.*sovrd.py")
kill $PID
sleep 3
curl --unix-socket /tmp/sovereign.sock http://localhost/health
# Must return {"status": "ok"}
```
If it doesn't come back, `KeepAlive` is wrong or `ThrottleInterval` is too high.

### Why `SuccessfulExit: false`
Restart ONLY on crash, not clean exit. Without this, `launchctl unload` triggers a respawn loop.

### Why `ThrottleInterval: 10`
Without it, a crash-loop spams logs and burns CPU. 10 seconds is enough breathing room.

### Unload/reload
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.sovrd.plist
# edit plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.sovrd.plist
```

## Part 4 — OpenClaw plugin registration

### How OpenClaw plugin resolution actually works

OpenClaw resolves plugins from `node_modules` only — **not** from raw directories under `~/.openclaw/plugins/`. A plugin living at `~/.openclaw/plugins/sovereign-memory/` as a bare directory is NEVER found, regardless of what's in `openclaw.json`. Every invocation will log:

```
plugins.entries.sovereign-memory: plugin not found: sovereign-memory (stale config entry ignored)
```

Manually editing `openclaw.json` to add `plugins.allow` and `plugins.entries` entries with a fake `entryPoint` field **does not work** — `entryPoint` is not a recognized loader directive; it's arbitrary config passed to the plugin at runtime.

### Correct registration path

Two prerequisites in the plugin directory:

**1. `openclaw.plugin.json`** (manifest, read before plugin code loads):
```json
{
  "id": "minni",
  "name": "Sovereign Memory",
  "description": "Local-first vector + FTS5 memory for OpenClaw agents",
  "configSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {}
  }
}
```

**2. `package.json` must have `openclaw.extensions` field:**
```json
{
  "name": "sovereign-memory-bridge",
  "type": "module",
  "openclaw": {
    "extensions": ["./dist/index.js"]
  },
  ...
}
```
The `extensions` array is what the plugin runtime loads — point it at the compiled JS, not the TS source.

### Install command

```bash
# --link symlinks instead of copying — use for local dev plugins
openclaw plugins install --link ~/.openclaw/plugins/sovereign-memory
```

This registers the plugin via `plugins.installs` in `openclaw.json` and adds it to `plugins.allow` + `plugins.entries` automatically.

### Verify
```bash
openclaw plugins list
openclaw plugins doctor
```

Then restart the gateway for the plugin to actually load.

### If plugin was previously added manually to openclaw.json
Remove the stale entry from `plugins.entries` and `plugins.allow` before running install, or use `--force`:
```bash
openclaw plugins install --force --link ~/.openclaw/plugins/sovereign-memory
```

### Install CLI reference
```
openclaw plugins install <path-or-spec>
  --link     Symlink local path instead of copying (for local dev)
  --force    Overwrite existing installed plugin
  --pin      Record exact resolved version for npm installs
```
OpenClaw tries ClawHub first, then npm, then treats the arg as a filesystem path.

## Orchestration pattern

All 4 parts can be dispatched IN PARALLEL to Gemini subagents — they don't depend on each other at the file level. User pattern: "run all in parallel, don't gate."

Route recommendation:
- Part 1 (dedup fix) — Gemini 3.1 Pro (test-heavy, needs reasoning about idempotency)
- Part 2 (identity seeding) — Gemini 3.1 Pro (audit + schema reasoning)
- Part 3 (launchd) — Gemini 3 Flash (mechanical)
- Part 4 (plugin reg) — Gemini 3 Flash (mechanical)

Dispatch via `delegate_task` with `acp_command="gemini"`, `acp_args=["--acp", "-m", "<model-id>"]`.

## Pitfalls

### Bootstrap paradox
The `forge` agent CANNOT edit files under `~/.openclaw/**` because exec loops through the gateway → timeout. Use Gemini CLI via ACP for any OpenClaw file edits, not council agents.

### `whole_document` column
Not in `db.py` CREATE TABLE but exists in actual DB (migration or manual). `agent_api.py` queries it unconditionally. Don't `DROP TABLE documents` without preserving this column.

### Hermes memory says "already seeded" when it isn't
`agent_api.py` `identity_context()` can read from multiple sources. DB empty + on-disk fallback working = false sense of completeness. Always verify with direct SQL.

### Drift has no identity source
`drift` is the Lead cognitive role in the council but may not have a SOUL.md/IDENTITY.md on disk. Don't fabricate. Either user writes one, or drift is virtual-only (no Layer 1 entry).

### launchd exit code -15
SIGTERM from your own `kill` command during test. NOT a crash. Agent status showing exit -15 after a kill test is expected and the service should auto-recover within `ThrottleInterval` seconds.

# Troubleshooting Minni

This guide records product-level failure modes that can happen when the daemon,
plugins, and agent runtime are updated at different speeds.

## Codex Plugin Reports `Parse Error: Expected HTTP/, RTSP/ or ICE/`

### Symptom

`minni_status`, `minni_recall`, or `minni_learn` can show a socket
failure like:

```text
Parse Error: Expected HTTP/, RTSP/ or ICE/
```

The vault may still write notes, while daemon-backed recall or learn storage
fails.

### Root Cause

This means a client tried to speak HTTP over the Minni daemon Unix socket,
but the live daemon is speaking line-delimited JSON-RPC.

This commonly happens after updating Minni source code while Codex is
still running an older installed plugin cache. The v4 daemon socket protocol is:

```text
JSON.stringify({"jsonrpc":"2.0","id":1,"method":"status","params":{}}) + "\n"
```

It is not:

```text
GET /health HTTP/1.1
```

### Diagnosis

Check the live socket:

```bash
ls -l ~/.minni/run/minnid.sock 2>&1 || true
lsof -U | rg 'minni/run/minnid\.sock|minnid\.sock'
```

Probe JSON-RPC directly:

```bash
node - <<'NODE'
const net = require('node:net');
const socketPath = `${process.env.HOME}/.minni/run/minnid.sock`;
const client = net.createConnection(socketPath);
client.on('connect', () => {
  client.write(JSON.stringify({jsonrpc: '2.0', id: 1, method: 'status', params: {}}) + '\n');
});
client.on('data', (chunk) => {
  console.log(chunk.toString('utf8').split('\n')[0]);
  client.destroy();
});
client.on('error', (error) => {
  console.error(error.message);
  process.exitCode = 1;
});
NODE
```

If that succeeds but the plugin still reports the HTTP parse error, compare the
repo plugin and the installed Codex plugin cache:

```bash
rg -n 'socketRequest|jsonRpcSocketRequest|/health|/learn|/recall' \
  plugins/minni/src \
  ~/.codex/plugins/cache/minni -g '!node_modules'
```

The stale cache usually still contains HTTP fallback calls such as
`socketRequest("GET", "/health")` without JSON-RPC-first helpers.

### Fix

Build and test the repo plugin first:

```bash
cd ~/Projects/Minni/plugins/minni
npm run build
npm test
node dist/cli.js status
```

If the repo build succeeds but Codex still fails, reinstall or resync the Codex
plugin cache so the running MCP server uses the current `dist/` output. Then
restart stale plugin server processes or restart Codex.

On this machine, stale plugin servers can be spotted with:

```bash
ps aux | rg 'minni|dist/server|minnid' | rg -v rg
for pid in $(pgrep -f 'minni.*/dist/server.js'); do
  lsof -p "$pid" -a -d cwd
done
```

Do not restart the daemon as the first fix unless direct JSON-RPC probing fails.
If direct JSON-RPC works, the daemon is healthy and the problem is the client
cache or plugin protocol layer.

### Regression Guard

The plugin test suite should include a Unix-socket JSON-RPC test that proves the
client writes a newline-delimited JSON-RPC request and parses the daemon result.
This prevents a future HTTP-over-socket fallback from becoming the primary path
again.

## Daemon hits the file-descriptor ceiling under sustained load

Each pooled RPC worker thread holds SQLite handles (db + wal) per database
file, so the daemon's fd footprint scales with executor width times database
count. launchd's default soft `RLIMIT_NOFILE` (256) is low enough that
sustained multi-agent load exhausts it: `accept()` starts failing with
`EMFILE`, every client sees `EPIPE`, and `launchctl print` still reports the
job as "running". The daemon raises its own soft limit at startup
(`_raise_fd_ceiling` in `src/minni/minnid.py`, default ceiling 16384), and the
launchd plist template sets `SoftResourceLimits`/`NumberOfFiles` as a floor
for the window before that runs and for older daemon builds — see
`src/minni/launchd/com.minni.minnid.plist.example`. If an existing live plist
lacks that key, add it and reload with a full `launchctl bootout` +
`bootstrap`; `launchctl kickstart -k` restarts the job but does not re-read
plist changes.

## `UserPromptSubmit hook timed out after 30s — output discarded`

Claude Code kills a `UserPromptSubmit` hook at 30s and throws away everything it
had produced, so the turn silently loses its recall injection, corrections
matching and active-plan pointer. Nothing is logged as an error — the turn just
quietly has no memory.

The usual trigger is a **cold daemon**. Retrieval models are first-call cached
singletons (`get_embedder` / `get_cross_encoder` in `src/minni/models.py`) and
the FAISS index loads on demand, so the first search after a `minnid` restart
paid the whole cold cost inside the caller. That load also makes live
huggingface.co calls to revalidate the cache, so it is not bounded by local disk
speed. Restart the daemon a few times in one session and prompt-time hooks start
overrunning.

Two guards now make this fail open rather than fail silent:

- **Hook budget.** The hook owns an internal deadline (`MINNI_HOOK_BUDGET_MS`,
  default 8000). When the daemon overruns it, the hook emits what it already has
  — the active-plan pointer, plus the previous turn's recall pointer clearly
  marked stale — and the envelope carries a `degraded` block saying memory was
  not consulted. Do not raise this above ~20s: the point is to finish well
  inside the harness kill, not to outlast it.
- **Daemon warmup.** `minnid` preloads the embedder, reranker and retrieval
  engine in a background thread right after it binds its socket
  (`MINNI_WARMUP=off` to disable), so nobody waits on the cold path. It logs
  `warmup complete in N.Ns`; grep `~/Library/Logs/minni/minnid.err.log` for it
  after a restart.

`hooks/hooks.json` also sets `"timeout": 60` on `UserPromptSubmit` as a safety
margin. That is deliberately *secondary* — it only stops a hook that is already
inside its own budget from being killed mid-write. Raising the harness timeout
alone would not have fixed this, because the hook had no deadline of its own to
finish within.

If you still see the timeout: check `daemon_timed_out` in the vault audit log
(`hook_user_prompt_submit` entries) to confirm it is the daemon rather than the
vault search, then check whether the daemon is repeatedly restarting.

## Stale plugin after `git pull` (deploy honesty)

### Symptom

You pulled or merged `main` in the dogfood checkout, but agents still behave as
if nothing changed: old hook logic, missing tools, or MCP pointing at a legacy
cache tree. `minni doctor` may still pass (it does not prove every host dist
matches checkout HEAD).

### Diagnosis

```bash
minni status
# look for deploy.stale / plugin_dist.stale in the deploy block
make check-versions
make check-deployments   # from a source checkout
```

Top-level `deploy.stale` includes nested `plugin_dist.stale` (process *or*
plugin lag counts as stale).

### Fix

From the live checkout (clean tree on `main`):

```bash
make sync-root
```

That refreshes the editable install, rebuilds the plugin, redeploys with the
D7 fleet partition (`minni wire all --from-repo`, then propagate for
antigravity + cursor only), restarts the daemon when launchd is loaded, and
runs the version/deployment checkers. Do **not** "fix" a wire-adopted host by
re-running `propagate update-plugin --platform codex|kilocode|grok` — that
rewrites MCP onto legacy trees. See [deploy/README.md](../deploy/README.md).

## Active thread vs active-plan naming

The durable planning surface ships as **`minni_thread_*`** tools and envelope
keys **`active_thread` / `active_thread_ref`**. Older docs and vault artifacts
still say "active plan" / `_active_plan.json` / `minni_plan_*` — those plan
names remain as **deprecated aliases** until a later release, and on-disk
pointer files may still use the historical `_active_plan.json` path. When
debugging "no active plan" injection or id-less tool resolution, check both the
envelope's `active_thread` field and the vault artifacts pointer; the MCP
primary names are `minni_thread_*`.

## Dual-resolution candidate twins (issue #239)

### Symptom

AFM consolidation or inbox ingest leaves **byte-identical** twin rows in
`candidate_packets` (same inbox file + candidate index + content hash) so the
queue looks noisier than the vault, or repair tooling reports dual-resolution
candidates.

### Fix (operator)

Dry-run first; stop writers (AFM loop / minnid) before `--apply` when practical:

```bash
python3 scripts/repair_issue_239.py
python3 scripts/repair_issue_239.py --apply
# optional destructive index prune (explicit):
# python3 scripts/repair_issue_239.py --apply --prune-index --vault /path/to/vault
```

Default `--apply` only collapses identical dual-resolution twins (+ optional
inbox unique index). It never touches `learnings`. See the script header and
`minni.repair_dual_candidates` for the winner rule.

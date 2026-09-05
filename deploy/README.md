# deploy/ — root update propagation (operator)

The live machine executes code from several places that all descend from this
install: the engine package (wheel or editable), the wire-managed plugin tree
(`~/.minni/plugin/<version>`), and per-platform hook dists. When the **package
or `main` moves**, hosts do not automatically follow — Claude/Codex/Grok/Kilo
can keep running last week's `server.js` while `minni doctor` still passes
daemon probes.

## Product command (preferred)

```bash
minni sync              # redeploy fleet from *this* install
minni sync --full       # editable checkout: git ff + rebuild + redeploy
minni sync --install-auto   # macOS: schedule full checkout sync (opt-in)
```

Customer journeys:

| Install | Update path |
|---------|-------------|
| `pipx install minni` | `pipx upgrade minni && minni sync` |
| Editable dogfood | clean `main` + `minni sync` or `minni sync --full` |

`minni doctor` **WARNs** when `deploy.stale` / `plugin_dist.stale` and names
`minni sync`. Daemon `status.deploy` remains the machine-readable signal.

Implementation: `src/minni/fleet_sync.py` + CLI `minni sync`.

## 1. `make sync-root` (checkout dogfood / CI parity)

```
make sync-root            # do it
make sync-root DRY_RUN=1  # plan only (still runs `git fetch` for honest diverge checks; no local installs/rewrites)
```

Runs `scripts/update_root.sh`, which loudly and idempotently:

1. `git fetch origin`, then fast-forwards the checkout to `origin/main`.
   **Refuses** on a dirty tree, a non-`main` branch, or local commits
   `origin/main` lacks — it never discards or rewrites local state.
2. Refreshes the editable pip install.
3. Rebuilds the plugin (`npm run build`).
4. Redeploys the platform surfaces with the D7 fleet partition:
   `minni wire all --from-repo` (codex, claude-code, kilocode, grok), then
   `propagate.py update-plugin` for **antigravity** and **cursor** only, plus
   a grok hooks/rules refresh against the active wire install root.
   `propagate --platform all` expands only to antigravity+cursor (wire-managed
   platforms are skipped). Do **not** re-run explicit
   `update-plugin --platform codex|kilocode|grok` after wire adoption — that
   still rewrites MCP onto legacy cache/agents trees.
5. Restarts `minnid` via `launchctl kickstart` when the
   `com.minni.minnid` agent is loaded. If the agent is **not** loaded
   (Linux, `minni up`, manual process), the script still runs step 6 and
   **probes the daemon socket** for `deploy.stale`. A green probe (operator
   already bounced the process) allows `sync complete`; a stale or missing
   socket exits non-zero with an explicit restart recipe
   (`minni down && minni up` / `systemctl --user restart …`). Optional:
   `MINNI_SYNC_RESTART_CMD` is not wired yet — use launchd or bounce by hand.
6. Verifies with `scripts/check_versions.py` and
   `scripts/check_deployments.py --strict`, so the run fails loudly if the
   fleet still disagrees afterward. The post-redeploy socket probe retries
   semantic failures (stale True / missing deploy) for ~45s so a dying
   pre-restart process does not fail a successful kickstart.

**Antigravity half-state (D11):** if `wire antigravity` fails on hook
registration (`agy` is on PATH but `agy plugin install` fails), MCP views may
already be updated while hooks are not. Fix `agy`, then re-run
`minni wire antigravity --from-repo .` to complete hooks deliberately.
Bulk sync preserves existing plugin registration and refreshes recognized
installed hook commands; it does not install or enable missing native hooks.
Wire records the install root for GC protection and names the hook gap in the
platform result.

The daemon's `status` response carries a `deploy` block
(`src/minni/minnid_runtime/deploy_honesty.py`) that reports when the running
process or the deployed plugin dist is stale relative to the checkout — that
signal is what tells you a sync (and daemon restart) is due.

## 2. Scheduled sync (opt-in automation)

Unattended deploy of `origin/main` is an **operator decision**. Prefer the
entry point (installs the same plist template):

```sh
minni sync --install-auto
minni sync --auto-status
minni sync --uninstall-auto
```

Manual template install (equivalent) remains in
`deploy/com.minni.sync-root.plist.template`. The refusal rules still hold:
dirt or divergence aborts the sync; it never force-updates.

Logs: `$HOME/.minni/logs/sync-root.log` / `sync-root.err.log`.

### Scheduled environment (PATH / SSH / Node)

launchd does **not** inherit your interactive shell. The template sets:

```
PATH=__REPO__/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
```

That is enough when `git` and `npm` come from Homebrew (or `/usr/bin`). If
Node lives under nvm/fnm/asdf, append that bin directory to the template’s
`PATH` before loading. For private `origin` remotes, either:

- use a credential helper that does not need an interactive SSH agent, or
- add an `SSH_AUTH_SOCK` (or equivalent) `EnvironmentVariables` entry that
  points at a long-lived agent socket the scheduled job can read.

Without those, unattended runs fail at `git fetch` or `npm run build` and
spam the err log every interval.

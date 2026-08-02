# deploy/ — root update propagation (operator-gated)

The live machine executes code from several places that all descend from this
checkout: the editable pip install, the wire-managed plugin tree
(`~/.minni/plugin/<version>`), and per-platform hook dists. When `main` moves,
none of them move by themselves — that gap is how a merged fix stayed unshipped
for days (2026-08-01 audit, GA1-3/GA5-1), with every health surface silent.

Two mechanisms close it. The first is on-demand and safe to run any time; the
second is optional automation that only an operator may activate.

## 1. `make sync-root` (on demand)

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
   `com.minni.minnid` agent is loaded. If the agent is **not** loaded, the
   script still runs step 6 (so you see WORKTREE/BADCONFIG from this run) and
   then exits non-zero with “redeployed … but daemon was not restarted” —
   it never prints `sync complete` without a bounced daemon.
6. Verifies with `scripts/check_versions.py` and
   `scripts/check_deployments.py --strict`, so the run fails loudly if the
   fleet still disagrees afterward. After a successful kickstart it also
   probes the daemon socket for `deploy.stale`.

**Antigravity half-state (D11):** if `wire antigravity` fails on hook
registration (`agy` is on PATH but `agy plugin install` fails), MCP views may
already be updated while hooks are not. Fix `agy`, then re-run
`minni wire antigravity --from-repo .` (or `make sync-root`) to complete hooks.
Wire records the install root for GC protection and names the hook gap in the
platform result.

The daemon's `status` response carries a `deploy` block
(`src/minni/minnid_runtime/deploy_honesty.py`) that reports when the running
process or the deployed plugin dist is stale relative to the checkout — that
signal is what tells you a sync (and daemon restart) is due.

## 2. Scheduled sync (`com.minni.sync-root.plist.template`) — NOT installed

The template in this directory runs the same script on an interval. It is
**deliberately not installed by anything in this repo** — loading it means the
machine deploys `origin/main` unattended, and only the operator gets to decide
that. The refusal rules above still hold on every unattended run: dirt or
divergence aborts the sync; it never force-updates.

To activate (operator only):

```sh
mkdir -p "$HOME/.minni/logs"
sed -e "s|__REPO__|$HOME/Projects/Minni|g" \
    -e "s|__HOME__|$HOME|g" \
    deploy/com.minni.sync-root.plist.template \
    > "$HOME/Library/LaunchAgents/com.minni.sync-root.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.minni.sync-root.plist"
```

To check / disable:

```sh
launchctl print "gui/$(id -u)/com.minni.sync-root"   # status + last exit code
launchctl bootout "gui/$(id -u)/com.minni.sync-root" # unload
rm "$HOME/Library/LaunchAgents/com.minni.sync-root.plist"
```

Logs land in `$HOME/.minni/logs/sync-root.log` / `sync-root.err.log`. A run
that refused (dirty/diverged checkout) exits 1 and says why in the err log.

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

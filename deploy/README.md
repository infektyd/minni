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
make sync-root DRY_RUN=1  # print the plan, execute nothing
```

Runs `scripts/update_root.sh`, which loudly and idempotently:

1. `git fetch origin`, then fast-forwards the checkout to `origin/main`.
   **Refuses** on a dirty tree, a non-`main` branch, or local commits
   `origin/main` lacks — it never discards or rewrites local state.
2. Refreshes the editable pip install.
3. Rebuilds the plugin (`npm run build`).
4. Redeploys the platform surfaces: `minni wire claude-code --from-repo` plus
   `propagate.py update-plugin --platform all`.
5. Restarts `minnid` via `launchctl kickstart` when the
   `com.minni.minnid` agent is loaded (otherwise it tells you to restart
   however you run it).
6. Verifies with `scripts/check_versions.py` and
   `scripts/check_deployments.py --strict`, so the run fails loudly if the
   fleet still disagrees afterward.

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

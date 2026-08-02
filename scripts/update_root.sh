#!/bin/bash
# update_root.sh — bring the live Minni install up to origin/main, loudly.
#
# The live machine runs code from this checkout (editable pip install, wire-
# managed plugin tree, per-platform hook dists). Nothing used to move any of
# that when main advanced, so merged fixes sat unshipped until someone
# remembered (audit GA1-3/GA5-1). This script is that mechanism:
#
#   1. git fetch origin, then fast-forward the checkout to origin/main.
#      REFUSES on a dirty tree or a diverged branch — it never discards
#      local state; you resolve, it retries.
#   2. Refresh the editable pip install (dependency/metadata changes).
#   3. Rebuild the plugin (npm run build).
#   4. Redeploy the platform surfaces: `minni wire claude-code --from-repo`
#      plus `propagate.py update-plugin --platform all`.
#   5. Restart the minni daemon (launchd kickstart when the agent is loaded).
#   6. Verify: check_versions.py + check_deployments.py --strict.
#
# Idempotent: a second run on an already-synced checkout does no harm and says
# so. Every step is announced; --dry-run prints the plan without executing.
#
# Usage: scripts/update_root.sh [--dry-run] [--repo <path>]
#   MINNI_SYNC_REPO   overrides the repo root (default: the checkout containing
#                     this script).
set -euo pipefail

DRY_RUN=0
REPO="${MINNI_SYNC_REPO:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --repo) REPO="$2"; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "update-root: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$REPO" ]; then
  REPO="$(cd "$(dirname "$0")/.." && pwd)"
fi
cd "$REPO"

say()  { printf '\n== %s\n' "$*"; }
act()  {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'would run: %s\n' "$*"
  else
    printf 'running:   %s\n' "$*"
    "$@"
  fi
}
refuse() { printf 'update-root: REFUSING: %s\n' "$*" >&2; exit 1; }

[ -d .git ] || refuse "$REPO is not a git checkout"

VENV_PY="$REPO/.venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="python3"

say "sync $REPO -> origin/main$([ "$DRY_RUN" = 1 ] && echo '  [DRY RUN]')"

# ── 1. fetch + fast-forward, refusing to touch local state ───────────────────
say "step 1/6: git fetch + fast-forward"
act git fetch origin

if [ -n "$(git status --porcelain)" ]; then
  git status --short >&2
  refuse "working tree is dirty — commit, stash, or clean it first"
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
  refuse "checkout is on branch '$BRANCH', not main — switch branches yourself"
fi

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/main)"
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "already at origin/main ($(git rev-parse --short HEAD)) — continuing to redeploy/verify (idempotent)"
elif [ "$(git merge-base HEAD origin/main)" != "$LOCAL" ]; then
  refuse "local main has commits origin/main lacks (diverged) — reconcile yourself; this script never rewrites local history"
else
  act git merge --ff-only origin/main
fi

# ── 2. editable install refresh ──────────────────────────────────────────────
say "step 2/6: refresh editable pip install"
act "$VENV_PY" -m pip install --no-deps -q -e .

# ── 3. plugin build ──────────────────────────────────────────────────────────
say "step 3/6: rebuild plugin"
act npm --prefix plugins/minni run build

# ── 4. redeploy platform surfaces ────────────────────────────────────────────
say "step 4/6: redeploy platform surfaces"
act "$VENV_PY" -m minni.minni_cli wire claude-code --from-repo "$REPO"
act "$VENV_PY" plugins/minni/skills/minni-install/scripts/propagate.py \
  --repo "$REPO" update-plugin --platform all --no-build

# ── 5. restart the daemon ────────────────────────────────────────────────────
say "step 5/6: restart minnid"
LABEL="com.minni.minnid"
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  act launchctl kickstart -k "gui/$(id -u)/$LABEL"
else
  echo "launchd agent $LABEL is not loaded — restart minnid however you run it"
fi

# ── 6. verify ────────────────────────────────────────────────────────────────
say "step 6/6: verify versions + deployments"
act "$VENV_PY" scripts/check_versions.py
act "$VENV_PY" scripts/check_deployments.py --strict

say "sync complete at $(git rev-parse --short HEAD)"

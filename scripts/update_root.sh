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
#   2. Refresh locked dependencies (requirements.lock) and the editable
#      pip install.
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

# `.git` is a directory in a normal clone and a *file* (gitdir pointer) in a
# linked worktree. `-d` alone refuses every worktree checkout.
[ -e .git ] || refuse "$REPO is not a git checkout"

VENV_PY="$REPO/.venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="python3"

say "sync $REPO -> origin/main$([ "$DRY_RUN" = 1 ] && echo '  [DRY RUN]')"

# ── 1. fetch + fast-forward, refusing to touch local state ───────────────────
say "step 1/6: git fetch + fast-forward"
# fetch runs even under --dry-run: it only updates remote-tracking refs (never
# the local branch or worktree), and without it the LOCAL/REMOTE comparison
# below reads stale refs — a behind clone would print a plan that omits the
# merge step entirely.
printf 'running:   git fetch origin\n'
git fetch origin

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

# ── 2. dependency + editable install refresh ─────────────────────────────────
say "step 2/6: refresh locked dependencies + editable pip install"
# Both halves, matching the Makefile's setup pattern: --no-deps alone would
# skip a dependency main just added to requirements.lock, and the daemon
# restarted below would then fail to import it.
act "$VENV_PY" -m pip install -q -r requirements.lock
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
SOCKET="${MINNI_SOCKET:-$HOME/.minni/run/minnid.sock}"
DAEMON_RESTARTED=0
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  act launchctl kickstart -k "gui/$(id -u)/$LABEL"
  DAEMON_RESTARTED=1
else
  # Do not claim "sync complete" when the live process was not bounced —
  # checkout/plugin trees may be current while minnid still runs pre-sync code
  # (the GA1-3 failure mode this path exists to close).
  if [ "$DRY_RUN" = 1 ]; then
    echo "would fail: launchd agent $LABEL is not loaded — daemon would not be restarted"
  else
    refuse "launchd agent $LABEL is not loaded — daemon was not restarted; start/restart minnid yourself (or load the launchd agent) and re-run"
  fi
fi

# ── 6. verify ────────────────────────────────────────────────────────────────
say "step 6/6: verify versions + deployments"
act "$VENV_PY" scripts/check_versions.py
act "$VENV_PY" scripts/check_deployments.py --strict

# After a real restart, require the socket to answer and report deploy.stale
# is not True. Skip under dry-run (kickstart was not executed).
if [ "$DRY_RUN" != 1 ] && [ "$DAEMON_RESTARTED" = 1 ]; then
  say "step 6b: probe daemon deploy honesty after restart"
  if ! "$VENV_PY" - "$SOCKET" <<'PY'
import json, socket, sys, time
sock_path = sys.argv[1]
last = None
for _ in range(15):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock_path)
            req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "status"}).encode()
            s.sendall(req + b"\n")
            buf = b""
            while b"\n" not in buf and len(buf) < 1_000_000:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        msg = json.loads(buf.decode("utf-8", errors="replace").splitlines()[0])
        result = msg.get("result") or {}
        deploy = (result.get("daemon") or {}).get("deploy") or result.get("deploy") or {}
        stale = deploy.get("stale")
        if stale is True:
            print(
                f"update-root: daemon deploy.stale is True after restart: {deploy!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        print(f"daemon deploy probe ok (stale={stale!r})")
        sys.exit(0)
    except Exception as exc:
        last = exc
        time.sleep(0.4)
print(f"update-root: daemon socket unreachable after restart ({sock_path}): {last}", file=sys.stderr)
sys.exit(1)
PY
  then
    refuse "daemon did not come back clean after launchd kickstart (socket $SOCKET)"
  fi
fi

say "sync complete at $(git rev-parse --short HEAD)"

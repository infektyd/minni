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
#   4. Redeploy with the D7 fleet partition: `minni wire all --from-repo`
#      (codex/claude-code/kilocode/grok), then `propagate update-plugin` for
#      antigravity + cursor only, plus grok hooks/rules against the active
#      wire install root — never `propagate --platform all` (that rewrites
#      wire-managed MCP paths back onto legacy cache trees).
#   5. Restart the minni daemon (launchd kickstart when the agent is loaded).
#   6. Verify: check_versions.py + check_deployments.py --strict
#      (also evaluated read-only under --dry-run).
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

# Mutual exclusion: concurrent syncs interleave pip/npm/wire/kickstart.
# mkdir is atomic and portable (macOS has no util-linux flock by default).
# Stale recovery: if the lock holds a PID that is no longer alive (SIGKILL /
# OOM / sleep-kill skipped the EXIT trap), reclaim so unattended sync cannot
# brick itself forever.
LOCKDIR="${MINNI_SYNC_LOCKDIR:-$HOME/.minni/run/sync-root.lockdir}"
mkdir -p "$(dirname "$LOCKDIR")"
_claim_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCKDIR/pid"
    return 0
  fi
  return 1
}
if ! _claim_lock; then
  old_pid=""
  if [ -f "$LOCKDIR/pid" ]; then
    old_pid="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  fi
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    refuse "another sync-root is already running (pid $old_pid, lock $LOCKDIR)"
  fi
  echo "update-root: reclaiming stale sync lock${old_pid:+ (dead pid $old_pid)} at $LOCKDIR" >&2
  rm -rf "$LOCKDIR"
  if ! _claim_lock; then
    refuse "another sync-root is already running (lock $LOCKDIR)"
  fi
fi
# shellcheck disable=SC2064
trap 'rm -rf "$LOCKDIR" 2>/dev/null || true' EXIT INT TERM

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
# Fleet partition (D7): wire owns codex/claude-code/kilocode/grok (points MCP
# at ~/.minni/plugin/<ver>). propagate owns antigravity + cursor. Never run
# `propagate --platform all` after a partial wire — that rewrites codex/kilo/
# grok back onto legacy cache trees and undoes wire adoption every sync.
#
# wire all partial (missing config root for one host) must NOT abort under
# set -e: still propagate antigravity/cursor, restart, and verify, then refuse
# at the end if anything failed.
say "step 4/6: redeploy platform surfaces (wire all + propagate antigravity/cursor)"
REDEPLOY_EXIT=0
if [ "$DRY_RUN" = 1 ]; then
  act "$VENV_PY" -m minni.minni_cli wire all --from-repo "$REPO"
else
  printf 'running:   %s -m minni.minni_cli wire all --from-repo %s\n' "$VENV_PY" "$REPO"
  if ! "$VENV_PY" -m minni.minni_cli wire all --from-repo "$REPO"; then
    echo "update-root: wire all reported failures — continuing redeploy/verify" >&2
    REDEPLOY_EXIT=1
  fi
fi
if [ "$DRY_RUN" = 1 ]; then
  act "$VENV_PY" plugins/minni/skills/minni-install/scripts/propagate.py \
    --repo "$REPO" update-plugin --platform antigravity --no-build
  act "$VENV_PY" plugins/minni/skills/minni-install/scripts/propagate.py \
    --repo "$REPO" update-plugin --platform cursor --no-build
else
  printf 'running:   propagate update-plugin --platform antigravity\n'
  if ! "$VENV_PY" plugins/minni/skills/minni-install/scripts/propagate.py \
      --repo "$REPO" update-plugin --platform antigravity --no-build; then
    echo "update-root: propagate antigravity failed — continuing" >&2
    REDEPLOY_EXIT=1
  fi
  printf 'running:   propagate update-plugin --platform cursor\n'
  if ! "$VENV_PY" plugins/minni/skills/minni-install/scripts/propagate.py \
      --repo "$REPO" update-plugin --platform cursor --no-build; then
    echo "update-root: propagate cursor failed — continuing" >&2
    REDEPLOY_EXIT=1
  fi
fi
# Grok hooks/rules are propagate-only (wire does not install them) but a full
# `propagate --platform grok` would re-stamp MCP onto the legacy agents tree.
# Refresh hooks/rules against the active wire install root only — failure is
# a redeploy failure (not "sync complete").
if [ "$DRY_RUN" = 1 ]; then
  printf 'would run: refresh grok hooks/rules from active wire install root\n'
else
  printf 'running:   refresh grok hooks/rules from active wire install root\n'
  if ! "$VENV_PY" - "$REPO" <<'PY'
import json, sys
from pathlib import Path
repo = Path(sys.argv[1])
prop = repo / "plugins/minni/skills/minni-install/scripts/propagate.py"
import importlib.util
spec = importlib.util.spec_from_file_location("minni_propagate", prop)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)
home = Path.home()
base = home / ".minni" / "plugin"
root = None
wired = base / "wired.json"
try:
    data = json.loads(wired.read_text(encoding="utf-8"))
    entries = [
        (str(w.get("wired_at") or ""), Path(str(w.get("install_root"))))
        for w in data.get("wires", [])
        if isinstance(w, dict) and w.get("install_root")
    ]
    entries = [(t, p) for t, p in entries if p.is_dir()]
    if entries:
        root = max(entries, key=lambda t: t[0])[1]
except Exception:
    root = None
if root is None:
    current = base / "current"
    root = current.resolve() if current.exists() else None
if root is None:
    print("no wire install root for grok hooks", file=sys.stderr)
    sys.exit(1)
hooks = mod.update_grok_hooks(root)
rules = mod.write_grok_rules()
print("grok hooks:", hooks)
print("grok rules:", rules)
if not hooks.get("installed") or not rules.get("installed"):
    sys.exit(1)
PY
  then
    echo "update-root: grok hooks/rules refresh failed — will not report sync complete" >&2
    REDEPLOY_EXIT=1
  fi
fi

# ── 5. restart the daemon ────────────────────────────────────────────────────
say "step 5/6: restart minnid"
LABEL="com.minni.minnid"
SOCKET="${MINNI_SOCKET:-$HOME/.minni/run/minnid.sock}"
DAEMON_RESTARTED=0
DAEMON_MISSING=0
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  # kickstart must not abort under set -e before step 6 — a failed bounce
  # still needs version/deployment verify (same as the missing-agent path).
  if [ "$DRY_RUN" = 1 ]; then
    echo "would run: launchctl kickstart -k gui/$(id -u)/$LABEL"
    DAEMON_RESTARTED=1
  elif launchctl kickstart -k "gui/$(id -u)/$LABEL"; then
    DAEMON_RESTARTED=1
  else
    echo "update-root: launchctl kickstart failed — continuing to verify (will not report sync complete)" >&2
    DAEMON_MISSING=1
  fi
else
  # Still run step 6 (versions + D14 deployment gates) so the operator sees
  # WORKTREE/BADCONFIG from this run. Do NOT claim "sync complete" later —
  # checkout/plugin trees may be current while minnid still runs pre-sync code.
  DAEMON_MISSING=1
  if [ "$DRY_RUN" = 1 ]; then
    echo "would fail: launchd agent $LABEL is not loaded — daemon would not be restarted"
  else
    echo "update-root: launchd agent $LABEL is not loaded — restart minnid however you run it; continuing to verify (will not report sync complete)" >&2
  fi
fi

# ── 6. verify ────────────────────────────────────────────────────────────────
say "step 6/6: verify versions + deployments"
VERIFY_EXIT=0
if [ ! -f "$REPO/scripts/check_versions.py" ] || [ ! -f "$REPO/scripts/check_deployments.py" ]; then
  # Throwaway test clones (and any non-minni tree) lack these scripts.
  if [ "$DRY_RUN" = 1 ]; then
    echo "would run: check_versions + check_deployments --strict (scripts not present in this checkout — skipped)"
  else
    refuse "checkout is missing scripts/check_versions.py or check_deployments.py — is REPO a Minni tree?"
  fi
else
  if [ "$DRY_RUN" = 1 ]; then
    # Current-state visibility only. Do not fold pre-sync WORKTREE/BADCONFIG
    # into "plan would FAIL" — the real run redeploys first, then verifies.
    printf 'current state (not a plan gate): %s scripts/check_versions.py\n' "$VENV_PY"
    "$VENV_PY" scripts/check_versions.py || true
    printf 'current state (not a plan gate): %s scripts/check_deployments.py --strict\n' "$VENV_PY"
    "$VENV_PY" scripts/check_deployments.py --strict || true
  else
    printf 'running:   %s scripts/check_versions.py\n' "$VENV_PY"
    if ! "$VENV_PY" scripts/check_versions.py; then
      VERIFY_EXIT=1
    fi
    printf 'running:   %s scripts/check_deployments.py --strict\n' "$VENV_PY"
    if ! "$VENV_PY" scripts/check_deployments.py --strict; then
      VERIFY_EXIT=1
    fi
  fi
fi

# After a real restart, require the socket to answer and report known-stale
# is not True. Unmeasurable (None) is a soft warning — kickstart can race
# slow imports. Skip under dry-run (kickstart was not executed).
if [ "$DRY_RUN" != 1 ] && [ "$DAEMON_RESTARTED" = 1 ]; then
  say "step 6b: probe daemon deploy honesty after restart"
  if ! "$VENV_PY" - "$SOCKET" <<'PY'
import json, socket, sys, time
sock_path = sys.argv[1]
last = None
# ~45s: kickstart + DB migrate / import can exceed a few seconds.
for i in range(45):
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
        plugin = deploy.get("plugin_dist") if isinstance(deploy.get("plugin_dist"), dict) else {}
        plugin_stale = plugin.get("stale")
        if stale is True or plugin_stale is True:
            print(
                f"update-root: daemon deploy honesty still stale after restart: {deploy!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        if stale is None:
            # Soft: socket is up; honesty unmeasurable (manifest race / layout).
            print(
                f"daemon deploy probe soft-ok (stale unmeasurable): {deploy!r}",
            )
            sys.exit(0)
        print(f"daemon deploy probe ok (stale={stale!r}, plugin_dist.stale={plugin_stale!r})")
        sys.exit(0)
    except Exception as exc:
        last = exc
        time.sleep(1.0)
print(f"update-root: daemon socket unreachable after restart ({sock_path}): {last}", file=sys.stderr)
sys.exit(1)
PY
  then
    VERIFY_EXIT=1
    echo "update-root: daemon did not come back clean after launchd kickstart (socket $SOCKET)" >&2
  fi
fi

# Final status: never print "sync complete" unless redeploy + daemon restart +
# verify all succeeded. Dry-run exits non-zero only when the *plan* would fail
# (e.g. launchd agent not loaded) — not because pre-sync hygiene is dirty.
if [ "$DRY_RUN" = 1 ]; then
  if [ "$DAEMON_MISSING" = 1 ]; then
    echo "dry-run plan would FAIL (daemon would not be restarted)" >&2
    exit 1
  fi
  say "dry-run plan complete (no changes applied; current-state checkers are informational)"
  exit 0
fi

if [ "$REDEPLOY_EXIT" != 0 ]; then
  refuse "redeploy reported failures (wire/propagate/grok-hooks) — see messages above"
fi

if [ "$VERIFY_EXIT" != 0 ]; then
  refuse "verification failed after redeploy"
fi

if [ "$DAEMON_MISSING" = 1 ]; then
  refuse "redeployed and verified, but daemon was not restarted (launchd agent $LABEL not loaded) — bounce minnid yourself"
fi

say "sync complete at $(git rev-parse --short HEAD)"

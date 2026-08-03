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
#      wire install root. `propagate --platform all` expands only those two
#      (wire platforms are named skips); avoid explicit codex|kilocode|grok
#      propagate after wire (those still rewrite MCP onto legacy trees).
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
# Reclaim is race-safe: never `rm -rf` a live peer's lock. Move the stale
# dir aside (atomic rename), verify the moved pid was still dead/empty, then
# claim and re-read `$LOCKDIR/pid` must equal `$$`. EXIT releases only if we
# still own the lock (so concurrent reclaimers cannot wipe a winner).
LOCKDIR="${MINNI_SYNC_LOCKDIR:-$HOME/.minni/run/sync-root.lockdir}"
# Token written into the lock so a reused PID that is *not* this script does
# not brick unattended sync forever (kill -0 alone is insufficient).
_LOCK_MARKER="update_root.sh"
mkdir -p "$(dirname "$LOCKDIR")"
_claim_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCKDIR/pid"
    printf '%s\n' "$_LOCK_MARKER" >"$LOCKDIR/cmd"
    return 0
  fi
  return 1
}
_own_lock() {
  [ -f "$LOCKDIR/pid" ] || return 1
  [ "$(cat "$LOCKDIR/pid" 2>/dev/null || true)" = "$$" ]
}
_release_lock() {
  if _own_lock; then
    rm -rf "$LOCKDIR" 2>/dev/null || true
  fi
}
# True when pid is alive *and* looks like a real sync-root holder.
# A live PID whose argv is not update_root.sh is treated as PID reuse (stale).
_lock_holder_is_live_sync() {
  local pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # Prefer the marker we wrote; fall back to ps argv fingerprint.
  local marker
  marker="$(cat "$LOCKDIR/cmd" 2>/dev/null || true)"
  if [ -n "$marker" ] && [ "$marker" != "$_LOCK_MARKER" ]; then
    return 1
  fi
  local cmd
  # portable: macOS/BSD and Linux both accept -p/-o command=
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$cmd" in
    *update_root.sh*|*sync-root*) return 0 ;;
    "")
      # ps failed (permission / race): if marker matches and pid lives, assume holder
      [ "$marker" = "$_LOCK_MARKER" ]
      return $?
      ;;
    *) return 1 ;;
  esac
}
if ! _claim_lock; then
  old_pid=""
  if [ -f "$LOCKDIR/pid" ]; then
    old_pid="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  fi
  if _lock_holder_is_live_sync "$old_pid"; then
    refuse "another sync-root is already running (pid $old_pid, lock $LOCKDIR)"
  fi
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "update-root: reclaiming stale sync lock (pid $old_pid live but not update_root.sh — PID reuse) at $LOCKDIR" >&2
  else
    echo "update-root: reclaiming stale sync lock${old_pid:+ (dead pid $old_pid)} at $LOCKDIR" >&2
  fi
  # Atomic rename beats rm-then-mkdir: only one reclaimer moves the dir;
  # the other either claims the free name or sees the winner's live pid.
  stale_bak="${LOCKDIR}.reclaim.$$"
  if [ -d "$LOCKDIR" ]; then
    if mv "$LOCKDIR" "$stale_bak" 2>/dev/null; then
      moved_pid="$(cat "$stale_bak/pid" 2>/dev/null || true)"
      # If we accidentally moved a live holder's lock (TOCTOU after the
      # kill -0 check), put it back and refuse.
      if [ -n "$moved_pid" ] && [ "$moved_pid" != "$$" ]; then
        # Temporarily restore path so _lock_holder_is_live_sync can read marker
        if mv "$stale_bak" "$LOCKDIR" 2>/dev/null; then
          if _lock_holder_is_live_sync "$moved_pid"; then
            refuse "another sync-root is already running (pid $moved_pid, lock $LOCKDIR)"
          fi
          # Not a real holder — re-move and reclaim
          mv "$LOCKDIR" "$stale_bak" 2>/dev/null || true
        fi
      fi
      rm -rf "$stale_bak" 2>/dev/null || true
    fi
  fi
  if ! _claim_lock; then
    refuse "another sync-root is already running (lock $LOCKDIR)"
  fi
  if ! _own_lock; then
    refuse "failed to confirm sync lock ownership at $LOCKDIR"
  fi
fi
# shellcheck disable=SC2064
trap '_release_lock' EXIT INT TERM

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
# at ~/.minni/plugin/<ver>). propagate owns antigravity + cursor. `all` expands
# only those two (wire platforms are named skips). Do not re-run explicit
# update-plugin --platform codex|kilocode|grok after wire — those still
# rewrite MCP onto legacy cache trees and undo wire adoption.
#
# wire all partial (missing config root for one host) must NOT abort under
# set -e: still propagate antigravity/cursor, restart, and verify, then refuse
# at the end if anything failed.
say "step 4/6: redeploy platform surfaces (wire all + propagate antigravity/cursor)"
REDEPLOY_EXIT=0
if [ "$DRY_RUN" = 1 ]; then
  act "$VENV_PY" -m minni.minni_cli wire all --from-repo "$REPO" --prune
else
  # --prune: non-TTY automation otherwise skips GC and leaves historical
  # +git.* dirs that make check_deployments --strict fail forever.
  printf 'running:   %s -m minni.minni_cli wire all --from-repo %s --prune\n' "$VENV_PY" "$REPO"
  # Capture JSON so an all-skipped run (D5 exit 1: no wire-managed hosts on
  # this machine) is not treated as redeploy failure — propagate still owns
  # antigravity/cursor. failed/partial still set REDEPLOY_EXIT.
  _WIRE_JSON="$(mktemp "${TMPDIR:-/tmp}/minni-wire.XXXXXX")"
  set +e
  "$VENV_PY" -m minni.minni_cli wire all --from-repo "$REPO" --prune >"$_WIRE_JSON"
  _WIRE_RC=$?
  set -e
  cat "$_WIRE_JSON" || true
  if [ "$_WIRE_RC" -ne 0 ]; then
    # Decode the *last* JSON object: --from-repo may still leave noise on
    # stdout if an older from_repo path leaks npm banners before WireOutput.
    _WIRE_STATUS="$("$VENV_PY" -c "
import json, sys
text = open(sys.argv[1], encoding='utf-8', errors='replace').read()
doc = None

def _is_wire_output(d):
    # WireOutput.emit shape (pretty or compact). Nested 'gc':{} must not win.
    return (
        isinstance(d, dict)
        and 'schema' in d
        and 'status' in d
        and 'results' in d
    )

# Prefer pure JSON; else walk brace positions for a WireOutput-shaped dict
# (pretty emit ends with 'gc': {} — last '{' alone is wrong). Prefer the *last*
# WireOutput-shaped object when noise embeds an earlier partial/JSON fragment.
try:
    cand = json.loads(text)
    if _is_wire_output(cand):
        doc = cand
except Exception:
    pass
if doc is None:
    idx = text.find('{')
    while idx >= 0:
        try:
            cand = json.loads(text[idx:])
            if _is_wire_output(cand):
                doc = cand  # keep scanning; last match wins
        except Exception:
            pass
        idx = text.find('{', idx + 1)
if not isinstance(doc, dict):
    print('unparseable')
else:
    print(doc.get('status') or 'unknown')
" "$_WIRE_JSON" 2>/dev/null || echo unparseable)"
    if [ "$_WIRE_STATUS" = "skipped" ]; then
      echo "update-root: wire all status=skipped (no wire-managed host surfaces) — continuing with propagate" >&2
    else
      echo "update-root: wire all reported failures (status=${_WIRE_STATUS}) — continuing redeploy/verify" >&2
      REDEPLOY_EXIT=1
    fi
  fi
  rm -f "$_WIRE_JSON"
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
    # Prefer the latest *grok* wire only — global max wired_at after a partial
    # wire all can pick codex/claude-code's new root while grok MCP still
    # points at an older tree (hooks would stamp the wrong dist paths).
    grok_entries = [
        (str(w.get("wired_at") or ""), Path(str(w.get("install_root"))))
        for w in data.get("wires", [])
        if isinstance(w, dict)
        and w.get("install_root")
        and str(w.get("platform") or "") == "grok"
    ]
    grok_entries = [(t, p) for t, p in grok_entries if p.is_dir()]
    if grok_entries:
        root = max(grok_entries, key=lambda t: t[0])[1]
except Exception:
    root = None
if root is None:
    current = base / "current"
    root = current.resolve() if current.exists() else None
if root is None:
    # No usable grok wire install root (and no current). Leftover ~/.grok
    # alone is not enough to require hooks refresh — skip loud, do not fail
    # redeploy for an optional surface.
    print(
        "skip: no grok wire install root (and no ~/.minni/plugin/current) "
        "— hooks refresh not needed"
    )
    sys.exit(0)
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
DAEMON_NOT_LOADED=0
DAEMON_RESTART_FAILED=0
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  # kickstart must not abort under set -e before step 6 — a failed bounce
  # still needs version/deployment verify (same as the missing-agent path).
  if [ "$DRY_RUN" = 1 ]; then
    echo "would run: launchctl kickstart -k gui/$(id -u)/$LABEL"
    DAEMON_RESTARTED=1
  elif launchctl kickstart -k "gui/$(id -u)/$LABEL"; then
    DAEMON_RESTARTED=1
  else
    echo "update-root: launchctl kickstart failed (agent is loaded) — will probe existing socket and continue verify (will not report sync complete)" >&2
    DAEMON_RESTART_FAILED=1
  fi
else
  # Still run step 6 (versions + D14 deployment gates) so the operator sees
  # WORKTREE/BADCONFIG from this run. Do NOT claim "sync complete" later —
  # checkout/plugin trees may be current while minnid still runs pre-sync code.
  DAEMON_NOT_LOADED=1
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
    # Skip in-repo stage-payload tree: sync does not refresh it; leftover
    # src/minni/plugin_payload must not fail day-to-day root sync.
    MINNI_CHECK_DEPLOYMENTS_SKIP_REPO=1 \
      "$VENV_PY" scripts/check_deployments.py --strict || true
  else
    printf 'running:   %s scripts/check_versions.py\n' "$VENV_PY"
    if ! "$VENV_PY" scripts/check_versions.py; then
      VERIFY_EXIT=1
    fi
    printf 'running:   %s scripts/check_deployments.py --strict\n' "$VENV_PY"
    if ! MINNI_CHECK_DEPLOYMENTS_SKIP_REPO=1 \
        "$VENV_PY" scripts/check_deployments.py --strict; then
      VERIFY_EXIT=1
    fi
  fi
fi

# After a real restart (or a failed kickstart with agent still loaded), require
# the socket to answer and report known-stale is not True. For editable
# checkouts, unmeasurable stale hard-fails; wheel installs soft-ok. Skip under
# dry-run (kickstart was not executed).
if [ "$DRY_RUN" != 1 ] && { [ "$DAEMON_RESTARTED" = 1 ] || [ "$DAEMON_RESTART_FAILED" = 1 ]; }; then
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
        # Do not default missing deploy to {} — empty would soft-ok on stale=None.
        deploy = (result.get("daemon") or {}).get("deploy") or result.get("deploy")
        if not isinstance(deploy, dict) or not deploy:
            print(
                "update-root: daemon status missing deploy block after restart "
                f"(result keys={list((result.get('daemon') or result or {}).keys())!r})",
                file=sys.stderr,
            )
            sys.exit(4)
        if deploy.get("error"):
            print(
                f"update-root: daemon deploy honesty errored after restart: {deploy!r}",
                file=sys.stderr,
            )
            sys.exit(5)
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
            # Wheel installs have no local checkout — unmeasurable is expected.
            # Editable checkout after kickstart must report a boolean; null means
            # the honesty path could not measure (git race / unreadable) and must
            # not green-wash "daemon came back clean".
            if deploy.get("install_kind") == "wheel":
                print(
                    f"daemon deploy probe soft-ok (wheel, stale unmeasurable): {deploy!r}",
                    file=sys.stderr,
                )
                sys.exit(0)
            print(
                f"update-root: daemon deploy honesty unmeasurable after restart "
                f"(expected boolean for editable checkout): {deploy!r}",
                file=sys.stderr,
            )
            sys.exit(3)
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
  if [ "$DAEMON_NOT_LOADED" = 1 ]; then
    echo "dry-run plan would FAIL (launchd agent not loaded — daemon would not be restarted)" >&2
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

if [ "$DAEMON_NOT_LOADED" = 1 ]; then
  refuse "redeployed and verified, but daemon was not restarted (launchd agent $LABEL not loaded) — bounce minnid yourself"
fi

if [ "$DAEMON_RESTART_FAILED" = 1 ]; then
  refuse "redeployed and verified, but launchctl kickstart failed (agent $LABEL is loaded) — bounce minnid yourself; deploy probe above reports whether the still-running process is stale"
fi

say "sync complete at $(git rev-parse --short HEAD)"

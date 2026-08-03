"""Deploy honesty: does the RUNNING daemon code match the repo it came from?

Audit GA1-3/GA5-1 (2026-08-01): the version/deployment checkers were
manual-only scripts, so a daemon could run code days behind a merged fix and
no health surface ever said so. This module gives `status` a truthful
"you are running stale code" signal, from LOCAL comparisons only — no network:

  - At daemon start, capture where the running `minni` package was imported
    from and, when that is a git checkout (editable install), its HEAD sha and
    dirty state.
  - At status time, compare the captured sha against the checkout's CURRENT
    HEAD. A moved HEAD means the process is executing code from a commit the
    checkout has left behind.
  - Also compare the wire-managed plugin payload (~/.minni/plugin/current)
    against the checkout HEAD via its payload-manifest git_sha — the cheap,
    always-on version of what scripts/check_deployments.py measures on demand.

`stale` is only ever True on evidence; anything unmeasurable reports
stale=None with the reason, never a guess.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Optional

_START_STATE: Optional[dict] = None

# Current-HEAD lookups shell out to git; cache briefly so a health poller
# cannot turn status into a git-subprocess loop.
_HEAD_CACHE: dict[str, tuple[float, Optional[str]]] = {}
_HEAD_CACHE_TTL = 10.0


def _git(args: list[str], cwd: Path) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=str(cwd), text=True,
            stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return None


def _source_checkout() -> Optional[Path]:
    """Git checkout of an *editable Minni* install, or None for wheels.

    Must not treat any ancestor ``.git`` as Minni: a wheel living under a
    project venv (``~/myapp/.venv/.../site-packages/minni``) or under a
    git-tracked ``$HOME`` would otherwise report ``editable-checkout`` and
    measure *that* repo's HEAD — false ``deploy.stale: true`` on every
    unrelated commit. Only accept the standard editable layout:

      <repo>/src/minni/__init__.py  +  <repo>/pyproject.toml names ``minni``
      +  <repo> is a git checkout
    """
    import re
    import minni

    pkg = Path(minni.__file__).resolve().parent
    if pkg.name != "minni" or pkg.parent.name != "src":
        return None
    repo = pkg.parent.parent
    if not (repo / ".git").exists():
        return None
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    if not re.search(r'(?m)^name\s*=\s*["\']minni["\']', text):
        return None
    return repo


def _current_head(checkout: Path) -> Optional[str]:
    key = str(checkout)
    now = time.monotonic()
    cached = _HEAD_CACHE.get(key)
    if cached and now - cached[0] < _HEAD_CACHE_TTL:
        return cached[1]
    head = _git(["rev-parse", "HEAD"], checkout)
    _HEAD_CACHE[key] = (now, head)
    return head


def capture_start_state() -> dict:
    """Record, at daemon start, which code this process is actually running.

    Called from minnid.main(); falls back lazily on first status call so
    directly-constructed test contexts still get a coherent answer.
    """
    global _START_STATE
    state: dict = {"captured_at": time.time()}
    checkout = _source_checkout()
    if checkout is None:
        state["install_kind"] = "wheel"
    else:
        state["install_kind"] = "editable-checkout"
        state["checkout"] = str(checkout)
        state["git_sha"] = _git(["rev-parse", "HEAD"], checkout)
        status_out = _git(["status", "--porcelain"], checkout)
        state["git_dirty"] = None if status_out is None else bool(status_out.strip())
    _START_STATE = state
    return state


def _active_payload_roots() -> list[tuple[Path, str]]:
    """Active wire install roots (latest wired_at per platform).

    Shared with scripts/check_versions.py and scripts/check_deployments.py via
    ``minni.wire.active_roots`` so a half-written install root cannot be
    "active" for one surface and invisible for another. ``current`` is
    fallback only when *no* valid wire records exist (release-only / pre-wire).
    """
    from minni.wire.active_roots import active_wire_plugin_roots_ordered

    return active_wire_plugin_roots_ordered(Path.home())


def _plugin_dist_status(checkout_head: Optional[str]) -> dict:
    """Staleness of active wire-managed plugin payloads, by manifest git_sha.

    ``stale: true`` if *any* active root lags checkout HEAD.
    """
    roots = _active_payload_roots()
    if not roots:
        # Measurable absence: no live wire payload is not "unmeasurable" —
        # process HEAD can still be honest, and sync-root must not hard-fail
        # the probe for hosts that only use propagate-managed surfaces.
        return {
            "stale": False,
            "reason": "no wire-managed plugin payload found",
        }
    lagging: list[str] = []
    unreadable: list[str] = []
    readable = 0
    first_sha = "unknown"
    first_ver = "unknown"
    hows: list[str] = []
    for root, how in roots:
        hows.append(how)
        try:
            manifest = json.loads(
                (root / "payload-manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            # Do not discard lag evidence from peers already inspected.
            unreadable.append(f"{root.name} via {how}: {type(exc).__name__}")
            continue
        readable += 1
        dist_sha = str(manifest.get("git_sha") or "unknown")
        if first_sha == "unknown" and dist_sha != "unknown":
            first_sha = dist_sha
            first_ver = str(manifest.get("version") or "unknown")
        elif first_sha == "unknown":
            first_ver = str(manifest.get("version") or first_ver)
        if checkout_head is not None and dist_sha not in ("unknown", checkout_head):
            lagging.append(f"{root.name}@{dist_sha[:12]} via {how}")
    out: dict = {
        "dist_git_sha": first_sha[:12],
        "dist_version": first_ver,
        "resolved_via": ",".join(hows),
        "active_roots": len(roots),
    }
    # Known lag always wins over "unmeasurable" — any proven lagging root is
    # stale even when a peer has unknown/unreadable sha.
    if lagging:
        out["stale"] = True
        out["lagging"] = lagging
        reason = (
            f"active wire root(s) lag checkout HEAD "
            f"{(checkout_head or 'unknown')[:12]}: "
            f"{'; '.join(lagging)} — re-run `minni wire all` / `make sync-root`"
        )
        if unreadable:
            reason += f"; also unreadable: {'; '.join(unreadable)}"
            out["unreadable"] = unreadable
        out["reason"] = reason
    elif readable > 0 and checkout_head is not None and first_sha != "unknown":
        # At least one root compared cleanly and none lag — healthy even if a
        # peer manifest is unreadable (note the unreadable, do not null out).
        out["stale"] = False
        if unreadable:
            out["unreadable"] = unreadable
            out["reason"] = (
                "active wire roots match HEAD; unreadable peer(s): "
                + "; ".join(unreadable)
            )
    elif unreadable and readable == 0:
        out["stale"] = None
        out["unreadable"] = unreadable
        out["reason"] = (
            "payload-manifest unreadable: " + "; ".join(unreadable)
        )
    elif checkout_head is None or first_sha == "unknown":
        out["stale"] = None
        out["reason"] = "no checkout HEAD or manifest sha to compare against"
        if unreadable:
            out["unreadable"] = unreadable
    else:
        out["stale"] = False
    return out


def deploy_status() -> dict:
    """The `deploy` block for the daemon status surface. Never raises."""
    try:
        started = _START_STATE or capture_start_state()
        out: dict = {"install_kind": started["install_kind"]}
        checkout_head: Optional[str] = None
        if started["install_kind"] == "wheel":
            out["stale"] = None
            out["reason"] = (
                "installed from a wheel; no local checkout to compare against"
            )
        else:
            checkout = Path(started["checkout"])
            started_sha = started.get("git_sha")
            checkout_head = _current_head(checkout)
            out["started_git_sha"] = (started_sha or "unknown")[:12]
            out["current_git_sha"] = (checkout_head or "unknown")[:12]
            out["started_dirty"] = started.get("git_dirty")
            if started_sha is None or checkout_head is None:
                out["stale"] = None
                out["reason"] = "git state unreadable; staleness unmeasurable"
            elif started_sha != checkout_head:
                out["stale"] = True
                out["reason"] = (
                    f"daemon started from {started_sha[:12]} but the checkout "
                    f"is now at {checkout_head[:12]} — the running code is "
                    "stale; restart the daemon (make sync-root does this)"
                )
            elif started.get("git_dirty"):
                # Dirty-at-start is not green: callers that only read the
                # boolean (including sync-root's post-kickstart probe) must
                # not treat "running uncommitted tree" as clean.
                out["stale"] = True
                out["reason"] = (
                    "checkout was dirty at daemon start: the running code "
                    "does not correspond exactly to any commit; restart from "
                    "a clean checkout (make sync-root refuses dirty trees)"
                )
            else:
                out["stale"] = False
        out["plugin_dist"] = _plugin_dist_status(checkout_head)
        # Roll up process + plugin honesty: callers that only read top-level
        # `deploy.stale` must see plugin lag too (README: "process or plugin
        # dist is stale"). Nested plugin_dist remains for detail.
        proc_stale = out.get("stale")
        plugin_stale = out["plugin_dist"].get("stale")
        if proc_stale is True or plugin_stale is True:
            out["stale"] = True
            if proc_stale is not True and plugin_stale is True:
                out["reason"] = (
                    "deployed plugin dist lags checkout HEAD — re-run "
                    "`minni wire` / `make sync-root` "
                    f"({out['plugin_dist'].get('reason', 'plugin_dist stale')})"
                )
        elif proc_stale is False and plugin_stale is False:
            out["stale"] = False
        else:
            # Neither side is known-true; any unmeasurable side keeps null.
            out["stale"] = None
            if "reason" not in out:
                out["reason"] = "process and/or plugin dist staleness unmeasurable"
        return out
    except Exception as exc:  # status must never break on this block
        return {"stale": None, "error": f"{type(exc).__name__}: {exc}"}

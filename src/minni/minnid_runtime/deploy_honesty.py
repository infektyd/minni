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
    """The git checkout the running `minni` package was imported from, if any."""
    import minni

    root = Path(minni.__file__).resolve().parent
    for parent in (root, *root.parents):
        if (parent / ".git").exists():
            return parent
    return None


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

    Reading only the global-newest root greens a partial rewire: codex moves
    to a fresh tree while claude-code still points at an older root, and the
    newest root alone matches HEAD. Mirror check_deployments / check_versions:
    every platform's latest record stays active.

    ``current`` is fallback only when *no* valid wire records exist
    (release-only / pre-wire). Local (+git.*) installs never move ``current``
    (see wire/install.update_current_symlink); treating a leftover release
    symlink as always-active permanently fails sync-root after --from-repo.
    """
    base = Path("~/.minni/plugin").expanduser()
    actives: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    try:
        data = json.loads((base / "wired.json").read_text(encoding="utf-8"))
        latest_by_platform: dict[str, tuple[str, Path]] = {}
        for entry in data.get("wires", []) or []:
            if not isinstance(entry, dict):
                continue
            root_str = entry.get("install_root")
            if not root_str:
                continue
            root = Path(str(root_str))
            if not (root / "payload-manifest.json").is_file():
                continue
            platform = str(entry.get("platform") or "_")
            wired_at = str(entry.get("wired_at") or "")
            prev = latest_by_platform.get(platform)
            if prev is None or wired_at >= prev[0]:
                latest_by_platform[platform] = (wired_at, root.resolve())
        for platform, (_wired_at, root) in sorted(latest_by_platform.items()):
            if root not in seen:
                actives.append((root, f"wired.json:{platform}"))
                seen.add(root)
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    if actives:
        return actives
    # Fallback: release-only machines with no usable wired.json records.
    current = base / "current"
    if (current / "payload-manifest.json").is_file():
        try:
            resolved = current.resolve()
        except OSError:
            resolved = current
        return [(resolved, "current")]
    try:
        candidates = [
            d for d in base.iterdir()
            if d.is_dir() and (d / "payload-manifest.json").is_file()
        ]
    except OSError:
        candidates = []
    if candidates:
        newest = max(candidates, key=lambda d: d.stat().st_mtime)
        return [(newest, "version-dir scan")]
    return []


def _plugin_dist_status(checkout_head: Optional[str]) -> dict:
    """Staleness of active wire-managed plugin payloads, by manifest git_sha.

    ``stale: true`` if *any* active root lags checkout HEAD.
    """
    roots = _active_payload_roots()
    if not roots:
        return {"stale": None, "reason": "no wire-managed plugin payload found"}
    lagging: list[str] = []
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
            return {
                "stale": None,
                "resolved_via": ",".join(hows),
                "reason": f"payload-manifest unreadable: {type(exc).__name__}",
            }
        dist_sha = str(manifest.get("git_sha") or "unknown")
        if first_sha == "unknown":
            first_sha = dist_sha
            first_ver = str(manifest.get("version") or "unknown")
        if checkout_head is not None and dist_sha not in ("unknown", checkout_head):
            lagging.append(f"{root.name}@{dist_sha[:12]} via {how}")
    out: dict = {
        "dist_git_sha": first_sha[:12],
        "dist_version": first_ver,
        "resolved_via": ",".join(hows),
        "active_roots": len(roots),
    }
    if checkout_head is None or first_sha == "unknown":
        out["stale"] = None
        out["reason"] = "no checkout HEAD or manifest sha to compare against"
    elif lagging:
        out["stale"] = True
        out["lagging"] = lagging
        out["reason"] = (
            f"active wire root(s) lag checkout HEAD {checkout_head[:12]}: "
            f"{'; '.join(lagging)} — re-run `minni wire all` / `make sync-root`"
        )
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
                out["stale"] = False
                out["reason"] = (
                    "checkout was dirty at daemon start: the running code "
                    "does not correspond exactly to any commit"
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

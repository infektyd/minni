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


def _active_payload_root() -> tuple[Optional[Path], str]:
    """(root, how) for the wire-managed payload actually in use.

    Reading `current` alone is blind on the --from-repo / sync-root path:
    local (+git.*) installs deliberately never move `current` (see
    wire/install.update_current_symlink and its pins), so a machine wired
    from a checkout would report stale=None forever — or judge a zombie
    `current` left behind by an old release install. Prefer what wire
    actually RECORDED (the newest wired.json entry whose install root still
    carries a manifest), then `current`, then the newest manifest-carrying
    version dir.
    """
    base = Path("~/.minni/plugin").expanduser()
    try:
        data = json.loads((base / "wired.json").read_text(encoding="utf-8"))
        entries = sorted(
            (
                (str(w.get("wired_at") or ""), str(w.get("install_root") or ""))
                for w in data.get("wires", [])
                if isinstance(w, dict) and w.get("install_root")
            ),
            reverse=True,
        )
        for _wired_at, root_str in entries:
            root = Path(root_str)
            if (root / "payload-manifest.json").is_file():
                return root, "wired.json"
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    current = base / "current"
    if (current / "payload-manifest.json").is_file():
        return current, "current"
    try:
        candidates = [
            d for d in base.iterdir()
            if d.is_dir() and (d / "payload-manifest.json").is_file()
        ]
    except OSError:
        candidates = []
    if candidates:
        newest = max(candidates, key=lambda d: d.stat().st_mtime)
        return newest, "version-dir scan"
    return None, "none"


def _plugin_dist_status(checkout_head: Optional[str]) -> dict:
    """Staleness of the wire-managed plugin payload, by its manifest git_sha."""
    root, how = _active_payload_root()
    if root is None:
        return {"stale": None, "reason": "no wire-managed plugin payload found"}
    manifest_path = root / "payload-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "stale": None,
            "resolved_via": how,
            "reason": f"payload-manifest unreadable: {type(exc).__name__}",
        }
    dist_sha = str(manifest.get("git_sha") or "unknown")
    out: dict = {
        "dist_git_sha": dist_sha[:12],
        "dist_version": str(manifest.get("version") or "unknown"),
        "resolved_via": how,
    }
    if checkout_head is None or dist_sha == "unknown":
        out["stale"] = None
        out["reason"] = "no checkout HEAD or manifest sha to compare against"
    elif dist_sha == checkout_head:
        out["stale"] = False
    else:
        out["stale"] = True
        out["reason"] = (
            f"deployed plugin dist is from {dist_sha[:12]}, checkout HEAD is "
            f"{checkout_head[:12]} — re-run `minni wire` / `make sync-root`"
        )
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
        return out
    except Exception as exc:  # status must never break on this block
        return {"stale": None, "error": f"{type(exc).__name__}: {exc}"}

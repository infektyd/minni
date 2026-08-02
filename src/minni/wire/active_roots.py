"""Shared active wire install-root resolution.

One definition for deploy honesty, check_versions, and check_deployments so a
half-written install root cannot be "active" for one surface and invisible for
another. A wire record is active only when its install_root is a directory that
carries ``payload-manifest.json`` (the wire install stamp). Latest ``wired_at``
wins per platform. ``current`` is fallback only when no usable wire records
exist, and only when it also has a payload-manifest.
"""

from __future__ import annotations

import json
from pathlib import Path


def active_wire_plugin_state(home: Path) -> tuple[set[Path], set[str]]:
    """Return (active install roots, platforms with an active wire record).

    Roots are resolved Paths. Platforms are the wire ``platform`` strings for
    those records (used to scope marketplace-cache skips per surface).
    """
    ordered = active_wire_plugin_roots_ordered(home)
    roots = {root for root, _how in ordered}
    platforms: set[str] = set()
    # Re-read platforms from the same selection rules without inventing a
    # second code path: ordered how strings are "wired.json:<platform>" or
    # "current" / "version-dir scan".
    for _root, how in ordered:
        if how.startswith("wired.json:"):
            platforms.add(how.split(":", 1)[1])
    return roots, platforms


def active_wire_plugin_roots_ordered(home: Path) -> list[tuple[Path, str]]:
    """Active roots as ``(resolved_root, how)`` for honesty/status reporting.

    ``how`` is ``wired.json:<platform>``, ``current``, or ``version-dir scan``.
    """
    base = Path(home).expanduser() / ".minni" / "plugin"
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
            # Require a real install tree with the wire stamp — is_dir() alone
            # lets a half-written root be "active" for checkers while honesty
            # falls through to current (different answers on the same host).
            if not root.is_dir() or not (root / "payload-manifest.json").is_file():
                continue
            platform = str(entry.get("platform") or "_")
            wired_at = str(entry.get("wired_at") or "")
            prev = latest_by_platform.get(platform)
            if prev is None or wired_at >= prev[0]:
                try:
                    resolved = root.resolve()
                except OSError:
                    continue
                latest_by_platform[platform] = (wired_at, resolved)
        # Drop platforms whose host config root is gone. A retired surface
        # (e.g. ~/.codex removed after an earlier wire) must not stay "active"
        # forever and force deploy.stale / sync-root probe fail while other
        # platforms rewire successfully.
        try:
            from minni.wire.platform import (
                config_root_candidates,
                config_root_exists,
            )
        except Exception:  # pragma: no cover — package always present in tree
            config_root_candidates = None  # type: ignore[assignment]
            config_root_exists = None  # type: ignore[assignment]
        for platform, (_wired_at, root) in sorted(latest_by_platform.items()):
            if config_root_exists is not None and config_root_candidates is not None:
                # Probe under the *same* home as wired.json (not ambient $HOME).
                candidates = config_root_candidates(home).get(platform)
                if candidates is not None:
                    ok, _probed = config_root_exists(platform, home=home)
                    if not ok:
                        continue
            if root not in seen:
                actives.append((root, f"wired.json:{platform}"))
                seen.add(root)
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    if actives:
        return actives
    # Fallback when no usable wired.json rows: prefer the newest *version*
    # dir with a payload-manifest. Do not let a lagging release-era
    # ``current`` symlink blind a fresher from-repo ``+git.*`` install that
    # never moves current (local versions leave the symlink alone).
    try:
        candidates = [
            d for d in base.iterdir()
            if d.is_dir()
            and d.name not in {"current", "cache"}
            and (d / "payload-manifest.json").is_file()
        ]
    except OSError:
        candidates = []
    if candidates:
        newest = max(candidates, key=lambda d: d.stat().st_mtime)
        try:
            resolved = newest.resolve()
        except OSError:
            resolved = newest
        return [(resolved, "version-dir scan")]
    current = base / "current"
    if current.exists() and (current / "payload-manifest.json").is_file():
        try:
            resolved = current.resolve()
        except OSError:
            resolved = current
        return [(resolved, "current")]
    return []

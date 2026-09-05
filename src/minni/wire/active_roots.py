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
    ordered = _active_wire_plugin_entries(home)
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
    """Unique active roots for status reporting, retaining first provenance."""
    unique: dict[Path, str] = {}
    for root, how in _active_wire_plugin_entries(home):
        unique.setdefault(root, how)
    return list(unique.items())


def _active_wire_plugin_entries(home: Path) -> list[tuple[Path, str]]:
    """Active roots as ``(resolved_root, how)`` for honesty/status reporting.

    ``how`` is ``wired.json:<platform>``, ``current``, or ``version-dir scan``.

    Fallback (version-dir / ``current``) is only for *pre-wire* hosts where
    ``wired.json`` is missing or unreadable. Once the file exists and parses —
    including empty ``wires: []`` after ``retire_platform`` — a zero-row result
    means "no wire-managed payload", not "scan historical version dirs".
    Re-promoting abandoned trees after retirement would brick ``make sync-root``
    on propagate-only hosts (cursor/antigravity leftovers).
    """
    base = Path(home).expanduser() / ".minni" / "plugin"
    actives: list[tuple[Path, str]] = []
    wired_path = base / "wired.json"
    wired_parsed = False
    try:
        data = json.loads(wired_path.read_text(encoding="utf-8"))
        wired_parsed = True
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
            # A payload is shared by several hosts. Preserve every platform
            # here; only the root-only reporting API may deduplicate paths.
            actives.append((root, f"wired.json:{platform}"))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    if actives:
        return actives
    # Parsed wired.json with zero surviving platforms = intentional empty
    # (retirement / all zombies). Do not re-animate historical version dirs.
    if wired_parsed:
        return []
    # Pre-wire / missing wired.json only: prefer newest version dir over a
    # lagging release-era ``current`` (local versions never move the symlink).
    try:
        candidates = [
            d for d in base.iterdir()
            if d.is_dir()
            and d.name not in {"current", "cache"}
            and (d / "payload-manifest.json").is_file()
        ]
    except OSError:
        candidates = []
    current = base / "current"
    current_ok = current.exists() and (current / "payload-manifest.json").is_file()
    if candidates:
        newest = max(candidates, key=lambda d: d.stat().st_mtime)
        try:
            resolved = newest.resolve()
        except OSError:
            resolved = newest
        if current_ok:
            try:
                cur_res = current.resolve()
            except OSError:
                cur_res = current
            if cur_res == resolved:
                return [(resolved, "current")]
        return [(resolved, "version-dir scan")]
    if current_ok:
        try:
            resolved = current.resolve()
        except OSError:
            resolved = current
        return [(resolved, "current")]
    return []

"""Reference-aware GC for ~/.minni/plugin version dirs."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import Version

from minni.wire.platform import default_config_scan_paths
from minni.wire.paths import is_safe_version_segment, plugin_base
from minni.wire.wired import wired_install_roots


@dataclass
class GcResult:
    pruned: list[str] = field(default_factory=list)
    retained_in_use: list[str] = field(default_factory=list)
    skipped_no_tty: bool = False
    would_prune: list[str] = field(default_factory=list)


def _all_version_dirs() -> list[tuple[str, Version]]:
    dirs: list[tuple[str, Version]] = []
    base = plugin_base()
    if not base.is_dir():
        return dirs
    for child in base.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        name = child.name
        if name.startswith("."):
            continue
        if not is_safe_version_segment(name):
            continue
        try:
            ver = Version(name)
        except Exception:
            continue
        dirs.append((name, ver))
    return sorted(dirs, key=lambda x: x[1], reverse=True)


def _release_version_dirs() -> list[tuple[str, Version]]:
    return [(name, ver) for name, ver in _all_version_dirs() if ver.local is None]


def _config_references_dir(version_dir: Path) -> bool:
    needle = str(version_dir)
    for path in default_config_scan_paths().values():
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle in text:
            return True
    return False


def _protected_versions() -> set[str]:
    # Rule (b)'s config-scan covers ALL version dirs, dev/local included — a dev
    # dir referenced by a stamped config but missing from wired.json (partial
    # write, out-of-band edit, legacy tooling) must not be silently pruned.
    protected = wired_install_roots()
    for name, _ in _all_version_dirs():
        if _config_references_dir(plugin_base() / name):
            protected.add(str(plugin_base() / name))
            protected.add(name)
    # Normalize to dir names
    names: set[str] = set()
    for item in protected:
        p = Path(item)
        names.add(p.name if p.name else item)
    return names


def _retention_set() -> set[str]:
    release = _release_version_dirs()
    keep: set[str] = set()
    if release:
        keep.add(release[0][0])
    if len(release) > 1:
        keep.add(release[1][0])
    keep |= _protected_versions()
    return keep


def _orphan_staging_dirs() -> list[Path]:
    base = plugin_base()
    if not base.is_dir():
        return []
    return [
        p for p in base.iterdir()
        if p.is_dir() and p.name.startswith(".staging-")
    ]


def _quarantine_dirs() -> list[Path]:
    base = plugin_base()
    if not base.is_dir():
        return []
    return [
        p for p in base.iterdir()
        if p.is_dir() and p.name.startswith(".quarantine-")
    ]


def run_gc(
    *,
    prune: bool | None,
    stdin_is_tty: bool,
    dry_run: bool = False,
) -> GcResult:
    result = GcResult()
    keep = _retention_set()
    candidates: list[Path] = []
    base = plugin_base()
    if base.is_dir():
        for child in base.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.startswith("."):
                if child.name.startswith(".staging-") or child.name.startswith(".quarantine-"):
                    if child.name not in {k for k in keep}:
                        candidates.append(child)
                continue
            if child.name in keep:
                result.retained_in_use.append(str(child))
                continue
            try:
                Version(child.name)
            except Exception:
                continue
            candidates.append(child)

    candidates.extend(_orphan_staging_dirs())
    candidates.extend(_quarantine_dirs())
    # Deduplicate
    seen: set[str] = set()
    unique: list[Path] = []
    for p in candidates:
        key = str(p)
        if key not in seen and p.name not in keep:
            seen.add(key)
            unique.append(p)

    if dry_run:
        result.would_prune = [str(p) for p in unique]
        return result

    if prune is None and not stdin_is_tty:
        result.skipped_no_tty = True
        result.would_prune = [str(p) for p in unique]
        if result.would_prune:
            print(
                "[wire] GC skipped (stdin not a TTY); would prune:",
                ", ".join(result.would_prune),
                file=sys.stderr,
            )
        return result

    if prune is False:
        return result

    if prune is None:
        if not unique:
            return result
        print(
            "[wire] GC would prune:",
            ", ".join(str(p) for p in unique),
            file=sys.stderr,
        )
        try:
            answer = input("[wire] Prune these now? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            return result

    for path in unique:
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        result.pruned.append(str(path))
        print(f"[wire] pruned {path}", file=sys.stderr)
    return result
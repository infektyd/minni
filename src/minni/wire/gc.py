"""Reference-aware GC and long-lived server protection for ~/.minni/plugin version dirs (§4.5)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packaging.version import Version

from minni.wire.paths import is_safe_version_segment, plugin_base
from minni.wire.platform import default_config_scan_paths
from minni.wire.wired import wired_install_roots


@dataclass
class GcResult:
    pruned: list[str] = field(default_factory=list)
    retained_in_use: list[str] = field(default_factory=list)
    skipped_no_tty: bool = False
    would_prune: list[str] = field(default_factory=list)


def _resolve_base(home: Path | None = None) -> Path:
    if home is not None:
        return Path(home) / ".minni" / "plugin"
    return plugin_base()


def _resolve_home_from_base(base: Path, home: Path | None = None) -> Path:
    if home is not None:
        return Path(home)
    if base.name == "plugin" and base.parent.name == ".minni":
        return base.parent.parent
    return base.parent


def _all_version_dirs(base: Path | None = None) -> list[tuple[str, Version]]:
    dirs: list[tuple[str, Version]] = []
    target_base = base if base is not None else plugin_base()
    if not target_base.is_dir():
        return dirs
    for child in target_base.iterdir():
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


def _release_version_dirs(base: Path | None = None) -> list[tuple[str, Version]]:
    return [(name, ver) for name, ver in _all_version_dirs(base) if ver.local is None]


def _version_built_epoch(vdir: Path) -> float:
    """Return built_at epoch seconds from payload-manifest.json, falling back to directory mtime."""
    manifest_path = vdir / "payload-manifest.json"
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            built_at = str(data.get("built_at") or "") if isinstance(data, dict) else ""
            if built_at:
                dt = datetime.fromisoformat(built_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.timestamp()
        except Exception:
            pass
    try:
        return vdir.stat().st_mtime
    except OSError:
        return 0.0


def _version_dirs_by_built_at(base: Path) -> list[tuple[str, float]]:
    items: list[tuple[str, float]] = []
    for name, _ in _all_version_dirs(base):
        vdir = base / name
        items.append((name, _version_built_epoch(vdir)))
    return sorted(items, key=lambda x: x[1], reverse=True)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _lease_protected_versions(base: Path, home: Path | None = None) -> tuple[set[str], set[int], dict[int, dict[str, Any]]]:
    """Read live `~/.minni/run/mcp-servers/<pid>.json` leases (§4.5 #1 & #3)."""
    resolved_home = _resolve_home_from_base(base, home)
    lease_dir = resolved_home / ".minni" / "run" / "mcp-servers"
    protected: set[str] = set()
    pinned_pids: set[int] = set()
    lease_by_pid: dict[int, dict[str, Any]] = {}
    if not lease_dir.is_dir():
        return protected, pinned_pids, lease_by_pid

    for entry in lease_dir.iterdir():
        if not entry.name.endswith(".json") or not entry.is_file():
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            pid = int(data.get("pid", 0))
            if not _pid_is_alive(pid):
                continue
            ver_root = str(data.get("version_root") or "")
            if ver_root:
                vpath = Path(ver_root)
                protected.add(vpath.name)
                pinned_pids.add(pid)
                lease_by_pid[pid] = data
        except Exception:
            continue
    return protected, pinned_pids, lease_by_pid


def _lsof_open_version_dirs(base: Path, pids: list[int]) -> tuple[set[str], dict[int, str]]:
    """Inspect open FDs (`pinFd`) and `cwd` via `lsof -Fn -p <pids>` (§4.5 #1 & #3)."""
    protected: set[str] = set()
    pid_to_ver: dict[int, str] = {}
    if not pids or not base.is_dir():
        return protected, pid_to_ver

    try:
        base_real = str(base.resolve())
    except OSError:
        base_real = str(base)

    pid_arg = ",".join(str(p) for p in pids if p > 0)
    if not pid_arg:
        return protected, pid_to_ver

    try:
        proc = subprocess.run(
            ["lsof", "-Fn", "-p", pid_arg],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        current_pid = 0
        for raw_line in proc.stdout.splitlines():
            if raw_line.startswith("p"):
                try:
                    current_pid = int(raw_line[1:].strip())
                except ValueError:
                    current_pid = 0
            elif raw_line.startswith("n") and current_pid > 0:
                fpath = raw_line[1:].strip()
                for prefix in (str(base), base_real):
                    if fpath.startswith(prefix + "/"):
                        rel = fpath[len(prefix) + 1 :]
                        seg = rel.split("/", 1)[0]
                        if seg and seg != "current" and not seg.startswith("."):
                            protected.add(seg)
                            pid_to_ver.setdefault(current_pid, seg)
    except Exception:
        pass
    return protected, pid_to_ver


def bounded_lstart_protected_versions(
    base: Path,
    unpinned_lstarts: list[float],
) -> set[str]:
    """Bounded per-PID launch-window match for legacy unpinned `server.js` PIDs (§4.5 #2, `N8`).

    Protects at most 2 directories per legacy unpinned PID:
    - The single version directory with the newest `built_at <= T_start + 60s`,
    - Plus its 1 immediate predecessor if built within 5 minutes (300s) of `T_start`.
    """
    protected: set[str] = set()
    if not unpinned_lstarts:
        return protected

    # Ascending order by built_at epoch
    by_built_asc = list(reversed(_version_dirs_by_built_at(base)))
    if not by_built_asc:
        return protected

    for t_start in unpinned_lstarts:
        eligible_indices = [
            idx for idx, (_name, epoch) in enumerate(by_built_asc)
            if epoch <= t_start + 60.0
        ]
        if not eligible_indices:
            continue
        primary_idx = eligible_indices[-1]
        primary_name, _primary_epoch = by_built_asc[primary_idx]
        protected.add(primary_name)
        if primary_idx > 0:
            pred_name, pred_epoch = by_built_asc[primary_idx - 1]
            if (t_start - pred_epoch) <= 300.0:
                protected.add(pred_name)
    return protected


def _parse_ps_lstart(lstart_str: str) -> float | None:
    text = " ".join(lstart_str.strip().split())
    if not text:
        return None
    for fmt in ("%a %b %d %H:%M:%S %Y", "%c"):
        try:
            dt = datetime.strptime(text, fmt).astimezone(UTC)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _discover_live_server_pids_for_home(base: Path, home: Path | None = None) -> list[dict[str, Any]]:
    """Enumerate live `server.js` processes belonging to `home`."""
    resolved_home = _resolve_home_from_base(base, home)
    try:
        home_real = str(resolved_home.resolve())
    except OSError:
        home_real = str(resolved_home)
    home_prefixes = (str(resolved_home), home_real)

    results: list[dict[str, Any]] = []
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,lstart=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if "server.js" not in line:
                continue
            parts = line.split(None, 7)
            if len(parts) < 8:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            lstart_raw = " ".join(parts[2:7])
            cmd = parts[7]
            if not any(hp in cmd for hp in home_prefixes):
                continue
            results.append({
                "pid": pid,
                "ppid": ppid,
                "lstart_epoch": _parse_ps_lstart(lstart_raw),
                "command": cmd,
            })
    except Exception:
        pass
    return results


def _config_references_dir(version_dir: Path, home: Path | None = None) -> bool:
    needle = str(version_dir)
    rel_needle = f".minni/plugin/{version_dir.name}"
    effective_home = _resolve_home_from_base(version_dir.parent, home)
    try:
        scan_map = default_config_scan_paths(home=effective_home)
    except TypeError:
        scan_map = default_config_scan_paths()
    paths_to_scan: list[Path] = list(scan_map.values())
    try:
        from minni.wire.registry import expand_host_surface_catalog

        expanded = expand_host_surface_catalog(home=effective_home)
        for hspec in expanded.values():
            for surf in hspec.get("surfaces", []):
                if not isinstance(surf, dict):
                    continue
                if surf.get("path"):
                    paths_to_scan.append(Path(str(surf["path"])))
                for vp in surf.get("paths") or []:
                    paths_to_scan.append(Path(str(vp)))
        paths_to_scan.append(effective_home / ".muse" / "config.json")
        paths_to_scan.append(
            effective_home / ".claude" / "plugins" / "marketplaces" / "local-desktop-app-uploads" / "plugins" / "minni"
        )
    except Exception:
        pass
    for path in paths_to_scan:
        if path.is_symlink():
            try:
                link_target = os.readlink(path)
                if needle in link_target or rel_needle in link_target:
                    return True
            except OSError:
                pass
        if path.is_dir() and not path.is_symlink():
            for sub in ("dist", "node_modules", "skills", "commands", "hooks"):
                sub_p = path / sub
                if sub_p.is_symlink():
                    try:
                        sub_target = os.readlink(sub_p)
                        if needle in sub_target or rel_needle in sub_target:
                            return True
                    except OSError:
                        pass
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle in text or rel_needle in text:
            return True
    return False


def _protected_versions(
    base: Path,
    *,
    home: Path | None = None,
    prev_target: str | None = None,
    legacy_unpinned_lstarts: list[float] | None = None,
    extra_protected: set[str] | list[str] | None = None,
) -> set[str]:
    protected = wired_install_roots()
    if extra_protected:
        for ep in extra_protected:
            if ep:
                protected.add(Path(str(ep)).name)
    current_link = base / "current"
    if current_link.is_symlink():
        try:
            protected.add(Path(os.readlink(current_link)).name)
        except OSError:
            pass
    if prev_target:
        protected.add(Path(prev_target).name)

    for name, _ in _all_version_dirs(base):
        if _config_references_dir(base / name, home=home):
            protected.add(str(base / name))
            protected.add(name)

    # Lease files + lsof open FDs + bounded lstart for legacy unpinned servers (§4.5)
    lease_vers, pinned_pids, _lease_map = _lease_protected_versions(base, home=home)
    protected |= lease_vers

    ps_procs = _discover_live_server_pids_for_home(base, home=home)
    all_pids = sorted({p["pid"] for p in ps_procs} | pinned_pids)
    lsof_vers, lsof_pid_map = _lsof_open_version_dirs(base, all_pids)
    protected |= lsof_vers

    unpinned_lstarts: list[float] = list(legacy_unpinned_lstarts or [])
    for proc_info in ps_procs:
        pid = proc_info["pid"]
        if pid not in pinned_pids and pid not in lsof_pid_map:
            lstart_ep = proc_info.get("lstart_epoch")
            if isinstance(lstart_ep, float):
                unpinned_lstarts.append(lstart_ep)
    if unpinned_lstarts:
        protected |= bounded_lstart_protected_versions(base, unpinned_lstarts)

    names: set[str] = set()
    for item in protected:
        p = Path(item)
        names.add(p.name if p.name else item)
    return names


def _retention_set(
    base: Path,
    *,
    home: Path | None = None,
    keep_last: int = 0,
    prev_target: str | None = None,
    legacy_unpinned_lstarts: list[float] | None = None,
    extra_protected: set[str] | list[str] | None = None,
) -> set[str]:
    release = _release_version_dirs(base)
    keep: set[str] = set()
    if release:
        keep.add(release[0][0])
    if len(release) > 1:
        keep.add(release[1][0])
    if keep_last > 0:
        for name, _epoch in _version_dirs_by_built_at(base)[:keep_last]:
            keep.add(name)
    keep |= _protected_versions(
        base,
        home=home,
        prev_target=prev_target,
        legacy_unpinned_lstarts=legacy_unpinned_lstarts,
        extra_protected=extra_protected,
    )
    return keep


def _orphan_staging_dirs(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return [
        p for p in base.iterdir()
        if p.is_dir() and p.name.startswith(".staging-")
    ]


def _quarantine_dirs(base: Path) -> list[Path]:
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
    home: Path | None = None,
    keep_last: int = 0,
    prev_target: str | None = None,
    legacy_unpinned_lstarts: list[float] | None = None,
    extra_protected: set[str] | list[str] | None = None,
) -> GcResult:
    result = GcResult()
    base = _resolve_base(home)
    keep = _retention_set(
        base,
        home=home,
        keep_last=keep_last,
        prev_target=prev_target,
        legacy_unpinned_lstarts=legacy_unpinned_lstarts,
        extra_protected=extra_protected,
    )
    candidates: list[Path] = []
    if base.is_dir():
        for child in base.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.startswith("."):
                if child.name.startswith(".staging-") or child.name.startswith(".quarantine-"):
                    if child.name not in keep:
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

    candidates.extend(_orphan_staging_dirs(base))
    candidates.extend(_quarantine_dirs(base))
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
        shutil.rmtree(path, ignore_errors=True)
        result.pruned.append(str(path))
        print(f"[wire] pruned {path}", file=sys.stderr)
    return result


def collect_garbage(
    *,
    home: Path | None = None,
    keep_last: int = 2,
    prune: bool = True,
    dry_run: bool = False,
    prev_target: str | None = None,
    legacy_unpinned_lstarts: list[float] | None = None,
    extra_protected: set[str] | list[str] | None = None,
) -> GcResult:
    """Execute §4.5 safe garbage collection protecting `keep_last=2`, open FDs, leases, and bounded `lstart`."""
    return run_gc(
        prune=prune,
        stdin_is_tty=False,
        dry_run=dry_run,
        home=home,
        keep_last=keep_last,
        prev_target=prev_target,
        legacy_unpinned_lstarts=legacy_unpinned_lstarts,
        extra_protected=extra_protected,
    )


def _read_version_git_sha(vdir: Path) -> str:
    for rel in ("payload-manifest.json", "dist/build-manifest.json"):
        p = vdir / rel
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("git_sha"):
                    return str(data["git_sha"])
            except Exception:
                pass
    if "+git." in vdir.name:
        return vdir.name.split("+git.", 1)[1].split(".", 1)[0]
    return ""


def _process_name(pid: int) -> str:
    if pid <= 0:
        return "unknown"
    try:
        out = subprocess.check_output(
            ["ps", "-o", "comm=", "-p", str(pid)],
            text=True,
            timeout=2,
        ).strip()
        return Path(out).name if out else "unknown"
    except Exception:
        return "unknown"


def inspect_running_mcp_servers(
    *,
    target_sha: str = "",
    target_version: str = "",
    home: Path | None = None,
) -> list[dict[str, Any]]:
    """Inspect all live `server.js` processes and report loaded version/SHA vs target (§4.5 #4 & §4.7 #3)."""
    base = _resolve_base(home)
    _lease_vers, pinned_pids, lease_map = _lease_protected_versions(base, home=home)
    ps_procs = _discover_live_server_pids_for_home(base, home=home)
    all_pids = sorted({p["pid"] for p in ps_procs} | pinned_pids)
    _lsof_vers, lsof_pid_map = _lsof_open_version_dirs(base, all_pids)

    ps_by_pid = {p["pid"]: p for p in ps_procs}
    records: list[dict[str, Any]] = []

    for pid in all_pids:
        if not _pid_is_alive(pid):
            continue
        lease = lease_map.get(pid, {})
        proc_info = ps_by_pid.get(pid, {})
        ppid = int(lease.get("ppid") or proc_info.get("ppid") or 0)
        host = str(lease.get("agent_id") or "unknown")
        ver_root_str = str(lease.get("version_root") or "")
        if not ver_root_str and pid in lsof_pid_map:
            ver_root_str = str(base / lsof_pid_map[pid])
        if not ver_root_str:
            lstart_ep = proc_info.get("lstart_epoch")
            if isinstance(lstart_ep, float):
                matched = sorted(bounded_lstart_protected_versions(base, [lstart_ep]))
                if matched:
                    ver_root_str = str(base / matched[-1])

        vdir = Path(ver_root_str) if ver_root_str else Path()
        backing_exists = bool(ver_root_str and vdir.is_dir() and (vdir / "dist" / "server.js").is_file())
        loaded_version = vdir.name if ver_root_str else "unknown"
        loaded_sha = str(lease.get("git_sha") or (_read_version_git_sha(vdir) if backing_exists else ""))

        stale = False
        if target_sha and loaded_sha:
            stale = not (
                loaded_sha.startswith(target_sha[:8]) or target_sha.startswith(loaded_sha[:8])
            )
        elif target_version and loaded_version != "unknown":
            stale = loaded_version != target_version

        records.append({
            "host": host,
            "parent_name": _process_name(ppid),
            "pid": pid,
            "ppid": ppid,
            "version_root": ver_root_str,
            "version": loaded_version,
            "git_sha": loaded_sha,
            "loaded_version": loaded_version,
            "loaded_sha": loaded_sha,
            "target_version": target_version,
            "target_sha": target_sha,
            "stale": stale,
            "backing_dir_exists": backing_exists,
        })
    return records


def stale_server_host_names(records: list[dict[str, Any]]) -> list[str]:
    """Return sorted deduplicated human host names for running stale MCP servers."""
    stale = [r for r in records if r.get("stale", True)]
    names: list[str] = []
    for rec in stale:
        host = str(rec.get("host") or "").strip()
        parent_name = str(rec.get("parent_name") or "").strip()
        label = host if (host and host != "unknown") else (parent_name if (parent_name and parent_name != "unknown") else f"pid:{rec.get('pid')}")
        if label not in names:
            names.append(label)
    return names


def format_restart_host_apps_notice(records: list[dict[str, Any]]) -> str:
    """Format the operator-facing restart notice for running stale MCP servers (H36)."""
    names = stale_server_host_names(records)
    if not names:
        return ""
    return f"restart these host apps: {', '.join(names)}"


def format_running_stale_servers(records: list[dict[str, Any]]) -> str:
    """Format the `RUNNING STALE SERVERS` deploy & status report (§4.5 #4)."""
    stale = [r for r in records if r.get("stale")]
    if not stale:
        return "RUNNING STALE SERVERS: none"
    lines = ["RUNNING STALE SERVERS (protected from GC until host reloads MCP):"]
    for rec in stale:
        lines.append(
            f"  - host={rec['host']} (parent={rec['parent_name']} pid={rec['ppid']}) "
            f"server_pid={rec['pid']} loaded={rec['loaded_version']} "
            f"target={rec['target_version'] or rec['target_sha']}"
        )
    return "\n".join(lines)

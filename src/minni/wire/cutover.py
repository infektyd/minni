"""7-step atomic cutover, pre-flip scratch probes, and key-scoped CAS rollback (§4.3, §5)."""

from __future__ import annotations

import argparse
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from minni.wire.containers import (
    CurrentFlipState,
    SubtreeSwapRecord,
    archive_container_prev_subtrees,
    converge_host_container,
    flip_current_pointer,
    resolve_verified_plugin_root,
    rollback_container_subtrees,
    rollback_current_pointer,
    stage_scratch_containers,
    swap_container_subtrees,
)
from minni.wire.gc import collect_garbage
from minni.wire.install import install_payload
from minni.wire.manifest import PayloadManifest
from minni.wire.registry import (
    _VERSIONED_PLUGIN_RE,
    HOST_SURFACE_CATALOG,
    effective_container_mode,
    evaluate_mcp_surface_diff,
    expand_for_home,
)
from minni.wire.wired import (
    _load_wired,
    build_wired_v2,
    record_ui_scanner_gate,
    save_wired_v2,
)
from minni.wire.writers import _toml_basic_str


@dataclass
class _ConfigRollbackRecord:
    host: str
    path: Path
    rewrite_on_run: bool
    pre_bytes: bytes
    pre_mtime_ns: int
    pre_sha256: str
    previous_minni_entry: Any
    post_step5_mtime_ns: int
    post_step5_sha256: str
    pre_ino: int = 0
    post_step5_ino: int = 0
    surface_kind: str = "mcp_json"
    pre_mode: int = 0o600


class _CasConflictError(Exception):
    def __init__(self, host: str, path: Path, reason: str, conflict_artifact: str = "") -> None:
        self.host = host
        self.path = path
        self.reason = reason
        self.conflict_artifact = conflict_artifact
        super().__init__(f"CAS conflict on {host} ({path}): {reason}")


@dataclass
class CutoverOutcome:
    exit_code: int
    status: str
    target_version: str
    target_git_sha: str
    aborted_pre_flip: bool = False
    rolled_back: bool = False
    deferred_running_hosts: list[str] = field(default_factory=list)
    updated_surfaces: list[str] = field(default_factory=list)
    skipped_surfaces: list[str] = field(default_factory=list)
    cas_conflicts: list[str] = field(default_factory=list)
    gc_result: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "status": self.status,
            "target_version": self.target_version,
            "target_git_sha": self.target_git_sha,
            "aborted_pre_flip": self.aborted_pre_flip,
            "rolled_back": self.rolled_back,
            "deferred_running_hosts": list(self.deferred_running_hosts),
            "updated_surfaces": list(self.updated_surfaces),
            "skipped_surfaces": list(self.skipped_surfaces),
            "cas_conflicts": list(self.cas_conflicts),
            "gc_result": dict(self.gc_result),
            "message": self.message,
        }


def _file_stat_and_sha256(path: Path) -> tuple[int, str, bytes]:
    raw = path.read_bytes()
    st = path.stat()
    digest = hashlib.sha256(raw).hexdigest()
    return st.st_mtime_ns, digest, raw


def _file_cas_tuple(path: Path) -> tuple[int, int, str, bytes]:
    raw = path.read_bytes()
    st = path.stat()
    digest = hashlib.sha256(raw).hexdigest()
    return st.st_ino, st.st_mtime_ns, digest, raw


def _archive_dir(home: Path, ts: str | None = None) -> Path:
    stamp = ts or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    d = home / ".minni" / "archive" / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


def _archive_file_before_write(home: Path, path: Path, ts: str) -> Path | None:
    if not path.is_file():
        return None
    arch = _archive_dir(home, ts) / "configs"
    arch.mkdir(parents=True, exist_ok=True)
    safe_name = str(path).lstrip("/").replace("/", "__")
    dest = arch / safe_name
    shutil.copy2(path, dest)
    return dest


def _atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    tmp.write_bytes(data)
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


def _check_host_lockfile(home: Path, host_id: str, surface_path: Path, *, retries: int = 3) -> str | None:
    """Return lock/temp path string if a host lockfile or temp sibling is present after short backoff."""
    hspec = HOST_SURFACE_CATALOG.get(host_id) or {}
    lock_glob_raw = str(hspec.get("lock_glob") or "")
    expanded_glob = expand_for_home(lock_glob_raw, home=home) if lock_glob_raw else ""

    def _scan_once() -> str | None:
        if expanded_glob:
            matches = [
                m for m in glob.glob(expanded_glob)
                if not m.endswith(f".minni-cas.{os.getpid()}") and not m.endswith(f".tmp.{os.getpid()}")
            ]
            if matches:
                return matches[0]
        parent = surface_path.parent
        if parent.is_dir():
            for cand in (
                parent / f".{surface_path.name}.lock",
                parent / f"{surface_path.name}.lock",
            ):
                if cand.exists() or cand.is_symlink():
                    return str(cand)
            for pat in (f"{surface_path.name}.tmp.*", f".{surface_path.name}.tmp.*"):
                for m in parent.glob(pat):
                    if m.name.endswith(f".tmp.{os.getpid()}"):
                        continue
                    return str(m)
        return None

    found = _scan_once()
    for _ in range(max(0, retries - 1)):
        if found is None:
            return None
        time.sleep(0.02)
        found = _scan_once()
    return found


def _cas_write_surface(
    path: Path,
    pre_ino: int,
    pre_mtime_ns: int,
    pre_sha256: str,
    new_bytes: bytes,
    *,
    home: Path,
    host: str,
    ts: str,
    mode: int | None = None,
) -> tuple[int, int, str]:
    """Stage `new_bytes`, verify `(st_ino, st_mtime_ns, sha256)` immediately before `os.replace`, and verify readback."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.minni-cas.{os.getpid()}"
    try:
        tmp.write_bytes(new_bytes)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        lock_hit = _check_host_lockfile(home, host, path, retries=1)
        if lock_hit:
            cdir = _archive_dir(home, ts) / "cas-conflict"
            cdir.mkdir(parents=True, exist_ok=True)
            cpath = cdir / path.name
            cpath.write_bytes(new_bytes)
            raise _CasConflictError(host, path, f"lockfile present before replace: {lock_hit}", str(cpath))

        cur_ino, cur_mtime_ns, cur_sha256, cur_bytes = _file_cas_tuple(path)
        if (cur_ino, cur_mtime_ns, cur_sha256) != (pre_ino, pre_mtime_ns, pre_sha256):
            cdir = _archive_dir(home, ts) / "cas-conflict"
            cdir.mkdir(parents=True, exist_ok=True)
            cpath = cdir / path.name
            cpath.write_bytes(cur_bytes)
            raise _CasConflictError(
                host,
                path,
                "concurrent modification detected before os.replace",
                str(cpath),
            )
        os.replace(tmp, path)
    finally:
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink(missing_ok=True)

    post_ino, post_mtime_ns, post_sha256, post_bytes = _file_cas_tuple(path)
    if post_bytes != new_bytes:
        cdir = _archive_dir(home, ts) / "cas-conflict"
        cdir.mkdir(parents=True, exist_ok=True)
        cpath = cdir / path.name
        cpath.write_bytes(post_bytes)
        raise _CasConflictError(host, path, "post-replace readback mismatch", str(cpath))
    return post_ino, post_mtime_ns, post_sha256


def _cas_write_or_create_surface(
    path: Path,
    pre_tuple: tuple[int, int, str] | None,
    new_bytes: bytes,
    *,
    home: Path,
    host: str,
    ts: str,
    mode: int | None = None,
) -> tuple[int, int, str]:
    """CAS-protected write for both existing files (`pre_tuple` not None) and newly created files (`pre_tuple` is None)."""
    if pre_tuple is not None:
        return _cas_write_surface(
            path,
            pre_tuple[0],
            pre_tuple[1],
            pre_tuple[2],
            new_bytes,
            home=home,
            host=host,
            ts=ts,
            mode=mode,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.minni-cas.{os.getpid()}"
    try:
        tmp.write_bytes(new_bytes)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        lock_hit = _check_host_lockfile(home, host, path, retries=1)
        if lock_hit:
            cdir = _archive_dir(home, ts) / "cas-conflict"
            cdir.mkdir(parents=True, exist_ok=True)
            cpath = cdir / path.name
            cpath.write_bytes(new_bytes)
            raise _CasConflictError(host, path, f"lockfile present before replace: {lock_hit}", str(cpath))
        if path.exists() or path.is_symlink():
            cdir = _archive_dir(home, ts) / "cas-conflict"
            cdir.mkdir(parents=True, exist_ok=True)
            cpath = cdir / path.name
            try:
                cpath.write_bytes(path.read_bytes())
            except OSError:
                cpath.write_bytes(new_bytes)
            raise _CasConflictError(
                host,
                path,
                "concurrent file creation detected before os.replace",
                str(cpath),
            )
        os.replace(tmp, path)
    finally:
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink(missing_ok=True)

    post_ino, post_mtime_ns, post_sha256, post_bytes = _file_cas_tuple(path)
    if post_bytes != new_bytes:
        cdir = _archive_dir(home, ts) / "cas-conflict"
        cdir.mkdir(parents=True, exist_ok=True)
        cpath = cdir / path.name
        cpath.write_bytes(post_bytes)
        raise _CasConflictError(host, path, "post-replace readback mismatch", str(cpath))
    return post_ino, post_mtime_ns, post_sha256


_FIXED_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
_INITIAL_NODE_BIN = shutil.which("node")
_FIXED_PROBE_PATH = (
    f"{_FIXED_BASE_PATH}:{Path(_INITIAL_NODE_BIN).parent}"
    if _INITIAL_NODE_BIN and str(Path(_INITIAL_NODE_BIN).parent) not in _FIXED_BASE_PATH.split(":")
    else _FIXED_BASE_PATH
)


def _minimal_probe_env(probe_home: Path, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Construct a minimal environment for pre-flip probes with fixed PATH/LANG and no parent secret leakage (P3-12)."""
    tmp_dir = probe_home / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {
        "PATH": _FIXED_PROBE_PATH,
        "HOME": str(probe_home),
        "TMPDIR": str(tmp_dir),
        "LANG": "C.UTF-8",
        "MINNI_HOME": str(probe_home / ".minni"),
        "MINNI_VAULT_PATH": str(probe_home / "vault"),
        "MINNI_AGENT_ID": "probe",
    }
    if extra_env:
        for k, v in extra_env.items():
            if isinstance(k, str) and isinstance(v, str) and k.startswith("MINNI_"):
                env[k] = v
        # Always keep probe isolated to probe_home
        env["HOME"] = str(probe_home)
        env["MINNI_HOME"] = str(probe_home / ".minni")
        env["MINNI_VAULT_PATH"] = str(probe_home / "vault")
        env["MINNI_SOCKET_PATH"] = str(probe_home / ".minni" / "run" / "minnid.sock")
    return env


def run_behavioral_probe(
    version_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    command: str = "node",
    args_prefix: list[str] | None = None,
    probe_hooks: bool = True,
    timeout: float = 20.0,
) -> tuple[bool, str]:
    """Run pre-flip MCP stdio `initialize` + `tools/list` (>=40 tools) and `SessionStart` hook probe."""
    server_js = version_dir / "dist" / "server.js"
    hook_js = version_dir / "dist" / "hook.js"
    if not server_js.is_file():
        return False, f"missing server.js at {server_js}"

    with tempfile.TemporaryDirectory(prefix="minni-probe-home-") as probe_home_str:
        probe_home = Path(probe_home_str)
        probe_env = _minimal_probe_env(probe_home, extra_env=extra_env)
        rpc_input = (
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "minni-cutover-probe", "version": "1.0"},
                },
            })
            + "\n"
            + json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            })
            + "\n"
        )
        cmd = [command, *(args_prefix or []), str(server_js)]
        try:
            proc = subprocess.run(
                cmd,
                input=rpc_input,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=probe_env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"MCP stdio probe timed out after {timeout}s on {server_js}"
        except OSError as exc:
            return False, f"failed to spawn {cmd[0]} on {server_js}: {exc}"

        tools_count = 0
        init_ok = False
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 1 and isinstance(msg.get("result"), dict):
                init_ok = True
            elif msg.get("id") == 2 and isinstance(msg.get("result"), dict):
                tools = msg["result"].get("tools")
                if isinstance(tools, list):
                    tools_count = len(tools)

        if proc.returncode != 0 or not init_ok or tools_count < 40:
            return (
                False,
                f"MCP stdio probe failed on {server_js} (rc={proc.returncode}, init_ok={init_ok}, tools={tools_count})",
            )

        if probe_hooks and hook_js.is_file():
            try:
                hproc = subprocess.run(
                    ["node", str(hook_js)],
                    input='{"hook_event_name":"SessionStart","cwd":"/tmp"}\n',
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=probe_env,
                    check=False,
                )
                if hproc.returncode != 0:
                    return False, f"hook.js probe failed with exit {hproc.returncode}: {hproc.stderr}"
            except subprocess.TimeoutExpired:
                return False, f"hook.js probe timed out after {timeout}s on {hook_js}"
            except Exception as exc:
                return False, f"hook.js probe exception: {exc}"

    return True, "ok"


def _validate_scratch_containers(
    scratch_dirs: dict[str, Path],
    *,
    home: Path | None = None,
) -> tuple[bool, str]:
    for host, sdir in scratch_dirs.items():
        if not sdir.is_dir():
            return False, f"scratch container missing for {host}: {sdir}"
        for sub in ("dist", "skills", "commands", "hooks"):
            sub_path = sdir / sub
            if not sub_path.exists():
                return False, f"scratch container {host} missing subtree {sub}"
        if not (sdir / "dist" / "server.js").is_file():
            return False, f"scratch container {host} missing dist/server.js"
        hspec = HOST_SURFACE_CATALOG.get(host) or {}
        host_env = {
            "MINNI_AGENT_ID": str(hspec.get("agent_id") or host),
            "MINNI_VAULT_PATH": expand_for_home(
                str(hspec.get("vault_path") or f"~/.minni/{host}-vault"), home=home
            ),
        }
        ok, msg = run_behavioral_probe(sdir, extra_env=host_env, probe_hooks=False)
        if not ok:
            return False, f"scratch container {host} behavioral probe failed: {msg}"
    return True, "ok"


def _probe_catalog_host_launchers(vdir: Path, home: Path) -> tuple[bool, str]:
    """Probe catalog host wrapper launchers (`command` / `args_prefix` / `MINNI_AGENT_ID`) when present on `home` (P1-4)."""
    probed: set[tuple[str, tuple[str, ...], str]] = set()
    for host_id, hspec in HOST_SURFACE_CATALOG.items():
        agent_id = str(hspec.get("agent_id") or host_id)
        vault_path = expand_for_home(str(hspec.get("vault_path") or f"~/.minni/{host_id}-vault"), home=home)
        host_env = {
            "MINNI_AGENT_ID": agent_id,
            "MINNI_VAULT_PATH": vault_path,
        }
        for surf in hspec.get("surfaces", []):
            if not isinstance(surf, dict):
                continue
            if surf.get("kind") not in ("mcp_json", "mcp_toml", "mcp_view_mirrors"):
                continue
            raw_cmd = str(surf.get("command") or "node")
            expanded_cmd = expand_for_home(raw_cmd, home=home)
            raw_prefix = [expand_for_home(str(a), home=home) for a in (surf.get("args_prefix") or [])]
            if expanded_cmd != "node":
                cmd_path = Path(expanded_cmd)
                if not cmd_path.exists() and not cmd_path.is_symlink():
                    continue
                key = (expanded_cmd, tuple(raw_prefix), agent_id)
                if key in probed:
                    continue
                probed.add(key)
                ok, msg = run_behavioral_probe(
                    vdir,
                    extra_env=host_env,
                    command=expanded_cmd,
                    args_prefix=raw_prefix,
                    probe_hooks=False,
                )
                if not ok:
                    return False, f"catalog launcher probe failed for {host_id} ({expanded_cmd}): {msg}"
    return True, "ok"


def _detect_running_rewrite_hosts() -> set[str]:
    running: set[str] = set()
    checks = {
        "muse": ["pgrep", "-x", "muse"],
        "claude-desktop": ["pgrep", "-x", "Claude"],
        "grok-build": ["pgrep", "-x", "grok"],
        "devin": ["pgrep", "-x", "devin"],
        "cursor": ["pgrep", "-x", "Cursor"],
    }
    for host, cmd in checks.items():
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=3, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                running.add(host)
        except Exception:
            pass
    return running


def _container_has_delta(
    container_dir: Path,
    version_dir: Path,
    base_semver: str,
    *,
    forbid_mcp_json: bool = False,
) -> bool:
    if not container_dir.is_dir() or container_dir.is_symlink():
        return True
    if forbid_mcp_json and ((container_dir / ".mcp.json").exists() or (container_dir / ".mcp.json").is_symlink()):
        return True
    srv_js = container_dir / "dist" / "server.js"
    target_srv_js = version_dir / "dist" / "server.js"
    if not srv_js.is_file() or not target_srv_js.is_file():
        return True
    try:
        if srv_js.read_bytes() != target_srv_js.read_bytes():
            return True
    except OSError:
        return True
    for sub in ("dist", "skills", "commands", "hooks"):
        if not (container_dir / sub).exists():
            return True
    for rel_pjson in (".claude-plugin/plugin.json", ".cursor-plugin/plugin.json"):
        pj = container_dir / rel_pjson
        if pj.is_file():
            try:
                pdoc = json.loads(pj.read_text(encoding="utf-8"))
                if not isinstance(pdoc, dict) or "mcpServers" in pdoc or pdoc.get("version") != base_semver:
                    return True
            except Exception:
                return True
    return False


def _converge_claude_local_marketplace(
    home: Path,
    version_dir: Path,
    target_version: str,
    target_git_sha: str,
    *,
    pid: int,
    ts: str,
) -> tuple[list[SubtreeSwapRecord], tuple[Path, bytes | None, int, int, str] | None, list[str], list[str]]:
    claude_plugins = home / ".claude" / "plugins"
    if not claude_plugins.is_dir():
        return [], None, [], []

    updated: list[str] = []
    skipped: list[str] = []
    base_semver = target_version.split("+", 1)[0] if target_version else "0.6.0"
    mkt_plugin_dir = claude_plugins / "marketplaces" / "minni-local" / "plugins" / "minni"
    mkt_has_delta = _container_has_delta(mkt_plugin_dir, version_dir, base_semver, forbid_mcp_json=True)
    mkt_plugin_dir.mkdir(parents=True, exist_ok=True)
    stable_current = home / ".minni" / "plugin" / "current"
    swaps = swap_container_subtrees(
        mkt_plugin_dir,
        stable_current_root=stable_current,
        target_version_root=version_dir,
        container_mode="symlink_subtrees",
        pid=pid,
    )

    cplugin_dir = mkt_plugin_dir / ".claude-plugin"
    cplugin_dir.mkdir(parents=True, exist_ok=True)
    src_pjson = version_dir / ".claude-plugin" / "plugin.json"
    if src_pjson.is_file():
        data = json.loads(src_pjson.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.pop("mcpServers", None)
            data["version"] = base_semver
            desired_pjson = (json.dumps(data, indent=2) + "\n").encode("utf-8")
            dest_pjson = cplugin_dir / "plugin.json"
            pre_pjson_tuple = _file_cas_tuple(dest_pjson)[:3] if dest_pjson.is_file() else None
            if pre_pjson_tuple is None or dest_pjson.read_bytes() != desired_pjson:
                _cas_write_or_create_surface(
                    dest_pjson,
                    pre_pjson_tuple,
                    desired_pjson,
                    home=home,
                    host="claude-code",
                    ts=ts,
                )
                mkt_has_delta = True
    src_pkg = version_dir / "package.json"
    if src_pkg.is_file():
        dest_pkg = mkt_plugin_dir / "package.json"
        src_pkg_bytes = src_pkg.read_bytes()
        pre_pkg_tuple = _file_cas_tuple(dest_pkg)[:3] if dest_pkg.is_file() else None
        if pre_pkg_tuple is None or dest_pkg.read_bytes() != src_pkg_bytes:
            _cas_write_or_create_surface(
                dest_pkg,
                pre_pkg_tuple,
                src_pkg_bytes,
                home=home,
                host="claude-code",
                ts=ts,
            )
            mkt_has_delta = True

    if mkt_has_delta:
        updated.append(f"claude-code:{mkt_plugin_dir}")
    else:
        skipped.append(f"claude-code:{mkt_plugin_dir}")

    inst_path = claude_plugins / "installed_plugins.json"
    pre_inst_tuple: tuple[int, int, str] | None = None
    prev_raw: bytes | None = None
    doc: dict[str, Any] = {"version": 2, "plugins": {}}
    if inst_path.is_file():
        inst_ino, inst_mtime_ns, inst_sha256, prev_raw = _file_cas_tuple(inst_path)
        pre_inst_tuple = (inst_ino, inst_mtime_ns, inst_sha256)
        parsed = json.loads(prev_raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(f"installed_plugins.json at {inst_path} is not a JSON object")
        doc = parsed

    plugins_map = doc.setdefault("plugins", {})
    needs_write = pre_inst_tuple is None
    if isinstance(plugins_map, dict):
        expanded_install_path = str(mkt_plugin_dir)
        expanded_project_path = str(home / ".claude" / "plugins" / "marketplaces" / "minni-local")
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        found_any = False
        for key in ("minni@minni-local", "minni@minni"):
            entries = plugins_map.get(key)
            if isinstance(entries, list) and entries:
                found_any = True
                for item in entries:
                    if isinstance(item, dict):
                        item_changed = False
                        if item.get("installPath") != expanded_install_path or "~" in str(item.get("installPath") or ""):
                            item["installPath"] = expanded_install_path
                            item_changed = True
                        if item.get("version") != base_semver:
                            item["version"] = base_semver
                            item_changed = True
                        if item.get("gitCommitSha") != target_git_sha:
                            item["gitCommitSha"] = target_git_sha
                            item_changed = True
                        if "projectPath" in item and (
                            item.get("projectPath") != expanded_project_path
                            or "~" in str(item.get("projectPath") or "")
                        ):
                            item["projectPath"] = expanded_project_path
                            item_changed = True
                        if item_changed:
                            item["lastUpdated"] = now_iso
                            needs_write = True
        if not found_any:
            plugins_map["minni@minni-local"] = [
                {
                    "scope": "user",
                    "installPath": expanded_install_path,
                    "version": base_semver,
                    "installedAt": now_iso,
                    "lastUpdated": now_iso,
                    "gitCommitSha": target_git_sha,
                }
            ]
            needs_write = True

    if not needs_write:
        skipped.append(f"claude-code:{inst_path}")
        return swaps, None, updated, skipped

    new_inst_bytes = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    post_ino, post_mtime_ns, post_sha256 = _cas_write_or_create_surface(
        inst_path,
        pre_inst_tuple,
        new_inst_bytes,
        home=home,
        host="claude-code",
        ts=ts,
    )
    updated.append(f"claude-code:{inst_path}")
    return swaps, (inst_path, prev_raw, post_ino, post_mtime_ns, post_sha256), updated, skipped


def _extract_referenced_versions(text: str, plugin_dir: Path) -> set[str]:
    found: set[str] = set()
    if not plugin_dir.is_dir():
        return found
    for child in plugin_dir.iterdir():
        if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
            if str(child) in text or f".minni/plugin/{child.name}" in text:
                found.add(child.name)
    return found


def _render_toml_with_updated_minni(raw_text: str, desired_entry: dict[str, Any]) -> str:
    """Replace `[mcp_servers.minni]` and `[mcp_servers.minni.env]` in `raw_text` with `desired_entry`."""
    cmd = str(desired_entry.get("command") or "node")
    args_list = desired_entry.get("args") or []
    args_rendered = ", ".join(f'"{_toml_basic_str(a)}"' for a in args_list)
    minni_lines = [
        "[mcp_servers.minni]",
        f'command = "{_toml_basic_str(cmd)}"',
        f"args = [{args_rendered}]",
    ]
    if "cwd" in desired_entry and desired_entry["cwd"]:
        minni_lines.append(f'cwd = "{_toml_basic_str(desired_entry["cwd"])}"')
    minni_lines.append("enabled = true")

    env_dict = desired_entry.get("env") or {}
    env_lines = ["[mcp_servers.minni.env]"]
    for k, v in env_dict.items():
        env_lines.append(f'{k} = "{_toml_basic_str(v)}"')

    text = raw_text
    for sec_name in ("mcp_servers.minni", "mcp_servers.minni.env"):
        pattern = re.compile(rf"(?ms)^\[{re.escape(sec_name)}\]\n.*?(?=^\[|\Z)")
        text = pattern.sub("", text)
    sections_str = "\n".join(minni_lines) + "\n\n" + "\n".join(env_lines)
    if text.strip():
        return text.rstrip() + "\n\n" + sections_str + "\n"
    return sections_str + "\n"


def _reconcile_text_surface(
    raw_text: str,
    *,
    kind: str,
    surf_spec: dict[str, Any],
    home: Path,
) -> tuple[bool, str]:
    """Compute `(has_delta, desired_text)` for `hooks_json`, `launcher_wrapper`, `hook_wrapper`, `js_bridge`, `rules_md`, `skill_copy`."""
    canonical_entry = expand_for_home(str(surf_spec.get("entry") or ""), home=home)
    stable_current = str(home / ".minni" / "plugin" / "current")
    use_exec = bool(surf_spec.get("use_exec"))

    updated = raw_text
    # 1. Replace any versioned plugin directory reference `.minni/plugin/<ver>+git.<sha>` with `.minni/plugin/current`
    updated = _VERSIONED_PLUGIN_RE.sub(".minni/plugin/current", updated)
    # 2. Expand leading `~/` inside paths
    updated = updated.replace("~/", f"{home}/")

    if kind == "hooks_json":
        # Validate JSON structure and ensure canonical entry / exec node
        doc = json.loads(updated)
        if not isinstance(doc, dict):
            raise ValueError("hooks_json is not a JSON object")
        entry_basename = Path(canonical_entry).name
        if entry_basename and not str(surf_spec.get("entry") or "").startswith("./"):
            # Replace any path ending with `entry_basename` with `canonical_entry`
            updated = re.sub(
                rf"(?:{re.escape(str(home))}[^\s\"']*?/{re.escape(entry_basename)})",
                canonical_entry,
                updated,
            )
        if use_exec:
            updated = re.sub(r'(?<!exec )\bnode\s+([^\s"\']*?' + re.escape(entry_basename) + r')', r'exec node \1', updated)
        # Re-validate JSON
        json.loads(updated)
    elif kind in ("launcher_wrapper", "hook_wrapper"):
        entry_basename = Path(canonical_entry).name
        if entry_basename:
            updated = re.sub(
                rf"(?:{re.escape(str(home))}[^\s\"']*?/{re.escape(entry_basename)})",
                canonical_entry,
                updated,
            )
        if use_exec:
            updated = re.sub(r"(?m)^(\s*)node\s+", r"\1exec node ", updated)
    elif kind == "js_bridge":
        entry_basename = Path(canonical_entry).name
        if entry_basename:
            updated = re.sub(
                rf"(?:{re.escape(str(home))}[^\s\"']*?/{re.escape(entry_basename)})",
                canonical_entry,
                updated,
            )
    elif kind in ("rules_md", "skill_copy"):
        updated = updated.replace(f"{home}/.minni/plugin/current", stable_current)

    return (updated != raw_text), updated


def execute_atomic_cutover(
    *,
    home: Path,
    target_version: str,
    target_git_sha: str,
    dry_run: bool = False,
    running_hosts: set[str] | None = None,
    post_flip_verifier: Callable[[Path], bool] | None = None,
) -> CutoverOutcome:
    """Execute the 7-step atomic cutover with pre-flip probes and CAS rollback (§4.3)."""
    plugin_dir = home / ".minni" / "plugin"
    vdir = plugin_dir / target_version
    pid = os.getpid()
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base_semver = target_version.split("+", 1)[0] if target_version else "0.6.0"

    if not vdir.is_dir():
        return CutoverOutcome(
            exit_code=1,
            status="ABORTED_PRE_FLIP",
            target_version=target_version,
            target_git_sha=target_git_sha,
            aborted_pre_flip=True,
            message=f"target payload directory does not exist: {vdir}",
        )

    if dry_run:
        return CutoverOutcome(
            exit_code=0,
            status="DRY_RUN",
            target_version=target_version,
            target_git_sha=target_git_sha,
            message=f"dry-run: would cut over {home} to {target_version}",
        )

    # Step 2 — Pre-Flip Scratch Container Stage (R4-2)
    scratch_root, scratch_dirs = stage_scratch_containers(vdir, home=home, base_semver=base_semver)
    try:
        # Step 3 — Pre-Flip Behavioral Probe (R4-2, P1-4)
        scratch_ok, scratch_msg = _validate_scratch_containers(scratch_dirs, home=home)
        if not scratch_ok:
            return CutoverOutcome(
                exit_code=1,
                status="ABORTED_PRE_FLIP",
                target_version=target_version,
                target_git_sha=target_git_sha,
                aborted_pre_flip=True,
                message=scratch_msg,
            )
        probe_ok, probe_msg = run_behavioral_probe(vdir)
        if not probe_ok:
            return CutoverOutcome(
                exit_code=1,
                status="ABORTED_PRE_FLIP",
                target_version=target_version,
                target_git_sha=target_git_sha,
                aborted_pre_flip=True,
                message=probe_msg,
            )
        launchers_ok, launchers_msg = _probe_catalog_host_launchers(vdir, home)
        if not launchers_ok:
            return CutoverOutcome(
                exit_code=1,
                status="ABORTED_PRE_FLIP",
                target_version=target_version,
                target_git_sha=target_git_sha,
                aborted_pre_flip=True,
                message=launchers_msg,
            )
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    wired_doc = _load_wired(plugin_dir / "wired.json")
    active_running = (
        set(running_hosts)
        if running_hosts is not None
        else _detect_running_rewrite_hosts()
    )
    rollback_records: list[_ConfigRollbackRecord] = []
    commands_symlink_rollbacks: list[tuple[Path, str | None]] = []
    updated_surfaces: list[str] = []
    skipped_surfaces: list[str] = []
    deferred_running_hosts: list[str] = []
    deferred_details: list[str] = []
    deferred_protected_versions: set[str] = set()

    def _should_defer_host_write(host: str, cfg_path: Path, raw_text: str) -> bool:
        lock_hit = _check_host_lockfile(home, host, cfg_path, retries=3)
        is_running_rewrite_host = (
            host in ("muse", "claude-desktop", "grok-build", "devin", "cursor")
            and host in active_running
        )
        if lock_hit or is_running_rewrite_host:
            if host not in deferred_running_hosts:
                deferred_running_hosts.append(host)
            detail = f"{host} ({cfg_path})" + (f" [lock: {lock_hit}]" if lock_hit else "")
            if detail not in deferred_details:
                deferred_details.append(detail)
            deferred_protected_versions.update(_extract_referenced_versions(raw_text, plugin_dir))
            return True
        return False

    # Step 4 — Flip `current` & Swap Real Per-Host Containers (P2-6, R4-1, P1-1, P1-2)
    flip_state: CurrentFlipState = flip_current_pointer(target_version, home=home, pid=pid)
    all_container_swaps: list[SubtreeSwapRecord] = []
    prev_installed_plugins: tuple[Path, bytes | None, int, int, str] | None = None
    step5_error: str = ""
    cas_conflicts: list[str] = []

    try:
        for chost in ("antigravity", "cursor"):
            if chost == "antigravity":
                ag_plugins_dir = home / ".agents" / "plugins"
                should_converge = ag_plugins_dir.exists()
                cpath = ag_plugins_dir / "Minni@Minni"
            else:
                cu_dir = home / ".cursor" / "plugins" / "local" / "minni"
                should_converge = cu_dir.exists() or cu_dir.parent.exists()
                cpath = cu_dir
            if should_converge:
                has_c_delta = _container_has_delta(cpath, vdir, base_semver)
                if not has_c_delta:
                    skipped_surfaces.append(f"{chost}:{cpath}")
                    continue
                if _should_defer_host_write(chost, cpath, ""):
                    continue
                mode = effective_container_mode(chost, wired_doc)
                swaps = converge_host_container(
                    chost,
                    home=home,
                    container_mode=mode,
                    target_version_root=vdir,
                    base_semver=base_semver,
                    pid=pid,
                )
                all_container_swaps.extend(swaps)
                updated_surfaces.append(f"{chost}:{cpath}")

        mkt_swaps, prev_installed_plugins, claude_upd, claude_skp = _converge_claude_local_marketplace(
            home, vdir, target_version, target_git_sha, pid=pid, ts=ts
        )
        all_container_swaps.extend(mkt_swaps)
        updated_surfaces.extend(claude_upd)
        skipped_surfaces.extend(claude_skp)
    except _CasConflictError as exc:
        if exc.conflict_artifact:
            cas_conflicts.append(exc.conflict_artifact)
        step5_error = str(exc)
    except Exception as exc:
        step5_error = f"Step 4 container/marketplace convergence failed: {exc}"

    # Step 5 — Reconcile Host Configs Across All Catalog Surface Kinds (`delta != 0` only, Lock/Running Guard & CAS)
    # Flatten all catalog surfaces + legacy ~/.muse/config.json
    all_surfaces: list[tuple[str, str, Path, dict[str, Any], bool]] = []
    for host_id, hspec in HOST_SURFACE_CATALOG.items():
        rewrite_hazard = str(hspec.get("rewrite_on_run_hazard") or "none") != "none"
        for surf in hspec.get("surfaces", []):
            if not isinstance(surf, dict):
                continue
            kind = str(surf.get("kind") or "")
            if kind in ("plugin_container", "claude_plugin_registry"):
                continue
            if kind == "mcp_view_mirrors":
                for vp_raw in surf.get("paths") or []:
                    vp = Path(expand_for_home(str(vp_raw), home=home))
                    view_spec = {
                        "kind": "mcp_json",
                        "path": str(vp_raw),
                        "key": "mcpServers.minni",
                        "command": surf.get("command"),
                        "args_prefix": surf.get("args_prefix"),
                        "entry": surf.get("entry"),
                        "accepted_entries": [
                            str(surf.get("entry") or ""),
                            "~/.agents/plugins/minni@minni/dist/server.js",
                            "~/.minni/plugin/current/dist/server.js",
                        ],
                    }
                    all_surfaces.append((host_id, "mcp_json", vp, view_spec, rewrite_hazard))
                continue
            p_raw = str(surf.get("path") or "").replace("{base_semver}", base_semver)
            p = Path(expand_for_home(p_raw, home=home))
            all_surfaces.append((host_id, kind, p, surf, rewrite_hazard))

    muse_legacy_cfg = home / ".muse" / "config.json"
    if muse_legacy_cfg.is_file():
        all_surfaces.append(
            (
                "muse",
                "mcp_json",
                muse_legacy_cfg,
                {
                    "kind": "mcp_json",
                    "path": "~/.muse/config.json",
                    "key": "mcpServers.minni",
                    "command": "node",
                    "entry": "~/.minni/plugin/current/dist/server.js",
                },
                True,
            )
        )

    for host, kind, cfg_path, surf_spec, rewrite_on_run in all_surfaces:
        if step5_error:
            break

        if kind == "commands_dir":
            if not cfg_path.exists() and not cfg_path.is_symlink():
                continue
            expected_target = expand_for_home(str(surf_spec.get("entry") or ""), home=home)
            if cfg_path.is_symlink():
                try:
                    pre_lstat = cfg_path.lstat()
                    cur_target = os.readlink(cfg_path)
                except OSError as exc:
                    step5_error = f"failed to read symlink for {host} ({cfg_path}): {exc}"
                    break
                if cur_target == expected_target:
                    skipped_surfaces.append(f"{host}:{cfg_path}")
                    continue
                if _should_defer_host_write(host, cfg_path, cur_target):
                    continue
                tmp_link = cfg_path.parent / f".{cfg_path.name}.tmp.{pid}"
                try:
                    if tmp_link.exists() or tmp_link.is_symlink():
                        tmp_link.unlink(missing_ok=True)
                    os.symlink(expected_target, tmp_link)
                    lock_hit = _check_host_lockfile(home, host, cfg_path, retries=1)
                    if lock_hit:
                        raise _CasConflictError(host, cfg_path, f"lockfile present before symlink replace: {lock_hit}")
                    now_lstat = cfg_path.lstat()
                    now_target = os.readlink(cfg_path)
                    if (now_lstat.st_ino, now_lstat.st_mtime_ns, now_target) != (
                        pre_lstat.st_ino,
                        pre_lstat.st_mtime_ns,
                        cur_target,
                    ):
                        raise _CasConflictError(
                            host, cfg_path, "concurrent modification of commands_dir symlink before replace"
                        )
                    os.replace(tmp_link, cfg_path)
                except _CasConflictError as exc:
                    if exc.conflict_artifact:
                        cas_conflicts.append(exc.conflict_artifact)
                    step5_error = str(exc)
                    break
                finally:
                    if tmp_link.exists() or tmp_link.is_symlink():
                        tmp_link.unlink(missing_ok=True)
                commands_symlink_rollbacks.append((cfg_path, cur_target))
                updated_surfaces.append(f"{host}:{cfg_path}")
            continue

        if cfg_path.is_symlink() and not cfg_path.exists():
            step5_error = f"broken symlink surface for {host} ({cfg_path})"
            break
        if not cfg_path.is_file():
            continue

        try:
            pre_ino, pre_mtime_ns, pre_sha256, pre_bytes = _file_cas_tuple(cfg_path)
            pre_mode = stat.S_IMODE(cfg_path.stat().st_mode)
            raw_text = pre_bytes.decode("utf-8")
        except Exception as exc:
            step5_error = f"unreadable surface for {host} ({cfg_path}): {exc}"
            break

        hspec = HOST_SURFACE_CATALOG[host]

        if kind == "mcp_json":
            try:
                doc = json.loads(raw_text)
                if not isinstance(doc, dict):
                    raise ValueError("root is not a JSON object")
            except Exception as exc:
                step5_error = f"unparseable JSON surface for {host} ({cfg_path}): {exc}"
                break
            container_key = "mcp" if surf_spec.get("key") == "mcp.minni" else "mcpServers"
            servers = doc.get(container_key)
            live_minni = servers.get("minni") if isinstance(servers, dict) else None
            diff = evaluate_mcp_surface_diff(
                live_minni,
                host_id=host,
                agent_id=str(hspec["agent_id"]),
                vault_path=str(hspec["vault_path"]),
                surface_spec=surf_spec,
                home=home,
            )
            if not diff.has_delta:
                skipped_surfaces.append(f"{host}:{cfg_path}")
                continue
            if _should_defer_host_write(host, cfg_path, raw_text):
                continue
            prev_minni = copy.deepcopy(live_minni)
            new_doc = copy.deepcopy(doc)
            if not isinstance(new_doc.get(container_key), dict):
                new_doc[container_key] = {}
            new_doc[container_key]["minni"] = diff.desired_entry
            new_bytes = (json.dumps(new_doc, indent=2) + "\n").encode("utf-8")
            try:
                _archive_file_before_write(home, cfg_path, ts)
                post_ino, post_mtime_ns, post_sha256 = _cas_write_surface(
                    cfg_path,
                    pre_ino,
                    pre_mtime_ns,
                    pre_sha256,
                    new_bytes,
                    home=home,
                    host=host,
                    ts=ts,
                    mode=pre_mode,
                )
            except _CasConflictError as exc:
                if exc.conflict_artifact:
                    cas_conflicts.append(exc.conflict_artifact)
                step5_error = str(exc)
                break
            rollback_records.append(
                _ConfigRollbackRecord(
                    host=host,
                    path=cfg_path,
                    rewrite_on_run=rewrite_on_run,
                    pre_bytes=pre_bytes,
                    pre_mtime_ns=pre_mtime_ns,
                    pre_sha256=pre_sha256,
                    previous_minni_entry=prev_minni,
                    post_step5_mtime_ns=post_mtime_ns,
                    post_step5_sha256=post_sha256,
                    pre_ino=pre_ino,
                    post_step5_ino=post_ino,
                    surface_kind="mcp_json",
                    pre_mode=pre_mode,
                )
            )
            updated_surfaces.append(f"{host}:{cfg_path}")

        elif kind == "mcp_toml":
            try:
                doc = tomllib.loads(raw_text)
            except Exception as exc:
                step5_error = f"unparseable TOML surface for {host} ({cfg_path}): {exc}"
                break
            servers = doc.get("mcp_servers")
            live_minni = servers.get("minni") if isinstance(servers, dict) else None
            diff = evaluate_mcp_surface_diff(
                live_minni,
                host_id=host,
                agent_id=str(hspec["agent_id"]),
                vault_path=str(hspec["vault_path"]),
                surface_spec=surf_spec,
                home=home,
            )
            if not diff.has_delta:
                skipped_surfaces.append(f"{host}:{cfg_path}")
                continue
            if _should_defer_host_write(host, cfg_path, raw_text):
                continue
            prev_minni = copy.deepcopy(live_minni)
            new_text = _render_toml_with_updated_minni(raw_text, diff.desired_entry)
            new_bytes = new_text.encode("utf-8")
            try:
                _archive_file_before_write(home, cfg_path, ts)
                post_ino, post_mtime_ns, post_sha256 = _cas_write_surface(
                    cfg_path,
                    pre_ino,
                    pre_mtime_ns,
                    pre_sha256,
                    new_bytes,
                    home=home,
                    host=host,
                    ts=ts,
                    mode=pre_mode,
                )
            except _CasConflictError as exc:
                if exc.conflict_artifact:
                    cas_conflicts.append(exc.conflict_artifact)
                step5_error = str(exc)
                break
            rollback_records.append(
                _ConfigRollbackRecord(
                    host=host,
                    path=cfg_path,
                    rewrite_on_run=rewrite_on_run,
                    pre_bytes=pre_bytes,
                    pre_mtime_ns=pre_mtime_ns,
                    pre_sha256=pre_sha256,
                    previous_minni_entry=prev_minni,
                    post_step5_mtime_ns=post_mtime_ns,
                    post_step5_sha256=post_sha256,
                    pre_ino=pre_ino,
                    post_step5_ino=post_ino,
                    surface_kind="mcp_toml",
                    pre_mode=pre_mode,
                )
            )
            updated_surfaces.append(f"{host}:{cfg_path}")

        elif kind in ("hooks_json", "launcher_wrapper", "hook_wrapper", "js_bridge", "rules_md", "skill_copy", "claude_marketplace"):
            if kind in ("launcher_wrapper", "hook_wrapper"):
                chk = subprocess.run(["bash", "-n", str(cfg_path)], capture_output=True, text=True, check=False)
                if chk.returncode != 0:
                    step5_error = f"unparseable shell wrapper for {host} ({cfg_path}): {chk.stderr.strip()}"
                    break
            elif kind == "claude_marketplace":
                try:
                    json.loads(raw_text)
                except Exception as exc:
                    step5_error = f"unparseable claude_marketplace JSON for {host} ({cfg_path}): {exc}"
                    break
            try:
                has_delta, desired_text = _reconcile_text_surface(
                    raw_text,
                    kind="hooks_json" if kind == "claude_marketplace" else kind,
                    surf_spec=surf_spec,
                    home=home,
                )
            except Exception as exc:
                step5_error = f"unparseable {kind} surface for {host} ({cfg_path}): {exc}"
                break
            if not has_delta:
                skipped_surfaces.append(f"{host}:{cfg_path}")
                continue
            if _should_defer_host_write(host, cfg_path, raw_text):
                continue
            target_mode = (pre_mode | 0o111) if kind in ("launcher_wrapper", "hook_wrapper") else pre_mode
            new_bytes = desired_text.encode("utf-8")
            try:
                _archive_file_before_write(home, cfg_path, ts)
                post_ino, post_mtime_ns, post_sha256 = _cas_write_surface(
                    cfg_path,
                    pre_ino,
                    pre_mtime_ns,
                    pre_sha256,
                    new_bytes,
                    home=home,
                    host=host,
                    ts=ts,
                    mode=target_mode,
                )
            except _CasConflictError as exc:
                if exc.conflict_artifact:
                    cas_conflicts.append(exc.conflict_artifact)
                step5_error = str(exc)
                break
            rollback_records.append(
                _ConfigRollbackRecord(
                    host=host,
                    path=cfg_path,
                    rewrite_on_run=rewrite_on_run,
                    pre_bytes=pre_bytes,
                    pre_mtime_ns=pre_mtime_ns,
                    pre_sha256=pre_sha256,
                    previous_minni_entry=None,
                    post_step5_mtime_ns=post_mtime_ns,
                    post_step5_sha256=post_sha256,
                    pre_ino=pre_ino,
                    post_step5_ino=post_ino,
                    surface_kind=kind,
                    pre_mode=pre_mode,
                )
            )
            updated_surfaces.append(f"{host}:{cfg_path}")
        else:
            step5_error = f"unsupported catalog surface kind {kind!r} for {host} ({cfg_path})"
            break

    # Step 6 — Post-Flip Verification & CAS Rollback (N5, R4-1, P1-1, P1-3)
    step6_ok = not bool(step5_error)
    step6_reason = step5_error
    if step6_ok:
        try:
            resolve_verified_plugin_root(plugin_dir / "current", expected_base=plugin_dir)
        except Exception as exc:
            step6_ok = False
            step6_reason = f"current pointer verification failed: {exc}"

    if step6_ok and post_flip_verifier is not None:
        try:
            if not post_flip_verifier(home):
                step6_ok = False
                step6_reason = "post_flip_verifier returned False"
        except Exception as exc:
            step6_ok = False
            step6_reason = f"post_flip_verifier raised: {exc}"

    if step6_ok:
        # Verify zero residual delta across all non-deferred catalog surfaces (P1-1)
        from minni.wire.health import audit_host_propagation_health

        post_health = audit_host_propagation_health(home=home, expected_git_sha=target_git_sha)
        non_deferred_drift = [
            d for d in post_health.drifted_surfaces
            if d.get("host") not in deferred_running_hosts
        ]
        if non_deferred_drift:
            first = non_deferred_drift[0]
            step6_ok = False
            step6_reason = (
                f"post-flip surface delta remains for {first.get('host')} "
                f"({first.get('path')}): {first.get('reasons')}"
            )

    if not step6_ok:
        # 1. Restore `current` (handles real-dir `current` via unlink+rename without ENOTDIR, R4-1)
        rollback_current_pointer(flip_state)
        # 2. Restore container subtrees and commands_dir symlinks
        rollback_container_subtrees(all_container_swaps)
        for cmd_link, prev_target in reversed(commands_symlink_rollbacks):
            if prev_target is not None:
                tmp_l = cmd_link.parent / f".{cmd_link.name}.rb.{pid}"
                if tmp_l.exists() or tmp_l.is_symlink():
                    tmp_l.unlink(missing_ok=True)
                os.symlink(prev_target, tmp_l)
                os.replace(tmp_l, cmd_link)
        if prev_installed_plugins is not None:
            inst_path, prev_raw, post_ino, post_mtime_ns, post_sha256 = prev_installed_plugins
            try:
                cur_ino, cur_mtime_ns, cur_sha256, cur_bytes = _file_cas_tuple(inst_path)
            except OSError:
                cas_conflicts.append(str(inst_path))
            else:
                if (cur_ino, cur_mtime_ns, cur_sha256) == (post_ino, post_mtime_ns, post_sha256):
                    if prev_raw is None:
                        inst_path.unlink(missing_ok=True)
                    else:
                        _atomic_write_bytes(inst_path, prev_raw)
                else:
                    cdir = _archive_dir(home, ts) / "rollback-conflict"
                    cdir.mkdir(parents=True, exist_ok=True)
                    cpath = cdir / inst_path.name
                    cpath.write_bytes(cur_bytes)
                    cas_conflicts.append(str(cpath))

        # 3. Restore modified host config files with CAS check on EVERY record; leave live bytes on CAS mismatch (P1-3, New P3 #1)
        for rec in reversed(rollback_records):
            try:
                cur_ino, cur_mtime_ns, cur_sha256, cur_bytes = _file_cas_tuple(rec.path)
            except OSError:
                cas_conflicts.append(str(rec.path))
                continue
            if (cur_ino, cur_mtime_ns, cur_sha256) == (rec.post_step5_ino, rec.post_step5_mtime_ns, rec.post_step5_sha256):
                _atomic_write_bytes(rec.path, rec.pre_bytes, mode=rec.pre_mode)
            else:
                # Concurrent mutation happened between Step 5 and Step 7a rollback:
                # Leave live bytes untouched on disk, archive conflict under rollback-conflict, and record in cas_conflicts!
                conflict_dir = _archive_dir(home, ts) / "rollback-conflict"
                conflict_dir.mkdir(parents=True, exist_ok=True)
                cpath = conflict_dir / rec.path.name
                cpath.write_bytes(cur_bytes)
                cas_conflicts.append(str(cpath))

        return CutoverOutcome(
            exit_code=1,
            status="ROLLED_BACK",
            target_version=target_version,
            target_git_sha=target_git_sha,
            rolled_back=True,
            updated_surfaces=updated_surfaces,
            skipped_surfaces=skipped_surfaces,
            cas_conflicts=cas_conflicts,
            message=step6_reason or "rolled back after Step 6 failure",
        )

    # Step 6 succeeded: archive `.prev.<pid>` and `.current.prev_dir.<pid>` into `~/.minni/archive/<ts>/`
    arch_root = _archive_dir(home, ts)
    archive_container_prev_subtrees(all_container_swaps, arch_root / "containers")
    if flip_state.prev_dir_path and flip_state.prev_dir_path.exists():
        dest_prev_current = arch_root / "current_prev_dir"
        if dest_prev_current.exists():
            shutil.rmtree(dest_prev_current, ignore_errors=True)
        shutil.move(str(flip_state.prev_dir_path), str(dest_prev_current))

    # Write wired.json v2
    new_wired = build_wired_v2(
        active_version=target_version,
        git_commit_sha=target_git_sha,
        home=home,
        existing=wired_doc,
    )
    save_wired_v2(new_wired, home=home)

    # Step 7 — Reference-Aware GC (§4.5, New P1 #3)
    gc_obj = collect_garbage(
        home=home,
        keep_last=2,
        prev_target=flip_state.prev_link_target,
        extra_protected=deferred_protected_versions,
    )
    gc_res = {
        "pruned": list(gc_obj.pruned),
        "retained_in_use": list(gc_obj.retained_in_use),
        "would_prune": list(gc_obj.would_prune),
    }

    if deferred_running_hosts:
        hosts_str = ", ".join(deferred_running_hosts)
        details_str = ", ".join(deferred_details) if deferred_details else hosts_str
        return CutoverOutcome(
            exit_code=3,
            status="DEFERRED_RUNNING_HOST_DELTA",
            target_version=target_version,
            target_git_sha=target_git_sha,
            deferred_running_hosts=deferred_running_hosts,
            updated_surfaces=updated_surfaces,
            skipped_surfaces=skipped_surfaces,
            gc_result=gc_res,
            message=(
                f"DEFERRED_RUNNING_HOST_DELTA: deferred config rewrite for running/locked host(s) "
                f"[{details_str}] to prevent rewrite-on-exit clobber; quit [{hosts_str}] and re-run `minni sync`."
            ),
        )

    return CutoverOutcome(
        exit_code=0,
        status="CONVERGED",
        target_version=target_version,
        target_git_sha=target_git_sha,
        updated_surfaces=updated_surfaces,
        skipped_surfaces=skipped_surfaces,
        gc_result=gc_res,
        message=f"converged all active surfaces to {target_version}",
    )


def reconcile_all_hosts(
    *,
    home: Path | None = None,
    from_repo: Path | None = None,
    target_version: str | None = None,
    target_git_sha: str | None = None,
    dry_run: bool = True,
    running_hosts: set[str] | None = None,
    post_flip_verifier: Callable[[Path], bool] | None = None,
) -> CutoverOutcome:
    """High-level entry point for host reconciliation and migration (`--dry-run` default per §5)."""
    resolved_home = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    plugin_dir = resolved_home / ".minni" / "plugin"

    if target_version is None and from_repo is not None:
        pkg_dir = Path(from_repo) / "plugins" / "minni"
        if (pkg_dir / "dist" / "server.js").is_file() and not dry_run:
            vdir = install_payload(pkg_dir)
            manifest = PayloadManifest.load(vdir / "payload-manifest.json")
            target_version = manifest.version
            target_git_sha = manifest.git_sha

    if target_version is None:
        current = plugin_dir / "current"
        if current.is_symlink() and current.exists():
            target_version = current.resolve().name
        elif plugin_dir.is_dir():
            candidates = [
                d for d in plugin_dir.iterdir()
                if d.is_dir()
                and not d.is_symlink()
                and not d.name.startswith(".")
                and (d / "payload-manifest.json").is_file()
                and all((d / sub).exists() for sub in ("dist", "skills", "commands", "hooks"))
            ]
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                target_version = candidates[0].name

    if target_version is None:
        return CutoverOutcome(
            exit_code=0,
            status="SKIPPED",
            target_version="",
            target_git_sha="",
            message="no staged plugin payload found",
        )

    vdir = plugin_dir / target_version
    if target_git_sha is None:
        if (vdir / "payload-manifest.json").is_file():
            try:
                target_git_sha = PayloadManifest.load(vdir / "payload-manifest.json").git_sha
            except Exception:
                target_git_sha = "unknown"
        else:
            target_git_sha = "unknown"

    return execute_atomic_cutover(
        home=resolved_home,
        target_version=target_version,
        target_git_sha=target_git_sha,
        dry_run=dry_run,
        running_hosts=running_hosts,
        post_flip_verifier=post_flip_verifier,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m minni.wire.cutover",
        description="Execute 7-step atomic cutover and per-host reconciliation (--dry-run default).",
    )
    parser.add_argument("--repo", "--from-repo", dest="from_repo", type=Path, default=None)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--target-version", default=None)
    parser.add_argument("--target-git-sha", default=None)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", default=False, help="Apply changes (overrides --dry-run default)")
    parser.add_argument("--migrate-config", action="store_true", default=False)
    parser.add_argument("--record-ui-scanner-gate", dest="gate_host", default=None)
    parser.add_argument("--verified", choices=["true", "false"], default="true")
    parser.add_argument("--mode", choices=["symlink_subtrees", "staged_copy"], default="symlink_subtrees")
    parser.add_argument("--evidence", default="operator-verified")
    args = parser.parse_args(argv)

    resolved_home = args.home or Path(os.environ.get("HOME") or Path.home())
    if args.gate_host:
        record_ui_scanner_gate(
            args.gate_host,
            container_mode=args.mode,
            evidence=args.evidence,
            verified=(args.verified == "true"),
            home=resolved_home,
            dry_run=(not args.apply),
        )
        print(json.dumps({"recorded_ui_scanner_gate": args.gate_host, "verified": args.verified == "true", "mode": args.mode}))
        return 0

    effective_dry_run = False if args.apply else True
    outcome = reconcile_all_hosts(
        home=resolved_home,
        from_repo=args.from_repo,
        target_version=args.target_version,
        target_git_sha=args.target_git_sha,
        dry_run=effective_dry_run,
    )
    print(json.dumps(outcome.to_dict(), indent=2))
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())

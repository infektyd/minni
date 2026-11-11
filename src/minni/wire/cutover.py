"""7-step atomic cutover, pre-flip scratch probes, and key-scoped CAS rollback (§4.3, §5)."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
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
    _rewrite_node_invocations_with_exec,
    effective_container_mode,
    evaluate_mcp_surface_diff,
    expand_for_home,
    node_invocation_lacks_exec,
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
    pre_bytes: bytes | None
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
    would_abort: list[dict[str, Any]] = field(default_factory=list)
    gc_result: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    stale_locks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
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
            "would_abort": copy.deepcopy(self.would_abort),
            "would-abort": copy.deepcopy(self.would_abort),
            "gc_result": dict(self.gc_result),
            "findings": copy.deepcopy(self.findings),
            "stale_locks": copy.deepcopy(self.stale_locks),
            "message": self.message,
        }
        if self.plan:
            out["plan"] = copy.deepcopy(self.plan)
            for k in (
                "hosts",
                "surfaces",
                "would-write",
                "would_write",
                "would-create",
                "would_create",
                "would-defer",
                "would_defer",
                "would-archive",
                "would_archive",
                "would-skip",
                "would_skip",
                "would-abort",
                "would_abort",
                "findings",
                "stale_locks",
                "gc",
            ):
                if k in self.plan:
                    out[k] = copy.deepcopy(self.plan[k])
        return out


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


_TMP_LOCK_MAX_AGE_SECONDS = 600.0
_TMP_LOCK_PID_RE = re.compile(r"\.tmp\.(\d+)(?:\..+)?$")


def _is_pid_alive(pid: int) -> bool:
    """Return True if `pid` is a currently running OS process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _is_file_open_by_any_process(cand: Path, *, timeout_s: float = 2.0) -> bool:
    """Return True if any OS process currently holds `cand` open (bounded lsof probe, fail-closed on timeout/error).

    `lsof` exit 1 with empty stdout and stderr means not open; any other outcome means held.
    """
    lsof_bin = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").is_file() else "lsof")
    targets: list[str] = [str(cand)]
    try:
        resolved_path = cand.resolve()
        resolved = str(resolved_path)
        if resolved_path.exists() and resolved not in targets:
            targets.append(resolved)
    except OSError:
        pass
    try:
        proc = subprocess.run(
            [lsof_bin, "-F", "p", "--", *targets],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return True
    if proc.returncode == 1 and not (proc.stdout or "").strip() and not (proc.stderr or "").strip():
        return False
    return True


def _probe_tmp_lock(cand: Path, host_id: str) -> tuple[bool, dict[str, Any] | None]:
    """Evaluate a tmp-style lock (`<surface>.tmp[.<pid>[.<hex>]]`).

    - With an embedded `<pid>`: counts as active if `<pid>` is alive, OR its
      `mtime` is under 10 minutes (`600` seconds) old, OR any process still has
      the file open (`lsof` check).
    - Without an embedded `<pid>`: counts as active while any process has the
      file open (bounded `lsof` check), else by `mtime` (`< 600` seconds).
    Returns `(is_active, stale_finding_or_None)`. Never deletes a stale file.
    """
    name = cand.name
    if name.endswith(f".minni-cas.{os.getpid()}") or name == f".minni.tmp.{os.getpid()}":
        return False, None
    m = _TMP_LOCK_PID_RE.search(name)
    embedded_pid: int | None = int(m.group(1)) if m else None
    try:
        st = os.lstat(cand)
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None
    age_s = max(0.0, time.time() - st.st_mtime)
    mtime_fresh = age_s < _TMP_LOCK_MAX_AGE_SECONDS
    if embedded_pid is not None:
        pid_alive = _is_pid_alive(embedded_pid)
        if pid_alive or mtime_fresh:
            return True, None
        open_by_proc = _is_file_open_by_any_process(cand)
        if open_by_proc:
            return True, None
    else:
        pid_alive = False
        open_by_proc = _is_file_open_by_any_process(cand)
        if open_by_proc or mtime_fresh:
            return True, None
    finding: dict[str, Any] = {
        "host": host_id,
        "path": str(cand),
        "kind": "stale_tmp_lock",
        "reason": "stale_tmp_lock",
        "pid": embedded_pid,
        "pid_alive": pid_alive,
        "open_by_process": open_by_proc,
        "mtime": st.st_mtime,
        "age_seconds": round(age_s, 1),
        "detail": (
            f"stale_tmp_lock (pid={embedded_pid} dead, age={int(age_s)}s >= 600s)"
            if embedded_pid is not None
            else f"stale_tmp_lock (pid=None not open, age={int(age_s)}s >= 600s)"
        ),
    }
    return False, finding


def _is_flock_held(cand: Path) -> bool:
    """Return True if `cand` is held or cannot be cleanly acquired (fail-closed R9 P2).

    - Probes in-process (no worker thread).
    - Opens with `O_RDONLY | O_NONBLOCK | O_NOFOLLOW`.
    - `fstat`s the descriptor and refuses a non-regular file (= held).
    - Probes with `flock(LOCK_EX | LOCK_NB)`. On success, immediately calls
      `flock(LOCK_UN)` and closes `fd`, and reports not held (`False`).
    - On `EWOULDBLOCK` / `EAGAIN` or any other error (other than `ENOENT` on open),
      reports held (`True`). Every path closes `fd` before returning.
    """
    open_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(cand), open_flags)
    except FileNotFoundError:
        return False
    except Exception:
        return True
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            return True
        except Exception:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            return True
        return False
    except Exception:
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _record_stale_finding(
    stale_findings: list[dict[str, Any]] | None,
    finding: dict[str, Any] | None,
) -> None:
    if stale_findings is None or finding is None:
        return
    fpath = finding.get("path")
    if not any(f.get("path") == fpath for f in stale_findings):
        stale_findings.append(finding)


def _check_host_lockfile(
    home: Path,
    host_id: str,
    surface_path: Path,
    *,
    retries: int = 3,
    stale_findings: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return active lock/temp path string if a host lockfile or temp sibling is held after short backoff."""
    hspec = HOST_SURFACE_CATALOG.get(host_id) or {}
    lock_glob_raw = str(hspec.get("lock_glob") or "")
    raw_globs: list[str] = [
        str(g) for g in (hspec.get("lock_globs") or ([lock_glob_raw] if lock_glob_raw else [])) if g
    ]
    lock_style = str(
        hspec.get("lock_style")
        or ("flock" if lock_glob_raw.endswith(".lock") else "tmp_pid")
    )
    expanded_globs = [expand_for_home(g, home=home) for g in raw_globs]

    def _scan_once() -> str | None:
        for exp_glob in expanded_globs:
            has_wildcard = any(ch in exp_glob for ch in ("*", "?", "["))
            matched_paths = sorted(glob.glob(exp_glob)) if has_wildcard else [exp_glob]
            for m_str in matched_paths:
                m_path = Path(m_str)
                if lock_style == "flock" or m_path.name.endswith(".lock") or m_path.name == ".Lock":
                    if _is_flock_held(m_path):
                        return m_str
                else:
                    active, finding = _probe_tmp_lock(m_path, host_id)
                    _record_stale_finding(stale_findings, finding)
                    if active:
                        return m_str
        parent = surface_path.parent
        if parent.is_dir():
            for cand in (
                parent / f".{surface_path.name}.lock",
                parent / f"{surface_path.name}.lock",
                parent / ".Lock",
            ):
                if _is_flock_held(cand):
                    return str(cand)
            for pat in (
                f"{surface_path.name}.tmp",
                f".{surface_path.name}.tmp",
                f"{surface_path.name}.tmp.*",
                f".{surface_path.name}.tmp.*",
            ):
                for m_path in sorted(parent.glob(pat)):
                    active, finding = _probe_tmp_lock(m_path, host_id)
                    _record_stale_finding(stale_findings, finding)
                    if active:
                        return str(m_path)
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
    """Run pre-flip MCP stdio `initialize` + `tools/list` (>=12 tools) and `SessionStart` hook probe."""
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

        if proc.returncode != 0 or not init_ok or tools_count < 12:
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
        for sub in ("dist", "skills", "hooks"):
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
    """Probe catalog host wrapper launchers (`command` / `args_prefix` / `MINNI_AGENT_ID`) across all catalog surfaces (P1-4)."""
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
                if not cmd_path.is_file() or not os.access(cmd_path, os.X_OK):
                    return (
                        False,
                        f"catalog launcher probe failed for {host_id} ({expanded_cmd}): wrapper missing or not executable",
                    )
            key = (
                expanded_cmd,
                tuple(raw_prefix),
                agent_id if (expanded_cmd != "node" or raw_prefix) else "",
            )
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
        except (OSError, subprocess.SubprocessError):
            running.add(host)
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            running.add(host)
        elif proc.returncode != 1 or proc.stderr.strip():
            running.add(host)
    return running


def _extract_container_referenced_text(cpath: Path) -> str:
    parts: list[str] = []
    if cpath.is_symlink():
        try:
            parts.append(os.readlink(cpath))
        except OSError:
            pass
    if cpath.is_dir() and not cpath.is_symlink():
        for sub in ("dist", "node_modules", "skills", "commands", "hooks"):
            sub_p = cpath / sub
            if sub_p.is_symlink():
                try:
                    parts.append(os.readlink(sub_p))
                except OSError:
                    pass
        try:
            for child in cpath.iterdir():
                if child.is_symlink():
                    try:
                        parts.append(os.readlink(child))
                    except OSError:
                        pass
        except OSError:
            pass
        for rel in (".claude-plugin/plugin.json", ".cursor-plugin/plugin.json", "package.json"):
            fp = cpath / rel
            if fp.is_file():
                try:
                    parts.append(fp.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
    return "\n".join(parts)


def _mcp_surface_expected_on_home(
    home: Path,
    host_id: str,
    spath: Path,
    wired_doc: dict[str, Any] | None,
) -> bool:
    _ = (host_id, wired_doc)
    if spath.name == ".claude.json":
        return (home / ".claude").is_dir()
    if spath.name == "kilo.json":
        return (home / ".config" / "kilo").is_dir() and (
            (home / ".config" / "kilo" / "plugin").is_dir()
            or not (home / ".config" / "kilo" / "plugins" / "minni").is_dir()
        )
    if spath == home / ".muse" / "mcp.json":
        return spath.parent.is_dir() and not (spath.parent / "config.json").is_file()
    if spath == home / ".config" / "muse" / "settings.json":
        return spath.parent.is_dir() and not (home / ".muse" / "config.json").is_file()
    return spath.parent.is_dir()


def _commands_dir_expected_on_home(home: Path, spath: Path) -> bool:
    return spath.parent.is_dir() or (spath.parent.parent != home and spath.parent.parent.is_dir())


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
        sub_p = container_dir / sub
        if not sub_p.exists():
            return True
        if sub_p.is_symlink():
            try:
                if _VERSIONED_PLUGIN_RE.search(os.readlink(sub_p)):
                    return True
            except OSError:
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
    if forbid_mcp_json:
        if (version_dir / ".claude-plugin" / "plugin.json").is_file() and not (
            container_dir / ".claude-plugin" / "plugin.json"
        ).is_file():
            return True
        src_pkg = version_dir / "package.json"
        if src_pkg.is_file():
            dest_pkg = container_dir / "package.json"
            try:
                if not dest_pkg.is_file() or dest_pkg.read_bytes() != src_pkg.read_bytes():
                    return True
            except OSError:
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
    inst_path: Path | None = None,
    host: str = "claude-code",
    should_defer_fn: Callable[[str, Path, str], bool] | None = None,
) -> tuple[list[SubtreeSwapRecord], tuple[Path, bytes | None, int, int, str] | None, list[str], list[str]]:
    effective_inst_path = inst_path if inst_path is not None else (home / ".claude" / "plugins" / "installed_plugins.json")
    claude_plugins = effective_inst_path.parent
    if not claude_plugins.is_dir():
        return [], None, [], []
    if (claude_plugins / "cache").is_dir() and not effective_inst_path.is_file() and not (claude_plugins / "marketplaces").is_dir():
        return [], None, [], []

    updated: list[str] = []
    skipped: list[str] = []
    base_semver = target_version.split("+", 1)[0] if target_version else "0.6.0"
    mkt_plugin_dir = claude_plugins / "marketplaces" / "minni-local" / "plugins" / "minni"
    mkt_has_delta = _container_has_delta(mkt_plugin_dir, version_dir, base_semver, forbid_mcp_json=True)

    pre_inst_tuple: tuple[int, int, str] | None = None
    prev_raw: bytes | None = None
    doc: dict[str, Any] = {"version": 2, "plugins": {}}
    if effective_inst_path.is_file():
        inst_ino, inst_mtime_ns, inst_sha256, prev_raw = _file_cas_tuple(effective_inst_path)
        pre_inst_tuple = (inst_ino, inst_mtime_ns, inst_sha256)
        parsed = json.loads(prev_raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(f"installed_plugins.json at {effective_inst_path} is not a JSON object")
        doc = parsed

    plugins_map = doc.setdefault("plugins", {})
    needs_write = pre_inst_tuple is None
    if isinstance(plugins_map, dict):
        expanded_install_path = str(mkt_plugin_dir)
        expanded_project_path = str(claude_plugins / "marketplaces" / "minni-local")
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

    swaps: list[SubtreeSwapRecord] = []
    if mkt_has_delta:
        mkt_ref_text = _extract_container_referenced_text(mkt_plugin_dir)
        if should_defer_fn is not None and should_defer_fn(host, mkt_plugin_dir, mkt_ref_text):
            if needs_write:
                should_defer_fn(
                    host,
                    effective_inst_path,
                    prev_raw.decode("utf-8", errors="replace") if prev_raw else "",
                )
            return [], None, [], []
        mkt_plugin_dir.mkdir(parents=True, exist_ok=True)
        stable_current = home / ".minni" / "plugin" / "current"
        swaps = swap_container_subtrees(
            mkt_plugin_dir,
            stable_current_root=stable_current,
            target_version_root=version_dir,
            container_mode="symlink_subtrees",
            pid=pid,
        )
        try:
            from minni.wire.install import sanitize_staged_payload

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
                            host=host,
                            ts=ts,
                        )
            sanitize_staged_payload(mkt_plugin_dir, version=base_semver, remove_mcp_json=True)
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
                        host=host,
                        ts=ts,
                    )
        except Exception:
            rollback_container_subtrees(swaps)
            raise
        updated.append(f"{host}:{mkt_plugin_dir}")
    else:
        skipped.append(f"{host}:{mkt_plugin_dir}")

    if not needs_write:
        skipped.append(f"{host}:{effective_inst_path}")
        return swaps, None, updated, skipped

    if should_defer_fn is not None and should_defer_fn(
        host,
        effective_inst_path,
        prev_raw.decode("utf-8", errors="replace") if prev_raw else "",
    ):
        return swaps, None, updated, skipped

    new_inst_bytes = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    post_ino, post_mtime_ns, post_sha256 = _cas_write_or_create_surface(
        effective_inst_path,
        pre_inst_tuple,
        new_inst_bytes,
        home=home,
        host=host,
        ts=ts,
    )
    updated.append(f"{host}:{effective_inst_path}")
    return swaps, (effective_inst_path, prev_raw, post_ino, post_mtime_ns, post_sha256), updated, skipped


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
        if use_exec and node_invocation_lacks_exec(updated, "hooks_json"):
            updated = _rewrite_node_invocations_with_exec(updated, "hooks_json")
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
        if use_exec and node_invocation_lacks_exec(updated, kind):
            updated = _rewrite_node_invocations_with_exec(updated, kind)
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


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|key|password|passwd|credential|auth|bearer|private|cookie|session)"
)
_SENSITIVE_ARG_FLAG_RE = re.compile(
    r"(?i)^(--?(?:token|api[-_]?key|secret|password|passwd|credential|auth|bearer|header)|-e|--env)$"
)
_TOKEN_PREFIX_RE = re.compile(
    r"(?:"
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"
    r"|\b(?:gh[pousr])_[A-Za-z0-9]{12,}"
    r"|\bgithub[_]pat[_][A-Za-z0-9_]{12,}"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|\bgl[p]at-[A-Za-z0-9_-]{10,}"
    r"|\bAIza[A-Za-z0-9_-]{20,}"
    r"|\b(?:npm|pypi)[_-][A-Za-z0-9_-]{12,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"
    r")"
)
_BEARER_BASIC_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s@]+)@")
_INLINE_SENSITIVE_KV_RE = re.compile(
    r"(?i)(--?(?:token|api[-_]?key|secret|password|passwd|credential|auth|bearer)[=:])(\S+)"
)
_URL_QUERY_SENSITIVE_RE = re.compile(
    r"(?i)([?&](?:token|api[-_]?key|secret|password|passwd|credential|auth|key|access_token)=)([^&#\s]+)"
)
_ENV_ASSIGN_SENSITIVE_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Za-z0-9_]*=)(\S+)"
)


def _default_repo_root() -> Path | None:
    """Resolve the checkout repo root containing `src/minni/wire/cutover.py` when present."""
    try:
        cand = Path(__file__).resolve().parents[3]
        if (cand / "pyproject.toml").is_file() and (cand / "plugins" / "minni").is_dir():
            return cand
    except Exception:
        pass
    return None


def _stage_repo_payload_to_temp(
    repo_root: Path,
    *,
    version_override: str | None = None,
    sha_override: str | None = None,
) -> tuple[Path, PayloadManifest]:
    """Build and sanitize the staged payload tree that `install_payload` would install, in a temp dir outside HOME."""
    from minni.wire.from_repo import JUNK, MANIFEST_STAMP_PATHS, PAYLOAD_ITEMS, _git_sha
    from minni.wire.install import _sanitize_staged_payload
    from minni.wire.manifest import deterministic_built_at, dev_version, sha256_file

    repo_root = repo_root.expanduser().resolve()
    plugin_src = repo_root / "plugins" / "minni"
    if not (repo_root / "pyproject.toml").is_file() or not (plugin_src / "dist" / "server.js").is_file():
        raise ValueError(f"repo payload missing pyproject.toml or plugins/minni/dist/server.js under {repo_root}")
    version = version_override or dev_version(repo_root)
    git_sha = sha_override or _git_sha(repo_root)
    tmp = Path(tempfile.mkdtemp(prefix="minni-cutover-payload-"))
    try:
        for item in PAYLOAD_ITEMS:
            src = plugin_src / item
            if not src.exists():
                raise ValueError(f"missing payload item: {src}")
            dest = tmp / item
            if src.is_dir():
                shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*JUNK))
            else:
                shutil.copy2(src, dest)

        staged_server = tmp / "dist" / "server.js"
        if staged_server.is_file():
            try:
                srv_txt = staged_server.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise RuntimeError(f"failed to read staged server.js ({staged_server}): {exc}") from exc
            if re.search(r"(?m)^\s*import\b[^;\n]*@modelcontextprotocol/sdk", srv_txt):
                tmp_nm = tmp / "node_modules"
                src_nm = plugin_src / "node_modules"
                created_nm_link = False
                if src_nm.is_dir() and not tmp_nm.exists() and not tmp_nm.is_symlink():
                    os.symlink(src_nm, tmp_nm)
                    created_nm_link = True
                try:
                    subprocess.run(
                        [
                            "node",
                            "--input-type=module",
                            "-e",
                            (
                                'import * as esbuild from "esbuild";'
                                "await esbuild.build({"
                                "entryPoints:[process.argv[1]],"
                                "bundle:true,"
                                'platform:"node",'
                                'format:"esm",'
                                "outfile:process.argv[1],"
                                "allowOverwrite:true,"
                                'external:["node:*"],'
                                "nodePaths:[process.argv[2]],"
                                'logLevel:"silent"'
                                "});"
                            ),
                            str(staged_server),
                            str(src_nm),
                        ],
                        cwd=str(tmp),
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                except subprocess.CalledProcessError as exc:
                    raise RuntimeError(
                        f"esbuild bundling failed (rc={exc.returncode}): {(exc.stderr or '').strip()}"
                    ) from exc
                finally:
                    if created_nm_link and tmp_nm.is_symlink():
                        tmp_nm.unlink(missing_ok=True)

        from minni.wire.install import sanitize_staged_payload

        for rel in MANIFEST_STAMP_PATHS:
            p = tmp / rel
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["version"] = version
                p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        sanitize_staged_payload(tmp, version=version)

        files: dict[str, str] = {}
        for path in sorted(tmp.rglob("*")):
            if path.is_file() and not any(part in JUNK for part in path.parts):
                rel_p = path.relative_to(tmp).as_posix()
                files[rel_p] = sha256_file(path)

        manifest_path = tmp / "payload-manifest.json"
        built_at = deterministic_built_at(repo_root)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "version": version,
                    "git_sha": git_sha,
                    "built_at": built_at,
                    "node_engine": ">=20",
                    "files": files,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        files["payload-manifest.json"] = sha256_file(manifest_path)
        manifest = PayloadManifest(
            schema=1,
            version=version,
            git_sha=git_sha,
            built_at=built_at,
            node_engine=">=20",
            files=files,
            path=manifest_path,
        )
        _sanitize_staged_payload(tmp, manifest)
        return tmp, manifest
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _install_staged_payload_to_home(
    staged_dir: Path,
    manifest: PayloadManifest,
    home: Path,
) -> Path:
    """Install a built staged payload tree into `<home>/.minni/plugin/<version>` without flipping `current` yet."""
    from minni.wire.install import _copy_tree
    from minni.wire.manifest import hash_mismatches

    base = home / ".minni" / "plugin"
    base.mkdir(parents=True, exist_ok=True)
    target = base / manifest.version
    if target.is_dir():
        try:
            mismatched = hash_mismatches(manifest, target)
            if not mismatched:
                return target
        except Exception:
            pass
    stage = base / f".staging-{manifest.version}-{os.getpid()}"
    try:
        _copy_tree(staged_dir, stage, manifest)
        if target.exists():
            qdir = base / f".quarantine-{manifest.version}-{os.getpid()}"
            if qdir.exists():
                shutil.rmtree(qdir, ignore_errors=True)
            target.rename(qdir)
        os.rename(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return target


def _redact_string(val: str) -> str:
    """Redact token-shaped substrings, credentials, and sensitive inline assignments within a string."""
    out = _BEARER_BASIC_RE.sub(r"\1 <REDACTED>", val)
    out = _URL_USERINFO_RE.sub(r"\1<REDACTED>@", out)
    out = _INLINE_SENSITIVE_KV_RE.sub(r"\1<REDACTED>", out)
    out = _URL_QUERY_SENSITIVE_RE.sub(r"\1<REDACTED>", out)
    out = _ENV_ASSIGN_SENSITIVE_RE.sub(r"\1<REDACTED>", out)
    out = _TOKEN_PREFIX_RE.sub("<REDACTED>", out)
    return out


def _redact_minni_entry(entry: Any) -> Any:
    """Return a copy of a `minni` entry with EVERY env value redacted to `<REDACTED>` and lists/strings walked for tokens."""
    if entry is None:
        return None
    if isinstance(entry, str):
        return _redact_string(entry)
    if isinstance(entry, list):
        out_list: list[Any] = []
        skip_next = False
        for idx, item in enumerate(entry):
            if skip_next:
                out_list.append("<REDACTED>")
                skip_next = False
                continue
            if isinstance(item, str) and _SENSITIVE_ARG_FLAG_RE.match(item.strip()) and idx + 1 < len(entry):
                out_list.append(item)
                skip_next = True
                continue
            out_list.append(_redact_minni_entry(item))
        return out_list
    if isinstance(entry, dict):
        redacted: dict[str, Any] = {}
        for k, v in entry.items():
            k_str = str(k)
            if k_str == "env" and isinstance(v, dict):
                redacted[k_str] = {str(ek): "<REDACTED>" for ek in v}
            elif _SENSITIVE_KEY_RE.search(k_str):
                redacted[k_str] = "<REDACTED>"
            else:
                redacted[k_str] = _redact_minni_entry(v)
        return redacted
    return copy.deepcopy(entry)


def _build_dry_run_plan(
    *,
    home: Path,
    target_version: str,
    target_git_sha: str,
    running_hosts: set[str] | None,
    repo_root: Path | None,
    ts: str,
    base_semver: str,
    payload_ref_dir: Path | None = None,
) -> CutoverOutcome:
    """Compute lossless per-surface plan (`would-write`, `would-create`, `would-defer`, `would-archive`, `would-skip`, and GC `would-prune`/`would-keep`) with zero disk writes."""
    plugin_dir = home / ".minni" / "plugin"
    vdir = plugin_dir / target_version
    ephemeral_staged_dir: Path | None = None
    if payload_ref_dir is not None and payload_ref_dir.is_dir():
        ref_vdir = payload_ref_dir
    elif vdir.is_dir():
        ref_vdir = vdir
    else:
        eff_repo = (
            repo_root
            if (repo_root is not None and (repo_root / "plugins" / "minni").is_dir())
            else _default_repo_root()
        )
        if eff_repo is not None and (eff_repo / "plugins" / "minni").is_dir():
            try:
                ephemeral_staged_dir, _ = _stage_repo_payload_to_temp(
                    eff_repo,
                    version_override=target_version or None,
                    sha_override=target_git_sha or None,
                )
                ref_vdir = ephemeral_staged_dir
            except Exception as exc:
                return CutoverOutcome(
                    exit_code=1,
                    status="ABORTED_PRE_FLIP",
                    target_version=target_version,
                    target_git_sha=target_git_sha,
                    aborted_pre_flip=True,
                    message=f"payload_staging_failed: {type(exc).__name__}",
                )
        else:
            return CutoverOutcome(
                exit_code=1,
                status="ABORTED_PRE_FLIP",
                target_version=target_version,
                target_git_sha=target_git_sha,
                aborted_pre_flip=True,
                message=f"payload_staging_failed: target payload directory does not exist ({vdir}) and no repo payload available to stage",
            )

    try:
        return _build_dry_run_plan_with_ref(
            home=home,
            plugin_dir=plugin_dir,
            vdir=vdir,
            ref_vdir=ref_vdir,
            target_version=target_version,
            target_git_sha=target_git_sha,
            running_hosts=running_hosts,
            ts=ts,
            base_semver=base_semver,
        )
    finally:
        if ephemeral_staged_dir is not None:
            shutil.rmtree(ephemeral_staged_dir, ignore_errors=True)


def _build_dry_run_plan_with_ref(
    *,
    home: Path,
    plugin_dir: Path,
    vdir: Path,
    ref_vdir: Path,
    target_version: str,
    target_git_sha: str,
    running_hosts: set[str] | None,
    ts: str,
    base_semver: str,
) -> CutoverOutcome:
    wired_doc = _load_wired(plugin_dir / "wired.json")
    active_running = (
        set(running_hosts)
        if running_hosts is not None
        else _detect_running_rewrite_hosts()
    )

    hosts_plan: dict[str, dict[str, Any]] = {}
    for host_id, hspec in HOST_SURFACE_CATALOG.items():
        hosts_plan[host_id] = {
            "agent_id": str(hspec.get("agent_id") or host_id),
            "running": host_id in active_running,
            "surfaces": [],
            "would-write": [],
            "would-create": [],
            "would-defer": [],
            "would-archive": [],
            "would-skip": [],
            "would-abort": [],
            "would_abort": [],
            "stale_locks": [],
        }

    would_write: list[dict[str, Any]] = []
    would_create: list[dict[str, Any]] = []
    would_defer: list[dict[str, Any]] = []
    would_archive: list[dict[str, Any]] = []
    would_skip: list[dict[str, Any]] = []
    would_abort: list[dict[str, Any]] = []
    stale_findings: list[dict[str, Any]] = []
    all_surface_plans: list[dict[str, Any]] = []
    updated_surfaces: list[str] = []
    skipped_surfaces: list[str] = []
    deferred_running_hosts: list[str] = []
    deferred_protected_versions: set[str] = set()

    archive_base = home / ".minni" / "archive" / f"{ts}-dry-run"

    def _preview_archive_dest(path: Path) -> str:
        try:
            rel = path.relative_to(home)
        except ValueError:
            rel = Path(path.name)
        return str(archive_base / rel)

    def _check_defer(host: str, cfg_path: Path, raw_text: str) -> str | None:
        lock_hit = _check_host_lockfile(
            home,
            host,
            cfg_path,
            retries=1,
            stale_findings=stale_findings,
        )
        is_running_rewrite_host = (
            host in ("muse", "claude-desktop", "grok-build", "devin", "cursor")
            and host in active_running
        )
        if lock_hit or is_running_rewrite_host:
            if host not in deferred_running_hosts:
                deferred_running_hosts.append(host)
            deferred_protected_versions.update(_extract_referenced_versions(raw_text, plugin_dir))
            if lock_hit and is_running_rewrite_host:
                return f"host {host} is running and lockfile is present ({lock_hit})"
            if lock_hit:
                return f"host {host} lockfile present ({lock_hit})"
            return f"host {host} is running (rewrite-on-run hazard)"
        return None

    def _record_surface(
        host: str,
        kind: str,
        cfg_path: Path,
        action: str,
        *,
        diff: dict[str, Any] | None = None,
        reason: str = "",
        archive_dest: str | None = None,
    ) -> None:
        norm_action = "would_abort" if action in ("would-abort", "would_abort") else action
        item: dict[str, Any] = {
            "host": host,
            "kind": kind,
            "path": str(cfg_path),
            "action": norm_action,
        }
        if diff is not None:
            item["diff"] = diff
        if reason:
            item["reason"] = reason
        if archive_dest:
            item["archive_dest"] = archive_dest
        all_surface_plans.append(item)
        h_bucket = hosts_plan.setdefault(
            host,
            {
                "agent_id": host,
                "running": host in active_running,
                "surfaces": [],
                "would-write": [],
                "would-create": [],
                "would-defer": [],
                "would-archive": [],
                "would-skip": [],
                "would-abort": [],
                "would_abort": [],
            },
        )
        h_bucket["surfaces"].append(item)
        if action in ("would-abort", "would_abort"):
            would_abort.append(item)
            h_bucket.setdefault("would-abort", []).append(item)
            h_bucket.setdefault("would_abort", []).append(item)
        elif action in h_bucket:
            h_bucket[action].append(item)
        if action == "would-write":
            would_write.append(item)
            updated_surfaces.append(f"{host}:{cfg_path}")
        elif action == "would-create":
            would_create.append(item)
            updated_surfaces.append(f"{host}:{cfg_path}")
        elif action == "would-defer":
            would_defer.append(item)
        elif action == "would-skip":
            would_skip.append(item)
            skipped_surfaces.append(f"{host}:{cfg_path}")
        if archive_dest:
            arch_item = {
                "host": host,
                "kind": kind,
                "path": str(cfg_path),
                "archive_dest": archive_dest,
                "reason": "pre_write_backup",
            }
            would_archive.append(arch_item)
            h_bucket["would-archive"].append(arch_item)

    container_and_registry_surfaces: list[tuple[str, str, Path, dict[str, Any], bool]] = []
    config_and_file_surfaces: list[tuple[str, str, Path, dict[str, Any], bool]] = []
    for host_id, hspec in HOST_SURFACE_CATALOG.items():
        rewrite_hazard = str(hspec.get("rewrite_on_run_hazard") or "none") != "none"
        for surf in hspec.get("surfaces", []):
            if not isinstance(surf, dict):
                continue
            kind = str(surf.get("kind") or "")
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
                    config_and_file_surfaces.append((host_id, "mcp_json", vp, view_spec, rewrite_hazard))
                continue
            p_raw = str(surf.get("path") or "").replace("{base_semver}", base_semver)
            p = Path(expand_for_home(p_raw, home=home))
            if kind in ("plugin_container", "claude_plugin_registry"):
                container_and_registry_surfaces.append((host_id, kind, p, surf, rewrite_hazard))
            else:
                config_and_file_surfaces.append((host_id, kind, p, surf, rewrite_hazard))

    all_surfaces: list[tuple[str, str, Path, dict[str, Any], bool]] = (
        container_and_registry_surfaces + config_and_file_surfaces
    )
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

    # H16 pre-flip abort check in dry-run: if any commands_dir exists as a non-symlink (real directory/file),
    # report the abort as a non-zero result and do not present other surfaces as a plan that would be committed.
    for host, kind, cfg_path, surf_spec, _rewrite_on_run in all_surfaces:
        if kind == "commands_dir" and cfg_path.exists() and not cfg_path.is_symlink():
            expected_target = expand_for_home(str(surf_spec.get("entry") or ""), home=home)
            after_entry = {"kind": "commands_dir", "symlink_target": expected_target}
            diff_obj = {
                "before": {"kind": "non_symlink_commands_dir"},
                "after": after_entry,
                "reasons": ["broken_or_non_symlink_commands_dir"],
            }
            _record_surface(
                host,
                kind,
                cfg_path,
                "would_abort",
                diff=diff_obj,
                reason="broken_or_non_symlink_commands_dir",
            )

    if would_abort:
        abort_items = ", ".join(f"{a.get('host')}:{a.get('path')}" for a in would_abort)
        empty_gc: dict[str, Any] = {
            "would-prune": [],
            "would_prune": [],
            "would-keep": [],
            "would_keep": [],
            "retained_in_use": [],
            "pruned": [],
        }
        abort_plan_doc: dict[str, Any] = {
            "target_version": target_version,
            "target_git_sha": target_git_sha,
            "hosts": hosts_plan,
            "surfaces": all_surface_plans,
            "would-write": [],
            "would_write": [],
            "would-create": [],
            "would_create": [],
            "would-defer": [],
            "would_defer": [],
            "would-archive": [],
            "would_archive": [],
            "would-skip": [],
            "would_skip": [],
            "would-abort": would_abort,
            "would_abort": would_abort,
            "findings": stale_findings,
            "stale_locks": stale_findings,
            "gc": {
                "would-prune": [],
                "would-keep": [],
            },
        }
        abort_msg = (
            f"dry-run: would abort cutover on {home} to {target_version}: "
            f"would_abort={len(would_abort)} ({abort_items})"
        )
        return CutoverOutcome(
            exit_code=1,
            status="ABORTED_PRE_FLIP",
            target_version=target_version,
            target_git_sha=target_git_sha,
            aborted_pre_flip=True,
            rolled_back=False,
            deferred_running_hosts=[],
            updated_surfaces=[],
            skipped_surfaces=[],
            would_abort=would_abort,
            gc_result=empty_gc,
            message=abort_msg,
            plan=abort_plan_doc,
            findings=stale_findings,
            stale_locks=stale_findings,
        )

    for host, kind, cfg_path, surf_spec, _rewrite_on_run in all_surfaces:
        if kind == "plugin_container":
            exists = cfg_path.exists() or cfg_path.is_symlink()
            should_converge = exists or cfg_path.parent.exists()
            mode = effective_container_mode(host, wired_doc)
            after_entry = {
                "kind": "plugin_container",
                "container_mode": mode,
                "target_version": target_version,
                "subtrees": ["dist", "node_modules", "skills", "commands", "hooks"],
            }
            if not should_converge:
                _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta (host root absent)")
                continue
            has_c_delta = _container_has_delta(cfg_path, ref_vdir, base_semver)
            if not has_c_delta:
                _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta")
                continue
            c_ref_text = _extract_container_referenced_text(cfg_path)
            defer_reason = _check_defer(host, cfg_path, c_ref_text)
            before_entry = {
                "kind": "plugin_container",
                "exists": exists,
                "referenced_versions": sorted(_extract_referenced_versions(c_ref_text, plugin_dir)),
            }
            diff_obj = {"before": before_entry, "after": after_entry, "reasons": ["container_delta"]}
            if defer_reason:
                _record_surface(host, kind, cfg_path, "would-defer", diff=diff_obj, reason=defer_reason)
            elif not exists:
                _record_surface(host, kind, cfg_path, "would-create", diff=diff_obj, reason="create_container")
            else:
                _record_surface(
                    host,
                    kind,
                    cfg_path,
                    "would-write",
                    diff=diff_obj,
                    reason="container_delta",
                    archive_dest=str(archive_base / "containers" / host),
                )
            continue

        if kind == "claude_plugin_registry":
            claude_plugins_dir = home / ".claude" / "plugins"
            mkt_root = claude_plugins_dir / "marketplaces" / "minni-local"
            mkt_plugin_dir = mkt_root / "plugins" / "minni"
            exists = cfg_path.is_file()
            should_converge = exists or claude_plugins_dir.is_dir() or mkt_root.exists()
            expected_install = str(mkt_plugin_dir)
            expected_project = str(mkt_root)
            after_entry = {
                "scope": "user",
                "installPath": expected_install,
                "projectPath": expected_project,
                "version": base_semver,
                "gitCommitSha": target_git_sha,
            }
            if not should_converge:
                _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta (host root absent)")
                continue
            before_entry = None
            inst_has_delta = True
            raw_text = ""
            if exists:
                try:
                    raw_text = cfg_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    abort_reason = f"unreadable_{kind}:{type(exc).__name__}"
                    _record_surface(
                        host,
                        kind,
                        cfg_path,
                        "would_abort",
                        diff={"before": {"kind": kind}, "after": None, "reasons": [abort_reason]},
                        reason=abort_reason,
                    )
                    continue
                try:
                    doc = json.loads(raw_text) if raw_text.strip() else {}
                    plugins = doc.get("plugins", {}) if isinstance(doc, dict) else {}
                    entries = plugins.get("minni@minni-local") if isinstance(plugins, dict) else None
                    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
                        before_entry = dict(entries[0])
                        if (
                            before_entry.get("version") == base_semver
                            and before_entry.get("gitCommitSha") == target_git_sha
                            and before_entry.get("installPath") == expected_install
                            and ("projectPath" not in before_entry or before_entry.get("projectPath") == expected_project)
                            and "~" not in str(before_entry.get("installPath") or "")
                            and "~" not in str(before_entry.get("projectPath") or "")
                        ):
                            inst_has_delta = False
                except Exception:
                    inst_has_delta = True
            mkt_has_delta = _container_has_delta(mkt_plugin_dir, ref_vdir, base_semver, forbid_mcp_json=True)
            if not inst_has_delta and not mkt_has_delta:
                _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta")
                continue
            defer_reason = _check_defer(host, cfg_path, raw_text)
            reasons: list[str] = []
            if inst_has_delta:
                reasons.append("installed_plugins_delta")
            if mkt_has_delta:
                reasons.append("marketplace_container_delta")
            diff_obj = {
                "before": _redact_minni_entry(before_entry),
                "after": _redact_minni_entry(after_entry),
                "reasons": reasons,
            }
            if defer_reason:
                _record_surface(host, kind, cfg_path, "would-defer", diff=diff_obj, reason=defer_reason)
            elif not exists:
                _record_surface(host, kind, cfg_path, "would-create", diff=diff_obj, reason=",".join(reasons))
            else:
                _record_surface(
                    host,
                    kind,
                    cfg_path,
                    "would-write",
                    diff=diff_obj,
                    reason=",".join(reasons),
                    archive_dest=_preview_archive_dest(cfg_path),
                )
            continue

        if kind == "commands_dir":
            expected_target = expand_for_home(str(surf_spec.get("entry") or ""), home=home)
            after_entry = {"kind": "commands_dir", "symlink_target": expected_target}
            if cfg_path.is_symlink():
                try:
                    cur_target = os.readlink(cfg_path)
                except OSError:
                    cur_target = ""
                target_ok = (ref_vdir / "commands").is_dir() and cfg_path.exists()
                if cur_target == expected_target and target_ok:
                    _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta")
                    continue
                defer_reason = _check_defer(host, cfg_path, cur_target)
                reason_code = (
                    "commands_dir_target_mismatch"
                    if cur_target != expected_target
                    else "broken_commands_dir_symlink"
                )
                diff_obj = {
                    "before": {"kind": "commands_dir", "symlink_target": cur_target},
                    "after": after_entry,
                    "reasons": [reason_code],
                }
                if defer_reason:
                    _record_surface(host, kind, cfg_path, "would-defer", diff=diff_obj, reason=defer_reason)
                else:
                    _record_surface(host, kind, cfg_path, "would-write", diff=diff_obj, reason=reason_code)
                continue
            if not cfg_path.exists():
                if not _commands_dir_expected_on_home(home, cfg_path):
                    _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta (host root absent)")
                    continue
                defer_reason = _check_defer(host, cfg_path, "")
                diff_obj = {
                    "before": None,
                    "after": after_entry,
                    "reasons": ["missing_commands_dir"],
                }
                if defer_reason:
                    _record_surface(host, kind, cfg_path, "would-defer", diff=diff_obj, reason=defer_reason)
                else:
                    _record_surface(host, kind, cfg_path, "would-create", diff=diff_obj, reason="missing_commands_dir")
                continue
            diff_obj = {
                "before": {"kind": "non_symlink_commands_dir"},
                "after": after_entry,
                "reasons": ["broken_or_non_symlink_commands_dir"],
            }
            _record_surface(host, kind, cfg_path, "would_abort", diff=diff_obj, reason="broken_or_non_symlink_commands_dir")
            continue

        if kind in ("mcp_json", "mcp_toml"):
            hspec = HOST_SURFACE_CATALOG.get(host) or {}
            base_diff = evaluate_mcp_surface_diff(
                None,
                host_id=host,
                agent_id=str(hspec.get("agent_id") or host),
                vault_path=str(hspec.get("vault_path") or f"~/.minni/{host}-vault"),
                surface_spec=surf_spec,
                home=home,
            )
            after_entry = base_diff.desired_entry
            if not cfg_path.exists() and not cfg_path.is_symlink():
                if not _mcp_surface_expected_on_home(home, host, cfg_path, wired_doc):
                    _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta (host root absent)")
                    continue
                defer_reason = _check_defer(host, cfg_path, "")
                diff_obj = {
                    "before": None,
                    "after": _redact_minni_entry(after_entry),
                    "reasons": ["missing_mcp_surface"],
                }
                if defer_reason:
                    _record_surface(host, kind, cfg_path, "would-defer", diff=diff_obj, reason=defer_reason)
                else:
                    _record_surface(host, kind, cfg_path, "would-create", diff=diff_obj, reason="missing_mcp_surface")
                continue
            if not cfg_path.is_file():
                diff_obj = {
                    "before": {"error": "non_regular_or_broken_symlink"},
                    "after": _redact_minni_entry(after_entry),
                    "reasons": ["broken_or_non_regular_surface"],
                }
                _record_surface(host, kind, cfg_path, "would-write", diff=diff_obj, reason="broken_or_non_regular_surface")
                continue
            try:
                raw_text = cfg_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                abort_reason = f"unreadable_{kind}:{type(exc).__name__}"
                _record_surface(
                    host,
                    kind,
                    cfg_path,
                    "would_abort",
                    diff={"before": {"kind": kind}, "after": None, "reasons": [abort_reason]},
                    reason=abort_reason,
                )
                continue
            cur_entry: Any = None
            has_delta = True
            reasons = []
            try:
                if kind == "mcp_json":
                    doc = json.loads(raw_text) if raw_text.strip() else {}
                    container_key = "mcp" if surf_spec.get("key") == "mcp.minni" else "mcpServers"
                    mcp_map = doc.get(container_key) if isinstance(doc, dict) else None
                    cur_entry = copy.deepcopy(mcp_map.get("minni")) if isinstance(mcp_map, dict) else None
                else:
                    doc = tomllib.loads(raw_text) if raw_text.strip() else {}
                    mcp_map = doc.get("mcp_servers") if isinstance(doc, dict) else None
                    cur_entry = copy.deepcopy(mcp_map.get("minni")) if isinstance(mcp_map, dict) else None
                mcp_diff = evaluate_mcp_surface_diff(
                    cur_entry if isinstance(cur_entry, dict) else None,
                    host_id=host,
                    agent_id=str(hspec.get("agent_id") or host),
                    vault_path=str(hspec.get("vault_path") or f"~/.minni/{host}-vault"),
                    surface_spec=surf_spec,
                    home=home,
                )
                has_delta = mcp_diff.has_delta
                reasons = list(mcp_diff.reasons)
                after_entry = mcp_diff.desired_entry
                if kind == "mcp_toml" and not has_delta and isinstance(cur_entry, dict) and cur_entry.get("enabled") is not True:
                    has_delta = True
                    reasons = ["missing_enabled_true"]
            except Exception as exc:
                has_delta = True
                reasons = [f"parse_error:{type(exc).__name__}"]
            if not has_delta:
                _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta")
                continue
            defer_reason = _check_defer(host, cfg_path, raw_text)
            diff_obj = {
                "before": _redact_minni_entry(cur_entry),
                "after": _redact_minni_entry(after_entry),
                "reasons": reasons,
            }
            if defer_reason:
                _record_surface(host, kind, cfg_path, "would-defer", diff=diff_obj, reason=defer_reason)
            else:
                _record_surface(
                    host,
                    kind,
                    cfg_path,
                    "would-write",
                    diff=diff_obj,
                    reason=",".join(reasons),
                    archive_dest=_preview_archive_dest(cfg_path),
                )
            continue

        # Text / hook / launcher / bridge / skill / rules surfaces
        if not cfg_path.exists() and not cfg_path.is_symlink():
            _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta (surface absent)")
            continue
        if not cfg_path.is_file():
            _record_surface(host, kind, cfg_path, "would-write", reason="broken_or_non_regular_surface")
            continue
        try:
            raw_text = cfg_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            abort_reason = f"unreadable_{kind}:{type(exc).__name__}"
            _record_surface(
                host,
                kind,
                cfg_path,
                "would_abort",
                diff={"before": {"kind": kind}, "after": None, "reasons": [abort_reason]},
                reason=abort_reason,
            )
            continue
        if kind in ("launcher_wrapper", "hook_wrapper"):
            chk = subprocess.run(["bash", "-n", str(cfg_path)], capture_output=True, text=True, check=False)
            if chk.returncode != 0:
                _record_surface(
                    host,
                    kind,
                    cfg_path,
                    "would_abort",
                    diff={"before": {"kind": kind}, "after": None, "reasons": ["unparseable_shell_wrapper"]},
                    reason="unparseable_shell_wrapper",
                )
                continue
        from minni.wire.health import evaluate_text_surface_health

        pre_health_reasons = evaluate_text_surface_health(raw_text, kind=kind, surf_spec=surf_spec)
        reconcile_failed = False
        _updated_text = raw_text
        try:
            has_delta, _updated_text = _reconcile_text_surface(
                raw_text,
                kind="hooks_json" if kind == "claude_marketplace" else kind,
                surf_spec=surf_spec,
                home=home,
            )
        except Exception as exc:
            reconcile_failed = True
            has_delta = True
            abort_reasons = pre_health_reasons or [f"unparseable_{kind}:{type(exc).__name__}"]
            _record_surface(
                host,
                kind,
                cfg_path,
                "would_abort",
                diff={"before": {"kind": kind}, "after": None, "reasons": abort_reasons},
                reason=",".join(abort_reasons),
            )
            continue

        # Evaluate the same post-flip health predicates against the projected state (H23 Seam Parity)
        post_health_reasons = evaluate_text_surface_health(_updated_text, kind=kind, surf_spec=surf_spec)
        defer_reason = _check_defer(host, cfg_path, raw_text)
        if post_health_reasons and not defer_reason and not reconcile_failed:
            _record_surface(
                host,
                kind,
                cfg_path,
                "would_abort",
                diff={
                    "before": {"kind": kind},
                    "after": {"kind": kind},
                    "reasons": post_health_reasons,
                },
                reason=",".join(post_health_reasons),
            )
            continue

        if not has_delta:
            _record_surface(host, kind, cfg_path, "would-skip", reason="no_delta")
            continue
        canonical_entry = expand_for_home(str(surf_spec.get("entry") or ""), home=home)
        before_refs = sorted({m.group(0) for m in _VERSIONED_PLUGIN_RE.finditer(raw_text)})
        surface_reasons = list(pre_health_reasons) if pre_health_reasons else [f"versioned_or_tilde_in_{kind}"]
        diff_obj = {
            "before": {
                "kind": kind,
                "versioned_refs": before_refs,
                "has_tilde": ("~/" in raw_text),
            },
            "after": {
                "kind": kind,
                "entry": canonical_entry,
                "has_tilde": False,
            },
            "reasons": surface_reasons,
        }
        reason_str = ",".join(surface_reasons)
        if defer_reason:
            _record_surface(host, kind, cfg_path, "would-defer", diff=diff_obj, reason=defer_reason)
        else:
            _record_surface(
                host,
                kind,
                cfg_path,
                "would-write",
                diff=diff_obj,
                reason=reason_str,
                archive_dest=_preview_archive_dest(cfg_path),
            )

    if would_abort:
        abort_items = ", ".join(f"{a.get('host')}:{a.get('path')}" for a in would_abort)
        empty_gc_post: dict[str, Any] = {
            "would-prune": [],
            "would_prune": [],
            "would-keep": [],
            "would_keep": [],
            "retained_in_use": [],
            "pruned": [],
        }
        abort_plan_doc_post: dict[str, Any] = {
            "target_version": target_version,
            "target_git_sha": target_git_sha,
            "hosts": hosts_plan,
            "surfaces": all_surface_plans,
            "would-write": [],
            "would_write": [],
            "would-create": [],
            "would_create": [],
            "would-defer": [],
            "would_defer": [],
            "would-archive": [],
            "would_archive": [],
            "would-skip": [],
            "would_skip": [],
            "would-abort": would_abort,
            "would_abort": would_abort,
            "findings": stale_findings,
            "stale_locks": stale_findings,
            "gc": {
                "would-prune": [],
                "would-keep": [],
            },
        }
        abort_msg_post = (
            f"dry-run: would abort cutover on {home} to {target_version}: "
            f"would_abort={len(would_abort)} ({abort_items})"
        )
        return CutoverOutcome(
            exit_code=1,
            status="ABORTED_PRE_FLIP",
            target_version=target_version,
            target_git_sha=target_git_sha,
            aborted_pre_flip=True,
            rolled_back=False,
            deferred_running_hosts=[],
            updated_surfaces=[],
            skipped_surfaces=[],
            would_abort=would_abort,
            gc_result=empty_gc_post,
            message=abort_msg_post,
            plan=abort_plan_doc_post,
            findings=stale_findings,
            stale_locks=stale_findings,
        )

    # Check if `current` is a real directory that Step 4/7b would archive
    current_path = plugin_dir / "current"
    if current_path.exists() and not current_path.is_symlink() and current_path.is_dir():
        would_archive.append({
            "host": "minni-plugin",
            "kind": "current_real_dir",
            "path": str(current_path),
            "archive_dest": str(archive_base / "previous-current-real-dir"),
            "reason": "replace_real_dir_with_current_symlink",
        })

    # Compute GC would-prune and would-keep without deleting anything
    prev_link_target: str | None = None
    if current_path.is_symlink():
        try:
            prev_link_target = Path(os.readlink(current_path)).name
        except OSError:
            prev_link_target = None
    gc_out = collect_garbage(
        home=home,
        keep_last=2,
        prune=False,
        dry_run=True,
        prev_target=prev_link_target,
        extra_protected=deferred_protected_versions | {target_version},
    )
    would_keep_paths = list(gc_out.retained_in_use)
    target_vdir_str = str(plugin_dir / target_version)
    if target_version and target_vdir_str not in would_keep_paths and target_vdir_str not in gc_out.would_prune:
        would_keep_paths.append(target_vdir_str)

    gc_dict: dict[str, Any] = {
        "would-prune": list(gc_out.would_prune),
        "would_prune": list(gc_out.would_prune),
        "would-keep": would_keep_paths,
        "would_keep": list(would_keep_paths),
        "retained_in_use": list(gc_out.retained_in_use),
        "pruned": [],
    }

    for sf in stale_findings:
        sf_host = str(sf.get("host") or "")
        if sf_host in hosts_plan:
            hosts_plan[sf_host].setdefault("stale_locks", []).append(sf)

    plan_doc: dict[str, Any] = {
        "target_version": target_version,
        "target_git_sha": target_git_sha,
        "hosts": hosts_plan,
        "surfaces": all_surface_plans,
        "would-write": would_write,
        "would_write": would_write,
        "would-create": would_create,
        "would_create": would_create,
        "would-defer": would_defer,
        "would_defer": would_defer,
        "would-archive": would_archive,
        "would_archive": would_archive,
        "would-skip": would_skip,
        "would_skip": would_skip,
        "would-abort": would_abort,
        "would_abort": would_abort,
        "findings": stale_findings,
        "stale_locks": stale_findings,
        "gc": {
            "would-prune": list(gc_out.would_prune),
            "would-keep": would_keep_paths,
        },
    }

    abort_suffix = ""
    if would_abort:
        abort_items = ", ".join(f"{a.get('host')}:{a.get('path')}" for a in would_abort)
        abort_suffix = f", would_abort={len(would_abort)} ({abort_items})"

    msg = (
        f"dry-run: would cut over {home} to {target_version}: "
        f"would-write={len(would_write)}, would-create={len(would_create)}, "
        f"would-defer={len(would_defer)}, would-archive={len(would_archive)}, "
        f"would-skip={len(would_skip)}, would-prune={len(gc_out.would_prune)}, "
        f"would-keep={len(would_keep_paths)}{abort_suffix}"
    )
    return CutoverOutcome(
        exit_code=0,
        status="DRY_RUN",
        target_version=target_version,
        target_git_sha=target_git_sha,
        deferred_running_hosts=deferred_running_hosts,
        updated_surfaces=updated_surfaces,
        skipped_surfaces=skipped_surfaces,
        would_abort=would_abort,
        gc_result=gc_dict,
        message=msg,
        plan=plan_doc,
        findings=stale_findings,
        stale_locks=stale_findings,
    )


def execute_atomic_cutover(
    *,
    home: Path,
    target_version: str,
    target_git_sha: str,
    dry_run: bool = False,
    running_hosts: set[str] | None = None,
    post_flip_verifier: Callable[[Path], bool] | None = None,
    repo_root: Path | None = None,
    payload_ref_dir: Path | None = None,
) -> CutoverOutcome:
    """Execute the 7-step atomic cutover with pre-flip probes and CAS rollback (§4.3)."""
    plugin_dir = home / ".minni" / "plugin"
    vdir = plugin_dir / target_version
    pid = os.getpid()
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base_semver = target_version.split("+", 1)[0] if target_version else "0.6.0"

    if dry_run:
        return _build_dry_run_plan(
            home=home,
            target_version=target_version,
            target_git_sha=target_git_sha,
            running_hosts=running_hosts,
            repo_root=repo_root,
            ts=ts,
            base_semver=base_semver,
            payload_ref_dir=payload_ref_dir,
        )

    # H16 pre-flip abort check before any write or probe: if any commands_dir is a non-symlink, abort immediately.
    early_would_abort: list[dict[str, Any]] = []
    for _host_id, _hspec in HOST_SURFACE_CATALOG.items():
        for _surf in _hspec.get("surfaces", []):
            if not isinstance(_surf, dict) or str(_surf.get("kind") or "") != "commands_dir":
                continue
            _p_raw = str(_surf.get("path") or "").replace("{base_semver}", base_semver)
            _cfg_path = Path(expand_for_home(_p_raw, home=home))
            if _cfg_path.exists() and not _cfg_path.is_symlink():
                early_would_abort.append(
                    {
                        "host": _host_id,
                        "kind": "commands_dir",
                        "path": str(_cfg_path),
                        "action": "would_abort",
                        "reason": "broken_or_non_symlink_commands_dir",
                    }
                )
    if early_would_abort:
        _first_abort = early_would_abort[0]
        return CutoverOutcome(
            exit_code=1,
            status="ABORTED_PRE_FLIP",
            target_version=target_version,
            target_git_sha=target_git_sha,
            aborted_pre_flip=True,
            rolled_back=False,
            would_abort=early_would_abort,
            message=(
                f"broken or non-symlink commands_dir surface for "
                f"{_first_abort['host']} ({_first_abort['path']})"
            ),
        )

    if not vdir.is_dir():
        return CutoverOutcome(
            exit_code=1,
            status="ABORTED_PRE_FLIP",
            target_version=target_version,
            target_git_sha=target_git_sha,
            aborted_pre_flip=True,
            message=f"target payload directory does not exist: {vdir}",
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

    # Flatten all catalog surfaces (containers & plugin registries first, then config/hook/wrapper surfaces) + legacy ~/.muse/config.json
    container_and_registry_surfaces: list[tuple[str, str, Path, dict[str, Any], bool]] = []
    config_and_file_surfaces: list[tuple[str, str, Path, dict[str, Any], bool]] = []
    for host_id, hspec in HOST_SURFACE_CATALOG.items():
        rewrite_hazard = str(hspec.get("rewrite_on_run_hazard") or "none") != "none"
        for surf in hspec.get("surfaces", []):
            if not isinstance(surf, dict):
                continue
            kind = str(surf.get("kind") or "")
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
                    config_and_file_surfaces.append((host_id, "mcp_json", vp, view_spec, rewrite_hazard))
                continue
            p_raw = str(surf.get("path") or "").replace("{base_semver}", base_semver)
            p = Path(expand_for_home(p_raw, home=home))
            if kind in ("plugin_container", "claude_plugin_registry"):
                container_and_registry_surfaces.append((host_id, kind, p, surf, rewrite_hazard))
            else:
                config_and_file_surfaces.append((host_id, kind, p, surf, rewrite_hazard))

    all_surfaces: list[tuple[str, str, Path, dict[str, Any], bool]] = (
        container_and_registry_surfaces + config_and_file_surfaces
    )

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

    # H15 & H16: BEFORE Step 4 (`flip_current_pointer`) and BEFORE any container/marketplace/config convergence,
    # abort pre-flip if any commands_dir surface is a non-symlink (real directory/file) or has a missing payload commands/ directory.
    would_abort: list[dict[str, Any]] = []
    current_prefix = str(plugin_dir / "current")
    for host, kind, cfg_path, surf_spec, _rewrite_on_run in all_surfaces:
        if kind != "commands_dir":
            continue
        if cfg_path.exists() and not cfg_path.is_symlink():
            would_abort.append(
                {
                    "host": host,
                    "kind": kind,
                    "path": str(cfg_path),
                    "action": "would_abort",
                    "reason": "broken_or_non_symlink_commands_dir",
                }
            )
            continue
        if cfg_path.is_symlink() or _commands_dir_expected_on_home(home, cfg_path):
            expected_target = expand_for_home(str(surf_spec.get("entry") or ""), home=home)
            if expected_target == current_prefix or expected_target.startswith(current_prefix + "/"):
                rel_sub = expected_target[len(current_prefix):].lstrip("/")
                post_flip_target_dir = (vdir / rel_sub) if rel_sub else vdir
            else:
                post_flip_target_dir = Path(expected_target)
            if not (vdir / "commands").is_dir() or not post_flip_target_dir.is_dir():
                return CutoverOutcome(
                    exit_code=1,
                    status="ABORTED_PRE_FLIP",
                    target_version=target_version,
                    target_git_sha=target_git_sha,
                    aborted_pre_flip=True,
                    rolled_back=False,
                    message=(
                        f"broken commands_dir symlink for {host} ({cfg_path}): "
                        f"payload commands/ directory missing at {expected_target}"
                    ),
                )
    if would_abort:
        first_abort = would_abort[0]
        return CutoverOutcome(
            exit_code=1,
            status="ABORTED_PRE_FLIP",
            target_version=target_version,
            target_git_sha=target_git_sha,
            aborted_pre_flip=True,
            rolled_back=False,
            would_abort=would_abort,
            message=(
                f"broken or non-symlink commands_dir surface for "
                f"{first_abort['host']} ({first_abort['path']})"
            ),
        )

    wired_doc = _load_wired(plugin_dir / "wired.json")
    active_running = (
        set(running_hosts)
        if running_hosts is not None
        else _detect_running_rewrite_hosts()
    )
    rollback_records: list[_ConfigRollbackRecord] = []
    commands_symlink_rollbacks: list[tuple[Path, str | None, int, int, str]] = []
    updated_surfaces: list[str] = []
    skipped_surfaces: list[str] = []
    deferred_running_hosts: list[str] = []
    deferred_details: list[str] = []
    deferred_surface_paths: set[str] = set()
    deferred_protected_versions: set[str] = set()
    stale_findings: list[dict[str, Any]] = []

    def _should_defer_host_write(host: str, cfg_path: Path, raw_text: str) -> bool:
        lock_hit = _check_host_lockfile(home, host, cfg_path, retries=3, stale_findings=stale_findings)
        is_running_rewrite_host = (
            host in ("muse", "claude-desktop", "grok-build", "devin", "cursor")
            and host in active_running
        )
        if lock_hit or is_running_rewrite_host:
            if host not in deferred_running_hosts:
                deferred_running_hosts.append(host)
            deferred_surface_paths.add(str(cfg_path))
            detail = f"{host} ({cfg_path})" + (f" [lock: {lock_hit}]" if lock_hit else "")
            if detail not in deferred_details:
                deferred_details.append(detail)
            deferred_protected_versions.update(_extract_referenced_versions(raw_text, plugin_dir))
            return True
        return False

    # Step 4 — Flip `current` Pointer (P2-6, R4-1, P1-1, P1-2)
    flip_state: CurrentFlipState = flip_current_pointer(target_version, home=home, pid=pid)
    all_container_swaps: list[SubtreeSwapRecord] = []
    prev_installed_plugins_list: list[tuple[Path, bytes | None, int, int, str]] = []
    step5_error: str = ""
    cas_conflicts: list[str] = []

    # Step 5 — Reconcile Host Surfaces Across All Catalog Surface Kinds (`delta != 0` only, Lock/Running Guard & CAS)
    for host, kind, cfg_path, surf_spec, _rewrite_on_run in all_surfaces:
        if kind != "commands_dir":
            continue
        if cfg_path.is_symlink() or _commands_dir_expected_on_home(home, cfg_path):
            expected_target = expand_for_home(str(surf_spec.get("entry") or ""), home=home)
            if not (vdir / "commands").is_dir() or not Path(expected_target).is_dir():
                step5_error = (
                    f"broken commands_dir symlink for {host} ({cfg_path}): "
                    f"payload commands/ directory missing at {expected_target}"
                )
                break

    for host, kind, cfg_path, surf_spec, rewrite_on_run in all_surfaces:
        if step5_error:
            break

        if kind == "plugin_container":
            should_converge = cfg_path.exists() or cfg_path.is_symlink() or cfg_path.parent.exists()
            if not should_converge:
                continue
            if cfg_path.is_symlink() and not cfg_path.exists():
                step5_error = f"broken container symlink surface for {host} ({cfg_path})"
                break
            has_c_delta = _container_has_delta(cfg_path, vdir, base_semver)
            if not has_c_delta:
                skipped_surfaces.append(f"{host}:{cfg_path}")
                continue
            c_ref_text = _extract_container_referenced_text(cfg_path)
            if _should_defer_host_write(host, cfg_path, c_ref_text):
                continue
            try:
                mode = effective_container_mode(host, wired_doc)
                if host in ("antigravity", "cursor"):
                    swaps = converge_host_container(
                        host,
                        home=home,
                        container_mode=mode,
                        target_version_root=vdir,
                        base_semver=base_semver,
                        pid=pid,
                    )
                else:
                    cfg_path.mkdir(parents=True, exist_ok=True)
                    swaps = swap_container_subtrees(
                        cfg_path,
                        stable_current_root=plugin_dir / "current",
                        target_version_root=vdir,
                        container_mode=mode,
                        pid=pid,
                    )
                all_container_swaps.extend(swaps)
                updated_surfaces.append(f"{host}:{cfg_path}")
            except _CasConflictError as exc:
                if exc.conflict_artifact:
                    cas_conflicts.append(exc.conflict_artifact)
                step5_error = str(exc)
                break
            except Exception as exc:
                step5_error = f"container convergence failed for {host} ({cfg_path}): {exc}"
                break
            continue

        if kind == "claude_plugin_registry":
            if (cfg_path.exists() or cfg_path.is_symlink()) and not cfg_path.is_file():
                step5_error = f"broken symlink or non-regular surface for {host} ({cfg_path})"
                break
            try:
                mkt_swaps, inst_rb, claude_upd, claude_skp = _converge_claude_local_marketplace(
                    home,
                    vdir,
                    target_version,
                    target_git_sha,
                    pid=pid,
                    ts=ts,
                    inst_path=cfg_path,
                    host=host,
                    should_defer_fn=_should_defer_host_write,
                )
                all_container_swaps.extend(mkt_swaps)
                if inst_rb is not None:
                    prev_installed_plugins_list.append(inst_rb)
                updated_surfaces.extend(claude_upd)
                skipped_surfaces.extend(claude_skp)
            except _CasConflictError as exc:
                if exc.conflict_artifact:
                    cas_conflicts.append(exc.conflict_artifact)
                step5_error = str(exc)
                break
            except Exception as exc:
                step5_error = f"claude_plugin_registry convergence failed for {host} ({cfg_path}): {exc}"
                break
            continue

        if kind == "commands_dir":
            if cfg_path.exists() and not cfg_path.is_symlink():
                would_abort.append(
                    {
                        "host": host,
                        "kind": kind,
                        "path": str(cfg_path),
                        "action": "would_abort",
                        "reason": "broken_or_non_symlink_commands_dir",
                    }
                )
                step5_error = f"broken or non-symlink commands_dir surface for {host} ({cfg_path})"
                break
            expected_target = expand_for_home(str(surf_spec.get("entry") or ""), home=home)
            if not cfg_path.exists() and not cfg_path.is_symlink():
                if not _commands_dir_expected_on_home(home, cfg_path):
                    continue
                if _should_defer_host_write(host, cfg_path, ""):
                    continue
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_link = cfg_path.parent / f".{cfg_path.name}.tmp.{pid}"
                try:
                    if tmp_link.exists() or tmp_link.is_symlink():
                        tmp_link.unlink(missing_ok=True)
                    os.symlink(expected_target, tmp_link)
                    lock_hit = _check_host_lockfile(home, host, cfg_path, retries=1)
                    if lock_hit:
                        raise _CasConflictError(host, cfg_path, f"lockfile present before symlink create: {lock_hit}")
                    if cfg_path.exists() or cfg_path.is_symlink():
                        raise _CasConflictError(
                            host, cfg_path, "concurrent creation of commands_dir symlink before replace"
                        )
                    os.replace(tmp_link, cfg_path)
                    post_lstat = cfg_path.lstat()
                    post_target = os.readlink(cfg_path)
                except _CasConflictError as exc:
                    if exc.conflict_artifact:
                        cas_conflicts.append(exc.conflict_artifact)
                    step5_error = str(exc)
                    break
                finally:
                    if tmp_link.exists() or tmp_link.is_symlink():
                        tmp_link.unlink(missing_ok=True)
                commands_symlink_rollbacks.append(
                    (cfg_path, None, post_lstat.st_ino, post_lstat.st_mtime_ns, post_target)
                )
                updated_surfaces.append(f"{host}:{cfg_path}")
                continue
            if cfg_path.is_symlink():
                try:
                    pre_lstat = cfg_path.lstat()
                    cur_target = os.readlink(cfg_path)
                except OSError as exc:
                    step5_error = f"failed to read symlink for {host} ({cfg_path}): {exc}"
                    break
                if cur_target == expected_target:
                    if not (vdir / "commands").is_dir() or not cfg_path.exists() or not cfg_path.is_dir():
                        step5_error = (
                            f"broken commands_dir symlink for {host} ({cfg_path}): "
                            f"payload commands/ directory missing at {expected_target}"
                        )
                        break
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
                    post_lstat = cfg_path.lstat()
                    post_target = os.readlink(cfg_path)
                except _CasConflictError as exc:
                    if exc.conflict_artifact:
                        cas_conflicts.append(exc.conflict_artifact)
                    step5_error = str(exc)
                    break
                finally:
                    if tmp_link.exists() or tmp_link.is_symlink():
                        tmp_link.unlink(missing_ok=True)
                commands_symlink_rollbacks.append(
                    (cfg_path, cur_target, post_lstat.st_ino, post_lstat.st_mtime_ns, post_target)
                )
                updated_surfaces.append(f"{host}:{cfg_path}")
            continue

        if (cfg_path.exists() or cfg_path.is_symlink()) and not cfg_path.is_file():
            step5_error = f"broken symlink or non-regular surface for {host} ({cfg_path})"
            break
        if not cfg_path.is_file():
            if kind in ("mcp_json", "mcp_toml") and _mcp_surface_expected_on_home(home, host, cfg_path, wired_doc):
                hspec = HOST_SURFACE_CATALOG[host]
                diff = evaluate_mcp_surface_diff(
                    None,
                    host_id=host,
                    agent_id=str(hspec["agent_id"]),
                    vault_path=str(hspec["vault_path"]),
                    surface_spec=surf_spec,
                    home=home,
                )
                if not diff.has_delta:
                    skipped_surfaces.append(f"{host}:{cfg_path}")
                    continue
                if _should_defer_host_write(host, cfg_path, ""):
                    continue
                if kind == "mcp_json":
                    container_key = "mcp" if surf_spec.get("key") == "mcp.minni" else "mcpServers"
                    new_doc = {container_key: {"minni": diff.desired_entry}}
                    new_bytes = (json.dumps(new_doc, indent=2) + "\n").encode("utf-8")
                else:
                    new_text = _render_toml_with_updated_minni("", diff.desired_entry)
                    new_bytes = new_text.encode("utf-8")
                try:
                    post_ino, post_mtime_ns, post_sha256 = _cas_write_or_create_surface(
                        cfg_path,
                        None,
                        new_bytes,
                        home=home,
                        host=host,
                        ts=ts,
                        mode=0o600,
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
                        pre_bytes=None,
                        pre_mtime_ns=0,
                        pre_sha256="",
                        previous_minni_entry=None,
                        post_step5_mtime_ns=post_mtime_ns,
                        post_step5_sha256=post_sha256,
                        pre_ino=0,
                        post_step5_ino=post_ino,
                        surface_kind=kind,
                        pre_mode=0o600,
                    )
                )
                updated_surfaces.append(f"{host}:{cfg_path}")
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
        # Verify zero residual delta across all non-deferred catalog surfaces by path (P1-1, New P3 #1)
        from minni.wire.health import audit_host_propagation_health

        post_health = audit_host_propagation_health(home=home, expected_git_sha=target_git_sha)
        non_deferred_drift = [
            d for d in post_health.drifted_surfaces
            if str(d.get("path") or "") not in deferred_surface_paths
        ]
        if non_deferred_drift:
            first = non_deferred_drift[0]
            step6_ok = False
            step6_reason = (
                f"post-flip surface delta remains for {first.get('host')} "
                f"({first.get('path')}): {first.get('reasons')}"
            )

    if not step6_ok:
        # 1. Restore container subtrees and commands_dir symlinks with CAS tuple check (New P2 #1)
        rollback_container_subtrees(all_container_swaps)
        for cmd_link, prev_target, post_ino, post_mtime_ns, post_target in reversed(commands_symlink_rollbacks):
            try:
                cur_lstat = cmd_link.lstat()
                cur_target = os.readlink(cmd_link)
            except OSError:
                cas_conflicts.append(str(cmd_link))
                continue
            if (cur_lstat.st_ino, cur_lstat.st_mtime_ns, cur_target) != (
                post_ino,
                post_mtime_ns,
                post_target,
            ):
                cas_conflicts.append(str(cmd_link))
                continue
            if prev_target is None:
                cmd_link.unlink(missing_ok=True)
            else:
                tmp_l = cmd_link.parent / f".{cmd_link.name}.rb.{pid}"
                if tmp_l.exists() or tmp_l.is_symlink():
                    tmp_l.unlink(missing_ok=True)
                os.symlink(prev_target, tmp_l)
                os.replace(tmp_l, cmd_link)
        for inst_path, prev_raw, post_ino, post_mtime_ns, post_sha256 in reversed(prev_installed_plugins_list):
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

        # 2. Classify modified host config files by CAS check BEFORE restoring config bytes (H18)
        conflicted_entries: list[tuple[_ConfigRollbackRecord, bytes | None]] = []
        restorable_records: list[_ConfigRollbackRecord] = []
        for rec in reversed(rollback_records):
            try:
                cur_ino, cur_mtime_ns, cur_sha256, cur_bytes = _file_cas_tuple(rec.path)
            except OSError:
                cas_conflicts.append(f"{rec.host}:{rec.path}")
                conflicted_entries.append((rec, None))
                continue
            if (cur_ino, cur_mtime_ns, cur_sha256) == (
                rec.post_step5_ino,
                rec.post_step5_mtime_ns,
                rec.post_step5_sha256,
            ):
                restorable_records.append(rec)
            else:
                # Concurrent mutation happened between Step 5 and Step 7a rollback:
                # Leave live bytes untouched on disk, archive conflict under rollback-conflict, and record host+path in cas_conflicts!
                conflict_dir = _archive_dir(home, ts) / "rollback-conflict"
                conflict_dir.mkdir(parents=True, exist_ok=True)
                cpath = conflict_dir / rec.path.name
                cpath.write_bytes(cur_bytes)
                cas_conflicts.append(f"{rec.host}:{rec.path} ({cpath})")
                conflicted_entries.append((rec, cur_bytes))

        # 3. Decide and restore `current` pointer BEFORE restoring host config bytes, using the same
        # live-path check for symlink `current`, real-directory `current`, and absent `current` (R4-1 & H18).
        current_abs_prefix = str(plugin_dir / "current")

        def _extract_current_rel_paths(txt: str) -> tuple[bool, set[str]]:
            refs = False
            rels: set[str] = set()
            if current_abs_prefix in txt or "~/.minni/plugin/current" in txt:
                refs = True
                for m in re.finditer(re.escape(current_abs_prefix) + r"/([^\s\"'\],]+)", txt):
                    rels.add(m.group(1))
                for m in re.finditer(r"~/.minni/plugin/current/([^\s\"'\],]+)", txt):
                    rels.add(m.group(1))
            return refs, rels

        def _dir_satisfies_refs(cand_dir: Path | None, req_rel_paths: set[str]) -> bool:
            if cand_dir is None or not cand_dir.is_dir():
                return False
            for rel_p in req_rel_paths:
                if not (cand_dir / rel_p).exists():
                    return False
            return (cand_dir / "dist" / "server.js").is_file()

        unrestorable_refs_current = False
        unrestorable_rel_paths: set[str] = set()
        for _crec, cbytes in conflicted_entries:
            if cbytes is not None:
                c_refs, c_rels = _extract_current_rel_paths(cbytes.decode("utf-8", errors="ignore"))
                if c_refs:
                    unrestorable_refs_current = True
                    unrestorable_rel_paths.update(c_rels)

        rollback_paths_set = {r.path for r in rollback_records}
        for _h, _k, spath, _spec, _rw in all_surfaces:
            if _k != "plugin_container" and spath not in rollback_paths_set and spath.is_file():
                try:
                    utxt = spath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                u_refs, u_rels = _extract_current_rel_paths(utxt)
                if u_refs:
                    unrestorable_refs_current = True
                    unrestorable_rel_paths.update(u_rels)

        restorable_refs_current = False
        restorable_rel_paths: set[str] = set()
        for rrec in restorable_records:
            if rrec.pre_bytes is not None:
                r_refs, r_rels = _extract_current_rel_paths(rrec.pre_bytes.decode("utf-8", errors="ignore"))
                if r_refs:
                    restorable_refs_current = True
                    restorable_rel_paths.update(r_rels)

        if unrestorable_refs_current and not unrestorable_rel_paths:
            unrestorable_rel_paths.add("dist/server.js")
        if restorable_refs_current and not restorable_rel_paths:
            restorable_rel_paths.add("dist/server.js")

        live_refs_current = unrestorable_refs_current or restorable_refs_current
        live_current_rel_paths = set(unrestorable_rel_paths) | set(restorable_rel_paths)

        prev_current_dir: Path | None = None
        if flip_state.prev_dir_path is not None:
            prev_current_dir = flip_state.prev_dir_path
        elif flip_state.prev_link_target is not None:
            prev_current_dir = (
                Path(flip_state.prev_link_target)
                if Path(flip_state.prev_link_target).is_absolute()
                else (plugin_dir / flip_state.prev_link_target)
            )

        chosen_ver: str | None = None
        if not live_refs_current or _dir_satisfies_refs(prev_current_dir, live_current_rel_paths):
            rollback_current_pointer(flip_state)
            current_matches_prev = True
            effective_prev_dir_on_disk = prev_current_dir
        else:
            current_matches_prev = False
            if flip_state.prev_dir_path is not None and flip_state.prev_dir_path.exists():
                arch_prev_current = _archive_dir(home, ts) / "current_prev_dir"
                if arch_prev_current.exists():
                    shutil.rmtree(arch_prev_current, ignore_errors=True)
                shutil.move(str(flip_state.prev_dir_path), str(arch_prev_current))
                effective_prev_dir_on_disk = arch_prev_current
            else:
                effective_prev_dir_on_disk = prev_current_dir

            required_for_current = (
                unrestorable_rel_paths if unrestorable_refs_current else live_current_rel_paths
            )
            for crec, _cbytes in conflicted_entries:
                if crec.pre_bytes:
                    pre_txt = crec.pre_bytes.decode("utf-8", errors="ignore")
                    for v_cand in sorted(_extract_referenced_versions(pre_txt, plugin_dir)):
                        cand_d = Path(v_cand) if Path(v_cand).is_absolute() else (plugin_dir / v_cand)
                        if _dir_satisfies_refs(cand_d, required_for_current):
                            chosen_ver = v_cand
                            break
                if chosen_ver:
                    break
            if not chosen_ver and flip_state.prev_link_target:
                cand_d = (
                    Path(flip_state.prev_link_target)
                    if Path(flip_state.prev_link_target).is_absolute()
                    else (plugin_dir / flip_state.prev_link_target)
                )
                if _dir_satisfies_refs(cand_d, required_for_current):
                    chosen_ver = flip_state.prev_link_target
            if not chosen_ver:
                active_v = str(wired_doc.get("active_version") or "")
                if active_v and _dir_satisfies_refs(plugin_dir / active_v, required_for_current):
                    chosen_ver = active_v
            if not chosen_ver:
                chosen_ver = target_version
            tmp_rb_link = plugin_dir / f".current.rb.{pid}"
            if tmp_rb_link.exists() or tmp_rb_link.is_symlink():
                tmp_rb_link.unlink(missing_ok=True)
            os.symlink(chosen_ver, tmp_rb_link)
            if (
                flip_state.current_path.exists()
                and not flip_state.current_path.is_symlink()
                and flip_state.current_path.is_dir()
            ):
                shutil.rmtree(flip_state.current_path, ignore_errors=True)
            os.replace(tmp_rb_link, flip_state.current_path)

        # 4. Restore restorable host config bytes after `current` pointer is decided.
        # If `current` could not roll back to its previous target, ensure any restored config that
        # points at `current/...` runs the version it was written for whenever still on disk;
        # otherwise keep `current/...` (which resolves to `chosen_ver`) and report host+path.
        for rec in restorable_records:
            if rec.pre_bytes is None:
                rec.path.unlink(missing_ok=True)
                continue
            bytes_to_restore = rec.pre_bytes
            if not current_matches_prev:
                pre_txt = rec.pre_bytes.decode("utf-8", errors="ignore")
                rec_refs_cur, rec_rels = _extract_current_rel_paths(pre_txt)
                if rec_refs_cur:
                    if not rec_rels:
                        rec_rels = {"dist/server.js"}
                    intended_dir: Path | None = effective_prev_dir_on_disk
                    if intended_dir is None or not intended_dir.is_dir():
                        active_v = str(wired_doc.get("active_version") or "")
                        if active_v and (plugin_dir / active_v).is_dir():
                            intended_dir = plugin_dir / active_v
                    if _dir_satisfies_refs(intended_dir, rec_rels) and intended_dir is not None:
                        rewritten_txt = pre_txt.replace(current_abs_prefix, str(intended_dir)).replace(
                            "~/.minni/plugin/current", str(intended_dir)
                        )
                        bytes_to_restore = rewritten_txt.encode("utf-8")
                    else:
                        entry_tag = f"{rec.host}:{rec.path} (current_repointed:{chosen_ver})"
                        if not any(c.startswith(f"{rec.host}:{rec.path}") for c in cas_conflicts):
                            cas_conflicts.append(entry_tag)
            _atomic_write_bytes(rec.path, bytes_to_restore, mode=rec.pre_mode)

        rb_msg = step6_reason or "rolled back after Step 6 failure"
        if cas_conflicts:
            rb_msg = f"{rb_msg}; cas_conflicts: {', '.join(cas_conflicts)}"

        return CutoverOutcome(
            exit_code=1,
            status="ROLLED_BACK",
            target_version=target_version,
            target_git_sha=target_git_sha,
            rolled_back=True,
            updated_surfaces=updated_surfaces,
            skipped_surfaces=skipped_surfaces,
            cas_conflicts=cas_conflicts,
            would_abort=would_abort,
            message=rb_msg,
            findings=stale_findings,
            stale_locks=stale_findings,
        )

    # Step 6 succeeded: archive `.prev.<pid>` and `.current.prev_dir.<pid>` into `~/.minni/archive/<ts>/`
    arch_root = _archive_dir(home, ts)
    archive_container_prev_subtrees(all_container_swaps, arch_root / "containers")
    if flip_state.prev_dir_path and flip_state.prev_dir_path.exists():
        dest_prev_current = arch_root / "current_prev_dir"
        if dest_prev_current.exists():
            shutil.rmtree(dest_prev_current, ignore_errors=True)
        shutil.move(str(flip_state.prev_dir_path), str(dest_prev_current))

    # Write wired.json v2 (skip when already converged with zero surface changes)
    new_wired = build_wired_v2(
        active_version=target_version,
        git_commit_sha=target_git_sha,
        home=home,
        existing=wired_doc,
    )
    already_wired_v2_identical = (
        not updated_surfaces
        and not all_container_swaps
        and flip_state.prev_link_target == target_version
        and flip_state.prev_dir_path is None
        and isinstance(wired_doc, dict)
        and wired_doc.get("schema_version") == 2
        and wired_doc.get("active_version") == target_version
        and wired_doc.get("git_commit_sha") == target_git_sha
        and wired_doc.get("hosts") == new_wired.get("hosts")
        and wired_doc.get("wires") == new_wired.get("wires")
    )
    if not already_wired_v2_identical:
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
            findings=stale_findings,
            stale_locks=stale_findings,
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
        findings=stale_findings,
        stale_locks=stale_findings,
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
    allow_default_repo: bool = True,
) -> CutoverOutcome:
    """High-level entry point for host reconciliation and migration (`--dry-run` default per §5)."""
    resolved_home = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    plugin_dir = resolved_home / ".minni" / "plugin"

    # Single repo-root resolution rule for both --dry-run and --apply (R7 P2)
    resolved_repo: Path | None = None
    if from_repo is not None:
        resolved_repo = Path(from_repo).expanduser().resolve()
    elif allow_default_repo and target_version is None:
        resolved_repo = _default_repo_root()

    staged_tmp: Path | None = None
    try:
        if target_version is None and resolved_repo is not None:
            pkg_dir = resolved_repo / "plugins" / "minni"
            if (resolved_repo / "pyproject.toml").is_file() or pkg_dir.is_dir():
                try:
                    staged_tmp, manifest = _stage_repo_payload_to_temp(
                        resolved_repo,
                        sha_override=target_git_sha,
                    )
                    target_version = manifest.version
                    if target_git_sha is None:
                        target_git_sha = manifest.git_sha
                    if not dry_run:
                        base_sv = str(target_version).split("+", 1)[0] if target_version else "0.6.0"
                        has_cmd_abort = False
                        for _hid, _hspec in HOST_SURFACE_CATALOG.items():
                            for _surf in _hspec.get("surfaces", []):
                                if isinstance(_surf, dict) and str(_surf.get("kind") or "") == "commands_dir":
                                    _cp = Path(
                                        expand_for_home(
                                            str(_surf.get("path") or "").replace("{base_semver}", base_sv),
                                            home=resolved_home,
                                        )
                                    )
                                    if _cp.exists() and not _cp.is_symlink():
                                        has_cmd_abort = True
                                        break
                        if not has_cmd_abort:
                            _install_staged_payload_to_home(staged_tmp, manifest, resolved_home)
                except Exception as exc:
                    if staged_tmp is not None:
                        shutil.rmtree(staged_tmp, ignore_errors=True)
                        staged_tmp = None
                    return CutoverOutcome(
                        exit_code=1,
                        status="ABORTED_PRE_FLIP",
                        target_version=target_version or "",
                        target_git_sha=target_git_sha or "",
                        aborted_pre_flip=True,
                        message=f"payload_staging_failed: {type(exc).__name__}",
                    )

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
            repo_root=resolved_repo,
            payload_ref_dir=staged_tmp,
        )
    finally:
        if staged_tmp is not None:
            shutil.rmtree(staged_tmp, ignore_errors=True)


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

"""7-step atomic cutover, pre-flip scratch probes, and key-scoped CAS rollback (§4.3, §5)."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    resolve_antigravity_container_dir,
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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    tmp.write_bytes(data)
    os.replace(tmp, path)


def run_behavioral_probe(version_dir: Path) -> tuple[bool, str]:
    """Run pre-flip MCP stdio `initialize` + `tools/list` (>=40 tools) and `SessionStart` hook probe."""
    server_js = version_dir / "dist" / "server.js"
    hook_js = version_dir / "dist" / "hook.js"
    if not server_js.is_file():
        return False, f"missing server.js at {server_js}"

    with tempfile.TemporaryDirectory(prefix="minni-probe-home-") as probe_home:
        probe_env = {
            **os.environ,
            "HOME": probe_home,
            "MINNI_HOME": str(Path(probe_home) / ".minni"),
            "MINNI_VAULT_PATH": str(Path(probe_home) / "vault"),
            "MINNI_AGENT_ID": "probe",
        }
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
        try:
            proc = subprocess.run(
                ["node", str(server_js)],
                input=rpc_input,
                capture_output=True,
                text=True,
                timeout=8,
                env=probe_env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_text = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            proc = subprocess.CompletedProcess(
                args=["node", str(server_js)],
                returncode=0,
                stdout=stdout_text,
                stderr="",
            )
        except OSError as exc:
            return False, f"failed to spawn node on {server_js}: {exc}"

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

        if not init_ok or tools_count < 40:
            return (
                False,
                f"MCP stdio probe failed on {server_js} (rc={proc.returncode}, init_ok={init_ok}, tools={tools_count})",
            )

        if hook_js.is_file():
            try:
                hproc = subprocess.run(
                    ["node", str(hook_js)],
                    input='{"hook_event_name":"SessionStart","cwd":"/tmp"}\n',
                    capture_output=True,
                    text=True,
                    timeout=8,
                    env=probe_env,
                    check=False,
                )
                if hproc.returncode != 0:
                    return False, f"hook.js probe failed with exit {hproc.returncode}: {hproc.stderr}"
            except Exception as exc:
                return False, f"hook.js probe exception: {exc}"

    return True, "ok"


def _validate_scratch_containers(scratch_dirs: dict[str, Path]) -> tuple[bool, str]:
    for host, sdir in scratch_dirs.items():
        if not sdir.is_dir():
            return False, f"scratch container missing for {host}: {sdir}"
        for sub in ("dist", "skills", "commands", "hooks"):
            sub_path = sdir / sub
            if not sub_path.exists():
                return False, f"scratch container {host} missing subtree {sub}"
        if not (sdir / "dist" / "server.js").is_file():
            return False, f"scratch container {host} missing dist/server.js"
    return True, "ok"


def _detect_running_rewrite_hosts() -> set[str]:
    running: set[str] = set()
    checks = {
        "muse": ["pgrep", "-x", "muse"],
        "claude-desktop": ["pgrep", "-x", "Claude"],
    }
    for host, cmd in checks.items():
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=3, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                running.add(host)
        except Exception:
            pass
    return running


def _converge_claude_local_marketplace(
    home: Path,
    version_dir: Path,
    target_version: str,
    target_git_sha: str,
    *,
    pid: int,
) -> tuple[list[SubtreeSwapRecord], tuple[Path, bytes | None] | None]:
    claude_plugins = home / ".claude" / "plugins"
    if not claude_plugins.is_dir():
        return [], None

    mkt_plugin_dir = claude_plugins / "marketplaces" / "minni-local" / "plugins" / "minni"
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
            _atomic_write_bytes(
                cplugin_dir / "plugin.json",
                (json.dumps(data, indent=2) + "\n").encode("utf-8"),
            )
    src_pkg = version_dir / "package.json"
    if src_pkg.is_file():
        shutil.copy2(src_pkg, mkt_plugin_dir / "package.json")

    inst_path = claude_plugins / "installed_plugins.json"
    prev_inst: tuple[Path, bytes | None] = (
        inst_path,
        inst_path.read_bytes() if inst_path.is_file() else None,
    )
    doc: dict[str, Any] = {"version": 2, "plugins": {}}
    if inst_path.is_file():
        try:
            parsed = json.loads(inst_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                doc = parsed
        except Exception:
            pass
    plugins_map = doc.setdefault("plugins", {})
    if isinstance(plugins_map, dict):
        expanded_install_path = str(mkt_plugin_dir)
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        for key in ("minni@minni-local", "minni@minni"):
            entries = plugins_map.get(key)
            if isinstance(entries, list) and entries:
                for item in entries:
                    if isinstance(item, dict):
                        item["installPath"] = expanded_install_path
                        item["version"] = target_version
                        item["gitCommitSha"] = target_git_sha
                        item["lastUpdated"] = now_iso
                        if "projectPath" in item:
                            item["projectPath"] = str(
                                home / ".claude" / "plugins" / "marketplaces" / "minni-local"
                            )
        if "minni@minni-local" not in plugins_map and "minni@minni" not in plugins_map:
            plugins_map["minni@minni-local"] = [
                {
                    "scope": "user",
                    "installPath": expanded_install_path,
                    "version": target_version,
                    "installedAt": now_iso,
                    "lastUpdated": now_iso,
                    "gitCommitSha": target_git_sha,
                }
            ]
    _atomic_write_bytes(inst_path, (json.dumps(doc, indent=2) + "\n").encode("utf-8"))
    return swaps, prev_inst


def _extract_referenced_versions(text: str, plugin_dir: Path) -> set[str]:
    found: set[str] = set()
    if not plugin_dir.is_dir():
        return found
    for child in plugin_dir.iterdir():
        if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
            if str(child) in text:
                found.add(child.name)
    return found


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
    scratch_root, scratch_dirs = stage_scratch_containers(vdir, home=home)
    try:
        # Step 3 — Pre-Flip Behavioral Probe (R4-2)
        scratch_ok, scratch_msg = _validate_scratch_containers(scratch_dirs)
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
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    wired_doc = _load_wired(plugin_dir / "wired.json")
    # Step 4 — Flip `current` & Swap Real Per-Host Containers (P2-6, R4-1)
    flip_state: CurrentFlipState = flip_current_pointer(target_version, home=home, pid=pid)
    all_container_swaps: list[SubtreeSwapRecord] = []
    prev_installed_plugins: tuple[Path, bytes | None] | None = None

    for chost in ("antigravity", "cursor"):
        if chost == "antigravity":
            ag_plugins_dir = home / ".agents" / "plugins"
            should_converge = ag_plugins_dir.exists()
        else:
            cu_dir = home / ".cursor" / "plugins" / "local" / "minni"
            should_converge = cu_dir.exists() or cu_dir.parent.exists()
        if should_converge:
            mode = effective_container_mode(chost, wired_doc)
            swaps = converge_host_container(
                chost,
                home=home,
                container_mode=mode,
                target_version_root=vdir,
                pid=pid,
            )
            all_container_swaps.extend(swaps)

    mkt_swaps, prev_installed_plugins = _converge_claude_local_marketplace(
        home, vdir, target_version, target_git_sha, pid=pid
    )
    all_container_swaps.extend(mkt_swaps)

    # Step 5 — Reconcile Host Configs (`delta != 0` only, Rewrite-on-Run Guard & CAS Snapshot)
    active_running = (
        set(running_hosts)
        if running_hosts is not None
        else _detect_running_rewrite_hosts()
    )
    rollback_records: list[_ConfigRollbackRecord] = []
    updated_surfaces: list[str] = []
    skipped_surfaces: list[str] = []
    deferred_running_hosts: list[str] = []
    deferred_protected_versions: set[str] = set()

    # Build list of JSON MCP surfaces from HOST_SURFACE_CATALOG plus legacy muse config.json
    json_mcp_surfaces: list[tuple[str, Path, dict[str, Any], bool]] = []
    for host_id, hspec in HOST_SURFACE_CATALOG.items():
        rewrite_hazard = str(hspec.get("rewrite_on_run_hazard") or "none") != "none"
        for surf in hspec.get("surfaces", []):
            if isinstance(surf, dict) and surf.get("kind") == "mcp_json":
                p = Path(expand_for_home(str(surf["path"]), home=home))
                json_mcp_surfaces.append((host_id, p, surf, rewrite_hazard))
    # Also support ~/.muse/config.json as a muse mcp_json surface
    muse_legacy_cfg = home / ".muse" / "config.json"
    if muse_legacy_cfg.is_file():
        json_mcp_surfaces.append(
            (
                "muse",
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

    for host, cfg_path, surf_spec, rewrite_on_run in json_mcp_surfaces:
        if not cfg_path.is_file():
            continue
        try:
            pre_mtime_ns, pre_sha256, pre_bytes = _file_stat_and_sha256(cfg_path)
            doc = json.loads(pre_bytes.decode("utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        container_key = "mcp" if surf_spec.get("key") == "mcp.minni" else "mcpServers"
        servers = doc.get(container_key)
        if not isinstance(servers, dict) or "minni" not in servers:
            continue

        hspec = HOST_SURFACE_CATALOG[host]
        diff = evaluate_mcp_surface_diff(
            servers.get("minni"),
            host_id=host,
            agent_id=str(hspec["agent_id"]),
            vault_path=str(hspec["vault_path"]),
            surface_spec=surf_spec,
            home=home,
        )
        if not diff.has_delta:
            skipped_surfaces.append(f"{host}:{cfg_path}")
            continue

        # Running rewrite-on-run host guard (N6, P1-5, R4-4)
        if host in ("muse", "claude-desktop") and host in active_running:
            if host not in deferred_running_hosts:
                deferred_running_hosts.append(host)
            deferred_protected_versions |= _extract_referenced_versions(
                pre_bytes.decode("utf-8", errors="replace"),
                plugin_dir,
            )
            continue

        prev_minni = copy.deepcopy(servers.get("minni"))
        new_doc = copy.deepcopy(doc)
        new_doc.setdefault(container_key, {})["minni"] = diff.desired_entry
        new_bytes = (json.dumps(new_doc, indent=2) + "\n").encode("utf-8")
        _archive_file_before_write(home, cfg_path, ts)
        _atomic_write_bytes(cfg_path, new_bytes)
        post_mtime_ns, post_sha256, _ = _file_stat_and_sha256(cfg_path)
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
            )
        )
        updated_surfaces.append(f"{host}:{cfg_path}")

    # Step 6 — Post-Flip Verification & CAS Rollback (N5, R4-1)
    step6_ok = True
    step6_reason = ""
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

    if not step6_ok:
        cas_conflicts: list[str] = []
        # 1. Restore `current` (handles real-dir `current` via unlink+rename without ENOTDIR, R4-1)
        rollback_current_pointer(flip_state)
        # 2. Restore container subtrees
        rollback_container_subtrees(all_container_swaps)
        if prev_installed_plugins is not None:
            inst_path, prev_raw = prev_installed_plugins
            if prev_raw is None:
                inst_path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(inst_path, prev_raw)
        # 3. Restore modified host config files with CAS check on rewrite-on-run files (N5)
        for rec in reversed(rollback_records):
            if not rec.rewrite_on_run:
                _atomic_write_bytes(rec.path, rec.pre_bytes)
                continue
            try:
                cur_mtime_ns, cur_sha256, cur_bytes = _file_stat_and_sha256(rec.path)
            except OSError:
                _atomic_write_bytes(rec.path, rec.pre_bytes)
                continue
            if cur_mtime_ns == rec.post_step5_mtime_ns and cur_sha256 == rec.post_step5_sha256:
                _atomic_write_bytes(rec.path, rec.pre_bytes)
            else:
                # Concurrent mutation after Step 5! Perform key-scoped restore (N5)
                try:
                    live_doc = json.loads(cur_bytes.decode("utf-8"))
                    if not isinstance(live_doc, dict):
                        raise ValueError("not a JSON object")
                    ckey = "mcp" if rec.host == "kilocode" else "mcpServers"
                    srv = live_doc.setdefault(ckey, {})
                    if isinstance(srv, dict):
                        if rec.previous_minni_entry is None:
                            srv.pop("minni", None)
                        else:
                            srv["minni"] = rec.previous_minni_entry
                    _atomic_write_bytes(
                        rec.path,
                        (json.dumps(live_doc, indent=2) + "\n").encode("utf-8"),
                    )
                except Exception:
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

    # Step 7 — Reference-Aware GC (§4.5)
    gc_obj = collect_garbage(
        home=home,
        keep_last=2,
        prev_target=flip_state.prev_link_target,
    )
    gc_res = {
        "pruned": list(gc_obj.pruned),
        "retained_in_use": list(gc_obj.retained_in_use),
        "would_prune": list(gc_obj.would_prune),
    }

    if deferred_running_hosts:
        hosts_str = ", ".join(deferred_running_hosts)
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
                f"DEFERRED_RUNNING_HOST_DELTA: deferred config rewrite for running host(s) "
                f"[{hosts_str}] to prevent rewrite-on-exit clobber; quit [{hosts_str}] and re-run `minni sync`."
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
                if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and (d / "payload-manifest.json").is_file()
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

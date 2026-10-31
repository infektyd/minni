"""Stable launch pointer (~/.minni/plugin/current) & real per-host plugin containers (§4.1)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minni.wire.manifest import sha256_file
from minni.wire.registry import expand_for_home, resolve_home

CONTAINER_SUBTREES: tuple[str, ...] = ("dist", "node_modules", "skills", "commands", "hooks")


@dataclass
class CurrentFlipState:
    current_path: Path
    new_version: str
    pid: int
    prev_link_target: str | None = None
    prev_dir_path: Path | None = None


@dataclass
class SubtreeSwapRecord:
    container_dir: Path
    subtree_name: str
    subtree_path: Path
    pid: int
    container_mode: str
    prev_dir: Path | None = None
    prev_link_target: str | None = None
    had_prior: bool = False


def flip_current_pointer(
    version: str,
    *,
    home: Path | None = None,
    pid: int | None = None,
) -> CurrentFlipState:
    """Atomically flip `~/.minni/plugin/current -> version`, handling a real dir at `current` (`P2-6`, `R4-1`)."""
    proc_pid = pid if pid is not None else os.getpid()
    base = resolve_home(home) / ".minni" / "plugin"
    base.mkdir(parents=True, exist_ok=True)
    current_path = base / "current"

    prev_link_target: str | None = None
    prev_dir_path: Path | None = None

    if current_path.is_symlink():
        try:
            prev_link_target = os.readlink(current_path)
        except OSError:
            prev_link_target = None
    elif current_path.exists() and current_path.is_dir():
        prev_dir_path = base / f".current.prev_dir.{proc_pid}"
        if prev_dir_path.exists():
            shutil.rmtree(prev_dir_path, ignore_errors=True)
        os.rename(current_path, prev_dir_path)

    tmp_link = base / f".current.tmp.{proc_pid}"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(version, tmp_link)
    try:
        os.replace(tmp_link, current_path)
    except Exception:
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink(missing_ok=True)
        if prev_dir_path is not None and prev_dir_path.exists() and not current_path.exists():
            os.rename(prev_dir_path, current_path)
        raise

    return CurrentFlipState(
        current_path=current_path,
        new_version=version,
        pid=proc_pid,
        prev_link_target=prev_link_target,
        prev_dir_path=prev_dir_path,
    )


def rollback_current_pointer(state: CurrentFlipState) -> None:
    """Restore `~/.minni/plugin/current` on Step 7a rollback (`R4-1`).

    When `current` was previously a real directory renamed aside to `.current.prev_dir.<pid>`,
    uses unlink-then-rename (`os.unlink` + `os.rename`) so a directory is never renamed directly
    onto a symlink (`ENOTDIR`).
    """
    current_path = state.current_path
    if state.prev_dir_path is not None and state.prev_dir_path.exists():
        if current_path.is_symlink() or current_path.is_file():
            os.unlink(current_path)
        elif current_path.exists() and current_path.is_dir():
            shutil.rmtree(current_path, ignore_errors=True)
        os.rename(state.prev_dir_path, current_path)
        return

    if state.prev_link_target is not None:
        tmp_rollback = current_path.parent / f".current.rollback.{state.pid}"
        if tmp_rollback.exists() or tmp_rollback.is_symlink():
            tmp_rollback.unlink()
        os.symlink(state.prev_link_target, tmp_rollback)
        os.replace(tmp_rollback, current_path)
        return

    if current_path.is_symlink() or current_path.is_file():
        os.unlink(current_path)


def resolve_verified_plugin_root(
    link_path: Path,
    expected_base: Path | None = None,
) -> Path:
    """Strict symlink validation rule (§4.1 #6, fixes D3)."""
    base = expected_base if expected_base is not None else (link_path.parent)
    if not link_path.is_symlink():
        raise ValueError(f"{link_path} is not a symlink")
    if not link_path.exists():
        raise ValueError(f"{link_path} is a dangling symlink (target does not exist)")
    try:
        target = link_path.resolve(strict=True)
        base_resolved = base.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"cannot resolve {link_path}: {exc}") from exc

    if not target.is_dir():
        raise ValueError(f"{link_path} target {target} is not a directory")
    if not os.path.samestat(os.stat(target.parent), os.stat(base_resolved)):
        raise ValueError(f"{link_path} target {target} escapes expected base {base_resolved}")

    manifest_file = target / "payload-manifest.json"
    server_file = target / "dist" / "server.js"
    if not manifest_file.is_file():
        raise ValueError(f"missing payload-manifest.json in {target}")
    if not server_file.is_file():
        raise ValueError(f"missing dist/server.js in {target}")

    try:
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"unreadable payload-manifest.json in {target}: {exc}") from exc

    files = manifest_data.get("files") if isinstance(manifest_data, dict) else None
    if isinstance(files, dict) and "dist/server.js" in files:
        expected_hash = str(files["dist/server.js"])
        actual_hash = sha256_file(server_file)
        if actual_hash != expected_hash:
            raise ValueError(
                f"dist/server.js hash mismatch in {target}: {actual_hash} != {expected_hash}"
            )
    return target


def resolve_antigravity_container_dir(home: Path | None = None) -> tuple[Path, bool]:
    """Resolve `~/.agents/plugins/Minni@Minni` with case-insensitive APFS `samestat` guard (`P1-2`).

    Returns `(canonical_container_dir, alias_symlink_created)`.
    """
    resolved_home = resolve_home(home)
    plugins_dir = resolved_home / ".agents" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    camel_path = plugins_dir / "Minni@Minni"
    lower_path = plugins_dir / "minni@minni"

    if camel_path.is_symlink():
        camel_path.unlink()
    camel_path.mkdir(parents=True, exist_ok=True)

    if os.path.exists(lower_path) and os.path.exists(camel_path):
        try:
            if os.path.samestat(os.stat(lower_path), os.stat(camel_path)):
                return camel_path, False
        except OSError:
            pass

    if not lower_path.exists() and not lower_path.is_symlink():
        os.symlink("Minni@Minni", lower_path)
        return camel_path, True

    return camel_path, False


def render_antigravity_container_manifests(
    home: Path | None = None,
    base_semver: str = "0.6.0",
) -> dict[str, dict[str, Any]]:
    """Generate expanded per-host manifests for `~/.agents/plugins/Minni@Minni` (`P1-1`, `N6`, `N7`)."""
    resolved_home = resolve_home(home)
    clean_ver = base_semver.split("+", 1)[0]
    server_entry = expand_for_home("~/.agents/plugins/Minni@Minni/dist/server.js", home=resolved_home)
    vault_path = expand_for_home("~/.minni/gemini-vault", home=resolved_home)
    env = {
        "MINNI_AGENT_ID": "gemini",
        "MINNI_VAULT_PATH": vault_path,
    }
    mcp_doc = {
        "mcpServers": {
            "minni": {
                "command": "node",
                "args": [server_entry],
                "env": dict(env),
            }
        }
    }
    ext_doc = {
        "name": "minni",
        "version": clean_ver,
        "description": "Minni sovereign memory extension for Gemini & Antigravity",
        "env": dict(env),
        "mcpServers": {
            "minni": {
                "command": "node",
                "args": [server_entry],
                "env": dict(env),
            }
        },
    }
    return {
        ".mcp.json": mcp_doc,
        "gemini-extension.json": ext_doc,
        ".gemini-plugin/gemini-extension.json": ext_doc,
    }


def render_cursor_container_manifests(
    home: Path | None = None,
    base_semver: str = "0.6.0",
) -> dict[str, dict[str, Any]]:
    """Generate expanded per-host manifests for `~/.cursor/plugins/local/minni` (`P1-1`, `N6`, `N7`)."""
    resolved_home = resolve_home(home)
    clean_ver = base_semver.split("+", 1)[0]
    server_entry = expand_for_home("~/.cursor/plugins/local/minni/dist/server.js", home=resolved_home)
    vault_path = expand_for_home("~/.minni/cursor-vault", home=resolved_home)
    env = {
        "MINNI_AGENT_ID": "cursor",
        "MINNI_VAULT_PATH": vault_path,
    }
    mcp_doc = {
        "mcpServers": {
            "minni": {
                "command": "node",
                "args": [server_entry],
                "env": dict(env),
            }
        }
    }
    plugin_doc = {
        "name": "minni",
        "version": clean_ver,
        "description": "Minni local plugin for Cursor",
    }
    return {
        ".mcp.json": mcp_doc,
        ".cursor-plugin/plugin.json": plugin_doc,
    }


def _write_container_manifests(
    container_dir: Path,
    manifests: dict[str, dict[str, Any]],
    archive_dir: Path | None = None,
) -> None:
    container_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, doc in manifests.items():
        dest = container_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if archive_dir is not None and dest.exists() and not dest.is_symlink():
            arch_target = archive_dir / "container-manifests" / container_dir.name / rel_path
            arch_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, arch_target)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
        os.close(tmp_fd)
        try:
            Path(tmp_name).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_name, dest)
        finally:
            if Path(tmp_name).exists():
                Path(tmp_name).unlink(missing_ok=True)


def swap_container_subtrees(
    container_dir: Path,
    *,
    stable_current_root: Path,
    target_version_root: Path,
    container_mode: str = "symlink_subtrees",
    pid: int | None = None,
) -> list[SubtreeSwapRecord]:
    """Swap subtrees `(dist, node_modules, skills, commands, hooks)` in-place via rename-aside (`N3`, `R4-2`)."""
    if container_mode not in ("symlink_subtrees", "staged_copy"):
        raise ValueError(f"unsupported container_mode {container_mode!r}")

    proc_pid = pid if pid is not None else os.getpid()
    records: list[SubtreeSwapRecord] = []

    for sub in CONTAINER_SUBTREES:
        src_in_version = target_version_root / sub
        sub_path = container_dir / sub
        if not src_in_version.exists() and not sub_path.exists() and not sub_path.is_symlink():
            continue

        prev_link_target: str | None = None
        prev_dir: Path | None = None
        had_prior = sub_path.exists() or sub_path.is_symlink()

        if container_mode == "symlink_subtrees":
            link_target = str(stable_current_root / sub)
            tmp_link = container_dir / f"{sub}.tmp.{proc_pid}"
            if tmp_link.exists() or tmp_link.is_symlink():
                tmp_link.unlink()
            os.symlink(link_target, tmp_link)

            if sub_path.is_symlink():
                try:
                    prev_link_target = os.readlink(sub_path)
                except OSError:
                    prev_link_target = None
                os.replace(tmp_link, sub_path)
            elif sub_path.exists():
                prev_dir = container_dir / f"{sub}.prev.{proc_pid}"
                if prev_dir.exists() or prev_dir.is_symlink():
                    if prev_dir.is_symlink() or prev_dir.is_file():
                        prev_dir.unlink()
                    else:
                        shutil.rmtree(prev_dir, ignore_errors=True)
                os.rename(sub_path, prev_dir)
                try:
                    os.replace(tmp_link, sub_path)
                except Exception:
                    if tmp_link.exists() or tmp_link.is_symlink():
                        tmp_link.unlink(missing_ok=True)
                    if prev_dir.exists() and not sub_path.exists():
                        os.rename(prev_dir, sub_path)
                    raise
            else:
                os.replace(tmp_link, sub_path)
        else:
            # staged_copy mode
            if not src_in_version.exists():
                continue
            new_dir = container_dir / f"{sub}.new.{proc_pid}"
            if new_dir.exists() or new_dir.is_symlink():
                if new_dir.is_symlink() or new_dir.is_file():
                    new_dir.unlink()
                else:
                    shutil.rmtree(new_dir, ignore_errors=True)
            if src_in_version.is_dir():
                shutil.copytree(src_in_version, new_dir, symlinks=False)
            else:
                shutil.copy2(src_in_version, new_dir)

            if sub_path.is_symlink():
                try:
                    prev_link_target = os.readlink(sub_path)
                except OSError:
                    prev_link_target = None
                sub_path.unlink()
                os.rename(new_dir, sub_path)
            elif sub_path.exists():
                prev_dir = container_dir / f"{sub}.prev.{proc_pid}"
                if prev_dir.exists():
                    shutil.rmtree(prev_dir, ignore_errors=True)
                os.rename(sub_path, prev_dir)
                try:
                    os.rename(new_dir, sub_path)
                except Exception:
                    if prev_dir.exists() and not sub_path.exists():
                        os.rename(prev_dir, sub_path)
                    raise
            else:
                os.rename(new_dir, sub_path)

        records.append(
            SubtreeSwapRecord(
                container_dir=container_dir,
                subtree_name=sub,
                subtree_path=sub_path,
                pid=proc_pid,
                container_mode=container_mode,
                prev_dir=prev_dir,
                prev_link_target=prev_link_target,
                had_prior=had_prior,
            )
        )
    return records


def rollback_container_subtrees(records: list[SubtreeSwapRecord]) -> None:
    """Restore container subtrees from `S.prev.<pid>` or prior link targets on Step 7a rollback (`N3`)."""
    for rec in reversed(records):
        sub_path = rec.subtree_path
        if rec.prev_dir is not None and (rec.prev_dir.exists() or rec.prev_dir.is_symlink()):
            if sub_path.is_symlink() or sub_path.is_file():
                os.unlink(sub_path)
            elif sub_path.exists() and sub_path.is_dir():
                failed_dir = rec.container_dir / f"{rec.subtree_name}.failed.{rec.pid}"
                if failed_dir.exists():
                    shutil.rmtree(failed_dir, ignore_errors=True)
                os.rename(sub_path, failed_dir)
                shutil.rmtree(failed_dir, ignore_errors=True)
            os.rename(rec.prev_dir, sub_path)
        elif rec.prev_link_target is not None:
            if sub_path.exists() and not sub_path.is_symlink() and sub_path.is_dir():
                shutil.rmtree(sub_path, ignore_errors=True)
            tmp_link = rec.container_dir / f"{rec.subtree_name}.rollback.{rec.pid}"
            if tmp_link.exists() or tmp_link.is_symlink():
                tmp_link.unlink()
            os.symlink(rec.prev_link_target, tmp_link)
            os.replace(tmp_link, sub_path)
        elif not rec.had_prior:
            if sub_path.is_symlink() or sub_path.is_file():
                sub_path.unlink(missing_ok=True)
            elif sub_path.exists() and sub_path.is_dir():
                shutil.rmtree(sub_path, ignore_errors=True)


def archive_container_prev_subtrees(
    records: list[SubtreeSwapRecord],
    archive_dir: Path,
) -> list[Path]:
    """Move held `S.prev.<pid>` directories into `archive_dir` once Step 6 verification passes (`N3`, `P3-1`)."""
    moved: list[Path] = []
    for rec in records:
        if rec.prev_dir is not None and (rec.prev_dir.exists() or rec.prev_dir.is_symlink()):
            dest_parent = archive_dir / rec.container_dir.name
            dest_parent.mkdir(parents=True, exist_ok=True)
            dest = dest_parent / rec.subtree_name
            if dest.exists() or dest.is_symlink():
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                else:
                    shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(rec.prev_dir), str(dest))
            moved.append(dest)
    return moved


def converge_host_container(
    host_id: str,
    *,
    home: Path | None = None,
    base_semver: str = "0.6.0",
    container_mode: str = "symlink_subtrees",
    target_version_root: Path,
    archive_dir: Path | None = None,
    pid: int | None = None,
) -> list[SubtreeSwapRecord]:
    """Converge `antigravity` (`Minni@Minni`) or `cursor` (`local/minni`) real container directory."""
    resolved_home = resolve_home(home)
    stable_current = resolved_home / ".minni" / "plugin" / "current"

    if host_id == "antigravity":
        container_dir, _ = resolve_antigravity_container_dir(resolved_home)
        manifests = render_antigravity_container_manifests(resolved_home, base_semver=base_semver)
    elif host_id == "cursor":
        container_dir = resolved_home / ".cursor" / "plugins" / "local" / "minni"
        if container_dir.is_symlink():
            container_dir.unlink()
        container_dir.mkdir(parents=True, exist_ok=True)
        manifests = render_cursor_container_manifests(resolved_home, base_semver=base_semver)
    else:
        raise ValueError(f"unsupported container host {host_id!r}")

    _write_container_manifests(container_dir, manifests, archive_dir=archive_dir)
    return swap_container_subtrees(
        container_dir,
        stable_current_root=stable_current,
        target_version_root=target_version_root,
        container_mode=container_mode,
        pid=pid,
    )


def stage_scratch_containers(
    target_version_root: Path,
    *,
    home: Path | None = None,
    base_semver: str = "0.6.0",
    container_modes: dict[str, str] | None = None,
) -> tuple[Path, dict[str, Path]]:
    """Build disposable pre-flip scratch containers `<mkdtemp>/Minni@Minni` and `local/minni` (§4.1 #3, `N3`)."""
    resolved_home = resolve_home(home)
    modes = container_modes or {}
    scratch_root = Path(tempfile.mkdtemp(prefix="minni-scratch-containers-"))

    ag_scratch = scratch_root / "Minni@Minni"
    cu_scratch = scratch_root / "local" / "minni"
    ag_scratch.mkdir(parents=True, exist_ok=True)
    cu_scratch.mkdir(parents=True, exist_ok=True)

    _write_container_manifests(
        ag_scratch,
        render_antigravity_container_manifests(resolved_home, base_semver=base_semver),
    )
    _write_container_manifests(
        cu_scratch,
        render_cursor_container_manifests(resolved_home, base_semver=base_semver),
    )

    ag_mode = modes.get("antigravity", "symlink_subtrees")
    cu_mode = modes.get("cursor", "symlink_subtrees")

    swap_container_subtrees(
        ag_scratch,
        stable_current_root=target_version_root,
        target_version_root=target_version_root,
        container_mode=ag_mode,
    )
    swap_container_subtrees(
        cu_scratch,
        stable_current_root=target_version_root,
        target_version_root=target_version_root,
        container_mode=cu_mode,
    )
    return scratch_root, {"antigravity": ag_scratch, "cursor": cu_scratch}

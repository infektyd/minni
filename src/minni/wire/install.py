"""Atomic payload install under ~/.minni/plugin/<version>/."""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from minni.wire.manifest import PayloadManifest, hash_mismatches
from minni.wire.paths import install_lock_path, plugin_base, quarantine_dir, staging_dir


@dataclass
class InstallResult:
    install_root: Path
    version: str
    skipped: bool = False
    quarantined: str | None = None


class InstallError(Exception):
    pass


class HashMismatchError(InstallError):
    def __init__(self, version: str, mismatched: list[str]) -> None:
        self.version = version
        self.mismatched = mismatched
        super().__init__(
            f"version dir ~/.minni/plugin/{version} exists with hash mismatch "
            f"({len(mismatched)} file(s)); use --force-reinstall",
        )


def _sanitize_staged_payload(stage: Path) -> None:
    """Omit `.mcp.json` and strip `mcpServers` from `.claude-plugin/plugin.json` in staged copy only (§4.1 #1, `N2`)."""
    import json
    from minni.wire.manifest import sha256_file

    mcp_file = stage / ".mcp.json"
    if mcp_file.exists() or mcp_file.is_symlink():
        mcp_file.unlink(missing_ok=True)

    cplugin_file = stage / ".claude-plugin" / "plugin.json"
    cplugin_modified = False
    if cplugin_file.is_file():
        try:
            cdata = json.loads(cplugin_file.read_text(encoding="utf-8"))
            if isinstance(cdata, dict) and "mcpServers" in cdata:
                cdata.pop("mcpServers", None)
                cplugin_file.write_text(
                    json.dumps(cdata, indent=2) + "\n", encoding="utf-8",
                )
                cplugin_modified = True
        except Exception:
            pass

    pmanifest_file = stage / "payload-manifest.json"
    if pmanifest_file.is_file():
        try:
            mdata = json.loads(pmanifest_file.read_text(encoding="utf-8"))
            files = mdata.get("files")
            if isinstance(files, dict):
                changed = False
                if ".mcp.json" in files:
                    files.pop(".mcp.json", None)
                    changed = True
                if cplugin_modified and cplugin_file.is_file():
                    files[".claude-plugin/plugin.json"] = sha256_file(cplugin_file)
                    changed = True
                if changed:
                    mdata["files"] = files
                    pmanifest_file.write_text(
                        json.dumps(mdata, indent=2) + "\n", encoding="utf-8",
                    )
        except Exception:
            pass


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))
    _sanitize_staged_payload(dest)


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _install_locked(
    payload_src: Path,
    version: str,
    manifest: PayloadManifest,
    *,
    force_reinstall: bool,
    dry_run: bool,
) -> InstallResult:
    src_root = payload_src
    base = plugin_base()
    target = base / version

    if dry_run:
        # Read-only probe: a dry run must leave no state behind — no base dir,
        # no lock file. Mirrors the locked branches below without writing.
        if target.exists():
            mismatched = hash_mismatches(manifest, target)
            if not mismatched:
                return InstallResult(install_root=target, version=version, skipped=True)
            if not force_reinstall:
                raise HashMismatchError(version, mismatched)
            return InstallResult(
                install_root=target, version=version, skipped=False,
                quarantined=str(quarantine_dir(version)),
            )
        return InstallResult(install_root=target, version=version, skipped=False)

    base.mkdir(parents=True, exist_ok=True)
    for old_lock in base.glob(".install-*.lock"):
        old_lock.unlink(missing_ok=True)

    lock_path = install_lock_path(version)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if target.exists():
            mismatched = hash_mismatches(manifest, target)
            if not mismatched:
                return InstallResult(install_root=target, version=version, skipped=True)
            if not force_reinstall:
                raise HashMismatchError(version, mismatched)
            qdir = quarantine_dir(version)
            target.rename(qdir)
            result_quarantine = str(qdir)
        else:
            result_quarantine = None

        stage = staging_dir(version)
        try:
            _copy_tree(src_root, stage)
            _fsync_dir(stage)
            try:
                os.rename(stage, target)
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                if target.exists():
                    mismatched = hash_mismatches(manifest, target)
                    if not mismatched:
                        shutil.rmtree(stage, ignore_errors=True)
                        return InstallResult(
                            install_root=target, version=version, skipped=True,
                        )
                    if not force_reinstall:
                        shutil.rmtree(stage, ignore_errors=True)
                        raise HashMismatchError(version, mismatched) from exc
                raise InstallError(f"install rename failed: {exc}") from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

        return InstallResult(
            install_root=target, version=version, skipped=False,
            quarantined=result_quarantine,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def update_current_symlink(version: str, *, dry_run: bool = False) -> None:
    from minni.wire.manifest import is_local_version

    if is_local_version(version):
        return
    link = plugin_base() / "current"
    if dry_run:
        return
    if link.exists() and not link.is_symlink() and link.is_dir():
        prev_dir = link.parent / f".current.prev_dir.{os.getpid()}"
        if prev_dir.exists():
            shutil.rmtree(prev_dir, ignore_errors=True)
        os.rename(link, prev_dir)
    tmp = link.parent / f".current-{os.getpid()}"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(version, tmp)
    os.replace(tmp, link)


def install_payload(
    payload_src: Path,
    version: str,
    manifest: PayloadManifest,
    *,
    force_reinstall: bool = False,
    dry_run: bool = False,
    update_current: bool = True,
) -> InstallResult:
    result = _install_locked(
        payload_src, version, manifest,
        force_reinstall=force_reinstall, dry_run=dry_run,
    )
    if update_current and not dry_run:
        update_current_symlink(version)
    return result


install_plugin_payload = install_payload
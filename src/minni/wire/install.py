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


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))


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
    base.mkdir(parents=True, exist_ok=True)
    target = base / version

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
            if dry_run:
                return InstallResult(
                    install_root=target, version=version, skipped=False,
                    quarantined=str(quarantine_dir(version)),
                )
            qdir = quarantine_dir(version)
            target.rename(qdir)
            result_quarantine = str(qdir)
        else:
            result_quarantine = None

        if dry_run:
            return InstallResult(
                install_root=target, version=version, skipped=False,
                quarantined=result_quarantine,
            )

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
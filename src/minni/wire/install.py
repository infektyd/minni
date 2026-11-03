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


SANITIZED_PLUGIN_MANIFEST_RELPATHS: tuple[str, ...] = (
    ".claude-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
)


def sanitize_plugin_manifest_dict(
    cdata: dict[str, object],
    *,
    base_semver: str | None = None,
) -> tuple[dict[str, object], bool]:
    """Apply canonical H17 sanitization to a parsed `.claude-plugin/plugin.json` or `.cursor-plugin/plugin.json` dict."""
    out = dict(cdata)
    modified = False
    if "mcpServers" in out:
        out.pop("mcpServers", None)
        modified = True
    effective_semver = base_semver
    if not effective_semver and isinstance(out.get("version"), str) and "+" in str(out["version"]):
        effective_semver = str(out["version"]).split("+", 1)[0]
    if effective_semver and out.get("version") != effective_semver:
        out["version"] = effective_semver
        modified = True
    return out, modified


def sanitize_staged_payload(
    stage: Path,
    manifest: PayloadManifest | None = None,
    *,
    version: str | None = None,
    remove_mcp_json: bool = True,
) -> None:
    """Single canonical sanitization path for staged/installed payloads and host containers (H17, §4.1 #1, `N2`).

    - Omits `.mcp.json` when `remove_mcp_json=True` (shared payload / Claude marketplace).
    - Strips `mcpServers` and normalizes `version` to `base_semver` in both
      `.claude-plugin/plugin.json` and `.cursor-plugin/plugin.json`.
    - Updates `manifest.files` and `payload-manifest.json` hashes accordingly.
    """
    import json
    from minni.wire.manifest import sha256_file

    if remove_mcp_json:
        mcp_file = stage / ".mcp.json"
        if mcp_file.exists() or mcp_file.is_symlink():
            mcp_file.unlink(missing_ok=True)

    raw_ver = version or (manifest.version if manifest is not None else None)
    base_semver = str(raw_ver).split("+", 1)[0] if raw_ver else None

    modified_manifests: dict[str, bool] = {}
    for rel_pjson in SANITIZED_PLUGIN_MANIFEST_RELPATHS:
        pjson_file = stage / rel_pjson
        pjson_modified = False
        if pjson_file.is_file():
            try:
                cdata = json.loads(pjson_file.read_text(encoding="utf-8"))
                if not isinstance(cdata, dict):
                    raise InstallError(f"staged {rel_pjson} is not a JSON object: {pjson_file}")
                cdata, pjson_modified = sanitize_plugin_manifest_dict(cdata, base_semver=base_semver)
                if pjson_modified:
                    pjson_file.write_text(
                        json.dumps(cdata, indent=2) + "\n", encoding="utf-8",
                    )
            except InstallError:
                raise
            except Exception as exc:
                raise InstallError(
                    f"failed to sanitize staged {rel_pjson} ({pjson_file}): {exc}"
                ) from exc
        modified_manifests[rel_pjson] = pjson_modified

    if manifest is not None and isinstance(manifest.files, dict):
        if remove_mcp_json:
            manifest.files.pop(".mcp.json", None)
        for rel_pjson, pjson_modified in modified_manifests.items():
            pjson_file = stage / rel_pjson
            if pjson_file.is_file() and (pjson_modified or rel_pjson in manifest.files):
                manifest.files[rel_pjson] = sha256_file(pjson_file)

    pmanifest_file = stage / "payload-manifest.json"
    if pmanifest_file.is_file():
        try:
            mdata = json.loads(pmanifest_file.read_text(encoding="utf-8"))
            if not isinstance(mdata, dict):
                raise InstallError(f"staged payload-manifest.json is not a JSON object: {pmanifest_file}")
            files = mdata.get("files")
            if isinstance(files, dict):
                changed = False
                if remove_mcp_json and ".mcp.json" in files:
                    files.pop(".mcp.json", None)
                    changed = True
                for rel_pjson, pjson_modified in modified_manifests.items():
                    pjson_file = stage / rel_pjson
                    if pjson_file.is_file() and (pjson_modified or rel_pjson in files):
                        new_hash = sha256_file(pjson_file)
                        if files.get(rel_pjson) != new_hash:
                            files[rel_pjson] = new_hash
                            changed = True
                if changed:
                    mdata["files"] = files
                    pmanifest_file.write_text(
                        json.dumps(mdata, indent=2) + "\n", encoding="utf-8",
                    )
            if manifest is not None and isinstance(manifest.files, dict) and "payload-manifest.json" in manifest.files:
                manifest.files["payload-manifest.json"] = sha256_file(pmanifest_file)
        except InstallError:
            raise
        except Exception as exc:
            raise InstallError(
                f"failed to update staged payload-manifest.json ({pmanifest_file}): {exc}"
            ) from exc


_sanitize_staged_payload = sanitize_staged_payload


def _copy_tree(src: Path, dest: Path, manifest: PayloadManifest | None = None) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))
    sanitize_staged_payload(dest, manifest)


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
    old_locks = list(base.glob(".install-*.lock"))
    if old_locks:
        from datetime import UTC, datetime

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        lock_arch = base.parent / "archive" / stamp / "backups-and-locks"
        try:
            lock_arch.mkdir(parents=True, exist_ok=True)
            for old_lock in old_locks:
                shutil.move(str(old_lock), str(lock_arch / old_lock.name))
        except OSError as exc:
            raise InstallError(
                f"failed to archive legacy install lock into {lock_arch}: {exc}"
            ) from exc

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
            _copy_tree(src_root, stage, manifest)
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
    if not dry_run:
        if update_current:
            update_current_symlink(version)
        home_dir = plugin_base().parent.parent
        for rel_c, drop_mcp in (
            (Path(".agents") / "plugins" / "Minni@Minni", False),
            (Path(".agents") / "plugins" / "minni@minni", False),
            (Path(".cursor") / "plugins" / "local" / "minni", False),
            (Path(".claude") / "plugins" / "marketplaces" / "minni-local" / "plugins" / "minni", True),
        ):
            cdir = home_dir / rel_c
            if cdir.is_dir() and not cdir.is_symlink():
                sanitize_staged_payload(cdir, version=version, remove_mcp_json=drop_mcp)
    return result


install_plugin_payload = install_payload
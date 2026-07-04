"""Payload manifest load, verify, and version helpers."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from packaging.version import Version


@dataclass(frozen=True)
class PayloadManifest:
    schema: int
    version: str
    git_sha: str
    built_at: str
    node_engine: str
    files: dict[str, str]
    path: Path | None = None

    @classmethod
    def load(cls, manifest_path: Path) -> PayloadManifest:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cls(
            schema=int(data.get("schema", 0)),
            version=str(data["version"]),
            git_sha=str(data.get("git_sha", "")),
            built_at=str(data.get("built_at", "")),
            node_engine=str(data.get("node_engine", ">=20")),
            files={str(k): str(v) for k, v in data.get("files", {}).items()},
            path=manifest_path,
        )


def package_version() -> str:
    return importlib.metadata.version("minni")


def read_pyproject_version(repo_root: Path) -> str:
    text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise ValueError(f"Cannot read version from {repo_root / 'pyproject.toml'}")
    return match.group(1)


def dev_version(repo_root: Path) -> str:
    public = read_pyproject_version(repo_root)
    try:
        short = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        short = "unknown"
    dirty = False
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo_root, text=True,
        )
        dirty = bool(status.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    suffix = f"+git.{short}"
    if dirty:
        suffix += ".dirty"
    return f"{public}{suffix}"


def is_local_version(version: str) -> bool:
    return Version(version).local is not None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


# Wire re-stamps this file post-install; exclude from idempotency hash checks.
_INSTALL_MUTABLE_PATHS = frozenset({".mcp.json"})


def verify_manifest_hashes(manifest: PayloadManifest, root: Path) -> list[str]:
    errors: list[str] = []
    for rel, expected in manifest.files.items():
        if rel in _INSTALL_MUTABLE_PATHS:
            continue
        path = root / rel
        if not path.is_file():
            errors.append(f"missing file: {rel}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            errors.append(f"hash mismatch: {rel}")
    return errors


def hash_mismatches(
    manifest: PayloadManifest, installed_root: Path,
) -> list[str]:
    mismatched: list[str] = []
    for rel, expected in manifest.files.items():
        if rel in _INSTALL_MUTABLE_PATHS:
            continue
        path = installed_root / rel
        if not path.is_file():
            mismatched.append(rel)
            continue
        if sha256_file(path) != expected:
            mismatched.append(rel)
    return mismatched


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
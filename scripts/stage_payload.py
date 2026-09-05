#!/usr/bin/env python3
"""Stage the Minni plugin payload into src/minni/plugin_payload/ for wheel shipping.

Release-time only: runs npm build + esbuild bundle (via Makefile), copies the §4.1
file set, stamps versions, hashes files, and fails hard on glob-coverage gaps.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PLUGIN_DIR = REPO_ROOT / "plugins" / "minni"
PAYLOAD_ROOT = REPO_ROOT / "src" / "minni" / "plugin_payload"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# §4.1 payload dirs (npm files allowlist minus dev exclusions).
PAYLOAD_ITEMS = (
    "dist",
    ".claude-plugin",
    ".codex-plugin",
    ".cursor-plugin",
    ".gemini-plugin",
    ".kilocode-plugin",
    ".mcp.json",
    "commands",
    "hooks",
    "skills",
    "README.md",
    "frontend",
    "package.json",
)

JUNK_NAMES = {".DS_Store", "__pycache__", ".pytest_cache"}

MANIFEST_STAMP_PATHS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
    ".kilocode-plugin/plugin.json",
    ".gemini-plugin/gemini-extension.json",
)

# §4.2 step 5 — the package-data globs come from pyproject.toml itself (D9,
# #233): this file used to carry its own hand-maintained copy of the list,
# making a third manifest copy nothing cross-checked. setuptools reads
# pyproject, so pyproject is the only source of truth worth checking against.
def package_data_globs() -> list[str]:
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(
        r"\[tool\.setuptools\.package-data\]\s*\nminni\s*=\s*\[(.*?)\]",
        text,
        re.DOTALL,
    )
    if not block:
        raise SystemExit(f"Cannot find [tool.setuptools.package-data] in {PYPROJECT}")
    globs = [
        g for g in re.findall(r'"([^"]+)"', block.group(1))
        if g.startswith("plugin_payload/")
    ]
    if not globs:
        raise SystemExit(f"No plugin_payload/ package-data globs in {PYPROJECT}")
    return globs


def read_pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit(f"Cannot read version from {PYPROJECT}")
    return match.group(1)


def git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
        ).strip()
        return out or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def is_junk(path: Path) -> bool:
    return any(part in JUNK_NAMES for part in path.parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def stamp_version(path: Path, version: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _deterministic_built_at() -> str:
    # Lazy import: this module is also loaded by tests purely for its glob
    # helpers, and an import-time sys.path mutation would leak this checkout's
    # src/ into unrelated test paths (worktree import discipline, #258).
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from minni.wire.manifest import deterministic_built_at
    finally:
        sys.path.remove(str(REPO_ROOT / "src"))
    return deterministic_built_at(REPO_ROOT)


def copy_payload_tree(version: str) -> dict[str, str]:
    if PAYLOAD_ROOT.exists():
        shutil.rmtree(PAYLOAD_ROOT)
    PAYLOAD_ROOT.mkdir(parents=True)

    file_hashes: dict[str, str] = {}

    for item in PAYLOAD_ITEMS:
        src = PLUGIN_DIR / item
        if not src.exists():
            raise SystemExit(f"Missing payload source: {src}")
        dest = PAYLOAD_ROOT / item
        if src.is_dir():
            shutil.copytree(
                src, dest,
                ignore=shutil.ignore_patterns(*JUNK_NAMES),
            )
        else:
            shutil.copy2(src, dest)

    for rel in MANIFEST_STAMP_PATHS:
        stamp_version(PAYLOAD_ROOT / rel, version)

    for path in sorted(PAYLOAD_ROOT.rglob("*")):
        if path.is_file() and not is_junk(path):
            rel = path.relative_to(PAYLOAD_ROOT).as_posix()
            file_hashes[rel] = sha256_file(path)

    manifest = {
        "schema": 1,
        "version": version,
        "git_sha": git_sha(),
        "built_at": _deterministic_built_at(),
        "node_engine": ">=20",
        "files": file_hashes,
    }
    manifest_path = PAYLOAD_ROOT / "payload-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    file_hashes["payload-manifest.json"] = sha256_file(manifest_path)
    return file_hashes


def glob_matches(rel_path: str, pattern: str) -> bool:
    """Match a payload-relative path against a package-data glob line, with
    the semantics setuptools actually uses.

    setuptools expands package-data patterns with stdlib ``glob``, where a
    wildcard component never matches a name starting with ``.`` — a dotfile is
    only included when a pattern component names it (or starts with ``.``).
    This checker used to model ``*`` as matching dotfiles (D8, #233): a new
    payload dotfile passed the gate green while silently dropping from the
    built wheel. Component-wise match, no ``**`` support (none is used).
    """
    inner = pattern.removeprefix("plugin_payload/")
    names = rel_path.split("/")
    pats = inner.split("/")
    if len(names) != len(pats):
        return False
    for name, pat in zip(names, pats):
        if name.startswith(".") and not pat.startswith("."):
            return False
        if not fnmatch.fnmatchcase(name, pat):
            return False
    return True


def check_glob_coverage(file_hashes: dict[str, str]) -> None:
    globs = package_data_globs()
    unmatched: list[str] = []
    for rel in sorted(file_hashes):
        if not any(glob_matches(rel, g) for g in globs):
            unmatched.append(rel)
    if unmatched:
        print("stage-payload: files with no matching package-data glob:", file=sys.stderr)
        for rel in unmatched:
            print(f"  - {rel}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    version = read_pyproject_version()
    file_hashes = copy_payload_tree(version)
    check_glob_coverage(file_hashes)
    print(f"Staged plugin payload {version} → {PAYLOAD_ROOT} ({len(file_hashes)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
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
from datetime import UTC, datetime
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
    ".gemini-plugin",
    ".kilocode-plugin",
    ".mcp.json",
    "commands",
    "hooks",
    "skills",
    "README.md",
)

JUNK_NAMES = {".DS_Store", "__pycache__", ".pytest_cache"}

MANIFEST_STAMP_PATHS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".kilocode-plugin/plugin.json",
    ".gemini-plugin/gemini-extension.json",
)

# §4.2 step 5 — flat per-directory package-data globs (authoritative list).
PACKAGE_DATA_GLOBS = [
    "plugin_payload/*",
    "plugin_payload/dist/*",
    "plugin_payload/hooks/*",
    "plugin_payload/.claude-plugin/*",
    "plugin_payload/.codex-plugin/*",
    "plugin_payload/.gemini-plugin/*",
    "plugin_payload/.gemini-plugin/skills/sovereign-memory/*",
    "plugin_payload/.kilocode-plugin/*",
    "plugin_payload/.kilocode-plugin/commands/*",
    "plugin_payload/.kilocode-plugin/hooks/*",
    "plugin_payload/.kilocode-plugin/skills/sovereign-memory/*",
    "plugin_payload/commands/*",
    "plugin_payload/skills/minni/*",
    "plugin_payload/skills/minni-consolidation/*",
    "plugin_payload/skills/minni-consolidation/scripts/*",
    "plugin_payload/skills/minni-doctor/*",
    "plugin_payload/skills/minni-engine/*",
    "plugin_payload/skills/minni-health-check/*",
    "plugin_payload/skills/minni-ingestion/*",
    "plugin_payload/skills/minni-install/*",
    "plugin_payload/skills/minni-install/references/*",
    "plugin_payload/skills/minni-install/scripts/*",
]


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
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "node_engine": ">=20",
        "files": file_hashes,
    }
    manifest_path = PAYLOAD_ROOT / "payload-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    file_hashes["payload-manifest.json"] = sha256_file(manifest_path)
    return file_hashes


def glob_matches(rel_path: str, pattern: str) -> bool:
    """Match a payload-relative path against a package-data glob line."""
    inner = pattern.removeprefix("plugin_payload/")
    if inner == "*":
        return "/" not in rel_path
    if inner.endswith("/*"):
        dir_prefix = inner[:-2]
        if not dir_prefix:
            return "/" not in rel_path
        if not rel_path.startswith(dir_prefix + "/"):
            return False
        suffix = rel_path[len(dir_prefix) + 1:]
        return "/" not in suffix
    return fnmatch.fnmatch(rel_path, inner)


def check_glob_coverage(file_hashes: dict[str, str]) -> None:
    unmatched: list[str] = []
    for rel in sorted(file_hashes):
        if not any(glob_matches(rel, g) for g in PACKAGE_DATA_GLOBS):
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
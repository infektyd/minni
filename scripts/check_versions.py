#!/usr/bin/env python3
"""Assert version fields agree across pyproject, package.json, manifests, propagate.py."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_JSON = REPO_ROOT / "plugins" / "minni" / "package.json"
PROPAGATE = Path(
    os.environ.get("MINNI_CHECK_VERSIONS_PROPAGATE")
    or (
        REPO_ROOT / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
        / "propagate.py"
    ),
)

MANIFEST_PATHS = (
    REPO_ROOT / "plugins" / "minni" / ".claude-plugin" / "plugin.json",
    REPO_ROOT / "plugins" / "minni" / ".codex-plugin" / "plugin.json",
    REPO_ROOT / "plugins" / "minni" / ".kilocode-plugin" / "plugin.json",
    REPO_ROOT / "plugins" / "minni" / ".gemini-plugin" / "gemini-extension.json",
)

# Lines in propagate.py allowed to mention semver literals (comments, docs).
PROPAGATE_ALLOWLIST_PATTERNS = (
    re.compile(r"^\s*#"),
    re.compile(r'""".*"""'),
    re.compile(r"'''.*'''"),
)

VERSION_LITERAL = re.compile(r"\b0\.\d+\.\d+\b")


def read_pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit(f"Cannot read version from {PYPROJECT}")
    return match.group(1)


def read_json_version(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    if not isinstance(version, str):
        raise SystemExit(f"Missing version field in {path}")
    return version


def check_propagate_literals() -> list[str]:
    errors: list[str] = []
    for lineno, line in enumerate(
        PROPAGATE.read_text(encoding="utf-8").splitlines(), start=1,
    ):
        if not VERSION_LITERAL.search(line):
            continue
        if any(p.search(line) for p in PROPAGATE_ALLOWLIST_PATTERNS):
            continue
        errors.append(f"propagate.py:{lineno}: stale version literal: {line.strip()}")
    return errors


def main() -> int:
    canonical = read_pyproject_version()
    mismatches: list[str] = []

    pkg_ver = json.loads(PACKAGE_JSON.read_text(encoding="utf-8")).get("version")
    if pkg_ver != canonical:
        mismatches.append(f"package.json version {pkg_ver!r} != {canonical!r}")

    for path in MANIFEST_PATHS:
        ver = read_json_version(path)
        if ver != canonical:
            mismatches.append(f"{path.relative_to(REPO_ROOT)} version {ver!r} != {canonical!r}")

    literal_errors = check_propagate_literals()

    if mismatches:
        print("check-versions: version mismatches:", file=sys.stderr)
        for msg in mismatches:
            print(f"  - {msg}", file=sys.stderr)
    if literal_errors:
        print("check-versions: stale literals in propagate.py:", file=sys.stderr)
        for msg in literal_errors:
            print(f"  - {msg}", file=sys.stderr)

    if mismatches or literal_errors:
        return 1
    print(f"check-versions: all versions agree at {canonical}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
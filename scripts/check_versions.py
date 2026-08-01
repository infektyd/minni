#!/usr/bin/env python3
"""Assert the Minni version agrees everywhere it is written down.

Three layers, because a version can disagree with itself in three places and
only one of them used to be checked:

  repo        pyproject (canonical), package.json, the five plugin manifests,
              .claude-plugin/marketplace.json, and version literals in
              propagate.py.
  installed   the pip metadata for the `minni` distribution on this machine.
  deployed    the plugin manifests inside every deployment under $HOME.

Until 2026-08-01 only the repo layer existed, and even it omitted
marketplace.json. The result was a three-way split -- marketplace.json at 0.1.0,
every deployed manifest at 0.3.0, the repo at 0.4.1 -- that this script reported
as "all versions agree at 0.4.1". A checker that cannot see the thing it exists
to catch is worse than no checker, because it is believed.

    python3 scripts/check_versions.py              # all three layers
    python3 scripts/check_versions.py --repo-only  # repo layer only (CI/commit gate)

Anything this script cannot inspect -- unreadable metadata, an unparseable
manifest -- is reported as UNINSPECTABLE and fails. Absence is different from
drift and is reported as such: no deployments and no installed distribution are
both fine, silently skipping an unreadable one is not.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_JSON = REPO_ROOT / "plugins" / "minni" / "package.json"
MARKETPLACE = Path(
    os.environ.get("MINNI_CHECK_VERSIONS_MARKETPLACE")
    or (REPO_ROOT / ".claude-plugin" / "marketplace.json")
)
PROPAGATE = Path(
    os.environ.get("MINNI_CHECK_VERSIONS_PROPAGATE")
    or (
        REPO_ROOT / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
        / "propagate.py"
    ),
)

PLUGIN_ROOT = REPO_ROOT / "plugins" / "minni"

# Relative to a plugin root (the repo's or a deployment's).
MANIFEST_RELPATHS = (
    Path(".claude-plugin") / "plugin.json",
    Path(".codex-plugin") / "plugin.json",
    Path(".cursor-plugin") / "plugin.json",
    Path(".kilocode-plugin") / "plugin.json",
    Path(".gemini-plugin") / "gemini-extension.json",
)

MANIFEST_PATHS = tuple(PLUGIN_ROOT / rel for rel in MANIFEST_RELPATHS)

# Lines in propagate.py allowed to mention semver literals (comments, docs).
PROPAGATE_ALLOWLIST_PATTERNS = (
    re.compile(r"^\s*#"),
    re.compile(r'""".*"""'),
    re.compile(r"'''.*'''"),
)

VERSION_LITERAL = re.compile(r"\b0\.\d+\.\d+\b")


def _home() -> Path:
    """$HOME, overridable so the deployed layer is testable without one."""
    override = os.environ.get("MINNI_CHECK_VERSIONS_HOME")
    return Path(override) if override else Path.home()


def deployment_roots() -> list[Path]:
    """Deployment roots, derived from check_deployments.py's globs.

    Imported rather than restated so the two checks can never disagree about
    which trees exist: adding a tree there adds it here.
    """
    spec = importlib.util.spec_from_file_location(
        "_minni_check_deployments", Path(__file__).resolve().parent / "check_deployments.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise SystemExit("check-versions: cannot load scripts/check_deployments.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    home = _home()
    roots: list[Path] = []
    for pattern in module.DEPLOYMENT_GLOBS:
        # The globs address dist/; the manifests live one level up.
        root_pattern = pattern[: -len("/dist")] if pattern.endswith("/dist") else pattern
        for path in sorted(home.glob(root_pattern)):
            if path.is_dir():
                roots.append(path)
    return roots


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


def read_marketplace_version(path: Path) -> str:
    """marketplace.json nests the version under plugins[].version.

    It needs its own reader, which is exactly why it was omitted from the flat
    manifest list and drifted three minor versions behind unnoticed.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    plugins = data.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise SystemExit(f"{path}: no plugins[] entries to read a version from")
    entry = next((p for p in plugins if isinstance(p, dict) and p.get("name") == "minni"), None)
    if entry is None:
        raise SystemExit(f"{path}: no plugins[] entry named 'minni'")
    version = entry.get("version")
    if not isinstance(version, str):
        raise SystemExit(f"{path}: plugins[minni] has no version field")
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


def check_repo(canonical: str) -> list[str]:
    mismatches: list[str] = []

    pkg_ver = json.loads(PACKAGE_JSON.read_text(encoding="utf-8")).get("version")
    if pkg_ver != canonical:
        mismatches.append(f"package.json version {pkg_ver!r} != {canonical!r}")

    for path in MANIFEST_PATHS:
        ver = read_json_version(path)
        if ver != canonical:
            mismatches.append(f"{path.relative_to(REPO_ROOT)} version {ver!r} != {canonical!r}")

    market_ver = read_marketplace_version(MARKETPLACE)
    if market_ver != canonical:
        try:
            label = str(MARKETPLACE.relative_to(REPO_ROOT))
        except ValueError:  # overridden for tests
            label = str(MARKETPLACE)
        mismatches.append(
            f"{label} plugins[minni].version {market_ver!r} != {canonical!r}"
        )

    return mismatches


def check_installed(canonical: str) -> tuple[list[str], list[str]]:
    """(mismatches, notes). An unreadable distribution is a mismatch, not a note."""
    import importlib.metadata as md

    try:
        installed = md.version("minni")
    except md.PackageNotFoundError:
        return [], ["installed: minni is not installed in this interpreter (nothing to drift)"]
    except Exception as exc:
        return (
            [f"installed: UNINSPECTABLE — cannot read pip metadata for 'minni': "
             f"{type(exc).__name__}: {exc}"],
            [],
        )
    if installed != canonical:
        return [f"installed: pip metadata for 'minni' is {installed!r} != {canonical!r}"], []
    return [], [f"installed: minni {installed} (agrees)"]


def check_deployed(canonical: str) -> tuple[list[str], list[str]]:
    """(mismatches, notes). Every deployed plugin manifest under $HOME."""
    mismatches: list[str] = []
    notes: list[str] = []
    home = _home()
    roots = deployment_roots()
    if not roots:
        return [], ["deployed: no deployments discovered under $HOME"]
    for root in roots:
        label = str(root).replace(str(home), "~")
        versions: dict[str, list[str]] = {}
        for rel in MANIFEST_RELPATHS:
            path = root / rel
            if not path.is_file():
                continue
            try:
                ver = json.loads(path.read_text(encoding="utf-8")).get("version")
            except Exception as exc:
                mismatches.append(
                    f"deployed: UNINSPECTABLE — {label}/{rel}: {type(exc).__name__}: {exc}"
                )
                continue
            versions.setdefault(str(ver), []).append(str(rel))
        if not versions:
            mismatches.append(
                f"deployed: UNINSPECTABLE — {label} carries no readable plugin manifest; "
                "its version cannot be established"
            )
            continue
        # One line per deployment when its manifests agree with each other; the
        # per-file breakdown only appears when they do not.
        off = {v: files for v, files in versions.items() if v != canonical}
        if not off:
            notes.append(f"deployed: {label} at {canonical} (agrees)")
        elif len(versions) == 1:
            mismatches.append(f"deployed: {label} at {next(iter(versions))!r} != {canonical!r}")
        else:
            for ver, files in sorted(off.items()):
                mismatches.append(
                    f"deployed: {label} at {ver!r} != {canonical!r} in {', '.join(sorted(files))}"
                )
    return mismatches, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--repo-only",
        action="store_true",
        help="check only files tracked in this repo; skip installed/deployed state",
    )
    args = ap.parse_args(argv)

    canonical = read_pyproject_version()
    mismatches = check_repo(canonical)
    notes: list[str] = []

    if args.repo_only:
        notes.append("installed/deployed layers skipped (--repo-only)")
    else:
        for found, extra in (check_installed(canonical), check_deployed(canonical)):
            mismatches.extend(found)
            notes.extend(extra)

    literal_errors = check_propagate_literals()

    for note in notes:
        print(f"check-versions: {note}")

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
    scope = "repo" if args.repo_only else "repo, installed and deployed"
    print(f"check-versions: all versions agree at {canonical} ({scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

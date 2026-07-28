#!/usr/bin/env python3
"""Report which deployed Minni plugin builds are stale.

The plugin build is copied into every agent runtime that uses it. A copy that
drifts behind source does not fail -- it just quietly runs old behavior, and
nothing in the system reveals it. Two instances found 2026-07-27: four runtime
installs two days behind a committed fix (so the fix was live nowhere), and the
wheel payload three weeks behind.

Reads dist/build-manifest.json (written by plugins/minni/scripts/
emit_build_manifest.mjs) from each known deployment and compares its git_sha
against source HEAD.

    python3 scripts/check_deployments.py           # report
    python3 scripts/check_deployments.py --strict  # exit 1 if any is stale

A deployment with no manifest predates this check. That is reported as UNKNOWN
rather than passed over: unknown vintage is the condition this exists to end.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIST = REPO_ROOT / "plugins" / "minni" / "dist"

# Deployment roots, as globs relative to $HOME. Each should resolve to a dist/.
DEPLOYMENT_GLOBS = [
    ".claude/plugins/cache/minni/minni/*/dist",
    ".codex/plugins/cache/minni/minni/*/dist",
    ".config/kilo/plugins/minni/dist",
    ".agents/plugins/*inni*/dist",
]
# Deployments inside the repo itself.
REPO_DEPLOYMENTS = ["src/minni/plugin_payload/dist"]


def source_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def discover() -> list[Path]:
    home = Path.home()
    found: list[Path] = []
    for pattern in DEPLOYMENT_GLOBS:
        found.extend(sorted(home.glob(pattern)))
    for rel in REPO_DEPLOYMENTS:
        p = REPO_ROOT / rel
        if p.is_dir():
            found.append(p)
    return found


def read_manifest(dist: Path) -> dict | None:
    f = dist / "build-manifest.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="exit 1 if any deployment is stale or unknown")
    args = ap.parse_args(argv)

    head = source_sha()
    print(f"source HEAD: {head[:8]}  ({REPO_ROOT})\n")

    rows: list[tuple[str, str, str]] = []
    stale = unknown = 0
    for dist in discover():
        label = str(dist).replace(str(Path.home()), "~")
        # A symlinked deployment cannot drift by construction; say so plainly
        # rather than reporting it as merely up to date.
        target = dist if not dist.is_symlink() else dist.resolve()
        if dist.is_symlink() and target == SOURCE_DIST.resolve():
            rows.append(("LINKED", "-> source", label))
            continue
        m = read_manifest(dist)
        if m is None:
            rows.append(("UNKNOWN", "no manifest", label))
            unknown += 1
            continue
        sha = str(m.get("git_sha", "unknown"))
        dirty = " +dirty" if m.get("git_dirty") else ""
        built = str(m.get("built_at", "?"))
        if sha == head:
            rows.append(("OK", f"{sha[:8]}{dirty}", label))
        else:
            rows.append(("STALE", f"{sha[:8]}{dirty} built {built}", label))
            stale += 1

    width = max((len(r[1]) for r in rows), default=0)
    for status, detail, label in rows:
        print(f"  {status:<8} {detail:<{width}}  {label}")

    print()
    if not rows:
        print("No deployments discovered.")
    else:
        print(f"{len(rows)} deployment(s): {stale} stale, {unknown} unknown vintage.")
    if stale or unknown:
        print("\nRefresh with: make stage-payload  (payload)  /  npm run build  (source dist)")
        print("Or symlink runtimes at the source dist so they cannot drift.")
    return 1 if args.strict and (stale or unknown) else 0


if __name__ == "__main__":
    sys.exit(main())

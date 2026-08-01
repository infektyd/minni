#!/usr/bin/env python3
"""Report which deployed Minni plugin builds are stale.

The plugin build is copied into every agent runtime that uses it. A copy that
drifts behind source does not fail -- it just quietly runs old behavior, and
nothing in the system reveals it. Two instances found 2026-07-27: four runtime
installs two days behind a committed fix (so the fix was live nowhere), and the
wheel payload three weeks behind.

Two things are checked, because a deployment is more than its dist/:

  dist/       compared by dist/build-manifest.json (written by
              plugins/minni/scripts/emit_build_manifest.mjs) against source HEAD.
  everything  hooks/, commands/, skills/ and the five plugin-manifest dirs,
  else        compared file-by-file against plugins/minni/.

That second half is new as of 2026-08-01 and it is the whole point. dist/ is the
only subtree anyone symlinks; hooks/, skills/, commands/ and the manifest dirs
are real copies in every deployment. This script used to see a symlinked dist/,
conclude the deployment "cannot drift by construction", and skip it -- while
four of those same deployments were running hooks.json files that differed from
source. LINKED is now a fact about dist/, not a verdict on the deployment.

    python3 scripts/check_deployments.py           # report
    python3 scripts/check_deployments.py --strict  # exit 1 if anything is off

A deployment with no manifest predates this check. That is reported as UNKNOWN
rather than passed over: unknown vintage is the condition this exists to end.
Likewise a file that cannot be read is reported as UNREADABLE and counts against
the deployment -- it is never assumed to match.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("MINNI_CHECK_DEPLOYMENTS_REPO_ROOT") or Path(__file__).resolve().parent.parent)
SOURCE_ROOT = REPO_ROOT / "plugins" / "minni"
SOURCE_DIST = SOURCE_ROOT / "dist"

# Deployment roots, as globs relative to $HOME. Each should resolve to a dist/.
# ~/.minni/plugin/*/dist is the wire-managed tree. It is what Claude Code now
# loads hooks, skills and commands from (installed_plugins.json points at it), so
# leaving it out would make the one deployment this check exists to watch the
# only one it could not see.
DEPLOYMENT_GLOBS = [
    ".minni/plugin/*/dist",
    ".claude/plugins/cache/minni/minni/*/dist",
    ".codex/plugins/cache/minni/minni/*/dist",
    ".config/kilo/plugins/minni/dist",
    ".agents/plugins/*inni*/dist",
]

# Cursor's local plugin install. Kept as a separate append rather than folded
# into the list above only to keep this file mergeable with in-flight work that
# is extending the same list. It is a full real copy that ships no
# build-manifest at all, so it reports UNKNOWN vintage -- which is the point:
# until 2026-08-01 it was not discovered here at all.
DEPLOYMENT_GLOBS.append(".cursor/plugins/local/minni/dist")

# Deployments inside the repo itself.
REPO_DEPLOYMENTS = ["src/minni/plugin_payload/dist"]

# Subtrees copied verbatim into every deployment. These are what the runtimes
# actually execute and read; none of them is ever symlinked.
COMPARED_SUBTREES = (
    "hooks",
    "commands",
    "skills",
    ".claude-plugin",
    ".codex-plugin",
    ".cursor-plugin",
    ".kilocode-plugin",
    ".gemini-plugin",
)

# Not compared, with the reason, so the gap is stated rather than silent:
#   .mcp.json    materialized per host (absolute paths, per-agent MINNI_AGENT_ID
#                and vault), so it is a template output, not a copy.
#   node_modules/frontend/src/tests/scripts  build inputs, not runtime surface.
NOT_COMPARED = {
    ".mcp.json": "host-materialized (absolute paths + per-agent env), not a copy",
}

# Editor/interpreter droppings. Excluded because they are not part of the
# plugin: they are generated on both sides independently and are gitignored.
IGNORED_NAMES = {".DS_Store", "__pycache__", ".pytest_cache", ".ruff_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}

# The version field is owned by scripts/check_versions.py, which compares it
# across repo, pip metadata and every deployment. Comparing it here too would
# report the same drift twice and, worse, drown real content drift in a manifest
# diff that is expected on any wire-installed tree (which stamps its own
# PEP440-local version).
#
# Scoped to the five plugin-manifest files check_versions.py actually reads --
# NOT every ".json" under the compared subtrees. hooks/hooks-cursor.json also
# has a top-level "version" field, but it is Cursor's *hooks schema* version,
# not plugin semver, and check_versions.py never reads it. Stripping it here
# too would re-blind exactly the file this whole slice exists to stop
# under-reporting: a schema-version-only change would digest identically and
# report no drift, with no other check catching it either.
VERSION_NORMALIZED_RELPATHS = {
    "plugin.json",
    "gemini-extension.json",
}


def source_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _home() -> Path:
    override = os.environ.get("MINNI_CHECK_DEPLOYMENTS_HOME")
    return Path(override) if override else Path.home()


def discover() -> list[Path]:
    home = _home()
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


def _ignored(rel: Path) -> bool:
    if any(part in IGNORED_NAMES for part in rel.parts):
        return True
    return rel.suffix in IGNORED_SUFFIXES


def _digest(path: Path, rel: Path) -> str:
    """Content hash, with the version field normalized out of plugin manifests.

    ``rel`` is the path within the compared subtree (e.g. ``plugin.json`` for
    ``.claude-plugin/plugin.json``), which is what scopes the strip to the
    manifest files by name -- not to every ``.json`` under the subtree, which
    would also strip hooks/hooks-cursor.json's unrelated schema-version field.
    """
    data = path.read_bytes()
    if rel.parent == Path(".") and rel.name in VERSION_NORMALIZED_RELPATHS:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        else:
            if isinstance(parsed, dict) and "version" in parsed:
                parsed = {k: v for k, v in parsed.items() if k != "version"}
                data = json.dumps(parsed, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _hash_tree(root: Path) -> tuple[dict[str, str], list[str]]:
    """(relpath -> digest, unreadable relpaths). Missing root yields ({}, [])."""
    digests: dict[str, str] = {}
    unreadable: list[str] = []
    if not root.is_dir():
        return digests, unreadable
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if _ignored(rel):
            continue
        if not path.is_file():
            continue
        try:
            digests[str(rel)] = _digest(path, rel)
        except OSError as exc:
            unreadable.append(f"{rel} ({type(exc).__name__})")
    return digests, unreadable


def hash_source() -> dict[str, tuple[dict[str, str], list[str]]]:
    """Hash every compared subtree under source once, shared across deployments."""
    return {sub: _hash_tree(SOURCE_ROOT / sub) for sub in COMPARED_SUBTREES}


def compare_content(
    deployment_root: Path, source: dict[str, tuple[dict[str, str], list[str]]]
) -> tuple[list[str], list[str]]:
    """(drifted 'subtree/relpath' entries, unreadable entries).

    Only files unreadable *on the deployment side* count against the
    deployment: a source file this tool cannot read is a defect in the tool or
    the checkout it is running from, not evidence that the deployment itself
    has drifted, and charging it to every deployment in the same run would
    make an unrelated deployment fail for a fault that is not its own. Source
    unreadability is surfaced once, separately, by the caller.
    """
    drifted: list[str] = []
    unreadable: list[str] = []
    for sub in COMPARED_SUBTREES:
        src, _src_bad = source[sub]
        dep, dep_bad = _hash_tree(deployment_root / sub)
        unreadable.extend(f"{sub}/{e}" for e in dep_bad)
        if not src and not dep:
            continue
        for rel in sorted(set(src) | set(dep)):
            if src.get(rel) != dep.get(rel):
                if rel not in dep:
                    drifted.append(f"{sub}/{rel} (missing)")
                elif rel not in src:
                    drifted.append(f"{sub}/{rel} (extra)")
                else:
                    drifted.append(f"{sub}/{rel}")
    return drifted, unreadable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="exit 1 if any deployment is stale, drifted or unknown")
    ap.add_argument("--show", type=int, default=6, help="max drifted files listed per deployment (0 = all)")
    args = ap.parse_args(argv)

    head = source_sha()
    print(f"source HEAD: {head[:8]}  ({REPO_ROOT})")
    print(f"content compared: {', '.join(COMPARED_SUBTREES)}")
    for name, why in NOT_COMPARED.items():
        print(f"not compared: {name} — {why}")
    print(
        "version field normalized out of: "
        + ", ".join(sorted(VERSION_NORMALIZED_RELPATHS))
        + " — owned by scripts/check_versions.py"
    )
    print()

    source = hash_source()
    source_bad = [
        f"{sub}/{e}" for sub, (_digests, bad) in source.items() for e in bad
    ]
    if source_bad:
        print("SOURCE UNREADABLE (tool/checkout defect, not a deployment fault):")
        for e in source_bad:
            print(f"      {e}")
        print()

    home = _home()
    rows: list[tuple[str, str, str]] = []
    details: list[tuple[str, list[str]]] = []
    stale = unknown = drifted_count = unreadable_count = 0
    for dist in discover():
        root = dist.parent
        label = str(root).replace(str(home), "~")

        # dist/ vintage. A symlink at the source dist genuinely cannot drift --
        # but that is a fact about dist/ only, and no longer skips the rest.
        if dist.is_symlink() and dist.resolve() == SOURCE_DIST.resolve():
            rows.append(("LINKED", "dist -> source", label))
        else:
            m = read_manifest(dist)
            if m is None:
                rows.append(("UNKNOWN", "dist: no manifest", label))
                unknown += 1
            else:
                sha = str(m.get("git_sha", "unknown"))
                dirty = " +dirty" if m.get("git_dirty") else ""
                built = str(m.get("built_at", "?"))
                if sha == head:
                    rows.append(("OK", f"dist {sha[:8]}{dirty}", label))
                else:
                    rows.append(("STALE", f"dist {sha[:8]}{dirty} built {built}", label))
                    stale += 1

        drift, bad = compare_content(root, source)
        if bad:
            unreadable_count += 1
            rows.append(("UNREADABLE", f"{len(bad)} file(s) unreadable", label))
            details.append((f"{label} (unreadable)", bad))
        if drift:
            drifted_count += 1
            rows.append(("DRIFT", f"{len(drift)} file(s) differ", label))
            details.append((label, drift))

    width = max((len(r[1]) for r in rows), default=0)
    for status, detail, label in rows:
        print(f"  {status:<10} {detail:<{width}}  {label}")

    if details:
        print()
        for label, files in details:
            shown = files if args.show <= 0 else files[: args.show]
            print(f"  {label}:")
            for f in shown:
                print(f"      {f}")
            if len(files) > len(shown):
                print(f"      ... and {len(files) - len(shown)} more (--show 0 for all)")

    print()
    roots = len({r[2] for r in rows})
    if not rows:
        print("No deployments discovered.")
    else:
        print(
            f"{roots} deployment(s): {stale} stale dist, {drifted_count} with content drift, "
            f"{unknown} unknown vintage, {unreadable_count} partly unreadable."
        )
    failed = stale or unknown or drifted_count or unreadable_count or source_bad
    if failed:
        print("\nRefresh with: make stage-payload  (payload)  /  npm run build  (source dist)")
        print("Content drift is a propagation problem, not a build one: re-run the installer")
        print("(minni wire / propagate.py) so hooks, skills, commands and manifests are recopied.")
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    sys.exit(main())

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


def _installed_version() -> str:
    """The 'minni' distribution's pip-reported version.

    Test-only override: MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE stands in for
    the real pip metadata lookup, the same way MARKETPLACE/PROPAGATE are
    overridable. This exists so tests exercising the *deployed* layer's
    pass/fail behavior can hold the installed layer at a known value instead
    of being coupled to whatever `minni` version happens to be on the test
    interpreter's sys.path -- and so the installed layer's own mismatch/agree
    logic has direct test coverage rather than none at all.
    """
    override = os.environ.get("MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE")
    if override is not None:
        return override
    import importlib.metadata as md

    return md.version("minni")


def check_installed(canonical: str) -> tuple[list[str], list[str]]:
    """(mismatches, notes). An unreadable distribution is a mismatch, not a note."""
    import importlib.metadata as md

    try:
        installed = _installed_version()
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


def _public_version(version: str) -> str:
    """Strip PEP 440 local segment (+…) so from-repo stamps agree with pyproject.

    ``minni wire --from-repo`` / ``make sync-root`` write
    ``0.4.1+git.<short>`` into every plugin manifest (see
    ``wire.from_repo.dev_version``). Exact string equality against the public
    pyproject version then makes a successful redeploy fail its own verify
    step. Public/base agreement still catches real drift (``0.3.0`` vs
    ``0.4.1``). Falls back to a cheap split when packaging is unavailable.
    """
    text = str(version or "").strip()
    if not text:
        return text
    try:
        from packaging.version import Version

        return Version(text).base_version
    except Exception:
        return text.split("+", 1)[0].split("-", 1)[0]


def _version_agrees(deployed: str, canonical: str) -> bool:
    if deployed == canonical:
        return True
    return bool(deployed) and _public_version(deployed) == _public_version(canonical)


def _active_wire_plugin_state(home: Path) -> tuple[set[Path], set[str]]:
    """Live wire install roots + platforms under ~/.minni/plugin.

    Shared with check_deployments + deploy honesty (``minni.wire.active_roots``):
    latest ``wired_at`` per platform; root must be a dir with
    ``payload-manifest.json``; marketplace-cache skip is per-surface.
    """
    _src = Path(__file__).resolve().parent.parent / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
    from minni.wire.active_roots import active_wire_plugin_state

    return active_wire_plugin_state(home)


def _active_wire_plugin_roots(home: Path) -> set[Path]:
    """Live wire install roots (latest per platform)."""
    roots, _platforms = _active_wire_plugin_state(home)
    return roots


def _is_versioned_wire_plugin_root(root: Path, home: Path) -> bool:
    """True for ~/.minni/plugin/<version> (not the plugin base itself)."""
    try:
        rel = root.resolve().relative_to((home / ".minni" / "plugin").resolve())
    except (OSError, ValueError):
        return False
    return len(rel.parts) == 1 and rel.parts[0] not in {"current", "cache"}


def _marketplace_cache_platform(root: Path, home: Path) -> str | None:
    """Platform that owns this wire-superseded legacy root, or None.

    Parity with scripts/check_deployments._marketplace_cache_platform.
    """
    try:
        rel = str(root.resolve().relative_to(home.resolve()))
    except (OSError, ValueError):
        rel = str(root)
    if rel.startswith(".claude/plugins/cache/"):
        return "claude-code"
    if rel.startswith(".codex/plugins/cache/"):
        return "codex"
    # Pre-wire Kilo install root; wire-primary points kilo.json at ~/.minni/plugin.
    if rel.startswith(".config/kilo/plugins/"):
        return "kilocode"
    return None


def _is_legacy_marketplace_cache_root(root: Path, home: Path) -> bool:
    """Claude/Codex marketplace cache trees (path shape only)."""
    return _marketplace_cache_platform(root, home) is not None


def check_deployed(canonical: str) -> tuple[list[str], list[str]]:
    """(mismatches, notes). Every deployed plugin manifest under $HOME."""
    mismatches: list[str] = []
    notes: list[str] = []
    home = _home()
    roots = deployment_roots()
    if not roots:
        return [], ["deployed: no deployments discovered under $HOME"]
    active_wire, active_platforms = _active_wire_plugin_state(home)
    for root in roots:
        label = str(root).replace(str(home), "~")
        # Inactive historical wire version dirs: note + skip, do not fail.
        # Empty active (post-retire wires:[]) must skip *all* such trees.
        if (
            _is_versioned_wire_plugin_root(root, home)
            and root.resolve() not in active_wire
        ):
            notes.append(
                f"deployed: {label} skipped (not an active wire install; "
                "historical version dir left by non-interactive wire)"
            )
            continue
        # Release-era plugin/current: always skip the logical path (parity
        # with check_deployments); live payload is the versioned active root.
        if root.name == "current":
            try:
                if root.parent.resolve() == (home / ".minni" / "plugin").resolve():
                    notes.append(
                        f"deployed: {label} skipped (legacy plugin/current; "
                        "wire-primary — not managed by sync-root)"
                    )
                    continue
            except OSError:
                pass
        # Per-platform marketplace skip (parity with check_deployments).
        cache_plat = _marketplace_cache_platform(root, home)
        if cache_plat is not None and cache_plat in active_platforms:
            notes.append(
                f"deployed: {label} skipped (legacy marketplace cache for "
                f"{cache_plat}; wire-primary — not managed by sync-root)"
            )
            continue
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
        # Root-vs-hidden divergence for Gemini (#359): the root-level generated
        # manifest (gemini-extension.json) and the dotdir manifest
        # (.gemini-plugin/gemini-extension.json) must agree with each other.
        # Check this explicitly so the report says divergence rather than a
        # plain version mismatch.
        _gemini_hidden_rel = Path(".gemini-plugin") / "gemini-extension.json"
        _gemini_root_path = root / "gemini-extension.json"
        _gemini_hidden_path = root / _gemini_hidden_rel
        _root_gem_ver: str | None = None
        _hidden_gem_ver: str | None = None
        if _gemini_root_path.is_file() and _gemini_hidden_path.is_file():
            try:
                _root_gem_ver = json.loads(_gemini_root_path.read_text(encoding="utf-8")).get(
                    "version"
                )
                _root_gem_ver = str(_root_gem_ver) if _root_gem_ver is not None else None
            except Exception:
                _root_gem_ver = None
            try:
                _hidden_gem_ver = json.loads(
                    _gemini_hidden_path.read_text(encoding="utf-8")
                ).get("version")
                _hidden_gem_ver = str(_hidden_gem_ver) if _hidden_gem_ver is not None else None
            except Exception:
                _hidden_gem_ver = None
            if (
                _root_gem_ver is not None
                and _hidden_gem_ver is not None
                and not _version_agrees(_root_gem_ver, _hidden_gem_ver)
            ):
                mismatches.append(
                    f"deployed: {label} root-vs-hidden divergence: "
                    f"gemini-extension.json at {_root_gem_ver!r} != "
                    f"{_gemini_hidden_rel} at {_hidden_gem_ver!r} "
                    f"(canonical {canonical!r})"
                )
                # No continue: the divergence line is additive, so drift in the
                # root's OTHER manifests still reports in the same run.
        if not versions:
            mismatches.append(
                f"deployed: UNINSPECTABLE — {label} carries no readable plugin manifest; "
                "its version cannot be established"
            )
            continue
        # Public/base agreement: from-repo local stamps (+git.<sha>) match the
        # pyproject public version; real public drift still fails.
        off = {
            v: files for v, files in versions.items() if not _version_agrees(v, canonical)
        }
        if not off:
            shown = next(iter(versions))
            if shown == canonical:
                notes.append(f"deployed: {label} at {canonical} (agrees)")
            else:
                notes.append(
                    f"deployed: {label} at {shown!r} (public version agrees with {canonical})"
                )
        elif len(versions) == 1:
            ver = next(iter(versions))
            files = sorted(versions[ver])
            mismatches.append(
                f"deployed: {label} at {ver!r} != {canonical!r} in {', '.join(files)}"
            )
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

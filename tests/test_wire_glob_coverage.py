"""§9.3: every npm-files payload file maps to a pyproject package-data glob.

The matcher and the glob list are IMPORTED from scripts/stage_payload.py, not
restated here (D8/D9, #233): this test used to carry its own copy of both, and
both copies modeled `*` as matching dotfiles — which setuptools' stdlib-glob
expansion does not — so a payload dotfile could pass this gate and still drop
silently from the built wheel.
"""

from __future__ import annotations

import glob as stdlib_glob
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE_JSON = REPO / "plugins" / "minni" / "package.json"
PLUGIN_ROOT = REPO / "plugins" / "minni"

EXCLUDED = {
    "frontend-src", "src", "tests", "node_modules",
    "package.json", "package-lock.json",
}
JUNK = {".DS_Store", "__pycache__", ".pytest_cache"}


def _load_stage_payload():
    spec = importlib.util.spec_from_file_location(
        "_minni_stage_payload", REPO / "scripts" / "stage_payload.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_stage = _load_stage_payload()
_glob_matches = _stage.glob_matches
_package_data_globs = _stage.package_data_globs


def _payload_files() -> list[str]:
    allowlist = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["files"]
    paths: list[str] = []
    for item in allowlist:
        if item in EXCLUDED:
            continue
        src = PLUGIN_ROOT / item
        if not src.exists():
            continue
        if src.is_file():
            paths.append(item)
            continue
        for f in src.rglob("*"):
            if f.is_file() and not any(p in JUNK for p in f.parts):
                paths.append(f.relative_to(PLUGIN_ROOT).as_posix())
    return sorted(set(paths))


def test_payload_files_map_to_package_data_globs():
    globs = _package_data_globs()
    uncovered = [
        rel for rel in _payload_files()
        if not any(_glob_matches(rel, g) for g in globs)
    ]
    assert not uncovered, "uncovered:\n" + "\n".join(uncovered[:30])


def test_glob_matcher_agrees_with_stdlib_glob(tmp_path):
    """D8 (#233): the checker's matcher must agree with stdlib glob — which is
    what setuptools uses to expand package-data — on a tree that includes
    dotfiles. The old matcher said `*` covered `.hidden.json`; glob does not,
    so the file dropped from the wheel while the gate stayed green.
    """
    root = tmp_path / "plugin_payload"
    files = [
        "README.md",
        ".mcp.json",
        "hooks/hooks.json",
        "hooks/.hidden.json",
        "skills/minni/SKILL.md",
        ".kilocode-plugin/.mcp.json",
        ".kilocode-plugin/plugin.json",
    ]
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    patterns = [
        "plugin_payload/*",
        "plugin_payload/.mcp.json",
        "plugin_payload/hooks/*",
        "plugin_payload/skills/minni/*",
        "plugin_payload/.kilocode-plugin/*",
        "plugin_payload/.kilocode-plugin/.mcp.json",
    ]
    for pattern in patterns:
        expanded = {
            Path(m).relative_to(root).as_posix()
            for m in stdlib_glob.glob(str(tmp_path / pattern))
            if Path(m).is_file()
        }
        matched = {rel for rel in files if _glob_matches(rel, pattern)}
        assert matched == expanded, (
            f"matcher disagrees with stdlib glob for {pattern!r}: "
            f"matcher={sorted(matched)} glob={sorted(expanded)}"
        )


def test_new_payload_dotfile_without_explicit_glob_is_flagged():
    """The regression #233 describes: a NEW dotfile with no explicit package-
    data entry must be reported uncovered, not waved through by `*`."""
    globs = _package_data_globs()
    assert not any(
        _glob_matches("hooks/.brand-new.json", g) for g in globs
    ), "a dotfile with no explicit package-data entry must not be covered by *"
    # And the explicit dotfile entries that DO exist are honored.
    assert any(_glob_matches(".mcp.json", g) for g in globs)
    assert any(_glob_matches(".kilocode-plugin/.mcp.json", g) for g in globs)


def test_stage_payload_items_match_from_repo_items():
    """D9 (#233): stage_payload and wire's --from-repo builder each list the
    §4.1 payload items; drift between them ships different payloads depending
    on the install path. Pin them equal."""
    from minni.wire.from_repo import MANIFEST_STAMP_PATHS, PAYLOAD_ITEMS

    assert tuple(_stage.PAYLOAD_ITEMS) == tuple(PAYLOAD_ITEMS)
    assert tuple(_stage.MANIFEST_STAMP_PATHS) == tuple(MANIFEST_STAMP_PATHS)

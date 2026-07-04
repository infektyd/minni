"""§9.3: every npm-files payload file maps to a pyproject package-data glob."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE_JSON = REPO / "plugins" / "minni" / "package.json"
PYPROJECT = REPO / "pyproject.toml"
PLUGIN_ROOT = REPO / "plugins" / "minni"

EXCLUDED = {
    "frontend-src", "src", "tests", "node_modules",
    "package.json", "package-lock.json",
}
JUNK = {".DS_Store", "__pycache__", ".pytest_cache"}


def _package_data_globs() -> list[str]:
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(
        r"\[tool\.setuptools\.package-data\]\s*\nminni\s*=\s*\[(.*?)\]",
        text,
        re.DOTALL,
    )
    assert block
    return [g for g in re.findall(r'"([^"]+)"', block.group(1)) if g.startswith("plugin_payload/")]


def _glob_matches(rel: str, pattern: str) -> bool:
    inner = pattern.removeprefix("plugin_payload/")
    if inner == "*":
        return "/" not in rel
    if inner.endswith("/*"):
        prefix = inner[:-2]
        if not prefix:
            return "/" not in rel
        if not rel.startswith(prefix + "/"):
            return False
        suffix = rel[len(prefix) + 1:]
        return "/" not in suffix
    return False


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
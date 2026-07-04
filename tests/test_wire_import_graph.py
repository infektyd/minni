"""§9.1: hooks/cli must not import external packages (except server.ts)."""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_SRC = Path(__file__).resolve().parent.parent / "plugins" / "minni" / "src"
IMPORT_RE = re.compile(
    r"""^\s*import\s+(?:type\s+)?(?:[\w*{}\s,]+\s+from\s+)?['"]([^'"]+)['"]""",
)
EXTERNAL_IMPORT = re.compile(r"^[^./]")


def test_hook_cli_import_graph_is_dependency_free():
    violations: list[str] = []
    for path in sorted(PLUGIN_SRC.rglob("*.ts")):
        if path.name == "server.ts":
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1,
        ):
            match = IMPORT_RE.match(line)
            if not match:
                continue
            specifier = match.group(1)
            if specifier.startswith("node:"):
                continue
            if EXTERNAL_IMPORT.match(specifier):
                violations.append(f"{path.relative_to(PLUGIN_SRC)}:{lineno}: {specifier}")
    assert not violations, "external imports found:\n" + "\n".join(violations)
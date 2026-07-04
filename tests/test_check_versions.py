"""§8 / §9.6: check-versions script passes and catches stale literals."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_versions.py"


def test_check_versions_passes_on_current_tree():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_check_versions_fails_on_injected_literal(tmp_path):
    src = REPO / "plugins" / "minni" / "skills" / "minni-install" / "scripts" / "propagate.py"
    copy = tmp_path / "propagate.py"
    text = src.read_text(encoding="utf-8")
    text += '\nSTALE = "~/.minni/plugin/0.9.9/dist/cli.js"\n'
    copy.write_text(text, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO,
        env={**os.environ, "MINNI_CHECK_VERSIONS_PROPAGATE": str(copy)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "0.9.9" in (proc.stdout + proc.stderr)
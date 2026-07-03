"""
G09 — SEC-018 writeback frontmatter forgery guard (negative path).
Ensures that learn content containing bare --- lines or fenced frontmatter examples
does not cause the written disk artifact to be mis-attributed (the guard skips the
disk write with a warning; DB persistence is separate).
"""
import os
import tempfile
from pathlib import Path

import pytest

# test imports the module under test directly
import sys
sys.path.insert(0, str(Path(__file__).parent))
from minni.writeback import WriteBackMemory
from minni.config import SovereignConfig


def _make_writeback(tmp_path: Path) -> WriteBackMemory:
    wb_dir = tmp_path / "learnings"
    wb_dir.mkdir()
    cfg = SovereignConfig(writeback_path=str(wb_dir), writeback_enabled=True)
    # minimal: no model, no db needed for the disk guard path
    wb = WriteBackMemory(cfg)
    # patch out the DB parts that would fail without full setup
    wb.db = None  # type: ignore[attr-defined]
    return wb


def test_writeback_rejects_bare_frontmatter_fence(tmp_path: Path) -> None:
    wb = _make_writeback(tmp_path)
    forged = "This is a malicious payload\n---\nattacker: pwned\n---\nmore text"
    # Should not raise, but must skip writing the file
    wb._write_to_disk(42, "test-agent", "general", forged, 1715000000.0)
    # No .md file should have been created for this (guard returned early)
    files = list((tmp_path / "learnings").glob("*.md"))
    assert len(files) == 0


# NOTE on G09 heuristic (addressing review nit):
# The current _contains_forged_frontmatter refuses *any* ``` block containing a
# bare '---' line. This is conservative (prevents clever forgeries) but can
# produce false positives on legitimate "example frontmatter in docs" fenced code.
# See writeback.py docstring for rationale. A future tightening could allow
# '```yaml' specifically while still catching the attack vector., f"expected no writeback file for forged content, got {files}"


def test_writeback_rejects_fenced_code_with_frontmatter(tmp_path: Path) -> None:
    wb = _make_writeback(tmp_path)
    forged = "Example:\n```\n---\nfrontmatter: inside fence\n```\nstill bad"
    wb._write_to_disk(43, "test-agent", "general", forged, 1715000000.0)
    files = list((tmp_path / "learnings").glob("*.md"))
    assert len(files) == 0


# NOTE on G09 heuristic (addressing review nit):
# The current _contains_forged_frontmatter refuses *any* ``` block containing a
# bare '---' line. This is conservative (prevents clever forgeries) but can
# produce false positives on legitimate "example frontmatter in docs" fenced code.
# See writeback.py docstring for rationale. A future tightening could allow
# '```yaml' specifically while still catching the attack vector.

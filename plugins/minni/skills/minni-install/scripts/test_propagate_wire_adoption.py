"""propagate's side of the wire-pipeline adoption.

Two behaviors are pinned here. claude-code must fail loudly rather than write to
a tree Claude Code no longer reads, and every remaining install dir must be
resolved from directories that actually exist -- the old resolver could name a
version from pip metadata that was never installed, or substitute a literal
`current` path segment that exists nowhere under the codex cache.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402


# --- version resolution -----------------------------------------------------


def test_max_present_version_compares_numerically_not_lexically(tmp_path):
    """A two-digit minor outranks a one-digit one; lexical sorting gets this wrong."""
    for name in ("0.2.0", "0.10.0", "0.9.0"):
        (tmp_path / name).mkdir()

    assert propagate.max_present_version(tmp_path) == "0.10.0"


def test_max_present_version_takes_the_newest_not_the_oldest(tmp_path):
    for name in ("0.3.0", "0.4.0"):
        (tmp_path / name).mkdir()

    assert propagate.max_present_version(tmp_path) == "0.4.0"


def test_max_present_version_sorts_local_segments_after_the_release(tmp_path):
    (tmp_path / "0.4.0").mkdir()
    (tmp_path / "0.4.0+git.abc1234").mkdir()

    assert propagate.max_present_version(tmp_path) == "0.4.0+git.abc1234"


def test_max_present_version_ignores_non_version_dirs(tmp_path):
    (tmp_path / "0.4.0").mkdir()
    (tmp_path / "current").mkdir()
    (tmp_path / ".staging-x").mkdir()

    assert propagate.max_present_version(tmp_path) == "0.4.0"


def test_max_present_version_is_none_for_a_missing_base(tmp_path):
    assert propagate.max_present_version(tmp_path / "nope") is None


def test_plugin_version_segment_prefers_on_disk_over_pip_metadata(tmp_path, monkeypatch):
    """No `current` on a dev machine must not mean "guess from the wheel"."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".minni" / "plugin" / "0.4.0+git.abc1234").mkdir(parents=True)

    assert propagate.plugin_version_segment() == "0.4.0+git.abc1234"


def test_plugin_version_segment_prefers_the_current_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    base = tmp_path / ".minni" / "plugin"
    (base / "0.4.0+git.abc1234").mkdir(parents=True)
    (base / "0.3.0").mkdir()
    (base / "current").symlink_to("0.3.0")

    assert propagate.plugin_version_segment() == "0.3.0"


# --- codex install root -----------------------------------------------------


def test_codex_install_root_resolves_against_codex_own_tree(tmp_path, monkeypatch):
    """Codex must not inherit a version segment from the wire tree."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".minni" / "plugin" / "0.4.0").mkdir(parents=True)
    codex = tmp_path / ".codex" / "plugins" / "cache" / "minni" / "minni" / "0.3.0"
    codex.mkdir(parents=True)

    assert propagate.codex_install_root() == codex


def test_codex_install_root_never_yields_a_literal_current_segment(tmp_path, monkeypatch):
    """`current` is not a directory the codex cache ever has."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".minni" / "plugin" / "0.4.0").mkdir(parents=True)

    assert propagate.codex_install_root().name != "current"


def test_codex_platform_spec_uses_the_resolved_root(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    codex = tmp_path / ".codex" / "plugins" / "cache" / "minni" / "minni" / "0.3.0"
    codex.mkdir(parents=True)

    spec = propagate.platform_spec("codex", tmp_path)

    assert spec["install"] == codex
    assert spec["agent"] == "codex"


# --- claude-code is wire-managed --------------------------------------------


@pytest.mark.parametrize("name", ["claude-code", "claude", "claude_code"])
def test_claude_code_platform_fails_loud_and_points_at_wire(name, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        propagate.platform_spec(name, tmp_path)

    message = str(excinfo.value)
    assert "minni wire claude-code" in message
    assert "minni wire-adopt claude-code" in message


def test_claude_code_is_absent_from_the_all_expansion():
    assert "claude-code" not in propagate.ALL_PLATFORMS
    assert "codex" in propagate.ALL_PLATFORMS
    assert "cursor" in propagate.ALL_PLATFORMS

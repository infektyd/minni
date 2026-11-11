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


def test_newest_built_version_dir_uses_manifest_built_at_not_hex_sha(tmp_path):
    """D3 (§4.1 #6): never sort +git.<sha> lexicographically; prefer payload-manifest built_at."""
    import json

    older = tmp_path / "0.6.0+git.ffff999"
    newer = tmp_path / "0.6.0+git.1111000"
    older.mkdir()
    newer.mkdir()
    (older / "payload-manifest.json").write_text(
        json.dumps({"built_at": "2026-11-10T01:00:00Z"}), encoding="utf-8"
    )
    (newer / "payload-manifest.json").write_text(
        json.dumps({"built_at": "2026-11-11T02:00:00Z"}), encoding="utf-8"
    )

    assert propagate._newest_built_version_dir(tmp_path) == "0.6.0+git.1111000"


def test_newest_built_version_dir_falls_back_to_mtime_when_manifest_absent(tmp_path):
    import os

    older = tmp_path / "0.3.0"
    newer = tmp_path / "0.4.0"
    older.mkdir()
    newer.mkdir()
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))

    assert propagate._newest_built_version_dir(tmp_path) == "0.4.0"


def test_newest_built_version_dir_prefers_manifest_built_at_over_release_without_manifest(tmp_path):
    import json

    plain = tmp_path / "0.4.0"
    built = tmp_path / "0.4.0+git.abc1234"
    plain.mkdir()
    built.mkdir()
    (built / "payload-manifest.json").write_text(
        json.dumps({"built_at": "2026-11-11T00:00:00Z"}), encoding="utf-8"
    )

    assert propagate._newest_built_version_dir(tmp_path) == "0.4.0+git.abc1234"


def test_newest_built_version_dir_ignores_non_version_dirs(tmp_path):
    (tmp_path / "0.4.0").mkdir()
    (tmp_path / "current").mkdir()
    (tmp_path / ".staging-x").mkdir()

    assert propagate._newest_built_version_dir(tmp_path) == "0.4.0"


def test_newest_built_version_dir_is_none_for_a_missing_base(tmp_path):
    assert propagate._newest_built_version_dir(tmp_path / "nope") is None


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
    """D7 partition: propagate `all` is antigravity+cursor only; wire owns
    codex/claude-code/kilocode/grok (named ALL_SKIPS)."""
    assert "claude-code" not in propagate.ALL_PLATFORMS
    assert "codex" not in propagate.ALL_PLATFORMS
    assert "kilocode" not in propagate.ALL_PLATFORMS
    assert "grok" not in propagate.ALL_PLATFORMS
    assert set(propagate.ALL_PLATFORMS) == {"antigravity", "cursor"}
    assert "codex" in propagate.ALL_SKIPS
    assert "claude-code" in propagate.ALL_SKIPS
    assert "cursor" in propagate.ALL_PLATFORMS


def test_toml_basic_str_escapes_control_chars():
    """Propagate escaper must match wire: raw newlines cannot break TOML strings."""
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent / "propagate.py"
    spec = importlib.util.spec_from_file_location("_prop_toml", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    escaped = mod._toml_basic_str("a\nb")
    assert "\\n" in escaped
    assert "\n" not in escaped


def test_toml_basic_str_matches_wire_writers():
    """Dual-maintained escapers must stay byte-identical on control/meta chars."""
    import importlib.util
    from pathlib import Path

    from minni.wire.writers import _toml_basic_str as wire_esc

    p = Path(__file__).resolve().parent / "propagate.py"
    spec = importlib.util.spec_from_file_location("_prop_toml_parity", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    samples = (
        "plain",
        'quote"here',
        "back\\slash",
        "new\nline",
        "tab\there",
        "carr\riage",
        "nul\x00byte",
        "unit\x1fsep",
        "del\x7f",
        'mix\\"\n\t\r\x01',
    )
    for raw in samples:
        assert mod._toml_basic_str(raw) == wire_esc(raw), repr(raw)

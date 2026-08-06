"""#359 — copy_tree must refresh .gemini-plugin/gemini-extension.json.

Root cause: rsync --exclude gemini-extension.json matched the basename at any
depth, so the source dotdir manifest was never copied and the dest's stale
copy survived --delete. The fix anchors the exclude to /gemini-extension.json
and the fallback path already preserved only the root-level file.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPTS_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import propagate  # noqa: E402

OLD = "0.3.0"
NEW = "0.5.0"


def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / ".gemini-plugin").mkdir(parents=True)
    (src / ".gemini-plugin" / "gemini-extension.json").write_text(
        json.dumps({"name": "minni", "version": NEW}), encoding="utf-8"
    )
    # Add another manifest so the tree is non-trivial.
    (src / ".claude-plugin").mkdir()
    (src / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "minni", "version": NEW}), encoding="utf-8"
    )
    (src / "dist").mkdir()
    (src / "dist" / "server.js").write_text("// server", encoding="utf-8")
    return src


def _make_dest(tmp_path: Path, root_blob: bytes) -> Path:
    dest = tmp_path / "dest"
    (dest / ".gemini-plugin").mkdir(parents=True)
    stale = dest / ".gemini-plugin" / "gemini-extension.json"
    # Different byte length AND backdated mtime: rsync -a's quick-check skips
    # same-size same-second files, which would mask the refresh under test.
    stale.write_text(
        json.dumps({"name": "minni", "version": OLD, "stale": True}), encoding="utf-8"
    )
    os.utime(stale, (0, 0))
    # Root-level generated manifest that copy_tree must preserve byte-for-byte.
    (dest / "gemini-extension.json").write_bytes(root_blob)
    return dest


def _assert_hidden_refreshed_and_root_preserved(dest: Path, root_blob: bytes):
    hidden = json.loads(
        (dest / ".gemini-plugin" / "gemini-extension.json").read_text(encoding="utf-8")
    )
    assert hidden["version"] == NEW, (
        "hidden manifest should be refreshed from source; got "
        f"{hidden['version']!r} expected {NEW!r}"
    )
    assert (dest / "gemini-extension.json").read_bytes() == root_blob, (
        "root-level generated gemini-extension.json must be preserved byte-for-byte"
    )


def test_copy_tree_refreshes_hidden_manifest_via_rsync(tmp_path):
    """Rsync path (default): hidden manifest refreshed, root preserved."""
    root_blob = b'{"name":"minni","version":"0.9.9-root","extra":1}\n'
    src = _make_source(tmp_path)
    dest = _make_dest(tmp_path, root_blob)
    # rsync is present on macOS (openrsync); if missing, this still exercises
    # fallback but we explicitly want the rsync branch — skip if not present.
    import shutil as _sh

    if _sh.which("rsync") is None:
        # Fallback-only machine: assert fallback still satisfies contract.
        propagate.copy_tree(src, dest)
        _assert_hidden_refreshed_and_root_preserved(dest, root_blob)
        return
    propagate.copy_tree(src, dest)
    _assert_hidden_refreshed_and_root_preserved(dest, root_blob)


def test_copy_tree_refreshes_hidden_manifest_via_fallback(tmp_path, monkeypatch):
    """Non-rsync fallback: forced by making shutil.which return None."""
    root_blob = b'{"name":"minni","version":"0.9.9-root","extra":2}\n'
    src = _make_source(tmp_path)
    dest = _make_dest(tmp_path, root_blob)
    monkeypatch.setattr(propagate.shutil, "which", lambda _name: None)
    propagate.copy_tree(src, dest)
    _assert_hidden_refreshed_and_root_preserved(dest, root_blob)


def test_copy_tree_rsync_passes_no_generated_file_excludes(tmp_path, monkeypatch):
    """Regression (#359): no rsync --exclude may name a generated install file.

    An exclude by name — bare OR /-anchored — skips the SOURCE dotdir manifest
    on macOS's openrsync, which matches anchored patterns at any depth. The
    generated root-level file survives via preserve-and-restore instead, so
    the only legitimate exclude left is node_modules.
    """
    root_blob = b"root-preserved\n"
    src = _make_source(tmp_path)
    dest = _make_dest(tmp_path, root_blob)
    captured: dict = {}
    orig_run = propagate.run

    def capture_run(cmd, cwd=None):
        captured["cmd"] = list(cmd)
        return orig_run(cmd, cwd=cwd)

    monkeypatch.setattr(propagate, "run", capture_run)
    if propagate.shutil.which("rsync") is None:
        import pytest

        pytest.skip("no rsync on this machine; fallback path covered elsewhere")
    propagate.copy_tree(src, dest)
    cmd = captured.get("cmd", [])
    excludes = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--exclude" and i + 1 < len(cmd)]
    for name in propagate.GENERATED_INSTALL_FILES:
        offenders = [e for e in excludes if e.lstrip("/") == name]
        assert offenders == [], f"generated file excluded from refresh: {offenders}"
    _assert_hidden_refreshed_and_root_preserved(dest, root_blob)

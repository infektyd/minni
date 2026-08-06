"""#359 — copy_tree must refresh .gemini-plugin/gemini-extension.json.

Root cause: the rsync path excluded the generated root-level
gemini-extension.json by BARE basename, which rsync matches at any depth —
so the source dotdir manifest was never copied and the dest's stale copy
survived every refresh. The fix anchors the exclude to /gemini-extension.json
(root-only match, verified on macOS openrsync), which also keeps the generated
file protected from --delete no matter how the copy exits. The copytree
fallback preserves-and-restores the root file and never had the depth bug.
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
    """Rsync path (default when rsync exists): hidden refreshed, root preserved."""
    import pytest

    if propagate.shutil.which("rsync") is None:
        pytest.skip("no rsync on this machine; fallback path covered below")
    root_blob = b'{"name":"minni","version":"0.9.9-root","extra":1}\n'
    src = _make_source(tmp_path)
    dest = _make_dest(tmp_path, root_blob)
    propagate.copy_tree(src, dest)
    _assert_hidden_refreshed_and_root_preserved(dest, root_blob)


def test_copy_tree_refreshes_hidden_manifest_via_fallback(tmp_path, monkeypatch):
    """Copytree fallback (no rsync): hidden refreshed, root preserved."""
    root_blob = b'{"name":"minni","version":"0.9.9-root","extra":1}\n'
    src = _make_source(tmp_path)
    dest = _make_dest(tmp_path, root_blob)
    monkeypatch.setattr(propagate.shutil, "which", lambda name: None)
    propagate.copy_tree(src, dest)
    _assert_hidden_refreshed_and_root_preserved(dest, root_blob)


def test_copy_tree_rsync_excludes_are_anchored_not_bare(tmp_path, monkeypatch):
    """Regression (#359): generated-file excludes must be /-anchored.

    A bare basename exclude matches at any depth and skips the SOURCE dotdir
    manifest; the anchored form shields only the root-level generated file and
    keeps it protected from --delete even when the copy dies mid-run.
    """
    import pytest

    if propagate.shutil.which("rsync") is None:
        pytest.skip("no rsync on this machine; fallback path covered above")
    root_blob = b"root-preserved\n"
    src = _make_source(tmp_path)
    dest = _make_dest(tmp_path, root_blob)
    captured: dict = {}
    orig_run = propagate.run

    def capture_run(cmd, cwd=None):
        captured["cmd"] = list(cmd)
        return orig_run(cmd, cwd=cwd)

    monkeypatch.setattr(propagate, "run", capture_run)
    propagate.copy_tree(src, dest)
    cmd = captured.get("cmd", [])
    excludes = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--exclude" and i + 1 < len(cmd)]
    for name in propagate.GENERATED_INSTALL_FILES:
        assert f"/{name}" in excludes, f"anchored exclude /{name} missing: {excludes}"
        assert name not in excludes, f"bare exclude {name!r} reintroduces #359: {excludes}"
    _assert_hidden_refreshed_and_root_preserved(dest, root_blob)

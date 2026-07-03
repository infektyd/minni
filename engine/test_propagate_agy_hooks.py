"""Tests for propagate.update_agy_plugin_hooks (#133, agy CLI hook wiring).

The agy plugin system's live-verified quirks drive these invariants:
  - agy does not expand ``${CLAUDE_PLUGIN_ROOT}``: the stamped hooks.json must
    carry the absolute dist path and no residue of the template token.
  - Registration must go through ``agy plugin install <staging>`` where the
    staging directory is NEVER the destination (agy copies source onto itself
    and truncates every file to zero bytes).
  - ``agy plugin enable`` exits non-zero when already enabled; tolerated.
  - A missing agy binary is a graceful skip with a reason, not a failure.
"""

import json
import os
import stat
import sys
from pathlib import Path

SCRIPTS_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import propagate  # noqa: E402


HOOKS_TEMPLATE = {
    "_comment": "test template",
    "hooks": {
        "PreToolUse": [
            {"hooks": [{"type": "command", "command": "node __MINNI_GEMINI_DIST__/gemini-hook.js PreToolUse"}]}
        ],
        "Stop": [
            {"hooks": [{"type": "command", "command": "node __MINNI_GEMINI_DIST__/gemini-hook.js Stop"}]}
        ],
    },
}


def make_install_root(tmp_path: Path) -> Path:
    install_root = tmp_path / "extensions" / "minni"
    (install_root / "hooks").mkdir(parents=True)
    (install_root / "hooks" / "hooks-gemini.json").write_text(
        json.dumps(HOOKS_TEMPLATE), encoding="utf-8"
    )
    return install_root


def make_fake_agy(
    tmp_path: Path, plugins_dir: Path, *, enable_rc: int = 0, enable_stderr: str = ""
) -> Path:
    """A fake agy CLI: `plugin install <src>` copies src into plugins_dir under
    its plugin.json name (mirroring real agy), `plugin enable` exits enable_rc
    after printing enable_stderr. It records every argv line for
    install-source assertions."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "agy-calls.log"
    script = bin_dir / "agy"
    script.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> {log}\n"
        "if [ \"$1\" = plugin ] && [ \"$2\" = install ]; then\n"
        "  src=\"$3\"\n"
        "  name=$(python3 -c \"import json,sys;print(json.load(open(sys.argv[1]+'/plugin.json'))['name'])\" \"$src\")\n"
        f"  mkdir -p {plugins_dir}/$name\n"
        f"  cp \"$src\"/* {plugins_dir}/$name/\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = plugin ] && [ \"$2\" = enable ]; then\n"
        f"  [ -n \"{enable_stderr}\" ] && echo \"{enable_stderr}\" >&2\n"
        f"  exit {enable_rc}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return log


def test_agy_missing_is_a_graceful_skip(tmp_path, monkeypatch):
    install_root = make_install_root(tmp_path)
    monkeypatch.setattr(propagate.shutil, "which", lambda name: None)

    result = propagate.update_agy_plugin_hooks(install_root)

    assert result["installed"] is False
    assert "agy CLI not found" in str(result["reason"])


def test_missing_template_reports_reason(tmp_path):
    install_root = tmp_path / "extensions" / "minni"
    install_root.mkdir(parents=True)

    result = propagate.update_agy_plugin_hooks(install_root)

    assert result["installed"] is False
    assert "hooks-gemini.json" in str(result["reason"])


def test_install_stamps_absolute_paths_and_never_stages_in_destination(tmp_path, monkeypatch):
    install_root = make_install_root(tmp_path)
    plugins_dir = tmp_path / "gemini-config-plugins"
    plugins_dir.mkdir()
    log = make_fake_agy(tmp_path, plugins_dir)
    monkeypatch.setattr(
        propagate.shutil, "which",
        lambda name: str(tmp_path / "bin" / "agy") if name == "agy" else None,
    )
    monkeypatch.setattr(propagate, "AGY_PLUGINS_DIR", str(plugins_dir))

    result = propagate.update_agy_plugin_hooks(install_root)

    assert result["installed"] is True
    installed = plugins_dir / "minni"
    hooks = json.loads((installed / "hooks.json").read_text(encoding="utf-8"))
    plugin = json.loads((installed / "plugin.json").read_text(encoding="utf-8"))
    assert plugin == {"name": "minni"}
    # The template _comment must not ship in the registered manifest.
    assert "_comment" not in hooks
    rendered = json.dumps(hooks)
    assert "__MINNI_GEMINI_DIST__" not in rendered
    assert "CLAUDE_PLUGIN_ROOT" not in rendered
    assert str(install_root / "dist" / "gemini-hook.js") in rendered
    # Install must have been invoked from a staging dir, never the destination
    # (agy truncates files to zero bytes when source == destination).
    install_calls = [
        line for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("plugin install")
    ]
    assert len(install_calls) == 1
    src = install_calls[0].split()[2]
    assert not src.startswith(str(plugins_dir))
    assert not src.startswith(str(install_root))
    # Staging is cleaned up afterwards.
    assert not Path(src).exists()


def test_enable_already_enabled_is_tolerated(tmp_path, monkeypatch):
    install_root = make_install_root(tmp_path)
    plugins_dir = tmp_path / "gemini-config-plugins"
    plugins_dir.mkdir()
    make_fake_agy(
        tmp_path, plugins_dir, enable_rc=1,
        enable_stderr='Error: plugin minni is already enabled',
    )
    monkeypatch.setattr(
        propagate.shutil, "which",
        lambda name: str(tmp_path / "bin" / "agy") if name == "agy" else None,
    )
    monkeypatch.setattr(propagate, "AGY_PLUGINS_DIR", str(plugins_dir))

    result = propagate.update_agy_plugin_hooks(install_root)

    # The known already-enabled response must not fail the install.
    assert result["installed"] is True


def test_other_enable_failures_surface_as_unsuccessful(tmp_path, monkeypatch):
    install_root = make_install_root(tmp_path)
    plugins_dir = tmp_path / "gemini-config-plugins"
    plugins_dir.mkdir()
    make_fake_agy(
        tmp_path, plugins_dir, enable_rc=1,
        enable_stderr='Error: plugin minni is suspended',
    )
    monkeypatch.setattr(
        propagate.shutil, "which",
        lambda name: str(tmp_path / "bin" / "agy") if name == "agy" else None,
    )
    monkeypatch.setattr(propagate, "AGY_PLUGINS_DIR", str(plugins_dir))

    result = propagate.update_agy_plugin_hooks(install_root)

    # A genuinely failed enable means agy may never dispatch hook events; the
    # function must not report success just because hooks.json landed on disk.
    assert result["installed"] is False
    assert "enable failed" in str(result["reason"])
    assert "suspended" in str(result["reason"])


def test_failed_install_surfaces_reason_not_success(tmp_path, monkeypatch):
    install_root = make_install_root(tmp_path)
    plugins_dir = tmp_path / "gemini-config-plugins"
    plugins_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agy = bin_dir / "agy"
    agy.write_text("#!/bin/sh\nif [ \"$2\" = install ]; then echo boom >&2; exit 2; fi\nexit 0\n", encoding="utf-8")
    agy.chmod(agy.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(
        propagate.shutil, "which",
        lambda name: str(agy) if name == "agy" else None,
    )
    monkeypatch.setattr(propagate, "AGY_PLUGINS_DIR", str(plugins_dir))

    result = propagate.update_agy_plugin_hooks(install_root)

    assert result["installed"] is False
    assert "boom" in str(result["reason"])

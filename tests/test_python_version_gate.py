"""The Makefile's interpreter checks must match .python-version exactly.

`make setup` used to accept any `sys.version_info >= (3, 14)`. A system
python3 of 3.15 then built a venv on which the locked wheels (scikit-learn
1.9.0) cannot install, and setup failed far from the cause.

The behavioural tests run `make venv` (the interpreter step `setup` depends
on) in a sandbox copy of the Makefile and gate. They use a stub `uv` that only
records its arguments, and interpreter shims that report a chosen version
through `sitecustomize`. Nothing is installed and nothing touches the network.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "python_version_gate.py"
MAKE = shutil.which("make")


def _gate():
    spec = importlib.util.spec_from_file_location("python_version_gate", GATE)
    assert spec is not None and spec.loader is not None, f"missing {GATE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pin() -> tuple[int, int]:
    return _gate().pinned_version(ROOT / ".python-version")


def _executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _python_shim(tmp_path: Path, name: str, version: tuple[int, int, int]) -> Path:
    """An interpreter that runs the real one but reports `version`."""
    site = tmp_path / f"site-{name}"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        f"import sys\nsys.version_info = {(*version, 'final', 0)!r}\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return _executable(
        bin_dir / name,
        f"#!/bin/sh\nPYTHONPATH='{site}' exec '{sys.executable}' \"$@\"\n",
    )


def _sandbox(tmp_path: Path, pin_text: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "Makefile", repo / "Makefile")
    shutil.copy(GATE, repo / "scripts" / "python_version_gate.py")
    (repo / ".python-version").write_text(pin_text, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv_log = tmp_path / "uv.log"
    _executable(bin_dir / "uv", f"#!/bin/sh\necho \"$@\" >> '{uv_log}'\nexit 0\n")
    return repo, uv_log


def _venv_python(repo: Path, shim: Path) -> Path:
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    return _executable(bin_dir / "python", f"#!/bin/sh\nexec '{shim}' \"$@\"\n")


def _make_venv(repo: Path, tmp_path: Path, system_python: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    return subprocess.run(
        [MAKE, "-C", str(repo), "venv", f"PYTHON_FOR_VENV={system_python}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_gate_accepts_only_the_pinned_minor():
    gate = _gate()
    major, minor = _pin()
    assert gate.accepts((major, minor, 8, "final", 0), (major, minor))
    assert not gate.accepts((major, minor + 1, 0, "final", 0), (major, minor))
    assert not gate.accepts((major, minor - 1, 9, "final", 0), (major, minor))


@pytest.mark.parametrize("delta,expected", [(0, 0), (1, 1), (-1, 1)])
def test_gate_cli_exit_codes_for_reported_versions(tmp_path, delta, expected):
    major, minor = _pin()
    shim = _python_shim(tmp_path, "py", (major, minor + delta, 0))
    result = subprocess.run([str(shim), str(GATE)], capture_output=True, text=True)
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("text", ["3", "", "three.fourteen", "3.x"])
def test_malformed_pin_is_rejected(tmp_path, text):
    gate = _gate()
    pin = tmp_path / ".python-version"
    pin.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        gate.pinned_version(pin)


@pytest.mark.skipif(MAKE is None, reason="make is not installed")
def test_malformed_pin_stops_before_touching_the_venv(tmp_path):
    repo, uv_log = _sandbox(tmp_path, "3.x")
    major, minor = _pin()
    real = _python_shim(tmp_path, "python-real", (major, minor, 8))
    venv_python = _venv_python(repo, real)

    result = _make_venv(repo, tmp_path, real)

    assert result.returncode != 0
    assert "make setup stopped: fix .python-version" in result.stdout + result.stderr
    assert venv_python.exists(), "an existing .venv must survive a malformed pin"
    assert not uv_log.exists(), "uv must not be called for a malformed pin"


@pytest.mark.skipif(MAKE is None, reason="make is not installed")
def test_newer_system_python_keeps_a_matching_venv(tmp_path):
    major, minor = _pin()
    repo, uv_log = _sandbox(tmp_path, f"{major}.{minor}")
    newer = _python_shim(tmp_path, "python-newer", (major, minor + 1, 0))
    matching = _python_shim(tmp_path, "python-matching", (major, minor, 8))
    venv_python = _venv_python(repo, matching)

    result = _make_venv(repo, tmp_path, newer)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f".venv already uses Python {major}.{minor}" in result.stdout
    assert venv_python.exists()
    assert not uv_log.exists()


@pytest.mark.skipif(MAKE is None, reason="make is not installed")
def test_newer_system_python_without_a_venv_provisions_the_pin_with_uv(tmp_path):
    major, minor = _pin()
    repo, uv_log = _sandbox(tmp_path, f"{major}.{minor}")
    newer = _python_shim(tmp_path, "python-newer", (major, minor + 1, 0))

    result = _make_venv(repo, tmp_path, newer)

    assert result.returncode == 0, result.stdout + result.stderr
    assert uv_log.read_text(encoding="utf-8").split() == [
        "venv",
        "--seed",
        "--python",
        f"{major}.{minor}",
        ".venv",
    ]

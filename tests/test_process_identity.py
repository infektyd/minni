"""Check native naming in children, never rename the pytest process."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from minni import process_identity


def test_naming_uses_extension_without_changing_python_arguments(monkeypatch):
    names = []
    monkeypatch.setitem(sys.modules, "setproctitle", SimpleNamespace(setproctitle=names.append))
    original_argv = list(sys.argv)
    assert process_identity.set_process_identity() is True
    assert names == ["minni"]
    assert sys.argv == original_argv


@pytest.mark.parametrize("name", [None, "", "bad\x00name", 123])
def test_invalid_names_never_reach_native_api(monkeypatch, name):
    names = []
    monkeypatch.setitem(sys.modules, "setproctitle", SimpleNamespace(setproctitle=names.append))
    assert process_identity.set_process_identity(name) is False
    assert names == []


def test_missing_extension_does_not_prevent_startup(monkeypatch):
    monkeypatch.setitem(sys.modules, "setproctitle", None)
    assert process_identity.set_process_identity() is False


def test_native_failure_does_not_prevent_startup(monkeypatch):
    def unavailable(_name):
        raise RuntimeError("native naming unavailable")

    monkeypatch.setitem(sys.modules, "setproctitle", SimpleNamespace(setproctitle=unavailable))
    assert process_identity.set_process_identity() is False


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="ps contract is for macOS/Linux")
@pytest.mark.parametrize("daemon_entrypoint", [False, True])
def test_child_process_is_visible_as_minni(daemon_entrypoint):
    # The daemon entrypoint is stopped immediately after naming, before model
    # initialization, sockets, databases, or background services can start.
    start = """
import minni.minnid as daemon
class StartupObserved(Exception):
    pass
original_time = daemon.time.time
def stop_after_naming():
    raise StartupObserved()
daemon.time.time = stop_after_naming
try:
    daemon.main()
except StartupObserved:
    pass
finally:
    daemon.time.time = original_time
""" if daemon_entrypoint else """
from minni.process_identity import set_process_identity
assert set_process_identity()
"""
    code = "import json, os, subprocess, sys\noriginal_argv = list(sys.argv)\n" + start + """
import setproctitle
command = subprocess.check_output(
    ["ps", "-p", str(os.getpid()), "-o", "command="], text=True
).strip()
print(json.dumps({"title": setproctitle.getproctitle(), "command": command,
                  "argv_preserved": sys.argv == original_argv}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(process_identity.__file__).resolve().parents[1])
    child = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True,
        text=True, timeout=30, check=True,
    )
    observed = json.loads(child.stdout)
    assert observed == {"title": "minni", "command": "minni", "argv_preserved": True}

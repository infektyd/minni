"""The offline alias diagnostic must open its database as a read-only URI.

`sqlite3.connect("file:...?mode=ro")` without `uri=True` treats the string as
a literal filename unless the SQLite library was built with URI filenames on
by default. Homebrew's build has them on; python-build-standalone's does not,
so the diagnostic failed with "unable to open database file" there.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_legacy_aliases.py"


def _script():
    spec = importlib.util.spec_from_file_location("diagnose_legacy_aliases_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_connects_with_uri_true_and_read_only_mode(tmp_path, monkeypatch, capsys):
    script = _script()
    db = tmp_path / "memory.db"
    sqlite3.connect(db).close()
    vault = tmp_path / "vault"
    vault.mkdir()

    calls = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(script.sqlite3, "connect", recording_connect)
    # The empty database has no memory tables, so the diagnostic itself fails
    # (exit 1); what matters is that it got past opening the file.
    exit_code = script.main(["--db", str(db), "--vault", str(vault)])

    assert calls, "the diagnostic never opened its database"
    args, kwargs = calls[0]
    assert kwargs.get("uri") is True
    assert str(args[0]).startswith("file:")
    assert str(args[0]).endswith("?mode=ro")
    assert exit_code in (0, 1)
    assert "unable to open database file" not in capsys.readouterr().err

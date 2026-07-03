"""NEW-05: _safe_status_error must redact Windows drive-letter paths too.

The POSIX-only regex left `C:\\Users\\...` style paths raw in status/error output.
"""

from __future__ import annotations

from minni.afm_provider import _safe_status_error


def test_redacts_posix_path():
    out = _safe_status_error("failed reading /Users/alice/.minni/secret.db")
    assert "[local-path]" in out
    assert "/Users/alice" not in out


def test_redacts_windows_backslash_path():
    out = _safe_status_error(r"helper crashed at C:\Users\alice\AppData\minni\helper.exe")
    assert "[local-path]" in out
    assert "alice" not in out
    assert "AppData" not in out


def test_redacts_windows_forwardslash_path():
    out = _safe_status_error("config not found: D:/data/minni/providers.json")
    assert "[local-path]" in out
    assert "providers.json" not in out


def test_does_not_redact_http_url_scheme():
    # The \b guard means multi-letter URL schemes are not mistaken for drive
    # letters (http:// -> the char before 'p' is not a word boundary).
    out = _safe_status_error("posted to http://localhost:9999/health ok")
    assert "http://localhost" in out

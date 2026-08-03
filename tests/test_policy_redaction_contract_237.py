"""#237 / SEC-G7 — POLICY.md redaction contract must match the engine.

POLICY.md previously claimed a MUST-redact for JSON-quoted secrets and all
local absolute paths. The implementation is label-oriented and path-limited
(see THREAT_MODEL.md residual and redaction.py). These tests pin the docs and
the engine to the same honest coverage surface.
"""
from __future__ import annotations

from pathlib import Path

from minni.minnid_runtime.redaction import redact_text

ROOT = Path(__file__).resolve().parents[1]
POLICY = (ROOT / "docs" / "contracts" / "POLICY.md").read_text(encoding="utf-8")


def test_policy_marks_secret_and_path_redaction_partial():
    assert "### 2.1 Secret patterns (PARTIAL)" in POLICY
    assert "### 2.2 Local filesystem paths (PARTIAL)" in POLICY
    assert "### 2.3 Adapter and launchd filenames (PARTIAL)" in POLICY
    # Must not re-introduce the overstated JSON-quoted example as a covered form.
    assert '"api_key": "..."' not in POLICY
    assert "JSON-quoted" in POLICY
    assert "THREAT_MODEL.md" in POLICY
    # §2.3 honesty: bare plist/socket names are not claimed as rewritten.
    assert "are **not** rewritten" in POLICY
    assert "com.minni.minnid.plist" in POLICY


def test_redaction_engine_matches_documented_coverage():
    # Bare keyword assignment — covered.
    bare, bare_changed = redact_text("api_key=sk-live-exampletokenvalue")
    assert bare_changed is True
    assert "api_key=[REDACTED]" in bare

    # JSON-quoted form — not covered (the #237 PROVEN miss).
    quoted, quoted_changed = redact_text('"api_key": "sk-live-exampletokenvalue"')
    assert quoted_changed is False
    assert "sk-live-exampletokenvalue" in quoted

    # macOS home path — covered; /home Docker layout — not covered.
    mac, mac_changed = redact_text("path=/Users/operator/.minni/auth.json")
    assert mac_changed is True
    assert "[REDACTED_PATH]" in mac

    home, home_changed = redact_text("path=/home/minni/vault/secrets.md")
    assert home_changed is False
    assert "/home/minni" in home

    # §2.3: bare infrastructure names leave the process unchanged; path-shaped
    # sockets under macOS patterns may still become [REDACTED_PATH].
    bare_plist, bare_plist_changed = redact_text("com.minni.minnid.plist")
    assert bare_plist_changed is False
    assert bare_plist == "com.minni.minnid.plist"

    bare_sock, bare_sock_changed = redact_text("minnid.sock")
    assert bare_sock_changed is False
    assert bare_sock == "minnid.sock"

    tmp_db, tmp_db_changed = redact_text("path=/tmp/minni.db")
    assert tmp_db_changed is False
    assert "/tmp/minni.db" in tmp_db

    mac_sock, mac_sock_changed = redact_text("socket=/Users/op/.minni/run/minnid.sock")
    assert mac_sock_changed is True
    assert "[REDACTED_PATH]" in mac_sock

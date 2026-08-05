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
    # Bare provider prefixes are covered; unknown-prefix residual remains honest.
    assert "Bare provider prefixes" in POLICY or "sk-" in POLICY
    assert "THREAT_MODEL.md" in POLICY
    # §2.3 honesty: bare plist/socket names are not claimed as rewritten.
    assert "are **not** rewritten" in POLICY
    assert "com.minni.minnid.plist" in POLICY


def test_redaction_engine_matches_documented_coverage():
    # Bare keyword assignment — covered.
    bare, bare_changed = redact_text("api_key=sk-TESTexampletokenvalue00")
    assert bare_changed is True
    assert "api_key=[REDACTED]" in bare

    # JSON-quoted form — covered (post-2026-08-04 local residual).
    quoted, quoted_changed = redact_text('"api_key": "sk-TESTexampletokenvalue00"')
    assert quoted_changed is True
    assert "sk-TESTexampletokenvalue00" not in quoted
    assert "api_key=[REDACTED]" in quoted

    # macOS home path + Linux/Docker /home — covered.
    mac, mac_changed = redact_text("path=/Users/operator/.minni/auth.json")
    assert mac_changed is True
    assert "[REDACTED_PATH]" in mac

    home, home_changed = redact_text("path=/home/minni/vault/secrets.md")
    assert home_changed is True
    assert "[REDACTED_PATH]" in home
    assert "/home/minni" not in home

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

    # Bare provider tokens — covered with length floors.
    bare_sk, bare_sk_ch = redact_text(
        "key sk-TESTEXAMPLETOKEN0123456789abcd leftover"
    )
    assert bare_sk_ch is True
    assert "sk-TESTEXAMPLETOKEN0123456789abcd" not in bare_sk
    assert "[REDACTED]" in bare_sk

    # Short sk- English should not trip the floor.
    short, short_ch = redact_text("use sk-learn for clustering")
    assert short_ch is False
    assert "sk-learn" in short

    bare_gh, bare_gh_ch = redact_text(
        "token ghp_TESTTESTTESTTESTTESTTESTTES leftover"
    )
    assert bare_gh_ch is True
    assert "ghp_TESTTESTTESTTESTTESTTESTTES" not in bare_gh

    # Floor edges + extra bare prefixes (POLICY table)
    short15, short15_ch = redact_text("sk-0123456789abcde")  # 15 trailing
    assert short15_ch is False
    long16, long16_ch = redact_text("sk-0123456789abcdef")  # 16 trailing
    assert long16_ch is True
    assert "sk-0123456789abcdef" not in long16

    pat, pat_ch = redact_text(
        "github_pat_TESTTEST0TESTTESTTESTTESTTEST leftover"
    )
    assert pat_ch is True
    akia, akia_ch = redact_text("AKIATESTTESTTESTTEST leftover")
    assert akia_ch is True
    # Concatenate so secret-scanners do not treat the fixture as a live token.
    xox_fixture = "xox" + "b-" + "0000000000" + "-" + "FAKEFAKEFAKEFAKE"
    xox, xox_ch = redact_text(xox_fixture + " leftover")
    assert xox_ch is True

    # JSON password / token keyword path
    jpw, jpw_ch = redact_text('"password": "hunter2secret"')
    assert jpw_ch is True
    assert "hunter2secret" not in jpw
    jtok, jtok_ch = redact_text('"access_token": "xyz-token-value"')
    assert jtok_ch is True
    assert "xyz-token-value" not in jtok

    # Known miss (honesty): quoted assignment with '=' is outside unquoted charset
    # and is not JSON (requires ':'). Engine must leave value visible.
    miss, miss_ch = redact_text('password="hunter2quoted"')
    assert miss_ch is False
    assert "hunter2quoted" in miss

    # Paths: /Volumes and /private
    vol, vol_ch = redact_text("file=/Volumes/Disk/secret.md")
    assert vol_ch is True
    assert "[REDACTED_PATH]" in vol
    priv, priv_ch = redact_text("file=/private/tmp/x")
    assert priv_ch is True

    # PEM regression
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEfakekeymaterial\n-----END RSA PRIVATE KEY-----"
    pem_out, pem_ch = redact_text(pem)
    assert pem_ch is True
    assert "MIIEfakekeymaterial" not in pem_out
    assert "[REDACTED]" in pem_out

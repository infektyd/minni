"""Tests for .github/scripts/check-no-credential-leak.py — the gate that
refuses to post a Grok reply carrying real credential material.

Every "leaks" case below is a bypass that a REAL review of this gate found in
its first implementation (PR #216): the sliding window stepped by 8 and
stopped at `len - WINDOW`, so a quote of a token's tail or an unaligned slice
walked straight through, and any re-encoding did too. They are pinned here so
the gate cannot silently regress to that state.

The "does not leak" cases matter just as much: the version before that one
matched the WORDS refresh_token/principal_id and blocked a legitimate security
review, which is how a gate gets switched off.
"""

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "check-no-credential-leak.py"

# Shaped like a real auth.json, with values that are obviously fake.
FAKE_TOKEN = (
    "eyJ0eXAiOiJhdCtqd3QiLCJhbGciOiJFUzI1NiJ9"
    ".ZmFrZXBheWxvYWRmb3J0ZXN0aW5nb25seXh4eHh4eHh4eHg"
    ".c2lnbmF0dXJlZmFrZQ"
)
FAKE_REFRESH = "FAKEREFRESHTOKENfortestingonly1234567890abcdefGHIJ"


@pytest.fixture
def auth_file(tmp_path: Path) -> Path:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "https://auth.x.ai::client": {
            "key": FAKE_TOKEN,
            "refresh_token": FAKE_REFRESH,
            "email": "test@example.com",
            "principal_type": "User",
        }
    }))
    return path


def check(tmp_path: Path, auth: Path | None, reply: str) -> int:
    reply_path = tmp_path / "reply.md"
    reply_path.write_text(reply)
    args = [sys.executable, str(SCRIPT), str(reply_path)]
    if auth is not None:
        args.append(str(auth))
    return subprocess.run(args, capture_output=True, text=True).returncode


# ── leaks: each of these must FAIL the gate ────────────────────────────────

def test_full_token_is_caught(tmp_path, auth_file):
    assert check(tmp_path, auth_file, f'Found: "key": "{FAKE_TOKEN}"') == 1


def test_token_tail_is_caught(tmp_path, auth_file):
    """The exact miss in the first implementation: `range` stopped one window
    short, so the last 24 characters — a JWT signature — matched nothing."""
    assert check(tmp_path, auth_file, f"tail is {FAKE_REFRESH[-24:]}") == 1


def test_unaligned_slice_is_caught(tmp_path, auth_file):
    """With the old step of 8, a quote starting at offset 3 aligned with no
    window at all."""
    assert check(tmp_path, auth_file, f"fragment {FAKE_REFRESH[3:27]} here") == 1


def test_base64_encoded_token_is_caught(tmp_path, auth_file):
    encoded = base64.b64encode(FAKE_REFRESH.encode()).decode()
    assert check(tmp_path, auth_file, f"b64: {encoded}") == 1


def test_hex_encoded_token_is_caught(tmp_path, auth_file):
    assert check(tmp_path, auth_file, f"hex: {FAKE_REFRESH.encode().hex()}") == 1


def test_whitespace_spaced_token_is_caught(tmp_path, auth_file):
    assert check(tmp_path, auth_file, " ".join(FAKE_REFRESH)) == 1


def test_jwt_shape_is_caught_without_any_auth_file(tmp_path):
    """Shape check stands alone: an unrelated JWT is still credential-shaped."""
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"
    assert check(tmp_path, None, f"token: {jwt}") == 1


# ── clean: each of these must PASS ─────────────────────────────────────────

def test_security_review_prose_passes(tmp_path, auth_file):
    """The false positive that broke a real review: talking ABOUT credentials
    is what a security review does."""
    reply = (
        "needs changes — an injection could `cat auth.json` and dump the "
        'refresh_token into the reply; principal_id and "key": are readable. '
        "Fix: restrict_network plus a scan before posting. A JWT starts eyJ."
    )
    assert check(tmp_path, auth_file, reply) == 0


def test_ordinary_review_passes(tmp_path, auth_file):
    assert check(tmp_path, auth_file,
                 "Reviewed 12 files. No blocking issues; see "
                 "src/minni/afm_passes/consolidation.py:313.") == 0


def test_short_values_do_not_trip_the_gate(tmp_path, auth_file):
    """`email` and `principal_type` are below MIN_SECRET_LEN — quoting them is
    not a leak, and treating them as one would block routine discussion."""
    assert check(tmp_path, auth_file, "The account is test@example.com (User).") == 0


def test_missing_auth_file_is_not_itself_a_failure(tmp_path):
    assert check(tmp_path, tmp_path / "nope.json", "A perfectly ordinary reply.") == 0

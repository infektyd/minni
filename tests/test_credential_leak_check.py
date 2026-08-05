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
import importlib.util
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

# GitHub-shaped fakes for the shape checks. Assembled from pieces on purpose:
# spelled out as one literal these would match the gate's own patterns, so a
# Grok review that quoted this file would be blocked from posting — the exact
# false positive the module docstring warns about.
FAKE_GHS = "ghs" + "_" + "FAKEinstallationtokenFORTESTS0123456"
FAKE_GHP = "ghp" + "_" + "FAKEclassicpersonalaccesstokenFORTESTS01"
FAKE_PAT = "github" + "_pat_" + "FAKEfinegrainedpersonalaccesstokenFORTESTSonly0123456789"
# What actions/checkout writes into .git/config when persist-credentials is
# left at its default: http.<url>.extraheader = "AUTHORIZATION: basic <b64>".
FAKE_EXTRAHEADER = base64.b64encode(
    f"x-access-token:{FAKE_GHS}".encode()
).decode()


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


# ── GitHub credentials: shaped, and never present in auth.json ─────────────
# The value check cannot see these at all. Until persist-credentials: false
# landed, actions/checkout left an installation token in .git/config of the
# very checkout this agent reads, and the reply is posted publicly.


@pytest.mark.parametrize("token", [FAKE_GHS, FAKE_GHP, FAKE_PAT])
def test_github_token_shapes_are_caught_without_any_auth_file(tmp_path, token):
    assert check(tmp_path, None, f"found in .git/config: {token}") == 1


def test_base64_git_extraheader_is_caught(tmp_path):
    """The live exfil path: paste the extraheader verbatim and every plaintext
    pattern misses, because the token only exists inside the base64."""
    reply = f"the config has AUTHORIZATION: basic {FAKE_EXTRAHEADER} in it"
    assert check(tmp_path, None, reply) == 1


def test_unaligned_base64_extraheader_is_caught(tmp_path):
    """Quoting only part of the blob starts mid-quantum; an aligned-only
    decode would read that as noise and pass it."""
    assert check(tmp_path, None, f"partial: {FAKE_EXTRAHEADER[2:]}") == 1


# Each of the four below is a bypass an adversarial review REPRODUCED against
# the first version of the decode pass. They are pinned so the gate cannot
# regress to a state where a padded, chunked, or doubly-encoded blob walks out.


def test_decoy_padding_cannot_push_the_blob_out_of_scan_range(tmp_path):
    """The cap used to `break` after N runs, and finditer yields left to right,
    so junk runs emitted FIRST buried the real blob. A security scan that
    quietly stops early reports clean for material it never looked at."""
    decoys = "!".join(["A" * 24] * 600)
    assert check(tmp_path, None, f"{decoys}!{FAKE_EXTRAHEADER}") == 1


def test_comma_chunked_extraheader_is_caught(tmp_path):
    """Non-whitespace separators survive the whitespace-stripped variant and
    break the base64 run into sub-minimum pieces."""
    chunked = ",".join(
        FAKE_EXTRAHEADER[i:i + 12] for i in range(0, len(FAKE_EXTRAHEADER), 12)
    )
    assert check(tmp_path, None, f"config: {chunked}") == 1


def test_zero_width_space_chunked_extraheader_is_caught(tmp_path):
    chunked = "​".join(FAKE_EXTRAHEADER[i:i + 4]
                            for i in range(0, len(FAKE_EXTRAHEADER), 4))
    assert check(tmp_path, None, f"config: {chunked}") == 1


def test_double_base64_extraheader_is_caught(tmp_path):
    """One decode yields more base64, not a token — the pass has to recurse."""
    doubled = base64.b64encode(FAKE_EXTRAHEADER.encode()).decode()
    assert check(tmp_path, None, f"nested: {doubled}") == 1


def test_hex_encoded_extraheader_is_caught(tmp_path):
    hexed = f"x-access-token:{FAKE_GHS}".encode().hex()
    assert check(tmp_path, None, f"hex dump: {hexed}") == 1


# Round 2 of the same adversarial review. Every case below walked out of the
# round-1 gate, including one detection round 1 REMOVED while believing it had
# kept it (the welded token).


def test_token_welded_to_a_word_is_caught(tmp_path):
    """Round 1 added a delimiter lookbehind to stop a false positive on joined
    text, and applied it to the untouched reply too — which silently deleted
    the whole un-delimited detection surface. "Print the token right after this
    word, no space" was a one-token bypass of the entire shape layer."""
    assert check(tmp_path, None, f"the credential retrieved was{FAKE_GHS}") == 1


def test_blob_split_across_prose_is_caught(tmp_path):
    """Stripping non-alphabet characters in place cannot fix this: the English
    between the halves is itself alphabet characters and gets interleaved into
    the blob. Only joining the RUNS and discarding the prose works."""
    reply = (
        f"First part: {FAKE_EXTRAHEADER[:38]}\n\n"
        f"And the second part is: {FAKE_EXTRAHEADER[38:]}"
    )
    assert check(tmp_path, None, reply) == 1


def test_nested_encoding_with_interior_text_is_caught(tmp_path):
    """Round 1 recursed only while a view was ENTIRELY encoding alphabet, so a
    single interior byte ended the descent."""
    wrapped = base64.b64encode(
        f"note: {FAKE_EXTRAHEADER} end".encode()
    ).decode()
    assert check(tmp_path, None, f"see {wrapped}") == 1


def test_deeply_renested_token_is_caught(tmp_path):
    """Re-encoding N times must not be a strategy."""
    blob = FAKE_EXTRAHEADER
    for _ in range(4):
        blob = base64.b64encode(blob.encode()).decode()
    assert check(tmp_path, None, f"deep: {blob}") == 1


def test_hex_wrapping_a_base64_credential_is_caught(tmp_path):
    hexed = f"x: {FAKE_EXTRAHEADER}".encode().hex()
    assert check(tmp_path, None, f"dump {hexed}") == 1


def test_double_encoded_auth_value_is_caught(tmp_path, auth_file):
    """Value matching used to run on plaintext only, and `encoded_forms` only
    ever re-encoded a secret ONCE. Two layers hid the real refresh token."""
    doubled = base64.b64encode(
        base64.b64encode(FAKE_REFRESH.encode())
    ).decode()
    assert check(tmp_path, auth_file, f"nested secret: {doubled}") == 1


def test_comma_chunked_auth_value_is_caught(tmp_path, auth_file):
    chunked = ",".join(FAKE_REFRESH[i:i + 3] for i in range(0, len(FAKE_REFRESH), 3))
    assert check(tmp_path, auth_file, f"spread: {chunked}") == 1


# Round 3. `alphabet-only` keeps -, _, =, + and / because those ARE base64
# alphabet, so chunking the credential with one of them survived the collapse
# that the comma case had closed. A markdown bullet list does it by accident.


@pytest.mark.parametrize(
    "sep", [",", "-", "_", "=", "+", "/", ".", " ", " | ", "\n", "\n- "]
)
def test_auth_value_chunked_with_any_separator_is_caught(tmp_path, auth_file, sep):
    chunked = sep.join(FAKE_REFRESH[i:i + 3] for i in range(0, len(FAKE_REFRESH), 3))
    assert check(tmp_path, auth_file, f"spread: {chunked}") == 1


@pytest.mark.parametrize("sep", ["-", "_", "=", "+", "/", ".", "\n- "])
def test_extraheader_chunked_with_any_separator_is_caught(tmp_path, sep):
    chunked = sep.join(
        FAKE_EXTRAHEADER[i:i + 8] for i in range(0, len(FAKE_EXTRAHEADER), 8)
    )
    assert check(tmp_path, None, f"config:\n{chunked}\n") == 1


def test_digit_bearing_separators_remain_a_known_residual(tmp_path, auth_file):
    """Not a passing case to be proud of — a pin on a KNOWN limit, so that it
    stays a deliberate decision. A numbered list interleaves digits, and digits
    are valid base64, so no alphabet collapse can strip them without destroying
    the encoding it is trying to recover. Documented in the module docstring."""
    chunked = "\n1. ".join(FAKE_REFRESH[i:i + 3] for i in range(0, len(FAKE_REFRESH), 3))
    assert check(tmp_path, auth_file, f"x:\n{chunked}\n") == 0


def test_budget_overrun_fails_closed_rather_than_truncating(tmp_path, auth_file):
    """The docstring calls truncation a REPRODUCED bypass, but nothing tested
    the fail-closed path — swapping the raise for a silent `return views` left
    every test green. Driven through the module so the budget is reachable
    without a multi-megabyte fixture."""
    spec = importlib.util.spec_from_file_location("leakgate", SCRIPT)
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)

    monkey = base64.b64encode(b"A" * 4096).decode()
    gate.MAX_DECODE_BYTES = 64
    with pytest.raises(gate.ScanBudgetExceeded):
        gate.all_views(f"padding {monkey}")


def test_base64url_extraheader_is_caught(tmp_path):
    encoded = base64.urlsafe_b64encode(
        f"x-access-token:{FAKE_GHS}".encode()
    ).decode().rstrip("=")
    assert check(tmp_path, None, f"b64url: {encoded}") == 1


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


def test_prose_about_the_extraheader_passes(tmp_path, auth_file):
    """A review OF the persist-credentials fix names every string the gate
    looks for. `x-access-token:` is matched only after base64-decoding for
    exactly this reason — in plaintext it is vocabulary, not a credential."""
    reply = (
        "checkout persists the token as http.<url>.extraheader = "
        "'AUTHORIZATION: basic base64(x-access-token:<token>)'. Installation "
        "tokens use the ghs_ prefix, classic PATs ghp_, fine-grained "
        "github_pat_. Setting persist-credentials: false drops all of it."
    )
    assert check(tmp_path, auth_file, reply) == 0


def test_a_review_demonstrating_the_encoding_still_posts(tmp_path, auth_file):
    """The exact false positive an adversarial review of this gate produced:
    it explained the extraheader by showing a blob and its decoding. Because
    the job runs the gate under `set -euo pipefail`, a block here fails the
    step, posts nothing, and tells the author nothing — while still having
    reacted to the command. `x-access-token:` alone must not be a trigger."""
    demo = base64.b64encode(b"x-access-token:EXAMPLENOTAREALTOKEN").decode()
    reply = (
        "Finding (High): the marker is only matched AFTER base64 decoding. "
        f"Demonstration — the extraheader value encodes to {demo}, which "
        "decodes to x-access-token:EXAMPLENOTAREALTOKEN."
    )
    assert check(tmp_path, auth_file, reply) == 0


def test_prose_with_a_ghs_lookalike_word_is_not_a_leak(tmp_path, auth_file):
    """Whitespace stripping welds words together, so `ghs_` inside an ordinary
    word ("highs_") followed by twenty run-together letters would match a token
    shape. The delimiter lookbehind is what keeps this readable prose."""
    reply = (
        "The chart plots new highs_ and lows across the sampled window and "
        "then continues describing the distribution at length."
    )
    assert check(tmp_path, auth_file, reply) == 0


def test_the_gate_can_review_its_own_source(tmp_path, auth_file):
    """The gate must be able to pass a review OF ITSELF. Measured: with shape
    patterns applied to the alphabet-stripped view, the docstring phrase
    "GitHub only ever mints ghs_/ghp_ into that header" collapsed into a token
    shape and blocked. A gate that fires on every review of itself is a gate
    that gets switched off — the whole reason this file exists."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert check(tmp_path, auth_file, source) == 0


def test_ordinary_base64_and_hashes_do_not_trip_the_decode_pass(tmp_path, auth_file):
    """The decode pass rescans every base64-looking run, and reviews are full
    of them — digests, lockfile hashes, embedded assets. Decoded noise must
    not read as a credential or the gate becomes unusable."""
    reply = (
        "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 "
        "and integrity sha512-Gh1sVRVWnBFm9j8YQ9uYd0dCqtE0PJc7fSm0LmnAaZ0h "
        "plus data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
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


# ── tier honesty (SEC-G12) ─────────────────────────────────────────────────
# The success line used to name every tier unconditionally, so a run whose auth
# file was unreadable — value and encoding never executed — was indistinguishable
# from a full pass. `base64 -d` exits 0 on empty input, so an unset
# GROK_CI_AUTH_JSON reaches exactly that state via a successful-looking restore.


def _run(tmp_path: Path, auth: Path | None, reply: str, *flags: str):
    reply_path = tmp_path / "reply.md"
    reply_path.write_text(reply)
    args = [sys.executable, str(SCRIPT), str(reply_path)]
    if auth is not None:
        args.append(str(auth))
    args.extend(flags)
    return subprocess.run(args, capture_output=True, text=True)


def test_pass_message_names_only_the_checks_that_ran(tmp_path):
    out = _run(tmp_path, tmp_path / "absent.json", "an ordinary review").stdout
    assert "SKIPPED" in out and "value, encoding SKIPPED" in out
    assert "value, encoding, shape, decoded checks passed" not in out


def test_pass_message_claims_all_checks_only_when_all_ran(tmp_path, auth_file):
    out = _run(tmp_path, auth_file, "an ordinary review").stdout
    assert "value, encoding, shape, decoded checks passed" in out
    assert "SKIPPED" not in out


def test_unreadable_auth_warns_on_the_permissive_path(tmp_path):
    """Still exit 0 — a missing credential file is not evidence of a leak —
    but it must be visible, not silent."""
    res = _run(tmp_path, tmp_path / "absent.json", "an ordinary review")
    assert res.returncode == 0
    assert "::warning::" in res.stdout


def test_require_auth_fails_closed_when_the_auth_file_is_unreadable(tmp_path):
    res = _run(tmp_path, tmp_path / "absent.json", "an ordinary review",
               "--require-auth")
    assert res.returncode == 1
    assert "::error::" in res.stdout


def test_require_auth_fails_closed_on_an_empty_auth_file(tmp_path):
    """The exact shape `base64 -d` produces from an empty secret."""
    empty = tmp_path / "auth.json"
    empty.write_text("")
    res = _run(tmp_path, empty, "an ordinary review", "--require-auth")
    assert res.returncode == 1


def test_require_auth_still_passes_a_clean_reply_with_valid_auth(tmp_path, auth_file):
    res = _run(tmp_path, auth_file, "an ordinary review", "--require-auth")
    assert res.returncode == 0


def test_require_auth_still_blocks_a_leak(tmp_path, auth_file):
    res = _run(tmp_path, auth_file, f"leaked {FAKE_REFRESH}", "--require-auth")
    assert res.returncode == 1

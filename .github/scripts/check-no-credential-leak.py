#!/usr/bin/env python3
"""Fail closed if a Grok reply carries real credential material.

Usage: check-no-credential-leak.py <reply-file> [auth-json]

WHY THIS IS NOT A REGEX OVER SCARY WORDS
----------------------------------------
The first version of this gate matched `refresh_token|principal_id|"key":`
and promptly blocked a legitimate review — of the credential-handling PR
itself, where those words are the subject matter. A gate that fires on every
security review is a gate that gets deleted, so this compares against the
ACTUAL secret values instead of the vocabulary around them.

CHECKS
------
  1. VALUE MATCH — every sufficiently long string in auth.json, searched in
     the reply whole and as EVERY sliding window (step 1). An earlier version
     stepped by 8 and stopped at `len - WINDOW`, so it missed both unaligned
     quotes and — the common case for a JWT signature — the token's own tail.
  2. ENCODED MATCH — the same values re-encoded as base64 and hex, and the
     reply re-checked with whitespace removed, since "paste it base64'd" and
     "space it out" are the first things a determined injection tries.
  3. SHAPE MATCH — tokens that are credential-shaped regardless of
     provenance: a long JWT body (`eyJ...` with a dot) and the GitHub token
     families (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`, `github_pat_`). auth.json
     is NOT the only credential on the runner — actions/checkout can leave an
     installation token in .git/config, which the value check above would
     never see because it never appears in auth.json.
  4. DECODED MATCH — base64 and hex runs are decoded and rescanned for the
     same shapes, plus `x-access-token:` WHEN a token shape sits in the same
     decoded view. That string is the username half of the git credential
     checkout persists as `AUTHORIZATION: basic base64(x-access-token:<token>)`,
     so it only ever reads as a leak once DECODED. It is deliberately not
     matched in plaintext: a security review of this very file says it out
     loud, and blocking those reviews is exactly how the first version of this
     gate earned its rewrite. Requiring a token shape beside it extends that
     same reasoning one layer down — a review that DEMONSTRATES the encoding
     ("this blob decodes to x-access-token:<token>") must still get posted.

     Three lessons here are REPRODUCED bypasses of the version that shipped
     before this one, not hypotheticals:
       * A cap that TRUNCATES is a bypass. Stopping after N base64 runs meant
         padding the reply with junk runs first left the real blob unscanned.
         Budget overrun now fails CLOSED (ScanBudgetExceeded).
       * Separators that are not whitespace (commas, zero-width spaces) split a
         blob so that neither the run regex nor the whitespace-stripped variant
         sees it. Hence the alphabet-only decode source.
       * One decode is not enough — base64 of base64 walked through. Decoding
         now recurses while the output is still pure encoding alphabet.

RESIDUAL RISK, STATED PLAINLY: this cannot be complete. A model asked to
rot13, reverse, or describe a token in words will defeat any substring
matcher. Two residuals are deliberate rather than merely unhandled, and both
were measured:
  * A token chunked character-by-character in PLAINTEXT (`g,h,s,_,F,...`) is
    not SHAPE-matched, because the only view that would catch it also
    collapses ordinary prose into token-shaped strings and blocked a review of
    this very file. Chunked auth VALUES are still caught.
  * A separator containing DIGITS (a markdown numbered list) survives every
    collapse, because digits are valid base64 and stripping them would destroy
    the encoding the collapse exists to recover. Eleven other separator
    classes — including `-`, `_`, `=`, `+`, `/` and bullet lists — are caught. This gate is the last line, not the boundary — the boundary is that
child-process egress is blocked (verified on Linux) and the blast radius is
small (short-lived token, collaborator-only triggers, ephemeral runner).

WHAT A PASS MEANS: the success line names the checks that ACTUALLY ran. An
earlier version printed "value + encoding + shape checks passed" unconditionally,
including on runs where the auth file was unreadable and the value and encoding
checks never executed — a pass that overstated its own coverage (SEC-G12).
Production callers additionally pass --require-auth, which makes an unparseable
auth file a failure rather than a silent downgrade to shape-only.

Nothing secret is ever printed: findings are reported as a redacted prefix.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys

# Shorter than this and a "secret" is not distinctive enough to match on
# without false positives (short config values, ids that also appear in prose).
# Opt-in strictness for production callers — see main().
REQUIRE_AUTH_FLAG = "--require-auth"

MIN_SECRET_LEN = 20
# Any window this long is unmistakably token material rather than prose.
WINDOW = 24

# Credential SHAPES, independent of any local auth file. Each needs a literal
# prefix followed by token-length payload, so prose that merely names the
# format ("tokens start with ghp_") does not match.
#
# Two families of the SAME patterns. The anchored one requires the prefix to
# start at a delimiter; the raw one does not.
#
# Which is used depends on the variant, and getting this wrong has already cost
# a real detection once. On the untouched `reply`, prose keeps its spaces, so a
# review that says "the ghs_ prefix marks installation tokens" cannot match
# (a space follows the underscore) while a genuinely welded leak — an injection
# told to print the token with no separator, `...retrieved wasghs_AAAA...` — is
# caught. On variants WE created by joining text together, welding is an
# artifact of our own normalisation rather than the attacker's choice, and the
# anchored family is what stops the joined text of an ordinary review
# ("...reaching new highs_ and then twenty more characters...") from matching.
_START = r"(?<![A-Za-z0-9_])"
_SHAPES = (
    ("JWT-shaped token", r"eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{10,}"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("GitHub fine-grained PAT", r"github_pat_[A-Za-z0-9_]{50,}"),
    # Installation tokens are the ones this repo actually mints, and short
    # ones exist, so they get a looser bound than the family pattern above.
    ("GitHub installation token", r"ghs_[A-Za-z0-9]{20,}"),
)
SHAPE_PATTERNS = tuple((label, re.compile(_START + p)) for label, p in _SHAPES)
RAW_SHAPE_PATTERNS = tuple((label, re.compile(p)) for label, p in _SHAPES)

# The username half of the git credential `actions/checkout` persists. Only
# ever evaluated on DECODED bytes (see CHECKS 4), and only when a token shape
# sits in the same decoded view: on its own this string is vocabulary, and a
# review that DEMONSTRATES the encoding — "this blob decodes to
# x-access-token:<token>" — is a legitimate review, not a leak. Requiring a
# real token shape alongside it keeps the true positive (GitHub only ever
# mints ghs_/ghp_ into that header) without blocking the explanation of it.
DECODED_ONLY_MARKERS = ("x-access-token:",)

# A run shorter than this decodes to too few bytes to carry a token prefix.
MIN_B64_RUN = 24
MIN_HEX_RUN = 40
B64_RUN_RE = re.compile(r"[A-Za-z0-9+/_-]{%d,}" % MIN_B64_RUN)
HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{%d,}" % MIN_HEX_RUN)
_B64_URLSAFE = str.maketrans("-_", "+/")
# Everything outside this class is separator noise. Stripping it defeats
# "chunk the blob with commas / zero-width spaces", which neither the run
# regex nor the whitespace-stripped variant catches on its own.
_NOT_ENCODED_RE = re.compile(r"[^A-Za-z0-9+/=_-]+")

# Recursion depth for nested encodings (base64 of base64 of the credential).
# Deep enough that simply re-encoding N times is not a strategy; the byte
# budget below is what actually bounds the work.
MAX_DECODE_DEPTH = 8
# A decoded view is re-decoded when it is MOSTLY encoding alphabet. An earlier
# version required the whole view to be pure alphabet, which meant one interior
# byte ended the descent: base64("note: " + base64(cred) + " end") walked out.
MIN_ENCODED_DENSITY = 0.5
# Total decoded bytes the scan will produce before giving up. A review is a
# review, not a corpus; a reply that blows this budget is not something this
# gate can honestly certify, so exceeding it FAILS CLOSED rather than
# truncating. Truncation is what made the previous cap a bypass: an attacker
# emitted junk runs first and the real blob was simply never reached.
MAX_DECODE_BYTES = 8 << 20


class ScanBudgetExceeded(Exception):
    """The reply produced more decoded material than the gate will certify."""


def secret_values(auth: object) -> list[str]:
    """Every string in the auth document long enough to be secret material."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node) >= MIN_SECRET_LEN:
            found.append(node)

    walk(auth)
    return found


def encoded_forms(value: str) -> list[tuple[str, str]]:
    """(label, needle) pairs for common re-encodings of a secret."""
    raw = value.encode("utf-8", "replace")
    forms = [
        ("base64", base64.b64encode(raw).decode("ascii")),
        ("base64url", base64.urlsafe_b64encode(raw).decode("ascii")),
        ("hex", raw.hex()),
    ]
    # Only forms long enough to be distinctive are worth searching.
    return [(label, needle) for label, needle in forms if len(needle) >= WINDOW]


def contains(haystacks: dict[str, str], needle: str) -> str | None:
    """Name of the first reply variant containing `needle`, if any."""
    for variant, text in haystacks.items():
        if needle in text:
            return variant
    return None


def windows_hit(haystacks: dict[str, str], value: str) -> str | None:
    """First reply variant containing ANY window of `value`.

    Step 1 and an inclusive upper bound: a quote of the token's last 24
    characters, or one starting at an odd offset, must not slip through.
    """
    for start in range(0, len(value) - WINDOW + 1):
        variant = contains(haystacks, value[start:start + WINDOW])
        if variant:
            return variant
    return None


def decode_once(text: str) -> list[bytes]:
    """Every base64 and hex run in `text`, decoded at every plausible offset.

    Phase matters: a reply that quotes only PART of a blob starts mid-quantum,
    and an aligned-only decode reads that as noise. Four offsets for base64 and
    two for hex is cheap, and is the difference between catching a pasted
    extraheader and not.
    """
    out: list[bytes] = []
    for match in B64_RUN_RE.finditer(text):
        run = match.group(0).translate(_B64_URLSAFE)
        for phase in range(4):
            chunk = run[phase:]
            chunk = chunk[:len(chunk) // 4 * 4]
            if len(chunk) < MIN_B64_RUN:
                continue
            try:
                out.append(base64.b64decode(chunk, validate=True))
            except ValueError:  # binascii.Error subclasses ValueError
                continue
    for match in HEX_RUN_RE.finditer(text):
        run = match.group(0)
        for phase in (0, 1):
            chunk = run[phase:]
            chunk = chunk[:len(chunk) // 2 * 2]
            if len(chunk) < MIN_HEX_RUN:
                continue
            try:
                out.append(bytes.fromhex(chunk))
            except ValueError:
                continue
    return out


def decoded_views(text: str, budget: list[int]) -> list[str]:
    """Decoded views of `text`, following nested encodings to a bounded depth.

    `budget` is a one-element list of remaining bytes, shared across the whole
    scan. Running out raises rather than truncating — a scan that quietly stops
    early reports "clean" for material it never looked at.
    """
    views: list[str] = []
    frontier = [text]
    for _ in range(MAX_DECODE_DEPTH):
        nxt: list[str] = []
        for source in frontier:
            for raw in decode_once(source):
                budget[0] -= len(raw)
                if budget[0] < 0:
                    raise ScanBudgetExceeded
                # latin-1 never raises, and every marker we look for is ASCII.
                view = raw.decode("latin-1")
                views.append(view)
                # Recurse only into output that is itself pure encoding
                # alphabet: that is what base64-of-base64 looks like, and it
                # keeps the fan-out from exploding on ordinary garbage.
                stripped = view.strip()
                alpha = len(_NOT_ENCODED_RE.sub("", stripped))
                if (len(stripped) >= MIN_B64_RUN
                        and alpha >= MIN_ENCODED_DENSITY * len(stripped)
                        and alpha >= MIN_B64_RUN):
                    nxt.append(view)
        if not nxt:
            break
        frontier = nxt
    return views


def all_views(reply: str) -> dict[str, str]:
    """Every rendering of the reply that this gate is willing to certify.

    Four plaintext variants plus, for each, one concatenation of everything
    reachable by decoding it. Decoded material is concatenated rather than kept
    per-run so that the value scan below stays linear in the number of secrets
    instead of the number of runs; NUL separators keep a match from spanning
    two unrelated decodes.

    `runs-joined` is the answer to a blob split across ordinary prose. Stripping
    non-alphabet characters in place does not help there — English between the
    halves is itself alphabet characters and gets interleaved into the blob.
    Joining only the RUNS discards the prose, and no English word reaches
    MIN_B64_RUN, so nothing legitimate is ever welded together by it.
    """
    budget = [MAX_DECODE_BYTES]
    plaintext = {
        "reply": reply,
        "reply(no-whitespace)": re.sub(r"\s+", "", reply),
        # One collapse per alphabet. `alphabet-only` keeps -, _, =, + and /
        # because they are base64 alphabet, which means chunking the credential
        # WITH one of those characters survives it — and "put a dash between
        # every three characters", or a markdown bullet list, is as easy an
        # instruction as the comma this originally closed. Each narrower
        # collapse removes a different separator class.
        "reply(alphabet-only)": _NOT_ENCODED_RE.sub("", reply),
        "reply(b64std-only)": re.sub(r"[^A-Za-z0-9+/=]+", "", reply),
        "reply(b64url-only)": re.sub(r"[^A-Za-z0-9_=-]+", "", reply),
        "reply(alnum-only)": re.sub(r"[^A-Za-z0-9]+", "", reply),
        "reply(runs-joined)": "".join(m.group(0) for m in B64_RUN_RE.finditer(reply)),
    }
    views = dict(plaintext)
    for label, text in plaintext.items():
        decoded = decoded_views(text, budget)
        if decoded:
            views[f"decoded {label}"] = "\x00".join(decoded)
    return views


# `alphabet-only` keeps `/`, `+`, `=`, `-` and `_` as well as alphanumerics, so
# stripping an ordinary sentence welds words into token-shaped strings. Measured
# on THIS FILE: the docstring phrase "GitHub only ever mints ghs_/ghp_ into that
# header" collapses to ghp_ followed by 36 letters and matched. That view is
# therefore a decode source and a value-scan target only — never a shape target.
# The cost is that a token chunked character-by-character in PLAINTEXT is not
# caught; that is the documented residual, and it is worth paying to keep every
# review of this file postable.
_NO_SHAPE_SCAN = (
    "reply(alphabet-only)",
    "reply(b64std-only)",
    "reply(b64url-only)",
    "reply(alnum-only)",
)


def shape_hits(views: dict[str, str]) -> list[str]:
    """Shape findings across every view, with the right pattern family."""
    hits: list[str] = []
    for label, text in views.items():
        if label in _NO_SHAPE_SCAN:
            continue
        decoded = label.startswith("decoded ")
        # Un-anchored on the untouched reply and on decoded bytes; anchored on
        # the variants we produced by joining text. See _START for why.
        patterns = (RAW_SHAPE_PATTERNS
                    if decoded or label == "reply"
                    else SHAPE_PATTERNS)
        for name, pattern in patterns:
            if pattern.search(text):
                hits.append(f"{name} in {label}")
        if decoded:
            for marker in DECODED_ONLY_MARKERS:
                if marker in text and any(p.search(text) for _, p in patterns):
                    hits.append(f"{marker} with a token in {label}")
    return hits


def value_hits(views: dict[str, str], auth: object) -> list[str]:
    """Findings for the ACTUAL secret values in the local auth document.

    Runs over decoded views too. Restricting this to plaintext is what let a
    doubly-encoded refresh token through: `encoded_forms` only ever re-encoded
    the secret ONCE and compared that against the raw reply.
    """
    hits: list[str] = []
    for value in secret_values(auth):
        redacted = f"{value[:6]}…({len(value)} chars)"
        variant = contains(views, value)
        if variant:
            hits.append(f"full value {redacted} in {variant}")
            continue
        variant = windows_hit(views, value)
        if variant:
            hits.append(f"partial value {redacted} in {variant}")
            continue
        for label, needle in encoded_forms(value):
            variant = contains(views, needle)
            if variant:
                hits.append(f"{label}-encoded value {redacted} in {variant}")
                break
    return hits


def main() -> int:
    args = [a for a in sys.argv[1:] if a != REQUIRE_AUTH_FLAG]
    # Production callers pass --require-auth: for them an unreadable auth file
    # is a broken run, not an absent credential. It lives here rather than in
    # the workflow because this script is loaded from the default branch while
    # the workflow YAML comes from the merge ref — a PR can delete a step it
    # dislikes, but not this.
    require_auth = REQUIRE_AUTH_FLAG in sys.argv[1:]
    # After the filter, not before: `check-no-credential-leak.py --require-auth`
    # otherwise passed a `len(sys.argv) < 2` guard and died on an IndexError.
    if not args:
        print("::error::usage: check-no-credential-leak.py <reply> "
              f"[auth.json] [{REQUIRE_AUTH_FLAG}]")
        return 2
    reply_path = args[0]
    auth_path = args[1] if len(args) > 1 else os.path.expanduser(
        "~/.grok/auth.json"
    )

    try:
        with open(reply_path, encoding="utf-8", errors="replace") as handle:
            reply = handle.read()
    except OSError as exc:
        print(f"::error::Cannot read reply file {reply_path}: {exc}")
        return 1

    hits: list[str] = []

    try:
        with open(auth_path, encoding="utf-8") as handle:
            auth = json.load(handle)
        auth_error = None
    except (OSError, ValueError, RecursionError) as exc:
        # RecursionError included: a pathologically nested auth document must
        # produce the annotation below, not a traceback out of walk().
        # No auth file to compare against (or unreadable): the shape and
        # decoded checks below still run. A missing credential file is not by
        # itself evidence of a leak, so this is not a failure on its own —
        # but it IS two of four checks not running, and saying so is the whole
        # point of the tier reporting below.
        auth, auth_error = None, exc

    # SEC-G12 one layer down: `auth is not None` is not the same as "there was
    # something to compare against". An auth document of {}, [], null, 0, or one
    # whose strings are all shorter than MIN_SECRET_LEN yields no comparison
    # values at all — the value and encoding checks then iterate an empty list
    # and verify nothing, while the summary claimed both tiers ran. Coverage is
    # derived from the VALUES from here on, never from the file parsing.
    try:
        values = secret_values(auth) if auth is not None else []
    except RecursionError as exc:
        # Same reasoning as the parse above: walk() is unbounded recursion, and
        # a deep document must fail closed with an annotation, not a traceback.
        auth, auth_error, values = None, exc, []

    if not values and require_auth:
        # `base64 -d` exits 0 on empty input, so an empty or unset
        # GROK_CI_AUTH_JSON produces a successful-looking restore and an
        # unparseable auth file. Without this, that misconfiguration silently
        # downgraded the gate to shape-only and still reported a pass.
        if auth is None:
            # `null` parses fine, so distinguish it from an unreadable file
            # rather than printing "(None)" as if it were the error.
            why = f"could not be parsed ({auth_error})" if auth_error else "parsed as null"
        else:
            why = "parsed but yielded no comparison values"
        print(f"::error::Auth file {auth_path} {why}; refusing to certify a "
              "reply against a credential set this gate never loaded.")
        return 1

    try:
        views = all_views(reply)
        if values:
            hits.extend(value_hits(views, auth))
        hits.extend(shape_hits(views))
    except ScanBudgetExceeded:
        print("::error::Reply produced more decoded material than this gate "
              "will certify — refusing to post.")
        return 1

    if hits:
        print("::error::Credential material detected — refusing to post.")
        for hit in dict.fromkeys(hits):  # dedupe, keep first-seen order
            print(f"::error::  {hit}")
        return 1

    # Name the checks that ACTUALLY ran. The previous message claimed every
    # tier unconditionally, so a run with an unreadable auth file — where the
    # value and encoding checks never executed — was indistinguishable from a
    # full pass (SEC-G12).
    ran = ["shape", "decoded"]
    skipped = []
    if values:
        ran = ["value", "encoding", *ran]
    else:
        skipped = ["value", "encoding"]
        detail = f"unreadable ({auth_error})" if auth is None else "held no comparison values"
        print(f"::warning::Auth file {auth_path} {detail}; "
              f"the {' and '.join(skipped)} checks did NOT run.")
    summary = f"No credential material in reply ({', '.join(ran)} checks passed"
    summary += f"; {', '.join(skipped)} SKIPPED)." if skipped else ")."
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

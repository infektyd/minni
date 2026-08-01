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
  3. SHAPE MATCH — a long JWT body (`eyJ...` with a dot) is credential-shaped
     regardless of provenance, and prose has no reason to contain one.

RESIDUAL RISK, STATED PLAINLY: this cannot be complete. A model asked to
rot13, reverse, chunk, or describe a token in words will defeat any substring
matcher. This gate is the last line, not the boundary — the boundary is that
child-process egress is blocked (verified on Linux) and the blast radius is
small (short-lived token, collaborator-only triggers, ephemeral runner).

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
MIN_SECRET_LEN = 20
# Any window this long is unmistakably token material rather than prose.
WINDOW = 24


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


def main() -> int:
    if len(sys.argv) < 2:
        print("::error::usage: check-no-credential-leak.py <reply> [auth.json]")
        return 2

    reply_path = sys.argv[1]
    auth_path = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser(
        "~/.grok/auth.json"
    )

    try:
        with open(reply_path, encoding="utf-8", errors="replace") as handle:
            reply = handle.read()
    except OSError as exc:
        print(f"::error::Cannot read reply file {reply_path}: {exc}")
        return 1

    # Variants defeat the cheapest obfuscations: spacing a token out, or
    # wrapping it across lines.
    haystacks = {
        "reply": reply,
        "reply(no-whitespace)": re.sub(r"\s+", "", reply),
    }

    hits: list[str] = []

    try:
        with open(auth_path, encoding="utf-8") as handle:
            auth = json.load(handle)
    except (OSError, ValueError):
        # No auth file to compare against (or unreadable): the shape check
        # below still runs. Do not fail here — a missing credential file is
        # not evidence of a leak.
        auth = None

    if auth is not None:
        for value in secret_values(auth):
            redacted = f"{value[:6]}…({len(value)} chars)"
            variant = contains(haystacks, value)
            if variant:
                hits.append(f"full value {redacted} in {variant}")
                continue
            variant = windows_hit(haystacks, value)
            if variant:
                hits.append(f"partial value {redacted} in {variant}")
                continue
            for label, needle in encoded_forms(value):
                variant = contains(haystacks, needle)
                if variant:
                    hits.append(f"{label}-encoded value {redacted} in {variant}")
                    break

    # Credential SHAPE, independent of the local auth file: a long JWT body.
    if re.search(r"eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{10,}", reply):
        hits.append("JWT-shaped token in reply")

    if hits:
        print("::error::Credential material detected — refusing to post.")
        for hit in hits:
            print(f"::error::  {hit}")
        return 1

    print("No credential material in reply (value + encoding + shape checks passed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

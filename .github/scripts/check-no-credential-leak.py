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

Two checks:
  1. VALUE MATCH — every sufficiently long string inside auth.json, plus
     sliding windows of it, is searched for verbatim in the reply. This
     catches whole tokens and partial quotes alike.
  2. SHAPE MATCH — a long JWT body (`eyJ...` with a dot) is credential-shaped
     regardless of provenance, and prose has no reason to contain one.

Nothing secret is ever printed: findings are reported as a redacted prefix.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Shorter than this and a "secret" is not distinctive enough to match on
# without false positives (short config values, ids that also appear in prose).
MIN_SECRET_LEN = 20
# Window for partial quotes: long enough to be unmistakably token material.
WINDOW = 24
WINDOW_STEP = 8


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


def main() -> int:
    reply_path = sys.argv[1]
    auth_path = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser(
        "~/.grok/auth.json"
    )

    try:
        reply = open(reply_path, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"::error::Cannot read reply file {reply_path}: {exc}")
        return 1

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
            if value in reply:
                hits.append(f"full value {redacted}")
                continue
            for start in range(0, max(1, len(value) - WINDOW), WINDOW_STEP):
                if value[start:start + WINDOW] in reply:
                    hits.append(f"partial value {redacted}")
                    break

    # Credential SHAPE, independent of the local auth file: a long JWT body.
    if re.search(r"eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{10,}", reply):
        hits.append("JWT-shaped token in reply")

    if hits:
        print("::error::Credential material detected — refusing to post.")
        for hit in hits:
            print(f"::error::  {hit}")
        return 1

    print("No credential material in reply (value + shape checks passed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Parse a machine VERDICT line from a Grok review reply (fail-closed).

Usage:
  parse_grok_verdict.py <reply-file>           # prints: <event>\\t<note>
  parse_grok_verdict.py --event-only <file>   # prints event only

v1 policy (Design Crucible 2026-08-01): the GitHub App may post
REQUEST_CHANGES or COMMENT review events. A model line of VERDICT: APPROVE
is *downgraded* to COMMENT — LLM output must never mint merge trust.

Accepted last-line forms (case-sensitive enum after the colon). Only the
last non-empty line of the reply is parsed — mid-body / echoed VERDICT
lines are ignored (matches the review prompt contract):
  VERDICT: REQUEST_CHANGES
  VERDICT: COMMENT
  VERDICT: APPROVE          # → event COMMENT + downgrade note

Anything else (missing, trailing prose after VERDICT, prose "LGTM",
substring "approve") → COMMENT. Never APPROVE as the Reviews API event.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Full-line match only (no MULTILINE scan of the whole body).
VERDICT_RE = re.compile(
    r"^VERDICT:\s*(REQUEST_CHANGES|COMMENT|APPROVE)\s*$",
)

# Reviews API `event` values we will actually send in v1.
ALLOWED_EVENTS = frozenset({"REQUEST_CHANGES", "COMMENT"})


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return ""


def parse_verdict(text: str) -> tuple[str, str]:
    """Return (reviews_api_event, note). event is never APPROVE."""
    last = _last_nonempty_line(text)
    m = VERDICT_RE.match(last)
    if not m:
        return "COMMENT", "no VERDICT line; defaulted to COMMENT"
    raw = m.group(1)
    if raw == "APPROVE":
        return (
            "COMMENT",
            "VERDICT: APPROVE downgraded to COMMENT (v1: LLM cannot APPROVE)",
        )
    if raw in ALLOWED_EVENTS:
        return raw, f"VERDICT: {raw}"
    return "COMMENT", f"unknown VERDICT {raw!r}; defaulted to COMMENT"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("reply_file", type=Path)
    p.add_argument(
        "--event-only",
        action="store_true",
        help="Print only the Reviews API event token",
    )
    args = p.parse_args(argv)
    text = args.reply_file.read_text(encoding="utf-8", errors="replace")
    event, note = parse_verdict(text)
    if args.event_only:
        print(event)
    else:
        print(f"{event}\t{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

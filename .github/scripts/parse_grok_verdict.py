#!/usr/bin/env python3
"""Parse a machine VERDICT line from a Grok review reply (fail-closed).

Usage:
  parse_grok_verdict.py <reply-file>                # prints: <event>\\t<note>
  parse_grok_verdict.py --event-only <file>         # prints event only
  parse_grok_verdict.py --allow-approve <file>      # may emit APPROVE (gate only)

Default (v1 callers / grok-review post path): Reviews API events are only
REQUEST_CHANGES or COMMENT. VERDICT: APPROVE is downgraded to COMMENT — the
LLM must never mint merge trust via the Reviews API.

v2 mechanical merge trust is a *required check run* (`grok-mechanical-approve`),
not App APPROVE (measured 2026-08-01: bot APPROVE does not clear
reviewDecision on this user-owned repo). The gate may call --allow-approve
when reading a stored reply; grok-review still posts COMMENT and stamps an
eligibility marker when the raw line was APPROVE.

Accepted last-line forms (case-sensitive enum after the colon). Only the
last non-empty line of the reply is parsed — mid-body / echoed VERDICT
lines are ignored (matches the review prompt contract):
  VERDICT: REQUEST_CHANGES
  VERDICT: COMMENT
  VERDICT: APPROVE          # → COMMENT unless --allow-approve
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

# Reviews API `event` values safe for the default (no --allow-approve) path.
DEFAULT_EVENTS = frozenset({"REQUEST_CHANGES", "COMMENT"})


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return ""


def parse_verdict(text: str, *, allow_approve: bool = False) -> tuple[str, str]:
    """Return (event_token, note).

    event_token is never APPROVE unless allow_approve=True.
    """
    last = _last_nonempty_line(text)
    m = VERDICT_RE.match(last)
    if not m:
        return "COMMENT", "no VERDICT line; defaulted to COMMENT"
    raw = m.group(1)
    if raw == "APPROVE":
        if allow_approve:
            return "APPROVE", "VERDICT: APPROVE"
        return (
            "COMMENT",
            "VERDICT: APPROVE downgraded to COMMENT (Reviews API; use check-run gate)",
        )
    if raw in DEFAULT_EVENTS:
        return raw, f"VERDICT: {raw}"
    return "COMMENT", f"unknown VERDICT {raw!r}; defaulted to COMMENT"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("reply_file", type=Path)
    p.add_argument(
        "--event-only",
        action="store_true",
        help="Print only the event token",
    )
    p.add_argument(
        "--allow-approve",
        action="store_true",
        help="Permit APPROVE token (mechanical gate / eligibility readers only)",
    )
    args = p.parse_args(argv)
    text = args.reply_file.read_text(encoding="utf-8", errors="replace")
    event, note = parse_verdict(text, allow_approve=args.allow_approve)
    if args.event_only:
        print(event)
    else:
        print(f"{event}\t{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

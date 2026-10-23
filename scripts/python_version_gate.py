#!/usr/bin/env python3
"""Exit 0 when the running interpreter matches the repo's .python-version.

`make setup` calls this instead of an inline `sys.version_info >= (3, 14)`.
A minimum also admitted 3.15, and a system python3 of 3.15 then built a venv
on which the locked wheels cannot install. The pin is exact on major.minor.

Exit codes: 0 match, 1 mismatch, 2 unreadable or malformed .python-version.
"""

from __future__ import annotations

import sys
from pathlib import Path


def pinned_version(path: Path) -> tuple[int, int]:
    """Parse `<major>.<minor>[.<patch>]` from a .python-version file."""
    text = path.read_text(encoding="utf-8").strip()
    parts = text.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"unexpected .python-version contents: {text!r}")
    return int(parts[0]), int(parts[1])


def accepts(version_info, pinned: tuple[int, int]) -> bool:
    return tuple(version_info[:2]) == pinned


def main() -> int:
    pin_file = Path(__file__).resolve().parents[1] / ".python-version"
    try:
        pinned = pinned_version(pin_file)
    except (OSError, ValueError) as exc:
        print(f"python version gate: {exc}", file=sys.stderr)
        return 2
    return 0 if accepts(sys.version_info, pinned) else 1


if __name__ == "__main__":
    sys.exit(main())

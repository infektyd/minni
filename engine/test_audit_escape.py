"""SEC-014 audit-log injection tests.

A forged ``summary`` containing ``\\n## [...]`` must not be split into
multiple audit entries on the Python side, and the literal escaped
``\\n## `` sequence must appear verbatim in the written log body.
"""

from __future__ import annotations

import re
from pathlib import Path

from sovrd import _append_handoff_audit, _escape_audit_field


def test_summary_newline_is_escaped(tmp_path: Path) -> None:
    forged = "x\n## [2099-01-01] sovereign_learn | injected"
    _append_handoff_audit(tmp_path, "sovereign_learn", forged, {"k": "v"})

    log_text = (tmp_path / "log.md").read_text(encoding="utf-8")

    # Header lines starting with `## [` — exactly one legitimate entry.
    headers = re.findall(r"(?m)^## \[", log_text)
    assert len(headers) == 1, f"expected one audit header, got {len(headers)}: {log_text!r}"

    # Literal escaped sequence must appear in the body.
    assert "\\n## " in log_text, "escaped \\n## sequence should appear verbatim"

    # Forged header must not appear at the start of a line.
    assert not re.search(r"(?m)^## \[2099-01-01\]", log_text), \
        "forged `## [2099-...]` heading must not appear as a heading"


def test_tool_newline_is_escaped(tmp_path: Path) -> None:
    _append_handoff_audit(
        tmp_path,
        "sovereign_learn\n## [2099-01-01] forged | bad",
        "ok",
        {},
    )
    log_text = (tmp_path / "log.md").read_text(encoding="utf-8")
    headers = re.findall(r"(?m)^## \[", log_text)
    assert len(headers) == 1
    assert "\\n## " in log_text
    assert not re.search(r"(?m)^## \[2099-01-01\]", log_text)


def test_summary_capped_to_500_chars_with_ellipsis(tmp_path: Path) -> None:
    huge = "a" * 2000
    _append_handoff_audit(tmp_path, "sovereign_learn", huge, {})
    log_text = (tmp_path / "log.md").read_text(encoding="utf-8")
    header_line = next(ln for ln in log_text.splitlines() if ln.startswith("## ["))
    after_pipe = header_line.split("| ", 1)[1]
    assert len(after_pipe) == 500, f"summary must be capped to 500 chars, got {len(after_pipe)}"
    assert after_pipe.endswith("…")


def test_escape_audit_field_inline_escapes_leading_hash() -> None:
    out = _escape_audit_field("## fake", mode="inline")
    assert out.startswith("\\##"), out


def test_escape_audit_field_inline_escapes_carriage_return_and_newline() -> None:
    out = _escape_audit_field("a\r\nb", mode="inline")
    assert out == "a\\r\\nb"

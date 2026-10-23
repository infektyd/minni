"""Harness-envelope TEXT rules: the Python mirror of the prompt classifier.

Mirrors ``plugins/minni/src/harness-envelope-rules.ts`` (constants) and
``plugins/minni/src/harness-envelope.ts`` (``stripHarnessBlocks`` and
``classifyPromptEnvelope``), per recall-to-use rev5, 5.2.1. Host-injected text
(system reminders, task notifications, relayed messages) arrives on the same
hook as what a human typed; these rules tell them apart.

Only the text rules live here, with the same ``rule_id`` values. The scheduled
template rule is TypeScript only: it reads a counts file under the vault's
``.runtime`` that the daemon never sees.

There is no runtime-shared data file (a bare JSON import fails typecheck under
NodeNext, and a ``dist/data/`` file would fail ``stage_payload.py``). Parity is
held by one shared case file, ``tests/fixtures/harness_envelope_cases.json``,
which ``tests/test_harness_envelope_parity.py`` and
``plugins/minni/tests/harness-envelope-parity.test.mjs`` both run. Each side
also asserts that every one of its ``rule_id``s has a case, so a rule added on
one side only fails a gate. Precedent: ``inbox_ingest.is_audit_echo``.

Rule order matters (first match wins) and matching is case sensitive. Every
character class is spelled out, never ``\\s`` or a bare ``strip()``, because the
two languages disagree about what those cover. The only known divergence left is
that the length bounds below count UTF-16 units in JS and code points here, so
they differ only for astral characters inside an over-long tag attribute run.

Record-only: nothing calls the classifier yet.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

RULES_VERSION = 1

#: Characters trimmed from both ends of every stripped prompt.
TRIM_CHARS = " \t\r\n\f\v"

#: Blocks stripped anywhere in the text, nesting included. A block that is
#: never closed, and a stray close tag, are left alone.
BLOCK_TAGS: Tuple[Dict[str, str], ...] = (
    {"rule_id": "block.system_reminder", "tag": "system-reminder"},
)

#: Prompts whose stripped text STARTS with ``prefix`` are machine output. The
#: prefixes are open ended on the right on purpose.
SKIP_RULES: Tuple[Dict[str, str], ...] = (
    {"rule_id": "skip.task_notification", "prefix": "<task-notification", "subkind": "task_notification"},
    {"rule_id": "skip.side_sessions_event", "prefix": "<side-sessions-event>", "subkind": "side_sessions_event"},
    {"rule_id": "skip.ci_monitor_event", "prefix": "<ci-monitor-event>", "subkind": "ci_monitor_event"},
    {"rule_id": "skip.command_name", "prefix": "<command-name>", "subkind": "slash_command"},
    {"rule_id": "skip.command_message", "prefix": "<command-message>", "subkind": "slash_command"},
    {"rule_id": "skip.local_command_stdout", "prefix": "<local-command-stdout>", "subkind": "local_command_output"},
    {"rule_id": "skip.local_command_caveat", "prefix": "<local-command-caveat>", "subkind": "local_command_output"},
    {"rule_id": "skip.bash_input", "prefix": "<bash-input>", "subkind": "human_shell"},
)

#: Wrappers are stripped and the body is kept. Tried at the start of the text
#: and repeated (at most ``MAX_WRAPPER_DEPTH`` passes); the first rule that
#: matched names the result.
#:   tag_pair   ``<tag ...>`` inner ``</tag>``; a missing close tag keeps the rest
#:   lead_line  the whole first line is dropped
#:   header     the bracketed ``[Prefix ...]`` part is dropped, the rest of its
#:              line stays; no ``]`` on the first line means it is not a header
WRAPPER_RULES: Tuple[Dict[str, str], ...] = (
    {
        "rule_id": "wrap.cross_session_message",
        "form": "tag_pair",
        "tag": "cross-session-message",
        "subkind": "cross_session_message",
    },
    {
        "rule_id": "wrap.another_session_lead",
        "form": "lead_line",
        "prefix": "Another Claude session sent a message",
        "subkind": "cross_session_lead",
    },
    {
        "rule_id": "wrap.workflow_harness_header",
        "form": "header",
        "prefix": "[Workflow harness",
        "subkind": "workflow_harness",
    },
    {
        "rule_id": "wrap.artifact_comment_header",
        "form": "header",
        "prefix": "[Artifact comment",
        "subkind": "artifact_comment",
    },
)

MAX_WRAPPER_DEPTH = 4
MAX_OPEN_TAG_CHARS = 512
MAX_BLOCK_OPEN_TAG_CHARS = 256
MAX_HEADER_CHARS = 256

#: A ``human`` text that starts like harness output but matched no rule is
#: flagged ``suspicious_prefix``. It only flags; it never changes the class.
SUSPICIOUS_PREFIX_PATTERN = r"^(?:<[A-Za-z]|\[[A-Z])"
_SUSPICIOUS_PREFIX = re.compile(SUSPICIOUS_PREFIX_PATTERN)


def rule_ids() -> List[str]:
    """Every rule id, in rule order (the same set the TypeScript constants name)."""
    return (
        [r["rule_id"] for r in BLOCK_TAGS]
        + [r["rule_id"] for r in SKIP_RULES]
        + [r["rule_id"] for r in WRAPPER_RULES]
    )


def _strip_block_tag(text: str, tag: str) -> Tuple[str, bool]:
    """Remove every complete ``<tag ...>...</tag>`` block, nesting included."""
    pattern = re.compile(
        r"<(/?)"
        + re.escape(tag)
        + r"(?=[ \t\r\n>])[^>]{0,"
        + str(MAX_BLOCK_OPEN_TAG_CHARS)
        + r"}>"
    )
    depth = 0
    block_start = 0
    last = 0
    stripped = False
    out: List[str] = []
    for m in pattern.finditer(text):
        closing = m.group(1) == "/"
        if depth == 0:
            if closing:
                continue
            depth = 1
            block_start = m.start()
        elif closing:
            depth -= 1
            if depth == 0:
                out.append(text[last:block_start])
                last = m.end()
                stripped = True
        else:
            depth += 1
    out.append(text[last:])
    return "".join(out), stripped


def _strip_blocks_detailed(text: Any) -> Tuple[str, List[str]]:
    current = text if isinstance(text, str) else ""
    fired: List[str] = []
    for rule in BLOCK_TAGS:
        current, stripped = _strip_block_tag(current, rule["tag"])
        if stripped:
            fired.append(rule["rule_id"])
    return current.strip(TRIM_CHARS), fired


def strip_harness_blocks(text: Any) -> str:
    """Remove harness blocks (such as ``system-reminder``) anywhere, then trim."""
    return _strip_blocks_detailed(text)[0]


def _unwrap(rule: Dict[str, str], text: str) -> Optional[str]:
    """One wrapper rule applied to the start of ``text``: the body, or None."""
    form = rule["form"]
    if form == "tag_pair":
        open_tag = "<" + rule["tag"]
        if not text.startswith(open_tag):
            return None
        nxt = text[len(open_tag) : len(open_tag) + 1]
        if nxt == ">":
            open_end = len(open_tag) + 1
        elif nxt != "" and nxt in TRIM_CHARS:
            gt = text.find(">", len(open_tag))
            if gt < 0 or gt - len(open_tag) > MAX_OPEN_TAG_CHARS:
                return None
            open_end = gt + 1
        else:
            return None
        rest = text[open_end:]
        close_tag = "</" + rule["tag"] + ">"
        close_at = rest.find(close_tag)
        if close_at < 0:
            return rest
        return rest[:close_at] + rest[close_at + len(close_tag) :]
    if form == "lead_line":
        if not text.startswith(rule["prefix"]):
            return None
        newline = text.find("\n")
        return "" if newline < 0 else text[newline + 1 :]
    if form == "header":
        prefix = rule["prefix"]
        if not text.startswith(prefix):
            return None
        close = text.find("]", len(prefix))
        newline = text.find("\n")
        if close < 0:
            return None
        if newline >= 0 and close > newline:
            return None
        if close - len(prefix) > MAX_HEADER_CHARS:
            return None
        return text[close + 1 :]
    return None


def classify_prompt_envelope(text: Any) -> Dict[str, Any]:
    """Classify one prompt by the text rules.

    Returns ``{kind, subkind, body}`` plus ``rule_id`` when a rule decided (for
    ``human``: the block rule, when a block was removed; a skip or wrapper rule
    outranks it) and ``suspicious_prefix: True`` when it applies. ``kind`` is
    ``human`` (subkind ``typed`` or ``empty``), ``skip`` or ``wrapped``. The
    ``scheduled`` kind needs the template rule, which is TypeScript only.
    """
    stripped, fired = _strip_blocks_detailed(text)
    block_rule_id = fired[0] if fired else None

    if stripped == "":
        result: Dict[str, Any] = {"kind": "human", "subkind": "empty", "body": ""}
        if block_rule_id is not None:
            result["rule_id"] = block_rule_id
        return result

    for rule in SKIP_RULES:
        if stripped.startswith(rule["prefix"]):
            return {
                "kind": "skip",
                "subkind": rule["subkind"],
                "body": stripped,
                "rule_id": rule["rule_id"],
            }

    current = stripped
    outermost: Optional[Dict[str, str]] = None
    for _ in range(MAX_WRAPPER_DEPTH):
        matched = False
        for rule in WRAPPER_RULES:
            body = _unwrap(rule, current)
            if body is None:
                continue
            if outermost is None:
                outermost = rule
            current = body.strip(TRIM_CHARS)
            matched = True
            break
        if not matched:
            break
    if outermost is not None:
        return {
            "kind": "wrapped",
            "subkind": outermost["subkind"],
            "body": current,
            "rule_id": outermost["rule_id"],
        }

    result = {"kind": "human", "subkind": "typed", "body": stripped}
    if block_rule_id is not None:
        result["rule_id"] = block_rule_id
    if _SUSPICIOUS_PREFIX.match(stripped):
        result["suspicious_prefix"] = True
    return result

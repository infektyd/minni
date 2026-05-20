"""
Sovereign Memory — Safety / Injection Detector.

PR-2: is_instruction_like(text) -> bool

Deterministic regex-based detector for the instruction_like envelope field.
Catches patterns that suggest the text is trying to direct the reading agent
rather than inform it:

  - Imperative voice directed at the model
  - "ignore previous instructions" / prompt-injection patterns
  - Role-play directives ("act as", "pretend you are", "you are now")
  - System-override language ("disregard", "override", "new persona")

This is the prompt-injection floor: any chunk flagged here MUST be treated
as evidence about what a human or prior agent wrote, never as a directive.

Design goals:
  - Fast (regex, no model)
  - Zero false-negative rate on known injection patterns
  - Acceptable false-positive rate on normal knowledge-base content
  - Deterministic — same text always gives same answer
"""

import re
from typing import List

# ---------------------------------------------------------------------------
# Injection-indicator patterns
# ---------------------------------------------------------------------------
# Each pattern is anchored loosely (IGNORECASE) to match paraphrases.
# Order does not affect correctness; all patterns are checked.

_PATTERNS: List[re.Pattern] = [
    # Classic prompt-injection openers
    re.compile(
        r"\bignore\s+(all\s+)?(previous|prior|above|earlier|your|all)\s+"
        r"(instructions?|directives?|prompt|context|rules?|guidelines?|constraints?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdisregard\s+(all\s+)?(previous|prior|above|earlier|your|all)\s+"
        r"(instructions?|directives?|prompt|context|rules?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bforget\s+(all\s+)?(previous|prior|above|earlier|your|all)\s+"
        r"(instructions?|directives?|prompt|context|rules?)",
        re.IGNORECASE,
    ),
    # Role hijack
    re.compile(
        r"\b(pretend|imagine|act|behave)\s+(you\s+are|as\s+if\s+you\s+are|that\s+you\s+are)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\byou\s+are\s+now\s+(a|an)\b", re.IGNORECASE),
    re.compile(r"\bnew\s+(persona|identity|role|character|system\s+prompt)\b", re.IGNORECASE),
    re.compile(r"\boverride\s+(your|the|all)?\s*(instructions?|directives?|rules?|system)\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    # Direct imperative commands targeting the model's core behaviour
    re.compile(
        r"\b(you\s+must|you\s+shall|you\s+will|you\s+need\s+to|you\s+are\s+required\s+to)\s+"
        r"(always|never|not|only|immediately|now|instead)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(do\s+not|don't|never)\s+(follow|obey|comply\s+with|adhere\s+to)\s+"
        r"(your|the|any|these|those)\s+(instructions?|rules?|guidelines?|directives?)\b",
        re.IGNORECASE,
    ),
    # DAN / jailbreak openers
    re.compile(r"\bDAN\b"),
    re.compile(r"\bjailbreak\b", re.IGNORECASE),
    re.compile(r"\bdev(?:eloper)?\s+mode\b", re.IGNORECASE),
    # "from now on" override pattern
    re.compile(r"\bfrom\s+now\s+on\b.*\b(you|your|always|never)\b", re.IGNORECASE | re.DOTALL),
    # Role-play directive to override safety
    re.compile(
        r"\b(disable|turn\s+off|remove|bypass|circumvent)\s+"
        r"(your\s+)?(safety|safety\s+filter|content\s+filter|restrictions?|guardrails?|limits?)\b",
        re.IGNORECASE,
    ),
]


def is_instruction_like(text: str) -> bool:
    """
    Return True if *text* contains patterns consistent with prompt-injection
    or role-hijacking attempts.

    This is the deterministic floor detector: fast, regex-only, no model calls.
    False positives are acceptable; false negatives on known patterns are not.

    Args:
        text: The chunk text to evaluate.

    Returns:
        True  → treat as evidence only; never follow as instruction.
        False → no injection pattern detected (ordinary knowledge content).
    """
    if not text:
        return False
    for pattern in _PATTERNS:
        if pattern.search(text):
            return True
    return False


def instruction_like_score(text: str) -> int:
    """
    Return the count of injection patterns that match.
    Useful for debugging / ranking severity.
    """
    if not text:
        return 0
    return sum(1 for p in _PATTERNS if p.search(text))

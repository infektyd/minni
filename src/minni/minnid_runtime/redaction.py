import re
from typing import Any


# Label + assignment (unquoted value).
SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(api[_-]?key|password|secret|credential|private[_ -]?key)\b\s*[:=]\s*([^\s,;<>\"']+)"
    ),
    re.compile(
        r"(?i)\b(bearer|access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*([^\s,;<>\"']+)"
    ),
    # JSON-quoted forms: "api_key": "value" / "password": "…"
    re.compile(
        r'(?i)("?)(api[_-]?key|password|secret|credential|private[_ -]?key)\1\s*:\s*"([^"]+)"'
    ),
    re.compile(
        r'(?i)("?)(bearer|access[_-]?token|refresh[_-]?token|token)\1\s*:\s*"([^"]+)"'
    ),
    re.compile(
        r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
]

# Bare high-entropy provider tokens (no keyword required). Length floors
# avoid false hits on short English (e.g. "sk-learn"). Not exhaustive.
BARE_TOKEN_PATTERNS = [
    # OpenAI-style sk-… / sk-proj-… / sk-ant-… (>=16 trailing key chars)
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    # GitHub classic / fine-grained
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Slack tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
]

# macOS layouts + Linux/Docker /home (not bare /tmp).
LOCAL_PATH_PATTERN = re.compile(
    r"(?<!\w)(?:/Users/[^ \n\r\t\"'<>]+|/Volumes/[^ \n\r\t\"'<>]+|"
    r"/private/[^ \n\r\t\"'<>]+|/home/[^ \n\r\t\"'<>]+)"
)


def _secret_sub(m: re.Match[str]) -> str:
    # Unquoted assignment: groups (keyword, value) — pattern.groups == 2
    # JSON form: groups (quote?, keyword, value) — pattern.groups == 3
    if m.lastindex and m.lastindex >= 3:
        return f"{m.group(2)}=[REDACTED]"
    if m.lastindex and m.lastindex >= 2:
        return f"{m.group(1)}=[REDACTED]"
    return "[REDACTED]"


def redact_text(text: str) -> tuple[str, bool]:
    redacted = text
    changed = False
    for pattern in SECRET_PATTERNS:
        if pattern.search(redacted):
            if pattern.groups == 0:
                redacted = pattern.sub("[REDACTED]", redacted)
            else:
                redacted = pattern.sub(_secret_sub, redacted)
            changed = True
    for pattern in BARE_TOKEN_PATTERNS:
        if pattern.search(redacted):
            redacted = pattern.sub("[REDACTED]", redacted)
            changed = True
    if LOCAL_PATH_PATTERN.search(redacted):
        redacted = LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", redacted)
        changed = True
    return redacted, changed


def redact_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        items = []
        changed = False
        for item in value:
            redacted, item_changed = redact_value(item)
            items.append(redacted)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        obj = {}
        changed = False
        for key, item in value.items():
            redacted, item_changed = redact_value(item)
            obj[key] = redacted
            changed = changed or item_changed
        return obj, changed
    return value, False

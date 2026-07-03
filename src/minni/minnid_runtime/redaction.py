import re
from typing import Any


SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|password|secret|credential|private[_ -]?key)\b\s*[:=]\s*([^\s,;<>\"']+)"),
    re.compile(r"(?i)\b(bearer|access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*([^\s,;<>\"']+)"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]
LOCAL_PATH_PATTERN = re.compile(r"(?<!\w)(?:/Users/[^ \n\r\t\"'<>]+|/Volumes/[^ \n\r\t\"'<>]+|/private/[^ \n\r\t\"'<>]+)")


def redact_text(text: str) -> tuple[str, bool]:
    redacted = text
    changed = False
    for pattern in SECRET_PATTERNS:
        if pattern.search(redacted):
            if pattern.groups >= 2:
                redacted = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", redacted)
            else:
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

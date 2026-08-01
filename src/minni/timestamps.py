"""
Minni — epoch timestamp coercion and defensive parsing.

Audit R0. One `documents` row (an identity envelope) carried `indexed_at` AND
`last_modified` as the TEXT ``'2026-06-19T22:55:32.509Z'`` instead of a REAL
epoch. SQLite's REAL *affinity* does not reject a non-numeric string, so the
poison sits in the column silently until a reader does ``float()`` or
arithmetic on it — at which point a SINGLE bad row takes down the WHOLE
operation:

  - ``retrieval._filter_candidates`` raises ValueError, which propagates out of
    ``handle_search`` and aborts the entire recall with -32000;
  - ``decay.run_decay`` raises TypeError inside its transaction and aborts the
    entire decay pass.

Two sides, both required:

  write side
      ``coerce_epoch`` — a caller can never hand a non-numeric value to the DB.
      Backed by normalizing triggers (migration 016) so writers *outside* this
      tree cannot poison the column either.

  read side
      ``parse_epoch_or_report`` — a bad row is skipped instead of fatal, and is
      COUNTED and LOGGED so the skip stays visible in the health surface. The
      standing constraint is that we never fix a signal by suppressing it: a
      skipped row must be observable, not merely tolerated.

Naive (tz-less) ISO strings are read as UTC, which matches how SQLite's
``strftime('%s', ...)`` interprets them in migration 016. The two sides must
agree or a repaired row would not equal a re-parsed one.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("sovereign.timestamps")

# Malformed values seen on the READ path this process, keyed by "source.field".
# Surfaced through malformed_timestamp_report() into the daemon health report.
_malformed_lock = threading.Lock()
_malformed_counts: Dict[str, int] = {}
_malformed_examples: Dict[str, list] = {}

# Cap the per-key example list: a systematically poisoned column must not grow
# an unbounded in-process list just because it is being reported.
_MAX_EXAMPLES = 10


def parse_epoch(value: Any) -> Optional[float]:
    """
    Best-effort conversion of *value* to a float epoch, or None.

    Accepts: int/float (not bool), numeric strings, and ISO-8601 strings
    (including the trailing-``Z`` form SQLite writes). Returns None for
    anything else, for NaN/inf, and for None itself. Never raises.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return None if math.isnan(result) or math.isinf(result) else result
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        result = float(text)
        return None if math.isnan(result) or math.isinf(result) else result
    except ValueError:
        pass

    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Match migration 016 / SQLite strftime('%s', ...): naive means UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def coerce_epoch(
    value: Any,
    *,
    field: str,
    default: Optional[float] = None,
    context: Optional[str] = None,
) -> Optional[float]:
    """
    WRITE-path guard: return a float epoch that is safe to store, or *default*.

    A value that is already numeric passes through untouched. A recoverable
    value (numeric string, ISO-8601 string) is converted and logged at WARNING
    so the offending caller is identifiable. An unrecoverable value falls back
    to *default* — also at WARNING — rather than being written through.

    None in, None out: NULL is a legal stored value and callers rely on it
    (``COALESCE(indexed_at, last_modified, 0)``).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        if not (math.isnan(result) or math.isinf(result)):
            return result

    parsed = parse_epoch(value)
    where = f" ({context})" if context else ""
    if parsed is None:
        logger.warning(
            "Refusing to store non-numeric %s=%r%s — falling back to %r",
            field, value, where, default,
        )
        return default
    logger.warning(
        "Coerced non-numeric %s=%r%s to epoch %r before storing",
        field, value, where, parsed,
    )
    return parsed


def parse_epoch_or_report(
    value: Any,
    *,
    field: str,
    source: str,
    doc_id: Any = None,
) -> Optional[float]:
    """
    READ-path guard: return a float epoch, or None when *value* is unusable.

    Returning None lets the caller skip one row instead of aborting the whole
    query. The skip is NOT silent: it is logged at WARNING with the row id and
    counted into malformed_timestamp_report() so it stays visible in health.
    """
    if value is None:
        return None
    parsed = parse_epoch(value)
    if parsed is not None:
        return parsed
    _record_malformed(source=source, field=field, doc_id=doc_id, value=value)
    return None


def _record_malformed(*, source: str, field: str, doc_id: Any, value: Any) -> None:
    key = f"{source}.{field}"
    with _malformed_lock:
        _malformed_counts[key] = _malformed_counts.get(key, 0) + 1
        examples = _malformed_examples.setdefault(key, [])
        if len(examples) < _MAX_EXAMPLES:
            examples.append({"doc_id": doc_id, "value": repr(value)[:120]})
        count = _malformed_counts[key]
    logger.warning(
        "Skipping malformed %s on row %r (value=%r); %d skipped so far in %s. "
        "Run migration 016 to normalize stored timestamps.",
        field, doc_id, value, count, source,
    )


# The REAL-affinity timestamp columns on `documents`. SQLite stores whatever a
# writer hands these, so "is it actually numeric?" is a per-row question.
TIMESTAMP_COLUMNS = ("indexed_at", "last_modified", "last_accessed")

_STORED_MALFORMED_SQL = "SELECT COUNT(*) AS n FROM documents WHERE " + " OR ".join(
    f"({col} IS NOT NULL AND typeof({col}) NOT IN ('integer', 'real'))"
    for col in TIMESTAMP_COLUMNS
)


def stored_malformed_timestamp_count(cursor) -> int:
    """
    How many `documents` rows still hold a non-numeric value in a timestamp
    column. Read-path skips alone under-report the problem: a row nothing has
    queried yet is still poison. Health reports both numbers.
    """
    try:
        row = cursor.execute(_STORED_MALFORMED_SQL).fetchone()
    except Exception:  # missing table on a partial schema, etc.
        return 0
    if row is None:
        return 0
    try:
        return int(row["n"] or 0)
    except (TypeError, IndexError, KeyError):
        return int(row[0] or 0)


def malformed_timestamp_report() -> Dict[str, Any]:
    """Snapshot of read-path timestamp skips for the health surface."""
    with _malformed_lock:
        return {
            "total": sum(_malformed_counts.values()),
            "by_field": dict(_malformed_counts),
            "examples": {k: list(v) for k, v in _malformed_examples.items()},
        }


def reset_malformed_timestamps() -> None:
    """Clear the in-process skip counters (tests)."""
    with _malformed_lock:
        _malformed_counts.clear()
        _malformed_examples.clear()

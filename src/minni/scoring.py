"""
Minni — Confidence Scoring.

PR-2: compute_confidence() produces a calibrated [0,1] float from raw retrieval signals.

Calibration uses a rolling window of the last 1000 scores from the
score_distribution table (written during retrieval). On first call or
empty DB the raw score is returned as-is (no calibration available yet).

compute_confidence(rrf_score, cross_encoder_score, decay_factor) -> float [0,1]
"""

import logging
import math
from typing import Optional

from minni.request_deadline import RequestDeadlineExceeded

logger = logging.getLogger("sovereign.scoring")

# Rolling window size for percentile calibration
_WINDOW_SIZE = 1000

# Samples required before _calibrate stops returning the raw score. Crossing it
# CHANGES CONFIDENCE SEMANTICS for every caller — from a raw blend to a
# percentile rank — so it is a named constant shared with calibration_status()
# below rather than a literal buried in _calibrate. The surface and the
# behaviour must not be able to drift apart.
_ACTIVATION_THRESHOLD = 10


def calibration_status(db) -> dict:
    """Whether percentile calibration is live, and how close it is to becoming so.

    GA4-1 guardrail. Wiring record_score means score_distribution fills up during
    normal retrieval, and the moment it crosses _ACTIVATION_THRESHOLD every
    caller's `confidence` silently changes meaning — raw blend before, percentile
    rank after. A feature that switches semantics on a row count with nothing
    observable is the same silent-degrade class this audit exists to remove, so
    the transition gets a surface: health_report can say which side of the
    threshold the index is on, and when it flipped.

    Counts only, no scores — safe for the pre-identity health report.
    """
    try:
        with db.cursor() as c:
            rows = c.execute(
                """SELECT COUNT(*) AS n FROM (
                       SELECT raw_score FROM score_distribution
                       WHERE kind = 'combined' ORDER BY id DESC LIMIT ?
                   )""",
                (_WINDOW_SIZE,),
            ).fetchone()["n"]
    except Exception as exc:
        logger.debug("calibration_status unavailable: %s", exc)
        return {"error": str(exc)}

    active = rows >= _ACTIVATION_THRESHOLD
    return {
        "window_rows": rows,
        "window_size": _WINDOW_SIZE,
        "activation_threshold": _ACTIVATION_THRESHOLD,
        "active": active,
        # Spell out the consequence rather than making a reader infer it from
        # a boolean: this is the field that explains why a confidence number
        # changed meaning between two health reports.
        "confidence_basis": "percentile_rank" if active else "raw_blend",
        "samples_until_active": max(0, _ACTIVATION_THRESHOLD - rows),
    }


def raw_confidence(
    rrf_score: Optional[float],
    cross_encoder_score: Optional[float],
    decay_factor: Optional[float],
) -> float:
    """The pre-calibration raw blend — the value the rolling window stores.

    Extracted from compute_confidence (grok-review round 4, finding 1) so the
    RPC boundary can record exactly this value for the final caller-visible
    result set without re-running calibration.
    """
    rrf = rrf_score or 0.0
    decay = decay_factor if decay_factor is not None else 1.0

    # Cross-encoder logit → sigmoid probability if present
    ce_prob: Optional[float] = None
    if cross_encoder_score is not None:
        try:
            ce_prob = 1.0 / (1.0 + math.exp(-float(cross_encoder_score)))
        except (OverflowError, ValueError):
            ce_prob = None

    # Blend: prefer cross-encoder when available
    if ce_prob is not None:
        # 60% cross-encoder, 40% RRF (normalised to 0-1)
        rrf_norm = min(1.0, rrf * 20.0)  # RRF scores are typically ~0.01-0.05
        raw = 0.6 * ce_prob + 0.4 * rrf_norm
    else:
        raw = min(1.0, rrf * 20.0)

    # Apply decay
    return raw * max(0.0, min(1.0, decay))


def calibrated_confidence(raw_score: float, db) -> float:
    """Percentile-calibrate a pre-computed raw blend against ``db``'s window.

    grok-review round 5 (finding 1): default search is multi-ENGINE
    (scope=both merges vault + shared hits), and per-engine calibration gave
    one response two meanings of confidence — percentile ranks for shared
    hits, raw blends for vault hits whose windows never fill. The RPC boundary
    calls this against the SHARED db for every final result so the whole
    payload shares one basis. Same clamp/round contract as compute_confidence.
    """
    return round(max(0.0, min(1.0, _calibrate(float(raw_score), db))), 4)


def compute_confidence(
    rrf_score: Optional[float],
    cross_encoder_score: Optional[float],
    decay_factor: Optional[float],
    db=None,
    record: bool = False,
) -> float:
    """
    Compute a calibrated confidence score in [0, 1].

    Pipeline:
    1. Blend rrf_score and cross_encoder_score into a raw combined score.
    2. Apply decay_factor attenuation.
    3. Percentile-calibrate against the rolling window from score_distribution.
    4. Clamp to [0, 1].

    Args:
        rrf_score: Reciprocal Rank Fusion score (positive float, unbounded).
        cross_encoder_score: Cross-encoder logit (may be negative). None → ignored.
        decay_factor: Memory decay multiplier in (0, 1]. None → 1.0.
        db: Optional SovereignDB for percentile calibration. None → uncalibrated.
        record: Append this call's PRE-calibration raw score to the rolling
            window (GA4-1). Opt-in, and deliberately not the default: only the
            final caller-visible result set should feed the distribution.
            Speculative paths that score candidates they may discard — the
            HyDE probe in retrieval.py is the live example — would otherwise
            inflate the window with scores no caller ever saw. grok-review
            round 4 (finding 1): the daemon search path no longer records via
            this flag at all — formatting runs once per engine per variant, so
            recall.handle_search records the final merged set through
            record_score at the RPC boundary instead.

    Returns:
        float in [0, 1].
    """
    raw = raw_confidence(rrf_score, cross_encoder_score, decay_factor)

    # GA4-1: record BEFORE calibrating. record_score had zero production call
    # sites, so score_distribution stayed empty, _calibrate always fell through
    # its `total < 10` guard, and calibration was permanently inert — the
    # docstring above ("written during retrieval") described a wire that did not
    # exist. Recording the calibrated value instead would feed the distribution
    # its own output and make the percentiles converge on themselves.
    if db is not None and record:
        record_score(raw, "combined", db)

    # Percentile calibration against rolling window
    if db is not None:
        raw = _calibrate(raw, db)

    return round(max(0.0, min(1.0, raw)), 4)


def _calibrate(raw: float, db) -> float:
    """
    Convert raw score to percentile rank within the rolling window.

    Returns raw if calibration fails (table empty, DB error, etc.).
    """
    try:
        with db.cursor() as c:
            # How many scores in the window are ≤ raw?
            c.execute(
                """
                SELECT COUNT(*) as cnt
                FROM (
                    SELECT raw_score FROM score_distribution
                    WHERE kind = 'combined'
                    ORDER BY id DESC
                    LIMIT ?
                ) t
                WHERE raw_score <= ?
                """,
                (_WINDOW_SIZE, raw),
            )
            below = c.fetchone()["cnt"]

            c.execute(
                """
                SELECT COUNT(*) as cnt
                FROM (
                    SELECT raw_score FROM score_distribution
                    WHERE kind = 'combined'
                    ORDER BY id DESC
                    LIMIT ?
                ) t
                """,
                (_WINDOW_SIZE,),
            )
            total = c.fetchone()["cnt"]

        if total < _ACTIVATION_THRESHOLD:
            # Not enough data for meaningful calibration
            return raw

        return below / total

    except RequestDeadlineExceeded:
        raise
    except Exception as e:
        logger.debug("Calibration failed (non-fatal): %s", e)
        return raw


def record_score(raw_score: float, kind: str, db) -> None:
    """
    Append a score to the rolling window table.

    Called after each retrieval to build the calibration distribution.
    Non-fatal on any error.
    """
    try:
        with db.cursor() as c:
            c.execute(
                "INSERT INTO score_distribution (raw_score, kind) VALUES (?, ?)",
                (raw_score, kind),
            )
            # Prune window: keep only last 2×_WINDOW_SIZE rows to bound growth
            c.execute(
                """
                DELETE FROM score_distribution
                WHERE id NOT IN (
                    SELECT id FROM score_distribution
                    WHERE kind = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                AND kind = ?
                """,
                (kind, _WINDOW_SIZE * 2, kind),
            )
    except RequestDeadlineExceeded:
        raise
    except Exception as e:
        logger.debug("record_score failed (non-fatal): %s", e)

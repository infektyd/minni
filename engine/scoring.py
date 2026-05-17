"""
Sovereign Memory — Confidence Scoring.

PR-2: compute_confidence() produces a calibrated [0,1] float from raw retrieval signals.

Calibration uses a rolling window of the last 1000 scores from the
score_distribution table (written during retrieval). On first call or
empty DB the raw score is returned as-is (no calibration available yet).

compute_confidence(rrf_score, cross_encoder_score, decay_factor) -> float [0,1]
"""

import logging
import math
from typing import Optional

logger = logging.getLogger("sovereign.scoring")

# Rolling window size for percentile calibration
_WINDOW_SIZE = 1000


def compute_confidence(
    rrf_score: Optional[float],
    cross_encoder_score: Optional[float],
    decay_factor: Optional[float],
    db=None,
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

    Returns:
        float in [0, 1].
    """
    # Defaults
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
    raw = raw * max(0.0, min(1.0, decay))

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

        if total < 10:
            # Not enough data for meaningful calibration
            return raw

        return below / total

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
    except Exception as e:
        logger.debug("record_score failed (non-fatal): %s", e)

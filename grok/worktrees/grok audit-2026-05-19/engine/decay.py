"""
Sovereign Memory V3.1 — Memory Decay.

Unchanged from V3 — the decay logic was already correct.
Exponential decay with access-based reinforcement, no hard cutoffs.
"""

import time
import math
import logging
from typing import Dict

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB

logger = logging.getLogger("sovereign.decay")


class MemoryDecay:
    """Exponential memory decay with access-based reinforcement."""

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
    ):
        self.db = db
        self.config = config

    def run_decay(self) -> Dict:
        """
        Update decay_score for all documents.
        Should be called daily (cron at 04:00).
        """
        now = time.time()
        half_life_sec = self.config.decay_half_life_days * 86400
        min_score = self.config.decay_min_score
        stats = {"updated": 0, "reinforced": 0}

        with self.db.transaction() as c:
            c.execute("""
                SELECT doc_id, indexed_at, last_accessed, access_count, decay_score
                FROM documents
            """)

            for row in c.fetchall():
                doc_id = row["doc_id"]
                indexed_at = row["indexed_at"] or now
                last_accessed = row["last_accessed"]
                access_count = row["access_count"] or 0

                reference_time = last_accessed if last_accessed else indexed_at
                age_sec = now - reference_time

                raw_decay = math.pow(0.5, age_sec / half_life_sec) if half_life_sec > 0 else 1.0

                # Access reinforcement: each access adds 0.05 to floor, capped at 0.5
                access_boost = min(access_count * 0.05, 0.5)

                new_score = max(min_score, min(1.0, raw_decay + access_boost))

                old_score = row["decay_score"] or 1.0
                if abs(new_score - old_score) > 0.001:
                    c.execute(
                        "UPDATE documents SET decay_score = ? WHERE doc_id = ?",
                        (round(new_score, 4), doc_id),
                    )
                    stats["updated"] += 1

                    if access_boost > 0:
                        stats["reinforced"] += 1

        logger.info(
            "Decay pass complete: %d updated, %d reinforced",
            stats["updated"], stats["reinforced"],
        )
        return stats

    def get_decay_report(self) -> Dict:
        """Get summary of current decay state."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT
                    COUNT(*) as total,
                    AVG(decay_score) as avg_score,
                    MIN(decay_score) as min_score,
                    MAX(decay_score) as max_score,
                    SUM(CASE WHEN decay_score < 0.1 THEN 1 ELSE 0 END) as fading,
                    SUM(CASE WHEN decay_score > 0.8 THEN 1 ELSE 0 END) as strong
                FROM documents
            """)
            row = c.fetchone()
            return {
                "total_docs": row["total"],
                "avg_decay_score": round(row["avg_score"] or 0, 3),
                "min_score": round(row["min_score"] or 0, 3),
                "max_score": round(row["max_score"] or 0, 3),
                "fading_count": row["fading"],
                "strong_count": row["strong"],
            }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = SovereignDB()
    decay = MemoryDecay(db)
    stats = decay.run_decay()
    print(f"Decay stats: {stats}")
    report = decay.get_decay_report()
    print(f"Report: {report}")
    db.close()

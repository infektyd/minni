"""
Minni V3.1 — Memory Decay.

Exponential decay with access-based reinforcement, no hard cutoffs.

recall-F4 (audit cluster C1): correction-class notes get a grace window and a
decay floor — access-recency reinforcement otherwise up-ranks stale-but-reread
beliefs over the fresh, unread corrections that supersede them.
"""

import time
import math
import logging
from typing import Dict

from minni.config import SovereignConfig, DEFAULT_CONFIG, correction_class_page_types
from minni.db import SovereignDB
from minni.timestamps import parse_epoch_or_report

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
        # recall-F4: correction-class notes must not lose to stale-but-reread
        # beliefs whose decay saturates at 1.0 via access reinforcement.
        # Shared helper (config.py) — same set retrieval.py boosts on.
        correction_types = correction_class_page_types(self.config)
        # Direct field access: these are SovereignConfig dataclass fields with
        # defaults (config.py); only correction_class_page_types() tolerates
        # duck-typed configs.
        grace_sec = float(self.config.correction_decay_grace_days) * 86400
        correction_floor = float(self.config.correction_decay_floor)
        # grok-review (PR #242, rounds 2-4): a bad last_accessed or a bad
        # indexed_at each still get decayed (falling back to indexed_at, then
        # to `now`, exactly like their respective NULL cases) — no row is
        # ever left undecayed by this loop, `now` is always an available
        # last-resort anchor. Each degrade gets its own counter rather than a
        # single "skipped" stat that would misreport a row that was, in fact,
        # processed (round 4: a stat that can never be nonzero is worse than
        # no stat — it implies a hard-skip path that no longer exists).
        stats = {
            "updated": 0, "reinforced": 0,
            "bad_indexed_at": 0, "bad_last_accessed": 0,
        }

        with self.db.transaction() as c:
            c.execute("""
                SELECT doc_id, indexed_at, last_accessed, access_count,
                       decay_score, page_type
                FROM documents
            """)

            for row in c.fetchall():
                doc_id = row["doc_id"]
                # Audit R0: indexed_at / last_accessed are REAL-affinity columns,
                # which SQLite happily fills with a non-numeric TEXT value. The
                # arithmetic below then raises TypeError inside this transaction
                # and aborts the ENTIRE decay pass over every document. Parse
                # each field defensively; a bad value degrades to the same
                # fallback its NULL counterpart already uses, and is counted
                # (not silently absorbed) rather than aborting the row.
                indexed_at = parse_epoch_or_report(
                    row["indexed_at"], field="indexed_at",
                    source="decay.run_decay", doc_id=doc_id,
                )
                last_accessed = parse_epoch_or_report(
                    row["last_accessed"], field="last_accessed",
                    source="decay.run_decay", doc_id=doc_id,
                )
                # grok-review round 3: a bad (non-NULL, unparseable) indexed_at
                # used to `continue` past the whole document, even though a
                # valid last_accessed could still anchor decay, and even a
                # missing last_accessed just falls to `now` below — the same
                # fallback the NULL-indexed_at case already gets. Degrade
                # instead of skipping; parse_epoch_or_report already
                # counted/logged the bad value in the malformed-timestamp
                # report, so this counter is about "not decayed to plan", not
                # visibility of the poison itself.
                if row["indexed_at"] is not None and indexed_at is None:
                    stats["bad_indexed_at"] += 1
                if indexed_at is None:
                    indexed_at = now
                # grok-review round 1: a poisoned last_accessed used to
                # `continue` past the whole document, even though the normal
                # NULL-last_accessed path already falls back to indexed_at.
                # It just becomes "no last_accessed", same as NULL — the
                # document is still decayed below, so it is NOT a skip
                # (grok-review round 2: the old code counted it as one).
                if row["last_accessed"] is not None and last_accessed is None:
                    stats["bad_last_accessed"] += 1
                access_count = row["access_count"] or 0

                reference_time = last_accessed if last_accessed else indexed_at
                age_sec = now - reference_time

                raw_decay = math.pow(0.5, age_sec / half_life_sec) if half_life_sec > 0 else 1.0

                # Access reinforcement: each access adds 0.05 to floor, capped at 0.5
                access_boost = min(access_count * 0.05, 0.5)

                new_score = max(min_score, min(1.0, raw_decay + access_boost))

                # recall-F4: corrections start unaccessed, so access-recency
                # decay up-ranks the very beliefs they supersede. Within the
                # grace window a correction holds full strength; afterwards it
                # decays normally but never below the correction floor.
                if (row["page_type"] or "").lower() in correction_types:
                    if (now - indexed_at) <= grace_sec:
                        new_score = 1.0
                    else:
                        new_score = max(new_score, correction_floor)

                old_score = row["decay_score"] or 1.0
                if abs(new_score - old_score) > 0.001:
                    c.execute(
                        "UPDATE documents SET decay_score = ? WHERE doc_id = ?",
                        (round(new_score, 4), doc_id),
                    )
                    stats["updated"] += 1

                    if access_boost > 0:
                        stats["reinforced"] += 1

        if stats["bad_indexed_at"]:
            logger.warning(
                "Decay pass decayed %d document(s) using a fallback anchor "
                "(last_accessed or now): the stored indexed_at was non-numeric. "
                "Run migration 016 to normalize them.",
                stats["bad_indexed_at"],
            )
        if stats["bad_last_accessed"]:
            logger.warning(
                "Decay pass decayed %d document(s) from indexed_at instead of "
                "last_accessed: the stored last_accessed was non-numeric. "
                "Run migration 016 to normalize them.",
                stats["bad_last_accessed"],
            )
        logger.info(
            "Decay pass complete: %d updated, %d reinforced, %d decayed via "
            "fallback anchor (bad indexed_at), %d decayed via indexed_at "
            "fallback (bad last_accessed)",
            stats["updated"], stats["reinforced"],
            stats["bad_indexed_at"], stats["bad_last_accessed"],
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

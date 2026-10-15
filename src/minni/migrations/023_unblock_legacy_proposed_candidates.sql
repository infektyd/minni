-- Migration 023: add category column to candidate_packets and supersede stale
-- afm_review privacy fences that permanently parked legacy proposed candidates.
--
-- NULL requested_privacy is deliberately NOT backfilled: the author's original
-- intent was never recorded, so it must stay non-judge-eligible (I1/I2).
-- Only automated privacy-routing fences ("<reason>: privacy=<level>") are
-- superseded; operator holds from mark_candidate_review stay in place.

ALTER TABLE candidate_packets ADD COLUMN category TEXT NOT NULL DEFAULT 'general';

UPDATE consolidation_actions
   SET status = 'superseded'
 WHERE action_type = 'afm_review'
   AND COALESCE(status, '') != 'superseded'
   AND detail LIKE '%: privacy=%'
   AND claim IN (
       SELECT CAST(candidate_id AS TEXT)
         FROM candidate_packets
        WHERE status = 'proposed'
   );

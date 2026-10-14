-- Migration 023: add category column to candidate_packets, backfill NULL
-- requested_privacy on proposed candidates, and supersede stale afm_review
-- fences that permanently parked legacy proposed candidates.

ALTER TABLE candidate_packets ADD COLUMN category TEXT NOT NULL DEFAULT 'general';

UPDATE candidate_packets
   SET requested_privacy = COALESCE(requested_privacy, 'safe')
 WHERE status = 'proposed'
   AND requested_privacy IS NULL;

UPDATE consolidation_actions
   SET status = 'superseded'
 WHERE action_type = 'afm_review'
   AND COALESCE(status, '') != 'superseded'
   AND claim IN (
       SELECT CAST(candidate_id AS TEXT)
         FROM candidate_packets
        WHERE status = 'proposed'
   );

-- Migration 022: add requested_privacy column to candidate_packets
-- Preserves the author's declared privacy level before non-operator clamping.

ALTER TABLE candidate_packets ADD COLUMN requested_privacy TEXT;

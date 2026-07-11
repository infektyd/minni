-- Migration 015: expand candidate_packets.status CHECK to allow do_not_store
-- and log_only (console quarantine / log-only zones).
--
-- The table rebuild CANNOT run as plain SQL inside run_migrations' BEGIN
-- IMMEDIATE block: SQLite treats PRAGMA foreign_keys=OFF as a no-op inside a
-- transaction, so DROP candidate_packets fails when contradiction_log rows
-- reference resolution_id. The actual rebuild is implemented in Python
-- (_apply_migration_015_candidate_status_expand) which toggles foreign_keys
-- outside any transaction. This file is intentionally a no-op marker so the
-- version number remains discoverable and recorded in schema_migrations.
--
-- See migrations.py:_apply_migration_015_candidate_status_expand.

SELECT 1;

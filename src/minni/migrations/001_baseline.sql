-- 001_baseline.sql
-- Marks the existing V3.1 schema as version 1.
-- No-op: all tables were created by db.py _init_schema().
-- Fresh DBs run this after _init_schema()
-- Existing DBs pick up user_version=1 with zero row/column changes.
SELECT 1;

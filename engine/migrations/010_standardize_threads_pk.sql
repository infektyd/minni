-- Migration 010: Standardize threads primary key column name
-- Renames threads.id to threads.thread_id to align with the schema definition in db.py.
-- The trg_thread_msg_count trigger is dropped here and is automatically recreated by db._init_schema on startup.
DROP TRIGGER IF EXISTS trg_thread_msg_count;

ALTER TABLE threads RENAME COLUMN id TO thread_id;

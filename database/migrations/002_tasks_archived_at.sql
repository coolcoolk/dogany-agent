-- 002: add tasks.archived_at -- soft-delete marker for the task-archive verb
-- (DGN-167). NULL = active; set to local datetime when archived. Archived rows
-- are hidden from task-find / task-overdue.
-- Fresh DBs built from schema.sql already include this column (baseline is
-- user_version 2), so update.sh only applies this to pre-existing v1 DBs.
ALTER TABLE tasks ADD COLUMN archived_at TEXT;
PRAGMA user_version = 2;

-- 007: DGN-273 notify policy -- per-routine / per-event notification policy.
-- Adds notify_policy + notify_lead_min to routine_def (policy home, asked
-- once at routine registration) and to event (stamped onto materialized
-- instances by the roller; one-off events may carry their own override).
--
-- Semantics (remind poller contract):
--   NULL / 'default' -> pre-007 behavior (task: 30-min lead + on-time alert;
--                       appointment: 120-min lead + on-time alert)
--   'silent'         -> no alerts at all
--   'start_only'     -> on-time alert only
--   'custom'         -> notify_lead_min minutes lead alert + on-time alert
-- notify_lead_min is meaningful ONLY with 'custom' (minutes, >= 0).
--
-- Convention per 002/005/006: plain ALTERs guarded by update.sh's version
-- check (applied only when user_version < 7). sqlite ALTER cannot add
-- cross-column CHECKs, so the lifekit verbs are the coherence enforcement
-- point on migrated DBs (fresh DBs get table CHECKs from schema.sql).
-- busy_timeout: live pollers must not fail mid-flight with SQLITE_BUSY.
-- .bail on: abort on first error so a partial migration is never silently
-- committed (convention for all migrations from 007 onward).

.bail on
PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE routine_def ADD COLUMN notify_policy TEXT;
ALTER TABLE routine_def ADD COLUMN notify_lead_min INTEGER;

ALTER TABLE event ADD COLUMN notify_policy TEXT;
ALTER TABLE event ADD COLUMN notify_lead_min INTEGER;

PRAGMA user_version = 7;

COMMIT;

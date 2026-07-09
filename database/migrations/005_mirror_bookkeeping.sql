-- 005: DGN-180 W6 -- mirror surface bookkeeping columns on event.
-- Spec basis: v4 X9c / v3 W6 ("bookkeeping columns -- required promotion").
-- Nullable mirror-bookkeeping only: no CHECK, no derivation input, no slot
-- predicate involvement -- does not touch any 179 v5 LOCK semantics.
--
-- Convention per 002/004: plain ALTERs guarded by update.sh's version check
-- (applied only when user_version < 5), wrapped in one transaction.
-- busy_timeout (grill-6 item 6): a live poller/bridge write must not make
-- this migration fail mid-flight with SQLITE_BUSY; wait up to 10s instead.
-- (update.sh `sqlite3 -bail` hardening = framework follow-up, see MANIFEST.)
-- Values are backfilled from the mirror state KV by the instance cutover
-- script only where a pre-existing state KV exists -- data migration is NOT
-- this file's job.

PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE event ADD COLUMN gcal_event_id TEXT;
ALTER TABLE event ADD COLUMN gtask_id      TEXT;
ALTER TABLE event ADD COLUMN gcal_etag     TEXT;
ALTER TABLE event ADD COLUMN gtask_etag    TEXT;

PRAGMA user_version = 5;

COMMIT;

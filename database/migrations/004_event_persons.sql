-- 004: DGN-179 D2 (verb-delta spec v2) -- event_persons junction (appointment
-- participants for the unified event table; successor of appointment_persons).
--
-- DDL ONLY. No RENAME, no data mutation (the legacy appointment_persons remap is
-- the Ag data-migration script's job, DGN-180 lane). Every object IF NOT EXISTS
-- -> idempotent (double-apply is a no-op; only user_version advances).
--
-- user_version lineage: 003_event_schema owns user_version=3, so this is 004.
-- update.sh applies this only when the DB's current user_version < 4, so a v3
-- install upgrades to 4 with no skip.
--
-- Same-DB FK is INTEGER id (G9 rule as amended by v4 MINOR-c: only cross-agent/
-- host refs must be ulid). ON DELETE CASCADE both sides matches the legacy
-- appointment_persons convention (inert in practice -- no delete verb exists).
--
-- NOTE (MIN-5): the all_day belt CHECK is NOT added here. A CHECK cannot be
-- ALTERed onto the existing event table in SQLite (no ALTER ADD CONSTRAINT); it
-- ships in schema.sql for fresh DBs and is enforced on the live path by the app
-- validator (validate_all_day_instants). Migrated DBs rely on the app validator;
-- no non-SDK all_day writer exists (caller inventory R-1..R-4).
--
-- Wrapped in a single transaction so a partial failure rolls back cleanly.

BEGIN;

CREATE TABLE IF NOT EXISTS event_persons (
    event_id  INTEGER NOT NULL REFERENCES event(id)   ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, person_id)
);
-- reverse lookup ("events with person X") -- lands as the read path when
-- remind/weekly-review switch off Notion at the DGN-180 cutover (R-5).
CREATE INDEX IF NOT EXISTS idx_event_persons_person
    ON event_persons(person_id);

PRAGMA user_version = 4;

COMMIT;

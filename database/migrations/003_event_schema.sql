-- 003: DGN-179 event schema -- unified L1 event model (task + appointment)
-- over three tables (event / sub_event / reschedule_requests). Spec v5 LOCK
-- 2026-07-07.
--
-- DDL ONLY. No RENAME, no data mutation. Every object is IF NOT EXISTS so this
-- is idempotent: a v2 install that lacks the tables gets them created; an
-- install that already has them (e.g. a live agent, hand-applied pre-DGN-180) is a
-- no-op and only the user_version stamp advances 2 -> 3.
--
-- user_version lineage (DGN-180 D180-0): 002_tasks_archived_at owns
-- user_version=2. The event schema is therefore migration 003 (renumbered from
-- the sandbox's provisional 2). update.sh applies this only when the DB's
-- current user_version < 3, so a v2 install upgrades to 3 with no skip.
--
-- Wrapped in a single transaction so a partial failure rolls back cleanly.
-- All time columns are canonical UTC 'YYYY-MM-DDThh:mm:ssZ' (fixed 20 chars).

BEGIN;

-- event (L1) -- unified task + appointment
CREATE TABLE IF NOT EXISTS event (
    id            INTEGER PRIMARY KEY,
    ulid          TEXT NOT NULL UNIQUE,           -- cross-agent/host reference of record
    kind          TEXT NOT NULL,                  -- 'task' | 'appointment'
    title         TEXT NOT NULL,
    note          TEXT,
    area_id       INTEGER REFERENCES areas(id),

    -- schedule classification. v5.1 overlap predicate targets any exclusive row
    -- with a start instant (timed OR all_day day-block); untimed has no start.
    schedule_kind TEXT NOT NULL,                  -- 'timed' | 'all_day' | 'untimed'

    -- canonical UTC instants. all_day stores display_tz midnight..next-midnight
    -- as UTC instants (multi-day = [start_day 00:00 local, end_day+1 00:00 local)).
    start_at      TEXT,
    end_at        TEXT,
    display_tz    TEXT NOT NULL DEFAULT 'Asia/Seoul',

    open_ended    INTEGER NOT NULL DEFAULT 0,     -- open-ended end = end NULL + this=1
    slot_exclusive INTEGER NOT NULL,              -- SDK kind-policy decides, not per-write

    -- derived status cache. NEVER written directly (SDK recompute only).
    -- priority: settled(outcome verbatim) > expired > vacuous-open > all-done > open.
    -- derivation NEVER produces 'abandoned' -- abandoned only via settled_outcome.
    status        TEXT NOT NULL DEFAULT 'open',

    -- grill-5 (OQ-2/MAJOR-2): enum shrunk to the two rules whose derivation is
    -- actually defined ('all' / 'manual'). 'any' / 'n_of_m' had no derive_status
    -- branch, so they are dropped from the CHECK. completion_n is KEPT (cheap,
    -- no data) for a future re-introduction of n_of_m -- currently unused.
    completion_rule TEXT NOT NULL DEFAULT 'all',  -- 'all' | 'manual'
    completion_n  INTEGER,                        -- unused (reserved for future n_of_m)

    version       INTEGER NOT NULL DEFAULT 0,     -- CAS token

    -- settle (force-settle / cancel). settled_at present iff settled_outcome present.
    settled_at    TEXT,
    settled_by    TEXT,
    settled_outcome TEXT,                         -- 'done' | 'abandoned'

    owning_agent  TEXT NOT NULL,                  -- move rights
    created_by    TEXT NOT NULL,                  -- audit

    -- declarative move constraints (Ag rescheduler respects these)
    earliest      TEXT,
    latest        TEXT,
    anchor        TEXT,
    immovable     INTEGER NOT NULL DEFAULT 0,

    -- kind-specific nullable columns (D7: single table, no join)
    priority      TEXT,                           -- task
    seq           REAL,                           -- task
    is_routine    INTEGER DEFAULT 0,              -- task
    location      TEXT,                           -- appointment
    location_url  TEXT,                           -- appointment
    purpose       TEXT,                           -- appointment
    summary       TEXT,                           -- appointment

    recurrence_id TEXT,                           -- prebought, unused (future recurrence)
    notion_id     TEXT,                           -- inherits UNIQUE partial index

    created_at    TEXT NOT NULL,                  -- canonical UTC
    updated_at    TEXT NOT NULL,                  -- canonical UTC

    CHECK (kind IN ('task','appointment')),
    CHECK (schedule_kind IN ('timed','all_day','untimed')),
    -- appointment may never be untimed (row 83 dropped in migration).
    CHECK (NOT (kind = 'appointment' AND schedule_kind = 'untimed')),
    -- timed events must have a start.
    CHECK (schedule_kind != 'timed' OR start_at IS NOT NULL),
    -- canonical UTC fixed-width shape (belt-and-suspenders with app validator).
    CHECK (start_at IS NULL OR start_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (end_at IS NULL OR end_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (created_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (updated_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (settled_at IS NULL OR settled_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (open_ended IN (0,1)),
    CHECK (slot_exclusive IN (0,1)),
    CHECK (immovable IN (0,1)),
    -- open-ended + slot_exclusive = infinite occupancy brick. Forbidden (provisional).
    CHECK (NOT (open_ended = 1 AND slot_exclusive = 1)),
    -- grill-5 belt CHECK #1 (OQ-4/MAJOR-3): a raw exclusive row with a start but
    -- NULL end and open_ended=0 would hit the +inf sentinel and occupy [start,+inf).
    -- Forbid it at the schema layer (SDK never emits it; migrated data has 0).
    CHECK (NOT (slot_exclusive = 1 AND start_at IS NOT NULL
                AND end_at IS NULL AND open_ended = 0)),
    -- grill-5 belt CHECK #2 (MAJOR-1 root): an 'untimed' row must carry NO time
    -- instants. Makes untimed structurally unable to hold a stale occupancy bit
    -- (start_at) after a schedule_kind transition -- kills the phantom-blocker
    -- class at the root.
    CHECK (NOT (schedule_kind = 'untimed'
                AND (start_at IS NOT NULL OR end_at IS NOT NULL))),
    CHECK (status IN ('open','done','expired','abandoned')),
    -- grill-5 enum shrink (OQ-2/MAJOR-2): only 'all' and 'manual' have defined
    -- derivation.
    CHECK (completion_rule IN ('all','manual')),
    -- settle pairing: settled_at present iff settled_outcome present.
    CHECK ((settled_at IS NULL) = (settled_outcome IS NULL)),
    CHECK (settled_outcome IS NULL OR settled_outcome IN ('done','abandoned')),
    -- grill-5 (MINOR-1): reversed interval illegal (zero-length is legal, use >=).
    CHECK (end_at IS NULL OR start_at IS NULL OR end_at >= start_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_notion
    ON event(notion_id) WHERE notion_id IS NOT NULL;
-- overlap-scan support: lead the index with the v5.1 liveness-filter columns.
CREATE INDEX IF NOT EXISTS idx_event_overlap
    ON event(slot_exclusive, settled_at, start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_event_kind ON event(kind);

-- sub_event (L1-L2 link) -- pointers to L2 domain work units
CREATE TABLE IF NOT EXISTS sub_event (
    id           INTEGER PRIMARY KEY,
    ulid         TEXT NOT NULL UNIQUE,
    -- same-DB FK is INTEGER id (G9 rule: only cross-agent/host refs must be ulid).
    event_id     INTEGER NOT NULL REFERENCES event(id),
    owning_agent TEXT NOT NULL,
    kind         TEXT,
    ref          TEXT,                            -- L2 domain row ulid (e.g. workout session)
    done         INTEGER NOT NULL DEFAULT 0,
    tombstone    INTEGER NOT NULL DEFAULT 0,      -- delete != complete; invisible to derivation
    created_at   TEXT,
    settled_at   TEXT,
    CHECK (done IN (0,1)),
    CHECK (tombstone IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_sub_event_parent ON sub_event(event_id, tombstone);
CREATE INDEX IF NOT EXISTS idx_sub_event_owner ON sub_event(owning_agent, tombstone);

-- reschedule_requests (M1) -- durable cross-agent reschedule queue
CREATE TABLE IF NOT EXISTS reschedule_requests (
    id              INTEGER PRIMARY KEY,
    ulid            TEXT NOT NULL UNIQUE,
    event_ulid      TEXT NOT NULL,                -- cross-agent ref = ulid
    requester_agent TEXT NOT NULL,
    proposed_start  TEXT,
    proposed_end    TEXT,
    reason          TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    created_at      TEXT,
    resolved_at     TEXT,
    CHECK (status IN ('queued','claimed','applied','rejected','expired'))
);
CREATE INDEX IF NOT EXISTS idx_reschedule_status ON reschedule_requests(status);

PRAGMA user_version = 3;

COMMIT;

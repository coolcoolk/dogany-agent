-- 006: DGN-240 routine recurrence -- event columns + new tables.
-- Adds rec_date / rec_exception columns on event; creates routine_def,
-- roller_log, projects, routine, session_type, session_muscle tables.
-- Convention per 002/004: plain ALTERs guarded by update.sh's version check
-- (applied only when user_version < 6), wrapped in transactions.
-- busy_timeout: live poller must not fail mid-flight with SQLITE_BUSY.

PRAGMA busy_timeout = 10000;

BEGIN;

-- event: recurrence columns (DGN-240 spec v3 T5/T7)
ALTER TABLE event ADD COLUMN rec_date      TEXT;
ALTER TABLE event ADD COLUMN rec_exception INTEGER NOT NULL DEFAULT 0;

-- routine_def: lifecycle anchor for recurring task series.
CREATE TABLE IF NOT EXISTS routine_def (
    id            INTEGER PRIMARY KEY,
    ulid          TEXT NOT NULL UNIQUE,
    recurrence_id TEXT NOT NULL UNIQUE,   -- group anchor stamped on instances
    title         TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'task',
    cadence       TEXT,                   -- DSL v1; NULL only when retired (import)
    schedule_kind TEXT NOT NULL,          -- 'timed' | 'all_day' (untimed forbidden)
    time_of_day   TEXT,                   -- 'HH:MM' local, timed only
    duration_min  INTEGER,                -- timed only, default 30
    exclusive     INTEGER NOT NULL DEFAULT 0,  -- schema-only in v1 (OQ-12)
    display_tz    TEXT NOT NULL DEFAULT 'Asia/Seoul',
    area_id       INTEGER REFERENCES areas(id),
    project_id    INTEGER REFERENCES projects(id),   -- nullable (rollup join)
    purpose       TEXT,                   -- one line, user language
    status        TEXT NOT NULL DEFAULT 'active',
    start_date    TEXT NOT NULL,          -- local date, first eligible day
    end_date      TEXT,                   -- user-set hard end; NULL = autonomous
    valid_until   TEXT NOT NULL,          -- renewal window, AUTONOMOUS defs only
                                          -- (end_date defs: set = end_date, unused)
    rule_effective_from TEXT NOT NULL,    -- F-A: persisted at registration (today)
                                          -- and at EVERY routine_update (= verb's
                                          -- effective_from); conformance floor
    anomaly_ack   TEXT,                   -- JSON snapshot of retro choice-4 ack (6.3)
    version       INTEGER NOT NULL DEFAULT 0,        -- CAS
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    CHECK (kind IN ('task','appointment')),
    CHECK (schedule_kind IN ('timed','all_day')),
    CHECK (status IN ('active','paused','retired')),
    CHECK (schedule_kind != 'timed' OR time_of_day IS NOT NULL),
    CHECK (exclusive IN (0,1)),
    CHECK (cadence IS NOT NULL OR status = 'retired')
);

-- roller_log: audit + displaced/health source.
CREATE TABLE IF NOT EXISTS roller_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    recurrence_id TEXT,
    category TEXT NOT NULL,  -- materialized|displaced|lapse_settled|extended
                             -- |anomaly_ping|regen_cancel|regen_delete|retired
                             -- |conformance_regen|conformance_frozen
    detail TEXT
);

-- projects: rollup join target for routine_def.
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY,
    ulid       TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    status     TEXT,
    start_date TEXT,
    end_date   TEXT,
    note       TEXT,
    area_id    INTEGER REFERENCES areas(id),
    notion_id  TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_notion
    ON projects(notion_id) WHERE notion_id IS NOT NULL;

-- routine: workout session routine grouping (legacy table, kept for
-- session_type FK). Separate from routine_def.
CREATE TABLE IF NOT EXISTS routine (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    style TEXT,
    goal_mode TEXT,
    start_date TEXT,
    end_date TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- session_type / session_muscle: workout session typing.
CREATE TABLE IF NOT EXISTS session_type (
    id INTEGER PRIMARY KEY,
    routine_id INTEGER NOT NULL REFERENCES routine(id),
    code TEXT NOT NULL,
    display_name TEXT,
    sort INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    UNIQUE(routine_id, code)
);

CREATE TABLE IF NOT EXISTS session_muscle (
    session_type_id INTEGER NOT NULL REFERENCES session_type(id),
    type_id INTEGER NOT NULL REFERENCES workout_types(id),
    role TEXT NOT NULL,
    fatigue INTEGER NOT NULL DEFAULT 2,
    PRIMARY KEY(session_type_id, type_id)
);

-- Unique partial index: at most one live (rec_date, recurrence_id) pair per
-- recurrence series (prevents duplicate materialization).
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_rec_live
    ON event(recurrence_id, rec_date)
    WHERE recurrence_id IS NOT NULL
      AND rec_date IS NOT NULL
      AND settled_at IS NULL
      AND rec_exception = 0;

PRAGMA user_version = 6;

COMMIT;

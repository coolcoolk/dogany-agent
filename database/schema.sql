PRAGMA user_version = 4;
CREATE TABLE areas (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,               -- 영역이름 (신체건강, 식습관…)
  domain      TEXT NOT NULL,                      -- 건강/재무/관계/일/취미/관리/외모/반려동물
  description TEXT,
  notion_id   TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_areas_domain ON areas(domain);
CREATE UNIQUE INDEX idx_areas_notion ON areas(notion_id) WHERE notion_id IS NOT NULL;
CREATE TABLE workouts (
  id         INTEGER PRIMARY KEY,
  date       TEXT NOT NULL,                       -- YYYY-MM-DD (KST)
  minutes    REAL NOT NULL DEFAULT 0,
  kcal       REAL NOT NULL DEFAULT 0,             -- 소모 칼로리
  note       TEXT,
  area_id    INTEGER REFERENCES areas(id),
  notion_id  TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
, avg_hr REAL);
CREATE INDEX idx_workouts_date ON workouts(date);
CREATE UNIQUE INDEX idx_workouts_notion ON workouts(notion_id) WHERE notion_id IS NOT NULL;
CREATE VIEW v_daily_energy AS
SELECT
  d.date,
  COALESCE(m.intake_kcal, 0)  AS intake_kcal,
  COALESCE(m.protein_g,  0)   AS protein_g,
  COALESCE(m.carb_g,     0)   AS carb_g,
  COALESCE(m.fat_g,      0)   AS fat_g,
  COALESCE(w.burn_kcal,  0)   AS burn_kcal,
  COALESCE(w.workout_min,0)   AS workout_min
FROM
  (SELECT date FROM meals UNION SELECT date FROM workouts) d
  LEFT JOIN (
    SELECT date, SUM(kcal) intake_kcal, SUM(protein) protein_g,
           SUM(carb) carb_g, SUM(fat) fat_g
    FROM meals GROUP BY date
  ) m ON m.date = d.date
  LEFT JOIN (
    SELECT date, SUM(kcal) burn_kcal, SUM(minutes) workout_min
    FROM workouts GROUP BY date
  ) w ON w.date = d.date
/* v_daily_energy(date,intake_kcal,protein_g,carb_g,fat_g,burn_kcal,workout_min) */;
CREATE TABLE workout_types (
  id       INTEGER PRIMARY KEY,
  category TEXT NOT NULL,   -- 대분류
  subtype  TEXT NOT NULL,   -- 세부분류
  sort     INTEGER NOT NULL DEFAULT 0,
  active   INTEGER NOT NULL DEFAULT 1,
  UNIQUE(category, subtype)
);
CREATE TABLE IF NOT EXISTS "meals" (
  id         INTEGER PRIMARY KEY,
  date       TEXT NOT NULL,                       -- YYYY-MM-DD (KST)
  meal       TEXT,                                -- 아침/점심/저녁/간식/운동
  name       TEXT NOT NULL,                       -- "음식명 (양g)"
  grams      REAL,
  carb       REAL NOT NULL DEFAULT 0,
  protein    REAL NOT NULL DEFAULT 0,
  fat        REAL NOT NULL DEFAULT 0,
  fiber      REAL NOT NULL DEFAULT 0,
  sugar      REAL NOT NULL DEFAULT 0,
  alt_sugar  REAL NOT NULL DEFAULT 0,
  alcohol    REAL NOT NULL DEFAULT 0,             -- 순수 알코올 그램 (7kcal/g)
  kcal       REAL GENERATED ALWAYS AS
               (protein*4 + fat*9 + (carb-fiber)*4 + alcohol*7) STORED,
  area_id    INTEGER REFERENCES areas(id),
  notion_id  TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_meals_date ON meals(date);
CREATE UNIQUE INDEX idx_meals_notion ON meals(notion_id) WHERE notion_id IS NOT NULL;
CREATE TABLE intimacy_levels (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,            -- e.g. "5(편한친구)"
  cycle_months REAL,                     -- 권장 만남 주기(개월)
  criteria     TEXT,                     -- 기준
  notion_id    TEXT UNIQUE
);
CREATE TABLE persons (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  relation        TEXT,                  -- 관계 select (친구/비즈니스/연인…)
  intimacy_id     INTEGER REFERENCES intimacy_levels(id),
  birthday        TEXT,                  -- YYYY-MM-DD
  residence       TEXT,
  contact         TEXT,
  job             TEXT,                  -- 하는일
  mbti            TEXT,                  -- comma-joined multi_select
  groups          TEXT,                  -- 같이 속한 그룹, comma-joined
  manual_priority REAL,
  notion_id       TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
, aliases TEXT);
CREATE UNIQUE INDEX idx_persons_notion ON persons(notion_id) WHERE notion_id IS NOT NULL;
CREATE TABLE appointments (
  id           INTEGER PRIMARY KEY,
  title        TEXT NOT NULL,
  start_at     TEXT,                     -- ISO with +09:00 as stored in Notion date.start
  end_at       TEXT,
  location     TEXT,                     -- plain place name
  location_url TEXT,                     -- naver map deep link (from rich_text link)
  purpose      TEXT,                     -- 목적 select
  summary      TEXT,                     -- 요약
  notion_id    TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX idx_appt_notion ON appointments(notion_id) WHERE notion_id IS NOT NULL;
CREATE INDEX idx_appt_start ON appointments(start_at);
CREATE TABLE appointment_persons (
  appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
  person_id      INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  PRIMARY KEY (appointment_id, person_id)
);
CREATE TABLE tasks (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  area_id    INTEGER REFERENCES areas(id),
  due_start  TEXT,                       -- 예정/완료 날짜 date.start (may be datetime)
  due_end    TEXT,
  done       INTEGER NOT NULL DEFAULT 0, -- 완료 checkbox
  is_routine INTEGER NOT NULL DEFAULT 0, -- 루틴 checkbox
  priority   TEXT,                       -- P0/P1/P2
  seq        REAL,                       -- 순번
  note       TEXT,                       -- 비고
  notion_id  TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  archived_at TEXT                        -- soft-delete marker (task-archive, 002); NULL = active
);
CREATE UNIQUE INDEX idx_tasks_notion ON tasks(notion_id) WHERE notion_id IS NOT NULL;
CREATE INDEX idx_tasks_due ON tasks(due_start);
CREATE INDEX idx_tasks_done ON tasks(done);
CREATE TABLE workout_classifications (
  workout_id INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
  type_id    INTEGER NOT NULL REFERENCES workout_types(id),
  PRIMARY KEY (workout_id, type_id)
);
CREATE INDEX idx_wc_type ON workout_classifications(type_id);
CREATE TABLE metric_log (
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, metric TEXT NOT NULL,
  value REAL NOT NULL, note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(date, metric)
);
CREATE INDEX idx_metric_log_metric_date ON metric_log(metric, date);
CREATE TABLE config (
  key TEXT PRIMARY KEY, value TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ===========================================================================
-- DGN-179 event schema (folded migration 003; spec v5 LOCK 2026-07-07).
-- Three-layer event model: event (L1) / sub_event (L1-L2 link) /
-- reschedule_requests (M1). All time columns are canonical UTC
-- 'YYYY-MM-DDThh:mm:ssZ' (fixed 20 chars). Identical to migrations/
-- 003_event_schema.sql (IF NOT EXISTS -> idempotent). Fresh DBs are born at
-- user_version 4 (this file's PRAGMA above; the verb-delta added event_persons
-- as migration 004).
-- ===========================================================================

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
    -- verb-delta v2 MIN-5 belt CHECK: an all_day row must carry BOTH instants.
    -- all_day is stored as a display_tz midnight..next-midnight UTC instant
    -- range; a NULL-instant all_day is schema-legal under v5 but silently
    -- vanishes from appt_find's range compare. Forbid it for fresh DBs. Migrated
    -- DBs cannot receive this CHECK (SQLite has no ALTER ADD CONSTRAINT) -- the
    -- app validator (validate_all_day_instants) enforces it on the live path;
    -- the SDK never emits a NULL-instant all_day (all_day flows through
    -- all_day_instants).
    CHECK (schedule_kind != 'all_day'
           OR (start_at IS NOT NULL AND end_at IS NOT NULL)),
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

-- ===========================================================================
-- DGN-179 verb-delta (D2, folded migration 004; spec v2). event_persons
-- junction -- appointment participants for the unified event table (successor
-- of appointment_persons). Same-DB FK is INTEGER id. Identical to migrations/
-- 004_event_persons.sql (IF NOT EXISTS -> idempotent). Fresh DBs are born at
-- user_version 4 (this file's PRAGMA above).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS event_persons (
    event_id  INTEGER NOT NULL REFERENCES event(id)   ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_event_persons_person
    ON event_persons(person_id);

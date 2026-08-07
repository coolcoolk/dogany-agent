PRAGMA user_version = 11;
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

-- event (L1) -- unified task + appointment + presence (DGN-579 v10).
-- This DDL is byte-identical to migrations/010_unified_event.sql event_new
-- (first-ever fresh/migrated CHECK parity -- deliberate benefit of the 010
-- rebuild). Facets: kind (permanent subtype tag), time_mode (NEW),
-- slot/resolution/provenance derived, place_id FK (NEW).
CREATE TABLE IF NOT EXISTS event (
    id            INTEGER PRIMARY KEY,
    ulid          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,        -- 'task'|'appointment'|'presence'
    title         TEXT NOT NULL,
    note          TEXT,
    area_id       INTEGER REFERENCES areas(id),
    schedule_kind TEXT NOT NULL,        -- 'timed'|'all_day'|'untimed'
    start_at      TEXT,
    end_at        TEXT,
    display_tz    TEXT NOT NULL DEFAULT 'Asia/Seoul',
    open_ended    INTEGER NOT NULL DEFAULT 0,
    slot_exclusive INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    completion_rule TEXT NOT NULL DEFAULT 'all',
    completion_n  INTEGER,
    version       INTEGER NOT NULL DEFAULT 0,
    settled_at    TEXT,
    settled_by    TEXT,
    settled_outcome TEXT,
    owning_agent  TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    earliest      TEXT,
    latest        TEXT,
    anchor        TEXT,
    immovable     INTEGER NOT NULL DEFAULT 0,
    priority      TEXT,
    seq           REAL,
    is_routine    INTEGER DEFAULT 0,
    location      TEXT,
    location_url  TEXT,
    purpose       TEXT,
    summary       TEXT,
    recurrence_id TEXT,
    rec_date      TEXT,
    rec_exception INTEGER NOT NULL DEFAULT 0,
    notion_id     TEXT,
    gcal_event_id TEXT,
    gtask_id      TEXT,
    gcal_etag     TEXT,
    gtask_etag    TEXT,
    notify_policy   TEXT,
    notify_lead_min INTEGER,
    block_class    TEXT,                -- 'travel' | NULL (read: movement)
    derived_from   TEXT,                -- anchor event/presence ulid
    derived_role   TEXT,                -- 'before'|'after'|'before_prep'
    derived_pinned INTEGER NOT NULL DEFAULT 0,
    derived_delta  TEXT,
    -- NEW (DGN-579)
    time_mode     TEXT,                 -- 'anchored'|'due'|'floating'|NULL
    place_id      INTEGER REFERENCES place(id),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,

    -- carried v9 CHECKs, verbatim except the kind enum extension:
    CHECK (kind IN ('task','appointment','presence')),          -- EXTENDED
    CHECK (schedule_kind IN ('timed','all_day','untimed')),
    CHECK (NOT (kind = 'appointment' AND schedule_kind = 'untimed')),
    CHECK (schedule_kind != 'timed' OR start_at IS NOT NULL),
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
    CHECK (NOT (open_ended = 1 AND slot_exclusive = 1)),
    CHECK (NOT (slot_exclusive = 1 AND start_at IS NOT NULL
                AND end_at IS NULL AND open_ended = 0)),
    CHECK (NOT (schedule_kind = 'untimed'
                AND (start_at IS NOT NULL OR end_at IS NOT NULL))),
    CHECK (schedule_kind != 'all_day'
           OR (start_at IS NOT NULL AND end_at IS NOT NULL)),
    CHECK (status IN ('open','done','expired','abandoned')),
    CHECK (completion_rule IN ('all','manual')),
    CHECK ((settled_at IS NULL) = (settled_outcome IS NULL)),
    CHECK (settled_outcome IS NULL OR settled_outcome IN ('done','abandoned')),
    CHECK (end_at IS NULL OR start_at IS NULL OR end_at >= start_at),
    CHECK (notify_policy IS NULL OR notify_policy IN
           ('default','silent','start_only','custom')),
    CHECK (notify_lead_min IS NULL OR COALESCE(notify_policy,'') = 'custom'),
    CHECK (COALESCE(notify_policy,'') != 'custom'
           OR (notify_lead_min IS NOT NULL AND notify_lead_min >= 0)),

    -- NEW CHECKs (DGN-579; identical fresh + migrated):
    CHECK (time_mode IS NULL OR time_mode IN ('anchored','due','floating')),
    -- actionable rows carry a time_mode; presence must not:
    CHECK ((kind = 'presence') = (time_mode IS NULL)),
    -- floating = no time grid; only floating (or presence) may be untimed:
    CHECK (time_mode != 'floating' OR schedule_kind = 'untimed'),
    CHECK (schedule_kind != 'untimed' OR time_mode = 'floating'),
    -- presence invariants:
    CHECK (kind != 'presence' OR schedule_kind IN ('timed','all_day')),
    CHECK (kind != 'presence' OR place_id IS NOT NULL),
    CHECK (kind != 'presence' OR slot_exclusive = 0),
    CHECK (kind != 'presence' OR settled_outcome IS NULL
           OR settled_outcome = 'abandoned'),
    -- grill B6(b): a presence span must have a bounded end. Open-ended
    -- presence (ongoing trip) is FORBIDDEN in v1 -- extend end_at instead.
    CHECK (kind != 'presence' OR (end_at IS NOT NULL AND open_ended = 0)),
    -- anchored/due rows must have a start instant:
    CHECK (time_mode NOT IN ('anchored','due') OR start_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_notion
    ON event(notion_id) WHERE notion_id IS NOT NULL;
-- overlap-scan support: lead the index with the v5.1 liveness-filter columns.
CREATE INDEX IF NOT EXISTS idx_event_overlap
    ON event(slot_exclusive, settled_at, start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_event_kind ON event(kind);
-- DGN-579 facet indexes
CREATE INDEX IF NOT EXISTS idx_event_time_mode ON event(time_mode, start_at);
CREATE INDEX IF NOT EXISTS idx_event_place
    ON event(place_id) WHERE place_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_presence_live
    ON event(start_at, end_at) WHERE kind = 'presence' AND settled_at IS NULL;

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

-- ===========================================================================
-- DGN-240 routine recurrence (folded migration 006; spec v3 T7).
-- Creates routine_def, roller_log, projects, routine, session_type,
-- session_muscle tables. rec_date / rec_exception / mirror bookkeeping
-- columns are folded into the event CREATE TABLE above (fresh DBs born at
-- user_version 6 include them natively; migrated DBs get them via 005/006
-- ALTER ADD COLUMN). Identical to migrations/006_routine_recurrence.sql
-- (IF NOT EXISTS -> idempotent on all paths).
-- ===========================================================================

-- projects table (nullable FK target for routine_def.project_id)
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

-- routine_def: lifecycle anchor for recurring task series.
CREATE TABLE IF NOT EXISTS routine_def (
    id            INTEGER PRIMARY KEY,
    ulid          TEXT NOT NULL UNIQUE,
    recurrence_id TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'task',
    cadence       TEXT,
    schedule_kind TEXT NOT NULL,
    time_of_day   TEXT,
    duration_min  INTEGER,
    exclusive     INTEGER NOT NULL DEFAULT 0,
    display_tz    TEXT NOT NULL DEFAULT 'Asia/Seoul',
    area_id       INTEGER REFERENCES areas(id),
    project_id    INTEGER REFERENCES projects(id),
    purpose       TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    start_date    TEXT NOT NULL,
    end_date      TEXT,
    valid_until   TEXT NOT NULL,
    rule_effective_from TEXT NOT NULL,
    anomaly_ack   TEXT,
    -- DGN-273 notify policy home (migration 007). NULL = 'default'.
    notify_policy   TEXT,
    notify_lead_min INTEGER,
    version       INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    -- DGN-579: presence routine defs anchor a place (migration 010 rebuild).
    place_id      INTEGER REFERENCES place(id),
    CHECK (kind IN ('task','appointment','presence')),          -- EXTENDED
    CHECK (kind != 'presence' OR place_id IS NOT NULL),
    CHECK (schedule_kind IN ('timed','all_day')),
    CHECK (status IN ('active','paused','retired')),
    CHECK (schedule_kind != 'timed' OR time_of_day IS NOT NULL),
    CHECK (exclusive IN (0,1)),
    CHECK (cadence IS NOT NULL OR status = 'retired'),
    -- DGN-273 notify belt CHECKs (fresh DBs; verbs enforce on migrated DBs).
    CHECK (notify_policy IS NULL OR notify_policy IN
           ('default','silent','start_only','custom')),
    CHECK (notify_lead_min IS NULL OR COALESCE(notify_policy,'') = 'custom'),
    CHECK (COALESCE(notify_policy,'') != 'custom'
           OR (notify_lead_min IS NOT NULL AND notify_lead_min >= 0))
);

-- roller_log: nightly roller audit log.
CREATE TABLE IF NOT EXISTS roller_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    recurrence_id TEXT,
    category TEXT NOT NULL,
    detail TEXT
);

-- routine: workout session grouping (legacy).
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

-- Unique partial index: at most one live (recurrence_id, rec_date) pair per
-- recurrence series (prevents duplicate materialization races).
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_rec_live
    ON event(recurrence_id, rec_date)
    WHERE recurrence_id IS NOT NULL
      AND rec_date IS NOT NULL
      AND settled_at IS NULL
      AND rec_exception = 0;

-- ===========================================================================
-- DGN-274 travel blocks (folded migration 008; build spec v4 section 5.1).
-- event travel columns (block_class / derived_*) are folded into the event
-- CREATE TABLE above (fresh DBs born at user_version 8 include them natively;
-- migrated DBs get them via 008 ALTER ADD COLUMN). travel_rule / place tables
-- and the live dup guard index are identical to migrations/
-- 008_travel_blocks.sql (IF NOT EXISTS -> idempotent).
-- ===========================================================================
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_travel_live
    ON event(derived_from, derived_role)
    WHERE block_class = 'travel' AND settled_at IS NULL;
    -- live dup guard, mirrors idx_event_rec_live

CREATE TABLE IF NOT EXISTS travel_rule (
    id           INTEGER PRIMARY KEY,
    ulid         TEXT NOT NULL UNIQUE,
    anchor_type  TEXT NOT NULL,            -- 'def' (routine_def) | 'event'
    anchor_ulid  TEXT NOT NULL,            -- routine_def.ulid or event.ulid
    place_id     INTEGER REFERENCES place(id),   -- nullable
    before_min   INTEGER NOT NULL DEFAULT 0,     -- 0 = no before block
    after_min    INTEGER NOT NULL DEFAULT 0,     -- 0 = no after block
    prep_min     INTEGER NOT NULL DEFAULT 0,     -- prep before departure
                                                 -- (2.6; before block only)
    before_src   TEXT NOT NULL DEFAULT 'explicit',  -- 'place' | 'explicit'
    after_src    TEXT NOT NULL DEFAULT 'explicit',  -- (2.3 propagation)
    prep_src     TEXT NOT NULL DEFAULT 'explicit',
    status       TEXT NOT NULL DEFAULT 'active',
                 -- 'proposed' = deferred offer carrier (2.5, derives
                 --   nothing) | 'active' | 'retired'
    version      INTEGER NOT NULL DEFAULT 0,        -- CAS
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (anchor_type IN ('def','event')),
    CHECK (status IN ('proposed','active','retired')),
    CHECK (before_src IN ('place','explicit')),
    CHECK (after_src IN ('place','explicit')),
    CHECK (prep_src IN ('place','explicit')),
    CHECK (place_id IS NOT NULL
           OR (before_src = 'explicit' AND after_src = 'explicit'
               AND prep_src = 'explicit')),
    CHECK (before_min >= 0 AND after_min >= 0 AND prep_min >= 0),
    CHECK (before_min % 15 = 0 AND after_min % 15 = 0
           AND prep_min % 15 = 0),
    CHECK (status <> 'active' OR before_min > 0 OR after_min > 0)
           -- proposed may carry zero minutes (unknown place, 2.5);
           -- active must derive at least one block; prep alone does
           -- not make a rule active (prep rides the before block)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_travel_rule_anchor
    ON travel_rule(anchor_type, anchor_ulid) WHERE status != 'retired';
    -- one LIVE rule (proposed or active) per anchor

CREATE TABLE IF NOT EXISTS place (
    id           INTEGER PRIMARY KEY,
    ulid         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,            -- canonical user-language name
    aliases      TEXT,                     -- JSON array, location-text match
    to_min       INTEGER,                  -- travel to place (from base)
    from_min     INTEGER,                  -- travel back (asymmetric ok)
    prep_min     INTEGER,                  -- declared prep default (2.6)
    note         TEXT,                     -- e.g. 'provisional' (6)
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    -- DGN-579: base place (where the user sleeps). NO value CHECK by design:
    -- migrated DBs get this via ALTER ADD COLUMN (cannot add CHECK) and the
    -- 008 parity rule says fresh carries none either -- SDK enforces (0,1).
    is_base      INTEGER NOT NULL DEFAULT 0,
    CHECK (to_min IS NULL OR to_min % 15 = 0),
    CHECK (from_min IS NULL OR from_min % 15 = 0),
    CHECK (prep_min IS NULL OR (prep_min >= 0 AND prep_min % 15 = 0))
);
-- DGN-579: mechanical single-base guarantee.
CREATE UNIQUE INDEX IF NOT EXISTS idx_place_base
    ON place(is_base) WHERE is_base = 1;
-- DGN-579 base seed: fresh DBs are born with the implicit base place
-- (locked decisions 4/5). Idempotent; content-identical to migration 010.
INSERT INTO place (ulid, name, is_base, created_at, updated_at)
SELECT upper(substr(hex(randomblob(16)),1,26)), '집', 1,
       strftime('%Y-%m-%dT%H:%M:%SZ','now'),
       strftime('%Y-%m-%dT%H:%M:%SZ','now')
 WHERE NOT EXISTS (SELECT 1 FROM place WHERE is_base = 1)
   AND NOT EXISTS (SELECT 1 FROM place WHERE name = '집');

-- ===========================================================================
-- DGN-553 consumption module v1 (folded migration 009; spec section 3.1)
-- + DGN-564 income delta (folded migration 011). spend_category / spend_entry
-- tables + indexes are identical to migrations/009_spend_tables.sql plus the
-- 011 kind columns (IF NOT EXISTS -> idempotent). The category seed is folded
-- too (INSERT OR IGNORE on the UNIQUE name -> idempotent): a fresh DB born at
-- user_version 11 must be content-identical to a migrated (009->010->011) DB
-- (fixed-cost defaults + kind ride the seed rows). The kind column sits LAST
-- in each column list: 011's ALTER appends it, so fresh column ordering
-- matches migrated DBs. kind has NO CHECK by design (sqlite cannot ALTER a
-- CHECK; SDK/verb layer enforces 'expense'|'income' -- 'source' precedent).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS spend_category (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,   -- Korean label (user-facing)
    is_fixed_default INTEGER NOT NULL DEFAULT 0,
                     -- category-level fixed-cost default; spend-add copies it
                     -- onto the row unless caller overrides (grill r1 M2)
    sort             INTEGER NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1,
    kind             TEXT NOT NULL DEFAULT 'expense',  -- 'expense'|'income' (011)
    CHECK (is_fixed_default IN (0,1))
);
-- seed: names + is_fixed_default assignments owner-confirmed (OQ 5, dec-096).
-- kind rides the DEFAULT ('expense') exactly as 011's ALTER backfills it.
INSERT OR IGNORE INTO spend_category (name, is_fixed_default, sort) VALUES
  ('식비',0,10),('카페/간식',0,11),('교통',0,20),('주거/공과금',1,30),
  ('구독',1,31),('통신',1,32),('생활용품',0,40),('의류/미용',0,41),
  ('문화/여가',0,50),('건강/의료',0,60),('경조/선물',0,70),('기타',0,99);
-- income seed (folded migration 011): sort band 110-119, clear of the
-- expense band (10..99). 급여 carries is_fixed_default=1 (regular salary).
INSERT OR IGNORE INTO spend_category (name, is_fixed_default, sort, kind) VALUES
  ('급여',1,110,'income'),('이자·배당',0,111,'income'),
  ('환급',0,112,'income'),('용돈·이체',0,113,'income'),
  ('기타수입',0,119,'income');

CREATE TABLE IF NOT EXISTS spend_entry (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,          -- YYYY-MM-DD (KST), lifekit convention
    amount      INTEGER NOT NULL,       -- KRW; expense > 0, refund/cancel < 0
    name        TEXT NOT NULL,          -- item description, user language
    category_id INTEGER REFERENCES spend_category(id),
    method      TEXT,                   -- free text v1: card/account/cash label
    is_fixed    INTEGER NOT NULL DEFAULT 0,  -- row value; default copied from
                                             -- category is_fixed_default
    scope       TEXT NOT NULL DEFAULT 'personal',
                -- 'personal' | 'business' | 'unknown'
                -- business rows: recorded, EXCLUDED from personal reports,
                -- exportable when bizkit (DGN-464) lands (boundary = OQ 1).
                -- 'unknown' rows are swept at every C2 session (3.4).
    source      TEXT NOT NULL DEFAULT 'chat',
                -- v1 values: 'chat' | 'batch'. NO CHECK by design: sqlite
                -- cannot ALTER a CHECK, and v2 adapters add 'import'/'receipt'
                -- without a table rebuild. SDK/verb layer enforces the value
                -- set (007/008 "SDK enforces" precedent). (grill r1 m3)
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    kind        TEXT NOT NULL DEFAULT 'expense',
                -- 'expense' | 'income' (011). Income rows: amount POSITIVE.
                -- NO CHECK (source precedent); SDK/verb layer enforces.
    CHECK (scope IN ('personal','business','unknown')),
    CHECK (is_fixed IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_spend_date ON spend_entry(date);
CREATE INDEX IF NOT EXISTS idx_spend_cat  ON spend_entry(category_id);

-- ===========================================================================
-- DGN-579 unified event model (folded migration 010; spec v2 section A5).
-- Resolution facet is DERIVED over INSTANTS in this ONE view -- the status
-- cache is NOT consulted (no expiry sweep exists; an untouched row's cache
-- can be stale forever). missed := anchored AND unsettled AND eff_end <= now.
-- Late settle flips missed -> happened by design. Overdue 'due' rows stay
-- 'open' (backlog pressure, not a miss). Presence excluded (liveness =
-- span vs now, not a resolution). The inline CASE is EFF_END_SQL verbatim
-- (INF sentinel '~'x20 sorts above every canonical timestamp).
-- ===========================================================================
CREATE VIEW IF NOT EXISTS v_event_resolution AS
SELECT e.id, e.ulid, e.kind, e.time_mode,
  CASE
    WHEN e.settled_outcome = 'abandoned' THEN 'skipped'
    WHEN e.settled_outcome = 'done' THEN
         CASE WHEN e.time_mode = 'anchored' THEN 'happened' ELSE 'done' END
    WHEN e.time_mode = 'anchored'
         AND (CASE WHEN e.end_at IS NULL THEN '~~~~~~~~~~~~~~~~~~~~'
                   WHEN e.end_at = e.start_at THEN
                        strftime('%Y-%m-%dT%H:%M:%SZ', e.start_at, '+1 second')
                   ELSE e.end_at END)
             <= strftime('%Y-%m-%dT%H:%M:%SZ','now')
      THEN 'missed'
    ELSE 'open'
  END AS resolution
FROM event e
WHERE e.kind != 'presence';

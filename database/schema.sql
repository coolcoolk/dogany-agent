PRAGMA user_version = 2;
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
  archived_at TEXT,                      -- soft-delete marker (task-archive); NULL = active
  notion_id  TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
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

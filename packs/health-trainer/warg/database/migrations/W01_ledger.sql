-- W01: Warg instance-local migration (not a framework migration;
-- promotion to canonical schema = product work, post-pilot).
-- DGN-238 v3 section 1.2, verbatim. Does NOT touch PRAGMA user_version
-- (framework numbering stays untouched; 006 is DGN-240's).
-- Apply gate: lib/ledger.py apply_w01() -- apply-once, sqlite_master guarded.

CREATE TABLE ledger_goal (
    id           INTEGER PRIMARY KEY,
    ulid         TEXT NOT NULL UNIQUE,
    layer        TEXT NOT NULL,      -- 'long'|'mid'|'short'|'event_overlay'
    parent_id    INTEGER REFERENCES ledger_goal(id),
    title        TEXT NOT NULL,      -- user language
    detail       TEXT,               -- JSON per layer:
                                     -- long: {archetype, tradeoff_priority}
                                     -- mid:  {phase_kind, min_weeks, freq_per_week}
                                     -- short:{habit, freq_per_week, window_weeks}
                                     -- event_overlay: {program, trigger_event}
    status       TEXT NOT NULL DEFAULT 'proposed',
                 -- 'proposed'|'active'|'suspended'|'completed'|'failed'
                 -- |'superseded'|'archived'
    starts_on    TEXT,
    ends_on      TEXT,               -- event_overlay: NOT NULL (deadline-bound)
    resume_to_id INTEGER REFERENCES ledger_goal(id),  -- overlay revert target
    source       TEXT NOT NULL,      -- 'interview'|'migration-digest'|'review'|'user'
    recorded_at  TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 0,          -- CAS
    CHECK (layer IN ('long','mid','short','event_overlay')),
    CHECK (layer != 'event_overlay' OR ends_on IS NOT NULL),
    -- grill-2 MAJOR-5(c): an overlay ALWAYS knows its revert target
    CHECK (layer != 'event_overlay' OR resume_to_id IS NOT NULL),
    CHECK (status IN ('proposed','active','suspended','completed','failed',
                      'superseded','archived'))
);
-- exactly one ACTIVE goal per base layer; suspended rows do not collide
CREATE UNIQUE INDEX idx_goal_active_layer ON ledger_goal(layer)
    WHERE status = 'active' AND layer IN ('long','mid','short');
-- grill-2 MAJOR-5(a): at most ONE active overlay in v1 (no stacking)
CREATE UNIQUE INDEX idx_overlay_single_active ON ledger_goal(layer)
    WHERE status = 'active' AND layer = 'event_overlay';

CREATE TABLE ledger_constraint (
    id          INTEGER PRIMARY KEY,
    ulid        TEXT NOT NULL UNIQUE,
    class       TEXT NOT NULL,       -- 'nutrition'|'safety'|'time'
    key         TEXT NOT NULL,       -- 'kcal_deficit','protein_g','max_deficit_cap',...
    value       TEXT NOT NULL,
    goal_id     INTEGER REFERENCES ledger_goal(id),  -- nutrition: owning phase
    inviolable  INTEGER NOT NULL DEFAULT 0,          -- safety quadrant = 1
    status      TEXT NOT NULL DEFAULT 'active',      -- 'active'|'superseded'
    source      TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    CHECK (class IN ('nutrition','safety','time')),
    CHECK (inviolable IN (0,1))
);

CREATE TABLE ledger_resource (
    id          INTEGER PRIMARY KEY,
    ulid        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,       -- 'pantry'|'supplement'|'slot'|'budget'|'env'
    name        TEXT NOT NULL,
    detail      TEXT,                -- qty / dose / schedule (JSON)
    volatile    INTEGER NOT NULL DEFAULT 0,  -- pantry = 1 (day-level freshness)
    as_of       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    source      TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE ledger_preference (
    id          INTEGER PRIMARY KEY,
    ulid        TEXT NOT NULL UNIQUE,
    item        TEXT NOT NULL,
    polarity    TEXT NOT NULL,       -- 'inviolable'|'negotiable'
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    source      TEXT NOT NULL,       -- accrual only, never questionnaire (D-rule)
    recorded_at TEXT NOT NULL,
    CHECK (polarity IN ('inviolable','negotiable'))
);

CREATE TABLE ledger_audit (          -- transition trail (phase death survives here)
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,  -- 'phase_start'|'phase_fail'|'habit_restart'
                              -- |'overlay_start'|'overlay_expire'|'ladder_review'
                              -- |'overlay_noop'|'overlay_orphaned'|'overlay_gc'
    goal_ulid TEXT,
    detail    TEXT
);

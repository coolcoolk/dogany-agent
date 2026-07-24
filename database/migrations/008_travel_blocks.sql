-- 008: DGN-274 travel blocks -- derived travel-time blocks + travel_rule /
-- place tables (build spec v4 section 5.1, verbatim).
--
-- event additive columns (sqlite ALTER cannot add CHECK -- SDK enforces,
-- 006 precedent):
--   block_class    'travel' | NULL   -- discriminator (4.4)
--   derived_from   anchor event ulid
--   derived_role   'before' | 'after'
--   derived_pinned 0/1               -- user pin (3.3)
--   derived_delta  JSON {"offset_min","duration_min"} -- pin snapshot;
--                  CLI pin engraves in the SAME txn; inbound pin engraves
--                  lazily (3.3)
--
-- Convention per 007: .bail on, busy_timeout, single txn, version bump.
-- Lockstep obligations on live landing (5.5): mirror ALLOWED_USER_VERSIONS
-- (7,)->(7,8), Warg L1 pin 7->8, vendored copies sync.

.bail on
PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE event ADD COLUMN block_class    TEXT;
ALTER TABLE event ADD COLUMN derived_from   TEXT;
ALTER TABLE event ADD COLUMN derived_role   TEXT;
ALTER TABLE event ADD COLUMN derived_pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE event ADD COLUMN derived_delta  TEXT;

CREATE UNIQUE INDEX idx_event_travel_live
    ON event(derived_from, derived_role)
    WHERE block_class = 'travel' AND settled_at IS NULL;
    -- live dup guard, mirrors idx_event_rec_live

CREATE TABLE travel_rule (
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
CREATE UNIQUE INDEX idx_travel_rule_anchor
    ON travel_rule(anchor_type, anchor_ulid) WHERE status != 'retired';
    -- one LIVE rule (proposed or active) per anchor

CREATE TABLE place (
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
    CHECK (to_min IS NULL OR to_min % 15 = 0),
    CHECK (from_min IS NULL OR from_min % 15 = 0),
    CHECK (prep_min IS NULL OR (prep_min >= 0 AND prep_min % 15 = 0))
);

PRAGMA user_version = 8;

COMMIT;

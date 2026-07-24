-- 009: DGN-553 consumption module v1 -- spend_entry + spend_category
-- (first finance-domain table family; spec locked v1 section 3.1, verbatim).
--
-- Precondition: M0 reconcile done (canonical at user_version 8, 008 upstreamed).
-- Convention per 007/008: .bail on, busy_timeout, single txn, version bump.
-- Lockstep obligations on live landing: mirror ALLOWED_USER_VERSIONS += 9
-- (rollout-window tuple fixed at M2), Warg L1 pin update, vendored copies sync.
-- No areas(id) FK by design (DGN-461 name-layer retirement; see spec 2.4).

.bail on
PRAGMA busy_timeout = 10000;

BEGIN;

CREATE TABLE spend_category (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,   -- Korean label (user-facing)
    is_fixed_default INTEGER NOT NULL DEFAULT 0,
                     -- category-level fixed-cost default; spend-add copies it
                     -- onto the row unless caller overrides (grill r1 M2)
    sort             INTEGER NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1,
    CHECK (is_fixed_default IN (0,1))
);
-- seed: names + is_fixed_default assignments owner-confirmed (OQ 5, dec-096)
INSERT INTO spend_category (name, is_fixed_default, sort) VALUES
  ('식비',0,10),('카페/간식',0,11),('교통',0,20),('주거/공과금',1,30),
  ('구독',1,31),('통신',1,32),('생활용품',0,40),('의류/미용',0,41),
  ('문화/여가',0,50),('건강/의료',0,60),('경조/선물',0,70),('기타',0,99);

CREATE TABLE spend_entry (
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
    CHECK (scope IN ('personal','business','unknown')),
    CHECK (is_fixed IN (0,1))
);
CREATE INDEX idx_spend_date ON spend_entry(date);
CREATE INDEX idx_spend_cat  ON spend_entry(category_id);

PRAGMA user_version = 9;

COMMIT;

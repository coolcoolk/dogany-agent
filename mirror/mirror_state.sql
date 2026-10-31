-- DGN-180 mirror_state schema -- sandbox.
-- Persists bootstrap ids, sync cursors, per-event push hashes.
-- English/ASCII only.

CREATE TABLE IF NOT EXISTS mirror_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- mirror_outbox: durable push queue (reschedule_requests pattern).
-- requeues: reconcile-driven re-attempts of failed rows (X10b item 6).
CREATE TABLE IF NOT EXISTS mirror_outbox (
    id           INTEGER PRIMARY KEY,
    event_ulid   TEXT NOT NULL,
    op           TEXT NOT NULL DEFAULT 'sync',  -- always 'sync'
    status       TEXT NOT NULL DEFAULT 'queued', -- queued/claimed/pushed/failed
    lease_at     TEXT,                           -- claim timestamp (ISO UTC)
    attempts     INTEGER NOT NULL DEFAULT 0,
    requeues     INTEGER NOT NULL DEFAULT 0,
    dead         INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (status IN ('queued','claimed','pushed','failed'))
);

-- notify_outbox: sandbox notification sink (live cutover = push.sh drain).
CREATE TABLE IF NOT EXISTS notify_outbox (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    event_ulid TEXT,
    message    TEXT NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0,
    CHECK (delivered IN (0,1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_ulid_pending
    ON mirror_outbox(event_ulid) WHERE status IN ('queued','claimed');

-- per-event push snapshot for echo suppression AND per-field 3-way base (W9).
-- field_json = full canonical surface projection (sqlite representation) at
-- the last reconcile point; field_hash = sha256 of that JSON (fast echo path).
CREATE TABLE IF NOT EXISTS push_snapshot (
    event_ulid   TEXT PRIMARY KEY,
    surface      TEXT NOT NULL,   -- 'calendar' or 'tasks'
    field_hash   TEXT NOT NULL,   -- sha256 hex of canonical projection JSON
    field_json   TEXT NOT NULL DEFAULT '{}',
    pushed_at    TEXT NOT NULL
);

-- audit log: 3-way conflicts, bypass overlap notices, foreign skips,
-- circuit breaker trips, outbox exhaustion (one line each).
CREATE TABLE IF NOT EXISTS mirror_log (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    event_ulid TEXT,
    category   TEXT NOT NULL,
    detail     TEXT
);

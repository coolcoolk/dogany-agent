-- FIXTURE: schema.sql with mismatched PRAGMA user_version
PRAGMA user_version = 3;

CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY);

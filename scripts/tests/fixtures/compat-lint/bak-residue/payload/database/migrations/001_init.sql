-- FIXTURE: migration 001 for bak-residue C4b test (DGN-868)
-- reversible: DROP TABLE items
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY);

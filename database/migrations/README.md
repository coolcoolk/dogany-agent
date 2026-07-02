# lifekit.db migrations

Forward-only, versioned schema migrations for `lifekit.db`, applied by
`update.sh` using SQLite's built-in `PRAGMA user_version` as the version marker.

## Version model

- A DB freshly created from `../schema.sql` is **version 1** (the schema file
  sets `PRAGMA user_version = 1;` at its top).
- `001` is therefore RESERVED: the `schema.sql` baseline *is* version 1. There is
  no `001_*.sql` file. Real migrations start at **002**.

## File convention

- Name each migration `NNN_description.sql`, zero-padded 3 digits, ascending
  (e.g. `002_add_foo_column.sql`, `003_backfill_bar.sql`).
- Each migration MUST end with `PRAGMA user_version = NNN;` where `NNN` matches
  the file's number. This records the DB as upgraded to that version.
- Write migrations idempotent-friendly where the SQL allows it (e.g. guard
  additive DDL so a partial/rerun does not hard-fail). `update.sh` only applies a
  migration whose `NNN` is strictly greater than the DB's current
  `user_version`, so a fully-applied migration is never re-run.

## How update.sh applies them

For an instance whose `database/lifekit.db` exists, `update.sh`:

1. reads the current version: `sqlite3 "$DB" 'PRAGMA user_version;'`
2. for each `migrations/NNN_*.sql` with `NNN > current`, in ascending numeric
   order:
   - **backs up the DB first** (copy to `database/lifekit.db.bak-<timestamp>`),
   - applies it: `sqlite3 "$DB" < migration`,
3. if no migrations are pending, does nothing (silent).

Under `--dry-run`, `update.sh` prints `would apply migration NNN` instead of
touching the DB.

The `*.db` file is never deleted or overwritten except through these controlled,
backed-up migrations.

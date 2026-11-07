# Transcript recall archive

Archive selected rows from the current `transcript_notes` recall cache:

```sh
python3 memory.py transcript-archive --match 'topic text' --dry-run
python3 memory.py transcript-archive --match 'topic text'
python3 memory.py transcript-archive --session SESSION_ID
python3 memory.py transcript-archive --since 2026-11-01 --until 2026-11-03
python3 memory.py transcript-list
python3 memory.py transcript-restore transcript-ARCHIVE_ID
```

Choose exactly one selector. Match is a literal, case-sensitive substring (SQL
wildcards have no special meaning). Session is an exact session ID. Dates are
inclusive local calendar dates, including the entire `--until` day; both are
required. These commands select cached rows, not every raw historical message.
Dry run reports a count and up to five numbered samples without changing the
database, archive files, or ingest watermark. It refuses a pending journal;
`transcript-list` can recover that journal before retrying the preview.

Each nonempty archive operation returns one archive ID for the whole selection.
`transcript-list` shows numbered selections, their IDs, counts, timestamps and
selectors. Restore takes that ID and returns the exact saved timestamp, role,
session and text. It preserves rowids when available; a rebuilt cache may have
reused them. A conflicting active row is refused, not overwritten. Repeating
restore on an already restored selection is an error. Normal cache retention
continues after restore.

Archived rows are removed from the cache and its FTS index, excluding them from
search, today browse and both same-day recall hooks. The recent raw-history
fallback also honors exclusions, so archived content stays excluded tomorrow.
Raw transcripts and markdown notes are never modified by these verbs.

The durable source is `archive/transcripts.json` under `MEMORY_DATA_ROOT`
(normally `data/memory-engine/`). Keep this directory when backing up or moving
an instance; `state.db` alone is not an archive backup. Snapshots include a
payload hash and exclusions use session + timestamp + role + SHA-256 of the
exact indexed (secret-redacted) text. Changed text at the same position is a
different identity. Restored snapshots remain in the manifest for recovery.

Changes use `archive_engine`'s transaction lock, recovery journal and audit
stream. Journal commit precedes cache changes; database initialization and
transcript ingestion reconcile incomplete operations. A new database retains
all archived exclusions. The `transcript_archive_applied` table tracks restores
already applied to a cache so they do not defeat ordinary retention. Ingestion
and archive/restore share the lock to prevent concurrent ingest resurrection.

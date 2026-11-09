# Dispatch archive index and retention

`dispatch-archive.py` sits beside `dispatch-detached.sh`. Python 3 stdlib only.
It scans every direct, non-symlink `dsp-*` directory in the archive and its
configured Trash namespace. It never executes `run.env` or reads stream contents.
The cited local index commit 15511a8d was unavailable in canonical history;
this implementation uses the current dispatch status/run.env/brief.md/out.log contract.

```sh
python3 routines/dispatch-archive.py index --archive .telegram_bot/dispatch > /tmp/dispatch-index.jsonl
python3 routines/dispatch-archive.py retain --archive .telegram_bot/dispatch
python3 routines/dispatch-archive.py retain --archive .telegram_bot/dispatch --apply
```

Index output: one JSON object per run, including current path/location, routing,
result, timestamps/duration, commit outcome, recursive file sizes (including the
size of stream.jsonl), brief prefix and final-report tail (4096 bytes each).
`out.log` is the dispatch report artifact; its tail can contain diagnostic text.
Missing/corrupt status still produces an index row with `status_issue`.
Symlink directories/files are excluded. The index is disposable, never a source
of truth. Regenerate after retention to get current Trash paths; no cached index
is consulted for retention. Preserve raw run directories to reconstruct it.

Retention uses **ended_at**, timezone required, strictly more than 30 * 86400
seconds ago. Completed and failed terminal runs have the same policy. Active,
unknown, missing/corrupt status, and undated runs stay for manual inspection.
The index marks `pending_review` when status.json has `pending_review: true`,
`review_state: "pending-review"`, result `pending-review`, or commits with no
explicit review state. Such runs stay, including failed runs. After review the
owning dispatcher may record `review_state: "reviewed"` and clear pending flags
in the source status; editing a generated JSONL file has no effect.

Dry-run is default and writes nothing. `--apply` moves eligible directories,
intact, using rename only. There is no deletion or cross-filesystem copy/delete
fallback. Default Trash is `~/.Trash/dogany-dispatch/<archive-path-hash>/`.
`--trash /path/to/Trash/namespace` explicitly selects another dedicated namespace
(e.g. a fixture directory or same-volume Trash on Linux). Always supply the same
archive and Trash paths when indexing and retaining. They must be separate trees.
An existing destination refuses the batch, preserving both copies. Apply uses
an archive lock directory to serialize this tool; a crash can leave the lock,
which the dispatcher must inspect before clearing. Concurrent external writers
must not edit old terminal records during apply. Permission/device/move errors
return nonzero; rerun the index to see any successful moves before the error.

Recovery: locate the run through `index`, then move its whole directory from
its reported Trash path back to the archive after checking for a name collision.
Emptying the system Trash removes recoverability; this tool never empties it.
If a person empties Trash, a regenerable index cannot retain those removed runs.

No schedule is installed. Dispatcher action: review dry-run on the real archive,
then arrange a daily `retain --apply` followed by `index` regeneration using the
same explicit paths. Live application, scheduling and release propagation are
outside the detached worker's authority. Without that scheduling the repository
implementation alone does not bound the live archive's growth.

Owning fast Tier-1 fixture suite:
`bash git-hooks/run-tests.sh --gate python3 agents/.template/routines/tests/test-dispatch-archive.py`.
Clock injection: `--now 2026-10-23T00:00:00Z`. No sleep or host archive required.

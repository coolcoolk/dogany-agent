# Dispatch run storage

Every local worker and script job inherits `DOGANY_RUN_SCRATCH=$TMPDIR`,
pointing to `<workspace>/.telegram_bot/dispatch/<runid>/tmp`. The injected
worker contract requires large clones, rehearsals, test fixtures and browser
profiles/screenshots to live there. Existing sandbox grants open that path.
`DOGANY_TEST_SCRATCH` points to its `tests` child so the test runner also
stays inside the run root. Run records, reports and logs remain outside scratch.

Completion, cancellation (including the dead-worker backstop), failure and
replacement move scratch to `<rundir>/reclaim/scratch`. Clean worktrees move
with `git worktree move` to `<rundir>/reclaim/worktree`; their branch refs stay.
A receipt records retirement time, original checkout, HEAD and branch. Failed
moves retain their sources and can be retried. Interrupted moves with a
finalization receipt are retried by the sweep. A changed retired checkout is
kept for recovery.

A sweep deletes retired resources after their retention window. It runs on
new dispatch admission, `--list`, `--cancel`, explicit
`bash routines/dispatch-detached.sh --reclaim`, and the existing nightly
`cleanup-files.sh` host reaper. No new timer or service is installed: an idle
host reclaims on its next nightly sweep, not at an exact wall-clock deadline.
The storage helper's `sweep --workspace <path>` previews; `--apply` reclaims.

| agent.conf setting | Environment override | Default |
| --- | --- | --- |
| `DISPATCH_RECLAIM_HOURS` | `DOGANY_DISPATCH_RECLAIM_HOURS` | 3 hours |
| `DISPATCH_MIN_FREE_GB` | `DOGANY_DISPATCH_MIN_FREE_GB` | 10 GiB |
| `DISPATCH_PRESERVE_WARN_HOURS` | `DOGANY_DISPATCH_PRESERVE_WARN_HOURS` | 24 hours |

Values must be finite nonnegative numbers. Local admission checks free space
on the workspace and worktree filesystems before holding a slot or creating a
checkout, and repeats after runtime fallback. Low space refuses with exit 2
and a one-line free/floor/path reason. Cloud and codex-cloud skip this check.

For evidence, create `$DOGANY_RUN_SCRATCH/.keep` with a reason before the run
ends, or have the dispatcher create `<rundir>/keep`. Either retains both
resources. The scratch marker moves with the evidence; remove it from
`<rundir>/reclaim/scratch/.keep` after recovery. A worktree on the existing
`preserve=kept-worktree` path stays at its original location until the recovery owner
recovers it. Neither the new sweep nor the legacy nightly reaper expires it;
old preserved worktrees produce age warnings.

User append/immutable flags (`uappnd`/`uappend`, `uchg`) are cleared only on
owned inodes within marked run scratch. Symlinks are not followed; flagged hard links
and foreign-owned inodes refuse reclamation. Ordinary hard links, including
local Git clone objects, are unlinked without changing shared flags or modes.
Flags in worktrees or other paths are never cleared. A failed reclaim reports its retained path and is
retryable, including partial deletes that removed the inner scratch marker:
the protected reclaim receipt authorizes the remaining run-owned contents.
Sweeps never delete branch refs or sweep unrelated temp directories.

Verification: `tests/dispatch_storage_selftest.py` drives the production
finalizer, dead-worker cancel, replacement, nightly reaper, native macOS flags,
disk admission, marker retention, preserved checkout and interrupted move.
Its `--mutations` mode first verifies an isolated green copy, then requires
broken retention, cleanup, flag, preservation and admission guards to re-red.
Run both through `git-hooks/run-tests.sh --gate`.

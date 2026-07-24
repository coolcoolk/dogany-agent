# bridge upstream

This `bridge/` directory is a VENDORED (in-tree) copy of the standalone Telegram
<-> Claude bridge so that `dogany-agent` runs immediately after a plain
`git clone` with no extra `--recursive` step.

- Upstream: https://github.com/coolcoolk/claude-code-telegram
- Pinned commit: feca63efc507f820774be6be036aa1695113c950
- Vendor-rev: DGN-399 (ensure_owner_stream bootstrap; pushed OSS),
  DGN-460 (max_buffer_size env-configurable, CLAUDE_MAX_BUFFER_SIZE default
  16MB, fixes 1MB SDK transport buffer crash on large tool results; pushed
  OSS 01764ee),
  DGN-541 (dashboard.py debounced empty-delete pin state machine; OSS commit
  883841f, local -- OSS push pending owner approval). Base pin held
  (pre-existing STREAM_INTERIM drift vs OSS HEAD).

## Why vendored instead of a git submodule

A submodule would leave `bridge/` empty on a plain `git clone` (without
`--recursive`), which breaks the "self-contained, runs when cloned standalone"
goal of this repo. The bridge is therefore vendored. Submodule wiring is
deferred; if this repo later wants the bridge as a submodule, remove this
directory and run:

    git submodule add https://github.com/coolcoolk/claude-code-telegram bridge
    cd bridge && git checkout feca63efc507f820774be6be036aa1695113c950

To refresh the vendored copy from upstream, re-copy the upstream tree over this
directory (excluding any real `.env`, `venv/`, `__pycache__/`).

## Pin discipline (DGN-385 MAJOR-1)

Any canonical change to files under `bridge/` in this repo MUST bump the
"Pinned commit" line above (or add a `Vendor-rev: <marker>` line in the same
commit).

Rationale: `update.sh` detects whether an instance's bridge is "locally ahead"
by comparing the pin in the instance's `bridge/UPSTREAM.md` with the pin in
this template file.  If the pins are equal and rsync shows a diff, the script
concludes the instance has local patches and skips the rsync to avoid a silent
downgrade.  An unbumped canonical bridge change therefore makes every instance
misread the update as local drift and silently skip it -- the fix never lands.
Always bump the pin (or add a `Vendor-rev` marker) in the same commit as the
bridge change.

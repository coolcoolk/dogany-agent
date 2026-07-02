#!/bin/bash
# __AGENT_LABEL__ nightly consolidate -- runs once a day, off-hours.
# Transcript delta -> Sonnet compression -> rule/two-stage filter -> dedup ->
# plain append into inbox.md -> silent report.
# Only reads conversations after the watermark (state.db), so each run handles
# only the new conversations. Runs quietly while the user is asleep.
# No topic routing here -- the nightly pass always writes to inbox.md; the weekly
# classify-inbox distributes it into topic files.
# Paths are resolved relative to the script's own location (dynamic), so the job
# survives a workspace move.
cd "$(dirname "$0")/../memory-engine" || exit 1
# dedup / neighbor lookup assume a fresh notes index -- the search hook does not
# run at night, so re-index explicitly.
/usr/bin/python3 memory.py index
/usr/bin/python3 memory.py consolidate

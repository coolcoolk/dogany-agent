#!/bin/bash
# daily-0405.sh -- Warg daily job (v3 section 4 daily line + dec-013
# backstop sweep #1). Order:
#   1. lib/daily_job.py : overlay expiry state machine + volatile stale
#      marking + consult watchdog + handoff retention (monthly)
#   2. handoff-consume.sh : inbox sweep (04:05 backstop)
#
# The two steps are FAILURE-DECOUPLED (grill-final MINOR-2): a daily_job
# crash (e.g. a db version gate refusal) must not take backstop sweep #1
# down with it. Both always run; the exit code stays nonzero when either
# failed (launchd LastExitStatus surfaces it).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WARG_ROOT="${WARG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LIB_DIR="${HANDOFF_LIB_DIR:-$SCRIPT_DIR/lib}"

rc=0
python3 "$LIB_DIR/daily_job.py" --root "$WARG_ROOT" || rc=$?
"$SCRIPT_DIR/handoff-consume.sh" || rc=$?
exit "$rc"

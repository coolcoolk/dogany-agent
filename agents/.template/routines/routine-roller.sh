#!/bin/bash
# DGN-240 routine roller wrapper (spec v3 2.1, T9).
# - flock single execution (inline materialize from verbs runs outside this
#   lock; the UNIQUE benign-skip in the engine absorbs the race, spec n3).
# - anomaly push seam (2.5): the python engine prints "ANOMALY_PUSH: ..."
#   lines; this wrapper aggregates them into ONE weekly ping via push.sh.
# Paths are resolved relative to this script's own location (dynamic), so the
# job survives a workspace move.
set -u

AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="/tmp/com.telegram-skill-bot.__AGENT_NAME__.routine-roller.lock"
ROLLER="$AGENT_ROOT/database/routine_roller.py"
PUSH="$AGENT_ROOT/routines/push.sh"

# macOS has no flock(1); use python fcntl via a lock-holder subshell
# (same guarantee: kernel lock released on process exit, stale-safe).
exec 9>"$LOCK_FILE"
if ! /usr/bin/python3 -c 'import fcntl,sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' 9>&9 2>/dev/null <&9; then
    echo "routine-roller: another run holds the lock, exiting" >&2
    exit 0
fi

OUT="$(/usr/bin/python3 "$ROLLER" 2>&1)"
RC=$?
echo "$OUT"

if [ $RC -ne 0 ]; then
    echo "routine-roller: engine failed rc=$RC" >&2
    exit $RC
fi

# aggregate anomaly lines into one weekly ping (never one push per routine)
ANOMALIES="$(echo "$OUT" | sed -n 's/^ANOMALY_PUSH: //p')"
if [ -n "$ANOMALIES" ] && [ -x "$PUSH" ]; then
    BODY="Routine weekly check: some scheduled items may have been missed.
$ANOMALIES
Will review in the weekly retro."
    "$PUSH" --text "$BODY" || echo "routine-roller: push failed (non-fatal)" >&2
fi

exit 0

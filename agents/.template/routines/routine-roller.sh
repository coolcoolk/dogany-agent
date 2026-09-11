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

# No routine_roller.py = the NORMAL state of a non-lifekit mint (the engine
# ships with the lifekit pack, DGN-803 LS-5; same tolerance contract as
# remind.sh / backup-data.sh) -> silent no-op, exit 0 (this is a cron unit;
# a hard fail here is pure error spam).
[ -f "$ROLLER" ] || exit 0

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
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
    # Do not treat this wording as final; a test pins this marker to the
    # string. The engine's ANOMALY_PUSH lines are raw internal item slugs and
    # used to be forwarded verbatim, in English. Only the COUNT is owner-
    # facing now; the lines go to stderr, the maintainer's rail.
    #
    # AMBIGUITY FIX (owner ruling 2026-09-11): the owner read the previous
    # text and could not tell whether it meant "the scheduler failed to run"
    # or "items you registered went undone". Measured answer: the SECOND.
    # These anomalies come out of the kit's own engine -- the roller this
    # wrapper drives ships with the lifekit pack (see the tolerance note near
    # the top of this file) -- so they are the owner's own recurring items;
    # and this branch is reached only AFTER the engine exited 0 above, so a
    # scheduler failure cannot produce them. The copy therefore (a) never uses
    # the bare word 루틴, which reads either way, (b) says whose items these
    # are, and (c) rules the machine reading out in so many words.
    ANOMALY_COUNT="$(printf '%s\n' "$ANOMALIES" | grep -c '[^[:space:]]' || true)"
    printf 'routine-roller: anomaly lines (owner text carries the count only):\n%s\n' "$ANOMALIES" >&2
    BODY="지난주에 직접 등록해 두신 반복 일정 중 ${ANOMALY_COUNT}개가 그냥 지나갔어요. 예약 실행이 실패한 게 아니라 실제로 한 기록이 없는 항목이에요. 주간 회고에서 함께 짚어드릴게요."
    "$PUSH" --text "$BODY" || echo "routine-roller: push failed (non-fatal)" >&2
fi

exit 0

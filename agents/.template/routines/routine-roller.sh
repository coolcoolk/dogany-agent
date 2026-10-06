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

# DGN-1669 (owner ruling 2026-09-23 19:53, relayed): resolve the interpreter
# instead of pinning /usr/bin/python3. That pin is macOS 3.9, and `holidays`
# dropped 3.9 after 0.83 -- so the pinned binary carries a table missing
# 2027-05-01 / 05-03 / 07-17 / 07-19 and materialises work blocks onto public
# holidays. The pin cannot be repaired by upgrading: 0.83 is the newest
# installable there, forever.
#
# The resolver already existed (scripts/pack/lib/py_resolve.sh, installer-side)
# and simply was not shipped to instances; it now lives here so routines share
# ONE floor instead of each hardcoding a number.
#
# Failure is LOUD and reaches the owner, not the stderr log: this is a cron
# unit whose stderr has zero readers (measured 2026-09-23), so a fail-closed
# refusal would otherwise trade silently-wrong holidays for silently-absent
# blocks -- the same defect wearing the opposite sign.
# The guard below is CHECKED, not incidental. A half-delivered update (roller
# present, lib absent) already degraded to this same refusal -- but only because
# `set -u` does not abort on a failed `.` and an undefined command returns
# non-zero. That is luck, and the next person who decides `set -e` looks tidier
# at the top of this file would silently convert it into a death with no push
# and no message. Naming the condition makes the behaviour survive that edit.
# Guard the SOURCE itself, not just its result. `.` is a POSIX special
# builtin: sourcing a missing file makes a non-interactive shell exit
# immediately, and `|| true` does NOT rescue it. Measured 2026-09-23 with
# set -eu: the trace ends on the `.` line with no output at all. Checking
# readability first is the only form that survives both set -u and set -e.
PY_RESOLVE_LIB="$AGENT_ROOT/routines/lib/py_resolve.sh"
if [ -r "$PY_RESOLVE_LIB" ]; then
    . "$PY_RESOLVE_LIB"
fi
if ! command -v pack_resolve_py3 >/dev/null 2>&1 || ! pack_resolve_py3; then
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate),
    # same status as the anomaly copy below. It deliberately says what was NOT
    # done and why, because the alternative failure here is silence.
    MSG="정기 일정 만들기를 잠시 멈췄어요. 이 기기에 필요한 파이썬(3.11 이상)이 없어서, 공휴일 표가 맞는지 확인할 수 없는 채로 일정을 만드는 대신 멈춰 뒀어요."
    # Style choice, NOT a set -e fix. An earlier version of this comment
    # claimed `[ -x "$PUSH" ] && { ...; }` dies here under set -e; that is
    # FALSE and was retracted 2026-09-23 (review GDYHE53E). set -e ignores a
    # failure in a non-final element of an && list, so the compound is
    # harmless in this position -- verified in isolation: the statement after
    # it still runs. The silent death that prompted this rewrite came solely
    # from sourcing a missing file below. Kept as an if for readability.
    if [ -x "$PUSH" ]; then
        "$PUSH" --text "$MSG" --audience owner >/dev/null 2>&1 || true
    fi
    echo "routine-roller: no Python 3.11+ interpreter -- refusing to roll with an unverifiable holiday table (DGN-1669)" >&2
    exit 1
fi
PY3="$PACK_PY3"

# macOS has no flock(1); use python fcntl via a lock-holder subshell
# (same guarantee: kernel lock released on process exit, stale-safe).
exec 9>"$LOCK_FILE"
if ! "$PY3" -c 'import fcntl,sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' 9>&9 2>/dev/null <&9; then
    echo "routine-roller: another run holds the lock, exiting" >&2
    exit 0
fi

OUT="$("$PY3" "$ROLLER" 2>&1)"
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
    "$PUSH" --text "$BODY" --audience owner || echo "routine-roller: push failed (non-fatal)" >&2
fi

exit 0

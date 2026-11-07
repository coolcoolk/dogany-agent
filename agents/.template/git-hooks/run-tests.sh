#!/usr/bin/env bash
# Generic framework test entry. All commands share the machine-wide test budget.
set -u
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || dirname "$SELF_DIR")"
_note() { echo "[run-tests] $*" >&2; }
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX
_QHOME="${DOGANY_SHARED_HOME:-${HOME:-}/.dogany}/cron-queue"
_ncpu="$(sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
case "$_ncpu" in ''|*[!0-9]*|0) _ncpu=1 ;; esac
HEAVY_LOCK="none"
_HEAVY_SLOT=""
_CUT_MARK=""
_heavy_release() {
  [ -n "$_HEAVY_SLOT" ] || return 0
  [ "$(cat "$_HEAVY_SLOT/pid" 2>/dev/null)" = "$$" ] && rm -rf "$_HEAVY_SLOT" 2>/dev/null
  _HEAVY_SLOT=""
}
# Slot-holder liveness (DGN-1937). Inside a dispatch worker the seatbelt
# allows signals only within the worker's own sandbox, so `kill -0` on a
# holder outside it (a sweep, another worker) fails with EPERM. Reading that
# as "dead" would reclaim a LIVE slot and break the budget. So: rc 0 (alive)
# unless the pid is malformed or the kernel says ESRCH. Unknown error = alive
# (a doubt keeps the slot; the 60s pidfile-less reclaim still applies).
_pid_alive() {  # <pid> -> rc 0 when the pid may still exist
  local _e
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  _e="$(LC_ALL=C kill -0 "$1" 2>&1)" && return 0
  case "$_e" in *"No such process"*) return 1 ;; esac
  return 0
}
# DGN-2008: contention is EEXIST, not a denied write. Exit 77 (EX_NOPERM)
# before any suite runs; 1 remains the internal "occupied" result. Preserve
# the OS diagnostic so EPERM/EROFS/EACCES cannot masquerade as queueing.
_slot_refuse() {  # <path> <diagnostic>; exits this runner, including callers
  _note "test budget: REFUSED -- sandbox cannot take a test slot: $1: $2 (rc=77; tests not run by worker) Accepted path: return this test request to the dispatcher for an authorized runner; report tests not run by worker."
  exit 77
}
_slot_mkdir() {  # mkdir arguments; rc 1 ONLY for an existing slot
  local _err
  _err="$(LC_ALL=C mkdir "$@" 2>&1)" && return 0
  case "$_err" in *": File exists") return 1 ;; esac
  _slot_refuse "${*: -1}" "$_err"
}
_slot_claim() {  # <new slot-dir>; incomplete ownership must never run tests
  local _err
  if ! _err="$( { echo "$$" > "$1/pid" &&
      printf '%s lane=%s root=%s since=%s\n' "$$" "${LANE:-gate}" "$ROOT" "$(date +%s)" > "$1/who"; } 2>&1)"; then
    rm -f "$1/pid" "$1/who" 2>/dev/null
    rmdir "$1" 2>/dev/null
    _slot_refuse "$1" "$_err"
  fi
  _HEAVY_SLOT="$1"
}
_heavy_try() {  # <slot-dir> -> rc 0 acquired (cron-guard try_queue_slot shape)
  local _o _o2 _mt
  if _slot_mkdir "$1"; then
    _slot_claim "$1"; return 0
  fi
  _o="$(cat "$1/pid" 2>/dev/null || true)"
  if [ -n "$_o" ]; then
    _pid_alive "$_o" && return 1
    _o2="$(cat "$1/pid" 2>/dev/null || true)"
    [ "$_o2" = "$_o" ] || return 1
    _note "heavy lock: reclaimed stale slot (dead pid $_o): $1"
  else
    # GNU stat first: GNU `stat -f` is filesystem mode and prints a status
    # block to stdout; BSD stat rejects -c and falls through (DGN-1920 U1).
    _mt="$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || date +%s)"
    _mt=$(( $(date +%s) - _mt ))
    [ "$_mt" -gt 60 ] || return 1
    _note "heavy lock: reclaimed abandoned slot (no pidfile, age ${_mt}s): $1"
  fi
  # Atomic rename first, so two reclaimers never both remove a fresh slot.
  mv "$1" "${1%/*}/.reclaim.$$.$RANDOM" 2>/dev/null || return 1
  rm -rf "${1%/*}/.reclaim.$$."* 2>/dev/null
  _slot_mkdir "$1" || return 1
  _slot_claim "$1"
}
_slot_live() {  # <slot-dir> -> rc 0 when a live pid holds it
  local _o
  _o="$(cat "$1/pid" 2>/dev/null)"
  [ -n "$_o" ] && _pid_alive "$_o"
}
_worker_live() {  # stdout: live worker-test slots machine-wide
  local _n=0 _s
  for _s in "$_QHOME"/worker-test/slot-*; do
    [ -d "$_s" ] && _slot_live "$_s" && _n=$((_n + 1))
  done
  echo "$_n"
}
_worker_width() {  # stdout: worker-test slots open right now
  local _w="${DGN_WORKER_TEST_SLOTS:-}"
  case "$_w" in ''|*[!0-9]*|0) _w=$(( _ncpu / 4 )); [ "$_w" -ge 1 ] || _w=1 ;; esac
  if [ "$_w" -gt 1 ] && _slot_live "$_QHOME/heavy-test/slot-1"; then _w=1; fi
  echo "$_w"
}
WORKER_GATE="none"
_worker_acquire() {  # queue on contention; refuse unusable storage with rc 77
  local _wd="$_QHOME/worker-test" _t0 _w _k _wait=0 _last=-1 _tmo _poll _sw
  _tmo="${DGN_WORKER_TEST_TIMEOUT:-3600}"
  case "$_tmo" in ''|*[!0-9]*) _tmo=3600 ;; esac
  _poll="${DGN_HEAVY_LOCK_POLL:-5}"
  case "$_poll" in ''|*[!0-9]*|0) _poll=5 ;; esac
  _slot_mkdir -p "$_wd" || _slot_refuse "$_wd" "queue home is not a directory"
  _t0="$(date +%s)"
  while :; do
    _w="$(_worker_width)"; _k=1
    while [ "$_k" -le "$_w" ]; do
      _heavy_try "$_wd/slot-$_k" && break
      _k=$((_k + 1))
    done
    [ -n "$_HEAVY_SLOT" ] && break
    _wait=$(( $(date +%s) - _t0 ))
    if [ $((_wait / 300)) -ne "$_last" ]; then
      _last=$((_wait / 300)); _sw=""
      _slot_live "$_QHOME/heavy-test/slot-1" && _sw="; a sweep holds the machine-wide test slot: $(cat "$_QHOME/heavy-test/slot-1/who" 2>/dev/null || echo '(holder unreadable)')"
      _note "test budget: QUEUED ${_wait}s -- all $_w worker test slot(s) busy$_sw (DGN-1878; waits, never fails)"
    fi
    sleep "$_poll"
  done
  _wait=$(( $(date +%s) - _t0 ))
  WORKER_GATE="held(waited=${_wait}s)"
  export DGN_WORKER_TEST_HELD="$$"
  _note "test budget: holding worker test ${_HEAVY_SLOT##*/} after ${_wait}s ($_HEAVY_SLOT)"
}


if [ "${1:-}" = --gate ]; then
  shift
  [ "$#" -gt 0 ] || { _note "--gate requires a command"; exit 2; }
elif [ -n "${DOGANY_TEST_DRIVER:-}" ]; then
  [ -x "$DOGANY_TEST_DRIVER" ] || { _note "configured test driver is not executable"; exit 78; }
  set -- "$DOGANY_TEST_DRIVER" "$@"
else
  set -- python3 -B "$SELF_DIR/framework-tests.py" "$@"
  [ "${DGN_TEST_LANE:-}" != fast ] || set -- "$@" --all
fi
LANE=gate
if [ -z "${DGN_HEAVY_LOCK_HELD:-}${DGN_WORKER_TEST_HELD:-}" ]; then
  trap '_heavy_release' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  _worker_acquire
fi

# Reclaim test roots whose runner can no longer execute its EXIT trap.  Only
# owned, real directories with this runner's prefix are candidates; uncertainty
# about owner liveness always keeps the directory.
_pid_gone() {
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  python3 -c 'import os, sys
try:
    os.kill(int(sys.argv[1]), 0)
except ProcessLookupError:
    sys.exit(0)
except Exception:
    pass
sys.exit(1)' "$1" 2>/dev/null
}
_root_owner_gone() {
  [ -f "$1/owner.pid" ] && _pid_gone "$(head -1 "$1/owner.pid" 2>/dev/null)"
}
_sweep_stale_roots() {
  local _sd _sa _now _floor _fmax _logs
  _now="$(date +%s)"
  _floor=$(( ${DGN_TEST_STALE_TMP_MIN_AGE_H:-6} * 3600 ))
  _fmax=$(( ${DGN_TEST_FAILED_TMP_MAX_AGE_H:-24} * 3600 ))
  for _sd in "${1%/}"/run-tests-disc.*; do
    [ -d "$_sd" ] && [ ! -L "$_sd" ] && [ -O "$_sd" ] || continue
    _sa="$(stat -c %Y "$_sd" 2>/dev/null || stat -f %m "$_sd" 2>/dev/null || echo "$_now")"
    _sa=$(( _now - _sa )); _logs=""
    case "$_sd" in
      *.failed-*)
        [ "$_sa" -ge "$_fmax" ] || continue
        [ -f "${_sd%.failed-*}/owner.pid" ] && ! _root_owner_gone "${_sd%.failed-*}" && continue
        ;;
      *.sweep.*) [ "$_sa" -ge "$_floor" ] || continue ;;
      *)
        if [ -f "$_sd/owner.pid" ]; then
          _root_owner_gone "$_sd" || continue
        else
          [ "$_sa" -ge "$_floor" ] || continue
        fi
        _logs="${_sd}.failed-logs"
        ;;
    esac
    mv "$_sd" "$_sd.sweep.$$" 2>/dev/null || continue
    if [ -n "$_logs" ] && [ -d "$_logs" ] && [ ! -L "$_logs" ] && [ -O "$_logs" ]; then
      mv "$_logs" "$_sd.sweep.$$/failed-logs" 2>/dev/null || true
    fi
    chflags -R nouappend,nouchg "$_sd.sweep.$$" 2>/dev/null || true
    chmod -R u+w "$_sd.sweep.$$" 2>/dev/null || true
    rm -rf "$_sd.sweep.$$" 2>/dev/null
    _note "temp sweep: removed stale run root ${_sd##*/} (age ${_sa}s)"
  done
}
_sweep_stale_roots "${TMPDIR:-/tmp}"
_GATE_TMPD="$(mktemp -d "${TMPDIR:-/tmp}/run-tests-disc.gate.XXXXXX")" || exit 77
_gate_cleanup() {
  chflags -R nouappend,nouchg "$_GATE_TMPD" 2>/dev/null || true
  chmod -R u+w "$_GATE_TMPD" 2>/dev/null || true
  rm -rf "$_GATE_TMPD"
  _heavy_release
}
trap _gate_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'dogany-test-scratch-v1\n' > "$_GATE_TMPD/.dogany-test-scratch"
printf '%s\n' "$$" > "$_GATE_TMPD/owner.pid"
export TMPDIR="$_GATE_TMPD" DOGANY_BROWSER=0 DOGANY_COMPUTER_USE=0 DOGANY_BOOK_PLAN_READ=0
export DOGANY_SECRET_BACKEND=file DOGANY_SECRET_DIR="$_GATE_TMPD/no-secrets"
unset CLAUDE_PROJECT_DIR PROJECT_ROOT GATE_SWITCH_CONF ANTHROPIC_API_KEY_FILE
mkdir -p "$_GATE_TMPD/jobs"
"$@" > >(tee "$_GATE_TMPD/jobs/1.out") 2>&1
rc=$?
if [ "$rc" -eq 1 ]; then
  mv "$_GATE_TMPD/jobs" "${_GATE_TMPD}.failed-logs" 2>/dev/null || true
fi
_note "test budget: gate=$WORKER_GATE rc=$rc"
exit "$rc"

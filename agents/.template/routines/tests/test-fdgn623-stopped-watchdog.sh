#!/usr/bin/env bash
# test-lane: fast
# test-host: macos native host dependency
# test-tier: 0
# test-component: dispatch-stopped-watchdog
# test-prereq: subprocess, tmp-files
# test-new-component: the existing dispatch lifecycle suite is Tier 2 and too
# slow for the portable merge selector; this focused process-state E2E is Tier 0
# promise: a stopped local worker and its timeout wrapper are resumed, reaped,
# finalized timed-out, and their slot/ledger artifacts close within the bound

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPATCH="$(cd "$HERE/.." && pwd)/dispatch-detached.sh"
TDIR="$(mktemp -d "${TMPDIR:-/tmp}/fdgn623.XXXXXX")"
export DOGANY_RESOURCE_SETTINGS_DIR="$TDIR/resource-settings"
cleanup() { pkill -f "$TDIR" 2>/dev/null || true; rm -rf "$TDIR"; }
trap cleanup EXIT

run_case() {
  local kind="$1" rd="$TDIR/$1" worker wrapper watchdog
  mkdir -p "$rd"
  cat > "$rd/run.env" <<EOF
TIMEOUT_MIN=2
WT_PATH=
RUNTIME=codex
RUNID=$kind
EOF
  : > "$rd/out.log"

  # A worker plus a distinct timeout-wrapper-shaped child, matching the
  # incident topology without depending on GNU timeout being installed.
  bash -c 'bash -c "exec sleep 30" & echo $! > "$1/wrapper.pid"; wait' _ "$rd" &
  worker=$!
  printf '%s\n' "$worker" > "$rd/pid"
  for _ in $(seq 1 20); do [[ -s "$rd/wrapper.pid" ]] && break; sleep .05; done
  wrapper="$(cat "$rd/wrapper.pid")"

  (
    export DISPATCH_TEST=1
    export DOGANY_DISPATCH_STOPPED_SECS=1 DOGANY_DISPATCH_WATCHDOG_POLL_SECS=1
    set --
    # shellcheck disable=SC1090
    source "$DISPATCH"
    WORKER_RUNDIR="$rd"
    restore_legacy_return_route() { :; }
    finalize_run() {
      mkdir -p "$RUNDIR/.finalized"
      printf '{"result":"%s","exit_code":%s}\n' "$1" "$2" > "$RUNDIR/status.json"
      printf '%s\n' "$1" > "$RUNDIR/ledger.result"
      printf 'released\n' > "$RUNDIR/slot.state"
      touch "$RUNDIR/.finalized/complete"
    }
    do_watchdog
  ) &
  watchdog=$!

  if [[ "$kind" == stopped ]]; then
    kill -STOP "$worker" "$wrapper"
    for _ in $(seq 1 8); do [[ -f "$rd/.finalized/complete" ]] && break; sleep 1; done
    [[ -f "$rd/.finalized/complete" ]] || return 1
    grep -q '"result":"timed-out"' "$rd/status.json" || return 1
    grep -qx timed-out "$rd/ledger.result" || return 1
    grep -qx released "$rd/slot.state" || return 1
    ! kill -0 "$worker" 2>/dev/null || return 1
    ! kill -0 "$wrapper" 2>/dev/null || return 1
  else
    kill -TERM "$worker" 2>/dev/null || true
    wait "$worker" 2>/dev/null || true
    mkdir -p "$rd/.finalized"; touch "$rd/.finalized/complete"
    wait "$watchdog"
    [[ ! -e "$rd/status.json" ]] || return 1
  fi
  wait "$watchdog" 2>/dev/null || true
}

run_case stopped
run_case normal
echo "PASS: stopped worker watchdog and normal completion"

#!/bin/bash
# routines/lib/launch-host.sh -- bridge launch-host seam, CLI (v2.7 T6 P2a,
# owner dec-319; design docs/design/v27-t6-launch-host-design.md 4.1-4.2).
#
# The ONLY place allowed to start, stop or restart an instance bridge or read
# its PID (invariant I1; enforced by tests/dgn_v27_t6_p2a_launch_host_lint.py).
# Callers exec it; nothing is sourced.
#
# Usage:
#   launch-host.sh host    <root>
#   launch-host.sh status  <root> [--label L]
#   launch-host.sh doctor  <root> [--label L] [--home H]
#   launch-host.sh start   <root> [--label L] [--reason R]
#   launch-host.sh stop    <root> [--label L] [--reason R]
#   launch-host.sh restart <root> [--label L] [--reason R]
#   launch-host.sh adopt   <root> --to app            (P5; not in this build)
#   launch-host.sh release <root> --to launchd [...]  (P5; not in this build)
# One JSON object on stdout.
#
# Exit codes: 0 ok | 1 operation failed | 2 usage | 4 refused (no label /
# no plist) | 69 not available in this build (app backend, adopt, release).
#
# Read verbs (host/status/doctor) and label resolution are delegated to the
# Python twin routines/lib/launch_host.py so there is one implementation of
# the lease, the resolver and the doctor. Actions run here.
#
# launchd backend = today's exact calls:
#   restart -> launchctl kickstart -k gui/<uid>/<label>   (self_restart.sh,
#              watchdog.sh), after the P1 bridge.restart_wait marker so the
#              revived bridge waits for bridge.lock instead of exiting 75.
#   stop    -> launchctl bootout gui/<uid>/<label> + the DGN-1640 bounded
#              poll (registration+PID read, kill -0, read again; final read
#              wins). Stop never disables the label (that is adopt, P5).
#   start   -> loaded: launchctl kickstart gui/<uid>/<label> (no -k: no-op on
#              a running job); not loaded: launchctl enable + launchctl
#              bootstrap gui/<uid> <plist> (watchdog.sh DGN-888 sequence).
#   A label with no loaded job and no plist on disk is refused (4), never
#   guessed.
#
# Test knobs: DOGANY_LAUNCHCTL (binary), DOGANY_LAUNCHD_CAPTURE (append the
# launchctl argv with gui/UID to this file instead of running it -- same
# capture format as scripts/pack/update_apply.sh), DOGANY_LAUNCH_HOST_STOP_
# POLL_TRIES / _INTERVAL (DGN-1640 bound; default 300 x 0.2s = 60s).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TWIN="$SCRIPT_DIR/launch_host.py"
LAUNCHCTL="${DOGANY_LAUNCHCTL:-launchctl}"
CAPTURE="${DOGANY_LAUNCHD_CAPTURE:-}"
STOP_POLL_TRIES="${DOGANY_LAUNCH_HOST_STOP_POLL_TRIES:-300}"
STOP_POLL_INTERVAL="${DOGANY_LAUNCH_HOST_STOP_POLL_INTERVAL:-0.2}"

EX_FAIL=1
EX_USAGE=2
EX_REFUSED=4
EX_UNAVAILABLE=69

usage() {
  sed -n '9,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit "$EX_USAGE"
}

_py() {
  if [ -z "${LH_PY:-}" ]; then
    LH_PY=""
    if [ -f "$SCRIPT_DIR/py_resolve.sh" ]; then
      # shellcheck source=/dev/null
      . "$SCRIPT_DIR/py_resolve.sh"
      pack_resolve_py3 2>/dev/null && LH_PY="$PACK_PY3"
    fi
    [ -n "$LH_PY" ] || LH_PY="$(command -v python3 2>/dev/null || true)"
  fi
  [ -n "$LH_PY" ] || return 127
  "$LH_PY" "$TWIN" "$@"
}

_json_str() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

# _emit <verb> <ok:true|false> <host> <label> <detail>
_emit() {
  printf '{"verb": %s, "ok": %s, "host": %s, "label": %s, "detail": %s}\n' \
    "$(_json_str "$1")" "$2" "$(_json_str "$3")" "$(_json_str "$4")" "$(_json_str "$5")"
}

_log() {
  local d="$ROOT/.telegram_bot/logs"
  mkdir -p "$d" 2>/dev/null || return 0
  printf '%s %s host=%s label=%s reason=%s rc=%s %s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$VERB" "$HOST" "$LABEL" "${REASON:-}" "$1" "${2:-}" \
    >> "$d/launch-host.log" 2>/dev/null || true
}

_lc() {
  if [ -n "$CAPTURE" ]; then
    local a out=()
    for a in "$@"; do out+=("${a/gui\/$(id -u)/gui/UID}"); done
    printf 'launchctl %s\n' "${out[*]}" >> "$CAPTURE"
    return 0
  fi
  "$LAUNCHCTL" "$@"
}

_loaded() {
  [ -n "$CAPTURE" ] && return 1
  "$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null 2>&1
}

_list_pid() {
  "$LAUNCHCTL" list 2>/dev/null | awk -v l="$LABEL" '$3==l {print $1; exit}'
}

# DGN-1640 (scripts/pack/update_apply.sh _label_live): rc 0 only when the
# label is still registered with a live PID on BOTH reads.
_label_live() {
  local pid final_pid
  pid="$(_list_pid)"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  final_pid="$(_list_pid)"
  [[ "$final_pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$final_pid" 2>/dev/null || return 1
  return 0
}

_label_gone() {
  local i=0
  while :; do
    _label_live || return 0
    i=$((i + 1))
    [ "$i" -ge "$STOP_POLL_TRIES" ] && return 1
    sleep "$STOP_POLL_INTERVAL"
  done
}

# --- args --------------------------------------------------------------------

[ $# -ge 2 ] || usage
VERB="$1"; ROOT_ARG="$2"; shift 2
LABEL_ARG=""; REASON=""; TO=""; HOME_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --label)  [ $# -ge 2 ] || usage; LABEL_ARG="$2"; shift 2 ;;
    --reason) [ $# -ge 2 ] || usage; REASON="$2"; shift 2 ;;
    --to)     [ $# -ge 2 ] || usage; TO="$2"; shift 2 ;;
    --home)   [ $# -ge 2 ] || usage; HOME_ARG="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[ -d "$ROOT_ARG" ] || { _emit "$VERB" false "" "" "instance root not a directory: $ROOT_ARG"; exit "$EX_USAGE"; }
ROOT="$(cd "$ROOT_ARG" && pwd)"
HOST=""; LABEL=""

LABEL_OPT=()
[ -n "$LABEL_ARG" ] && LABEL_OPT=(--label "$LABEL_ARG")

case "$VERB" in
  host)   _py host "$ROOT"; exit $? ;;
  status) _py status "$ROOT" ${LABEL_OPT[@]+"${LABEL_OPT[@]}"}; exit $? ;;
  doctor)
    HOME_OPT=()
    [ -n "$HOME_ARG" ] && HOME_OPT=(--home "$HOME_ARG")
    _py doctor "$ROOT" ${LABEL_OPT[@]+"${LABEL_OPT[@]}"} ${HOME_OPT[@]+"${HOME_OPT[@]}"}; exit $? ;;
  adopt|release)
    _emit "$VERB" false "" "" "adopt/release (to=${TO:-?}) is the P5 handover state machine; not in this build"
    exit "$EX_UNAVAILABLE" ;;
  start|stop|restart) ;;
  *) usage ;;
esac

# --- actions -----------------------------------------------------------------

host_json="$(_py host "$ROOT")" || { _emit "$VERB" false "" "" "host resolution failed (python twin unavailable)"; exit "$EX_FAIL"; }
case "$host_json" in
  *'"host": "launchd"'*) HOST="launchd" ;;
  *'"host": "app"'*)     HOST="app" ;;
  *) _emit "$VERB" false "" "" "host resolution returned no host"; exit "$EX_FAIL" ;;
esac

label_json="$(_py label "$ROOT" ${LABEL_OPT[@]+"${LABEL_OPT[@]}"})"
LABEL="$(printf '%s' "$label_json" | sed -n 's/.*"label": "\([^"]*\)".*/\1/p')"
PLIST="$(printf '%s' "$label_json" | sed -n 's/.*"plist": "\([^"]*\)".*/\1/p')"
if [ -z "$LABEL" ]; then
  _emit "$VERB" false "$HOST" "" "bridge label unresolved (no service marker, no DOGANY_AGENT_NAME, no --label)"
  _log "$EX_REFUSED" "label unresolved"
  exit "$EX_REFUSED"
fi

if [ "$HOST" = "app" ]; then
  # Never fall back to launchd under a live app lease (I1/I6): that would
  # start a second launcher. The app backend lands in P5.
  _emit "$VERB" false "$HOST" "$LABEL" "app backend not available in this build (P5)"
  _log "$EX_UNAVAILABLE" "app backend missing"
  exit "$EX_UNAVAILABLE"
fi

uid="$(id -u)"
_bootstrap() {
  if [ -z "$PLIST" ] || [ ! -f "$PLIST" ]; then
    _emit "$VERB" false "$HOST" "$LABEL" "label not loaded and no bridge plist on disk; refusing to guess"
    _log "$EX_REFUSED" "no plist"
    exit "$EX_REFUSED"
  fi
  _lc enable "gui/$uid/$LABEL" >/dev/null 2>&1 || true
  if _lc bootstrap "gui/$uid" "$PLIST" >/dev/null 2>&1; then
    _emit "$VERB" true "$HOST" "$LABEL" "bootstrapped $PLIST"
    _log 0 "bootstrap"
    exit 0
  fi
  _emit "$VERB" false "$HOST" "$LABEL" "launchctl bootstrap failed for $PLIST"
  _log "$EX_FAIL" "bootstrap failed"
  exit "$EX_FAIL"
}

case "$VERB" in
  start)
    if _loaded; then
      if _lc kickstart "gui/$uid/$LABEL" >/dev/null 2>&1; then
        _emit start true "$HOST" "$LABEL" "kickstart (already loaded)"; _log 0 "kickstart"; exit 0
      fi
      _emit start false "$HOST" "$LABEL" "launchctl kickstart failed"; _log "$EX_FAIL" "kickstart failed"; exit "$EX_FAIL"
    fi
    _bootstrap ;;
  restart)
    if _loaded || [ -n "$CAPTURE" ]; then
      # P1 contract: restart-path marker so the new bridge waits (<=10 s) for
      # the old process to release bridge.lock.
      mkdir -p "$ROOT/.telegram_bot/state" 2>/dev/null \
        && date '+%s' > "$ROOT/.telegram_bot/state/bridge.restart_wait" 2>/dev/null || true
      if _lc kickstart -k "gui/$uid/$LABEL" >/dev/null 2>&1; then
        _emit restart true "$HOST" "$LABEL" "kickstart -k"; _log 0 "kickstart -k"; exit 0
      fi
      _emit restart false "$HOST" "$LABEL" "launchctl kickstart -k failed"; _log "$EX_FAIL" "kickstart -k failed"; exit "$EX_FAIL"
    fi
    _bootstrap ;;
  stop)
    booted="bootout rc!=0 (not loaded?)"
    _lc bootout "gui/$uid/$LABEL" >/dev/null 2>&1 && booted="bootout accepted"
    if [ -n "$CAPTURE" ] || _label_gone; then
      _emit stop true "$HOST" "$LABEL" "$booted; verified gone"; _log 0 "$booted"; exit 0
    fi
    _emit stop false "$HOST" "$LABEL" "$booted; still live after ${STOP_POLL_TRIES}x${STOP_POLL_INTERVAL}s (DGN-1640 bound)"
    _log "$EX_FAIL" "stop verify failed"
    exit "$EX_FAIL" ;;
esac

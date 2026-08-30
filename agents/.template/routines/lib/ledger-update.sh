#!/usr/bin/env bash
# routines/lib/ledger-update.sh
#
# PURPOSE: Single write path for the current-state ledger (product/auto-loop-ledger.md).
#
# DOCTRINE: two-actor one-write-path
#   Two actors (autonomous loop + the agent main session) share this one script as the
#   exclusive write path. All ledger mutations go through here -- no actor writes
#   the ledger file directly. This is the concrete realisation of the one-writer
#   invariant for the ledger surface (LOCKED spec v2, What -- E, DGN-532).
#
# USAGE:
#   ledger-update.sh <branch> <state> [note] [--attempts N] [--flag X] [--backoff TS] [--item TEXT] [--ts TIMESTAMP] [--ledger PATH]
#
#   branch          : auto/<slug> or any branch name identifying the work item
#   state           : one of: pending / running / pending-review / escalated /
#                     resolved-requeued / merged / rejected / nonconforming /
#                     failed / quarantined / deferred
#   note            : optional free-text note (no pipes -- they break table format)
#   --attempts N    : set the attempts counter explicitly
#   --flag X        : add a flag (may be repeated; flags are comma-joined)
#   --backoff TS    : backoff_until timestamp string (e.g. "2026-07-23 05:00 KST")
#   --item TEXT     : queue item label (used when inserting a new row)
#   --ts TIMESTAMP  : last_ts value (caller provides; avoids forking date inside)
#   --ledger PATH   : override ledger path (used by tests; default: auto-resolved)
#
# ROW FORMAT (pipe-table, one row per branch):
#   | branch | item | state | attempts | backoff_until | last_ts | flags | note |
#
# LOCK [N1]:
#   Exclusive lock on <ledger>.lock using lockf(1) (macOS) or flock(1) (Linux).
#   Finite wait: 15 seconds. On timeout: short sleep + retry (never silent-drop).
#   Failure exits non-zero to stderr -- a dropped write corrupts the state machine.
#
# WRITE ATOMICITY:
#   Critical section = single row read-modify-write. Written via tmp file + mv
#   atomic replace (POSIX rename on same filesystem is atomic).

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DEFAULT_LEDGER="${REPO_ROOT}/product/auto-loop-ledger.md"
MAX_LOCK_ATTEMPTS=3
RETRY_SLEEP=2

VALID_STATES=(
  pending running pending-review escalated resolved-requeued
  merged rejected nonconforming failed quarantined deferred skipped
)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
if [[ $# -lt 2 ]]; then
  echo "USAGE: ledger-update.sh <branch> <state> [note] [--attempts N] [--flag X] [--backoff TS] [--item TEXT] [--ts TIMESTAMP] [--ledger PATH]" >&2
  exit 1
fi

BRANCH="$1"
STATE="$2"
shift 2

NOTE=""
ATTEMPTS_ARG=""
FLAGS=()
BACKOFF_ARG=""
ITEM_ARG=""
TS_ARG=""
LEDGER="${DEFAULT_LEDGER}"

# Consume positional note if next arg is not a flag
if [[ $# -gt 0 && "${1:-}" != --* ]]; then
  NOTE="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --attempts)
      ATTEMPTS_ARG="$2"; shift 2 ;;
    --flag)
      FLAGS+=("$2"); shift 2 ;;
    --backoff)
      BACKOFF_ARG="$2"; shift 2 ;;
    --item)
      ITEM_ARG="$2"; shift 2 ;;
    --ts)
      TS_ARG="$2"; shift 2 ;;
    --ledger)
      LEDGER="$2"; shift 2 ;;
    *)
      echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

LOCK_FILE="${LEDGER}.lock"

# Validate state
VALID=0
for s in "${VALID_STATES[@]}"; do
  [[ "$s" == "$STATE" ]] && VALID=1 && break
done
if [[ $VALID -eq 0 ]]; then
  echo "Invalid state '${STATE}'. Valid: ${VALID_STATES[*]}" >&2
  exit 1
fi

# Ledger must exist
if [[ ! -f "${LEDGER}" ]]; then
  echo "Ledger not found: ${LEDGER}" >&2
  exit 1
fi

# Sanitize: no pipes in note (would break table)
NOTE="${NOTE//|/;}"

# ---------------------------------------------------------------------------
# Core update logic (runs inside the lock)
# ---------------------------------------------------------------------------
do_update() {
  local tmp
  tmp="$(mktemp "${LEDGER}.tmp.XXXXXX")"

  # Check if row for this branch already exists
  local found=0
  local existing_attempts=""
  local existing_flags=""
  local existing_backoff=""
  local existing_item=""
  local existing_note=""

  if grep -qF "| ${BRANCH} |" "${LEDGER}" 2>/dev/null; then
    found=1
    local existing_row
    existing_row="$(grep -F "| ${BRANCH} |" "${LEDGER}" | head -1)"
    # Row: | branch | item | state | attempts | backoff_until | last_ts | flags | note |
    #        col1     col2   col3    col4       col5            col6      col7    col8
    existing_item="$(echo "${existing_row}"     | cut -d'|' -f3 | xargs)"
    existing_attempts="$(echo "${existing_row}" | cut -d'|' -f5 | xargs)"
    existing_backoff="$(echo "${existing_row}"  | cut -d'|' -f6 | xargs)"
    existing_flags="$(echo "${existing_row}"    | cut -d'|' -f8 | xargs)"
    existing_note="$(echo "${existing_row}"     | cut -d'|' -f9 | xargs)"
  fi

  # Resolve field values
  local item="${ITEM_ARG:-}"
  [[ -z "$item" ]] && item="${existing_item:-}"
  [[ -z "$item" ]] && item="-"

  local attempts="${ATTEMPTS_ARG:-}"
  [[ -z "$attempts" ]] && attempts="${existing_attempts:-}"
  [[ -z "$attempts" || "$attempts" == "-" ]] && attempts=0

  local backoff="${BACKOFF_ARG:-}"
  [[ -z "$backoff" ]] && backoff="${existing_backoff:-}"
  [[ -z "$backoff" ]] && backoff="-"

  local ts="${TS_ARG:-}"
  [[ -z "$ts" ]] && ts="$(date '+%Y-%m-%d %H:%M %Z')"

  # Merge flags: preserve existing, append new (deduplicated)
  local merged_flags="${existing_flags:-}"
  [[ "$merged_flags" == "-" ]] && merged_flags=""
  for f in "${FLAGS[@]}"; do
    if [[ -z "$merged_flags" ]]; then
      merged_flags="$f"
    elif ! echo "$merged_flags" | grep -qF "$f"; then
      merged_flags="${merged_flags},${f}"
    fi
  done
  [[ -z "$merged_flags" ]] && merged_flags="-"

  # Note: prefer explicit arg, fall back to existing
  local final_note="${NOTE:-}"
  [[ -z "$final_note" ]] && final_note="${existing_note:-}"
  [[ -z "$final_note" ]] && final_note="-"

  local new_row="| ${BRANCH} | ${item} | ${STATE} | ${attempts} | ${backoff} | ${ts} | ${merged_flags} | ${final_note} |"

  if [[ $found -eq 1 ]]; then
    # Replace the matching row in-place using awk
    awk -v branch="${BRANCH}" -v new_row="${new_row}" '
      BEGIN { replaced=0 }
      !replaced && index($0, "| " branch " |") > 0 {
        print new_row
        replaced=1
        next
      }
      { print }
    ' "${LEDGER}" > "${tmp}"
  else
    # Append new row after the last pipe-table row
    awk -v new_row="${new_row}" '
      /^\|/ { last_table=NR }
      { lines[NR]=$0 }
      END {
        for (i=1; i<=NR; i++) {
          print lines[i]
          if (i == last_table) {
            print new_row
          }
        }
      }
    ' "${LEDGER}" > "${tmp}"
  fi

  # Atomic replace
  mv "${tmp}" "${LEDGER}"
}

# ---------------------------------------------------------------------------
# Locking helper: cross-platform (macOS lockf / Linux flock)
# ---------------------------------------------------------------------------
acquire_lock_and_update() {
  # macOS: lockf -t <seconds> -k <file> <cmd> [args...]
  #   -k  keep lock file after release (we manage it ourselves)
  #   -t  timeout in seconds (exit 1 on timeout, no signal)
  # Linux: flock -w <seconds> <fd>  (FD-based, needs exec trick)
  # We wrap the critical section in a subprocess via lockf or flock.

  if command -v flock >/dev/null 2>&1; then
    # Linux path: use FD-based flock with a subshell
    (
      exec 9>"${LOCK_FILE}"
      if ! flock -w 15 9; then
        return 1
      fi
      do_update
    )
  elif command -v lockf >/dev/null 2>&1; then
    # macOS path: lockf executes a command while holding the lock.
    # We export the do_update function and invoke it in a bash subprocess.
    export -f do_update
    export LEDGER NOTE BRANCH STATE ITEM_ARG ATTEMPTS_ARG BACKOFF_ARG TS_ARG
    export_flags="$(printf '%s\n' "${FLAGS[@]+"${FLAGS[@]}"}")"
    export EXPORTED_FLAGS="${export_flags}"

    lockf -t 15 -k "${LOCK_FILE}" bash -c '
      # Reconstruct FLAGS array from exported env
      IFS=$'"'"'\n'"'"' read -r -d '"'"''"'"' -a FLAGS <<< "${EXPORTED_FLAGS}" || true
      do_update
    '
  else
    echo "ledger-update: no flock or lockf found -- cannot acquire lock" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Main: lock with retry on timeout (never silent-drop [N1])
# ---------------------------------------------------------------------------
attempt=0
while true; do
  attempt=$(( attempt + 1 ))

  if acquire_lock_and_update; then
    exit 0
  fi

  # Lock timed out or failed -- do NOT skip; retry after sleep
  if [[ $attempt -ge $MAX_LOCK_ATTEMPTS ]]; then
    echo "ledger-update: lock acquisition failed after ${MAX_LOCK_ATTEMPTS} attempts (lock=${LOCK_FILE})" >&2
    echo "  branch=${BRANCH} state=${STATE}" >&2
    exit 1
  fi

  echo "ledger-update: lock timeout (attempt ${attempt}/${MAX_LOCK_ATTEMPTS}), retrying in ${RETRY_SLEEP}s" >&2
  sleep "${RETRY_SLEEP}"
done

#!/bin/bash
# run-notify.sh -- routine failure notification wrapper.
#
# Usage: run-notify.sh <label> -- <cmd> [args...]
#
# Runs <cmd>. If exit code != 0, sends ONE warning push via push.sh --text
# (per-label-per-day dedup: at most one notification per label per calendar day).
# On success (exit 0): no push, exits 0.
# On failure: push warning if not already sent today, then exits with the
# original non-zero exit code (so launchd/systemd still records the failure).
#
# Dedup marker: <runtime_dir>/run-notify/<label>.last-warn (mtime date used).
# runtime_dir = PROJECT_ROOT/.telegram_bot  (matches config.py BOT_DATA_DIR).
#
# K.I.S.S. design: no retries, no queuing, no state DB. One file per label.
# push.sh path: PROJECT_ROOT/routines/push.sh (resolved from this script's loc).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PUSH="$PROJECT_ROOT/routines/push.sh"

# Parse args: run-notify.sh <label> -- <cmd...>
LABEL="${1:-}"
if [[ -z "$LABEL" ]]; then
  echo "[run-notify] usage: run-notify.sh <label> -- <cmd> [args...]" >&2
  exit 1
fi
shift

# Expect "--" separator
if [[ "${1:-}" != "--" ]]; then
  echo "[run-notify] expected '--' after label, got: ${1:-}" >&2
  exit 1
fi
shift

if [[ $# -eq 0 ]]; then
  echo "[run-notify] no command after --" >&2
  exit 1
fi

# Run the wrapped command. Capture exit code without letting set -e fire.
RC=0
"$@" || RC=$?

if [[ "$RC" -eq 0 ]]; then
  exit 0
fi

# Command failed. Check dedup: has a warning already been sent today for this label?
RUNTIME_DIR="$PROJECT_ROOT/.telegram_bot/run-notify"
mkdir -p "$RUNTIME_DIR" 2>/dev/null || true
MARKER="$RUNTIME_DIR/${LABEL}.last-warn"
TODAY="$(date +%F)"

already_sent=0
if [[ -f "$MARKER" ]]; then
  # Compare marker's date (stored as a date string inside the file).
  marker_date="$(cat "$MARKER" 2>/dev/null || true)"
  if [[ "$marker_date" == "$TODAY" ]]; then
    already_sent=1
  fi
fi

if [[ "$already_sent" -eq 0 ]]; then
  MSG="[warn] routine ${LABEL} failed (exit ${RC}), see logs"
  if [[ -x "$PUSH" ]]; then
    "$PUSH" --text "$MSG" 2>/dev/null || true
  else
    echo "[run-notify] push.sh not found or not executable: $PUSH" >&2
  fi
  # Write today's date as the dedup marker.
  printf '%s\n' "$TODAY" > "$MARKER" 2>/dev/null || true
fi

exit "$RC"

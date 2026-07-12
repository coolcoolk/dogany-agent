#!/bin/bash
# remind.sh -- timed-event alert poller (appointments + tasks), 5-min cadence.
#
# Selection lives in database/remind_select.py (DGN-273 notify policy):
#   NULL/'default' -> lead alert (task 30 min, appt 120 min) + on-time alert
#   'silent'       -> nothing
#   'start_only'   -> on-time alert only
#   'custom'       -> notify_lead_min lead alert + on-time alert
# This script is a thin pipe: read due alerts, dedup via daily sent markers
# (one send per key; keys include the start instant so a moved event
# re-alerts), localize the header, push.
#
# Usage: remind.sh [--dry]   (--dry = print alerts, no send, no marker write)
# Exit:  0 ok / 1 config error
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DRY=0
[[ "${1:-}" == "--dry" ]] && DRY=1

SELECT_PY="$AGENT_DIR/database/remind_select.py"
PUSH="$SCRIPT_DIR/push.sh"
[[ -f "$SELECT_PY" ]] || { echo "[remind] remind_select.py not found: $SELECT_PY" >&2; exit 1; }
[[ -f "$PUSH" ]] || { echo "[remind] push.sh not found: $PUSH" >&2; exit 1; }

SENT_DIR="$AGENT_DIR/.remind_sent"
mkdir -p "$SENT_DIR"
TODAY_SENT="$SENT_DIR/$(date +%Y-%m-%d).sent"
touch "$TODAY_SENT"

# ---- locale (same pattern as morning-brief.sh) ----
AGENT_LANG="$(grep -E '^AGENT_LANG=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
AGENT_LANG="${AGENT_LANG:-en}"
I18N="$AGENT_DIR/config/i18n/${AGENT_LANG}.json"
i18n_get() { # i18n_get <key> -> value ('' if missing)
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2],""))' "$I18N" "$1" 2>/dev/null || true
}

hdr() { # hdr <kind> <alert> <lead_min> -> localized header line
  local key="remind.${1}_${2}" tpl
  tpl="$(i18n_get "$key")"
  if [[ -z "$tpl" ]]; then
    case "${1}_${2}" in
      appointment_lead)  tpl="Appointment in {min} min" ;;
      appointment_start) tpl="Appointment starting" ;;
      task_lead)         tpl="Task in {min} min" ;;
      task_start)        tpl="Task starting" ;;
      *)                 tpl="Reminder" ;;
    esac
  fi
  printf '%s\n' "${tpl//\{min\}/$3}"
}

ROWS="$(python3 "$SELECT_PY" 2>/dev/null || true)"
[[ -z "$ROWS" ]] && exit 0

while IFS=$'\t' read -r key kind alert lead hhmm title location purpose; do
  [[ -z "$key" ]] && continue
  grep -qxF "$key" "$TODAY_SENT" && continue
  HEADER="$(hdr "$kind" "$alert" "$lead")"
  BODY="$title"
  [[ -n "$hhmm" ]] && BODY+=" · $hhmm"
  [[ -n "$location" ]] && BODY+=" · $location"
  [[ -n "$purpose" ]] && BODY+=" · $purpose"
  MSG="$HEADER
$BODY"
  if [[ "$DRY" == "1" ]]; then
    echo "[DRY] $MSG"
    continue
  fi
  if "$PUSH" --text "$MSG"; then
    echo "$key" >> "$TODAY_SENT"
  else
    echo "[remind] push failed ($key)" >&2
  fi
done <<< "$ROWS"

exit 0

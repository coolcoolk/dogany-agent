#!/bin/bash
# morning-brief.sh -- lifekit bundle routine: daily morning briefing.
# Data source: local lifekit.db only (via database/lifekit.sh). No external APIs.
#   (1) today's timed schedule -- appointments AND task blocks (event-window)
#   (2) yesterday's diet/workout one-line recap (agg-day)
# event-window takes UTC ISO timestamps; day boundaries derived from AGENT_TZ_OFFSET_HOURS.
# Output cols: ulid, kind, title, start_at(UTC), end_at(UTC), status -- converted to local tz for display.
#
# Usage: morning-brief.sh [--dry]   (--dry = print data + prompt, no send)
# Exit:  0 ok / 1 config error
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIFE_SH="$AGENT_DIR/database/lifekit.sh"
PUSH_SH="$AGENT_DIR/routines/push.sh"
DRY=0
[[ "${1:-}" == "--dry" ]] && DRY=1

[[ -x "$LIFE_SH" || -f "$LIFE_SH" ]] || { echo "[brief] lifekit.sh not found: $LIFE_SH" >&2; exit 1; }
[[ -f "$PUSH_SH" ]] || { echo "[brief] push.sh not found: $PUSH_SH" >&2; exit 1; }

# ---- portable date math (BSD/macOS -v, GNU/Linux -d) ----
day_shift() { # day_shift <+1|-1> -> YYYY-MM-DD
  if date -v+1d +%F >/dev/null 2>&1; then date -v"${1}d" +%F; else date -d "${1} day" +%F; fi
}
TODAY="$(date +%F)"
YDAY="$(day_shift -1)"
NOW_HM="$(date +%H:%M)"   # actual send time -- keep briefing wording in sync

# ---- locale (address + tone come from the instance i18n) ----
AGENT_LANG="$(grep -E '^AGENT_LANG=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
AGENT_LANG="${AGENT_LANG:-en}"
I18N="$AGENT_DIR/config/i18n/${AGENT_LANG}.json"
i18n_get() { # i18n_get <key> -> value ('' if missing)
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2],""))' "$I18N" "$1" 2>/dev/null || true
}
ADDRESS="$(grep -E '^AGENT_ADDRESS=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
[[ -z "$ADDRESS" ]] && ADDRESS="$(i18n_get address)"
TONE="$(i18n_get tone_guide)"

# ---- timezone offset (integer hours, e.g. 9 for UTC+9) ----
TZ_OFFSET_H="$(grep -E '^AGENT_TZ_OFFSET_HOURS=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
TZ_OFFSET_H="${TZ_OFFSET_H:-0}"

# ---- (1) today's timed schedule (appointments + task blocks via event-window) ----
# Local day boundary in UTC: local midnight = YDAY T(24-offset):00:00Z (for offset>=0, adjust for negative).
# Computed inline via python3 to avoid bash integer-overflow on edge offsets.
read -r FROM_UTC TO_UTC < <(python3 -c "
from datetime import datetime, timezone, timedelta
off = int('$TZ_OFFSET_H')
tz = timezone(timedelta(hours=off))
today = datetime.strptime('$TODAY', '%Y-%m-%d').replace(tzinfo=tz)
yday = datetime.strptime('$YDAY', '%Y-%m-%d').replace(tzinfo=tz)
from_utc = yday.replace(hour=0, minute=0, second=0).astimezone(timezone.utc)
to_utc   = today.replace(hour=0, minute=0, second=0).astimezone(timezone.utc)
print(from_utc.strftime('%Y-%m-%dT%H:%M:%SZ') + ' ' + to_utc.strftime('%Y-%m-%dT%H:%M:%SZ'))
")
SCHED_TXT="$("$LIFE_SH" event-window "$FROM_UTC" "$TO_UTC" 2>/dev/null | python3 -c "
import sys
from datetime import datetime, timezone, timedelta
LOCAL_TZ = timezone(timedelta(hours=int('$TZ_OFFSET_H')))
for line in sys.stdin:
    cols = line.rstrip('\n').split('\t')
    if len(cols) < 5:
        continue
    title = cols[2]; sa = cols[3]; ea = cols[4]
    try:
        start = datetime.strptime(sa, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
        s_str = start.strftime('%H:%M')
    except Exception:
        s_str = ''
    try:
        end = datetime.strptime(ea, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
        e_str = end.strftime('%H:%M')
    except Exception:
        e_str = ''
    time_range = (s_str + '-' + e_str + ' ') if s_str else ''
    print('- ' + time_range + title)
" || true)"
SCHED_CNT=0; [[ -n "$SCHED_TXT" ]] && SCHED_CNT="$(printf '%s\n' "$SCHED_TXT" | wc -l | tr -d ' ')"

# ---- (2) yesterday recap (diet/workout, from agg-day KEY=VALUE) ----
AGG="$("$LIFE_SH" agg-day "$YDAY" 2>/dev/null || true)"
ag() { printf '%s\n' "$AGG" | grep "^$1=" | head -1 | cut -d= -f2-; }
Y_KCAL="$(ag intake_kcal)"; Y_KCAL="${Y_KCAL:-0}"
Y_MEALS="$(ag meal_cnt)";   Y_MEALS="${Y_MEALS:-0}"
Y_WO="$(ag workout_cnt)";   Y_WO="${Y_WO:-0}"
Y_BURN="$(ag burn_kcal)";   Y_BURN="${Y_BURN:-0}"

DATA="[today ${TODAY}]"$'\n\n'
DATA+="# today's schedule: ${SCHED_CNT}"$'\n'
if [[ "$SCHED_CNT" -gt 0 ]]; then DATA+="${SCHED_TXT}"$'\n'; else DATA+="(none)"$'\n'; fi
DATA+=$'\n'"# yesterday recap"$'\n'
DATA+="meals logged: ${Y_MEALS} (intake ${Y_KCAL} kcal), workouts: ${Y_WO} (burned ${Y_BURN} kcal)"$'\n'

EMPTY_NOTE=""
if [[ "$SCHED_CNT" -eq 0 ]]; then
  EMPTY_NOTE="NOTE: no scheduled items today. Do not invent items; briefly note it is an open day and close with light encouragement."
fi

PROMPT="You are the user's personal assistant sending the morning briefing over Telegram. The current time is ${NOW_HM}.
Write the briefing in the user's language (locale: ${AGENT_LANG}). Address the user as: ${ADDRESS:-"(no fixed form of address; write naturally)"}.
Tone rules: ${TONE}

Structure:
- one-line good-morning opener (if you mention a clock time, use ${NOW_HM} exactly; never state a different hour)
- today's schedule as short bullet items (keep HH:MM-HH:MM prefixes from the data as-is); omit the section if none
- one line recapping yesterday (meals/workouts) ONLY if something was logged
- one-line closing (light encouragement or nudge)

${EMPTY_NOTE}
Use only the data below; never invent schedules or numbers. Output the message body only.

=== DATA ===
${DATA}"

if [[ "$DRY" -eq 1 ]]; then
  echo "---- DATA ----"; echo "$DATA"
  echo "---- PROMPT chars ----"; echo "${#PROMPT}"
  exit 0
fi

exec "$PUSH_SH" --model haiku --prompt "$PROMPT"

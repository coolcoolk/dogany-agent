#!/bin/bash
# morning-brief.sh -- lifekit bundle routine: daily morning briefing.
# Data source: local lifekit.db only (via database/lifekit.sh). No external APIs.
#   (1) today's appointments (appt-find)
#   (2) yesterday's diet/workout one-line recap (agg-day)
# Tasks are intentionally absent: lifekit has no task CLI yet (see task-update
# skill -- inert until implemented). Add a tasks section when it lands.
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

# ---- (1) today's appointments ----
APPT_TXT="$("$LIFE_SH" appt-find "$TODAY" 2>/dev/null | awk -F'\t' '{
  t=$2; time=""; if (t ~ /T/) { time=substr(t, index(t,"T")+1, 5)" " }
  loc=($4=="")?"":"  @"$4; print "- " time $3 loc }' || true)"
APPT_CNT=0; [[ -n "$APPT_TXT" ]] && APPT_CNT="$(printf '%s\n' "$APPT_TXT" | wc -l | tr -d ' ')"

# ---- (2) yesterday recap (diet/workout, from agg-day KEY=VALUE) ----
AGG="$("$LIFE_SH" agg-day "$YDAY" 2>/dev/null || true)"
ag() { printf '%s\n' "$AGG" | grep "^$1=" | head -1 | cut -d= -f2-; }
Y_KCAL="$(ag intake_kcal)"; Y_KCAL="${Y_KCAL:-0}"
Y_MEALS="$(ag meal_cnt)";   Y_MEALS="${Y_MEALS:-0}"
Y_WO="$(ag workout_cnt)";   Y_WO="${Y_WO:-0}"
Y_BURN="$(ag burn_kcal)";   Y_BURN="${Y_BURN:-0}"

DATA="[today ${TODAY}]"$'\n\n'
DATA+="# today's appointments: ${APPT_CNT}"$'\n'
if [[ "$APPT_CNT" -gt 0 ]]; then DATA+="${APPT_TXT}"$'\n'; else DATA+="(none)"$'\n'; fi
DATA+=$'\n'"# yesterday recap"$'\n'
DATA+="meals logged: ${Y_MEALS} (intake ${Y_KCAL} kcal), workouts: ${Y_WO} (burned ${Y_BURN} kcal)"$'\n'

EMPTY_NOTE=""
if [[ "$APPT_CNT" -eq 0 ]]; then
  EMPTY_NOTE="NOTE: no appointments today. Do not invent items; briefly note it is an open day and close with light encouragement."
fi

PROMPT="You are the user's personal assistant sending a 6 AM morning briefing over Telegram.
Write the briefing in the user's language (locale: ${AGENT_LANG}). Address the user as: ${ADDRESS:-"(no fixed form of address; write naturally)"}.
Tone rules: ${TONE}

Structure:
- one-line good-morning opener
- today's appointments as short bullet items (keep HH:MM prefixes from the data as-is); omit the section if none
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

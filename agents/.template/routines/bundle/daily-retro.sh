#!/bin/bash
# daily-retro.sh -- lifekit bundle routine: end-of-day retrospective.
# Data source: local lifekit.db only (via database/lifekit.sh). No external APIs.
#   (1) today's diet aggregate + calorie balance (agg-day / targets)
#   (2) today's workouts
#   (3) today's appointments (ask how they went)
# Tasks are intentionally absent: lifekit has no task CLI yet (see task-update
# skill -- inert until implemented). Add done/remaining sections when it lands.
#
# Usage: daily-retro.sh [--dry]   (--dry = print data + prompt, no send)
# Exit:  0 ok / 1 config error
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIFE_SH="$AGENT_DIR/database/lifekit.sh"
PUSH_SH="$AGENT_DIR/routines/push.sh"
DRY=0
[[ "${1:-}" == "--dry" ]] && DRY=1

[[ -x "$LIFE_SH" || -f "$LIFE_SH" ]] || { echo "[retro] lifekit.sh not found: $LIFE_SH" >&2; exit 1; }
[[ -f "$PUSH_SH" ]] || { echo "[retro] push.sh not found: $PUSH_SH" >&2; exit 1; }

TODAY="$(date +%F)"

# ---- locale (address + tone come from the instance i18n) ----
AGENT_LANG="$(grep -E '^AGENT_LANG=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
AGENT_LANG="${AGENT_LANG:-en}"
I18N="$AGENT_DIR/config/i18n/${AGENT_LANG}.json"
i18n_get() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2],""))' "$I18N" "$1" 2>/dev/null || true
}
ADDRESS="$(grep -E '^AGENT_ADDRESS=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
[[ -z "$ADDRESS" ]] && ADDRESS="$(i18n_get address)"
TONE="$(i18n_get tone_guide)"

# ---- (1)(2) diet / workout aggregates ----
AGG="$("$LIFE_SH" agg-day "$TODAY" 2>/dev/null || true)"
ag() { printf '%s\n' "$AGG" | grep "^$1=" | head -1 | cut -d= -f2-; }
DIET_CNT="$(ag meal_cnt)";     DIET_CNT="${DIET_CNT:-0}"
DIET_KCAL="$(ag intake_kcal)"; DIET_KCAL="${DIET_KCAL:-0}"
DIET_PROT="$(ag protein_g)";   DIET_PROT="${DIET_PROT:-0}"
WO_CNT="$(ag workout_cnt)";    WO_CNT="${WO_CNT:-0}"
WO_KCAL="$(ag burn_kcal)";     WO_KCAL="${WO_KCAL:-0}"
WO_MIN="$(ag workout_min)";    WO_MIN="${WO_MIN:-0}"

MEALS="$("$LIFE_SH" meal-find "$TODAY" 2>/dev/null | cut -f2 | paste -sd ', ' - || true)"
[[ -z "$MEALS" ]] && MEALS="(none)"
WO_TYPES="$("$LIFE_SH" workout-find "$TODAY" 2>/dev/null | cut -f2 | sort -u | grep -v '^$' | paste -sd ', ' - || true)"

# ---- calorie targets (deficit model; empty config -> zeros, handled below) ----
TARGETS="$("$LIFE_SH" targets --burn "${WO_KCAL:-0}" 2>/dev/null || true)"
read -r EFF_GOAL BMR NEAT DEFICIT GOAL_PROT <<< "$TARGETS" || true
EFF_GOAL="${EFF_GOAL:-0}"; GOAL_PROT="${GOAL_PROT:-0}"
BALANCE_LINE=""
if [[ "$EFF_GOAL" != "0" ]]; then
  DIFF=$(( DIET_KCAL - EFF_GOAL ))
  SIGN="+"; [[ $DIFF -lt 0 ]] && SIGN=""
  BALANCE_LINE="recommended intake ${EFF_GOAL} kcal vs actual ${DIET_KCAL} = ${SIGN}${DIFF} kcal (protein goal ${GOAL_PROT} g vs actual ${DIET_PROT} g)"
fi

# ---- (3) today's appointments ----
APPT_TXT="$("$LIFE_SH" appt-find "$TODAY" 2>/dev/null | awk -F'\t' '{
  t=$2; time=""; if (t ~ /T/) { time=substr(t, index(t,"T")+1, 5)" " }
  loc=($4=="")?"":"  @"$4; print "- " time $3 loc }' || true)"
APPT_CNT=0; [[ -n "$APPT_TXT" ]] && APPT_CNT="$(printf '%s\n' "$APPT_TXT" | wc -l | tr -d ' ')"

DATA="[today ${TODAY}]"$'\n\n'
DATA+="# diet (${DIET_CNT} meals)"$'\n'
if [[ "$DIET_CNT" -gt 0 ]]; then
  DATA+="intake ${DIET_KCAL} kcal, protein ${DIET_PROT} g"$'\n'
  DATA+="meals: ${MEALS}"$'\n'
else
  DATA+="(no meals logged today)"$'\n'
fi
DATA+=$'\n'"# workouts"$'\n'
if [[ "$WO_CNT" -gt 0 ]]; then
  DATA+="${WO_TYPES:-workout} ${WO_MIN} min, burned ${WO_KCAL} kcal"$'\n'
else
  DATA+="(no workouts logged today)"$'\n'
fi
if [[ -n "$BALANCE_LINE" ]]; then
  DATA+=$'\n'"# calorie balance"$'\n'"${BALANCE_LINE}"$'\n'
fi
DATA+=$'\n'"# today's appointments (${APPT_CNT})"$'\n'
if [[ "$APPT_CNT" -gt 0 ]]; then DATA+="${APPT_TXT}"$'\n'; else DATA+="(none)"$'\n'; fi

NOTES=""
[[ "$DIET_CNT" -eq 0 ]] && NOTES+="NOTE: no meals logged; gently ask whether logging was missed. "
[[ "$WO_CNT" -eq 0 ]] && NOTES+="NOTE: no workouts logged; lightly ask if today was a rest day. "
[[ "$APPT_CNT" -gt 0 ]] && NOTES+="NOTE: there were appointments today; naturally ask how they went (mention you can record a short summary). "

PROMPT="You are the user's personal assistant sending a 10 PM daily retrospective over Telegram.
Write the retro in the user's language (locale: ${AGENT_LANG}). Address the user as: ${ADDRESS:-"(no fixed form of address; write naturally)"}.
Tone rules: ${TONE}

Structure (short labeled sections, emoji + line breaks, no prose walls):
- one-line day-closing opener
- diet: intake/protein and meal list (or note nothing was logged)
- workouts: type/minutes/burned kcal (or note nothing was logged)
- calorie balance line ONLY if present in the data
- appointments: ask in one line how each went; omit the section if none
- one-line closing (encouragement or gentle nudge)

${NOTES}
Use only the data below; never invent numbers or events. Output the message body only.

=== DATA ===
${DATA}"

if [[ "$DRY" -eq 1 ]]; then
  echo "---- DATA ----"; echo "$DATA"
  echo "---- PROMPT chars ----"; echo "${#PROMPT}"
  exit 0
fi

exec "$PUSH_SH" --model haiku --prompt "$PROMPT"

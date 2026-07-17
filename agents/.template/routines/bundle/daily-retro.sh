#!/bin/bash
# daily-retro.sh -- lifekit bundle routine: end-of-day retrospective.
# Data source: local lifekit.db (via database/lifekit.sh) and/or Warg inbox section.
#   local mode (RETRO_HEALTH_SOURCE=local, default):
#     (1) today's diet aggregate + calorie balance (agg-day / targets)
#     (2) today's workouts
#   warg mode (RETRO_HEALTH_SOURCE=warg):
#     (1)(2) replaced by Warg report.section.retro verbatim quote (DGN-319 pattern)
#   always:
#     (3) today's appointments (ask how they went)
#     (4) tomorrow's appointments (next-day preview)
#     (5) today's completed tasks + overdue backlog (omitted when both empty)
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
TOMORROW="$(date -v+1d +%F 2>/dev/null || date -d tomorrow +%F)"

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

# ---- timezone offset (integer hours, e.g. 9 for UTC+9) ----
TZ_OFFSET_H="$(grep -E '^AGENT_TZ_OFFSET_HOURS=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
TZ_OFFSET_H="${TZ_OFFSET_H:-0}"

# ---- health source gate (config-driven, DEFAULT local) ----
# RETRO_HEALTH_SOURCE=warg: skip local diet/workout/calorie computation and
# instead quote Warg's report.section.retro verbatim (DGN-319/DGN-389 pattern).
# Unset/empty -> local (all instances without an explicit warg opt-in are unchanged).
RETRO_HEALTH_SOURCE="$(grep -E '^RETRO_HEALTH_SOURCE=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
RETRO_HEALTH_SOURCE="${RETRO_HEALTH_SOURCE:-local}"

# ---- (1)(2) health data: local or Warg section ----
WARG_HEALTH_SECTION=""
DIET_CNT=0; DIET_KCAL=0; DIET_PROT=0
WO_CNT=0; WO_KCAL=0; WO_MIN=0
MEALS="(none)"; WO_TYPES=""; BALANCE_LINE=""

if [[ "$RETRO_HEALTH_SOURCE" == "warg" ]]; then
  # Read Warg's report.section.retro from Ag inbox.
  # Staleness guard: frontmatter created field must parse to TODAY (local tz).
  # Glob both TODAY_COMPACT and YDAY_COMPACT prefixes (Warg may submit just before midnight).
  AG_INBOX="$AGENT_DIR/files/handoff/inbox"
  TODAY_COMPACT="$(date +%Y%m%d)"
  YDAY_COMPACT="$(date -v-1d +%Y%m%d 2>/dev/null || date -d '-1 day' +%Y%m%d)"
  _read_section_body() {
    python3 -c "
import sys
txt = open(sys.argv[1]).read()
lines = txt.split('\n')
in_fm = False; closed = False; body = []
for i, l in enumerate(lines):
    if i == 0 and l.strip() == '---':
        in_fm = True; continue
    if in_fm and l.strip() == '---':
        closed = True; in_fm = False; continue
    if closed:
        body.append(l)
print('\n'.join(body).strip())
" "$1" 2>/dev/null || true
  }
  if [[ -d "$AG_INBOX" ]]; then
    for _prefix in "$TODAY_COMPACT" "$YDAY_COMPACT"; do
      for _f in "$AG_INBOX"/"${_prefix}"-report.section.retro-*.md; do
        [[ -f "$_f" ]] || continue
        _created_utc="$(grep '^created:' "$_f" 2>/dev/null | head -1 | awk '{print $2}')"
        _created_day="$(python3 -c "
import sys, datetime
s = sys.argv[1]; off = int('$TZ_OFFSET_H')
try:
    dt = datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    local_tz = datetime.timezone(datetime.timedelta(hours=off))
    local_dt = dt.astimezone(local_tz)
    print(local_dt.strftime('%Y-%m-%d'))
except Exception:
    print('')
" "$_created_utc" 2>/dev/null || true)"
        if [[ "$_created_day" == "$TODAY" ]]; then
          WARG_HEALTH_SECTION="$(_read_section_body "$_f")"
          break 2
        fi
      done
    done
  fi
  # Fallback: section absent or stale -> one-liner
  [[ -z "$WARG_HEALTH_SECTION" ]] && WARG_HEALTH_SECTION="워그 건강 리포트 미도착"

else
  # local mode: full diet/workout/calorie computation (unchanged behavior)
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
  # Truncate floats before integer arithmetic (lifekit can emit e.g. 160.0 from DB config).
  EFF_GOAL="${EFF_GOAL%.*}"; GOAL_PROT="${GOAL_PROT%.*}"
  if [[ "$EFF_GOAL" != "0" ]]; then
    DIFF=$(( DIET_KCAL - EFF_GOAL ))
    SIGN="+"; [[ $DIFF -lt 0 ]] && SIGN=""
    BALANCE_LINE="recommended intake ${EFF_GOAL} kcal vs actual ${DIET_KCAL} = ${SIGN}${DIFF} kcal (protein goal ${GOAL_PROT} g vs actual ${DIET_PROT} g)"
  fi
fi

# ---- (3) today's appointments ----
APPT_TXT="$("$LIFE_SH" appt-find "$TODAY" 2>/dev/null | awk -F'\t' '{
  t=$2; time=""; if (t ~ /T/) { time=substr(t, index(t,"T")+1, 5)" " }
  loc=($4=="")?"":"  @"$4; print "- " time $3 loc }' || true)"
APPT_CNT=0; [[ -n "$APPT_TXT" ]] && APPT_CNT="$(printf '%s\n' "$APPT_TXT" | wc -l | tr -d ' ')"

# ---- (4) tomorrow's appointments (simple next-day preview) ----
TMR_TXT="$("$LIFE_SH" appt-find "$TOMORROW" 2>/dev/null | awk -F'\t' '{
  t=$2; time=""; if (t ~ /T/) { time=substr(t, index(t,"T")+1, 5)" " }
  loc=($4=="")?"":"  @"$4; print "- " time $3 loc }' || true)"
TMR_CNT=0; [[ -n "$TMR_TXT" ]] && TMR_CNT="$(printf '%s\n' "$TMR_TXT" | wc -l | tr -d ' ')"

# ---- (5) task section: today's done + overdue backlog ----
# task-done-between today tomorrow: tasks completed today.
# task-overdue: incomplete tasks past their due date (all accumulated).
# Render: top few most-delayed overdue items + "외 N건" compression.
# Omit the entire section when both lists are empty.
TASK_DONE_ROWS="$("$LIFE_SH" task-done-between "$TODAY" "$TOMORROW" 2>/dev/null || true)"
TASK_DONE_CNT="$(printf '%s' "$TASK_DONE_ROWS" | grep -c . || true)"
TASK_OVERDUE_ROWS="$("$LIFE_SH" task-overdue 2>/dev/null || true)"
TASK_OVERDUE_CNT="$(printf '%s' "$TASK_OVERDUE_ROWS" | grep -c . || true)"

# Compress overdue list: show up to 3 most-delayed (first rows = oldest due), rest as "외 N건".
TASK_OVERDUE_SHOW=3
TASK_OVERDUE_TXT=""
if [[ "$TASK_OVERDUE_CNT" -gt 0 ]]; then
  _top="$(printf '%s\n' "$TASK_OVERDUE_ROWS" | head -"$TASK_OVERDUE_SHOW" | awk -F'\t' '{title=$2; due=$3; if(due=="") due="(기한 없음)"; print "- " title " (기한: " due ")"}' || true)"
  _rest=$(( TASK_OVERDUE_CNT - TASK_OVERDUE_SHOW ))
  if [[ "$_rest" -gt 0 ]]; then
    TASK_OVERDUE_TXT="${_top}"$'\n'"  외 ${_rest}건"
  else
    TASK_OVERDUE_TXT="${_top}"
  fi
fi

# ---- data block ----
DATA="[today ${TODAY}]"$'\n\n'

if [[ "$RETRO_HEALTH_SOURCE" == "warg" ]]; then
  DATA+="# warg health section"$'\n'"${WARG_HEALTH_SECTION}"$'\n'
else
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
fi

DATA+=$'\n'"# today's appointments (${APPT_CNT})"$'\n'
if [[ "$APPT_CNT" -gt 0 ]]; then DATA+="${APPT_TXT}"$'\n'; else DATA+="(none)"$'\n'; fi
DATA+=$'\n'"# tomorrow preview (${TOMORROW}, ${TMR_CNT} appointments)"$'\n'
if [[ "$TMR_CNT" -gt 0 ]]; then DATA+="${TMR_TXT}"$'\n'; else DATA+="(no appointments scheduled)"$'\n'; fi

# Task section: only include when there is data.
if [[ "$TASK_DONE_CNT" -gt 0 || "$TASK_OVERDUE_CNT" -gt 0 ]]; then
  DATA+=$'\n'"# tasks"$'\n'
  DATA+="done today: ${TASK_DONE_CNT}건"$'\n'
  if [[ "$TASK_OVERDUE_CNT" -gt 0 ]]; then
    DATA+="overdue backlog: ${TASK_OVERDUE_CNT}건"$'\n'
    DATA+="${TASK_OVERDUE_TXT}"$'\n'
  else
    DATA+="overdue backlog: 0건"$'\n'
  fi
fi

# ---- notes / prompt instructions ----
NOTES=""
if [[ "$RETRO_HEALTH_SOURCE" != "warg" ]]; then
  [[ "$DIET_CNT" -eq 0 ]] && NOTES+="NOTE: no meals logged; gently ask whether logging was missed. "
  [[ "$WO_CNT" -eq 0 ]] && NOTES+="NOTE: no workouts logged; lightly ask if today was a rest day. "
fi
[[ "$APPT_CNT" -gt 0 ]] && NOTES+="NOTE: there were appointments today; naturally ask how they went (mention you can record a short summary). "

# Content-experience appointments (config-driven, default off): when an
# appointment title matches a configured keyword (comma-separated in
# RETRO_CONTENT_TITLE_KEYWORDS), instruct the model to ask for the user's
# impressions/review of that content specifically. Empty/missing = off.
CONTENT_KEYWORDS_RAW="$(grep -E '^RETRO_CONTENT_TITLE_KEYWORDS=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
if [[ "$APPT_CNT" -gt 0 && -n "$CONTENT_KEYWORDS_RAW" ]]; then
  CONTENT_KEYWORDS_RE="${CONTENT_KEYWORDS_RAW//,/|}"
  if printf '%s\n' "$APPT_TXT" | grep -qE "$CONTENT_KEYWORDS_RE"; then
    NOTES+="NOTE: today's appointments include a content experience (title matches a configured keyword: movie/performance/exhibition/book etc). Ask specifically for the user's impressions and a short review of that content -- not just a generic 'how was it'. "
  fi
fi

# Warg section render instruction (warg mode only).
WARG_HEALTH_NOTE=""
if [[ "$RETRO_HEALTH_SOURCE" == "warg" ]]; then
  if [[ "$WARG_HEALTH_SECTION" == "워그 건강 리포트 미도착" ]]; then
    WARG_HEALTH_NOTE="The warg health section reports '워그 건강 리포트 미도착' -- output that exact one-liner for the health section without elaboration."
  else
    WARG_HEALTH_NOTE="A Warg health section is included in the data below under '# warg health section'. Render it VERBATIM as the health block of the retro -- do NOT rewrite, summarize, or add local diet/calorie numbers. This is the authoritative health source."
  fi
fi

# Task section render instruction (only when task data is present).
TASK_NOTE=""
if [[ "$TASK_DONE_CNT" -gt 0 || "$TASK_OVERDUE_CNT" -gt 0 ]]; then
  TASK_NOTE="A tasks section is included in the data below under '# tasks'. Render it as its own short section: done today as a count line, then overdue backlog items (list the shown items + 외 N건 remainder) -- keep it terse, no elaboration."
fi

HEALTH_STRUCT=""
if [[ "$RETRO_HEALTH_SOURCE" == "warg" ]]; then
  HEALTH_STRUCT="- warg health section: render verbatim from the '# warg health section' data"
else
  HEALTH_STRUCT="- diet: intake/protein and meal list (or note nothing was logged)
- workouts: type/minutes/burned kcal (or note nothing was logged)
- calorie balance line ONLY if present in the data"
fi

PROMPT="You are the user's personal assistant sending a 10 PM daily retrospective over Telegram.
Write the retro in the user's language (locale: ${AGENT_LANG}). Address the user as: ${ADDRESS:-"(no fixed form of address; write naturally)"}.
Tone rules: ${TONE}

Structure (short labeled sections, emoji + line breaks, no prose walls):
- one-line day-closing opener
${HEALTH_STRUCT}
- appointments: ask in one line how each went; omit the section if none
- tasks: done today count + overdue backlog highlights; omit the section entirely if the data contains no task data
- tomorrow preview: a short, simple heads-up of tomorrow's schedule (each appointment's time + name in one line); if none scheduled, one light line that tomorrow is open
- one-line closing (encouragement or gentle nudge)

${WARG_HEALTH_NOTE}
${TASK_NOTE}
${NOTES}
Use only the data below; never invent numbers or events. Output the message body only.

=== DATA ===
${DATA}"

if [[ "$DRY" -eq 1 ]]; then
  echo "---- RETRO_HEALTH_SOURCE ----"; echo "${RETRO_HEALTH_SOURCE}"
  echo "---- DATA ----"; echo "$DATA"
  echo "---- PROMPT chars ----"; echo "${#PROMPT}"
  exit 0
fi

exec "$PUSH_SH" --model haiku --prompt "$PROMPT"

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

# ---- title line (date + Korean weekday) ----
# KWEEKDAY = single Korean char for TODAY's weekday. Portable numeric weekday
# (0=Sun..6=Sat via %w): BSD/macOS `date -j -f`, GNU/Linux `date -d`.
_WNUM="$(date -j -f %Y-%m-%d "$TODAY" +%w 2>/dev/null || date -d "$TODAY" +%w)"
_KWEEK=(일 월 화 수 목 금 토)   # index by %w (0=Sun)
KWEEKDAY="${_KWEEK[$_WNUM]}"
TITLE="🌅 ${TODAY} (${KWEEKDAY}) 모닝 브리핑"

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

# ---- schedule title-prefix exclusion (config-driven, default off) ----
# Comma-separated list of title prefixes; a schedule event whose title (after
# stripping leading whitespace) starts with any listed prefix is omitted from
# the brief. Empty/missing = no filtering. Passed to python via env so config
# values never appear as code literals. Only surrounding whitespace of the raw
# value is trimmed here; per-item trim + split happen inside the python block.
# Trailing `|| true`: key is optional (default off); a no-match grep under
# `set -euo pipefail` would otherwise abort the whole brief.
EXCLUDE_PREFIXES="$(grep -E '^BRIEF_EXCLUDE_TITLE_PREFIXES=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' || true)"

# ---- yesterday diet/workout recap gate (config-driven, DEFAULT on) ----
# BRIEF_DIET_RECAP=off suppresses the "# yesterday recap" block entirely. Used
# on instances where the diet/workout domain was transferred to another agent,
# so local agg-day always returns 0 and the recap would be a false "0" line.
# Unset/empty -> on (instances that still own diet locally are unchanged).
BRIEF_DIET_RECAP="$(grep -E '^BRIEF_DIET_RECAP=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
BRIEF_DIET_RECAP="${BRIEF_DIET_RECAP:-on}"

# ---- weather image card gate (config-driven, DEFAULT off) ----
# BRIEF_WEATHER_CARD=on renders the weather+air-quality+quote PNG card and
# sends it as a photo after the text brief, suppressing the text weather block.
# Unset/empty -> off (instances keep the text weather line unless they opt in).
BRIEF_WEATHER_CARD="$(grep -E '^BRIEF_WEATHER_CARD=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
BRIEF_WEATHER_CARD="${BRIEF_WEATHER_CARD:-off}"

# ---- weather (Open-Meteo, no API key) ----
# Both coords must be set; any error -> WEATHER_TXT="" and brief continues normally.
AGENT_LAT="$(grep -E '^AGENT_LAT=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
AGENT_LNG="$(grep -E '^AGENT_LNG=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"

# ---- weather card (image, config-gated) ----
# When BRIEF_WEATHER_CARD=on, render the PNG card up front. CARD_PATH set +
# CARD_OK=1 on success; on any failure CARD_OK stays 0 and we fall back to the
# text weather path below (never lose weather silently). The card is SENT later
# in the tail (after the text brief), and only if not --dry.
# Interpreter: matplotlib is not in system python3. Resolve a render venv the
# same way the diet-log card does (RENDER_PYTHON env -> ~/dogany/.venvs/render
# -> PATH python3 only if matplotlib present).
CARD_OK=0
CARD_PATH=""
CARD_SH="$AGENT_DIR/routines/bundle/morning_brief_card.py"
if [[ "$BRIEF_WEATHER_CARD" == "on" ]]; then
  RENDER_PY="${RENDER_PYTHON:-}"
  [[ -z "$RENDER_PY" && -x "$HOME/dogany/.venvs/render/bin/python" ]] && RENDER_PY="$HOME/dogany/.venvs/render/bin/python"
  [[ -z "$RENDER_PY" ]] && command -v python3 >/dev/null && python3 -c 'import matplotlib' 2>/dev/null && RENDER_PY="$(command -v python3)"
  [[ -z "$RENDER_PY" ]] && RENDER_PY="python3"   # last resort (script exits non-zero if matplotlib missing)
  CARD_LAT="${AGENT_LAT:-37.5665}"; CARD_LNG="${AGENT_LNG:-126.9780}"
  # mktemp -d gives a clean dir so the output is a single .png with no orphan
  # base file (appending .png to a mktemp file would leave the base behind).
  CARD_TMPDIR="$(mktemp -d -t morning_brief_card)"
  CARD_PATH="$CARD_TMPDIR/card.png"
  if AGENT_ROOT="$AGENT_DIR" "$RENDER_PY" "$CARD_SH" --output "$CARD_PATH" --lat "$CARD_LAT" --lon "$CARD_LNG" >/dev/null 2>&1 \
     && [[ -s "$CARD_PATH" ]]; then
    CARD_OK=1
  else
    echo "[brief] weather card generation failed; falling back to text weather" >&2
    rm -rf "$CARD_TMPDIR" 2>/dev/null || true
    CARD_PATH=""
  fi
fi

# Text weather is suppressed only when the card is on AND actually generated.
# Otherwise (card off, or card failed) we build the text weather as before.
WEATHER_TXT=""
if [[ "$CARD_OK" -ne 1 && -n "$AGENT_LAT" && -n "$AGENT_LNG" ]]; then
  WEATHER_TXT="$(python3 -c "
import sys, json
from datetime import datetime, timezone, timedelta
try:
    from urllib.request import urlopen
    from urllib.error import URLError
    lat = '$AGENT_LAT'
    lng = '$AGENT_LNG'
    url = (
        'https://api.open-meteo.com/v1/forecast'
        '?latitude=' + lat +
        '&longitude=' + lng +
        '&hourly=precipitation_probability,temperature_2m'
        '&timezone=auto&forecast_days=1'
    )
    with urlopen(url, timeout=5) as r:
        if r.status != 200:
            sys.exit(0)
        data = json.loads(r.read())
    times = data['hourly']['time']           # list of 'YYYY-MM-DDTHH:MM'
    precip = data['hourly']['precipitation_probability']
    temps  = data['hourly']['temperature_2m']
    # max/min across the full day
    t_max = max(t for t in temps if t is not None)
    t_min = min(t for t in temps if t is not None)
    lines = ['최고 ' + str(round(t_max)) + 'C / 최저 ' + str(round(t_min)) + 'C']
    # key hours at 3h steps starting from the current local hour onward
    now_local = datetime.now(tz=timezone(timedelta(hours=int('$TZ_OFFSET_H'))))
    key_hours = [h for h in [9, 12, 15, 18, 21] if h >= now_local.hour]
    for i, ts in enumerate(times):
        dt = datetime.strptime(ts, '%Y-%m-%dT%H:%M')
        if dt.hour in key_hours:
            p = precip[i] if precip[i] is not None else 0
            t = temps[i]  if temps[i]  is not None else 0
            lines.append('- ' + str(dt.hour).zfill(2) + '시 ' + str(round(p)) + '% ' + str(round(t)) + 'C')
    print('\n'.join(lines))
except Exception:
    pass
" 2>/dev/null || true)"
fi

# ---- (1) today's timed schedule (appointments + task blocks via event-window) ----
# Window = [today 00:00 local, tomorrow 00:00 local) converted to UTC.
# e.g. offset +9: (TODAY-1)T15:00:00Z .. TODAY T15:00:00Z.
# Computed inline via python3 to avoid bash integer-overflow on edge offsets.
read -r FROM_UTC TO_UTC < <(python3 -c "
from datetime import datetime, timezone, timedelta
off = int('$TZ_OFFSET_H')
tz = timezone(timedelta(hours=off))
today = datetime.strptime('$TODAY', '%Y-%m-%d').replace(tzinfo=tz)
tmrw = today + timedelta(days=1)
from_utc = today.astimezone(timezone.utc)
to_utc   = tmrw.astimezone(timezone.utc)
print(from_utc.strftime('%Y-%m-%dT%H:%M:%SZ') + ' ' + to_utc.strftime('%Y-%m-%dT%H:%M:%SZ'))
")
SCHED_TXT="$("$LIFE_SH" event-window "$FROM_UTC" "$TO_UTC" 2>/dev/null | BRIEF_EXCLUDE_TITLE_PREFIXES="$EXCLUDE_PREFIXES" python3 -c "
import sys, os
from datetime import datetime, timezone, timedelta
LOCAL_TZ = timezone(timedelta(hours=int('$TZ_OFFSET_H')))
# Title-prefix exclusion list (config-driven). Read from env so config values
# never appear as code literals. Split on comma, trim each item, drop empties.
_raw = os.environ.get('BRIEF_EXCLUDE_TITLE_PREFIXES', '')
EXCLUDE_PREFIXES = [p.strip() for p in _raw.split(',') if p.strip()]
for line in sys.stdin:
    cols = line.rstrip('\n').split('\t')
    if len(cols) < 5:
        continue
    title = cols[2]; sa = cols[3]; ea = cols[4]
    # Skip events whose title starts with any excluded prefix (leading ws stripped).
    if any(title.lstrip().startswith(p) for p in EXCLUDE_PREFIXES):
        continue
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
# Gated by BRIEF_DIET_RECAP (default on). When off, skip agg-day entirely so a
# transferred diet domain (local agg-day = 0) does not leak a false "0" recap.
Y_KCAL=0; Y_MEALS=0; Y_WO=0; Y_BURN=0
if [[ "$BRIEF_DIET_RECAP" == "on" ]]; then
  AGG="$("$LIFE_SH" agg-day "$YDAY" 2>/dev/null || true)"
  ag() { printf '%s\n' "$AGG" | grep "^$1=" | head -1 | cut -d= -f2-; }
  Y_KCAL="$(ag intake_kcal)"; Y_KCAL="${Y_KCAL:-0}"
  Y_MEALS="$(ag meal_cnt)";   Y_MEALS="${Y_MEALS:-0}"
  Y_WO="$(ag workout_cnt)";   Y_WO="${Y_WO:-0}"
  Y_BURN="$(ag burn_kcal)";   Y_BURN="${Y_BURN:-0}"
fi

# ---- (3) Warg health section -- look for today's report.section.morning ----
# Sections stay in inbox (VERDICT_LEAVE in ag_handlers); we read directly.
# Staleness guard: only accept a section whose frontmatter created field is
# TODAY (local tz, YYYY-MM-DD). Warg submits ahead of the brief, so the
# filename prefix may be YDAY_COMPACT or TODAY_COMPACT -- glob both and let
# the frontmatter created field be the authoritative same-day filter.
AG_INBOX="$AGENT_DIR/files/handoff/inbox"
WARG_SECTION=""
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
    for _f in "$AG_INBOX"/"${_prefix}"-report.section.morning-*.md; do
      [[ -f "$_f" ]] || continue
      # parse created field from frontmatter (YAML subset: created: YYYY-MM-DDTHH:MM:SSZ)
      # convert UTC created to local date using TZ_OFFSET_H for the same-day check
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
        WARG_SECTION="$(_read_section_body "$_f")"
        break 2
      fi
    done
  done
fi

# ---- (4) contact-gap suggestions (relationship-care alert-pick) ----
# alert-pick owns all gating (weekly cap, snooze, fade, checkin) and logs
# 'shown' as a side effect -- the morning brief IS the weekly surface. We only
# render its PICK lines. PICK carries meeting-based fields:
#   PICK <pid> <name> <gapdays> <ratio> <hint> <meet_days> <meet_ctx>
# We render each as `name | meet_ctx | meet_days일전`. Persons with an upcoming
# appointment are already dropped inside alert-pick. CAP_REACHED/NONE -> empty.
REL_SH="$AGENT_DIR/database/relmod.sh"
CONTACT_TXT=""
if [[ -f "$REL_SH" ]]; then
  CONTACT_TXT="$("$REL_SH" alert-pick 2>/dev/null | python3 -c "
import sys
rows = []
for line in sys.stdin:
    cols = line.rstrip('\n').split('\t')
    if not cols or cols[0] != 'PICK':
        continue
    # PICK pid name gapdays ratio hint meet_days meet_ctx
    name = cols[2] if len(cols) > 2 else ''
    meet_days = cols[6] if len(cols) > 6 else ''
    meet_ctx  = cols[7] if len(cols) > 7 else ''
    ctx = meet_ctx if meet_ctx else '-'
    when = (meet_days + '일전') if meet_days != '' else '-'
    rows.append(name + ' | ' + ctx + ' | ' + when)
print('\n'.join(rows))
" || true)"
fi

DATA="[today ${TODAY}]"$'\n\n'
DATA+="# title"$'\n'"${TITLE}"$'\n\n'
DATA+="# today's schedule: ${SCHED_CNT}"$'\n'
if [[ "$SCHED_CNT" -gt 0 ]]; then DATA+="${SCHED_TXT}"$'\n'; else DATA+="(none)"$'\n'; fi
# Yesterday recap block only when BRIEF_DIET_RECAP=on (else omit entirely).
if [[ "$BRIEF_DIET_RECAP" == "on" ]]; then
  DATA+=$'\n'"# yesterday recap"$'\n'
  DATA+="meals logged: ${Y_MEALS} (intake ${Y_KCAL} kcal), workouts: ${Y_WO} (burned ${Y_BURN} kcal)"$'\n'
fi

WARG_SECTION_NOTE=""
if [[ -n "$WARG_SECTION" ]]; then
  DATA+=$'\n'"# warg section"$'\n'
  DATA+="${WARG_SECTION}"$'\n'
  WARG_SECTION_NOTE="A Warg section is included in the data below under '# warg section'. Render it FIRST after the title, under a header line that reads EXACTLY '💪 운동 - 워그' (this header carries the 워그 attribution). Render the content as terse '- ' bullet items, NOT prose -- keep it short. If the data uses bracket labels ([현재 프로그램] / [어제 회고] / [오늘 세팅] or similar), preserve those labels as-is and itemize under them. If it is prose, itemize best-effort. Do NOT drop, merge, or invent any fact. This is authoritative for diet/protein/workout; add no separate diet recap."
fi

CONTACT_NOTE=""
if [[ -n "$CONTACT_TXT" ]]; then
  DATA+=$'\n'"# contact suggestions"$'\n'
  DATA+="${CONTACT_TXT}"$'\n'
  CONTACT_NOTE="A contact-suggestions block is included in the data below under '# contact suggestions'. Render it as its own short section: a header line that reads EXACTLY '💬 연락해보세요', then each data row VERBATIM on its own line, unchanged (format 'name | context | N일전'). Do NOT rewrite rows into sentences, do NOT add guilt/urgency wording, do NOT reorder or merge columns, do NOT add trailing punctuation. Omit this whole section if no such data is present."
fi

WEATHER_NOTE=""
if [[ -n "$WEATHER_TXT" ]]; then
  DATA+=$'\n'"# weather"$'\n'
  DATA+="${WEATHER_TXT}"$'\n'
  WEATHER_NOTE="A weather section is included in the data below under '# weather'. Render a single weather line near the schedule section; use 🌧️ if the max precipitation probability across the listed hours is >= 40%, otherwise use ☀️. Omit this line entirely when no weather data is present."
fi

PROMPT="You are the user's personal assistant sending the morning briefing over Telegram.
Write the briefing in the user's language (locale: ${AGENT_LANG}). Address the user as: ${ADDRESS:-"(no fixed form of address; write naturally)"}.
Tone rules: ${TONE}

Structure (in this exact order; no closing line):
- first output line is EXACTLY the title provided in the data (the '# title' value), verbatim
- second output line is ONE short greeting sentence in the agent's own voice (address the user per the address/tone rules); it may reflect today's context from the data (weekday, empty or busy schedule) but must never invent facts; exactly one line, no icon, no filler beyond this single sentence
- 💪 운동 - 워그 section next (only if a warg section is present in the data)
- today's schedule under the 📅 header, ONLY if schedule items are present; if there are none, OMIT the schedule section entirely -- no line and no filler commentary (no 'open day' / '여유로운 하루' / etc.)
- 💬 연락해보세요 contact list (only if present in the data)

Icon rules (fixed, apply the SAME icons every brief -- do not vary or substitute):
- title line starts with 🌅 (use the provided title verbatim)
- 💪 운동 - 워그 header for the warg section (carries the 워그 attribution)
- schedule section header line starts with 📅 (bullets themselves stay plain)
- contact suggestions header line is EXACTLY 💬 연락해보세요 (rows below it stay plain, one per line, verbatim)

${WARG_SECTION_NOTE}
${CONTACT_NOTE}
${WEATHER_NOTE}
Use only the data below; never invent schedules or numbers. Output the message body only.

=== DATA ===
${DATA}"

if [[ "$DRY" -eq 1 ]]; then
  echo "---- DATA ----"; echo "$DATA"
  echo "---- PROMPT chars ----"; echo "${#PROMPT}"
  echo "---- weather card ----"
  if [[ "$BRIEF_WEATHER_CARD" == "on" ]]; then
    if [[ "$CARD_OK" -eq 1 ]]; then
      echo "card ON: would send photo AFTER text brief: ${CARD_PATH} ($(wc -c < "$CARD_PATH" | tr -d ' ') bytes)"
    else
      echo "card ON but generation failed: falling back to text weather this run"
    fi
  else
    echo "card OFF: text weather in DATA"
  fi
  [[ -n "${CARD_TMPDIR:-}" ]] && rm -rf "$CARD_TMPDIR" 2>/dev/null || true
  exit 0
fi

# Send the TEXT brief FIRST, then the weather card photo AFTER. When a card
# must follow, do NOT exec the text push (exec would replace the process and
# the card push below would never run); run it, then push the card, then exit.
# If no card (off or gen failed), exec the text push as the final step.
if [[ "$CARD_OK" -eq 1 ]]; then
  "$PUSH_SH" --model haiku --prompt "$PROMPT" || echo "[brief] text brief send failed" >&2
  "$PUSH_SH" --photo "$CARD_PATH" || echo "[brief] weather card send failed (text already sent)" >&2
  [[ -n "${CARD_TMPDIR:-}" ]] && rm -rf "$CARD_TMPDIR" 2>/dev/null || true
  exit 0
fi

exec "$PUSH_SH" --model haiku --prompt "$PROMPT"

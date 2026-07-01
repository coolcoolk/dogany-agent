#!/bin/bash
# weekly-review.sh — 일요일 21:00 주간 회고 (__AGENT_LABEL__)
# 1부 주간 브리핑: 이번 주(월~일, KST) 운동·식단·완료 태스크·약속 집계.
# 2부 성찰 회고: __USER_LABEL__께 한 주를 정리하는 질문(기분 좋았던/안 좋았던 일,
#                무슨 생각, 누구를 만났는지 등)을 던져 대화로 마무리.
#
# 사용법: weekly-review.sh [--dry]
# 종료코드: 0 성공 / 1 설정오류

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$AGENT_DIR/runtime/.env"
DRY=0
[[ "${1:-}" == "--dry" ]] && DRY=1

NOTION_TOKEN="$(grep -E '^NOTION_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')"
if [[ -z "$NOTION_TOKEN" ]]; then echo "[weekly] NOTION_TOKEN 없음 ($ENV_FILE)" >&2; exit 1; fi
NV="Notion-Version: 2022-06-28"
AUTH="Authorization: Bearer ${NOTION_TOKEN}"
LIFE_SH="$AGENT_DIR/../../database/lifekit.sh"

DB_TASKS="b19ede9c-ac48-4cb9-861a-45eb48d333b2"
DB_PROMISE="1790d222-9835-8070-81b7-d7602629b6d8"
DB_PROJECT="7534fb66-1c49-48ec-bdc6-16fd34771642"

# ---- 이번 주 경계 (KST, 월요일 0시 ~ 다음주 월요일 0시) ----
DOW="$(date +%u)"                                   # 1=월..7=일
MON="$(date -v-$((DOW-1))d +%F)"
NEXTMON="$(date -v-$((DOW-1))d -v+7d +%F)"
TODAY="$(date +%F)"
WEEK_START="${MON}T00:00:00+09:00"
WEEK_END="${NEXTMON}T00:00:00+09:00"
TODAY_START="${TODAY}T00:00:00+09:00"

q() { curl -s "https://api.notion.com/v1/databases/$1/query" \
        -H "$AUTH" -H "$NV" -H "Content-Type: application/json" -d "$2"; }

# ---- 운동·식단 (이번 주): 로컬 lifekit.db (agg-week) ----
WEEK_AGG="$("$LIFE_SH" agg-week "$MON" 2>/dev/null || true)"
aw_get() { echo "$WEEK_AGG" | grep "^$1=" | head -1 | cut -d= -f2-; }
# 일별추이 블록(--- daily --- 아래 줄들)
DAILY_TREND="$(echo "$WEEK_AGG" | sed -n '/^--- daily ---/,$p' | tail -n +2)"

WO_CNT="$(aw_get workout_cnt)";   WO_CNT="${WO_CNT:-0}"
WO_MIN="$(aw_get workout_min)";   WO_MIN="${WO_MIN:-0}"
WO_KCAL="$(aw_get burn_total)";   WO_KCAL="${WO_KCAL:-0}"
DIET_CNT="$(aw_get meal_cnt)";    DIET_CNT="${DIET_CNT:-0}"
DIET_DAYS="$(aw_get meal_days)";  DIET_DAYS="${DIET_DAYS:-0}"
DIET_KCAL="$(aw_get intake_total)"; DIET_KCAL="${DIET_KCAL:-0}"
DIET_AVG="$(aw_get intake_avg_day)"; DIET_AVG="${DIET_AVG:-0}"
# 전주 대비
PREV_DIET_AVG="$(aw_get prev_intake_avg_day)"; PREV_DIET_AVG="${PREV_DIET_AVG:-0}"
PREV_WO_CNT="$(aw_get prev_workout_cnt)";       PREV_WO_CNT="${PREV_WO_CNT:-0}"
DIFF_DIET_AVG="$(aw_get diff_intake_avg)";       DIFF_DIET_AVG="${DIFF_DIET_AVG:-0}"
DIFF_WO_CNT="$(aw_get diff_workout_cnt)";         DIFF_WO_CNT="${DIFF_WO_CNT:-0}"
DIFF_BURN="$(aw_get diff_burn_total)";            DIFF_BURN="${DIFF_BURN:-0}"
# 운동 유형 (이번 주, 중복 제거)
WO_TYPES="$(for i in 0 1 2 3 4 5 6; do D="$(date -v-$((DOW-1))d -v+${i}d +%F)"; "$LIFE_SH" workout-find "$D" 2>/dev/null; done | cut -f2 | sort -u | grep -v '^$' | paste -sd '|' - | sed 's/|/, /g')"

# ---- 완료 태스크 (이번 주 예정/완료) ----
DONE_JSON="$(q "$DB_TASKS" "{\"filter\":{\"and\":[{\"property\":\"완료\",\"checkbox\":{\"equals\":true}},{\"property\":\"예정/완료 날짜\",\"date\":{\"on_or_after\":\"$WEEK_START\"}},{\"property\":\"예정/완료 날짜\",\"date\":{\"before\":\"$WEEK_END\"}}]},\"page_size\":100}")"
DONE_CNT="$(echo "$DONE_JSON" | jq -r 'if .object=="error" then 0 else (.results|length) end' 2>/dev/null)"

# ---- 미완료 누적 (오늘 0시 이전 미완료) ----
OVERDUE_JSON="$(q "$DB_TASKS" "{\"filter\":{\"and\":[{\"property\":\"완료\",\"checkbox\":{\"equals\":false}},{\"property\":\"예정/완료 날짜\",\"date\":{\"before\":\"$TODAY_START\"}}]},\"page_size\":100}")"
OVERDUE_CNT="$(echo "$OVERDUE_JSON" | jq -r 'if .object=="error" then 0 else (.results|length) end' 2>/dev/null)"

# ---- 약속 (이번 주) ----
PROM_JSON="$(q "$DB_PROMISE" "{\"filter\":{\"and\":[{\"property\":\"날짜\",\"date\":{\"on_or_after\":\"$WEEK_START\"}},{\"property\":\"날짜\",\"date\":{\"before\":\"$WEEK_END\"}}]},\"page_size\":100}")"
PROM_CNT="$(echo "$PROM_JSON" | jq -r 'if .object=="error" then 0 else (.results|length) end' 2>/dev/null)"
PROM_TXT="$(echo "$PROM_JSON" | jq -r 'if .object=="error" then "" else ([.results[].properties."이름".title[0].plain_text // empty]|join(", ")) end' 2>/dev/null)"

# ---- 진행 중 프로젝트 ----
PROJ_JSON="$(q "$DB_PROJECT" "{\"filter\":{\"property\":\"상태\",\"status\":{\"equals\":\"진행 중\"}},\"page_size\":100}")"
PROJ_CNT="$(echo "$PROJ_JSON" | jq -r 'if .object=="error" then 0 else (.results|length) end' 2>/dev/null)"
PROJ_TXT="$(echo "$PROJ_JSON" | jq -r 'if .object=="error" then "" else ([.results[].properties."이름".title[0].plain_text // empty]|join(", ")) end' 2>/dev/null)"

# ---- 데이터 블록 ----
DATA="[이번 주 ${MON} ~ $(date -v-$((DOW-1))d -v+6d +%F) (KST)]"$'\n'
DATA+="운동: ${WO_CNT}회"
[[ "$WO_CNT" -gt 0 ]] && DATA+=" / ${WO_MIN}분 / 소모 ${WO_KCAL}kcal${WO_TYPES:+ / $WO_TYPES}"
DATA+=" (지난주 ${PREV_WO_CNT}회 대비 ${DIFF_WO_CNT}회, 소모 ${DIFF_BURN}kcal)"
DATA+=$'\n'"식단: ${DIET_CNT}건 기록 (${DIET_DAYS}일치)"
[[ "$DIET_CNT" -gt 0 ]] && DATA+=" / 합계 ${DIET_KCAL}kcal / 일평균 ${DIET_AVG}kcal"
DATA+=" (지난주 일평균 ${PREV_DIET_AVG}kcal 대비 ${DIFF_DIET_AVG}kcal)"
DATA+=$'\n'"완료한 일: ${DONE_CNT}건 / 아직 못 끝낸 누적: ${OVERDUE_CNT}건"
DATA+=$'\n'"약속: ${PROM_CNT}건${PROM_TXT:+ ($PROM_TXT)}"
DATA+=$'\n'"진행 중 프로젝트: ${PROJ_CNT}건${PROJ_TXT:+ ($PROJ_TXT)}"
DATA+=$'\n\n'"[일별 추이 (섭취/소모)]"$'\n'"${DAILY_TREND}"

LOW_DATA=""
if [[ "$WO_CNT" -eq 0 && "$DIET_CNT" -eq 0 && "$DONE_CNT" -eq 0 && "$PROM_CNT" -eq 0 ]]; then
  LOW_DATA="※ 이번 주 트래킹된 데이터가 거의 없음. 숫자를 억지로 부풀리지 말고, 기록이 적었다는 점만 가볍게 짚은 뒤 곧바로 성찰 질문으로 넘어갈 것."
fi

PROMPT="너는 __USER_LABEL__의 생활비서 __AGENT_LABEL__다. 지금은 일요일 밤 9시, 한 주를 마무리하는 주간 회고 시간이다. __USER_LABEL__께 보낼 주간 회고 메시지를 한글로 작성해라.
절대 규칙: 사용자를 반드시 __USER_LABEL__이라고 부르고 메시지 첫머리를 __USER_LABEL__ 호칭으로 연다. 반드시 공손한 존댓말(반말 금지). 별표 기호(**)나 마크다운 강조, 표는 쓰지 말고 기호는 최소화. 이모지는 한두 개 이하.
구성은 두 부분이다.
1부 주간 브리핑: 아래 집계 데이터를 바탕으로 이번 주 운동·식단·완료한 일·약속을 두세 문장으로 짧게 정리하고, 잘한 점이나 아쉬운 점을 가볍게 한마디 곁들인다. 진행 중인 프로젝트가 있으면, 특히 목표가 걸린 프로젝트(예: 체중감량)는 이번 주 운동·식단 흐름과 연결해 그 진척을 한 줄로 돌아본다. 지난주 대비 증감(운동 횟수·식단 일평균 칼로리)과 일별 추이에서 눈에 띄는 흐름(예: 주말에 섭취 급증, 운동이 특정 요일에 몰림)이 보이면 한두 마디로 짚어준다. 추이 숫자를 나열만 하지 말고 흐름·변화를 해석해라.
2부 성찰 회고: 이어서 __USER_LABEL__이 한 주를 스스로 정리할 수 있도록 따뜻하게 질문을 던진다. 이번 주 가장 기분 좋았던 일과 속상했거나 안 좋았던 일이 무엇이었는지, 요즘 어떤 생각을 하고 계신지, 누구를 만났는지 등을 자연스럽게 물어라. __USER_LABEL__이 편하게 답하고 싶게, 질문은 두세 개 정도로 추리고 열린 질문으로 끝맺는다.
${LOW_DATA}
데이터에 있는 항목만 근거로 쓰고 없는 수치는 지어내지 마라. 전체 8~12문장 이내. 메시지 본문만 출력하고 다른 설명은 붙이지 마라.

=== 이번 주 데이터 ===
${DATA}"

if [[ "$DRY" -eq 1 ]]; then
  echo "---- DATA ----"; echo "$DATA"
  echo "---- LOW_DATA ----"; echo "${LOW_DATA:-(없음)}"
  echo "---- PROMPT 길이 ----"; echo "${#PROMPT} chars"
  exit 0
fi

exec "$SCRIPT_DIR/push.sh" --model haiku --prompt "$PROMPT"

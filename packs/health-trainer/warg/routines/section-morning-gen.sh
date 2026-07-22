#!/bin/bash
# section-morning-gen.sh -- HANDOFF_SECTION_GENERATOR for the morning section.
#
# Called by section_submit.py as: section-morning-gen.sh morning
# stdout = section body (Korean, <=10 lines, no preamble).
#
# Pulls real nutrition data from Warg lifekit, injects numbers into the
# section-morning.md prompt so the model never invents protein figures.
# Headless claude invocation mirrors digest-run.sh (env -u CLAUDECODE pattern).
#
# Test seam: SECTION_MORNING_CLAUDE_CMD overrides the claude invocation.
#   export SECTION_MORNING_CLAUDE_CMD='cat /tmp/section-stub.txt'
#
# Exit: 0 + section on stdout; non-zero on failure (section_submit.py will
# propagate the error and leave the state file absent for retry).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WARG_ROOT="${WARG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LIFE_SH="$WARG_ROOT/database/lifekit.sh"
PROMPT_TPL="$SCRIPT_DIR/prompts/section-morning.md"

# ---- portable yesterday date (BSD macOS -v; GNU -d) ----
if date -v-1d +%F >/dev/null 2>&1; then
  YDAY="$(date -v-1d +%F)"
else
  YDAY="$(date -d '-1 day' +%F)"
fi

# ---- pull yesterday protein from lifekit ----
AGG="$("$LIFE_SH" agg-day "$YDAY" 2>/dev/null || true)"
ag_val() { printf '%s\n' "$AGG" | grep "^$1=" | head -1 | cut -d= -f2-; }
PROT_ACTUAL="$(ag_val protein_g)"; PROT_ACTUAL="${PROT_ACTUAL:-0}"
KCAL_ACTUAL="$(ag_val intake_kcal)"; KCAL_ACTUAL="${KCAL_ACTUAL:-0}"
MEALS="$(ag_val meal_cnt)"; MEALS="${MEALS:-0}"
WORKOUTS="$(ag_val workout_cnt)"; WORKOUTS="${WORKOUTS:-0}"

# ---- pull protein target from lifekit config ----
# targets output: eff_goal bmr neat deficit protein_goal  (space-separated)
TARGETS="$("$LIFE_SH" targets 2>/dev/null || true)"
PROT_TARGET="$(printf '%s' "$TARGETS" | awk '{print $5}')"
PROT_TARGET="${PROT_TARGET:-160}"
# strip trailing decimals for display (160.0 -> 160)
PROT_TARGET_INT="$(printf '%.0f' "$PROT_TARGET" 2>/dev/null || printf '%s' "$PROT_TARGET")"

# ---- compute shortfall (integer, floor; 0 if met/exceeded) ----
PROT_ACTUAL_INT="$(printf '%.0f' "$PROT_ACTUAL" 2>/dev/null || printf '%s' "$PROT_ACTUAL")"
SHORTFALL=$(( PROT_TARGET_INT > PROT_ACTUAL_INT ? PROT_TARGET_INT - PROT_ACTUAL_INT : 0 ))

# ---- build enriched prompt (data header + original template) ----
DATA_BLOCK="=== HEALTH DATA (어제 ${YDAY}) ===
어제 단백질: ${PROT_ACTUAL_INT}g / 목표: ${PROT_TARGET_INT}g / 부족: ${SHORTFALL}g
어제 칼로리: ${KCAL_ACTUAL} kcal
어제 식사 횟수: ${MEALS} / 운동 횟수: ${WORKOUTS}
=== END DATA ===

아래 지침에 따라 섹션을 작성하라.
IMPORTANT: 단백질 수치는 위 데이터의 정확한 숫자를 그대로 사용하라 -- 수치를 만들어내지 말 것.
반드시 \"어제 단백질 ${PROT_ACTUAL_INT}g / 목표 ${PROT_TARGET_INT}g (부족 ${SHORTFALL}g)\" 형식 한 줄을 포함하라.

"

FULL_PROMPT="${DATA_BLOCK}$(cat "$PROMPT_TPL")"

# ---- invoke headless claude (or test stub) ----
if [ -n "${SECTION_MORNING_CLAUDE_CMD:-}" ]; then
  eval "$SECTION_MORNING_CLAUDE_CMD"
else
  # Strip CLAUDECODE vars so headless claude does not detect an enclosing session.
  env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT \
    claude -p "$FULL_PROMPT" \
    --allowedTools "Bash,Read,Write,Edit,Glob,Grep" \
    --cwd "$WARG_ROOT"
fi

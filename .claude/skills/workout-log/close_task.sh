#!/bin/bash
# close_task.sh -- auto-close today's workout task after a workout log.
#
# The workout-log skill calls this after recording a workout to auto-complete
# that day's "운동" task. This standalone version is lifekit-backed (SQL lane),
# NOT Notion. It degrades gracefully: if lifekit has no task CLI yet (task-update
# is the pending SQL path), it prints NONE and exits 0 -- the skill then simply
# skips the "task closed" line, and the workout log itself still succeeds.
#
# Usage:
#   close_task.sh [YYYY-MM-DD]   date defaults to today (KST)
#
# stdout:
#   CLOSED<TAB><name>   exactly one matching todo found and completed
#   NONE                no matching open workout task (or task CLI unavailable)
#   MULTI               2+ candidates -- not auto-closed; candidate lines follow:
#     <id><TAB><name>
#
# Exit codes: 0 success (CLOSED/NONE/MULTI) / 1 config/arg error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve lifekit.sh path-independently: prefer $PROJECT_ROOT, else walk up from
# the skill dir (.claude/skills/workout-log -> three levels up = PROJECT_ROOT).
if [[ -n "${PROJECT_ROOT:-}" && -x "$PROJECT_ROOT/database/lifekit.sh" ]]; then
  LIFEKIT="$PROJECT_ROOT/database/lifekit.sh"
else
  ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
  LIFEKIT="$ROOT/database/lifekit.sh"
fi

DAY="${1:-$(TZ=Asia/Seoul date +%F)}"

# task CLI not implemented in lifekit yet -> graceful no-op (matches task-update
# being the pending SQL path). Detect by probing for the 'task-find' subcommand.
if [[ ! -x "$LIFEKIT" ]] || ! "$LIFEKIT" task-find "운동" >/dev/null 2>&1; then
  echo "NONE"
  exit 0
fi

# When lifekit gains a task CLI, this is the intended contract:
#   task-find "운동"  -> id<TAB>name<TAB>due<TAB>done|todo   (one row per match)
#   task-done <id>    -> mark complete
MATCHES="$("$LIFEKIT" task-find "운동" 2>/dev/null \
  | awk -F'\t' -v d="$DAY" '$3==d && $4=="todo" {print $1"\t"$2}')"

COUNT="$(printf '%s\n' "$MATCHES" | grep -c . || true)"

case "$COUNT" in
  0)
    echo "NONE"
    ;;
  1)
    ID="$(printf '%s\n' "$MATCHES" | head -1 | cut -f1)"
    NAME="$(printf '%s\n' "$MATCHES" | head -1 | cut -f2)"
    "$LIFEKIT" task-done "$ID" >/dev/null 2>&1 || { echo "NONE"; exit 0; }
    printf 'CLOSED\t%s\n' "$NAME"
    ;;
  *)
    echo "MULTI"
    printf '%s\n' "$MATCHES"
    ;;
esac

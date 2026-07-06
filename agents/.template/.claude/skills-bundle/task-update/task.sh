#!/bin/bash
# task.sh -- THIN delegator to lifekit.sh task-* verbs (task-update skill).
#
# lifekit.py (via lifekit.sh) OWNS lifekit.db. This script contains ZERO SQL:
# every verb is forwarded to the owner CLI. Do not add sqlite3 calls here --
# schema knowledge lives in one place only (database/lifekit.py).
#
# Usage (mirrors lifekit.sh task-* 1:1):
#   task.sh add <title> [due_date] [note]     new task
#   task.sh find [date|all|keyword]           list, TSV: id<TAB>title<TAB>due<TAB>done|todo
#   task.sh done <id>                         mark done
#   task.sh undone <id>                       revert to todo
#   task.sh reschedule <id> <YYYY-MM-DD>      change due date
#   task.sh archive <id>                      soft-delete (hidden from find/overdue)
#   task.sh overdue                           todos with due date before today
#
# Exit codes: passed through from lifekit.sh unchanged.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve PROJECT_ROOT: prefer env, else walk up from skills-bundle dir
# (.claude/skills-bundle/task-update -> three levels up = PROJECT_ROOT).
if [[ -n "${PROJECT_ROOT:-}" ]]; then
  ROOT="$PROJECT_ROOT"
else
  ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
fi

LIFEKIT="$ROOT/database/lifekit.sh"
if [[ ! -x "$LIFEKIT" ]]; then
  echo "[task] lifekit.sh not found or not executable: $LIFEKIT" >&2
  exit 1
fi

CMD="${1:-}"
case "$CMD" in
  add|find|done|undone|reschedule|archive|overdue)
    shift
    exec "$LIFEKIT" "task-${CMD}" "$@"
    ;;
  *)
    echo "usage: task.sh add|find|done|undone|reschedule|archive|overdue ..." >&2
    exit 1
    ;;
esac

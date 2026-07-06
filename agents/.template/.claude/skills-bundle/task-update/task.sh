#!/bin/bash
# task.sh -- task management helper for task-update skill.
# Storage: lifekit.db tasks table (SQLite, same DB as meals/workouts).
# Does NOT use Notion. Does NOT require network.
#
# Usage:
#   task.sh find "keyword"              -> id<TAB>name<TAB>due<TAB>done|todo
#   task.sh done <id>                   mark task done=1
#   task.sh undone <id>                 mark task done=0
#   task.sh reschedule <id> <YYYY-MM-DD>  update due_start to new date
#   task.sh archive <id>                  soft-delete: set note to [archived]
#   task.sh overdue                     list todos where due_start < today (local date)
#
# Exit codes: 0 ok / 1 arg or config error / 2 db error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve PROJECT_ROOT: prefer env, else walk up from skills-bundle dir
# (.claude/skills-bundle/task-update -> three levels up = PROJECT_ROOT).
if [[ -n "${PROJECT_ROOT:-}" ]]; then
  ROOT="$PROJECT_ROOT"
else
  ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
fi

DB="$ROOT/database/lifekit.db"

if [[ ! -f "$DB" ]]; then
  echo "[task] lifekit.db not found: $DB" >&2
  exit 2
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "[task] sqlite3 not available" >&2
  exit 2
fi

CMD="${1:-}"
[[ -z "$CMD" ]] && { echo "usage: task.sh find|done|undone|reschedule|archive|overdue ..." >&2; exit 1; }

# today in local date for overdue comparison
TODAY="$(date +%F)"

case "$CMD" in
  find)
    KEYWORD="${2:-}"
    [[ -z "$KEYWORD" ]] && { echo "usage: task.sh find <keyword>" >&2; exit 1; }
    sqlite3 -separator $'\t' "$DB" \
      "SELECT id, name, COALESCE(due_start,''), CASE WHEN done=1 THEN 'done' ELSE 'todo' END
       FROM tasks
       WHERE name LIKE '%${KEYWORD}%' AND (note IS NULL OR note NOT LIKE '%[archived]%')
       ORDER BY due_start ASC, id ASC
       LIMIT 20;"
    ;;

  done)
    ID="${2:-}"
    [[ -z "$ID" ]] && { echo "usage: task.sh done <id>" >&2; exit 1; }
    sqlite3 "$DB" "UPDATE tasks SET done=1 WHERE id=${ID};" || { echo "[task] db error" >&2; exit 2; }
    NAME="$(sqlite3 "$DB" "SELECT name FROM tasks WHERE id=${ID};")"
    echo "[task] done: ${NAME:-id=${ID}}"
    ;;

  undone)
    ID="${2:-}"
    [[ -z "$ID" ]] && { echo "usage: task.sh undone <id>" >&2; exit 1; }
    sqlite3 "$DB" "UPDATE tasks SET done=0 WHERE id=${ID};" || { echo "[task] db error" >&2; exit 2; }
    NAME="$(sqlite3 "$DB" "SELECT name FROM tasks WHERE id=${ID};")"
    echo "[task] undone: ${NAME:-id=${ID}}"
    ;;

  reschedule)
    ID="${2:-}"
    DATE="${3:-}"
    [[ -z "$ID" || -z "$DATE" ]] && { echo "usage: task.sh reschedule <id> <YYYY-MM-DD>" >&2; exit 1; }
    # Validate date format loosely
    if ! echo "$DATE" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
      echo "[task] bad date format -- expected YYYY-MM-DD" >&2; exit 1
    fi
    sqlite3 "$DB" "UPDATE tasks SET due_start='${DATE}' WHERE id=${ID};" || { echo "[task] db error" >&2; exit 2; }
    NAME="$(sqlite3 "$DB" "SELECT name FROM tasks WHERE id=${ID};")"
    echo "[task] rescheduled: ${NAME:-id=${ID}} -> ${DATE}"
    ;;

  archive)
    ID="${2:-}"
    [[ -z "$ID" ]] && { echo "usage: task.sh archive <id>" >&2; exit 1; }
    sqlite3 "$DB" "UPDATE tasks SET note=COALESCE(note||' ','')|| '[archived]' WHERE id=${ID};" \
      || { echo "[task] db error" >&2; exit 2; }
    NAME="$(sqlite3 "$DB" "SELECT name FROM tasks WHERE id=${ID};")"
    echo "[task] archived: ${NAME:-id=${ID}}"
    ;;

  overdue)
    sqlite3 -separator $'\t' "$DB" \
      "SELECT id, name, COALESCE(due_start,''), CASE WHEN done=1 THEN 'done' ELSE 'todo' END
       FROM tasks
       WHERE done=0
         AND due_start IS NOT NULL
         AND due_start < '${TODAY}'
         AND (note IS NULL OR note NOT LIKE '%[archived]%')
       ORDER BY due_start ASC, id ASC;"
    ;;

  *)
    echo "unknown verb: $CMD (find|done|undone|reschedule|archive|overdue)" >&2; exit 1 ;;
esac

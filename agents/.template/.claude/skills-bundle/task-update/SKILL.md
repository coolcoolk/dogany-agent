---
name: task-update
description: >-
  When __USER_LABEL__ says they did something, the agent finds it in the Tasks DB and
  marks it done. Fires on utterances like "I did my vocab today", "finished the gym",
  "reading done", "did my face mask", "handled X". Conversely, "I didn't do X",
  "cancel that", "set it back to incomplete", "that got checked by mistake" reverts
  the completion (undone). Also searches and answers task-progress questions
  ("what's left today", "what do I have to do", "what's overdue"). Handles
  reschedule ("move X to Friday"), archive ("drop X"), and overdue list.
---

# task-update -- task management (SQL edition, lifekit.db)

storage: lifekit.db tasks table (SQLite). no network, no Notion.
task.sh is the interface; agent calls it with Bash tool.

## trigger signals
- done:       "X did / finished / completed / handled / done"
- undo:       "X not done / cancel / checked by mistake / set back"
- query:      "what's left today / what do I have to do"
- overdue:    "what's overdue / what did I miss"
- reschedule: "move X to <date> / push X to <date>"
- archive:    "drop X / remove X / delete X from tasks"

## tools (task.sh -- lives in this skill directory)
```
task.sh find "keyword"              id<TAB>name<TAB>due<TAB>done|todo
task.sh done <id>                   mark done=1
task.sh undone <id>                 mark done=0
task.sh reschedule <id> YYYY-MM-DD  change due date
task.sh archive <id>                soft-delete (hidden from find/overdue)
task.sh overdue                     list todo rows with due_start < today
```
all verbs: exit 0 ok / exit 1 arg error / exit 2 db error.

## procedure

### done / undo
1. extract task keyword from utterance -> task.sh find.
2. recurring tasks -> today todo first; if none, most recent overdue todo.
3. unique match -> task.sh done (or undone). report in one line.
4. ambiguous (2+ matches) -> show list, ask __USER_LABEL__ which one.
5. 0 results -> say not found, ask if __USER_LABEL__ wants to create new.

### reschedule
1. extract keyword + new date -> find.
2. unique match -> task.sh reschedule <id> <YYYY-MM-DD>. confirm change.
3. ambiguous -> ask first.

### archive
1. extract keyword -> find.
2. confirm with __USER_LABEL__ before archiving (irreversible from user's view).
3. task.sh archive <id>.

### overdue
1. task.sh overdue -> list all overdue todos.
2. if empty -> say nothing is overdue.
3. show as simple list: name (due date). do not auto-close.

## boundaries
- done/undo = own data -> proceed without extra confirm. ambiguous -> ask first.
- archive = soft-delete -> confirm before running.
- multiple in one utterance ("vocab and reading done") -> each: find -> done.
- telegram tone: address = __USER_LABEL__. no asterisk bold. concise.

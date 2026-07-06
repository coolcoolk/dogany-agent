---
name: task-update
description: >-
  When __USER_LABEL__ says they did something, the agent finds it in the Tasks DB and
  marks it done. Fires on utterances like "I did my vocab today", "finished the gym",
  "reading done", "did my face mask", "handled X". Conversely, "I didn't do X",
  "cancel that", "set it back to incomplete", "that got checked by mistake" reverts
  the completion (undone). Also searches and answers task-progress questions
  ("what's left today", "what do I have to do", "what's overdue"). Handles
  new tasks ("add X for Friday"), reschedule ("move X to Friday"), archive
  ("drop X"), and overdue list.
---

# task-update -- task management (lifekit.db, owner-CLI edition)

storage: lifekit.db tasks table. OWNED by database/lifekit.py (lifekit.sh).
task.sh here is a THIN delegator -- it only forwards to lifekit.sh task-*.
never query the sqlite file directly. agent calls task.sh with Bash tool.

## trigger signals
- done:       "X did / finished / completed / handled / done"
- undo:       "X not done / cancel / checked by mistake / set back"
- query:      "what's left today / what do I have to do"
- overdue:    "what's overdue / what did I miss"
- add:        "add task X / remind me to X on <date>"
- reschedule: "move X to <date> / push X to <date>"
- archive:    "drop X / remove X / delete X from tasks"

## tools (task.sh in this skill dir -> lifekit.sh task-*)
```
task.sh add <title> [due] [note]    new task. echoes row
task.sh find [date|all|keyword]     TSV: id<TAB>title<TAB>due<TAB>done|todo
task.sh done <id>                   mark done. echoes updated row
task.sh undone <id>                 back to todo. echoes updated row
task.sh reschedule <id> YYYY-MM-DD  change due date. echoes updated row
task.sh archive <id>                soft-delete (hidden from find/overdue)
task.sh overdue                     todos with due before today
```
exit codes pass through from lifekit.sh (0 ok / 1 arg or config error).

## procedure

### done / undo
1. extract task keyword from utterance -> task.sh find "keyword".
2. recurring tasks -> today todo first; if none, most recent overdue todo.
3. unique match -> task.sh done (or undone). report in one line.
4. ambiguous (2+ matches) -> show list, ask __USER_LABEL__ which one.
5. 0 results -> say not found, ask if __USER_LABEL__ wants to create new
   -> yes: task.sh add "title" [due].

### add
1. extract title + optional due date -> task.sh add.
2. report the created row in one line.

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
3. show as simple list: title (due date). do not auto-close.

## boundaries
- done/undo/add = own data -> proceed without extra confirm. ambiguous -> ask first.
- archive = soft-delete -> confirm before running.
- multiple in one utterance ("vocab and reading done") -> each: find -> done.
- data goes through the owner: lifekit.sh only, never raw sqlite.
- telegram tone: address = __USER_LABEL__. no asterisk bold. concise.

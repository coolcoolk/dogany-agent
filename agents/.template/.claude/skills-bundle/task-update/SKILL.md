---
name: task-update
description: >-
  When __USER_LABEL__ says they did something, the agent finds it in the Tasks DB and
  marks it done. Fires on utterances like "I did my vocab today", "finished the gym",
  "reading done", "did my face mask", "handled X". Conversely, "I didn't do X",
  "cancel that", "set it back to incomplete", "that got checked by mistake" reverts
  the completion (undone). Also searches and answers task-progress questions
  ("what's left today", "what do I have to do"). lifekit.db tasks-based SQL edition
  (activates once the task CLI is implemented).
---

# task-update — task completion (SQL version, future standard)

lifekit.db tasks table SQL version. task CLI not yet implemented in lifekit — pending.
interface below is the target standard; activate once task CLI is added.

## trigger signals
- done: "X 했어 / 끝냈어 / 완료 / 처리했어 / 했음"
- undo: "X 안 했어 / 취소 / 잘못 체크됐어 / 다시 미완료로"
- query: "오늘 뭐 남았어 / 할 일 뭐 있지"

## target tools (not yet implemented)
when lifekit gets task CLI, adopt this interface as standard:
```
lifekit.sh task-find "keyword"   -> id<TAB>name<TAB>scheduled_date<TAB>done|todo
lifekit.sh task-done <id>        done=true
lifekit.sh task-undone <id>      done=false
```
currently not runnable (activate after lifekit task CLI added).

## procedure (apply after implementation)
1. extract task keyword from __USER_LABEL__ utterance -> task-find.
2. recurring tasks -> today (KST) todo first -> if none, most recent overdue todo.
3. unique match -> run done, report in one line.
4. ambiguous -> show candidates, ask __USER_LABEL__.
5. 0 results -> inform none found, ask if __USER_LABEL__ wants to create new.
6. undo request -> undone.

## boundaries
- done/undo = __USER_LABEL__'s own data -> proceed without confirm. ambiguous match -> ask first.
- multiple in one utterance ("말해보카랑 독서 했어") -> each: find -> done.
- telegram tone: address = __USER_LABEL__. no bold (asterisk). concise.

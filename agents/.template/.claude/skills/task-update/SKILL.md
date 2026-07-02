---
name: task-update
description: >-
  __USER_LABEL__이 한 일을 말하면 에이전트가 Tasks DB에서 찾아 완료 처리한다. "오늘 말해보카 했어", "헬스 끝냈어",
  "독서 완료", "마스크팩 했음", "X 처리했어" 류 발화에 발동. 반대로 "X 안 했어", "그거 취소",
  "다시 미완료로", "잘못 체크됐어"면 완료를 되돌린다(undone). 태스크 진행상황 질문("오늘 뭐 남았지",
  "할 일 뭐 있어")에도 검색해 답한다. lifekit.db tasks 기반 SQL판(task CLI 구현 후 활성화).
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

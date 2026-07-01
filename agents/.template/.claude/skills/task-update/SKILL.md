---
name: task-update
description: >-
  __USER_LABEL__이 한 일을 말하면 에이전트가 Tasks DB에서 찾아 완료 처리한다. "오늘 말해보카 했어", "헬스 끝냈어",
  "독서 완료", "마스크팩 했음", "X 처리했어" 류 발화에 발동. 반대로 "X 안 했어", "그거 취소",
  "다시 미완료로", "잘못 체크됐어"면 완료를 되돌린다(undone). 태스크 진행상황 질문("오늘 뭐 남았지",
  "할 일 뭐 있어")에도 검색해 답한다. lifekit.db tasks 기반 SQL판(task CLI 구현 후 활성화).
---

# task-update — 태스크 완료 관리 (SQL판, 미래 표준)

이 스킬은 lifekit.db tasks 테이블 기반 SQL판이다.
lifekit에 task CLI가 아직 없어 헬퍼는 미구현(pending) 상태다 — 아래 인터페이스가 구현되면 그대로 동작한다.

## 발동 신호
- 완료: "X 했어 / 끝냈어 / 완료 / 처리했어 / 했음"
- 되돌리기: "X 안 했어 / 취소 / 잘못 체크됐어 / 다시 미완료로"
- 조회: "오늘 뭐 남았어 / 할 일 뭐 있지"

## 목표 도구 (미구현)
향후 lifekit에 task CLI가 생기면 아래 인터페이스를 표준으로 채택할 것:
```
lifekit.sh task-find "키워드"   → id<TAB>이름<TAB>예정날짜<TAB>done|todo
lifekit.sh task-done <id>       완료=true
lifekit.sh task-undone <id>     완료=false
```
현재는 구현되지 않아 실행 불가(lifekit에 task CLI 추가 후 활성화).

## 절차 (구현 후 적용)
1. __USER_LABEL__ 발화에서 태스크 키워드를 뽑아 task-find.
2. 반복 태스크는 오늘(KST) 날짜의 todo → 없으면 가장 최근 밀린 todo.
3. 유일하게 정해지면 done 실행하고 한 줄 보고.
4. 애매하면 후보 보여주며 __USER_LABEL__께 질문.
5. 0개면 없다고 알리고 새로 만들지 __USER_LABEL__께 확인.
6. 되돌리기는 undone.

## 경계·톤
- 완료/되돌리기는 __USER_LABEL__ 본인 데이터라 확인 없이 진행 OK. 단 대상이 애매하면 질문 우선.
- 한 번에 여러 건("말해보카랑 독서 했어") → 각각 find→done.
- 텔레그램 톤: __USER_LABEL__ 호칭, 존댓말, 별표·불필요 기호 금지, 간결하게.

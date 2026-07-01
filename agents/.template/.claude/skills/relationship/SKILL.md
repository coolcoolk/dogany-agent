---
name: relationship
description: >-
  __USER_LABEL__이 사람·관계를 조회하거나 등록하거나 별명으로 찾을 때 발동. "이름이 뭐더라", "OO 누구야",
  "OO 관계에 추가해줘", 별명/닉네임으로 사람을 언급할 때. 공용 SQL판(lifekit.sh)으로 persons 관리.
  이 스킬은 lifekit.db 기반 미래 표준 SQL판이다.
---

# relationship — 관계 관리 (SQL판, 미래 표준)

이 스킬은 lifekit.db의 persons 테이블을 lifekit.sh CLI로 관리한다.
lifekit(로컬 SQLite 라이프 OS)를 SoT로 삼는 표준 SQL판이다.

- 헬퍼 경로: `$PROJECT_ROOT/database/lifekit.sh` (레포 루트 기준. PROJECT_ROOT 미설정 시 스킬 위치에서 상위로 올라가 database/를 찾는다)
- SoT: 로컬 `$PROJECT_ROOT/database/lifekit.db` (persons 테이블)
- 별명(aliases)은 lifekit.db persons.aliases 컬럼에 콤마 구분으로 저장 — Notion과 달리 네이티브 지원

## lifekit.sh 빠른 참조
```
person-find  <이름또는별명>                 → id<TAB>name<TAB>relation<TAB>aliases
person-add   <name> [relation] [aliases]    → 새 person id
person-alias <id> <별명>                    → 별명 추가(중복 무시)
```

## 사람 조회 절차
1. `lifekit.sh person-find <이름또는별명>` (name + aliases 동시 검색)
2. 1명이면 그 사람 정보 보고
3. 0명이면 __USER_LABEL__께 "새 사람인가요, 기존 별명인가요?" 질문 (추측 등록 금지)
4. 다수이면 후보 보여주고 __USER_LABEL__ 확인

## 사람 등록 절차
1. `lifekit.sh person-add <name> [relation] [aliases]`
2. 별명이 있으면 `lifekit.sh person-alias <id> <별명>`
3. 완료 후 보고

## 경계·톤
- 사람 삭제는 __USER_LABEL__ 확인 후에만.
- 볼드(별표) 금지, 호칭 __USER_LABEL__, 존댓말.
- 이 스킬의 SoT는 lifekit.db 한 곳이다. 다른 저장소와 혼용하지 말 것.

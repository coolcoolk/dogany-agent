---
name: appointment-log
description: >-
  사용자이 약속/일정을 잡거나 조회·수정하거나 참가자를 다룰 때 발동. "X랑 약속 잡았어",
  "누구누구 만나기로 했어", "이번 주(오늘/내일/주말) 약속 뭐 있지", "그 약속에 누구 추가해줘",
  "약속 장소/시간 바꿔줘", "OO이랑 저녁 약속", 사람 이름이나 별명(예: 사카린)을 약속 맥락에서
  언급할 때 사용. lifekit.db의 appointments / persons / appointment_persons를 lifekit.sh
  CLI로 직접 다룬다. 핵심은 사람을 약속에 붙일 때 본명·별명으로 먼저 찾고, 없거나 모호하면
  사용자께 확인하는 "사람 해소 규칙"이다.
---

# appointment-log — 약속 관리

사용자의 약속(appointments)을 lifekit.db에서 등록·수정·조회하고, 사람(persons)을
참가자로 연결한다. 모든 DB 접근은 `lifekit.sh`(=lifekit.py 코어)를 통한다 — 생 SQL 금지.

- 헬퍼 경로: `$PROJECT_ROOT/database/lifekit.sh` (레포 루트 기준. PROJECT_ROOT 미설정 시 스킬 위치에서 상위로 올라가 database/를 찾는다)
- SoT는 로컬 lifekit.db. Notion 동기화 없음(notion_id는 과거 임포트 잔재). 직접 수정 안전.
- 시간대는 항상 사용자 기준 GMT+9(+09:00). 날짜는 YYYY-MM-DD, 시작/끝은 ISO(+09:00) 권장.

## 데이터 구조
- `appointments`: id, title, start_at, end_at, location, location_url, purpose, summary
- `persons`: id, name, relation, aliases(별명, 콤마조인), birthday, job …
- `appointment_persons`: (appointment_id, person_id) N:M 조인

## 사람 해소 규칙 (참가자 붙일 때 반드시 이 순서)
사용자이 약속에 사람을 언급하면(본명이든 별명이든) 곧장 등록하지 말고 먼저 찾는다.

```
1. lifekit.sh person-find <이름또는별명>   (name + aliases 동시 검색)
2. 정확히 1명 매치   → 그 사람으로 바로 연결 (appt-person)
3. 0명 매치          → 사용자께 질문:
      "OO님은 기존에 등록된 누구의 별명인가요, 아니면 새 사람인가요?"
      - 기존 사람의 별명 → person-alias <id> <별명> 로 별명 달고 → 연결
      - 새 사람          → person-add <name> [relation] [aliases] 로 등록 후 → 연결
4. 2명 이상 매치      → 후보를 보여주고 누구인지 사용자께 확인 후 연결
```
질문은 [[OPTIONS]] 형식(번호 목록, 마지막 줄에 마커)으로 던진다. 추측해서 새로
만들지 말 것 — 0명이어도 동명이인/별명일 수 있으니 반드시 사용자 확인.

## 약속 등록·수정 절차
1. 시간 파싱: "내일 저녁 7시" 같은 표현은 현재 시각(GMT+9) 기준으로 ISO 변환.
   start_at은 필수. end_at 미지정시 appt-add가 자동으로 시작+3시간을 디폴트로 넣는다
   (사용자이 끝시간/소요시간을 주면 그 값 우선).
2. `lifekit.sh appt-add <title> <start_at> [end_at location purpose summary]` → 새 id.
3. 참가자는 위 "사람 해소 규칙"을 사람마다 적용해 `appt-person <appt_id> <person_id>`.
4. 부분 수정은 `appt-upd <id> field=value ...` (지정 필드만 바뀜).
5. 끝나면 `appt-show <id>`로 약속+참가자 한 번 확인하고 사용자께 요약 보고.

## CLI 빠른 참조 (lifekit.sh)
```
person-find  <이름또는별명>                         → id  name  relation  aliases
person-add   <name> [relation] [aliases]            → 새 person id
person-alias <id> <별명>                            → 별명 추가(중복 무시)
appt-find    <date_from> [date_to]                  → 기간 내 약속 목록
appt-add     <title> <start_at> [end_at loc purpose summary] → 새 appt id
appt-upd     <id> field=value ...                   → 부분 수정
            (field: title start_at end_at location location_url purpose summary)
appt-person  <appt_id> <person_id>                  → 참가자 연결
appt-show    <id>                                   → 약속 1건 + 참가자 전체
```
빈 인자는 ""로 자리만 잡는다. 예: `appt-add "AI Day" "2026-07-05T12:00:00+09:00" "" "우리집"`

## 조회 응답
- "이번 주 약속" 류는 회사·이동·준비 같은 반복 블록은 빼고 사람 약속·할 일만 추려 보고.
- 표·정렬 데이터는 코드블록(monospace)으로. 볼드(별표) 금지, 호칭은 사용자, 존댓말.

## 경계
- 캘린더(Google Calendar) 반영이 필요하면 별도로 사용자께 확인 후 처리(이 스킬은 로컬 DB만).
- 약속 삭제는 되돌리기 어려우니 사용자 확인 후에만.

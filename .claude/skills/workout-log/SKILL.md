---
name: workout-log
description: >
  사용자이 운동을 했다고 말하면 발동. "헬스 40분 했어", "오늘 러닝 5km 뛰었어",
  "운동 끝 400kcal 소모", "요가 1시간", "PT 받았어", "가슴운동 했어", "자전거 탔어" 등
  운동 종류·시간·소모칼로리·거리 언급이 들어간 모든 운동 입력 상황에 사용.
  로컬 lifekit.db(신체건강 영역)에 운동 한 건을 기록하고 그날 칼로리 밸런스 카드를 갱신한다.
---

# workout-log 스킬

## 개요

사용자이 운동을 했다고 말하면 로컬 lifekit.db의 workouts 테이블에 기록한다.
식단(diet-log)과 한 DB를 쓰며, 운동 소모칼로리는 그날 칼로리 밸런스(섭취−소모) 카드에 자동 반영된다.
모든 DB 접근은 `lifekit.sh`(= lifekit.py 코어)를 통한다 — 생 SQL 금지.

## 경로 규약

- 헬퍼: `$PROJECT_ROOT/database/lifekit.sh` (레포 루트 기준. PROJECT_ROOT 미설정 시 스킬 위치에서 상위로 올라가 database/를 찾는다).
- 아래 예시의 상대경로(`.claude/...`, `routines/...`, `files/...`)는 워크스페이스 루트(PROJECT_ROOT)를 cwd로 가정한다. 스킬 디렉토리로 cd하지 말 것.

## 기록 대상: lifekit.db (로컬 SQLite, SoT)

- 운동은 로컬 lifekit.db의 workouts 테이블에 기록한다. 헬퍼는 `$PROJECT_ROOT/database/lifekit.sh`.
- area_id는 lifekit.sh가 "신체건강" 영역으로 자동 연결한다 (직접 지정 불필요).
- 컬럼: date, type(=대분류 category), name(=세부 subtype), minutes, kcal(활동 소모칼로리), note, avg_hr(평균 심박수, bpm), type_id(workout_types 사전 FK, N:M).
- ★운동 분류는 정규화 사전(workout_types 테이블)으로 통일한다. type=대분류, name=세부 를 아래 분류 사전 어휘로 결정해 넣는다. lifekit.sh가 (category, subtype)로 type_id를 자동 조회해 채운다. 사전에 없으면 자동 등록(경고만, 실패 안 함).
- ★kcal은 반드시 활동(active) 칼로리만 넣는다. 애플워치가 "활동 / 총"을 따로 주면 활동값을 kcal에. 총 칼로리는 운동 시간대 기초대사가 포함돼 이중계산되므로 kcal에 넣지 말 것(필요하면 note에 "총 Xkcal"로 보존).

## 운동 분류 사전 (workout_types) — 대분류 / 세부

발화를 아래 어휘에 맞춰 (대분류, 세부) 한 쌍으로 결정한다.

- 근력: 가슴, 등, 어깨, 하체, 삼두, 이두, 복근
- 유산소: 러닝, 사이클, 스텝퍼, 걷기, 로잉, 수영, 일립티컬
- 유연·회복: 요가, 스트레칭, 폼롤러
- 스포츠·액티비티: 등산, 클라이밍, 축구, 농구, 테니스, 골프
- 펑셔널·HIIT: 크로스핏, F45, 서킷, HIIT, 부트캠프, 컨디셔닝

매핑 가이드(흔한 발화 → 분류):
- "헬스 / 가슴운동" → 근력/가슴, "등운동" → 근력/등, "하체/스쿼트" → 근력/하체, "어깨" → 근력/어깨, "이두" → 근력/이두, "삼두" → 근력/삼두, "복근/코어/플랭크" → 근력/복근.
- "러닝 / 5km 뛰었어" → 유산소/러닝(거리는 note), "자전거" → 유산소/사이클, "천국의 계단/스텝퍼" → 유산소/스텝퍼, "걷기/산책" → 유산소/걷기.
- "요가" → 유연·회복/요가, "스트레칭" → 유연·회복/스트레칭, "폼롤러" → 유연·회복/폼롤러.
- "등산" → 스포츠·액티비티/등산, "클라이밍/볼더링" → 스포츠·액티비티/클라이밍, "축구/농구/테니스/골프" → 해당.
- "크로스핏/F45/HIIT/서킷/부트캠프" → 펑셔널·HIIT/해당.
- "PT 받았어" 처럼 부위를 모르면 사용자께 짧게 부위를 확인하고, 주요 부위를 복수로 기록한다.
- "전신/풀바디" 류는 단일 "전신" 분류가 없으니, 그날 실제로 한 부위들을 복수 분류로 나눠 기록한다.

★분류가 사전에 없으면: 가장 가까운 세부로 매핑하되, 애매하면 사용자께 한 번 확인한다.
사전에 없는 값을 그대로 넣으면 lifekit이 workout_types에 자동 등록하고 stderr에 "미등록 분류 자동 등록" 경고를 남긴다(가급적 사전 어휘로).

## 절차

1. 사용자 발화에서 (대분류/세부)를 위 분류 사전에 맞춰 결정하고, 시간(분)·소모칼로리·기타 세부를 뽑는다.
   - "헬스 가슴운동 40분" → category=근력, subtype=가슴, minutes=40
   - "러닝 5km" → category=유산소, subtype=러닝 (거리 "5km"는 note에)
   - "운동 끝 400kcal" → kcal=400 (분류 모르면 사용자께 짧게 확인)
2. 소모칼로리(kcal)를 사용자이 안 줬으면:
   - 애플워치 등으로 아는 값이 있으면 그걸 우선(단 "활동 칼로리", 총 칼로리 아님).
   - 없으면 종류·시간으로 합리적 추정(근력 ~6kcal/분, 러닝 ~10kcal/분, 요가 ~4kcal/분). 추정값임은 보고에 가볍게 표시.
   - 평균 심박수(bpm)가 있으면 avg_hr로 기록(없으면 생략 = NULL).
3. lifekit.sh workout-add로 기록 (인자 1=대분류 category, 인자 2=세부 subtype).

```bash
$PROJECT_ROOT/database/lifekit.sh workout-add \
  "<YYYY-MM-DD>" "<category>" "<subtype>" <minutes> <kcal> "<note>" <avg_hr>
```

- 인자 순서: date category subtype minutes kcal note [avg_hr]
- avg_hr(평균 심박수, bpm)는 맨 끝 선택 인자. 모르면 생략 → NULL.
- 모르는 값은 0(숫자) 또는 빈 문자열(텍스트)로. name/note 없으면 ""로.
- 출력: `id<TAB>type<TAB>name<TAB>kcal<TAB>avg_hr` (avg_hr 미지정 시 끝 칸은 빔).

### 복수 부위(분류) 입력 — 한 운동에 부위가 둘 이상일 때

"가슴+삼두", "등이랑 이두", "하체+코어" 처럼 한 운동에 부위가 여러 개면, DB가 다대다(workout_classifications junction)라 운동 한 건에 분류를 여러 개 붙일 수 있다.

- (a) 첫 부위로 workout-add 실행 — minutes/kcal/note/avg_hr 같은 운동 전체 메타는 이때 다 넣는다. 출력 첫 칸에서 새 workout id를 받는다.
- (b) 나머지 부위는 각각 `workout-classify <그 id> <대분류> <세부>`로 추가 연결한다(INSERT OR IGNORE라 중복 무시).
- (c) minutes·kcal은 운동 1건 전체 값이라 add에서 한 번만 넣고, classify에는 분류만 붙인다(시간·칼로리 중복 입력 금지).

```bash
# "가슴+삼두 40분 240kcal": 가슴=근력/가슴, 삼두=근력/삼두
OUT=$($PROJECT_ROOT/database/lifekit.sh workout-add "$(date +%F)" "근력" "가슴" 40 240 "가슴+삼두")
WID=$(echo "$OUT" | head -1 | cut -f1)
$PROJECT_ROOT/database/lifekit.sh workout-classify "$WID" "근력" "삼두"
```

- workout-find가 부위를 묶어 `근력  가슴, 삼두`처럼 복원하므로, 분할 균형 집계에 두 부위 모두 잡힌다.

4. 그날 운동 태스크 자동 완료(선택). workout-add가 성공하면 이어서 실행한다.

```bash
bash .claude/skills/workout-log/close_task.sh
```

   - (테스트·과거 날짜 지정 시 첫 인자로 YYYY-MM-DD.)
   - 출력 `CLOSED<TAB><이름>` → 그날 운동 태스크를 자동 완료처리했다. 보고에 "Tasks의 '<이름>'도 완료처리했어요" 한 줄 덧붙인다.
   - 출력 `NONE` → 완료할 운동 태스크가 없거나(정상), lifekit에 task CLI가 아직 없어 우아하게 스킵됨. 아무 말도 덧붙이지 않고 조용히 넘어간다.
   - 출력 `MULTI` → 후보가 2건 이상. 둘째 줄부터 `id<TAB>이름` 후보. 사용자께 어느 걸 닫을지 물어본 뒤 task-update 스킬로 처리한다.
   - (task-update는 lifekit SQL 기반 미래 표준이며 task CLI 구현 후 활성화된다. 그전까지 close_task.sh는 NONE으로 무해하게 스킵한다.)

## 보고 형식 (사용자께)

기록 후 간결하게 (별표·마크다운 금지, 존댓말):

```
헬스 40분 — 운동 기록 완료
소모 240kcal
Tasks의 '운동'도 완료처리했어요
```

소모칼로리가 추정값이면 끝에 가볍게 "(추정)".
마지막 "Tasks ... 완료처리했어요" 줄은 close_task.sh가 CLOSED를 반환했을 때만 붙인다(NONE이면 생략, MULTI면 사용자께 확인 후 처리).

## 칼로리 밸런스 카드 갱신

운동을 기록하면 그날 칼로리 밸런스가 바뀌므로, 식단 카드를 한 번 갱신해 보낸다.
diet-log의 card.py가 운동 소모를 자동으로 읽어 "운동한 만큼 권장 섭취 바가 커지는" 구조로 그린다.

★전송 경로 분기: card.py 기본 출력은 `/tmp/diet_card.png`인데, /tmp는 PROJECT_ROOT 밖이라 라이브 턴의 `send_file::` 마커로는 자동전송이 막힌다(RULES Files 참고). 그래서 카드 출력을 반드시 `files/outbox/` 안으로 지정한다.

card.py 실행 python은 matplotlib이 있는 인터프리터로 해소한다(diet-log SKILL의 "카드 렌더러: python/폰트 해소" 참고):

```bash
RENDER_PY="${RENDER_PYTHON:-}"
[ -z "$RENDER_PY" ] && "$PROJECT_ROOT/bridge/venv/bin/python" -c 'import matplotlib' 2>/dev/null && RENDER_PY="$PROJECT_ROOT/bridge/venv/bin/python"
[ -z "$RENDER_PY" ] && command -v python3 >/dev/null && python3 -c 'import matplotlib' 2>/dev/null && RENDER_PY="$(command -v python3)"
[ -z "$RENDER_PY" ] && RENDER_PY="python3"

# 라이브 턴: outbox로 출력 후 send_file:: <절대경로>
OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{"output":"files/outbox/diet_card.png"}')

# 크론/루틴(봇 직발송): /tmp도 가능
OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{}')
routines/push.sh --photo "$OUT"
```

- 카드는 date 생략 시 오늘(KST) 기준으로 lifekit.db에서 자동 집계.
- card.py가 matplotlib을 못 찾으면 종료코드 3으로 우아하게 스킵 — 이때는 카드 없이 텍스트로만 보고한다.
- 카드 전송 후 텍스트는 카드와 수치를 겹치게 쓰지 말 것. 카드에 없는 코멘트(넛지·격려)만 짧게.

## 조회·삭제

```bash
$PROJECT_ROOT/database/lifekit.sh workout-find "$(date +%F)"   # 그날 운동 목록 (TSV: id category subtype minutes kcal)
$PROJECT_ROOT/database/lifekit.sh workout-del <id>             # 잘못 기록 시 삭제
```

## 참고

- 식단 기록은 diet-log 스킬, 운동은 이 스킬. 둘 다 lifekit.sh / lifekit.db를 공유한다.
- 볼륨(세트·중량) 트래킹은 현재 스키마에 없다(보류). 필요하면 note에 자유 텍스트로.

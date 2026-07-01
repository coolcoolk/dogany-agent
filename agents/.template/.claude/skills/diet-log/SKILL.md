---
name: diet-log
description: >
  식단 기록 AND 식단/칼로리 현황 조회를 모두 담당한다. 두 상황 다 이 스킬로 처리한다.
  (1) 기록: __USER_LABEL__이 음식을 먹었다고 말하거나 식단 기록을 요청할 때.
  "아침 먹었어", "점심 삼겹살 먹었어", "간식으로 견과 먹었어", 음식 이름 + 양 언급,
  바코드 이미지나 번호 전송, 영양성분 라벨 사진 전송 등 모든 식단 입력 상황.
  식품 영양성분 DB를 먼저 조회하고, 없으면 LLM 추정으로 로컬 lifekit.db 식단(meals) 테이블에 기록한다.
  (2) 현황 조회: "오늘 칼로리 현황", "식단 현황", "오늘 뭐 먹었지", "얼마나 먹었어",
  "오늘/이번주 목표 칼로리 알려줘", "칼로리 얼마 남았어", "섭취 얼마야" 등 그날 섭취/목표/밸런스를
  묻는 모든 발화. 이때도 생 SQL을 짜지 말고 이 스킬의 lifekit.sh agg-day + card.py 로 답한다.
  기록이든 조회든 최종 산출물은 항상 상태 카드(files/outbox/ 로 렌더 + send_file:: 전송)다.
  ★기록이 성공하면 PostToolUse 훅(card-followup)이 "다음 행동은 무조건 카드 렌더+전송"이라는
  강제 지시를 주입한다. 그 지시가 오면 다른 답변보다 먼저 카드를 만들어 보낸다(뒤 턴으로 미루지 않는다).
---

# diet-log 스킬

## 개요

__USER_LABEL__이 음식/식단 정보를 주면:
1. 로컬 캐시 → 공공 식품영양성분 DB 조회 (정확한 수치 우선)
2. DB 미등록이면 (브랜드면 웹검색 후) LLM 추정
3. 로컬 lifekit.db meals 테이블에 기록 (`lifekit.sh meal-add`)

모든 DB 접근은 `lifekit.sh`(= lifekit.py 코어)를 통한다 — 생 SQL 금지.

## 경로 규약

- 헬퍼: `$PROJECT_ROOT/database/lifekit.sh` (레포 루트 기준. PROJECT_ROOT 미설정 시 스킬 위치에서 상위로 올라가 database/를 찾는다).
- 이 스킬 파일들: `$PROJECT_ROOT/.claude/skills/diet-log/` (lookup.py, card.py, fonts/).
- 아래 예시의 상대경로는 워크스페이스 루트(PROJECT_ROOT)를 cwd로 가정한다. 스킬 디렉토리로 cd하지 말 것.

## API 키 / 엔드포인트 (선택)

식품 공공 DB 조회 키는 소스에 평문으로 두지 않는다. `lookup.py`가 프로젝트 `.env`
(우선순위: `DIET_ENV_FILE` 환경변수 → `$PROJECT_ROOT/.env` → `.telegram_bot/.env` → `runtime/.env`)
또는 프로세스 환경에서 읽는다.

- `FOODSAFETY_KEY` — 식품안전나라 C005(바코드 조회)
- `DATAGOV_KEY` — 공공데이터포털 영양성분 DB
- 키가 없으면 name/barcode 공공DB 조회는 `{"found": false}`로 조용히 내려가고, 로컬 캐시와 LLM 추정으로 계속 동작한다(크래시 없음).

## 기록 대상: lifekit.db (로컬 SQLite, SoT)

- 식단은 로컬 lifekit.db의 meals 테이블에 기록한다. 헬퍼는 `$PROJECT_ROOT/database/lifekit.sh`.
- area_id는 lifekit.sh가 "식습관" 영역으로 자동 연결한다 (직접 지정 불필요).
- 칼로리는 DB가 generated로 자동계산: 단백질×4 + 지방×9 + 순탄수(탄수−식이섬유)×4 + 알코올×7. 매크로만 넘기면 된다(kcal 입력 금지).
- 컬럼: date, meal(아침/점심/저녁/간식/운동), name, grams, carb, protein, fat, fiber, sugar, alt_sugar, alcohol.
- `alcohol` = 순수 알코올 그램(선택, 7kcal/g). 술류는 이걸 채워야 kcal이 정확하다.

## 절차

### A. 음식명으로 입력받은 경우

```bash
python3 .claude/skills/diet-log/lookup.py name "음식명" <grams>
```

lookup.py는 name 조회 시 로컬 캐시를 먼저 본다. 캐시 hit이면 `source:"cache"`로 즉시 반환(토큰 0).

조회는 항상 캐시 → 공공DB 순으로 lookup.py가 자동 처리한다. 둘 다 miss면 그 음식에 **브랜드/프랜차이즈명이 있는지**로 분기한다:

- 브랜드명 있음 (제조사/매장명이 붙은 음식) → **웹검색 의무**. 공식값·신뢰 영양 소스를 찾아 정확히 잡는다. 추정으로 건너뛰지 말 것. 둘 이상 소스가 일치하면 신뢰값. 찾으면 반드시 `cache-add`로 적재(`estimated:false`)해 다음부터 토큰 0으로 조회되게 한다. 웹에서도 못 찾을 때만 LLM 추정으로 내려간다(`estimated:true`).
- 브랜드명 없음 (백미밥·삼겹살·사과 같은 일반 음식) → 웹검색 생략하고 바로 LLM 추정. 신뢰할 만하면 `estimated:true`로 캐시에 넣어둔다.

우선순위: 1) 로컬 캐시 → 2) 공공DB → 3) 웹/브랜드 공식값(브랜드면 필수, `cache-add estimated:false`) → 4) LLM 추정.

### B. 바코드로 입력받은 경우

```bash
python3 .claude/skills/diet-log/lookup.py barcode "바코드번호" <grams>
```

### C. 영양성분 라벨 사진인 경우

이미지에서 직접 수치를 읽어 lifekit.db에 기록한다(meal-add). 더불어 **읽은 라벨값은 반드시 `cache-add`로 캐시에 적재한다**. 라벨은 __USER_LABEL__이 직접 올린 공식 수치라 신뢰도가 가장 높으니 `estimated:false`로 넣고, 같은 제품을 다음에 또 먹으면 토큰 0으로 조회된다. `key_name`은 제품명, `grams`는 라벨 기준 제공량으로 넣는다.

### D. 캐시 적재 (cache-add)

```bash
python3 .claude/skills/diet-log/lookup.py cache-add '<json>'
```

JSON 구조 (값은 항상 grams 기준 1회 제공량 절대량):

```json
{
  "key_name": "제품명 (표시옵션)",
  "name": "표시용 원래 이름",
  "grams": 347,
  "kcal": 553,
  "carbs": 40, "protein": 18.7, "fat": 18.7,
  "fiber": 5, "sugar": 5, "sodium_mg": 430,
  "source": "출처",
  "estimated": true,
  "updated": "2026-06-25"
}
```

- `key_name`: 정규화 키의 원본 이름. lookup.py가 소문자화·괄호/특수문자 제거·공백 정리로 정규화해 저장한다(조회와 동일 함수).
- `grams`: 위 수치가 기준하는 1회 제공량(g). 조회 시 요청 grams로 비례 스케일.
- `updated`: 오늘 날짜를 호출자가 직접 넣는다(스크립트 자동 생성 안 함).
- 같은 정규화 키면 upsert(덮어쓰기).
- 캐시 파일: `$PROJECT_ROOT/database/food_cache.json` (개인 데이터라 리포에 커밋 금지).

### 공통 흐름

1. lookup.py 실행 → JSON 결과 파싱
2. `found: true`이면 DB 값 사용, `found: false`이면 (브랜드 웹검색 후) LLM 추정
3. 후보가 여럿이면 식품명·제조사를 보고 가장 근접한 것 선택 (판단 어려우면 __USER_LABEL__께 확인)
4. 양(g)이 없으면 __USER_LABEL__께 확인 후 기록
5. 식사 구분(아침/점심/저녁/간식)이 없으면 확인 후 기록
6. lifekit.sh meal-add로 기록

```bash
$PROJECT_ROOT/database/lifekit.sh meal-add \
  "<YYYY-MM-DD>" "<아침|점심|저녁|간식|운동>" "<음식명 (양g)>" \
  <carb> <protein> <fat> <fiber> <sugar> <alt_sugar> <grams> [alcohol]
```

- 인자 순서: date meal name carb protein fat fiber sugar alt_sugar grams [alcohol]
- 매크로(g)만 넘긴다 — kcal은 절대 넘기지 않는다(DB 자동계산). 모르는 매크로는 0, grams 모르면 끝 인자 생략(NULL).
- `alcohol`(선택, 마지막): 순수 알코올 그램. 생략하면 0. 술은 반드시 채운다(7kcal/g, 어느 매크로에도 안 잡혀서 빼면 과소집계).
  - 순수 알코올 그램 = 마신 양(ml) × ABV(도수 소수) × 0.789(에탄올 밀도).
  - 탄수(carb)는 실제 탄수 그램 그대로. 알코올을 탄수에 접어 넣지 말 것(이중계산).
- 출력: `id<TAB>name<TAB>kcal` (kcal은 DB 자동계산값).
- 음식명에 양을 괄호로 같이 넣는 관례 유지: "데일리견과 핑크 (20g)".

### 기록 수정 (meal-upd)

이미 들어간 식단 한 건은 삭제 후 재등록하지 말고 `meal-upd`로 부분 수정한다.

```bash
$PROJECT_ROOT/database/lifekit.sh meal-upd <id> field=value [field=value ...]
```

- field: `date meal name carb protein fat fiber sugar alt_sugar grams alcohol` (지정 필드만 갱신).
- `kcal`은 generated라 수정 대상 아님 — 매크로를 고치면 자동 재계산.
- 대상 id는 `meal-find <date>`(TSV) 또는 `meal-day <date>`로 찾는다.

## AMT_NUM 필드 매핑 (100g 기준)

| 필드 | 영양성분 | 단위 |
|------|---------|------|
| AMT_NUM1 | 에너지 | kcal |
| AMT_NUM3 | 단백질 | g |
| AMT_NUM4 | 지방 | g |
| AMT_NUM6 | 탄수화물 | g |
| AMT_NUM7 | 식이섬유 | g |
| AMT_NUM8 | 당류 | g |
| AMT_NUM13 | 나트륨 | mg |
| AMT_NUM24 | 포화지방 | g |
| AMT_NUM25 | 트랜스지방 | g |

## 보고 형식 (__USER_LABEL__께)

기록 후 간결하게:
```
데일리견과 핑크 20g — 간식 기록 완료
에너지 122kcal / 단백질 3.6g / 지방 9.2g / 탄수화물 6.3g
(DB 조회값)
```

DB 미등록 시: `(LLM 추정값)` 표기.

---

## 식단 현황 카드 생성

카드 생성 트리거는 두 가지:
1. 자동(강제) — 기록 완료 후 매번. `meal-add` 성공 직후 PostToolUse 훅(routines/card-followup.py)이
   "다음 행동은 무조건 카드 렌더+send_file:: 전송, 다른 답변보다 먼저, 뒤 턴으로 미루지 말 것"이라는
   additionalContext를 주입한다. 이 지시가 오면 반드시 이번 턴 안에서 카드를 만들어 보낸다.
   훅은 생 Bash로 meal-add를 직접 쳐도(스킬을 안 거쳐도) 발동한다 = 카드 누락이 구조적으로 막힌다.
2. 명시 요청 — "오늘 식단 현황", "칼로리 현황", "식단 얼마나 먹었어", "오늘 뭐 먹었지",
   "목표 칼로리 알려줘" 등 조회 발화. 이때도 이 스킬로 처리하고 카드로 답한다.

### 카드 스크립트

```
.claude/skills/diet-log/card.py
```

### 사용법

```bash
python3 .claude/skills/diet-log/card.py '<json>'
```

### JSON 입력 구조

데이터 소스가 lifekit.db라 보통은 비우면 된다. 카드는 입력에 `meals`/`intake_kcal`이 없으면 해당 날짜(없으면 오늘 KST)의 식단·운동을 lifekit.db에서 직접 읽어 채운다. 아래 키들은 전부 선택이며, 넘기면 lifekit.db 자동집계를 덮어쓴다.

```json
{
  "date": "2026-06-25",
  "output": "files/outbox/diet_card.png"
}
```

- `date`: lifekit.db 조회용은 "YYYY-MM-DD"(생략하면 오늘 KST). 카드 상단엔 "YYYY.MM.DD 요일"로 자동 변환 표시.
- `meals[].protein/carbs/fat`: 선택. 있으면 식사 행 오른쪽에 "단 N  탄 N  지 N g"로 표시.
- `meals[].sugar` / `carbs.sugar`: 선택. 당(g). lifekit.db 자동집계 시 meals.sugar에서 채워진다.
- `burn_kcal`: 생략하면 소모 라벨·운동 세그먼트 숨김. 운동이 있으면 섭취 바가 [기초+활동−적자] + [운동 추가] 로 나뉘어, 운동한 만큼 권장 섭취 바가 커진 게 한눈에 보인다.
- `output`: 생략 시 `/tmp/diet_card.png`.

### lifekit.db에서 오늘 식단/운동 집계

```bash
python3 .claude/skills/diet-log/card.py '{}'                    # 오늘 카드(자동 집계)
python3 .claude/skills/diet-log/card.py '{"date":"2026-06-25"}'  # 특정 날짜
```

수치를 손으로 확인하려면:

```bash
$PROJECT_ROOT/database/lifekit.sh agg-day "$(date +%F)"       # 섭취/매크로/소모/밸런스
$PROJECT_ROOT/database/lifekit.sh meal-day "$(date +%F)"      # 식사 목록
$PROJECT_ROOT/database/lifekit.sh workout-find "$(date +%F)"  # 운동 목록
```

**카드 전송 후 텍스트는 카드와 수치를 겹치게 쓰지 말 것.** 수치는 카드에 다 있으니, 텍스트로는 카드에 없는 코멘트(넛지·조언)만 짧게. 메타설명("코멘트만 짧게:" 같은 상투어구) 붙이지 말고 바로 본론부터.

### 카드 렌더러: python/폰트 해소 (경로 독립)

card.py는 matplotlib이 필요하다. 매트플롯립이 있는 인터프리터로 실행한다:

```bash
RENDER_PY="${RENDER_PYTHON:-}"
[ -z "$RENDER_PY" ] && RENDER_PY="$($PROJECT_ROOT/bridge/venv/bin/python -c 'import matplotlib' 2>/dev/null && echo $PROJECT_ROOT/bridge/venv/bin/python)"
[ -z "$RENDER_PY" ] && command -v python3 >/dev/null && python3 -c 'import matplotlib' 2>/dev/null && RENDER_PY="$(command -v python3)"
[ -z "$RENDER_PY" ] && RENDER_PY="python3"   # 최후 폴백(없으면 card.py가 코드3으로 우아하게 스킵)

OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{"output":"files/outbox/diet_card.png"}')
```

- python 해소 우선순위: `RENDER_PYTHON`(env) → 프로젝트 bridge venv → PATH의 python3(단 matplotlib 있을 때) → `python3`.
- card.py는 matplotlib을 못 찾으면 **종료코드 3 + stderr 메시지**로 우아하게 스킵한다. 이때는 카드 없이 텍스트로만 보고한다("카드 렌더 스킵 — matplotlib 미설치").
- 폰트: 스킬에 번들된 `fonts/ASDGN_*.ttf`를 우선 사용(경로 독립). 없으면 시스템 TTC(`DIET_CARD_TTC` env 또는 기본 후보)에서 추출 시도, 그것도 없으면 matplotlib 기본 폰트로 폴백(CJK 깨질 수 있음, 경고만).

### 카드 전송

★전송 경로 분기: card.py 기본 출력은 `/tmp/diet_card.png`인데, /tmp는 PROJECT_ROOT 밖이라 라이브 턴의 `send_file::` 마커로는 자동전송이 막힌다(RULES Files 참고). 라이브 턴이면 카드를 `files/outbox/` 안으로 출력하고 `send_file::`로 보낸다. 크론/봇 직발송이면 push.sh가 /tmp도 보낸다.

```bash
# 라이브 턴(__USER_LABEL__과 대화 중 직접 응답): outbox로 출력 후 send_file:: <절대경로>
OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{"output":"files/outbox/diet_card.png"}')

# 크론/루틴(봇 직발송):
OUT=$("$RENDER_PY" .claude/skills/diet-log/card.py '{}')
routines/push.sh --photo "$OUT"
```

### 목표 수치 (card.py가 신체 스탯에서 자동 계산 — 입력 불필요)

목표 칼로리는 lifekit.db config 테이블의 신체 스탯에서 **자동 계산**한다. __USER_LABEL__께 "총 칼로리 얼마" 물을 필요 없음.

**묻기 전에 읽는다:** 몸무게·목표(goal_mode)·매크로 목표를 되묻지 말고 먼저
`$PROJECT_ROOT/database/lifekit.sh body-state` 로 현재 goal_mode·weight·eff_goal·매크로 목표를 읽는다.
lifekit 값이 canonical. 찾고도 없을 때만 __USER_LABEL__께 확인.

**신체 스탯 단일 원천: lifekit config 테이블** (SqliteConfigStore).
필드: weight_kg, height_cm, skeletal_muscle_kg, fat_mass_kg, lean_mass_kg, avg_steps,
deficit_kcal, other_neat_kcal, protein_g, fat_ratio, goal_mode, updated.

| 항목 | 산식 |
|---|---|
| BMR | 370 + 21.6 × 제지방량(lean_mass_kg)  ← Katch-McArdle |
| NEAT(활동소모) | 걷기(0.5 × 체중 × 거리km, 거리=걸음수×보폭) + other_neat_kcal |
| 섭취칼로리(eff_goal) | BMR + NEAT − 적자(deficit_kcal) + 그날 운동소모 |
| 단백질 | protein_g 고정 |
| 지방 | eff_goal × fat_ratio ÷ 9 |
| 탄수화물 | (eff_goal − 단백질×4 − 지방kcal) ÷ 4 |

- lean_mass_kg 없으면 weight_kg − fat_mass_kg. 보폭 = 키 × 0.415.
- 운동소모 = 운동 DB 기록(`소모 칼로리`)을 burn_kcal.current로 넘김. NEAT(걷기·일상)는 스탯에서 자동.
- **스탯 갱신은 CLI로**: config 테이블 직접 SQL 금지.
  - 설정/목표 필드: `$PROJECT_ROOT/database/lifekit.sh config-set weight_kg=72 goal_mode=recomp ...` ('updated' 자동).
  - 측정값 시계열도 함께: `$PROJECT_ROOT/database/lifekit.sh log-metric <YYYY-MM-DD> weight_kg 72`.
  - (또는 모듈 임포트로 `lifekit.set_stats(**fields)` / `lifekit.log_metric(...)` 직접 호출 — card.py와 동일 코어 경로.)

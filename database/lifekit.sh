#!/bin/bash
# lifekit.sh — lifekit.py 래퍼
# 진짜 코어는 같은 디렉토리의 lifekit.py다. 이 스크립트는 호환을 위한 얇은 래퍼로,
# lifekit.py의 CLI를 python으로 호출만 한다(인터페이스 100% 보존).
# 서브커맨드·인자·표준출력 포맷은 lifekit.py가 옛 lifekit.sh와 한 글자도 다르지 않게 재현한다.
#
# DB: <database>/lifekit.db (SQLite, WAL)
#   meals    — 식단. area_id → "식습관". kcal은 generated(단백×4+지방×9+(탄-섬유)×4+알코올×7) → INSERT 금지.
#   workouts — 운동. area_id → "신체건강". kcal은 소모칼로리(직접 입력). avg_hr=평균 심박수(bpm, 선택).
#   v_daily_energy — 날짜별 섭취/소모 교차집계 뷰.
#
# 사용법 (lifekit.py 위임):
#   lifekit.sh meal-add <date> <meal> <name> [carb protein fat fiber sugar alt_sugar grams alcohol]
#         alcohol=순수 알코올 그램(선택, 7kcal/g). 술류는 volume_ml×ABV×0.789로 계산해 넘긴다.
#         출력: id<TAB>name<TAB>kcal
#   lifekit.sh meal-find <date>      그날 식단 목록 (TSV: id name meal kcal)
#   lifekit.sh meal-day  <date>      그날 식단 목록 (사람용)
#   lifekit.sh meal-del  <id>        식단 한 건 삭제
#   lifekit.sh meal-upd  <id> field=value [field=value ...]   식단 한 건 부분 수정
#         field: date meal name carb protein fat fiber sugar alt_sugar grams alcohol
#         지정한 필드만 갱신(나머지 보존). kcal은 자동 재계산. 출력: id<TAB>name<TAB>kcal
#   lifekit.sh workout-add <date> <category> <subtype> [minutes kcal note avg_hr]
#         category=대분류, subtype=세부 (옛 <type>/<name> 위치 그대로 — 호환 보존).
#         workout_types 사전에서 type_id를 조회해 type_id만 저장(type/name 캐시 컬럼 제거).
#         사전에 없는 분류면 workout_types에 자동 등록 후 그 id로 기록(경고만, 실패 안 함).
#         라벨은 항상 type_id→workout_types 조인으로 복원.
#         출력: id<TAB>type<TAB>name<TAB>kcal<TAB>avg_hr (avg_hr 미지정이면 끝 칸 빔)
#         avg_hr = 평균 심박수(bpm, REAL, 선택). 미지정이면 NULL로 저장.
#   lifekit.sh workout-find <date>   그날 운동 목록 (TSV: id type name minutes kcal)
#   lifekit.sh workout-del  <id>     운동 한 건 삭제
#   lifekit.sh workout-classify <workout_id> <category> <subtype>   운동 한 건에 분류 추가(다대다)
#   lifekit.sh agg-day  <date>           그날 섭취/매크로/소모/밸런스 (KEY=VALUE)
#   lifekit.sh agg-week <월요일date>     그 주(월~일) 집계 + 직전주 대비
#   lifekit.sh body-state                현재 신체/목표 상태 (KEY=VALUE)
#   lifekit.sh config-set key=value ...  신체 스탯/설정 config 테이블 upsert (updated 자동)
#   lifekit.sh log-metric <date> <metric> <value> [note]   측정값 시계열 1건 upsert
#   lifekit.sh targets --burn N          eff_goal bmr neat deficit protein_goal (공백구분 한 줄)
#   lifekit.sh dump                      sqlite3 .dump 상당 (백업용)
#
# 종료코드: lifekit.py 종료코드를 그대로 전달.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# lifekit.py 는 표준 stdlib만 쓰므로 어떤 python3 로도 동작한다.
# 우선순위: 1) LIFEKIT_PYTHON 환경변수, 2) PATH 의 python3, 3) /usr/bin/python3.
PY="${LIFEKIT_PYTHON:-}"
if [[ -z "$PY" ]]; then
  PY="$(command -v python3 || true)"
fi
[[ -x "$PY" ]] || PY="/usr/bin/python3"

exec "$PY" "$SCRIPT_DIR/lifekit.py" "$@"

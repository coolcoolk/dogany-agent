#!/bin/bash
# __AGENT_LABEL__ 야간 공고화 (consolidate) — 매일 04:30 (Asia/Seoul)
# 트랜스크립트 증분 → Sonnet 압축 → 룰/2단계 필터 → 중복제거 → inbox.md 단순 적재 → 무음 리포트.
# 워터마크(state.db) 이후 대화만 보므로 매일 새 대화만 처리. __USER_LABEL__ 자는 시간에 조용히 돈다.
# 주제 라우팅 없음 — 야간은 무조건 inbox.md. 주간 classify-inbox 가 주제파일로 분배.
# 경로는 스크립트 자기위치 기준(동적) — 워크스페이스 이동에도 안 깨진다(DGN-055).
cd "$(dirname "$0")/../memory" || exit 1
# dedup/이웃탐색은 신선한 notes 인덱스가 전제 — 야간엔 검색 훅이 안 도니 명시 재인덱스.
/usr/bin/python3 memory.py index
/usr/bin/python3 memory.py consolidate

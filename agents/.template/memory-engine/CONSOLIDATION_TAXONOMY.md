# 공고화 분류 기준 (Consolidation Taxonomy)

## 2.7.0 route-before-memory gate

After DROP versus DURABLE_CANDIDATE, classify durable matter before any
memory write. Skill or procedure improvements, workflows, rules and normative
constraints, kit resources or kit constraints, and owner-profile proposals
are always `SKIP_IN_FLOW`. They MUST NOT become a KEEP line, proposal, or
candidate tag. The engine first persists a `pending-handoff-v1` envelope; only
then may the transcript watermark advance. FDGN-622 owns acknowledgement at a
matching recurrence. Nightly output reports only the attention count and never
the envelope text.

Only durable general context without an available authoritative home is
`memory_residue`. Concrete records and tasks use `kit_record` and `kit_task`
only when an installed contract supplies read-side identity checks and a
write receipt; ambiguous, unavailable, or malformed ownership retains general
context as residue and never claims delivery.

> 2단계 필터가 이 문서를 읽어 각 압축 후보를 KEEP / DROP 판정한다.
> 여러 도메인의 에이전트(개발/생활 등) 공용. 공통 기준 + 도메인 특화.

## 단 하나의 판별 질문
이 항목이 한 달~일 년 뒤에도 참이고, 그때 검색해서 꺼내 쓸 가치가 있는가?
→ YES만 KEEP. 애매하면 DROP (놓친 사실은 다음 밤에 또 만난다).

## KEEP — 남긴다

공통(모든 에이전트):
- __USER_LABEL__ 프로필·선호·습관·배경 (호칭, 시간대, 일, 커뮤니케이션 방식)
- 확정된 결정·합의 (설계·규칙·정책 — 채택된 것만)
- 영구적 변경 (경로, 설정, 구조)
- 재사용 식별자·수치 (DB ID, 토큰 위치, 임계값)
- 교훈·함정 (다시 안 밟으려 기억할 것)
- 정정 (기존 사실을 뒤집는 것 → 맨 앞에 "정정:")

개발 에이전트 특화(예시):
- 아키텍처 결정과 그 이유
- 버그: 증상 → 원인 → 해결책 (한 줄로)
- 인프라/서비스/배선 변경
- 운영 규칙·가드레일

생활 에이전트 특화(예시):
- 건강 지표·신체 스탯·목표 (칼로리 모델 등)
- 생활 루틴·습관 (식단/운동 패턴, 일정 규칙)
- 관계·사람 맥락 (가족, 지인)
- 취향·선호 (음식, 운동)
- 재정 패턴 (가계부 규칙)

## DROP — 버린다 (강한 규칙)

- 발화 자체: 질문(~할까요?), 사과, 인사, 맞장구, 감탄
- 진행 보고: 지금 ~하는 중, ~해보겠습니다, ✓완료 / ⏳다음
- 일회성: 한 번 쓰고 마는 지시·셸 명령, 임시 상태(~기다리는 중)
- 형식 구조물: 코드블록·명령줄, 마크다운 헤더(#)·구분선(---)·불릿·라벨(요구사항:, 참고:)
- 추측·미확정: ~일 수도, 아직 안 정한 선택지 나열
- 중복: 이미 아는 사실의 재표현 (임베딩 중복제거가 1차로 처리)

## 경계 케이스 (헷갈릴 때)
- 결정 과정의 선택지 1/2/3 나열 → 최종 채택된 것만 KEEP, 선택지 자체는 DROP
- 어시스턴트가 길게 정리한 결론 → 그건 어시스턴트의 판단이다. 판단으로 남길 가치가 있으면 KEEP 하되
  **결정·확정으로 재진술하지 않는다**. "~하기로 했다 / ~로 결정 / ~는 금지" 같은 주체 없는 결정형은
  __USER_LABEL__가 그 내용을 직접 말했거나 명시적으로 승인한 경우에만 쓴다. 주체(누가 말했나)는 엔진이
  항목 꼬리표 `by=owner-quoted | agent-judged | unverified` 로 붙인다 -- owner-quoted 는 __USER_LABEL__
  원문 조각이 실제 __USER_LABEL__ 줄에서 확인된 것만이다 (DGN-1384)
- __USER_LABEL__ 발화는 KEEP 쪽으로, 어시스턴트 발화는 DROP 쪽으로 기운다 (사실은 __USER_LABEL__이 주고, 에이전트는 떠드는 경우가 많다)

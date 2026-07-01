---
name: dogany-proactive-push
description: __USER_LABEL__ 텔레그램으로 능동적으로 메시지를 보낸다(아무도 안 불러도 먼저 거는 outbound 푸시). 정기 루틴(브리핑/회고/알림)이나 작업 완료 통보에 사용. 내용을 그대로 보내거나(--text), 지정 모델로 생성해서 보낸다(--prompt --model). 크론/launchd에서 호출하는 핵심 부품.
---

# dogany-proactive-push — 능동 텔레그램 푸시

__USER_LABEL__ 텔레그램으로 봇이 **먼저** 메시지를 보낸다(봇 토큰·chat_id는 `runtime/.env`에서 읽음). 핵심 스크립트는 `routines/push.sh`.

## 언제 쓰나
- 정기 루틴(일일 회고, 식단 브리핑 등)을 크론/launchd에서 발사할 때
- 긴 작업 완료/실패를 __USER_LABEL__께 통보할 때
- 그 외 __USER_LABEL__이 요청 안 해도 알릴 가치가 있는 것 (단, 빈도 절제 — 알림 피로 주의)

## 사용법

**그대로 보내기 (claude 안 거침, 즉시):**
```bash
routines/push.sh --text "보낼 문구"
```

**모델로 생성해서 보내기 (난이도별 모델 라우팅):**
```bash
routines/push.sh --model haiku  --prompt "오늘 일일 회고 질문 하나 던져줘"
routines/push.sh --model sonnet --prompt "아래 데이터를 마아서 주간 요약 만들어줘: ..."
```

## 모델 선택 가이드 (비용 레버)
- `haiku` — 단순 정기 루틴, 고정 톤 (기본값)
- `sonnet` — 데이터 마는 중간 난이도(요약·집계)
- `opus` — 어려운 추론이 필요한 통보 (드물게)

## 주의
- 텔레그램 4096자 제한 → 스크립트가 4000자로 자동 컷.
- plain text로 전송(마크다운 파싱 안 함) — 깨짐 방지.
- 봇 토큰·chat_id는 `runtime/.env`에서 읽음.
- 루틴 채널 분리(설계 A/B) 확정 시 `--thread <id>` 인자 추가 예정.
- 종료코드: 0 성공 / 1 설정오류 / 2 텔레그램 전송실패.

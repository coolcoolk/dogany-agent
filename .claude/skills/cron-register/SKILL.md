---
name: cron-register
description: 사용자이 반복작업(크론/정기루틴)을 맡기면 에이전트가 launchd로 끝까지 직접 등록한다. "매일 몇시에 X 해줘", "이거 정기적으로", "크론 걸어줘" 류 요청에 발동. plist 작성→검증→테스트 발송→launchctl load까지 에이전트가 처리하고, 코드만 던지고 사용자께 미루지 않는다. 게이트웨이/메인 봇 재시작만 예외(사용자께 요청).
---

# cron-register — 반복작업 launchd 등록

사용자이 반복작업을 맡기면 에이전트가 plist 작성부터 launchctl load까지 끝까지 직접 한다. (skill-creator 컨벤션을 따른 산출물.)

## 발동 신호
- "매일/매주 몇시에 X 해줘", "정기적으로", "크론 걸어줘", "루틴으로 만들어"

## 핵심 규칙
- 코드만 던지고 사용자께 미루지 않는다 — 등록까지 에이전트가 한다.
- 예외: 게이트웨이/메인 봇 서비스 재시작·중지는 혼자 하지 말 것(사용자께 요청). 새 루틴 잡 load는 에이전트가 직접 OK.
- 모델은 정기·단순이면 haiku (모델 라우팅).
- 메시지 생성 프롬프트엔 톤 규칙 박을 것: 사용자 호칭, 존댓말(반말 금지), ** 안 씀, 기호 최소.

## 절차
1. 현재 시각·타임존 확인 (`date "+%Z %z %H:%M"`, `readlink /etc/localtime`). 맥은 Asia/Seoul KST = launchd 로컬타임 기준.
2. plist 작성 — 템플릿 `template.plist` 복사해서 채운다.
   - Label: `com.telegram-skill-bot.telegram-agent.<이름>` (kebab, 시각 접미사 예: retro-2100)
   - StartCalendarInterval Hour/Minute (필요시 Weekday)
   - RunAtLoad=false (load 즉시 발송 방지 — 다음 예정시각에 첫 발화)
   - ProgramArguments: push.sh --model haiku --prompt "<톤규칙 박은 프롬프트>"
   - 로그: `runtime/logs/<이름>.stdout.log` / `.stderr.log`
   - 템플릿 치환 시 `__ROOT__`(레포 루트), `__HOME__`($HOME), `__PATH__`(이식 가능한 PATH)도 함께 채운다 — `__NAME__`/`__PROMPT__`/`__HOUR__`/`__MINUTE__`와 동일하게.
3. 문법 검증: `plutil -lint <plist>` → OK 확인.
4. 실제 테스트 발송: push.sh를 그 프롬프트로 한 번 직접 실행해서 톤·내용·전송 확인 (수동 흉내 X).
5. 통과하면 등록:
   ```bash
   cp routines/<plist> ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/<plist>
   launchctl list | grep <이름>
   ```
6. `routines.md`에 등록 사실 원자적 기록 (날짜·Label·시각·모델·맥락).

## 갱신/삭제
- 수정: `launchctl unload <plist>` → 파일 수정 → 다시 load.
- 삭제: `launchctl unload <plist>` 후 `trash`로 plist 이동(rm 지양).

## 등록된 크론
- retro-2100 — 매일 21:00 KST 일일 회고 (haiku, 대화형). 2026-06-24 등록.
- morning-brief-0600 — 매일 06:00 KST 적응형 아침 브리핑 (haiku). routines/morning-brief.sh가 Notion REST로 어제 못 끝낸 일(누적)·오늘 할일·오늘 약속 읽어 브리핑. 2026-06-24 등록.
- weekly-review-sun2200 — 매주 일 22:00 KST 주간 회고 (haiku). routines/weekly-review.sh: 1부 주간 브리핑(이번주 집계) + 2부 성찰 회고(기분 좋/안좋았던 일·생각·만남 질문). 2026-06-24 등록.

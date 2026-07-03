# dogany-agent (한국어)

[English README ->](README.md)

텔레그램으로 연락하는 나만의 Claude Code 에이전트 프레임워크.
기기가 켜져 있는 동안 늘 실행되는 개인 에이전트.
장기기억·정기 루틴·스킬 시스템을 갖추고 있습니다.

## 철학

<p align="center"><img src="docs/img/golden-circle-ko.png" width="440" alt="WHY는 내가, HOW와 WHAT은 에이전트가 제안하고 실행합니다"></p>

- 내 삶의 CEO처럼: Why는 당신이 정하고, How와 What은 에이전트가
  제안하고 실행합니다. 당신은 결과물을 고르면 됩니다.
- 제련되는 기억: 나눈 대화가 압축되고 벼려져 세컨드 브레인이
  되고, 오래 쓸수록 나를 깊이 아는 에이전트가 됩니다.
- 매일 나아지는 나: Why를 정하고 결과물을 고르는
  과정에서 취향이 드러나고, 성장이 회고와 숫자로 눈에 보이게
  쌓입니다.
- 데이터는 당신의 것: 민트된 인스턴스의 기억·기록은 전부 로컬에
  남고, 배포되는 레포 트리에는 개인 데이터가 없으며 인스턴스 데이터는
  레포에 들어가지 않습니다.

## 티어

코어는 하나, 티어는 세 가지입니다.
에이전트를 내 손으로 빚고(HAND), 일상을 맡기고(CRAFT), 내 코어를 다 담은 작품으로 완성합니다(MASTER).

<p align="center"><img src="docs/img/tiers-ko.png" width="480" alt="세 동심원 티어: 안에서 HAND, CRAFT, MASTER 순으로 확장됩니다"></p>

티어는 `.instance.conf`의 플래그(`DOGANY_TIER=lite|basic|pro`)이며,
별도 레포가 아닙니다. 이 단일 레포에 전부 공개되어 있고 DRM도 없습니다.
필드가 없으면 lite로 동작하고, 재민트·업데이트 시에도 유지됩니다.

- **HAND** (`lite`, 이번 배포) — 무료 베이스. 브릿지·장기기억·루틴·
  스킬을 갖춘 완전한 범용 에이전트입니다. 그 자체로 끝이어도 좋고,
  나만의 에이전트(QA 에이전트, 콘텐츠 에이전트, ...)를 만드는 베이스로
  써도 좋습니다. Lifekit 번들은 동면 상태로 동봉됩니다.
- **CRAFT** (`basic`, 곧 공개) — 역시 무료. 일상을 에이전트에 맡기는 층으로,
  HAND 위에 Lifekit 번들(sql 베이스 메모리, 정기 루틴, 도메인
  에이전트 오케스트레이션)이 켜집니다.
- **MASTER** (`pro`, 먼 미래에 공개) — 유일한 유료 층. GUI와 매니지드 호스팅까지
  얹는 단계로, 서버측 계정 연결로 활성화됩니다. 셀프호스트도
  가능합니다(일부 기능은 서버 필요).

<p align="center"><img src="docs/img/pricing-ko.png" width="560" alt="HAND·CRAFT·MASTER 티어 기능 비교표"></p>

업그레이드는 재설치가 아니라 상태 변경이라
기억과 정체성이 그대로 유지됩니다 — 마이그레이션이 없습니다. 설치
시점에 티어를 고르지 않는 것도 그래서입니다(모두 HAND로 시작).

## 메모리

매 턴, 관련 기억이 자동으로 컨텍스트에 주입됩니다 — 같은 얘기를 두 번
할 필요가 없습니다. 모든 기억은 로컬 Markdown이 정본이며, 벡터 인덱스
(FTS5 + 임베딩)는 선택 사항이고 언제든 재빌드 가능합니다.

두 가지 접근 방식:

- 핫 인젝트: `USER.md`와 `AGENT.md`는 매 턴 컨텍스트에 로드됩니다
  (`CLAUDE.md`의 `@` 임포트). 늘 작고 항상-관련성 있는 내용만 담습니다 —
  당신의 프로필과 에이전트 정체성은 콜드 상태가 되지 않습니다.
- 콜드 리콜: 나머지는 `memories/` 토픽 파일에 보관됩니다.
  `UserPromptSubmit` 훅이 하이브리드 검색(키워드 + 시맨틱)을 실행하고,
  모델이 메시지를 보기 전에 상위 매칭 결과를 주입합니다. 시맨틱 리콜은
  로컬에서 Ollama + bge-m3 모델이 실행 중일 때만 활성화됩니다(선택 사항).
  설치하지 않으면 엔진이 자동으로 키워드 전용(FTS) 검색으로 전환됩니다.

두 가지 스케줄 쓰기 패스로 기억을 최신 상태로 유지합니다:

- 야간 consolidate: 그날 대화를 `memories/inbox.md`로 제련합니다.
  잡음과 중복은 걷어내고, 1년 뒤에도 가치 있는 사실만 기록합니다.
  시크릿은 디스크에 쓰기 전에 자동 리댁팅됩니다.
- 주간 classify-inbox: `inbox.md` 항목을 `memories/` 토픽 파일로
  라우팅합니다(올바른 파일에 추가, inbox에서 제거). 엔진은 진짜 새로운
  클러스터에 `NEW:<label>`을 제안하거나, 통과된 잡음을 DROP할 수 있습니다.

<p align="center"><img src="docs/img/distillation-ko.png" width="520" alt="기억 제련: 원시 대화가 매일 밤 inbox.md로 정제되고, 매주 memories/ 토픽 파일로 라우팅됩니다"></p>

쓰기 경로는 의도적입니다: 사실은 `대화 → inbox.md → 토픽 파일` 순으로
흐르며, 에이전트가 토픽 파일에 직접 쓰지 않습니다. 이렇게 볼트를
깔끔하고 감사 가능하게 유지합니다.

<p align="center"><img src="docs/img/routing-ko.png" width="560" alt="기억 라우팅: 사용자 대화가 AGENT.md·USER.md·SKILL.md·memories/ 토픽 파일로 분류됩니다. 수동은 에이전트가, 자동은 엔진이 처리합니다"></p>

## 레포 구조

레포는 검증된 멀티 에이전트 트리를 반영합니다. 공유 코드는 루트에 올리고,
각 에이전트는 `agents/` 아래에 위치합니다.

- **`agents/main/`** — 기본 민트 대상으로 레포 콘텐츠가 아닙니다. 새 클론에는
  존재하지 않으며 gitignore 처리됩니다. `install.sh`가 `agents/.template`에서
  민팅하여 생성합니다. 민트된 인스턴스는 rules, `bridge/`, `memory-engine/`,
  `memories/`, `routines/`, `files/`, `worklog/`, `.telegram_bot/`, `.claude/`의
  실제 복사본(심링크 아님)을 갖습니다. 참조 구조는 `agents/.template/`에서
  확인하세요.
- **`agents/.template/`** — 민트 소스. 플레이스홀더(`__PROJECT_ROOT__`,
  `__AGENT_NAME__` 등)로 구성된 에이전트로, 프레임워크 스킬과 `RULES.md`가
  공유 루트에 심링크됩니다. `scripts/mint.sh`가 여기서 복사해(심링크 역참조)
  새 자립형 에이전트를 생성합니다.
- **`rules/`** — 공유되고 불변인 `RULES.md`와 `USER.example.md`. 템플릿이
  RULES.md를 심링크하고, 민트된 인스턴스는 실사본을 받습니다. 인스턴스의
  `USER.md` 스캐폴드는 `agents/.template/`에서 옵니다.
- **`skills/`** — 에이전트 간 공유 프레임워크 스킬(`dogany-cron-register`,
  `dogany-lifekit-setup`, `dogany-mailer`, `dogany-memory-search`,
  `dogany-proactive-push`, `dogany-reminder`, `dogany-skill-creator`,
  `dogany-user-onboarding`). 민트된 인스턴스는 `.claude/skills/` 아래에 실제
  복사본을 갖습니다. 도메인(lifekit) 스킬은 `.claude/skills-bundle/` 아래에
  DORMANT 상태로 동봉되고, `dogany-lifekit-setup` 스킬로만 활성화됩니다.
- **`database/`** — `lifekit.py`/`lifekit.sh`, 선택적 정형 데이터 레인(로컬
  SQLite "생활 OS": 식사, 운동, 사람, 일정). 코드만 포함 — `schema.sql`은
  구조이며 `*.db` 데이터는 미포함.
- **`service/`** — lifekit 코어 위의 안정적 SDK 파사드(`service.lifekit`).
  스킬은 raw 데이터 레이어 대신 이를 임포트합니다.
- **`scripts/`** — `agents/.template`과 공유 루트에서 독립 에이전트를 생성하는
  `mint.sh`.
- **`install.sh`** — 이중 언어(ko/en) 설치 마법사. 사전 요구사항 확인, 봇 토큰
  + 소유자 id 수집(born-locked), `scripts/mint.sh` 호출로 자립형 인스턴스 민팅,
  선택적 자동시작 서비스 설치를 수행합니다.

각 에이전트의 `bridge/`는 공식 `claude-agent-sdk`(in-tree 벤더링, `bridge/UPSTREAM.md`
참조)를 기반으로 하는 자립형 Telegram <-> Claude 브릿지입니다.

## 경로 독립성

고정된 부모 트리를 가정하지 않습니다.

- 민트된 인스턴스는 그 자체가 `PROJECT_ROOT`입니다. rules, 프레임워크 스킬,
  데이터베이스 스키마, 서비스 SDK의 실제 복사본(심링크 아님)을 갖습니다.
- 브릿지는 환경에서 `PROJECT_ROOT`를 읽습니다(`start.sh`가 launchd plist로
  설정). plist와 훅은 민트 시 `__PROJECT_ROOT__` 플레이스홀더를 치환합니다.
- `lifekit.sh`는 PATH의 모든 `python3`으로 실행됩니다(`LIFEKIT_PYTHON`으로
  재정의 가능). `lifekit.py`와 DB 경로는 스크립트 자체 디렉터리 기준으로
  해석됩니다.
- `service.lifekit` 파사드는 자기 위치에서 lifekit 코어를 해석합니다
  (`service/lifekit/__init__.py` -> `../../database/lifekit.py`).

## 바로 시작하기

1. 레포를 받아 설치 마법사를 실행합니다.

       git clone https://github.com/coolcoolk/dogany-agent
       cd dogany-agent
       bash install.sh

2. 마법사가 언어·타임존·봇 토큰(BotFather에서 발급)·소유자 확인을
   차례로 안내하고, 자립형 에이전트 하나를 민트합니다(기본값
   `./agents/main`, 레포 내 gitignore 처리). 자동시작 서비스 설치와
   이메일 연결(에이전트 전용 계정 권장, 건너뛰기 가능)은 이 단계의
   선택 항목입니다.

3. 텔레그램을 열고 봇에게 인사하세요. 첫 대화에서 에이전트가 자기
   이름·말투를 물으며 온보딩을 시작합니다.

미리 보기만 하려면:

    bash install.sh --dry-run --lang ko

직접 경로를 지정해 민트하려면:

    bash scripts/mint.sh --root /path/to/instance --name myagent

`dogany-*` 스킬은 프레임워크 스킬로 `./update.sh`로 갱신됩니다. 직접
수정한 스킬이 있으면 `update.sh`가 감지하여 `.claude/skills/<skill>.user-<date>/`에
백업한 뒤 교체합니다. 지속적으로 커스터마이즈하려면 `dogany-` 이름이 아닌
스킬을 새로 만들어 그쪽을 편집하세요.

권장: 개인 계정 대신 에이전트 전용 Gmail/Apple 계정을 만드세요 — 개인
데이터와 격리되고, 이메일·연동에서 에이전트만의 정체성을 갖습니다.
계정 연결(이메일 발송 등)은 설치 중 선택 사항이며 나중에 추가할 수 있습니다.

## 데이터와 프라이버시

- 봇은 소유자만 접근 가능합니다(설치 때 지정하거나 1회 클레임).
- 메모리(`memories/`), 기록 DB, 세션, 토큰은 전부 인스턴스 로컬이며
  git에 커밋되지 않습니다(`.gitignore`).
- 레포에는 코드와 빈 구조만 있습니다.

## Notes

- 코드는 English/ASCII로만 작성합니다. 마크다운 문서는 에이전트의
  작업 언어로 작성할 수 있습니다.
- 시크릿이나 개인 데이터는 커밋되지 않습니다(`.gitignore` 참조).
- 선택적 `lifekit` 레인은 민트 시 `database/schema.sql`로 초기화됩니다
  (빈 정형 레인, 사용자 데이터 없음).

## 라이선스

[LICENSE](LICENSE) 참조.

# Changelog

All notable user-facing changes to Dogany are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.31.9] - 2026-08-14

compat-lint C4 mirror allowlist -- kit_mirror 설치 언블록 (DGN-872, hotfix).
- scripts/pack/compat-lint.sh: C4 payload allowlist에 mirror) 케이스 추가.
  mirror/ SOURCE(*.py/*.sql 등) 허용, 런타임 잔여(*.db*/*.pyc/download.html) 차단
  -- kit_mirror 배달 스텝의 제외 세트와 정합. v1.31.8이 배달 스텝만 싣고 짝이 되는
  C4 케이스를 빠뜨려 mirror/*가 설치 직전 C4에서 abort하던 것을 해소.
- scripts/tests/test-compat-lint.sh: 회귀 테스트 T14/T15 (mirror source PASS /
  mirror .db 잔여 FAIL) + 픽스처 신설 -- lint<->delivery 드리프트 재발 방지.
- 내부 기전, 유저 가시 변화 없음.

## [1.31.8] - 2026-08-14

팩이 mirror/를 소유하는 기전 (DGN-855 Metal seam, root fix).
- scripts/pack/pack_install.sh: kit_mirror 배달 스텝 신설 -- 팩 payload/mirror/를
  인스턴스 mirror/로 복사. sdk_bridge 역드리프트 가드 + .pack-owned 마커 드롭 +
  payload 부재 시 fail-safe SKIP.
- update.sh: 3e-mirror re-vendor 게이트 -- .pack-owned 마커 있으면 프레임워크가
  mirror/를 재벤더하지 않음 (팩 소유권 인계).
- 그릴 블로커 해소: 마커파일 게이트(B1/B3), kit_mirror fail-safe(B2),
  download.html/db-journal 제외.
- 내부 기전, 유저 가시 변화 없음. 현재 라이브 무접촉 (마커 미존재 -> 3e 정상 수신).
  나가면 Skull order-2(mirror 콘텐츠 팩 이관) 언블록.

## [1.31.7] - 2026-08-14

canonical mirror event->timeblock 테이블 참조 컷오버 (DGN-871, 855 step-3 coordinated).
- mirror/{adapter,reconcile,sdk_bridge}.py + scripts/crew-merge/merge.py의 `event`
  테이블 참조를 `timeblock`으로 리라이트 (컬럼/FK/enum/SDK verb 무변경).
- lifekit 팩(dogany-lifekit 1.2.0)의 event->timeblock 스키마 마이그레이션(user_version 23)과
  coordinated 착지 -- capability probe가 혼합상태를 양방향 fail-closed 게이트.
- 내부 리팩터, 유저 가시 변화 없음.

## [1.31.6] - 2026-08-13

Skull 핸드오프 착지 + 재시작 통지 정리 (DGN-869, DGN-801, DGN-834).
- DGN-869: dogany-lifekit-setup SKILL.md read-precedence를 3-layer 오버레이 해소로 변경
  (service/lifekit/i18n 오버레이 -> config/i18n 상속 -> SKILL.md fallback). Skull lifekit
  오버레이(855 step-2 tail) 활성화 스위치 (핸드오프 K513ZCY9).
- DGN-801: bridge set-result fast-path interceptor를 캐논 템플릿에 착지 (FASTPATH_HANDLER
  플래그 기본 OFF -- 인스턴스별 opt-in, Warg 활성화는 별도). 워크아웃 세트결과 숫자 입력의
  end-of-session 지연(~3분) 제거 경로.
- DGN-834: 재시작 완료 통지에서 resume-label을 단일 push 메시지로 인라인 병합
  (별도 "이어서 시작" 라인 제거, --resume-label 플래그).

## [1.31.5] - 2026-08-13

session recap 연속성-꼬리 누출 수정 + base-kit 프레임 계약 확정 (DGN-862, DGN-783).
- DGN-862: SessionStart 연속성 블록이 주입된 컨텍스트 턴을 다시 삼켜 자기증식하던 누출 차단
  -- recap이 injected-context 턴을 드롭해 continuity 블록이 자기 자신을 재생성하지 못하게 함.
- DGN-783: base-kit 프레임 계약 확정 (grill blocker B1-B4 해소: kit-owned migrate, atomic
  db-init, D-D publish-signature gate, units seam) + BK-1 kit-select 재배선.
  lifekit 카탈로그 행 scaffold->published 승격 (LS-6 signoff).

## [1.31.4] - 2026-08-13

canon lifekit/도메인 결합 분리 (DGN-851) -- config-first + i18n.
프레임워크 엔진·루틴·브릿지에 박혀 있던 도메인/locale 콘텐츠를 config/i18n으로 이관, 한국어
인스턴스 zero-delta.
- memory-engine consolidate/classify 프롬프트의 역할명 "생활비서" -> AGENT_ROLE config(기본값 generic).
- cron-guard 유예 라벨 / version-check·self-update 제품명 통지 / bridge fold 캡션(진행 기록 등)
  -> i18n 카탈로그(ko 원문 유지 + en 추가).
- status-footer 보드 제목 -> DASHBOARD_BOARD_TITLE config. outage-recovered dead code 정리.
- config/i18n 부재 시 전부 리터럴/raw 폴백(zero-delta), 다국어 인스턴스에서 한국어 잔재 제거.

## [1.31.3] - 2026-08-13

base-kit config-변수 seam (앵커) -- lifekit 커스터마이즈를 canonical에서 분리 (DGN-849/850).
목적: 벤더 코드(base-kit)엔 인스턴스/제품/locale-specific 데이터 ZERO, 변하는 건 전부
non-vendored config/pack으로. 재벤더가 커스터마이즈를 안 지우고, 도메인 데이터가 코어로
안 샌다.
- 캘린더 색 seam(kit-agnostic): adapter가 config/calendar-colors.json에서 color-source 축 +
  값->colorId 맵을 읽음, config 부재 시 no-color 폴백(zero-delta). done=10/expired=8 색마커
  은퇴, 색 축은 config 소유(D2). timed subtype 블록 projection(라벨/장식 슬롯은 config에서,
  프레임워크엔 도메인/locale 단어 0).
- base-kit variable-ization: NOTION import 경계(오너 이주일)·SWEEP_GRACE·PLACEHOLDER 등
  하드코딩값을 config-first + 리터럴 폴백으로. 새 인스턴스가 오너 데이터를 상속하던 유출 해소.
- pack-deps provisioning seam: pack install/update 시 requirements.txt를 인스턴스 런타임
  인터프리터에 pip-provision(idempotent/logged, requirements 부재=no-op, 실패=graceful degrade,
  PEP668 --user 한정 재시도). compat-lint가 payload/requirements.txt를 막던 버그 수정.

## [1.31.2] - 2026-08-13

회상 기억 주입에 우선순위 프레임 추가 (DGN-848).
매턴 콜드 메모리 주입 헤더가 종속 프레임 없이 들어가 stale 결정 메모리가 활성 스킬을
덮던 문제를 수리. 헤더를 `[관련 기억 -- 자동검색 · 참고용]`로 프레임하고, 충돌 시
우선순위(현재 지시 > 운영시스템(활성 스킬/AGENT.md/kit) > 회상 기억)를 명시.
기억은 사실(타임존/선호/기록) 출처로는 여전히 권위 유지, 행동·결정 충돌 시에만 종속.
noise-filter 재수집 가드 포함(신규 프레임 라인 자체는 재수집 대상에서 제외).

## [1.31.1] - 2026-08-12

usage-gate 과차단 완화 (앵커) + 인지 출력 포맷 + 통지 테스트 평문화 위생 (riders).
max_20x 플랜 리셋 임박 잔여예산 재계산(CAP 97), MUST-USE 편집기 heavy-gate 예외,
5h/7d 창별 차단 복구 자동화(/usageretry + 리셋 후 fresh 버튼), 정본 stale 2일 경보,
병렬 herd 봉쇄(ledger 시간버킷 원자화), 5h resets_at 캐시 추가(기존 오스케줄 버그 수리).
Riders: telegram.md bold/이모지 섹션헤더 규칙 정정, SECTION_GLYPHS 팔레트 도입,
L0 응답골격 게이트 명문화, self-update 통지 글리프화, 통지 테스트 평문화.
(DGN-835, DGN-829, DGN-840)

### Added
- usage-gate: 리셋 임박(24h 내) 잔여예산 재계산 로직 -- max_20x 한정, 남은 예산에
  맞게 CAP을 97로 클리핑하여 과차단 해소. (DGN-835)
- usage-gate: MUST-USE 편집기(baseline-editor 등 Sonnet/Haiku 모델) heavy-gate 예외 --
  필수 도구가 usage-gate에 잡히는 과차단 제거. (DGN-835)
- usage-gate: 5h resets_at 캐시 추가 -- 기존 오스케줄(5h 윈도우 오프셋 계산 버그) 동시 수리. (DGN-835)
- config: SECTION_GLYPHS env 변수 추가 -- 섹션 헤더 글리프(✅📌📋) 팔레트 on/off 제어. (DGN-829)

### Changed
- usage-gate: 5h 창 차단 -> 큐 자동재개, 7d 창 차단 -> 유저 알림 + /usageretry 슬래시
  커맨드 + 리셋 후 fresh 버튼 재알림으로 창별 차단 복구 자동화. (DGN-835)
- usage-gate: 정본 stale 경보 -- 2일 연속 defer 시 경보 발생. (DGN-835)
- usage-gate: ledger 시간버킷 원자화 -- 병렬 herd 봉쇄(동시 heavy 요청 스파이크 차단). (DGN-835)
- telegram.md: bold 규칙 정정 -- 브릿지가 이미 <b> 렌더하므로 마크다운 이중 적용 오류 제거.
  이모지 섹션헤더 채택, SECTION_GLYPHS 팔레트("복잡·긴 내용에서만" 게이트, 단순답=평문). (DGN-829)
- AGENT.md: L0 응답골격(결론/근거/결정 3칸) 게이트 명문화 -- 단순 답 = 1칸, 결정 有 = 3칸 고정. (DGN-829)
- self-update 통지 라벨 글리프화 -- 업데이트 노티스에 SECTION_GLYPHS 적용. (DGN-829)
- 통지 테스트 평문화 위생 -- dec-117 정합, 노티스 테스트 케이스 평문 기준으로 정렬. (DGN-840)

## [1.31.0] - 2026-08-11

bridge/output-contract layer split + progress narration on by default.
Output contract separated into two layers (bridge.md universal / telegram.md
render-specific); L0 response skeleton (conclusion/rationale/decision) formalized
in AGENT.md. Interim streaming default changed suppress -> fold so domain agents
show progress narration out of the box. /kill command fully removed (--stop covers
hard-interrupt fallback). Ready riders bundled: footer-stripper over-deletion fix
(DGN-816), push.sh HTML sanitize (DGN-822), update-notice plaintext (DGN-788),
private-backup secret-sweep exemption (DGN-808).
(DGN-825, DGN-816, DGN-822, DGN-788, DGN-808, DGN-581, DGN-681)

### Added
- bridge.md: new universal bridge output contract layer separating generic bridge
  rules from Telegram-render-specific rules (telegram.md). CLAUDE.md loader
  switches @telegram.md -> @bridge.md. [[OPTIONS]] body three-clause contract
  (label+description required / recommendation on label directly / reference
  tokens must match labels) lands in bridge.md as universal contract.
  Canonical landing pending recursive-import proof gate. (DGN-825 item 2-3)
- push.sh HTML sanitize hop: formatting.py `sanitize_message_for_telegram` applied
  to cron/push delivery path -- shell-rail format leaks (---/bold/HTML tags) and
  OPTIONS_MARKER telegram dependency chain severed. (DGN-822)

### Changed
- Interim streaming default: INTERIM_MODE-unset domain agents change from suppress
  to fold -- progress narration shown in folded quote block by default.
  STREAM_INTERIM=true inline setting stays inline (backward compatible). (DGN-681 / DGN-825 item 4)
- L0 response skeleton (conclusion/rationale/decision 3-slot) moved to top of
  AGENT.md workflows as formal entry. (DGN-825 item 3)
- Update-available notice changed from HTML fold to plaintext inline (dec-117);
  HTML tag raw leaks in SessionStart relay path resolved. Canonical carry-back:
  agents/.template/routines/version-check.py. (DGN-788)

### Removed
- /kill command fully removed from canonical .template/bridge and OSS bridge:
  CommandHandler, _cmd_kill method, BotCommand("kill"), CMD_DESC_KILL string.
  /stop handles both soft and hard-interrupt fallback. (DGN-581 / DGN-825 item 1)

### Fixed
- Footer stripper over-deletion: _FOOTER_BLOCK_RE anchored to line-start
  (MULTILINE) plus message-tail -- mid-message [live]/[pending-decision] literals
  no longer stripped. 21-case test suite green, zero regression. (DGN-816)
- Private-backup secret-sweep exemption: owner-declared private backup repos
  exempt cat{2,3,4,5,9} (face-declarable owner PII) via self-declarable marker;
  cat{7,8} (actual secrets / forbidden tracking files) remain enforced.
  Unblocks Ag/Warg backup push blocked 3.5 weeks. (DGN-808)

## [1.30.1] - 2026-08-09

countdown done-marker file-contract seam. The bridge countdown driver now writes
`<id>.done` on natural timer completion (cancel = no marker), letting a consumer
distinguish completion from cancellation by marker presence. No user-visible change
(existing countdown behavior unchanged, live instances untouched). This unblocks the
workout-domain pack engine's "timer end => next step auto" chaining. (DGN-805, DGN-782)

### Added
- bridge countdown driver done-marker: writes `$COUNTDOWN_DIR/<id>.done` on natural
  completion (fail-open); cancel/failure writes nothing. Marker consume/delete is the
  consumer pack's responsibility.

## [1.30.0] - 2026-08-09

lifekit independent product-line split. lifekit extracted from the framework core
into its own pack (dogany-lifekit); framework keeps only the contract + pack-install
machinery. No user-visible change (install layout / runtime paths unchanged, live
instances untouched). (DGN-681, DGN-803, DGN-789)

### Added
- compat-lint pack contract validator (publish + install side); legacy packs without
  contract_version are unaffected (legacy-grace).
- pack_install kit-class install: category placement + reverse-drift guard (instance
  newer than pack => core overwrite skipped) + i18n JSON merge.

### Changed
- lifekit (core, schema, migrations, service/lifekit, 8 domain skills, bundle
  routines/config) extracted from framework core into independent pack.
- mint: lifekit DB init moved from unconditional path to kit-activation (pack install);
  non-lifekit mint completes without lifekit.
- cadence-gate lifekit pin enforcement relocated to compat-lint 3-point version check.

## [1.29.0] - 2026-08-08

Option-button output consolidation + lifekit crew-merge engine (internal tooling).
(DGN-790, DGN-791)

### Added
- lifekit crew-merge engine: a reusable canonical primitive
  (`scripts/crew-merge/`) that merges two agent instances' lifekit databases into
  one crew database -- deterministic id-remap, natural-key dedup, atomic promote,
  umbrella count gate. Landed as tooling only; the actual data cutover is a
  separate owner-gated step and does NOT run at release. Core lifekit stays
  uncoupled (engine takes all DB/schema/migrate paths as injected CLI args).
  (DGN-791)

### Changed
- Option-button (`[[OPTIONS]]`) output unified: consistent width and truncation
  handling across the rendered button set. (DGN-790)

## [1.28.0] - 2026-08-08

base-kit kit-select onboarding + update-notice information architecture rework.
(DGN-466, DGN-783, DGN-785)

### Added
- Kit-select onboarding (base-kit): a newly minted main agent chooses its focus
  at the end of onboarding -- "everyday life management" installs lifekit
  (meals / workouts / appointments / relationships), "focused on specific work"
  starts kitless. Gated by a `KIT=` tristate (pending -> lifekit|none) plus a
  class enum (main|domain); tier language removed (DGN-590). Owner-approved offer
  copy wired via i18n `kit.offer` (ko/en). The `DOGANY_KIT` preset is whitelisted
  (lifekit|bizkit) with a pending fallback on unrecognized values.
  (DGN-466 / DGN-783 BK-1)

### Changed
- Update-completion notice information architecture (DGN-785): the notice fold is
  reorganized into three bracketed sections -- Summary / Details / Try this --
  driven by `ko` / `ko_detail` / `ko_try` user-summary keys. Ticket numbers and
  other developer-facing markers are stripped before render; empty sections are
  omitted.

## [1.27.4] - 2026-08-07

버튼 라벨 폭 재보정, 카운트다운 타이밍·UI 개선, 업데이트 안내문 정리 마무리 패치.
(DGN-779, DGN-780, DGN-780b, DGN-784)

### Added
- Countdown free-form customization (`bridge/countdown.py`, `bridge/config.py`,
  `bridge/i18n/en.py`, `bridge/i18n/ko.py`): `start_countdown` / `Countdown` /
  `render_countdown` gain optional `icon` / `done_icon` / `glyph` params with a
  single priority resolver (call > config > default) and silent safe fallback on
  markdown-risk/unsafe values. New config knob `COUNTDOWN_GLYPH_SET` (validated
  glyph allowlist: dot default / block-line / square). i18n templates carry
  `{icon}` / `{done_icon}` placeholders; control-file schema adds optional
  `icon` / `done_icon` / `glyph` fields. Backward compatible: omitting the params
  reproduces the prior default look. (DGN-780b)

### Changed
- Countdown timing switched from a fixed-cadence nap to deadline-anchored
  boundary snap (`bridge/countdown.py` `_next_boundary`): `editMessageText`
  latency no longer accumulates into display drift. Bar redesigned to DRAIN
  (filled = remaining) with hourglass/check icons. (DGN-780)
- Option button label width coefficient recalibrated (`bridge/options.py`):
  `_label_width` / `_shorten_button_label` add a whitespace-only branch
  (`isspace()` -> 0.4, was 1.0; on-device ~0.25, previously overcounted 2.7x)
  and `_BUTTON_LABEL_MAX_WIDTH` 30.0 -> 31.0 (on-device 1-line boundary ~31.5,
  1.0 reserved for the ellipsis glyph). Slightly longer labels now stay on one
  line without truncation. (DGN-779)

### Fixed
- Update-completion notice no longer falls back to developer CHANGELOG content
  (`routines/version-check.py`, `routines/self-update.sh`): the fallback path
  that called `changelog_section()` / `_release_notes_local()` /
  `_release_notes_remote()` when the user-summary block was absent is removed
  from all three call sites. The CHANGELOG is English/developer-facing (DGN-210)
  and contained raw markdown, backticks, and internal file/function names that
  must never reach user-facing output. When no user-summary is available, the
  notice shows no fold body (safe silence over raw developer content). The
  `releases/vX.md` `<!-- user-summary -->` contract (DGN-699) is unchanged. (DGN-784)

## [1.27.3] - 2026-08-07

OSS 브릿지 증분(ce3b597..8ace92b) 재벤더 앵커 번들: 비핀 카운트다운 프리미티브,
재시작 후 resume-intent, 마크다운 sanitize 백스톱.
(DGN-594, DGN-706, DGN-706b, DGN-775, DGN-704c, DGN-719)

### Added
- Non-pinned transient countdown primitive (`bridge/countdown.py`): `start_countdown`
  edits a single Telegram message in-place on a 10-second cadence, displaying remaining
  time without flooding new bubbles. Shared `EditRateGuard` (`bridge/edit_guard.py`)
  manages Telegram edit rate limits across the countdown and fold-edit paths.
  Warg workout rest timer uses this primitive for real-time countdown display. (DGN-594)
- Resume-intent auto-notice (`bridge/self_restart.sh`): after restart, if
  `--resume-intent` was passed, the bot automatically resurfaces the in-progress task
  to the user. Omitting the flag is an explicit "no in-flight" declaration. (DGN-706)
- Version update auto-notice (`bridge/self_restart.sh`): after a successful self-update,
  the bot automatically notifies the owner of the version change (old -> new). (DGN-706b)

### Changed
- Bridge output contract phase-1 alignment (`bridge/formatting.py`, `bridge/options.py`):
  core-semantic and render-layer output contracts aligned; includes OSS alignment of the
  previously un-pushed pin lineage (DGN-719 phase-1). `dashboard.py` and
  `tests/test_dgn704_label_shorten.py` clean-reflected to OSS parity (byte-identical
  to pin ce3b597). (DGN-719)

### Fixed
- Telegram markdown sanitize backstop (`bridge/formatting.py`, `bridge/options.py`):
  raw markdown headers (`#`), tables (`|col|`), and option labels are now caught and
  sanitized at the bridge output pipeline before reaching Telegram, preventing raw
  symbol leakthrough. (DGN-775)
- `[[OPTIONS]]` button number-only label edge case (`bridge/options.py`): fixes a
  parsing edge case where button labels rendered as number-only instead of the expected
  number + label combination. (DGN-704c)

## [1.27.2] - 2026-08-07

통지/fold 마무리 안정화 번들: 완료 통지 단일화, fold 라벨 정리, 최종답 불변 원칙 + 테스트·훅 위생 라이더.
(DGN-764, DGN-766, DGN-777, DGN-716, DGN-727, DGN-760)

### Changed
- Update-completion notice unified into a single `<blockquote expandable>`: the summary
  fold and detailed-changes fold were emitting two separate Telegram bubbles; they are
  now combined into one expandable block (summary first, detailed changes below; each
  section degrades gracefully when absent). Applies the same DGN-718 principle to the
  completion notice surface. (DGN-766)
- `agents/.template/bridge/sdk_bridge.py` fold finalize dedup direction inverted
  (final-sacred): when a final reply paragraph partially overlaps the interim fold,
  the paragraph is no longer removed from the final answer -- instead, overlapping
  paragraphs are subtracted from the fold side. Full-overlap case retains existing
  `fold_delete` behavior. Final reply content is now always fully visible in the
  message body. (DGN-777)

### Fixed
- Trailing ellipsis ("…" / "...") removed from four fold labels in
  `agents/.template/routines/self-update.sh` completion notice ("이번 업데이트 요약 …",
  "자세한 변경 내용 …", en mirrors). Functional truncation marker at 2500 chars is
  preserved. (DGN-764)
- `tests/dgn712_smoke_gate_selftest.sh` S1 abort-warning-push assertion updated to
  match the current push string ("업데이트 잠시 보류"); was failing against a stale
  expected value since v1.24.1. Behavior unchanged; test-only fix. (DGN-716)
- `scripts/publish.sh` pre-push secret-sweep fallback path changed from a hardcoded
  absolute Metal workspace path (pre-DGN-036 relocation artifact) to a repo-relative
  path, preventing fail-closed push blocks on workspace relocation. (DGN-727)
- `agents/.template/bridge/tests/conftest.py` now forces a hermetic test
  `PROJECT_ROOT` instead of inheriting from the shell environment, blocking live `.env`
  load that caused five test assertions to flip depending on whether a live Metal
  `PROJECT_ROOT` was set in the shell. (DGN-760)

## [1.27.1] - 2026-08-06

Self-update 버전 스큐 하드닝 + 업데이트 알림 단일 fold.
(DGN-762, DGN-718)

### Added
- Pre-restart `_selfcheck` cross-module constant resolution (`agents/.template/bridge/__main__.py`):
  scans bridge/*.py for `messages.<NAME>` refs and `from bridge.messages import <NAME>`,
  resolves each against the real messages module via getattr, and FAILs selfcheck if any
  is undefined. A bot.py<>messages.py skew now blocks the restart (old bridge stays alive)
  instead of crash-looping. (DGN-762)
- Assembly guard for the single-fold notice (`agents/.template/routines/tests/test-dgn718-single-fold-notice.py`):
  16 assertions covering the single-blockquote output contract so the three-bubble
  regression cannot recur silently. (DGN-718)

### Fixed
- `update.sh` ancestry-proof class coverage extended to classes 3 (local-edit->was-preserved)
  and 4 (conflict->was-held): when a bridge file's bytes byte-exactly match any point in
  the vendor template history, it is proven a pristine old vendor seed -> backup + land
  current template version; ancestry-fail (genuine edit) or no-git -> existing
  preserve/hold unchanged. Closes the root cause of the skull crash loop where messages.py
  was frozen by a misclassified class-3/4 preserve while bot.py advanced. (DGN-762)
- `routines/version-check.py` `_build_user_notice`: the entire update-available notice is
  now a single `<blockquote expandable>` (3-line collapsed preview + release-note summary
  below the fold) instead of three separate blockquote bubbles. Degrades to a plain 2-line
  blockquote when no release notes are available. Restores the owner-directed 2026-08-04
  single-fold merge that was dropped when DGN-738/742 rebuilt on the 3-block shape. (DGN-718)

## [1.27.0] - 2026-08-06

Bridge-hardening bundle: core/bridge output contract phase-1, full render-layer
re-vendor, and version-skew (render-layer freeze) prevention.
(DGN-719, DGN-721, DGN-757, DGN-742, DGN-725, DGN-710, DGN-682, DGN-726,
DGN-744, DGN-704b)

### Added
- Render-layer freeze fix (`update.sh`): `bridge_vendor_ancestor()` proves a
  no-manifest bridge file is a pristine vendor copy by matching its git blob id
  against the framework's full bridge path history. Proven -> auto-land current
  vendor version (backup first); unproven (genuine edit) -> existing
  adopt-provisional/CONFLICT semantics. Wired into class-5 (first run) and
  class-2-provisional (already-frozen instances). Resolves the Skull raw-HTML
  symptom. `--all` scope, 144/144 tests pass. (DGN-757)
- Bridge output contract phase-1 (`formatting.py`): (a) unknown-marker
  containment -- `contain_unknown_markers()` degrades unrecognized `word::`/
  `[[WORD]]` markers to safe literals (zero-width space) and logs them; wired at
  the `markdown_to_telegram_html` single choke-point, covering all paths; (b)
  `fold::` typed-block helpers (`compose_fold_block`/`render_fold_block`),
  summary as mandatory struct field, byte-identical render via DGN-619 `>!`
  path; (c) `link_preview::` added to recognized marker set; (d) `CONTRACT_VERSION`
  constant and `check_contract_skew()` function added as a **dormant phase-2 API
  surface** -- no producer stamps it and no consumer checks it at runtime; skew
  defence is performed entirely by (a). (DGN-719)
- Update-notice Korean detail fold (`self-update.sh`, `version-check.py`):
  second expandable fold rendered from `ko_detail`/`en_detail` keys in the
  release note `user-summary` block (existing parser supports underscore keys;
  zero parser-code change). Capped at ~2500 chars with truncation line. Empty
  detail key -> parity (byte-identical to pre-742 output). `release-closer`
  agent gains authoring duty for these keys. (DGN-742)
- `publish.sh` step 7a: after force-pushing the orphan snapshot commit to the
  public remote, create and push a plain `vX.Y.Z` tag on that commit. Dry-run
  reports the would-be tag without side effect. Fixes the public repo's last
  visible tag being v1.16.0 while content had advanced to v1.26.x. (DGN-744)

### Changed
- Full OSS bridge re-vendor (`agents/.template/bridge/`): 3-way reconcile of
  OSS pin ce3b597 into the vendored template, landing the fold-wiring delta
  (INTERIM_MODE config, `streaming.py` send/edit/finalize_fold_html,
  `sdk_bridge.py` interim capture + fold dispatch/finalize/delete + final-vs-
  interim dedup pair on all termination paths) and DGN-581 soft interrupt
  (`/stop` soft-first, `/kill` hard teardown, i18n en+ko). All vendored-only
  subsystems (DGN-163/531/616/670/686/376/429/665) preserved. +61 new tests,
  zero regressions. Options.py OSS<->vendored drift cleared (DGN-720/721).
  (DGN-721)
- Progress-narration fold wiring subsumed by DGN-721 re-vendor (substantive
  content); net-new from the DGN-682 merge: `test_dgn682_interim_fold.py`
  (26 tests), all pass. (DGN-682)
- Button-label weighted width (`options.py`): flat 16-char cap replaced with
  Unicode weighted budget 30 (CJK/full-width 1.5, else 1.0; ~18-20 pure-Korean
  chars on iPhone 13 mini). `_label_width()` helper, `unicodedata` import.
  Staged as DGN-704b and landed via the DGN-719 pin-bump re-vendor. (DGN-704b)

### Fixed
- Compose-fallback dedup (`sdk_bridge.py`): the final-vs-interim dedup in
  `_finalize_result` applied only to grown-bubble turns (`fold_msg_id is not
  None`). Short turns used the compose-fallback path with no dedup, causing the
  first paragraph to appear both in the collapsed progress fold and the message
  body. Now symmetric: full-dup -> fold not prepended; partial overlap ->
  `_dedup_final_against_interim` trim then prepend. Landed via DGN-721 re-vendor.
  (DGN-710)
- Consolidation cron usage-exhaustion silent defer (`memory-engine/memory.py`,
  `routines/`): when all compression calls fail due to usage exhaustion,
  the error report push and cron-guard "ROUTINE FAILED" alert are suppressed;
  `return 0` so cron-guard stays silent; watermark preserved; one-shot retry
  scheduled at the next usage-reset boundary. Fail-open: if usage status is
  unavailable, `_usage_exhausted=False` and genuine failures remain visible.
  (DGN-726)
- CHANGELOG fallback ticket-ref strip (`self-update.sh`, `version-check.py`):
  `(DGN-NNN)` and bare `DGN-NNN` refs removed from the CHANGELOG fallback
  output path so internal ticket numbers no longer surface in user-facing update
  notices. (DGN-725)

### Notes
- DGN-719 a1 `CONTRACT_VERSION` handshake is DORMANT in this release: no
  producer stamps the version and no consumer checks for skew at runtime. All
  skew defence is performed by the a2 unknown-marker containment layer. The
  handshake will be activated in phase-2 (after RULES-retrain, once the core
  model has learned the neutral-marker vocabulary). This is an honest record of
  the shipped state; earlier build reports describing an "active handshake" were
  inaccurate and are corrected here (dec-108).
- DGN-729 (display-name discipline) was shipped in [1.26.1] and is not
  re-listed here. Worklog status showing "open" is stale.

## [1.26.1] - 2026-08-05

Maintenance and discipline bundle centered on the framework update notice, plus
estate display-name discipline, a public-sync cadence gate, and a RULES
constitution cleanup.
(DGN-729, DGN-731, DGN-733, DGN-735, DGN-738)

### Added
- Estate display-name discipline (RULES.md Output): a new `DISPLAY_NAMES` config
  flag (default on) surfaces persona display-names for domain agents; when off,
  Metal/crew keep internal slugs and paths. Default seeded in
  `agents/.template/config/agent.conf`. (DGN-729)
- Public-sync cadence gate (`scripts/lib/cadence-gate.sh`): integrated into
  `publish.sh` as promotion-coupled forcing points FP2/FP3, blocking an
  out-of-cadence public push. Thresholds env-overridable; tests included. (DGN-731)

### Changed
- Framework update notice (`agents/.template/routines/version-check.py`), two
  fixes: (a) the once-per-notice guard is keyed on the offered VERSION string
  with a 7-day re-nag TTL instead of `session_id`, so the "update available"
  notice no longer re-fires on every conversation turn -- it shows once per new
  version, at most weekly (DGN-735); (b) the notice's expandable fold now renders
  the release note's localized `user-summary` instead of scraping the English
  CHANGELOG, so the fold is in the user's language with no ticket-ref exposure,
  with a CHANGELOG fallback for releases lacking the summary block (DGN-738).

### Fixed
- RULES.md constitution cleanup: removed the dec-094 provenance tag and the
  asterisk-bold output ban. The bridge auto-renders markdown to Telegram HTML,
  making the ban obsolete in the medium-neutral constitution; Telegram-specific
  concerns belong in the bridge layer. The dec-094 forcing-point principle is
  preserved (tag removed, not the rule). (DGN-733)

## [1.26.0] - 2026-08-04

Bridge drift forcing points + user-facing update summary. Adds write-side and
reconcile-side guards that make bridge drift conscious and visible, and moves
the update-completion notice off the English CHANGELOG scrape onto a
user-language summary field embedded in the release note.
(DGN-625, DGN-724, DGN-699, DGN-702)

### Added
- Pre-commit bridge pin-bump guard (canonical repo): a commit that modifies
  any file under the vendored `bridge/` tree is blocked unless it also stages
  a bump to `bridge/UPSTREAM.md`. Pure pin-only commits pass. This is the
  write-side forcing point paired with the existing release.sh pin gate.
  (DGN-625)
- Pre-commit instance bridge/ edit forcing point: a commit on a live instance
  that touches `bridge/` requires explicit `--no-verify` acknowledgement.
  The hook is inert for canonical-layout repos, and for merge/rebase/
  cherry-pick landings. Prevents silent drift that classifies bridge files as
  CONFLICT-preserve and stops canonical fixes from landing. (DGN-724)
- STALE-PRESERVE report line in update.sh reconcile: in the class-4 preserve
  path, when the canonical template side has also advanced since the file was
  last landed, update.sh now emits a `STALE-PRESERVE` line and a WARN so an
  instance silently missing canonical fixes on a frozen file is always visible.
  Existing CONFLICT report lines and counts are untouched. (DGN-724)
- User-facing update summary field (Spec A): release notes now carry a
  machine-readable `<!-- user-summary ko: | ... en: | ... -->` block near the
  top. self-update.sh extracts the summary for the resolved language
  (DOGANY_LANG -> AGENT_LANG -> en) and renders it as the update-completion
  notice body; falls back to the English CHANGELOG scrape for releases that
  predate the block. CHANGELOG stays English and untouched. The composed
  notice is handed to the restart worker so the restart-complete push carries
  the same user-language summary. (DGN-699)

### Notes
- DGN-702 (drop self-check ad-suffix from restart notice): no canonical code
  change required. The canonical self_restart.sh already defaulted to the
  clean completion line and never carried the self-check suffix; the suffix
  existed only transiently on the live Metal shadow and was already removed
  there. Policy satisfied; this entry records the verification, not a diff.
- DGN-677 (bridge manifest adopt-on-preserve): already shipped in [1.22.0].
  Not re-listed here.

## [1.25.0] - 2026-08-03

Self-update landing hardening. Divergent-lineage instances can take framework
code fixes without a forced schema migration, framework agent definitions now
reach existing instances on update, and the pre-push secret gate is canonical.
One purpose: make self-update landing correct across instance shapes.
(DGN-656, DGN-663, DGN-668)

### Added
- update.sh --code-only (alias --no-migrate): land framework code and files
  while leaving the database schema untouched. An intentional escape hatch for
  divergent-lineage instances whose local schema branched from the framework
  line. A forward-pin guard holds back code that would require a newer schema
  than the frozen database, keeping the instance safe rather than crashing a
  half-migrated verb. (DGN-656)
- .claude/agents/ is now a synced framework service: improvements to the
  framework agent definitions (baseline-editor, propagation-editor,
  release-closer) reach existing instances on update, not only at mint time.
  Instance-local customizations listed in .dogany-preserve stay protected.
  (DGN-663)

### Fixed
- Canonical git-hooks/pre-push with a full secret sweep and outbound-diff gate,
  seeded into the mint template and installed by mint.sh. Closes the gap where
  the pre-push gate went inert after a workspace relocation (core.hooksPath
  override left the per-clone hook unused). (DGN-668)

## [1.24.1] - 2026-08-03

Self-update hardening hotfix. An instance whose bridge translations had
drifted locally could fail to start after an update and get stuck restarting.
Now startup degrades safely instead of crashing, a restart only proceeds when
the new code imports cleanly, and a release-time check blocks the mismatch at
the source. One purpose: safe self-update. (DGN-712, DGN-683, DGN-603)

Converging fixes that sharpen everyday use: safer data handling, cleaner
message delivery, and steadier agent behavior. One purpose: usage-experience
convergence. (DGN-285, DGN-429, DGN-649, DGN-669, DGN-670, DGN-704)

### Added
- Output language guard: final replies are checked against the configured
  output language -- a system-prompt hint plus a charset-ratio advisory keep
  responses in the user's language. (DGN-429)
- Lifekit aggregate finality signal: daily rollups carry as_of / last_entry_at
  / closed markers so provisional and settled totals are distinguishable. (DGN-669)

### Fixed
- Oversized images are normalized before sending, and send failures now report
  the real reason instead of a generic network error. (DGN-649)
- OPTIONS buttons show a short action phrase so long descriptions no longer get
  truncated mid-word by Telegram; the full text stays in the message body. (DGN-704)
- New-instance memory hook no longer fabricates body-state from raw rows before
  real data exists. (DGN-285)
- Subagent placeholder-flake (a "still working" reply with no real result) is
  detected and recovered instead of surfacing as a dead turn. (DGN-670)

## [1.23.0] - 2026-08-02

Back-lands verified Metal-local bridge behavior into the canonical template so
the shipped bridge matches what has been battle-tested on the live instance,
and finalizes the /model switch wording. One coherent purpose: canonical bridge
parity. (DGN-696, DGN-697)

### Added
- HTML render pipeline (markdown -> Telegram HTML): structural pre-pass,
  word-only italic, lists and blockquote; link previews OFF by default with a
  `link_preview::` opt-in; streamed replies finalize as HTML. (DGN-376/619)
- Per-user message coalescing: rapid successive messages bundle into a single
  turn; voice-transcript preview bubble; busy-queue notice (ko/en). (DGN-616)
- Same-model no-switch guard: re-selecting the model already in use leaves the
  session untouched with an "already active" notice instead of spawning a
  needless new session. (DGN-192, restored)

### Changed
- /model wording finalized: scare-quote parentheticals removed from /help and
  the command menu; the selection screen states "starts a new session" only
  when a switch actually occurs; the switch result is a plain confirmation with
  no trailing duplicate warning. (DGN-697)
- Restart/update notices normalized: short status lines render as plain text;
  only release notes keep the expandable fold. (DGN-697)
- /skills output renders as bullets with block-scalar folding. (DGN-618)

### Removed
- /history command, its bindings, and i18n keys; /help no longer advertises it.
  (DGN-618)
- Legacy markdown bracket-escape shim retired -- bracket survival is now covered
  by the HTML prose path. (DGN-372 -> DGN-376)

### Fixed
- Footer sidecar consumer wired up: the consumer that status-footer.py already
  named now exists in sdk_bridge.py. (DGN-531/450)

### Notes
- Dependency: python-telegram-bot >= 20.8; pytest + pytest-asyncio added as
  dev/test deps. Suite: 344 passed / 0 failed.

## [1.22.2] - 2026-08-02

Recovers the reserved bundle that v1.22.1 shipped empty, plus an Ag-reported P1.
A new release gate (DGN-691) blocks tagging when any ticket reserved for the
target version is not done, so this class of silent drop cannot recur.

### Added
- Update-flow notices: availability nudge, install-complete, restart-complete,
  and a resume line -- rendered with Telegram expandable blockquote. (DGN-687)
- `push.sh --html`: proactive push (reminders/briefings/notices) supports code
  blocks, quotes, and expandable folds (parse_mode=HTML) with a plain-text
  fallback on HTTP 400. (DGN-688)
- is_error handling: transient failures (overloaded/529/timeout/5xx) auto-retry
  once, then surface a typed notice with a [retry] action; auth/token expiry
  prompts re-login without retry. (DGN-686)

### Changed
- Update approval now carries through to restart in one step (self-update
  wiring; interactive = immediate, autonomous = idle-guarded). (DGN-685)
- Register guard v2 is drop-only -- drops a pure-English block (0 Korean, 80+
  chars); fragment masking retired (leak blocked upstream). Emergency
  kill-switch restored. (DGN-686)

### Fixed
- `push.sh` no longer leaks `send_file::` marker lines as raw text in outbound
  Telegram messages. (DGN-692)

### Notes
- Rollout lag: this update run executes under the old self-update, so
  auto-restart (DGN-685) takes effect from the next update -- one manual restart
  is needed this time.

## [1.22.1] - 2026-08-01

Follow-up to the stability milestone: tightens how the agent narrates its work.

### Changed
- Output cadence rule (RULES `## Output`): the agent now acknowledges at task
  start and at each major phase, with no per-step chatter in between -- replacing
  the old "no narration at all" rule that led to long silences ending in a wall
  of text. Speak on issue or decision, report results as crisp bullets. (DGN-690)

## [1.22.0] - 2026-08-01

Stability milestone for serving Dogany to friends. This release makes updates
safe to apply and reversible, protects user data across migrations, and locks
release discipline so a version can never ship half-built.

### Added
- Fresh-install end-to-end smoke test (`tests/install_smoke.sh`): tier-1 checks
  (S1-S11) that exercise install -> mint -> first-run on the current version.
  The previous install smoke had been frozen since v1.0.3.
- lifekit database backup and restore chain: a WAL-safe, versioned backup is
  taken before every migration, with a restore path -- an interrupted or bad
  update can no longer lose life-management data.
- Release rollback path: a 9-step `docs/ROLLBACK.md` runbook plus the machinery
  behind it -- a drift guard that refuses to silently overwrite locally changed
  files during rollback (per-file acknowledgement required), a post-migrate
  check that the restored version pin and database agree, an update version pin
  (`DOGANY_UPDATE_PIN`) that survives across updates with a PINNED banner, and
  migration reversibility markers.

### Changed
- Canonical `main` now advances only through a versioned release. A pre-commit
  guard blocks bare commits to `main`; the release script is the sole path and
  gates VERSION bump, CHANGELOG entry, secret sweep, tag, and an atomic push.
  The updater is now channel-aware with version-pin support.
- Bridge manifest adopt-on-preserve (HYBRID): a missing manifest code-file no
  longer permanently freezes bridge updates; preserved files are adopted
  instead of blocking the update.
- Agent voice: an agent split across parallel sessions now refers to itself as
  one continuous self, never a third-party "that session".

## [1.21.0] - 2026-07-31

Adds the R7 information-card-block pattern and R8 resource-constraint model to
the design system, plus a reusable card-stack render primitive that produces
mobile-optimized image cards (e.g. a morning weather/air-quality brief) from
structured data.

### Added
- Card-block render primitive (`agents/.template/routines/lib/card_blocks.py`):
  builds vertical card stacks (value cards, quote cards, rows) as HTML/CSS and
  renders them to PNG via headless Chrome. Enforces a 3x3 type system (three
  sizes LG/MD/SM x three weights 800/600/400, generated from a single token
  table, zero scattered size literals) and a per-stack single-hero rule. A
  reference consumer (`card_blocks_demo.py`) assembles a weather brief card.
  (DGN-660)
- Design-system doctrine R7 (`docs/DESIGN-SYSTEM.md`): the information-card-block
  pattern -- a cross-surface (web + image) information-hierarchy layout of
  value-primary cards, one subject per card, color-block separation, low density.
  (DGN-660)
- Design-system doctrine R8: the resource-constraint (RCT) model for image cards
  -- target device 375 pt / 1080 px render width, vertical aspect gate
  w:h in [9:16, 4:5], PNG target under 1.5 MB, floor font 28 px. The card
  primitive's save path enforces these gates. (DGN-660)
- R3 enforcement-map rows for R7/R8. (DGN-660)
- Bundled Pretendard font (Regular 400 / SemiBold 600 / ExtraBold 800, OFL 1.1)
  for the card renderer, so image cards render identically on Linux hosts.
  (DGN-660)
- Token single-source-of-truth reconciliation (fix-then-ship, same release):
  the card primitive's default light palette is registered in
  `design_tokens.py` as the Layer B `card-light` theme plus a `GRADE_LIGHT`
  grade scale, and `card_blocks.py` now assembles its palette from those
  tokens (drift guard re-derives the mechanical slots at self-test). Icon
  artwork hex moves into a named bounded `ICON_COLORS` illustration-pigment
  palette with a matching R7 doctrine carve-out (illustration pigment is a
  distinct named surface from the semantic theme slots) -- resolving the
  release's own R7 "never new hex" rule against the shipped palette. (DGN-660)

### Changed
- Card-block layout tokens: stack gap 30 -> 33 px, horizontal card padding
  56 -> 62 px (meets the R7 >= 6% inner-padding rule at the 1080 render
  width). (DGN-660)
- Skill-creator authoring convention (`skills/dogany-skill-creator/SKILL.md`):
  rule/step examples must use schematic placeholders only; lifting real user or
  agent data into rule text is now explicitly disallowed (bias + privacy), with a
  pre-ship lint step. (DGN-535)

### Fixed
- Choice options no longer appear twice. When a message offers numbered choices
  with the options marker, the choice list is now shown only as tap buttons --
  the duplicated list is removed from the message body (lead-in text and other
  prose are kept). This applies to live-streamed replies as well; auto-detected
  menus keep their in-body list. (DGN-665)
- Tapping a choice button now reports the full option label to both the user and
  the agent. Long labels (e.g. Korean) that previously overflowed the button's
  callback data and collapsed to a bare number are restored from the button
  text, so the agent knows which option was picked. (DGN-665)

## [1.20.0] - 2026-07-31

Adds the R6 responsive/mobile-first doctrine to the design system, establishing
cross-surface rules for breakpoints, navigation, tables, and typography on
narrow canvases.

### Added
- Design-system doctrine R6 (`docs/DESIGN-SYSTEM.md`): responsive/mobile-first
  rules for all web and document-type visual surfaces. Covers breakpoint strategy
  (content-break triggers, not device-list copies), navigation (primary
  destinations always visible via bottom tab bar or equivalent on mobile;
  horizontal-scroll navigation is a defect), table form (natural-language columns
  convert to cards/lists on narrow canvases; value-only tables retain tabular form
  with frozen first column and ellipsis; horizontal-scroll + word-break combination
  is a defect), and typography (body >= 1 rem relative units; proportional typeface
  for natural-language text; touch targets >= 44 px). Enforcement reference
  implementation: console v1 (T-011 bottom tab bar, T-012 table conformance, T-013
  typography), live as of 2026-07-31. (DGN-376)
- R3 enforcement-map rows 6 and 7: console 375 px smoke gate (v2, prdt ticket-close
  gated) and all-other-web-surfaces unenforced debt row, both tracked explicitly
  per the R3 "no silent unenforced rule" invariant. (DGN-376)

### Changed
- R3 enforcement-map entry formerly labelled "R6 lint" renamed to "token-drift
  lint" for accuracy (it checks token drift, not the R6 responsive doctrine).
  (DGN-376)

## [1.19.0] - 2026-07-31

Adds the console theme layer to the design system and makes onboarding color
choices machine-checked.

### Added
- Console theme resolver in the design-token module (`design_tokens.py`):
  console-dark / console-light / console-dark-minimal themes (each AA 4.5:1
  verified per surface) plus `resolve_design(setting)`, which maps a
  `DESIGN_THEME` (dark|light) setting to a resolved 11-slot theme and the
  console's theme-bound extras. Surfaces that render the console consume this
  instead of hardcoded colors. (DGN-376, DGN-650)
- Onboarding color-identity gate: the background/accent validators
  (mid-luminance ban, crew distinctiveness, AA reachability) now run at the
  onboarding pick point and reject an invalid choice with a guided re-pick,
  instead of sitting inert. (DGN-650)

### Changed
- The design-token self-check now enforces AA contrast on every console theme,
  retiring the interim card-dark/console-dark overlap guard. (DGN-376)

## [1.18.2] - 2026-07-31

Follow-up to 1.18.1: a valid older domain database could still be refused. The
mirror adapter now trusts its capability probe as the real gate, so a
probe-passing schema is accepted even when its version predates the framework
baseline -- restoring calendar mirror for a health-domain agent on the older
lineage.

### Fixed
- Mirror adapter no longer pre-empts the capability probe with too high a
  version floor (`mirror/sdk_bridge.py`): the acceptance floor is separated into
  `PROBE_FLOOR_USER_VERSION` (v10, the oldest live lineage), so a valid v10
  database that passes every capability check is accepted, while
  `MIN_USER_VERSION` stays the canonical baseline and the monotonic propagation
  marker parsed by `update.sh`. Schemas at v9 or below are still rejected, and a
  probe failure still fails loudly. (DGN-654)

## [1.18.1] - 2026-07-31

Hotfix: the calendar mirror could silently die whenever a domain agent's local
database schema moved ahead of the framework. The mirror adapter now tolerates
newer schema versions instead of demanding an exact match, so ordinary domain
migrations no longer knock two-way calendar sync offline.

### Fixed
- Mirror adapter version gate is now forward-tolerant (`mirror/sdk_bridge.py`):
  the old exact-version whitelist `{11}` is replaced by a minimum-version floor
  plus a capability probe that checks the tables and columns the adapter
  actually reads. A live database at a newer schema version (e.g. v15 reached
  via domain relationship-module migrations) is accepted as long as every
  required capability is present, instead of crashing the poll loop with
  `MigrationRequired`. `update.sh` reverse-drift guard updated to parse the
  scalar minimum. (DGN-654)

Design-system color identity: agents now get a readable, machine-verified
color from a single hue, tuned per surface -- the foundation for consistent
visual output across cards, console, and future UI.

### Added
- Color identity system in the design-token canon (`design_tokens.py`, layer C):
  a fixed brand hue auto-tunes its brightness per surface so contrast is
  guaranteed by math, not by hand-picked hex. Ships 15 accent hues + 8 allowed
  background surfaces (4 dark / 4 light; mid-luminance greys rejected), with
  leader/domain reconciliation (crew accent for leaders, kit color for domain
  agents). All 120 accent/background pairs are checked against WCAG AA (4.5:1)
  at build time; chip outlines, perceptual distinctiveness (CIELAB dE), an
  over-tuning cap, and background validation are enforced in code. (DGN-376)
- Design-system doctrine R5 (`docs/DESIGN-SYSTEM.md`): the color identity model,
  resolution order, and honest residual status documented as the canonical home.

### Changed
- Token canon documented as three layers (brand core + surface themes + color
  identity), superseding the earlier two-layer description. (DGN-376)

## [1.17.0] - 2026-07-28

First curated public snapshot after the switch to a private canonical monorepo:
the public repo now receives one clean, allowlist-gated snapshot per release
instead of a full-history mirror.

### Added
- Allowlist publish gate (`scripts/publish.sh`): default-deny curation of the
  public snapshot. A path exports only when a `product.yaml` unit that is both
  `visibility:public` and `status:official` owns it (most-specific-owner-wins).
  Repo-meta and install/mint-essential shared files are injected by an
  enumerated list; domain-realdata / PoC-fixture and third-party IP classes are
  blocked even when a public parent glob would otherwise claim them. The public
  repo receives a single curated snapshot commit, never the canonical history.

### Changed
- Tier vocabulary retired: the HAND / CRAFT / MASTER tier scheme is replaced by
  the kit / module / staff-agent vocabulary across the product surface. (DGN-590)
- README rewritten (`README.md`, `README-ko.md`) with an updated product
  structure diagram reflecting the kit/module/staff-agent model.

## [1.16.0] - 2026-07-26

### Added
- `agents/.template/.claude/skills-bundle/spending-log/SKILL.md` (new): spending-log
  skill propagated to the template as consumption module J1. Covers spend capture (C1:
  spend-add, find, day, del, upd) and weekly ledger session (C2: week, month, unknown
  verbs), with a duplicate gate and scope rules. DB layer (migration 009, schema.sql v9,
  lifekit.py spend verbs) was already in canonical; newly minted agents now receive the
  skill at mint time. Bridge i18n entries added for both `ko` and `en` locales; bridge
  test suite extended to gate both catalog entries. (DGN-553)

### Changed
- `rules/RULES.md` (and `agents/.template/RULES.md` via symlink): dec-094 forcing-point
  meta-principle restored to canonical. The principle -- "rule unfollowed -> design a
  forcing point; structural wins over reminder/negative-command framing" -- existed only
  in Metal's local copy and was inadvertently reverted when v1.15.0 was consumed. All
  future mints and self-updates will carry it. Governance/baseline change only; no
  behavior change for instances already following the rule.

### Fixed
- `agents/.template/routines/push.sh`: four reliability hardening changes propagated
  from Metal canonical (DGN-452). (1) CLAUDECODE guard: when `CLAUDECODE=1` is inherited
  from an active session, a `--prompt` invocation now fails fast with a clear error and
  hint to use `--text` instead. (2) Per-call timeout: a 60-second wall-time cap via
  `timeout` / `gtimeout` prevents orphaned stdout holds. (3) stdin redirect: `< /dev/null`
  prevents the `claude -p` subprocess from hanging on stdin. (4) `set -e` safety: `|| true`
  on the claude call lets the retry loop continue after a non-zero exit. Propagated
  4-way across the estate (DGN-452).

## [1.15.0] - 2026-07-25

### Added
- `agents/.template/routines/session-recap.py`: size-log instrumentation
  (`_log_recap_size()`) records injected character totals per session and
  flags a hygiene warning when the 7-day median exceeds the configured cap.
  `agents/.template/routines/tests/test-session-recap-size-log.sh` (new,
  4 scenarios). `packs/dev/refdev/scripts/ticket-hygiene.sh`: 7-day median
  scan block and `[recap size]` flag line ported in generic English form.
  (DGN-501 follow-up)
- `packs/dev/refdev/scripts/ticket-hygiene.sh` (new): generic port of Metal's
  ticket-hygiene script. Includes DGN-409 additions: gate-resolution
  unpark-suggestion scan and weekly big-rock P1 table. Metal-specific paths
  stripped; `TICKET_PREFIX` config-driven; push path via `PUSH_CMD` env var.
  `agents/.template/worklog/_TEMPLATE.md` gains `parked` status and
  `gate:` comment convention. `packs/dev/refdev/AGENT.md.add` updated with
  gate convention one-liner. (DGN-409)

### Fixed
- `agents/.template/routines/generic-brief.sh`: blank line between sections
  now inserted consistently, matching Metal main behavior. (DGN-562)
- `agents/.template/routines/session-recap.py`: `_DEFAULT_CHAR_CAP` reduced
  from 500 to 200; cap is now applied per-half (first N + last N chars).
  `agents/.template/config/agent.conf` comment updated to document
  `RECAP_CHAR_CAP` as a per-half value. (DGN-562)
- `agents/.template/routines/remind.sh`: read the 9th `persons` field from
  `remind_select.py` and append it to the alert body; the template lagged the
  9-field output contract and silently dropped participant names on freshly
  minted agents. (release-preflight live-ahead harvest, DGN-225)

### Changed
- `rules/RULES.md`: work-items line rephrased from negative-command
  ("never memory") to positive ticket-surface ownership: work-items live on
  the ticket surface; memory holds durable facts only. Applies via symlink
  to `.template/RULES.md`. (dec-090 / DGN-446)
- `mirror/sdk_bridge.py`: `ALLOWED_USER_VERSIONS` tightened from `(8, 9)`
  to `(9,)` after the 24-hour M1 rollout window (v<9 nodes confirmed
  absent). (DGN-553)
- `agents/.template/routines/status-footer.py` (Rev 11): `[콘솔액션]`
  dashboard section added. Byte-identical to Metal canonical; section is
  omitted silently on instances without `decision-actions.md`. New test
  suite `agents/.template/routines/tests/test-status-footer.sh` (159 lines).
  (DGN-536)
- `install.sh`, `update.sh`: Pro (non-max) subscription tier now seeds
  `opus,sonnet,haiku` in the `/model` picker instead of `sonnet,haiku`.
  Sonnet remains the recommended default for non-max installs; fable stays
  max-only. Existing user-customized `BRIDGE_MODELS` values are untouched
  by the update backfill. (DGN-565)

## [1.14.0] - 2026-07-22

### Added
- `skills-bundle/youtube-digest/`: YouTube transcript digest skill promoted
  from the estate shared-skills incubation tier into the canonical
  skills-bundle. Ships dormant (present in bundle, not linked by default);
  per-instance opt-in via the standard default-off skill policy. Includes
  `SKILL.md` and `yt_fetch.sh` transcript fetcher. (DGN-507)
- `routines/promote-to-main.sh`: estate-growth promote-to-main script (build
  step-1). Grill-locked design: `--role` required, `--pack` opt-in, pack-id
  fail-fast, re-run recovery, depth-1 hardening, P28 order, token gate before
  class stamp. (DGN-475)

### Fixed
- `skills-bundle/diet-log/SKILL.md`: guard against English step-narration
  leak in the portion-scaling and composite-record flow. Internal planning
  lines were leaking into user-facing replies; the fix keeps scaling and
  per-row `--new` inserts silent (internal mechanics only), with only the
  final record summary presented to the user in the configured language.
  (DGN-503)

### Changed
- Template cron queue class set to `heavy` for the three stampede-prone
  Claude crons: `consolidate`, `classify-inbox`, and `mirror-reconcile`.
  Fresh mints now serialize these jobs via the machine-global cron-guard
  queue (slots=2, fail-open). Covers macOS plists and the Linux
  mirror-reconcile `.service` variant. (DGN-360)
- `bridge/options.py`: `extract_options` now takes the last contiguous
  `1..N` numbered run in a reply, so a prose description of choices
  preceding the `[[OPTIONS]]` block no longer suppresses button extraction.
  A length-1 run broken by a non-consecutive number is still discarded
  (DGN-085/325 guard preserved). Adds `test_dgn494_multi_list`. (DGN-494)
- `scripts/pack/mint_run.sh`: owner-id preflight at line 659 changed from a
  hard fail in dry-run mode to a WARN, mirroring the token-gate behavior.
  (DGN-475)

## [1.13.4] - 2026-07-21

### Fixed
- Bridge watchdog no longer force-restarts a running bot when the instance
  has been moved to a new path. `watchdog_setup.sh` now rewrites the
  registered LaunchAgent plist's baked absolute paths to match the instance's
  current location before arming the watchdog. Previously a relocated instance
  left the watchdog pointing at the old, dead path: the heartbeat file was
  never written there, so the watchdog detected a perpetual stall and
  repeatedly kicked the live bot -- resulting in a service-restarted
  notification approximately every 10 minutes with no real fault present.
  (DGN-480)
- The Claude Code auto-memory shadow store (`autoMemoryEnabled`) is now
  shipped as `false` in the template and protected by a new `PreToolUse`
  write-guard hook (`cc-memory-write-guard.py`). The hook denies writes into
  the CC shadow store (`.claude/projects/*/memory/`) and the Dogany engine
  store (`memories/`), keeping all memory writes inside the Dogany memory
  engine. Previously the v1.13.1 framework consume silently re-enabled the CC
  shadow store by overwriting DGN-431's off-switch, causing the CC store to
  drift out of sync with the memory engine. (DGN-479)

## [1.13.3] - 2026-07-21

### Fixed
- `update.sh` step 3j now re-substitutes mint tokens (`__USER_LABEL__` and
  friends) after rsyncing the skills-bundle directory. Previously the
  skills-bundle was excluded from the section-4 substitution pass, leaving
  non-pack bundle skills with raw dunder tokens after the first update -- a
  silent regression that required manual recovery on affected instances.
  Instance-affecting: live instances with un-substituted tokens in
  `skills-bundle/` need one `update.sh` run against this release to heal.
  (DGN-406)
- `database/relmod.py` / lifekit bundle: `relationship-care` bundle key
  normalized from hyphen to underscore across 5 surfaces
  (`service/lifekit/bundle.conf`, `.template/config/lifekit.conf`, i18n keys,
  `agents/main/` copy, and the canonical bundle.conf). Fixes a 3-way drift
  that left the relationship-care feature unreachable via the bundle toggle on
  instances where the key mismatch silently suppressed activation. (DGN-468)

### Changed
- `dogany` CLI local-door branding: the terminal title and statusLine now
  display a `dogany` label when the agent is launched via the dogany command,
  making the session visually identifiable as a Dogany agent session. No
  behavior change. (DGN-276)
- Morning brief: workout card session label and schedule header title updated.
  The workout card now shows the correct session label; the brief schedule
  section header title is adjusted for clarity. (DGN-382 + brief UX)

## [1.13.2] - 2026-07-20

### Fixed
- Bridge vendor picks up DGN-460: the SDK transport `max_buffer_size` is now
  configurable via `CLAUDE_MAX_BUFFER_SIZE` (default 16MB; was a hard 1MB SDK
  default). Fixes a reader-loop crash when a single CLI->SDK JSON message
  exceeds 1MB (e.g. a base64 image inline in a tool result). Vendored
  upstream-first from the OSS bridge repo (OSS commit 01764ee). (DGN-460)

## [1.13.1] - 2026-07-20

### Changed
- `skills/dogany-skill-creator/SKILL.md` gains a "driver-mode hook pattern"
  note: guidance for skills that drive a CLI subagent in a persistent loop to
  maintain a single-writer session registry via Claude Code hooks. Covers the
  hook payload contract, the one-writer invariant, and the CAVEAT that hook
  payload fields are implementation details, not a stable API surface. (DGN-458)
- `agents/.template/baseline-editor.md` unified to one canonical form
  (instance-neutral). The unified definition is the union of four previously
  split rule sets: writing mandate (English/ASCII), stamp-lint
  (move DGN/dec references to worklog), hot-inject discipline (Why-How-What;
  hot carries What only), and pack-mirror gate. Persona tokens are now generic
  throughout; no instance-specific proper nouns remain. (DGN-456)

Both changes are framework/dev-tooling documentation only -- no runtime or
behavior change for end users, no migration required.

## [1.13.0] - 2026-07-20

### Added
- `pack_publish.sh` finalize mode (`--mode finalize`): re-seals an
  existing hand-curated pack payload without overwriting it. Finalize
  skips the materialize step (no rm-rf / upstream section copy), runs
  the three B4 gates in detect-only, reports source-sync drift without
  modifying the baseline, prepends the CHANGELOG entry, regenerates
  checksums (excluding `checksums.sha` and `.source-sync`), and upserts
  the catalog while preserving the existing status field. Complements
  the existing snapshot mode (new pack from live agent); snapshot mode
  behavior is unchanged. (DGN-441, dec-069 GO)

### Changed
- `pack_publish.sh` CHANGELOG write refactored from overwrite to
  newest-first prepend: existing entries are preserved and the new
  entry is inserted at the top. Applies to both snapshot and finalize
  modes. Fixes a silent history-destruction bug where every re-seal
  replaced the entire CHANGELOG with a single entry. (DGN-441)
- Dev pack bumped to 1.1.0 (separate version axis -- not a framework
  version). Fragment additions: work-item ticket discipline
  (all incoming work / backlog / pending decisions tracked as worklog
  tickets; no work-tracking to memory store) and managed-target registry
  principle (one slug-keyed identity map per steward, always current).
  Consumed via `pack_install --upgrade` on instances with the dev pack
  installed; fresh install for instances without it. (DGN-449, dec-073)

### Fixed
- Harness R3 and R5 restored to green on `origin/main`. R3 was asserting
  a stale dev pack version literal (0.1.0) after the GA 1.0.0 promotion;
  fixed by reading the version dynamically from `catalog.json`. R5 was
  failing because a fixture `checksums.sha` was not regenerated after
  a file rename inside the dev pack; regenerated to match the actual pack
  state. Neither was a product defect -- both were test-fixture staleness.
  Combined harness: 280/280 green. (DGN-445)

## [1.12.0] - 2026-07-18

### Added
- Install-time agent class selection: `install.sh` now asks whether to mint a
  "main" agent (lifekit-bearing, aggregates domain sections) or a "domain"
  agent (specialist, standalone). The class is stamped into `.instance.conf`
  at install time and preserved across re-mints and updates.
- Domain agent briefing runtime: domain agents run their own morning brief,
  daily retro, and weekly retro. In standalone mode they push directly; when
  a main agent is present they write a section to the submit queue and the
  main agent aggregates. Config-driven fire times (defaults: morning 07:00,
  retro 22:00, weekly Sunday 20:00) with an onboarding step that lets the user
  set them at first contact.
- Standalone-to-submit mode transition: when a main agent is added after a
  domain agent is already live, the domain agent automatically switches from
  direct briefing to submit mode. The transition gate verifies that the main
  agent is running before flipping the routing flag.
- Pack publish pipeline: `scripts/pack/pack_publish.sh` (new) produces a
  publish-ready pack payload from a live agent snapshot. Three mandatory gates:
  personal-data strip, persona-field blank, and knowledge-pin verification.
  Generates `checksums.sha` for downstream install verification.
- Pack lifecycle completion: `pack_install.sh` gains a frozen-knowledge
  snapshot delivery channel (B5) so bundled knowledge reaches the instance at
  install time, and a net-new skill directory install mode (B6) for packs
  that introduce skills not already present. Checksum verification (NM3) at
  install time: mismatch is a loud failure; absent checksum file warns and
  continues. Pack-owned `plists.defer` entries are merge-appended rather than
  overwritten.
- Pack catalog: `en` locale fields added (previously `ko`-only); packs with
  `status != official` are filtered from install menus.
- Onboarding: briefing-time configuration step added for domain agents;
  role-question conditional retention aligned across all three template copies
  (AGENT.md, SKILL.md, onboarding-check.py).

### Changed
- `mint.sh` preserves `DOGANY_AGENT_CLASS` and `DOGANY_PACKS` across
  re-mints, preventing a domain agent from being silently reclassified as main
  on recovery or re-run.
- `update.sh` now substitutes the agent name into `plists.defer` during the
  routine-rename pass, matching the existing `.plist` substitution. Without
  this fix, generic-brief defer entries could reference mismatched plist labels
  after an update, causing unintended briefing activations on pack install.

### Fixed
- Fresh domain agent installs now correctly seed `LIFEKIT=off` in
  `lifekit.conf`, even when the mint scaffold ships a `pending` value. The
  prior behaviour left new domain agents with a pending lifekit state that
  triggered a lifekit setup prompt on first session.
- `install.sh` crash-and-resume path: a domain install that crashed after
  minting but before writing the class marker previously hard-exited on
  re-run. The installer now re-enters the class selection loop so recovery
  reaches the same final state without `--root` flag knowledge.
- `install.sh` main-add guard: adding a main agent from a domain install now
  enforces the single-main invariant before writing the registry entry,
  preventing a domain instance from being permanently mis-stamped as main.
- Briefing schedule clock and config now stay in sync: `BRIEF_TIME_*` config
  values are updated alongside the launchd plist when slot times are adjusted
  post-install, so the agent's internal time reference never drifts from the
  actual fire time.

## [1.11.1] - 2026-07-18

### Fixed
- `update.sh` distributes `database/relmod.py` to instances
  (was missing from the database copy list; DGN-410 delivery gap found at
  release preflight).
- `update.sh` guard(ii) now recognizes the `Vendor-rev:` marker in
  `bridge/UPSTREAM.md` as evidence of a canonical bridge change, in addition
  to the existing `Pinned commit:` bump check (DGN-413). Previously, a
  bridge commit that added a `Vendor-rev:` annotation without changing the
  pin (the correct discipline for bridge-content-only vendor revisions) was
  misread as "pins equal, no bridge change" and the bridge rsync was
  silently skipped. Every instance that consumed a release containing such a
  commit (beginning with v1.11.0 / DGN-399) missed the bridge update on
  `update.sh` and required manual recovery. This fix makes `Vendor-rev:`
  enforcement live at the guard level, matching its documented intent.
- `database/relmod.py` alert-pick removes the global weekly hard cap
  (`CAP_REACHED`) that silenced all relationship-module alerts after any
  single alert was shown within the past 7 days (DGN-410). Instead,
  candidates are sorted by unexposed-first rotation (last-shown oldest
  first, with ratio as tiebreak): people who have never been shown surface
  before any previously-shown person, and previously-shown people re-surface
  in rotation order once the unexposed pool is exhausted. The 3-pick limit
  per run and all per-person gates (let-fade, snooze, dismissed-cycle guard,
  upcoming-appointment exclusion, RESURFACE pass) are unchanged. Selftest
  29/29 green.

## [1.11.0] - 2026-07-18

### Added
- Post-restart resume without an owner message: the bridge now bootstraps
  the owner stream at startup, so restart-verify and session-inbox
  injections run before the owner speaks (DGN-399, template sync).
- Morning brief greeting: exactly one greeting sentence in the agent's own
  voice as the second line of the brief -- context-aware, fact-bound
  (DGN-401).
- Domain-agent knowledge wiring standard: 3-layer wiring convention
  (delivery / discovery / refraction) is now enforced by machinery at pack
  install time, preventing a recurrence of the DGN-398 incident where a
  correctly delivered snapshot had zero runtime consumption paths (DGN-402).
  - `scripts/pack/pack_install.sh`: knowledge preflight (half-declaration
    and scripts-category pairing FAIL; consumer skill and turn-type
    existence checked); STEP 6 now injects the `knowledge.source` path from
    the pack manifest into `knowledge-snapshot.sh`; STEP 7 per-skill render
    pipeline (`_render_to` + `_subst_mint_tokens`) replaces the former plain
    `cp`; preserve-list auto-register (pack-owned comment) and reconcile at
    install time; STEP 7c selftest gate.
  - `scripts/pack/knowledge_selftest.sh` (new): mechanical G1-G4 gates
    (snapshot existence + pin parse; AGENT.md pointer marker; agent.conf key
    + canonical skill gate line; consumer skill blocks + refract smoke) plus
    scoped inverse check for warehouse-less packs (zero knowledge/ references
    in AGENT.md + agent.conf). Zero-model; deterministic.
  - `docs/KNOWLEDGE-WIRING.md` (new): 3-layer wiring convention reference
    for pack authors. Covers delivery / discovery / refraction layers,
    warehouse-presence branching, template block wording, design-turn
    consumption types (T1 post-log comment, T2 program-design, T3 periodic
    review), and minting gate checklist G1-G5.
  - `skills/dogany-memory-search/SKILL.md`: conditional warehouse
    absence-gate line added to the `## absence claim gate` section --
    if `config/agent.conf` sets `KNOWLEDGE_WAREHOUSE=<name>`, a 0-hit
    memory search alone does not gate a "knowledge absent" claim; the
    warehouse directory must also be checked. `KNOWLEDGE_WAREHOUSE` key
    absent -> memory-search gate unchanged (self-disabling; no dangling
    reference for warehouse-less instances).
  - `packs/catalog.json`: health-trainer knowledge object demoted from a
    structured 3-key object to a single prose display line (manifest is the
    sole install-decision source; catalog is display-only).
  - `scripts/mint.sh` / `scripts/update.sh`: CROSS-REF comments added at
    the 4 canonical token-list sites for mint/update cross-traceability.
- Skill authoring routing gate: `skills/dogany-skill-creator/SKILL.md`
  gains a `## authoring routing gate (read FIRST)` section at the top of
  the document (DGN-408). Before authoring any skill, agents classify the
  target as framework / common / personal and route accordingly: framework
  skills route via the integration agent (Metal) or are overlaid -- never
  destroyed by direct edit; common skills are authored by the main agent;
  non-framework skills never enter the product canonical. Two routing trees
  are given verbatim: one for estates with a framework-integration agent,
  one for product users without one.

## [1.10.0] - 2026-07-18

### Added
- AGENT.md template diet campaign (DGN-387 + DGN-390, combined token diet).
  Recurring per-instance saving: every future mint starts ~550-620 tok/turn
  lighter; live estate (Ag + Warg + Smith + Kojeni) net ~1,650-1,850 tok/turn
  after migration. Two components:
  - DGN-387: new cold reference doc `AGENT-OPS.md` (ops procedures:
    self-restart, self-update, subagent dispatch routing, upstream report lane)
    delivered via a new `update.sh` section 3k2 refresh channel
    (post-substitution SHA, dest-adjacent atomic mv, identity-gated-token
    contract assert). Template `AGENT.md` sheds procedural boilerplate blocks
    (self-restart, self-update, dispatch pointers, framework code boundary,
    language register guard, CRAFT activation, Paths) in favor of a 3-bullet
    hot Ops pointer block. `RULES.md` gains +2 lines: framework code boundary
    with canonical + PoC exemption, and a language register guard.
    `mint.sh` gains mandatory framework-manifest recording (AGENT-OPS.md +
    RULES.md SHA at mint, closing cross-version spurious-WARN window).
    Template `baseline-editor.md` gains stamp-lint and English-ASCII writing
    mandate. Acceptance suite T1-T5 (including cross-version gate T5) ships
    at `tests/agentops/`. CRAFT activation note relocated to `mint.sh`
    Next-steps heredoc.
  - DGN-390: `routines/session-recap.py` injection budget is now config-driven
    (`RECAP_PAIRS` / `RECAP_CHAR_CAP` keys in `config/agent.conf`, defaults
    2 pairs / 500 chars, was hardcoded 4 / 1,000). Worst-case session-start
    injection reduced from ~8,000 to ~2,000 chars. Silent fallback on
    missing or garbage config values. Existing instances pick up new defaults
    on next framework update; no manual migration required.

### Fixed
- `dogany-skill-creator` step 6 RULES conflict (DGN-391). Step 6 previously
  instructed agents to write a fact directly to `MEMORY.md`, conflicting with
  the RULES `memories/` engine-ownership clause. New step 6: leave a one-line
  session note; nightly consolidate harvests it. Three instances (Kojeni,
  digear, digear-sh) had already been silently skipping the step via RULES
  inference on 2026-07-17 -- the skill now matches what agents were correctly
  doing.
- `routines/cron-guard.sh`: failure alert now prefers the sibling stderr log
  (`<name>.stderr.log`) over the stdout log when non-empty, with stdout
  fallback (DGN-395). Previously the alert attached only stdout, which on
  real failures contained stale "[push] sent OK" lines with zero diagnostic
  value.
- `routines/push.sh`: generation call now retries 3 attempts total with 5s
  backoff on empty `claude -p` output; emits a clear stderr reason on final
  failure (DGN-395). Previously a single transient empty response caused an
  immediate exit 1.

### Notes
- ERRATUM: the `agents/main/` scaffold diet mentioned in earlier drafts is
  NOT in this release. `agents/main/` is gitignored (never tracked); the
  scaffold fix is local-only and out of release scope.
- Language discipline (DGN-387) is editorial-layer only in this release:
  the RULES register guard and baseline-editor writing mandate are delivered,
  but mechanical non-ASCII lint on workflow sections is not yet implemented
  (follow-up ticket open). Nothing delivered here mechanically blocks a
  steward from hand-writing non-ASCII workflow prose outside baseline-editor.
- `estate-doc-watch` R5 coverage (judge rule for Ops pointer + baseline-editor
  stamp-lint integrity) is model-judged on weekly diff, not preventive.
- Remote instances (kkari, shawn): after updating, KEEP the `AGENT.md`
  dispatch-pointers block until your bridge version is confirmed to inject
  subagent descriptions (A1 gate unverified). Hand-apply the
  `baseline-editor.md` stamp-lint / writing-mandate delta manually (agent
  defs ship at mint only, never refreshed by `update.sh`). Migration
  procedure: run `self-update.sh` first, verify `AGENT-OPS.md` exists and
  is substituted (no dunder tokens) and `RULES.md` is at the canonical
  version, THEN delete the fat blocks and install the Ops pointer (backup
  before editing; the old fat `AGENT.md` and new `AGENT-OPS.md` coexist
  harmlessly if you defer).

## [1.9.0] - 2026-07-17

### Added
- Daily retro: `RETRO_HEALTH_SOURCE=warg|local` config gate (default `local`,
  existing instances unaffected). When set to `warg`, the retro quotes the Warg
  agent's `report.section.retro` verbatim; "워그 건강 리포트 미도착" fallback on
  missing section. Task section added: done-today (`task-done-between`) and
  overdue (`task-overdue`) tasks shown at retro close; compressed to a few items
  + "외 N건" when long; silently omitted when no task data. (DGN-389)

### Fixed
- `update.sh` reverse-drift protection for un-versioned components (DGN-385).
  Two new guards: (1) section-root hold -- a `.dogany-preserve` entry matching
  a top-level component root (e.g. `bridge/`) now skips the entire rsync section
  with a WARN (file count shown); previously the root entry generated zero excludes
  and the component was silently overwritten. (2) Pin-based auto-detection --
  before refreshing `bridge/`, `update.sh` compares instance vs template
  `bridge/UPSTREAM.md` pin SHA; matching pins with local file differences = local-
  ahead, section skipped with WARN; differing pins = re-vendor has occurred,
  normal refresh proceeds. Instances without a pin file or without preserve entries
  are unaffected.
- `routines/version-check.py`: update-check nudge now gates on strict semantic
  newer-than (`_version_tuple` + `_is_newer`) instead of plain `!=` (DGN-349).
  Previously fired when the public repo lagged the framework release (e.g. "built
  from 1.8.0, upstream has 1.7.1"). Nudge fires only when the other side is
  genuinely ahead; equal and local-newer cases are silent. 12-case unit suite +
  E2E hook simulation green.
- Daily retro (Warg mode): health data now read live via owner lifekit.sh at
  retro fire time instead of consuming a pre-generated snapshot (DGN-396). Closes
  the cross-agent freshness gap: stale snapshot data (meals logged after snapshot
  generation) was being quoted. Fallback to snapshot with generation-time
  annotation ("HH:MM 기준") on any failure; retro never blocks on the cross-agent
  read. Rides the DGN-389 `RETRO_HEALTH_SOURCE` gate surface.

## [1.8.0] - 2026-07-17

### Added
- Morning-brief: config-gated diet/workout recap and weather image card (DGN-383).
  `BRIEF_DIET_RECAP` (default `on`) suppresses the yesterday recap block on
  instances where the diet domain has been transferred to another agent (prevents
  false zero lines). `BRIEF_WEATHER_CARD` (default `off`) enables a rendered
  weather+air-quality+quote PNG card sent as a photo after the text brief; the
  text weather block is suppressed when the card generates successfully. Card
  failures are fail-open: the text weather path is used and the brief is never
  aborted. `morning_brief_card.py` (Open-Meteo, render-venv convention) ships in
  the template. Both gates are documented as comments-only in the template
  `agent.conf`; instance values stay per-instance.
- Relationship-care skill (`service/lifekit/bundle.conf` entry) (DGN-383).
  `database/relmod`: meet-based alert-pick field, upcoming-appointment exclusion
  in contact-gap alerts, snooze/unsnooze support via additive migration.
  Selftest 27/27 (TC-25/26/27 new). Skill behavior 3 rewritten with context-snooze
  (3b) and persona tokens standardized to `user`. Ships DORMANT in the lifekit
  skills-bundle, activated post-mint by `dogany-lifekit-setup`. i18n keys added
  (ko/en).
- Install UX: recommended clone location is now `~/.dogany/framework` (DGN-384).
  Quick-install one-liner, Windows/WSL2 paths, `install.sh` and `update.sh`
  guidance updated across README en/ko, `windows/setup-windows.ps1`. Old clone
  locations (e.g. `~/dogany-agent`) keep working with no migration required
  (resolver is config-based via `.instance.conf` `DOGANY_REPO_ROOT`).

### Changed
- USER.md content boundary enforced in framework baseline (DGN-382, dec-049).
  `rules/RULES.md` Memory routing rule expanded: USER.md holds stable profile
  facts only (identity, job, timezone, relationships, domain core constants).
  Procedures, output formats, session mechanics, and operating rules are
  explicitly excluded and redirected to AGENT.md workflows or the owning
  SKILL.md. Unconfirmed preferences and one-off records route to engine memories.
  Subagent USER.md write prohibition made explicit. Promotion path added:
  recurring cross-skill preferences may be promoted to AGENT.md workflows after
  repeated evidence -- never on first observation. Template USER.md scaffold
  comment, `rules/USER.example.md`, and `dogany-user-onboarding` skill section 3
  updated to match.

## [1.7.1] - 2026-07-17

### Fixed
- `claude-usage.sh` expiry-aware token source selection (DGN-375): file token
  no longer shadows a valid Keychain token when expired. Both sources now have
  `expiresAt` checked; file -> Keychain fallthrough on expiry. Fixes 401 on
  live usage lookup caused by stale file token winning over valid Keychain
  token. Dev pack copy (`packs/dev/refdev/scripts/claude-usage.sh`) synced
  with same logic. Exit 1 on live lookup failure (previously exit 0, making
  gate callers unable to detect failures).

## [1.7.0] - 2026-07-17

### Added
- Pack machinery migrated to framework repo (DGN-368, spec DGN-366 v3,
  dec-036/dec-037). `scripts/pack/pack_install.sh` and `scripts/pack/mint_run.sh`
  pipeline now live in dogany-agent and are consumed by tagged release (instances
  update via the normal release channel; no live-skill hotfix lever). Instance
  context passed via explicit `--instance-root <path>` contract; steps that
  require an instance root log and skip cleanly when it is absent (no silent
  skip). `--catalog` flag available for override (transition/test lever).
- Pack install generalized to manifest-driven category install (DGN-368).
  Each pack declares its own `pack-manifest.json` (categories, required flags,
  reference slug, AGENT.md marker, agent.conf marker, optional `domain_seed`).
  Installer preflight and install steps only run for declared categories.
  Hard-coded requirements removed: `lib/`, `knowledge-snapshot`, `ledger.py`
  are now optional manifest-declared categories. Payload subdirectory name,
  reference root, and both idempotent markers (AGENT.md + agent.conf) are
  fully manifest-parameterized -- no machine absolute paths in the public repo.
  Step 7b (AGENT.md.add append) now applies `_render_to` slug-substitution.
  Step 8 (`domain_seed` consult-state seed) is now manifest-declared; dev pack
  does not declare it, preventing spurious lifekit health rows on dev mints.
- Health pack back-filled with `pack-manifest.json` (DGN-368 S1). Legacy
  idempotent markers (`DGN-287-CONSULT-FRAGMENT` / DGN-238 conf marker)
  declared verbatim -- live Warg re-install remains idempotent.
- New dev pack `packs/dev/` (DGN-368 S2): generic developer-discipline
  AGENT.md fragment (`DEV-PACK-FRAGMENT` marker, all prose general-form --
  no estate-specific proper nouns) covering: ticket discipline (worklog/,
  slug-derived ID prefix, open>wip>blocked>done+parked), design grill
  (adversarial stance, 2-round backbone rule, real-code final grill,
  self-contained restatement after fix round, no-guessing delegation),
  spec-first patching (lock-spec search -> verbatim implement, else design
  first), delegation discipline (subagent+self-test default, model always
  named, usage-window check before heavy dispatch), commit checkpoints
  (natural-checkpoint autonomous local commit, theme grouping, secret-sweep
  before push, public push = owner approval), and specialist boundary
  (lifekit domain excluded). Catalog entry and `packs/catalog.json` updated.
- Generalized `scripts/pack/refresh-source-sync.sh`: regenerates
  `packs/dev/.source-sync` baseline (pathless sha256 format, snapshot date
  header) from the declared source file. Run after a conformance pass to
  reset the drift baseline.
- Dev pack scripts: `packs/dev/refdev/scripts/secret-sweep.sh` (estate-path
  dependencies removed; owner-pattern config file at
  `config/secret-patterns.conf`; pattern-absent -> structural-scan-only +
  explicit warning, no placebo pass) and
  `packs/dev/refdev/scripts/claude-usage.sh` (generalized, no estate paths).
- Drift gate: `packs/dev/.source-sync` records sha256 baseline for the
  5 pack-mirrored source sections (Role, Tickets, Design grill, Spec-first
  patching, Local commit checkpoint). `routines/release-preflight.sh` now
  checks this baseline and warns on section drift without blocking the script.

## [1.6.0] - 2026-07-16

### Added
- Universal portfolio schema, core v1 (DGN-350). New module:
  - `docs/PORTFOLIO-CORE.md` -- registry of record for the core spec; core
    version bumps ride this repo's release machinery from this release on.
  - `routines/lib/portfolio-core-lint.py` -- structural/schema lint for
    portfolio indexes (md + JSON substrates, C0 grandfather path,
    parse-or-die header contract, tombstone cross-check). Also provides the
    generic structural-parse subset (`--parse-only`) and section dumps.
  - `routines/lib/portfolio-core-parse.sh` -- generic parse entrypoint
    speaking the PORTFOLIO-PARSE-OK/FAIL contract (CORE profile: EDGES
    optional). Deliberately NOT named portfolio-parse.sh so it can never
    clobber an instance-local parser on the routines/ refresh.
  - `routines/portfolio-reconcile.sh` -- weekly reconcile pass skeleton
    (lint, staleness, multi-source existence diff, disappearance report,
    exclusion full print). Never pre-registered; the setup skill wires it.
  - `skills/dogany-portfolio-setup` -- conversational activation (fresh-mint
    offer, index created only on opt-in) + soft migration (existing PM
    artifacts stay authoritative until a dated cutover). TIER-FREE per
    owner ruling dec-035. Offer state: `PORTFOLIO=` key in config/agent.conf;
    one-shot SessionStart offer after onboarding, never in the same session
    as the lifekit offer.
  - `routines/tests/test-portfolio-core.py` + synthetic fixtures -- 76-test
    regression suite for the lint and parse entrypoint (machine-independent).
- Mirror engine: V15 multi-calendar adapter promoted to framework standard
  (DGN-364, dec-031). The adapter now supports a multi-calendar target dict
  (cal_id_appt / cal_id_task / cal_id_travel + gtasks_checklist_id) with
  `get_mirror_targets(state)` as the single resolver, eliminating raw
  state-key reads from shell scripts. Legacy single-calendar installs
  (engraved `agent_calendar_id` / `agent_tasklist_id`) are fully preserved
  via a compat shim -- no migration required for existing instances (Warg
  verified). Template mirror-poll.sh and mirror-reconcile.sh updated to
  adapter-API reads. Unengraved instances now get an exit-3 sentinel and a
  daily push notification rather than a silent 400 error loop. 144-test
  suite added (test_v15_promotion.py); s1-s7 green.
- Mint: `git init` at birth (DGN-357). `scripts/mint.sh` now runs an
  idempotent `git init` + initial commit at step 8 (local only, no remote),
  using the standard .gitignore convention (MEMORY.md + inbox.md tracked;
  .env, venv, logs excluded). Re-mint is idempotent. Remote setup remains
  a manual owner step.
- cron-guard: opt-in machine-global queue (DGN-360). New flags
  `--queue <class> [--slots N] [--queue-timeout SEC]` serialize heavy
  Claude-invoking crons across all instance roots on the machine
  (`~/.dogany/cron-queue/<class>/`). macOS-compatible atomic-mkdir
  spinlock with pidfile + stale-lock reclaim. Timeout policy: fail-open
  (WARN and run rather than drop). No-arg invocation is byte-identical to
  the previous behavior. Class assignment to plists is opt-in and
  per-instance; no plist changes ship in this release.
- routine-ctl.sh: optional `[weekday]` argument on `enable` schedules a
  routine weekly (launchd Weekday key / systemd OnCalendar day token) instead
  of daily. Additive; existing daily behavior unchanged.

### Fixed
- Memory engine: nested-session write no longer fails when a live agent
  session sub-launches the compression process (DGN-352). Root cause: the
  haiku sub-launch inherited `CLAUDECODE` from the parent session, causing
  "cannot launch inside another Claude Code session." Fix: env scrub strips
  session nesting vars before the child launch; if the sub-launch still fails,
  a raw-append fallback ensures the write is never lost. Nightly consolidate
  (launchd, no nesting env) is unaffected.
- Template `claude-usage.sh` now reads `~/.claude/.credentials.json` first
  and falls back to Keychain, matching current CLI behavior (DGN-362). Fixes
  stale token reads after an account switch, which caused usage-window gating
  to act on the wrong account's limits.

### Changed
- update.sh: instance-preserve list + hooks split (DGN-359). Two
  complementary guards against the recurring update-clobber pattern (3rd
  recurrence, DGN-363 class):
  1. Framework hooks land in `settings.json`; instance-local hooks go in
     `settings.local.json` (Claude Code merges both natively). update.sh
     never touches `settings.local.json`.
  2. `.claude/.dogany-preserve` manifest: paths listed here are skipped by
     every rsync/cp refresh path in update.sh. The preserve list is printed
     on each run. Instances prune entries when the upstream version ships
     the same fix (self-healing).
  Tested 15/15 on throwaway instances including no-customization
  byte-identical regression and dry-run no-write.
- upstream-report skill Layer B: the parse check now prefers an
  instance-local `routines/lib/portfolio-parse.sh` when present and falls
  back to the framework-shipped `portfolio-core-parse.sh`, turning the
  ledger overlay on for every adopting instance without instance-side edits.
- Model picker defaults: Fable is now the first-listed model for the max
  tier (fable, opus, sonnet, haiku order). No behavior change for instances
  not on the max tier.

## [1.5.3] - 2026-07-16

### Fixed
- Running update.sh against the repo clone itself (instance root == repo
  root) now produces a named, actionable error message with a pointer to
  the standard layout, instead of a generic root-guard refusal. The
  dogfood-layout is not supported; the guard now says so explicitly.
  (DGN-341)
- Memory scaffold text in fresh mints no longer reads as an agent write
  instruction. The ownership voice now makes clear that memories/ is
  engine-owned and the agent never writes there directly, preventing the
  live incident pattern where a first session hand-created inbox.md and
  misrouted user facts. (DGN-344)
- Onboarding closing message now matches the instance's actual wiring.
  Standalone mints (no HANDOFF_PEER_AG in config) are never told to
  return to a main agent; that branch fires only when the key is present.
  Previously the branch was inferred from role name, so fresh-direct
  mints could receive the migration-path closing guidance incorrectly.
  (DGN-345)
- Mint checklist now includes a persona-seeding order note: specialist
  Role seeding must happen before token/launchd steps. Prevents a
  crash-safety gap where an incomplete mint could reach live state before
  identity was seeded. (DGN-342)

## [1.5.2] - 2026-07-16

### Changed
- upstream-report skill: self-maintained repo defect routing is now
  fail-closed with two explicit layers. Layer A (universal, no ledger
  required): a hardcoded backstop prevents any coolcoolk/* repo from
  receiving a public GitHub issue regardless of ledger state. Layer B
  (conditional, instances with product/PORTFOLIO.md only): a portfolio
  ledger overlay is consulted after a mandatory parse check; parse
  failure or lookup miss routes to outbox draft with WARN instead of
  the public path. Instances without a ledger skip Layer B on file
  absence -- behavior is unchanged from pre-ledger semantics. Fixes the
  DGN-330 class misroute where a self-maintained repo defect could
  reach the public issue path if the routing rule was ambiguous.
  (DGN-293)

## [1.5.1] - 2026-07-16

### Added
- Morning brief weather section: today's temperature range and hourly
  precipitation probability are now shown at the top of the brief, sourced
  from the Open-Meteo free API (no key required). The section is off by default;
  set AGENT_LAT and AGENT_LNG in config/agent.conf to enable it. Fetch failures
  are silently suppressed and never block the brief. (DGN-332)
- Version-check throttle: the remote GET runs at most once every 6 hours. The
  last successful result is cached in .telegram_bot/state/version-check-cache.
  Cache read/write failures are silently ignored (fail-open). (DGN-335)

### Fixed
- Mirror engine: overlap warnings triggered by transient mid-batch state (events
  moving together in the same sync cycle) are now suppressed. Detection still
  happens per-apply, but notifications are deferred to the end of the poll cycle
  and re-verified: only overlaps that persist in the final DB state are notified.
  Transient overlaps (resolved within the same batch) are silently cleared.
  17-case test suite added. (DGN-333)

### Changed
- Remote version check is now default ON. Instances on other machines will
  automatically receive a nudge at session start when a newer framework version
  is available, with no configuration required. To opt out, set
  DOGANY_VERSION_CHECK=0 in your instance .telegram_bot/.env. The legacy opt-in
  value (DOGANY_VERSION_CHECK=1) remains valid and keeps the check on. (DGN-335)

## [1.5.0] - 2026-07-16

### Added
- Single-option [[OPTIONS]] buttons now render as a proper Telegram button
  instead of being silently suppressed. The two-part fix covers the render
  path and the TextBlock-precedence assembly; a scaffold-leak guard ensures
  agent thinking text can no longer bleed into the button payload. (DGN-325,
  DGN-285)
- Self-restart now guards against interrupting a live user session. If a
  session is active when a restart is triggered, the restart is deferred
  until the session is idle. (DGN-328)
- Project folder path sanitization now follows the Claude Code rule: all
  non-alphanumeric characters (not just slashes) become dashes in the
  transcript glob path. Fixes silent consolidation failure for usernames with
  dots or underscores. (DGN-295)
- Skills and routines now carry a user-facing display name in their
  frontmatter. Agents use the display name in menus and confirmations instead
  of the raw directory name. Existing skills have been backfilled; a short
  i18n name tier is available for localized surfaces. (DGN-324)
- Cron-guard failure notifications now lead with the friendly display name
  instead of the raw job label, so the alert is readable without knowing the
  cron internals.
- Reminder cancel now works by index: the agent lists active reminders by
  number and accepts an index to cancel, with no requirement to remember or
  type the machine label. (DGN-324 GAP-6)
- Onboarding batch: identity fields now start blank (no slug or user-label
  pre-fill), the first message pairs the agent greeting with the first
  question in a single turn, the migration-path completion branch now closes
  cleanly with a single guidance line and no empty menu, and neutral button
  labels are now enforced throughout. Role-adaptive quick-start options
  (option 2 adapts to the filled role instead of being hard-coded to
  record-keeping) are included in this batch. (DGN-277)
- Onboarding address guard and ambient-label hardening: the agent now avoids
  accidentally using the agent's own name as the user's address before the
  user's name has been confirmed. Tone question style and button spec
  tightened. (DGN-284)
- Agent-to-agent migration request wired at onboarding completion: when a
  user migrates from another agent, the onboarding flow now dispatches a
  migration.request handoff so the source agent can forward data
  automatically. (DGN-277 f9)
- cron-register skill revision round: test-fire exception documented (skip
  full re-register when only the schedule changes and the job ran cleanly
  that day), time-rename rule documented (label/script/log must all be
  renamed together and the old job trashed), ProcessType=Interactive added to
  the template plist for macOS display-sleep safety, and the worker-script
  pattern (task script as entrypoint, push.sh called internally) documented.
  Seven previously undocumented practices backfilled in the same round.
  (DGN-292)
- upstream-report skill: agents can now file a structured framework proposal
  as a GitHub issue on the canonical repos (dogany-agent or
  claude-code-telegram routing, coolcoolk identity gate, outbox-draft
  fallback). Self-maintained repos use an internal ticket + direct fix path
  instead. (DGN-293)
- Morning brief: title-prefix exclusion is now config-driven
  (BRIEF_TITLE_EXCLUDE_PREFIXES) and off by default. Routine titles that
  match a configured prefix are hidden from the brief schedule section without
  affecting the underlying event. (DGN-323)
- Daily retro: content-experience keyword matching is now config-driven
  (RETRO_CONTENT_TITLE_KEYWORDS). Entries whose title matches a configured
  keyword generate the content-impressions question instead of the default
  productivity prompt. (DGN-326)
- Morning brief task-lane and Warg-section embeds propagated to the template.
  Timed task-kind blocks (e.g., work routine events) now appear in the
  schedule section alongside appointments. Domain-agent morning sections
  (like Warg's workout summary) are injected inline with icon rules and
  timezone-generic layout. (DGN-282, DGN-283)
- lifekit.sh path-resolution note pinned in all four bundle skills that
  invoke it. The note now clarifies that the helper should be resolved
  relative to the workspace root, not the skill directory. (DGN-321)

### Fixed
- diet-log: multi-item meal logging via --new is now documented with correct
  splitting semantics; user-language-only splitting message propagated to the
  template.
- memory-engine: rrf_score=None no longer causes a crash in search output;
  the field is now guarded and treated as zero for ranking.
- Mirror engine: abandoned-transition leak fixed. When a recurring event
  batch is replaced, the old batch is now swept for tombstone entries and the
  corresponding calendar events are cancelled, eliminating duplicate calendar
  entries. 22 regression checks added to the mirror test suite. (DGN-302)
- diet-log and workout-log: render interpreter chain now points to the shared
  render venv (~/dogany/.venvs/render) instead of the bridge venv. Fixes card
  rendering failures on fresh instances where matplotlib is absent from the
  bridge venv. Propagation completes the fix that was partially delivered in
  v1.2.0 and subsequently overwritten by a skills-bundle refresh. (DGN-194)

### Changed
- upstream-report skill rerouted for self-maintained repos: dogany-agent and
  claude-code-telegram proposals now go through an internal ticket + direct
  fix rather than a public GitHub issue. Public issues are reserved for
  third-party framework dependencies. (DGN-293 owner directive 2026-07-16)
- notify policy (DGN-273), routine notify verbset, and remind engine: routine
  events now support per-event notification preferences; silent routines
  receive no reminder pushes. Template and 55-test suite updated.
- Mirror productization (DGN-268): S1-S5 landed, covering config seam,
  display-tz default pin, bootstrap adopt-or-create guard, onboarding UX,
  Google-unified auth, delivery wiring, Linux parity, cron safety rails, and
  poll-cycle per-step exception isolation. Merge-gate final-grill items
  resolved.
- install: model-choice step revised so newly minted agents default to an
  appropriate model for their subscription tier. (DGN-281)
- Baseline agent definitions (baseline-editor, propagation-editor,
  release-closer) propagated to the template with routing pointers in
  AGENT.md. New agents minted from the template inherit the full baseline
  toolset. (DGN-181)
- Retro and brief live-ahead improvements absorbed from Ag into the template
  baseline. (DGN-261)

## [1.4.0] - 2026-07-11

### Added
- Agents now resume interrupted work automatically after a bridge restart.
  The post-restart health check scans open wip tickets and the session inbox
  and picks up where it left off without waiting for user input. (DGN-254)
- lifekit project verbs: project-list, project-add, and project-upd are now
  available as delegatable CLI verbs. Agents can read and update projects
  through the SDK layer without direct SQL or live Notion API calls, removing
  the last Notion runtime dependency from the weekly-review routine. (DGN-256)
- lifekit v6: recurrence engine, routine_projection, and routine_roller land
  on canonical. Schema migrates to user_version 6 via migration 006
  (routine_recurrence tables). Migration applies automatically on the next
  update run and is additive-only. (DGN-259)
- Mirror engine ships as a flag-gated optional module (MIRROR_MODULE=off by
  default). When off, the enqueue hook is a fully silent no-op -- no errors,
  no output, no side effects. Agents that do not use the mirror feature are
  unaffected. (DGN-259)
- Live-dashboard sync (DashboardSync) ships in the agent template baseline.
  New agents minted from the template now inherit dashboard.py and the bot
  lifecycle wiring that keeps a pinned console dashboard current. The
  dashboard_enabled flag activates on file presence; the feature is off until
  you place the dashboard config file.
- Install completion flow improved: the wizard now shows your bot handle,
  explains how to start your agent with the dogany launcher, prompts you to
  configure sleep-prevention (so the agent stays up when the laptop lid is
  closed), and shows a cron schedule summary so you know when scheduled
  routines will first fire. (DGN-250)

### Fixed
- project-add now checks for an existing same-title project before inserting (EXISTS/exit 3, --new to force); mirror-poll.sh and mirror-reconcile.sh now enforce the MIRROR_MODULE flag at runtime instead of comment-only. (DGN-260)
- update.sh now refuses to overwrite instance files whose version marker is
  ahead of the framework source. If you have applied a hotfix or run a
  cutting-edge build that has not yet shipped in a release, the next update
  will skip that file and warn you loudly instead of silently rolling it back.
  The guard applies per-file; unguarded files update normally. There is no
  --force override by design. (DGN-249)

### Security
- Vendored bridge re-pinned to a clean, reachable upstream SHA (feca63e).
  The previous pin pointed to a dangling pre-history-rewrite commit authored
  by a blocked identity; UPSTREAM.md was the only public pointer to that
  object. The pointer has been removed.

### Notes
- This release carries user_version 6 (schema 006). Existing user_version 5
  installs migrate automatically through the 006_routine_recurrence migration
  on the next update run.
- The mirror module is off by default and requires explicit opt-in
  (MIRROR_MODULE=on in lifekit.conf) plus gws (Google Workspace) credentials.
  No setup is required for agents that do not use it.
- agents/main/database/ contains a stale 1685-line lifekit.py snapshot that
  predates this release. It is not the canonical copy; the canonical is
  database/lifekit.py at repository root. Cleanup is tracked separately.

## [1.3.0] - 2026-07-10

### Added
- lifekit task CLI verbs: task-add, task-find, task-done, task-undone,
  task-reschedule, task-archive, task-overdue, task-done-between, and
  event-window. Task mutations are now fully delegatable from the agent
  to the SDK layer without direct SQL. (DGN-180)
- Schema migration 005: nullable mirror-bookkeeping columns added to the
  event table, user_version pinned to 5. Migration applies automatically
  on the next update run and is additive-only (no existing data touched).
  (DGN-180)

### Fixed
- Updating an existing installation no longer leaves BRIDGE_MODELS missing
  from the instance .env. update.sh now backfills any absent keys
  (idempotent, add-only -- existing values are never overwritten). This
  closes a 3-release known issue where pre-v1.1 installs remained on
  sonnet-only after update because the seeding added in v1.1 only applied
  to fresh installs. (DGN-246)
- Slash command list order in the Telegram command picker now reflects
  usage frequency: new, stop, model, usage, skills, resume, history,
  help. (DGN-248)

### Notes
- This release carries user_version 5 (schema 005) and the full task CLI
  surface. It is the version-precondition for domain-agent minting: the
  Warg pilot requires a tag that carries 005 / user_version 5.

## [1.2.1] - 2026-07-09

### Added
- Agents now receive the results of background autonomous-loop runs as live
  session turns, so the agent knows what happened without waiting for you to
  ask. Quiet runs (nothing actionable) are suppressed and do not generate a
  notification. (DGN-217)
- After a bridge restart completes, the newly resumed session automatically
  verifies that the bridge is healthy and reports back only if something looks
  wrong. Routine restarts are now silent end-to-end. (DGN-226)
- Release preflight tool: before any release ships, a diff of the live agent
  against the canonical template is run and every divergence must receive an
  explicit verdict. Unreviewed live fixes can no longer be silently overwritten
  by an update. (DGN-225)

### Fixed
- /usage fallback display had a missing opening bracket in the Live Rate-Limit
  label; restored. (DGN-203)
- update.sh and self-update.sh now consume published release tags instead of
  pulling from main HEAD. Installing an update can no longer silently deliver
  unreleased development commits. A DOGANY_UPDATE_CHANNEL=main escape hatch is
  available for development instances. (DGN-221)
- install.sh now pins a fresh clone to the latest release tag before the setup
  wizard runs, so new installations start from a stable baseline rather than
  an arbitrary main HEAD. (DGN-221)
- Appointments whose start time falls between midnight and 09:00 local time
  (KST) no longer appear one day early in the morning briefing. The root cause
  was a date-bucketing query that applied UTC date() to locally-stored
  timestamps; the unified event schema (shipped in this release) resolves it
  structurally. (DGN-179 / DGN-220)
- Event schema upgraded to user_version 4: the event_persons junction table
  (appointment participants) and the appt_find/appt_show facade are now fully
  rewritten over the unified event table. Appointment queries are timezone-aware
  end-to-end. Migration 004 applies automatically on the next update run.
  (DGN-179 verb-delta v2)
- Framework updates no longer revert the bridge's launchd label and agent
  prefix back to generic placeholders, which previously caused self-restart to
  target the wrong service. Both values are now mint-time placeholders that
  survive update.sh. (DGN-213)
- Outgoing file transfers that time out now retry twice and send a user-visible
  notice on final failure, instead of silently discarding the file. (DGN-218)
- The memory-search skill now enforces a gate before the agent may claim that a
  value is not recorded: the agent must search first. This closes a gap where
  the agent would ask the user for data that was already in its own consolidated
  memory. (DGN-223)
- lifekit: workout sessions are now returned correctly from load_card_data, and
  the hook-effective burn macro is applied so morning brief calorie targets
  reflect actual workout output. (DGN-193)
- Transient Telegram send timeouts (ReadTimeout from the Telegram API) now show
  a friendly retry message instead of a raw "Error: Timed out" error string.
  (DGN-063)
- Single .env generator: secrets (bot token, email password) now travel only
  via environment variables, never through process arguments, closing a
  potential credential exposure in process listings. (DGN-096)

### Changed
- DGN-220 (appt_find UTC date-shift hotfix) is closed as superseded: the
  structural fix in DGN-179 covers the same bug for all users on this release.

## [1.2.0] - 2026-07-08

### Added
- New `/usage` command in Telegram: shows your live Claude rate-limit status
  as ASCII progress bars (5-hour window, weekly limit, per-model) with a
  countdown to the next reset. Output is localized (ko/en) to match your
  agent's language setting. Use `--full` flag for the detailed cache report;
  the default is the compact live view only.
- Self-update routing: agents now have a documented `self-update` workflow
  that consumes a published framework release without triggering a new
  release. The `routines/self-update.sh` script ships in the template so
  every minted instance inherits it.

### Fixed
- update.sh now requires a minted instance config (`.instance.conf`) before
  running and shows a preflight confirmation prompt. Bare invocations that
  previously silently targeted the wrong directory are now blocked upfront.
- update.sh AGENT_LANG lookup is now guarded against silent death under
  `pipefail`: a missing key in `agent.conf` no longer kills the update with
  no error message.
- Self-restart completion notice propagated to the `.template` baseline, so
  newly minted agents send a proactive Telegram notice when a bridge restart
  completes (previously users had to ask).
- `cleanup-files` routine no longer exits with an error code when the outbox
  or tmp directory is non-empty. A `set -e` footgun in the conditional log
  path was treating a false branch as a non-zero exit, causing a spurious
  "ROUTINE FAILED" notification every day once files accumulated.
- Event 3-layer SDK (task + appointment unified under `event`) landed on
  canonical: schema DDL, Python data-access layer, and migration script
  (DGN-178/179 P0). Fixes cross-agent data arbitration and time-slot
  ownership for multi-agent deployments.

## [1.1.0] - 2026-07-07

### Added
- update.sh now refreshes the framework constitution (RULES.md) and core shared
  services on every update, with the same user-edit detection and backup contract
  as dogany-* skills. A services manifest controls the exact refresh list; your
  AGENT.md and USER.md are never touched.
- Browser automation skill (agent-browser, Vercel Labs) ships as a default-dormant
  bundle skill. The skill is inactive unless the user opts in during install.
- Install wizard step 4c: optional browser automation opt-in. Discloses the
  Chrome for Testing download size (~684 MB) and that the agent-browser CLI will
  be installed via npm. Default answer is No.
- When opted in, the skill is activated by creating a symlink from
  .claude/skills/agent-browser into the bundle directory after the agent is minted.
- The DOGANY_BROWSER=1 env knob enables the opt-in in dry-run and scripted mode.
- Cron/routine failure visibility: all scheduled routines now run through a
  cron-guard wrapper. When a job exits with a non-zero code, a push notification
  is sent with the label, exit code, and log tail (one notification per label per
  day; repeats are suppressed).
- Skill display-name layer: skills can now declare a user-facing display name in
  their SKILL.md frontmatter, which the agent uses in menus and confirmations
  instead of the raw skill directory name.
- task-update skill gains three new verbs: reschedule, archive, and overdue.
  Tasks are now owned by lifekit.py (no direct SQL in the skill script). A
  schema migration adds the archived_at column for soft-delete support.
- Installer now seeds BRIDGE_MODELS into the instance .env based on your Claude
  subscription tier, so the /model picker shows the correct model options from
  the first session.
- Opt-in remote version check: set DOGANY_VERSION_CHECK=1 in your instance .env
  to receive a one-line notice when a newer Dogany version is available. Off by
  default; no data is sent beyond a plain version fetch.

### Fixed
- Bridge turn-death safety net: when a conversation turn ends abnormally (e.g.
  laptop sleep mid-turn), the agent now sends a user-visible notice instead of
  silently discarding the message. Includes inbound download retry for interrupted
  file transfers.

## [1.0.5] - 2026-07-06

### Added
- Bridge watchdog: a lightweight monitor checks the bot's polling heartbeat
  every 2 minutes and restarts the service when it goes zombie (alive but
  deaf). Two-strike design absorbs laptop sleep/wake; restarts are
  rate-limited so a deeper failure never causes a restart storm.
- Windows support via WSL2 (preview): setup script, install guard, and docs.
- The installer now auto-installs missing prerequisites (Homebrew, Python
  3.11+, git, Claude Code CLI) after a single confirmation, instead of
  failing one by one with manual instructions.
- Heavy downloads now run at the START of the install wizard, and large
  model downloads show live progress (native progress bars or an elapsed
  heartbeat) instead of a silent, frozen-looking screen.
- The agent remembers your model choice: a new session starts with the model
  the last session actually used, with a safe fallback chain (settings files,
  then default) when the remembered value is missing or invalid.

### Fixed
- update.sh no longer resets your model choice, leaves stale service files,
  misregisters backups, races during file replacement, or strips executable
  permissions (the last one could silently kill scheduled routines like
  morning briefings).
- Outgoing messages are scrubbed of internal tool-call markup that could
  occasionally leak into chat text.
- Appointment logging now checks the target date for existing entries before
  registering, preventing duplicate appointments.

### Changed
- Agents now propose skill updates at task completion when they had to
  deviate from a documented procedure (skill-feedback gate).
- Output rules: agents describe results in your terms and no longer expose
  internal mechanics (script names, API calls) in chat.

## [1.0.4] - 2026-07-04

### Added
- Onboarding now asks what you want your agent to be: a general life
  assistant or an agent with a specific role you describe.
- Your answer seeds the agent's primary focus in plain prose, so the
  agent starts out already oriented toward what you hired it for.

## [1.0.3] - 2026-07-04

### Added
- Arrow-key selection menus in the install wizard -- pick options with
  the arrow keys instead of typing numbers.
- Machine-aware model recommendations: the installer checks your RAM
  and free disk and recommends local models (embeddings, speech-to-text)
  that actually fit your machine.
- Claude token liveness check during install: a dead or invalid token
  is caught before setup finishes, not after.

### Fixed
- Reinstall guard: a stale marker from a deleted or moved installation
  no longer blocks a fresh install (it now self-heals).
- Removed outdated tier wording from installer messages.

## [1.0.2] - 2026-07-04

### Added
- New `dogany` command-line launcher for starting and managing your agent.

### Fixed
- Install wizard fixes from real-world install testing.
- Consistent generic labels in photo/voice message prompts (Korean).

### Changed
- Setup docs now recommend cloning into your home folder and warn
  against macOS-protected folders (Documents/Desktop/Downloads).

## [1.0.1] - 2026-07-03

### Added
- Optional semantic-memory step in the installer: install Ollama with
  the bge-m3 embedding model (~1.2GB) for cross-lingual memory recall,
  or skip it and use keyword search only.
- Manual timezone input is now validated (with retries) during install.

### Fixed
- Scheduled routines now convert your local times to the system clock,
  so they fire at the right time on servers set to a different timezone.
- Documentation now accurately describes uptime behavior and the
  semantic-recall dependency.

### Changed
- Refreshed Korean translations across the install and chat experience.

## [1.0.0] - 2026-07-03

Initial public release.

- A personal AI agent that lives on your machine and talks to you over
  Telegram, powered by Claude.
- Long-term memory: nightly consolidation of conversations plus
  semantic recall, so the agent remembers what matters to you.
- A skill system the agent uses (and extends) to do real work:
  reminders, scheduled routines, proactive messages, file handling.
- Optional life-management bundle (diet, workout, appointments,
  morning brief, daily retro) you can switch on conversationally.
- Guided installer with English and Korean support, from clone to a
  running agent in one session.
- Licensed under Apache-2.0.

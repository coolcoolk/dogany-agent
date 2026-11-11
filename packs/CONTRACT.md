# Dogany 설치 단위 계약 v1 (정본)

`contract_version: 1` — 이 문서는 Dogany 설치 단위(킷/모듈/스킬/에이전트팩)가 프레임워크와 맺는 계약의 **정본**이다.
제품 어휘(dec-425): **에이전트팩**은 에이전트의 정체를 담은 묶음, **모듈**은 붙이는 능력,
**킷**은 바탕(DB 규약)이다. 킷은 에이전트팩이 아니다. **설치 단위**는 킷·모듈·스킬·에이전트팩의
배포·설치·버전 단위를 묶어 부르는 말이다. **에이전트**는 실행 중인 인스턴스만 가리킨다.
이 제품 어휘는 기존 매니페스트 `kind: kit|pack`, 식별자·경로·CLI를 바꾸지 않는다.

서드파티 저작자는 이 문서만으로 설치 단위를 저작·발행·설치할 수 있어야 한다.

- **근거는 코드다.** 이 문서의 모든 주장은 실행 코드의 파일:라인을 각주로 단다.
  문서와 코드가 어긋나면 그것은 문서 버그이며, 코드가 이긴다.
- 기준 코드: dogany-agent **v1.43.0** (`VERSION`), 실측일 2026-08-24.
- 주 강제기 3종:
  - `scripts/pack/compat-lint.sh` — 계약 검증기 (발행측 lint + 설치측 게이트)
  - `scripts/pack/pack_install.sh` — 설치기 (인라인 이중검증 포함)
  - `scripts/pack/pack_publish.sh` — 발행기 (정제 게이트 + NM3 봉인)
- 보조 강제기: `update.sh`(프레임워크 업데이트 시 역방향 가드),
  `scripts/pack/update_plan.sh` / `update_apply.sh` (통합 업데이트 루프),
  `install.sh` (공개 설치 메뉴), `scripts/pack/lib/semver_range.py` (semver 단일원).

목차:
1. 설치 단위의 계약 클래스와 계약 진입 조건
2. 매니페스트 필드 전수 표
3. 폐기 어휘 (쓰지 마라)
4. compat-lint 게이트 슬롯 전수 표
5. 설치·업데이트 게이트
6. 발행 권한과 서명 (D-D)
7. 채널·태그·버전 lockstep
8. v1 계약 밖 / 미정
9. 동결 선언과 개정 절차
10. Base kit declaration (v2.7, dec-157) -- English
부록 A. 설치 단위 실물 3종 대조 실측

---

## 1. 설치 단위의 계약 클래스와 계약 진입 조건

설치 단위의 계약 정보는 `<pack-repo-root>/pack-manifest.json` 하나가 선언한다.
(설치기는 manifest 없는 설치 단위를 거부한다: "every pack must declare its categories -- no legacy fallback",
`pack_install.sh:373-374`)

계약 클래스는 2종뿐이다 (`compat-lint.sh:344-371` C-KIND):

| kind | 뜻 | 대표 실물 |
|---|---|---|
| `kit` | DB 레인·스키마·CLI 계약면을 **제공**하는 킷 | lifekit |
| `pack` | 에이전트의 정체(역할·지식·화법·일하는 방식)를 담은 에이전트팩. 킷 위에서 동작하며 DB 레인을 **소유하지 않는다** | health-trainer, dev |

진입/우회 경계:

- **매니페스트 파스 게이트**: 파일이 존재하는데 JSON 오브젝트로 파스되지 않으면
  즉시 C1 FAIL + ABORT (fail-closed; 손상 JSON은 legacy 에이전트팩이 아니다). `compat-lint.sh:161-181`
- **legacy-grace**: `contract_version` 부재 **AND** `kind != "kit"` **AND** (`name`, `reference_slug`)
  쌍이 `scripts/pack/legacy-grace-allowlist.tsv` 에 등재된 에이전트팩만 구계약(pre-v2) 에이전트팩으로 간주, 검사 전체
  SKIP 후 exit 0. 목록 밖에서 `contract_version` 이 없으면 C1 FAIL + ABORT (DGN-1498: 부재만으로
  면제되면 신규 에이전트팩의 우회로가 된다). 목록은 닫혀 있다 -- 신규 에이전트팩은 행을 추가하지 않고
  `contract_version` 을 선언한다. **킷은 grace 없음** — contract_version 없는 kit은 항상 FAIL.
  grace 통과 시 compat-lint 가 `GRACE:` 한 줄을 내고, 설치기가 이를 receipt 의 `lint_grace=` 로
  남긴다 (사후 판독: `grep -l '^lint_grace=' config/packs/*.receipt`).
  `compat-lint.sh` legacy-grace 블록 + `lib/legacy_grace.py` (설치기의 동일 경계: `pack_install.sh:444-449`)
- 설치 경로 분기: `kind=kit` 또는 (`kind=pack` AND `contract_version` 존재) → 계약(payload_root)
  파이프라인. 둘 다 아니면 legacy agent/module 경로. `pack_install.sh:828-841`
- `"agentpack"`은 **레포 명명 세그먼트**이지 manifest kind가 아니다. kind에 쓰면
  C-KIND FAIL + ABORT. `compat-lint.sh:57-64,356-368`

## 1.1 킷 축 설계 원칙 (선언용 -- lint 강제 없음)

킷의 **축 개수는 킷 속성**이며 계약이 고정하지 않는다(DGN-1413). "모든 킷은 N축"이라는
문장은 이 계약 어디에도 없고, 있어서도 안 된다.

판별 질문: **이 킷의 실행 주체는 누구인가. 에이전트가 모르는 것이 무엇인가.**
그 답만큼 축이 생긴다.

실측 사례(2026-09-12 재확인):

```
devkit    2축 (분할·실행)        payload/database/schema.sql -- metric/measure 계열 테이블 0건
lifekit   3축 (분할·실행·측정)   kit_life.db -- metric_axis(7행) / metric_def / metric_log / measure 실재
```

베이스킷이 주는 것은 **축 목록이 아니라 축 슬롯 + 위 판별 질문**이다.

이 절은 §8 의 다른 선언용 필드(`deploy_owner` 등)와 동일하게 **기계 강제가 없다** --
킷 저자를 위한 설계 규율이며 compat-lint 슬롯을 열지 않는다.

## 1.2 Kit roles and optional agent packs

**Role canon (DGN-2034, dec-400):** Kits declare roles only (leader/staff); agent packs are optional ways of working that require a kit; a kit never requires a specific leader agent pack. The lifekit leader is a generalist base agent, and specialist agent packs join as members. The PO agent pack is the devkit 0.1 default way of working, not a required leader agent pack.

Kit manifests must not carry `requires_pack` or `leader_pack`, even with an
empty or null value. In the canonical repository (the check is not shipped
in the public mirror), check manifests with
`python3 scripts/pack/kit-role-lint.py <pack-manifest.json> [...]`.
This focused lint checks the role boundary only; it does not replace
`compat-lint.sh`. The repository selftest checks shipped manifests and
exercises both forbidden fields (`tests/dgn2034_kit_roles_selftest.py`).

## 1.3 Kit vocabulary (DGN-2145)

Every kit manifest declares either `"terms": "config/terms.json"` (relative
to `payload_root`) or `"terms": "none", "terms_reason": "<reason>"`.
`compat-lint.sh` C-TERMS refuses a missing declaration, a missing/invalid terms
file, or `none` without a nonempty reason. `none` cannot accompany a terms
file. Domain packs may continue to omit the declaration.
Side: a missing declaration is refused publish-side (gate (d)); install-side
it is a SKIP, because every kit tag published before DGN-2145 lacks it and the
installer cannot add it (the C-RELNOTE precedent, DGN-2149). A declaration
that is present is validated on both sides.

Create a draft kit with `python3 scripts/pack/scaffold.py DEST --id kit_example
--kind kit --terms-file FILE`, or replace the last option with
`--no-terms-reason "This kit defines no domain vocabulary"`. The scaffold
validates vocabulary before writing and refuses an existing destination.

For a kit entry with a Latin canonical, `particle_class` records its Korean
pronunciation ending: `vowel` (merge/머지, build/빌드), `consonant`
(commit/커밋), or `rieul` (PR/피알). Along with `rewrite: true` and recorded
approval, it permits Korean-to-Latin replacement and particle repair:
`병합을` → `merge를`, `병합으로는` → `merge로는`. Rieul takes `을/이/은`
but `로`. Bare nouns and listed phrases may also be replaced. Missing class
leaves Korean-to-Latin entries count-only. This exception applies to kit
layers only; context-gated entries remain count-only, and exceptions,
contrast sentences, quotes and code retain their existing protections.

## 1.4 Kit-readable facts (DGN-2165)

A kit may declare facts the agent can read before asking the owner. With
`KIT=factkit` in `config/agent.conf`, the owner-ask Stop gate reads the optional
`READABLE_FACTS` key in `config/factkit.conf`. The value is a single-line,
single-quoted JSON array; each entry has a `kind` token (`[a-z][a-z0-9_]{0,63}`)
and a nonempty `read_hint` string:

```sh
READABLE_FACTS='[{"kind":"schedule","read_hint":"database/factkit.sh schedule yesterday; database/factkit.sh schedule today"},{"kind":"roster","read_hint":"database/factkit.sh roster today"}]'
```

Ship localized fact nouns as regex strings under `owner_ask.fact.<kind>` in
the kit's existing `config/i18n/<lang>.bundle.json` payload. For example,
English `owner_ask.fact.schedule` can be `"\\b(?:schedule|appointments?)\\b"`.
The normal kit i18n installer records these in
`config/packs/<id>.i18n.<lang>.json`; ordinary layer precedence applies.
Both Korean and English tables are checked, including mixed-language replies.
Framework `owner_ask.fact_request` and `owner_ask.fact_private` keys supply
owner-request verbs and specific private/status clauses; domain nouns and
read commands are never hardcoded into the classifier.

A declared-fact supply/list/check/send ask blocks even with an empty or
unreadable tool trace, after web fetches, or after a failed kit read. Its retry
includes the declared read hint: use kit context, accept an empty result, or
say that the fact could not be read without assigning the read to the owner.
The gate does not execute the hint or claim that a read succeeded. Named-item
done/defer questions, roster corrections and specific unknown private values
remain allowed. A private clause does not exempt a separate collection ask.
Owner-session scoping, record/off modes, the owner-ask gate switch and the
once-per-turn retry guard continue to apply.

No declaration means no behavior change. Invalid entries, missing localized
nouns or invalid regexes are skipped; they do not disable the source-search
gate. Implementation: `agents/.template/routines/lib/kit_readable_facts.py`,
`routines/lib/output_gate.py:owner_ask_verdict`, and
`routines/owner-ask-stop.py`. A two-kind fixture kit lives at
`agents/.template/routines/tests/fixtures/readable-facts-kit/`; its Stop-hook
regressions extend `routines/tests/test-owner-ask-gate.py`.

## 2. 매니페스트 필드 전수 표

compat-lint / pack_install이 실제로 읽고 강제하는 필드의 전수. "필수"는 계약 설치 단위
(contract_version 보유) 기준.

| 필드 | 필수/선택 | 문법 | 위반 시 잡는 게이트 |
|---|---|---|---|
| `terms`, `terms_reason` | kit 필수 | `config/terms.json` 또는 `none` + 비어 있지 않은 사유 | C-TERMS (`scripts/pack/lib/kit_terms.py`); pack 선택 |
| `contract_version` | 필수 (계약 설치 단위 정의 그 자체) | 정수 (현행 `1`) | 부재: kit이면 C1 FAIL, pack이면 legacy-grace로 강등 `compat-lint.sh:293-295,199-207` |
| `pack_version` | 필수 | `^[0-9]+\.[0-9]+\.[0-9]+$` | C1 `compat-lint.sh:290-291,301-305` |
| `requires_framework` | 필수 | semver range (§7 문법) | C1 (문법) `compat-lint.sh:296-298,308-312`; C2 (만족성) `compat-lint.sh:619-635`; 프레임워크 업데이트 시 fw_reqframework_guard `update.sh:1467` |
| `kind` | 필수 | 정확히 `"kit"` \| `"pack"` | C-KIND FAIL+ABORT `compat-lint.sh:344-371`; 설치측 인라인 재검증 `pack_install.sh:889-892` |
| `id` | 사실상 필수 (카탈로그 resolve·D-D 대조 키·pack의 config 소유자명) | 문법 강제 **없음** (§8 참조) | D-D 대조 `compat-lint.sh:1282-1297`; config/`<id>`.conf 소유 `compat-lint.sh:852-855`; requires_kit 자기의존 비교 `compat-lint.sh:584-590` |
| `payload_root` | 선택 (기본 `"payload"`) | 설치 단위 경계 내부 경로 | 경계 이탈 = C1 FAIL+ABORT `compat-lint.sh:640-658` |
| `provides_kit` | **kind=kit 필수** (DGN-1143: legacy 기본 `"lifekit"` **제거** — 의존 모집단 실측 0건이었고, dec-145 개명 후 폴백은 존재하지 않는 킷을 조용히 가리킨다) | `^[a-z][a-z0-9_-]{0,31}$` + 예약어 금지 (§2.1) | **부재 = C1 FAIL+ABORT** (DGN-1143); 위반 = C1 FAIL+ABORT; **kind=pack이 선언하면 C1 FAIL** (S4b 대칭: provides_kit ⇔ kind=kit); 설치측 lockstep `pack_install.sh` KIT_NAME 블록 + `update_apply.sh` 크루 eff_kit 검증 |
| `capabilities` | 선택 | 객체, 허용 키는 `db_lane` (bool) / `schedules` (bool, DGN-1402) 뿐 | malformed = C1 FAIL `compat-lint.sh:426-475`. `db_lane`: kit 부재/true → DB 레인 on, false → C3/C6 SKIP(+모순검사); pack 부재 → false, **true 선언 = C1 FAIL** (킷 전용면). `schedules`: kit 부재/false → 스케줄 배송 표면 없음(오늘과 동일), true → `routines/*.plist`+`plists.defer` 가 C4 를 통과(아래 §2.2); pack **true 선언 = C1 FAIL** (킷 전용면, 죽은 선언도 FAIL) `compat-lint.sh:479-565`; 설치측 twin `pack_install.sh` capabilities 블록(kit)/pack 인라인 검증(pack) |
| `service_namespace` | kind=pack 선택 | provides_kit과 동일 문법 (재사용, 새 문법 없음) | kind=kit 선언 = C1 FAIL (이중 진리원) `compat-lint.sh:512-514`; requires_kit.kit과 동일 = FAIL (의존킷 사칭) `compat-lint.sh:521-522`; 선언했는데 `payload/service/<ns>/` 부재 = FAIL (죽은 선언) `compat-lint.sh:663-665`; 설치측 twin `pack_install.sh:907-921,952-954` |
| `knowledge_warehouse` | Optional warehouse token | Same token grammar as `provides_kit`. An agent pack must ship the matching frame directory; a kit must also declare `knowledge_modules` and must not ship knowledge payload. |
| `knowledge_modules` | Optional nonempty array of distinct module tokens | Requires `knowledge_warehouse`; registry domain slugs only, no item-less `cross-cutting`. Kits consume verified modules only. Declaring agent packs deliver the versioned frame and only declared health bootstrap items under `.pack-items/`; top CHANGELOG.yaml/GAPS.md belong to the view, not the frame. See `docs/dgn1893-knowledge-handoff.md`. |
| `requires_kit` | kind=pack 선택 | 객체, 키는 정확히 `{kit, range}`; kit은 provides_kit 문법, range는 semver range | 형식 = C-KITDEP `compat-lint.sh:540-616`; **kind=kit 선언 = FAIL** (kit→kit 의존 미정의=거부) `compat-lint.sh:572-574`; 자기의존 = FAIL `compat-lint.sh:584-590`; **만족성은 설치측 전용** (§5-1) |
| `requires_modules` | kind=pack 선택 (DGN-1391) | 모듈 토큰 문자열의 JSON 배열, 중복 금지, 빈 배열 금지. 토큰 문법 = §2.1 킷 토큰 core 재사용 (예약어 목록은 미적용 — 그 목록은 payload 경로 사칭 방어용이고 모듈 토큰은 이 변경에서 경로를 가리키지 않는다) | **설치측 전용** 게이트 (§5-1-1b). 형식 손상 / `requires_kit` 없이 선언 / 지명한 모듈이 의존 킷의 로스터에 없음 = 전부 BLOCK. compat-lint 슬롯은 열지 않았다 (C-MODDEPS 예약 유지, §3) |
| `modules` | kind=kit 선택 (DGN-1391) | 모듈 토큰 문자열의 JSON 배열 = **이 킷이 담는 모듈 로스터** | 킷 자신에 대한 강제는 없다 (선언). 소비자는 `requires_modules` 게이트 하나 — 로스터가 없는 킷은 어떤 `requires_modules` 참조도 만족시킬 수 없다 (§5-1-1b) |
| `skills` | 선택 | `[{"name": "<payload skills-bundle 디렉터리>", "sharing_mode": "share"\|"own", "winner": "<pack-id>" (선택, DGN-1143)}]` | C7: 어휘/중복/죽은 선언/템플릿 충돌 + winner 문법 `compat-lint.sh` C7 블록; C4 allowlist 확장; 설치 동작 §5-6. **`winner`**: 같은 스킬 이름을 둘 이상의 설치 단위가 배송할 때(설치 단위 간 충돌) 정본 배송자를 선언 — 충돌 게이트(`lib/skill_collision_check.sh`, pack_publish GATE (e) + pack_install 인라인)는 **배송하는 모든 설치 단위가 같은 winner(배송자 중 하나)를 선언할 때만** 통과. 발행측(strict)에서는 충돌 없는 스킬의 winner 선언 = FAIL(죽은 선언). 판정 출력에 모수 필수: `skills checked: N, collisions: M` (N=0 은 "대상 없음/n-a" — 통과 문면과 갈림, DGN-1142 §3.1) |
| `hooks` | 선택 (양 클래스 + legacy 공통, DGN-1496) | `[{"event": "<Claude Code 훅 이벤트>", "script": "routines/bundle/<파일>.py\|.sh", "timeout": <초, 선택, 기본 10>}]` — 설치기가 **선언 그대로** `.claude/settings.json` 에 건다 (파일명·이벤트를 설치기가 모른다; 스크립트 내용 해석 금지 = 층 경계). 이벤트 어휘: PreToolUse/PostToolUse/UserPromptSubmit/Notification/Stop/SubagentStop/PreCompact/SessionStart/SessionEnd | C-HOOKS (선언 시점 loud FAIL: 어휘 밖 이벤트 / 절대경로·`..` / `routines/bundle/` 밖 = **경로 규약 위반을 이름 붙여 거부** — C4가 파일을 막고 설치기가 file-not-found를 내는 이중 증상 방지 / payload 부재 죽은 선언) `compat-lint.sh` C-HOOKS 블록; 설치측 인라인 twin + 멱등 배선 `pack_install.sh` `_hooks_validate`/`_hooks_wire` + census `hook_wiring`; legacy 경로 STEP 5 동일 헬퍼 (표면만 flat `routines/*.py\|.sh` — legacy STEP 2가 배달하는 실물) |
| `units` | kit 권장 (SHOULD) | `{primary, set}` 객체 | 부재 = **WARN만** (FAIL 아님; 표시가 unit.generic으로 폴백) `compat-lint.sh:318-330` — lifekit 실물 불일치는 부록 A |
| `status` | 발행 설치 단위 필수 | 설치 가능 상태는 `"published"` 하나 | 설치측 D-D 게이트 (§6) `compat-lint.sh:1276-1297` |
| `completion_verb` | kind=kit 선택 (DGN-1374) | 비어있지 않은 문자열 (킷이 이미 싣고 있는 CLI verb 이름 하나) | 설치측 **전용** (top-level, additive — capabilities lockstep 미적용). `capabilities.db_lane=false`와 동시 선언 = 모순 거부(C3 스킵밀수와 동형) `pack_install.sh:1762`. **설치기는 이 verb를 절대 실행하지 않는다** — 이름은 사람이 읽는 보고문에만 쓰인다. 완료 여부는 `$ROOT/config/packs/<id>.complete` 마커 파일의 존재로만 판정(추론 금지 — NULL/0으로 유추하지 않는다); 마커는 그 verb 자신이 성공 시 남기는 것으로, 이 계약이 규정하는 범위 밖이다. 이번 설치 run이 kit_core payload를 새로 안착시켰다면(§5-1 reverse-drift guard가 PROCEED 판정) 기존 마커는 무효화(제거)되어 재확인을 요구한다 — 설치 성공(exit 0, 롤백 없음)과 "complete" 보고는 별개 축이다. dry-run은 같은 코드경로로 판정만 하고 마커를 절대 쓰거나 지우지 않는다. `pack_install.sh:1735-1766,1967-1979,2201-2226` |
| `handover_brief_verb` | kind=kit 선택 (dsp hslot, 오너 재정 2026-09-18) | 비어있지 않은 문자열 (킷이 이미 싣고 있는 CLI verb 이름 하나 — `database/<kit>.sh`의 verb, C6가 스모크하는 그 CLI 표면) | 설치기·lint 강제 **없음** (top-level, additive — `completion_verb`와 같은 레인). 소비자는 민팅 흐름 하나: `mint_run.sh handover-brief` 가 민팅 **이전에** 리더 인스턴스의 설치된 킷 CLI로 이 verb를 실행해 "새 에이전트가 무엇을 이어받는지"를 킷 자신의 문장으로 받는다 — 프레임워크는 킷 스키마를 모른다(테이블·컬럼·SQL 0). 반환 문안은 오너 대면 카피이므로 렌더 전에 `scripts/user-gate-lint.py --kit-text` (C3/C5/C6/C7 + estate 카피 규칙) 판정을 통과해야 하며, **부재·무응답·오류·타임아웃·판정 탈락은 전부 "화면 없음"으로 강하**한다(절대 에러도 경고도 아니다 — 부재는 정상). verb 실행은 이 계약이 규정하는 범위에서 **읽기 전용이어야 한다** (민팅 전 안내 화면이 데이터를 바꾸면 안내가 아니다) |
| `vocab_verb` | kind=kit 선택 (DGN-1694) | 비어있지 않은 문자열 (킷 CLI `database/<kit>.sh` 의 verb 이름 하나) | 설치기·lint 강제 **없음** (`handover_brief_verb` 와 같은 레인). 소비자는 음성 어휘 리더 하나: `routines/lib/voice_vocab.py` 가 `<verb> --json` 을 실행해 `[{"term", "last_used"}]` 를 받는다 -- 프레임워크는 킷 스키마를 모른다. 부재·오류·타임아웃·형식 불량은 전부 "킷 어휘 없음" 으로 강하(침묵). 읽기 전용이어야 한다. 전문: DGN-1694 kit vocabulary contract (canonical repo) |
| `tone_candidates` | 선택 (양 클래스 + legacy 공통, DGN-1632 W3) | 언어 키(`^[a-z]{2}$`) → 후보 3~4개 배열 객체: `{"ko": [{"label": "<형용사> 스타일", "sample": "<그 말투의 한 줄 샘플>"}, ...], "en": [...]}`. 각 후보는 정확히 `label`/`sample` 두 키, 둘 다 비어있지 않은 한 줄 문자열. `ko` 라벨은 `<형용사> 스타일` 형식(`-형` 금지). `sample` 안의 `{address}` 토큰은 에이전트가 Q3에서 오너가 고른 호칭으로 바꿔 말한다(토큰 자체는 노출 금지) | 설치측 **전용** (compat-lint 슬롯 없음, `completion_verb` 선례): 위반 = 설치 FAIL, 기록 없음 `pack_install.sh` `_tone_candidates_record`. 설치 시 `config/packs/<id>.tone_candidates.json` 으로 기록(부재/null = 기존 기록 제거; `post_mint_next` 기록과 같은 규율). 소비자는 새 에이전트 온보딩 하나: SessionStart `routines/onboarding-check.py` `resolve_tone_candidates` 가 인스턴스 언어의 목록을 Q4(말투) 후보로 **순서·문구 그대로** 주입한다. 이 언어 목록이 없거나 형식 불량이면 프레임워크 일반 후보 4개로 강하(침묵). 여러 설치 단위가 선언하면 파일명 정렬 첫 유효 기록(결정적 타이브레이크 -- 킷 레인 오선언) |
| `event_handlers` | 선택 (양 클래스 + legacy 공통, v2.7 T6 P4 / dec-319) | `[{"kind": "<토큰 ^[a-z][a-z0-9_]{0,63}$>", "command": "<payload 기준 상대경로 = 설치 후 인스턴스 기준 경로>"}]` -- 앱 수신기 이벤트(예: 앱이 제공하는 kind)를 이 설치 단위가 받겠다는 선언. 프레임워크에는 kind -> 설치 단위 매핑이 없다: 동반 파일 `callback.events` 는 설치된 선언의 합집합 + 프레임워크 구독 kind (`scripts/dogany-event` `FRAMEWORK_KINDS`: corelocation `visit_arrival` / `visit_departure`, DGN-1825 -- 선언한 설치 단위가 없으면 디스패처가 로그만 남기고 exit 0 으로 드롭)이다. dedup 키에는 항상 kind 가 들어간다 (같은 id 의 도착 / 출발은 두 이벤트) | 설치측 **전용** (compat-lint 슬롯 없음, `tone_candidates` 선례): 형식 위반 / 절대경로·`~`·`.`/`..`/빈 세그먼트 / 설치된 인스턴스에 command 부재 = 설치 FAIL, 기록 없음 (`pack_install.sh` `_event_handlers_record` -> `scripts/dogany-event record`). 설치 시 `config/packs/<id>.event_handlers.json` 으로 기록(부재/null = 기존 기록 제거). 소비자는 `~/.dogany/bin/dogany-event` 디스패처 하나: 같은 stdin (수신기 실물 = `{source, kind, id, event_id}` 뿐 -- 핸들러는 이를 id만 실은 "가서 당겨라" 신호로 읽고 레코드는 스스로 당긴다; 수신기는 새 id에만 발화하고 갱신에는 발화하지 않으므로 갱신 이벤트에 기대는 핸들러 금지), cwd = 인스턴스 루트, 핸들러별 예산(레코드 필드 `timeout_s`, 기본 5 / 최대 15, 수신기 exec timeout 아래로 캡; `claim_required: true` 항목은 그 kind 를 소유한 인스턴스만 수신, 팩 제거/kind 삭제 시 소유 kind 는 release, FDGN-591); 실행 시점에 실경로가 인스턴스 루트 밖이거나 실행 불가이거나 group/other 쓰기 가능하면 거부(재시도 대상 실패). 핸들러는 수락 시 exit 0 -- 0이 아니면 수신기가 재시도하고, 이미 수락한 핸들러는 dedup 원장이 건너뛴다 |
| `base_contract` | 선택 (양 클래스 + legacy 공통, DGN-2028) | `{"version": 1, "defaults", "targets", "bindings", "providers", "workflows"}` -- 전문 §10-5 | 설치측 **전용** (compat-lint 슬롯 없음, `event_handlers` 선례): 형식 위반 / 설치된 인스턴스에 provider adapter 부재 = 설치 FAIL, 기록 없음 (`pack_install.sh` `_base_contract_record` -> `agents/.template/routines/lib/base_contract.py record`). 설치 시 `config/packs/<id>.base_contract.json` 으로 기록(부재/null = 기존 기록 제거) |
| `deploy_owner` | 선택 | 자유 문자열 | **코드 소비자 0 — 기계 강제 없음** (선언용; §8) |
| `name_en` / `name_ko` 등 표시 필드 | 선택 | 자유 | 강제 없음 (카탈로그/메뉴 표시용) |

legacy(agent/module 경로) 전용 필드 — 계약 설치 단위 파이프라인에서는 사용되지 않는다:
`reference_slug`, `reference_root`, `reference_home`, `agent_md_marker`, `agent_conf_marker`,
`domain_seed`, `categories`, `knowledge`. 정의: `pack_install.sh:49-70`.
(주의: `knowledge`는 legacy 전용, `knowledge_warehouse`는 계약 설치 단위 전용 — 이름이 비슷하지만
서로 다른 필드이며 어느 쪽도 다른 경로에서 읽히지 않는다.)

### 2.1 킷 토큰 문법 (단일원)

`provides_kit` / `service_namespace` / `requires_kit.kit` 세 필드가 같은 문법을 공유한다
(새 문법 발명 금지 — 재사용 규율). `compat-lint.sh:251-267`

- 정규식: `^[a-z][a-z0-9_-]{0,31}$`
- 프레임워크 예약어 금지 (allowlist 토큰·payload 루트 사칭 방지):
  `bridge` `memory-engine` `scripts` `routines` `service` `config` `database`
  `mirror` `skills-bundle` `payload`
- 오염된 토큰은 뒤 검사(C3/C4/C6 경로 규칙)를 절대 구동하면 안 되므로 위반 시
  FAIL + **ABORT** (fail-closed). `compat-lint.sh:24-30,417-421`

### 2.2 payload 표면 allowlist (C4가 강제하는 실물 배치)

payload는 default-deny다. 아래 표면만 허용된다. `compat-lint.sh:799-1054`

| 표면 | kind=kit | kind=pack |
|---|---|---|
| `database/*` | 허용 | **금지** (킷 전용 — ZERO-MIGRATION 불변식) `compat-lint.sh:903-913` |
| `service/<이름>/*` | `<provides_kit>` 만 | `<service_namespace>` 만 (선언 선행) `compat-lint.sh:914-940` |
| `skills-bundle/<스킬>/*` | 정적 allowlist 11종(`compat-lint.sh:824-836`) + manifest `skills[]` 선언분 | 동일 `compat-lint.sh:842-848,941-964` |
| `routines/bundle/*` | 허용 (bundle 외 routines/ 금지) | 동일 `compat-lint.sh:965-975` |
| `routines/*.plist` + `routines/plists.defer` (payload 루트 바로 아래, flat) | `capabilities.schedules: true` 선언 시만 허용 (DGN-1402; 선언이 표면보다 선행, service_namespace/knowledge_warehouse 선례) | **금지** (schedules 는 kind=kit 전용 capability — pack 이 true 선언하면 C1 FAIL) `compat-lint.sh:1137-1160` |
| `config/<소유자>.conf` + `config/i18n*` | 소유자 = provides_kit | 소유자 = id `compat-lint.sh:852-855,976-1004` |
| `config/terms.json` (DGN-1500 층별 용어 사전) | 허용 -- 설치 시 `config/packs/<id>.terms.json` 로 `layer=kit` 스탬프 | 허용 -- 같은 경로로 `layer=module` 스탬프. 내용 검증은 설치측 `scripts/pack/lib/term_dict_layer.py` (발화 직전 로더 `routines/lib/term_dict.py` 의 validate_layer 와 동일 검사) |
| `config/outbound-allowlist` (DGN-1860 D7 검토완료 아웃바운드 조각) | 허용 -- 설치 시 `config/packs/<id>.outbound-allowlist` 로 복사 (조각 미동봉 시 기존 층 삭제, 교체 의미론) | 동일. 형식·병합 규칙은 §2.3 |
| `mirror/*` (소스만; db/pyc/html 잔재 금지) | 허용 | **금지** `compat-lint.sh:1005-1029` |
| `knowledge/*` | Forbidden payload, including for a kit declaring modules | Agent packs may ship only the declared warehouse. With `knowledge_modules`, only exercise, nutrition and sleep-recovery may supply declared offline bootstrap; no generic legacy layer. |
| `requirements.txt` (payload 루트만) | 허용 (파이썬 의존 선언, pack_deps_provision.sh 소비) | 동일 `compat-lint.sh:891-895` |
| `bridge/` `memory-engine/` `scripts/` | **절대 금지** (프레임워크 코어) | 동일 `compat-lint.sh:1042-1046` |
| 그 외 루트 / 경로탈출 / 경계 밖 symlink | 금지 | 동일 `compat-lint.sh:865-886,1047-1051` |

### 2.3 D7 검토완료 아웃바운드 조각 (DGN-1860)

설치 단위가 **자기 payload 파일**의 아웃바운드 호출(검토를 마친 것)을 직접 선언하는 통로다.
인스턴스 백업 push 의 D7 게이트(secret-sweep 의 outbound-diff 검사)가 env 없이 병합한다.

- 저작 위치: `<payload_root>/config/outbound-allowlist`. 설치 위치: 인스턴스
  `config/packs/<id>.outbound-allowlist` (`pack_install.sh` outbound-allowlist 단계, 교체 의미론).
- 형식: 인스턴스 `.outbound-allowlist` 와 같은 한 줄 한 항목
  `<경로> | <reason-token> | <설명> | <d7-review 참조> | <YYYY-MM-DD>`.
  reason-token 은 `false-positive | reviewed-call | fixture | other` 중 하나. `#` 주석·빈 줄 무시.
- `<경로>` 는 **인스턴스 루트 기준 설치 경로**(예: `.claude/skills-bundle/<스킬>/lookup.py`)이고
  **정확히 일치**해야 한다(부분 문자열 아님).
- 킷 조각 항목은 킷 납품 표면(`.claude/skills-bundle/`, `routines/bundle/`, `service/`,
  `database/`, `knowledge/`)에서만 유효하다. 그 밖의 경로(프레임워크·오너 파일)는 게이트가
  무시하고 note 를 남긴다.
- 스킬 이름을 바꾸면 같은 릴리스에서 이 파일의 경로도 바꾼다 -- 승인이 킷과 함께 움직이는 것이
  이 통로의 목적이다.
- 항목은 실제 검토 근거(호출 내용, 보내는 데이터, 받는 곳, 끄는 방법)가 있는 것만 넣는다.
  근거 없는 호출은 넣지 말고 D7 hit 로 남겨 검토를 받는다.
- 병합 순서: 오너 `.outbound-allowlist` -> `D7_OUTBOUND_ALLOWLIST_EXTRA` -> 프레임워크 조각
  (`scripts/outbound-allowlist.framework`) -> 킷 조각(정렬). 합집합이라 어느 층도 다른 층의
  항목을 지우거나 덮지 못한다. 조각이 없으면 게이트 동작은 DGN-1860 이전과 같다.

## 3. 폐기 어휘 (쓰지 마라)

아래 어휘는 **폐기 확정**이다. 재발명 금지. 근거: DGN-681-S4b-RATIFIED (실물 착지 어휘 중
`requires_kit` 1종만 비준, 2종 제거; estate 전체 0-hit 검증 완료).

| 폐기 어휘 | 상태 | 대체 |
|---|---|---|
| `module_deps` | 폐기 (락 회수본 어휘; 코드 0-hit) | `requires_kit` |
| `pack_deps` | 폐기 (비준 거부) | `requires_kit` |
| `provides_modules` | 폐기 (매니페스트 필드 비준 거부 — 카탈로그 SoT와 모순). **DGN-1391 로도 돌아오지 않는다**: 아래 참조 어휘는 소유 주장이 아니다 | 에이전트팩이 참조를 적을 때는 `requires_modules` (§2), 소유·조성 관계는 카탈로그 엣지 |
| `module_active()` / `DOGANY_MODULES` 씨딩 | 미구현·폐기 (TK-13 0바이트; `pack_install.sh:184` 주석 언급만 존재, 쓰기 없음) | — |
| `status: "official"` | 폐기 철자 | `"published"` 만 인식 (`pack_publish.sh:658-662` 주석; D-D·설치 메뉴 모두 published만) |
| `C-TAG` / `C-MODDEPS` | **존재하지 않는 슬롯** — 코드 0-hit. DGN-681 락 항목7/8의 예약 명명 슬롯일 뿐이며 한 번도 구현되지 않았다 (C-MODDEPS는 S4b-2 예약 유지 — DGN-1391 이 착지시킨 `requires_modules` 검사는 **설치측 전용**이라 이 슬롯을 열지 않았다, §5-1-1b) | — |

## 4. compat-lint 게이트 슬롯 전수 표

실행 코드 기준 전수 (이 목록에 없는 슬롯은 존재하지 않는다). 실행 순서대로.
호출 형식과 종료 코드: PASS/SKIP=0, FAIL≥1건=1.

**알려진 드리프트 (2026-08-28 실측, DGN-1147)**: 이 표의 줄번호 참조 다수가 이미
어긋나 있다 — 예컨대 C5 행은 `1179-1261`을 가리키는데 DGN-1147 이전 HEAD 에서
`# CHECK 5` 블록 머리는 `1240`이다 (약 60줄 차이). **슬롯 이름과 판정 내용은 유효하고
줄번호만 낡았다.** 이번 편집은 자기 행(C-RELNOTE)과 이번 삽입으로 밀린 두 행(D-D / C6)만
실측값으로 맞췄다 — 나머지 행 일괄 재측정은 별건이다 (§9-1 기준으로는 이 문서의 버그다).

- 발행측(lint): `compat-lint.sh --pack-dir <root> --framework-version <v>`
- 설치측(게이트): 위 + `--install-side [--catalog <catalog.json>]` — **우회 없음, 같은 게이트**

| 슬롯 | 무엇을 검사하나 | 실패 시 | 측 | 근거 |
|---|---|---|---|---|
| (파스 게이트) | manifest가 JSON 오브젝트로 파스되는가 | C1 FAIL + ABORT | 양측 | `compat-lint.sh:161-181` |
| (legacy-grace) | contract_version 부재 & kind≠kit → 전체 SKIP exit 0 | — (게이트 아님, 경계) | 양측 | `compat-lint.sh:199-207` |
| **C1** | 필수 필드 존재 + pack_version semver + requires_framework range 문법 + provides_kit/capabilities/service_namespace/units 선언 검증군 | FAIL (provides_kit·payload_root 오염은 +ABORT) | 양측 | `compat-lint.sh:269-330,373-528,640-665` |
| **C-KIND** | kind 어휘 = 정확히 kit\|pack | FAIL + ABORT | 양측 | `compat-lint.sh:332-371` |
| **C-KITDEP** | requires_kit **형식** ({kit,range}, 문법, 자기의존, kind 제한). 만족성은 여기서 안 본다 (§5-1) | FAIL; 부재=SKIP | 양측 (형식만) | `compat-lint.sh:530-616` |
| **C2** | 프레임워크 VERSION이 requires_framework range를 만족하는가 | FAIL | 양측 | `compat-lint.sh:618-635` |
| **C3** | 버전 3점 일치: `EXPECTED_USER_VERSION` == max(migrations 파일번호) == schema.sql `PRAGMA user_version`; 마이그레이션마다 `-- reversible:` 마커; pin bump에 마이그레이션 파일 필수 | FAIL; payload 미시드=SKIP; kind=pack=클래스 SKIP; db_lane=false=capability SKIP (단 database/ 출하 시 skip-smuggling FAIL) | 양측 | `compat-lint.sh:684-797` |
| **C4** | payload 경로 allowlist (§2.2, 클래스 분기) + 경로탈출/symlink 경계 | FAIL | 양측 | `compat-lint.sh:799-1058` |
| **C-HOOKS** | manifest `hooks[]` 선언 검증 (DGN-1496): 이벤트 어휘 / 경로 안전(절대경로·`..` 거부) / `routines/bundle/` 표면(경로 규약 위반을 **이름 붙여** 거부) / payload 실재(죽은 선언) / timeout 정수 | FAIL; 선언 부재=PASS(검증 대상 없음) | 양측 | `compat-lint.sh` C-HOOKS 블록 (C4 직후) |
| **C4b** | 포장 잔재 위생: `*.bak*` `*~` `*.orig` `*.swp` `*.pyc`, `_archive`/`__pycache__`/`.pytest_cache` 경로 성분 | FAIL | 양측 | `compat-lint.sh:1060-1116` |
| **C7** | skills[] 공유 선언: share\|own 어휘, 중복 금지, 죽은 선언 금지, share 스킬의 프레임워크 템플릿 번들 충돌 금지 | FAIL; 블록 부재=SKIP (전부 own) | 양측 | `compat-lint.sh:1118-1177` |
| **C5** | gate(a) 개인데이터/실DB: `memories` `USER.md` `.env` `.telegram_bot` 파일·디렉터리, transcript 디렉터리, 소문자화 basename 기준 `*.db` `*.db-wal` `*.db-shm` `*.db-journal` `*.sqlite*`; gate(b) 페르소나 토큰 잔재 `__AGENT_LABEL__` 등 4종 | FAIL | 양측 | `compat-lint.sh:1179-1261` |
| **C-RELNOTE** | **레포 ROOT 릴리스 노트**(`CHANGELOG.md` / `RELEASES.md` / `RELEASE-NOTES.md` / `releases/*.md`) 산문: 인용된 **오너 발화**가 이유 자리를 대신하고 있는가 (§6-1c 규칙의 기계 바닥). 술어·오탐 실측은 `lib/relnote_authorship.sh` 헤더 | FAIL; 루트에 릴리스 노트 부재=SKIP(무엇을 찾았는지 인쇄); 예측기가 어느 로케일에서도 카나리아를 못 보면 FAIL(무음 0 금지) | **발행측 전용** (설치측 SKIP) | `compat-lint.sh:1330-1427`, `lib/relnote_authorship.sh` |
| **D-D** | 발행 반대서명: manifest `status=="published"` AND 카탈로그 row의 동일 id가 같은 status (자기선언만으로는 앵커가 아니다) | FAIL (→ C6 억제: 미서명 payload 코드는 실행 금지) | **설치측 전용** (발행측 SKIP) | `compat-lint.sh:1429-1469` |
| **C6** | kit CLI 계약 동사 스모크: `payload/database/<kit>.sh check` + `dump` 종료코드 0 (PYTHONDONTWRITEBYTECODE=1로 자기오염 방지) | FAIL; D-D FAIL/미시드/kind=pack/db_lane=false → SKIP | 양측 | `compat-lint.sh:1471-1530` |

## 5. 설치·업데이트 게이트

### 5-1. pack_install.sh 가 실제로 거부하는 것 (계약 설치 단위 경로, 순서대로)

0. **대표 킷 하나 규칙** (dec-382/393, kit 클래스 설치, payload copy **이전**, dry-run 포함):
   에이전트(인스턴스) 하나는 대표 킷 하나만 가진다. `.instance.conf`의 `DOGANY_PACKS`에
   이미 **다른** 킷(카탈로그 `kind=kit`, `retired` 아님, `provides_kit`이 다름)이 기록돼
   있으면 두 번째 킷 설치를 명확한 메시지로 거부한다. 같은 킷의 재설치·업그레이드는 통과.
   `pack_install.sh` "One representative kit per agent" 블록.

   **크루 리더와 seat** (DGN-2040, 설계: DGN-2040 secretary-leader design (canonical repo)).
   대표 킷 하나 규칙은 모든 **루트**를 묶는다. 크루 리더(`DOGANY_CREW_ROLE=leader`,
   `crew_lead.sh` 만 쓴다)는 자기 루트에 킷을 두지 않는다. 이끄는 크루마다 **seat**
   하나를 가진다: `seats/<kit>/<리더 이름>` 에 있는 킷 하나짜리 멤버 루트로, 다른
   멤버와 똑같이 크루에 합류한다(DB 링크, migrate, `crew.conf` 행, 크루 업데이트).
   seat 에는 스케줄·루틴 번들·훅·활성 스킬이 없다. 리더는 크루 장부를 오직
   `crew_lead.sh run` 으로만 쓴다 — 그 seat 안에서 킷 자신의 명령을 실행한다. 두 키
   (`DOGANY_CREW_ROLE`, `DOGANY_SEAT_OF`)는 인스턴스 속성이며 에이전트팩·킷 id 에서 절대
   유도하지 않는다(dec-400). db_derived 킷은 장부가 파일이라 C2(한 번에 장부 하나)는
   선언적이다(G3). 킷은 `DOGANY_ACTOR` 가 있으면 행위자를 거기서 가져와야 한다(SHOULD);
   멤버 수를 세는 킷은 모든 멤버를 staff 로 가정하면 안 된다 — seat 은
   `DOGANY_SEAT_OF` 로 구분된다. 리더 루트의 킷 설치 = 거부(블록 1), seat 의 에이전트팩
   설치·잘못된 경로·리더가 아닌 소유자 = 거부(블록 2). `pack_install.sh` "DGN-2040:
   crew seat keys" 블록.

1. **requires_kit 만족성 게이트** (payload copy **이전**): 대상 인스턴스
   `.instance.conf`의 `DOGANY_PACKS=<id>@<ver>,...` 장부에서 `<kit>@` 항목을 찾아
   선언 range를 평가. 장부/항목 부재, 버전 파스 불가, range 불만족 → **BLOCK**
   (fail-closed, warn 레인 없음). 형식 검증은 C-KITDEP과 verdict-identical 이중구현.
   `pack_install.sh:482-634`. 크루 리더 루트에서는 같은 판정을 리더의 seat
   `.instance.conf` 에 대해 내리고, 리더 루트로 킷을 자동 설치하지 않는다 — seat 이
   없으면 `crew_lead.sh seat` 명령줄을 담아 거부한다(DGN-2040 5.4).

   **1b. requires_modules 참조 게이트** (DGN-1391, requires_kit 게이트 **직후**,
   역시 payload copy 이전). 에이전트팩이 `requires_modules: ["<모듈>", ...]` 로 **참조**를
   선언하면, 그 이름들이 `requires_kit` 이 가리키는 킷의 **모듈 로스터**에 실재하는지
   확인한다. 이름을 적는 것은 소유 주장이 아니다 — 에이전트팩은 그 모듈의 테이블을 소유·이전·
   버전하지 않는다(폐기된 소유 어휘는 §3, 돌아오지 않는다).

   판정 (전부 fail-closed, warn 레인 없음):
   legacy 에이전트팩(contract_version 부재 AND kind≠kit) → SKIP /
   `requires_modules` 부재 → SKIP(**이 게이트 이전과 동일한 설치** — 오늘 존재하는
   모든 매니페스트가 이 경우다) / 형식 손상(배열 아님·문자열 아님·토큰 문법 위반·중복·
   빈 배열) → BLOCK / `requires_kit` 없이 선언 → BLOCK(참조를 풀 대상이 없다) /
   킷 카탈로그 행·좌표·매니페스트 도달 불가 → BLOCK(검증 불가 = no-go, DGN-1004) /
   킷이 로스터를 선언하지 않음 → BLOCK / 로스터 손상 → BLOCK /
   지명한 모듈이 로스터에 없음 → BLOCK(없는 이름 + 로스터 전량을 같이 인쇄) /
   전건 실재 → PASS(참조 수 + 로스터 수 모수 인쇄).

   **로스터의 출처와 그 한계 (부재를 부재로 적는다)**: 킷의 모듈 목록은 오늘 어디에도
   기계가 읽을 수 있는 형태로 없다. 존재하는 유일한 목록은 이 레포 밖 거버넌스 문서의
   **산문**이며 설치기는 그것을 읽을 수 없고 의존해서도 안 된다. 그래서 로스터는 그
   사실을 소유하는 곳이자 설치기가 이미 가리킬 수 있는 곳 — **의존 킷 자신의 매니페스트
   `modules` 배열** — 에서 읽고, 킷 위치는 카탈로그 좌표 단일원(§6-2,
   `lib/pack_coords.sh`)으로 푼다. 결과로 두 조건이 남고 숨기지 않는다: 로스터를
   선언하지 않은 킷은 어떤 참조도 만족시킬 수 없고, 킷 매니페스트에 도달할 수 없는
   인스턴스는 통과가 아니라 BLOCK 이다. 킷 설치 시점에 로스터를 인스턴스에 포착하는 것
   (`config/packs/<id>.requires_framework` 선례)이 도달 조건을 없애는 방향이며, 그것은
   이번 변경이 아니다.

   **compat-lint twin 없음**: 이 검사는 인스턴스 상태(카탈로그·킷 실물)를 보므로
   requires_kit **만족성**과 같은 부류다 — 설치측 전용이고, 예약 슬롯 C-MODDEPS 는
   열지 않았다(§3). 착지 지점: `pack_install.sh` 의 requires_modules 게이트 블록
   (requires_kit 게이트 `esac` 바로 다음), 테스트
   `scripts/tests/test-pack-install-requires-modules.sh`.
2. **인라인 재검증** (verdict-identical 이중검증): kind/provides_kit/service_namespace/
   knowledge_warehouse/db_lane/killstep 규칙을 설치기가 **자체적으로 다시** 검사한다 — agent pack
   `pack_install.sh:992-1094`, kit `pack_install.sh:1106-1178`.
   **왜 이중인가**: 아래 3의 compat-lint 게이트는 compat-lint.sh 부재 시 WARN+skip으로
   구조적 fail-open인데, 검증 결과가 파일시스템 쓰기 경로(`database/<kit>.db`,
   `service/<kit>/` 등)를 구동하므로 설치기는 호출자를 신뢰하지 않는다. 두 구현의
   판정은 **verdict-identical 규율**로 묶인다: 한쪽을 바꾸면 반드시 다른 쪽도 바꾼다.
   `pack_install.sh:488-493,997-1003,1099-1109`
3. **compat-lint 설치측 게이트**: `--install-side --catalog`로 §4 전 슬롯 실행
   (D-D 포함). FAIL → install abort. compat-lint.sh **부재 시 WARN+skip (fail-open)** —
   단독 실행 한정이며 통합 루프에서는 §5-3 벨트가 닫는다.
   `pack_install.sh:1196-1220` (legacy agent/module 경로의 동일 게이트: `:2302-2328`)
4. **reverse-drift guard**: 인스턴스에 이미 있는 `<kit>.py`의 pin이 payload pin보다
   높으면 kit_core copy를 loud SKIP (다운그레이드로 덮지 않는다).
   `pack_install.sh:1222-1259` (kit 전용 — agent pack에는 kit_core가 없다)
5. **NM3 무결성 게이트**: 봉인의 **물리적 정체에 따라 레인이 갈린다** (DGN-1549).
   - **git 워크트리 소스**: 봉인은 git 객체 저장소다 — 검증된 태그(source-tag 게이트)
     → 커밋 → 트리. 게이트는 (1) 패키지 서브트리의 tracked 바이트·모드가 태그 트리와
     동일한가 (`git diff`), (2) payload 서브트리에 untracked/ignored 파일(봉인 없이
     설치될 바이트)이 숨어 있지 않은가 (`git status --ignored`)를 판정한다. 불일치 =
     FATAL — 단, 문구는 **"좌표가 태그에서 이동했다"**이지 "변조"가 아니다.
     워크트리 좌표의 `checksums.sha`는 **절대 참조하지 않는다** (존재하면 loud NOTE 후
     무시): 그 파일이 바로 DGN-1549의 이음새 — 한 번 쓰이고 아무도 갱신하지 않는
     투영을 정본으로 읽다가 dev 팩 1.3.1/1.4.0 이 설치 불가로 발행됐다.
   - **비-git 소스** (태그 아카이브 추출·테스트 픽스처·순수 export): 패키지의
     `checksums.sha`(sha256, `<hex>  <relpath>`)와 payload 대조. 불일치·결손 = FATAL
     (warn-continue 없음) — 실패 문구는 두 가지 진실(전송 중 손상 / 봉인 자체가 낡은
     채 컷됨)을 모두 명명한다. checksums.sha 부재 = loud WARN 후 진행 (DGN-1549 이후
     git 태그에서 유도된 아티팩트는 정당하게 체크섬 파일이 없다 — 무결성은 `git
     archive <검증된 태그>`가 담보).
   게이트 본체는 **두 경로가 공유하는 함수 하나** (`pack_install.sh` `_nm3_verify`),
   호출 지점은 계약 설치 단위 경로 / legacy agent·module 경로. 두 경로 모두 **dry-run 종료
   이후, 첫 payload 쓰기 이전**에 돈다 (dry-run은 지킬 쓰기가 없으므로 무결성 게이트를
   돌리지 않는다 — 양 경로 동일).

   **정정 기록 (2026-08-25, DGN-1045 후속)**: 위 5번은 2026-08-25까지 **문서만 사실이었다.**
   NM3는 legacy 경로에만 인라인으로 존재했고, 계약 설치 단위 경로는 그보다 앞서 dispatch 되어
   `exit 0` 으로 끝나므로 **그 블록에 도달한 적이 없다.** 실측: 일부러 틀린 sha256을 적은
   `checksums.sha` 를 가진 kind=pack 픽스처가 rc 0으로 설치되고 로그·stdout 어디에도 NM3
   줄이 없었다. compat-lint는 양측 모두 payload 바이트를 해시하지 않으므로, **신규·서드파티
   팩이 전부 속하는 계약 클래스에 무결성 게이트가 아예 없었다** — 폐지 예정인 legacy
   클래스만 게이트를 갖고 있었다. `packs/dev` 의 stale seal이 4커밋 동안 조용했던 것은
   무해해서가 아니라 **보는 눈이 없어서**다(dec-128). 게이트를 공유 함수로 올려 양 경로에서
   호출하는 것으로 닫았다. **문서 서술이 옳고 코드가 틀린 쪽이었다.**

6. **훅 배선 게이트** (DGN-1496, routines-bundle 배달 **직후**): manifest `hooks[]`
   선언을 C-HOOKS 와 verdict-identical 하게 인라인 재검증(위 2와 같은 이유 — lint
   게이트는 compat-lint.sh 부재 시 fail-open)한 뒤, 선언 그대로
   `.claude/settings.json` 에 멱등 배선한다(기배선 스캔 후 append, 단일 최종 쓰기 —
   거부 시 partial write 없음). 선언 스크립트가 인스턴스에 **배달되지 않았으면**
   (예: 소유 카테고리 미선언) BLOCK — 댕글링 커맨드를 걸어 세션 시점 file-not-found
   로 미루지 않는다. census: `hook_wiring` = `run` / `already-present` /
   `skip:no-manifest-hooks`. legacy 경로 STEP 5 는 같은 헬퍼로 같은 판정을 하되
   표면만 flat `routines/*.py|.sh` 이다. 선언 없이 `routines/ledger-inject.py` 를
   실은 legacy payload 는 **migration tripwire 로 BLOCK** (조용한 미배선 금지 —
   한 줄 선언 안내를 인쇄). `pack_install.sh` `_hooks_validate`/`_hooks_wire`/STEP 5.

설치가 남기는 계약 기록 2종:

- `DOGANY_PACKS` 장부 upsert: `.instance.conf`에 `<id>@<version>` 원자적 기록
  (임시파일+mv). requires_kit 만족성·크루 검증·업데이트 플래너가 이 장부를 읽는다.
  `pack_install.sh:300-370`
- `config/packs/<id>.requires_framework` 캐시: manifest의 range를 설치 시점에 VERBATIM
  포착 (업데이트 시점엔 manifest가 도달 불가능하기 때문 — release 채널은 throwaway
  `git archive` 트리에서 돈다). 필드 부재 = 기록 없음 (유효 상태). `pack_install.sh:397-436`

### 5-2. 프레임워크 업데이트측 게이트 (역방향)

`update.sh` **fw_reqframework_guard**: 프레임워크 파일이 착지하기 **전에**, 인스턴스에
마운트된 모든 설치 단위의 requires_framework를 **타깃** 프레임워크 버전에 대해 평가한다
(C2는 설치 단위 설치 시점만 보므로 이 게이트가 역방향 구멍을 닫는다). 판정 4상태
(절대 서로 뭉개지 않는다):

- PASS (제약 확인+만족) / SKIP (manifest가 제약 없음을 **확인**) /
  WARN-UNKNOWN (제약을 알 수 없음 + minor/patch 범프 — 비차단, 항상 인쇄) /
  FAIL (위반, 손상 기록, 또는 **major 범프에서의 UNKNOWN**) → 전체 fail-closed exit 1,
  우회 플래그 없음.
- 캐시 부재 시 자기치유 backfill (DOGANY_REPO_ROOT 기준 manifest 재해석 후 캐시).
- "packs evaluated: N" + 판정별 카운트 항상 인쇄 (0은 통과가 아니라 의심).

`update.sh:1328-1467` (호출 `update.sh:1659`)

### 5-3. 통합 업데이트 루프 (update_plan / update_apply)

- 플래너(`update_plan.sh`)는 **zero-write** (읽기 전용 계획): 설치 단위 소스 레지스트리
  `~/.dogany/pack-sources.conf` + 인스턴스 `DOGANY_PACKS` + 크루 3원 교차검증
  (crew.conf × DOGANY_PACKS × DB symlink 실물), 드리프트 가드(설치본보다 낮은
  후보 태그 채택 금지 = BLOCKED-DRIFT), 원자 그룹 산출. `update_plan.sh:1-108`
- 적용기(`update_apply.sh`)는 자체 설치 알고리즘이 없다 — 설치 단위는
  `pack_install.sh --upgrade`, 프레임워크는 `self-update.sh --no-restart`로 위임.
  `update_apply.sh:2-17`
- **belt 1**: compat-lint.sh 부재 → 진입 시 ABORT + 유닛별 재확인. `update_apply.sh:407,749`
- **belt 2**: pack_install이 rc 0이어도 출력에
  `"compat-lint: install-side gate PASS"` 라인이 없으면 (fail-open WARN+skip 케이스)
  그 유닛은 INCOMPLETE — §5-1-3의 단독 경로 fail-open이 루프 경로에서는
  fail-closed로 닫힌다. `update_apply.sh:97-106,792`
- 원자 유닛 정책: 크루 킷은 전 멤버가 하나의 T0~T-end 트랜잭션 (정지→백업→적용→검증→재기동);
  INCOMPLETE 유닛의 브리지는 **재기동하지 않는다** (반쪽 상태는 라이브로 돌아가지 않는다).
  `update_apply.sh:52-95`

## 6. 발행 권한과 서명 (D-D)

### 6-1. 발행 파이프라인이 강제하는 것 (`pack_publish.sh`)

발행은 라이브 소스를 **읽기만** 하고 (one-writer 불변식) 정제 payload + 봉인 산출물을
만든다. 산출물: `pack-manifest.json` + `CHANGELOG.md` + `checksums.sha`(NM3 발행측,
**비-git 좌표 한정** — git 좌표의 봉인은 태그 트리다, DGN-1549) + `.source-sync` 출처
기준선 + 카탈로그 row upsert + **STEP 7 소비 리허설** (실설치 1회, 아래 참조).
`pack_publish.sh:1-46`

`CHANGELOG.md` 의 **본문 작성 규칙**은 §6-1c (릴리스 노트 정본), **절 문법**은 §6-1b 끝의
seal 절 문법 항이다 — 전자는 무엇을 쓰는가, 후자는 어떤 형식으로 쓰는가다.

발행 게이트 (각각 loud-FAIL):

- **gate (a)** 개인데이터/대화기억 제거: `memories` `USER.md` `.env` `.telegram_bot`,
  transcript 디렉터리류, 실DB 파일 (suffix 목록은 §8의 알려진 드리프트 참조).
  `pack_publish.sh:103-110,665-690`
- **gate (b)** 페르소나 토큰 잔재: `__AGENT_LABEL__|__USER_LABEL__|__AGENT_NAME__|__USER_NAME__`.
  `pack_publish.sh:99-101,692-702`
- **gate (c)** knowledge 스냅샷 릴리스 핀: 부동 참조 금지. `pack_publish.sh:704-726`
- **gate (d)** 계약 게이트 (`compat-lint.sh`, 발행측; DGN-1035): FAIL-CLOSED, WARN 레인
  없음. `pack_publish.sh:728-756` — §6-1a 참조.
- **NM3 봉인** (DGN-1549 레인 분리): **git 좌표**에서는 checksums.sha 를 생성하지
  않고 존재하면 **삭제**한다 — 봉인은 태그 트리이고, 워크트리 좌표의 체크섬 파일은
  갱신 강제점 없는 낡을 수 있는 투영이다 (dev 팩 1.3.1/1.4.0 을 죽인 바로 그 형상).
  **비-git 좌표**에서만 checksums.sha 생성 (`<hex>  <relpath>`, 설치 게이트와 동일
  포맷 — §5-1-5 비-git 레인이 실제로 그 포맷을 읽는다).
- **STEP 7 소비 리허설** (DGN-1549): 봉인 직후 발행 레인이 **실제 설치를 한 번
  집행**한다 — 격리 인스턴스 픽스처 + 격리 shared home (DGN-1474; 실 `~/.dogany`
  불가침), 비-git export 사본, 임시 카탈로그(자기 row 만 status=published 로 서명
  시뮬레이트, throwaway 사본 한정). 설치가 거부하면 발행이 FATAL 로 중단된다.
  dry-run 이 아니라 실설치인 이유: dry-run 은 NM3 이전에 종료하므로 오늘의 결함
  클래스를 잡지 못한다. 우회 스위치는 없다 (D1 no-escape-hatch 독트린).
  소비자 0 인 상태에서도 "발행물이 설치되는가"를 발행 게이트 스스로 묻는 것이
  이 단계의 존재 이유다 — 1.3.1/1.4.0 이 3주간 숨을 수 있었던 구멍.

### 6-1b. finalize 모드 전용 게이트 (`--mode finalize`, DGN-441 + DGN-1045 후속)

finalize 는 **이미 디스크에 있는 payload 를 제자리에서 봉인**한다 (materialize 없음,
I1 = payload 불변; manifest 도 payload 산출물이므로 finalize 는 **고쳐 쓰지 않는다**).

- **G-F1 payload 존재·실내용**: 두 payload 형상을 구분한다.
  **계약 형상**(`contract_version` 존재) = payload 루트가 manifest `payload_root`(기본
  `payload`), 실내용 판정은 compat-lint 와 **같은 술어**
  (`scripts/pack/lib/payload_seeded.sh` `pack_payload_seeded` — `.gitkeep` 이 아닌 파일이
  하나라도 있으면 seeded). **legacy 형상** = `<refslug>/` 아래 `AGENT.md.add` /
  `skills` / `routines` / `scripts` / `knowledge` (종전과 동일).
  `pack_publish.sh:233-336`

  **정정 기록 (2026-08-25, DGN-1045 후속)**: G-F1 은 legacy 목록 하나만 알았고 계약형
  payload(`payload/service/<ns>/...`)를 **"봉인할 내용 없음"으로 거부**했다. 실측:
  `packs/dev`(kind=pack, `payload/service/dev/` 4파일) finalize 가 G-F1 에서 죽었고,
  refslug 추론은 `dogany-lifekit`(top-level 디렉터리 6개 -- payload + crew/files/sot/
  tools/.pytest_cache)에서 "후보가 여럿이라 추론 불가"로 죽었다. **즉 계약 설치 단위은 발행기로
  봉인이 불가능했다** — dec-128 이 "기계가 dev 를 봉인조차 못 한다"고 적은 것이 이것이다.
  compat-lint 의 seeded 술어를 공유해 닫았다.

- **G-F2 모드 가드**: finalize 에 materialize 플래그(`--section`/`--skill`/`--routine`/
  `--script`/`--reference-slug`/`--manifest-in`/`--knowledge-warehouse`) = 사용자 오류로
  거부 (무시하면 "복사한다"는 뜻이 되므로). `pack_publish.sh:226-231`
- **G-F3 드리프트 리포트**: `.source-sync` 기준선 대비 MATCH/DRIFT/MISSING 항상 인쇄.
  **비파괴** — finalize 는 기준선을 재생성하지 않는다(의도된 divergence 를 숨기지 않기
  위해). WARN only, 차단하지 않음. `pack_publish.sh:366-417`
- **G-F4 봉인 좌표 정합** (신규, DGN-1045 후속): `--pack-version` != manifest
  `pack_version` → **FATAL, 아무것도 쓰기 전에 중단**. manifest 에 `pack_version` 이
  없으면 loud WARN. `pack_publish.sh:338-357`

  **왜 자동 갱신이 아니라 거부인가**: 버전 선택은 **컷 결정**이다
  (`dev-crew/sot/PACK-VERSIONING.md` §1 판정표 / §2 scope-lock — manifest 범프와 seal 은
  같은 커밋, 저자는 컷하는 사람). 발행기가 조용히 manifest 를 고쳐 쓰면 판정표를 한 번도
  적용하지 않고 `--pack-version` 만으로 버전을 올리는 우회로가 생긴다. 게이트 이전에는
  `--pack-version 9.9.9` + manifest `1.2.0` 이 **rc 0 으로** `## [9.9.9]` seal 절과
  카탈로그 row 를 만들어냈다 — 발행기가 **스스로 3점 일치를 깬** 상태다(실측).

**CHANGELOG seal 절 문법** (`## [X.Y.Z] -- YYYY-MM-DD`): 발행기가 쓰는 절 머리는
릴리스측 3점 게이트가 읽는 형식과 **같아야** 한다 (`pack-version-lint` P3 =
`^##\s+\[X.Y.Z\]` + `-- YYYY-MM-DD` 날짜가 sealed 를 의미). `pack_publish.sh:596,621-632`

  **정정 기록 (2026-08-25, DGN-1045 후속)**: 발행기는 대괄호 없는 `## X.Y.Z -- 날짜` 를
  썼고 린트는 대괄호 형식만 읽었다 → **발행기로 낸 설치 단위는 P3 영구 FAIL**. 실측: 실제로
  발행된 설치 단위(lifekit 1.0.0~1.3.1, health-trainer 0.1.0/0.1.1)은 **전부 대괄호 형식**이고,
  트리에서 유일한 비대괄호 CHANGELOG 는 **이 발행기가 쓴 `packs/dev`** 였다. 그래서
  발행기를 대괄호로 맞췄다(린트를 바꾸는 방향이면 발행된 설치 단위 전부가 깨진다). 기존 절은
  다시 쓰지 않는다 — 같은 버전 재봉인의 replace 경로만 두 문법을 모두 인식한다.

### 6-1c. 릴리스 노트 작성 규칙 (DGN-1147, 정본)

> **릴리스 노트는 바뀐 것과 그 이유를 작성자 자기 문장으로 쓴다.**

이유는 **작성자의 것**이다. 무엇이 바뀌었는지 쓰고, 왜 바꿨는지를 자기가 이해한
문장으로 적는다. 바꾼 용어를 이름으로 부르는 것(`프라이머` → `세팅`), 커밋을 대는 것,
티켓을 대는 것 — 전부 정상이다. 넘길 수 없는 것은 **이유 그 자체**다: 인용된 오너
발화를 이유 자리에 세워두고 독자에게 진 빚을 갚았다고 할 수 없다.

**적용 범위**: 발행 패키지의 **봉인 산출물**과 레포 루트의 릴리스 노트. `CHANGELOG.md`
는 §6-1이 열거하는 발행 산출물이고 발행기가 STEP 4에서 직접 쓴다
(`pack_publish.sh:667`) — 즉 **설치하는 낯선 사람이 읽는 파일**이다. payload 산문은 이
조항이 아니라 발행 gate (s)(estate-scrub) 소관이다.

**왜 긍정명령인가**: 이 estate의 기존 판정 — 부정명령("~하지 마라")은 취약하며,
규칙은 **긍정명령(소유권 귀속)**이거나 룰 자체 제거로 처리한다. 그래서 이 조항은
"인용 금지"가 아니다. 인용을 지우는 것이 목적이 아니라 **이유의 소유권이 작성자에게
있다**는 것이 규칙이고, 게이트 실패 메시지도 같은 말을 인쇄한다
(`compat-lint.sh:1418-1419`).

**기계 바닥**: compat-lint **C-RELNOTE** 슬롯(§4)이 이 규칙의 기계로 검사 가능한
부분집합 — *인용된 오너 발화* — 만 강제한다. 규칙이 정본이고 게이트는 그 아래 바닥이다;
게이트가 못 보는 형태(따옴표 없는 의역, 영어 전용 인용 등)는
`lib/relnote_authorship.sh`의 KNOWN NON-COVERAGE에 열거돼 있으며 **규칙 위반이 아닌
것이 아니라 기계가 못 보는 것**이다.

**발생 근거 (실측 2026-08-28)**: `dogany-agentpack-health-trainer/CHANGELOG.md:126`이
트래킹·발행된 채 오너 육성 발화를 인용하고 있었다. 기존 어느 게이트도 이 파일을 읽지
않았다 — C4/C4b는 `$PAYLOAD_DIR` 스코프, C5는 `$PACK_DIR`를 걷지만 **파일명**을
판정하고, 발행 gate (s)는 REF_DIR만 본다 (그 게이트 주석이 이 구멍을 스스로 적어두고
있었다: "seal artifacts at the package root ... are outside REF_DIR and not scanned").

**착지 방향** (§6-1a 표의 논리를 이 슬롯에 적용): **발행측 fail-closed, 설치측 SKIP**.
발행측은 결함을 아직 **예방할 수 있는** 유일한 지점이다 — gate (d)가 STEP 4가
CHANGELOG를 쓴 **뒤에** `$PACKAGE_DIR`를 상대로 이 린트를 돌리므로
(`pack_publish.sh:667` → `:812`), 위반 봉인물은 발행되기 전에 거부된다. 설치측은
설치자가 고칠 수 없는 결함으로 **이미 발행된 설치 단위**을 거부하게 되고, 과거 태그의
CHANGELOG(처분 전 본문)를 물고 있는 업데이트를 새로 막는다 — 태그 모집단 실측이 붙어야
하는 별개 결정이라 여기서 조용히 켜지 않는다. D-D가 설치측 전용인 것의 대칭이다.

**fail-closed로 켠 근거는 먼저 센 것이다**: 발행된 설치 단위 루트 3종(lifekit /
health-trainer / dev) 전수 **0건**. 지금 존재하는 어떤 것도 깨지 않는다. (많았다면 WARN
으로 시작하고 그 수를 남겼을 것이다 — 0이었으므로 §6-1a가 발행측 게이트에 요구하는
형태로 들어갔다.)

### 6-1a. 발행측 fail-open/closed 방향 (DGN-1035, 명시적 결정)

pack/lifekit/v1.3.0이 compat-lint 13-FAIL 상태로 태그 발행된 사건(DGN-1035)은
"발행 컷이 버전 게이트만 물어보고 계약 게이트는 안 물어본다"는 미결정 상태가 만든
구멍이었다 — compat-lint가 발행 파이프라인에 배선조차 되어 있지 않았다. gate (d)로
닫았고, 방향을 다음과 같이 **명시적으로** 정한다:

| 측 | compat-lint 호출 | 부재/실패 시 | 근거 |
|---|---|---|---|
| **발행측** (gate (d)) | `--pack-dir <payload> --framework-version <v>` (install-side 아님) | **FAIL-CLOSED, WARN 레인 없음** — compat-lint.sh 부재도 FAIL | compat-lint.sh는 pack_publish.sh와 같은 디렉터리(`scripts/pack/`)의 co-located dev machinery다. "이미 배포된 구형 인스턴스가 이 스크립트를 아직 못 받았다"는 설치측 사정이 발행 시점에는 존재하지 않는다 — 부재는 깨진 체크아웃만을 뜻하므로 fail-closed가 유일하게 말이 되는 방향이다. `pack_publish.sh:531-548` |
| **설치측** (§5-1-3) | `--pack-dir <payload> --framework-version <v> --install-side --catalog <catalog.json>` | **fail-open (WARN+skip)** 단독 실행 한정; 통합 루프(`update_apply.sh`)는 §5-3 벨트가 fail-closed로 닫는다 | 설치는 임의의 이미 배포된 인스턴스에서 실행되며, 그 인스턴스가 compat-lint.sh를 아직 못 받은 구형 배포일 수 있다 (레거시 호환). `pack_install.sh:1098-1122,2237-2264` |

이 표가 §"발행 권한과 서명" 원문이 요구한 "두 side의 방향을 의도적으로 정해야 한다"의
답이다 — 두 방향이 다른 것은 비대칭이 아니라, 각 측이 실제로 다른 신뢰 경계에
서 있기 때문이다 (발행은 always-canonical-checkout, 설치는 arbitrary-deployed-instance).

**install-side PREVIEW (advisory, non-blocking)**: gate (d)는 발행측 판정 뒤에
`--install-side --catalog`도 한 번 더 돌려 결과를 로그로만 남긴다 (게이트 아님 —
FAIL이어도 발행을 막지 않는다). 이유는 실사례 2(health-trainer v0.1.0, 아래)다 —
발행측 lint는 D-D(설치측 전용, §4)를 구조적으로 SKIP하므로, 발행측 "ALL CHECKS
PASS"만으로는 "설치도 될 것"이라고 읽으면 안 된다. draft 상태의 정당한 사전-서명
finalize 봉인(예: health-trainer v0.1.0 자신의 발행 당시 상태)을 발행 자체에서
거부하지 않기 위해 PREVIEW는 비차단으로 유지한다 — D-D 서명(카탈로그 status
반대서명, §6-2)은 발행과 분리된, 더 나중의 별도 행위다.

**실사례 2 (health-trainer v0.1.0, 2026-08-23)**: 같은 export가 발행측 0 FAIL이면서
설치측은 D-D(`manifest status='draft' != 'published'`)로 거부됐다 — 모순이 아니라
검사 범위가 다른 것이다. gate (d)의 install-side PREVIEW는 정확히 이 상황을 발행
시점에 loud하게 보여주기 위한 것이다 (검토 에이전트 제안, DGN-1035 티켓 흡수).

### 6-2. D-D 반대서명 규약

설치 가능 상태의 앵커는 **두 곳의 일치**다:

1. 설치 단위 자신의 manifest `status: "published"` (자기선언)
2. 프레임워크 카탈로그(`packs/catalog.json`)의 동일 `id` row가 같은 status (반대서명)

둘 중 하나라도 어긋나면 설치측 D-D FAIL → 설치 거부 + payload 코드 실행(C6) 억제.
`compat-lint.sh:1429-1469`. 카탈로그는 프레임워크 레포에 있으므로, **카탈로그 row를
published로 뒤집는 머지 권한자(= 프레임워크 레인 sole-merge)가 발행 승인권자다** —
이것이 코드가 구현하는 발행 권한의 전부다.

status 결정에는 **silent promotion이 없다**: 우선순위 = 명시적 `--catalog-fields-in`
status > 기존 row status > (신규 row일 때만) 모드 기본값. `pack_publish.sh:655-668`

**좌표 단일원 (DGN-1079 RR1).** 설치 단위의 위치는 카탈로그 row의 `package_dir` 하나뿐이며,
상대경로는 **그 카탈로그 파일이 있는 디렉터리** 기준으로 해석된다 (절대경로는 그대로).
발행기와 설치기가 **같은 resolver**(`scripts/pack/lib/pack_coords.sh`)로 이 값을 읽는다 —
발행기는 이 필드를 **되쓰지 않는다**. 되쓰면 `"../../dogany-lifekit"` 같은 독립 레포 좌표가
발행 1회로 설치 단위 id 로 회귀하고 다음 설치가 깨진다(DGN-1079 P1). 카탈로그 row가 아직 없는
신규 설치 단위만 `<packs-dir>/<pack-id>` 생성 기본값으로 필드를 **시딩**한다(기존 값이 있으면 no-op).

같은 이유로 `pack_version` 은 **카탈로그에 재주입되지 않는다** — 설치 단위 자신의
`pack-manifest.json` 이 버전 정본이고 `pack_install.sh` 가 거기서 폴백한다
(DGN-227 B3/P6; 설치 단위 실물 3행의 `package_dir_note` 가 같은 말을 한다).

snapshot 모드는 payload 를 **재생성**한다(`rm -rf` 후 materialize)므로 `--packs-dir`
바깥으로 나가는 좌표를 **거부한다**(G-C1). 독립 레포 설치 단위는 `--mode finalize` 로 발행한다.

`deploy_owner` 필드는 카탈로그/manifest에 존재하지만 **코드 소비자가 0**이다 —
운영 관례(누가 어느 레인에 착지시키나)의 선언일 뿐 기계 강제가 아니다 (§8).

### 6-3. 설치 노출 (공개 인스톨러)

- `status=="published"` row만 메뉴에 든다. `install.sh:1768`
- `kind=="kit"` row는 도메인 에이전트팩 메뉴에서 제외 (킷은 post-mint `pack_install.sh`로 활성화,
  도메인 에이전트로 민팅되지 않는다). `install.sh:1776-1783`
- `menu_visible: false` row는 대화형 메뉴에서 제외되지만 `DOGANY_PACK_ID` 프리셋으로는
  설치 가능 (hidden ≠ forbidden). `install.sh:1785-1794,746-750`

## 7. 채널·태그·버전 lockstep

### 7-1. semver 문법 (단일원)

`scripts/pack/lib/semver_range.py`가 문법+만족성의 **단일원**이다 (compat-lint /
update.sh 가드 / update_plan 모두 이 모듈의 thin wrapper — 사본 재작성은 결함).

- range = 공백 구분 AND 토큰 나열; 토큰 = `(>=|>|<=|<|==|!=)X.Y.Z`
- `^` `~` 는 **명시적 미지원** (스펙 없이 키우지 마라)
- 버전 파스는 X.Y.Z prefix-match (`1.2.3-rc1` → (1,2,3))
- 모듈 부재/로드 실패 = 소비자 전원 fail-closed ("0건 검사 통과"는 없다)

`semver_range.py:9-16,30-47`, `compat-lint.sh:227-249`, `update.sh` 가드 주석.

### 7-2. 태그 네임스페이스와 채널

- 프레임워크: 안정 태그 `vX.Y.Z`. 인스턴스는 `DOGANY_UPDATE_CHANNEL`(기본 `release`;
  `main`은 개발 체크아웃 탈출구)로 소비하고, `resolve_channel_tag`가 태그를 해석하며
  `DOGANY_UPDATE_PIN`으로 계획된 후보에 고정할 수 있다.
  `update.sh:16,107-128,1245-1293`, `self-update.sh:113-130,493-519`
- 설치 단위: **`pack/<id>/v*`** 태그 네임스페이스 (설치 단위 소스 레포에). stable 채널의 pre-release
  필터는 마지막 `v` 뒤 버전 세그먼트에만 적용 (하이픈 있는 설치 단위 id가 배제되지 않도록).
  `update_plan.sh:22-26,203,510-516`
- 설치 단위 구독원: `~/.dogany/pack-sources.conf` 레지스트리 (중복 키 = loud FAIL).
  `update_plan.sh:13-15`

### 7-3. EXPECTED_USER_VERSION 3점 매치가 보장하는 것

킷 DB 스키마 버전의 세 좌표 — ① `payload/database/<kit>.py`의
`EXPECTED_USER_VERSION` (코드 pin) ② `migrations/NNN_*.sql` 최대 파일번호
③ `schema.sql`의 `PRAGMA user_version` — 가 **전부 같아야** 발행/설치를 통과한다
(C3, `compat-lint.sh:684-797`).

보장 내용: **코드가 기대하는 스키마 == 마이그레이션이 도달시키는 스키마 == 신규 씨딩
스키마.** pin만 올리고 마이그레이션 파일이 없는 킷(기존 설치가 도달 불가능해지는
케이스)은 발행 자체가 안 된다. 여기에 설치측 reverse-drift guard(§5-1-4)와
업데이트 드리프트 가드(§5-3)가 겹쳐, 이미 앞선 인스턴스를 낮은 payload가 덮는
방향도 차단된다. 설치 단위↔프레임워크 lockstep은 requires_framework(C2, 설치 방향) +
fw_reqframework_guard(§5-2, 업데이트 방향)의 쌍으로 완성된다.

## 8. v1 계약 밖 / 미정 (부재를 부재로 적는다)

코드에 **없는** 것들. "있어야 한다"가 아니라 "지금 없다"의 기록이다.

- **promote.sh / 3-스테이지 링의 staging 채널**: SoT 선언(sot/DEPLOY-DISCIPLINE.md)만
  있고 미구현 — 링은 현재 single-track. (채널 해석과 dev-tag 절차는 구현되어 있음.)
- **deploy_owner 기계 강제**: 필드 소비 코드 0건. 순수 선언.
- **`C-TAG` / `C-MODDEPS`**: 예약 명명 슬롯, 구현 0 (§3).
- **기계가 읽는 모듈 로스터**: DGN-1391 이 `requires_modules`(참조) + 킷 `modules`(로스터)
  칸과 설치측 게이트를 열었지만, **로스터를 실제로 선언한 킷은 아직 0개**다(실측
  2026-09-11). 즉 오늘 이 계약에서 `requires_modules` 를 쓰는 팩은 전부 BLOCK 되며,
  그것이 거짓 통과보다 정직한 상태다. 로스터를 채우는 것은 킷 소유자의 행위다.
- **모듈 런타임 레지스트리**: 없다. "이 인스턴스가 이 모듈을 보유하는가 / 소유권이
  누구에게 있는가"를 답하는 기계는 존재하지 않는다(§3의 `module_active()` /
  `DOGANY_MODULES` 폐기 행 참조). `requires_modules` 는 **설치 시점의 이름 실재**만
  검사하며 보유·활성·소유권을 주장하지 않는다.
- **`requires_modules` 의 풍부한 형태**(`{module, need, on_absent}` 엔트리): 미착지.
  `on_absent` 는 위 레지스트리 없이는 강제 불가능하므로 이번 착지는 **이름 배열**
  하나로 좁혔다. 확장은 additive 여지로 남는다.
- **kit→kit 의존**: 설치 의미론 미정의 = 거부 (C-KITDEP FAIL). 미래 확장 여지로 남김.
- **`id` 필드 문법 강제**: id는 D-D·카탈로그·config 소유자명으로 쓰이지만 자체 문법
  검사가 없다 (kind=pack에서 config/<id>.conf 경로로 쓰일 때도 그대로 통과).
- **contract_version 2**: 존재하지 않는다. 지금까지의 모든 확장(provides_kit,
  capabilities, kind/service_namespace, skills, units)은 additive로 v1 안에 남았다.
- 이 문서의 영문판: 미작성.

알려진 코드 내 드리프트 (코드 주석이 스스로 플래그한 것 포함):

- pack_publish의 `EXCLUDE_SUFFIXES=( .db .sqlite .sqlite3 )`는 compat-lint gate(a)의
  확장 세트(case-fold + `-wal/-shm/-journal` sidecar)보다 좁다 — compat-lint 쪽에
  결함으로 기록되어 있음. `compat-lint.sh:1185-1190`, `pack_publish.sh:66`
- compat-lint.sh 헤더 주석의 "Six checks"는 현행 슬롯 수(§4)와 불일치 — 주석 드리프트.
  `compat-lint.sh:5`
- skills-bundle 정적 allowlist는 주석에 "8 types"라 적혀 있으나 실제 배열은 **11종**
  (ledger-setup/ledger-session/ledger-log 추가분 미반영) — 주석 드리프트.
  `compat-lint.sh:823-836`

## 9. 동결 선언과 개정 절차

1. 이 문서는 **설치 단위 계약 v1의 정본**이다. 계약을 알기 위해 코드 고고학이 필요하면
   그것은 이 문서의 버그다 — 티켓으로 정정하라.
2. **문서는 기록이지 설계가 아니다.** 이 문서에 새 필드·게이트를 먼저 적어서 계약을
   바꿀 수 없다. 순서는 언제나: 설계(그릴) → 코드 게이트 착지 → 이 문서 갱신.
   코드와 문서가 어긋난 기간에는 코드가 정본이다.
3. **additive 확장** (기존 설치 단위이 전부 그대로 통과하는 필드/슬롯 추가)은
   `contract_version: 1`을 유지한다 — DGN-1002(provides_kit/capabilities),
   DGN-1018(kind/service_namespace), DGN-956(skills), DGN-783 B4(units),
   DGN-1143(skills[].winner), DGN-1391(requires_modules / kit `modules`),
   DGN-1496(hooks[] 선언 배선), DGN-1374(completion_verb),
   dsp-hslot(handover_brief_verb, 2026-09-18), DGN-1632 W3(tone_candidates) 선례.
   기존 설치 단위을 깨는 변경만 contract_version 범프 사유다.
   (DGN-1143 의 provides_kit 필수화는 형식상 breaking 이지만 **부재 모집단
   실측 0건** — 발행 전 태그·전 레포·전 라이브 어디에도 의존 매니페스트가
   없어, 실질 깨지는 설치 단위 0개로 v1 잔류 판정. 근거: DGN-1143 산출 보고.)
4. 게이트 변경 시 **verdict-identical 이중구현 규율** (§5-1-2)을 유지하라:
   compat-lint 쪽과 pack_install 인라인 쪽을 항상 같은 커밋 계열에서 함께 바꾸고
   lockstep 테스트(`scripts/tests/test-pack-install-kit.sh` 등)를 유지한다.
5. 계약 변경의 착지는 프레임워크 레인 규율을 따른다: 브랜치 작업 → 리뷰 →
   sole-merge 게이트 통과 → 릴리스 태그. 카탈로그 status 반대서명(§6-2)이 발행의
   최종 관문이다.

## 10. Base kit declaration (v2.7, dec-157)

status: **DECLARED 2026-10-10 (DGN-1982).** Written boundary only. This section
opens no compat-lint slot and no install gate; the code extraction is v2.8.
Per dec-157, a v2.8 mismatch with this section is a revision of this section,
not a v2.8 failure. It extends the DGN-783 base-kit frame contract (v1.31.5:
kit-owned migrate, atomic db-init, D-D publish signature, units seam) and the
§1.1 axis-slot principle; it does not start a new kit.

Rule 9.2 still holds for the gated parts of this document. This section is
the one place where the order is reversed on purpose (dec-157: declare in
v2.7, extract in v2.8), and it says so by carrying no enforcement claim.
Where it names current code, that is a location for v2.8, not a gate.

### 10-1. What the base kit is

The base kit is the part of the framework that **every** agent carries,
whatever kit it runs (lifekit, devkit, a third-party kit). A kit is installed
on top of it. Today the base kit is not a package: it is spread over
`agents/.template/` (routines, bridge, config, rules) and `scripts/`. v2.8
extracts it behind the seams below.

Test for "is this base": **would an agent with no kit installed still need
it?** Yes -> base. Only a kit's work needs it -> that kit.

### 10-2. Resource layer split

Every agent on a host draws from the same physical and paid resources. The
base kit owns the **books** for them: the one place a resource is read,
cached and reported. A kit consumes the books; it never keeps a second copy
of a base resource's state.

| Resource | Layer | Source of truth (read, never hand-typed) | Scope of the book |
|---|---|---|---|
| Wall clock / time zone | BASE | OS clock + configured TZ | host |
| Owner time (attention: asks, pushes, decision menus) | BASE | owner-turn record + Why-net ask budget | owner |
| CPU / load | BASE | OS load average, process table | host |
| Memory / swap | BASE | OS memory and swap counters | host |
| Storage / file churn | BASE | data-volume usage, write counters | host |
| Machine concurrency queue (slot primitive) | BASE | `~/.dogany/cron-queue/<class>/slot-N` | host |
| Claude subscription 5h / 7d windows | BASE | the account's live usage endpoint | account x host |
| Claude reset passes | BASE | the user's pass inventory (user setting via `/passes`) | account |
| Cloud sessions (claude.ai cloud runtime) | BASE | the cloud environment's own state; exhaustion flag from the runtime's refusal | account |
| API credit (monthly pool) | BASE | the plan read + the org cost report; the API's own "balance too low" answer | account (org) x host |
| Codex subscription (CLI weekly window) | BASE | the CLI app-server quota read | account x host |
| Codex reset passes | BASE | the user's pass inventory | account |
| Codex image generation | BASE | the same Codex account read (no reader today) | account x host |
| Test slots (heavy-test / worker-test classes) | DEV KIT | the queue classes the dev kit declares on the BASE slot primitive | host |
| CI minutes (GitHub Actions monthly budget) | DEV KIT | the CI meter ref written by the CI verdict writer | repo account |
| Worktrees | DEV KIT | `git worktree list` of the target repo | repo x host |
| Repos / remotes | DEV KIT | the repo's own remote config | repo |
| Release lanes | DEV KIT | the unit's `product.yaml` `lanes:` block | unit |

Split rule: **all LLM consumption is BASE**, whatever kit spends it. A dev
dispatch and a life reminder draw from the same Claude window; one book
answers both. The dev kit owns only what exists because software is being
built (tests, CI, worktrees, repos, releases).

The slot primitive is base and the classes on it are kit-declared: a kit
says "my heavy job is class `heavy-test`, 1 slot"; the base queue enforces
it across every instance root on the host.

#### 10-2-1. Rules every BASE resource book follows

1. **Read, never typed.** The value comes from the provider (console, plan
   read, live endpoint, OS counter). A config key may only **override** a
   read (a ceiling, an off switch) or record a **user-owned fact** the
   provider does not expose (reset-pass inventory, written through its own
   verb after the user confirms). A config key never stands in for a value
   the provider can be asked for.
2. **One book per account per host** (DGN-1978 / DGN-1983). The book key is
   `(provider, account, host)`. Two agents on one host under one account
   share one book; one agent switched to another account gets another book.
   A book never mixes two accounts' numbers.
3. **Re-read on change.** A book is invalidated and re-read, not carried
   over, when any of these change:
   - account (login, relogin, credential rebind);
   - plan (upgrade / downgrade; the plan slug is itself a read where the
     provider offers one);
   - provider policy (window lengths, credit rules, a new error shape);
   - the reset of a window (the provider's `resets_at`, not a local timer).
   On a failed read the book serves the last value **with its age** inside a
   stale bound and fails closed past it; it never invents a value.
4. **Consumers do not branch on kit.** The host dispatcher (DGN-1979) reads
   the BASE books with no dev-kit-only assumption: no "repo", "worktree" or
   "CI" term appears in a BASE book's schema.
5. **The base never spends on the user's behalf.** It does not redeem a
   pass, buy credit or change a plan. It reports and plans; the user acts.

#### 10-2-2. DEV KIT resource rules

- CI minutes: a monthly budget per repo account; when spent, CI writes an
  error verdict and the local lane runs instead (2026-10: spent). The budget
  number is a dev-kit config default until a billing read exists.
- Test slots: the dev kit declares its queue classes and slot counts; it may
  not create a second queue home.
- Worktrees and release lanes: owned and cleaned by the dev kit; the base
  only provides the machine numbers (storage, churn) that tell it when.

### 10-3. Rest of the boundary

| Piece | Owner | Declaration |
|---|---|---|
| Module slots `setup` / `log` / `session` | BASE (slot) / kit (filling) | The v2.6 contract's Why / What / spiral slots. The base kit ships the slots and their skill bundle types (`ledger-setup`, `ledger-session`, `ledger-log`); a kit fills them. A kit may not add a fourth base slot; it may add its own module slots in its `modules` roster (§8). |
| Axis slots | BASE (slot) / kit (count) | §1.1: the base gives axis slots and the judging question, never an axis list. |
| Why net common part | BASE | The Why net (dec 944 line; v2.7 T1 LOCKED spec) is base kit common part: the store, the owner-turn resolver, the ask budget and the morning ask. A kit may register Why sources and surfaces through the existing seams; it may not keep its own Why store or its own ask budget. |
| Term layer (i18n + term dictionary, DGN-1980) | BASE (loader, framework layer) / kit (its own layer) | Layered lookup framework -> kit -> module (dec-204, DGN-1500). A kit overrides only keys it needs, in its own layer; a framework update never touches a kit layer. A kit may not ship framework-layer keys. |
| Secret store (DGN-1981) | BASE | One store and one resolver for credentials (OAuth credential, API keys, admin key, bot tokens), plus the one secret-shape scanner. A kit names a secret it needs; it never reads a keychain, file or env var for a secret directly, and never writes one. |
| Resource books (10-2) | BASE | As above. |
| DB lane, schema, CLI surface | kit (`kind=kit`) | Unchanged from §1-§2. |
| Behavior on top of a kit | pack (`kind=pack`) | Unchanged from §1. |

#### 10-3-1. What a kit may own

- Its DB lane, schema, migrations and CLI surface (§1, DGN-783 B1).
- Its axes (§1.1), its module roster and the filling of the base slots.
- Its term-layer keys, its config file, its skills.
- DEV-KIT-class resources it declares (the devkit: test slots classes, CI
  budget, worktrees, repos, release lanes).
- Consumption of BASE books through their read API.

#### 10-3-2. What a kit may not own

- Any BASE resource book or a cache of one (no per-kit usage snapshot, no
  per-kit credit ledger, no per-kit machine poller).
- A second Why store, a second ask budget, or owner-time policy.
- Secrets: storage, lookup, or a direct keychain / credential-file read.
- Framework-layer term keys or the term loader.
- The machine concurrency queue itself (it declares classes on it).
- Anything that spends money or redeems a pass.

### 10-4. v2.8 extraction map

Current locations each declared piece is extracted from (grep of
`dsp/v27-t1-integrate`, 2026-10-10; `T/` = `agents/.template/`).

| Piece | Current code location(s) |
|---|---|
| Wall clock / TZ | `T/routines/lib/tz_offset.sh`; each script reads the OS clock directly today (no shared reader) |
| Owner time | `T/routines/lib/why_net/budget.py`, `T/routines/lib/owner_turns.py`, `T/routines/lib/owner_session.py`, `T/routines/lib/why_owner_turn.py`, `T/routines/decision-ledger.py` |
| CPU / memory / storage | `T/routines/health-observer.py` (load, disk, swap alerts), `skills/dogany-mac-load-check/loadcheck.sh`, `skills/dogany-mac-optimize/`, load reads in `T/routines/dispatch-detached.sh` |
| File churn | none today (no reader) |
| Slot primitive | `T/routines/cron-guard.sh` (`--queue/--slots`, `~/.dogany/cron-queue`) |
| Claude 5h / 7d | `T/routines/claude-usage.sh` (machine-wide snapshot, credential resolution), `T/routines/usage-gate.py`, `T/routines/usage-gate.sh`, `T/routines/lib/runtime_headroom.py`, `T/routines/consolidate-0430.sh` (usage defer), `PLAN=` in `T/config/agent.conf` |
| Reset passes | `T/routines/lib/reset_passes.py`, `T/bridge/passes.py`, `RESET_PASSES_*` in `T/config/agent.conf` |
| Cloud sessions | `T/routines/lib/cloud_routine.py`, `T/routines/lib/cloud_completion.sh`, cloud lane + `.cloud-exhausted` in `T/routines/dispatch-detached.sh`, `CLOUD_*` in `T/config/agent.conf`, `scripts/cloud/env-setup.sh` |
| API credit | `T/routines/lib/api_credit_pool.py`, `API_CREDIT_*` in `T/config/agent.conf`, claude-api lane in `T/routines/dispatch-detached.sh` |
| Codex CLI window | `T/routines/codex-ratelimit.py`, `codex_quota_gate` in `T/routines/dispatch-detached.sh`, `T/routines/lib/runtime_headroom.py` |
| Codex image generation | none today (no reader) |
| Per-runtime dispatch counters | `T/routines/dispatch-detached.sh` (`active-count*` files), `T/routines/lib/dispatch-*.tsv` |
| Test slots (dev kit) | `git-hooks/run-tests.sh` + `T/git-hooks/run-tests.sh` (DGN-1878 classes), `scripts/dev/test-cpu-report.sh` |
| CI minutes (dev kit) | `ci/lanes.sh` (`CI_MINUTES_BUDGET`), `ci/verdict-write.sh`, `ci/verdict-read.sh`, `.github/workflows/lanes.yml` |
| Worktrees (dev kit) | `--worktree` path in `T/routines/dispatch-detached.sh`, `skills/dogany-mint-agent/scripts/v2-ctl.sh` |
| Release lanes (dev kit) | `scripts/release.sh`, `scripts/promote.sh`, `scripts/lane-lint.py`, `scripts/release-approval.py`, `product.yaml` |
| Module slots | `scripts/pack/compat-lint.sh` (skills-bundle types `ledger-*`, `modules` roster), `scripts/pack/pack_install.sh`, `T/routines/lib/kit_subset.py` |
| Why net | `T/routines/why-net.py`, `T/routines/lib/why_net/`, `T/routines/lib/retro_slot.py` |
| Term layer | `T/routines/lib/i18n_lookup.py`, `T/config/i18n/`, `T/routines/lib/term_dict.py`, `T/config/terms/`, `T/routines/lib/owner_term.py`, `T/routines/lib/first_contact_terms.py`, `T/routines/lib/voice_vocab.py`, `T/bridge/i18n/` |
| Secret store | credential reads in `T/routines/claude-usage.sh`, `T/routines/usage-gate.sh`, `T/routines/lib/api_credit_pool.py`, `T/routines/dispatch-detached.sh`, `T/routines/browser-auth-open.sh`, `T/routines/self-update.sh`, `T/bridge/auth_login.py`, `skills/dogany-relogin-rebind/token-sync.sh`; scanner `T/routines/lib/secret_scan.py`, `T/scripts/secret-sweep.sh`, `T/config/secret-patterns.conf` |

#### 10-4-1. Known gaps against 10-2-1 (recorded; re-read on change closed by DGN-1979 phase 2)

- **Account scope.** The Claude usage snapshot is keyed per OS user
  (`~/.dogany/usage/`, comment: "one Claude account per macOS user"), not per
  account. A host with two accounts under one OS user shares one book.
  This is the DGN-1978 / DGN-1983 seam.
- **Hand-typed values** (narrowed 2026-10-10, DGN-1979 phase 2, dec-359).
  `PLAN=` is now an **override of a plan read** where one exists: the plan
  slug is read from the Claude credential (`claudeAiOauth.subscriptionType`
  + `rateLimitTier`: `max_5x`, `max_20x`, `pro`, `team_*`, `enterprise`,
  `free`), and both the usage gate thresholds and the API credit budget use
  that read. Where there is **no read** (no signed-in account on the host, a
  credential without a recognizable tier), `PLAN=` keeps working exactly as
  before (unset -> `pro` for the gate, no credit for the pool). The budget
  source text says which applied ("plan read max_20x", "PLAN=max_5x override
  (plan read max_20x)", "PLAN=max_5x (no plan read)").
  `API_CREDIT_MONTHLY_USD` stays as-is: no provider endpoint returns the
  monthly grant amount, so there is no read for it to override; it remains
  the override over the plan-derived budget. `CI_MINUTES_BUDGET` (dev kit)
  is still a typed default for the same reason.
- **Codex image generation** and **file churn** have no reader today.
- **Re-read on change -- closed 2026-10-10 (DGN-1979 phase 2, dec-359).**
  Each BASE book stores the identity fingerprint it was computed under
  (`T/routines/lib/book_identity.py`; `.book-<book>.json` next to the book)
  and every read compares it first. The fingerprint is sha256 (12 hex) over
  non-secret components only: account ids are fingerprinted before they are
  stored, keys and tokens are never read into it.

  | Book | Identity components | On change |
  |---|---|---|
  | Claude usage snapshot (`~/.dogany/usage/`, `claude-usage.sh`) | Claude account fp (`oauthAccount.accountUuid`), plan read | snapshot + 429 back-off to history; the same call reads live |
  | Workspace usage-gate copy (`.claude/.usage-cache.json` + history) | as above + `PLAN=` | cache + burn history to history; next read live (no mixed-account burn rate) |
  | State root input `DOGANY_USAGE_STATE_DIR` (usage-gate.py) | unset = workspace `.claude`; set = relocates cache, history, ledger + lock, sentinel, book-history | none; test/sandbox callers only (DGN-2168) |
  | Claude / Codex reset-pass stores | account fp (Claude `accountUuid`, Codex `account_id`) | the store is per account already; the switch is recorded, the old store stays as it is |
  | Codex weekly window | `codex login status` state + method, `account_id` fp, the read `windowDurationMins` | the quota is read live on every gate (no payload); the switch is recorded and the host lanes follow |
  | API credit account (`~/.dogany/api-credit/<key fp>/`) | key fp (pointer `current.json`) | key rotation: the old dir gets `superseded.json` and stays as history; the new dir starts with no marker / reconcile state, so the API answer and the console report are re-read |
  | API credit account, plan / policy | plan read, `PLAN=`, `API_CREDIT_MONTHLY_USD`, `API_CREDIT_SEATS`, `API_CREDIT_SEAT_USD`, `API_CREDIT_RESERVE_USD` (env > conf) | `exhausted.json` + `reconcile.json` to history, re-read on the same decision; the ledger rows are spend facts and stay |
  | Host dispatcher lanes (`runtime@account fp`) | the ticket's account re-resolved from its own env pointers (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`; claude-api: the current key fp) | a queued ticket moves to the new account's lane with its age, skips and tier, and drain re-scores it there; it is never dropped |

  On a change the old payload moves to `book-history/<book>/<utc>-<old fp>/`
  (files 0444, never read back as current) and ONE line
  `{"kind": "book-change", book, from, to, changed: [{what, from, to}],
  invalidated, history}` lands in the host decision log
  (`~/.dogany/dispatcher/decisions.jsonl`); a moved ticket is a `relane`
  line in the same log. The first identity seen is adopted silently.
  Provider policy reads: the Codex window length is a read and part of the
  identity; Claude publishes no window length (5h / 7d are constants, the
  window reset is the provider's `resets_at`), so no Claude window length is
  in it. A new error shape is still a code change, not a read.
  Cost: the Claude account and credential-file plan are stat-cached
  (`~/.dogany/books/identity-cache.json`); a keychain plan read is retaken at
  most once a minute and only while an account is signed in; `codex login
  status` runs once per dispatch, never on a hook path.
- The usage readers fail open in places (usage gate) and closed in others
  (snapshot past STALE_MAX); v2.8 states one direction per consumer.

---

### 10-5. `base_contract` field (DGN-2028) -- validated, unlike 10-1..10-4

status: **VALIDATED at install (v2.7 build step 1).** This subsection, and
only this subsection, has an enforcing check. The rest of section 10 stays
declaration-only as stated at its top; adding this field gave no other part
of section 10 any enforcement.

What enforces it: one validator, `agents/.template/routines/lib/base_contract.py`
(`validate`). The install record (`_base_contract_record`, both the kit and
the pack call sites of `pack_install.sh`) runs it and fails the install on a
defect; the runtime reader (`load_installed`) re-runs the same function over
installed records and skips one that no longer validates. compat-lint has no
slot for it. `base_contract.py check <manifest>` runs the check without writing.

What the validator checks (shape and references only):

- `version` is 1; unknown keys anywhere are refused.
- `defaults.problem_line` is `on` or `off` when present.
- `targets[]`: unique `id` (`^[a-z][a-z0-9_-]{0,63}$`); `unit_ref` names a
  `base_units.resource_units[].key` of the same manifest (when the manifest
  declares no `base_units`, the reference is shape-checked only and the
  install logs a WARN); `why_scope` uses the existing Why scope syntax
  (`framework` / `kit:<x>` / `product:<x>`); a `base:` reference is refused
  (base resource books are not kit units).
- `bindings[]`: `slot` is a base slot (`why decision problem work_unit
  target_state resource constraint goal explanation context`) or a proposal
  destination (`procedure`, `profile`: never `mode: write`); `target` is a
  declared target; one home per slot/target inside one contract; `mode`
  `reference | write | none`; `none` carries no home; otherwise `ownership`
  `kit | crew`, a declared `provider` offering `lookup`, `on_missing`
  `handoff | hold` (default `handoff`). A `write` binding's provider must
  declare `write_door`, `capability_probe` and `receipt_lookup`.
- `providers{}`: instance-relative `adapter` (no absolute path, `~`, `.` or
  `..` segment), `operations` a nonempty subset of `lookup changes evidence`,
  `protocol` 1. At record time every declared path must exist inside the
  installed instance.
- `workflows[]`: unique `id`, declared `target`, `cues` = language key
  (`^[a-z]{2}$`) -> 1-32 one-line cue strings, `stakes_hint`
  `low | normal | high`, `evidence[]` of `{"kind": "unit", "ref": <unit key>}`.

What it does NOT check or do (later build steps): cross-kit overlap holds
(`contract_overlap`), a kit lowering a base workflow's stakes, provider
execution or protocol answers, routing, and any write. Installing a contract
runs no adapter.

#### Ordered turn sources (FDGN-457, build steps 1-3)

The optional `sources` array extends version 1 additively (at most 32 per
contract). Each entry declares `id`, `kind` (`skill`, `module`, `kit`,
`framework`, or `owner-file`), an instance-relative `path`, and a declared
`target`. Optional `slots` limits ownership to those base slots; an empty
list owns the target across slots. Optional `cues` uses the workflow cue
format. Targets may also declare `cues`, for topic aliases independent of
source wording. For example:

```json
{
  "version": 1,
  "targets": [{"id": "allocation", "cues": {"en": ["allocator", "seat assignment"]}}],
  "sources": [
    {"id": "allocation-guide", "kind": "kit", "target": "allocation",
     "path": "service/example/allocation.md"}
  ]
}
```

The existing memory hook resolves these installed records before recall,
then renders one source block in the turn frame: skill -> module -> kit ->
framework -> memory. Target aliases, source cues and workflow cues select
the applicable sources. Owner files are read at the framework tier; global
framework safety stays active. The hook runs no provider: provider-backed
homes need local snapshots declared as sources. A provider home without one
is explicitly unavailable, never absent. Source files must resolve inside
the instance and be regular UTF-8 files (8 KiB each, 64 KiB total, at most
32 source reads per turn, 128 resolved homes, under the existing
frame/whole-hook time budgets).

Recall consumes the same target/slot ownership map. Structured memory
`target`/`slot` fields or `target=... slot=...` tags resolve directly;
declared aliases resolve otherwise worded facts without requiring overlap
with the source prose. Unknown slots in slot-limited homes retain unresolved
evidence. A loaded
home suppresses the memory line. Unavailable homes, ambiguous targets and cross-pack overlaps
retain separately labelled unresolved evidence; neither silently appoints
memory or an alphabetically first kit as the authority. Facts whose target
cannot be resolved retain the existing relevance and curation checks.

`TURN_FRAME` controls source reads and rendering; `RECALL_ROUTES` controls
ownership-based memory admission. Both default off. The existing canonical
UserPromptSubmit memory hook is the only composer: no new outer hook or
provider invocation is needed. Why/decision answer-resolution hooks and the
ledger computation point/deadlines remain unchanged. Install records use
the shared recorder on both pack-install paths; normal framework delivery
already carries `routines/lib/*.py`, `memory-engine/*.py` and canonical
settings. Actual kit declarations and their snapshots are build step 4;
enabling flags alone cannot supply a kit that declares no sources.

## 부록 A. 설치 단위 실물 3종 대조 실측 (2026-08-24, framework 1.43.0)

| 설치 단위 | kind | 매니페스트가 쓰는 계약 필드 | publish-side lint 실측 |
|---|---|---|---|
| lifekit 1.3.1 (`dogany-lifekit/pack-manifest.json`) | kit | contract_version 1, requires_framework `>=1.30.0 <2.0.0`, provides_kit `lifekit`, skills 6종(전부 own), payload_root, status published | **FAIL 81건** — 전부 워킹트리 런타임 잔재: C4b `__pycache__`/*.pyc 다수 + C5 gate(a) `payload/mirror/mirror_state.db`. 소스 체크아웃 오염이며 발행 태그 시점 상태와 다를 수 있음 → 정리 대상으로 별도 보고 |
| health-trainer 0.1.1 (`dogany-agentpack-health-trainer/pack-manifest.json`) | pack | contract_version 1, requires_kit `{lifekit, >=1.1.0 <2.0.0}`, capabilities.db_lane false, service_namespace `health`, status published | **FAIL 1건** — C4b `service/health/__pycache__/ax_engine...pyc` (동일 클래스 잔재). 나머지 전 슬롯 문서대로 판정 (C-KITDEP PASS, C3/C6 클래스 SKIP, C4 PASS) |
| dev 1.2.0 (`dogany-agent/packs/dev/pack-manifest.json`) | pack | contract_version 1, requires_framework `>=1.42.0 <2.0.0`, capabilities.db_lane false, service_namespace `dev`, status published, 카탈로그 row menu_visible false | **ALL CHECKS PASS** |

필드 대조 특이점: lifekit manifest는 단위 어휘를 `base_units`로 선언하는데 compat-lint
C1의 B4 블록은 `units` 키를 본다 (`compat-lint.sh:322-330`) — 따라서 lifekit은 "no units
block" WARN을 맞는다 (FAIL 아님). 필드명 불일치는 코드-실물 간 알려진 드리프트로 기록.

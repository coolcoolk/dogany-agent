# Dogany 팩 계약 v1 (정본)

`contract_version: 1` — 이 문서는 Dogany 팩(킷/팩)이 프레임워크와 맺는 계약의 **정본**이다.
서드파티 저작자는 이 문서만으로 팩을 저작·발행·설치할 수 있어야 한다.

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
1. 팩 클래스와 계약 진입 조건
2. 매니페스트 필드 전수 표
3. 폐기 어휘 (쓰지 마라)
4. compat-lint 게이트 슬롯 전수 표
5. 설치·업데이트 게이트
6. 발행 권한과 서명 (D-D)
7. 채널·태그·버전 lockstep
8. v1 계약 밖 / 미정
9. 동결 선언과 개정 절차
부록 A. 실팩 3종 대조 실측

---

## 1. 팩 클래스와 계약 진입 조건

팩의 정체는 `<pack-repo-root>/pack-manifest.json` 하나가 선언한다.
(설치기는 manifest 없는 팩을 거부한다: "every pack must declare its categories -- no legacy fallback",
`pack_install.sh:373-374`)

계약 클래스는 2종뿐이다 (`compat-lint.sh:344-371` C-KIND):

| kind | 뜻 | 대표 실물 |
|---|---|---|
| `kit` | DB 레인·스키마·CLI 계약면을 **제공**하는 팩 (구조 킷) | lifekit |
| `pack` | 행동 팩. 킷 위에서 동작하며 DB 레인을 **소유하지 않는다** | health-trainer, dev |

진입/우회 경계:

- **매니페스트 파스 게이트**: 파일이 존재하는데 JSON 오브젝트로 파스되지 않으면
  즉시 C1 FAIL + ABORT (fail-closed; 손상 JSON은 legacy 팩이 아니다). `compat-lint.sh:161-181`
- **legacy-grace**: `contract_version` 부재 **AND** `kind != "kit"` 인 팩은 구계약(pre-v2) 팩으로
  간주, 검사 전체 SKIP 후 exit 0. **킷은 grace 없음** — contract_version 없는 kit은 항상 FAIL.
  `compat-lint.sh:199-207` (설치기의 동일 경계: `pack_install.sh:444-449`)
- 설치 경로 분기: `kind=kit` 또는 (`kind=pack` AND `contract_version` 존재) → 계약(payload_root)
  파이프라인. 둘 다 아니면 legacy agent/module 경로. `pack_install.sh:828-841`
- `"agentpack"`은 **레포 명명 세그먼트**이지 manifest kind가 아니다. kind에 쓰면
  C-KIND FAIL + ABORT. `compat-lint.sh:57-64,356-368`

## 2. 매니페스트 필드 전수 표

compat-lint / pack_install이 실제로 읽고 강제하는 필드의 전수. "필수"는 계약팩
(contract_version 보유) 기준.

| 필드 | 필수/선택 | 문법 | 위반 시 잡는 게이트 |
|---|---|---|---|
| `contract_version` | 필수 (계약팩 정의 그 자체) | 정수 (현행 `1`) | 부재: kit이면 C1 FAIL, pack이면 legacy-grace로 강등 `compat-lint.sh:293-295,199-207` |
| `pack_version` | 필수 | `^[0-9]+\.[0-9]+\.[0-9]+$` | C1 `compat-lint.sh:290-291,301-305` |
| `requires_framework` | 필수 | semver range (§7 문법) | C1 (문법) `compat-lint.sh:296-298,308-312`; C2 (만족성) `compat-lint.sh:619-635`; 프레임워크 업데이트 시 fw_reqframework_guard `update.sh:1467` |
| `kind` | 필수 | 정확히 `"kit"` \| `"pack"` | C-KIND FAIL+ABORT `compat-lint.sh:344-371`; 설치측 인라인 재검증 `pack_install.sh:889-892` |
| `id` | 사실상 필수 (카탈로그 resolve·D-D 대조 키·pack의 config 소유자명) | 문법 강제 **없음** (§8 참조) | D-D 대조 `compat-lint.sh:1282-1297`; config/`<id>`.conf 소유 `compat-lint.sh:852-855`; requires_kit 자기의존 비교 `compat-lint.sh:584-590` |
| `payload_root` | 선택 (기본 `"payload"`) | 팩 경계 내부 경로 | 경계 이탈 = C1 FAIL+ABORT `compat-lint.sh:640-658` |
| `provides_kit` | **kind=kit 필수** (DGN-1143: legacy 기본 `"lifekit"` **제거** — 의존 모집단 실측 0건이었고, dec-145 개명 후 폴백은 존재하지 않는 킷을 조용히 가리킨다) | `^[a-z][a-z0-9_-]{0,31}$` + 예약어 금지 (§2.1) | **부재 = C1 FAIL+ABORT** (DGN-1143); 위반 = C1 FAIL+ABORT; **kind=pack이 선언하면 C1 FAIL** (S4b 대칭: provides_kit ⇔ kind=kit); 설치측 lockstep `pack_install.sh` KIT_NAME 블록 + `update_apply.sh` 크루 eff_kit 검증 |
| `capabilities` | 선택 | 객체, 허용 키는 `db_lane` (bool) 뿐 | malformed = C1 FAIL `compat-lint.sh:426-475`. kit: 부재/true → DB 레인 on, false → C3/C6 SKIP(+모순검사). pack: 부재 → false, **true 선언 = C1 FAIL** (킷 전용면) `compat-lint.sh:453-459`; 설치측 twin `pack_install.sh:923-938,995-1031` |
| `service_namespace` | kind=pack 선택 | provides_kit과 동일 문법 (재사용, 새 문법 없음) | kind=kit 선언 = C1 FAIL (이중 진리원) `compat-lint.sh:512-514`; requires_kit.kit과 동일 = FAIL (의존킷 사칭) `compat-lint.sh:521-522`; 선언했는데 `payload/service/<ns>/` 부재 = FAIL (죽은 선언) `compat-lint.sh:663-665`; 설치측 twin `pack_install.sh:907-921,952-954` |
| `requires_kit` | kind=pack 선택 | 객체, 키는 정확히 `{kit, range}`; kit은 provides_kit 문법, range는 semver range | 형식 = C-KITDEP `compat-lint.sh:540-616`; **kind=kit 선언 = FAIL** (kit→kit 의존 미정의=거부) `compat-lint.sh:572-574`; 자기의존 = FAIL `compat-lint.sh:584-590`; **만족성은 설치측 전용** (§5-1) |
| `skills` | 선택 | `[{"name": "<payload skills-bundle 디렉터리>", "sharing_mode": "share"\|"own", "winner": "<pack-id>" (선택, DGN-1143)}]` | C7: 어휘/중복/죽은 선언/템플릿 충돌 + winner 문법 `compat-lint.sh` C7 블록; C4 allowlist 확장; 설치 동작 §5-6. **`winner`**: 같은 스킬 이름을 둘 이상의 팩이 배송할 때(팩간 충돌) 정본 배송자를 선언 — 충돌 게이트(`lib/skill_collision_check.sh`, pack_publish GATE (e) + pack_install 인라인)는 **배송하는 모든 팩이 같은 winner(배송자 중 하나)를 선언할 때만** 통과. 발행측(strict)에서는 충돌 없는 스킬의 winner 선언 = FAIL(죽은 선언). 판정 출력에 모수 필수: `skills checked: N, collisions: M` (N=0 은 "대상 없음/n-a" — 통과 문면과 갈림, DGN-1142 §3.1) |
| `units` | kit 권장 (SHOULD) | `{primary, set}` 객체 | 부재 = **WARN만** (FAIL 아님; 표시가 unit.generic으로 폴백) `compat-lint.sh:318-330` — lifekit 실물 불일치는 부록 A |
| `status` | 발행 팩 필수 | 설치 가능 상태는 `"published"` 하나 | 설치측 D-D 게이트 (§6) `compat-lint.sh:1276-1297` |
| `deploy_owner` | 선택 | 자유 문자열 | **코드 소비자 0 — 기계 강제 없음** (선언용; §8) |
| `name_en` / `name_ko` 등 표시 필드 | 선택 | 자유 | 강제 없음 (카탈로그/메뉴 표시용) |

legacy(agent/module 경로) 전용 필드 — 계약팩 파이프라인에서는 사용되지 않는다:
`reference_slug`, `reference_root`, `reference_home`, `agent_md_marker`, `agent_conf_marker`,
`domain_seed`, `categories`, `knowledge`. 정의: `pack_install.sh:49-70`.

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
| `config/<소유자>.conf` + `config/i18n*` | 소유자 = provides_kit | 소유자 = id `compat-lint.sh:852-855,976-1004` |
| `mirror/*` (소스만; db/pyc/html 잔재 금지) | 허용 | **금지** `compat-lint.sh:1005-1029` |
| `knowledge/*` | **금지** (소비자 0 표면) | 허용 `compat-lint.sh:1030-1041` |
| `requirements.txt` (payload 루트만) | 허용 (파이썬 의존 선언, pack_deps_provision.sh 소비) | 동일 `compat-lint.sh:891-895` |
| `bridge/` `memory-engine/` `scripts/` | **절대 금지** (프레임워크 코어) | 동일 `compat-lint.sh:1042-1046` |
| 그 외 루트 / 경로탈출 / 경계 밖 symlink | 금지 | 동일 `compat-lint.sh:865-886,1047-1051` |

## 3. 폐기 어휘 (쓰지 마라)

아래 어휘는 **폐기 확정**이다. 재발명 금지. 근거: DGN-681-S4b-RATIFIED (실물 착지 어휘 중
`requires_kit` 1종만 비준, 2종 제거; estate 전체 0-hit 검증 완료).

| 폐기 어휘 | 상태 | 대체 |
|---|---|---|
| `module_deps` | 폐기 (락 회수본 어휘; 코드 0-hit) | `requires_kit` |
| `pack_deps` | 폐기 (비준 거부) | `requires_kit` |
| `provides_modules` | 폐기 (매니페스트 필드 비준 거부 — 카탈로그 SoT와 모순) | 카탈로그 엣지로 이전 |
| `module_active()` / `DOGANY_MODULES` 씨딩 | 미구현·폐기 (TK-13 0바이트; `pack_install.sh:184` 주석 언급만 존재, 쓰기 없음) | — |
| `status: "official"` | 폐기 철자 | `"published"` 만 인식 (`pack_publish.sh:658-662` 주석; D-D·설치 메뉴 모두 published만) |
| `C-TAG` / `C-MODDEPS` | **존재하지 않는 슬롯** — 코드 0-hit. DGN-681 락 항목7/8의 예약 명명 슬롯일 뿐이며 한 번도 구현되지 않았다 (C-MODDEPS는 S4b-2 예약 유지) | — |

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
| **C4b** | 포장 잔재 위생: `*.bak*` `*~` `*.orig` `*.swp` `*.pyc`, `_archive`/`__pycache__`/`.pytest_cache` 경로 성분 | FAIL | 양측 | `compat-lint.sh:1060-1116` |
| **C7** | skills[] 공유 선언: share\|own 어휘, 중복 금지, 죽은 선언 금지, share 스킬의 프레임워크 템플릿 번들 충돌 금지 | FAIL; 블록 부재=SKIP (전부 own) | 양측 | `compat-lint.sh:1118-1177` |
| **C5** | gate(a) 개인데이터/실DB: `memories` `USER.md` `.env` `.telegram_bot` 파일·디렉터리, transcript 디렉터리, 소문자화 basename 기준 `*.db` `*.db-wal` `*.db-shm` `*.db-journal` `*.sqlite*`; gate(b) 페르소나 토큰 잔재 `__AGENT_LABEL__` 등 4종 | FAIL | 양측 | `compat-lint.sh:1179-1261` |
| **C-RELNOTE** | **레포 ROOT 릴리스 노트**(`CHANGELOG.md` / `RELEASES.md` / `RELEASE-NOTES.md` / `releases/*.md`) 산문: 인용된 **오너 발화**가 이유 자리를 대신하고 있는가 (§6-1c 규칙의 기계 바닥). 술어·오탐 실측은 `lib/relnote_authorship.sh` 헤더 | FAIL; 루트에 릴리스 노트 부재=SKIP(무엇을 찾았는지 인쇄); 예측기가 어느 로케일에서도 카나리아를 못 보면 FAIL(무음 0 금지) | **발행측 전용** (설치측 SKIP) | `compat-lint.sh:1330-1427`, `lib/relnote_authorship.sh` |
| **D-D** | 발행 반대서명: manifest `status=="published"` AND 카탈로그 row의 동일 id가 같은 status (자기선언만으로는 앵커가 아니다) | FAIL (→ C6 억제: 미서명 payload 코드는 실행 금지) | **설치측 전용** (발행측 SKIP) | `compat-lint.sh:1429-1469` |
| **C6** | kit CLI 계약 동사 스모크: `payload/database/<kit>.sh check` + `dump` 종료코드 0 (PYTHONDONTWRITEBYTECODE=1로 자기오염 방지) | FAIL; D-D FAIL/미시드/kind=pack/db_lane=false → SKIP | 양측 | `compat-lint.sh:1471-1530` |

## 5. 설치·업데이트 게이트

### 5-1. pack_install.sh 가 실제로 거부하는 것 (계약팩 경로, 순서대로)

1. **requires_kit 만족성 게이트** (payload copy **이전**): 대상 인스턴스
   `.instance.conf`의 `DOGANY_PACKS=<id>@<ver>,...` 장부에서 `<kit>@` 항목을 찾아
   선언 range를 평가. 장부/항목 부재, 버전 파스 불가, range 불만족 → **BLOCK**
   (fail-closed, warn 레인 없음). 형식 검증은 C-KITDEP과 verdict-identical 이중구현.
   `pack_install.sh:482-634`
2. **인라인 재검증** (verdict-identical 이중검증): kind/provides_kit/service_namespace/
   db_lane/killstep 규칙을 설치기가 **자체적으로 다시** 검사한다 — behavior-pack
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
   `pack_install.sh:1222-1259` (kit 전용 — behavior-pack에는 kit_core가 없다)
5. **NM3 무결성 게이트**: 패키지의 `checksums.sha`(sha256, `<hex>  <relpath>`)와 payload
   대조. 불일치·결손 = FATAL (warn-continue 없음). checksums.sha 부재 = legacy/pre-NM3 팩
   loud WARN 후 진행. 게이트 본체는 **두 경로가 공유하는 함수 하나**
   (`pack_install.sh:203-277` `_nm3_verify`), 호출 지점은 계약팩 경로 `:1302-1309` /
   legacy agent·module 경로 `:2295-2300`. 두 경로 모두 **dry-run 종료 이후, 첫 payload
   쓰기 이전**에 돈다 (dry-run은 지킬 쓰기가 없으므로 무결성 게이트를 돌리지 않는다 —
   양 경로 동일).

   **정정 기록 (2026-08-25, DGN-1045 후속)**: 위 5번은 2026-08-25까지 **문서만 사실이었다.**
   NM3는 legacy 경로에만 인라인으로 존재했고, 계약팩 경로는 그보다 앞서 dispatch 되어
   `exit 0` 으로 끝나므로 **그 블록에 도달한 적이 없다.** 실측: 일부러 틀린 sha256을 적은
   `checksums.sha` 를 가진 kind=pack 픽스처가 rc 0으로 설치되고 로그·stdout 어디에도 NM3
   줄이 없었다. compat-lint는 양측 모두 payload 바이트를 해시하지 않으므로, **신규·서드파티
   팩이 전부 속하는 계약 클래스에 무결성 게이트가 아예 없었다** — 폐지 예정인 legacy
   클래스만 게이트를 갖고 있었다. `packs/dev` 의 stale seal이 4커밋 동안 조용했던 것은
   무해해서가 아니라 **보는 눈이 없어서**다(dec-128). 게이트를 공유 함수로 올려 양 경로에서
   호출하는 것으로 닫았다. **문서 서술이 옳고 코드가 틀린 쪽이었다.**

설치가 남기는 계약 기록 2종:

- `DOGANY_PACKS` 장부 upsert: `.instance.conf`에 `<id>@<version>` 원자적 기록
  (임시파일+mv). requires_kit 만족성·크루 검증·업데이트 플래너가 이 장부를 읽는다.
  `pack_install.sh:300-370`
- `config/packs/<id>.requires_framework` 캐시: manifest의 range를 설치 시점에 VERBATIM
  포착 (업데이트 시점엔 manifest가 도달 불가능하기 때문 — release 채널은 throwaway
  `git archive` 트리에서 돈다). 필드 부재 = 기록 없음 (유효 상태). `pack_install.sh:397-436`

### 5-2. 프레임워크 업데이트측 게이트 (역방향)

`update.sh` **fw_reqframework_guard**: 프레임워크 파일이 착지하기 **전에**, 인스턴스에
마운트된 모든 팩의 requires_framework를 **타깃** 프레임워크 버전에 대해 평가한다
(C2는 팩 설치 시점만 보므로 이 게이트가 역방향 구멍을 닫는다). 판정 4상태
(절대 서로 뭉개지 않는다):

- PASS (제약 확인+만족) / SKIP (manifest가 제약 없음을 **확인**) /
  WARN-UNKNOWN (제약을 알 수 없음 + minor/patch 범프 — 비차단, 항상 인쇄) /
  FAIL (위반, 손상 기록, 또는 **major 범프에서의 UNKNOWN**) → 전체 fail-closed exit 1,
  우회 플래그 없음.
- 캐시 부재 시 자기치유 backfill (DOGANY_REPO_ROOT 기준 manifest 재해석 후 캐시).
- "packs evaluated: N" + 판정별 카운트 항상 인쇄 (0은 통과가 아니라 의심).

`update.sh:1328-1467` (호출 `update.sh:1659`)

### 5-3. 통합 업데이트 루프 (update_plan / update_apply)

- 플래너(`update_plan.sh`)는 **zero-write** (읽기 전용 계획): 팩 소스 레지스트리
  `~/.dogany/pack-sources.conf` + 인스턴스 `DOGANY_PACKS` + 크루 3원 교차검증
  (crew.conf × DOGANY_PACKS × DB symlink 실물), 드리프트 가드(설치본보다 낮은
  후보 태그 채택 금지 = BLOCKED-DRIFT), 원자 그룹 산출. `update_plan.sh:1-108`
- 적용기(`update_apply.sh`)는 자체 설치 알고리즘이 없다 — 팩은
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
만든다. 산출물: `pack-manifest.json` + `CHANGELOG.md` + `checksums.sha`(NM3 발행측) +
`.source-sync` 출처 기준선 + 카탈로그 row upsert. `pack_publish.sh:1-46`

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
- **NM3 봉인**: checksums.sha 생성 (`<hex>  <relpath>`, 설치 게이트와 동일 포맷 —
  §5-1-5 가 실제로 그 포맷을 읽는다). `pack_publish.sh:786-803`

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
  tools/.pytest_cache)에서 "후보가 여럿이라 추론 불가"로 죽었다. **즉 계약팩은 발행기로
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
  썼고 린트는 대괄호 형식만 읽었다 → **발행기로 낸 팩은 P3 영구 FAIL**. 실측: 실제로
  발행된 팩(lifekit 1.0.0~1.3.1, health-trainer 0.1.0/0.1.1)은 **전부 대괄호 형식**이고,
  트리에서 유일한 비대괄호 CHANGELOG 는 **이 발행기가 쓴 `packs/dev`** 였다. 그래서
  발행기를 대괄호로 맞췄다(린트를 바꾸는 방향이면 발행된 팩 전부가 깨진다). 기존 절은
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
설치자가 고칠 수 없는 결함으로 **이미 발행된 팩**을 거부하게 되고, 과거 태그의
CHANGELOG(처분 전 본문)를 물고 있는 업데이트를 새로 막는다 — 태그 모집단 실측이 붙어야
하는 별개 결정이라 여기서 조용히 켜지 않는다. D-D가 설치측 전용인 것의 대칭이다.

**fail-closed로 켠 근거는 먼저 센 것이다**: 발행된 팩 루트 3종(lifekit /
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
시점에 loud하게 보여주기 위한 것이다 (스컬 제안, DGN-1035 티켓 흡수).

### 6-2. D-D 반대서명 규약

설치 가능 상태의 앵커는 **두 곳의 일치**다:

1. 팩 자신의 manifest `status: "published"` (자기선언)
2. 프레임워크 카탈로그(`packs/catalog.json`)의 동일 `id` row가 같은 status (반대서명)

둘 중 하나라도 어긋나면 설치측 D-D FAIL → 설치 거부 + payload 코드 실행(C6) 억제.
`compat-lint.sh:1429-1469`. 카탈로그는 프레임워크 레포에 있으므로, **카탈로그 row를
published로 뒤집는 머지 권한자(= 프레임워크 레인 sole-merge)가 발행 승인권자다** —
이것이 코드가 구현하는 발행 권한의 전부다.

status 결정에는 **silent promotion이 없다**: 우선순위 = 명시적 `--catalog-fields-in`
status > 기존 row status > (신규 row일 때만) 모드 기본값. `pack_publish.sh:655-668`

**좌표 단일원 (DGN-1079 RR1).** 팩의 위치는 카탈로그 row의 `package_dir` 하나뿐이며,
상대경로는 **그 카탈로그 파일이 있는 디렉터리** 기준으로 해석된다 (절대경로는 그대로).
발행기와 설치기가 **같은 resolver**(`scripts/pack/lib/pack_coords.sh`)로 이 값을 읽는다 —
발행기는 이 필드를 **되쓰지 않는다**. 되쓰면 `"../../dogany-lifekit"` 같은 독립 레포 좌표가
발행 1회로 팩 id 로 회귀하고 다음 설치가 깨진다(DGN-1079 P1). 카탈로그 row가 아직 없는
신규 팩만 `<packs-dir>/<pack-id>` 생성 기본값으로 필드를 **시딩**한다(기존 값이 있으면 no-op).

같은 이유로 `pack_version` 은 **카탈로그에 재주입되지 않는다** — 팩 자신의
`pack-manifest.json` 이 버전 정본이고 `pack_install.sh` 가 거기서 폴백한다
(DGN-227 B3/P6; 실팩 3행의 `package_dir_note` 가 같은 말을 한다).

snapshot 모드는 payload 를 **재생성**한다(`rm -rf` 후 materialize)므로 `--packs-dir`
바깥으로 나가는 좌표를 **거부한다**(G-C1). 독립 레포 팩은 `--mode finalize` 로 발행한다.

`deploy_owner` 필드는 카탈로그/manifest에 존재하지만 **코드 소비자가 0**이다 —
운영 관례(누가 어느 레인에 착지시키나)의 선언일 뿐 기계 강제가 아니다 (§8).

### 6-3. 설치 노출 (공개 인스톨러)

- `status=="published"` row만 메뉴에 든다. `install.sh:1768`
- `kind=="kit"` row는 도메인 팩 메뉴에서 제외 (킷은 post-mint `pack_install.sh`로 활성화,
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
- 팩: **`pack/<id>/v*`** 태그 네임스페이스 (팩 소스 레포에). stable 채널의 pre-release
  필터는 마지막 `v` 뒤 버전 세그먼트에만 적용 (하이픈 있는 팩 id가 배제되지 않도록).
  `update_plan.sh:22-26,203,510-516`
- 팩 구독원: `~/.dogany/pack-sources.conf` 레지스트리 (중복 키 = loud FAIL).
  `update_plan.sh:13-15`

### 7-3. EXPECTED_USER_VERSION 3점 매치가 보장하는 것

킷 DB 스키마 버전의 세 좌표 — ① `payload/database/<kit>.py`의
`EXPECTED_USER_VERSION` (코드 pin) ② `migrations/NNN_*.sql` 최대 파일번호
③ `schema.sql`의 `PRAGMA user_version` — 가 **전부 같아야** 발행/설치를 통과한다
(C3, `compat-lint.sh:684-797`).

보장 내용: **코드가 기대하는 스키마 == 마이그레이션이 도달시키는 스키마 == 신규 씨딩
스키마.** pin만 올리고 마이그레이션 파일이 없는 팩(기존 설치가 도달 불가능해지는
케이스)은 발행 자체가 안 된다. 여기에 설치측 reverse-drift guard(§5-1-4)와
업데이트 드리프트 가드(§5-3)가 겹쳐, 이미 앞선 인스턴스를 낮은 payload가 덮는
방향도 차단된다. 팩↔프레임워크 lockstep은 requires_framework(C2, 설치 방향) +
fw_reqframework_guard(§5-2, 업데이트 방향)의 쌍으로 완성된다.

## 8. v1 계약 밖 / 미정 (부재를 부재로 적는다)

코드에 **없는** 것들. "있어야 한다"가 아니라 "지금 없다"의 기록이다.

- **promote.sh / 3-스테이지 링의 staging 채널**: SoT 선언(sot/DEPLOY-DISCIPLINE.md)만
  있고 미구현 — 링은 현재 single-track. (채널 해석과 dev-tag 절차는 구현되어 있음.)
- **deploy_owner 기계 강제**: 필드 소비 코드 0건. 순수 선언.
- **`C-TAG` / `C-MODDEPS`**: 예약 명명 슬롯, 구현 0 (§3).
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

1. 이 문서는 **팩 계약 v1의 정본**이다. 계약을 알기 위해 코드 고고학이 필요하면
   그것은 이 문서의 버그다 — 티켓으로 정정하라.
2. **문서는 기록이지 설계가 아니다.** 이 문서에 새 필드·게이트를 먼저 적어서 계약을
   바꿀 수 없다. 순서는 언제나: 설계(그릴) → 코드 게이트 착지 → 이 문서 갱신.
   코드와 문서가 어긋난 기간에는 코드가 정본이다.
3. **additive 확장** (기존 팩이 전부 그대로 통과하는 필드/슬롯 추가)은
   `contract_version: 1`을 유지한다 — DGN-1002(provides_kit/capabilities),
   DGN-1018(kind/service_namespace), DGN-956(skills), DGN-783 B4(units),
   DGN-1143(skills[].winner) 선례.
   기존 팩을 깨는 변경만 contract_version 범프 사유다.
   (DGN-1143 의 provides_kit 필수화는 형식상 breaking 이지만 **부재 모집단
   실측 0건** — 발행 전 태그·전 레포·전 라이브 어디에도 의존 매니페스트가
   없어, 실질 깨지는 팩 0개로 v1 잔류 판정. 근거: DGN-1143 산출 보고.)
4. 게이트 변경 시 **verdict-identical 이중구현 규율** (§5-1-2)을 유지하라:
   compat-lint 쪽과 pack_install 인라인 쪽을 항상 같은 커밋 계열에서 함께 바꾸고
   lockstep 테스트(`scripts/tests/test-pack-install-kit.sh` 등)를 유지한다.
5. 계약 변경의 착지는 프레임워크 레인 규율을 따른다: 브랜치 작업 → 리뷰 →
   sole-merge 게이트 통과 → 릴리스 태그. 카탈로그 status 반대서명(§6-2)이 발행의
   최종 관문이다.

---

## 부록 A. 실팩 3종 대조 실측 (2026-08-24, framework 1.43.0)

| 팩 | kind | 매니페스트가 쓰는 계약 필드 | publish-side lint 실측 |
|---|---|---|---|
| lifekit 1.3.1 (`dogany-lifekit/pack-manifest.json`) | kit | contract_version 1, requires_framework `>=1.30.0 <2.0.0`, provides_kit `lifekit`, skills 6종(전부 own), payload_root, status published | **FAIL 81건** — 전부 워킹트리 런타임 잔재: C4b `__pycache__`/*.pyc 다수 + C5 gate(a) `payload/mirror/mirror_state.db`. 소스 체크아웃 오염이며 발행 태그 시점 상태와 다를 수 있음 → 정리 대상으로 별도 보고 |
| health-trainer 0.1.1 (`dogany-agentpack-health-trainer/pack-manifest.json`) | pack | contract_version 1, requires_kit `{lifekit, >=1.1.0 <2.0.0}`, capabilities.db_lane false, service_namespace `health`, status published | **FAIL 1건** — C4b `service/health/__pycache__/ax_engine...pyc` (동일 클래스 잔재). 나머지 전 슬롯 문서대로 판정 (C-KITDEP PASS, C3/C6 클래스 SKIP, C4 PASS) |
| dev 1.2.0 (`dogany-agent/packs/dev/pack-manifest.json`) | pack | contract_version 1, requires_framework `>=1.42.0 <2.0.0`, capabilities.db_lane false, service_namespace `dev`, status published, 카탈로그 row menu_visible false | **ALL CHECKS PASS** |

필드 대조 특이점: lifekit manifest는 단위 어휘를 `base_units`로 선언하는데 compat-lint
C1의 B4 블록은 `units` 키를 본다 (`compat-lint.sh:322-330`) — 따라서 lifekit은 "no units
block" WARN을 맞는다 (FAIL 아님). 필드명 불일치는 코드-실물 간 알려진 드리프트로 기록.

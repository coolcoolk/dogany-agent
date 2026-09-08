#!/bin/bash
# update_plan.sh -- DGN-1031 slice 1: read-only planner for the unified
# update loop (spec DGN-1031-SPEC §첫 조각 "update-plan").
#
# ZERO-WRITE CONTRACT: this planner writes NOTHING outside one private
# mktemp dir (removed on exit). No conf upsert, no lock files, no git fetch
# (candidate resolution is local-tags-only, logged), no cache/backfill
# persistence (the reqfw guard is always invoked with dry_run=1, whose
# BACKFILL lines are explicitly "not persisted").
#
# What it does (per spec §1/§2/§3/§6, as amended by the Metal correction
# directive of 2026-08-22):
#   1. Parses the pack source registry (~/.dogany/pack-sources.conf) --
#      duplicate keys are a loud FAIL, never first-line-wins.
#   2. Reads every estate instance's .instance.conf mount set. A corrupt /
#      unreadable conf is reported as 판독 FAIL with a DISTINCT code
#      (CONF-READ-FAIL / CONF-PARSE-FAIL), never as "mount 0건".
#   3. Crew discovery with 3-source cross-validation (crew.conf x
#      DOGANY_PACKS x database symlink ground truth); any mismatch, member
#      count 0, or member version divergence (half-updated crew) = ABORT.
#   4. Candidate resolution: framework tag per instance channel (channel=main
#      gets a loud SKIP, pack evaluation continues); pack tags from the
#      pack/<id>/v* namespace. The stable-channel pre-release filter applies
#      to the VERSION SEGMENT after the final 'v' only (grill defect 3 --
#      a hyphenated pack id like health-trainer is never excluded).
#   5. Compatibility end-state + prefix evaluation. Per Metal correction 1
#      this planner does NOT open a new compat-lint C2 call path and does
#      NOT duplicate semver logic (single-sourced in scripts/pack/lib/
#      semver_range.py since DGN-1031 residual debt 1; any re-copy is a
#      defect). Every range-satisfaction question is answered by the ALREADY
#      MERGED machinery: update.sh fw_reqframework_guard() (DGN-1031,
#      commit 923aace5) is EXTRACTED VERBATIM from the shipped update.sh
#      (sed-extraction precedent: tests/dgn621_channel_selector_selftest.sh)
#      and invoked either (a) directly against a live instance root
#      (dry_run=1) for installed-state questions, or (b) against a synthetic
#      one-pack instance dir in the private tmp for candidate-range
#      questions. Same truth source, zero duplication.
#   6. Prefix compatibility rule + ATOMIC GROUP (spec §2, grill defect 2):
#      for every (framework item, pack update item) pair on one instance,
#      the fw-first prefix and pack-first prefix are each evaluated; if
#      neither sequential order is compatible but the end state is, the
#      items are emitted as one ATOMIC GROUP.
#   7. Drift guard (spec §6): a candidate tag BELOW the installed version is
#      never adopted -- "표류 의심: 구독원이 설치본보다 뒤" + BLOCKED-DRIFT.
#      "up to date" is only ever declared after printing BOTH strings.
#   8. Silent-failure guard (spec §6 table): every count is printed; 0 is
#      suspicion, not a pass.
#
# Exit codes:
#   0 = plan produced (items may still be BLOCKED/HOLD -- they are printed
#       loudly; a blocked item is a plan RESULT, not a planner failure)
#   2 = ABORT / FAIL (fail-closed conditions: registry absent with mounts
#       present, duplicate registry keys, conf 판독 FAIL, crew discovery
#       mismatch or 0 members, missing shipped machinery)
#
# Env overrides (TEST HERMETICITY / read-only live verification ONLY --
# same doctrine as DOGANY_SHARED_HOME in pack_install.sh):
#   DOGANY_SHARED_HOME     estate shared home       (default: $HOME/.dogany)
#   DOGANY_PACK_SOURCES    registry path            (default: $SHARED_HOME/pack-sources.conf)
#   DOGANY_CREW_DIR        crew.conf home           (default: $SHARED_HOME/crews)
#                          (symlink GROUND TRUTH always compares against
#                          $SHARED_HOME/crews -- overriding DOGANY_CREW_DIR
#                          alone lets a seeded overlay be read while the
#                          live symlink topology is still verified)
#   DOGANY_PLAN_INSTANCES  "name:root,..."          (default: $HOME estate scan,
#                          see _discover_instances -- DGN-1037)
#   DOGANY_PLAN_FW_CANDIDATE  virtual framework candidate version (spec
#                          §첫 조각 verification criterion 4 injection)
#   DOGANY_PLAN_EMIT       (slice 2 seam) optional path: the planner ALSO
#                          writes its emitted apply-order items to this file
#                          in a machine-readable TSV form, so the apply loop
#                          (update_apply.sh) consumes the planner's product
#                          instead of re-deriving a plan. This is an explicit
#                          caller-requested output channel (the caller points
#                          it into its OWN private tmp); the zero-write
#                          contract above is unchanged when the var is unset.
#                          Format (tab-separated, header PLAN-EMIT-V1):
#                            FW\t<name>\t<root>\t<from>\t<to>
#                            PACK\t<id>\t<kind>\t<provides_kit>\t<inst_ver>\t<cand_tag>\t<cand_ver>\t<members name:root ...>
#                            ATOMIC\t<group description>
#                          An ATOMIC header is followed (slice 4, additive)
#                          by its structured member lines so the apply loop
#                          can execute the group without re-deriving it:
#                            ATOMIC-FW\t<name>\t<root>\t<from>\t<to>
#                            ATOMIC-PACK\t<id>\t<kind>\t<provides_kit>\t<inst_ver>\t<cand_tag>\t<cand_ver>\t<members>
#                            ATOMIC-END\t<member line count>
#                          The trailing ATOMIC-END count lets the consumer
#                          assert the group arrived whole (a truncated emit
#                          must never be applied as a smaller group).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
SOURCES_CONF="${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
FW_CAND_OVERRIDE="${DOGANY_PLAN_FW_CANDIDATE:-}"

_log()   { echo "[plan] $*"; }
_abort() { echo "[plan] ABORT: $*" >&2; exit 2; }

OVERALL_RC=0
_failline() { echo "[plan] FAIL: $*" >&2; OVERALL_RC=2; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/dogany-plan.XXXXXX")" \
  || _abort "mktemp 실패 -- 사설 작업 디렉터리 없이는 진행하지 않습니다"
trap 'rm -rf "$TMP"' EXIT INT TERM

# Machine-readable plan emit (slice 2 seam -- see header). Truncate at start
# so a stale file from an earlier run can never be consumed as this run's
# plan; the header line lets the consumer verify provenance.
EMIT_FILE="${DOGANY_PLAN_EMIT:-}"
if [[ -n "$EMIT_FILE" ]]; then
  printf 'PLAN-EMIT-V1\n' > "$EMIT_FILE" \
    || _abort "plan emit 파일 쓰기 불가: $EMIT_FILE"
fi
_emit() { if [[ -n "$EMIT_FILE" ]]; then printf '%s\n' "$1" >> "$EMIT_FILE"; fi; }

# ---------------------------------------------------------------------------
# Shipped-machinery extraction (fail-closed: absent machinery = ABORT, the
# §4 presence-assert doctrine applied to this slice's actual dependencies).
# ---------------------------------------------------------------------------
GUARD_SNIP="$TMP/guard.snippet.sh"
sed -n '/^fw_reqframework_guard() {/,/^}/p' "$REPO_DIR/update.sh" > "$GUARD_SNIP" 2>/dev/null || true
if ! grep -q '^fw_reqframework_guard() {' "$GUARD_SNIP"; then
  _abort "fw_reqframework_guard 추출 실패 ($REPO_DIR/update.sh) -- 호환성 판정 기계 부재, fail-closed"
fi
# shellcheck disable=SC1090
source "$GUARD_SNIP"

# Single-source semver comparator wiring (DGN-1031 residual debt 1): the
# extracted guard resolves scripts/pack/lib/semver_range.py via
# DOGANY_SEMVER_LIB first (its REPO_ROOT fallback only holds inside
# update.sh's own tree -- unset here). Presence-asserted like the two
# extractions above: absent module = ABORT (the guard itself also fails
# closed per-call, but a loud early abort names the cause once instead of
# once per invocation).
DOGANY_SEMVER_LIB="$REPO_DIR/scripts/pack/lib/semver_range.py"
if [[ ! -f "$DOGANY_SEMVER_LIB" ]]; then
  _abort "semver 비교기 모듈 부재 ($DOGANY_SEMVER_LIB) -- 호환성 판정 기계 부재, fail-closed"
fi

RCT_SNIP="$TMP/rct.snippet.sh"
{
  printf '%s\n' 'msg() { :; }'
  printf '%s\n' 'die() { printf "[plan] FAIL: %s\n" "$1" >&2; exit 1; }'
  sed -n '/^resolve_channel_tag() {/,/^}/p' "$REPO_DIR/agents/.template/routines/self-update.sh" 2>/dev/null
} > "$RCT_SNIP"
if ! grep -q '^resolve_channel_tag() {' "$RCT_SNIP"; then
  _abort "resolve_channel_tag 추출 실패 ($REPO_DIR/agents/.template/routines/self-update.sh) -- 채널 해석 기계 부재, fail-closed"
fi

# ---------------------------------------------------------------------------
# Guard invocation helpers (zero-duplication seam, Metal correction 1).
# ---------------------------------------------------------------------------
GUARD_OUT=""
GUARD_RC=0
_guard_live() { # <instance_root> <target_fw_version>
  local root="$1" target="$2"
  GUARD_RC=0
  GUARD_OUT="$(fw_reqframework_guard "$root" "$target" 1 2>&1)" || GUARD_RC=$?
}

_SYNTH_N=0
_guard_range() { # <id> <ver> <range> <cur_fw> <target_fw> -- rc 0 iff satisfied
  local pid="$1" ver="$2" range="$3" curfw="$4" target="$5"
  _SYNTH_N=$((_SYNTH_N + 1))
  local d="$TMP/synth.$_SYNTH_N"
  mkdir -p "$d/config/packs"
  printf 'DOGANY_PACKS=%s@%s\nDOGANY_FW_VERSION=%s\n' "$pid" "$ver" "$curfw" > "$d/.instance.conf"
  printf '%s\n' "$range" > "$d/config/packs/$pid.requires_framework"
  GUARD_RC=0
  GUARD_OUT="$(fw_reqframework_guard "$d" "$target" 1 2>&1)" || GUARD_RC=$?
  return "$GUARD_RC"
}

# _ver_ok <version> <range> -- version comparison via the SAME shipped
# comparator (no 3rd semver copy): rc 0 iff <version> satisfies <range>.
_ver_ok() {
  _guard_range "vercmp" "0.0.0" "$2" "$1" "$1"
}

# ---------------------------------------------------------------------------
# Candidate tag resolution.
# ---------------------------------------------------------------------------
_fw_tag() { # <repo> <channel> <pin> -- prints tag (empty = none); rc 1 = pin miss
  local repo="$1" channel="$2" pin="$3"
  (
    # shellcheck disable=SC1090
    . "$RCT_SNIP"
    if [ -n "$pin" ]; then DOGANY_UPDATE_PIN="$pin"; export DOGANY_UPDATE_PIN; fi
    resolve_channel_tag "$repo" "$channel"
  )
}

PT_COUNT=0
PT_TAG=""
_pack_tag() { # <repo> <id> <channel> <pin> -- sets PT_COUNT / PT_TAG; rc 1 = pin miss
  local repo="$1" pid="$2" channel="$3" pin="$4"
  PT_TAG=""
  local tags
  tags="$(git -C "$repo" -c versionsort.suffix=- tag --list "pack/$pid/v*" --sort=-v:refname 2>/dev/null || true)"
  if [[ -z "$tags" ]]; then PT_COUNT=0; else PT_COUNT="$(printf '%s\n' "$tags" | wc -l | tr -d ' ')"; fi
  if [[ -n "$pin" ]]; then
    if git -C "$repo" rev-parse -q --verify "refs/tags/$pin" >/dev/null 2>&1; then
      PT_TAG="$pin"
      return 0
    fi
    return 1
  fi
  [[ "$PT_COUNT" -gt 0 ]] || return 0
  if [[ "$channel" == "dev" ]]; then
    PT_TAG="$(printf '%s\n' "$tags" | head -n1)"
  else
    # stable: exclude pre-release by inspecting ONLY the version segment
    # after the final 'v' (grill defect 3 -- 'pack/health-trainer/v0.1.0'
    # must survive; the id's hyphen is irrelevant).
    local t seg
    while IFS= read -r t; do
      seg="${t#pack/$pid/v}"
      if [[ "$seg" != *-* ]]; then PT_TAG="$t"; break; fi
    done <<< "$tags"
  fi
  return 0
}

_tag_manifest() { # <repo> <tag> -- prints tab-separated "id kind pack_version requires_framework provides_kit"
  git -C "$1" archive --format=tar "$2" pack-manifest.json 2>/dev/null \
    | tar -xOf - 2>/dev/null \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(d, dict):
    sys.exit(1)
def s(k):
    v = d.get(k)
    return v if isinstance(v, str) else ''
print('\t'.join([s('id'), s('kind'), s('pack_version'), s('requires_framework'), s('provides_kit')]))
" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Instance discovery (DGN-1037): _discover_instances lives in
# lib/discover_instances.sh -- shared with update_apply.sh (fixed-sender
# resolution) so both sides scan the estate through ONE truth source.
# ---------------------------------------------------------------------------
DISCOVER_LIB="$SCRIPT_DIR/lib/discover_instances.sh"
[[ -f "$DISCOVER_LIB" && -r "$DISCOVER_LIB" ]] \
  || _abort "인스턴스 탐지 라이브러리 부재 ($DISCOVER_LIB) -- fail-closed"
# shellcheck source=lib/discover_instances.sh
. "$DISCOVER_LIB"

_log "== 통합 업데이트 계획 (read-only 플래너, 쓰기 0건) =="
_log "read-only: git fetch 생략 -- 후보 해석은 로컬 태그 기준"

if [[ -n "${DOGANY_PLAN_INSTANCES:-}" ]]; then
  IFS=',' read -r -a INSTANCES <<< "$DOGANY_PLAN_INSTANCES"
  _log "인스턴스 원천: DOGANY_PLAN_INSTANCES 오버라이드 ${#INSTANCES[@]}건 (스캔 생략)"
else
  if ! _scan="$(_discover_instances)"; then
    _abort "HOME 미설정/비실재 (HOME='${HOME:-}') -- 인스턴스 스캔 불가"
  fi
  INSTANCES=()
  while IFS= read -r _ln; do
    if [[ -n "$_ln" ]]; then INSTANCES+=("$_ln"); fi
  done <<< "$_scan"
  _log "인스턴스 스캔 루트: \$HOME/.dogany/agents/* + \$HOME/dogany/* + \$HOME/dogany/*/agents/* + \$HOME/dogany/*/poc/* (판정 기준: .instance.conf 보유)"
  _log "인스턴스 발견: ${#INSTANCES[@]}건"
  for _ln in ${INSTANCES[@]+"${INSTANCES[@]}"}; do
    _log "  발견: $_ln"
  done
  if [[ "${#INSTANCES[@]}" -eq 0 ]]; then
    _abort "인스턴스 발견 0건 -- 0건은 통과가 아니라 ABORT (§6 조용한 실패 금지; 스캔 루트를 확인하세요)"
  fi
fi

# ---------------------------------------------------------------------------
# 1) Registry parse (spec §1). Duplicate key = FAIL (never head -n1 wins).
# ---------------------------------------------------------------------------
REG_IDS=()
REG_REPOS=()
REG_CHANNELS=()
REG_PINS=()

_reg_lookup() { # <id> <field:repo|channel|pin> -- prints value (may be empty)
  local pid="$1" field="$2" i=0
  while [[ "$i" -lt "${#REG_IDS[@]}" ]]; do
    if [[ "${REG_IDS[$i]}" == "$pid" ]]; then
      case "$field" in
        repo)    echo "${REG_REPOS[$i]}" ;;
        channel) echo "${REG_CHANNELS[$i]}" ;;
        pin)     echo "${REG_PINS[$i]}" ;;
      esac
      return 0
    fi
    i=$((i + 1))
  done
  echo ""
}

REGISTRY_PRESENT=0
if [[ -f "$SOURCES_CONF" ]]; then
  REGISTRY_PRESENT=1
  _id_re='^[a-z][a-z0-9_-]{0,31}$'
  while IFS= read -r line; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    pid="${key%.*}"
    field="${key##*.}"
    if [[ "$key" == "$pid" || ! "$pid" =~ $_id_re ]]; then
      _failline "레지스트리 라인 파싱 실패: '$line' ($SOURCES_CONF)"
      continue
    fi
    case "$field" in
      repo|channel|pin) : ;;
      *) _failline "레지스트리 미지원 키: '$key' ($SOURCES_CONF)"; continue ;;
    esac
    # duplicate key detection (grill: silent first-line-wins refused)
    idx=-1
    i=0
    while [[ "$i" -lt "${#REG_IDS[@]}" ]]; do
      if [[ "${REG_IDS[$i]}" == "$pid" ]]; then idx="$i"; fi
      i=$((i + 1))
    done
    if [[ "$idx" -lt 0 ]]; then
      REG_IDS+=("$pid"); REG_REPOS+=(""); REG_CHANNELS+=(""); REG_PINS+=("")
      idx=$((${#REG_IDS[@]} - 1))
    fi
    case "$field" in
      repo)
        if [[ -n "${REG_REPOS[$idx]}" ]]; then
          _abort "레지스트리 중복 키: $pid.repo (중복은 조용한 첫 줄 승리가 아니라 FAIL)"
        fi
        REG_REPOS[$idx]="$val" ;;
      channel)
        if [[ -n "${REG_CHANNELS[$idx]}" ]]; then
          _abort "레지스트리 중복 키: $pid.channel"
        fi
        REG_CHANNELS[$idx]="$val" ;;
      pin)
        if [[ -n "${REG_PINS[$idx]}" ]]; then
          _abort "레지스트리 중복 키: $pid.pin"
        fi
        REG_PINS[$idx]="$val" ;;
    esac
  done < "$SOURCES_CONF"
  _log "레지스트리 등록 ${#REG_IDS[@]}건${REG_IDS[0]:+ (${REG_IDS[*]})} ($SOURCES_CONF)"
else
  _log "레지스트리 부재: $SOURCES_CONF (시드 미실행 -- update_seed.sh 필요)"
  _log "레지스트리 등록 0건"
fi

# ---------------------------------------------------------------------------
# 2) Instance scan (spec §6: 판독 OK/FAIL codes distinct from mount 0건).
# ---------------------------------------------------------------------------
I_NAME=(); I_ROOT=(); I_OK=(); I_MOUNTS=(); I_CHANNEL=(); I_FW=(); I_REPO=(); I_PIN=()

_pk_re='^[a-z][a-z0-9_-]{0,31}$'
_vv_re='^[0-9][0-9A-Za-z.+-]*$'
TOTAL_MOUNTS=0

for entry in "${INSTANCES[@]}"; do
  name="${entry%%:*}"
  root="${entry#*:}"
  conf="$root/.instance.conf"
  ok=1
  mounts=""
  channel=""
  fw=""
  repo=""
  pin=""
  if [[ ! -f "$conf" ]]; then
    _failline "$name: .instance.conf 판독 FAIL(CONF-READ-FAIL): 파일 부재 ($conf)"
    ok=0
  elif ! conf_text="$(cat "$conf" 2>/dev/null)"; then
    _failline "$name: .instance.conf 판독 FAIL(CONF-READ-FAIL): 읽기 오류 ($conf)"
    ok=0
  else
    packs_line="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_PACKS=//p' | head -n1)"
    channel="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_UPDATE_CHANNEL=//p' | head -n1)"
    fw="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_FW_VERSION=//p' | head -n1)"
    repo="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_REPO_ROOT=//p' | head -n1)"
    pin="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_UPDATE_PIN=//p' | head -n1)"
    if [[ -n "$packs_line" ]]; then
      IFS=',' read -r -a items <<< "$packs_line"
      for it in "${items[@]}"; do
        [[ -z "$it" ]] && continue
        pid="${it%%@*}"
        pver="${it#*@}"
        if [[ "$it" != *"@"* || ! "$pid" =~ $_pk_re || ! "$pver" =~ $_vv_re ]]; then
          _failline "$name: .instance.conf 판독 FAIL(CONF-PARSE-FAIL): DOGANY_PACKS 항목 '$it' 형식 위반 -- mount 0건과 다른 코드로 구분"
          ok=0
          break
        fi
        mounts="${mounts:+$mounts,}$it"
      done
    fi
    if [[ "$ok" -eq 1 && -z "$fw" ]]; then
      _failline "$name: .instance.conf 판독 FAIL(CONF-PARSE-FAIL): DOGANY_FW_VERSION 부재"
      ok=0
    fi
  fi
  if [[ "$ok" -eq 1 ]]; then
    _log "$name: .instance.conf 판독 OK"
    cnt=0
    if [[ -n "$mounts" ]]; then cnt="$(awk -F',' '{print NF}' <<< "$mounts")"; fi
    if [[ "$cnt" -eq 0 ]]; then
      _log "$name: mount 0건 (판독 성공, 팩 미기재) -- 프레임워크 항목만 진행"
    else
      _log "$name: mount ${cnt}건 ($mounts)"
    fi
    TOTAL_MOUNTS=$((TOTAL_MOUNTS + cnt))
    if [[ -f "$root/VERSION" ]]; then
      inst_file_ver="$(head -n1 "$root/VERSION" | tr -d '[:space:]')"
      if [[ -n "$inst_file_ver" && "$inst_file_ver" != "$fw" ]]; then
        _log "WARN: $name: VERSION 파일($inst_file_ver) != DOGANY_FW_VERSION($fw) -- 설치본 버전 표류 의심 (그릴 결함 10 어서트)"
      fi
    fi
  fi
  I_NAME+=("$name"); I_ROOT+=("$root"); I_OK+=("$ok"); I_MOUNTS+=("$mounts")
  I_CHANNEL+=("${channel:-release}"); I_FW+=("$fw"); I_REPO+=("$repo"); I_PIN+=("$pin")
done

# ---------------------------------------------------------------------------
# 3) Registry-vs-mount validation (spec §1: mount>0 & 등록 0 = 전체 ABORT).
# ---------------------------------------------------------------------------
if [[ "$TOTAL_MOUNTS" -gt 0 && "${#REG_IDS[@]}" -eq 0 ]]; then
  _abort "mount ${TOTAL_MOUNTS}건인데 레지스트리 등록 0건 -- 의심 경로, 전체 ABORT (시드: update_seed.sh)"
fi

# Unique mounted ids (union) + per-id installed version consistency map.
M_IDS=()
i=0
while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
  mounts="${I_MOUNTS[$i]}"
  i=$((i + 1))
  [[ -z "$mounts" ]] && continue
  IFS=',' read -r -a items <<< "$mounts"
  for it in "${items[@]}"; do
    pid="${it%%@*}"
    seen=0
    for m in ${M_IDS[@]+"${M_IDS[@]}"}; do
      if [[ "$m" == "$pid" ]]; then seen=1; fi
    done
    if [[ "$seen" -eq 0 ]]; then M_IDS+=("$pid"); fi
  done
done

# ---------------------------------------------------------------------------
# 4) Pack candidate resolution + crew discovery, per mounted id (id order =
#    alphabetical for determinism, spec §2 rule 4).
# ---------------------------------------------------------------------------
SORTED_IDS=()
if [[ "${#M_IDS[@]}" -gt 0 ]]; then
  while IFS= read -r x; do SORTED_IDS+=("$x"); done < <(printf '%s\n' "${M_IDS[@]}" | sort)
fi

# Pack item arrays (one item per mounted pack id; crew packs are ONE item).
P_ID=(); P_STATUS=(); P_DETAIL=(); P_CAND_TAG=(); P_CAND_VER=(); P_INST_VER=()
P_KIND=(); P_RANGE=(); P_MEMBERS=(); P_PROVIDES=()   # members: "name:root name:root" (crew) or mounting instances

for pid in ${SORTED_IDS[@]+"${SORTED_IDS[@]}"}; do
  status="OK"; detail=""; cand_tag=""; cand_ver=""; inst_ver=""; kind=""; range=""; members=""; provides=""

  # mounting instances + installed version consistency
  i=0
  while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
    mounts="${I_MOUNTS[$i]}"
    if [[ "${I_OK[$i]}" -eq 1 && -n "$mounts" ]]; then
      IFS=',' read -r -a items <<< "$mounts"
      for it in "${items[@]}"; do
        if [[ "${it%%@*}" == "$pid" ]]; then
          members="${members:+$members }${I_NAME[$i]}:${I_ROOT[$i]}"
          v="${it#*@}"
          if [[ -z "$inst_ver" ]]; then
            inst_ver="$v"
          elif [[ "$inst_ver" != "$v" ]]; then
            _abort "팩 $pid: 인스턴스 간 설치 버전 불일치 ($inst_ver vs $v) -- 반쪽 갱신 의심, fail-closed"
          fi
        fi
      done
    fi
    i=$((i + 1))
  done

  repo="$(_reg_lookup "$pid" repo)"
  channel="$(_reg_lookup "$pid" channel)"
  pin="$(_reg_lookup "$pid" pin)"
  channel="${channel:-stable}"

  if [[ -z "$repo" ]]; then
    _log "$pid: 레지스트리 미등록 -- BLOCKED (mount됐는데 구독원 없음, loud)"
    status="BLOCKED-UNREGISTERED"
  elif [[ ! -d "$repo" ]]; then
    _log "$pid: 등록 repo 경로 실존하지 않음 ($repo) -- BLOCKED"
    status="BLOCKED-REPO-MISSING"
  else
    if ! _pack_tag "$repo" "$pid" "$channel" "$pin"; then
      _failline "$pid: 핀 태그 부재 (pin='$pin', repo=$repo) -- 최신 폴백 금지, FAIL"
      status="FAIL-PIN-MISS"
    fi
    if [[ "$status" == "OK" ]]; then
      _log "$pid: pack/$pid/v* 태그 ${PT_COUNT}건${PT_TAG:+, 후보 $PT_TAG} (channel=$channel${pin:+, pin=$pin})"
      if [[ -z "$PT_TAG" ]]; then
        _log "$pid: 태그 0건(또는 채널 적격 후보 없음) -- BLOCKED (\"최신\" 판정 금지, 조용한 통과 아님)"
        status="BLOCKED-NO-TAG"
      else
        cand_tag="$PT_TAG"
        cand_ver="${cand_tag#pack/$pid/v}"
        # manifest (candidate tag archive, streamed -- no on-disk extraction)
        mf="$(_tag_manifest "$repo" "$cand_tag" || true)"
        if [[ -z "$mf" ]]; then
          _failline "$pid: 후보 태그 $cand_tag 에서 pack-manifest.json 판독 불가 -- fail-closed"
          status="FAIL-MANIFEST"
        else
          mf_id="$(printf '%s' "$mf" | cut -f1)"
          kind="$(printf '%s' "$mf" | cut -f2)"
          mf_ver="$(printf '%s' "$mf" | cut -f3)"
          range="$(printf '%s' "$mf" | cut -f4)"
          provides="$(printf '%s' "$mf" | cut -f5)"
          if [[ "$mf_id" != "$pid" ]]; then
            _failline "$pid: manifest id('$mf_id') != 레지스트리 id('$pid') -- 구독원 오염 의심, FAIL (그릴 결함 6 어서트)"
            status="FAIL-ID-MISMATCH"
          elif [[ -n "$mf_ver" && "$mf_ver" != "$cand_ver" ]]; then
            _log "WARN: $pid: 태그 버전($cand_ver) != manifest pack_version($mf_ver) -- 3점 일치 위반 관측 (install-side 게이트가 재차 막음)"
          fi
        fi
      fi
    fi
    if [[ "$status" == "OK" && -n "$inst_ver" ]]; then
      # drift guard + up-to-date: both strings printed, then compared via
      # the shipped comparator (no 3rd semver copy). Format precheck only
      # (X.Y.Z shape) -- an uncomparable version must FAIL as such, never
      # masquerade as drift.
      _semverish='^[0-9]+\.[0-9]+\.[0-9]+'
      if [[ ! "$cand_ver" =~ $_semverish || ! "$inst_ver" =~ $_semverish ]]; then
        _failline "$pid: 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$inst_ver' -- semver 형식 아님), fail-closed"
        status="FAIL-VERSION-FORM"
      elif _ver_ok "$cand_ver" "==$inst_ver"; then
        _log "$pid: 설치 $inst_ver == 후보 $cand_ver -- 최신 상태 (업데이트 없음)"
        status="UP-TO-DATE"
      elif ! _ver_ok "$cand_ver" ">=$inst_ver"; then
        _log "$pid: 경고 -- 표류 의심: 구독원이 설치본보다 뒤 (후보 $cand_tag < 설치 $inst_ver) -- BLOCKED-DRIFT (다운그레이드 후보 생성 금지)"
        status="BLOCKED-DRIFT"
      else
        _log "$pid: 설치 $inst_ver vs 후보 $cand_tag -> 업그레이드 후보 판정"
      fi
    fi
  fi

  # crew discovery (3-source cross-validation) -- runs for any id with a
  # crew.conf OR a kit-kind candidate manifest (crew consistency is a
  # plan-level invariant regardless of the item's candidate status).
  crew_conf="$CREW_DIR/$pid/crew.conf"
  crew_target="$SHARED_HOME/crews/$pid/$pid.db"
  if [[ -f "$crew_conf" || "$kind" == "kit" ]]; then
    if [[ ! -f "$crew_conf" ]]; then
      _abort "크루 $pid: kind=kit인데 crew.conf 부재 ($crew_conf) -- 시드 필요 (update_seed.sh), fail-closed"
    fi
    crew_members=""
    member_n=0
    while IFS= read -r cl; do
      case "$cl" in
        member=*) : ;;
        ''|'#'*) continue ;;
        *) _abort "크루 $pid: crew.conf 라인 파싱 실패: '$cl'" ;;
      esac
      mroot="${cl#member=}"
      mname="$(basename "$mroot")"
      i=0
      while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
        if [[ "${I_ROOT[$i]}" == "$mroot" ]]; then mname="${I_NAME[$i]}"; fi
        i=$((i + 1))
      done
      member_n=$((member_n + 1))
      crew_members="${crew_members:+$crew_members }$mname:$mroot"
      # source 1: crew.conf (this line) -- present by construction
      _log "크루 $pid: 교차검증 $mname source1(crew.conf) PASS"
      # source 2: member conf DOGANY_PACKS has <kit>@
      m_packs="$(sed -n 's/^DOGANY_PACKS=//p' "$mroot/.instance.conf" 2>/dev/null | head -n1)"
      case ",$m_packs," in
        *",$pid@"*) _log "크루 $pid: 교차검증 $mname source2(DOGANY_PACKS) PASS" ;;
        *) _log "크루 $pid: 교차검증 $mname source2(DOGANY_PACKS) FAIL"
           _abort "크루 $pid: $mname DOGANY_PACKS에 $pid@ 엔트리 없음 -- 3소스 불일치, fail-closed" ;;
      esac
      # source 3: symlink ground truth
      link="$(readlink "$mroot/database/$pid.db" 2>/dev/null || echo "")"
      if [[ "$link" == "$crew_target" ]]; then
        _log "크루 $pid: 교차검증 $mname source3(symlink) PASS ($link)"
      else
        _log "크루 $pid: 교차검증 $mname source3(symlink) FAIL (readlink='$link' != '$crew_target')"
        _abort "크루 $pid: $mname symlink ground truth 불일치 -- fail-closed"
      fi
    done < "$crew_conf"
    _log "크루 $pid 멤버 ${member_n}건${crew_members:+ ($crew_members)}"
    if [[ "$member_n" -eq 0 ]]; then
      _abort "크루 $pid: 멤버 0건 = ABORT (DGN-1030 §주의 -- 0건은 통과가 아니라 의심)"
    fi
    # reverse sweep: an instance that mounts <kit>@ AND matches the symlink
    # pattern but is missing from crew.conf = discovery mismatch.
    i=0
    while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
      root="${I_ROOT[$i]}"
      mounts="${I_MOUNTS[$i]}"
      nm="${I_NAME[$i]}"
      i=$((i + 1))
      case ",$mounts," in
        *",$pid@"*) : ;;
        *) continue ;;
      esac
      link="$(readlink "$root/database/$pid.db" 2>/dev/null || echo "")"
      if [[ "$link" == "$crew_target" ]]; then
        case " $crew_members " in
          *" $nm:$root "*) : ;;
          *) _abort "크루 $pid: $nm($root)이 symlink+mount 두 조건을 충족하는데 crew.conf에 없음 -- 발견 불일치, fail-closed" ;;
        esac
      fi
    done
    members="$crew_members"
  fi

  P_ID+=("$pid"); P_STATUS+=("$status"); P_DETAIL+=("$detail")
  P_CAND_TAG+=("$cand_tag"); P_CAND_VER+=("$cand_ver"); P_INST_VER+=("$inst_ver")
  P_KIND+=("$kind"); P_RANGE+=("$range"); P_MEMBERS+=("$members"); P_PROVIDES+=("$provides")
done

# ---------------------------------------------------------------------------
# 5) Framework candidate per instance (channel=main = loud SKIP, spec §6).
# ---------------------------------------------------------------------------
F_NAME=(); F_ROOT=(); F_FROM=(); F_TO=(); F_STATUS=(); F_DETAIL=()

i=0
while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
  name="${I_NAME[$i]}"
  root="${I_ROOT[$i]}"
  ok="${I_OK[$i]}"
  channel="${I_CHANNEL[$i]}"
  fw="${I_FW[$i]}"
  repo="${I_REPO[$i]}"
  pin="${I_PIN[$i]}"
  i=$((i + 1))
  [[ "$ok" -eq 1 ]] || continue

  if [[ -n "$FW_CAND_OVERRIDE" ]]; then
    _log "$name: 가상 프레임워크 후보 주입: $FW_CAND_OVERRIDE (DOGANY_PLAN_FW_CANDIDATE, 검증용)"
    cand_ver="$FW_CAND_OVERRIDE"
    cand_disp="v$FW_CAND_OVERRIDE(virtual)"
  elif [[ "$channel" == "main" ]]; then
    _log "$name: 프레임워크 SKIP (channel=main -- 태그 기반 후보 없음)"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_TO+=("")
    F_STATUS+=("SKIP-MAIN"); F_DETAIL+=("channel=main")
    continue
  else
    if [[ -z "$repo" || ! -d "$repo" ]]; then
      _failline "$name: DOGANY_REPO_ROOT 미해석 ('$repo') -- 프레임워크 후보 해석 불가, FAIL"
      F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_TO+=("")
      F_STATUS+=("FAIL-REPO"); F_DETAIL+=("repo unresolved")
      continue
    fi
    tag=""
    if ! tag="$(_fw_tag "$repo" "$channel" "$pin")"; then
      _failline "$name: 프레임워크 태그 해석 실패 (channel=$channel, pin='$pin')"
      F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_TO+=("")
      F_STATUS+=("FAIL-TAG"); F_DETAIL+=("tag resolve failed")
      continue
    fi
    if [[ -z "$tag" ]]; then
      _log "$name: 프레임워크 v* 태그 0건 -- BLOCKED (\"최신\" 판정 금지)"
      F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_TO+=("")
      F_STATUS+=("BLOCKED-NO-TAG"); F_DETAIL+=("0 tags")
      continue
    fi
    cand_ver="${tag#v}"
    cand_disp="$tag"
  fi

  _semverish='^[0-9]+\.[0-9]+\.[0-9]+'
  if [[ ! "$cand_ver" =~ $_semverish || ! "$fw" =~ $_semverish ]]; then
    _failline "$name: 프레임워크 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$fw'), fail-closed"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_TO+=("$cand_ver")
    F_STATUS+=("FAIL-VERSION-FORM"); F_DETAIL+=("")
    continue
  fi
  if _ver_ok "$cand_ver" "==$fw"; then
    _log "$name: 프레임워크 설치 $fw == 후보 $cand_ver -- 최신 상태 (업데이트 없음)"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_TO+=("$cand_ver")
    F_STATUS+=("UP-TO-DATE"); F_DETAIL+=("")
    continue
  fi
  if ! _ver_ok "$cand_ver" ">=$fw"; then
    _log "$name: 경고 -- 표류 의심: 구독원이 설치본보다 뒤 (프레임워크 후보 $cand_disp < 설치 $fw) -- BLOCKED-DRIFT"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_TO+=("$cand_ver")
    F_STATUS+=("BLOCKED-DRIFT"); F_DETAIL+=("")
    continue
  fi
  _log "$name: 프레임워크 설치 $fw vs 후보 $cand_disp -> 업그레이드 후보 판정"
  F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_TO+=("$cand_ver")
  F_STATUS+=("OK"); F_DETAIL+=("$cand_disp")
done

# ---------------------------------------------------------------------------
# 6) Compatibility end-state + prefix rule (spec §2, via the shipped guard).
# ---------------------------------------------------------------------------
_pack_item_idx() { # <id> -- prints index or -1
  local pid="$1" i=0
  while [[ "$i" -lt "${#P_ID[@]}" ]]; do
    if [[ "${P_ID[$i]}" == "$pid" ]]; then echo "$i"; return 0; fi
    i=$((i + 1))
  done
  echo "-1"
}

ATOMIC_NOTES=()
PACK_BEFORE_FW=()   # entries "inst:packid" -- pack must precede fw on inst

fi_i=0
while [[ "$fi_i" -lt "${#F_NAME[@]}" ]]; do
  name="${F_NAME[$fi_i]}"
  root="${F_ROOT[$fi_i]}"
  fw_from="${F_FROM[$fi_i]}"
  fw_to="${F_TO[$fi_i]}"
  fstatus="${F_STATUS[$fi_i]}"
  cur_idx="$fi_i"
  fi_i=$((fi_i + 1))
  [[ "$fstatus" == "OK" ]] || continue

  # installed-state evaluation: the merged machinery itself, dry-run.
  _guard_live "$root" "$fw_to"
  live_rc="$GUARD_RC"
  live_out="$GUARD_OUT"
  printf '%s\n' "$live_out" | sed 's/^/[plan]   /'

  # per-pack end-state: candidate range (if an upgrade item exists) else the
  # live installed-state verdict from the guard above.
  hold_reasons=""
  # instance mount list
  inst_mounts=""
  i=0
  while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
    if [[ "${I_NAME[$i]}" == "$name" ]]; then inst_mounts="${I_MOUNTS[$i]}"; fi
    i=$((i + 1))
  done
  if [[ -n "$inst_mounts" ]]; then
    IFS=',' read -r -a items <<< "$inst_mounts"
    for it in "${items[@]}"; do
      pid="${it%%@*}"
      pidx="$(_pack_item_idx "$pid")"
      has_cand=0
      cand_range=""
      cand_ver=""
      if [[ "$pidx" -ge 0 && "${P_STATUS[$pidx]}" == "OK" ]]; then
        has_cand=1
        cand_range="${P_RANGE[$pidx]}"
        cand_ver="${P_CAND_VER[$pidx]}"
      fi
      live_verdict="$(printf '%s\n' "$live_out" \
        | grep -E "^\[fw-reqfw-guard\] (PASS|FAIL|WARN|SKIP): $pid@" \
        | head -n1 | awk '{print $2}' | tr -d ':' || true)"
      live_verdict="${live_verdict:-UNSEEN}"
      if [[ "$has_cand" -eq 1 ]]; then
        end_ok=1
        if [[ -n "$cand_range" ]]; then
          if ! _guard_range "$pid" "$cand_ver" "$cand_range" "$fw_from" "$fw_to"; then end_ok=0; fi
        else
          _log "$name/$pid: 후보 manifest requires_framework 미선언 -- 제약 없음(SKIP 동형)"
        fi
        if [[ "$end_ok" -eq 0 ]]; then
          hold_reasons="${hold_reasons:+$hold_reasons; }$pid 후보($cand_ver) requires_framework='$cand_range' 이탈"
        else
          # prefix analysis (grill defect 2): fw-first needs the INSTALLED
          # pack to tolerate the fw candidate; pack-first needs the pack
          # CANDIDATE to tolerate the installed fw.
          fw_first_ok=0
          case "$live_verdict" in
            PASS|SKIP) fw_first_ok=1 ;;
            WARN)
              fw_first_ok=1
              _log "$name/$pid: 현재본 제약 UNKNOWN(미확보) -- 후보본 단독 판정 (조용한 통과 아님, 위 WARN 라인 참조)" ;;
            *) fw_first_ok=0 ;;
          esac
          pack_first_ok=0
          if [[ -z "$cand_range" ]]; then
            pack_first_ok=1
          elif _guard_range "$pid" "$cand_ver" "$cand_range" "$fw_from" "$fw_from"; then
            pack_first_ok=1
          fi
          if [[ "$fw_first_ok" -eq 1 ]]; then
            : # default order fw -> pack holds
          elif [[ "$pack_first_ok" -eq 1 ]]; then
            _log "$name/$pid: prefix 규칙 -- fw 선적용 시 설치본($pid@${P_INST_VER[$pidx]}) 비호환, 팩 선적용 순서 채택"
            PACK_BEFORE_FW+=("$name:$pid")
          else
            _log "$name/$pid: prefix 규칙 -- 성립하는 순차 순서 없음, ATOMIC GROUP으로 묶음"
            ATOMIC_NOTES+=("$name:$pid")
          fi
        fi
      else
        # no candidate: pack stays at installed version in the end state.
        case "$live_verdict" in
          PASS|SKIP) : ;;
          WARN) _log "$name/$pid: 현재본 제약 UNKNOWN -- minor/patch 진행 정책(guard WARN) 준수, 미검증 명시" ;;
          FAIL) hold_reasons="${hold_reasons:+$hold_reasons; }$pid 설치본(교체 후보 없음) 비호환" ;;
          UNSEEN) hold_reasons="${hold_reasons:+$hold_reasons; }$pid 판정 라인 미검출(fail-closed)" ;;
        esac
      fi
    done
  fi
  if [[ -z "$hold_reasons" && "$live_rc" -ne 0 ]]; then
    # guard blocked but every failing pack had a passing candidate --
    # sequencing (pack-first / atomic) already recorded above.
    _log "$name: 설치 상태 게이트 BLOCKED이나 전 항목이 후보로 해소됨 -- 순서/원자 그룹 조건부 진행"
  fi
  if [[ -n "$hold_reasons" ]]; then
    _log "$name: 프레임워크 항목 HOLD (fail-closed) -- $hold_reasons"
    F_STATUS[$cur_idx]="HOLD"
    F_DETAIL[$cur_idx]="$hold_reasons"
  fi
done

# ---------------------------------------------------------------------------
# 6b) Pack-candidate end-state per member (covers members WITHOUT a
#     framework item too, e.g. channel=main: the pack candidate must satisfy
#     that member's framework end state = its INSTALLED framework).
# ---------------------------------------------------------------------------
pi=0
while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
  if [[ "${P_STATUS[$pi]}" == "OK" && -n "${P_RANGE[$pi]}" ]]; then
    pid="${P_ID[$pi]}"
    for m in ${P_MEMBERS[$pi]}; do
      mname="${m%%:*}"
      mroot="${m#*:}"
      m_fw=""
      m_fw_end=""
      i=0
      while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
        if [[ "${I_ROOT[$i]}" == "$mroot" ]]; then m_fw="${I_FW[$i]}"; fi
        i=$((i + 1))
      done
      if [[ -z "$m_fw" ]]; then
        m_fw="$(sed -n 's/^DOGANY_FW_VERSION=//p' "$mroot/.instance.conf" 2>/dev/null | head -n1)"
      fi
      m_fw_end="$m_fw"
      fj=0
      while [[ "$fj" -lt "${#F_NAME[@]}" ]]; do
        if [[ "${F_NAME[$fj]}" == "$mname" && "${F_STATUS[$fj]}" == "OK" ]]; then
          m_fw_end="${F_TO[$fj]}"
        fi
        fj=$((fj + 1))
      done
      if [[ -z "$m_fw_end" ]]; then
        _log "$pid: 멤버 $mname 프레임워크 버전 미확인 -- 종상태 판정 불가, HOLD (fail-closed)"
        P_STATUS[$pi]="HOLD"
        continue
      fi
      if ! _guard_range "$pid" "${P_CAND_VER[$pi]}" "${P_RANGE[$pi]}" "$m_fw" "$m_fw_end"; then
        _log "$pid: 멤버 $mname 종상태(fw $m_fw_end)에서 후보 requires_framework='${P_RANGE[$pi]}' 이탈 -- HOLD (fail-closed)"
        P_STATUS[$pi]="HOLD"
      fi
    done
  fi
  pi=$((pi + 1))
done

# ---------------------------------------------------------------------------
# 7) Plan assembly: deterministic order (spec §2) with prefix-rule
#    reorders/groups. fw items (instance order) -> kit packs -> other packs
#    (alphabetical; requires_kit packs after their kit by class rule).
# ---------------------------------------------------------------------------
echo ""
_log "== 적용 순서 =="
STEP=0
RESTART_TOTAL=0
PLANNED=0

_emit_fw() { # idx
  local idx="$1"
  STEP=$((STEP + 1))
  _log "  $STEP. framework ${F_NAME[$idx]}: ${F_FROM[$idx]} -> ${F_TO[$idx]} (재시작 1건: ${F_NAME[$idx]})"
  _emit "$(printf 'FW\t%s\t%s\t%s\t%s' "${F_NAME[$idx]}" "${F_ROOT[$idx]}" "${F_FROM[$idx]}" "${F_TO[$idx]}")"
  RESTART_TOTAL=$((RESTART_TOTAL + 1))
  PLANNED=$((PLANNED + 1))
}

_emit_pack() { # idx
  local idx="$1"
  local pid="${P_ID[$idx]}"
  local members="${P_MEMBERS[$idx]}"
  local n=0
  local names=""
  local m
  for m in $members; do
    n=$((n + 1))
    names="${names:+$names, }${m%%:*}"
  done
  STEP=$((STEP + 1))
  if [[ "${P_KIND[$idx]}" == "kit" ]]; then
    _log "  $STEP. kit $pid (크루, 멤버 ${n}건): ${P_INST_VER[$idx]} -> ${P_CAND_TAG[$idx]} (재시작 ${n}건: $names)"
    if [[ "$n" -eq 0 ]]; then
      _abort "크루 $pid: 재시작 대상 0건 -- ABORT (§6)"
    fi
  else
    _log "  $STEP. pack $pid (인스턴스 ${n}건: $names): ${P_INST_VER[$idx]} -> ${P_CAND_TAG[$idx]} (재시작 ${n}건)"
  fi
  _emit "$(printf 'PACK\t%s\t%s\t%s\t%s\t%s\t%s\t%s' \
    "$pid" "${P_KIND[$idx]}" "${P_PROVIDES[$idx]}" "${P_INST_VER[$idx]}" \
    "${P_CAND_TAG[$idx]}" "${P_CAND_VER[$idx]}" "$members")"
  RESTART_TOTAL=$((RESTART_TOTAL + n))
  PLANNED=$((PLANNED + 1))
}

# atomic-group membership helper (bash 3.2: linear scan)
_fw_in_atomic() { # <inst> -- rc 0 if fw item of inst is in an atomic group
  local nm="$1" a
  for a in ${ATOMIC_NOTES[@]+"${ATOMIC_NOTES[@]}"}; do
    if [[ "${a%%:*}" == "$nm" ]]; then return 0; fi
  done
  return 1
}

# pack-first items (must precede their fw items)
pi=0
while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
  if [[ "${P_STATUS[$pi]}" == "OK" ]]; then
    for a in ${PACK_BEFORE_FW[@]+"${PACK_BEFORE_FW[@]}"}; do
      if [[ "${a#*:}" == "${P_ID[$pi]}" ]]; then
        _emit_pack "$pi"
        P_STATUS[$pi]="EMITTED"
        break
      fi
    done
  fi
  pi=$((pi + 1))
done

# atomic groups (fw + packs applied together -- no internal prefix).
# COALESCING RULE: all atomically-paired items merge into ONE group. A crew
# pack paired with SEVERAL instances' fw items must land with ALL of them --
# emitting per-fw groups would leave a later fw applying against an
# already-advanced pack, the exact half-state the prefix rule forbids
# (self-grill catch). Conservative over-merge of genuinely independent
# atomic pairs is safe: atomicity is strictly stronger, never weaker;
# revisit minimal-unit splitting if group size becomes a cost.
if [[ "${#ATOMIC_NOTES[@]}" -gt 0 ]]; then
  group=""
  n_restart=0
  a_lines=()   # structured member lines (slice 4 consumer contract)
  fi_i=0
  while [[ "$fi_i" -lt "${#F_NAME[@]}" ]]; do
    if [[ "${F_STATUS[$fi_i]}" == "OK" ]] && _fw_in_atomic "${F_NAME[$fi_i]}"; then
      group="${group:+$group + }framework ${F_NAME[$fi_i]}: ${F_FROM[$fi_i]} -> ${F_TO[$fi_i]}"
      a_lines+=("$(printf 'ATOMIC-FW\t%s\t%s\t%s\t%s' \
        "${F_NAME[$fi_i]}" "${F_ROOT[$fi_i]}" "${F_FROM[$fi_i]}" "${F_TO[$fi_i]}")")
      n_restart=$((n_restart + 1))
      F_STATUS[$fi_i]="EMITTED"
    fi
    fi_i=$((fi_i + 1))
  done
  pi=0
  while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
    if [[ "${P_STATUS[$pi]}" == "OK" ]]; then
      for a in ${ATOMIC_NOTES[@]+"${ATOMIC_NOTES[@]}"}; do
        if [[ "${a#*:}" == "${P_ID[$pi]}" ]]; then
          group="${group:+$group + }$([[ "${P_KIND[$pi]}" == "kit" ]] && echo kit || echo pack) ${P_ID[$pi]} ${P_INST_VER[$pi]} -> ${P_CAND_TAG[$pi]}"
          a_lines+=("$(printf 'ATOMIC-PACK\t%s\t%s\t%s\t%s\t%s\t%s\t%s' \
            "${P_ID[$pi]}" "${P_KIND[$pi]}" "${P_PROVIDES[$pi]}" "${P_INST_VER[$pi]}" \
            "${P_CAND_TAG[$pi]}" "${P_CAND_VER[$pi]}" "${P_MEMBERS[$pi]}")")
          m_n=0
          for m in ${P_MEMBERS[$pi]}; do m_n=$((m_n + 1)); done
          n_restart=$((n_restart + m_n))
          P_STATUS[$pi]="EMITTED"
          break
        fi
      done
    fi
    pi=$((pi + 1))
  done
  if [[ -n "$group" ]]; then
    STEP=$((STEP + 1))
    _log "  $STEP. ATOMIC GROUP [$group] -- 그룹 내부 prefix 없음, 함께 적용 (재시작 ${n_restart}건)"
    _emit "$(printf 'ATOMIC\t%s' "$group")"
    for al in ${a_lines[@]+"${a_lines[@]}"}; do
      _emit "$al"
    done
    _emit "$(printf 'ATOMIC-END\t%s' "${#a_lines[@]}")"
    RESTART_TOTAL=$((RESTART_TOTAL + n_restart))
    PLANNED=$((PLANNED + 1))
  fi
fi

# framework items (default first position)
fi_i=0
while [[ "$fi_i" -lt "${#F_NAME[@]}" ]]; do
  if [[ "${F_STATUS[$fi_i]}" == "OK" ]]; then
    _emit_fw "$fi_i"
    F_STATUS[$fi_i]="EMITTED"
  fi
  fi_i=$((fi_i + 1))
done

# kit packs, then non-kit packs (alphabetical id order preserved from scan)
for pass in kit other; do
  pi=0
  while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
    if [[ "${P_STATUS[$pi]}" == "OK" ]]; then
      k="${P_KIND[$pi]}"
      if { [[ "$pass" == "kit" && "$k" == "kit" ]]; } || { [[ "$pass" == "other" && "$k" != "kit" ]]; }; then
        _emit_pack "$pi"
        P_STATUS[$pi]="EMITTED"
      fi
    fi
    pi=$((pi + 1))
  done
done

if [[ "$PLANNED" -eq 0 ]]; then
  _log "  (적용 항목 0건)"
fi

# ---------------------------------------------------------------------------
# 8) Blocked/held summary + restart counts (spec §6: counts always printed).
# ---------------------------------------------------------------------------
echo ""
_log "== 차단/보류/스킵 =="
BLOCKED_N=0
fi_i=0
while [[ "$fi_i" -lt "${#F_NAME[@]}" ]]; do
  st="${F_STATUS[$fi_i]}"
  case "$st" in
    EMITTED|UP-TO-DATE) : ;;
    *) _log "  framework ${F_NAME[$fi_i]}: $st${F_DETAIL[$fi_i]:+ -- ${F_DETAIL[$fi_i]}}"
       BLOCKED_N=$((BLOCKED_N + 1)) ;;
  esac
  fi_i=$((fi_i + 1))
done
pi=0
while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
  st="${P_STATUS[$pi]}"
  case "$st" in
    EMITTED|UP-TO-DATE) : ;;
    *) _log "  pack ${P_ID[$pi]}: $st"
       BLOCKED_N=$((BLOCKED_N + 1)) ;;
  esac
  pi=$((pi + 1))
done
if [[ "$BLOCKED_N" -eq 0 ]]; then
  _log "  (차단/보류 0건)"
fi

echo ""
_log "적용 항목 ${PLANNED}건 / 차단·보류 ${BLOCKED_N}건 / 재시작 ${RESTART_TOTAL}건"
_log "계획 산출 완료 (read-only, 쓰기 0건 -- 사설 tmp 외 어떤 파일도 만들지 않음)"

exit "$OVERALL_RC"

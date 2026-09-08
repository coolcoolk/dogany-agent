#!/bin/bash
# update_apply.sh -- DGN-1031 slice 2+3+4: the WRITE path of the unified
# update loop (instance-scoped units + crew-atomic transactions + framework
# items + ATOMIC GROUPS + the single completion notice = the closed loop).
#
# CONSUMES the slice-1 planner (scripts/pack/update_plan.sh) through its
# machine-readable emit channel (DOGANY_PLAN_EMIT). Plan logic is NOT
# re-derived here; the planner's product is the plan. Application itself is
# delegated ENTIRELY to existing machinery (spec §0: no new install
# algorithm, standalone paths untouched):
#   - packs      -> scripts/pack/pack_install.sh --upgrade
#   - framework  -> <instance>/routines/self-update.sh --no-restart
#     (--no-restart is self-update.sh's OWN shipped local flag -- the
#     restart deferral needs zero modification of self-update.sh. The loop
#     pins the child to the PLANNED candidate via DOGANY_UPDATE_PIN, which
#     resolve_channel_tag honours by contract, so the plan-apply window can
#     never land a different tag than the one the plan gated.)
# This script owns ONLY the loop seam:
#   - gate assertion (fail-closed, spec §4 belt 1)
#   - atomic-unit bridge stop / restart policy (spec §2 중단·재시작 정책)
#   - crew-atomic T0~T-end transaction (spec §3 -- slice 3), generalized to
#     the ATOMIC GROUP boundary (spec §2 -- slice 4)
#   - framework restart deferral to loop end (spec §2 rule 1 -- slice 4)
#   - ONE completion notice per run (slice 4; copy NOT locked, dec-094)
#   - the §6 loud accounting (counts always printed; 0 is suspicion)
#
# SCOPE (spec §2 결정적 적용 순서 -- the full unified turn):
#   - framework items (FW)                             -> self-update.sh
#     delegation; restart DEFERRED to loop end (spec §2: "재시작은 루프
#     끝으로 유예"). A completed fw unit whose instance gets restarted by a
#     LATER completed unit is not restarted twice (one restart per agent);
#     a completed fw unit whose instance is held down by a later INCOMPLETE
#     unit stays down (the stay-down policy of that unit wins).
#   - PACK items with kind != kit and no provides_kit  -> APPLY per instance
#   - crew-scoped kit items (kind=kit / provides_kit)  -> crew-atomic apply
#     (slice 3: the WHOLE crew is ONE atomic unit, spec §3 T0~T-end)
#   - ATOMIC GROUP items (fw + crew pack, prefix-incompatible by planner
#     proof)                                           -> ONE atomic unit:
#     the slice-3 T0~T-end transaction generalized to the group boundary
#     (stop everything -> backup -> apply -> verify -> restart everything).
#     INTERNAL ORDER = packs FIRST, then framework. Rationale: update.sh
#     runs fw_reqframework_guard fail-closed at fw-apply time, evaluating
#     the INSTALLED pack set -- fw-first inside the group would be blocked
#     by the still-old pack range (the exact incompatibility that made the
#     group atomic). Pack-first lets the shipped guard see the advanced
#     pack set; the group has no live prefix anyway (all bridges are down),
#     and a residual violation still fail-closes as INCOMPLETE.
#     Supported composition: >=1 framework item + exactly ONE crew-scoped
#     pack item (today's only structurally producible group). Any other
#     composition ABORTs loudly -- never guessed at.
#
# ATOMIC-UNIT POLICY (spec §2, single source):
#   instance-scoped: one (pack, instance) application = one atomic unit.
#   crew-scoped: the WHOLE crew (all member instances) = ONE atomic unit --
#   never an individual member. The unit's bridge(s) are stopped BEFORE any
#   write. A COMPLETED unit restarts its own bridge(s) and counts as landed.
#   An INCOMPLETE unit (pack_install rc != 0, or a rc-0 run whose output
#   lacks the install-side gate PASS line, or a post-apply DOGANY_PACKS
#   record mismatch) is NEVER restarted -- its bridge(s) STAY DOWN (no
#   half-state returns to live; for a crew that means EVERY member bridge
#   stays down) and remaining units are NOT attempted (fail-fast; units
#   completed BEFORE the failure stay restarted -- the stop does not
#   propagate across atomic-unit boundaries).
#
# CREW TRANSACTION (spec §3, T0~T-end):
#   T0 (pre-entry, ZERO writes until every check passes):
#     - apply-time crew re-discovery: crew.conf members re-read fresh and
#       3-source cross-validated (crew.conf / DOGANY_PACKS <kit>@ entry /
#       readlink <root>/database/<kit>.db == $SHARED_HOME/crews/<kit>/<kit>.db).
#       ANY mismatch = ABORT. 0 members = ABORT (never a pass, §6).
#       plan-vs-conf member-set drift = ABORT (plan snapshot never trusted).
#     - per-member fresh installed versions must be IDENTICAL (an asymmetric
#       crew is suspected half-state) + stale-plan / downgrade guards.
#     - ONE payload extraction; every member installs from the same tree
#       (per-member version divergence structurally impossible).
#     - per-member pack_install --dry-run must PASS; belt-1 lint presence;
#       restart-target plists per member (0 = suspicion); disk headroom.
#     - stop ALL member bridges (verified). A stop failure = pre-entry ABORT
#       with zero writes; already-stopped members stay down conservatively
#       (same direction as the slice-2 preflight-failure policy) with manual
#       restart commands printed.
#     - ONE loop-level shared-DB backup (sqlite3 .backup, fixed naming),
#       taken AFTER the stop so it snapshots the quiesced pre-transaction
#       state -- and so a pre-entry abort provably wrote NOTHING.
#   T1..TN: pack_install --upgrade per member from the single extraction.
#     The shared crew DB migrates EXACTLY ONCE: member #1's kit-migrate
#     advances PRAGMA user_version to the code pin THROUGH the symlink;
#     every later member re-reads cur==pin and skips the apply loop
#     (pack_install.sh kit-migrate contract), with the endpoint check still
#     asserting sync. No loop-side migration logic exists here at all.
#   T-end: per-member DOGANY_PACKS record verified (both strings printed),
#     THEN all member bridges restart. Any earlier failure -> every crew
#     bridge stays down + T0 backup path and manual recovery steps printed.
#
# GATES (all fail-closed):
#   - planner rc != 0                    -> ABORT (zero application)
#   - compat-lint.sh absent/unreadable   -> ABORT at entry + re-assert per
#     unit immediately before pack_install (spec §4 belt 1). The lint RUN
#     itself is pack_install's install-side gate -- invoked exactly once per
#     unit (a second loop-level run would double-invoke the linter on the
#     same payload; see DGN-1036).
#   - belt-2 output scan: a pack_install rc-0 run whose output lacks
#     "compat-lint: install-side gate PASS" (e.g. the fail-open WARN+skip
#     path fired) is treated as INCOMPLETE -- never as success.
#     DOGANY_STRICT_GATES=1 is also exported for the future in-installer
#     belt (spec §4 belt 2; inert today, standalone path unchanged).
#   - downgrade guard: candidate < installed AT APPLY TIME (re-read fresh
#     from .instance.conf, never trusted from the plan snapshot) -> BLOCKED,
#     never applied. candidate == installed -> BLOCKED-STALE-PLAN (the plan
#     aged between plan and apply -- suspicious, loud, not silent success).
#     Comparison runs through scripts/pack/lib/semver_range.py (single
#     truth source -- no new semver logic).
#   - extracted payload manifest id/kind/provides/pack_version asserts
#     (subscription-poisoning + crew-scope defense in depth).
#   - restart-target plists 0건 -> BLOCKED (0 is suspicion, §6).
#
# NOTIFICATION (slice 4 + sender/abort fix): AT MOST ONE push per run --
# normal completion OR abort, never both. Body = §6 counts (완주/차단/
# 미완주/재시작); an INCOMPLETE unit MUST surface in the notice (a run with
# any incomplete unit can never read as a clean success). SENDER IS FIXED:
# the notice leaves from the operator instance (slug match via
# .instance.conf DOGANY_AGENT_NAME == DOGANY_APPLY_NOTIFY_SLUG, default
# "metal") -- never from whichever plan item came first (predictable sender;
# domain agents are not infra-report channels). Operator unresolvable ->
# LOUD WARN + full body into the run log; NO proxy send through another
# instance. ABORT paths send one notice too (EXIT trap): reason, best-known
# counts (marked non-final when the walk did not finish), and which bridges
# remain STOPPED -- an update that dies half-way can never die silently.
# Push path = the existing per-instance routines/push.sh core
# (dogany-proactive-push; watchdog.sh precedent) -- no new push channel.
# USER-FACING COPY IS NOT LOCKED: every notice line is carried under an
# explicit 미확정(형님 확인 대기) marker until the owner approves final
# wording (dec-094 UX gate) -- this script ships mechanics and wiring only.
#
# --dry-run: full plan consumption, per-unit guard evaluation and intended
# actions printed, pack_install --dry-run preflight per applicable unit.
# Framework units print the intended self-update command WITHOUT invoking it
# (self-update.sh performs a `git fetch --tags` against the shared repo even
# under --dry-run -- a network op and a ref write outside this loop's
# zero-write promise). ZERO instance writes, ZERO launchctl calls, ZERO
# restarts, ZERO pushes (extraction goes to this script's private tmp only,
# removed on exit).
#
# Env (TEST HERMETICITY / rehearsal ONLY -- update_plan.sh doctrine):
#   DOGANY_SHARED_HOME / DOGANY_PACK_SOURCES / DOGANY_CREW_DIR /
#   DOGANY_PLAN_INSTANCES   passed through to the planner; registry lookup
#                           uses the same resolution
#   DOGANY_LAUNCHD_CAPTURE  rehearsal file (pack_install convention): launchd
#                           stop/restart commands are CAPTURED there, not
#                           executed (the completion notice is NOT captured
#                           here -- notice delivery goes through the resolved
#                           push.sh, which tests stub via
#                           DOGANY_APPLY_NOTIFY_ROOT)
#   DOGANY_PACK_CATALOG     catalog override passed to pack_install --catalog
#                           (update_seed.sh precedent; unset = installer
#                           default, i.e. the framework repo catalog)
#   DOGANY_APPLY_PLAN_FILE  consume this emit file instead of running the
#                           planner (guard-path tests only)
#   DOGANY_APPLY_INSTALLER  pack_install.sh override (belt-2 scan test only)
#   DOGANY_APPLY_SELFUPDATE self-update.sh override (fw failure-injection
#                           tests only; unset = <root>/routines/self-update.sh)
#   DOGANY_APPLY_NOTIFY_ROOT  instance root whose routines/push.sh sends the
#                           single completion/abort notice (test stub seam;
#                           unset = fixed-sender resolution below)
#   DOGANY_APPLY_NOTIFY_SLUG  operator-instance slug the fixed-sender
#                           resolution matches against .instance.conf
#                           DOGANY_AGENT_NAME (default: metal). Infra notices
#                           always leave from the OPERATOR instance -- never
#                           from whichever plan item happened to come first
#                           (sender must be predictable for the owner; domain
#                           agents are not infra-report channels)
#
# Exit codes:
#   0 = every applicable unit landed and restarted (or applicable set is
#       legitimately empty with loud per-item reasons)
#   2 = ABORT / any BLOCKED / any INCOMPLETE / restart failure / accounting
#       mismatch
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
SOURCES_CONF="${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
PLANNER="$SCRIPT_DIR/update_plan.sh"
INSTALLER="${DOGANY_APPLY_INSTALLER:-$SCRIPT_DIR/pack_install.sh}"
MINT_RUN="$SCRIPT_DIR/mint_run.sh"
COMPAT_LINT="$SCRIPT_DIR/compat-lint.sh"
SEMVER_LIB="$SCRIPT_DIR/lib/semver_range.py"
CAPTURE="${DOGANY_LAUNCHD_CAPTURE:-}"
PLAN_FILE_OVERRIDE="${DOGANY_APPLY_PLAN_FILE:-}"
CATALOG_OVERRIDE="${DOGANY_PACK_CATALOG:-}"
SELFUPD_OVERRIDE="${DOGANY_APPLY_SELFUPDATE:-}"
NOTIFY_ROOT_OVERRIDE="${DOGANY_APPLY_NOTIFY_ROOT:-}"
NOTIFY_SLUG="${DOGANY_APPLY_NOTIFY_SLUG:-metal}"

# Installer arg vector shared by dry-run and real paths (single source).
CATALOG_ARGS=()
if [[ -n "$CATALOG_OVERRIDE" ]]; then
  CATALOG_ARGS=(--catalog "$CATALOG_OVERRIDE")
fi

DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run|--dry) DRY=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

_log()   { echo "[apply] $*"; }
_abort() { LAST_ABORT_MSG="$*"; echo "[apply] ABORT: $*" >&2; exit 2; }

_conf_get() { # <conf> <key>
  sed -n "s|^$2=||p" "$1" 2>/dev/null | head -n1
}

# Shared estate discovery (DGN-1037 machinery, extracted to a lib): the
# fixed-sender resolution scans the SAME instance set as the planner.
DISCOVER_LIB="$SCRIPT_DIR/lib/discover_instances.sh"
[[ -f "$DISCOVER_LIB" && -r "$DISCOVER_LIB" ]] \
  || _abort "인스턴스 탐지 라이브러리 부재 ($DISCOVER_LIB) -- fail-closed"
# shellcheck source=lib/discover_instances.sh
. "$DISCOVER_LIB"

# Notification state -- one run sends AT MOST one push (normal OR abort).
NOTIFY_SENT=0        # any push attempted (success or push-rc-fail) this run
RUN_COMPLETED=0      # set immediately before the normal final exit
LAST_ABORT_MSG=""    # last explicit _abort reason (abort-notice body)
STOPPED_PENDING=""   # roots this run stopped and has not restarted (yet)

# _resolve_notify_push -- fixed-sender resolution (sender defect fix).
# Sets NOTIFY_PUSH (executable push.sh path, '' = unresolved) and
# NOTIFY_PUSH_WHY (loud reason when unresolved). Read-only; never aborts
# (also runs inside the EXIT trap). Resolution order:
#   1. DOGANY_APPLY_NOTIFY_ROOT override (test stub seam -- kept)
#   2. DOGANY_PLAN_INSTANCES entries (hermetic runs scan the SAME candidate
#      set the planner saw -- a fixture run must never scan the real HOME)
#   3. _discover_instances HOME estate scan (shared DGN-1037 machinery)
# Match = .instance.conf DOGANY_AGENT_NAME == NOTIFY_SLUG. Multiple matches
# (misconfig) -> WARN + first in deterministic sorted order. NO fallback to
# other instances: domain agents are not infra-report channels.
NOTIFY_PUSH=""
NOTIFY_PUSH_WHY=""
_resolve_notify_push() {
  NOTIFY_PUSH=""; NOTIFY_PUSH_WHY=""
  local entries="" e root slug matches="" m n=0
  if [[ -n "$NOTIFY_ROOT_OVERRIDE" ]]; then
    if [[ -x "$NOTIFY_ROOT_OVERRIDE/routines/push.sh" ]]; then
      NOTIFY_PUSH="$NOTIFY_ROOT_OVERRIDE/routines/push.sh"
    else
      NOTIFY_PUSH_WHY="오버라이드 루트($NOTIFY_ROOT_OVERRIDE)에 실행 가능한 routines/push.sh 없음"
    fi
    return 0
  fi
  if [[ -n "${DOGANY_PLAN_INSTANCES:-}" ]]; then
    entries="${DOGANY_PLAN_INSTANCES//,/ }"
  else
    if ! entries="$(_discover_instances | tr '\n' ' ')"; then
      NOTIFY_PUSH_WHY="인스턴스 스캔 불가 (HOME 미설정/비실재)"
      return 0
    fi
  fi
  for e in $entries; do
    root="${e#*:}"
    [[ -f "$root/.instance.conf" ]] || continue
    # '|| true': an unreadable conf must not errexit-kill the resolver
    # (this also runs inside the EXIT trap) -- it just fails the match.
    slug="$(_conf_get "$root/.instance.conf" DOGANY_AGENT_NAME || true)"
    if [[ -n "$slug" && "$slug" == "$NOTIFY_SLUG" ]]; then
      matches="${matches:+$matches }$root"
      n=$((n + 1))
    fi
  done
  if [[ "$n" -eq 0 ]]; then
    NOTIFY_PUSH_WHY="발신자 인스턴스 미발견 -- 스캔 후보에 슬러그 '$NOTIFY_SLUG' 없음"
    return 0
  fi
  if [[ "$n" -gt 1 ]]; then
    _log "WARN: 발신자 슬러그 '$NOTIFY_SLUG' 다중 매치 ${n}건 -- 정렬 순서상 첫 매치 사용 (결정적)"
  fi
  for m in $matches; do
    if [[ -x "$m/routines/push.sh" ]]; then
      NOTIFY_PUSH="$m/routines/push.sh"
      return 0
    fi
  done
  NOTIFY_PUSH_WHY="슬러그 '$NOTIFY_SLUG' 인스턴스는 있으나 실행 가능한 routines/push.sh 없음"
  return 0
}

# _notify_send <label> <body> -- resolve the fixed sender and push ONCE.
# Marks NOTIFY_SENT on any attempt. Unresolved sender / push failure =
# LOUD WARN + full body into the run log (never a proxy send, never abort).
_notify_send() {
  local label="$1" body="$2"
  NOTIFY_SENT=1
  _resolve_notify_push
  if [[ -z "$NOTIFY_PUSH" ]]; then
    _log "WARN: ${label} 통지 발송 불가 -- ${NOTIFY_PUSH_WHY}. 대리 발송 없음(발신자 고정 -- 도메인 에이전트는 인프라 보고 채널이 아님), 수동 통지 필요 (조용한 실패 아님). 본문:"
    printf '%s\n' "$body" | sed 's/^/[apply]     /'
    return 0
  fi
  if "$NOTIFY_PUSH" --text "$body" >/dev/null 2>&1; then
    _log "${label} 통지 1통 발송 -- 발신자 고정: $NOTIFY_PUSH (슬러그 '$NOTIFY_SLUG')"
  else
    _log "WARN: ${label} 통지 발송 실패 (push rc != 0, $NOTIFY_PUSH) -- 수동 확인 필요. 본문:"
    printf '%s\n' "$body" | sed 's/^/[apply]     /'
  fi
  return 0
}

# _notify_abort <rc> -- ABORT-path single notice (EXIT trap only). Fires
# ONLY when the run died before its normal completion line (RUN_COMPLETED=0)
# and nothing was pushed yet (NOTIFY_SENT=0): one run = at most one push.
# Runs AFTER the deferred-restart flush so the bridge-state line reflects
# what the trap already recovered. Body carries: abort reason, best-known
# §6 counts (explicitly marked non-final / unknown when the walk never
# started), and which bridges remain STOPPED -- the owner must be able to
# answer "is my agent alive right now?". Never aborts, never exits (a
# notification failure inside the trap must not mask the original rc).
_notify_abort() {
  local xrc="$1" body r n names=""
  body="[미확정(형님 확인 대기)] 업데이트가 비정상 중단(ABORT)되었습니다 (rc=$xrc)"
  if [[ -n "${LAST_ABORT_MSG:-}" ]]; then
    body="$body
[미확정(형님 확인 대기)] 사유: ${LAST_ABORT_MSG}"
  else
    body="$body
[미확정(형님 확인 대기)] 사유: 명시적 ABORT 메시지 없음 -- 커맨드 실패/신호로 중단, 실행 로그 확인 필요"
  fi
  if [[ -n "${N_TARGET+x}" ]]; then
    body="$body
[미확정(형님 확인 대기)] 중단 시점 집계(최종 아님): 대상 ${N_TARGET}건 / 완주 ${N_OK}건 / 차단 ${N_BLOCK}건 / 미완주 ${N_INCOMPLETE}건 / 재시작 ${N_RESTART}건"
  else
    body="$body
[미확정(형님 확인 대기)] 집계 미확정 -- 적용 시작 전(계획/기계 검증) 단계에서 중단, 인스턴스 쓰기 0건"
  fi
  if [[ -n "${STOPPED_PENDING:-}" ]]; then
    for r in ${STOPPED_PENDING}; do
      n="$(basename "$r")"
      case " $names " in *" $n "*) : ;; *) names="${names:+$names }$n" ;; esac
    done
    body="$body
[미확정(형님 확인 대기)] 브리지 정지 유지: ${names} -- 이 실행이 정지시킨 뒤 재시작에 도달하지 못함. 수동 확인 필요"
  else
    body="$body
[미확정(형님 확인 대기)] 정지 상태로 남은 브리지 없음 (이 실행이 정지 후 재시작 못 한 브리지 0건 기준)"
  fi
  _notify_send "ABORT" "$body"
  return 0
}

# _unmark_stopped <root> -- drop every occurrence from STOPPED_PENDING
# (called on verified restart; a root can enter twice via fw + pack units).
_unmark_stopped() {
  local r new=""
  for r in ${STOPPED_PENDING:-}; do
    [[ "$r" == "$1" ]] && continue
    new="${new:+$new }$r"
  done
  STOPPED_PENDING="$new"
}

# Deferred-restart state (slice 4, spec §2 rule 1). Space-separated lists;
# roots contain no spaces (estate path doctrine, same assumption as the
# member "name:root" plan tokens).
DEFER_FW_ROOTS=""    # "name:root" of COMPLETED fw units awaiting loop-end restart
RESTARTED_ROOTS=""   # roots restarted by any completed unit this run
DOWN_ROOTS=""        # roots of INCOMPLETE units -- must stay down, never resurrected
FLUSH_DONE=0

TMP="$(mktemp -d "${TMPDIR:-/tmp}/dogany-apply.XXXXXX")" \
  || _abort "mktemp 실패 -- 사설 작업 디렉터리 없이는 진행하지 않습니다"

# EXIT trap: an ABORT anywhere after a completed fw unit must NOT lose the
# deferred restart (self-grill b: a lost deferral = a bridge that never
# comes back). The flush itself skips roots held down by an incomplete unit
# and roots already restarted -- abnormal exit gets the SAME policy.
_on_exit() {
  local xrc=$?
  if [[ "${FLUSH_DONE:-0}" -eq 0 && "${DRY:-0}" -eq 0 && -n "${DEFER_FW_ROOTS:-}" ]]; then
    _log "비정상 종료 경로 -- 프레임워크 재시작 유예분을 유실하지 않도록 지금 flush합니다 (완주 단위만)"
    _flush_deferred || true
  fi
  # ABORT-path notice (silent-death fix): a run that dies before its normal
  # completion line still owes the owner ONE notice -- gated on NOTIFY_SENT
  # so a run can never push twice, and on DRY (dry-run sends nothing).
  # Ordered AFTER the flush so the bridge-state line sees the recovery.
  if [[ "${RUN_COMPLETED:-0}" -eq 0 && "${NOTIFY_SENT:-0}" -eq 0 && "${DRY:-0}" -eq 0 ]]; then
    if declare -F _notify_abort >/dev/null 2>&1; then
      _notify_abort "$xrc" || true
    fi
  fi
  rm -rf "$TMP"
  exit "$xrc"
}
trap '_on_exit' EXIT INT TERM

# ---------------------------------------------------------------------------
# 0) Machinery presence asserts (fail-closed -- absent machinery = ABORT).
# ---------------------------------------------------------------------------
[[ -f "$PLANNER"   && -r "$PLANNER"   ]] || _abort "플래너 부재 ($PLANNER) -- fail-closed"
[[ -f "$INSTALLER" && -r "$INSTALLER" ]] || _abort "pack_install 부재 ($INSTALLER) -- fail-closed"
[[ -f "$SEMVER_LIB" ]] || _abort "semver 비교기 부재 ($SEMVER_LIB) -- fail-closed"
if [[ ! -f "$COMPAT_LINT" || ! -r "$COMPAT_LINT" ]]; then
  _abort "호환성 검사기(compat-lint.sh)가 없어 전체를 중단합니다 -- fail-closed (spec §4 belt 1). 프레임워크 설치가 불완전한 상태입니다. ($COMPAT_LINT)"
fi
if [[ "$DRY" -eq 0 ]]; then
  [[ -f "$MINT_RUN" && -r "$MINT_RUN" ]] || _abort "mint_run.sh 부재 ($MINT_RUN) -- 재시작 기계 없이 적용하지 않습니다, fail-closed"
fi

# _vercmp <version> <range> -- rc 0 satisfied / 1 not / 2 unevaluable
_vercmp() {
  local rc=0
  python3 "$SEMVER_LIB" satisfies "$1" "$2" >/dev/null 2>&1 || rc=$?
  return "$rc"
}

_reg_repo() { # <id> -- prints registry repo path ('' when absent)
  [[ -f "$SOURCES_CONF" ]] || { echo ""; return 0; }
  sed -n "s|^$1\.repo=||p" "$SOURCES_CONF" | head -n1
}

_installed_ver() { # <conf> <pack-id> -- prints installed version ('' = none)
  local packs it
  packs="$(_conf_get "$1" DOGANY_PACKS)"
  [[ -n "$packs" ]] || { echo ""; return 0; }
  IFS=',' read -r -a _items <<< "$packs"
  for it in "${_items[@]}"; do
    if [[ "${it%%@*}" == "$2" ]]; then echo "${it#*@}"; return 0; fi
  done
  echo ""
}

# ---------------------------------------------------------------------------
# 1) Produce the plan (the planner's product IS the plan -- no re-derivation).
# ---------------------------------------------------------------------------
PLAN_TSV="$TMP/plan.tsv"
if [[ -n "$PLAN_FILE_OVERRIDE" ]]; then
  _log "TEST: DOGANY_APPLY_PLAN_FILE 주입 -- 플래너 실행 생략 ($PLAN_FILE_OVERRIDE)"
  cp "$PLAN_FILE_OVERRIDE" "$PLAN_TSV" || _abort "주입 plan 파일 판독 불가"
else
  _log "== 계획 산출 (update_plan.sh 소비) =="
  PLAN_RC=0
  DOGANY_PLAN_EMIT="$PLAN_TSV" bash "$PLANNER" || PLAN_RC=$?
  if [[ "$PLAN_RC" -ne 0 ]]; then
    _abort "플래너 rc=$PLAN_RC -- 계획 실패 상태에서는 어떤 적용도 하지 않습니다 (fail-closed)"
  fi
fi
[[ -f "$PLAN_TSV" ]] || _abort "plan emit 파일 미생성 ($PLAN_TSV) -- fail-closed"
head -n1 "$PLAN_TSV" | grep -q '^PLAN-EMIT-V1$' \
  || _abort "plan emit 헤더 불일치 -- 이 실행의 산출물이 아님, fail-closed"

# Item count: FW / PACK / ATOMIC headers only -- the structured ATOMIC-FW /
# ATOMIC-PACK / ATOMIC-END member lines belong to their group, not the count.
PLAN_ITEMS="$(awk -F'\t' '$1=="FW" || $1=="PACK" || $1=="ATOMIC" {n++} END {print n+0}' "$PLAN_TSV")"
_log ""
_log "== 적용 단계 (통합 1턴: framework + pack) dry-run=$DRY =="
_log "계획 적용 항목 ${PLAN_ITEMS}건"
if [[ "$PLAN_ITEMS" -eq 0 ]]; then
  _log "계획 적용 항목 0건 -- 차단/보류 사유는 상단 플래너 출력 참조 (0건은 통과가 아니라 의심)"
fi

# ---------------------------------------------------------------------------
# 2) Unit walk. Counts per §6 -- every lane printed, 0 is suspicion.
# ---------------------------------------------------------------------------
N_TARGET=0      # applicable atomic units (instance-scoped pack x instance)
N_OK=0          # completed + record-verified
N_BLOCK=0       # blocked before any write (bridge untouched)
N_INCOMPLETE=0  # started but did not complete -- bridge stays DOWN
N_UNTRIED=0     # not attempted after a fail-fast stop
N_SKIP=0        # out-of-scope plan items (fw / crew / atomic), loud
N_RESTART=0     # bridges restarted
N_RESTART_FAIL=0
FAILFAST=0
UNIT_N=0
OVERALL_RC=0

# _launchd_stop <root> -- bootout every bridge/routines plist label (the
# routines/watchdog units are included so nothing can resurrect the bridge
# mid-apply). rc 0 only when every label is VERIFIED gone afterwards -- a
# bootout failure with the job still alive must never read as "already
# stopped" (files would be swapped under a RUNNING bridge, self-grill a).
_launchd_stop() {
  local root="$1" p label rc=0
  # Abort-notice bookkeeping: from this point the root counts as "stopped
  # and not yet restarted" until a verified restart unmarks it.
  STOPPED_PENDING="${STOPPED_PENDING:+$STOPPED_PENDING }$root"
  for p in "$root"/bridge/*.plist "$root"/routines/*.plist; do
    [[ -e "$p" ]] || continue
    label="$(basename "$p" .plist)"
    if [[ -n "$CAPTURE" ]]; then
      printf 'launchctl bootout gui/UID/%s\n' "$label" >> "$CAPTURE"
      _log "    (rehearsal) bootout 캡처: $label"
      continue
    fi
    if launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1; then
      _log "    브리지 정지: $label"
    else
      _log "    브리지 정지: $label (bootout rc!=0 -- 미적재 추정, 검증으로 확인)"
    fi
    # verification: the label must be GONE from launchd now (self_restart.sh
    # cur_pid precedent). Alive label = stop failure = unit must not proceed.
    if launchctl list 2>/dev/null | awk -v l="$label" '$3==l {found=1} END {exit !found}'; then
      _log "    정지 검증 FAIL: $label 이 launchd에 살아 있음 -- 이 단위를 진행하지 않습니다 (fail-closed)"
      rc=1
    fi
  done
  return "$rc"
}

_plist_count() { # <root>
  local root="$1" n=0 p
  for p in "$root"/bridge/*.plist "$root"/routines/*.plist; do
    [[ -e "$p" ]] && n=$((n + 1))
  done
  echo "$n"
}

_restart() { # <root> -- rc 0 on success (successful roots recorded for the
  local root="$1" rc=0    # loop-end deferral dedup: one restart per agent)
  if [[ -n "$CAPTURE" ]]; then
    printf 'mint_run.sh start --root %s\n' "$root" >> "$CAPTURE"
    _log "    (rehearsal) 재시작 캡처: mint_run.sh start --root $root"
  else
    bash "$MINT_RUN" start --root "$root" < /dev/null || rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    RESTARTED_ROOTS="${RESTARTED_ROOTS:+$RESTARTED_ROOTS }$root"
    _unmark_stopped "$root"
  fi
  return "$rc"
}

_mark_down() { # <root>... -- record roots of an INCOMPLETE unit (stay down)
  local r
  for r in "$@"; do
    DOWN_ROOTS="${DOWN_ROOTS:+$DOWN_ROOTS }$r"
  done
}

# _flush_deferred -- loop-end flush of deferred framework restarts (spec §2:
# fw restart deferred to loop end, folded into the atomic-unit policy):
#   - root held down by a later INCOMPLETE unit  -> stays down (loud)
#   - root already restarted by a later completed unit -> skip (one restart
#     per agent -- never a double restart)
#   - otherwise -> restart now, counted
# Runs on the normal path AND from the EXIT trap (abnormal exits keep their
# deferrals -- self-grill b). Idempotent via FLUSH_DONE + list reset.
_flush_deferred() {
  FLUSH_DONE=1
  [[ -n "$DEFER_FW_ROOTS" ]] || return 0
  local e name root
  _log ""
  _log "== 프레임워크 재시작 유예분 flush (루프 끝 -- 완주 단위만) =="
  for e in $DEFER_FW_ROOTS; do
    name="${e%%:*}"
    root="${e#*:}"
    if [[ " $DOWN_ROOTS " == *" $root "* ]]; then
      _log "  framework $name: 재시작 보류 -- 동일 인스턴스의 후속 원자 단위 미완주로 정지 유지 (반쪽 상태 라이브 복귀 금지)"
      continue
    fi
    if [[ " $RESTARTED_ROOTS " == *" $root "* ]]; then
      _log "  framework $name: 후속 완주 단위가 이미 재시작함 -- 중복 재시작 생략 (에이전트당 재시작 1회)"
      continue
    fi
    if _restart "$root"; then
      N_RESTART=$((N_RESTART + 1))
      _log "  framework $name: 브리지 재시작 완료 (유예분)"
    else
      N_RESTART_FAIL=$((N_RESTART_FAIL + 1))
      OVERALL_RC=2
      _log "  framework $name: 재시작 실패 -- 수동 재기동 필요: $MINT_RUN start --root $root"
    fi
  done
  DEFER_FW_ROOTS=""
}

# ---------------------------------------------------------------------------
# Framework unit machinery (slice 4). Application is delegated ENTIRELY to
# the instance's own routines/self-update.sh (the shipped framework loop:
# channel/pin resolution, fetch, git-archive extraction, child update.sh
# --no-pull --yes, and the fail-closed fw_reqframework_guard inside
# update.sh). The loop adds ONLY: fresh pre-checks, the stop envelope, the
# planned-candidate pin, landing verification, and restart deferral via
# self-update.sh's OWN --no-restart flag (no self-update modification).
# ---------------------------------------------------------------------------

# _fw_precheck <name> <root> <to> -- read-only; rc 0 ok / 1 blocked (logged).
# Sets FW_SU (resolved self-update entrypoint) and FW_NOW (fresh version).
FW_SU=""
FW_NOW=""
_fw_precheck() {
  local name="$1" root="$2" to="$3"
  local conf="$root/.instance.conf" ch now eq_rc=0 ge_rc=0 plc
  FW_SU=""; FW_NOW=""

  if [[ ! -f "$conf" || ! -r "$conf" ]]; then
    _log "  framework $name: BLOCKED -- .instance.conf 판독 불가 ($conf), fail-closed"
    return 1
  fi
  # Channel re-read: a channel=main instance is never a tag-candidate target
  # (planner SKIP-MAIN); seeing one here means the conf changed in the
  # plan-apply window -- fail closed, never pull-apply.
  ch="$(_conf_get "$conf" DOGANY_UPDATE_CHANNEL)"
  if [[ "$ch" == "main" ]]; then
    _log "  framework $name: BLOCKED -- 적용 시점 채널이 main (태그 후보 대상 아님. 계획-적용 창 사이 변동 의심), fail-closed"
    return 1
  fi
  now="$(_conf_get "$conf" DOGANY_FW_VERSION)"
  if [[ -z "$now" ]]; then
    _log "  framework $name: BLOCKED -- 적용 시점 재판독에서 DOGANY_FW_VERSION 부재, fail-closed"
    return 1
  fi
  _log "  framework $name: 적용 시점 재판독 -- 설치 $now vs 후보 $to (두 문자열 실비교)"
  _vercmp "$to" "==$now" || eq_rc=$?
  if [[ "$eq_rc" -eq 2 ]]; then
    _log "  framework $name: BLOCKED -- 버전 비교 불가 (후보 '$to' vs 설치 '$now'), fail-closed"
    return 1
  fi
  if [[ "$eq_rc" -eq 0 ]]; then
    _log "  framework $name: BLOCKED-STALE-PLAN -- 후보 == 설치($now). 계획 이후 설치본이 변동됨 (조용한 성공으로 세지 않음)"
    return 1
  fi
  _vercmp "$to" ">=$now" || ge_rc=$?
  if [[ "$ge_rc" -ne 0 ]]; then
    _log "  framework $name: BLOCKED-DOWNGRADE -- 후보 $to < 설치 $now. 다운그레이드는 절대 적용하지 않습니다 (fail-closed)"
    return 1
  fi
  FW_SU="${SELFUPD_OVERRIDE:-$root/routines/self-update.sh}"
  if [[ ! -f "$FW_SU" || ! -r "$FW_SU" ]]; then
    _log "  framework $name: BLOCKED -- self-update 진입점 부재 ($FW_SU). 프레임워크 적용은 self-update 경로 전량 위임이므로 진입점 없이 적용하지 않습니다 (fail-closed)"
    return 1
  fi
  plc="$(_plist_count "$root")"
  if [[ "$plc" -eq 0 ]]; then
    _log "  framework $name: BLOCKED -- 재시작 대상 plist 0건 ($root) -- 0건은 통과가 아니라 의심 (fail-closed)"
    return 1
  fi
  FW_NOW="$now"
  return 0
}

# _fw_payload <name> <root> <to> <su> -- the delegated application itself.
# rc 0 landed+verified / 2 incomplete (logged; caller keeps the bridge down).
_fw_payload() {
  local name="$1" root="$2" to="$3" su="$4"
  local conf="$root/.instance.conf" out="$TMP/fw.$UNIT_N.$name.out" rc=0 rec
  _log "  framework $name: self-update.sh 위임 (--no-restart, DOGANY_UPDATE_PIN=v$to -- 계획 후보 고정)"
  DOGANY_UPDATE_PIN="v$to" bash "$su" --no-restart < /dev/null > "$out" 2>&1 || rc=$?
  sed 's/^/[apply]     /' "$out"
  if [[ "$rc" -ne 0 ]]; then
    _log "  framework $name: 미완주 -- self-update rc=$rc. 브리지 재시작하지 않음, 정지 유지 (반쪽 상태 라이브 복귀 금지)."
    _log "  framework $name: 수동 복구 후 재기동: $MINT_RUN start --root $root"
    return 2
  fi
  # Belt 2 (fw wording): update.sh runs fw_reqframework_guard unconditionally
  # and its "[fw-reqfw-guard] INFO: packs evaluated: N" line prints for EVERY
  # evaluation including N=0. A rc-0 run without that line did not pass
  # through the shipped gate (old update.sh / laundered path) -- refused.
  if ! grep -qF "[fw-reqfw-guard] INFO: packs evaluated:" "$out"; then
    _log "  framework $name: FATAL -- self-update rc=0인데 [fw-reqfw-guard] 판정 라인 부재 (게이트 미통과 경로 의심). 성공으로 세지 않음, 정지 유지."
    return 2
  fi
  rec="$(_conf_get "$conf" DOGANY_FW_VERSION)"
  _log "  framework $name: 착지 검증 -- DOGANY_FW_VERSION '$rec' vs 후보 '$to' (두 문자열 실비교)"
  if [[ "$rec" != "$to" ]]; then
    _log "  framework $name: FATAL -- 적용 후 기록('$rec')이 후보('$to')와 불일치. 성공으로 세지 않음, 정지 유지."
    return 2
  fi
  return 0
}

# _apply_fw <name> <root> <from> <to> -- standalone framework atomic unit.
# returns: 0 OK (restart DEFERRED to loop end) / 1 BLOCKED / 2 INCOMPLETE
_apply_fw() {
  local name="$1" root="$2" from="$3" to="$4"
  local plc rc=0

  _fw_precheck "$name" "$root" "$to" || return 1
  plc="$(_plist_count "$root")"

  if [[ "$DRY" -eq 1 ]]; then
    _log "  framework $name: (dry-run) 계획 -- 정지 plist ${plc}건 -> self-update.sh --no-restart (DOGANY_UPDATE_PIN=v$to) -> 착지 검증(DOGANY_FW_VERSION) -> 재시작은 루프 끝으로 유예"
    _log "  framework $name: (dry-run) self-update 미호출 -- dry-run에서도 git fetch(공유 레포 ref 쓰기)가 발생하므로 호출 자체를 생략 (쓰기 0건 계약)"
    _log "  framework $name: (dry-run) 적용 예정 -- 쓰기/정지/재시작 실행 안 함"
    return 0
  fi

  _log "  framework $name: 브리지 정지 (plist ${plc}건) -- 원자 단위 진입"
  if ! _launchd_stop "$root"; then
    _log "  framework $name: BLOCKED -- 브리지 정지 검증 실패. 라이브 브리지 밑에서 프레임워크를 교체하지 않습니다 (fail-closed, 쓰기 0건 상태)"
    return 1
  fi

  _fw_payload "$name" "$root" "$to" "$FW_SU" || rc=$?
  [[ "$rc" -eq 0 ]] || return "$rc"

  DEFER_FW_ROOTS="${DEFER_FW_ROOTS:+$DEFER_FW_ROOTS }$name:$root"
  _log "  framework $name: 완주(재시작 유예) -- $FW_NOW -> $to. 재시작은 루프 끝 일괄 flush (동일 인스턴스 후속 단위와 합류, 에이전트당 재시작 1회)"
  return 0
}

# _apply_unit <pid> <root> <name> <cand_tag> <cand_ver> <payload_dir>
# returns: 0 OK / 1 BLOCKED / 2 INCOMPLETE
_apply_unit() {
  local pid="$1" root="$2" name="$3" cand_tag="$4" cand_ver="$5" pdir="$6"
  local conf="$root/.instance.conf" slug inst_now rc plc

  slug="$(_conf_get "$conf" DOGANY_AGENT_NAME)"
  if [[ -z "$slug" ]]; then
    _log "  $name/$pid: BLOCKED -- DOGANY_AGENT_NAME 부재 ($conf), 설치 슬러그 해석 불가 (fail-closed)"
    return 1
  fi

  # Fresh installed version -- the plan snapshot is never trusted here.
  inst_now="$(_installed_ver "$conf" "$pid")"
  if [[ -z "$inst_now" ]]; then
    _log "  $name/$pid: BLOCKED -- 적용 시점 재판독에서 DOGANY_PACKS에 $pid@ 엔트리 없음 (계획-적용 창 사이 변동 의심, fail-closed)"
    return 1
  fi
  _log "  $name/$pid: 적용 시점 재판독 -- 설치 $inst_now vs 후보 $cand_ver (두 문자열 실비교)"
  local eq_rc=0 ge_rc=0
  _vercmp "$cand_ver" "==$inst_now" || eq_rc=$?
  if [[ "$eq_rc" -eq 2 ]]; then
    _log "  $name/$pid: BLOCKED -- 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$inst_now'), fail-closed"
    return 1
  fi
  if [[ "$eq_rc" -eq 0 ]]; then
    _log "  $name/$pid: BLOCKED-STALE-PLAN -- 후보 == 설치($inst_now). 계획 이후 설치본이 변동됨 (조용한 성공으로 세지 않음)"
    return 1
  fi
  _vercmp "$cand_ver" ">=$inst_now" || ge_rc=$?
  if [[ "$ge_rc" -ne 0 ]]; then
    _log "  $name/$pid: BLOCKED-DOWNGRADE -- 후보 $cand_ver < 설치 $inst_now. 다운그레이드는 절대 적용하지 않습니다 (표류 가드와 동일 방향, fail-closed)"
    return 1
  fi

  # Restart-target presence: 0 plists is suspicion, not a pass (§6).
  plc="$(_plist_count "$root")"
  if [[ "$plc" -eq 0 ]]; then
    _log "  $name/$pid: BLOCKED -- 재시작 대상 plist 0건 ($root) -- 0건은 통과가 아니라 의심 (fail-closed)"
    return 1
  fi

  # Belt 1 re-assert immediately before the installer call (spec §4).
  if [[ ! -f "$COMPAT_LINT" || ! -r "$COMPAT_LINT" ]]; then
    _log "  $name/$pid: 호환성 검사기(compat-lint.sh)가 없어 이 항목을 중단합니다 -- fail-closed. 프레임워크 설치가 불완전한 상태입니다."
    return 1
  fi

  if [[ "$DRY" -eq 1 ]]; then
    _log "  $name/$pid: (dry-run) 계획 -- 정지 plist ${plc}건 -> pack_install --upgrade (--pack-dir $pdir) -> mint_run.sh start"
    local drc=0 dout="$TMP/dry.$UNIT_N.out"
    DOGANY_STRICT_GATES=1 bash "$INSTALLER" "$slug" "$root" \
      --pack "$pid" --pack-dir "$pdir" ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
      --upgrade --no-start --no-state --dry-run \
      < /dev/null > "$dout" 2>&1 || drc=$?
    sed 's/^/[apply]     /' "$dout"
    if [[ "$drc" -ne 0 ]]; then
      _log "  $name/$pid: BLOCKED -- pack_install --dry-run 플랜 FAIL (rc=$drc)"
      return 1
    fi
    _log "  $name/$pid: (dry-run) 적용 예정 -- 쓰기/정지/재시작 실행 안 함"
    return 0
  fi

  # ---- real apply: stop -> install -> verify -> restart --------------------
  _log "  $name/$pid: 브리지 정지 (plist ${plc}건) -- 원자 단위 진입"
  if ! _launchd_stop "$root"; then
    _log "  $name/$pid: BLOCKED -- 브리지 정지 검증 실패. 라이브 브리지 밑에서 파일을 교체하지 않습니다 (fail-closed, 쓰기 0건 상태)"
    return 1
  fi

  local iout="$TMP/install.$UNIT_N.out" irc=0
  _log "  $name/$pid: pack_install --upgrade 위임 (payload=$cand_tag)"
  DOGANY_STRICT_GATES=1 bash "$INSTALLER" "$slug" "$root" \
    --pack "$pid" --pack-dir "$pdir" ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
    --upgrade --no-start --no-state \
    < /dev/null > "$iout" 2>&1 || irc=$?
  sed 's/^/[apply]     /' "$iout"

  if [[ "$irc" -ne 0 ]]; then
    _log "  $name/$pid: 미완주 -- pack_install rc=$irc. 브리지 재시작하지 않음, 정지 유지 (반쪽 상태 라이브 복귀 금지)."
    _log "  $name/$pid: 수동 복구 후 재기동: $MINT_RUN start --root $root"
    return 2
  fi

  # Belt 2: a rc-0 run without the install-side gate PASS line means the
  # fail-open WARN+skip path fired mid-run -- laundering refused.
  if ! grep -q "compat-lint: install-side gate PASS" "$iout"; then
    _log "  $name/$pid: FATAL -- pack_install rc=0인데 compat-lint install-side gate PASS 라인 부재 (fail-open 경로 의심). 성공으로 세지 않음, 정지 유지."
    return 2
  fi

  # Post-apply record verification: both strings printed, then compared.
  local rec
  rec="$(_installed_ver "$conf" "$pid")"
  _log "  $name/$pid: 착지 검증 -- DOGANY_PACKS 기록 '$rec' vs 후보 '$cand_ver'"
  if [[ "$rec" != "$cand_ver" ]]; then
    _log "  $name/$pid: FATAL -- 적용 후 기록($rec)이 후보($cand_ver)와 불일치. 성공으로 세지 않음, 정지 유지."
    return 2
  fi

  if _restart "$root"; then
    N_RESTART=$((N_RESTART + 1))
    _log "  $name/$pid: 완주 -- $inst_now -> $cand_ver, 브리지 재시작 완료"
  else
    N_RESTART_FAIL=$((N_RESTART_FAIL + 1))
    OVERALL_RC=2
    _log "  $name/$pid: 적용은 완주했으나 재시작 실패 -- 수동 재기동 필요: $MINT_RUN start --root $root"
  fi
  return 0
}

# _crew_incomplete <kit> <pid> <member> <reason> <backup> <members>
# INCOMPLETE crew transaction surface: every crew bridge stays down, the T0
# backup path and manual recovery steps are printed (spec §3 rollback story
# item 2 -- no automatic restore, first cut is a manual procedure).
_crew_incomplete() {
  local kit="$1" pid="$2" mname="$3" reason="$4" bak="$5" members="$6" m
  _log "  크루 $kit/$pid: 미완주 -- $mname: $reason"
  _log "  크루 $kit/$pid: 크루 전 멤버 브리지 재시작하지 않음, 정지 유지 (반쪽 상태 라이브 복귀 금지 -- 원자 단위 = 크루 전체)"
  _log "  크루 $kit/$pid: 복구 절차 (1) 공유 DB 복원이 필요하면 T0 백업 사용: $bak"
  _log "  크루 $kit/$pid: 복구 절차 (2) 원인 해소 후 재실행 (다운그레이드 없음 -- forward-only), 복구 완료 후 멤버별 재기동:"
  for m in $members; do
    _log "    $MINT_RUN start --root ${m#*:}"
  done
}

# _apply_crew <pid> <kind> <provides> <cand_tag> <cand_ver> <plan_members>
#             [fw_units]
# The WHOLE crew is ONE atomic unit (spec §3 T0~T-end). See the header
# CREW TRANSACTION block for the full contract.
# fw_units (slice 4, ATOMIC GROUP generalization): space-separated
# "name;root;from;to" framework items applied INSIDE this transaction --
# same stop/backup/restart envelope, packs FIRST then framework (see the
# header ATOMIC GROUP block for why fw-first is structurally blocked by the
# shipped fw_reqframework_guard). Callers MUST have run _fw_precheck on
# every fw unit already (read-only, pre-entry). Empty = plain crew unit.
# returns: 0 OK / 1 BLOCKED (pre-entry, zero instance writes) / 2 INCOMPLETE
# (transaction entered and did not complete -- EVERY group bridge stays down).
# Cross-source discovery mismatches / member-set drift / member asymmetry
# ABORT the whole run (estate state itself is suspect, spec §3 fail-closed).
_apply_crew() {
  local pid="$1" pkind="$2" provides="$3" cand_tag="$4" cand_ver="$5" plan_members="$6"
  local fw_units="${7:-}"
  local crew_kit="${provides:-$pid}"
  local crew_home="$SHARED_HOME/crews/$crew_kit"
  local crew_conf="$CREW_DIR/$crew_kit/crew.conf"
  local crew_db="$crew_home/$crew_kit.db"
  local m mname mroot cl link m_packs

  _log "  크루 $crew_kit/$pid: T0 진입 전 전수 검사 -- 원자 단위 = 크루 전체 (개별 멤버 아님)"

  if [[ -z "${plan_members// /}" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 계획 항목에 멤버 0건 (0건은 통과가 아니라 의심), fail-closed"
    return 1
  fi
  if [[ ! -f "$crew_conf" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- crew.conf 부재 ($crew_conf). 크루 스코프 항목인데 크루 레지스트리가 없음 (시드: update_seed.sh), fail-closed"
    return 1
  fi

  # ---- T0-1: apply-time crew re-discovery + 3-source cross-validation ----
  # The plan snapshot is NEVER trusted here (same doctrine as the fresh
  # version re-read on the instance path). Sources: (1) crew.conf line,
  # (2) member DOGANY_PACKS has <pack>@, (3) readlink symlink ground truth.
  local conf_members="" member_n=0
  while IFS= read -r cl; do
    case "$cl" in
      member=*) : ;;
      ''|'#'*) continue ;;
      *) _abort "크루 $crew_kit: crew.conf 라인 파싱 실패: '$cl' -- fail-closed" ;;
    esac
    mroot="${cl#member=}"
    mname="$(basename "$mroot")"
    for m in $plan_members; do
      if [[ "${m#*:}" == "$mroot" ]]; then mname="${m%%:*}"; fi
    done
    member_n=$((member_n + 1))
    conf_members="${conf_members:+$conf_members }$mname:$mroot"
    _log "  크루 $crew_kit: 교차검증 $mname source1(crew.conf) PASS"
    m_packs="$(_conf_get "$mroot/.instance.conf" DOGANY_PACKS)"
    case ",$m_packs," in
      *",$pid@"*) _log "  크루 $crew_kit: 교차검증 $mname source2(DOGANY_PACKS) PASS" ;;
      *) _log "  크루 $crew_kit: 교차검증 $mname source2(DOGANY_PACKS) FAIL"
         _abort "크루 $crew_kit: $mname DOGANY_PACKS에 $pid@ 엔트리 없음 -- 3소스 불일치, ABORT (fail-closed, 쓰기 0건)" ;;
    esac
    link="$(readlink "$mroot/database/$crew_kit.db" 2>/dev/null || echo "")"
    if [[ "$link" == "$crew_db" ]]; then
      _log "  크루 $crew_kit: 교차검증 $mname source3(symlink) PASS ($link)"
    else
      _log "  크루 $crew_kit: 교차검증 $mname source3(symlink) FAIL (readlink='$link' != '$crew_db')"
      _abort "크루 $crew_kit: $mname symlink ground truth 불일치 -- 3소스 불일치, ABORT (fail-closed, 쓰기 0건)"
    fi
  done < "$crew_conf"
  _log "  크루 $crew_kit 멤버 ${member_n}건${conf_members:+ ($conf_members)}"
  if [[ "$member_n" -eq 0 ]]; then
    _abort "크루 $crew_kit: 멤버 0건 = ABORT (0건은 통과가 아니라 의심, DGN-1030 §주의)"
  fi

  # plan-vs-conf member-set drift, both directions (plan-apply window).
  local pm cm found
  for pm in $plan_members; do
    found=0
    for cm in $conf_members; do
      if [[ "${cm#*:}" == "${pm#*:}" ]]; then found=1; fi
    done
    if [[ "$found" -ne 1 ]]; then
      _abort "크루 $crew_kit: 계획 멤버 ${pm%%:*}(${pm#*:})가 적용 시점 crew.conf에 없음 -- 계획-적용 창 사이 크루 구성 변동, ABORT (쓰기 0건)"
    fi
  done
  for cm in $conf_members; do
    found=0
    for pm in $plan_members; do
      if [[ "${pm#*:}" == "${cm#*:}" ]]; then found=1; fi
    done
    if [[ "$found" -ne 1 ]]; then
      _abort "크루 $crew_kit: 적용 시점 crew.conf 멤버 ${cm%%:*}(${cm#*:})가 계획에 없음 -- 계획-적용 창 사이 크루 구성 변동, ABORT (쓰기 0건)"
    fi
  done

  # ---- ATOMIC GROUP extension (slice 4): fw units share this transaction's
  # stop/backup/restart envelope. fw roots NOT among the crew members get
  # their own stop/restart entries; overlapping roots are handled once.
  local fw_n=0 fwonly="" fu funame furoot fu_rest
  local group_members="$conf_members"
  if [[ -n "$fw_units" ]]; then
    for fu in $fw_units; do
      fw_n=$((fw_n + 1))
      funame="${fu%%;*}"; fu_rest="${fu#*;}"; furoot="${fu_rest%%;*}"
      found=0
      for cm in $conf_members; do
        if [[ "${cm#*:}" == "$furoot" ]]; then found=1; fi
      done
      if [[ "$found" -ne 1 ]]; then
        fwonly="${fwonly:+$fwonly }$funame:$furoot"
        group_members="${group_members:+$group_members }$funame:$furoot"
      fi
    done
    _log "  크루 $crew_kit/$pid: ATOMIC GROUP -- framework ${fw_n}건이 같은 트랜잭션에 동승 (그룹 전체 = 최소 적용 단위, 내부 순서 = 팩 -> 프레임워크)"
  fi

  # ---- T0-2: fresh per-member installed versions (symmetric or ABORT) ----
  local inst_now="" v
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    v="$(_installed_ver "$mroot/.instance.conf" "$pid")"
    if [[ -z "$v" ]]; then
      _abort "크루 $crew_kit: $mname 적용 시점 재판독에서 DOGANY_PACKS $pid@ 엔트리 없음 -- 3소스 불일치, ABORT"
    fi
    if [[ -z "$inst_now" ]]; then
      inst_now="$v"
    elif [[ "$inst_now" != "$v" ]]; then
      _abort "크루 $crew_kit: 멤버 간 설치 버전 불일치 ($inst_now vs $mname=$v) -- 비대칭 크루는 반쪽 갱신 의심, ABORT (fail-closed, 쓰기 0건)"
    fi
  done
  # kit-core symmetry probe: every member's installed kit core must carry an
  # IDENTICAL version-pin line (raw string equality -- no version semantics
  # here, the comparator stays pack_install's). An asymmetric crew would
  # otherwise fail MID-transaction (INCOMPLETE, every bridge down); catching
  # it at T0 keeps the failure pre-entry with zero downtime.
  local pin_line ref_pin="" ref_set=0
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    if [[ -f "$mroot/database/$crew_kit.py" ]]; then
      pin_line="$(grep -E '^EXPECTED_USER_VERSION' "$mroot/database/$crew_kit.py" 2>/dev/null | head -n1)"
      pin_line="${pin_line:-<no-pin-line>}"
    else
      pin_line="<no-kit-core>"
    fi
    if [[ "$ref_set" -eq 0 ]]; then
      ref_pin="$pin_line"; ref_set=1
    elif [[ "$pin_line" != "$ref_pin" ]]; then
      _abort "크루 $crew_kit: 멤버 간 kit core 핀 라인 불일치 ('$ref_pin' vs $mname='$pin_line') -- 비대칭 크루는 반쪽 갱신 의심, ABORT (fail-closed, 쓰기 0건)"
    fi
  done
  _log "  크루 $crew_kit/$pid: kit core 핀 라인 대칭 확인 (멤버 ${member_n}건 동일: '${ref_pin}')"
  _log "  크루 $crew_kit/$pid: 적용 시점 재판독 -- 설치 $inst_now vs 후보 $cand_ver (두 문자열 실비교, 멤버 ${member_n}건 전원 동일)"
  local eq_rc=0 ge_rc=0
  _vercmp "$cand_ver" "==$inst_now" || eq_rc=$?
  if [[ "$eq_rc" -eq 2 ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$inst_now'), fail-closed"
    return 1
  fi
  if [[ "$eq_rc" -eq 0 ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED-STALE-PLAN -- 후보 == 설치($inst_now). 계획 이후 설치본이 변동됨 (조용한 성공으로 세지 않음)"
    return 1
  fi
  _vercmp "$cand_ver" ">=$inst_now" || ge_rc=$?
  if [[ "$ge_rc" -ne 0 ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED-DOWNGRADE -- 후보 $cand_ver < 설치 $inst_now. 다운그레이드는 절대 적용하지 않습니다 (fail-closed)"
    return 1
  fi

  # ---- T0-3: shared DB + tooling + restart-target presence ----
  if [[ ! -f "$crew_db" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 공유 DB 실체 부재 ($crew_db): symlink는 있으나 대상 파일이 없음, fail-closed"
    return 1
  fi
  if ! command -v sqlite3 >/dev/null 2>&1; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- sqlite3 부재: T0 공유 DB 백업 불가, fail-closed"
    return 1
  fi
  local plc total_plc=0
  for m in $conf_members; do
    plc="$(_plist_count "${m#*:}")"
    if [[ "$plc" -eq 0 ]]; then
      _log "  크루 $crew_kit/$pid: BLOCKED -- ${m%%:*} 재시작 대상 plist 0건 (${m#*:}) -- 크루 항목에서 0건은 통과가 아니라 의심 (fail-closed)"
      return 1
    fi
    total_plc=$((total_plc + plc))
  done
  _log "  크루 $crew_kit/$pid: 재시작 대상 plist 총 ${total_plc}건 (멤버 ${member_n}건, 0건 아님)"

  # Belt 1 re-assert immediately before installer involvement (spec §4).
  if [[ ! -f "$COMPAT_LINT" || ! -r "$COMPAT_LINT" ]]; then
    _log "  크루 $crew_kit/$pid: 호환성 검사기(compat-lint.sh)가 없어 이 항목을 중단합니다 -- fail-closed. 프레임워크 설치가 불완전한 상태입니다."
    return 1
  fi

  # ---- T0-4: ONE payload extraction + manifest asserts ----
  local repo pdir
  repo="$(_reg_repo "$pid")"
  if [[ -z "$repo" || ! -d "$repo" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 레지스트리 repo 미해석 ('$repo'), fail-closed"
    return 1
  fi
  pdir="$TMP/payload.crew.$UNIT_N"
  mkdir -p "$pdir"
  if ! git -C "$repo" rev-parse -q --verify "refs/tags/$cand_tag" >/dev/null 2>&1; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 후보 태그 $cand_tag 이 repo($repo)에 없음 (계획-적용 창 사이 변동 의심), fail-closed"
    return 1
  fi
  if ! git -C "$repo" archive --format=tar "refs/tags/$cand_tag" | tar -xf - -C "$pdir"; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 태그 $cand_tag payload 추출 실패, fail-closed"
    return 1
  fi
  _log "  크루 $crew_kit/$pid: payload 1회 추출 -> 전 멤버가 같은 추출본에서 설치 (멤버 간 버전 분화 원천 차단)"
  local mfline mf_id mf_kind mf_ver mf_prov
  mfline="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
def s(k):
    v = d.get(k)
    return v if isinstance(v, str) else ''
print('\t'.join([s('id'), s('kind'), s('pack_version'), s('provides_kit')]))
" "$pdir/pack-manifest.json" 2>/dev/null || true)"
  mf_id="$(printf '%s' "$mfline" | cut -f1)"
  mf_kind="$(printf '%s' "$mfline" | cut -f2)"
  mf_ver="$(printf '%s' "$mfline" | cut -f3)"
  mf_prov="$(printf '%s' "$mfline" | cut -f4)"
  if [[ -z "$mfline" || "$mf_id" != "$pid" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출 manifest id('$mf_id') != 레지스트리 id('$pid') -- 구독원 오염 의심, fail-closed"
    return 1
  fi
  if [[ "$mf_kind" != "kit" && -z "$mf_prov" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출본이 크루 스코프가 아님 (kind='$mf_kind' provides_kit='') -- 계획과 불일치, fail-closed"
    return 1
  fi
  # Installer KIT_NAME contract (pack_install.sh lockstep, DGN-1143):
  # provides_kit is MANDATORY for a crew-scope (kit) extraction -- the legacy
  # 'lifekit' default is removed (zero published manifests relied on it, and
  # after the dec-145 kit rename it would silently equal a stale crew key).
  # The effective kit name MUST equal the crew key -- KIT_NAME drives every
  # write path incl. crews/<kit>/.
  if [[ -z "$mf_prov" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출 manifest 에 provides_kit 부재 (legacy 'lifekit' 폴백은 DGN-1143 으로 제거됨; 개명 후 폴백은 존재하지 않는 킷을 가리킨다) -- fail-closed"
    return 1
  fi
  local eff_kit="$mf_prov"
  if [[ "$eff_kit" != "$crew_kit" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출본 kit 이름('$eff_kit') != 크루 키('$crew_kit') -- 설치 경로 불일치 위험, fail-closed"
    return 1
  fi
  if [[ -n "$mf_ver" && "$mf_ver" != "$cand_ver" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출 manifest pack_version($mf_ver) != 태그 버전($cand_ver), 3점 불일치 fail-closed"
    return 1
  fi

  # ---- T0-5: per-member slug + pack_install --dry-run (all must pass) ----
  local slug drc dout un=0
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    slug="$(_conf_get "$mroot/.instance.conf" DOGANY_AGENT_NAME)"
    if [[ -z "$slug" ]]; then
      _log "  크루 $crew_kit/$pid: BLOCKED -- $mname DOGANY_AGENT_NAME 부재 ($mroot/.instance.conf), 설치 슬러그 해석 불가 (fail-closed)"
      return 1
    fi
    un=$((un + 1))
    drc=0; dout="$TMP/crew.dry.$UNIT_N.$un"
    DOGANY_STRICT_GATES=1 bash "$INSTALLER" "$slug" "$mroot" \
      --pack "$pid" --pack-dir "$pdir" ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
      --upgrade --no-start --no-state --dry-run \
      < /dev/null > "$dout" 2>&1 || drc=$?
    sed 's/^/[apply]     /' "$dout"
    if [[ "$drc" -ne 0 ]]; then
      _log "  크루 $crew_kit/$pid: BLOCKED -- T0 pack_install --dry-run FAIL ($mname rc=$drc) -- 진입 전 차단 (쓰기 0건, 브리지 무접촉)"
      return 1
    fi
    _log "  크루 $crew_kit/$pid: T0 dry-run PASS [$un/${member_n}] $mname"
  done

  # ---- T0-6: disk headroom for the shared-DB backup ----
  local db_kb avail_kb need_kb
  db_kb="$(du -k "$crew_db" 2>/dev/null | awk '{print $1}' | head -n1)"
  db_kb="${db_kb:-0}"
  avail_kb="$(df -Pk "$crew_home" 2>/dev/null | awk 'NR==2 {print $4}')"
  avail_kb="${avail_kb:-0}"
  need_kb=$((db_kb * 2 + 5120))
  _log "  크루 $crew_kit/$pid: 디스크 여유 검사 -- 공유 DB ${db_kb}KB, 필요 ${need_kb}KB, 가용 ${avail_kb}KB"
  if [[ "$avail_kb" -lt "$need_kb" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 디스크 여유 부족 (가용 ${avail_kb}KB < 필요 ${need_kb}KB), fail-closed"
    return 1
  fi

  if [[ "$DRY" -eq 1 ]]; then
    if [[ -n "$fw_units" ]]; then
      local g_n=0
      for m in $group_members; do g_n=$((g_n + 1)); done
      _log "  크루 $crew_kit/$pid: (dry-run) 계획 -- 그룹 전 브리지 정지(${g_n}건) -> 공유 DB 백업 1건($crew_db.t0.*) -> pack_install --upgrade x${member_n} (단일 추출본) -> 착지 검증 x${member_n} -> framework x${fw_n} (self-update.sh --no-restart, 계획 후보 핀 고정) -> 그룹 전 브리지 재시작(${g_n}건)"
      _log "  크루 $crew_kit/$pid: (dry-run) 그룹 내부 순서 = 팩 -> 프레임워크 (fw_reqframework_guard가 전진된 팩 집합을 보도록 -- fw 선적용은 게이트가 정당하게 차단)"
    else
      _log "  크루 $crew_kit/$pid: (dry-run) 계획 -- 전 멤버 정지(${member_n}건, plist ${total_plc}건) -> 공유 DB 백업 1건($crew_db.t0.*) -> pack_install --upgrade x${member_n} (단일 추출본) -> 착지 검증 x${member_n} -> 전 멤버 재시작(${member_n}건)"
    fi
    _log "  크루 $crew_kit/$pid: (dry-run) 공유 DB 마이그레이션은 1개 DB에 1회 -- 첫 멤버가 user_version을 핀으로 전진, 이후 멤버는 cur==pin 무이동 통과"
    _log "  크루 $crew_kit/$pid: (dry-run) 적용 예정 -- 쓰기/정지/백업/재시작 실행 안 함"
    return 0
  fi

  # ---- T0-7: stop ALL member bridges (verified). Failure = pre-entry ABORT
  # with zero writes; already-stopped members stay down (conservative --
  # same direction as the slice-2 preflight policy; availability relaxation
  # is a separate decision).
  local stopped="" sm stop_n=0
  for m in $group_members; do stop_n=$((stop_n + 1)); done
  if [[ -n "$fw_units" ]]; then
    _log "  크루 $crew_kit/$pid: 그룹 전 브리지 정지 (${stop_n}건 -- 크루 ${member_n} + fw 단독 $((stop_n - member_n))) -- 트랜잭션 진입"
  else
    _log "  크루 $crew_kit/$pid: 크루 전 멤버 브리지 정지 (${member_n}건) -- 트랜잭션 진입"
  fi
  for m in $group_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    if ! _launchd_stop "$mroot"; then
      _log "  크루 $crew_kit/$pid: BLOCKED -- 멤버 $mname 정지 검증 실패. 크루 트랜잭션 진입 전 중단 (쓰기 0건 상태 -- 백업 포함 어떤 파일도 만들지 않음)"
      if [[ -n "$stopped" ]]; then
        _log "  크루 $crew_kit/$pid: 이미 정지된 멤버는 보수적으로 정지 유지 (쓰기 0건이나 자동 재시작은 별도 판단 레인). 수동 재기동:"
        for sm in $stopped; do
          _log "    $MINT_RUN start --root ${sm#*:}"
        done
      fi
      return 1
    fi
    stopped="${stopped:+$stopped }$m"
  done

  # ---- T0-8: loop-level shared-DB backup (quiesced, fixed naming) ----
  local cur_uv ts bak
  cur_uv="$(sqlite3 "$crew_db" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
  ts="$(date +%Y%m%d-%H%M%S)"
  bak="$crew_db.t0.v${cur_uv}.bak-$ts"
  if ! sqlite3 "$crew_db" ".backup '$bak'"; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- T0 공유 DB 백업 실패 ($bak). 인스턴스 쓰기 0건, 그룹 브리지는 보수적으로 정지 유지. 수동 재기동:"
    for sm in $group_members; do
      _log "    $MINT_RUN start --root ${sm#*:}"
    done
    return 1
  fi
  _log "  크루 $crew_kit/$pid: T0 공유 DB 백업 1건 -> $bak (user_version=$cur_uv)"

  # ---- T1..TN: install every member from the single extraction ----
  local iout irc rec
  un=0
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    slug="$(_conf_get "$mroot/.instance.conf" DOGANY_AGENT_NAME)"
    un=$((un + 1))
    iout="$TMP/crew.install.$UNIT_N.$un"; irc=0
    _log "  크루 $crew_kit/$pid: [T$un/${member_n}] $mname pack_install --upgrade 위임 (payload=$cand_tag, 단일 추출본)"
    DOGANY_STRICT_GATES=1 bash "$INSTALLER" "$slug" "$mroot" \
      --pack "$pid" --pack-dir "$pdir" ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
      --upgrade --no-start --no-state \
      < /dev/null > "$iout" 2>&1 || irc=$?
    sed 's/^/[apply]     /' "$iout"
    if [[ "$irc" -ne 0 ]]; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "pack_install rc=$irc" "$bak" "$group_members"
      return 2
    fi
    # Belt 2 (kit path wording): rc-0 without the kit gate PASS line means
    # the fail-open WARN+skip path fired mid-run -- laundering refused.
    if ! grep -q "kit: compat-lint PASS" "$iout"; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "pack_install rc=0인데 'kit: compat-lint PASS' 라인 부재 (fail-open 경로 의심, 세탁 거부)" "$bak" "$group_members"
      return 2
    fi
    rec="$(_installed_ver "$mroot/.instance.conf" "$pid")"
    _log "  크루 $crew_kit/$pid: $mname 착지 검증 -- DOGANY_PACKS 기록 '$rec' vs 후보 '$cand_ver'"
    if [[ "$rec" != "$cand_ver" ]]; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "적용 후 기록('$rec')이 후보('$cand_ver')와 불일치" "$bak" "$group_members"
      return 2
    fi
  done

  local end_uv
  end_uv="$(sqlite3 "$crew_db" 'PRAGMA user_version;' 2>/dev/null || echo '?')"
  _log "  크루 $crew_kit/$pid: 공유 DB user_version $cur_uv -> $end_uv (마이그레이션은 DB 1개에 1회 -- 첫 멤버만 전진, 이후 멤버 cur==pin 무이동 통과)"

  # ---- ATOMIC GROUP fw phase (slice 4): AFTER the pack landings so the
  # shipped fw_reqframework_guard inside update.sh evaluates the ADVANCED
  # pack set (fw-first would be blocked by the still-old installed range --
  # the very incompatibility that made this group atomic). Bridges are all
  # down; there is no live prefix. Any failure = the WHOLE group INCOMPLETE.
  if [[ -n "$fw_units" ]]; then
    local futo su frc
    for fu in $fw_units; do
      funame="${fu%%;*}"; fu_rest="${fu#*;}"; furoot="${fu_rest%%;*}"
      fu_rest="${fu_rest#*;}"; futo="${fu_rest#*;}"
      su="${SELFUPD_OVERRIDE:-$furoot/routines/self-update.sh}"
      frc=0
      _fw_payload "$funame" "$furoot" "$futo" "$su" || frc=$?
      if [[ "$frc" -ne 0 ]]; then
        _crew_incomplete "$crew_kit" "$pid" "framework $funame" "self-update 실패 또는 착지 검증 실패 (위 로그 참조)" "$bak" "$group_members"
        return 2
      fi
    done
  fi

  # ---- T-end: restart the WHOLE group (crew members + fw-only roots) ----
  local r_ok_n=0
  for m in $group_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    if _restart "$mroot"; then
      N_RESTART=$((N_RESTART + 1))
      r_ok_n=$((r_ok_n + 1))
    else
      N_RESTART_FAIL=$((N_RESTART_FAIL + 1))
      OVERALL_RC=2
      _log "  크루 $crew_kit/$pid: $mname 재시작 실패 -- 수동 재기동 필요: $MINT_RUN start --root $mroot"
    fi
  done
  if [[ -n "$fw_units" ]]; then
    _log "  크루 $crew_kit/$pid: 완주(ATOMIC GROUP) -- 팩 $inst_now -> $cand_ver + framework ${fw_n}건, 그룹 재시작 성공 ${r_ok_n}건"
  else
    _log "  크루 $crew_kit/$pid: 완주 -- $inst_now -> $cand_ver (멤버 ${member_n}건, 재시작 성공 ${r_ok_n}건)"
  fi
  return 0
}

# _apply_atomic <desc> <fw_items "name;root;from;to ..."> <pack_line_file>
# ONE atomic unit = the whole group (spec §2: the slice-3 T0~T-end procedure
# generalized to the atomic-group boundary -- no new transaction machinery).
# returns: 0 OK / 1 BLOCKED (pre-entry, zero writes) / 2 INCOMPLETE (every
# group bridge stays down).
_apply_atomic() {
  local desc="$1" fw_items="$2" pack_line="$3"
  local a_pid a_kind a_prov a_tag a_cver a_members fu funame furoot fu_rest futo

  a_pid="$(printf '%s' "$pack_line" | cut -f2)"
  a_kind="$(printf '%s' "$pack_line" | cut -f3)"
  a_prov="$(printf '%s' "$pack_line" | cut -f4)"
  a_tag="$(printf '%s' "$pack_line" | cut -f6)"
  a_cver="$(printf '%s' "$pack_line" | cut -f7)"
  a_members="$(printf '%s' "$pack_line" | cut -f8)"

  _log "  ATOMIC GROUP [$desc]: 원자 단위 진입 판정 -- 그룹 전체 = 최소 적용 단위"
  # Supported composition today: crew-scoped pack + fw items. Anything else
  # is structurally unproducible by the planner -- fail closed, never guess.
  if [[ "$a_kind" != "kit" && -z "$a_prov" ]]; then
    _abort "ATOMIC GROUP [$desc]: 크루 스코프 아닌 팩($a_pid, kind='$a_kind')이 원자 그룹에 포함 -- 지원 구성 밖 (단일 크루 팩 + framework만 지원), fail-closed"
  fi

  # Pre-entry fw checks (read-only; a BLOCKED fw member blocks the WHOLE
  # group before any stop -- the group is the unit, spec §2).
  for fu in $fw_items; do
    funame="${fu%%;*}"; fu_rest="${fu#*;}"; furoot="${fu_rest%%;*}"
    fu_rest="${fu_rest#*;}"; futo="${fu_rest#*;}"
    if ! _fw_precheck "$funame" "$furoot" "$futo"; then
      _log "  ATOMIC GROUP [$desc]: BLOCKED -- framework $funame 사전검사 실패 (그룹 전체 차단, 부분 적용 없음)"
      return 1
    fi
  done

  _apply_crew "$a_pid" "$a_kind" "$a_prov" "$a_tag" "$a_cver" "$a_members" "$fw_items"
}

# NOTE: parsing uses cut (not IFS=tab read) -- tab is an IFS whitespace
# class character in bash, so consecutive tabs around an EMPTY field (e.g.
# provides_kit='') would collapse and shift every later column.
IN_ATOMIC=0
A_DESC=""
A_FW_ITEMS=""
A_PACK_LINE=""
A_PACK_N=0
A_N=0
while IFS= read -r line; do
  kind="$(printf '%s' "$line" | cut -f1)"
  f1="$(printf '%s' "$line" | cut -f2)"
  f2="$(printf '%s' "$line" | cut -f3)"
  f3="$(printf '%s' "$line" | cut -f4)"
  f4="$(printf '%s' "$line" | cut -f5)"
  f5="$(printf '%s' "$line" | cut -f6)"
  f6="$(printf '%s' "$line" | cut -f7)"
  f7="$(printf '%s' "$line" | cut -f8)"

  # An open ATOMIC group accepts ONLY its member lines / terminator -- any
  # other kind mid-group means a truncated or interleaved emit: fail closed.
  if [[ "$IN_ATOMIC" -eq 1 ]]; then
    case "$kind" in
      ATOMIC-FW|ATOMIC-PACK|ATOMIC-END) : ;;
      *) _abort "plan emit: ATOMIC 그룹이 ATOMIC-END 없이 '$kind' 라인으로 이어짐 -- 불완전/구버전 emit, fail-closed" ;;
    esac
  fi

  case "$kind" in
    PLAN-EMIT-V1) continue ;;
    FW)
      # Standalone framework atomic unit (slice 4): self-update delegation,
      # restart deferred to loop end (spec §2 rule 1).
      fw_name="$f1"; fw_root="$f2"; fw_from="$f3"; fw_to="$f4"
      N_TARGET=$((N_TARGET + 1))
      UNIT_N=$((UNIT_N + 1))
      if [[ "$FAILFAST" -eq 1 ]]; then
        _log "  framework $fw_name: 미시도 -- 선행 원자 단위 미완주로 이후 적용 중단 (정지는 해당 단위에만 유지, 완주분 재시작은 유효)"
        N_UNTRIED=$((N_UNTRIED + 1))
        continue
      fi
      frc=0
      _apply_fw "$fw_name" "$fw_root" "$fw_from" "$fw_to" || frc=$?
      case "$frc" in
        0) N_OK=$((N_OK + 1)) ;;
        1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2 ;;
        2) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; FAILFAST=1
           _mark_down "$fw_root" ;;
      esac
      continue ;;
    ATOMIC)
      IN_ATOMIC=1; A_DESC="$f1"; A_FW_ITEMS=""; A_PACK_LINE=""; A_PACK_N=0; A_N=0
      continue ;;
    ATOMIC-FW)
      [[ "$IN_ATOMIC" -eq 1 ]] || _abort "plan emit: 그룹 밖 ATOMIC-FW 라인 -- 손상된 emit, fail-closed"
      A_FW_ITEMS="${A_FW_ITEMS:+$A_FW_ITEMS }$f1;$f2;$f3;$f4"
      A_N=$((A_N + 1))
      continue ;;
    ATOMIC-PACK)
      [[ "$IN_ATOMIC" -eq 1 ]] || _abort "plan emit: 그룹 밖 ATOMIC-PACK 라인 -- 손상된 emit, fail-closed"
      A_PACK_LINE="$line"
      A_PACK_N=$((A_PACK_N + 1))
      A_N=$((A_N + 1))
      continue ;;
    ATOMIC-END)
      [[ "$IN_ATOMIC" -eq 1 ]] || _abort "plan emit: 그룹 밖 ATOMIC-END 라인 -- 손상된 emit, fail-closed"
      IN_ATOMIC=0
      if [[ "$f1" != "$A_N" ]]; then
        _abort "plan emit: ATOMIC-END 멤버 수($f1) != 수신 라인 수($A_N) -- 그룹이 온전하게 도착하지 않음, fail-closed"
      fi
      if [[ "$A_PACK_N" -ne 1 || -z "$A_FW_ITEMS" ]]; then
        _abort "ATOMIC GROUP [$A_DESC]: 팩 ${A_PACK_N}건 + framework $([[ -n "$A_FW_ITEMS" ]] && echo 有 || echo 0건) -- 지원 구성 밖 (framework >=1 + 크루 팩 정확히 1), fail-closed"
      fi
      N_TARGET=$((N_TARGET + 1))
      UNIT_N=$((UNIT_N + 1))
      a_members="$(printf '%s' "$A_PACK_LINE" | cut -f8)"
      if [[ "$FAILFAST" -eq 1 ]]; then
        _log "  ATOMIC GROUP [$A_DESC]: 미시도 -- 선행 원자 단위 미완주로 이후 적용 중단"
        N_UNTRIED=$((N_UNTRIED + 1))
        continue
      fi
      arc=0
      _apply_atomic "$A_DESC" "$A_FW_ITEMS" "$A_PACK_LINE" || arc=$?
      case "$arc" in
        0) N_OK=$((N_OK + 1)) ;;
        1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2 ;;
        2) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; FAILFAST=1
           for m in $a_members; do _mark_down "${m#*:}"; done
           for fu in $A_FW_ITEMS; do
             _fu_rest="${fu#*;}"
             _mark_down "${_fu_rest%%;*}"
           done ;;
      esac
      continue ;;
    PACK) : ;;
    *) _abort "plan emit 미지원 항목 '$kind' -- fail-closed" ;;
  esac

  pid="$f1"; p_kind="$f2"; p_provides="$f3"; p_inst="$f4"
  p_tag="$f5"; p_cver="$f6"; p_members="$f7"

  if [[ "$p_kind" == "kit" || -n "$p_provides" ]]; then
    # Crew-scoped item (slice 3): the WHOLE crew is ONE atomic unit.
    N_TARGET=$((N_TARGET + 1))
    UNIT_N=$((UNIT_N + 1))
    if [[ "$FAILFAST" -eq 1 ]]; then
      _log "  크루 ${p_provides:-$pid}/$pid: 미시도 -- 선행 원자 단위 미완주로 이후 적용 중단 (정지는 해당 단위에만 유지, 완주분 재시작은 유효)"
      N_UNTRIED=$((N_UNTRIED + 1))
      continue
    fi
    crc=0
    _apply_crew "$pid" "$p_kind" "$p_provides" "$p_tag" "$p_cver" "$p_members" || crc=$?
    case "$crc" in
      0) N_OK=$((N_OK + 1)) ;;
      1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2 ;;
      2) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; FAILFAST=1
         for m in $p_members; do _mark_down "${m#*:}"; done ;;
    esac
    continue
  fi

  # Empty member set on an emitted PACK item: structurally impossible from
  # the planner (an OK item implies >=1 mounting instance) -- if it appears,
  # it must FAIL loudly, never contribute a silent zero (self-grill b).
  if [[ -z "${p_members// /}" ]]; then
    _log "  pack $pid: BLOCKED -- 계획 항목에 멤버 0건 (0건은 통과가 아니라 의심), fail-closed"
    N_TARGET=$((N_TARGET + 1)); N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
    continue
  fi

  # Registry repo + tag existence (extraction source).
  repo="$(_reg_repo "$pid")"
  if [[ -z "$repo" || ! -d "$repo" ]]; then
    _log "  pack $pid: BLOCKED -- 레지스트리 repo 미해석 ('$repo'), fail-closed"
    N_TARGET=$((N_TARGET + 1)); N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
    continue
  fi

  for m in $p_members; do
    mname="${m%%:*}"
    mroot="${m#*:}"
    N_TARGET=$((N_TARGET + 1))
    UNIT_N=$((UNIT_N + 1))

    if [[ "$FAILFAST" -eq 1 ]]; then
      _log "  $mname/$pid: 미시도 -- 선행 원자 단위 미완주로 이후 적용 중단 (정지는 해당 단위에만 유지, 완주분 재시작은 유효)"
      N_UNTRIED=$((N_UNTRIED + 1))
      continue
    fi

    # Fresh extraction per unit (immutable tag archive -> private tmp).
    pdir="$TMP/payload.$UNIT_N"
    mkdir -p "$pdir"
    if ! git -C "$repo" rev-parse -q --verify "refs/tags/$p_tag" >/dev/null 2>&1; then
      _log "  $mname/$pid: BLOCKED -- 후보 태그 $p_tag 이 repo($repo)에 없음 (계획-적용 창 사이 변동 의심), fail-closed"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
      continue
    fi
    if ! git -C "$repo" archive --format=tar "refs/tags/$p_tag" | tar -xf - -C "$pdir"; then
      _log "  $mname/$pid: BLOCKED -- 태그 $p_tag payload 추출 실패, fail-closed"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
      continue
    fi
    # Extracted-manifest asserts (id poisoning + crew-scope defense in depth).
    mfline="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
def s(k):
    v = d.get(k)
    return v if isinstance(v, str) else ''
print('\t'.join([s('id'), s('kind'), s('pack_version'), s('provides_kit')]))
" "$pdir/pack-manifest.json" 2>/dev/null || true)"
    mf_id="$(printf '%s' "$mfline" | cut -f1)"
    mf_kind="$(printf '%s' "$mfline" | cut -f2)"
    mf_ver="$(printf '%s' "$mfline" | cut -f3)"
    mf_prov="$(printf '%s' "$mfline" | cut -f4)"
    if [[ -z "$mfline" || "$mf_id" != "$pid" ]]; then
      _log "  $mname/$pid: BLOCKED -- 추출 manifest id('$mf_id') != 레지스트리 id('$pid') -- 구독원 오염 의심, fail-closed"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
      continue
    fi
    if [[ "$mf_kind" == "kit" || -n "$mf_prov" ]]; then
      _log "  $mname/$pid: BLOCKED -- 계획은 인스턴스 스코프인데 추출본이 크루 스코프 (kind='$mf_kind' provides_kit='$mf_prov') -- 계획-추출 불일치, 크루 트랜잭션 없이 적용하지 않음 (fail-closed)"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
      continue
    fi
    if [[ -n "$mf_ver" && "$mf_ver" != "$p_cver" ]]; then
      _log "  $mname/$pid: BLOCKED -- 추출 manifest pack_version($mf_ver) != 태그 버전($p_cver), 3점 불일치 fail-closed"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
      continue
    fi

    urc=0
    _apply_unit "$pid" "$mroot" "$mname" "$p_tag" "$p_cver" "$pdir" || urc=$?
    case "$urc" in
      0) N_OK=$((N_OK + 1)) ;;
      1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2 ;;
      2) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; FAILFAST=1
         _mark_down "$mroot" ;;
    esac
  done
done < <(tail -n +1 "$PLAN_TSV")

if [[ "$IN_ATOMIC" -eq 1 ]]; then
  _abort "plan emit: ATOMIC 그룹이 ATOMIC-END 없이 파일이 끝남 -- 불완전 emit, 부분 그룹으로는 절대 적용하지 않습니다 (fail-closed)"
fi

# ---------------------------------------------------------------------------
# 3) Loop-end: deferred framework restarts (spec §2 rule 1 -- fw restarts
#    join the atomic-unit policy at the loop boundary; completed units only).
# ---------------------------------------------------------------------------
_flush_deferred

# ---------------------------------------------------------------------------
# 4) Completion notice -- exactly ONE push per run (dec-121 direction).
#    COPY NOT LOCKED: every user-facing line rides under an explicit
#    미확정(형님 확인 대기) marker until the owner approves final wording
#    (dec-094 UX gate). An INCOMPLETE unit MUST surface here -- a run with
#    any incomplete unit can never read as a clean success.
# ---------------------------------------------------------------------------
_notify() {
  if [[ "$N_TARGET" -eq 0 ]]; then
    _log "통지 생략 -- 적용 대상 0건 (통지할 착지/중단 없음. 0건 사유는 상단에 명시됨)"
    return 0
  fi
  local head body
  if [[ "$N_INCOMPLETE" -gt 0 ]]; then
    head="[미확정(형님 확인 대기)] 업데이트가 완료되지 않았습니다 -- 미완주 ${N_INCOMPLETE}건, 해당 브리지 정지 유지"
  else
    head="[미확정(형님 확인 대기)] 업데이트 완료"
  fi
  body="$head
완주 ${N_OK}건 / 차단 ${N_BLOCK}건 / 미완주(정지 유지) ${N_INCOMPLETE}건 / 재시작 ${N_RESTART}건"
  # A restart failure must never hide behind a clean-success header
  # (self-grill c: a completed apply with a dead bridge is not a success).
  if [[ "$N_RESTART_FAIL" -gt 0 ]]; then
    body="$body
[미확정(형님 확인 대기)] 재시작 실패 ${N_RESTART_FAIL}건 -- 수동 재기동 필요"
  fi
  if [[ "$DRY" -eq 1 ]]; then
    _log "(dry-run) 완료 통지 1통 예정 -- 발송 안 함. 본문:"
    printf '%s\n' "$body" | sed 's/^/[apply]     /'
    # Sender preview: --dry-run must show WHO would send (read-only scan)
    # so a live rehearsal can verify the fixed sender resolves correctly.
    _resolve_notify_push
    if [[ -n "$NOTIFY_PUSH" ]]; then
      _log "(dry-run) 발신 예정자: $NOTIFY_PUSH (발신자 고정, 슬러그 '$NOTIFY_SLUG')"
    else
      _log "(dry-run) 발신 예정자 해소 실패 -- ${NOTIFY_PUSH_WHY}. 실발송 시엔 WARN + 본문 로그로 남습니다 (대리 발송 없음)"
    fi
    return 0
  fi
  _notify_send "완료" "$body"
  return 0
}
_notify

# ---------------------------------------------------------------------------
# 5) §6 accounting -- counts always printed; 0 targets get loud reasons.
# ---------------------------------------------------------------------------
_log ""
if [[ "$DRY" -eq 1 ]]; then
  _log "== 결과 (dry-run -- 실행 0건) =="
else
  _log "== 결과 =="
fi
_log "적용 대상 ${N_TARGET}건 / 적용 성공 ${N_OK}건 / 차단 ${N_BLOCK}건 / 미완주(정지 유지) ${N_INCOMPLETE}건 / 미시도 ${N_UNTRIED}건 / 범위외 스킵 ${N_SKIP}건 / 재시작 ${N_RESTART}건"
if [[ "$N_RESTART_FAIL" -gt 0 ]]; then
  _log "재시작 실패 ${N_RESTART_FAIL}건 -- 수동 재기동 필요 (위 로그 참조)"
fi
if [[ "$N_TARGET" -eq 0 ]]; then
  if [[ "$PLAN_ITEMS" -eq 0 ]]; then
    _log "적용 대상 0건 -- 계획 자체가 0건 (플래너 차단/보류 사유는 상단 출력에 명시됨. 0건은 통과가 아니라 의심)"
  else
    _log "적용 대상 0건 -- 계획 ${PLAN_ITEMS}건이 전부 범위외 스킵(${N_SKIP}건). 항목별 사유는 위에 명시됨 (조용한 통과 아님)"
  fi
fi
if [[ "$((N_OK + N_BLOCK + N_INCOMPLETE + N_UNTRIED))" -ne "$N_TARGET" ]]; then
  _abort "카운트 정합 실패: 성공+차단+미완주+미시도(${N_OK}+${N_BLOCK}+${N_INCOMPLETE}+${N_UNTRIED}) != 대상 ${N_TARGET} -- FATAL"
fi
if [[ "$N_INCOMPLETE" -gt 0 ]]; then
  _log "미완주 ${N_INCOMPLETE}건: 해당 인스턴스 브리지는 정지 유지 상태입니다. 반쪽 상태로 라이브 복귀하지 않습니다."
fi

# Normal completion reached: the EXIT trap must NOT fire the abort notice
# (a nonzero OVERALL_RC here was already surfaced by the completion notice).
RUN_COMPLETED=1
exit "$OVERALL_RC"

#!/bin/bash
# update_seed.sh -- DGN-1031 slice 1: one-shot ADDITIVE seed for the unified
# update loop's two Metal-owned registry files (spec DGN-1031-SPEC §1 seed
# procedure + §3 crew.conf seed procedure).
#
#   1. ~/.dogany/pack-sources.conf   -- pack-id -> subscription source
#        <id>.repo=<abs path>        (from pack_coords.sh pack_coord_for,
#                                     resolved relative to the catalog file;
#                                     when that sibling is absent and the row
#                                     has source_url: the pack-src clone,
#                                     DGN-1818)
#        <id>.channel=stable         (explicit default; operator-editable)
#   2. ~/.dogany/crews/<kit>/crew.conf -- crew member instance roots
#        member=<abs instance root>  (one line per member, listed order =
#                                     crew member order, spec §2 rule 4)
#        A root is seeded ONLY when BOTH §3 conditions hold:
#          (a) readlink <root>/database/<kit>.db == <crew-home>/<kit>/<kit>.db
#          (b) <root>/.instance.conf DOGANY_PACKS carries a "<kit>@" entry
#        DGN-2056: a kit with no crew DB lane (db_derived) has no link to
#        check -- (b) alone. Its declared crew (crew_declare.sh) is
#        converted to a kit crew once every declared member mounts it.
#
#   3. ~/.dogany/crews/crew.conf   -- crew-wide settings (DGN-1704)
#        TZ_OFFSET_HOURS=<int>       founded once from the first seeded
#                                    member's declaration, else host offset
#
# Contract (spec §1 seed steps 1-4):
#   - ADDITIVE + REVERSIBLE: never overwrites an existing key/line; existing
#     values are preserved with a loud notice. Removal = delete the file(s).
#   - IDEMPOTENT: re-running with the same inputs produces file diff 0
#     (a present line is left untouched -- not rewritten).
#   - ATOMIC: every write goes through the same atomic-upsert family as
#     pack_install.sh _packs_upsert_atomic (flock on <conf>.lock serializing
#     the whole read-modify-write + same-dir tmp + fsync + os.replace).
#     No new locking scheme is invented.
#   - LOUD: every count is printed; a mount-set read failure aborts the seed
#     (fail-closed -- seeding from a corrupt conf is a suspect path, §6).
#
# Usage:
#   update_seed.sh [--dry-run]
#   update_seed.sh --target <instance-root> [--kit <id>] [--dry-run]
#   update_seed.sh --target <instance-root> --pack <id> [--dry-run]
#     --dry-run : print exactly what WOULD be written; write nothing.
#     --target  : DGN-1683 WP5 target-scoped registration. The instance set is
#                 exactly <instance-root> (no estate scan, and DOGANY_PLAN_
#                 INSTANCES is ignored -- that variable is test hermeticity,
#                 not a production scope switch). Same lease, same atomic
#                 upsert, same symlink+mount membership conditions as the
#                 estate seed; only THIS root's kits/packs are touched.
#     --kit     : (with --target) restrict to one kit id and make "not
#                 registered" a FAIL (rc 1) instead of a silent 0-member
#                 line. Prints "[seed] target-registered kit=<id> root=<r>
#                 result=added|present" on success.
#     --pack    : (with --target, not with --kit) DGN-1877 source-only
#                 registration of ONE mounted id: seeds <id>.repo/.channel in
#                 pack-sources.conf and nothing else -- no crew.conf, no crew
#                 timezone (a staff agent pack has no crew db; the kit's own crew
#                 registration is --kit's job). Refused while <id>'s crew
#                 lease is held (same rule as the crew.conf seed). Not
#                 mounted / no resolvable source = FAIL (rc 1). Prints
#                 "[seed] target-source pack=<id> root=<r>
#                 result=added|present|preserved" on success.
#
# Instance list source (DGN-1037): a directory carrying .instance.conf that
# the install registry ($HOME/.dogany/instances), the lite_instance pointer,
# or the fixed-depth fallback scan ($HOME/.dogany/main, $HOME/.dogany/agents/*,
# $HOME/dogany/*, $HOME/dogany/*/agents/*, $HOME/dogany/*/poc/*) names is an
# instance; see _discover_instances in
# lib/discover_instances.sh (shared with update_plan.sh/update_apply.sh --
# DGN-1517 sourced this copy into the library too, closing the drift risk
# a duplicated function body carried).
# The former hardcoded estate table leaked owner PII (account name +
# private estate topology) into the public mirror and could never match
# another user's disk. A discovered root without a DOGANY_PACKS line is
# still judged (loud "mount 0건" path) -- never silently excluded.
#
# Env overrides (TEST HERMETICITY ONLY -- same doctrine as DOGANY_SHARED_HOME
# in pack_install.sh and DOGANY_CONF_LOCK_TIMEOUT):
#   DOGANY_SHARED_HOME    estate shared home        (default: $HOME/.dogany)
#   DOGANY_PACK_SOURCES   pack-sources.conf path    (default: $SHARED_HOME/pack-sources.conf)
#   DOGANY_CREW_DIR       crews home                (default: $SHARED_HOME/crews)
#   DOGANY_PACK_CATALOG   catalog.json path         (default: canonical repo's
#                         packs/catalog.json when the relevant instance names
#                         a different DOGANY_REPO_ROOT; otherwise this tree)
#   DOGANY_PLAN_INSTANCES "name:root,name:root,..." (default: $HOME estate scan,
#                         see _discover_instances -- DGN-1037)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# DGN-1424: resolve the python3 interpreter ONCE, PATH-independent (a bare
# `python3` call resolves whatever the CALLER's PATH hands back -- see the
# lib file header).
PY_RESOLVE_LIB="$SCRIPT_DIR/lib/py_resolve.sh"
[[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
  || { echo "ERROR: python resolver library missing ($PY_RESOLVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/py_resolve.sh
. "$PY_RESOLVE_LIB"
pack_resolve_py3 || exit 1

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
SOURCES_CONF="${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
CATALOG="${DOGANY_PACK_CATALOG:-}"

# DGN-1818: pack_source_dir (the public-install clone location, one rule).
COORDS_LIB="$SCRIPT_DIR/lib/pack_coords.sh"
[[ -f "$COORDS_LIB" && -r "$COORDS_LIB" ]] \
  || { echo "ERROR: pack coordinate library missing ($COORDS_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/pack_coords.sh
. "$COORDS_LIB"

# DGN-1509 M3: update_seed.sh is crew-lease participant 5 of 5 (spec §4).
# crew.conf is the registry T0-1 cross-validates and the plan-vs-conf drift
# check compares against; a seed run inside a live crew transaction's window
# would change that registry under the transaction's feet even though each
# individual write is additive/idempotent -- it is still an input the
# transaction already committed to at T0-1. Refused per-kit below (not the
# whole seed run -- a lease on one kit says nothing about another's).
CREW_LEASE_LIB="$SCRIPT_DIR/lib/crew_lease.sh"
[[ -f "$CREW_LEASE_LIB" && -r "$CREW_LEASE_LIB" ]] \
  || { echo "ERROR: 크루 lease 라이브러리 부재 ($CREW_LEASE_LIB) -- fail-closed (DGN-1509)" >&2; exit 1; }
# shellcheck source=lib/crew_lease.sh
. "$CREW_LEASE_LIB"

DRY_RUN=0
TARGET_ROOT=""
TARGET_KIT=""
TARGET_PACK=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --target)
      [[ $# -ge 2 && -n "$2" ]] || { echo "[seed] ERROR: --target needs an instance root" >&2; exit 1; }
      TARGET_ROOT="$2"; shift 2 ;;
    --kit)
      [[ $# -ge 2 && -n "$2" ]] || { echo "[seed] ERROR: --kit needs a kit id" >&2; exit 1; }
      TARGET_KIT="$2"; shift 2 ;;
    --pack)
      [[ $# -ge 2 && -n "$2" ]] || { echo "[seed] ERROR: --pack needs a pack id" >&2; exit 1; }
      TARGET_PACK="$2"; shift 2 ;;
    *) echo "[seed] ERROR: unknown option: $1" >&2; exit 1 ;;
  esac
done
if [[ -n "$TARGET_KIT" && -z "$TARGET_ROOT" ]]; then
  echo "[seed] ERROR: --kit is only valid with --target" >&2; exit 1
fi
if [[ -n "$TARGET_PACK" && ( -z "$TARGET_ROOT" || -n "$TARGET_KIT" ) ]]; then
  echo "[seed] ERROR: --pack is only valid with --target and without --kit" >&2; exit 1
fi
if [[ -n "$TARGET_ROOT" ]]; then
  [[ "$TARGET_ROOT" = /* && -d "$TARGET_ROOT" ]] \
    || { echo "[seed] ERROR: --target must be an existing absolute directory: $TARGET_ROOT" >&2; exit 1; }
  TARGET_ROOT="$(cd "$TARGET_ROOT" && pwd)"
  [[ -f "$TARGET_ROOT/.instance.conf" ]] \
    || { echo "[seed] ERROR: --target has no .instance.conf (not a minted instance): $TARGET_ROOT" >&2; exit 1; }
  if [[ -n "$TARGET_KIT" && ! "$TARGET_KIT" =~ ^[a-z][a-z0-9_-]{0,31}$ ]]; then
    echo "[seed] ERROR: --kit is not a valid pack id: $TARGET_KIT" >&2; exit 1
  fi
  if [[ -n "$TARGET_PACK" && ! "$TARGET_PACK" =~ ^[a-z][a-z0-9_-]{0,31}$ ]]; then
    echo "[seed] ERROR: --pack is not a valid pack id: $TARGET_PACK" >&2; exit 1
  fi
fi

_log()  { echo "[seed] $*"; }
_fail() { echo "[seed] FAIL: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Instance discovery (DGN-1037): _discover_instances lives in
# lib/discover_instances.sh -- shared with update_plan.sh/update_apply.sh so
# the planner, the applier, and the seeder all see the same estate through
# ONE truth source (DGN-1517: a duplicated copy here previously could not
# be reached by DOGANY_DISCOVER_INSTANCES_OVERRIDE test injection, unlike
# its siblings -- structurally untestable). 0 discovered instances = FAIL
# (silent-failure guard, §6), enforced below where INSTANCES is populated.
# ---------------------------------------------------------------------------
DISCOVER_LIB="$SCRIPT_DIR/lib/discover_instances.sh"
[[ -f "$DISCOVER_LIB" && -r "$DISCOVER_LIB" ]] \
  || _fail "인스턴스 탐지 라이브러리 부재 ($DISCOVER_LIB) -- fail-closed"
# shellcheck source=lib/discover_instances.sh
. "$DISCOVER_LIB"

if [[ -n "$TARGET_ROOT" ]]; then
  INSTANCES=("$(basename "$TARGET_ROOT"):$TARGET_ROOT")
  _log "인스턴스 원천: --target 단일 인스턴스 $TARGET_ROOT (스캔·오버라이드 생략)"
elif [[ -n "${DOGANY_PLAN_INSTANCES:-}" ]]; then
  IFS=',' read -r -a INSTANCES <<< "$DOGANY_PLAN_INSTANCES"
  _log "인스턴스 원천: DOGANY_PLAN_INSTANCES 오버라이드 ${#INSTANCES[@]}건 (스캔 생략)"
else
  if ! _scan="$(_discover_instances)"; then
    _fail "HOME 미설정/비실재 (HOME='${HOME:-}') -- 인스턴스 스캔 불가"
  fi
  INSTANCES=()
  while IFS= read -r _ln; do
    if [[ -n "$_ln" ]]; then INSTANCES+=("$_ln"); fi
  done <<< "$_scan"
  _log "인스턴스 스캔 루트: \$HOME/.dogany/instances 레지스트리 + \$HOME/.dogany/lite_instance 포인터 + 폴백 스캔 \$HOME/.dogany/main + \$HOME/.dogany/agents/* + \$HOME/dogany/* + \$HOME/dogany/*/agents/* + \$HOME/dogany/*/poc/* (판정 기준: .instance.conf 보유)"
  _log "인스턴스 발견: ${#INSTANCES[@]}건"
  for _ln in ${INSTANCES[@]+"${INSTANCES[@]}"}; do
    _log "  발견: $_ln"
  done
  if [[ "${#INSTANCES[@]}" -eq 0 ]]; then
    _fail "인스턴스 발견 0건 -- 0건은 통과가 아니라 FAIL (§6 조용한 실패 금지; 스캔 루트를 확인하세요)"
  fi
fi

# DGN-1698: a target-scoped seed is part of the mint/crew-join path.  Its
# catalog coordinates must be anchored at the target's canonical repo, not a
# tag-pinned consume checkout that happened to supply this machinery.
CATALOG_DEFAULT_LIB="$SCRIPT_DIR/lib/canonical_catalog.sh"
[[ -f "$CATALOG_DEFAULT_LIB" && -r "$CATALOG_DEFAULT_LIB" ]] \
  || _fail "canonical catalog library missing ($CATALOG_DEFAULT_LIB) -- fail-closed"
# shellcheck source=lib/canonical_catalog.sh
. "$CATALOG_DEFAULT_LIB"
if [[ -z "$CATALOG" ]]; then
  CATALOG_INSTANCE_ROOT="$TARGET_ROOT"
  if [[ -z "$CATALOG_INSTANCE_ROOT" && "${#INSTANCES[@]}" -gt 0 ]]; then
    # Estate mode operates on the discovered set. Discovery is sorted, so
    # this anchor is deterministic; normal estates share one canonical repo.
    CATALOG_INSTANCE_ROOT="${INSTANCES[0]#*:}"
  fi
  CATALOG="$(pack_default_catalog_for_instance "$REPO_DIR" "$CATALOG_INSTANCE_ROOT" "seed")" \
    || _fail "canonical catalog default resolution failed"
fi

# ---------------------------------------------------------------------------
# Atomic line upsert -- same atomic function family as pack_install.sh
# _packs_upsert_atomic (flock <conf>.lock + same-dir tmp + fsync +
# os.replace). Generalized to whole-line upsert because the two seeded files
# are line-registries, not a single DOGANY_PACKS= list:
#   _line_upsert <conf> <line> <uniq_prefix>
#     - a line identical to <line> already present  -> NO-OP (idempotency:
#       the file is not rewritten, diff 0 by construction)
#     - <uniq_prefix> non-empty and a DIFFERENT line starts with it ->
#       PRESERVED (additive contract: never overwrite), loud notice
#     - otherwise -> append <line> atomically
# ---------------------------------------------------------------------------
. "$SCRIPT_DIR/lib/line_upsert.sh"

# _upsert <conf> <line> <uniq_prefix> -- dry-run aware wrapper.
_upsert() {
  local conf="$1" line="$2" uniq="$3"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    "$PACK_PY3" - "$conf" "$line" "$uniq" <<'PYEOF'
import sys, pathlib
conf = pathlib.Path(sys.argv[1]); line, uniq = sys.argv[2], sys.argv[3]
lines = conf.read_text(encoding="utf-8").splitlines() if conf.exists() else []
if line in lines:
    print("[seed][dry-run]   present, no write: %s" % line)
elif uniq and any(ln.startswith(uniq) for ln in lines):
    old = next(ln for ln in lines if ln.startswith(uniq))
    print("[seed][dry-run]   would PRESERVE existing: %s (seed wanted: %s)" % (old, line))
else:
    print("[seed][dry-run]   would append to %s: %s" % (conf, line))
PYEOF
  else
    _line_upsert "$conf" "$line" "$uniq"
  fi
}

# ---------------------------------------------------------------------------
# Step 1 (spec seed step 1): mount set = union of DOGANY_PACKS over the
# instance table. A corrupt/unreadable conf ABORTS the seed (fail-closed;
# seeding from a suspect mount set would silently under-register, §6).
# Missing conf file entirely = that root is not a minted instance -> loud
# skip (estate table rows may include non-pack instances).
# ---------------------------------------------------------------------------
MOUNT_IDS=()          # unique pack ids
INST_NAMES=()         # per-instance parallel arrays for the crew step
INST_ROOTS=()
INST_MOUNTLINES=()

_id_re='^[a-z][a-z0-9_-]{0,31}$'
_ver_re='^[0-9][0-9A-Za-z.+-]*$'

for entry in "${INSTANCES[@]}"; do
  name="${entry%%:*}"
  root="${entry#*:}"
  conf="$root/.instance.conf"
  if [[ ! -f "$conf" ]]; then
    _log "$name: .instance.conf 없음 ($conf) -- 마운트 스캔 제외 (loud skip)"
    continue
  fi
  if ! packs_line="$(sed -n 's/^DOGANY_PACKS=//p' "$conf" 2>/dev/null | head -n1)"; then
    _fail "$name: .instance.conf 판독 FAIL (읽기 오류: $conf) -- 시드 중단 (fail-closed)"
  fi
  mounts=""
  if [[ -n "$packs_line" ]]; then
    IFS=',' read -r -a items <<< "$packs_line"
    for it in "${items[@]}"; do
      [[ -z "$it" ]] && continue
      pid="${it%%@*}"
      pver="${it#*@}"
      if [[ "$it" != *"@"* || ! "$pid" =~ $_id_re || ! "$pver" =~ $_ver_re ]]; then
        _fail "$name: DOGANY_PACKS 항목 파싱 실패 ('$it') -- 시드 중단 (fail-closed)"
      fi
      mounts="${mounts:+$mounts,}$it"
      seen=0
      for m in ${MOUNT_IDS[@]+"${MOUNT_IDS[@]}"}; do
        if [[ "$m" == "$pid" ]]; then seen=1; fi
      done
      if [[ "$seen" -eq 0 ]]; then MOUNT_IDS+=("$pid"); fi
    done
  fi
  cnt=0
  if [[ -n "$mounts" ]]; then cnt="$(awk -F',' '{print NF}' <<< "$mounts")"; fi
  _log "$name: mount ${cnt}건${mounts:+ ($mounts)}"
  INST_NAMES+=("$name")
  INST_ROOTS+=("$root")
  INST_MOUNTLINES+=("$mounts")
done

if [[ -n "$TARGET_KIT" ]]; then
  _kit_seen=0
  for m in ${MOUNT_IDS[@]+"${MOUNT_IDS[@]}"}; do
    if [[ "$m" == "$TARGET_KIT" ]]; then _kit_seen=1; fi
  done
  if [[ "$_kit_seen" -eq 0 ]]; then
    _fail "target $TARGET_ROOT: DOGANY_PACKS에 $TARGET_KIT@ 마운트 없음 -- 등록 불가 (fail-closed)"
  fi
  MOUNT_IDS=("$TARGET_KIT")
fi
if [[ -n "$TARGET_PACK" ]]; then
  _pack_seen=0
  for m in ${MOUNT_IDS[@]+"${MOUNT_IDS[@]}"}; do
    if [[ "$m" == "$TARGET_PACK" ]]; then _pack_seen=1; fi
  done
  if [[ "$_pack_seen" -eq 0 ]]; then
    _fail "target $TARGET_ROOT: DOGANY_PACKS에 $TARGET_PACK@ 마운트 없음 -- 등록 불가 (fail-closed)"
  fi
  if _crew_lease_active "$TARGET_PACK"; then
    _fail "크루 $TARGET_PACK -- lease 보유 중 (state=$(_crew_lease_state "$TARGET_PACK")) -- 소스 등록 거부 (DGN-1509 M3, fail-closed). 트랜잭션 종료 후 재실행하세요"
  fi
  MOUNT_IDS=("$TARGET_PACK")
fi
_log "마운트 팩 합집합: ${#MOUNT_IDS[@]}건${MOUNT_IDS[0]:+ (${MOUNT_IDS[*]})}"
if [[ "${#MOUNT_IDS[@]}" -eq 0 ]]; then
  _log "마운트 팩 0건 -- 시드할 레지스트리 항목이 없습니다 (0건은 통과가 아니라 의심: 인스턴스 표를 확인하세요)"
  if [[ -n "$TARGET_ROOT" ]]; then
    _fail "target $TARGET_ROOT: 마운트 팩 0건 -- 등록된 것이 없습니다 (target 모드에서 0건은 실패)"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 2+3 (spec seed steps 2-3): resolve each id's repo through the one
# coordinate lookup (pack_coords.sh pack_coord_for, DGN-1883 WP2: legacy
# package_dir, an existing pack-sources .repo, the pack-src clone), seed
# <id>.repo + explicit default <id>.channel=stable.
# ---------------------------------------------------------------------------
[[ -f "$CATALOG" ]] || _fail "catalog 없음: $CATALOG"

ERRORS=0
KIT_MODES=" "   # DGN-2056: " <id>=crew|derived ..." from each id's manifest
for pid in "${MOUNT_IDS[@]}"; do
  row="$(pack_catalog_row "$CATALOG" "$pid" 2>/dev/null)" || row="null"
  abs_repo=""
  [[ "$row" == "null" ]] || abs_repo="$(pack_coord_for "$pid" "$CATALOG" "$row" 2>/dev/null)" || abs_repo=""
  if [[ -z "$abs_repo" ]]; then
    would="$( [[ "$row" == "null" ]] || pack_row_dir "$CATALOG" "$row" 2>/dev/null)" || would=""
    if [[ -z "$would" ]]; then
      _log "FAIL: $pid -- no coordinate (no package_dir, pack-sources.conf .repo or source_url on the row; not registered)"
    else
      # A row with source_url and no pack-src clone yet: the clone is made by
      # pack_install.sh -- a path that does not exist is never registered.
      src_url="$(pack_row_field "$row" source_url 2>/dev/null)" || src_url=""
      _log "FAIL: $pid -- no pack-manifest.json at the coordinate: $would (not registered)${src_url:+; no source clone either: $(pack_source_dir "$pid") -- install with pack_install.sh first}"
    fi
    ERRORS=$((ERRORS + 1))
    continue
  fi
  # Registry values are normalized absolute paths (no "packs/../.." residue).
  abs_repo="$("$PACK_PY3" -c "import os,sys; print(os.path.normpath(sys.argv[1]))" "$abs_repo")"
  if pack_source_is_clone "$abs_repo"; then
    _log "$pid: no local checkout -- using the public source clone: $abs_repo"
  fi
  _log "$pid: repo=$abs_repo channel=stable"
  # DGN-2056: the kit's crew DB mode picks the crew.conf membership predicate
  # below (lib/kit_db_mode.py; unknown keeps the symlink predicate).
  if _km="$("$PACK_PY3" "$SCRIPT_DIR/lib/kit_db_mode.py" "$abs_repo/pack-manifest.json" 2>/dev/null)"; then
    KIT_MODES="$KIT_MODES $pid=$_km"
  fi
  if [[ -n "$TARGET_PACK" ]]; then
    _up_out="$(_upsert "$SOURCES_CONF" "$pid.repo=$abs_repo" "$pid.repo=")"
    printf '%s\n' "$_up_out"
    case "$_up_out" in
      *"appended:"*|*"would append"*) _src_result=added ;;
      *"PRESERVE"*) _src_result=preserved ;;
      *) _src_result=present ;;
    esac
    _upsert "$SOURCES_CONF" "$pid.channel=stable" "$pid.channel="
    _log "target-source pack=$pid root=$TARGET_ROOT result=$_src_result"
  else
    _upsert "$SOURCES_CONF" "$pid.repo=$abs_repo" "$pid.repo="
    _upsert "$SOURCES_CONF" "$pid.channel=stable" "$pid.channel="
  fi
done

# DGN-1877: --pack is source-only -- the crew registry and crew timezone
# below belong to the kit (--kit / estate seed), never to a staff pack.
if [[ -n "$TARGET_PACK" ]]; then
  if [[ "$ERRORS" -gt 0 ]]; then
    _fail "$TARGET_PACK 소스 등록 실패 -- 위 FAIL 라인 참조"
  fi
  _log "소스 등록 완료 (--pack $TARGET_PACK, crew.conf 미접촉)"
  exit 0
fi

# ---------------------------------------------------------------------------
# crew.conf seed (spec §3): upsert a root into <kit> crew.conf ONLY when both
# the readlink pattern and the DOGANY_PACKS "<kit>@" conditions hold.
# DGN-2056: a kit with no crew DB lane (capabilities.db_derived=true or
# db_lane=false, lib/kit_db_mode.py) has no database/<kit>.db to link, so
# for it the predicate is the mount alone -- and its crew must then have no
# crew DB at all (a crew DB beside a derived declaration is a contradiction,
# refused: the symlink truth is never silently dropped).
# ---------------------------------------------------------------------------
_kit_mode() { # <id> -- crew | derived | "" (unknown: the symlink predicate)
  local e
  for e in $KIT_MODES; do
    if [[ "${e%%=*}" == "$1" ]]; then echo "${e#*=}"; return 0; fi
  done
  echo ""
}

# Derived crews activate mounted declarations only. Unmounted declarations
# remain in crew.declared-pending and are promoted by a later seed after mount.
# Both registries and marker retirement share the crew.conf lock and lease gate.
_convert_declared() { # <id> -- dry-run aware, no instance writes
  "$PACK_PY3" "$SCRIPT_DIR/lib/declared_crew.py" "$CREW_DIR/$1" "$1" "$DRY_RUN"
}

SEEDED_ROOTS=()
for pid in "${MOUNT_IDS[@]}"; do
  kit_mode="$(_kit_mode "$pid")"
  # DGN-1716: a declared crew (crew_declare.sh, no kit) is never seeded over
  # as is; a crew DB kit converts by an explicit step (remove crew.declared).
  # DGN-2056: a derived kit converts here, after the lease check below.
  if [[ -f "$CREW_DIR/$pid/crew.declared" && "$kit_mode" != "derived" ]]; then
    _log "FAIL: crew $pid -- declared crew ($CREW_DIR/$pid/crew.declared); remove the marker to convert it to a kit crew, then re-run. fail-closed"
    ERRORS=$((ERRORS + 1))
    continue
  fi
  # DGN-1509 M3: refuse THIS kit's crew.conf seed while its lease is held
  # (any state -- PREPARING/DIRTY/INCOMPLETE all mean a crew transaction
  # owns this kit's registry right now). A seed re-run is cheap for a human
  # to repeat, so this is a hard skip + error, not a wait: "seed는 lease
  # 존재 시 거부한다 (대기 아님)" (spec §4 participant 5).
  if _crew_lease_active "$pid"; then
    _log "FAIL: 크루 $pid -- lease 보유 중 (state=$(_crew_lease_state "$pid")) -- crew.conf 시드 거부 (DGN-1509 M3, fail-closed). 트랜잭션 종료 후 재실행하세요"
    ERRORS=$((ERRORS + 1))
    continue
  fi
  # Symlink GROUND TRUTH always compares against $SHARED_HOME/crews (same
  # rule as update_plan.sh): overriding DOGANY_CREW_DIR alone redirects only
  # where crew.conf is WRITTEN, so a read-only overlay seed still verifies
  # the live symlink topology.
  crew_target="$SHARED_HOME/crews/$pid/$pid.db"
  crew_conf="$CREW_DIR/$pid/crew.conf"
  if [[ "$kit_mode" == "derived" && ( -e "$crew_target" || -L "$crew_target" ) ]]; then
    _log "FAIL: crew $pid -- the kit declares no crew DB lane (db_derived) but a crew DB exists ($crew_target) -- membership cannot be read from the mount alone, not seeded. fail-closed"
    ERRORS=$((ERRORS + 1))
    continue
  fi
  if [[ "$kit_mode" == "derived" && ( -f "$CREW_DIR/$pid/crew.declared" || -f "$CREW_DIR/$pid/crew.declared-pending" ) ]]; then
    if ! _convert_declared "$pid"; then
      ERRORS=$((ERRORS + 1))
      continue
    fi
  fi
  member_n=0
  i=0
  while [[ "$i" -lt "${#INST_NAMES[@]}" ]]; do
    name="${INST_NAMES[$i]}"
    root="${INST_ROOTS[$i]}"
    mounts="${INST_MOUNTLINES[$i]}"
    i=$((i + 1))
    case ",$mounts," in
      *",$pid@"*) : ;;
      *) continue ;;
    esac
    if [[ "$kit_mode" == "derived" ]]; then
      _log "crew $pid: member $name ($root) -- mount condition met (db_derived: no crew DB, the mount is the membership, DGN-2056)"
    else
      link="$(readlink "$root/database/$pid.db" 2>/dev/null || echo "")"
      [[ -n "$link" && "$link" == "$crew_target" ]] || continue
      _log "크루 $pid: 멤버 확인 $name ($root) -- symlink+mount 두 조건 충족"
    fi
    member_n=$((member_n + 1))
    SEEDED_ROOTS+=("$root")
    if [[ -n "$TARGET_KIT" ]]; then
      _up_out="$(_upsert "$crew_conf" "member=$root" "")"
      printf '%s\n' "$_up_out"
      case "$_up_out" in
        *"appended:"*|*"would append"*) _tgt_result=added ;;
        *) _tgt_result=present ;;
      esac
      _log "target-registered kit=$pid root=$root result=$_tgt_result"
    else
      _upsert "$crew_conf" "member=$root" ""
    fi
  done
  if [[ -n "$TARGET_KIT" && "$member_n" -eq 0 ]]; then
    _log "FAIL: 크루 $pid -- target $TARGET_ROOT 는 멤버 조건 미충족 (readlink database/$pid.db != $crew_target 또는 마운트 없음) -- 등록 불가"
    ERRORS=$((ERRORS + 1))
  fi
  _log "크루 $pid: 시드 멤버 ${member_n}건"
done

# ---------------------------------------------------------------------------
# DGN-1704 crew timezone: the crews-home ROOT crew.conf carries ONE
# TZ_OFFSET_HOURS every member inherits at mint (routines/lib/tz_offset.sh:
# per-agent override > crew value > host).  It is founded the first time a
# crew member is seeded -- from the founder's (first seeded member in listed
# order with a valid declaration) AGENT_TZ_OFFSET_HOURS, else this host's
# offset -- and never overwritten afterwards (same additive _upsert).  The
# per-kit <kit>/crew.conf registries stay member=-only: their parsers are
# fail-closed on any other line.
# ---------------------------------------------------------------------------
if [[ "${#SEEDED_ROOTS[@]}" -gt 0 ]]; then
  # shellcheck source=../../agents/.template/routines/lib/tz_offset.sh
  . "$REPO_DIR/agents/.template/routines/lib/tz_offset.sh"
  crew_tz_conf="$(tz_offset_crew_conf)"
  crew_tz="$(tz_offset_crew)"
  if [[ -n "$crew_tz" ]]; then
    _log "크루 시간대: TZ_OFFSET_HOURS=$crew_tz 유지 ($crew_tz_conf)"
  else
    crew_tz="" crew_tz_src=""
    for root in "${SEEDED_ROOTS[@]}"; do
      crew_tz="$(tz_offset_agent "$root")"
      if [[ -n "$crew_tz" ]]; then crew_tz_src="founder $root"; break; fi
    done
    if [[ -z "$crew_tz" ]]; then
      if crew_tz="$(tz_offset_host)"; then crew_tz_src="host localtime"
      else crew_tz=""; fi
    fi
    if [[ -n "$crew_tz" ]]; then
      _log "크루 시간대: TZ_OFFSET_HOURS=$crew_tz ($crew_tz_src) -> $crew_tz_conf"
      _upsert "$crew_tz_conf" "TZ_OFFSET_HOURS=$crew_tz" "TZ_OFFSET_HOURS="
    else
      _log "WARN: 크루 시간대 미설정 -- 창립 멤버 선언 없음 + 호스트 오프셋이 정수 시간 아님; 멤버는 개별 AGENT_TZ_OFFSET_HOURS 로 해석"
    fi
  fi
fi

if [[ "$ERRORS" -gt 0 ]]; then
  _fail "${ERRORS}건 등록 실패 -- 위 FAIL 라인 참조 (부분 시드는 유효하나 종료코드로 실패를 알립니다)"
fi
_log "시드 완료 (additive/idempotent -- 재실행 시 동일 입력이면 diff 0건)"

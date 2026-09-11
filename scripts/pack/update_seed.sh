#!/bin/bash
# update_seed.sh -- DGN-1031 slice 1: one-shot ADDITIVE seed for the unified
# update loop's two Metal-owned registry files (spec DGN-1031-SPEC §1 seed
# procedure + §3 crew.conf seed procedure).
#
#   1. ~/.dogany/pack-sources.conf   -- pack-id -> subscription source
#        <id>.repo=<abs path>        (from packs/catalog.json package_dir,
#                                     resolved relative to the catalog file)
#        <id>.channel=stable         (explicit default; operator-editable)
#   2. ~/.dogany/crews/<kit>/crew.conf -- crew member instance roots
#        member=<abs instance root>  (one line per member, listed order =
#                                     crew member order, spec §2 rule 4)
#        A root is seeded ONLY when BOTH §3 conditions hold:
#          (a) readlink <root>/database/<kit>.db == <crew-home>/<kit>/<kit>.db
#          (b) <root>/.instance.conf DOGANY_PACKS carries a "<kit>@" entry
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
#     --dry-run : print exactly what WOULD be written; write nothing.
#
# Instance list source (DGN-1037): HOME-relative disk scan -- a directory
# carrying .instance.conf at one of the estate layout depths
# ($HOME/.dogany/agents/*, $HOME/dogany/*, $HOME/dogany/*/agents/*,
# $HOME/dogany/*/poc/*) is an instance; see _discover_instances below.
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
#   DOGANY_PACK_CATALOG   catalog.json path         (default: <repo>/packs/catalog.json)
#   DOGANY_PLAN_INSTANCES "name:root,name:root,..." (default: $HOME estate scan,
#                         see _discover_instances -- DGN-1037)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
SOURCES_CONF="${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
CATALOG="${DOGANY_PACK_CATALOG:-$REPO_DIR/packs/catalog.json}"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "[seed] ERROR: unknown option: $arg" >&2; exit 1 ;;
  esac
done

_log()  { echo "[seed] $*"; }
_fail() { echo "[seed] FAIL: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Instance discovery (DGN-1037): HOME-relative disk scan, IDENTICAL rule to
# update_plan.sh _discover_instances (keep the two blocks in sync -- the
# planner and the seeder must see the same estate). Fixed-depth globs only
# (no recursion -> no symlink-cycle risk); dedup on the PHYSICAL root so a
# symlink alias (agents/main -> agents/<leader>) yields one entry; display
# name = basename of the physical root; LC_ALL=C sorted -> deterministic
# seed order. 0 discovered instances = FAIL (silent-failure guard, §6).
# ---------------------------------------------------------------------------
_discover_instances() { # prints sorted "name:root" lines; rc 1 = HOME unusable
  local d phys name seen="|" out=""
  if [[ -z "${HOME:-}" || ! -d "${HOME:-}" ]]; then return 1; fi
  for d in "$HOME/.dogany/agents"/*/ "$HOME/dogany"/*/ \
           "$HOME/dogany"/*/agents/*/ "$HOME/dogany"/*/poc/*/; do
    if [[ ! -d "$d" || ! -f "${d}.instance.conf" ]]; then continue; fi
    phys="$(cd "$d" 2>/dev/null && pwd -P)" || continue
    if [[ -z "$phys" ]]; then continue; fi
    case "$seen" in *"|$phys|"*) continue ;; esac
    seen="$seen$phys|"
    name="$(basename "$phys")"
    case "$name" in
      *[:,]*)
        echo "[discover] SKIP: instance dirname contains ':' or ',' (name:root format reserved): $phys" >&2
        continue ;;
    esac
    out="$out$name:$phys"$'\n'
  done
  printf '%s' "$out" | LC_ALL=C sort
}

if [[ -n "${DOGANY_PLAN_INSTANCES:-}" ]]; then
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
  _log "인스턴스 스캔 루트: \$HOME/.dogany/agents/* + \$HOME/dogany/* + \$HOME/dogany/*/agents/* + \$HOME/dogany/*/poc/* (판정 기준: .instance.conf 보유)"
  _log "인스턴스 발견: ${#INSTANCES[@]}건"
  for _ln in ${INSTANCES[@]+"${INSTANCES[@]}"}; do
    _log "  발견: $_ln"
  done
  if [[ "${#INSTANCES[@]}" -eq 0 ]]; then
    _fail "인스턴스 발견 0건 -- 0건은 통과가 아니라 FAIL (§6 조용한 실패 금지; 스캔 루트를 확인하세요)"
  fi
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
_line_upsert() {
  local conf="$1" line="$2" uniq="$3"
  python3 - "$conf" "$line" "$uniq" <<'PYEOF'
import fcntl, os, sys, tempfile, time, pathlib
conf = pathlib.Path(sys.argv[1])
line, uniq = sys.argv[2], sys.argv[3]
lock_path = str(conf) + ".lock"
timeout = float(os.environ.get("DOGANY_CONF_LOCK_TIMEOUT", "30"))
conf.parent.mkdir(parents=True, exist_ok=True)
lock_fh = open(lock_path, "a")
deadline = time.monotonic() + timeout
while True:
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.monotonic() >= deadline:
            sys.stderr.write(
                "[seed] FATAL: could not acquire %s within %.0fs "
                "(concurrent writer stuck?) -- conf untouched\n" % (lock_path, timeout))
            sys.exit(1)
        time.sleep(0.05)
try:
    lines = conf.read_text(encoding="utf-8").splitlines() if conf.exists() else []
    if line in lines:
        print("[seed]   present, untouched: %s" % line)
        sys.exit(0)
    if uniq and any(ln.startswith(uniq) for ln in lines):
        old = next(ln for ln in lines if ln.startswith(uniq))
        print("[seed]   PRESERVED existing (additive contract, not overwritten): "
              "%s (seed wanted: %s)" % (old, line))
        sys.exit(0)
    lines.append(line)
    data = "\n".join(lines) + "\n"
    mode = conf.stat().st_mode & 0o7777 if conf.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".seed.tmp.", dir=str(conf.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, conf)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(str(conf.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    print("[seed]   appended: %s" % line)
finally:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()
PYEOF
}

# _upsert <conf> <line> <uniq_prefix> -- dry-run aware wrapper.
_upsert() {
  local conf="$1" line="$2" uniq="$3"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    python3 - "$conf" "$line" "$uniq" <<'PYEOF'
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

_log "마운트 팩 합집합: ${#MOUNT_IDS[@]}건${MOUNT_IDS[0]:+ (${MOUNT_IDS[*]})}"
if [[ "${#MOUNT_IDS[@]}" -eq 0 ]]; then
  _log "마운트 팩 0건 -- 시드할 레지스트리 항목이 없습니다 (0건은 통과가 아니라 의심: 인스턴스 표를 확인하세요)"
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 2+3 (spec seed steps 2-3): resolve each id's repo via catalog
# package_dir (relative to the catalog FILE), seed <id>.repo + explicit
# default <id>.channel=stable.
# ---------------------------------------------------------------------------
[[ -f "$CATALOG" ]] || _fail "catalog 없음: $CATALOG"
CATALOG_DIR="$(cd "$(dirname "$CATALOG")" && pwd)"

ERRORS=0
for pid in "${MOUNT_IDS[@]}"; do
  pkg_dir="$(python3 -c "
import json, sys
cat = json.load(open(sys.argv[1]))
for p in cat.get('packs', []):
    if p.get('id') == sys.argv[2]:
        print(p.get('package_dir') or '')
        break
" "$CATALOG" "$pid" 2>/dev/null || echo "")"
  if [[ -z "$pkg_dir" ]]; then
    _log "FAIL: $pid -- catalog에 package_dir 없음 (repo 좌표 미해석, 등록 불가)"
    ERRORS=$((ERRORS + 1))
    continue
  fi
  if [[ "$pkg_dir" = /* ]]; then
    abs_repo="$pkg_dir"
  else
    abs_repo="$(python3 -c "import os,sys; print(os.path.normpath(os.path.join(sys.argv[1], sys.argv[2])))" "$CATALOG_DIR" "$pkg_dir")"
  fi
  if [[ ! -d "$abs_repo" ]]; then
    _log "FAIL: $pid -- repo 경로 실존하지 않음: $abs_repo (등록 불가)"
    ERRORS=$((ERRORS + 1))
    continue
  fi
  _log "$pid: repo=$abs_repo channel=stable"
  _upsert "$SOURCES_CONF" "$pid.repo=$abs_repo" "$pid.repo="
  _upsert "$SOURCES_CONF" "$pid.channel=stable" "$pid.channel="
done

# ---------------------------------------------------------------------------
# crew.conf seed (spec §3): upsert a root into <kit> crew.conf ONLY when both
# the readlink pattern and the DOGANY_PACKS "<kit>@" conditions hold.
# ---------------------------------------------------------------------------
for pid in "${MOUNT_IDS[@]}"; do
  # Symlink GROUND TRUTH always compares against $SHARED_HOME/crews (same
  # rule as update_plan.sh): overriding DOGANY_CREW_DIR alone redirects only
  # where crew.conf is WRITTEN, so a read-only overlay seed still verifies
  # the live symlink topology.
  crew_target="$SHARED_HOME/crews/$pid/$pid.db"
  crew_conf="$CREW_DIR/$pid/crew.conf"
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
    link="$(readlink "$root/database/$pid.db" 2>/dev/null || echo "")"
    if [[ -n "$link" && "$link" == "$crew_target" ]]; then
      member_n=$((member_n + 1))
      _log "크루 $pid: 멤버 확인 $name ($root) -- symlink+mount 두 조건 충족"
      _upsert "$crew_conf" "member=$root" ""
    fi
  done
  _log "크루 $pid: 시드 멤버 ${member_n}건"
done

if [[ "$ERRORS" -gt 0 ]]; then
  _fail "${ERRORS}건 등록 실패 -- 위 FAIL 라인 참조 (부분 시드는 유효하나 종료코드로 실패를 알립니다)"
fi
_log "시드 완료 (additive/idempotent -- 재실행 시 동일 입력이면 diff 0건)"

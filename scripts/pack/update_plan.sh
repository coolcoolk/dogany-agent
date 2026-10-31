#!/bin/bash
# update_plan.sh -- DGN-1031 slice 1: read-only planner for the unified
# update loop (spec DGN-1031-SPEC §첫 조각 "update-plan").
#
# ZERO-WRITE CONTRACT: this planner writes NOTHING outside one private
# mktemp dir (removed on exit). No conf upsert, no lock files, no git fetch
# (candidate resolution is local-tags-only, logged -- ONE exception: step 1b
# tag-fetches public-install clones under <shared home>/pack-src/, DGN-1818,
# never a developer checkout; step 2b registers such a clone for a
# MOUNTED but unregistered pack through update_seed.sh --pack, DGN-1877;
# and step 5 tag-fetches a framework clone whose origin IS the public
# mirror, DGN-1947 -- all three skipped under DOGANY_PLAN_NO_FETCH=1; every
# other framework repo is only READ on origin with git ls-remote before any
# "up to date" verdict, DGN-1947), no cache/backfill
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
#      DOGANY_PACKS x database symlink ground truth); any mismatch or member
#      count 0 = ABORT. Member version divergence (half-updated crew) blocks
#      that crew item and its requires_kit dependents only (DGN-1884).
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
#   9. Dogfooding scope (DGN-1461): candidate selection is per
#      (instance, pack), not per pack id. The former "every instance
#      mounting a pack must run the same version or the WHOLE plan aborts"
#      check was a pack-id property applied before the pack kind was even
#      read; it is now a SHARED-RESOURCE property: only a kit crew (one
#      physical DB / shared skills) requires version lockstep, and a
#      lockstep violation there blocks that crew item (BLOCKED-CREW-LOCKSTEP,
#      crew-joint-update verdict) and every pack whose candidate requires
#      its kit (BLOCKED-BY-DEPENDENCY); the rest of the plan stays (DGN-1884,
#      rc=2 + PLAN-END = the DGN-1843 complete-plan-with-blocked-items).
#      dec-263 (a): the split is judged for a crew sync -- every member on a
#      stable tag of the registry repo, the candidate = the newest stable
#      tag, no member above it -> "sync" (the owner's [버전 맞추기] brings
#      every member to that tag through update_apply's crew transaction);
#      anything else (a trial / untagged install, a pin, a member ahead of
#      the newest stable) -> "operator" (never offered to the owner).
#      DOGANY_PLAN_CREW_SYNC=<kit> plans THAT sync: the kit is an apply item
#      at the newest stable tag carrying the per-member versions it was
#      planned from, and its dependents stay applicable. A
#      non-shared behavior pack may legitimately diverge across instances
#      (one member dogfoods a pre-release) -- each member then gets its own
#      candidate item. Per-instance EXACT PINS come from the instance conf:
#        DOGANY_PACK_PINS=<id>=<tag>@<commit>[,<id>=<tag>@<commit>...]
#      The pin records the RESOLVED commit, not just the tag name: if the
#      same tag name later points at different content (the 2026-09-15
#      dev.3 -> dev.4 re-cut shape), the item is BLOCKED-PIN-CONTENT-DRIFT
#      and must be re-planned/re-declared -- a name match alone never
#      installs. A pin on a crew-shared kit is refused OUTRIGHT (scope
#      pre-reject): a single-member trial whose effect reaches the shared
#      DB/skills of unnamed members is never auto-expanded to the crew.
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
#   DOGANY_PLAN_CREW_SYNC  <kit id>: plan the dec-263 crew sync of that
#                          kit (see the CREW-SPLIT emit line below)
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
#                          DGN-1843 (additive, informational -- never an
#                          apply item):
#                            BLOCKED\t<FW|PACK|INSTANCE>\t<id or name>\t<status>\t<owners name:root ...>
#                            PLAN-END\t<apply item count>\t<blocked count>
#                          dec-263 (a) (additive, informational -- never an
#                          apply item; one per BLOCKED-CREW-LOCKSTEP kit):
#                            CREW-SPLIT\t<kit id>\t<sync|operator>\t<cand_tag>\t<versions name=ver ...>\t<members name:root ...>\t<dependents id ...>
#                          Under DOGANY_PLAN_CREW_SYNC=<kit> that kit's PACK
#                          line carries a 9th field, the per-member versions
#                          it was planned from (name=ver ...); update_apply
#                          then takes the asymmetric crew as declared instead
#                          of refusing it, and re-checks every declared fact.
#                          BLOCKED owners = the instances the block is
#                          attributable to (an evidence failure names only
#                          the failing members). PLAN-END is the LAST line
#                          and is written only when the plan was fully
#                          assembled: rc=2 WITH PLAN-END = a complete plan
#                          carrying blocked items; rc=2 WITHOUT it = ABORT,
#                          no plan.
#                          DGN-1922 (additive, an apply item -- one per
#                          (retired pack, mounting instance), emitted AFTER
#                          every kit/pack item and BEFORE framework items):
#                            RETIRE\t<id>\t<absorbing kit>\t<range>\t<inst_ver>\t<name:root>
#                          A retired pack whose absorbing kit is not (and is
#                          not planned to be) inside the range is a BLOCKED
#                          PACK line instead (BLOCKED-RETIRE-*), never an
#                          item.
#
# RETIREMENT (DGN-1922): a catalog row with status=retired +
# absorbed_by {kit, range} (read through lib/pack_retire.sh, the same parser
# pack_install.sh --retire uses; catalog = DOGANY_PACK_CATALOG, else this
# repo's packs/catalog.json) takes its id out of candidate resolution --
# a retired pack is never upgraded. Each mounting instance gets a RETIRE
# item when its absorbing kit's END STATE (the planned kit candidate, else
# the installed kit) satisfies the range; that instance's framework
# end-state evaluation then ignores the pack (it will not be there).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# DGN-1424: resolve the python3 interpreter ONCE, PATH-independent (a bare
# `python3` call resolves whatever the CALLER's PATH hands back -- see the
# lib file header). Reuses $PACK_PY3 if update_apply.sh (the parent) already
# resolved it.
PY_RESOLVE_LIB="$SCRIPT_DIR/lib/py_resolve.sh"
[[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
  || { echo "ERROR: python resolver library missing ($PY_RESOLVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/py_resolve.sh
. "$PY_RESOLVE_LIB"
pack_resolve_py3 || exit 1

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
SOURCES_CONF="${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
FW_CAND_OVERRIDE="${DOGANY_PLAN_FW_CANDIDATE:-}"
CREW_SYNC_KIT="${DOGANY_PLAN_CREW_SYNC:-}"

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

# DGN-1456/1460: 설치 증거 판독기 -- 설치기가 발급한 receipt 를 install
# (pack_install requires_kit)과 update(이 플래너, update_apply)가 같은 lib 로
# 읽는다. 부재 = ABORT (판독 기계 없이 장부만 믿는 판정은 이 티켓의 결함
# 그 자체다, fail-closed).
INSTALL_EVIDENCE_LIB="$SCRIPT_DIR/lib/install_evidence.sh"
if [[ ! -f "$INSTALL_EVIDENCE_LIB" || ! -r "$INSTALL_EVIDENCE_LIB" ]]; then
  _abort "설치 증거 판독기 부재 ($INSTALL_EVIDENCE_LIB) -- 장부 단독 판정 금지, fail-closed"
fi
# shellcheck source=lib/install_evidence.sh
. "$INSTALL_EVIDENCE_LIB"

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

# ---------------------------------------------------------------------------
# DGN-1947: framework remote read. Local tags alone never prove "up to date":
# a framework clone nobody fetches (a public install's ~/.dogany/framework,
# worst of all a shallow single-tag clone whose default fetch names one old
# tag) keeps answering "no update" forever while origin moves on. Before a
# framework verdict, origin is read once per repo:
#   - origin IS the public mirror (a framework-owned install clone, the
#     DGN-1818 class): a non-forced `git fetch --tags` -- it only adds tags
#     (a moved tag makes it fail, never clobbers), so release notes of the
#     new tag are readable too; then local tags ARE the fresh remote read;
#   - otherwise, or when that fetch fails: `git ls-remote --tags` (read-only)
#     run through the SAME channel whitelist (resolve_channel_tag) in a
#     private scratch repo carrying the remote tag names;
#   - no origin remote: local tags are the only source (nothing to be
#     behind); unreadable origin -> the verdict is "could not check"
#     (UNCHECKED-REMOTE), never "up to date".
# GIT_TERMINAL_PROMPT=0 plus a hard ceiling (DOGANY_PLAN_REMOTE_TIMEOUT,
# default 20s): a bridge has no terminal and a stalled keychain prompt must
# not hang /update. DOGANY_PLAN_NO_FETCH=1 skips the fetch (ls-remote still
# reads).
# ---------------------------------------------------------------------------
PRN_SNIP="$TMP/prn.snippet.sh"
sed -n '/^_pr_norm_url() {/,/^}/p' "$REPO_DIR/update.sh" > "$PRN_SNIP" 2>/dev/null || true
if ! grep -q '^_pr_norm_url() {' "$PRN_SNIP"; then
  _abort "_pr_norm_url 추출 실패 ($REPO_DIR/update.sh) -- 공개 미러 판정 기계 부재, fail-closed"
fi
# shellcheck disable=SC1090
source "$PRN_SNIP"

_fwr_timeout() { # <secs> <cmd...> -- rc of cmd; 143 on the ceiling
  local secs="$1" pid watcher rc=0
  shift
  GIT_TERMINAL_PROMPT=0 "$@" &
  pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) </dev/null >/dev/null 2>&1 &
  watcher=$!
  wait "$pid" 2>/dev/null || rc=$?
  kill "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true
  return "$rc"
}

FWR_REPOS=(); FWR_STATES=(); FWR_NAMES=()
FWR_STATE=""; FWR_NAMEFILE=""
_fw_remote_read() { # <repo> -- sets FWR_STATE (fetched|listed|no-origin|unreadable), FWR_NAMEFILE
  local repo="$1" i=0 url secs="${DOGANY_PLAN_REMOTE_TIMEOUT:-20}" refs
  while [[ "$i" -lt "${#FWR_REPOS[@]}" ]]; do
    if [[ "${FWR_REPOS[$i]}" == "$repo" ]]; then
      FWR_STATE="${FWR_STATES[$i]}"; FWR_NAMEFILE="${FWR_NAMES[$i]}"; return 0
    fi
    i=$((i + 1))
  done
  FWR_NAMEFILE="$TMP/fwr.${#FWR_REPOS[@]}.names"
  : > "$FWR_NAMEFILE"
  if ! url="$(git -C "$repo" remote get-url origin 2>/dev/null)" || [[ -z "$url" ]]; then
    FWR_STATE="no-origin"
  else
    FWR_STATE="unreadable"
    if [[ "${DOGANY_PLAN_NO_FETCH:-0}" != "1" \
          && "$(_pr_norm_url "$url")" == "$(_pr_norm_url "${DOGANY_PUBLIC_REPO_URL:-https://github.com/coolcoolk/dogany-agent}")" ]]; then
      if _fwr_timeout "$secs" git -C "$repo" fetch -q --tags origin >/dev/null 2>&1; then
        FWR_STATE="fetched"
      else
        _log "WARN: 프레임워크 clone 태그 fetch 실패 ($repo) -- ls-remote 로 원격을 읽습니다"
      fi
    fi
    if [[ "$FWR_STATE" != "fetched" ]] \
       && _fwr_timeout "$secs" git -C "$repo" ls-remote --tags --refs origin 'refs/tags/v*' > "$TMP/fwr.out" 2>/dev/null; then
      awk -F'\t' 'NF==2 { sub("^refs/tags/", "", $2); print $2 }' "$TMP/fwr.out" > "$FWR_NAMEFILE"
      FWR_STATE="listed"
    fi
  fi
  FWR_REPOS+=("$repo"); FWR_STATES+=("$FWR_STATE"); FWR_NAMES+=("$FWR_NAMEFILE")
}

_FWR_SCRATCH_N=0
_fw_remote_tag() { # <repo> <channel> <namefile> -- the channel's newest REMOTE tag (empty = none)
  local repo="$1" channel="$2" names="$3" s c tree
  _FWR_SCRATCH_N=$((_FWR_SCRATCH_N + 1))
  s="$TMP/fwr.scratch.$_FWR_SCRATCH_N"
  git init -q --bare "$s" >/dev/null 2>&1 || return 0
  tree="$(git -C "$s" hash-object -t tree -w /dev/null)" || return 0
  c="$(git -C "$s" -c user.name=plan -c user.email=plan@localhost commit-tree -m names "$tree" </dev/null)" || return 0
  {
    awk -v c="$c" 'NF { print "create refs/tags/" $0 " " c }' "$names"
    # release.sh's not-yet-published holds stay holds (DGN-1748).
    git -C "$repo" for-each-ref --format='%(refname:lstrip=3)' refs/dogany/unpublished/ 2>/dev/null \
      | awk -v c="$c" 'NF { print "create refs/dogany/unpublished/" $0 " " c }'
  } | git -C "$s" update-ref --stdin 2>/dev/null || return 0
  _fw_tag "$s" "$channel" "" 2>/dev/null || true
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

_tag_manifest() { # <repo> <tag> -- prints tab-separated "id kind pack_version requires_framework provides_kit requires_kit.kit"
  git -C "$1" archive --format=tar "$2" pack-manifest.json 2>/dev/null \
    | tar -xOf - 2>/dev/null \
    | "$PACK_PY3" -c "
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
rk = d.get('requires_kit')
rk = rk.get('kit') if isinstance(rk, dict) else ''
rk = rk if isinstance(rk, str) else ''
print('\t'.join([s('id'), s('kind'), s('pack_version'), s('requires_framework'), s('provides_kit'), rk]))
" 2>/dev/null
}

# dec-263 (a): judge a kit crew's version split (mem_names / mem_vers of the
# current pack) -- can the owner's [버전 맞추기] bring every member to the
# newest STABLE tag? Sets SPLIT_MODE (sync | operator), SPLIT_WHY (the
# operator reason), SPLIT_VERS ("name=ver ...") and SPLIT_LOW (the lowest
# member version). "operator" = the split is not the owner's to fix: a
# member runs an untagged or pre-release (trial) install, the registry pins
# the kit, the candidate is not the newest stable tag, or a member is ahead
# of it (a sync would be a downgrade).
_split_judge() { # <pid> <repo> <registry pin> <status> <cand_tag>
  local pid="$1" repo="$2" rpin="$3" st="$4" ctag="$5"
  local mi=0 v c c_rc low="" stable
  SPLIT_MODE="operator"; SPLIT_WHY=""; SPLIT_VERS=""; SPLIT_LOW=""
  while [[ "$mi" -lt "${#mem_names[@]}" ]]; do
    SPLIT_VERS="${SPLIT_VERS:+$SPLIT_VERS }${mem_names[$mi]}=${mem_vers[$mi]}"
    mi=$((mi + 1))
  done
  if [[ "$st" != "OK" || -z "$ctag" ]]; then
    SPLIT_WHY="후보 판정 상태 $st"; return 0
  fi
  if [[ -n "$rpin" ]]; then
    SPLIT_WHY="레지스트리 핀($rpin)"; return 0
  fi
  _pack_tag "$repo" "$pid" stable "" || true
  stable="$PT_TAG"
  if [[ -z "$stable" || "$stable" != "$ctag" ]]; then
    SPLIT_WHY="후보($ctag)가 최신 정식판 태그(${stable:-없음})가 아님"; return 0
  fi
  mi=0
  while [[ "$mi" -lt "${#mem_names[@]}" ]]; do
    v="${mem_vers[$mi]}"
    if [[ "$v" == *-* || "$v" == *+* ]]; then
      SPLIT_WHY="${mem_names[$mi]}=$v 시험(pre-release) 설치"; return 0
    fi
    if ! git -C "$repo" rev-parse -q --verify "refs/tags/pack/$pid/v$v" >/dev/null 2>&1; then
      SPLIT_WHY="${mem_names[$mi]}=$v 태그 없는 설치(시험 착지)"; return 0
    fi
    c_rc=0
    c="$("$PACK_PY3" "$DOGANY_SEMVER_LIB" compare "${ctag#pack/$pid/v}" "$v" 2>/dev/null)" || c_rc=$?
    if [[ "$c_rc" -ne 0 || -z "$c" ]]; then
      SPLIT_WHY="${mem_names[$mi]}=$v 버전 비교 불가"; return 0
    fi
    if [[ "$c" == "-1" ]]; then
      SPLIT_WHY="${mem_names[$mi]}=$v 가 최신 정식판(${ctag#pack/$pid/v})보다 높음 -- 맞추면 다운그레이드"; return 0
    fi
    if [[ -z "$low" ]]; then
      low="$v"
    else
      c="$("$PACK_PY3" "$DOGANY_SEMVER_LIB" compare "$v" "$low" 2>/dev/null || true)"
      if [[ "$c" == "-1" ]]; then low="$v"; fi
    fi
    mi=$((mi + 1))
  done
  SPLIT_MODE="sync"; SPLIT_LOW="$low"
  return 0
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
# DGN-1818: pack_source_is_clone / pack_source_refresh (step 1b).
COORDS_LIB="$SCRIPT_DIR/lib/pack_coords.sh"
[[ -f "$COORDS_LIB" && -r "$COORDS_LIB" ]] \
  || _abort "pack coordinate library missing ($COORDS_LIB) -- fail-closed"
# shellcheck source=lib/pack_coords.sh
. "$COORDS_LIB"
# DGN-1922: retirement declaration parser (shared with pack_install --retire).
RETIRE_LIB="$SCRIPT_DIR/lib/pack_retire.sh"
[[ -f "$RETIRE_LIB" && -r "$RETIRE_LIB" ]] \
  || _abort "pack retire library missing ($RETIRE_LIB) -- fail-closed"
# shellcheck source=lib/pack_retire.sh
. "$RETIRE_LIB"
RETIRE_CATALOG="${DOGANY_PACK_CATALOG:-$REPO_DIR/packs/catalog.json}"

_log "== 통합 업데이트 계획 (read-only 플래너, 쓰기 0건) =="
_log "read-only: 프레임워크 후보는 로컬 태그 + 원격 읽기(ls-remote; 공개 미러 clone 은 태그 fetch) 기준 (DGN-1947)"

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
  _log "인스턴스 스캔 루트: \$HOME/.dogany/instances 레지스트리 + \$HOME/.dogany/lite_instance 포인터 + 폴백 스캔 \$HOME/.dogany/main + \$HOME/.dogany/agents/* + \$HOME/dogany/* + \$HOME/dogany/*/agents/* + \$HOME/dogany/*/poc/* (판정 기준: .instance.conf 보유)"
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

# _inst_pack_pin <ppins_line> <pack_id> -- prints "<tag>@<commit>" or ""
# (per-instance exact pin lookup, DGN-1461; line format validated at conf
# read time, so this is a pure lookup).
_inst_pack_pin() {
  local line="$1" pid="$2" e
  [[ -n "$line" ]] || { printf ''; return 0; }
  local _pp=()
  IFS=',' read -r -a _pp <<< "$line"
  for e in ${_pp[@]+"${_pp[@]}"}; do
    [[ -z "$e" ]] && continue
    if [[ "${e%%=*}" == "$pid" ]]; then printf '%s' "${e#*=}"; return 0; fi
  done
  printf ''
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
# 1b) DGN-1818: refresh public-install pack clones before candidate
# resolution. A public user has no sibling checkout that someone else keeps
# current; their registered repo is the pack-src clone pack_install.sh made
# (pack_coords.sh pack_source_dir), and without a tag fetch here the planner
# would only ever see the tags that existed at first install -- no update
# would ever be offered. The ONE exception to the zero-write contract above:
# a non-force `fetch --tags` into a clone under <shared home>/pack-src/ (a
# framework-owned cache, never a developer checkout -- those are untouched,
# so an estate plan is byte-identical). A failed fetch (offline, or a
# published tag that moved) is loud and the plan continues on local tags.
# DOGANY_PLAN_NO_FETCH=1 skips it (strictly read-only verification runs).
# ---------------------------------------------------------------------------
if [[ "${#REG_REPOS[@]}" -gt 0 ]]; then
  for _rr in "${REG_REPOS[@]}"; do
    [[ -n "$_rr" ]] && pack_source_is_clone "$_rr" || continue
    if [[ "${DOGANY_PLAN_NO_FETCH:-0}" == "1" ]]; then
      _log "pack source clone: fetch skipped (DOGANY_PLAN_NO_FETCH=1) -- planning on local tags ($_rr)"
    elif pack_source_refresh "$_rr" 2>/dev/null; then
      _log "pack source clone: tags refreshed ($_rr)"
    else
      _log "WARN: pack source clone: tag fetch failed ($_rr) -- offline, or a published tag moved; continuing on local tags"
    fi
  done
fi

# ---------------------------------------------------------------------------
# 2) Instance scan (spec §6: 판독 OK/FAIL codes distinct from mount 0건).
# ---------------------------------------------------------------------------
I_NAME=(); I_ROOT=(); I_OK=(); I_MOUNTS=(); I_CHANNEL=(); I_FW=(); I_FWTAG=(); I_REPO=(); I_PIN=()
I_PPINS=()   # DGN-1461: per-instance pack exact pins (validated raw line)

_pk_re='^[a-z][a-z0-9_-]{0,31}$'
_vv_re='^[0-9][0-9A-Za-z.+-]*$'
_hex_re='^[0-9a-f]{7,40}$'
TOTAL_MOUNTS=0

for entry in "${INSTANCES[@]}"; do
  name="${entry%%:*}"
  root="${entry#*:}"
  conf="$root/.instance.conf"
  ok=1
  mounts=""
  channel=""
  fw=""
  fwtag=""
  repo=""
  pin=""
  ppins=""
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
    fwtag="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_FW_TAG=//p' | head -n1)"
    repo="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_REPO_ROOT=//p' | head -n1)"
    pin="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_UPDATE_PIN=//p' | head -n1)"
    ppins="$(printf '%s\n' "$conf_text" | sed -n 's/^DOGANY_PACK_PINS=//p' | head -n1)"
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
    # DGN-1461: per-instance pack exact-pin line. An exact pin MUST record
    # the resolved commit alongside the tag name (a name-only pin cannot
    # detect a re-cut tag = content drift) -- a pin without a 7-40 hex
    # commit is a parse FAIL, never a silently weaker pin.
    if [[ "$ok" -eq 1 && -n "$ppins" ]]; then
      IFS=',' read -r -a pentries <<< "$ppins"
      for pe in ${pentries[@]+"${pentries[@]}"}; do
        [[ -z "$pe" ]] && continue
        ppid="${pe%%=*}"
        prest="${pe#*=}"
        ptag="${prest%@*}"
        pcommit="${prest##*@}"
        if [[ "$pe" != *"="* || "$prest" != *"@"* || ! "$ppid" =~ $_pk_re \
              || -z "$ptag" || ! "$pcommit" =~ $_hex_re ]]; then
          _failline "$name: .instance.conf 판독 FAIL(CONF-PARSE-FAIL): DOGANY_PACK_PINS 항목 '$pe' 형식 위반 -- exact pin은 <id>=<tag>@<commit 7-40 hex> 로 해소된 내용 식별자까지 기록해야 함 (DGN-1461)"
          ok=0
          break
        fi
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
  I_CHANNEL+=("${channel:-release}"); I_FW+=("$fw"); I_FWTAG+=("$fwtag"); I_REPO+=("$repo"); I_PIN+=("$pin")
  I_PPINS+=("$ppins")
done

# ---------------------------------------------------------------------------
# 2b) DGN-1877 self-heal: a staff minted on 2.6.0 had only its kit registered,
# so its own pack is mounted, its public source clone sits in pack-src/, and
# yet it stays BLOCKED-UNREGISTERED forever. Such an id (mounted on a judged
# instance, no <id>.repo, pack-src/<id> clone present) is registered here by
# the seed's own target-scoped source registration -- same catalog source_url
# rule, same atomic upsert, same lease refusal (update_seed.sh --pack). A
# failure is loud and the id simply stays BLOCKED below. Strictly read-only
# runs (DOGANY_PLAN_NO_FETCH=1, e.g. update_apply --dry-run) only report it.
# ---------------------------------------------------------------------------
_reg_set_repo() { # <id> <repo> <channel>
  local pid="$1" i=0
  while [[ "$i" -lt "${#REG_IDS[@]}" ]]; do
    if [[ "${REG_IDS[$i]}" == "$pid" ]]; then
      REG_REPOS[$i]="$2"
      if [[ -z "${REG_CHANNELS[$i]}" ]]; then REG_CHANNELS[$i]="$3"; fi
      return 0
    fi
    i=$((i + 1))
  done
  REG_IDS+=("$pid"); REG_REPOS+=("$2"); REG_CHANNELS+=("$3"); REG_PINS+=("")
}
HEAL_TRIED=" "
i=0
while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
  h_root="${I_ROOT[$i]}"
  h_mounts="${I_MOUNTS[$i]}"
  h_ok="${I_OK[$i]}"
  i=$((i + 1))
  [[ "$h_ok" -eq 1 && -n "$h_mounts" ]] || continue
  IFS=',' read -r -a items <<< "$h_mounts"
  for it in "${items[@]}"; do
    pid="${it%%@*}"
    [[ -z "$(_reg_lookup "$pid" repo)" ]] || continue
    [[ "$HEAL_TRIED" == *" $pid "* ]] && continue
    h_src="$(pack_source_dir "$pid")"
    pack_source_is_clone "$h_src" || continue
    HEAL_TRIED="$HEAL_TRIED$pid "
    if [[ "${DOGANY_PLAN_NO_FETCH:-0}" == "1" ]]; then
      _log "$pid: 레지스트리 미등록이나 소스 클론 존재 ($h_src) -- self-heal 생략 (DOGANY_PLAN_NO_FETCH=1, read-only 실행)"
      continue
    fi
    _log "$pid: 레지스트리 미등록 + 소스 클론 존재 ($h_src) -- self-heal: update_seed.sh --target $h_root --pack $pid (DGN-1877)"
    h_rc=0
    bash "$SCRIPT_DIR/update_seed.sh" --target "$h_root" --pack "$pid" > "$TMP/heal.$pid.log" 2>&1 || h_rc=$?
    h_repo=""
    if [[ "$h_rc" -eq 0 && -f "$SOURCES_CONF" ]]; then
      h_repo="$(sed -n "s|^$pid\.repo=||p" "$SOURCES_CONF" | head -n1)"
    fi
    if [[ -n "$h_repo" ]]; then
      h_chan="$(sed -n "s|^$pid\.channel=||p" "$SOURCES_CONF" | head -n1)"
      _reg_set_repo "$pid" "$h_repo" "$h_chan"
      _log "$pid: self-heal 등록 완료 (repo=$h_repo channel=${h_chan:-stable})"
      if pack_source_is_clone "$h_repo"; then
        if pack_source_refresh "$h_repo" 2>/dev/null; then
          _log "pack source clone: tags refreshed ($h_repo)"
        else
          _log "WARN: pack source clone: tag fetch failed ($h_repo) -- offline, or a published tag moved; continuing on local tags"
        fi
      fi
    else
      _log "WARN: $pid: self-heal 실패 (update_seed rc=$h_rc) -- BLOCKED-UNREGISTERED 유지"
      while IFS= read -r h_ln; do
        _log "  seed: $h_ln"
      done < <(grep -E 'FAIL|ERROR' "$TMP/heal.$pid.log" | tail -n 3)
    fi
  done
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

# DGN-1922: retirement declarations of the mounted ids (catalog rows with
# status=retired). RD_OK=0 = malformed declaration (FAIL, items blocked).
RD_ID=(); RD_KIT=(); RD_RANGE=(); RD_OK=()
if [[ "${#SORTED_IDS[@]}" -gt 0 ]]; then
  if [[ ! -f "$RETIRE_CATALOG" ]]; then
    _log "WARN: catalog missing ($RETIRE_CATALOG) -- pack retirement declarations not read (DGN-1922)"
  else
    for pid in "${SORTED_IDS[@]}"; do
      _rd_rc=0
      _rd="$(pack_retire_declaration "$RETIRE_CATALOG" "$pid" 2>/dev/null)" || _rd_rc=$?
      if [[ "$_rd_rc" -eq 2 ]]; then
        _failline "catalog unreadable ($RETIRE_CATALOG) -- retirement declarations unknown, fail-closed (DGN-1922)"
        break
      fi
      [[ "$(printf '%s' "$_rd" | cut -f1)" == "retired" ]] || continue
      RD_ID+=("$pid"); RD_KIT+=("$(printf '%s' "$_rd" | cut -f2)"); RD_RANGE+=("$(printf '%s' "$_rd" | cut -f3)")
      if [[ "$_rd_rc" -eq 0 ]]; then
        RD_OK+=(1)
        _log "$pid: catalog status=retired (absorbed by $(printf '%s' "$_rd" | cut -f2) $(printf '%s' "$_rd" | cut -f3)) -- no upgrade candidate, judged for RETIRE (DGN-1922)"
      else
        RD_OK+=(0)
        _failline "$pid: catalog status=retired but absorbed_by {kit, range} is malformed -- never retired on a half declaration (DGN-1922)"
      fi
    done
  fi
fi
_rd_idx() { # <id> -- prints the RD index or -1
  local i=0
  while [[ "$i" -lt "${#RD_ID[@]}" ]]; do
    if [[ "${RD_ID[$i]}" == "$1" ]]; then echo "$i"; return 0; fi
    i=$((i + 1))
  done
  echo "-1"
}

# Pack item arrays. Pre-DGN-1461 this was strictly one item per mounted
# pack id; now a NON-SHARED pack whose members diverge (version or
# per-instance pin) yields one item PER (instance, pack) -- P_SCOPE names
# the single instance ("" = the item covers every mounting instance / the
# whole crew, the unchanged shape). Crew (kit) packs stay ONE item always.
P_ID=(); P_STATUS=(); P_DETAIL=(); P_CAND_TAG=(); P_CAND_VER=(); P_INST_VER=()
P_KIND=(); P_RANGE=(); P_MEMBERS=(); P_PROVIDES=()   # members: "name:root name:root" (crew) or mounting instances
P_SCOPE=(); P_SRC=()   # scope: "" or instance name; src: registry | instance-pin
P_OWNERS=()   # DGN-1843: "name:root ..." the block is attributable to ("" = every member)
P_RKIT=()     # DGN-1884: candidate manifest requires_kit.kit ("" = none)
P_SPLIT=()    # dec-263: "<sync|operator>\t<name=ver ...>" of a lockstep-blocked kit ("" = none)
P_SYNC=()     # dec-263: the planned crew sync's per-member versions "name=ver ..." ("" = none)

for pid in ${SORTED_IDS[@]+"${SORTED_IDS[@]}"}; do
  status="OK"; detail=""; cand_tag=""; cand_ver=""; inst_ver=""; kind=""; range=""; members=""; provides=""
  owners=""; rkit=""; split=""; sync_decl=""
  # DGN-1922: a retired pack is never an upgrade candidate (RETIRE lane).
  if [[ "$(_rd_idx "$pid")" -ge 0 ]]; then continue; fi

  # mounting instances + PER-MEMBER installed versions. DGN-1461: the old
  # in-place "every mounting instance at the same version or ABORT" check
  # ran here, BEFORE the pack kind was even read; version lockstep is a
  # property of a SHARED resource, not of a pack id, so divergence is only
  # recorded here and judged below once the kind is known.
  mem_names=(); mem_roots=(); mem_vers=(); mem_pins=()
  ver_diverged=0
  any_pin=0
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
            ver_diverged=1
          fi
          mem_names+=("${I_NAME[$i]}"); mem_roots+=("${I_ROOT[$i]}"); mem_vers+=("$v")
          mp="$(_inst_pack_pin "${I_PPINS[$i]}" "$pid")"
          mem_pins+=("$mp")
          if [[ -n "$mp" ]]; then any_pin=1; fi
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
          rkit="$(printf '%s' "$mf" | cut -f6)"
          if [[ "$mf_id" != "$pid" ]]; then
            _failline "$pid: manifest id('$mf_id') != 레지스트리 id('$pid') -- 구독원 오염 의심, FAIL (그릴 결함 6 어서트)"
            status="FAIL-ID-MISMATCH"
          elif [[ -n "$mf_ver" && "$mf_ver" != "$cand_ver" ]]; then
            if [[ "$cand_ver" == "$mf_ver"-* || "$cand_ver" == "$mf_ver"+* ]]; then
              _log "$pid: 태그 버전($cand_ver)은 manifest pack_version($mf_ver)의 pre-release 형 -- 도그푸딩 컷, 장부는 태그 버전을 기록 (dsp t2pin)"
            else
              _log "WARN: $pid: 태그 버전($cand_ver) != manifest pack_version($mf_ver) -- 3점 일치 위반 관측 (install-side 게이트가 재차 막음)"
            fi
          fi
        fi
      fi
    fi
  fi

  # -------------------------------------------------------------------------
  # DGN-1461: shared-resource lockstep + scope pre-reject. The kind is known
  # (or knowably unknown) by now -- judge divergence and per-instance pins
  # HERE, as a property of the shared resource, not of the pack id.
  # -------------------------------------------------------------------------
  crew_conf="$CREW_DIR/$pid/crew.conf"
  crew_target="$SHARED_HOME/crews/$pid/$pid.db"
  is_shared=0
  if [[ "$kind" == "kit" || -f "$crew_conf" ]]; then is_shared=1; fi
  # DGN-1716: a declared crew (crew_declare.sh) is a crew identity with NO
  # kit installed. Once the kit is mounted it is a kit crew: the registry
  # must be taken over by update_seed.sh first (remove crew.declared).
  if [[ -f "$CREW_DIR/$pid/crew.declared" ]]; then
    _abort "crew $pid: declared crew ($CREW_DIR/$pid/crew.declared, no kit) but $pid@ is mounted -- install the kit, remove crew.declared, then seed with update_seed.sh. fail-closed"
  fi

  if [[ "$is_shared" -eq 1 && "$any_pin" -eq 1 ]]; then
    # Scope pre-reject: a per-instance pin on a crew-shared kit is a
    # single-member trial whose effect (shared DB schema / shared skills)
    # reaches members the request never named. Never auto-expanded.
    pin_holders=""
    mi=0
    while [[ "$mi" -lt "${#mem_names[@]}" ]]; do
      if [[ -n "${mem_pins[$mi]}" ]]; then
        pin_holders="${pin_holders:+$pin_holders, }${mem_names[$mi]}"
      fi
      mi=$((mi + 1))
    done
    _abort "팩 $pid: 공유 자원(크루 kit)에 인스턴스별 exact pin 지정 ($pin_holders) -- 단독 시험의 효과가 공유 DB/skill을 통해 요청에 없는 멤버에 닿음; 자동 확대 금지, 사전 거부 (DGN-1461. 전 멤버 공동 시험 또는 격리 DB 시험으로 전환하세요)"
  fi
  if [[ "$ver_diverged" -eq 1 ]]; then
    ver_list=""
    mi=0
    while [[ "$mi" -lt "${#mem_names[@]}" ]]; do
      ver_list="${ver_list:+$ver_list, }${mem_names[$mi]}=${mem_vers[$mi]}"
      mi=$((mi + 1))
    done
    if [[ "$is_shared" -eq 1 ]]; then
      # DGN-1884: block THIS crew item (and, in 4b, the packs that require
      # its kit) -- never the whole plan. Pre-DGN-1884 this was an _abort:
      # one half-updated crew hid the framework and every unrelated pack,
      # and the owner read it as a lookup (connection) failure.
      # dec-263 (a): can the owner's [버전 맞추기] fix it, and is this the
      # run that plans exactly that sync?
      _split_judge "$pid" "$repo" "$pin" "$status" "$cand_tag"
      if [[ "$SPLIT_MODE" == "sync" && "$CREW_SYNC_KIT" == "$pid" ]]; then
        _log "팩 $pid: 크루 버전 맞추기 계획 (DOGANY_PLAN_CREW_SYNC) -- 멤버 버전 ($ver_list) 전원을 최신 정식판 $cand_tag 로, update_apply 크루 트랜잭션 1건 (멤버별 설치본은 적용 시점에 선언 대조, dec-263)"
        inst_ver="$SPLIT_LOW"
        sync_decl="$SPLIT_VERS"
      else
        _failline "팩 $pid: 공유 자원 호환성 검사 -- 크루 멤버 간 설치 버전 불일치 ($ver_list), 반쪽 갱신 의심, 크루 공동 갱신으로만 해소 가능, fail-closed -- 이 팩만 BLOCKED, 나머지 계획은 계속 (DGN-1461 lockstep / DGN-1884)"
        status="BLOCKED-CREW-LOCKSTEP"
        owners="$members"
        if [[ "$SPLIT_MODE" == "sync" ]]; then
          _log "팩 $pid: 크루 버전 맞추기 가능 -- 전원 정식판 태그 설치, 최신 정식판 $cand_tag (dec-263: 오너의 [버전 맞추기])"
        else
          _log "팩 $pid: 크루 버전 맞추기 불가 -- $SPLIT_WHY -> 운영자 전용 (오너에게 버튼 없음, dec-263)"
        fi
        if [[ "$CREW_SYNC_KIT" == "$pid" ]]; then
          _failline "팩 $pid: 크루 버전 맞추기 요청(DOGANY_PLAN_CREW_SYNC)이지만 맞출 수 없음 -- $SPLIT_WHY (dec-263)"
        fi
      fi
      split="$SPLIT_MODE"$'\t'"$SPLIT_VERS"
    elif [[ -z "$kind" ]]; then
      _abort "팩 $pid: 인스턴스 간 설치 버전 불일치 ($ver_list)인데 팩 종류 판독 불가(status=$status) -- 공유 자원이 아님을 증명할 수 없어 fail-closed (DGN-1461)"
    fi
  fi

  if [[ "$is_shared" -eq 1 || ( "$ver_diverged" -eq 0 && "$any_pin" -eq 0 ) ]]; then
  # single item covering every mounting instance (unchanged pre-DGN-1461
  # shape: uniform versions, one registry-sourced candidate).
    if [[ "$status" == "OK" && -n "$inst_ver" ]]; then
      # DGN-1456: 설치 증거 대조 -- 버전 비교보다 먼저. 장부(DOGANY_PACKS)가
      # "설치됐다"고 주장하는 각 인스턴스에서 설치기가 발급한 receipt 를
      # 판독한다. 측정된 결함: 장부만 읽던 이 플래너가 payload 0 인 유령
      # 엔트리(main-agent pack_health@0.10.0)를 "최신 상태"로 판정했다.
      # 소비 계약(보고서 §4.5): MISMATCH/INCOMPLETE/CHECK_ERROR = FAIL
      # (전체 rc=2 -- apply 가 plan nonzero 를 받아 적용 전체 거부);
      # UNKNOWN(receipt 이전 legacy 흔적) = 소리내며 통과 -- receipt 는 이
      # 계약 착지 이후의 설치에만 존재하므로, UNKNOWN 차단은 estate 전체의
      # 업데이트(=receipt 발급 경로)를 스스로 잠근다.
      _ev_bad=0
      for _ev_m in $members; do
        _ev_name="${_ev_m%%:*}"; _ev_root="${_ev_m#*:}"
        _ev_ver="$inst_ver"
        # dec-263: a planned crew sync reads each member at its OWN version.
        for _sd in $sync_decl; do
          if [[ "${_sd%%=*}" == "$_ev_name" ]]; then _ev_ver="${_sd#*=}"; fi
        done
        _ev="$(evidence_consume "$_ev_root" "$pid" "$_ev_ver")"
        _ev_v="${_ev%%|*}"; _ev_d="${_ev#*|}"
        case "$_ev_v" in
          VERIFIED) _log "$pid: $_ev_name 설치 증거 VERIFIED ($_ev_d)" ;;
          UNKNOWN)  _log "WARN: $pid: $_ev_name 설치 증거 UNKNOWN -- receipt 이전(legacy) 설치 흔적만 존재, 판정은 진행하나 무증거 상태 ($_ev_d)" ;;
          *)
            _failline "$pid: $_ev_name 설치 증거 FAIL(EVIDENCE-$_ev_v): $_ev_d -- 장부를 설치 사실로 읽지 않음 (DGN-1456 fail-closed)"
            owners="${owners:+$owners }$_ev_m"
            _ev_bad=1 ;;
        esac
      done
      if [[ "$_ev_bad" -eq 1 ]]; then
        status="FAIL-EVIDENCE"
      fi
    fi
    if [[ "$status" == "OK" && -n "$inst_ver" ]]; then
      # drift guard + up-to-date: both strings printed, then compared via
      # the shipped comparator's PRECEDENCE lane (semver_range.py compare,
      # SemVer §11 -- no 3rd semver copy). The old ==/>= range lane
      # PREFIX-parsed versions, so a pre-release ledger entry
      # (pack_health@0.11.0-dev.1, dsp t2pin) read EQUAL to the stable
      # 0.11.0 candidate -- the upgrade was never detected. compare orders
      # a pre-release BELOW its release, so dev-install -> stable-cut is an
      # upgrade candidate and stable -> dev pin is drift (downgrade), both
      # loud. An unparseable version exits 2 -> FAIL, never masquerades as
      # drift or up-to-date.
      _pcmp_rc=0
      _pcmp="$("$PACK_PY3" "$DOGANY_SEMVER_LIB" compare "$cand_ver" "$inst_ver" 2>/dev/null)" || _pcmp_rc=$?
      if [[ "$_pcmp_rc" -ne 0 || -z "$_pcmp" ]]; then
        _failline "$pid: 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$inst_ver' -- semver 형식 아님), fail-closed"
        status="FAIL-VERSION-FORM"
      elif [[ "$_pcmp" == "0" ]]; then
        _log "$pid: 설치 $inst_ver == 후보 $cand_ver -- 최신 상태 (업데이트 없음)"
        status="UP-TO-DATE"
      elif [[ "$_pcmp" == "-1" ]]; then
        _log "$pid: 경고 -- 표류 의심: 구독원이 설치본보다 뒤 (후보 $cand_tag < 설치 $inst_ver) -- BLOCKED-DRIFT (다운그레이드 후보 생성 금지)"
        status="BLOCKED-DRIFT"
      else
        _log "$pid: 설치 $inst_ver vs 후보 $cand_tag -> 업그레이드 후보 판정"
      fi
  fi
  else
    # ---------------------------------------------------------------------
    # DGN-1461 per-(instance, pack) candidates: a non-shared behavior pack
    # whose members diverge (version or pin) gets one item per member. A
    # pinned member's candidate is its EXACT pin; the pin's recorded commit
    # must still be what the tag name resolves to (content drift = the
    # 2026-09-15 dev.3 -> dev.4 re-cut shape = BLOCKED, re-plan).
    # ---------------------------------------------------------------------
    _log "$pid: 인스턴스 간 버전 상이 또는 개별 exact pin 존재 -- 후보를 (instance, pack) 단위로 계산 (DGN-1461; 비공유 behavior pack의 버전 차이는 정당한 도그푸딩 상태)"
    mi=0
    while [[ "$mi" -lt "${#mem_names[@]}" ]]; do
      mname="${mem_names[$mi]}"; mroot="${mem_roots[$mi]}"
      mver="${mem_vers[$mi]}"; mpin="${mem_pins[$mi]}"
      mi=$((mi + 1))
      m_status="$status"; m_tag="$cand_tag"; m_ver="$cand_ver"
      m_kind="$kind"; m_range="$range"; m_provides="$provides"; m_src="registry"; m_rkit="$rkit"
      if [[ -n "$mpin" ]]; then
        m_src="instance-pin"
        p_tag="${mpin%@*}"
        p_commit="${mpin##*@}"
        if [[ -z "$repo" || ! -d "$repo" ]]; then
          : # registry-unresolvable already loud above; the pin cannot resolve either
        elif [[ "$p_tag" != "pack/$pid/v"* ]]; then
          _failline "${pid}[$mname]: instance pin 태그 '$p_tag'가 pack/$pid/v* 네임스페이스가 아님 -- FAIL"
          m_status="FAIL-PIN-FORM"
        elif ! now_commit="$(git -C "$repo" rev-parse -q --verify "refs/tags/$p_tag^{commit}" 2>/dev/null)"; then
          _failline "${pid}[$mname]: instance pin 태그 부재 ($p_tag, repo=$repo) -- 최신 폴백 금지, FAIL"
          m_status="FAIL-PIN-MISS"
        elif [[ "$now_commit" != "$p_commit"* ]]; then
          _log "${pid}[$mname]: PIN CONTENT DRIFT -- 태그 $p_tag 의 현재 commit(${now_commit})이 pin에 기록된 ${p_commit}과 다름: 같은 이름, 다른 내용 -- BLOCKED-PIN-CONTENT-DRIFT (재계획 필요: pin을 현재 내용으로 재선언하거나 해제하세요)"
          m_status="BLOCKED-PIN-CONTENT-DRIFT"
        else
          m_tag="$p_tag"
          m_ver="${p_tag#pack/$pid/v}"
          mf="$(_tag_manifest "$repo" "$p_tag" || true)"
          if [[ -z "$mf" ]]; then
            _failline "${pid}[$mname]: pin 태그 $p_tag 에서 pack-manifest.json 판독 불가 -- fail-closed"
            m_status="FAIL-MANIFEST"
          else
            mf_id="$(printf '%s' "$mf" | cut -f1)"
            m_kind="$(printf '%s' "$mf" | cut -f2)"
            m_range="$(printf '%s' "$mf" | cut -f4)"
            m_provides="$(printf '%s' "$mf" | cut -f5)"
            m_rkit="$(printf '%s' "$mf" | cut -f6)"
            if [[ "$mf_id" != "$pid" ]]; then
              _failline "${pid}[$mname]: pin manifest id('$mf_id') != '$pid' -- 구독원 오염 의심, FAIL"
              m_status="FAIL-ID-MISMATCH"
            elif [[ "$m_kind" == "kit" ]]; then
              _abort "팩 ${pid}[$mname]: instance pin 태그 $p_tag 가 kind=kit 매니페스트로 해소됨 -- 공유 자원 단독 시험, 사전 거부 (DGN-1461)"
            else
              m_status="OK"
              _log "${pid}[$mname]: instance exact pin 채택 -- 후보 $p_tag (pin commit $p_commit 일치)"
            fi
          fi
        fi
      fi
      if [[ "$m_status" == "OK" && -n "$mver" ]]; then
        # same shipped-comparator drift/up-to-date lane as the single-item
        # path above, judged against THIS member's installed version.
        _pcmp_rc=0
        _pcmp="$("$PACK_PY3" "$DOGANY_SEMVER_LIB" compare "$m_ver" "$mver" 2>/dev/null)" || _pcmp_rc=$?
        if [[ "$_pcmp_rc" -ne 0 || -z "$_pcmp" ]]; then
          _failline "${pid}[$mname]: 버전 비교 불가 (후보 '$m_ver' vs 설치 '$mver' -- semver 형식 아님), fail-closed"
          m_status="FAIL-VERSION-FORM"
        elif [[ "$_pcmp" == "0" ]]; then
          _log "${pid}[$mname]: 설치 $mver == 후보 $m_ver -- 최신 상태 (업데이트 없음)"
          m_status="UP-TO-DATE"
        elif [[ "$_pcmp" == "-1" ]]; then
          _log "${pid}[$mname]: 경고 -- 표류 의심: 구독원이 설치본보다 뒤 (후보 $m_tag < 설치 $mver) -- BLOCKED-DRIFT (다운그레이드 후보 생성 금지; 도그푸딩 종료도 자동 downgrade가 아니라 명시 해제)"
          m_status="BLOCKED-DRIFT"
        else
          _log "${pid}[$mname]: 설치 $mver vs 후보 $m_tag -> 업그레이드 후보 판정"
        fi
      fi
      P_ID+=("$pid"); P_STATUS+=("$m_status"); P_DETAIL+=("")
      P_CAND_TAG+=("$m_tag"); P_CAND_VER+=("$m_ver"); P_INST_VER+=("$mver")
      P_KIND+=("$m_kind"); P_RANGE+=("$m_range"); P_MEMBERS+=("$mname:$mroot")
      P_PROVIDES+=("$m_provides"); P_SCOPE+=("$mname"); P_SRC+=("$m_src"); P_OWNERS+=("")
      P_RKIT+=("$m_rkit"); P_SPLIT+=(""); P_SYNC+=("")
    done
    continue
  fi

  # crew discovery (3-source cross-validation) -- runs for any id with a
  # crew.conf OR a kit-kind candidate manifest (crew consistency is a
  # plan-level invariant regardless of the item's candidate status).
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
  P_SCOPE+=(""); P_SRC+=("registry"); P_OWNERS+=("$owners"); P_RKIT+=("$rkit")
  P_SPLIT+=("$split"); P_SYNC+=("$sync_decl")
done

# ---------------------------------------------------------------------------
# 4b) DGN-1884: a crew lockstep block reaches the packs that need that kit.
#     A pack whose candidate declares requires_kit.kit = a lockstep-blocked
#     kit (its provides_kit, else its id -- the name update_apply matches)
#     is blocked with it: its install would fail the requires_kit gate
#     against a half-updated crew. Everything else stays in the plan.
# ---------------------------------------------------------------------------
LOCKSTEP_KITS=""
pi=0
while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
  if [[ "${P_STATUS[$pi]}" == "BLOCKED-CREW-LOCKSTEP" ]]; then
    LOCKSTEP_KITS="$LOCKSTEP_KITS ${P_ID[$pi]}${P_PROVIDES[$pi]:+ ${P_PROVIDES[$pi]}}"
  fi
  pi=$((pi + 1))
done
if [[ -n "$LOCKSTEP_KITS" ]]; then
  pi=0
  while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
    rk="${P_RKIT[$pi]}"
    if [[ -n "$rk" && " $LOCKSTEP_KITS " == *" $rk "* \
          && "${P_STATUS[$pi]}" != "BLOCKED-CREW-LOCKSTEP" ]]; then
      _failline "팩 ${P_ID[$pi]}${P_SCOPE[$pi]:+[${P_SCOPE[$pi]}]}: requires_kit '$rk' 가 크루 버전 불일치로 BLOCKED -- 이 팩도 BLOCKED-BY-DEPENDENCY (DGN-1884; 크루 공동 갱신 뒤 재계획)"
      P_STATUS[$pi]="BLOCKED-BY-DEPENDENCY"
    fi
    pi=$((pi + 1))
  done
fi

# ---------------------------------------------------------------------------
# 5) Framework candidate per instance (channel=main = loud SKIP, spec §6).
# ---------------------------------------------------------------------------
F_NAME=(); F_ROOT=(); F_FROM=(); F_FROMX=(); F_TO=(); F_STATUS=(); F_DETAIL=()

i=0
while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
  name="${I_NAME[$i]}"
  root="${I_ROOT[$i]}"
  ok="${I_OK[$i]}"
  channel="${I_CHANNEL[$i]}"
  fw="${I_FW[$i]}"
  fwtag="${I_FWTAG[$i]}"
  repo="${I_REPO[$i]}"
  pin="${I_PIN[$i]}"
  i=$((i + 1))
  FWR_STATE=""
  [[ "$ok" -eq 1 ]] || continue

  # DGN-1723: F_FROM stays the recorded bare X.Y.Z (feeds the pack req-range
  # guards, which must not see a pre-release); F_FROMX is the exact installed
  # build (DOGANY_FW_TAG while its X.Y.Z equals DOGANY_FW_VERSION, same rule
  # as update.sh fw_downgrade_guard) -- the compare basis AND every owner-facing
  # "from" (FW/ATOMIC-FW emit, logs), so a button reads dev.54 -> dev.55.
  fw_eff="$fw"
  _fwt="${fwtag#v}"
  if [[ "$fwtag" == v[0-9]* && "${_fwt%%[-+]*}" == "${fw%%[-+]*}" ]]; then
    fw_eff="$_fwt"
  fi

  if [[ -n "$FW_CAND_OVERRIDE" ]]; then
    _log "$name: 가상 프레임워크 후보 주입: $FW_CAND_OVERRIDE (DOGANY_PLAN_FW_CANDIDATE, 검증용)"
    cand_ver="$FW_CAND_OVERRIDE"
    cand_disp="v$FW_CAND_OVERRIDE(virtual)"
  elif [[ "$channel" == "main" ]]; then
    _log "$name: 프레임워크 SKIP (channel=main -- 태그 기반 후보 없음)"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("")
    F_STATUS+=("SKIP-MAIN"); F_DETAIL+=("channel=main")
    continue
  else
    if [[ -z "$repo" || ! -d "$repo" ]]; then
      _failline "$name: DOGANY_REPO_ROOT 미해석 ('$repo') -- 프레임워크 후보 해석 불가, FAIL"
      F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("")
      F_STATUS+=("FAIL-REPO"); F_DETAIL+=("repo unresolved")
      continue
    fi
    tag=""
    if ! tag="$(_fw_tag "$repo" "$channel" "$pin")"; then
      _failline "$name: 프레임워크 태그 해석 실패 (channel=$channel, pin='$pin')"
      F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("")
      F_STATUS+=("FAIL-TAG"); F_DETAIL+=("tag resolve failed")
      continue
    fi
    # DGN-1947: the verdict below rests on a fresh remote read (see
    # _fw_remote_read). A pin names its tag explicitly: no remote read.
    if [[ -z "$pin" ]]; then
      _fw_remote_read "$repo"
      case "$FWR_STATE" in
        fetched)
          _log "$name: 프레임워크 원격 태그 fetch 완료 ($repo) -- 로컬 태그 = 원격"
          if ! tag="$(_fw_tag "$repo" "$channel" "$pin")"; then
            _failline "$name: 프레임워크 태그 해석 실패 (channel=$channel, pin='$pin')"
            F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("")
            F_STATUS+=("FAIL-TAG"); F_DETAIL+=("tag resolve failed")
            continue
          fi ;;
        listed)
          rtag="$(_fw_remote_tag "$repo" "$channel" "$FWR_NAMEFILE")"
          if [[ -n "$rtag" && "$rtag" != "$tag" ]]; then
            _rcmp="-1"
            [[ -z "$tag" ]] || _rcmp="$("$PACK_PY3" "$DOGANY_SEMVER_LIB" compare "${tag#v}" "${rtag#v}" 2>/dev/null || echo "?")"
            if [[ "$_rcmp" == "-1" ]]; then
              _log "$name: 원격 origin 의 채널 최신 태그 $rtag > 로컬 ${tag:-없음} ($repo 가 아직 받지 않음) -- 원격 기준 후보"
              tag="$rtag"
            fi
          fi
          _log "$name: 프레임워크 원격 태그 확인 (ls-remote, channel=$channel): ${rtag:-없음}" ;;
        no-origin)
          _log "$name: 프레임워크 repo 에 origin 없음 ($repo) -- 로컬 태그가 유일한 원천" ;;
        *)
          _log "$name: 경고 -- 프레임워크 원격(origin) 을 읽지 못함 ($repo) -- \"최신\" 판정 금지, 확인 불가로 보고" ;;
      esac
    fi
    if [[ -z "$tag" ]]; then
      _log "$name: 프레임워크 v* 태그 0건 -- BLOCKED (\"최신\" 판정 금지)"
      F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("")
      F_STATUS+=("BLOCKED-NO-TAG"); F_DETAIL+=("0 tags")
      continue
    fi
    cand_ver="${tag#v}"
    cand_disp="$tag"
  fi

  _semverish='^[0-9]+\.[0-9]+\.[0-9]+'
  if [[ ! "$cand_ver" =~ $_semverish || ! "$fw" =~ $_semverish ]]; then
    _failline "$name: 프레임워크 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$fw'), fail-closed"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("$cand_ver")
    F_STATUS+=("FAIL-VERSION-FORM"); F_DETAIL+=("")
    continue
  fi
  # DGN-1723: precedence compare (semver_range.py compare, SemVer §11), never
  # the ==/>= range lane -- that one PREFIX-parses, so a pre-release candidate
  # (2.6.0-dev.55) read EQUAL to the recorded DOGANY_FW_VERSION (2.6.0, which
  # never carries a pre-release) and dev.54 -> dev.55 was "최신 상태". The
  # exact installed build is DOGANY_FW_TAG while its X.Y.Z still equals
  # DOGANY_FW_VERSION (same rule as update.sh fw_downgrade_guard); without it
  # a same-X.Y.Z pre-release candidate is not judged older/equal (proceeds).
  _fcmp_rc=0
  _fcmp="$("$PACK_PY3" "$DOGANY_SEMVER_LIB" compare "$cand_ver" "$fw_eff" 2>/dev/null)" || _fcmp_rc=$?
  if [[ "$_fcmp_rc" -ne 0 || -z "$_fcmp" ]]; then
    _failline "$name: 프레임워크 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$fw_eff'), fail-closed"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("$cand_ver")
    F_STATUS+=("FAIL-VERSION-FORM"); F_DETAIL+=("")
    continue
  fi
  if [[ "$_fcmp" == "-1" && "$fw_eff" == "$fw" && "${cand_ver%%[-+]*}" == "$fw" && "$cand_ver" == *-* ]]; then
    _log "$name: 설치 $fw 는 정확한 빌드(DOGANY_FW_TAG)를 알 수 없어 같은 X.Y.Z 의 프리릴리스 후보 $cand_ver 는 표류로 판정하지 않음"
    _fcmp=1
  fi
  if [[ "$_fcmp" == "0" && -z "$FW_CAND_OVERRIDE" && -z "$pin" && "$FWR_STATE" == "unreadable" ]]; then
    _log "$name: 프레임워크 설치 $fw_eff == 로컬 후보 $cand_ver 이지만 원격 확인 불가 -- UNCHECKED-REMOTE (최신 판정 아님)"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("$cand_ver")
    F_STATUS+=("UNCHECKED-REMOTE"); F_DETAIL+=("origin unreadable")
    continue
  fi
  if [[ "$_fcmp" == "0" ]]; then
    _log "$name: 프레임워크 설치 $fw_eff == 후보 $cand_ver -- 최신 상태 (업데이트 없음)"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("$cand_ver")
    F_STATUS+=("UP-TO-DATE"); F_DETAIL+=("")
    continue
  fi
  if [[ "$_fcmp" == "-1" ]]; then
    _log "$name: 경고 -- 표류 의심: 구독원이 설치본보다 뒤 (프레임워크 후보 $cand_disp < 설치 $fw_eff) -- BLOCKED-DRIFT"
    F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("$cand_ver")
    F_STATUS+=("BLOCKED-DRIFT"); F_DETAIL+=("")
    continue
  fi
  _log "$name: 프레임워크 설치 $fw_eff vs 후보 $cand_disp -> 업그레이드 후보 판정"
  F_NAME+=("$name"); F_ROOT+=("$root"); F_FROM+=("$fw"); F_FROMX+=("$fw_eff"); F_TO+=("$cand_ver")
  F_STATUS+=("OK"); F_DETAIL+=("$cand_disp")
done

# ---------------------------------------------------------------------------
# 6) Compatibility end-state + prefix rule (spec §2, via the shipped guard).
# ---------------------------------------------------------------------------
_pack_item_idx() { # <id> <inst_name> -- prints index or -1 (scope-aware, DGN-1461:
  # a per-instance item only answers for ITS instance; "" scope answers for all)
  local pid="$1" nm="$2" i=0
  while [[ "$i" -lt "${#P_ID[@]}" ]]; do
    if [[ "${P_ID[$i]}" == "$pid" ]] \
       && [[ -z "${P_SCOPE[$i]}" || "${P_SCOPE[$i]}" == "$nm" ]]; then
      echo "$i"; return 0
    fi
    i=$((i + 1))
  done
  echo "-1"
}

# ---------------------------------------------------------------------------
# 5b) DGN-1922: RETIRE items -- one per (retired pack, mounting instance).
#     The absorbing kit's END STATE on that instance decides: the planned kit
#     candidate when its item is OK (it applies earlier in this plan), else
#     the installed kit. Judged before section 6 so a retiring pack is left
#     out of that instance's framework end state; re-judged after 6b in case
#     the kit item it leaned on was held.
# ---------------------------------------------------------------------------
R_ID=(); R_KIT=(); R_RANGE=(); R_INST=(); R_MEM=(); R_NAME=(); R_STATUS=(); R_KVER=(); R_VIA=()
_retire_judge() { # <kit> <range> <kver> -- prints the status
  local rc=0
  if [[ -z "$3" ]]; then echo "BLOCKED-RETIRE-NO-KIT"; return 0; fi
  "$PACK_PY3" "$DOGANY_SEMVER_LIB" satisfies "$3" "$2" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) echo "OK" ;;
    1) echo "BLOCKED-RETIRE-KIT-RANGE" ;;
    *) echo "FAIL-VERSION-FORM" ;;
  esac
}
_inst_ver_of() { # <mounts> <id>
  local it
  IFS=',' read -r -a _ivo <<< "$1"
  for it in ${_ivo[@]+"${_ivo[@]}"}; do
    if [[ "${it%%@*}" == "$2" ]]; then printf '%s' "${it#*@}"; return 0; fi
  done
  printf ''
}
rd=0
while [[ "$rd" -lt "${#RD_ID[@]}" ]]; do
  pid="${RD_ID[$rd]}"; r_kit="${RD_KIT[$rd]}"; r_rng="${RD_RANGE[$rd]}"; r_ok="${RD_OK[$rd]}"
  rd=$((rd + 1))
  i=0
  while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
    name="${I_NAME[$i]}"; root="${I_ROOT[$i]}"; mounts="${I_MOUNTS[$i]}"
    i=$((i + 1))
    [[ "${I_OK[$((i - 1))]}" -eq 1 ]] || continue
    r_inst="$(_inst_ver_of "$mounts" "$pid")"
    [[ -n "$r_inst" ]] || continue
    kver="$(_inst_ver_of "$mounts" "$r_kit")"; via="installed"
    kidx="$(_pack_item_idx "$r_kit" "$name")"
    if [[ "$kidx" -ge 0 && "${P_STATUS[$kidx]}" == "OK" ]]; then
      kver="${P_CAND_VER[$kidx]}"; via="planned"
    fi
    if [[ "$r_ok" -ne 1 ]]; then
      st="BLOCKED-RETIRE-DECL"
    elif [[ -f "$CREW_DIR/$pid/crew.conf" ]]; then
      _failline "$pid[$name]: retired id is a crew-shared resource (crew.conf present) -- pack retirement is instance-scoped only, fail-closed (DGN-1922)"
      st="BLOCKED-RETIRE-CREW"
    else
      st="$(_retire_judge "$r_kit" "$r_rng" "$kver")"
    fi
    case "$st" in
      OK) _log "$pid[$name]: RETIRE -- absorbing kit $r_kit end state $kver ($via) satisfies '$r_rng'; installed $r_inst is removed (DGN-1922)" ;;
      BLOCKED-RETIRE-NO-KIT) _log "$pid[$name]: RETIRE held -- absorbing kit $r_kit neither installed nor planned, pack stays (BLOCKED-RETIRE-NO-KIT, DGN-1922)" ;;
      BLOCKED-RETIRE-KIT-RANGE) _log "$pid[$name]: RETIRE held -- absorbing kit $r_kit end state $kver ($via) does not satisfy '$r_rng', pack stays (BLOCKED-RETIRE-KIT-RANGE, DGN-1922)" ;;
      FAIL-VERSION-FORM) _failline "$pid[$name]: absorbing kit version '$kver' vs range '$r_rng' not comparable, fail-closed (DGN-1922)" ;;
    esac
    R_ID+=("$pid"); R_KIT+=("$r_kit"); R_RANGE+=("$r_rng"); R_INST+=("$r_inst")
    R_MEM+=("$name:$root"); R_NAME+=("$name"); R_STATUS+=("$st"); R_KVER+=("$kver"); R_VIA+=("$via")
  done
done
_retire_planned() { # <id> <inst_name> -- rc 0 when a RETIRE item is planned
  local j=0
  while [[ "$j" -lt "${#R_ID[@]}" ]]; do
    if [[ "${R_ID[$j]}" == "$1" && "${R_NAME[$j]}" == "$2" && "${R_STATUS[$j]}" == "OK" ]]; then return 0; fi
    j=$((j + 1))
  done
  return 1
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
      if _retire_planned "$pid" "$name"; then
        _log "$name/$pid: removed by a RETIRE item -- left out of the framework end state (DGN-1922)"
        continue
      fi
      pidx="$(_pack_item_idx "$pid" "$name")"
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

# DGN-1922: re-judge a RETIRE that leaned on a planned kit candidate the
# 6b pass then held -- the installed kit decides instead.
rj=0
while [[ "$rj" -lt "${#R_ID[@]}" ]]; do
  if [[ "${R_STATUS[$rj]}" == "OK" && "${R_VIA[$rj]}" == "planned" ]]; then
    kidx="$(_pack_item_idx "${R_KIT[$rj]}" "${R_NAME[$rj]}")"
    if [[ "$kidx" -lt 0 || "${P_STATUS[$kidx]}" != "OK" ]]; then
      i=0; r_mounts=""
      while [[ "$i" -lt "${#I_NAME[@]}" ]]; do
        if [[ "${I_NAME[$i]}" == "${R_NAME[$rj]}" ]]; then r_mounts="${I_MOUNTS[$i]}"; fi
        i=$((i + 1))
      done
      R_KVER[$rj]="$(_inst_ver_of "$r_mounts" "${R_KIT[$rj]}")"; R_VIA[$rj]="installed"
      R_STATUS[$rj]="$(_retire_judge "${R_KIT[$rj]}" "${R_RANGE[$rj]}" "${R_KVER[$rj]}")"
      _log "${R_ID[$rj]}[${R_NAME[$rj]}]: absorbing kit candidate item held -- re-judged on the installed kit ${R_KVER[$rj]:-none}: ${R_STATUS[$rj]} (DGN-1922)"
    fi
  fi
  rj=$((rj + 1))
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
  _log "  $STEP. framework ${F_NAME[$idx]}: ${F_FROMX[$idx]} -> ${F_TO[$idx]} (재시작 1건: ${F_NAME[$idx]})"
  _emit "$(printf 'FW\t%s\t%s\t%s\t%s' "${F_NAME[$idx]}" "${F_ROOT[$idx]}" "${F_FROMX[$idx]}" "${F_TO[$idx]}")"
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
    local pin_note=""
    if [[ "${P_SRC[$idx]}" == "instance-pin" ]]; then
      pin_note=" [instance exact pin]"
    fi
    _log "  $STEP. pack $pid (인스턴스 ${n}건: $names): ${P_INST_VER[$idx]} -> ${P_CAND_TAG[$idx]}$pin_note (재시작 ${n}건)"
  fi
  _emit "$(printf 'PACK\t%s\t%s\t%s\t%s\t%s\t%s\t%s' \
    "$pid" "${P_KIND[$idx]}" "${P_PROVIDES[$idx]}" "${P_INST_VER[$idx]}" \
    "${P_CAND_TAG[$idx]}" "${P_CAND_VER[$idx]}" "$members")${P_SYNC[$idx]:+$'\t'${P_SYNC[$idx]}}"
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
      group="${group:+$group + }framework ${F_NAME[$fi_i]}: ${F_FROMX[$fi_i]} -> ${F_TO[$fi_i]}"
      a_lines+=("$(printf 'ATOMIC-FW\t%s\t%s\t%s\t%s' \
        "${F_NAME[$fi_i]}" "${F_ROOT[$fi_i]}" "${F_FROMX[$fi_i]}" "${F_TO[$fi_i]}")")
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
            "${P_CAND_TAG[$pi]}" "${P_CAND_VER[$pi]}" "${P_MEMBERS[$pi]}")${P_SYNC[$pi]:+$'\t'${P_SYNC[$pi]}}")
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

# DGN-1417: the owner-facing update surface has one non-negotiable execution
# order.  A planner must emit that order, not merely print it differently:
# kit -> independent pack -> framework.  Atomic groups above remain the sole
# supported exception, because their bridges are all down and their internal
# pack-first executor proves there is no live intermediate prefix.
#
# Kit packs, then independent packs (alphabetical id order preserved from scan).
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

# DGN-1922: RETIRE items after every kit/pack item (the absorbing kit has
# landed by then) and before the framework (its end state excludes them).
ri=0
while [[ "$ri" -lt "${#R_ID[@]}" ]]; do
  if [[ "${R_STATUS[$ri]}" == "OK" ]]; then
    STEP=$((STEP + 1))
    _log "  $STEP. retire pack ${R_ID[$ri]} (${R_NAME[$ri]}): ${R_INST[$ri]} -> removed (absorbed by ${R_KIT[$ri]} ${R_KVER[$ri]}, 1 restart: ${R_NAME[$ri]})"
    _emit "$(printf 'RETIRE\t%s\t%s\t%s\t%s\t%s' "${R_ID[$ri]}" "${R_KIT[$ri]}" "${R_RANGE[$ri]}" "${R_INST[$ri]}" "${R_MEM[$ri]}")"
    RESTART_TOTAL=$((RESTART_TOTAL + 1))
    PLANNED=$((PLANNED + 1))
    R_STATUS[$ri]="EMITTED"
  fi
  ri=$((ri + 1))
done

# Framework is last.  This used to precede ordinary packs, which could erase
# kit data without an error; moving only update_apply would be insufficient as
# the emit file is its authority.
fi_i=0
while [[ "$fi_i" -lt "${#F_NAME[@]}" ]]; do
  if [[ "${F_STATUS[$fi_i]}" == "OK" ]]; then
    _emit_fw "$fi_i"
    F_STATUS[$fi_i]="EMITTED"
  fi
  fi_i=$((fi_i + 1))
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
# DGN-1843: an instance whose .instance.conf could not be read is not in
# any item at all -- name it, so its own surface can tell "blocked" from
# "nothing to update".
ii=0
while [[ "$ii" -lt "${#I_NAME[@]}" ]]; do
  if [[ "${I_OK[$ii]}" -ne 1 ]]; then
    _log "  instance ${I_NAME[$ii]}: CONF-FAIL"
    _emit "$(printf 'BLOCKED\tINSTANCE\t%s\t%s\t%s' "${I_NAME[$ii]}" "CONF-FAIL" "${I_NAME[$ii]}:${I_ROOT[$ii]}")"
    BLOCKED_N=$((BLOCKED_N + 1))
  fi
  ii=$((ii + 1))
done
fi_i=0
while [[ "$fi_i" -lt "${#F_NAME[@]}" ]]; do
  st="${F_STATUS[$fi_i]}"
  case "$st" in
    EMITTED|UP-TO-DATE) : ;;
    *) _log "  framework ${F_NAME[$fi_i]}: $st${F_DETAIL[$fi_i]:+ -- ${F_DETAIL[$fi_i]}}"
       _emit "$(printf 'BLOCKED\tFW\t%s\t%s\t%s' "${F_NAME[$fi_i]}" "$st" "${F_NAME[$fi_i]}:${F_ROOT[$fi_i]}")"
       BLOCKED_N=$((BLOCKED_N + 1)) ;;
  esac
  fi_i=$((fi_i + 1))
done
pi=0
while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
  st="${P_STATUS[$pi]}"
  case "$st" in
    EMITTED|UP-TO-DATE) : ;;
    *) _log "  pack ${P_ID[$pi]}${P_SCOPE[$pi]:+[${P_SCOPE[$pi]}]}: $st"
       _emit "$(printf 'BLOCKED\tPACK\t%s\t%s\t%s' "${P_ID[$pi]}" "$st" "${P_OWNERS[$pi]:-${P_MEMBERS[$pi]}}")"
       BLOCKED_N=$((BLOCKED_N + 1)) ;;
  esac
  pi=$((pi + 1))
done
ri=0
while [[ "$ri" -lt "${#R_ID[@]}" ]]; do
  if [[ "${R_STATUS[$ri]}" != "EMITTED" ]]; then
    _log "  retire ${R_ID[$ri]}[${R_NAME[$ri]}]: ${R_STATUS[$ri]}"
    _emit "$(printf 'BLOCKED\tPACK\t%s\t%s\t%s' "${R_ID[$ri]}" "${R_STATUS[$ri]}" "${R_MEM[$ri]}")"
    BLOCKED_N=$((BLOCKED_N + 1))
  fi
  ri=$((ri + 1))
done
if [[ "$BLOCKED_N" -eq 0 ]]; then
  _log "  (차단/보류 0건)"
fi
# dec-263 (a): what the owner surface needs to word a lockstep-blocked kit
# and offer (or not) its [버전 맞추기] -- informational, never an apply item.
pi=0
while [[ "$pi" -lt "${#P_ID[@]}" ]]; do
  if [[ "${P_STATUS[$pi]}" == "BLOCKED-CREW-LOCKSTEP" && -n "${P_SPLIT[$pi]}" ]]; then
    deps=""
    pj=0
    while [[ "$pj" -lt "${#P_ID[@]}" ]]; do
      rk="${P_RKIT[$pj]}"
      if [[ "${P_STATUS[$pj]}" == "BLOCKED-BY-DEPENDENCY" && -n "$rk" \
            && ( "$rk" == "${P_ID[$pi]}" || "$rk" == "${P_PROVIDES[$pi]}" ) \
            && " $deps " != *" ${P_ID[$pj]} "* ]]; then
        deps="${deps:+$deps }${P_ID[$pj]}"
      fi
      pj=$((pj + 1))
    done
    _emit "$(printf 'CREW-SPLIT\t%s\t%s\t%s\t%s\t%s' "${P_ID[$pi]}" "${P_SPLIT[$pi]%%$'\t'*}" \
      "${P_CAND_TAG[$pi]}" "${P_SPLIT[$pi]#*$'\t'}" "${P_MEMBERS[$pi]}")"$'\t'"$deps"
  fi
  pi=$((pi + 1))
done

echo ""
_log "적용 항목 ${PLANNED}건 / 차단·보류 ${BLOCKED_N}건 / 재시작 ${RESTART_TOTAL}건"
_log "계획 산출 완료 (read-only, 쓰기 0건 -- 사설 tmp 외 어떤 파일도 만들지 않음)"

# DGN-1843: completeness marker, the LAST emit line, written only here. rc
# alone cannot tell the two rc=2 outcomes apart: _failline (an item is
# blocked; the rest of the plan is still computed and emitted) vs _abort
# (no plan at all). A consumer that may act on a partial-estate plan (the
# /update surface) requires this line; update_apply.sh keeps its stricter
# "planner rc != 0 = apply nothing" contract unchanged.
_emit "$(printf 'PLAN-END\t%s\t%s' "$PLANNED" "$BLOCKED_N")"

exit "$OVERALL_RC"

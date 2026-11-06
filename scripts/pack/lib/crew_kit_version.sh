# crew_kit_version.sh -- the kit version an existing crew runs (DGN-1682 item 4).
# Sourced library + print-only CLI. MOVED out of pack_install.sh (DGN-1910
# locked spec 4.1(c): the mint flow's custom_ready reads the same answer, so
# there is one implementation, not a copy).
#
# Sourced:  _crew_kit_version <kit> [self-root]
#   self-root defaults to $ROOT (pack_install's instance) and is excluded:
#   the question is what the crew it is joining/staying in runs.
# CLI:      bash crew_kit_version.sh <kit> [--self <root>] [--tag-repo <git dir>]
#   prints CREW_KIT_STATE= / CREW_KIT_V= / CREW_KIT_PEERS= lines (and, with
#   --tag-repo, CREW_KIT_TAG= / CREW_KIT_TAG_PRESENT=0|1). Read-only.
#   rc 0 answered / 1 estate scan failed / 2 usage.
#
# members = the other estate instances whose database/<kit>.db resolves to
# the crew-shared DB (source 3 of the DGN-1509 3-source cross-validation,
# the same scan _rk_scope_gate / the lockstep guard use), version = each
# member's DOGANY_PACKS <kit>@<ver> record (source 2 -- the exact field
# update_plan.sh compares when it ABORTs a split crew).
# Sets:
#   CREW_KIT_STATE  none       -- no crew DB, or no other member
#                   uniform    -- every other member records the same version
#                   split      -- other members already disagree
#                   unrecorded -- a member mounts the DB with no <kit>@ record
#   CREW_KIT_V      the uniform version (uniform only)
#   CREW_KIT_PEERS  "name=ver, ..." for messages
# Returns 1 when the estate cannot be scanned (callers fail closed).

_CKV_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Physical path of a (possibly chained, relative) symlink; same rule as
# pack_install.sh _kit_resolve_link, kept private so sourcing this file never
# redefines the installer's own helper.
_ckv_resolve_link() {
  local src="$1" dir base
  while [[ -L "$src" ]]; do
    dir="$(cd -P "$(dirname "$src")" 2>/dev/null && pwd)" || return 1
    base="$(readlink "$src")"
    case "$base" in
      /*) src="$base" ;;
      *)  src="$dir/$base" ;;
    esac
  done
  dir="$(cd -P "$(dirname "$src")" 2>/dev/null && pwd)" || return 1
  printf '%s/%s' "$dir" "$(basename "$src")"
}

_crew_kit_version() {
  local kit="$1" self="${2-${ROOT:-}}"
  # The crew DB is symlink ground truth: always $SHARED_HOME/crews, never
  # DOGANY_CREW_DIR (that moves only the crew.conf registry -- the
  # update_plan.sh / crew_leave.sh doctrine; scripts/pack/lib/crew_paths.py).
  local shared_home="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
  local crew_db="$shared_home/crews/$kit/$kit.db"
  local crew_phys disc scan self_phys="" line name root other phys pv
  CREW_KIT_STATE="none"; CREW_KIT_V=""; CREW_KIT_PEERS=""
  [[ -e "$crew_db" || -L "$crew_db" ]] || return 0
  crew_phys="$(_ckv_resolve_link "$crew_db")" || return 0
  disc="$_CKV_LIB_DIR/discover_instances.sh"
  [[ -f "$disc" && -r "$disc" ]] || return 1
  # shellcheck source=discover_instances.sh
  . "$disc"
  scan="$(_discover_instances)" || return 1
  if [[ -n "$self" ]]; then
    self_phys="$(cd -P "$self" 2>/dev/null && pwd)" || self_phys="$self"
  fi
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    name="${line%%:*}"
    root="${line#*:}"
    [[ -n "$self_phys" && "$root" == "$self_phys" ]] && continue
    other="$root/database/$kit.db"
    [[ -e "$other" || -L "$other" ]] || continue
    phys="$(_ckv_resolve_link "$other")" || continue
    [[ "$phys" == "$crew_phys" ]] || continue
    pv="$(sed -n 's/^DOGANY_PACKS=//p' "$root/.instance.conf" 2>/dev/null | head -1 \
      | tr ',' '\n' | awk -F@ -v id="$kit" '$1 == id {print substr($0, length(id)+2)}' | head -1)"
    CREW_KIT_PEERS="${CREW_KIT_PEERS:+$CREW_KIT_PEERS, }$name=${pv:-<none>}"
    if [[ -z "$pv" ]]; then
      CREW_KIT_STATE="unrecorded"
    elif [[ "$CREW_KIT_STATE" == "none" ]]; then
      CREW_KIT_STATE="uniform"; CREW_KIT_V="$pv"
    elif [[ "$CREW_KIT_STATE" == "uniform" && "$pv" != "$CREW_KIT_V" ]]; then
      CREW_KIT_STATE="split"
    fi
  done <<< "$scan"
  [[ "$CREW_KIT_STATE" == "uniform" ]] || CREW_KIT_V=""
  return 0
}

# ---- print-only CLI (executed, not sourced) --------------------------------
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  _ckv_main() {
    local kit="" self="" repo="" tag
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --self)     [[ $# -ge 2 ]] || return 2; self="$2"; shift 2 ;;
        --tag-repo) [[ $# -ge 2 ]] || return 2; repo="$2"; shift 2 ;;
        -*)         return 2 ;;
        *)          [[ -z "$kit" ]] || return 2; kit="$1"; shift ;;
      esac
    done
    [[ "$kit" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || return 2
    _crew_kit_version "$kit" "$self" || return 1
    printf 'CREW_KIT_STATE=%s\nCREW_KIT_V=%s\nCREW_KIT_PEERS=%s\n' \
      "$CREW_KIT_STATE" "$CREW_KIT_V" "$CREW_KIT_PEERS"
    if [[ -n "$repo" && -n "$CREW_KIT_V" ]]; then
      # shellcheck source=pack_commit_gate.sh
      . "$_CKV_LIB_DIR/pack_commit_gate.sh" || return 1
      tag="$(pack_d1_tag_ref "$kit" "$CREW_KIT_V")"
      printf 'CREW_KIT_TAG=%s\n' "$tag"
      if git -C "$repo" rev-parse -q --verify "refs/tags/$tag" >/dev/null 2>&1; then
        echo "CREW_KIT_TAG_PRESENT=1"
      else
        echo "CREW_KIT_TAG_PRESENT=0"
      fi
    fi
    return 0
  }
  _ckv_main "$@"
  _ckv_rc=$?
  [[ "$_ckv_rc" -ne 2 ]] || echo "usage: crew_kit_version.sh <kit> [--self <root>] [--tag-repo <git dir>]" >&2
  exit "$_ckv_rc"
fi

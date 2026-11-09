#!/bin/bash
# crew_leave.sh -- DGN-1543 R8: crew membership's way back out.
#
# The join side landed in DGN-1509 (physical: pack_install.sh symlink +
# DOGANY_PACKS; administrative: update_seed.sh crew.conf row). This is its
# mirror: the ADMINISTRATIVE unjoin only. Predicate, refusal lanes, backup
# convention and the never-touched list live in lib/crew_leave.sh (the same
# implementation mint_run.sh teardown calls, so the manual verb and the
# teardown wiring can never disagree).
#
# Verbs:
#   scan  [--kit <kit>]
#         Detect ghost members: rows whose root no longer holds ANY physical
#         membership signal (dead path, or symlink+DOGANY_PACKS both gone).
#         READ-ONLY -- detection reports, it never removes (DGN-1543 R8
#         item 4). rc 0 = clean, rc 10 = findings, rc 1 = suspect registry.
#   leave --root <dir> [--kit <kit>] [--tag <tag>] [--dry-run]
#         Remove member=<root> rows (every kit registering the root, or one
#         kit with --kit) AFTER backing crew.conf up
#         (crew.conf.bak-leave-<tag>-<ts>, the tree's existing .bak-<ts>
#         family). Refuses a FULL physical member (rc 3: teardown first),
#         refuses DRIFT (rc 3: one signal survives -- not this verb's call),
#         refuses while the kit's crew lease is held (rc 1, update_seed M3
#         rule). Removing an unregistered root is a no-op, rc 0 (idempotent,
#         like the seed it mirrors). Never deletes the shared kit DB, the
#         crew dir, or anything inside the instance root.
#
# Env: DOGANY_SHARED_HOME / DOGANY_CREW_DIR (layout override, same
# resolution as crew_consent.sh / update_seed.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY_RESOLVE_LIB="$SCRIPT_DIR/lib/py_resolve.sh"
[[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
  || { echo "ERROR: python resolver library missing ($PY_RESOLVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/py_resolve.sh
. "$PY_RESOLVE_LIB"
pack_resolve_py3 || exit 1

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"

LEAVE_LIB="$SCRIPT_DIR/lib/crew_leave.sh"
[[ -f "$LEAVE_LIB" && -r "$LEAVE_LIB" ]] \
  || { echo "ERROR: leave library missing ($LEAVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/crew_leave.sh
. "$LEAVE_LIB"

usage() {
  echo "usage: crew_leave.sh scan [--kit <kit>]" >&2
  echo "       crew_leave.sh leave --root <dir> [--kit <kit>] [--tag <tag>] [--dry-run]" >&2
  exit 2
}

VERB="${1:-}"; shift || true
KIT="" ROOT_OPT="" TAG="" DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kit)  KIT="${2:-}";      shift 2 || usage ;;
    --root) ROOT_OPT="${2:-}"; shift 2 || usage ;;
    --tag)  TAG="${2:-}";      shift 2 || usage ;;
    --dry-run|--dry) DRY=1; shift ;;
    *) usage ;;
  esac
done
if [[ -n "$KIT" ]]; then
  case "$KIT" in
    */*|.*) echo "ERROR: kit name '$KIT' is not a bare kit id" >&2; exit 2 ;;
  esac
fi

case "$VERB" in
  scan)
    _crew_leave_scan "$KIT"
    ;;
  leave)
    [[ -n "$ROOT_OPT" ]] || usage
    [[ -n "$TAG" ]] || TAG="$(basename "$ROOT_OPT")"
    MATCHES="$(_crew_leave_matches "$ROOT_OPT" "$KIT")"
    if [[ -z "$MATCHES" ]]; then
      echo "[crew-leave] $ROOT_OPT is not a registered member of any crew${KIT:+ (kit $KIT)} -- nothing to leave (no-op)"
      exit 0
    fi
    RC=0
    while IFS= read -r row; do
      [[ -n "$row" ]] || continue
      kit="${row%% *}"; stored="${row#* }"
      one_rc=0
      _crew_leave_one "$kit" "$stored" "$TAG" "$DRY" || one_rc=$?
      if [[ "$one_rc" -eq 1 ]]; then RC=1
      elif [[ "$one_rc" -eq 3 && "$RC" -ne 1 ]]; then RC=3
      fi
    done <<< "$MATCHES"
    exit "$RC"
    ;;
  *) usage ;;
esac

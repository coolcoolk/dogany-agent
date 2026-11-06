# update-defer.sh -- shared update-door deferral policy (DGN-2023).
# Source after scripts/pack/lib/plist_label.sh. Both update.sh (including
# self-update) and update_apply.sh use this step. Root routines/ is a symlink
# to the template, so there is only one shipped implementation.

# update_defer_is_deferred <root> <plist-basename>
update_defer_is_deferred() {
  local root="$1" base="$2" line
  [[ -f "$root/routines/plists.defer" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line//[[:space:]]/}"
    [[ "$line" == "$base" ]] && return 0
  done < "$root/routines/plists.defer"
  return 1
}

# _update_defer_sandboxed <root> <launch-agents-dir> -- rc 0 = sandbox.
# DGN-1718 (drive-setup.sh / watchdog_setup.sh rule): a fake HOME, a launchd
# dir that is not this user's real ~/Library/LaunchAgents, or a temp instance
# root never reaches the live gui domain. An explicit seam
# (ROUTINE_CTL_LAUNCHCTL stub, ROUTINE_CTL_NO_LOAD=1) is a deliberate test
# harness and is honored instead.
_update_defer_sandboxed() {
  local root="$1" la="$2" u h t
  [[ -n "${ROUTINE_CTL_LAUNCHCTL:-}" || "${ROUTINE_CTL_NO_LOAD:-0}" == 1 ]] && return 1
  u="$(id -un 2>/dev/null)" || return 0
  case "$u" in ''|*[!A-Za-z0-9._-]*) return 0 ;; esac
  h="$(eval "printf '%s' ~$u")"
  case "$h" in /*) ;; *) return 0 ;; esac
  [[ "$( (cd "$la" 2>/dev/null && pwd -P) || printf '%s' "${la%/}")" \
     == "$( (cd "$h/Library/LaunchAgents" 2>/dev/null && pwd -P) || printf '%s' "$h/Library/LaunchAgents")" ]] || return 0
  [[ "$HOME" == "$h" ]] || return 0
  for t in /tmp /private/tmp /var/folders /private/var/folders "${TMPDIR:-}"; do
    [[ -n "$t" ]] || continue
    t="${t%/}"
    case "$root/" in "$t"/*) return 0 ;; esac
  done
  return 1
}

# Shared read-only policy and temporary-stop receipts. A deferred definition
# is not authority to disable a unit. Only the apply envelope stops routines;
# it captures activation before stopping and restores after payload success.
_UPDATE_DEFER_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/update_defer.py"
_update_defer_run() {
  local action="$1" root="$2" capture="${3:-}" la
  [[ -n "$capture" || "${ROUTINE_CTL_OS:-$(uname -s)}" == Darwin ]] || return 0
  if [[ "$action" == inspect && ! -f "$root/routines/plists.defer" ]]; then
    return 0
  fi
  if [[ -n "$capture" ]]; then
    # Rehearsal never queries the scheduler or writes an activation receipt.
    if [[ "$action" == inspect ]]; then
      python3 "$_UPDATE_DEFER_PY" plan "$root" >> "$capture"
    else
      printf 'update-defer %s previously activated routines (root %s)\n' "$action" "$root" >> "$capture"
    fi
    # A restore can run from update_apply.sh's EXIT recovery after TERM.
    # Do not inherit the interrupted shell's pending status: the capture is
    # the complete rehearsal operation and a successful write is success.
    return 0
  fi
  local needed=0
  python3 "$_UPDATE_DEFER_PY" needed "$root" || needed=$?
  [[ "$needed" != 3 ]] || return 0
  [[ "$needed" == 0 ]] || return "$needed"
  la="${DOGANY_LAUNCH_AGENTS_DIR:-${ROUTINE_CTL_LA_DIR:-$HOME/Library/LaunchAgents}}"
  if _update_defer_sandboxed "$root" "$la" || [[ "${ROUTINE_CTL_NO_LOAD:-0}" == 1 ]]; then
    echo "[update-defer] sandbox: activation unchanged and unverified" >&2
    return 5
  fi
  python3 "$_UPDATE_DEFER_PY" "$action" "$root"
}

update_defer_units() { _update_defer_run inspect "$@"; }
update_defer_snapshot() { _update_defer_run snapshot "$@"; }
update_defer_restore() { _update_defer_run restore "$@"; }
update_defer_was_active() {
  python3 "$_UPDATE_DEFER_PY" active "$1" "$2"
}

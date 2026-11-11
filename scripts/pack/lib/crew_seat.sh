# crew_seat.sh -- DGN-2040 crew seats: the shell side of seat-of resolution.
# Sourced (no shebang -- a library, not a runnable script). The Python twin
# is crew_paths.py seat_owner(); the two must give the same answer.
#
# A seat is a one-kit member root at $SHARED_HOME/seats/<kit>/<leader-name>
# whose .instance.conf carries DOGANY_SEAT_OF=<leader root>; the leader root
# carries DOGANY_CREW_ROLE=leader. Both keys are instance properties written
# only by crew_lead.sh and never derived from an agent pack or kit id (dec-400).
# Parse rule (the pack_install.sh block (1)/(2) rule): last value wins,
# quotes stripped; the role is lowercased. An empty last value is unset.
#
# Seats live OUTSIDE every instance root (N16: mint.sh rewrites every text
# file under PROJECT_ROOT) and outside every discovery glob (N15).

: "${SHARED_HOME:=${DOGANY_SHARED_HOME:-$HOME/.dogany}}"

# _seat_conf_last <conf-file> <KEY> -- last KEY= value, quotes stripped.
_seat_conf_last() {
  sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -n1 | tr -d "\"'"
}

# _seat_phys <dir> -- physical path, empty when unresolvable.
_seat_phys() {
  [[ -n "$1" && -d "$1" ]] || { printf ''; return 0; }
  (cd -P "$1" 2>/dev/null && pwd) || true
}

# _crew_role <root> -- "leader" or ''.
_crew_role() {
  _seat_conf_last "$1/.instance.conf" DOGANY_CREW_ROLE | tr '[:upper:]' '[:lower:]'
}

# _seat_of <root> -- the root's DOGANY_SEAT_OF value ('' when not a seat).
_seat_of() {
  _seat_conf_last "$1/.instance.conf" DOGANY_SEAT_OF
}

# _seat_owner <root> -- the leader root that owns this seat, or <root>
# itself. A seat whose owner is gone or is not a leader resolves to itself
# (a dead leader falls back to the DGN-1745 rule, M1).
_seat_owner() {
  local root="$1" owner
  owner="$(_seat_of "$root")"
  if [[ -n "$owner" && -d "$owner" && "$(_crew_role "$owner")" == "leader" ]]; then
    printf '%s' "$owner"
  else
    printf '%s' "$root"
  fi
}

# _seat_path <kit> <leader-root> -- where that leader's seat of <kit> lives.
_seat_path() {
  printf '%s/seats/%s/%s' "$SHARED_HOME" "$1" "$(basename "$2")"
}

# _seats_of_leader <leader-root> -- "<kit> <seat-root>" per seat this leader
# owns (DOGANY_SEAT_OF resolves to the same physical root). Keyed on the
# conf, not on nesting (N16).
_seats_of_leader() {
  local lphys d kit owner
  lphys="$(_seat_phys "$1")"
  [[ -n "$lphys" ]] || return 0
  for d in "$SHARED_HOME/seats"/*/"$(basename "$1")"; do
    [[ -d "$d" && -f "$d/.instance.conf" ]] || continue
    kit="$(basename "$(dirname "$d")")"
    [[ "$kit" == "_archive" ]] && continue
    owner="$(_seat_of "$d")"
    [[ -n "$owner" && "$(_seat_phys "$owner")" == "$lphys" ]] || continue
    printf '%s %s\n' "$kit" "$d"
  done
}

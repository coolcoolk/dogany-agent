#!/bin/bash
# tz_offset.sh -- DGN-1704: the ONE resolver for an instance's UTC offset.
#
# Resolution order (owner direction 2026-09-25 09:48, "크루 기본값으로"):
#   1. explicit per-agent override   config/agent.conf AGENT_TZ_OFFSET_HOURS
#   2. crew value                    <crews home>/crew.conf TZ_OFFSET_HOURS
#   3. host offset                   this machine's localtime, whole hours
#
# The crew value lives in the crews-home ROOT crew.conf
# (${DOGANY_CREW_DIR:-${DOGANY_SHARED_HOME:-$HOME/.dogany}/crews}/crew.conf),
# one per host estate, NOT in the per-kit <kit>/crew.conf registries: those
# are fail-closed member=<root> lists and every parser rejects any other
# line.  The root file is written once by scripts/pack/update_seed.sh when a
# crew is first seeded (founder's declared value, else host); it is never
# overwritten by the seed after that.
#
# Values are signed whole hours in [-12, 14]; kit readers parse integers
# only.  An out-of-range/non-integer value at a layer is ignored (loud on
# stderr) and resolution falls through to the next layer.  A fractional
# host zone has no reader-compatible form: host resolution fails (rc 2).
#
# Source it:   . "$AGENT_DIR/routines/lib/tz_offset.sh"
#              off="$(tz_offset_resolve "$AGENT_DIR")"
# or run it:   bash routines/lib/tz_offset.sh resolve [--source] <instance-root>
#              bash routines/lib/tz_offset.sh crew|host|crew-conf
# Pure bash + python3 (host offset only).  English/ASCII only.

tz_offset_valid() {
  [[ "${1:-}" =~ ^-?[0-9]{1,2}$ ]] || return 1
  local n=$((10#${1#-}))
  [[ "$1" == -* ]] && n=$((-n))
  (( n >= -12 && n <= 14 ))
}

# Normalize "+09" / "09" / "-05" to a plain signed integer string.
_tz_offset_norm() {
  local v="${1:-}" sign=""
  v="${v//[[:space:]]/}"; v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
  case "$v" in +*) v="${v#+}" ;; -*) sign="-"; v="${v#-}" ;; esac
  [[ "$v" =~ ^[0-9]+$ ]] || { printf '%s%s' "$sign" "$v"; return; }
  v=$((10#$v))
  [[ "$v" == 0 ]] && sign=""
  printf '%s%s' "$sign" "$v"
}

# _tz_offset_conf_get <file> <key> -- first KEY= line, normalized; "" if absent.
_tz_offset_conf_get() {
  [[ -f "$1" ]] || return 0
  local line
  line="$(grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null | head -1)" || true
  [[ -n "$line" ]] || return 0
  _tz_offset_norm "${line#*=}"
}

tz_offset_crew_conf() {
  printf '%s/crew.conf\n' "${DOGANY_CREW_DIR:-${DOGANY_SHARED_HOME:-$HOME/.dogany}/crews}"
}

# tz_offset_agent <instance-root> -- the explicit per-agent override, or "".
tz_offset_agent() {
  local v
  v="$(_tz_offset_conf_get "${1:-.}/config/agent.conf" AGENT_TZ_OFFSET_HOURS)"
  [[ -n "$v" ]] || return 0
  if tz_offset_valid "$v"; then printf '%s\n' "$v"
  else echo "[tz_offset] WARN ${1:-.}/config/agent.conf AGENT_TZ_OFFSET_HOURS='$v' is not a whole hour in [-12,14]; ignored" >&2
  fi
}

# tz_offset_crew -- the crew value, or "" (no crew / undeclared / invalid).
tz_offset_crew() {
  local conf v
  conf="$(tz_offset_crew_conf)"
  v="$(_tz_offset_conf_get "$conf" TZ_OFFSET_HOURS)"
  [[ -n "$v" ]] || return 0
  if tz_offset_valid "$v"; then printf '%s\n' "$v"
  else echo "[tz_offset] WARN $conf TZ_OFFSET_HOURS='$v' is not a whole hour in [-12,14]; ignored" >&2
  fi
}

# tz_offset_host -- this machine's whole-hour offset; rc 2 when fractional.
tz_offset_host() {
  python3 - <<'PY'
import sys, time
hours = time.localtime().tm_gmtoff / 3600
if not hours.is_integer():
    print("[tz_offset] ERROR host UTC offset %s hours is fractional; AGENT_TZ_OFFSET_HOURS readers accept whole hours only" % hours, file=sys.stderr)
    sys.exit(2)
print(int(hours))
PY
}

# tz_offset_resolve [--source] <instance-root>
#   prints "<offset>" (or "<offset> <agent|crew|host>" with --source).
tz_offset_resolve() {
  local with_src=0 v src
  if [[ "${1:-}" == "--source" ]]; then with_src=1; shift; fi
  v="$(tz_offset_agent "${1:-.}")"; src=agent
  if [[ -z "$v" ]]; then v="$(tz_offset_crew)"; src=crew; fi
  if [[ -z "$v" ]]; then v="$(tz_offset_host)" || return $?; src=host; fi
  if [[ "$with_src" == 1 ]]; then printf '%s %s\n' "$v" "$src"; else printf '%s\n' "$v"; fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-}" in
    resolve)   shift; tz_offset_resolve "$@" ;;
    crew)      tz_offset_crew ;;
    host)      tz_offset_host ;;
    crew-conf) tz_offset_crew_conf ;;
    *) echo "usage: tz_offset.sh resolve [--source] <instance-root> | crew | host | crew-conf" >&2; exit 64 ;;
  esac
fi

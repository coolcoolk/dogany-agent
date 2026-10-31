# module_gate.sh -- launchd units gated on an instance module flag (DGN-1875).
#
# Some routine units only do work when a kit module flag is on: mirror-poll.sh
# and mirror-reconcile.sh exit 0 unless MIRROR_MODULE=on in the kit conf. On
# an instance with the flag OFF, loading those units is "declared off but
# still scheduled", and every update's stop/start cycle (update_apply.sh
# _launchd_stop -> mint_run.sh start) re-loaded them and undid the owner's
# local unschedule (observed 2026-10-05 17:42).
#
# Scope: only the mirror pair. No other unit is flag-gated today; a new
# gated unit is one more case arm in module_gate_flag below.
#
# Flag semantics (deliberately narrower than the scripts' own gate):
#   value 'on'                     -> load (unchanged)
#   key present, any other value   -> module OFF: do not load
#   conf absent / key absent       -> load (unchanged). An unreadable or
#     missing flag is not an owner opt-out; dropping a schedule on a
#     resolution miss is the riskier failure, and the script still no-ops.
#
# Conf resolution replicates mirror-poll.sh exactly: config/lifekit.conf,
# overridden by life_engine_conf_path when the instance ships the resolver;
# last MIRROR_MODULE= line, whitespace stripped, lowercased.

# module_gate_flag <plist-basename> -- print the flag gating this unit, or
# nothing for an ungated unit.
module_gate_flag() {
  case "$1" in
    *.mirror-poll.plist|*.mirror-reconcile.plist) printf 'MIRROR_MODULE' ;;
  esac
  return 0
}

# module_gate_value <root> <flag> -- print the flag's resolved value
# (lowercased), or nothing when the conf or the key is absent.
module_gate_value() {
  local root="$1" flag="$2" conf le_conf
  conf="$root/config/lifekit.conf"
  if [[ -f "$root/routines/lib/life-engine.sh" ]]; then
    # Subshell: never source an instance lib into the caller's namespace.
    le_conf="$( { . "$root/routines/lib/life-engine.sh" >/dev/null 2>&1 \
                  && life_engine_conf_path "$root"; } 2>/dev/null || true)"
    [[ -n "$le_conf" ]] && conf="$le_conf"
  fi
  [[ -f "$conf" ]] || return 0
  { grep "^${flag}=" "$conf" 2>/dev/null || true; } \
    | tail -1 | cut -d= -f2- | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'
  return 0
}

# module_gate_off <root> <plist-basename> -- rc 0 when the unit is gated and
# its flag is explicitly set to something other than 'on'; prints
# "<FLAG>=<value>" for the caller's log line. rc 1 otherwise.
module_gate_off() {
  local root="$1" base="$2" flag val
  flag="$(module_gate_flag "$base")"
  [[ -n "$flag" ]] || return 1
  val="$(module_gate_value "$root" "$flag")"
  [[ -n "$val" && "$val" != "on" ]] || return 1
  printf '%s=%s' "$flag" "$val"
  return 0
}

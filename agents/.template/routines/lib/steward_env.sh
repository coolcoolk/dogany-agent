# steward_env.sh -- the bash side of the STEWARD_ENV_FILE contract (DGN-1811).
#
# STEWARD_ENV_FILE (config/agent.conf, DGN-1585 S2) names the operator's own
# .telegram_bot/.env; push.sh --audience operator resolves through it. Two
# things live here so no caller hand-rolls them:
#
#   steward_conf_value <conf> <key>
#       KEY=value lookup with the SAME normalization as the python parser
#       routines/lib/conf_reader.py conf_get (and bridge/machine_gate.py
#       steward_env_file): first line whose whitespace-stripped form starts
#       with "<key>=" wins; the value is whitespace-trimmed, then surrounding
#       double quotes, then surrounding single quotes are stripped. Missing
#       file or key -> "". Pinned equal by
#       routines/tests/test-steward-env-parity.sh.
#
#   steward_env_estate_default <instance_root>
#       The operator .env an instance in this estate should name, or "" when
#       there is none. The operator is the designated machine steward
#       (DGN-1900 marker: $DOGANY_MACHINE_STEWARD_FILE, else
#       ${DOGANY_SHARED_HOME:-~/.dogany}/machine-steward; first non-comment
#       line = the steward's instance root). "" when: no marker (a plain user
#       install), the root is missing, it is a PoC (<x>/poc/<name>), it is
#       the instance itself, or it carries no .telegram_bot/.env.
#
#   steward_env_stamp <instance_root>
#       Write STEWARD_ENV_FILE=<estate default> into <root>/config/agent.conf
#       when the conf has NO active STEWARD_ENV_FILE line. An active line --
#       even an empty one -- is an existing declaration and is never
#       rewritten. Prints one status line; returns 0 unless the write fails.
#
# Sourced by scripts/mint.sh and routines/push.sh. Pure bash; no set -e
# assumptions; never prints secrets (only paths).

steward_conf_value() { # <conf> <key>
  local conf="$1" key="$2" line v
  [[ -r "$conf" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ "$line" == "$key="* ]] || continue
    v="${line#*=}"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    while [[ "$v" == \"* ]]; do v="${v#\"}"; done
    while [[ "$v" == *\" ]]; do v="${v%\"}"; done
    while [[ "$v" == \'* ]]; do v="${v#\'}"; done
    while [[ "$v" == *\' ]]; do v="${v%\'}"; done
    printf '%s' "$v"
    return 0
  done < "$conf"
  return 0
}

steward_env_estate_default() { # <instance_root>
  local me sfile sroot line
  me="$(cd -P "$1" 2>/dev/null && pwd)" || return 0
  sfile="${DOGANY_MACHINE_STEWARD_FILE:-${DOGANY_SHARED_HOME:-$HOME/.dogany}/machine-steward}"
  [[ -r "$sfile" ]] || return 0
  sroot=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    sroot="$line"; break
  done < "$sfile"
  [[ -n "$sroot" && -d "$sroot" ]] || return 0
  sroot="$(cd -P "$sroot" 2>/dev/null && pwd)" || return 0
  [[ "$sroot" == "$me" ]] && return 0
  [[ "$(basename "$(dirname "$sroot")" | tr '[:upper:]' '[:lower:]')" == "poc" ]] && return 0
  [[ -f "$sroot/.telegram_bot/.env" ]] || return 0
  printf '%s' "$sroot/.telegram_bot/.env"
}

steward_env_stamp() { # <instance_root>
  local root="$1" conf val
  conf="$root/config/agent.conf"
  if [[ ! -f "$conf" ]]; then
    echo "[steward-env] no config/agent.conf -> skip"
    return 0
  fi
  if grep -qE '^[[:space:]]*STEWARD_ENV_FILE=' "$conf"; then
    echo "[steward-env] STEWARD_ENV_FILE already declared -> keep"
    return 0
  fi
  val="$(steward_env_estate_default "$root")"
  if [[ -z "$val" ]]; then
    echo "[steward-env] no operator in this estate (no machine-steward marker resolves) -> not set; operator alarms fall back to the owner"
    return 0
  fi
  case "$val" in
    *[[:space:]\"\'\\\$\`]*)
      echo "[steward-env] operator .env path has characters agent.conf cannot carry safely -> not set: $val" >&2
      return 0 ;;
  esac
  {
    printf '\n# Operator alarm routing (DGN-1811): the machine steward'\''s .env,\n'
    printf '# stamped at mint from the machine-steward marker. Clear the value to\n'
    printf '# send operator alarms to the owner instead.\n'
    printf 'STEWARD_ENV_FILE=%s\n' "$val"
  } >> "$conf" || return 1
  echo "[steward-env] stamped STEWARD_ENV_FILE=$val"
}

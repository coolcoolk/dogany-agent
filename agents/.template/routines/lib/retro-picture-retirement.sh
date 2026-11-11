# dec-650: remove the 2.7 picture beat without changing owner preferences.
# Source after update-defer.sh; its sandbox check prevents live scheduler
# access from rehearsal installs. routine-ctl owns unloading and retirement.
retire_retro_picture() {
  local root="$1" name label definition la ctl rc=0
  name="$(sed -n 's/^DOGANY_AGENT_NAME=//p' "$root/.instance.conf" | head -n1)"
  case "$name" in ''|*[!a-z0-9-]*)
    echo "[update] picture retirement: invalid instance name" >&2; return 1 ;;
  esac
  label="com.telegram-skill-bot.${name}.retro-slot"
  definition="$root/routines/${label}.plist"
  la="${DOGANY_LAUNCH_AGENTS_DIR:-${ROUTINE_CTL_LA_DIR:-$HOME/Library/LaunchAgents}}"
  ctl="$root/routines/lib/routine-ctl.sh"
  if _update_defer_sandboxed "$root" "$la"; then
    return 5
  fi
  bash "$ctl" status retro-slot || rc=$?
  case "$rc" in 0|3) : ;; *) return 1 ;; esac
  if [[ "$rc" == 0 || -e "$definition" || -e "$la/${label}.plist" \
        || -e "${ROUTINE_CTL_UNIT_DIR:-$HOME/.config/systemd/user}/dogany-${name}-retro-slot.timer" ]]; then
    ROUTINE_CTL_LA_DIR="$la" bash "$ctl" disable retro-slot || return 1
    # The generic start scanner must never rediscover the retired beat.
    rm -f "$definition" || return 1
  fi
}

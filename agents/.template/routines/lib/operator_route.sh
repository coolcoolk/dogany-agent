#!/bin/bash
# Resolve the resident operator for one instance.  Output is one tab-separated
# row: "steward <env>" or "session <inbox>".  A missing steward is a normal
# standalone-user shape, never permission to address the owner.
operator_route() {
  local root="$1" conf="$2" candidate="${3:-}" env="" kind="" marker="" steward_root=""
  if command -v steward_conf_value >/dev/null 2>&1; then
    env="$(steward_conf_value "$conf" STEWARD_ENV_FILE)"
  fi
  if operator_destination "$env"; then
    printf 'steward\t%s\n' "$env"
    return
  fi
  if [[ -n "$env" && -f "$env" ]]; then
    printf 'invalid\t-\n'
    return
  fi
  # Reuse the estate resolver; a marker naming a directory is not a route.
  env="$(steward_env_estate_default "$root" 2>/dev/null)"
  if operator_destination "$env"; then
    printf 'steward\t%s\n' "$env"
    return
  fi
  # The estate stamping helper excludes self; a maintainer's own valid
  # channel is still an operator destination when the marker names self.
  marker="${DOGANY_MACHINE_STEWARD_FILE:-${DOGANY_SHARED_HOME:-$HOME/.dogany}/machine-steward}"
  steward_root="$(grep -v '^[[:space:]]*#' "$marker" 2>/dev/null | grep -m1 '[^[:space:]]' | tr -d '[:space:]' || true)"
  if [[ -d "$steward_root" && "$(cd -P "$steward_root" && pwd)" == "$(cd -P "$root" && pwd)" ]] \
     && operator_destination "$root/.telegram_bot/.env"; then
    printf 'steward\t%s/.telegram_bot/.env\n' "$root"
    return
  fi
  kind="$(steward_conf_value "$conf" OWNER_KIND 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  if [[ "$kind" == operator ]] && operator_destination "$candidate"; then
    printf 'owner\t-\n'
    return
  fi
  printf 'session\t%s/.telegram_bot/session-inbox\n' "$root"
}

operator_destination() {
  local env="$1" token chat root
  [[ -r "$env" ]] || return 1
  token="$(steward_conf_value "$env" TELEGRAM_BOT_TOKEN)"
  [[ -n "$token" && "$token" != "your_bot_token_here" ]] || return 1
  chat="$(steward_conf_value "$env" ALLOWED_USER_IDS)"
  chat="${chat%%,*}"
  if [[ -z "$chat" ]]; then
    root="$(dirname "$(dirname "$env")")"
    [[ -f "$root/bridge/ownership.py" ]] || return 1
    chat="$("${BRIDGE_PYTHON:-python3}" -B - "$root" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, sys.argv[1] + "/bridge")
from ownership import read_owner_lock
print(read_owner_lock(sys.argv[1] + "/.telegram_bot") or "")
PY
)"
  fi
  [[ "$chat" =~ ^-?[0-9]+$ ]]
}

operator_queue_session() {
  local inbox="$1" text="$2" name tmp
  name="operator-push-$(date +%s)-$$.md"
  tmp="$inbox/.$name.tmp"
  mkdir -p "$inbox" 2>/dev/null || return 1
  printf '[operator-push]\n\n%s\n\nAgent instruction: machinery notice; inspect and act if needed. Reply NO_PUSH unless the owner must act.\n' \
    "$text" > "$tmp" 2>/dev/null || return 1
  mv "$tmp" "$inbox/$name" 2>/dev/null
}

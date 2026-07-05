#!/bin/bash
# bridge/watchdog_setup.sh -- idempotent registration of the polling watchdog
# (DGN-140, layer 2). Called from install.sh (auto service mode) and update.sh.
#
# NON-FATAL CONTRACT: this script never exits nonzero on registration failure.
# The bridge install/update must succeed even when the watchdog cannot be
# registered; failures are warned with the manual command to run.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

info() { echo "[watchdog-setup] $*"; }
warn() { echo "[watchdog-setup][WARN] $*" >&2; }

# Read the launchd Label key from a plist (mirrors install.sh plist_label:
# plutil, then PlistBuddy, then a grep fallback; empty on failure).
plist_label() {
  local plist="$1" label=""
  [ -f "$plist" ] || return 0
  if command -v plutil >/dev/null 2>&1; then
    label="$(plutil -extract Label raw -o - "$plist" 2>/dev/null || true)"
  fi
  if [ -z "$label" ] && [ -x /usr/libexec/PlistBuddy ]; then
    label="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist" 2>/dev/null || true)"
  fi
  if [ -z "$label" ]; then
    label="$(grep -A1 '<key>Label</key>' "$plist" 2>/dev/null \
             | grep '<string>' | head -n1 \
             | sed -E 's#.*<string>(.*)</string>.*#\1#')"
  fi
  printf '%s' "$label"
}

setup_macos() {
  local src="" label dest
  for p in "$SCRIPT_DIR"/*.watchdog.plist; do
    [ -e "$p" ] || continue
    src="$p"
    break
  done
  if [ -z "$src" ]; then
    warn "no *.watchdog.plist found in $SCRIPT_DIR, skipping registration"
    return 0
  fi
  # GRILL FIX: never register a plist still carrying mint placeholders --
  # the label/paths would be literal __AGENT_NAME__/__PROJECT_ROOT__ junk.
  if grep -qE '__(AGENT_NAME|PROJECT_ROOT|HOME)__' "$src"; then
    warn "unsubstituted placeholders in $src, skipping registration"
    return 0
  fi
  label="$(plist_label "$src")"
  [ -n "$label" ] || label="$(basename "$src" .plist)"
  case "$label" in
    *__*) warn "unsubstituted placeholders in label ($label), skipping registration"; return 0 ;;
  esac
  dest="$HOME/Library/LaunchAgents/$(basename "$src")"
  mkdir -p "$HOME/Library/LaunchAgents"
  cp -p "$src" "$dest"
  # Idempotent re-register: bootout an existing instance first (may not exist).
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dest" 2>/dev/null \
    || launchctl load "$dest" 2>/dev/null \
    || warn "bootstrap/load reported an error for $label"
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    info "watchdog registered: $label (every 2 min)"
  else
    warn "could not verify watchdog registration: $label"
    warn "register manually: launchctl bootstrap gui/$(id -u) \"$dest\""
  fi
  return 0
}

setup_linux() {
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found, skipping watchdog registration"
    return 0
  fi
  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/dogany-watchdog.service" <<UNIT
[Unit]
Description=Dogany bridge polling watchdog

[Service]
Type=oneshot
ExecStart=/bin/bash $PROJECT_ROOT/bridge/watchdog.sh --unit dogany-agent.service
UNIT
  cat > "$unit_dir/dogany-watchdog.timer" <<UNIT
[Unit]
Description=Run the Dogany bridge polling watchdog every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now dogany-watchdog.timer 2>/dev/null \
    || warn "enable dogany-watchdog.timer reported an error"
  if systemctl --user is-active --quiet dogany-watchdog.timer 2>/dev/null; then
    info "watchdog timer registered: dogany-watchdog.timer (every 2 min)"
  else
    warn "could not verify dogany-watchdog.timer is active"
    warn "enable manually: systemctl --user enable --now dogany-watchdog.timer"
  fi
  return 0
}

case "$(uname -s)" in
  Darwin) setup_macos ;;
  *)      setup_linux ;;
esac
exit 0

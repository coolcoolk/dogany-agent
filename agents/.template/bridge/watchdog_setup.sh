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
DATA_DIR="$PROJECT_ROOT/.telegram_bot"
# DGN-888: marker declaring the monitored bridge service plist for watchdog.sh's
# vanished-label recovery backstop. Format: line 1 = plist absolute path,
# line 2 = launchd Label. watchdog.sh trusts ONLY this marker (never guesses a
# label -> path mapping); no marker = no recovery (fresh install / manual mode).
SERVICE_PLIST_MARKER="$DATA_DIR/.service_plist"

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

# DGN-964: expected launchd bridge label for THIS instance, derived from the
# mint-time identity manifest (.instance.conf DOGANY_AGENT_NAME -- same source
# self_restart.sh reads for DOGANY_FW_VERSION). Prints nothing when the
# manifest is absent (fresh template / manual install) or the name still
# carries mint placeholders; callers then fall back to first-glob order.
expected_bridge_label() {
  local name
  name="$(sed -n 's/^DOGANY_AGENT_NAME=//p' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -n1)"
  [ -n "$name" ] || return 0
  case "$name" in *__*) return 0 ;; esac
  printf 'com.telegram-skill-bot.%s' "$name"
}

# DGN-964: deterministic plist pick when bridge/ holds more than one
# candidate. Legacy plists from a renamed agent survive on disk and can sort
# FIRST (2026-08-21 Skull incident: first-glob picked the stale
# dogany-smith.* plists, registered the wrong watchdog label AND bailed out
# of write_service_marker, leaving the DGN-888 backstop inert). Args:
# $1 = expected Label ('' = unknown), $2.. = candidate plist paths.
# Rule: one candidate -> take it. Multiple -> WARN (never silent), prefer the
# candidate whose Label key equals the expected label; expected label unknown
# or unmatched -> first-glob fallback (pre-DGN-964 behavior) with a WARN
# naming the winner so a stale pick is at least visible. Diagnostics go to
# stderr only (warn) -- stdout is the picked path (command substitution).
pick_plist() {
  local expected="$1" first cand label
  shift
  [ $# -ge 1 ] || return 0
  first="$1"
  if [ $# -eq 1 ]; then
    printf '%s' "$first"
    return 0
  fi
  warn "multiple candidate plists in $SCRIPT_DIR (legacy leftovers?): $*"
  if [ -n "$expected" ]; then
    for cand in "$@"; do
      label="$(plist_label "$cand")"
      if [ "$label" = "$expected" ]; then
        warn "picked $(basename "$cand") -- Label matches instance identity ($expected)"
        printf '%s' "$cand"
        return 0
      fi
    done
    warn "no candidate Label matches instance identity ($expected); falling back to first-glob pick: $(basename "$first")"
  else
    warn "instance identity unknown (.instance.conf DOGANY_AGENT_NAME unreadable); falling back to first-glob pick: $(basename "$first")"
  fi
  printf '%s' "$first"
}

# DGN-480: rewrite a macOS watchdog plist's baked absolute paths to THIS
# instance's current PROJECT_ROOT (derived at the top from BASH_SOURCE). The
# plist carries three path occurrences, all rooted at the mint-time PROJECT_ROOT:
#   - ProgramArguments: <root>/bridge/watchdog.sh
#   - StandardOutPath / StandardErrorPath: <root>/.telegram_bot/logs/watchdog_launchd.log
# We match on the stable path SUFFIXES (/bridge/watchdog.sh and the log tail) and
# replace the arbitrary prefix before them with the current PROJECT_ROOT. This is
# prefix-agnostic: it fixes a stale absolute path, an unexpected root, or a
# leftover __PROJECT_ROOT__ placeholder alike, and is a no-op when already correct
# (idempotent). Portable BSD/GNU sed via sed -i with a backup suffix, backup
# removed. NON-FATAL: a rewrite failure warns and leaves the copied plist as-is.
repoint_plist_paths() {
  local plist="$1"
  [ -f "$plist" ] || return 0
  # '#' sed delimiter avoids clashing with the '/' in the paths. Each pattern
  # matches one whole <string>...</string> element by its stable path suffix and
  # rewrites it wholesale to the current PROJECT_ROOT (no backrefs needed).
  if sed -i.bak \
      -e "s#<string>[^<]*/bridge/watchdog\.sh</string>#<string>${PROJECT_ROOT}/bridge/watchdog.sh</string>#" \
      -e "s#<string>[^<]*/\.telegram_bot/logs/watchdog_launchd\.log</string>#<string>${PROJECT_ROOT}/.telegram_bot/logs/watchdog_launchd.log</string>#" \
      "$plist" 2>/dev/null; then
    rm -f "$plist.bak"
  else
    warn "could not repoint plist paths in $plist (registering as copied)"
    rm -f "$plist.bak" 2>/dev/null || true
  fi
}

# DGN-888: declare the BRIDGE service plist (path + Label) that watchdog.sh
# monitors via --label, so the watchdog can re-register (enable + bootstrap)
# when a bootout leaves the label completely vanished from launchd. The bridge
# service is registered by install.sh (install_launchd) into
# $HOME/Library/LaunchAgents BEFORE this script runs; we only declare the
# registered artifact, never register it ourselves. NON-FATAL: any anomaly
# warns and skips -- a missing marker just means watchdog keeps its current
# "not registered -> skip" behavior (safe for fresh installs / manual mode).
write_service_marker() {
  local src="" dest label
  # The bridge service plist candidates are the non-watchdog *.plist files in
  # this directory. Note: install.sh install_launchd takes the FIRST *.plist
  # glob match with NO watchdog exclusion (it relies on alphabetical order,
  # newbridge < watchdog); this script excludes *.watchdog.plist explicitly
  # since it registers that one itself. DGN-964: with more than one candidate
  # (legacy plists of a renamed agent), pick_plist prefers the Label matching
  # this instance's identity instead of blind first-glob.
  local candidates=()
  for p in "$SCRIPT_DIR"/*.plist; do
    [ -e "$p" ] || continue
    case "$p" in *.watchdog.plist) continue ;; esac
    candidates+=("$p")
  done
  src="$(pick_plist "$(expected_bridge_label)" ${candidates[@]+"${candidates[@]}"})"
  if [ -z "$src" ]; then
    return 0
  fi
  dest="$HOME/Library/LaunchAgents/$(basename "$src")"
  if [ ! -f "$dest" ]; then
    warn "bridge service plist not installed ($dest), not writing service marker"
    return 0
  fi
  # Same placeholder guard as the watchdog registration above: never declare
  # a plist still carrying mint placeholders.
  if grep -qE '__(AGENT_NAME|PROJECT_ROOT|HOME)__' "$dest"; then
    warn "unsubstituted placeholders in $dest, not writing service marker"
    return 0
  fi
  label="$(plist_label "$dest")"
  if [ -z "$label" ]; then
    warn "cannot read Label from $dest, not writing service marker"
    return 0
  fi
  case "$label" in
    *__*) warn "unsubstituted placeholders in label ($label), not writing service marker"; return 0 ;;
  esac
  mkdir -p "$DATA_DIR"
  printf '%s\n%s\n' "$dest" "$label" > "$SERVICE_PLIST_MARKER"
  info "service plist marker written: $SERVICE_PLIST_MARKER ($label)"
  return 0
}

setup_macos() {
  local src="" label dest bridge_label
  # DGN-964: same deterministic pick as write_service_marker -- a legacy
  # *.watchdog.plist (renamed agent) can sort first and register a watchdog
  # for a label that no longer exists. Expected watchdog label = the bridge
  # label + ".watchdog" (mint plist convention).
  local candidates=()
  for p in "$SCRIPT_DIR"/*.watchdog.plist; do
    [ -e "$p" ] || continue
    candidates+=("$p")
  done
  bridge_label="$(expected_bridge_label)"
  src="$(pick_plist "${bridge_label:+$bridge_label.watchdog}" ${candidates[@]+"${candidates[@]}"})"
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
  # DGN-480: repoint the registered plist at THIS instance's current location.
  # The source plist's ProgramArguments/log paths were baked at mint time and
  # do NOT move when the instance directory is relocated -- a moved instance
  # would otherwise register a watchdog that runs the OLD (dead) watchdog.sh
  # and reads the OLD heartbeat, firing false-stall restarts on the live bot.
  # PROJECT_ROOT is derived from this script's own on-disk location (BASH_SOURCE
  # -> SCRIPT_DIR -> ..), so it is always the CURRENT root regardless of any
  # stale absolute path (or leftover placeholder) inside the plist. Rewriting to
  # the same value is a no-op, so this is idempotent on re-run. The launchd Label
  # is path-independent and is intentionally left untouched.
  repoint_plist_paths "$dest"
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
  # DGN-888: declare the bridge service plist for the vanished-label backstop.
  write_service_marker
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

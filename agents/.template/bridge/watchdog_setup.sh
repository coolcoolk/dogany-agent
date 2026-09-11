#!/bin/bash
# bridge/watchdog_setup.sh -- idempotent registration of the polling watchdog
# (DGN-140, layer 2). Called from install.sh (auto service mode) and update.sh.
#
# NON-FATAL CONTRACT (DGN-140), amended by DGN-1210: ordinary registration
# failures never exit nonzero -- the bridge install/update must succeed even
# when the watchdog cannot be registered; those are warned with the manual
# command to run. ONE exception: exit 3 = identity-conflict REFUSAL (this
# tree tried to register a label that is currently live at ANOTHER existing
# root -- the clone-hijack shape). Callers treat 3 as fatal-and-loud and
# every other outcome exactly as before.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$PROJECT_ROOT/.telegram_bot"

# DGN-1210: explicit relocation declaration. update.sh:5633 / install.sh:3537
# invoke this script with NO flags, so a clone's transitive self-update can
# never acquire --relocate; only a human retiring the old root passes it.
RELOCATE=0
for _arg in "$@"; do
  case "$_arg" in --relocate) RELOCATE=1 ;; esac
done
# DGN-888: marker declaring the monitored bridge service plist for watchdog.sh's
# vanished-label recovery backstop. Format: line 1 = plist absolute path,
# line 2 = launchd Label. watchdog.sh trusts ONLY this marker (never guesses a
# label -> path mapping); no marker = no recovery (fresh install / manual mode).
SERVICE_PLIST_MARKER="$DATA_DIR/.service_plist"

# Linux systemd unit names -- single source, configured, never forked per build.
# Defaults are the names an existing public install already has registered, so
# an update never leaves an orphan timer behind under a renamed unit. A
# distribution that wants its own names exports these two; nothing else in this
# script hardcodes a unit name.
# Resolution order per unit: explicit env > incumbent already registered on this
# host > default. The incumbent step means a host that was set up under either
# naming keeps that naming across updates in BOTH directions.
BRIDGE_UNIT="${DOGANY_BRIDGE_UNIT:-}"
WATCHDOG_UNIT="${DOGANY_WATCHDOG_UNIT:-}"

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
# FIRST (2026-08-21 incident: first-glob picked the stale
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

# DGN-1210-BEGIN identity gate -- a clone must not hijack a live label.
# A cloned tree (probe / backup copy) carries every in-tree credential of the
# original verbatim (.instance.conf, minted plists), so nothing INSIDE the
# tree can distinguish cloning from relocation. The one asymmetric fact lives
# OUTSIDE the tree: the label's CURRENT registration (incumbent). 2026-09-01
# live incident: a probe clone's self-update transitively re-ran this script,
# which re-registered the live watchdog label onto the clone path; the live
# watchdog then read the clone's bot-less heartbeat -> permanent stale ->
# 9 false-stall restarts of the live service over ~2h.
# Predicate (roots realpath-normalized -- symlinked roots must not read as
# clones of themselves):
#   incumbent absent                    -> allow (first mint)
#   incumbent == this root              -> allow (idempotent re-register)
#   incumbent != this root, path gone   -> allow + WARN (mv relocation)
#   incumbent != this root, path exists -> REFUSE, exit 3 (clone shape);
#                                          only an explicit --relocate
#                                          overrides.
# The refusal channel is the EXIT CODE plus a durable marker, not a notify:
# a clone's own alert channels live inside the clone and are exactly what its
# isolation disables (incident: dummy push token silenced all 9 alerts).

# Physical path of a directory; a missing dir prints back verbatim (callers
# handle the vanished-incumbent case before any equality could matter).
# No realpath/readlink -f: bash 3.2 / BSD userland contract (DGN-1202).
dgn1210_canon_dir() {
  (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"
}

# stdout: the /bridge/watchdog.sh path launchd currently RUNS for label $1,
# or nothing. Reads the loaded job (launchctl print), never the disk plist --
# the two can diverge and only the loaded one fires. Unreadable output ==
# no incumbent (a fresh install must stay registrable: fail-open on read).
dgn1210_incumbent_script_macos() {
  launchctl print "gui/$(id -u)/$1" 2>/dev/null \
    | sed -n 's#^[[:space:]]*\(/.*/bridge/watchdog\.sh\)$#\1#p' | head -n1
}

# Resolve the two unit names for THIS host. Explicit env wins; otherwise adopt
# whichever naming is already registered here; otherwise fall back to the
# published default. Adoption is what keeps an in-place update from orphaning
# the timer a host registered under the other naming.
resolve_linux_units() {
  local candidate
  if [ -z "$WATCHDOG_UNIT" ]; then
    for candidate in claude-code-telegram-watchdog dogany-watchdog; do
      if systemctl --user cat "$candidate.timer" >/dev/null 2>&1; then
        WATCHDOG_UNIT="$candidate"
        break
      fi
    done
    WATCHDOG_UNIT="${WATCHDOG_UNIT:-claude-code-telegram-watchdog}"
  fi
  if [ -z "$BRIDGE_UNIT" ]; then
    for candidate in claude-code-telegram.service dogany-agent.service; do
      if systemctl --user cat "$candidate" >/dev/null 2>&1; then
        BRIDGE_UNIT="$candidate"
        break
      fi
    done
    BRIDGE_UNIT="${BRIDGE_UNIT:-claude-code-telegram.service}"
  fi
}

# Linux mirror: the systemd unit name is one per host, so on a shared host ANY
# second instance -- clone or not -- collides; the same predicate refuses that
# instead of silently stealing the timer.
dgn1210_incumbent_script_linux() {
  systemctl --user show -p ExecStart "$WATCHDOG_UNIT.service" 2>/dev/null \
    | grep -oE '/[^ ;]*/bridge/watchdog\.sh' | head -n1
}

# dgn1210_gate <label> <incumbent-watchdog.sh-path-or-empty>
# rc 0 = allowed, rc 3 = identity-conflict refusal (caller must abort BEFORE
# any cp/repoint/bootout -- bootout erases the incumbent this gate reads).
dgn1210_gate() {
  local label="$1" inc_script="$2" inc_root me
  me="$(dgn1210_canon_dir "$PROJECT_ROOT")"
  [ -n "$inc_script" ] || return 0
  inc_root="$(dgn1210_canon_dir "${inc_script%/bridge/watchdog.sh}")"
  [ "$inc_root" = "$me" ] && return 0
  if [ ! -e "$inc_script" ]; then
    warn "DGN-1210: $label was registered at a vanished root ($inc_root); treating as relocation, re-registering here ($me)"
    return 0
  fi
  if [ "$RELOCATE" = "1" ]; then
    warn "DGN-1210: identity conflict on $label overridden by --relocate (incumbent $inc_root -> this root $me)"
    return 0
  fi
  warn "DGN-1210: REFUSING to register $label -- it is live at ANOTHER root that still exists."
  warn "  incumbent root: $inc_root"
  warn "  this root:      $me"
  warn "  this tree looks like a CLONE of the incumbent instance; registering would point the live label here and false-stall-restart the live service."
  warn "  real relocation (the old root is being retired): re-run bridge/watchdog_setup.sh --relocate"
  mkdir -p "$DATA_DIR" 2>/dev/null || true
  {
    printf 'refused_at=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || echo unknown)"
    printf 'label=%s\nincumbent_root=%s\nrefused_root=%s\n' "$label" "$inc_root" "$me"
  } > "$DATA_DIR/.watchdog_identity_refusal" 2>/dev/null || true
  return 3
}
# DGN-1210-END

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
  # DGN-1210: identity gate BEFORE cp/repoint/bootout -- the bootout below
  # would erase the very incumbent registration this gate reads.
  if ! dgn1210_gate "$label" "$(dgn1210_incumbent_script_macos "$label")"; then
    return 3
  fi
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
  resolve_linux_units
  # DGN-1210: identity gate BEFORE the unit files are rewritten -- writing
  # them would overwrite the very ExecStart the gate reads.
  if ! dgn1210_gate "$WATCHDOG_UNIT.service" "$(dgn1210_incumbent_script_linux)"; then
    return 3
  fi
  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  cat > "$unit_dir/$WATCHDOG_UNIT.service" <<UNIT
[Unit]
Description=Bridge polling watchdog

[Service]
Type=oneshot
ExecStart=/bin/bash $PROJECT_ROOT/bridge/watchdog.sh --unit $BRIDGE_UNIT
UNIT
  cat > "$unit_dir/$WATCHDOG_UNIT.timer" <<UNIT
[Unit]
Description=Run the bridge polling watchdog every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now "$WATCHDOG_UNIT.timer" 2>/dev/null \
    || warn "enable $WATCHDOG_UNIT.timer reported an error"
  if systemctl --user is-active --quiet "$WATCHDOG_UNIT.timer" 2>/dev/null; then
    info "watchdog timer registered: $WATCHDOG_UNIT.timer (every 2 min)"
  else
    warn "could not verify $WATCHDOG_UNIT.timer is active"
    warn "enable manually: systemctl --user enable --now $WATCHDOG_UNIT.timer"
  fi
  return 0
}

# DGN-1210: rc 3 (identity-conflict refusal) is the ONLY nonzero exit; every
# ordinary failure above still returns 0 (DGN-140 non-fatal contract).
rc=0
case "$(uname -s)" in
  Darwin) setup_macos || rc=$? ;;
  *)      setup_linux || rc=$? ;;
esac
exit "$rc"

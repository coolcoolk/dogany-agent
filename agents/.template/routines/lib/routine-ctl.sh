#!/bin/bash
# routine-ctl.sh -- idempotent, NON-conversational routine (un)scheduler.
#
# The automation primitive behind lifekit bundle activation (called by the
# dogany-lifekit-setup skill). The dogany-cron-register skill remains the
# CONVERSATIONAL path for ad-hoc user requests; this helper exists so that
# bundle activation does not have to hand-drive a multi-step interactive
# procedure (and it never sends a test-fire message).
#
# Usage:
#   routine-ctl.sh enable  <name> <script-path-rel-to-agent-root> <HH:MM> [weekday]
#   routine-ctl.sh disable <name>
#   routine-ctl.sh status  <name>          exit 0 = scheduled, 3 = not scheduled
#
# [weekday] (optional, additive): mon|tue|wed|thu|fri|sat|sun -- schedule the
# routine WEEKLY on that day instead of daily. Omitted = daily (legacy
# behavior unchanged).
#
# Idempotent: enable re-renders and reloads if already scheduled; disable of a
# non-scheduled routine is a no-op (exit 0).
#
# macOS -> launchd plist rendered from routines/bundle/routine.plist.tpl.
# Linux -> systemd --user .service + .timer (dogany-<agent>-<name>).
#
# DGN-1610: disable (macOS) also retires the health-observer marker for this
# label, AFTER launchctl bootout -- this is the sanctioned single-entry-point
# routine teardown; a manual launchctl removal must retire separately
# (health-observer.py --retire LABEL --why TEXT).
#
# Test/env overrides (never needed in normal operation):
#   ROUTINE_CTL_LA_DIR      override ~/Library/LaunchAgents (macOS)
#   ROUTINE_CTL_UNIT_DIR    override ~/.config/systemd/user (Linux)
#   ROUTINE_CTL_NO_LOAD=1   render + place files only, skip launchctl/systemctl
#   ROUTINE_CTL_LAUNCHCTL   override the launchctl binary (macOS)
#
# Exit: 0 ok / 1 usage or config error / 2 scheduler verification or observer
#       marker failed / 3 (status only) not scheduled
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
TPL="$AGENT_DIR/routines/bundle/routine.plist.tpl"

# Agent name from the instance manifest (mint.sh writes it); fallback = dirname.
# `|| true` guard: set -e + pipefail would otherwise kill the script here when
# .instance.conf is absent (grep exits 2), making the fallback unreachable.
AGENT_NAME="$(grep -E '^DOGANY_AGENT_NAME=' "$AGENT_DIR/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
AGENT_NAME="${AGENT_NAME:-$(basename "$AGENT_DIR")}"

CMD="${1:-}"; NAME="${2:-}"
[ -n "$CMD" ] && [ -n "$NAME" ] || {
  echo "usage: routine-ctl.sh enable <name> <script-rel> <HH:MM> | disable <name> | status <name>" >&2
  exit 1
}
case "$NAME" in (*[!a-z0-9-]*) echo "[routine-ctl] bad name (kebab-case only): $NAME" >&2; exit 1;; esac

OS="$(uname -s)"
LABEL="com.telegram-skill-bot.${AGENT_NAME}.${NAME}"
UNIT="dogany-${AGENT_NAME}-${NAME}"
LA_DIR="${ROUTINE_CTL_LA_DIR:-$HOME/Library/LaunchAgents}"
UNIT_DIR="${ROUTINE_CTL_UNIT_DIR:-$HOME/.config/systemd/user}"
UID_N="$(id -u)"

# Injection seam (mint_teardown.sh's MINT_TEARDOWN_LAUNCHCTL precedent): every
# launchctl call in this file goes through here so a test can substitute a
# stub instead of touching a real unit.
_rc_launchctl() { "${ROUTINE_CTL_LAUNCHCTL:-launchctl}" "$@"; }

# ---------- macOS (launchd) ----------
mac_status() {
  _rc_launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1 && return 0
  _rc_launchctl list 2>/dev/null | grep -q -- "$LABEL" && return 0
  return 3
}

mac_enable() {
  local script_rel="$1" hhmm="$2" weekday="${3:-}"
  local hour="${hhmm%%:*}" minute="${hhmm##*:}"
  local script_abs="$AGENT_DIR/$script_rel"
  local dest="$LA_DIR/${LABEL}.plist"
  [ -f "$TPL" ] || { echo "[routine-ctl] template not found: $TPL" >&2; exit 1; }
  [ -f "$script_abs" ] || { echo "[routine-ctl] script not found: $script_abs" >&2; exit 1; }
  mkdir -p "$LA_DIR" "$AGENT_DIR/.telegram_bot/logs"
  # strip leading zeros for plist <integer> (08 is not valid octal-safe input)
  hour=$((10#$hour)); minute=$((10#$minute))
  # Weekly routines: render a Weekday key into StartCalendarInterval
  # (launchd: 0=Sunday .. 6=Saturday). Daily: token renders to nothing.
  local weekday_entry=""
  if [ -n "$weekday" ]; then
    local wd_num
    case "$weekday" in
      sun) wd_num=0 ;; mon) wd_num=1 ;; tue) wd_num=2 ;; wed) wd_num=3 ;;
      thu) wd_num=4 ;; fri) wd_num=5 ;; sat) wd_num=6 ;;
      *) echo "[routine-ctl] bad weekday: $weekday (mon..sun)" >&2; exit 1 ;;
    esac
    weekday_entry="<key>Weekday</key><integer>${wd_num}</integer>"
  fi
  sed -e "s#__LABEL__#${LABEL}#g" \
      -e "s#__SCRIPT__#${script_abs}#g" \
      -e "s#__HOUR__#${hour}#g" \
      -e "s#__MINUTE__#${minute}#g" \
      -e "s#__ROOT__#${AGENT_DIR}#g" \
      -e "s#__HOMEDIR__#${HOME}#g" \
      -e "s#__LOGNAME__#${NAME}#g" \
      -e "s#__WEEKDAY_ENTRY__#${weekday_entry}#g" \
      "$TPL" > "$dest"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$dest" >/dev/null || { echo "[routine-ctl] plist lint failed: $dest" >&2; exit 2; }
  fi
  [ "${ROUTINE_CTL_NO_LOAD:-0}" = "1" ] && { echo "[routine-ctl] rendered (no-load): $dest"; return 0; }
  # idempotent (re)load: bootout if present, then bootstrap
  _rc_launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
  _rc_launchctl bootstrap "gui/$UID_N" "$dest" 2>/dev/null \
    || _rc_launchctl load "$dest" 2>/dev/null || true
  mac_status || { echo "[routine-ctl] could not verify scheduled: $LABEL" >&2; exit 2; }
  local cadence="daily"; [ -n "$weekday" ] && cadence="weekly on $weekday"
  echo "[routine-ctl] scheduled: $LABEL ($hhmm $cadence)"
}

mac_disable() {
  local dest="$LA_DIR/${LABEL}.plist"
  [ "${ROUTINE_CTL_NO_LOAD:-0}" = "1" ] || _rc_launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
  if [ -f "$dest" ]; then
    if command -v trash >/dev/null 2>&1; then trash "$dest"; else rm -f "$dest"; fi
  fi
  echo "[routine-ctl] unscheduled: $LABEL"

  # DGN-1610: tell the health observer this removal is intentional -- AFTER
  # the unit is down, never before. The observer clears a retired marker it
  # sees on a still-loaded label (re-mint hygiene), so a marker written
  # while the unit is still up erases itself on the next poll and the fix
  # silently does nothing. Down-then-mark leaks a failure as one false
  # alarm (noise); mark-then-down can leak a real incident as silence --
  # noise is the chosen failure direction (design-dgn-1610.md section 4).
  if [ "${ROUTINE_CTL_NO_LOAD:-0}" != "1" ]; then
    local retire_rc=0
    python3 "$AGENT_DIR/routines/health-observer.py" \
      --retire "$LABEL" --why "routine-ctl disable ${NAME} $(date +%Y-%m-%d)" \
      || retire_rc=$?
    case "$retire_rc" in
      0|3) : ;;  # 3 = observer never saw this label = no alarm possible = harmless
      *) echo "[routine-ctl] WARN: observer retire marker failed (rc=$retire_rc) -- a one-shot false loss alarm may fire" >&2; exit 2 ;;
    esac
  fi
}

# ---------- Linux (systemd --user) ----------
lin_status() {
  systemctl --user is-enabled "${UNIT}.timer" >/dev/null 2>&1 && return 0
  return 3
}

lin_enable() {
  local script_rel="$1" hhmm="$2" weekday="${3:-}"
  local script_abs="$AGENT_DIR/$script_rel"
  [ -f "$script_abs" ] || { echo "[routine-ctl] script not found: $script_abs" >&2; exit 1; }
  # Weekly routines: prefix OnCalendar with the systemd day-of-week token.
  local oncal_dow=""
  if [ -n "$weekday" ]; then
    case "$weekday" in
      mon) oncal_dow="Mon " ;; tue) oncal_dow="Tue " ;; wed) oncal_dow="Wed " ;;
      thu) oncal_dow="Thu " ;; fri) oncal_dow="Fri " ;; sat) oncal_dow="Sat " ;;
      sun) oncal_dow="Sun " ;;
      *) echo "[routine-ctl] bad weekday: $weekday (mon..sun)" >&2; exit 1 ;;
    esac
  fi
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/${UNIT}.service" <<EOF
[Unit]
Description=dogany routine ${NAME} (${AGENT_NAME})

[Service]
Type=oneshot
ExecStart=/bin/bash ${script_abs}
WorkingDirectory=${AGENT_DIR}
Environment=HOME=${HOME}
EOF
  cat > "$UNIT_DIR/${UNIT}.timer" <<EOF
[Unit]
Description=dogany routine ${NAME} timer (${AGENT_NAME})

[Timer]
OnCalendar=${oncal_dow}*-*-* ${hhmm}:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
  [ "${ROUTINE_CTL_NO_LOAD:-0}" = "1" ] && { echo "[routine-ctl] rendered (no-load): $UNIT_DIR/${UNIT}.timer"; return 0; }
  systemctl --user daemon-reload
  systemctl --user enable --now "${UNIT}.timer"
  lin_status || { echo "[routine-ctl] could not verify scheduled: ${UNIT}.timer" >&2; exit 2; }
  loginctl enable-linger "$USER" 2>/dev/null || true
  local cadence="daily"; [ -n "$weekday" ] && cadence="weekly on $weekday"
  echo "[routine-ctl] scheduled: ${UNIT}.timer (${hhmm} ${cadence})"
}

lin_disable() {
  [ "${ROUTINE_CTL_NO_LOAD:-0}" = "1" ] || {
    systemctl --user disable --now "${UNIT}.timer" 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
  }
  rm -f "$UNIT_DIR/${UNIT}.timer" "$UNIT_DIR/${UNIT}.service"
  echo "[routine-ctl] unscheduled: ${UNIT}.timer"
}

# ---------- dispatch ----------
case "$CMD" in
  enable)
    SCRIPT_REL="${3:-}"; HHMM="${4:-}"; WEEKDAY="${5:-}"
    [ -n "$SCRIPT_REL" ] && [ -n "$HHMM" ] || { echo "usage: routine-ctl.sh enable <name> <script-rel> <HH:MM> [weekday]" >&2; exit 1; }
    case "$HHMM" in
      ([0-2][0-9]:[0-5][0-9]) : ;;
      (*) echo "[routine-ctl] bad time (HH:MM): $HHMM" >&2; exit 1 ;;
    esac
    if [ -n "$WEEKDAY" ]; then
      case "$WEEKDAY" in
        (mon|tue|wed|thu|fri|sat|sun) : ;;
        (*) echo "[routine-ctl] bad weekday: $WEEKDAY (mon|tue|wed|thu|fri|sat|sun)" >&2; exit 1 ;;
      esac
    fi
    if [ "$OS" = "Darwin" ]; then mac_enable "$SCRIPT_REL" "$HHMM" "$WEEKDAY"; else lin_enable "$SCRIPT_REL" "$HHMM" "$WEEKDAY"; fi
    ;;
  disable)
    if [ "$OS" = "Darwin" ]; then mac_disable; else lin_disable; fi
    ;;
  status)
    if [ "$OS" = "Darwin" ]; then mac_status; else lin_status; fi
    ;;
  *)
    echo "unknown command: $CMD (enable|disable|status)" >&2; exit 1
    ;;
esac

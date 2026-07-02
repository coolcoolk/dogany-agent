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
#   routine-ctl.sh enable  <name> <script-path-rel-to-agent-root> <HH:MM>
#   routine-ctl.sh disable <name>
#   routine-ctl.sh status  <name>          exit 0 = scheduled, 3 = not scheduled
#
# Idempotent: enable re-renders and reloads if already scheduled; disable of a
# non-scheduled routine is a no-op (exit 0).
#
# macOS -> launchd plist rendered from routines/bundle/routine.plist.tpl.
# Linux -> systemd --user .service + .timer (dogany-<agent>-<name>).
#
# Test/env overrides (never needed in normal operation):
#   ROUTINE_CTL_LA_DIR    override ~/Library/LaunchAgents (macOS)
#   ROUTINE_CTL_UNIT_DIR  override ~/.config/systemd/user (Linux)
#   ROUTINE_CTL_NO_LOAD=1 render + place files only, skip launchctl/systemctl
#
# Exit: 0 ok / 1 usage or config error / 2 scheduler verification failed
#       3 (status only) not scheduled
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

# ---------- macOS (launchd) ----------
mac_status() {
  launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1 && return 0
  launchctl list 2>/dev/null | grep -q -- "$LABEL" && return 0
  return 3
}

mac_enable() {
  local script_rel="$1" hhmm="$2"
  local hour="${hhmm%%:*}" minute="${hhmm##*:}"
  local script_abs="$AGENT_DIR/$script_rel"
  local dest="$LA_DIR/${LABEL}.plist"
  [ -f "$TPL" ] || { echo "[routine-ctl] template not found: $TPL" >&2; exit 1; }
  [ -f "$script_abs" ] || { echo "[routine-ctl] script not found: $script_abs" >&2; exit 1; }
  mkdir -p "$LA_DIR" "$AGENT_DIR/.telegram_bot/logs"
  # strip leading zeros for plist <integer> (08 is not valid octal-safe input)
  hour=$((10#$hour)); minute=$((10#$minute))
  sed -e "s#__LABEL__#${LABEL}#g" \
      -e "s#__SCRIPT__#${script_abs}#g" \
      -e "s#__HOUR__#${hour}#g" \
      -e "s#__MINUTE__#${minute}#g" \
      -e "s#__ROOT__#${AGENT_DIR}#g" \
      -e "s#__HOMEDIR__#${HOME}#g" \
      -e "s#__LOGNAME__#${NAME}#g" \
      "$TPL" > "$dest"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$dest" >/dev/null || { echo "[routine-ctl] plist lint failed: $dest" >&2; exit 2; }
  fi
  [ "${ROUTINE_CTL_NO_LOAD:-0}" = "1" ] && { echo "[routine-ctl] rendered (no-load): $dest"; return 0; }
  # idempotent (re)load: bootout if present, then bootstrap
  launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" "$dest" 2>/dev/null \
    || launchctl load "$dest" 2>/dev/null || true
  mac_status || { echo "[routine-ctl] could not verify scheduled: $LABEL" >&2; exit 2; }
  echo "[routine-ctl] scheduled: $LABEL ($hhmm daily)"
}

mac_disable() {
  local dest="$LA_DIR/${LABEL}.plist"
  [ "${ROUTINE_CTL_NO_LOAD:-0}" = "1" ] || launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
  if [ -f "$dest" ]; then
    if command -v trash >/dev/null 2>&1; then trash "$dest"; else rm -f "$dest"; fi
  fi
  echo "[routine-ctl] unscheduled: $LABEL"
}

# ---------- Linux (systemd --user) ----------
lin_status() {
  systemctl --user is-enabled "${UNIT}.timer" >/dev/null 2>&1 && return 0
  return 3
}

lin_enable() {
  local script_rel="$1" hhmm="$2"
  local script_abs="$AGENT_DIR/$script_rel"
  [ -f "$script_abs" ] || { echo "[routine-ctl] script not found: $script_abs" >&2; exit 1; }
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
OnCalendar=*-*-* ${hhmm}:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
  [ "${ROUTINE_CTL_NO_LOAD:-0}" = "1" ] && { echo "[routine-ctl] rendered (no-load): $UNIT_DIR/${UNIT}.timer"; return 0; }
  systemctl --user daemon-reload
  systemctl --user enable --now "${UNIT}.timer"
  lin_status || { echo "[routine-ctl] could not verify scheduled: ${UNIT}.timer" >&2; exit 2; }
  loginctl enable-linger "$USER" 2>/dev/null || true
  echo "[routine-ctl] scheduled: ${UNIT}.timer (${hhmm} daily)"
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
    SCRIPT_REL="${3:-}"; HHMM="${4:-}"
    [ -n "$SCRIPT_REL" ] && [ -n "$HHMM" ] || { echo "usage: routine-ctl.sh enable <name> <script-rel> <HH:MM>" >&2; exit 1; }
    case "$HHMM" in
      ([0-2][0-9]:[0-5][0-9]) : ;;
      (*) echo "[routine-ctl] bad time (HH:MM): $HHMM" >&2; exit 1 ;;
    esac
    if [ "$OS" = "Darwin" ]; then mac_enable "$SCRIPT_REL" "$HHMM"; else lin_enable "$SCRIPT_REL" "$HHMM"; fi
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

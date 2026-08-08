#!/bin/bash
# brief-slot-ctl.sh -- per-slot generic-brief (un)scheduler (DGN-227 C2/P22).
#
# The generic-brief units (generic-brief-morning / -retro / -weekly) ship in
# routines/plists.defer and are loaded/unloaded by SLOT here. This is the
# swap primitive behind the lifekit-setup C2 obligation: when a lifekit
# briefing routine takes over a slot (morning-brief / daily-retro), the
# same-slot generic-brief must be UNLOADED (no double utterance at the same
# clock); when the lifekit routine is turned off, the generic-brief slot must
# be RESTORED (no briefing gap). The swap happens ONLY at the launchd load
# layer -- the plist FILE stays on disk unchanged (spec C2: 파일 배치는 불변).
#
# Usage:
#   brief-slot-ctl.sh enable  <slot>    # load the slot's generic-brief unit
#   brief-slot-ctl.sh disable <slot>    # bootout the slot's generic-brief unit
#   brief-slot-ctl.sh status  <slot>    # exit 0 = loaded, 3 = not loaded
# slot in {morning, retro, weekly}.
#
# Test/env overrides (mirrors routine-ctl.sh):
#   BRIEF_SLOT_NO_LOAD=1   record intent only, skip launchctl (harness seam)
#   BRIEF_SLOT_CAPTURE     append "enable|disable <label>" lines to this file
#
# Exit: 0 ok / 1 usage / 3 (status only) not loaded.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

AGENT_NAME="$(grep -E '^DOGANY_AGENT_NAME=' "$AGENT_DIR/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
AGENT_NAME="${AGENT_NAME:-$(basename "$AGENT_DIR")}"

CMD="${1:-}"; SLOT="${2:-}"
[ -n "$CMD" ] && [ -n "$SLOT" ] || {
  echo "usage: brief-slot-ctl.sh enable|disable|status <morning|retro|weekly>" >&2
  exit 1
}
case "$SLOT" in
  morning|retro|weekly) : ;;
  *) echo "[brief-slot-ctl] bad slot: $SLOT (morning|retro|weekly)" >&2; exit 1 ;;
esac

LABEL="com.telegram-skill-bot.${AGENT_NAME}.generic-brief-${SLOT}"
PLIST="$AGENT_DIR/routines/${LABEL}.plist"
LA_DIR="${ROUTINE_CTL_LA_DIR:-$HOME/Library/LaunchAgents}"
UID_N="$(id -u)"

_capture() { # <verb>
  [ -n "${BRIEF_SLOT_CAPTURE:-}" ] || return 0
  printf '%s %s\n' "$1" "$LABEL" >> "$BRIEF_SLOT_CAPTURE"
}

case "$CMD" in
  enable)
    [ -f "$PLIST" ] || { echo "[brief-slot-ctl] plist not found: $PLIST" >&2; exit 1; }
    _capture enable
    if [ "${BRIEF_SLOT_NO_LOAD:-0}" = "1" ]; then
      echo "[brief-slot-ctl] (no-load) would enable $LABEL"; exit 0
    fi
    mkdir -p "$LA_DIR"
    cp -f "$PLIST" "$LA_DIR/"
    launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$UID_N" "$LA_DIR/$(basename "$PLIST")" 2>/dev/null \
      || launchctl load "$LA_DIR/$(basename "$PLIST")" 2>/dev/null || true
    echo "[brief-slot-ctl] enabled $LABEL"
    ;;
  disable)
    _capture disable
    if [ "${BRIEF_SLOT_NO_LOAD:-0}" = "1" ]; then
      echo "[brief-slot-ctl] (no-load) would disable $LABEL"; exit 0
    fi
    launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1 || true
    if [ -f "$LA_DIR/$(basename "$PLIST")" ]; then
      if command -v trash >/dev/null 2>&1; then trash "$LA_DIR/$(basename "$PLIST")"; \
        else rm -f "$LA_DIR/$(basename "$PLIST")"; fi
    fi
    echo "[brief-slot-ctl] disabled $LABEL"
    ;;
  status)
    if launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then exit 0; fi
    launchctl list 2>/dev/null | grep -q -- "$LABEL" && exit 0
    exit 3
    ;;
  *)
    echo "[brief-slot-ctl] bad command: $CMD" >&2; exit 1 ;;
esac

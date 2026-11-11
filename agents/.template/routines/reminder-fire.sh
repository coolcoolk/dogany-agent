#!/bin/bash
# reminder-fire.sh -- deliver a one-shot reminder, then self-clean.
# Args: $1=label  $2=plist path (macOS launchd; empty on Linux/immediate)
# Sends once via push.sh, then removes its own meta + scheduler job.
#
# The header is localized via config/i18n (i18n "reminder_header").
# Portable: launchd on macOS, transient systemd --user unit on Linux.

set -u
LABEL="$1"; PLIST="${2:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
META_DIR="$SCRIPT_DIR/.reminders"

META="$META_DIR/$LABEL.meta"
# Cancellation/re-arm invalidates a stale native callback by removing or
# replacing this stable metadata record. A repeated callback is a no-op.
[ -f "$META" ] || exit 0
IFS=$'\t' read -r _TARGET _HUMAN MODE PAYLOAD POINTER < "$META"
if [ "$MODE" = "@selector" ]; then
  [ -n "$POINTER" ] && [ "$(cat "$POINTER" 2>/dev/null)" = "$LABEL" ] || exit 0
  "$SCRIPT_DIR/remind.sh" --owner "$PAYLOAD" --require-delivery || exit $?
else
  MSG="$MODE"
  # load i18n helper only for plain-text reminders.
  . "$SCRIPT_DIR/lib/agentlib.sh"
  HEADER="$(i18n reminder_header)"
  "$SCRIPT_DIR/push.sh" --text "$HEADER
$MSG" --audience owner || exit $?
fi

# self-clean: remove meta first (so nothing lingers if the scheduler kills us
# mid-run), then remove the job last.
rm -f "$META_DIR/$LABEL.meta"
[ "$MODE" = "@selector" ] && rm -f "$POINTER"

case "$(uname -s)" in
  Darwin)
    # macOS launchd: remove the plist file, then the loaded job.
    [ -n "$PLIST" ] && rm -f "$PLIST"
    /bin/launchctl remove "$LABEL" 2>/dev/null || true
    ;;
  *)
    # Linux systemd --user: the transient timer already fired; clear any
    # lingering failed state so `list`/reuse of the unit name is clean. The
    # transient unit auto-vanishes; reset-failed is a no-op if already gone.
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user reset-failed "$LABEL.timer" 2>/dev/null || true
      systemctl --user reset-failed "$LABEL.service" 2>/dev/null || true
    fi
    ;;
esac
exit 0

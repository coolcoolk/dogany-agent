#!/bin/bash
# reminder-fire.sh -- deliver a one-shot reminder, then self-clean.
# Args: $1=label  $2=plist path (macOS launchd; empty on Linux/immediate)  $3=message
# Sends once via push.sh, then removes its own meta + scheduler job.
#
# The header is localized via config/i18n (i18n "reminder_header").
# Portable: launchd on macOS, transient systemd --user unit on Linux.

LABEL="$1"; PLIST="$2"; MSG="$3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
META_DIR="$SCRIPT_DIR/.reminders"

# load i18n helper (portable, no hardcoded paths)
. "$SCRIPT_DIR/lib/agentlib.sh"
HEADER="$(i18n reminder_header)"

"$SCRIPT_DIR/push.sh" --text "$HEADER
$MSG"

# self-clean: remove meta first (so nothing lingers if the scheduler kills us
# mid-run), then remove the job last.
rm -f "$META_DIR/$LABEL.meta"

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

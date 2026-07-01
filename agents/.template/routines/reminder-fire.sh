#!/bin/bash
# reminder-fire.sh — deliver a one-shot reminder, then self-clean.
# Args: $1=label  $2=plist path (empty for the immediate/background path)  $3=message
# Sends once via push.sh, then removes its own plist/meta/launchd job.
#
# The header is localized via config/i18n (i18n "reminder_header").

LABEL="$1"; PLIST="$2"; MSG="$3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
META_DIR="$SCRIPT_DIR/.reminders"

# load i18n helper (portable, no hardcoded paths)
. "$SCRIPT_DIR/lib/agentlib.sh"
HEADER="$(i18n reminder_header)"

"$SCRIPT_DIR/push.sh" --text "$HEADER
$MSG"

# self-clean: remove meta + plist first (so nothing lingers if launchd kills
# us mid-run), then remove the job last.
rm -f "$META_DIR/$LABEL.meta"
[[ -n "$PLIST" ]] && rm -f "$PLIST"
/bin/launchctl remove "$LABEL" 2>/dev/null || true
exit 0

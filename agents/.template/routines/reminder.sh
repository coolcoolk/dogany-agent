#!/bin/bash
# reminder.sh — one-shot (single-fire) reminder: register / list / cancel.
#
# Turns "remind me in 10 minutes", "remind me at 3pm", etc. into a single
# delivery at the target time. Mechanism: a one-shot launchd job fires once
# via push.sh at the target time, then removes itself (plist + meta + job).
# Survives logout/reboot. Minute-level precision. Delays under 90s use a
# background sleep instead (sub-minute precision; not reboot-durable).
#
# PORTABLE: no hardcoded user name or absolute paths. Paths derive from this
# script's location and $HOME. User-facing strings come from config/i18n via
# reminder-fire.sh. Recipient/token come from runtime/.env (read by push.sh).
#
# Usage:
#   reminder.sh add "10m" "take meds"          # relative: s/m/h/d, combos like 1h30m
#   reminder.sh add "15:30" "prep meeting"     # today HH:MM (tomorrow if already past)
#   reminder.sh add "2026-06-28 09:00" "..."   # absolute timestamp
#   reminder.sh "10m" "take meds"              # 'add' is optional
#   reminder.sh list                           # list scheduled reminders
#   reminder.sh cancel <label|all>             # cancel
#
# Exit codes: 0 ok / 1 usage error / 2 time-parse failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
META_DIR="$SCRIPT_DIR/.reminders"
LABEL_PREFIX="com.telegram-skill-bot.telegram-agent.reminder"
mkdir -p "$META_DIR"

# ---- relative time (10m, 1h30m, 90s, 2d) -> seconds ----
parse_relative() {
  local s="$1" total=0 num unit rest="$1"
  [[ "$s" =~ ^([0-9]+[smhd])+$ ]] || return 1
  while [[ -n "$rest" ]]; do
    [[ "$rest" =~ ^([0-9]+)([smhd])(.*)$ ]] || return 1
    num="${BASH_REMATCH[1]}"; unit="${BASH_REMATCH[2]}"; rest="${BASH_REMATCH[3]}"
    case "$unit" in
      s) total=$(( total + num )) ;;
      m) total=$(( total + num*60 )) ;;
      h) total=$(( total + num*3600 )) ;;
      d) total=$(( total + num*86400 )) ;;
    esac
  done
  echo "$total"
}

# ---- when -> target epoch (uses system local time, no hardcoded TZ) ----
resolve_target() {
  local when="$1" now rel target
  now=$(date +%s)
  if rel=$(parse_relative "$when" 2>/dev/null); then
    echo $(( now + rel )); return 0
  fi
  if [[ "$when" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
    target=$(date -j -f "%Y-%m-%d %H:%M" "$(date +%Y-%m-%d) $when" +%s 2>/dev/null) || return 1
    if [[ "$target" -le "$now" ]]; then
      target=$(date -v +1d -j -f "%Y-%m-%d %H:%M" "$(date -v +1d +%Y-%m-%d) $when" +%s 2>/dev/null) || return 1
    fi
    echo "$target"; return 0
  fi
  if [[ "$when" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{1,2}:[0-9]{2}$ ]]; then
    target=$(date -j -f "%Y-%m-%d %H:%M" "$when" +%s 2>/dev/null) || return 1
    echo "$target"; return 0
  fi
  return 1
}

cmd_add() {
  local when="$1" msg="${2:-}"
  [[ -z "$when" || -z "$msg" ]] && { echo "usage: reminder.sh add <when> <message>" >&2; exit 1; }

  local now target
  now=$(date +%s)
  target=$(resolve_target "$when") || { echo "time parse failed: '$when'" >&2; exit 2; }
  if [[ "$target" -le "$now" ]]; then
    echo "target time is in the past (target=$target now=$now)" >&2; exit 2
  fi

  local human label delay
  human=$(date -j -f "%s" "$target" "+%Y-%m-%d %H:%M (%a)")
  label="${LABEL_PREFIX}-${target}-$$"
  delay=$(( target - now ))

  # meta (for list/cancel)
  printf '%s\t%s\t%s\n' "$target" "$human" "$msg" > "$META_DIR/$label.meta"

  # under 90s -> background sleep (minute-granularity launchd could miss it)
  if [[ "$delay" -lt 90 ]]; then
    nohup bash -c "sleep $delay; \
      '$SCRIPT_DIR/reminder-fire.sh' '$label' '' \"\$0\"" "$msg" \
      >/dev/null 2>&1 &
    echo "registered (immediate ${delay}s): $human — $msg"
    return 0
  fi

  # one-shot launchd job. PATH/HOME are expanded now so they are portable
  # (no hardcoded user). No TZ override -> fires in system local time, same
  # clock the target was computed against.
  local plist="$LA_DIR/$label.plist"
  local MIN HOUR DAY MONTH plist_home plist_path
  MIN=$(( 10#$(date -j -f "%s" "$target" +%M) ))
  HOUR=$(( 10#$(date -j -f "%s" "$target" +%H) ))
  DAY=$(( 10#$(date -j -f "%s" "$target" +%d) ))
  MONTH=$(( 10#$(date -j -f "%s" "$target" +%m) ))
  plist_home="$HOME"
  plist_path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin"

  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SCRIPT_DIR/reminder-fire.sh</string>
    <string>$label</string>
    <string>$plist</string>
    <string>$msg</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Month</key><integer>$MONTH</integer>
    <key>Day</key><integer>$DAY</integer>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MIN</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/../runtime/logs/reminder.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/../runtime/logs/reminder.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$plist_path</string>
    <key>HOME</key>
    <string>$plist_home</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR/..</string>
</dict>
</plist>
PLIST

  plutil -lint "$plist" >/dev/null || { echo "plist validation failed" >&2; rm -f "$plist"; exit 2; }
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "registered: $human — $msg"
  echo "label: $label"
}

cmd_list() {
  shopt -s nullglob
  local found=0 now
  now=$(date +%s)
  for f in "$META_DIR"/*.meta; do
    found=1
    local label target human msg
    label=$(basename "$f" .meta)
    IFS=$'\t' read -r target human msg < "$f"
    local left=$(( (target - now) / 60 ))
    echo "• $human (in ${left}m) — $msg"
    echo "  $label"
  done
  [[ "$found" -eq 0 ]] && echo "no scheduled reminders"
}

cmd_cancel() {
  local target="${1:-}"
  [[ -z "$target" ]] && { echo "usage: reminder.sh cancel <label|all>" >&2; exit 1; }
  shopt -s nullglob
  if [[ "$target" == "all" ]]; then
    for f in "$META_DIR"/*.meta; do
      local label; label=$(basename "$f" .meta)
      launchctl remove "$label" 2>/dev/null || true
      rm -f "$LA_DIR/$label.plist" "$f"
    done
    echo "all reminders cancelled"
    return 0
  fi
  launchctl remove "$target" 2>/dev/null || true
  rm -f "$LA_DIR/$target.plist" "$META_DIR/$target.meta"
  echo "cancelled: $target"
}

# ---- dispatch ----
sub="${1:-}"
case "$sub" in
  add)    shift; cmd_add "${1:-}" "${2:-}" ;;
  list)   cmd_list ;;
  cancel) shift; cmd_cancel "${1:-}" ;;
  "" )    echo "usage: reminder.sh add <when> <message> | list | cancel <label|all>" >&2; exit 1 ;;
  * )     cmd_add "${1:-}" "${2:-}" ;;   # 'add' omitted form
esac

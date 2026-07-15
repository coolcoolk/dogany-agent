#!/bin/bash
# reminder.sh -- one-shot (single-fire) reminder: register / list / cancel.
#
# Turns "remind me in 10 minutes", "remind me at 3pm", etc. into a single
# delivery at the target time.
#
# PORTABLE across macOS and Linux:
#   - macOS: a one-shot launchd job fires once via push.sh at the target time,
#     then removes itself (plist + meta + job). BSD `date -j` parsing.
#   - Linux: a transient systemd --user timer (systemd-run --user --on-calendar)
#     fires reminder-fire.sh once; --unit lets us cancel it by name. GNU `date`
#     parsing. Both survive logout/reboot (linger) with minute-level precision.
#   - Delays under 90s use a background sleep on BOTH (sub-minute precision; not
#     reboot-durable -- fine for very short ones).
#
# No hardcoded user name or absolute paths. Paths derive from this script's
# location and $HOME. User-facing strings come from config/i18n via
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
META_DIR="$SCRIPT_DIR/.reminders"
LABEL_PREFIX="com.telegram-skill-bot.telegram-agent.reminder"
mkdir -p "$META_DIR"

# ---- OS kind (macos = launchd/BSD date, linux = systemd/GNU date) ----
case "$(uname -s)" in
  Darwin) OS_KIND="macos" ;;
  *)      OS_KIND="linux" ;;
esac
LA_DIR="$HOME/Library/LaunchAgents"   # macOS only

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

# ---- portable epoch helpers (BSD vs GNU date) ----------------------------
# date_to_epoch "%Y-%m-%d %H:%M" "<string>" -> epoch seconds (or fail).
date_to_epoch() {
  local fmt="$1" val="$2"
  if [ "$OS_KIND" = "macos" ]; then
    date -j -f "$fmt" "$val" +%s 2>/dev/null
  else
    # GNU date parses "YYYY-MM-DD HH:MM" natively; the fmt is macOS-only.
    date -d "$val" +%s 2>/dev/null
  fi
}
# epoch_to_fmt <epoch> "<out-format>" -> formatted string.
epoch_to_fmt() {
  local epoch="$1" outfmt="$2"
  if [ "$OS_KIND" = "macos" ]; then
    date -j -f "%s" "$epoch" "$outfmt"
  else
    date -d "@$epoch" "$outfmt"
  fi
}
# today_date -> YYYY-MM-DD ; tomorrow_date -> YYYY-MM-DD (portable).
today_date()    { date +%Y-%m-%d; }
tomorrow_date() {
  if [ "$OS_KIND" = "macos" ]; then date -v +1d +%Y-%m-%d; else date -d "tomorrow" +%Y-%m-%d; fi
}

# ---- when -> target epoch (uses system local time, no hardcoded TZ) ----
resolve_target() {
  local when="$1" now rel target
  now=$(date +%s)
  if rel=$(parse_relative "$when" 2>/dev/null); then
    echo $(( now + rel )); return 0
  fi
  if [[ "$when" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
    target=$(date_to_epoch "%Y-%m-%d %H:%M" "$(today_date) $when") || return 1
    if [[ "$target" -le "$now" ]]; then
      target=$(date_to_epoch "%Y-%m-%d %H:%M" "$(tomorrow_date) $when") || return 1
    fi
    echo "$target"; return 0
  fi
  if [[ "$when" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{1,2}:[0-9]{2}$ ]]; then
    target=$(date_to_epoch "%Y-%m-%d %H:%M" "$when") || return 1
    echo "$target"; return 0
  fi
  return 1
}

# ---- immediate (sub-90s) path: background sleep, both OSes ----
fire_via_sleep() {
  local label="$1" delay="$2" msg="$3"
  nohup bash -c "sleep $delay; \
    '$SCRIPT_DIR/reminder-fire.sh' '$label' '' \"\$0\"" "$msg" \
    >/dev/null 2>&1 &
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
  human=$(epoch_to_fmt "$target" "+%Y-%m-%d %H:%M (%a)")
  label="${LABEL_PREFIX}-${target}-$$"
  delay=$(( target - now ))

  # meta (for list/cancel)
  printf '%s\t%s\t%s\n' "$target" "$human" "$msg" > "$META_DIR/$label.meta"

  # under 90s -> background sleep (minute-granularity schedulers could miss it)
  if [[ "$delay" -lt 90 ]]; then
    fire_via_sleep "$label" "$delay" "$msg"
    echo "registered (immediate ${delay}s): $human -- $msg"
    return 0
  fi

  if [ "$OS_KIND" = "macos" ]; then
    add_launchd "$label" "$target" "$msg"
  else
    add_systemd "$label" "$target" "$msg"
  fi
  echo "registered: $human -- $msg"
}

# ---- macOS: one-shot launchd job ----
add_launchd() {
  local label="$1" target="$2" msg="$3"
  local plist="$LA_DIR/$label.plist"
  local MIN HOUR DAY MONTH plist_home plist_path
  MIN=$(( 10#$(epoch_to_fmt "$target" +%M) ))
  HOUR=$(( 10#$(epoch_to_fmt "$target" +%H) ))
  DAY=$(( 10#$(epoch_to_fmt "$target" +%d) ))
  MONTH=$(( 10#$(epoch_to_fmt "$target" +%m) ))
  plist_home="$HOME"
  plist_path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin"
  mkdir -p "$LA_DIR"

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
}

# ---- Linux: transient systemd --user timer ----
# systemd-run --user schedules a one-shot: --on-calendar at the exact minute,
# --unit gives it our label so cancel/list can target it by name. The unit runs
# reminder-fire.sh, which self-cleans meta (the transient units auto-vanish
# after firing). Persistent linger keeps --user timers alive across logout.
add_systemd() {
  local label="$1" target="$2" msg="$3"
  command -v systemctl >/dev/null 2>&1 || {
    echo "systemctl not found; cannot schedule reminder on this host" >&2; exit 2; }
  # systemd unit names cannot contain ':' -> the label is dot/dash only, safe.
  local oncal
  oncal=$(epoch_to_fmt "$target" "+%Y-%m-%d %H:%M:00")
  # enable linger so the timer survives logout (best-effort; needs no sudo for self).
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    loginctl enable-linger "$USER" 2>/dev/null || true
  fi
  mkdir -p "$SCRIPT_DIR/../runtime/logs"
  systemd-run --user \
    --unit="$label" \
    --on-calendar="$oncal" \
    --timer-property=Persistent=true \
    /bin/bash "$SCRIPT_DIR/reminder-fire.sh" "$label" "" "$msg" >/dev/null
}

# ---- sorted meta list: echo "<epoch> <metafile>" lines sorted by epoch ----
# Used by both cmd_list and cmd_cancel so both see the same index ordering.
sorted_meta() {
  shopt -s nullglob
  local f
  for f in "$META_DIR"/*.meta; do
    local epoch
    IFS=$'\t' read -r epoch _ _ < "$f"
    echo "$epoch $f"
  done | sort -n
}

cmd_list() {
  local found=0 idx=0 now
  now=$(date +%s)
  while IFS=' ' read -r epoch f; do
    [[ -z "$f" ]] && continue
    found=1
    idx=$(( idx + 1 ))
    local target human msg
    IFS=$'\t' read -r target human msg < "$f"
    local left=$(( (target - now) / 60 ))
    echo "[$idx] $human (in ${left}m) -- $msg"
  done < <(sorted_meta)
  [[ "$found" -eq 0 ]] && echo "no scheduled reminders"
}

# ---- remove one job by label (OS-appropriate) ----
remove_job() {
  local label="$1"
  if [ "$OS_KIND" = "macos" ]; then
    launchctl remove "$label" 2>/dev/null || true
    rm -f "$LA_DIR/$label.plist"
  else
    # stop + reset the transient timer/service (both share the unit name).
    systemctl --user stop "$label.timer" 2>/dev/null || true
    systemctl --user stop "$label.service" 2>/dev/null || true
    systemctl --user reset-failed "$label.timer" 2>/dev/null || true
    systemctl --user reset-failed "$label.service" 2>/dev/null || true
  fi
}

cmd_cancel() {
  local arg="${1:-}"
  [[ -z "$arg" ]] && { echo "usage: reminder.sh cancel <[N]|N|label|all>" >&2; exit 1; }
  shopt -s nullglob
  if [[ "$arg" == "all" ]]; then
    local f
    for f in "$META_DIR"/*.meta; do
      local label; label=$(basename "$f" .meta)
      remove_job "$label"
      rm -f "$f"
    done
    echo "all reminders cancelled"
    return 0
  fi

  # Resolve short index: accept "[N]" or bare "N" (integer).
  local resolved_label=""
  local stripped="${arg#[}"
  stripped="${stripped%]}"
  if [[ "$stripped" =~ ^[0-9]+$ ]]; then
    local idx=0 want="$stripped" epoch f
    while IFS=' ' read -r epoch f; do
      [[ -z "$f" ]] && continue
      idx=$(( idx + 1 ))
      if [[ "$idx" -eq "$want" ]]; then
        resolved_label=$(basename "$f" .meta)
        break
      fi
    done < <(sorted_meta)
    if [[ -z "$resolved_label" ]]; then
      echo "no reminder at index $want" >&2; exit 1
    fi
  else
    # Full label passed (backward compat).
    resolved_label="$arg"
  fi

  remove_job "$resolved_label"
  rm -f "$META_DIR/$resolved_label.meta"
  echo "cancelled: $resolved_label"
}

# ---- dispatch ----
sub="${1:-}"
case "$sub" in
  add)    shift; cmd_add "${1:-}" "${2:-}" ;;
  list)   cmd_list ;;
  cancel) shift; cmd_cancel "${1:-}" ;;
  "" )    echo "usage: reminder.sh add <when> <message> | list | cancel <[N]|N|label|all>" >&2; exit 1 ;;
  * )     cmd_add "${1:-}" "${2:-}" ;;   # 'add' omitted form
esac

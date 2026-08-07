#!/bin/bash
# __AGENT_LABEL__ nightly consolidate -- runs once a day, off-hours.
# Transcript delta -> Sonnet compression -> rule/two-stage filter -> dedup ->
# plain append into inbox.md -> silent report.
# Only reads conversations after the watermark (state.db), so each run handles
# only the new conversations. Runs quietly while the user is asleep.
# No topic routing here -- the nightly pass always writes to inbox.md; the weekly
# classify-inbox distributes it into topic files.
# Paths are resolved relative to the script's own location (dynamic), so the job
# survives a workspace move.
#
# DGN-726: usage-exhaustion defer. If memory.py consolidate detects that the
# account usage window is exhausted, it stays fully silent (no report, exit 0 so
# cron-guard fires nothing), preserves the watermark, and drops a defer marker
# recording the next usage-reset boundary. This wrapper reads that marker and
# schedules a ONE-SHOT reset-aligned retry (launchd on macOS, systemd --user timer
# on Linux) so the deferred span is re-processed at the reset, not merely on the
# next daily run. Fail-open throughout: any scheduling failure leaves the marker
# and the preserved watermark, so the next daily cron re-collects the same span.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="$SCRIPT_DIR/../memory-engine"
DEFER_MARKER="$ENGINE_DIR/.consolidate-usage-defer.json"

cd "$ENGINE_DIR" || exit 1

# dedup / neighbor lookup assume a fresh notes index -- the search hook does not
# run at night, so re-index explicitly.
/usr/bin/python3 memory.py index
/usr/bin/python3 memory.py consolidate
CONSOLIDATE_RC=$?

# ---- DGN-726: honor a usage-defer marker by scheduling a reset-aligned retry ----
# Only act when the marker exists (memory.py wrote it this run or a prior deferred
# run left it). A successful (non-deferred) run does not create the marker; a run
# that actually processed the span clears it (see below).
schedule_reset_retry() {
  # $1 = reset epoch (seconds). Registers a one-shot re-fire of THIS script.
  local target="$1"
  local now label
  now="$(date +%s)"
  # Guard: never schedule in the past / immediately (min +60s) -- if the reset is
  # already due, fall through to the immediate re-run branch in the caller.
  if [[ -z "$target" || "$target" -le $(( now + 60 )) ]]; then
    return 1
  fi

  # Agent slug from instance manifest (same derivation reminder.sh uses); fallback
  # = memory-engine parent dir name. Keeps the label unique per instance.
  local agent_dir agent_slug
  agent_dir="$(cd "$SCRIPT_DIR/.." && pwd)"
  agent_slug="$(grep -E '^DOGANY_AGENT_NAME=' "$agent_dir/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
  agent_slug="${agent_slug:-$(basename "$agent_dir")}"
  label="com.telegram-skill-bot.${agent_slug}.consolidate-usage-retry"

  case "$(uname -s)" in
    Darwin)
      local la_dir="$HOME/Library/LaunchAgents"
      local plist="$la_dir/$label.plist"
      local MIN HOUR DAY MONTH plist_path
      MIN=$(( 10#$(date -j -f %s "$target" +%M) ))
      HOUR=$(( 10#$(date -j -f %s "$target" +%H) ))
      DAY=$(( 10#$(date -j -f %s "$target" +%d) ))
      MONTH=$(( 10#$(date -j -f %s "$target" +%m) ))
      plist_path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin"
      mkdir -p "$la_dir"
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
    <string>$SCRIPT_DIR/consolidate-0430.sh</string>
    <string>--from-reset-retry</string>
    <string>$label</string>
    <string>$plist</string>
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
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$plist_path</string>
    <key>HOME</key>
    <string>$HOME</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>$ENGINE_DIR</string>
</dict>
</plist>
PLIST
      if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$plist" >/dev/null 2>&1 || { rm -f "$plist"; return 1; }
      fi
      launchctl unload "$plist" 2>/dev/null || true
      launchctl load "$plist" 2>/dev/null || return 1
      return 0
      ;;
    *)
      command -v systemctl >/dev/null 2>&1 || return 1
      local oncal
      oncal="$(date -d "@$target" "+%Y-%m-%d %H:%M:00" 2>/dev/null)" || return 1
      if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
        loginctl enable-linger "$USER" 2>/dev/null || true
      fi
      systemctl --user reset-failed "$label.timer" 2>/dev/null || true
      systemctl --user reset-failed "$label.service" 2>/dev/null || true
      systemd-run --user \
        --unit="$label" \
        --on-calendar="$oncal" \
        --timer-property=Persistent=true \
        /bin/bash "$SCRIPT_DIR/consolidate-0430.sh" --from-reset-retry "$label" "" \
        >/dev/null 2>&1 || return 1
      return 0
      ;;
  esac
  return 1
}

# self-clean a fired one-shot retry job (called when we ran via --from-reset-retry).
cleanup_retry_job() {
  local label="$1" plist="$2"
  [[ -z "$label" ]] && return 0
  case "$(uname -s)" in
    Darwin)
      [[ -n "$plist" ]] && rm -f "$plist"
      launchctl remove "$label" 2>/dev/null || true
      ;;
    *)
      systemctl --user reset-failed "$label.timer" 2>/dev/null || true
      systemctl --user reset-failed "$label.service" 2>/dev/null || true
      ;;
  esac
}

# If this invocation IS the fired one-shot retry, clean the job up now that it ran.
# ($1=--from-reset-retry $2=label $3=plist). The consolidate above already ran.
if [[ "${1:-}" == "--from-reset-retry" ]]; then
  cleanup_retry_job "${2:-}" "${3:-}"
fi

# If consolidate SUCCEEDED (processed the span) and any marker present was left by a
# PRIOR deferred run (not freshly written by this run), it is now obsolete -> clear it
# so we do not schedule a spurious retry off a stale reset time. memory.py only WRITES
# the marker on defer and never clears it, so the wrapper owns clearing. "Fresh" =
# deferred_at within the last 10 min (this very run just deferred -> keep it).
if [[ "$CONSOLIDATE_RC" -eq 0 && -f "$DEFER_MARKER" ]]; then
  DEFERRED_THIS_RUN="$(/usr/bin/python3 -c '
import json, sys
from datetime import datetime, timezone
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    da = d.get("deferred_at", "")
    dt = datetime.fromisoformat(da.replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    print("1" if age < 600 else "0")
except Exception:
    print("1")
' "$DEFER_MARKER" 2>/dev/null)"
  if [[ "$DEFERRED_THIS_RUN" == "0" ]]; then
    rm -f "$DEFER_MARKER"
    echo "[consolidate-0430] prior usage-defer resolved -- marker cleared"
  fi
fi

# Marker present -> memory.py deferred (this run or earlier). Decide retry timing.
if [[ -f "$DEFER_MARKER" ]]; then
  RESET_ISO="$(/usr/bin/python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.load(f).get("reset_at", "") or "")
except Exception:
    print("")
' "$DEFER_MARKER" 2>/dev/null)"

  RESET_EPOCH=""
  if [[ -n "$RESET_ISO" ]]; then
    # ISO-8601 (may carry Z or +00:00) -> epoch. Portable via python (avoids BSD/GNU date drift).
    RESET_EPOCH="$(/usr/bin/python3 -c '
import sys
from datetime import datetime
try:
    dt = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    print(int(dt.timestamp()))
except Exception:
    print("")
' "$RESET_ISO" 2>/dev/null)"
  fi

  NOW_EPOCH="$(date +%s)"
  if [[ -n "$RESET_EPOCH" && "$RESET_EPOCH" -gt $(( NOW_EPOCH + 60 )) ]]; then
    # Reset is in the future -> schedule the one-shot retry at that boundary.
    if schedule_reset_retry "$RESET_EPOCH"; then
      echo "[consolidate-0430] usage-defer: reset-aligned retry scheduled for $RESET_ISO"
    else
      echo "[consolidate-0430] usage-defer: retry scheduling failed (fail-open: next daily run handles it)" >&2
    fi
  else
    echo "[consolidate-0430] usage-defer: reset already due (or unknown) -- next daily run handles it"
  fi
fi

exit "$CONSOLIDATE_RC"

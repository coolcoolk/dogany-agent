#!/bin/bash
# usage-defer-retry.sh -- reset-aligned ONE-SHOT retry scheduler for
# usage-deferred cron jobs (DGN-835; generalizes the DGN-726 scheduler that
# lived inside consolidate-0430.sh).
#
# A cron job that hits usage exhaustion signals cron-guard with the 2-factor
# protocol (exit 75 + fresh marker). For a FIVE_HOUR defer, cron-guard calls
# this helper to register a one-shot job at the usage-reset boundary that
# replays the ORIGINAL cron-guard argv. The one-shot re-enters through this
# script's --fire mode, which runs the replayed command TO COMPLETION and
# only then self-cleans the scheduler unit (order is load-bearing -- see
# --fire below).
#
# Usage:
#   schedule:  usage-defer-retry.sh --label <cron-label> --reset-epoch <epoch> \
#                                   -- <cmd> [args...]
#   fire:      usage-defer-retry.sh --fire <oneshot-label> <plist-or-empty> \
#                                   -- <cmd> [args...]
#
# Guarantees:
#   - Argv fidelity: the command is serialized ARGUMENT-BY-ARGUMENT into the
#     launchd plist ProgramArguments array (macOS) / systemd-run argv (Linux),
#     so spaces/quotes/globs in arguments survive verbatim (no shell -c).
#   - Dedup per label: one pending one-shot per cron label; re-scheduling
#     replaces the previous unit (unload + rewrite).
#   - Fail-open: any scheduling failure exits nonzero and changes nothing;
#     the caller's next regular run re-collects the work.
#
# Exit codes: schedule mode -- 0 scheduled / 1 invalid input or scheduling
# failed. Fire mode -- the replayed command's own exit code (1 when there is
# nothing to replay).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- fire mode: run the replay TO COMPLETION, then self-clean the unit ----
# ORDER IS THE LOAD-BEARING PART (DGN-835 final-grill MAJOR-A): on macOS
# `launchctl remove` SIGTERMs the job's still-running process, so cleaning
# BEFORE the replay killed the replayed command mid-run -- the 5h automatic
# recovery murdered its own payload. Legacy (DGN-726, consolidate-0430.sh)
# ran work-then-cleanup; that semantic is restored: the replay runs as a
# CHILD to completion, cleanup happens last. The trailing remove may still
# SIGTERM this wrapper itself, but by then the work is done and the plist is
# already gone (rm precedes remove), so nothing re-fires.
if [[ "${1:-}" == "--fire" ]]; then
  FIRE_LABEL="${2:-}"
  FIRE_PLIST="${3:-}"
  shift 3 || true
  if [[ "${1:-}" == "--" ]]; then shift; fi

  fire_cleanup() {
    case "$(uname -s)" in
      Darwin)
        [[ -n "$FIRE_PLIST" ]] && rm -f "$FIRE_PLIST"
        [[ -n "$FIRE_LABEL" ]] && launchctl remove "$FIRE_LABEL" 2>/dev/null || true
        ;;
      *)
        if [[ -n "$FIRE_LABEL" ]]; then
          systemctl --user reset-failed "$FIRE_LABEL.timer" 2>/dev/null || true
          systemctl --user reset-failed "$FIRE_LABEL.service" 2>/dev/null || true
        fi
        ;;
    esac
  }

  if [[ $# -eq 0 ]]; then
    echo "[usage-defer-retry] fire: no command to replay" >&2
    fire_cleanup  # an argument-less one-shot must not linger either
    exit 1
  fi
  "$@"
  FIRE_RC=$?
  fire_cleanup
  exit "$FIRE_RC"
fi

# ---- schedule mode ----
LABEL=""
RESET_EPOCH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)       LABEL="$2"; shift 2 ;;
    --reset-epoch) RESET_EPOCH="$2"; shift 2 ;;
    --)            shift; break ;;
    *) echo "[usage-defer-retry] unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$LABEL" || -z "$RESET_EPOCH" || $# -eq 0 ]]; then
  echo "[usage-defer-retry] need --label, --reset-epoch and a command after --" >&2
  exit 1
fi
# label becomes a filename / unit name -- restrict charset
case "$LABEL" in
  *[!A-Za-z0-9._-]*)
    echo "[usage-defer-retry] invalid --label (allowed: A-Za-z0-9._-): $LABEL" >&2
    exit 1 ;;
esac
case "$RESET_EPOCH" in
  ''|*[!0-9]*)
    echo "[usage-defer-retry] invalid --reset-epoch (epoch seconds): $RESET_EPOCH" >&2
    exit 1 ;;
esac

NOW="$(date +%s)"
# Never schedule in the past / immediately (min +60s). If the reset is already
# due the caller's next regular run handles the work (fail-open).
if [[ "$RESET_EPOCH" -le $(( NOW + 60 )) ]]; then
  echo "[usage-defer-retry] reset already due (epoch $RESET_EPOCH) -- nothing scheduled" >&2
  exit 1
fi

# Fire at reset + 120s pad (DGN-835 non-blocker follow-up): the calendar
# schedulers have MINUTE resolution (seconds truncated -> a unit can fire up
# to 59s EARLY) and the provider-side reset may lag the advertised boundary;
# firing marginally before the reset just re-hits exhaustion. --reset-epoch
# keeps meaning "the advertised reset time"; the pad is applied here once.
FIRE_EPOCH=$(( RESET_EPOCH + 120 ))

ONESHOT_LABEL="${LABEL}.usage-retry"

case "$(uname -s)" in
  Darwin)
    LA_DIR="$HOME/Library/LaunchAgents"
    PLIST="$LA_DIR/$ONESHOT_LABEL.plist"
    AGENT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    LOG_DIR="$AGENT_ROOT/.telegram_bot/logs"
    mkdir -p "$LA_DIR" "$LOG_DIR"
    # dedup per label: drop any previous pending one-shot before rewriting.
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    # Serialize argv into the plist with python3: per-argument <string>
    # entries with XML escaping (quoting-safe -- no shell -c hop).
    # WorkingDirectory + Standard{Out,Error}Path (DGN-835 non-blocker
    # follow-up): the replay inherits a sane cwd (AGENT_ROOT, matching the
    # regular cron plists) and its output lands in the instance log dir
    # instead of vanishing into the launchd void.
    if ! /usr/bin/python3 - "$PLIST" "$ONESHOT_LABEL" "$FIRE_EPOCH" \
        "$SCRIPT_DIR/usage-defer-retry.sh" "$AGENT_ROOT" "$LOG_DIR" "$@" <<'PYEOF'
import sys
from datetime import datetime
from xml.sax.saxutils import escape

plist_path, label, epoch, helper, agent_root, log_dir = sys.argv[1:7]
cmd = sys.argv[7:]
dt = datetime.fromtimestamp(int(epoch))  # local time (launchd calendar is local)
args = ["/bin/bash", helper, "--fire", label, plist_path, "--"] + list(cmd)
arg_xml = "\n".join("    <string>%s</string>" % escape(a) for a in args)
# $HOME/.local/bin first (DGN-963): native Claude CLI install must win
# over Homebrew / /usr/local/bin; no npm-global entry (deprecated).
path_env = ("%s/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin") % (
    __import__("os").path.expanduser("~"))
home = __import__("os").path.expanduser("~")
body = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>%s</string>
  <key>ProgramArguments</key>
  <array>
%s
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Month</key><integer>%d</integer>
    <key>Day</key><integer>%d</integer>
    <key>Hour</key><integer>%d</integer>
    <key>Minute</key><integer>%d</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>WorkingDirectory</key>
  <string>%s</string>
  <key>StandardOutPath</key>
  <string>%s</string>
  <key>StandardErrorPath</key>
  <string>%s</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>%s</string>
    <key>HOME</key>
    <string>%s</string>
  </dict>
</dict>
</plist>
""" % (escape(label), arg_xml, dt.month, dt.day, dt.hour, dt.minute,
       escape(agent_root),
       escape("%s/%s.stdout.log" % (log_dir, label)),
       escape("%s/%s.stderr.log" % (log_dir, label)),
       escape(path_env), escape(home))
with open(plist_path, "w", encoding="utf-8") as f:
    f.write(body)
PYEOF
    then
      rm -f "$PLIST"
      echo "[usage-defer-retry] plist generation failed" >&2
      exit 1
    fi
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$PLIST" >/dev/null 2>&1 || { rm -f "$PLIST"; exit 1; }
    fi
    launchctl load "$PLIST" 2>/dev/null || { rm -f "$PLIST"; exit 1; }
    echo "[usage-defer-retry] one-shot scheduled: $ONESHOT_LABEL at epoch $RESET_EPOCH"
    exit 0
    ;;
  *)
    command -v systemctl >/dev/null 2>&1 || exit 1
    AGENT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    ONCAL="$(date -d "@$FIRE_EPOCH" "+%Y-%m-%d %H:%M:00" 2>/dev/null)" || exit 1
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
      loginctl enable-linger "$USER" 2>/dev/null || true
    fi
    # dedup per label: clear any previous pending unit first.
    systemctl --user stop "$ONESHOT_LABEL.timer" 2>/dev/null || true
    systemctl --user reset-failed "$ONESHOT_LABEL.timer" 2>/dev/null || true
    systemctl --user reset-failed "$ONESHOT_LABEL.service" 2>/dev/null || true
    # cwd = AGENT_ROOT (parity with the Darwin plist); output rides the
    # user journal (journalctl --user -u <unit>), no explicit log files.
    systemd-run --user \
      --unit="$ONESHOT_LABEL" \
      --on-calendar="$ONCAL" \
      --timer-property=Persistent=true \
      --property=WorkingDirectory="$AGENT_ROOT" \
      /bin/bash "$SCRIPT_DIR/usage-defer-retry.sh" --fire "$ONESHOT_LABEL" "" -- "$@" \
      >/dev/null 2>&1 || exit 1
    echo "[usage-defer-retry] one-shot scheduled: $ONESHOT_LABEL at $ONCAL"
    exit 0
    ;;
esac

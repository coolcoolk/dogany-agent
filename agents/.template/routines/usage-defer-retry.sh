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
#     Dispatch --task is the exception: its bytes are snapshotted outside
#     files/tmp before arming, and that argument becomes the durable path
#     (DGN-1455). Shell command strings are not parsed or supported here.
#   - Dedup per label: one pending one-shot per cron label; re-scheduling
#     replaces the previous unit (unload + rewrite).
#   - Fail-open: any scheduling failure exits nonzero and changes nothing;
#     the caller's next regular run re-collects the work.
#
#   - No silent idle loop (DGN-1455 B): two consecutive failed replays of
#     one label disarm it and notify the owner once (see --fire).
#
# Exit codes: schedule mode -- 0 scheduled / 1 invalid input or scheduling
# failed. Fire mode -- the replayed command's own exit code (1 when there is
# nothing to replay).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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
  FIRE_SNAPSHOT=""
  if [[ "${1:-}" == "--task-snapshot" ]]; then
    FIRE_SNAPSHOT="${2:-}"
    shift 2
  fi
  if [[ "${1:-}" == "--" ]]; then shift; fi

  fire_cleanup() {
    # Remove only a snapshot owned by this helper, after the child returns.
    # A replay that defers again has already made its own independent copy.
    case "$FIRE_SNAPSHOT" in
      "$AGENT_ROOT/.telegram_bot/usage-retry/"*.task.*)
        rm -f -- "$FIRE_SNAPSHOT" ;;
    esac
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

  # DGN-1455 (B): a failed replay is retired, but whatever deferred it in the
  # first place (cron-guard's next run, a dispatch re-defer) re-arms the SAME
  # label and the next replay fails the same way -- a silent idle loop that
  # only the stderr log ever saw. The streak of consecutive rc!=0 replays is
  # kept per one-shot label; at the 2nd the label is disarmed (also any unit
  # re-armed meanwhile) and the owner is told ONCE per streak. A successful
  # replay ends the streak.
  STREAK_FILE=""
  case "$FIRE_LABEL" in
    ''|*[!A-Za-z0-9._-]*) ;;
    *) STREAK_FILE="$AGENT_ROOT/.telegram_bot/usage-retry/$FIRE_LABEL.fails" ;;
  esac
  fire_disarm() {
    case "$(uname -s)" in
      Darwin) ;;  # fire_cleanup's rm + remove already covers a re-armed plist
      *) [[ -n "$FIRE_LABEL" ]] && systemctl --user stop "$FIRE_LABEL.timer" 2>/dev/null || true ;;
    esac
  }
  fire_notify() {
    # Owner-facing copy: key usage_retry.replay_failed_disarmed is
    # PENDING_OWNER_CONFIRM (DGN-1455); the default below is its en draft.
    local tpl push="$SCRIPT_DIR/push.sh"
    tpl="$( ( set +e; . "$SCRIPT_DIR/lib/agentlib.sh" >/dev/null 2>&1
              i18n_or usage_retry.replay_failed_disarmed "" ) 2>/dev/null )" || tpl=""
    [[ -n "$tpl" ]] || tpl="The scheduled retry '{label}' failed {count} times in a row (last exit code {rc}), so I cancelled it. It will not run again on its own."
    tpl="${tpl//\{label\}/${FIRE_LABEL%.usage-retry}}"
    tpl="${tpl//\{count\}/$1}"
    tpl="${tpl//\{rc\}/$2}"
    if [[ -x "$push" ]]; then
      "$push" --text "$tpl" --audience owner >&2 || echo "[usage-defer-retry] owner notice failed: $FIRE_LABEL" >&2
    else
      echo "[usage-defer-retry] push.sh not found for owner notice: $push" >&2
    fi
  }
  fire_record() {
    local rc="$1" count=0 notified=""
    [[ -n "$STREAK_FILE" ]] || return 0
    if [[ "$rc" -eq 0 ]]; then
      rm -f -- "$STREAK_FILE"
      return 0
    fi
    # rc 75 = the defer protocol itself (usage still exhausted, re-deferred):
    # a normal wait, neither a failure nor a success -- the streak is untouched.
    [[ "$rc" -eq 75 ]] && return 0
    # A streak older than 7 days is not "in a row": start a new one.
    if [[ -f "$STREAK_FILE" ]] && [[ -n "$(find "$STREAK_FILE" -mtime +7 2>/dev/null)" ]]; then
      rm -f -- "$STREAK_FILE"
    fi
    if [[ -f "$STREAK_FILE" ]]; then
      { read -r count; read -r notified; } < "$STREAK_FILE" || true
      [[ "$count" =~ ^[0-9]+$ ]] || count=0
    fi
    count=$(( count + 1 ))
    local notify=0
    if [[ "$count" -ge 2 && "$notified" != notified ]]; then
      notify=1
      notified=notified
    fi
    # Persist (incl. the notified mark) before pushing: at most one notice.
    (umask 077; mkdir -p "${STREAK_FILE%/*}" && printf '%s\n%s\n' "$count" "$notified" > "$STREAK_FILE") \
      || echo "[usage-defer-retry] failure streak not recorded: $STREAK_FILE" >&2
    if [[ "$count" -ge 2 ]]; then
      echo "[usage-defer-retry] FAILED: $FIRE_LABEL replay failed $count times in a row; disarming" >&2
      fire_disarm
      [[ "$notify" -eq 1 ]] && fire_notify "$count" "$rc"
    fi
    return 0
  }

  if [[ $# -eq 0 ]]; then
    echo "[usage-defer-retry] fire: no command to replay" >&2
    fire_record 1
    fire_cleanup  # an argument-less one-shot must not linger either
    exit 1
  fi
  if [[ -n "$FIRE_SNAPSHOT" && ( ! -f "$FIRE_SNAPSHOT" || ! -r "$FIRE_SNAPSHOT" ) ]]; then
    echo "[usage-defer-retry] FAILED: $FIRE_LABEL task snapshot not readable: $FIRE_SNAPSHOT; retiring one-shot" >&2
    fire_record 1
    fire_cleanup
    exit 1
  fi
  "$@"
  FIRE_RC=$?
  if [[ "$FIRE_RC" -ne 0 ]]; then
    # Operator diagnostic, captured by launchd logs / systemd journal.
    echo "[usage-defer-retry] FAILED: $FIRE_LABEL replay rc=$FIRE_RC; retiring one-shot" >&2
  fi
  # Record BEFORE cleanup: the trailing launchctl remove may SIGTERM us.
  fire_record "$FIRE_RC"
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

# DGN-1455: argv preservation alone cannot preserve a file's lifetime.
# Recognize direct dispatch argv (also behind env/bash/cron-guard wrappers),
# never a --task belonging to some other command. Walk option/value pairs
# so a label or --why value equal to '--task' remains literal data.
REPLAY=("$@")
TASK_INDEX=-1
for (( i=0; i<${#REPLAY[@]}; i++ )); do
  if [[ "${REPLAY[$i]##*/}" == dispatch-detached.sh ]]; then
    for (( j=i+1; j<${#REPLAY[@]}; j++ )); do
      case "${REPLAY[$j]}" in
        --task)
          TASK_INDEX=$(( j + 1 ))
          if [[ "$TASK_INDEX" -ge "${#REPLAY[@]}" ]]; then
            echo "[usage-defer-retry] dispatch --task needs a file" >&2
            exit 1
          fi
          j=$(( j + 1 )) ;;
        --*)
          # Match dispatch's valueless switches; all other options consume
          # one value, even when the value itself starts with '--'.
          case "${REPLAY[$j]}" in
            --read-only|--write|--repo-contained|--help|--list|--calibration|--start-notice-flush|--start-notice-enqueue|--end-notice|--backfill-model-observed|--apply) ;;
            *) j=$(( j + 1 )) ;;
          esac ;;
      esac
    done
    break
  fi
done
SNAPSHOT=""
SCHEDULED=0
schedule_cleanup() {
  if [[ "$SCHEDULED" -eq 0 && -n "$SNAPSHOT" ]]; then
    rm -f -- "$SNAPSHOT"
  fi
}
trap schedule_cleanup EXIT
FIRE_OPTIONS=()
if [[ "$TASK_INDEX" -ge 0 ]]; then
  TASK_SOURCE="${REPLAY[$TASK_INDEX]}"
  if [[ ! -f "$TASK_SOURCE" || ! -r "$TASK_SOURCE" ]]; then
    echo "[usage-defer-retry] dispatch task file not readable: $TASK_SOURCE; nothing scheduled" >&2
    exit 1
  fi
  SNAP_DIR="$AGENT_ROOT/.telegram_bot/usage-retry"
  (umask 077; mkdir -p "$SNAP_DIR") || exit 1
  SNAPSHOT="$(mktemp "$SNAP_DIR/$ONESHOT_LABEL.task.XXXXXX")" || exit 1
  if ! cat -- "$TASK_SOURCE" > "$SNAPSHOT"; then
    echo "[usage-defer-retry] dispatch task snapshot failed; nothing scheduled" >&2
    exit 1
  fi
  REPLAY[$TASK_INDEX]="$SNAPSHOT"
  FIRE_OPTIONS=(--task-snapshot "$SNAPSHOT")
fi
set -- "${FIRE_OPTIONS[@]+"${FIRE_OPTIONS[@]}"}" -- "${REPLAY[@]}"

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
args = ["/bin/bash", helper, "--fire", label, plist_path] + list(cmd)
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
    SCHEDULED=1
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
      /bin/bash "$SCRIPT_DIR/usage-defer-retry.sh" --fire "$ONESHOT_LABEL" "" "$@" \
      >/dev/null 2>&1 || exit 1
    SCHEDULED=1
    echo "[usage-defer-retry] one-shot scheduled: $ONESHOT_LABEL at $ONCAL"
    exit 0
    ;;
esac

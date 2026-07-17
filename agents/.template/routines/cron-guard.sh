#!/bin/bash
# cron-guard.sh -- exit!=0 visibility wrapper for launchd routines.
#
# Usage:
#   cron-guard.sh --label <launchd-label> [--friendly-name <text>] \
#                 [--log <stdout-log>] [--env <dotenv>] \
#                 [--queue <class> [--slots N] [--queue-timeout SEC]] \
#                 -- <cmd> [args...]
#
#   --label           launchd job label (used as dedup key; shown as detail in alerts)
#   --friendly-name   human-readable job name for alert headline (optional).
#                     If omitted, the label's last dot-segment is used as headline.
#   --log             path to the job's stdout/stderr log (last N lines attached to alert)
#                     defaults to <AGENT_ROOT>/.telegram_bot/logs/<basename-label>.stdout.log
#   --env             path to .env for push.sh (defaults to AGENT_ROOT/.telegram_bot/.env)
#   --queue           opt-in machine-global concurrency queue class (DGN-360).
#                     Jobs sharing a class across ALL instance roots on this
#                     machine contend for the same slots. Omitted = no queue,
#                     exact legacy behavior (zero-risk default).
#   --slots           max concurrent jobs in the class (requires --queue).
#                     Class defaults: heavy -> 2; any other class -> 1.
#   --queue-timeout   max seconds to wait for a slot (requires --queue).
#                     Default 3600. Policy is FAIL-OPEN: on timeout, log WARN
#                     and run anyway (availability beats strict serialization).
#   --                separates wrapper args from the real command
#
# Behaviour:
#   1. Run the wrapped command, capture exit code.
#   2. If exit == 0: silent.
#   3. If exit != 0: check dedup file. If already fired today for this label,
#      suppress. Otherwise fire push.sh --text alert and write dedup marker.
#
# Dedup store: /tmp/dogany-cron-guard/<label>.<YYYYMMDD>
#   One file per (label, date). Cleaned up by the OS on reboot; no maintenance needed.
#
# Queue lock home: ~/.dogany/cron-queue/<class>/slot-N/  (machine-global).
#   macOS ships no flock(1), so each slot is an atomic-mkdir spinlock with a
#   pidfile. A slot whose owner pid is dead is reclaimed (crash/SIGKILL safe).
#   Waiters poll with sleep + jitter (2-5 s) to avoid thundering-herd rescans.
#
# push.sh location: resolved relative to this script (both live in routines/).
# push.sh --env: instance .env; no hardcoded tokens.
#
# Exit code: this wrapper exits with the same code as the wrapped command
#   so launchd's LastExitStatus is preserved.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LABEL=""
FRIENDLY_NAME=""
LOG_PATH=""
ENV_PATH="$AGENT_ROOT/.telegram_bot/.env"
DEDUP_DIR="/tmp/dogany-cron-guard"
QUEUE_CLASS=""
QUEUE_SLOTS=""
QUEUE_TIMEOUT=""
QUEUE_BASE_DIR="$HOME/.dogany/cron-queue"
QUEUE_SLOT_DIR=""

# ---- parse wrapper args (before --) ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)         LABEL="$2"; shift 2 ;;
    --friendly-name) FRIENDLY_NAME="$2"; shift 2 ;;
    --log)           LOG_PATH="$2"; shift 2 ;;
    --env)           ENV_PATH="$2"; shift 2 ;;
    --queue)         QUEUE_CLASS="$2"; shift 2 ;;
    --slots)         QUEUE_SLOTS="$2"; shift 2 ;;
    --queue-timeout) QUEUE_TIMEOUT="$2"; shift 2 ;;
    --)              shift; break ;;
    *) echo "[cron-guard] unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$LABEL" ]]; then
  echo "[cron-guard] --label is required" >&2; exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "[cron-guard] no command given after --" >&2; exit 1
fi

# ---- machine-global cron queue (opt-in; DGN-360) ----
# No --queue given: the whole block below is skipped and the wrapper behaves
# exactly like the pre-queue version (no traps, no extra filesystem access).

# release_queue_slot: free the held slot (rm the lock dir). No-op if none held.
release_queue_slot() {
  if [[ -n "$QUEUE_SLOT_DIR" ]]; then
    rm -rf "$QUEUE_SLOT_DIR" 2>/dev/null || true
    QUEUE_SLOT_DIR=""
  fi
}

# file_mtime <path>: epoch mtime; macOS (stat -f) first, GNU (stat -c) fallback.
file_mtime() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null
}

# reclaim_queue_slot <slot_dir>: remove a stale slot via atomic rename so two
# racing waiters cannot both delete it (only one mv succeeds; the loser sees
# ENOENT and simply retries mkdir). Returns 0 if this process did the reclaim.
reclaim_queue_slot() {
  local slot_dir="$1" trash
  trash="$QUEUE_CLASS_DIR/.reclaim.$$.$RANDOM"
  if mv "$slot_dir" "$trash" 2>/dev/null; then
    rm -rf "$trash" 2>/dev/null || true
    return 0
  fi
  return 1
}

# try_queue_slot <slot_dir>: attempt to acquire one slot.
#   0 = acquired (QUEUE_SLOT_DIR set), 1 = busy.
try_queue_slot() {
  local slot_dir="$1" owner_pid owner_pid2 dir_mtime now
  if mkdir "$slot_dir" 2>/dev/null; then
    QUEUE_SLOT_DIR="$slot_dir"
    echo "$$" > "$slot_dir/pid"
    return 0
  fi

  # Slot occupied: stale-owner detection.
  owner_pid="$(cat "$slot_dir/pid" 2>/dev/null || true)"
  if [[ -n "$owner_pid" ]]; then
    if kill -0 "$owner_pid" 2>/dev/null; then
      return 1  # live owner
    fi
    # Owner pid is dead. Re-read the pidfile just before reclaiming to shrink
    # the window where a fresh acquirer replaced the slot under our feet.
    owner_pid2="$(cat "$slot_dir/pid" 2>/dev/null || true)"
    if [[ "$owner_pid2" == "$owner_pid" ]] && reclaim_queue_slot "$slot_dir"; then
      echo "[cron-guard] reclaimed stale queue slot (dead pid $owner_pid): $slot_dir" >&2
      if mkdir "$slot_dir" 2>/dev/null; then
        QUEUE_SLOT_DIR="$slot_dir"
        echo "$$" > "$slot_dir/pid"
        return 0
      fi
    fi
    return 1
  fi

  # No pidfile: owner may be between mkdir and pidfile write. Only treat as
  # stale (crash in that gap) if the dir is older than 60 s.
  dir_mtime="$(file_mtime "$slot_dir")"
  now="$(date +%s)"
  if [[ -n "$dir_mtime" ]] && [[ $(( now - dir_mtime )) -gt 60 ]]; then
    if reclaim_queue_slot "$slot_dir"; then
      echo "[cron-guard] reclaimed abandoned queue slot (no pidfile, age>60s): $slot_dir" >&2
      if mkdir "$slot_dir" 2>/dev/null; then
        QUEUE_SLOT_DIR="$slot_dir"
        echo "$$" > "$slot_dir/pid"
        return 0
      fi
    fi
  fi
  return 1
}

# acquire_queue_slot: scan slot-1..slot-N until acquired or deadline passes.
#   0 = acquired, 1 = timed out (caller applies fail-open policy).
acquire_queue_slot() {
  local deadline i notice=0
  deadline=$(( $(date +%s) + QUEUE_TIMEOUT ))
  while :; do
    i=1
    while [[ $i -le $QUEUE_SLOTS ]]; do
      if try_queue_slot "$QUEUE_CLASS_DIR/slot-$i"; then
        return 0
      fi
      i=$((i + 1))
    done
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
      return 1
    fi
    if [[ "$notice" -eq 0 ]]; then
      echo "[cron-guard] queue wait: class=$QUEUE_CLASS slots=$QUEUE_SLOTS label=$LABEL" >&2
      notice=1
    fi
    # poll with jitter: 2-5 s between scans
    sleep $(( 2 + RANDOM % 4 ))
  done
}

if [[ -n "$QUEUE_CLASS" ]]; then
  # class name becomes a single path segment -- restrict charset
  case "$QUEUE_CLASS" in
    *[!A-Za-z0-9._-]*)
      echo "[cron-guard] invalid --queue class (allowed: A-Za-z0-9._-): $QUEUE_CLASS" >&2
      exit 1 ;;
  esac

  # class defaults: heavy -> slots=2; any other class -> slots=1; timeout 3600
  if [[ -z "$QUEUE_SLOTS" ]]; then
    case "$QUEUE_CLASS" in
      heavy) QUEUE_SLOTS=2 ;;
      *)     QUEUE_SLOTS=1 ;;
    esac
  fi
  if [[ -z "$QUEUE_TIMEOUT" ]]; then
    QUEUE_TIMEOUT=3600
  fi

  case "$QUEUE_SLOTS" in
    ''|0|*[!0-9]*) echo "[cron-guard] invalid --slots (positive integer): $QUEUE_SLOTS" >&2; exit 1 ;;
  esac
  case "$QUEUE_TIMEOUT" in
    ''|*[!0-9]*) echo "[cron-guard] invalid --queue-timeout (seconds): $QUEUE_TIMEOUT" >&2; exit 1 ;;
  esac

  QUEUE_CLASS_DIR="$QUEUE_BASE_DIR/$QUEUE_CLASS"
  mkdir -p "$QUEUE_CLASS_DIR"

  # Free the slot on ANY exit. Signal traps route through exit so the EXIT
  # trap fires on TERM/INT/HUP mid-run too. SIGKILL / hard crash leaves the
  # slot behind -- covered by dead-pid stale reclaim above.
  trap release_queue_slot EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  QUEUE_WAIT_START="$(date +%s)"
  if ! acquire_queue_slot; then
    # FAIL-OPEN: availability of backups/consolidation beats serialization.
    echo "[cron-guard] WARN: queue timeout after $(( $(date +%s) - QUEUE_WAIT_START ))s (class=$QUEUE_CLASS slots=$QUEUE_SLOTS timeout=${QUEUE_TIMEOUT}s label=$LABEL) -- running anyway (fail-open)" >&2
  fi
elif [[ -n "$QUEUE_SLOTS" || -n "$QUEUE_TIMEOUT" ]]; then
  echo "[cron-guard] --slots/--queue-timeout require --queue <class>" >&2
  exit 1
fi

# ---- default log path ----
# derive from label: take the last segment after the final dot
if [[ -z "$LOG_PATH" ]]; then
  LOG_BASENAME="${LABEL##*.}"
  LOG_PATH="$AGENT_ROOT/.telegram_bot/logs/${LOG_BASENAME}.stdout.log"
fi

# ---- run the wrapped command ----
EXIT_CODE=0
"$@" || EXIT_CODE=$?

# ---- success path: silent ----
if [[ "$EXIT_CODE" -eq 0 ]]; then
  exit 0
fi

# ---- failure path ----
TODAY="$(date +%Y%m%d)"
DEDUP_FILE="$DEDUP_DIR/${LABEL}.${TODAY}"

mkdir -p "$DEDUP_DIR"

if [[ -f "$DEDUP_FILE" ]]; then
  # already alerted today -- suppress
  echo "[cron-guard] suppressed (dedup): $LABEL exit=$EXIT_CODE" >&2
  exit "$EXIT_CODE"
fi

# ---- first failure today: build alert and push ----
# grab last 10 lines of log (best-effort; prefer stderr when non-empty, fall back to stdout)
LOG_TAIL=""
LOG_SOURCE=""
STDERR_LOG_PATH="${LOG_PATH%.stdout.log}.stderr.log"
if [[ -s "$STDERR_LOG_PATH" ]]; then
  LOG_TAIL="$(tail -10 "$STDERR_LOG_PATH" 2>/dev/null || true)"
  LOG_SOURCE="$STDERR_LOG_PATH"
fi
if [[ -z "$LOG_TAIL" && -s "$LOG_PATH" ]]; then
  LOG_TAIL="$(tail -10 "$LOG_PATH" 2>/dev/null || true)"
  LOG_SOURCE="$LOG_PATH"
fi

# Determine headline: friendly name if provided, else last dot-segment of label.
if [[ -n "$FRIENDLY_NAME" ]]; then
  HEADLINE="$FRIENDLY_NAME"
else
  HEADLINE="${LABEL##*.}"
fi

# Compose alert text (ASCII only; no emojis per project rules).
# Headline = human-readable job name; label shown as secondary ops detail.
ALERT="[cron-guard] ROUTINE FAILED: $HEADLINE
label : $LABEL
exit  : $EXIT_CODE
date  : $(date '+%Y-%m-%d %H:%M:%S %Z')"

if [[ -n "$LOG_TAIL" ]]; then
  ALERT="$ALERT
--- log tail ($LOG_SOURCE) ---
$LOG_TAIL"
fi

# ---- write dedup marker BEFORE pushing (prevent duplicate on push failure) ----
touch "$DEDUP_FILE"

# ---- push alert ----
PUSH_SH="$SCRIPT_DIR/push.sh"
if [[ ! -x "$PUSH_SH" ]]; then
  echo "[cron-guard] push.sh not found or not executable: $PUSH_SH" >&2
  exit "$EXIT_CODE"
fi

if [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]]; then
  "$PUSH_SH" --text "$ALERT" --env "$ENV_PATH" >&2 || true
else
  "$PUSH_SH" --text "$ALERT" >&2 || true
fi

exit "$EXIT_CODE"

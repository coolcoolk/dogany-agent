#!/bin/bash
# cron-guard.sh -- exit!=0 visibility wrapper for launchd routines.
#
# Usage:
#   cron-guard.sh --label <launchd-label> [--friendly-name <text>] \
#                 [--log <stdout-log>] [--env <dotenv>] \
#                 -- <cmd> [args...]
#
#   --label           launchd job label (used as dedup key; shown as detail in alerts)
#   --friendly-name   human-readable job name for alert headline (optional).
#                     If omitted, the label's last dot-segment is used as headline.
#   --log             path to the job's stdout/stderr log (last N lines attached to alert)
#                     defaults to <AGENT_ROOT>/.telegram_bot/logs/<basename-label>.stdout.log
#   --env             path to .env for push.sh (defaults to AGENT_ROOT/.telegram_bot/.env)
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

# ---- parse wrapper args (before --) ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)         LABEL="$2"; shift 2 ;;
    --friendly-name) FRIENDLY_NAME="$2"; shift 2 ;;
    --log)           LOG_PATH="$2"; shift 2 ;;
    --env)           ENV_PATH="$2"; shift 2 ;;
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
# grab last 10 lines of the log (best-effort; skip if log absent/empty)
LOG_TAIL=""
if [[ -s "$LOG_PATH" ]]; then
  LOG_TAIL="$(tail -10 "$LOG_PATH" 2>/dev/null || true)"
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
--- log tail ($LOG_PATH) ---
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

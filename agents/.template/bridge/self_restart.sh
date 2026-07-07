#!/bin/bash
# self_restart.sh -- safe self-restart of the metal bridge with auto Telegram notify.
#
# Why: restarting the bridge severs the live claude session, so nobody is left
# to tell 사용자 "it came back". This detaches from the caller, SIGTERMs the bridge
# (launchd KeepAlive revives it with new code), waits until polling is REALLY up
# (log marker, not just a live pid -> catches zombie-poll B2), then pushes a
# Telegram message via push.sh on the metal bot. Optional --verify runs a
# headless claude check after restart and includes its result in the notify.
#
# Usage:
#   self_restart.sh --reason "download timeout fix"
#   self_restart.sh --reason "..." --verify "방금 적용한 read_timeout이 라이브 코드에 있는지 한 줄로 확인"
#   self_restart.sh --reason "..." --dry-run        # no kill; exercises notify path only
#
# Flags:
#   --reason TEXT   (required) shown in the notify message
#   --verify PROMPT (optional) headless claude -p after restart; output appended to notify
#   --model NAME    (optional) model for --verify (default haiku)
#   --delay N       (optional) seconds before SIGTERM, lets the current turn flush (default 6)
#   --label LABEL   (optional) launchd label (default com.telegram-skill-bot.telegram-claude)
#   --env PATH      (optional) agent bot .env for push.sh (default workspace .telegram_bot/.env)
#   --prefix EMOJI  (optional) emoji prefix in notify messages (default: [agent])
#   --dry-run       (optional) skip the kill; test the wait+notify wiring
#
# Exit codes: 0 restarted+polling up / 2 came back but polling marker missing / 3 setup error
set -euo pipefail

LABEL="com.telegram-skill-bot.telegram-agent"
REASON=""
VERIFY=""
MODEL="haiku"
DELAY=6
ENV_FILE="__PROJECT_ROOT__/.telegram_bot/.env"
PUSH="__PROJECT_ROOT__/routines/push.sh"
MARKER_LOG="__PROJECT_ROOT__/.telegram_bot/logs/bot.log"
POLL_MARKER="Bot is running"
PREFIX="[agent]"
DRY_RUN=""
WORKER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason)  REASON="$2"; shift 2 ;;
    --verify)  VERIFY="$2"; shift 2 ;;
    --model)   MODEL="$2"; shift 2 ;;
    --delay)   DELAY="$2"; shift 2 ;;
    --label)   LABEL="$2"; shift 2 ;;
    --env)     ENV_FILE="$2"; shift 2 ;;
    --prefix)  PREFIX="$2"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift 1 ;;
    --_worker) WORKER="true"; shift 1 ;;
    *) echo "unknown arg: $1" >&2; exit 3 ;;
  esac
done

[[ -z "$REASON" ]] && { echo "need --reason" >&2; exit 3; }
[[ -x "$PUSH" ]]   || { echo "push.sh not executable at $PUSH" >&2; exit 3; }

notify() { "$PUSH" --env "$ENV_FILE" --text "$1" || echo "[self_restart] push failed" >&2; }
cur_pid() { launchctl list | awk -v l="$LABEL" '$3==l && $1 ~ /^[0-9]+$/ {print $1}'; }

# ---- Detach: re-exec ourselves in a new session so killing the bridge does not
# take this worker (or the caller's claude session) down with it. ----
if [[ -z "$WORKER" ]]; then
  ARGS=(--_worker --reason "$REASON" --model "$MODEL" --delay "$DELAY" --label "$LABEL" --env "$ENV_FILE" --prefix "$PREFIX")
  [[ -n "$VERIFY" ]]  && ARGS+=(--verify "$VERIFY")
  [[ -n "$DRY_RUN" ]] && ARGS+=(--dry-run)
  # Resolve $0 to an absolute path BEFORE re-exec. If invoked as a bare relative
  # name (e.g. `bash self_restart.sh`), nohup looks it up in PATH (not cwd) and
  # fails with "No such file or directory" -> worker never starts, no restart.
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  # nohup + background detaches from the caller's session (SIGHUP-proof); the
  # bridge SIGTERM targets only the bridge pid, so this worker survives it.
  # (macOS has no setsid; this is the same pattern used for prior safe restarts.)
  nohup "$SELF" "${ARGS[@]}" >>"__PROJECT_ROOT__/.telegram_bot/logs/self_restart.log" 2>&1 &
  disown 2>/dev/null || true
  echo "[self_restart] detached worker (pid $!), restart in ${DELAY}s; you will be notified on the agent bot."
  exit 0
fi

# ---- Worker (detached) ----
OLD_PID="$(cur_pid || true)"
echo "[$(date '+%F %T')] worker start: reason='$REASON' old_pid=${OLD_PID:-none} dry=${DRY_RUN:-no}"
sleep "$DELAY"

# Mark current end of log so we only match a marker emitted AFTER the restart.
LOG_BASE=0
[[ -f "$MARKER_LOG" ]] && LOG_BASE="$(wc -l < "$MARKER_LOG" | tr -d ' ')"

if [[ -z "$DRY_RUN" ]]; then
  if [[ -n "$OLD_PID" ]]; then
    kill -TERM "$OLD_PID" 2>/dev/null || true
  else
    echo "[self_restart] no live pid for $LABEL; kickstarting" >&2
    launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
  fi
fi

# ---- Wait for polling to be REALLY up: new pid + fresh "Bot is running" marker. ----
NEW_PID=""; POLL_UP=""
for _ in $(seq 1 60); do
  NEW_PID="$(cur_pid || true)"
  if [[ -f "$MARKER_LOG" ]]; then
    TOTAL="$(wc -l < "$MARKER_LOG" | tr -d ' ')"
    if [[ "$TOTAL" -gt "$LOG_BASE" ]] && \
       tail -n "$((TOTAL - LOG_BASE))" "$MARKER_LOG" | grep -q "$POLL_MARKER"; then
      POLL_UP="true"
    fi
  fi
  if [[ -n "$DRY_RUN" ]]; then POLL_UP="true"; NEW_PID="${OLD_PID:-dryrun}"; fi
  [[ -n "$POLL_UP" && -n "$NEW_PID" && "$NEW_PID" != "$OLD_PID" ]] && break
  [[ -n "$DRY_RUN" && -n "$POLL_UP" ]] && break
  sleep 1
done

# ---- Optional verify (headless claude) ----
VERIFY_OUT=""
if [[ -n "$VERIFY" && -z "$DRY_RUN" ]]; then
  VERIFY_OUT="$(claude -p "$VERIFY" --model "$MODEL" 2>/dev/null | head -c 800 || true)"
fi

# ---- Notify ----
if [[ -n "$POLL_UP" ]]; then
  MSG="${PREFIX} 재기동 완료: ${REASON}
pid ${OLD_PID:-?} → ${NEW_PID:-?}, 폴링 정상."
  [[ -n "$DRY_RUN" ]] && MSG="${PREFIX} [DRY-RUN] 재기동 통보 경로 정상: ${REASON}"
  [[ -n "$VERIFY_OUT" ]] && MSG="${MSG}
검증: ${VERIFY_OUT}"
  notify "$MSG"
  echo "[$(date '+%F %T')] done OK new_pid=${NEW_PID}"
  exit 0
else
  notify "⚠️ 재기동 이상: ${REASON}
새 pid=${NEW_PID:-none} 떴으나 '${POLL_MARKER}' 마커 60s 내 안 보임(좀비폴링 의심). 확인 필요."
  echo "[$(date '+%F %T')] WARN polling marker missing new_pid=${NEW_PID:-none}" >&2
  exit 2
fi

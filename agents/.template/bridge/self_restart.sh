#!/bin/bash
# self_restart.sh -- safe self-restart of the bridge with auto Telegram notify.
#
# Why: restarting the bridge severs the live claude session, so nobody is left
# to tell the user "it came back". This detaches from the caller, SIGTERMs the
# bridge (launchd KeepAlive revives it with new code), waits until polling is
# REALLY up (log marker, not just a live pid -> catches zombie-poll B2), then
# pushes a Telegram message. Optional --verify runs a headless claude check
# after restart and includes its result in the notify.
#
# DGN-226: on a successful (non-dry-run) restart the worker also drops a
# verification instruction into the session-inbox spool (DGN-217), so the
# RESUMED live session verifies real state itself -- silent (NO_PUSH) when
# healthy, warns the owner when broken. The owner no longer has to check.
#
# Usage:
#   self_restart.sh --reason "download timeout fix"
#   self_restart.sh --reason "..." --verify "check that fix landed in running code"
#   self_restart.sh --reason "..." --dry-run        # no kill; exercises notify path only
#
# Flags:
#   --reason TEXT   (required) technical reason; shown in the notify message
#                   only when --notice is absent, always kept in the worker log
#   --notice TEXT   (optional) user-facing notify body in the agent's persona
#                   voice (DGN-233). When set, the success notify is
#                   "PREFIX NOTICE" -- no pid, no technical reason. For a
#                   version-update restart, compose it release-note style
#                   (what changed for the user, not dev jargon). Failure
#                   notify always stays technical.
#   --verify PROMPT (optional) headless claude -p after restart; output appended to notify
#   --model NAME    (optional) model for --verify (default haiku)
#   --delay N       (optional) seconds before SIGTERM, lets the current turn flush (default 6)
#   --label LABEL   (optional) launchd label (default com.telegram-skill-bot.__AGENT_NAME__)
#   --env PATH      (optional) agent bot .env for push.sh (default workspace .telegram_bot/.env)
#   --prefix EMOJI  (optional) emoji prefix in notify messages (default: instance prefix, fallback [agent])
#   --dry-run       (optional) skip the kill; test the wait+notify wiring
#
# Exit codes: 0 restarted+polling up / 2 came back but polling marker missing / 3 setup error
set -euo pipefail

LABEL="com.telegram-skill-bot.__AGENT_NAME__"
REASON=""
NOTICE=""
VERIFY=""
MODEL="haiku"
DELAY=6
ENV_FILE="__PROJECT_ROOT__/.telegram_bot/.env"
PUSH="__PROJECT_ROOT__/routines/push.sh"
MARKER_LOG="__PROJECT_ROOT__/.telegram_bot/logs/bot.log"
POLL_MARKER="Bot is running"
PREFIX="__AGENT_PREFIX__"
DRY_RUN=""
WORKER=""
FORCE=""
IDLE_MINS=10

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason)  REASON="$2"; shift 2 ;;
    --notice)  NOTICE="$2"; shift 2 ;;
    --verify)  VERIFY="$2"; shift 2 ;;
    --model)   MODEL="$2"; shift 2 ;;
    --delay)   DELAY="$2"; shift 2 ;;
    --label)   LABEL="$2"; shift 2 ;;
    --env)     ENV_FILE="$2"; shift 2 ;;
    --prefix)  PREFIX="$2"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift 1 ;;
    --force)      FORCE="true"; shift 1 ;;
    --idle-mins)  IDLE_MINS="$2"; shift 2 ;;
    --_worker) WORKER="true"; shift 1 ;;
    *) echo "unknown arg: $1" >&2; exit 3 ;;
  esac
done

[[ -z "$REASON" ]] && { echo "need --reason" >&2; exit 3; }
[[ -x "$PUSH" ]]   || { echo "push.sh not executable at $PUSH" >&2; exit 3; }

notify() { "$PUSH" --env "$ENV_FILE" --text "$1" || echo "[self_restart] push failed" >&2; }
cur_pid() { launchctl list | awk -v l="$LABEL" '$3==l && $1 ~ /^[0-9]+$/ {print $1}'; }

# ---- Idle guard: refuse restart while the user is mid-session (DGN-328). ----
# Derives the Claude Code project transcript dir from this instance's root path
# using the same sanitize rule as Claude Code: replace every non-alphanumeric
# character with '-'. Checks the newest-modified *.jsonl file; if it was
# touched within IDLE_MINS minutes we treat the session as active and refuse.
# Fail-open: if the transcript dir is missing or has no jsonl files, print a
# warning and proceed so an emergency restart is never bricked.
check_idle_guard() {
  if [[ -n "$FORCE" ]]; then
    echo "[self_restart] --force set; skipping idle guard"
    return 0
  fi
  local instance_root
  instance_root="$(cd "$(dirname "$0")/.." && pwd)"
  local encoded_root
  encoded_root="$(echo "$instance_root" | sed 's/[^a-zA-Z0-9]/-/g')"
  local transcript_dir="${HOME}/.claude/projects/${encoded_root}"
  if [[ ! -d "$transcript_dir" ]]; then
    echo "[self_restart] WARN idle guard: transcript dir not found (${transcript_dir}); proceeding" >&2
    return 0
  fi
  local newest_jsonl
  newest_jsonl="$(find "$transcript_dir" -maxdepth 1 -name '*.jsonl' -type f \
    -exec stat -f '%m %N' {} \; 2>/dev/null \
    | sort -rn | head -1 | awk '{print $2}')"
  if [[ -z "$newest_jsonl" ]]; then
    echo "[self_restart] WARN idle guard: no jsonl transcripts found in ${transcript_dir}; proceeding" >&2
    return 0
  fi
  local file_mtime now_epoch age_secs threshold_secs
  file_mtime="$(stat -f '%m' "$newest_jsonl" 2>/dev/null || echo 0)"
  now_epoch="$(date '+%s')"
  age_secs=$(( now_epoch - file_mtime ))
  threshold_secs=$(( IDLE_MINS * 60 ))
  if [[ "$age_secs" -lt "$threshold_secs" ]]; then
    local last_activity_ts
    last_activity_ts="$(date -r "$file_mtime" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
      || date -d "@${file_mtime}" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
      || echo "epoch=${file_mtime}")"
    echo "[self_restart] REFUSED: session active -- last activity ${last_activity_ts} (${age_secs}s ago, threshold ${IDLE_MINS}m). Use --force to override." >&2
    exit 1
  fi
  echo "[self_restart] idle guard OK: last activity ${age_secs}s ago (threshold ${IDLE_MINS}m)"
}

SPOOL_DIR="__PROJECT_ROOT__/.telegram_bot/session-inbox"

# DGN-226: hand post-restart verification to the resumed live session via the
# DGN-217 session-inbox spool. Writer contract: temp write, then atomic rename
# to *.md so a half-written file is never picked up.
drop_verify_spool() {
  mkdir -p "$SPOOL_DIR" || { echo "[self_restart] spool dir unavailable" >&2; return 0; }
  local ts name tmp
  ts="$(date '+%Y%m%d-%H%M%S')"
  name="restart-verify-${ts}.md"
  tmp="${SPOOL_DIR}/.${name}.tmp"
  cat >"$tmp" <<EOF
[cron-inject] post-restart self-verification (DGN-226)

The bridge just self-restarted. Reason: ${REASON}
pid ${OLD_PID:-?} -> ${NEW_PID:-?}. The completion notice was already pushed
to the owner; do NOT repeat it.

Verify the real state yourself now:
1. Bridge process alive for label ${LABEL}; pid matches ${NEW_PID:-?}.
2. Tail ${MARKER_LOG}: no ERROR burst after the restart.
3. Spot-check that the restart reason above actually landed in running code.
4. Resume interrupted work: check worklog/ tickets with status wip owned by
   the agent, and any queued session-inbox items. If this restart cut off an
   in-flight task, pick it up and continue autonomously.

If everything is healthy AND nothing needs resuming: append one line
(self-verify OK + timestamp) to the worklog ticket this restart belongs to,
and end your output with the bare word NO_PUSH. If you resumed work, report
what you resumed (no NO_PUSH). If anything is broken: warn the owner
immediately (no NO_PUSH).
EOF
  mv "$tmp" "${SPOOL_DIR}/${name}" \
    && echo "[$(date '+%F %T')] verify spool dropped: ${name}" \
    || echo "[self_restart] spool drop failed" >&2
}

# ---- Detach: re-exec ourselves in a new session so killing the bridge does not
# take this worker (or the caller's claude session) down with it. ----
if [[ -z "$WORKER" ]]; then
  # Idle guard (DGN-328): runs in the launcher, before detach; --dry-run included.
  check_idle_guard

  ARGS=(--_worker --reason "$REASON" --model "$MODEL" --delay "$DELAY" --label "$LABEL" --env "$ENV_FILE" --prefix "$PREFIX")
  [[ -n "$NOTICE" ]]  && ARGS+=(--notice "$NOTICE")
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
  if [[ -n "$NOTICE" ]]; then
    # Persona notify (DGN-233): user-facing body only; pid/reason stay in
    # this worker log (echoed at worker start + done lines).
    MSG="${PREFIX} ${NOTICE}"
  else
    MSG="${PREFIX} 재기동 완료: ${REASON}
pid ${OLD_PID:-?} → ${NEW_PID:-?}, 폴링 정상."
  fi
  [[ -n "$DRY_RUN" ]] && MSG="${PREFIX} [DRY-RUN] 재기동 통보 경로 정상: ${REASON}"
  [[ -n "$VERIFY_OUT" ]] && MSG="${MSG}
검증: ${VERIFY_OUT}"
  [[ -z "$DRY_RUN" ]] && drop_verify_spool
  notify "$MSG"
  echo "[$(date '+%F %T')] done OK new_pid=${NEW_PID}"
  exit 0
else
  notify "⚠️ 재기동 이상: ${REASON}
새 pid=${NEW_PID:-none} 떴으나 '${POLL_MARKER}' 마커 60s 내 안 보임(좀비폴링 의심). 확인 필요."
  echo "[$(date '+%F %T')] WARN polling marker missing new_pid=${NEW_PID:-none}" >&2
  exit 2
fi

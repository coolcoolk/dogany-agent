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
#   --resume-intent TEXT (optional; DGN-706) in-flight task + next concrete
#                   action at restart time. When set, the post-restart spool
#                   (DGN-226) step 4 hands the resumed session this exact context
#                   to continue -- instead of guessing from chronically-open wip
#                   tickets. When omitted, the caller asserts NO in-flight task
#                   and the resumed session is told not to hunt wip. Wire this
#                   whenever you trigger a restart with work still pending.
#   --resume-label TEXT (optional; DGN-834) short user-facing label for the
#                   in-flight task shown in the restart completion push. When
#                   set with --resume-intent, this label is used verbatim in
#                   the push ("재시작 완료 — LABEL 이어서 진행합니다."). When
#                   omitted, derived from the first clause of RESUME_INTENT (up
#                   to the first colon or newline); falls back to "직전 작업"
#                   when derivation yields nothing. Has no effect without
#                   --resume-intent.
#   --model NAME    (optional) model for --verify (default haiku)
#   --delay N       (optional) seconds before SIGTERM, lets the current turn flush (default 6)
#   --label LABEL   (optional) launchd label (default com.telegram-skill-bot.__AGENT_NAME__)
#   --env PATH      (optional) agent bot .env for push.sh (default workspace .telegram_bot/.env)
#   --prefix EMOJI  (optional) emoji prefix in notify messages (default: instance prefix, fallback [agent])
#   --trigger T     (optional) user|auto (default auto; DGN-546). user = explicit
#                   owner command -> idle guard skipped entirely (semantic alias
#                   of --force). auto = autonomous restart -> idle guard applies;
#                   on refusal exit quietly (defer to next natural restart).
#   --dry-run       (optional) skip the kill; test the wait+notify wiring
#   --skip-smoke    (optional) bypass the pre-restart import smoke gate
#                   (DGN-712). Default is gate ON: before killing the running
#                   bridge, the new code is import-smoke-tested with the
#                   instance venv (`python -m bridge --selfcheck`); a failing
#                   import ABORTS the restart, keeps the old bridge alive, and
#                   warns the owner -- so a half-landed/stale bridge can never
#                   brick an instance into a watchdog restart loop. Use this
#                   flag only for a deliberate forced restart.
#
# Exit codes: 0 restarted+polling up / 2 came back but polling marker missing /
#             3 setup error / 4 aborted: pre-restart import smoke test failed
#
# DGN-964 OPERATOR GUARD -- restart the bridge with THIS script, never with
# raw launchctl. In particular `launchctl bootout gui/<uid>/<label>`
# UNREGISTERS the label: KeepAlive dies with it, nothing revives the bridge,
# and the watchdog can re-register the label only when the DGN-888 service
# marker (.telegram_bot/.service_plist) is present -- a bare bootout on a
# marker-less install is a silent permanent outage (2026-08-15 and 2026-08-21
# incidents). This script SIGTERMs the bridge pid instead and lets launchd's
# KeepAlive revive it with new code -- registration is never dropped. If a
# bootout is truly unavoidable (plist replacement), pair it with the
# bootstrap in the same breath:
#   launchctl bootout gui/$(id -u)/<label>; launchctl bootstrap gui/$(id -u) <plist>
set -euo pipefail

# UTF-8 locale regardless of caller env (cron/launchd may leave LANG/LC_ALL
# unset -> C locale). VERIFY_OUT below is a headless-claude response that can
# be Korean, and `head -c` truncates by BYTES regardless of locale -- so the
# fix is `cut -c` (POSIX character count, but ONLY under a UTF-8 locale;
# under C it too would count bytes). Pattern reused verbatim from
# routines/self-update.sh (DGN-1059): probe candidates and take the first
# whose charmap really is UTF-8 (C.UTF-8 is built into glibc >= 2.35, Ubuntu
# 22.04+, and present on macOS) -- setting a missing locale falls back to C
# SILENTLY, so a blind export would be inert. The probe must never kill the
# script under `set -e` -- hence 2>/dev/null + `|| true`.
_utf8_loc=""
for _cand in en_US.UTF-8 C.UTF-8; do
  if [ "$(LC_ALL="$_cand" locale charmap 2>/dev/null || true)" = "UTF-8" ]; then
    _utf8_loc="$_cand"
    break
  fi
done
if [ -n "$_utf8_loc" ]; then
  export LC_ALL="$_utf8_loc"
  export LANG="$_utf8_loc"
else
  # No UTF-8 locale at all. Do NOT abort the restart over it (restart still
  # works), but never stay silent: silent byte-truncation is the exact
  # defect class this block exists to prevent.
  printf '%s\n' "[self_restart.sh] WARN: no UTF-8 locale available (tried en_US.UTF-8, C.UTF-8); truncation may cut bytes, not characters, and can corrupt multi-byte (e.g. Korean) text" >&2
fi

LABEL="com.telegram-skill-bot.__AGENT_NAME__"
REASON=""
NOTICE=""
VERIFY=""
RESUME_INTENT=""
RESUME_LABEL=""
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
TRIGGER="auto"
SKIP_SMOKE=""

# DGN-706b: derive the instance root from this script's own location
# (bridge/ -> parent). Works in both the launcher and the re-exec'd worker
# ($0 is absolute there). Feeds the version-update auto-notice below.
SELF_BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTANCE_ROOT="$(cd "$SELF_BIN_DIR/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason)  REASON="$2"; shift 2 ;;
    --notice)  NOTICE="$2"; shift 2 ;;
    --verify)  VERIFY="$2"; shift 2 ;;
    --resume-intent) RESUME_INTENT="$2"; shift 2 ;;
    --resume-label)  RESUME_LABEL="$2"; shift 2 ;;
    --model)   MODEL="$2"; shift 2 ;;
    --delay)   DELAY="$2"; shift 2 ;;
    --label)   LABEL="$2"; shift 2 ;;
    --env)     ENV_FILE="$2"; shift 2 ;;
    --prefix)  PREFIX="$2"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift 1 ;;
    --force)      FORCE="true"; shift 1 ;;
    --skip-smoke) SKIP_SMOKE="true"; shift 1 ;;
    --idle-mins)  IDLE_MINS="$2"; shift 2 ;;
    --trigger)    TRIGGER="$2"; shift 2 ;;
    --_worker) WORKER="true"; shift 1 ;;
    *) echo "unknown arg: $1" >&2; exit 3 ;;
  esac
done

[[ -z "$REASON" ]] && { echo "need --reason" >&2; exit 3; }
case "$TRIGGER" in user|auto) ;; *) echo "invalid --trigger '$TRIGGER' (user|auto)" >&2; exit 3 ;; esac
[[ -x "$PUSH" ]]   || { echo "push.sh not executable at $PUSH" >&2; exit 3; }

# DGN-822: push.sh now sanitizes every text send (bridge sanitizer) and always
# transmits parse_mode=HTML. Contract for THIS caller: pass RAW text -- never
# pre-escape & < > (the sanitizer escapes them; pre-escaping double-escapes,
# e.g. "->" would render literally as "-&gt;"). Whitelisted Telegram tags
# (<blockquote expandable>, <b>, ...) stay raw and pass through the sanitizer.
# The old --html flag is a deprecated no-op and is no longer attached.
notify() {
  "$PUSH" --env "$ENV_FILE" --text "$1" || echo "[self_restart] push failed" >&2
}
cur_pid() { launchctl list | awk -v l="$LABEL" '$3==l && $1 ~ /^[0-9]+$/ {print $1}'; }

# DGN-706b: version-update auto-notice. When the applied framework version
# (.instance.conf DOGANY_FW_VERSION) differs from the last version we already
# notified about, and the caller gave NO explicit --notice, compose the
# owner-locked DGN-687 release-note fold from product/releases/v<ver>.md. This
# guarantees an update-restart always tells the owner "update complete + what
# changed" -- even when the restart is triggered manually (decoupled from
# routines/self-update.sh, which composes the same notice on its own path).
# The owner-locked copy is mirrored verbatim here; keep both sites in sync on
# any DGN-687 re-lock. Fail-open at every step (missing conf/notes -> no notice,
# never blocks the restart). Marker advances only when a notice is composed.
maybe_compose_update_notice() {
  local cur last relnote notes fold
  cur="$(sed -n 's/^DOGANY_FW_VERSION=//p' "$INSTANCE_ROOT/.instance.conf" 2>/dev/null | head -n1)"
  [[ -n "$cur" ]] || return 0
  last="$(cat "$VER_MARKER" 2>/dev/null || true)"
  [[ "$cur" == "$last" ]] && return 0
  relnote="$INSTANCE_ROOT/product/releases/v${cur}.md"
  [[ -f "$relnote" ]] || return 0
  notes="$(awk '/^## Summary/{g=1;next} g&&/^(---|## )/{exit} g{print}' "$relnote" | sed -e '/./,$!d' | head -n 12)"
  [[ -n "$notes" ]] || return 0
  fold="Update summary ..."
  # DGN-822: notes go in RAW (no html_esc) -- push.sh's sanitizer escapes
  # entities; the blockquote tags pass its whitelist verbatim.
  NOTICE="Restart complete · v${cur} update applied
<blockquote expandable>${fold}
${notes}</blockquote>"
  mkdir -p "$(dirname "$VER_MARKER")" 2>/dev/null && printf '%s\n' "$cur" >"$VER_MARKER"
  echo "[self_restart] version-update auto-notice composed for v${cur} (was '${last:-none}')"
}

# ---- Pre-restart import smoke gate (DGN-712). --------------------------------
# The restart severs the running bridge and lets launchd revive it with the new
# code. If that new code cannot even be IMPORTED (e.g. an i18n key referenced by
# messages.py at import time never landed on a drifted instance), the revived
# bridge crashes before it writes its poll heartbeat -> the watchdog restart-
# loops until it is rate-limited -> live DOWN. So BEFORE we kill the old bridge
# we dry-import the new code with the instance interpreter; only a clean import
# earns the restart. A failing import aborts and keeps the old bridge alive.
#
# Reuses the existing offline health entry `python -m bridge --selfcheck`
# (bridge/__main__.py), which imports bridge.config + bridge.bot + sdk_bridge
# and resolves the Claude CLI -- the exact import surface the revived process
# executes. Interpreter resolution mirrors start.sh: venv next to bridge/, else
# $BRIDGE_PYTHON, else python3.
smoke_test_import() {
  local script_dir instance_root python pypath out rc
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  instance_root="$(cd "$script_dir/.." && pwd)"
  if [[ -x "$script_dir/venv/bin/python" ]]; then
    python="$script_dir/venv/bin/python"
  elif [[ -n "${BRIDGE_PYTHON:-}" ]]; then
    python="$BRIDGE_PYTHON"
  else
    python="python3"
  fi
  pypath="$instance_root:${PYTHONPATH:-}"
  echo "[self_restart] smoke gate: importing new bridge via $python (root=$instance_root)"
  # --selfcheck prints one line and exits 0 (ok) / 1 (fail); capture for the log.
  out="$(cd "$instance_root" && PYTHONPATH="$pypath" "$python" -m bridge --path "$instance_root" --selfcheck 2>&1)"
  rc=$?
  echo "[self_restart] smoke gate result (rc=$rc): $out"
  if [[ $rc -ne 0 ]]; then
    SMOKE_FAIL_DETAIL="$out"
    return 1
  fi
  return 0
}

# ---- Idle guard: refuse restart while the user is mid-session (DGN-328). ----
# Derives the Claude Code project transcript dir from this instance's root path
# using the same sanitize rule as Claude Code: replace every non-alphanumeric
# character with '-'. Checks the newest-modified *.jsonl file; if it was
# touched within IDLE_MINS minutes we treat the session as active and refuse.
# Fail-open: if the transcript dir is missing or has no jsonl files, print a
# warning and proceed so an emergency restart is never bricked.
check_idle_guard() {
  # DGN-546: explicit owner command outranks the idle guard entirely.
  if [[ "$TRIGGER" == "user" ]]; then
    echo "[self_restart] trigger=user (explicit owner command); skipping idle guard"
    return 0
  fi
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
# DGN-706b: last framework version we auto-notified about (version-update fold).
VER_MARKER="$(dirname "$SPOOL_DIR")/state/last_notified_fw_version"
# DGN-1010 layer-2: unterminated-restart marker. Written by the worker just
# before it severs the bridge; CLAIMED (atomic rename) by whoever emits the
# terminal owner push -- this worker on its normal path, or the NEW bridge's
# backstop task (bot.py _restart_backstop_loop) when this worker died before
# pushing. Exactly one claimant wins the rename, so a restart tap always ends
# in exactly ONE terminal notification: never silence, never a duplicate.
RESTART_MARKER="$(dirname "$SPOOL_DIR")/state/restart-pending.marker"
MARKER_ARMED=""

# Claim the terminal push. Returns 1 when the marker is gone because the
# bridge backstop already terminal-closed this restart (this worker was slow
# enough to be presumed dead) -> caller must SKIP its push (no duplicate).
# Marker never armed (dry-run / write failed) -> this worker owns it trivially.
claim_terminal_push() {
  [[ -z "$MARKER_ARMED" ]] && return 0
  mv "$RESTART_MARKER" "${RESTART_MARKER}.claimed.$$" 2>/dev/null || return 1
  rm -f "${RESTART_MARKER}.claimed.$$" 2>/dev/null || true
  return 0
}

# DGN-226: hand post-restart verification to the resumed live session via the
# DGN-217 session-inbox spool. Writer contract: temp write, then atomic rename
# to *.md so a half-written file is never picked up.
drop_verify_spool() {
  mkdir -p "$SPOOL_DIR" || { echo "[self_restart] spool dir unavailable" >&2; return 0; }
  local ts name tmp resume_step4
  ts="$(date '+%Y%m%d-%H%M%S')"
  name="restart-verify-${ts}.md"
  tmp="${SPOOL_DIR}/.${name}.tmp"

  # DGN-706: step 4 signal depends on whether the caller passed --resume-intent.
  # Present -> hand the resumed session the exact in-flight context to continue.
  # Absent  -> caller asserted no in-flight task; tell it NOT to hunt chronically
  # open wip tickets (that guessing was the old failure mode).
  if [[ -n "$RESUME_INTENT" ]]; then
    resume_step4="4. Resume interrupted work -- this restart CUT OFF an in-flight task. You were
   mid-executing:
     ${RESUME_INTENT}
   Pick it up from there and continue autonomously now. Cross-check the owning
   worklog/ ticket for current state before acting, then carry it forward."
  else
    resume_step4="4. Resume interrupted work: the restart caller asserted NO specific in-flight
   task. Sanity-check queued session-inbox items and any ticket you were
   ACTIVELY executing this very session; if nothing was truly cut off, do NOT
   hunt through chronically-open wip tickets -- treat this as clean."
  fi
  cat >"$tmp" <<EOF
[cron-inject] post-restart self-verification (DGN-226)

The bridge just self-restarted. Reason: ${REASON}
pid ${OLD_PID:-?} -> ${NEW_PID:-?}. The completion notice was already pushed
to the owner; do NOT repeat it.

Verify the real state yourself now:
1. Bridge process alive for label ${LABEL}; pid matches ${NEW_PID:-?}.
2. Tail ${MARKER_LOG}: no ERROR burst after the restart.
3. Spot-check that the restart reason above actually landed in running code.
${resume_step4}

If everything is healthy AND nothing needs resuming: append one line
(self-verify OK + timestamp) to the worklog ticket this restart belongs to,
and end your output with the bare word NO_PUSH. If you resumed work, report
what you resumed directly (no NO_PUSH) -- do NOT prepend a separate resume
notice line (DGN-834: the restart completion push already carried the resume
task name; emitting a second line duplicates the signal). If anything is
broken: warn the owner immediately (no NO_PUSH).
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

  # DGN-706b: if the caller passed no explicit --notice and this is a real
  # restart, auto-compose the version-update release-note fold when the applied
  # framework version changed since we last notified. Runs AFTER the idle guard
  # so a refused restart never advances the marker or emits a stale notice.
  [[ -z "$NOTICE" && -z "$DRY_RUN" ]] && maybe_compose_update_notice

  ARGS=(--_worker --reason "$REASON" --model "$MODEL" --delay "$DELAY" --label "$LABEL" --env "$ENV_FILE" --prefix "$PREFIX")
  [[ -n "$NOTICE" ]]  && ARGS+=(--notice "$NOTICE")
  [[ -n "$VERIFY" ]]  && ARGS+=(--verify "$VERIFY")
  [[ -n "$RESUME_INTENT" ]] && ARGS+=(--resume-intent "$RESUME_INTENT")
  [[ -n "$RESUME_LABEL"  ]] && ARGS+=(--resume-label "$RESUME_LABEL")
  [[ -n "$DRY_RUN" ]] && ARGS+=(--dry-run)
  [[ -n "$SKIP_SMOKE" ]] && ARGS+=(--skip-smoke)
  # Resolve $0 to an absolute path BEFORE re-exec. If invoked as a bare relative
  # name (e.g. `bash self_restart.sh`), the exec looks it up in PATH (not cwd)
  # and fails with "No such file or directory" -> worker never starts, no restart.
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  WORKER_LOG="__PROJECT_ROOT__/.telegram_bot/logs/self_restart.log"
  # DGN-1010: REAL session detach (double-fork + os.setsid), not nohup.
  # nohup only blocks SIGHUP and leaves the worker INSIDE the caller's process
  # group. When the caller is the bridge itself (authsync restart CTA tap /
  # /restart command -> bot.py subprocess.run), the worker lands in the
  # bridge's launchd process group; AbandonProcessGroup defaults to false, so
  # launchd reaps the WHOLE GROUP the moment the bridge pid exits -- the
  # worker died between its own SIGTERM and the completion push (2026-08-22
  # 08:00 silent CTA failure). macOS ships no setsid(1) binary, but Python's
  # os.setsid() is right there: fork (so we are never a group leader), setsid
  # (new session -- out of the bridge's group, unreachable by the launchd
  # cleanup), then exec the worker. Same behavior for every caller (session
  # bash / bridge subprocess / watchdog).
  DETACH_PY=""
  if [[ -x "$SELF_BIN_DIR/venv/bin/python" ]]; then
    DETACH_PY="$SELF_BIN_DIR/venv/bin/python"
  elif [[ -n "${BRIDGE_PYTHON:-}" ]]; then
    DETACH_PY="$BRIDGE_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    DETACH_PY="python3"
  fi
  if [[ -n "$DETACH_PY" ]]; then
    WORKER_PID="$("$DETACH_PY" - "$WORKER_LOG" "$SELF" "${ARGS[@]}" <<'PYEOF'
import os, sys
log_path, target = sys.argv[1], sys.argv[2]
if os.fork() > 0:
    os._exit(0)  # launcher-side parent: return control to the caller now
os.setsid()      # own session: the launchd group cleanup can no longer reach us
print(os.getpid(), flush=True)  # daemon pid -> launcher capture; releases the pipe
devnull = os.open(os.devnull, os.O_RDONLY)
log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
os.dup2(devnull, 0)
os.dup2(log, 1)
os.dup2(log, 2)
os.close(devnull)
os.close(log)
os.execv(target, [target] + sys.argv[3:])
PYEOF
)"
    echo "[self_restart] detached worker (pid ${WORKER_PID:-?}, own session via setsid), restart in ${DELAY}s; you will be notified on the agent bot."
  else
    # Degraded fallback (no python interpreter found -- should not happen on a
    # host that runs the Python bridge): legacy nohup detach. SIGHUP-proof
    # only; when the caller is the bridge itself the worker may die with the
    # bridge's process group (the exact DGN-1010 hole). The layer-2 backstop
    # in bot.py (_restart_backstop_loop) still terminal-closes the restart.
    echo "[self_restart] WARN: no python interpreter for setsid detach; nohup fallback (worker may die with the bridge process group)" >&2
    nohup "$SELF" "${ARGS[@]}" >>"$WORKER_LOG" 2>&1 &
    disown 2>/dev/null || true
    echo "[self_restart] detached worker (pid $!, nohup fallback), restart in ${DELAY}s; you will be notified on the agent bot."
  fi
  exit 0
fi

# ---- Worker (detached) ----
OLD_PID="$(cur_pid || true)"
echo "[$(date '+%F %T')] worker start: reason='$REASON' old_pid=${OLD_PID:-none} dry=${DRY_RUN:-no}"
sleep "$DELAY"

# ---- Pre-restart import smoke gate (DGN-712): verify the NEW bridge imports
# cleanly BEFORE we sever the running one. On failure, abort the restart, leave
# the old bridge process untouched, warn the owner, and exit 4. Skipped for
# --dry-run (no real kill happens) and for an explicit --skip-smoke bypass.
SMOKE_FAIL_DETAIL=""
if [[ -z "$DRY_RUN" && -z "$SKIP_SMOKE" ]]; then
  if ! smoke_test_import; then
    # DGN712 smoke-fail push copy (owner-confirmed 2026-08-03): itemized,
    # non-technical, reassurance + escape hatch. Technical cause stays in the
    # worker log line below, NOT in the user push.
    notify "⚠️ 업데이트 잠시 보류
- 새 버전이 바로 안 떠서 멈췄어요
- 지금 버전 그대로 정상 운영 중 (서비스 이상 없음)
- 원인 확인 후 다시 안내드릴게요
- 바로 처리 원하시면: 다시 시도"
    echo "[$(date '+%F %T')] ABORT: pre-restart smoke gate failed; old bridge kept alive (pid ${OLD_PID:-none}). detail: ${SMOKE_FAIL_DETAIL}" >&2
    exit 4
  fi
elif [[ -n "$SKIP_SMOKE" && -z "$DRY_RUN" ]]; then
  echo "[self_restart] --skip-smoke set; bypassing pre-restart import smoke gate"
fi

# Mark current end of log so we only match a marker emitted AFTER the restart.
LOG_BASE=0
[[ -f "$MARKER_LOG" ]] && LOG_BASE="$(wc -l < "$MARKER_LOG" | tr -d ' ')"

if [[ -z "$DRY_RUN" ]]; then
  # DGN-1010 layer-2: arm the unterminated-restart marker BEFORE severing the
  # bridge. If this worker dies past this point (e.g. reaped with the old
  # bridge's process group), the NEW bridge's backstop finds the marker, sees
  # worker_pid dead, and terminal-closes the restart for the owner. Fail-open:
  # a failed write only disarms the backstop, never blocks the restart.
  if mkdir -p "$(dirname "$RESTART_MARKER")" 2>/dev/null \
     && printf 'ts=%s\nreason=%s\nold_pid=%s\nworker_pid=%s\n' \
          "$(date '+%s')" "$REASON" "${OLD_PID:-}" "$$" >"$RESTART_MARKER" 2>/dev/null; then
    MARKER_ARMED="true"
  else
    echo "[self_restart] WARN: restart marker write failed; layer-2 backstop disarmed for this run" >&2
  fi
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
  # `cut -c` not `head -c`: head -c is a byte-count truncation regardless of
  # locale and would still slice a multi-byte Hangul character in half; cut -c
  # is a POSIX character count under the UTF-8 locale exported above.
  VERIFY_OUT="$(claude -p "$VERIFY" --model "$MODEL" 2>/dev/null | cut -c1-800 || true)"
fi

# ---- Notify ----
if [[ -n "$POLL_UP" ]]; then
  if [[ -n "$NOTICE" ]]; then
    # Persona notify (DGN-233): user-facing body only; pid/reason stay in
    # this worker log (echoed at worker start + done lines).
    MSG="${PREFIX} ${NOTICE}"
  else
    # DGN-687 / DGN-233 / DGN-834: default fallback -- user-facing tone.
    # No REASON (dev jargon) and no pid in the push; both stay in this worker log.
    # When RESUME_INTENT is set, merge a short label into the single push line
    # (2통 -> 1통). Label source: explicit --resume-label (verbatim); else derived
    # from the first clause/line of RESUME_INTENT (up to first colon or newline);
    # ultimate fallback is the generic Korean phrase.
    if [[ -n "$RESUME_INTENT" ]]; then
      _push_label=""
      if [[ -n "$RESUME_LABEL" ]]; then
        _push_label="$RESUME_LABEL"
      else
        _push_label="$(printf '%s' "$RESUME_INTENT" | head -n1 | sed 's/:.*//' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')"
        if [[ -z "$_push_label" ]]; then
          _push_label="직전 작업"
        fi
      fi
      MSG="${PREFIX} 재시작 완료 — ${_push_label} 이어서 진행합니다."
    else
      MSG="${PREFIX} 재시작 완료"
    fi
  fi
  [[ -n "$DRY_RUN" ]] && MSG="${PREFIX} [DRY-RUN] 재시작 통보 경로 정상: ${REASON}"
  [[ -n "$VERIFY_OUT" ]] && MSG="${MSG}
검증: ${VERIFY_OUT}"
  # DGN-1010: claim before pushing. A lost claim means the bridge backstop
  # already terminal-closed this restart -- pushing again would duplicate.
  if ! claim_terminal_push; then
    echo "[$(date '+%F %T')] done OK new_pid=${NEW_PID} (terminal push already claimed by bridge backstop; skipping duplicate)"
    exit 0
  fi
  [[ -z "$DRY_RUN" ]] && drop_verify_spool
  notify "$MSG"
  echo "[$(date '+%F %T')] done OK new_pid=${NEW_PID}"
  exit 0
else
  if claim_terminal_push; then
    notify "⚠️ 재시작 이상: ${REASON}
새 pid=${NEW_PID:-none} 떴으나 '${POLL_MARKER}' 마커 60s 내 안 보임(좀비폴링 의심). 확인 필요."
  else
    echo "[$(date '+%F %T')] WARN push already claimed by bridge backstop; skipping duplicate warn" >&2
  fi
  echo "[$(date '+%F %T')] WARN polling marker missing new_pid=${NEW_PID:-none}" >&2
  exit 2
fi

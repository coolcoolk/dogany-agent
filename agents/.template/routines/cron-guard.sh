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
#   3. If exit == 75 (EX_TEMPFAIL) AND a FRESH usage-defer marker exists
#      (2-factor signal, DGN-835): recovery branch instead of an alert --
#      five_hour defer -> reset-aligned one-shot retry via usage-defer-retry.sh;
#      seven_day defer -> owner notification with a manual-retry button
#      (dedup per label/day) + a reset-aligned fresh-button re-notify
#      one-shot (DGN-841 B) + a /usageretry slash line in the body (DGN-841 A).
#      Both windows: when the marker carries last_success_at and the job has
#      not succeeded for >1d+6h, ONE stale-canon alert per label/day fires
#      (MAJOR-C reader -- paired with the notify dedup, never shipped alone).
#      exit 75 WITHOUT a fresh marker stays on the normal failure-alert path
#      (an accidental 75 from unrelated code must not be silenced).
#   4. Any other exit != 0: check dedup file. If already fired today for this
#      label, suppress. Otherwise fire push.sh --text alert and write dedup marker.
#
# Usage-defer protocol (DGN-835): this wrapper exports
#   DOGANY_USAGE_DEFER_MARKER=~/.dogany/usage-defer/<label>.json
# to the wrapped command. A job that detects usage exhaustion mid-run writes
# that marker ({window, reset_at, deferred_at, reason, last_success_at}) and
# exits 75. last_success_at (optional, ISO) = the job's own last successful
# completion; it feeds the stale-canon alert reader (MAJOR-C). The marker is
# "fresh" when its mtime is within 10 minutes -- stale markers are ignored so
# an old defer cannot hijack a later genuine failure.
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

# Shared i18n helper (config/i18n/<lang>.json lookup, DGN-851). Sourcing also
# loads config/agent.conf (AGENT_LANG); AGENT_ROOT is already set above and
# agentlib preserves it. Vars sourced from agent.conf stay shell-local (not
# exported), so they never leak into the wrapped command's environment.
# FAIL-OPEN: this guard wrapper must never die over a missing helper (it is
# also exercised as a standalone copy in tests) -- absent lib degrades
# i18n_or to its in-code default argument (zero-delta).
if [ -f "$SCRIPT_DIR/lib/agentlib.sh" ]; then
  . "$SCRIPT_DIR/lib/agentlib.sh"
else
  i18n_or() { printf '%s' "$2"; }
fi

# DGN-835: snapshot the FULL original argv before destructive parsing -- the
# usage-defer recovery paths replay this exact invocation (retry one-shot /
# manual replay file), argument-by-argument, quoting-safe.
ORIG_ARGS=("$@")

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
# DGN-835 usage-defer protocol: hand the wrapped command its per-label marker
# path. A usage-aware job writes this marker and exits 75 to request recovery.
USAGE_DEFER_DIR="$HOME/.dogany/usage-defer"
USAGE_DEFER_MARKER="$USAGE_DEFER_DIR/${LABEL}.json"
mkdir -p "$USAGE_DEFER_DIR" 2>/dev/null || true
export DOGANY_USAGE_DEFER_MARKER="$USAGE_DEFER_MARKER"

EXIT_CODE=0
"$@" || EXIT_CODE=$?

# ---- DGN-1012: cron liveness beat ----
# One ledger row per wrapped run makes "the job did not run" (no fresh beat)
# distinguishable from "the job ran clean" (beat rc=0) -- the two were
# indistinguishable before (DGN-1012 surface 3). Record-only by default;
# a plist that declares its cadence via DOGANY_TSL_BEAT_TTL (seconds) arms a
# missed-next-beat expiry in the sweep. rc!=0 does NOT alert here -- the
# failure-alert below already owns that (no duplicate nag). Fail-open.
if [[ -f "$SCRIPT_DIR/terminal-state-ledger.py" ]]; then
  # (stderr NOT swallowed: a failing beat leaves a trace in the job's stderr
  # log instead of dying silently -- self-grill finding, L5. The existence
  # guard keeps standalone test copies of this wrapper zero-delta.)
  python3 "$SCRIPT_DIR/terminal-state-ledger.py" beat --surface cron \
    --id "$LABEL" --rc "$EXIT_CODE" \
    ${DOGANY_TSL_BEAT_TTL:+--ttl "$DOGANY_TSL_BEAT_TTL"} >/dev/null || true
fi

# ---- DGN-1012 M2: terminal-state SWEEP DRIVE (leg 1 of 2) ----
# WHY HERE. The sweep is the only recovery path for a SIGKILLed delegation
# (measured: TERM/INT/HUP all run the worker EXIT trap, KILL does not) and
# canonical had nothing calling it. The reference implementation drives it
# from ONE dedicated launchd job (a housekeeper); when that job is unloaded
# the closure machine dies silently and nothing says so. Every periodic job in this estate already
# goes through this wrapper (measured: plist 11/11, systemd .service 2/2), so
# driving from here means the sweep survives any single job being unloaded --
# it dies only if the whole fleet dies, and that state is not silent for other
# reasons (3 of the 11 jobs are the owner's scheduled briefs). --throttle
# collapses ~288 daily invocations into one sweep per window.
#
# NOISE ISOLATION -- the objection that held this decision back. The sweep's
# rc is NEVER folded into the wrapped job's rc: $EXIT_CODE was captured above
# and is the only value this script exits with. A failing sweep raises its OWN
# owner alert under its OWN dedup key, so it can neither paint an unrelated
# job red nor go quiet. Fail-open: sweep trouble never blocks a routine.
if [[ -f "$SCRIPT_DIR/terminal-state-ledger.py" ]]; then
  TSL_SWEEP_RC=0
  TSL_SWEEP_OUT="$(python3 "$SCRIPT_DIR/terminal-state-ledger.py" sweep \
    --throttle "${DOGANY_TSL_SWEEP_INTERVAL:-3600}" 2>&1)" || TSL_SWEEP_RC=$?
  [[ -n "$TSL_SWEEP_OUT" ]] && echo "$TSL_SWEEP_OUT" >&2
  if [[ "$TSL_SWEEP_RC" -ne 0 ]]; then
    # Own identity, own dedup key -- deliberately NOT "$LABEL"'s, so this can
    # never be read as "the backup job failed".
    TSL_ALERT_MARK="$DEDUP_DIR/terminal-state-sweep.$(date +%Y%m%d)"
    if [[ -f "$TSL_ALERT_MARK" ]]; then
      echo "[cron-guard] terminal-state sweep failed rc=$TSL_SWEEP_RC (owner alert already sent today)" >&2
    else
      mkdir -p "$DEDUP_DIR" 2>/dev/null || true
      touch "$TSL_ALERT_MARK"       # mark BEFORE pushing (dup guard on crash)
      TSL_PUSH_SH="$SCRIPT_DIR/push.sh"
      # Agent signature emoji: SAME source the restart-push prefix uses
      # (.instance.conf DOGANY_AGENT_PREFIX). Never hardcode an emoji.
      TSL_EMOJI="$(grep -E '^DOGANY_AGENT_PREFIX=' "$AGENT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
      TSL_EMOJI="${TSL_EMOJI:-[agent]}"
      # 잠정 문구 -- 미확정(형님 확인 대기, UX 게이트). 발화 조건이 "종결
      # 백스톱 자신의 통보 실패"라는 예외 상황이므로 노출 위험은 그 상황에
      # 한정된다 (terminal-state-ledger.py notify_owner 문구와 같은 지위).
      TSL_ALERT_TEXT="${TSL_EMOJI} [종결 백스톱 이상] 끝나지 않은 비동기 작업을 줍는 점검이 통보에 실패했어요 (rc=${TSL_SWEEP_RC}). 놓친 작업이 있을 수 있어요 -- 다음 점검에서 자동으로 다시 시도해요."
      if [[ -x "$TSL_PUSH_SH" ]]; then
        if [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]]; then
          "$TSL_PUSH_SH" --text "$TSL_ALERT_TEXT" --env "$ENV_PATH" >&2 || true
        else
          "$TSL_PUSH_SH" --text "$TSL_ALERT_TEXT" >&2 || true
        fi
      else
        echo "[cron-guard] terminal-state sweep failed rc=$TSL_SWEEP_RC and push.sh is unavailable: $TSL_PUSH_SH" >&2
      fi
    fi
  fi
fi

# ---- success path: silent ----
if [[ "$EXIT_CODE" -eq 0 ]]; then
  exit 0
fi

# ---- DGN-835: usage-defer recovery branch (exit 75 + FRESH marker only) ----
# 2-factor: rc==75 alone is NOT enough (unrelated EX_TEMPFAIL must stay a
# visible failure); a stale marker (>10 min) is NOT enough either (an old
# defer must not hijack a later genuine failure).
if [[ "$EXIT_CODE" -eq 75 && -f "$USAGE_DEFER_MARKER" ]]; then
  MARKER_MTIME="$(file_mtime "$USAGE_DEFER_MARKER")"
  NOW_TS="$(date +%s)"
  if [[ -n "$MARKER_MTIME" ]] && [[ $(( NOW_TS - MARKER_MTIME )) -le 600 ]]; then
    # read window + reset epoch + last-success epoch from the marker
    # (best-effort; parse failure falls through to the normal alert path).
    DEFER_INFO="$(/usr/bin/python3 -c '
import json, sys
from datetime import datetime

def to_epoch(iso):
    try:
        return str(int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()))
    except Exception:
        return ""

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    window = d.get("window", "") or ""
    reset = d.get("reset_at", "") or ""
    last_success = d.get("last_success_at", "") or ""
    print(window + "|" + to_epoch(reset) + "|" + reset + "|" + to_epoch(last_success))
except Exception:
    print("")
' "$USAGE_DEFER_MARKER" 2>/dev/null)"
    DEFER_WINDOW="${DEFER_INFO%%|*}"
    _rest="${DEFER_INFO#*|}"
    DEFER_RESET_EPOCH="${_rest%%|*}"
    _rest="${_rest#*|}"
    DEFER_RESET_ISO="${_rest%%|*}"
    DEFER_LAST_SUCCESS_EPOCH="${_rest#*|}"

    # ---- shared owner-copy building blocks (7d notify + stale alert) ----
    if [[ -n "$FRIENDLY_NAME" ]]; then
      DEFER_HEADLINE="$FRIENDLY_NAME"
    else
      DEFER_HEADLINE="${LABEL##*.}"
    fi
    # Agent signature emoji: SAME source the restart-push prefix uses
    # (.instance.conf DOGANY_AGENT_PREFIX, written at mint; fallback
    # "[agent]" mirrors bridge/self_restart.sh). Never hardcode an emoji
    # here (owner rule: emoji = per-agent signature variable).
    AGENT_EMOJI="$(grep -E '^DOGANY_AGENT_PREFIX=' "$AGENT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    AGENT_EMOJI="${AGENT_EMOJI:-[agent]}"
    # Job display phrase (owner-approved copy 2026-08-12): the consolidate
    # label is CONSOLIDATE-ONLY wording. DEFER_WHAT = subject form (…가/이),
    # DEFER_WHAT_OBJ = object form (…를/을), so both approved sentences read
    # naturally. Any other usage-deferring job falls back to its friendly
    # name; registering a new job here means supplying its own owner-approved
    # phrases (UX gate).
    # DGN-851: the label text is locale data -- resolved from config/i18n
    # (cron.consolidate_defer_subject / _object); the in-code fallback is the
    # locked ko copy, so an instance without the keys is byte-identical.
    case "$LABEL" in
      *.consolidate-0430)
        DEFER_WHAT="$(i18n_or cron.consolidate_defer_subject "밤새 기억 정리가")"
        DEFER_WHAT_OBJ="$(i18n_or cron.consolidate_defer_object "밤새 기억 정리를")"
        ;;
      *)
        DEFER_WHAT="자동 작업 '${DEFER_HEADLINE}'이(가)"
        DEFER_WHAT_OBJ="자동 작업 '${DEFER_HEADLINE}'을(를)"
        ;;
    esac
    # human-readable reset time for the owner copy ({reset} slot)
    RESET_HUMAN=""
    if [[ -n "$DEFER_RESET_EPOCH" ]]; then
      RESET_HUMAN="$(date -r "$DEFER_RESET_EPOCH" '+%m/%d %H:%M' 2>/dev/null \
                     || date -d "@$DEFER_RESET_EPOCH" '+%m/%d %H:%M' 2>/dev/null || true)"
    fi
    [[ -z "$RESET_HUMAN" ]] && RESET_HUMAN="${DEFER_RESET_ISO:-unknown}"

    # defer_dedup_hit <kind>: per (label, kind, day) dedup marker. Returns 0
    # when already fired today (caller suppresses), else records and returns 1.
    defer_dedup_hit() {
      local f="$DEDUP_DIR/${LABEL}.$1.$(date +%Y%m%d)"
      [[ -f "$f" ]] && return 0
      mkdir -p "$DEDUP_DIR"
      touch "$f"
      return 1
    }

    # defer_push <push.sh args...>: push with the instance env when present.
    defer_push() {
      local PUSH_SH="$SCRIPT_DIR/push.sh"
      if [[ ! -x "$PUSH_SH" ]]; then
        echo "[cron-guard] push.sh not found for usage-defer notify: $PUSH_SH" >&2
        return 1
      fi
      if [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]]; then
        "$PUSH_SH" "$@" --env "$ENV_PATH" >&2 || true
      else
        "$PUSH_SH" "$@" >&2 || true
      fi
    }

    # ---- DGN-835 MAJOR-C: stale-canon alert READER ----
    # The deferring job embeds its last-SUCCESS timestamp in the marker
    # (last_success_at, read from its own success artifact -- consolidate:
    # memory-engine/.consolidate-last-success). When this run is ALSO a
    # defer and the last success is older than D_STALE (1 day) + 6h grace,
    # push ONE stale alert (dedup per label/day). The grace keeps the FIRST
    # missed daily run (~24h) silent and fires on the second (~48h) --
    # "2 consecutive defers" per the locked spec. This reader and the 7d
    # notify dedup below ship as ONE SET: dedup alone would open a silence
    # window in which the owner never learns the canon is stale.
    STALE_AFTER_SEC=$(( 86400 + 6 * 3600 ))
    if [[ -n "$DEFER_LAST_SUCCESS_EPOCH" ]] \
       && [[ $(( NOW_TS - DEFER_LAST_SUCCESS_EPOCH )) -gt "$STALE_AFTER_SEC" ]]; then
      if defer_dedup_hit stale; then
        echo "[cron-guard] usage-defer stale alert suppressed (dedup): $LABEL" >&2
      else
        STALE_DAYS=$(( (NOW_TS - DEFER_LAST_SUCCESS_EPOCH) / 86400 ))
        # Owner-approved copy (형님 2026-08-12). DEFER_WHAT = the i18n
        # consolidate label for the consolidate job (the only stale-eligible
        # job today); other jobs fall back to their friendly-name phrase
        # (see DEFER_WHAT above).
        STALE_TEXT="${AGENT_EMOJI} ${DEFER_WHAT} ${STALE_DAYS}일째 실행되지 못했어요. 사용량이 풀리면 재개돼요."
        defer_push --text "$STALE_TEXT"
        echo "[cron-guard] usage-defer stale alert pushed (last success ${STALE_DAYS}d ago, $LABEL)" >&2
      fi
    fi

    if [[ "$DEFER_WINDOW" == "five_hour" ]]; then
      # 5h defer: fully automatic -- schedule a reset-aligned one-shot that
      # replays THIS cron-guard invocation. No owner interaction.
      RETRY_SH="$SCRIPT_DIR/usage-defer-retry.sh"
      if [[ -n "$DEFER_RESET_EPOCH" && -x "$RETRY_SH" ]] \
         && "$RETRY_SH" --label "$LABEL" --reset-epoch "$DEFER_RESET_EPOCH" \
              -- /bin/bash "$SCRIPT_DIR/cron-guard.sh" "${ORIG_ARGS[@]}"; then
        echo "[cron-guard] usage-defer (five_hour): reset-aligned retry scheduled for $DEFER_RESET_ISO ($LABEL)" >&2
      else
        echo "[cron-guard] usage-defer (five_hour): retry scheduling unavailable -- next regular run handles it (fail-open, $LABEL)" >&2
      fi
      exit "$EXIT_CODE"
    fi

    if [[ "$DEFER_WINDOW" == "seven_day" ]]; then
      # 7d defer: owner decision. Write a replay file the bridge can execute
      # on button tap / slash command (charset-validated label; ~/.dogany is
      # user-owned), then push a notification with a manual-retry button.
      REPLAY_FILE="$USAGE_DEFER_DIR/${LABEL}.replay"
      REPLAY_TMP="${REPLAY_FILE}.tmp"
      {
        echo "#!/bin/bash"
        echo "# generated by cron-guard.sh -- DGN-835 seven_day usage-defer manual replay"
        echo "# label: $LABEL"
        echo "# one-shot: self-delete before exec (rm-on-launch replaces a TTL;"
        echo "# bash already holds the script fd, so the unlink is race-free)."
        echo 'rm -f -- "$0"'
        printf 'exec /bin/bash %q' "$SCRIPT_DIR/cron-guard.sh"
        printf ' %q' "${ORIG_ARGS[@]}"
        printf '\n'
      } > "$REPLAY_TMP" && chmod 700 "$REPLAY_TMP" && mv "$REPLAY_TMP" "$REPLAY_FILE"

      # Button rides only while callback_data fits Telegram's 64-BYTE cap
      # (DGN-835 MAJOR-발견3; push.sh guards this too, defense in depth). The
      # body ALWAYS carries the /usageretry slash line (DGN-841 A) -- the
      # slash path never hits the bridge's 20-min STALE gate, so an expired
      # or dropped button still leaves a working route.
      CB_DATA="usageretry:${LABEL}"
      BUTTON_OPTS=()
      if [[ "${#CB_DATA}" -le 64 ]]; then  # label charset is ASCII -> chars == bytes
        BUTTON_OPTS=(--button "지금 실행::${CB_DATA}")
      fi

      # Owner-approved copy (형님 2026-08-12 13:43). The consolidate label
      # wording is consolidate-only -- see DEFER_WHAT above. Button label:
      # "지금 실행".
      DEFER_TEXT="${AGENT_EMOJI} ${DEFER_WHAT} 주간 사용량 한도로 대기 중입니다. 사용량 리셋되면(${RESET_HUMAN}) 자동 재개돼요. 지금 바로 실행하려면 아래 버튼을 누르세요.
또는 /usageretry ${LABEL}"

      # Dedup per label/day: a daily job saturated for days would otherwise
      # re-notify every run. The stale reader above is the paired escape
      # hatch (they ship as one set -- see MAJOR-C comment).
      if defer_dedup_hit defer7d; then
        echo "[cron-guard] usage-defer (seven_day) notify suppressed (dedup): $LABEL" >&2
      else
        defer_push --text "$DEFER_TEXT" ${BUTTON_OPTS[@]+"${BUTTON_OPTS[@]}"}
        echo "[cron-guard] usage-defer (seven_day): owner notified with manual-retry button ($LABEL)" >&2
      fi

      # DGN-841 B: fresh-button re-notify at the reset boundary. The original
      # button dies at the bridge's 20-min STALE gate long before a 7d reset,
      # so a reset-aligned one-shot re-sends a NEW message (fresh timestamp =
      # tappable button). Reuses the usage-defer-retry scheduler; per-label
      # dedup there replaces any previous pending re-notify.
      RETRY_SH="$SCRIPT_DIR/usage-defer-retry.sh"
      if [[ -n "$DEFER_RESET_EPOCH" && -x "$RETRY_SH" ]]; then
        # Owner-approved copy (형님 2026-08-12) + [지금 실행] button. The
        # slash line rides along as an expiry backstop (DGN-841 A) even
        # though the approved sentence itself does not name it.
        RENOTIFY_TEXT="${AGENT_EMOJI} 주간 사용량이 리셋됐어요. ${DEFER_WHAT_OBJ} 지금 실행할 수 있습니다.
또는 /usageretry ${LABEL}"
        RENOTIFY_CMD=(/bin/bash "$SCRIPT_DIR/push.sh" --text "$RENOTIFY_TEXT")
        [[ ${#BUTTON_OPTS[@]} -gt 0 ]] && RENOTIFY_CMD+=("${BUTTON_OPTS[@]}")
        [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]] && RENOTIFY_CMD+=(--env "$ENV_PATH")
        if "$RETRY_SH" --label "${LABEL}.notify7d" --reset-epoch "$DEFER_RESET_EPOCH" \
             -- "${RENOTIFY_CMD[@]}"; then
          echo "[cron-guard] usage-defer (seven_day): fresh-button re-notify scheduled at reset ($LABEL)" >&2
        else
          echo "[cron-guard] usage-defer (seven_day): re-notify scheduling unavailable (fail-open, $LABEL)" >&2
        fi
      fi
      exit "$EXIT_CODE"
    fi
    # unknown window -> fall through to the normal alert path.
    echo "[cron-guard] usage-defer marker has unknown window '$DEFER_WINDOW' -- treating as normal failure" >&2
  fi
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

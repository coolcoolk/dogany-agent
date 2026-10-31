#!/bin/bash
# dawn-memory-queue.sh -- Unified Dawn Memory Queue (DMQ)
# DGN-820: replaces 16 scattered launchd plists with one serialized queue runner.
#
# Schedule: ONE launchd job 04:00 (alert-root instance's label namespace), nice'd.
# Registry: ~/.dogany/memory-queue/instances.d/<slug>.conf (KEY=VALUE)
# Host conf: ~/.dogany/memory-queue/dmq.conf (KEY=VALUE, optional):
#   ALERT_ROOT=<instance root>  who receives queue alerts (push.sh + .env
#                               there) and the CLI pre-step's ledger/inbox drop.
#   Unset -> alerts are logged only, never guessed onto another instance.
# Log dir:  ~/.dogany/logs/dawn-memory-queue/
#
# Source of truth (DGN-1673): this file is canonical at
#   dogany-agent agents/.template/routines/dawn-memory-queue.sh
# and is installed by copy to ~/.dogany/bin/dawn-memory-queue.sh. Until
# DGN-1673 it existed only as that hand-installed host file (DGN-820).
#
# Pre-step (DGN-1673, before the first instance): nightly external CLI update
#   $ALERT_ROOT/routines/cli-updater.py  timeout 1200s
#   order = CLI update -> smoke -> model probe, then the per-instance loop.
#   FAIL-OPEN: any pre-step rc (incl. timeout / missing script) is logged and
#   the queue continues; it is reported as prestep=<rc> on the summary line
#   and does NOT count into fail= (the updater alerts through its own seam).
#   --live -> updater --mode install (still needs ~/.dogany/cli-updates/armed);
#   otherwise --mode observe. Disable: DMQ_CLI_UPDATE=0 or
#   ~/.dogany/cli-updates/disabled.
# Post-step (DGN-1673, after the loop): if the pre-step installed a new claude
#   and the run is --live, each processed instance's bridge is restarted via
#   its own bridge/self_restart.sh --trigger auto (idle guard applies; a
#   refusal defers to the next natural restart). No new restart mechanism.
#   OFF unless ~/.dogany/cli-updates/restart-armed exists: hot.framework
#   Machine safety says bots are never auto-restarted, and the owner's DGN-1673
#   instruction to restart here is a divergence the host must opt into
#   explicitly, not a default this file ships.
# Post-step 2 (DGN-1673 r2, owner dec-208 (c)): after at least one restart,
#   cli-updater.py --mode agents waits (bounded, ${DMQ_AGENT_WAIT_SECS:-600}s)
#   for each restarted bridge to report its first turn (the restart drops a
#   self-verify turn into its session-inbox), then compares every agent's
#   model id with the last one seen. It runs HERE because a restarted bridge
#   reports a model only after a turn; the pre-step's own check (no wait)
#   catches daytime changes (manual restarts, new sessions) the next night.
# Owner push (dec-208): the updater pushes owner-grade notices itself (new
#   model / rollback / agent model change) only when this run is --live
#   (it passes --owner-push) AND ~/.dogany/cli-updates/owner-push-armed exists.
#
# Per-instance contract:
#   Step A: $ROOT/routines/consolidate-0430.sh  timeout 1500s (DGN-1582 r3:
#           a behind instance's raised budget + 300s, at most 2400s)
#   Step B: $ROOT/routines/classify-inbox-check.sh  timeout 900s  (runs regardless of A rc)
#   Each step is wrapped via the INSTANCE's own cron-guard.sh --queue heavy
#   (machine-global 2-slot heavy queue, same contention as today).
#
# Failure isolation: every rc captured; loop never aborts; missing instance -> SKIP+log.
# Timeout: process-group kill (TERM -> 30s -> KILL). gtimeout used if present,
#   else portable bash watchdog.
# Self-lock: runner.lock mkdir spinlock with stale-reclaim (mirrors cron-guard.sh).
# Queue-level alert -> the ALERT_ROOT instance's channel only for pathologies:
#   - registry empty
#   - step timeout kill
#   - wall time > 60min
#   Alert send results are logged (DGN-1468); a failed send never kills the queue.
# Summary line (DGN-1468): "dmq complete -- verdict=OK|DEGRADED ..."; fail=
#   includes timeout kills. Exit code: 0 = all clean, 1 = any step failed or
#   timed out (feeds launchd LastExitStatus -> health-observer fail streak).
#   DGN-1582: a consolidate "behind" exit (rc 77, work committed, backlog
#   stuck) is counted as behind=, not fail= -- cron-guard owns its notice.
#   DGN-1873: a step that exits 78 with a fresh retry marker (the job has
#   scheduled its own automatic retry) is counted as retry=, not fail= --
#   cron-guard sends it operator-only; the retry's own failure is the alarm.
# Unattended (DGN-1873, owner directive 2026-10-06): every step is wrapped
#   with cron-guard --unattended -- the owner is asleep, so no notice from
#   this queue carries an ask button (it would expire) or asks a question.
#
# CLI flags:
#   --dry-run           Print resolved plan; run nothing.
#   --instance <slug>   Run only the named instance (slug = conf filename without .conf).
#   --force-classify    Pass --force to classify-inbox-check.sh (ignore 7-day marker).
#
# MINOR defaults (owner-approved, DGN-820):
#   late-start  = run anyway, log warning if start >= 05:00 local
#   soft deadline 04:55 applies to CLASSIFY steps only; consolidate always runs
#   label namespace = the alert-root instance
#   heavy slots = 2 (cron-guard default)
#
# Compatibility: bash 3.2+ (macOS /bin/bash); no mapfile / no bash 4 features.

set -uo pipefail

# ---- constants ----
LOCK_BASE="$HOME/.dogany/cron-queue/dmq-runner"
LOCK_DIR="$LOCK_BASE/runner.lock"
REGISTRY_DIR="$HOME/.dogany/memory-queue/instances.d"
LOG_BASE="$HOME/.dogany/logs/dawn-memory-queue"
STATE_FILE="$HOME/.dogany/memory-queue/last-success"
HOST_CONF="$HOME/.dogany/memory-queue/dmq.conf"
# DGN-1673: the alert root used to be a hardcoded instance path here; it is
# host data, so it moved to dmq.conf (env DMQ_ALERT_ROOT overrides, tests).
ALERT_ROOT="${DMQ_ALERT_ROOT:-}"
if [[ -z "$ALERT_ROOT" && -f "$HOST_CONF" ]]; then
  ALERT_ROOT="$(sed -n 's/^ALERT_ROOT=//p' "$HOST_CONF" | tail -1)"
fi
ALERT_ENV="$ALERT_ROOT/.telegram_bot/.env"
ALERT_PUSH="$ALERT_ROOT/routines/push.sh"
CLI_STATE="${DMQ_CLI_STATE:-$HOME/.dogany/cli-updates}"
CLI_UPDATER="${DMQ_CLI_UPDATER:-$ALERT_ROOT/routines/cli-updater.py}"
CLI_UPDATE_TIMEOUT="${DMQ_CLI_UPDATE_TIMEOUT:-1200}"
RESTART_TIMEOUT="${DMQ_RESTART_TIMEOUT:-300}"

CONSOLIDATE_TIMEOUT=1500
CLASSIFY_TIMEOUT=900
# DGN-1582 (1): consolidate's "behind" exit (memory.py
# CONSOLIDATE_RC_BACKLOG_BEHIND) -- a completed, budget-bounded run whose
# backlog is stuck. Counted as behind=, never as fail=.
CONSOLIDATE_RC_BEHIND=77
# DGN-1873: cron-guard's retry-scheduled exit (2-factor with the marker that
# cron-guard hands the step as DOGANY_RETRY_MARKER).
STEP_RC_RETRY_SCHEDULED=78
RETRY_MARKER_DIR="$HOME/.dogany/retry-scheduled"
# DGN-1582 r3 (dec-236): a behind instance's raised budget. memory.py records
# it in <root>/memory-engine/.consolidate-budget-boost.json from the 2nd
# behind night; this runner exports it as DOGANY_CONSOLIDATE_BUDGET_SECS and
# keeps the kill timeout CONSOLIDATE_KILL_MARGIN above it. Values outside
# [CONSOLIDATE_BUDGET_DEFAULT, CONSOLIDATE_BUDGET_CAP] are clamped (memory.py
# CONSOLIDATE_BUDGET_CAP_SECS is the same cap).
CONSOLIDATE_BUDGET_DEFAULT=1200
CONSOLIDATE_BUDGET_CAP=2100
CONSOLIDATE_KILL_MARGIN=300
# An ops override already in this runner's environment survives every
# instance that has no boost record.
DMQ_BUDGET_ENV_SET="${DOGANY_CONSOLIDATE_BUDGET_SECS+1}"
DMQ_BUDGET_ENV="${DOGANY_CONSOLIDATE_BUDGET_SECS-}"
WALL_ALERT_SECS=$(( 60 * 60 ))     # 60 min
SOFT_DEADLINE_HOUR=4
SOFT_DEADLINE_MIN=55

# ---- CLI parse ----
DRY_RUN=0
ONLY_SLUG=""
FORCE_CLASSIFY=0
LIVE=0            # pathology pushes fire ONLY in a real scheduled run (--live).
                  # Manual / test invocations log alerts instead of pushing.

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)          DRY_RUN=1; shift ;;
    --instance)         ONLY_SLUG="$2"; shift 2 ;;
    --force-classify)   FORCE_CLASSIFY=1; shift ;;
    --live)             LIVE=1; shift ;;
    *) echo "[dmq] unknown flag: $1" >&2; exit 1 ;;
  esac
done

# ---- logging ----
QUEUE_START_EPOCH="$(date +%s)"
TODAY="$(date +%Y%m%d)"
mkdir -p "$LOG_BASE"
LOG_FILE="$LOG_BASE/queue-${TODAY}.log"

log() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[$ts] $*" | tee -a "$LOG_FILE"
}

# ---- alert-root push helper (queue-level pathology alerts only) ----
# Pushes to the owner channel ONLY in a real scheduled run (--live). Manual and
# test invocations (no --live) log the alert instead -- no owner-facing push leak.
# DGN-1468: the send result is CAPTURED and LOGGED (delivery used to be piped to
# /dev/null, so nobody could tell whether any alert ever reached the owner).
# Fail-open is preserved: a failed send logs WARN and returns 0 -- an alert
# failure must never kill the queue itself.
push_alert() {
  local msg="$1" push_out push_rc
  if [[ $LIVE -ne 1 ]]; then
    log "ALERT (suppressed, not --live): $msg"
    return 0
  fi
  if [[ -n "$ALERT_ROOT" && -x "$ALERT_PUSH" && -f "$ALERT_ENV" ]]; then
    push_out="$("$ALERT_PUSH" --text "[dmq] $msg" --env "$ALERT_ENV" 2>&1)"
    push_rc=$?
    if [[ $push_rc -eq 0 ]]; then
      log "INFO: alert push OK: $msg"
    else
      log "WARN: alert push FAILED (rc=$push_rc): $msg -- $(printf '%s' "$push_out" | tail -3 | tr '\n' ' ')"
    fi
  else
    log "WARN: push.sh not available (ALERT_ROOT='$ALERT_ROOT') -- alert suppressed: $msg"
  fi
  return 0
}

# ---- self-lock (mkdir spinlock, mirrors cron-guard.sh pattern) ----
# macOS has no flock(1); use atomic-mkdir with stale-pid reclaim.
SELF_LOCK_DIR=""

# shellcheck disable=SC2329  # called via trap EXIT
release_self_lock() {
  if [[ -n "$SELF_LOCK_DIR" ]]; then
    rm -rf "$SELF_LOCK_DIR" 2>/dev/null || true
    SELF_LOCK_DIR=""
  fi
}

file_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

acquire_self_lock() {
  mkdir -p "$LOCK_BASE"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    SELF_LOCK_DIR="$LOCK_DIR"
    echo "$$" > "$LOCK_DIR/pid"
    return 0
  fi

  # Check if owner is alive
  local owner_pid
  owner_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$owner_pid" ]]; then
    if kill -0 "$owner_pid" 2>/dev/null; then
      log "ERROR: another dmq instance is running (pid $owner_pid) -- exiting"
      return 1
    fi
    # Owner dead: stale reclaim via atomic rename
    local trash
    trash="$LOCK_BASE/.reclaim.$$.$RANDOM"
    if mv "$LOCK_DIR" "$trash" 2>/dev/null; then
      rm -rf "$trash" 2>/dev/null || true
      log "INFO: reclaimed stale self-lock (dead pid $owner_pid)"
      if mkdir "$LOCK_DIR" 2>/dev/null; then
        SELF_LOCK_DIR="$LOCK_DIR"
        echo "$$" > "$LOCK_DIR/pid"
        return 0
      fi
    fi
    log "ERROR: could not reclaim self-lock -- exiting"
    return 1
  fi

  # No pidfile: stale if dir age > 60s
  local dir_mtime now
  dir_mtime="$(file_mtime "$LOCK_DIR")"
  now="$(date +%s)"
  if [[ $(( now - dir_mtime )) -gt 60 ]]; then
    local trash
    trash="$LOCK_BASE/.reclaim.$$.$RANDOM"
    if mv "$LOCK_DIR" "$trash" 2>/dev/null; then
      rm -rf "$trash" 2>/dev/null || true
      log "INFO: reclaimed abandoned self-lock (no pidfile, age>60s)"
      if mkdir "$LOCK_DIR" 2>/dev/null; then
        SELF_LOCK_DIR="$LOCK_DIR"
        echo "$$" > "$LOCK_DIR/pid"
        return 0
      fi
    fi
  fi
  log "ERROR: self-lock busy -- exiting"
  return 1
}

trap release_self_lock EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---- process-group timeout watchdog ----
# gtimeout (coreutils) used if present; else portable bash watchdog.
# Runs cmd as a new process group; on timeout sends TERM, waits 30s, then KILL.
# Returns the cmd's exit code, or 124 on timeout kill.
run_with_timeout() {
  local timeout_secs="$1"
  shift
  # "$@" = command + args

  if command -v gtimeout >/dev/null 2>&1; then
    # gtimeout --kill-after=30 sends TERM then KILL after 30s grace
    gtimeout --kill-after=30 "$timeout_secs" "$@"
    return $?
  fi

  # Portable bash watchdog: run cmd in its own process group, kill group on timeout.
  # macOS has no setsid binary; use perl POSIX::setsid() as a drop-in.
  # perl -e 'POSIX::setsid(); exec @ARGV' creates new session (new pgid = cmd pid).
  # Watchdog kills the process group, catching any children.
  (
    set -uo pipefail
    perl -e 'use POSIX; POSIX::setsid(); exec @ARGV' -- "$@" &
    CMD_PID=$!

    # Background watchdog subshell
    (
      sleep "$timeout_secs"
      kill -TERM -- "-${CMD_PID}" 2>/dev/null || true
      sleep 30
      kill -KILL -- "-${CMD_PID}" 2>/dev/null || true
    ) &
    WATCHDOG_PID=$!

    wait "$CMD_PID"
    CMD_RC=$?
    kill "$WATCHDOG_PID" 2>/dev/null || true
    wait "$WATCHDOG_PID" 2>/dev/null || true
    exit "$CMD_RC"
  )
  local rc=$?
  # SIGTERM exit = 128+15=143; SIGKILL = 128+9=137. Normalize to 124 (timeout convention).
  if [[ $rc -eq 143 || $rc -eq 137 ]]; then
    return 124
  fi
  return $rc
}

# ---- soft deadline check (classify steps only) ----
# Returns 0 = before deadline (safe to run), 1 = past deadline (skip)
past_soft_deadline() {
  local h m
  h=$(( 10#$(date +%H) ))
  m=$(( 10#$(date +%M) ))
  if [[ $h -gt $SOFT_DEADLINE_HOUR ]]; then
    return 1
  fi
  if [[ $h -eq $SOFT_DEADLINE_HOUR && $m -ge $SOFT_DEADLINE_MIN ]]; then
    return 1
  fi
  return 0
}

# ---- DGN-1673 pre-step: nightly external CLI update (fail-open) ----
PRESTEP_RC="none"

cli_prestep_enabled() {
  [[ "${DMQ_CLI_UPDATE:-1}" != "0" ]] || return 1
  [[ ! -e "$CLI_STATE/disabled" ]] || return 1
  return 0
}

cli_prestep_mode() {
  if [[ $LIVE -eq 1 ]]; then echo "install"; else echo "observe"; fi
}

# Appends the flags every updater call shares to the caller's `args` array
# (bash 3.2: no namerefs, so the array name is fixed).
cli_common_args() {
  local reg="$1" _p _s iroot
  if [[ -n "$ALERT_ROOT" ]]; then
    args+=(--alert-root "$ALERT_ROOT")
  fi
  if [[ $LIVE -eq 1 ]]; then
    args+=(--owner-push)
  fi
  while IFS=: read -r _p _s iroot; do
    args+=(--instance-root "$iroot")
  done < "$reg"
}

# arg: sorted registry file ("PRIO:slug:ROOT" lines). Never returns non-zero:
# whatever the updater does, the per-instance loop runs next.
run_cli_prestep() {
  local reg="$1" mode rc=0 _p _s iroot
  local -a args
  if ! cli_prestep_enabled; then
    log "INFO: pre-step cli-update DISABLED (DMQ_CLI_UPDATE=0 or $CLI_STATE/disabled)"
    PRESTEP_RC="off"
    return 0
  fi
  if [[ ! -f "$CLI_UPDATER" ]]; then
    log "WARN: pre-step cli-update: updater missing ($CLI_UPDATER) -- queue continues"
    PRESTEP_RC="missing"
    return 0
  fi
  mode="$(cli_prestep_mode)"
  args=(--mode "$mode" --state-dir "$CLI_STATE")
  cli_common_args "$reg"
  log "INFO: pre-step cli-update: $CLI_UPDATER (mode=$mode timeout ${CLI_UPDATE_TIMEOUT}s)"
  run_with_timeout "$CLI_UPDATE_TIMEOUT" python3 "$CLI_UPDATER" "${args[@]}" \
    >> "$LOG_BASE/cli-update-${TODAY}.log" 2>&1 || rc=$?
  PRESTEP_RC="$rc"
  if [[ $rc -eq 0 ]]; then
    log "INFO: pre-step cli-update ok"
  elif [[ $rc -eq 124 ]]; then
    log "WARN: pre-step cli-update TIMEOUT-KILL after ${CLI_UPDATE_TIMEOUT}s -- queue continues"
    push_alert "pre-step cli-update TIMEOUT-KILL after ${CLI_UPDATE_TIMEOUT}s (check $CLI_STATE/last-run.env and the claude activator)"
  else
    log "WARN: pre-step cli-update rc=$rc -- queue continues (updater alerts via its own seam)"
  fi
  return 0
}

# ---- DGN-1673 post-step: bridge restart after a claude CLI change ----
# Runs after the per-instance loop. Only acts on THIS run's pre-step result
# (RUN_TS >= queue start), only on --live, and only through each instance's
# own self_restart.sh --trigger auto (its idle guard decides; a refusal
# defers to the next natural restart).
RESTARTS=0
RESTART_START=0

run_restart_poststep() {
  local reg="$1" env_file="$CLI_STATE/last-run.env" run_ts changed from to
  local _p slug iroot sr rc
  [[ -f "$env_file" ]] || return 0
  run_ts="$(sed -n 's/^RUN_TS=//p' "$env_file" | tail -1)"
  changed="$(sed -n 's/^CLAUDE_CHANGED=//p' "$env_file" | tail -1)"
  from="$(sed -n 's/^CLAUDE_FROM=//p' "$env_file" | tail -1)"
  to="$(sed -n 's/^CLAUDE_TO=//p' "$env_file" | tail -1)"
  [[ "$run_ts" =~ ^[0-9]+$ ]] || return 0
  [[ $run_ts -ge $QUEUE_START_EPOCH && "$changed" == "1" ]] || return 0
  if [[ $LIVE -ne 1 ]]; then
    log "INFO: post-step bridge restart skipped (claude $from -> $to; not --live)"
    return 0
  fi
  if [[ ! -e "$CLI_STATE/restart-armed" ]]; then
    log "INFO: post-step bridge restart skipped (claude $from -> $to; $CLI_STATE/restart-armed absent -- bridges keep the old image until their next restart)"
    return 0
  fi
  RESTART_START="$(date +%s)"
  while IFS=: read -r _p slug iroot; do
    sr="$iroot/bridge/self_restart.sh"
    if [[ ! -x "$sr" ]]; then
      log "SKIP [$slug] post-step restart: $sr not executable"
      continue
    fi
    rc=0
    log "INFO: [$slug] post-step: self_restart.sh --trigger auto (claude $from -> $to)"
    run_with_timeout "$RESTART_TIMEOUT" /bin/bash "$sr" --trigger auto \
      --reason "DGN-1673 nightly claude CLI $from -> $to" \
      >> "$LOG_BASE/instance-${slug}-${TODAY}.log" 2>&1 || rc=$?
    log "INFO: [$slug] post-step restart rc=$rc"
    RESTARTS=$(( RESTARTS + 1 ))
  done < "$reg"
  return 0
}

# ---- DGN-1673 r2 post-step 2: running-agent model check (dec-208 (c)) ----
# Only after a restart happened this run. Fail-open like the pre-step.
AGENT_WAIT_SECS="${DMQ_AGENT_WAIT_SECS:-600}"

run_agent_check_poststep() {
  local reg="$1" rc=0
  local -a args
  [[ $RESTARTS -gt 0 ]] || return 0
  cli_prestep_enabled || return 0
  [[ -f "$CLI_UPDATER" ]] || return 0
  args=(--mode agents --state-dir "$CLI_STATE" --wait-secs "$AGENT_WAIT_SECS"
        --since "$RESTART_START")
  cli_common_args "$reg"
  log "INFO: post-step agent model check (wait <= ${AGENT_WAIT_SECS}s for restarted bridges)"
  run_with_timeout $(( AGENT_WAIT_SECS + 120 )) python3 "$CLI_UPDATER" "${args[@]}" \
    >> "$LOG_BASE/cli-update-${TODAY}.log" 2>&1 || rc=$?
  log "INFO: post-step agent model check rc=$rc"
  return 0
}

# ---- retry_scheduled <rc> <label> <step-start-epoch> (DGN-1873) ----
# True when the step exited 78 AND wrote its retry marker during this step --
# the same 2-factor cron-guard applies, so a bare 78 stays a failure here too.
retry_scheduled() {
  local rc="$1" marker="$RETRY_MARKER_DIR/$2.json" since="$3" mt
  [[ $rc -eq $STEP_RC_RETRY_SCHEDULED && -f "$marker" ]] || return 1
  mt="$(file_mtime "$marker")"
  [[ -n "$mt" && $mt -ge $since ]]
}

# ---- consolidate_budget_boost <root> (DGN-1582 r3) ----
# Prints the instance's raised consolidate budget (seconds, clamped to
# [CONSOLIDATE_BUDGET_DEFAULT, CONSOLIDATE_BUDGET_CAP]) or nothing when there
# is no usable boost record.
consolidate_budget_boost() {
  local f="$1/memory-engine/.consolidate-budget-boost.json" v
  [[ -f "$f" ]] || return 0
  v="$(sed -n 's/.*"budget_secs": *\([0-9][0-9]*\).*/\1/p' "$f" | head -1)"
  [[ -n "$v" ]] || return 0
  [[ $v -gt $CONSOLIDATE_BUDGET_CAP ]] && v=$CONSOLIDATE_BUDGET_CAP
  [[ $v -le $CONSOLIDATE_BUDGET_DEFAULT ]] && return 0
  printf '%s' "$v"
}

# ---- load_registry ----
# Writes "PRIORITY:slug:ROOT" lines (sorted by priority) to stdout.
# Returns 0 if at least one enabled instance found, 1 if empty.
load_registry() {
  local conf slug root priority enabled tmp_entries
  tmp_entries="$(mktemp /tmp/dmq-reg.XXXXXX)"

  if [[ ! -d "$REGISTRY_DIR" ]]; then
    rm -f "$tmp_entries"
    return 1
  fi

  local found=0
  for conf in "$REGISTRY_DIR"/*.conf; do
    [[ -f "$conf" ]] || continue
    slug="$(basename "$conf" .conf)"

    # Filter by --instance if given
    if [[ -n "$ONLY_SLUG" && "$slug" != "$ONLY_SLUG" ]]; then
      continue
    fi

    root=""
    priority=""
    enabled="1"

    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in
        '#'*|'') continue ;;
      esac
      local key val
      key="${line%%=*}"
      val="${line#*=}"
      case "$key" in
        ROOT)     root="$val" ;;
        PRIORITY) priority="$val" ;;
        ENABLED)  enabled="$val" ;;
      esac
    done < "$conf"

    if [[ "$enabled" != "1" ]]; then
      echo "SKIP $slug: ENABLED!=1" >&2
      continue
    fi
    if [[ -z "$root" ]]; then
      echo "SKIP $slug: no ROOT in $conf" >&2
      continue
    fi
    if [[ -z "$priority" ]]; then
      priority=999
    fi

    echo "${priority}:${slug}:${root}" >> "$tmp_entries"
    found=$(( found + 1 ))
  done

  if [[ $found -eq 0 ]]; then
    rm -f "$tmp_entries"
    return 1
  fi

  sort -t: -k1,1n "$tmp_entries"
  rm -f "$tmp_entries"
  return 0
}

# ---- dry-run path (no lock, no log file writes, no execution) ----
if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dmq] --dry-run mode: resolving plan, executing nothing"
  echo "[dmq] registry: $REGISTRY_DIR"
  echo "[dmq] log dir:  $LOG_BASE"
  echo "[dmq] state:    $STATE_FILE"
  echo ""

  SORTED_TMP="$(mktemp /tmp/dmq-dry.XXXXXX)"
  if ! load_registry > "$SORTED_TMP" 2>/dev/null; then
    echo "[dmq] ERROR: registry empty or no enabled instances"
    rm -f "$SORTED_TMP"
    exit 1
  fi

  count=0
  while IFS= read -r line; do
    count=$(( count + 1 ))
  done < "$SORTED_TMP"

  pre_state="enabled"; cli_prestep_enabled || pre_state="DISABLED"
  pre_script="OK"; [[ -f "$CLI_UPDATER" ]] || pre_script="MISSING"
  pre_armed="no"; [[ -e "$CLI_STATE/armed" ]] && pre_armed="yes"
  echo "[dmq] pre-step (runs BEFORE the first instance; fail-open):"
  echo "         cli-update: python3 $CLI_UPDATER --mode $(cli_prestep_mode) --state-dir $CLI_STATE"
  echo "                 alert-root: ${ALERT_ROOT:-<unset: alerts logged only>}"
  echo "                 timeout ${CLI_UPDATE_TIMEOUT}s | state: $pre_state | script: $pre_script | armed: $pre_armed"
  echo ""
  echo "[dmq] execution plan ($count instances, sequential):"
  printf '%-6s  %-14s  %s\n' "PRIO" "SLUG" "ROOT"
  printf '%-6s  %-14s  %s\n' "------" "--------------" "----"

  while IFS=: read -r prio slug iroot; do
    printf '%-6s  %-14s  %s\n' "$prio" "$slug" "$iroot"

    cons_ok="OK"; clas_ok="OK"; cg_ok="OK"
    [[ -x "$iroot/routines/consolidate-0430.sh" ]] || cons_ok="MISSING"
    [[ -x "$iroot/routines/classify-inbox-check.sh" ]] || clas_ok="MISSING"
    [[ -x "$iroot/routines/cron-guard.sh" ]] || cg_ok="MISSING"

    cons_label="com.telegram-skill-bot.${slug}.dmq-consolidate"
    clas_label="com.telegram-skill-bot.${slug}.dmq-classify"

    echo "         step A: $iroot/routines/consolidate-0430.sh"
    echo "                 via $iroot/routines/cron-guard.sh --queue heavy --unattended --label $cons_label"
    echo "                 timeout ${CONSOLIDATE_TIMEOUT}s | cron-guard: $cg_ok | script: $cons_ok"
    echo "         step B: $iroot/routines/classify-inbox-check.sh$([[ $FORCE_CLASSIFY -eq 1 ]] && echo ' --force' || true)"
    echo "                 via $iroot/routines/cron-guard.sh --queue heavy --unattended --label $clas_label"
    echo "                 timeout ${CLASSIFY_TIMEOUT}s | soft-deadline 04:55 applies | script: $clas_ok"
    echo ""
  done < "$SORTED_TMP"

  rm -f "$SORTED_TMP"
  echo "[dmq] post-step: bridge restart via <root>/bridge/self_restart.sh --trigger auto"
  post_armed="no"; [[ -e "$CLI_STATE/restart-armed" ]] && post_armed="yes"
  echo "                 only when the pre-step installed a new claude AND --live AND restart-armed (armed: $post_armed)"
  owner_armed="no"; [[ -e "$CLI_STATE/owner-push-armed" ]] && owner_armed="yes"
  echo "[dmq] post-step 2: agent model check via cli-updater.py --mode agents (after >= 1 restart, wait <= ${AGENT_WAIT_SECS}s)"
  echo "                 owner push only on --live AND owner-push-armed (armed: $owner_armed)"
  echo "[dmq] dry-run complete -- nothing executed"
  exit 0
fi

# ---- acquire self-lock ----
if ! acquire_self_lock; then
  exit 1
fi

log "INFO: dmq started (pid=$$)"

# ---- late-start check ----
START_HOUR="$(date +%H)"
if [[ $(( 10#$START_HOUR )) -ge 5 ]]; then
  log "WARN: late-start detected ($(date '+%H:%M')) -- running anyway (owner-approved policy)"
fi

# ---- load registry into temp file (bash 3.2 compat: no mapfile) ----
REGISTRY_TMP="$(mktemp /tmp/dmq-registry.XXXXXX)"
if ! load_registry > "$REGISTRY_TMP"; then
  log "ERROR: registry empty or no enabled instances found in $REGISTRY_DIR"
  rm -f "$REGISTRY_TMP"
  push_alert "PATHOLOGY: registry empty -- no instances to process"
  exit 1
fi

INSTANCE_COUNT=0
while IFS= read -r _; do
  INSTANCE_COUNT=$(( INSTANCE_COUNT + 1 ))
done < "$REGISTRY_TMP"

log "INFO: ${INSTANCE_COUNT} instance(s) queued"

# ---- DGN-1673 pre-step (before the first instance consolidate) ----
run_cli_prestep "$REGISTRY_TMP"

# ---- per-instance loop ----
TIMEOUT_KILLS=0
SKIP_COUNT=0
SUCCESS_COUNT=0
FAIL_COUNT=0
BEHIND_COUNT=0
RETRY_COUNT=0

while IFS=: read -r prio slug iroot; do

  log "INFO: [$slug] priority=$prio root=$iroot"

  # Validate instance root
  if [[ ! -d "$iroot" ]]; then
    log "SKIP [$slug]: root dir missing: $iroot"
    SKIP_COUNT=$(( SKIP_COUNT + 1 ))
    continue
  fi
  if [[ ! -f "$iroot/.instance.conf" ]]; then
    log "SKIP [$slug]: .instance.conf missing in $iroot"
    SKIP_COUNT=$(( SKIP_COUNT + 1 ))
    continue
  fi

  CG="$iroot/routines/cron-guard.sh"
  CONSOLIDATE="$iroot/routines/consolidate-0430.sh"
  CLASSIFY="$iroot/routines/classify-inbox-check.sh"

  if [[ ! -x "$CG" ]]; then
    log "SKIP [$slug]: cron-guard.sh not executable: $CG"
    SKIP_COUNT=$(( SKIP_COUNT + 1 ))
    continue
  fi
  if [[ ! -x "$CONSOLIDATE" ]]; then
    log "SKIP [$slug]: consolidate-0430.sh not executable: $CONSOLIDATE"
    SKIP_COUNT=$(( SKIP_COUNT + 1 ))
    continue
  fi
  if [[ ! -x "$CLASSIFY" ]]; then
    log "SKIP [$slug]: classify-inbox-check.sh not executable: $CLASSIFY"
    SKIP_COUNT=$(( SKIP_COUNT + 1 ))
    continue
  fi

  # DGN-1582 (2): every step's output lands in this file, so it is ALSO the
  # --log cron-guard tails on failure. The legacy per-routine logs under
  # .telegram_bot/logs/ are never written on a dmq run; pointing cron-guard
  # there attached a weeks-old tail as this run's evidence.
  INSTANCE_LOG_FILE="$LOG_BASE/instance-${slug}-${TODAY}.log"
  # DGN-1873: this runner is installed host-wide but each step runs through
  # the INSTANCE's own cron-guard.sh, which may predate --unattended (an
  # unknown flag would fail the step without running it). Pass it only when
  # that copy parses it.
  CG_UNATTENDED=()
  if grep -q -- '--unattended)' "$CG" 2>/dev/null; then
    CG_UNATTENDED=(--unattended)
  else
    log "INFO: [$slug] cron-guard.sh predates --unattended -- notices may still carry an ask button"
  fi
  CONS_LABEL="com.telegram-skill-bot.${slug}.dmq-consolidate"
  CLAS_LABEL="com.telegram-skill-bot.${slug}.dmq-classify"

  # ---- step A: consolidate (always runs, no soft deadline) ----
  STEP_A_TIMEOUT="$CONSOLIDATE_TIMEOUT"
  STEP_A_BUDGET="$(consolidate_budget_boost "$iroot")"
  if [[ -n "$STEP_A_BUDGET" ]]; then
    export DOGANY_CONSOLIDATE_BUDGET_SECS="$STEP_A_BUDGET"
    if [[ $(( STEP_A_BUDGET + CONSOLIDATE_KILL_MARGIN )) -gt $STEP_A_TIMEOUT ]]; then
      STEP_A_TIMEOUT=$(( STEP_A_BUDGET + CONSOLIDATE_KILL_MARGIN ))
    fi
    log "INFO: [$slug] step A: raised budget ${STEP_A_BUDGET}s (behind; memory-engine boost record)"
  elif [[ -n "$DMQ_BUDGET_ENV_SET" ]]; then
    export DOGANY_CONSOLIDATE_BUDGET_SECS="$DMQ_BUDGET_ENV"
  else
    unset DOGANY_CONSOLIDATE_BUDGET_SECS
  fi
  log "INFO: [$slug] step A: consolidate (timeout ${STEP_A_TIMEOUT}s)"
  STEP_A_RC=0
  STEP_A_START="$(date +%s)"
  run_with_timeout "$STEP_A_TIMEOUT" \
    "$CG" \
    --label "$CONS_LABEL" \
    --log "$INSTANCE_LOG_FILE" \
    --env "$iroot/.telegram_bot/.env" \
    --queue heavy \
    ${CG_UNATTENDED[@]+"${CG_UNATTENDED[@]}"} \
    -- \
    /bin/bash "$CONSOLIDATE" \
    >> "$INSTANCE_LOG_FILE" 2>&1 || STEP_A_RC=$?

  if [[ $STEP_A_RC -eq 124 ]]; then
    log "WARN: [$slug] step A TIMEOUT-KILL (rc=124) after ${STEP_A_TIMEOUT}s"
    push_alert "[$slug] step A consolidate TIMEOUT-KILL after ${STEP_A_TIMEOUT}s"
    TIMEOUT_KILLS=$(( TIMEOUT_KILLS + 1 ))
  elif [[ $STEP_A_RC -eq $CONSOLIDATE_RC_BEHIND ]]; then
    # DGN-1582 (1): the run finished inside its budget and committed its
    # work, but the backlog has outlived CONSOLIDATE_BACKLOG_STUCK_RUNS
    # nights. Not a failure: cron-guard sends its own "behind" notice.
    log "INFO: [$slug] step A behind (rc=$STEP_A_RC: work committed, backlog carried; cron-guard sends the behind notice)"
    BEHIND_COUNT=$(( BEHIND_COUNT + 1 ))
  elif retry_scheduled "$STEP_A_RC" "$CONS_LABEL" "$STEP_A_START"; then
    log "INFO: [$slug] step A failed, automatic retry scheduled (rc=$STEP_A_RC; operator-only notice)"
    RETRY_COUNT=$(( RETRY_COUNT + 1 ))
  elif [[ $STEP_A_RC -ne 0 ]]; then
    log "WARN: [$slug] step A exit rc=$STEP_A_RC (cron-guard handles per-instance alert)"
    FAIL_COUNT=$(( FAIL_COUNT + 1 ))
  else
    log "INFO: [$slug] step A ok"
  fi

  # ---- step B: classify (runs regardless of A rc; soft deadline applies) ----
  STEP_B_RC=0
  if ! past_soft_deadline; then
    log "INFO: [$slug] step B: classify SKIPPED (past soft deadline 04:55)"
  else
    log "INFO: [$slug] step B: classify (timeout ${CLASSIFY_TIMEOUT}s)"
    STEP_B_START="$(date +%s)"
    if [[ $FORCE_CLASSIFY -eq 1 ]]; then
      run_with_timeout "$CLASSIFY_TIMEOUT" \
        "$CG" \
        --label "$CLAS_LABEL" \
        --log "$INSTANCE_LOG_FILE" \
        --env "$iroot/.telegram_bot/.env" \
        --queue heavy \
        ${CG_UNATTENDED[@]+"${CG_UNATTENDED[@]}"} \
        -- \
        /bin/bash "$CLASSIFY" --force \
        >> "$INSTANCE_LOG_FILE" 2>&1 || STEP_B_RC=$?
    else
      run_with_timeout "$CLASSIFY_TIMEOUT" \
        "$CG" \
        --label "$CLAS_LABEL" \
        --log "$INSTANCE_LOG_FILE" \
        --env "$iroot/.telegram_bot/.env" \
        --queue heavy \
        ${CG_UNATTENDED[@]+"${CG_UNATTENDED[@]}"} \
        -- \
        /bin/bash "$CLASSIFY" \
        >> "$INSTANCE_LOG_FILE" 2>&1 || STEP_B_RC=$?
    fi

    if [[ $STEP_B_RC -eq 124 ]]; then
      log "WARN: [$slug] step B TIMEOUT-KILL (rc=124) after ${CLASSIFY_TIMEOUT}s"
      push_alert "[$slug] step B classify TIMEOUT-KILL after ${CLASSIFY_TIMEOUT}s"
      TIMEOUT_KILLS=$(( TIMEOUT_KILLS + 1 ))
    elif retry_scheduled "$STEP_B_RC" "$CLAS_LABEL" "$STEP_B_START"; then
      log "INFO: [$slug] step B failed, automatic retry scheduled (rc=$STEP_B_RC; operator-only notice)"
      RETRY_COUNT=$(( RETRY_COUNT + 1 ))
    elif [[ $STEP_B_RC -ne 0 ]]; then
      log "WARN: [$slug] step B exit rc=$STEP_B_RC (cron-guard handles per-instance alert)"
      FAIL_COUNT=$(( FAIL_COUNT + 1 ))
    else
      log "INFO: [$slug] step B ok"
    fi
  fi

  if [[ ($STEP_A_RC -eq 0 || $STEP_A_RC -eq $CONSOLIDATE_RC_BEHIND) \
        && ($STEP_B_RC -eq 0 || $STEP_B_RC -eq 2) ]]; then
    SUCCESS_COUNT=$(( SUCCESS_COUNT + 1 ))
  fi

done < "$REGISTRY_TMP"

# ---- DGN-1673 post-step (after every consolidate) ----
run_restart_poststep "$REGISTRY_TMP"
run_agent_check_poststep "$REGISTRY_TMP"

rm -f "$REGISTRY_TMP"

# ---- wall-time check ----
WALL_SECS=$(( $(date +%s) - QUEUE_START_EPOCH ))
WALL_MIN=$(( WALL_SECS / 60 ))

# ---- summary verdict (DGN-1468) ----
# A timeout kill IS a failure: 4 days of one instance's consolidate TIMEOUT-KILLs read
# as "fail=0" because timeouts were counted in a side bucket whose name does
# not read as failure. fail= now includes timeout kills (timeout_kills= stays
# as the sub-count), and the line carries an explicit verdict token so fail=0
# can never stand alone as the health signal. "dmq complete" prefix kept as
# the stable grep anchor. Consumer census 2026-09-15: zero code parses this
# line (health-observer reads only the launchd exit code) -- format change is
# safe.
FAIL_TOTAL=$(( FAIL_COUNT + TIMEOUT_KILLS ))
if [[ $FAIL_TOTAL -eq 0 ]]; then
  VERDICT="OK"
  log "INFO: dmq complete -- verdict=$VERDICT instances=$INSTANCE_COUNT ok=$SUCCESS_COUNT fail=$FAIL_TOTAL skip=$SKIP_COUNT timeout_kills=$TIMEOUT_KILLS wall=${WALL_SECS}s prestep=$PRESTEP_RC restarts=$RESTARTS behind=$BEHIND_COUNT retry=$RETRY_COUNT"
else
  VERDICT="DEGRADED"
  log "WARN: dmq complete -- verdict=$VERDICT instances=$INSTANCE_COUNT ok=$SUCCESS_COUNT fail=$FAIL_TOTAL skip=$SKIP_COUNT timeout_kills=$TIMEOUT_KILLS wall=${WALL_SECS}s prestep=$PRESTEP_RC restarts=$RESTARTS behind=$BEHIND_COUNT retry=$RETRY_COUNT"
fi

if [[ $WALL_SECS -ge $WALL_ALERT_SECS ]]; then
  push_alert "PATHOLOGY: wall time ${WALL_MIN}min exceeded 60min threshold"
fi

# ---- write last-success state file ----
if [[ $SUCCESS_COUNT -gt 0 ]]; then
  mkdir -p "$(dirname "$STATE_FILE")"
  date '+%Y-%m-%dT%H:%M:%S%z' > "$STATE_FILE"
  echo "instances=$SUCCESS_COUNT wall_secs=$WALL_SECS" >> "$STATE_FILE"
fi

# ---- exit code = machine-readable verdict (DGN-1468) ----
# The script used to exit 0 unconditionally, so launchd LastExitStatus was
# always 0 and health-observer's fail-streak rail (the ONE mechanical
# consumer of this job's outcome) could never fire. Exit 1 when any step
# failed or was timeout-killed: after GENERAL_STREAK_NOTIFY consecutive bad
# days health-observer pushes on its own -- an automatic discovery path that
# does not depend on this script's own alert reaching the owner. launchd has
# no KeepAlive/retry on this label, so a non-zero exit changes nothing else.
if [[ $FAIL_TOTAL -gt 0 ]]; then
  exit 1
fi
exit 0

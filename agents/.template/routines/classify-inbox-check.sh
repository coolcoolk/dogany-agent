#!/bin/bash
# __AGENT_LABEL__ weekly inbox classification check -- daily 05:00 (Asia/Seoul),
# i.e. AFTER the 04:30 consolidate that appends the night's notes into inbox.md.
#
# Layer-2 of the memory model: consolidate (04:30) only dumps notes into
# inbox.md; this routine is what actually routes inbox.md into the topic files.
# Without it, inbox.md grows forever and is never classified.
#
# Cheap check first, expensive claude (Opus) call only when there is something
# to classify:
#   1) Marker (<workspace>/.classify_inbox_last) with a success within the last
#      7 days -> skip (already done this week).
#   2) Real-item presence via `memory.py inbox-count` (same _inbox_items basis as
#      classify-inbox -- excludes header / comment seeds / empty sections). If
#      empty (rc=2) -> skip.
#   3) Classification needed -> run `memory.py classify-inbox`.
#   4) memory.py classify-inbox exit contract:
#        rc=0 (real classification succeeded) -> marker only when drained;
#             carryover resumes after the dawn queue without a failure alert (DGN-2079).
#        rc=2 (nothing to classify)           -> no marker (keep checking daily
#                                                until real items arrive).
#        rc=1 (limit / error, inbox untouched) -> no marker -> retry next run.
#        rc=3 (classified, reindex could not get the database; DGN-1873)
#                                             -> marker written + reindex
#                                                pending: the next run retries
#                                                the reindex alone.
#   So the marker's presence IS the state; the agent never has to metacognate a
#   failure.
#
# Automatic retry (DGN-1873, owner directive 2026-10-06): this job runs again
# on its own schedule (the dawn queue's classify step and the 05:00 job), so
# its FIRST failure is not the owner's business. It exits 78 and writes the
# cron-guard retry marker ($DOGANY_RETRY_MARKER) -> operator-only notice. If
# the next run (the retry) fails too, it exits 1 -> the normal failure alarm.
# The "retry already pending" state is $RETRY_PENDING; any good run clears it.
# DGN-2079: a nonshrinking backlog across two dawns is actionable (rc=1,
# no extra retry grace). One budget-deferred run is normal carryover.
#
# systemd note: this wrapper is scheduled as a Type=oneshot service on Linux,
# where any non-zero exit is recorded as a FAILED unit. A "nothing to do" run
# (empty inbox, rc=2) is NORMAL, not a failure -- so we exit 0 for both rc=0 and
# rc=2. Only a failure (78 retry scheduled / 1 retry failed) exits non-zero,
# which correctly surfaces as a failed unit AND leaves the retry in place.
#
# Usage: classify-inbox-check.sh [--force]   (--force = ignore 7-day marker)
set -uo pipefail

# BASE derived from the script's own location (dynamic) -- survives workspace moves.
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEM_DIR="$BASE/memory-engine"
MARKER="$BASE/.classify_inbox_last"
RETRY_PENDING="$BASE/.classify_retry_pending"
REINDEX_PENDING="$BASE/.classify_reindex_pending"
CARRYOVER="$BASE/.classify_inbox_carryover"
PROGRESS="$MEM_DIR/.classify-progress.json"
DAWN_HISTORY="$MEM_DIR/.classify-dawn-history.json"
# DGN-2079: standalone scheduled runs get the same bounded work window.
# The queue supplies an earlier absolute deadline that includes heavy-slot wait.
export DOGANY_CLASSIFY_DEADLINE_EPOCH="${DOGANY_CLASSIFY_DEADLINE_EPOCH:-$(( $(date +%s) + 840 ))}"
export DOGANY_CLASSIFY_PROGRESS_FILE="$PROGRESS"
RC_RETRY_SCHEDULED=78
PY="/usr/bin/python3"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

log() { echo "[classify-check] $*"; }

# fail_retryable <what>: a failure the next scheduled run retries by itself.
# First one -> exit 78 + retry marker (cron-guard: operator only). One while a
# retry was already pending -> that retry failed too -> exit 1 (owner alarm).
fail_retryable() {
    local what="$1"
    if [[ -f "$RETRY_PENDING" ]]; then
        log "$what failed again on the automatic retry -- failure alarm (rc=1)."
        exit 1
    fi
    date '+%Y-%m-%dT%H:%M:%S%z' > "$RETRY_PENDING"
    if [[ -n "${DOGANY_RETRY_MARKER:-}" ]]; then
        printf '{"job":"classify-inbox","what":"%s","at":"%s"}\n' "$what" \
            "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$DOGANY_RETRY_MARKER" 2>/dev/null || true
    fi
    log "$what failed -- auto-retry at the next scheduled run (rc=$RC_RETRY_SCHEDULED)."
    exit "$RC_RETRY_SCHEDULED"
}

clear_retry() { rm -f "$RETRY_PENDING"; }

cd "$MEM_DIR" || { log "memory dir missing: $MEM_DIR"; exit 1; }

# The 05:00 job, dawn continuation and idle continuation share one writer.
# A busy invocation yields normally; the existing caller will finish or leave
# durable carryover for the next tick. flock is released even after SIGKILL.
if [[ "${DOGANY_CLASSIFY_LOCK_ROOT:-}" != "$BASE" ]]; then
    exec "$PY" - "$BASE" "$@" <<'PYLOCK'
import fcntl, os, subprocess, sys
base = sys.argv[1]
with open(os.path.join(base, "memory-engine", ".classify-run.lock"), "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[classify-check] classification already running -- yielding saved work.")
        sys.exit(0)
    env = dict(os.environ, DOGANY_CLASSIFY_LOCK_ROOT=base)
    sys.exit(subprocess.run(["/bin/bash", os.path.join(base, "routines", "classify-inbox-check.sh"),
                             *sys.argv[2:]], env=env).returncode)
PYLOCK
fi

# ---- 0) reindex left pending by a classify whose reindex was deferred (rc=3) ----
if [[ -f "$REINDEX_PENDING" && ! -f "$CARRYOVER" ]]; then
    log "search index refresh pending from the last classification -- retrying it."
    if "$PY" - "$DOGANY_CLASSIFY_DEADLINE_EPOCH" <<'PYINDEX'
import os, subprocess, sys, time
try:
    rc = subprocess.run([sys.executable, "memory.py", "index", "--wait-lock"],
                        timeout=max(1, float(sys.argv[1]) - time.time() - 5)).returncode
except subprocess.TimeoutExpired:
    rc = 1
sys.exit(rc)
PYINDEX
    then
        rm -f "$REINDEX_PENDING"
        clear_retry
        log "pending reindex ok."
    else
        fail_retryable "reindex"
    fi
fi

# ---- 1) 7-day marker check ----
if [[ "$FORCE" -eq 0 && -f "$MARKER" && ! -f "$CARRYOVER" ]]; then
    now_epoch=$(date +%s)
    # stat is BSD (-f %m) on macOS, GNU (-c %Y) on Linux -- try both.
    marker_epoch=$(stat -c %Y "$MARKER" 2>/dev/null || stat -f %m "$MARKER" 2>/dev/null || echo 0)
    age_days=$(( (now_epoch - marker_epoch) / 86400 ))
    if [[ "$age_days" -lt 7 ]]; then
        log "classified ${age_days}d ago -- already done this week, skip."
        clear_retry
        exit 0
    fi
fi

# ---- 2) real-item check (same basis as classify-inbox) ----
# inbox-count: prints the real-item count on stdout, rc=0 (has items) / rc=2 (empty).
# No claude call here (cheap).
count=$("$PY" memory.py inbox-count 2>/dev/null)
crc=$?
if [[ "$crc" -ne 0 && "$crc" -ne 2 ]]; then
    fail_retryable "inbox-count"
fi
if [[ "$crc" -eq 2 ]]; then
    rm -f "$CARRYOVER" "$DAWN_HISTORY"
    log "no real inbox items (${count:-0}) -- skip (no marker)."
    clear_retry
    exit 0
fi
log "inbox has ${count} real item(s) -- classifying."

# ---- 3) run classification ----
rm -f "$PROGRESS"
"$PY" memory.py classify-inbox
rc=$?

# A fresh structured result distinguishes saved carryover from a completed
# weekly run. Older engines without this result retain their exit contract.
if [[ -f "$PROGRESS" ]]; then
    status=$("$PY" - "$PROGRESS" "$DAWN_HISTORY" <<'PYSTATUS'
import datetime, json, os, sys
with open(sys.argv[1]) as f:
    result = json.load(f)
remaining = result["remaining"]
stalled = False
# Only DMQ supplies a dawn date. The 05:00 retry and manual runs must not
# advance the two-dawn comparison. Repeated runs on one date are one dawn.
day = os.environ.get("DOGANY_CLASSIFY_DAWN_DATE")
if day:
    try:
        with open(sys.argv[2]) as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = {}
    yesterday = (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()
    if remaining > 0 and previous.get("day") == yesterday:
        stalled = stalled or remaining >= previous.get("remaining", remaining + 1)
    if previous.get("day") != day:
        with open(sys.argv[2] + ".tmp", "w") as f:
            json.dump(dict(day=day, remaining=remaining), f)
        os.replace(sys.argv[2] + ".tmp", sys.argv[2])
print(remaining, int(stalled), int(result.get("reindex_pending", False)), result.get("processed", 0))
PYSTATUS
    ) || { log "cannot read classification progress"; exit 1; }
    read -r remaining stalled reindex_pending processed <<< "$status"
    if [[ "$reindex_pending" -eq 1 ]]; then
        touch "$REINDEX_PENDING"
    elif [[ "$remaining" -eq 0 && ( "$rc" -eq 0 || "$rc" -eq 3 ) ]]; then
        rm -f "$REINDEX_PENDING"
    fi
    if [[ "$remaining" -gt 0 ]]; then
        touch "$CARRYOVER"
        rm -f "$MARKER"
        if [[ "$stalled" -eq 1 ]]; then
            log "classification stalled: remaining count did not shrink across 2 consecutive dawns ($remaining items left)."
            # This is actionable now; do not hide it behind another retry.
            exit 1
        fi
        if [[ "$rc" -eq 0 || "$rc" -eq 3 ]]; then
            if [[ -n "${DOGANY_CLASSIFY_CARRYOVER_FILE:-}" ]]; then
                log "$remaining items left, continuation queued after the dawn queue"
            else
                log "$remaining items left, resumes at the next scheduled run"
            fi
            if [[ -n "${DOGANY_CLASSIFY_CARRYOVER_FILE:-}" ]]; then
                printf '%s %s\n' "$remaining" "$processed" > "$DOGANY_CLASSIFY_CARRYOVER_FILE"
            fi
            clear_retry
            exit 0
        fi
    else
        # Fresh notes must not be compared with a backlog already drained.
        rm -f "$CARRYOVER" "$DAWN_HISTORY"
    fi
fi

# ---- 4) marker handling + systemd-safe exit ----
case "$rc" in
    0)
        date '+%Y-%m-%dT%H:%M:%S%z' > "$MARKER"
        log "classification ok -- marker written (skip next 7 days)."
        clear_retry
        exit 0
        ;;
    2)
        log "nothing to classify (rc=2) -- no marker (checking daily until real items)."
        clear_retry
        exit 0
        ;;
    3)
        # DGN-1873: the classification itself is on disk and stays; only the
        # search index is stale. Marker as for rc=0; the reindex is retried.
        date '+%Y-%m-%dT%H:%M:%S%z' > "$MARKER"
        touch "$REINDEX_PENDING"
        # A classify retry that just succeeded is resolved; the deferred
        # reindex is a new failure with its own retry.
        clear_retry
        log "classification ok, search index refresh deferred (rc=3) -- marker written."
        fail_retryable "reindex"
        ;;
    *)
        log "classification failed (rc=$rc) -- no marker."
        fail_retryable "classify"
        ;;
esac

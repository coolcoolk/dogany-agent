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
#        rc=0 (real classification succeeded) -> write marker (skip next 7 days).
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

# ---- 0) reindex left pending by a classify whose reindex was deferred (rc=3) ----
if [[ -f "$REINDEX_PENDING" ]]; then
    log "search index refresh pending from the last classification -- retrying it."
    if "$PY" memory.py index --wait-lock; then
        rm -f "$REINDEX_PENDING"
        clear_retry
        log "pending reindex ok."
    else
        fail_retryable "reindex"
    fi
fi

# ---- 1) 7-day marker check ----
if [[ "$FORCE" -eq 0 && -f "$MARKER" ]]; then
    now_epoch=$(date +%s)
    # stat is BSD (-f %m) on macOS, GNU (-c %Y) on Linux -- try both.
    marker_epoch=$(stat -f %m "$MARKER" 2>/dev/null || stat -c %Y "$MARKER" 2>/dev/null || echo 0)
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
if [[ "$crc" -ne 0 ]]; then
    log "no real inbox items (${count:-0}) -- skip (no marker)."
    clear_retry
    exit 0
fi
log "inbox has ${count} real item(s) -- classifying."

# ---- 3) run classification ----
"$PY" memory.py classify-inbox
rc=$?

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

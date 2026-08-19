#!/bin/bash
# __AGENT_LABEL__ nightly consolidate -- runs once a day, off-hours.
# Transcript delta -> Sonnet compression -> rule/two-stage filter -> dedup ->
# plain append into inbox.md -> silent report.
# Only reads conversations after the watermark (state.db), so each run handles
# only the new conversations. Runs quietly while the user is asleep.
# No topic routing here -- the nightly pass always writes to inbox.md; the weekly
# classify-inbox distributes it into topic files.
# Paths are resolved relative to the script's own location (dynamic), so the job
# survives a workspace move.
#
# Usage-defer (DGN-726, protocol upgraded by DGN-835): when memory.py detects
# that the account usage window is exhausted it preserves the watermark, writes
# the defer marker (env DOGANY_USAGE_DEFER_MARKER when run under cron-guard,
# else the legacy .consolidate-usage-defer.json beside state.db) and exits 75
# (EX_TEMPFAIL). Recovery routing is OWNED BY cron-guard (rc75 + fresh marker
# -> five_hour: automatic reset-aligned one-shot via usage-defer-retry.sh;
# seven_day: owner notification with a manual-retry button). This wrapper no
# longer schedules its own retry -- the DGN-726 scheduler was generalized into
# routines/usage-defer-retry.sh so there is exactly ONE recovery mechanism.
# The --from-reset-retry cleanup branch below is kept for ONE release so
# one-shot jobs registered by the pre-DGN-835 scheduler still self-clean.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="$SCRIPT_DIR/../memory-engine"
DEFER_MARKER="$ENGINE_DIR/.consolidate-usage-defer.json"

cd "$ENGINE_DIR" || exit 1

# dedup / neighbor lookup assume a fresh notes index -- the search hook does not
# run at night, so re-index explicitly.
/usr/bin/python3 memory.py index
/usr/bin/python3 memory.py consolidate
CONSOLIDATE_RC=$?

# self-clean a fired one-shot retry job (legacy DGN-726 scheduler units; keep
# for one release after DGN-835, then remove together with this branch).
cleanup_retry_job() {
  local label="$1" plist="$2"
  [[ -z "$label" ]] && return 0
  case "$(uname -s)" in
    Darwin)
      [[ -n "$plist" ]] && rm -f "$plist"
      launchctl remove "$label" 2>/dev/null || true
      ;;
    *)
      systemctl --user reset-failed "$label.timer" 2>/dev/null || true
      systemctl --user reset-failed "$label.service" 2>/dev/null || true
      ;;
  esac
}

# If this invocation IS a fired legacy one-shot retry, clean the job up now that
# it ran. ($1=--from-reset-retry $2=label $3=plist). The consolidate above already ran.
if [[ "${1:-}" == "--from-reset-retry" ]]; then
  cleanup_retry_job "${2:-}" "${3:-}"
fi

# Legacy-marker hygiene: if consolidate SUCCEEDED and a LEGACY marker is present
# from a prior deferred run (not freshly written by this run), it is obsolete ->
# clear it. memory.py only WRITES the marker on defer and never clears it, so the
# wrapper owns clearing. "Fresh" = deferred_at within the last 10 min.
if [[ "$CONSOLIDATE_RC" -eq 0 && -f "$DEFER_MARKER" ]]; then
  DEFERRED_THIS_RUN="$(/usr/bin/python3 -c '
import json, sys
from datetime import datetime, timezone
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    da = d.get("deferred_at", "")
    dt = datetime.fromisoformat(da.replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    print("1" if age < 600 else "0")
except Exception:
    print("1")
' "$DEFER_MARKER" 2>/dev/null)"
  if [[ "$DEFERRED_THIS_RUN" == "0" ]]; then
    rm -f "$DEFER_MARKER"
    echo "[consolidate-0430] prior usage-defer resolved -- marker cleared"
  fi
fi

# DGN-835: rc75 = usage-defer signal. Recovery (retry scheduling / owner notify)
# is handled by cron-guard, which wraps this script in the shipped plist. When
# run WITHOUT cron-guard (manual invocation) there is no scheduler -- the next
# daily run re-collects the preserved span (fail-open).
if [[ "$CONSOLIDATE_RC" -eq 75 ]]; then
  echo "[consolidate-0430] usage-defer (rc75): recovery routed via cron-guard; next daily run covers the manual-invocation case"
fi

exit "$CONSOLIDATE_RC"

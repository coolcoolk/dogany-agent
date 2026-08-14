#!/bin/bash
# test-cron-guard-usage-defer.sh -- DGN-835 cron-guard rc75 recovery branch.
#
# Exercises the 2-factor usage-defer protocol WITHOUT touching launchd,
# Telegram, or a real job:
#   1. rc75 + fresh five_hour marker -> usage-defer-retry.sh invoked with the
#      ORIGINAL cron-guard argv; no alert push.
#   2. rc75 + fresh seven_day marker -> self-deleting replay file (quoting-safe)
#      + push with locked copy, "지금 실행" button, /usageretry slash line, and
#      a reset-aligned fresh-button re-notify one-shot (DGN-841 B).
#   2b. 7d notify dedup per label/day (replay still refreshed).
#   2c. oversize label -> button dropped (64B callback_data cap), text survives.
#   2d-2f. MAJOR-C stale reader: last_success_at older than 1d+6h -> ONE stale
#      alert per label/day; silent inside grace / without the field.
#   3. rc75 WITHOUT marker -> normal failure alert path (accidental 75 not silenced).
#   4. rc75 + STALE marker (>10 min)  -> normal failure alert path.
#   5. rc1 (non-75) + fresh marker    -> normal failure alert path (code must be 75).
#   6. wrapper exports DOGANY_USAGE_DEFER_MARKER=<dir>/<label>.json to the job.
#
# Strategy: copy cron-guard.sh into a temp routines/ dir with stub push.sh and
# stub usage-defer-retry.sh; the wrapped command is a stub that writes the
# marker (via the exported env var) and exits with the requested code.
# HOME is redirected so ~/.dogany/usage-defer stays in the sandbox.
#
# Run: bash routines/tests/test-cron-guard-usage-defer.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
assert_contains() {
  local desc="$1" hay="$2" needle="$3"
  if [[ "$hay" == *"$needle"* ]]; then ok "$desc"; else fail "$desc -- missing '$needle' in: $hay"; fi
}
assert_not_contains() {
  local desc="$1" hay="$2" needle="$3"
  if [[ "$hay" != *"$needle"* ]]; then ok "$desc"; else fail "$desc -- unexpected '$needle'"; fi
}

TMP="$(mktemp -d /tmp/cron-guard-defer-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

FAKE_HOME="$TMP/home"
mkdir -p "$FAKE_HOME"

# sandboxed routines dir: cron-guard + stub push.sh + stub usage-defer-retry.sh
RT="$TMP/routines"
mkdir -p "$RT" "$TMP/.telegram_bot/logs"
cp "$ROUTINES_DIR/cron-guard.sh" "$RT/cron-guard.sh"
chmod +x "$RT/cron-guard.sh"

PUSH_LOG="$TMP/push.log"
cat > "$RT/push.sh" <<EOF
#!/bin/bash
printf '%s\n' "PUSH \$*" >> "$PUSH_LOG"
exit 0
EOF
chmod +x "$RT/push.sh"

RETRY_LOG="$TMP/retry.log"
cat > "$RT/usage-defer-retry.sh" <<EOF
#!/bin/bash
printf '%s\n' "RETRY \$*" >> "$RETRY_LOG"
exit 0
EOF
chmod +x "$RT/usage-defer-retry.sh"

# stub job: writes the usage-defer marker via the exported env var (unless
# MARKER_MODE=none), then exits JOB_RC. MARKER_MODE=five_hour|seven_day|none.
# LAST_SUCCESS_AGO_H (optional): embed last_success_at N hours in the past
# (feeds the MAJOR-C stale reader).
JOB="$TMP/job.sh"
cat > "$JOB" <<'EOF'
#!/bin/bash
if [[ "${MARKER_MODE:-none}" != "none" && -n "${DOGANY_USAGE_DEFER_MARKER:-}" ]]; then
  mkdir -p "$(dirname "$DOGANY_USAGE_DEFER_MARKER")"
  python3 -c "
import json, os, sys
from datetime import datetime, timezone, timedelta
now = datetime.now(timezone.utc)
reset = (now + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
d = {'window': sys.argv[2], 'reset_at': reset,
     'deferred_at': now.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
     'reason': 'usage-exhaustion'}
ago = os.environ.get('LAST_SUCCESS_AGO_H', '')
if ago:
    d['last_success_at'] = (now - timedelta(hours=float(ago))).strftime('%Y-%m-%dT%H:%M:%SZ')
json.dump(d, open(sys.argv[1], 'w'))
" "$DOGANY_USAGE_DEFER_MARKER" "$MARKER_MODE"
fi
echo "job ran with arg: ${1:-none}"
exit "${JOB_RC:-0}"
EOF
chmod +x "$JOB"

LABEL="com.test.dgn835.defer-job"
DEFER_DIR="$FAKE_HOME/.dogany/usage-defer"
ENV_FILE="$TMP/.env"
echo "TELEGRAM_BOT_TOKEN=fake" > "$ENV_FILE"

run_guard() {
  # $1=JOB_RC $2=MARKER_MODE; extra job arg carries a space to test quoting.
  # GUARD_LABEL / LAST_SUCCESS_AGO_H may be pre-set by the caller.
  HOME="$FAKE_HOME" JOB_RC="$1" MARKER_MODE="$2" \
  LAST_SUCCESS_AGO_H="${LAST_SUCCESS_AGO_H:-}" \
    bash "$RT/cron-guard.sh" --label "${GUARD_LABEL:-$LABEL}" --friendly-name "defer job" \
      --log "$TMP/.telegram_bot/logs/job.stdout.log" --env "$ENV_FILE" \
      -- "$JOB" "arg with space" 2>&1
  echo "GUARD_RC=$?"
}

reset_logs() {
  : > "$PUSH_LOG"; : > "$RETRY_LOG"
  rm -rf "$DEFER_DIR" /tmp/dogany-cron-guard/"${LABEL}".* 2>/dev/null || true
}

echo "== DGN-835 cron-guard usage-defer recovery =="

# --- 1. rc75 + five_hour marker -> retry helper, no alert ---
reset_logs
OUT="$(run_guard 75 five_hour)"
assert_contains "5h: recovery branch taken" "$OUT" "usage-defer (five_hour)"
assert_contains "5h: exit code preserved (75)" "$OUT" "GUARD_RC=75"
if [[ -s "$RETRY_LOG" ]]; then ok "5h: usage-defer-retry.sh invoked"; else fail "5h: retry helper NOT invoked"; fi
RETRY_LINE="$(cat "$RETRY_LOG" 2>/dev/null || true)"
assert_contains "5h: helper got the label" "$RETRY_LINE" "--label $LABEL"
assert_contains "5h: helper replays cron-guard argv (label arg)" "$RETRY_LINE" "cron-guard.sh --label $LABEL"
assert_contains "5h: original job arg carried" "$RETRY_LINE" "arg with space"
if [[ -s "$PUSH_LOG" ]]; then fail "5h: push wrongly fired"; else ok "5h: no push (fully automatic)"; fi

# --- 2. rc75 + seven_day marker -> replay file + button push, no generic alert ---
reset_logs
OUT="$(run_guard 75 seven_day)"
assert_contains "7d: recovery branch taken" "$OUT" "usage-defer (seven_day)"
assert_contains "7d: exit code preserved (75)" "$OUT" "GUARD_RC=75"
REPLAY="$DEFER_DIR/${LABEL}.replay"
if [[ -x "$REPLAY" ]]; then ok "7d: replay file written+executable"; else fail "7d: replay file missing"; fi
REPLAY_BODY="$(cat "$REPLAY" 2>/dev/null || true)"
assert_contains "7d: replay execs cron-guard" "$REPLAY_BODY" "cron-guard.sh"
assert_contains "7d: replay carries quoted job arg" "$REPLAY_BODY" "arg\\ with\\ space"
assert_contains "7d: replay self-deletes on launch (rm-on-launch)" "$REPLAY_BODY" 'rm -f -- "$0"'
PUSH_LINE="$(cat "$PUSH_LOG" 2>/dev/null || true)"
assert_contains "7d: push carries usageretry button" "$PUSH_LINE" "--button"
assert_contains "7d: button callback data has the label" "$PUSH_LINE" "usageretry:$LABEL"
assert_contains "7d: button label = locked copy (지금 실행)" "$PUSH_LINE" "지금 실행::usageretry:$LABEL"
assert_contains "7d: body = locked waiting copy" "$PUSH_LINE" "주간 사용량 한도로 대기 중입니다"
assert_contains "7d: body carries the slash fallback line (DGN-841 A)" "$PUSH_LINE" "/usageretry $LABEL"
assert_contains "7d: emoji slot resolved (no .instance.conf -> [agent] fallback)" "$PUSH_LINE" "[agent]"
assert_not_contains "7d: no generic ROUTINE FAILED alert" "$PUSH_LINE" "ROUTINE FAILED"
# replay file must actually re-enter cron-guard (dry check: file references sandbox path)
assert_contains "7d: replay anchored at sandbox cron-guard" "$REPLAY_BODY" "$RT/cron-guard.sh"
RETRY_LINE="$(cat "$RETRY_LOG" 2>/dev/null || true)"
assert_contains "7d: fresh-button re-notify one-shot scheduled (DGN-841 B)" "$RETRY_LINE" "--label ${LABEL}.notify7d"
assert_contains "7d: re-notify replays push.sh with a button" "$RETRY_LINE" "push.sh --text"
assert_contains "7d: re-notify carries the approved reset copy" "$RETRY_LINE" "주간 사용량이 리셋됐어요"

# --- 2b. 7d notify dedup per label/day (paired with the stale reader) ---
OUT="$(run_guard 75 seven_day)"
assert_contains "7d dedup: second same-day defer suppressed" "$OUT" "notify suppressed (dedup)"
NOTIFY_COUNT="$(grep -c "usageretry:$LABEL" "$PUSH_LOG" 2>/dev/null || true)"
[[ "$NOTIFY_COUNT" == "1" ]] && ok "7d dedup: exactly one notify pushed today" \
                             || fail "7d dedup: expected 1 notify, got $NOTIFY_COUNT"
if [[ -x "$REPLAY" ]]; then ok "7d dedup: replay still refreshed on the suppressed run"; else fail "7d dedup: replay missing after suppressed run"; fi

# --- 2c. oversize label -> button dropped, text (with slash line) still sent ---
reset_logs
GUARD_LABEL="com.test.dgn835.$(printf 'x%.0s' $(seq 1 60))"   # usageretry:<label> > 64 bytes
rm -rf /tmp/dogany-cron-guard/"${GUARD_LABEL}".* 2>/dev/null || true
OUT="$(GUARD_LABEL="$GUARD_LABEL" run_guard 75 seven_day)"
PUSH_LINE="$(cat "$PUSH_LOG" 2>/dev/null || true)"
assert_not_contains "oversize: --button omitted (callback_data would exceed 64B)" "$PUSH_LINE" "--button"
assert_contains "oversize: text still pushed" "$PUSH_LINE" "주간 사용량 한도로 대기 중입니다"
assert_contains "oversize: slash fallback line still present" "$PUSH_LINE" "/usageretry $GUARD_LABEL"
rm -rf /tmp/dogany-cron-guard/"${GUARD_LABEL}".* 2>/dev/null || true
unset GUARD_LABEL

# --- 2d. MAJOR-C stale reader: old last_success + defer -> ONE stale alert ---
reset_logs
OUT="$(LAST_SUCCESS_AGO_H=40 run_guard 75 five_hour)"
PUSH_LINE="$(cat "$PUSH_LOG" 2>/dev/null || true)"
assert_contains "stale reader: alert fired (last success 40h ago > 1d+6h)" "$PUSH_LINE" "실행되지 못했어요"
if [[ -s "$RETRY_LOG" ]]; then ok "stale reader: 5h auto-retry still scheduled alongside"; else fail "stale reader: 5h retry missing"; fi
OUT="$(LAST_SUCCESS_AGO_H=40 run_guard 75 five_hour)"
assert_contains "stale reader: same-day re-fire suppressed (dedup)" "$OUT" "stale alert suppressed (dedup)"
STALE_COUNT="$(grep -c "실행되지 못했어요" "$PUSH_LOG" 2>/dev/null || true)"
[[ "$STALE_COUNT" == "1" ]] && ok "stale reader: exactly one stale alert today" \
                            || fail "stale reader: expected 1 stale alert, got $STALE_COUNT"

# --- 2e. stale reader stays silent inside the grace window ---
reset_logs
OUT="$(LAST_SUCCESS_AGO_H=25 run_guard 75 five_hour)"
PUSH_LINE="$(cat "$PUSH_LOG" 2>/dev/null || true)"
assert_not_contains "stale reader: 25h (< 30h threshold) stays silent" "$PUSH_LINE" "실행되지 못했어요"

# --- 2f. stale reader silent when marker has no last_success_at (fail-open) ---
reset_logs
OUT="$(run_guard 75 five_hour)"
PUSH_LINE="$(cat "$PUSH_LOG" 2>/dev/null || true)"
assert_not_contains "stale reader: no last_success_at -> no stale alert" "$PUSH_LINE" "실행되지 못했어요"

# --- 3. rc75 WITHOUT marker -> normal alert path ---
reset_logs
OUT="$(run_guard 75 none)"
assert_not_contains "75-alone: recovery NOT taken" "$OUT" "usage-defer (five_hour)"
PUSH_LINE="$(cat "$PUSH_LOG" 2>/dev/null || true)"
assert_contains "75-alone: generic alert fired" "$PUSH_LINE" "ROUTINE FAILED"
assert_contains "75-alone: exit code preserved" "$OUT" "GUARD_RC=75"

# --- 4. rc75 + STALE marker -> normal alert path ---
reset_logs
mkdir -p "$DEFER_DIR"
python3 -c "
import json, sys
json.dump({'window': 'five_hour', 'reset_at': '2026-01-01T00:00:00Z',
           'deferred_at': '2026-01-01T00:00:00.000000Z', 'reason': 'usage-exhaustion'},
          open(sys.argv[1], 'w'))
" "$DEFER_DIR/${LABEL}.json"
# age the marker beyond 10 minutes
OLD_TS="$(python3 -c 'import time; print(int(time.time()) - 1200)')"
touch -t "$(date -r "$OLD_TS" +%Y%m%d%H%M.%S 2>/dev/null || date -d "@$OLD_TS" +%Y%m%d%H%M.%S)" "$DEFER_DIR/${LABEL}.json"
OUT="$(run_guard 75 none)"
if [[ -s "$RETRY_LOG" ]]; then fail "stale: retry helper wrongly invoked"; else ok "stale: retry helper not invoked"; fi
PUSH_LINE="$(cat "$PUSH_LOG" 2>/dev/null || true)"
assert_contains "stale: generic alert fired" "$PUSH_LINE" "ROUTINE FAILED"

# --- 5. rc1 + fresh marker -> normal alert (code must be exactly 75) ---
reset_logs
OUT="$(run_guard 1 five_hour)"
if [[ -s "$RETRY_LOG" ]]; then fail "rc1: retry helper wrongly invoked"; else ok "rc1: retry helper not invoked"; fi
PUSH_LINE="$(cat "$PUSH_LOG" 2>/dev/null || true)"
assert_contains "rc1: generic alert fired" "$PUSH_LINE" "ROUTINE FAILED"
assert_contains "rc1: exit code preserved" "$OUT" "GUARD_RC=1"

# --- 6. success path still silent + env exported ---
reset_logs
OUT="$(run_guard 0 none)"
assert_contains "success: silent + rc0" "$OUT" "GUARD_RC=0"
if [[ -s "$PUSH_LOG" ]]; then fail "success: push wrongly fired"; else ok "success: no push"; fi

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]

#!/bin/bash
# test-lane: fast    # measured 1.1s (2026-09-04 full sweep, DGN-1267)
# test-cron-guard.sh -- unit tests for cron-guard.sh
#
# Tests (no real Telegram push; push.sh replaced by a stub):
#   1. success job (exit 0)  -> no alert
#   2. failure job (exit 1)  -> alert fired once, dedup file created
#   3. same failure, same day -> dedup suppresses second alert
#   4. success after prior failure -> still silent (exit 0)
#   5. different label same day -> independent dedup fires
#   6. alert headline carries NO machine identifiers (no label / exit /
#      component); the label rides only the folded detail block (DGN-1747)
#   7. no --log arg -> default log path derivation (no crash)
#  12. DGN-1747 approved copy: agent name via lib/agent_name.py, job via the
#      friendly seat, ask button naming agent + job, headline clean
#  13. DGN-1582 behind exit (77 + marker this run) -> never the failure
#      alarm, night 1 silent; stale/absent marker -> failure
#  14. dec-236 behind routing: night 2 -> one operator notice (ko approved
#      copy; cap -> no "more time" claim), night 3 no repeat, owner never
#      (with or without an operator), a clean night resets the streak
#  15. DGN-1873 retry scheduled (78 + marker this run) -> operator only with
#      --no-owner-fallback, no button, once per label/day; stale/absent
#      marker -> failure alarm
#  16. DGN-1873 --unattended -> dec-252 confirmed alarm: no ask button or
#      question, reason line classified from this run's own log bytes,
#      retry time from the next scheduled fire
#
# Run: bash routines/tests/test-cron-guard.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GUARD="$ROUTINES_DIR/cron-guard.sh"

# portable true/false (macOS: /usr/bin, not /bin)
TRUE_CMD="$(command -v true)"
FALSE_CMD="$(command -v false)"

# ---- test harness ----
PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_eq() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else fail "$desc -- got='$got' want='$want'"; fi
}

# run guard, capture exit code safely (no || true which swallows the code)
run_guard() {
  local _rc=0
  "$PATCHED_GUARD" "$@" || _rc=$?
  echo "$_rc"
}

# Same, but ALSO keeps the guard's stderr in $GUARD_ERR. The log tail used to
# ride the owner push; it is maintainer detail, so it now goes to stderr and
# tests 8/9 read it there.
run_guard_err() {
  local _rc=0
  "$PATCHED_GUARD" "$@" 2> "$GUARD_ERR" || _rc=$?
  echo "$_rc"
}

# ---- isolated temp workspace ----
STUB_DIR="$(mktemp -d /tmp/cron-guard-test.XXXXXX)"
trap 'rm -rf "$STUB_DIR"' EXIT

PUSH_CALL_LOG="$STUB_DIR/push_calls.log"
touch "$PUSH_CALL_LOG"
GUARD_ERR="$STUB_DIR/guard.err"
touch "$GUARD_ERR"

# ---- stub push.sh
# Writes one line per invocation: first --text argument (truncated) + arg count.
# Avoids multiline append by summarising rather than echoing $*. The first two
# text lines (the headline) and the --button spec of the latest call are kept
# whole in PUSH_HEAD / PUSH_BTN (DGN-1747).
STUB_PUSH="$STUB_DIR/push.sh"
PUSH_HEAD="$STUB_DIR/push_head.txt"
PUSH_BTN="$STUB_DIR/push_btn.txt"
PUSH_FULL="$STUB_DIR/push_full.txt"
PUSH_AUD_LOG="$STUB_DIR/push_aud.log"
NO_OPERATOR="$STUB_DIR/no_operator"
cat > "$STUB_PUSH" << STUB_EOF
#!/bin/bash
# stub push.sh -- records calls without real Telegram send
# Extract the --text value (first arg after --text flag)
_text=""
_btn=""
_aud="owner"
_nofb=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --text) _text="\$2"; shift 2 ;;
    --button) _btn="\$2"; shift 2 ;;
    --audience) _aud="\$2"; shift 2 ;;
    --no-owner-fallback) _nofb=1; shift ;;
    *) shift ;;
  esac
done
# DGN-1808 contract: operator unresolved + --no-owner-fallback -> nothing
# sent, rc 9. The NO_OPERATOR flag file stands in for an empty STEWARD_ENV_FILE.
if [[ "\$_aud" == "operator" && -n "\$_nofb" && -f "${NO_OPERATOR}" ]]; then
  echo "operator-unresolved" >> "${PUSH_AUD_LOG}"
  exit 9
fi
echo "\$_aud" >> "${PUSH_AUD_LOG}"
# Write a single summary line (first 80 chars of text, no newlines)
_summary="\$(printf '%s' "\$_text" | tr '\n' ' ' | cut -c1-200)"
echo "PUSH: \${_summary}" >> "${PUSH_CALL_LOG}"
printf '%s\n' "\$_text" | head -n 2 > "${PUSH_HEAD}"
printf '%s' "\$_btn" > "${PUSH_BTN}"
printf '%s' "\$_text" > "${PUSH_FULL}"
exit 0
STUB_EOF
chmod +x "$STUB_PUSH"

# ---- isolated dedup dir ----
DEDUP_DIR="$STUB_DIR/dedup"
mkdir -p "$DEDUP_DIR"

# ---- dummy log file for log-tail test ----
DUMMY_LOG="$STUB_DIR/dummy.stdout.log"
printf 'line1: some output\nline2: more output\nline3: final line\n' > "$DUMMY_LOG"

# ---- patched guard: replace DEDUP_DIR and PUSH_SH in a copy ----
PATCHED_GUARD="$STUB_DIR/cron-guard-patched.sh"
sed \
  -e "s|DEDUP_DIR=\"/tmp/dogany-cron-guard\"|DEDUP_DIR=\"$DEDUP_DIR\"|" \
  -e "s|PUSH_SH=\"\$SCRIPT_DIR/push.sh\"|PUSH_SH=\"$STUB_PUSH\"|" \
  "$GUARD" > "$PATCHED_GUARD"
chmod +x "$PATCHED_GUARD"

LABEL="com.test.cron-guard-test"
TODAY="$(date +%Y%m%d)"

echo ""
echo "=== cron-guard self-tests ==="
echo ""

# ---- test 1: success job -> no alert ----
echo "--- test 1: success job (exit 0) -> no alert ---"
rc="$(run_guard --label "$LABEL" --log "$DUMMY_LOG" -- "$TRUE_CMD")"
assert_eq "exit code == 0" "$rc" "0"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push NOT called for success" "$push_count" "0"
echo ""

# ---- test 2: failure job -> alert fired once ----
echo "--- test 2: failure job (exit 1) -> alert fired ---"
rc="$(run_guard --label "$LABEL" --log "$DUMMY_LOG" -- "$FALSE_CMD")"
assert_eq "exit code == 1 (preserved)" "$rc" "1"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push called exactly once" "$push_count" "1"
dedup_file="$DEDUP_DIR/${LABEL}.${TODAY}"
[[ -f "$dedup_file" ]] && ok "dedup file created" || fail "dedup file missing ($dedup_file)"
echo ""

# ---- test 3: same label, same day re-failure -> dedup suppresses ----
echo "--- test 3: same label re-failure (same day) -> dedup suppresses ---"
rc="$(run_guard --label "$LABEL" --log "$DUMMY_LOG" -- /bin/bash -c "exit 2")"
assert_eq "exit code == 2 (preserved even on dedup)" "$rc" "2"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push still only 1 call (dedup suppressed)" "$push_count" "1"
echo ""

# ---- test 4: success after prior failure -> no push ----
echo "--- test 4: success after prior failure -> no push ---"
rc="$(run_guard --label "$LABEL" --log "$DUMMY_LOG" -- "$TRUE_CMD")"
assert_eq "exit code == 0" "$rc" "0"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push count unchanged at 1" "$push_count" "1"
echo ""

# ---- test 5: different label same day -> independent dedup ----
echo "--- test 5: different label same day -> fires independently ---"
LABEL2="com.test.cron-guard-other"
rc="$(run_guard --label "$LABEL2" --log "$DUMMY_LOG" -- /bin/bash -c "exit 5")"
assert_eq "exit code == 5 (different label, preserved)" "$rc" "5"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push called 2 total (second label fires)" "$push_count" "2"
dedup_file2="$DEDUP_DIR/${LABEL2}.${TODAY}"
[[ -f "$dedup_file2" ]] && ok "dedup file created for second label" || fail "dedup file missing for second label"
echo ""

# ---- test 6: alert headline carries NO machine identifiers ----
# EXPLICITLY INVERTED (twice). This block first asserted the alert CONTAINED
# the job label, the word "exit", and the label's last dot-segment -- it
# pinned the leak. DGN-1747 (owner-approved copy 2026-09-27) then fixed the
# shape: the job is named through the friendly seat (--friendly-name, else
# health-observer's _friendly -- here the dot-segment fallback, since this
# stub dir ships no health-observer.py), and the label / exit code ride ONLY
# the folded detail block below the body, never the two headline lines.
echo "--- test 6: alert headline carries no label / exit code / component token ---"
last_call="$(tail -1 "$PUSH_CALL_LOG")"
head6="$(cat "$PUSH_HEAD")"
echo "  last push call: $last_call"
[[ "$head6" != *"$LABEL2"* ]] && ok "headline does not carry the job label" || fail "headline still leaks the label: $head6"
[[ "$head6" != *"exit"* && "$head6" != *"5"* ]] && ok "headline does not carry an exit code" || fail "headline still leaks an exit code: $head6"
[[ "$last_call" != *"ROUTINE FAILED"* ]] && ok "alert does not carry the component/state token" || fail "alert still leaks ROUTINE FAILED: $last_call"
[[ "$head6" == *"예약 작업이 방금 실패했어요."* ]] && ok "unknown job headline stays generic" || fail "generic job headline missing: $head6"
[[ "$head6" == *$'\n원인을 확인할까요?' ]] && ok "headline asks instead of promising a follow-up" || fail "ask line missing: $head6"
[[ "$last_call" != *"알려드릴게요"* ]] && ok "no promise of work nobody does" || fail "false promise still present: $last_call"
[[ "$last_call" == *">! 기술 상세 > 잡 라벨: $LABEL2 > 종료코드: 5"* ]] && ok "label + exit code ride the folded detail block" || fail "detail block missing; got: $last_call"
echo ""

# ---- test 7: no --log arg -> default log path derivation (no crash) ----
echo "--- test 7: no --log arg (default path fallback) -> no crash ---"
LABEL3="com.test.cron-guard-nolog"
rc="$(run_guard --label "$LABEL3" -- /bin/bash -c "exit 3")"
assert_eq "exit code == 3 (no --log, preserved)" "$rc" "3"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push called 3 total" "$push_count" "3"
echo ""

# ---- test 8: failure path, empty stderr log -> falls back to stdout tail ----
echo "--- test 8: empty stderr log -> fallback to stdout tail ---"
LABEL8="com.test.cg.s8"
STDOUT_LOG8="$STUB_DIR/t8.stdout.log"
STDERR_LOG8="$STUB_DIR/t8.stderr.log"
> "$STDERR_LOG8"
# logs are written by the wrapped run itself (fresh vs run start, DGN-1582)
rc="$(run_guard_err --label "$LABEL8" --log "$STDOUT_LOG8" -- /bin/bash -c "printf 'OUT-UNIQUE\nstdout-line-2\n' > '$STDOUT_LOG8'; exit 1")"
assert_eq "exit code == 1 (preserved)" "$rc" "1"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push called 4 total" "$push_count" "4"
# The tail-SOURCE selection is the behavior under test; its DESTINATION moved
# from the owner push to stderr (a raw log tail is maintainer detail and can
# carry anything, including other jobs' paths and identifiers).
guard_err="$(cat "$GUARD_ERR")"
last_call="$(tail -1 "$PUSH_CALL_LOG")"
[[ "$guard_err" == *"OUT-UNIQUE"* ]] && ok "log tail reaches stderr (empty stderr fallback -> stdout)" || fail "stderr missing stdout tail; got: $guard_err"
[[ "$last_call" != *"OUT-UNIQUE"* ]] && ok "log tail does NOT reach the owner push" || fail "log tail leaked into the push; got: $last_call"
echo ""

# ---- test 9: failure path, non-empty stderr log -> stderr preferred ----
echo "--- test 9: non-empty stderr log -> stderr preferred over stdout ---"
LABEL9="com.test.cg.s9"
STDOUT_LOG9="$STUB_DIR/t9.stdout.log"
STDERR_LOG9="$STUB_DIR/t9.stderr.log"
rc="$(run_guard_err --label "$LABEL9" --log "$STDOUT_LOG9" -- /bin/bash -c "printf 'OUT-SHOULD-NOT-APPEAR\nstdout-line-2\n' > '$STDOUT_LOG9'; printf 'ERR-UNIQUE\nstderr-line-2\n' > '$STDERR_LOG9'; exit 1")"
assert_eq "exit code == 1 (preserved)" "$rc" "1"
push_count="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
assert_eq "push called 5 total" "$push_count" "5"
guard_err="$(cat "$GUARD_ERR")"
last_call="$(tail -1 "$PUSH_CALL_LOG")"
[[ "$guard_err" == *"ERR-UNIQUE"* ]] && ok "stderr tail preferred over stdout tail" || fail "stderr missing stderr tail; got: $guard_err"
[[ "$guard_err" != *"OUT-SHOULD-NOT-APPEAR"* ]] && ok "stdout tail suppressed when stderr is non-empty" || fail "stdout tail wrongly used; got: $guard_err"
[[ "$last_call" != *"ERR-UNIQUE"* ]] && ok "log tail does NOT reach the owner push" || fail "log tail leaked into the push; got: $last_call"
echo ""

# ---- test 10: stale log (mtime before run start) -> no tail, absence noted ----
echo "--- test 10: stale log -> tail not attached, absence stated ---"
LABEL10="com.test.cg.s10"
STDOUT_LOG10="$STUB_DIR/t10.stdout.log"
STDERR_LOG10="$STUB_DIR/t10.stderr.log"
printf 'STALE-OUT-UNIQUE\n' > "$STDOUT_LOG10"
printf 'STALE-ERR-UNIQUE\n' > "$STDERR_LOG10"
touch -t 202001010000 "$STDOUT_LOG10" "$STDERR_LOG10"
rc="$(run_guard_err --label "$LABEL10" --log "$STDOUT_LOG10" -- /bin/bash -c "exit 1")"
assert_eq "exit code == 1 (preserved)" "$rc" "1"
guard_err="$(cat "$GUARD_ERR")"
[[ "$guard_err" != *"STALE-OUT-UNIQUE"* && "$guard_err" != *"STALE-ERR-UNIQUE"* ]] && ok "stale log tail NOT attached" || fail "stale tail leaked; got: $guard_err"
[[ "$guard_err" == *"log tail omitted: no log from this run found"* ]] && ok "absence of this run's log is stated" || fail "absence notice missing; got: $guard_err"
echo ""

# ---- test 11: fresh log (written during the run) -> tail attached, no notice ----
echo "--- test 11: fresh log -> tail attached, no absence notice ---"
LABEL11="com.test.cg.s11"
STDOUT_LOG11="$STUB_DIR/t11.stdout.log"
printf 'STALE-OLD\n' > "$STDOUT_LOG11"
touch -t 202001010000 "$STDOUT_LOG11"
rc="$(run_guard_err --label "$LABEL11" --log "$STDOUT_LOG11" -- /bin/bash -c "printf 'FRESH-UNIQUE\n' > '$STDOUT_LOG11'; exit 1")"
assert_eq "exit code == 1 (preserved)" "$rc" "1"
guard_err="$(cat "$GUARD_ERR")"
[[ "$guard_err" == *"FRESH-UNIQUE"* ]] && ok "fresh log tail attached" || fail "fresh tail missing; got: $guard_err"
[[ "$guard_err" != *"log tail omitted"* ]] && ok "no absence notice for fresh log" || fail "spurious absence notice; got: $guard_err"
echo ""

# ---- test 12: DGN-1747 approved copy through the real name seats ----
echo "--- test 12: approved copy -- agent name, job name, ask button ---"
INST12="$STUB_DIR/inst12"
mkdir -p "$INST12/routines/lib" "$INST12/identity"
cp "$ROUTINES_DIR/lib/agent_name.py" "$ROUTINES_DIR/lib/scheduled_job_names.py" "$INST12/routines/lib/"
cp "$ROUTINES_DIR/health-observer.py" "$INST12/routines/health-observer.py"
printf '# AGENT\n- Name: **김도가**\n' > "$INST12/identity/hot.custom.agent.md"
sed \
  -e "s|DEDUP_DIR=\"/tmp/dogany-cron-guard\"|DEDUP_DIR=\"$DEDUP_DIR\"|" \
  -e "s|PUSH_SH=\"\$SCRIPT_DIR/push.sh\"|PUSH_SH=\"$STUB_PUSH\"|" \
  "$GUARD" > "$INST12/routines/cron-guard.sh"
chmod +x "$INST12/routines/cron-guard.sh"
rc=0; "$INST12/routines/cron-guard.sh" --label "com.telegram-skill-bot.ag.brief12" --friendly-name "generic-brief-morning" \
  -- /bin/bash -c "exit 1" 2>/dev/null || rc=$?
assert_eq "exit code == 1 (preserved)" "$rc" "1"
assert_eq "headline is the approved copy verbatim" "$(cat "$PUSH_HEAD")" "⚠️ 김도가의 모닝 브리핑 작업이 방금 실패했어요.
원인을 확인할까요?"
assert_eq "ask button names agent + job and rides the opt: handler" "$(cat "$PUSH_BTN")" "김도가 모닝 브리핑 실패 원인 확인해줘::opt:1"
head12="$(cat "$PUSH_HEAD")"
if printf '%s' "$head12" | grep -q '[A-Za-z]\|DGN-\|[0-9]'; then
  fail "headline carries an ASCII internal (label / ticket / pid / exit): $head12"
else
  ok "headline carries no ticket id / pid / exit code / English internal"
fi
rc=0; "$INST12/routines/cron-guard.sh" --label "com.telegram-skill-bot.ag.morning-brief" \
  -- /bin/bash -c "exit 1" 2>/dev/null || rc=$?
[[ "$(cat "$PUSH_HEAD")" == "⚠️ 김도가의 예약 작업이 방금 실패했어요."* ]] \
  && ok "unknown job stays generic outside folded technical detail" \
  || fail "friendly fallback wrong: $(cat "$PUSH_HEAD")"
last_call="$(tail -1 "$PUSH_CALL_LOG")"
[[ "$last_call" == *">! 기술 상세 > 잡 라벨: com.telegram-skill-bot.ag.morning-brief"* ]] \
  && ok "unknown slug appears only in folded technical detail" \
  || fail "unknown label missing from folded detail: $last_call"
# DGN-1582 (3): the dawn memory queue's own labels resolve to the approved
# job names (they read as the generic fallback before), and the maintainer
# headline falls back to the label instead of "<no friendly name>".
rc=0; "$INST12/routines/cron-guard.sh" --label "com.telegram-skill-bot.ag.dmq-consolidate" \
  -- /bin/bash -c "exit 1" 2>"$GUARD_ERR" || rc=$?
[[ "$(cat "$PUSH_HEAD")" == "⚠️ 김도가의 기억 정리 작업이 방금 실패했어요."* ]] \
  && ok "dmq-consolidate label names the memory-consolidate job" \
  || fail "dmq-consolidate unnamed: $(cat "$PUSH_HEAD")"
grep -q "ROUTINE FAILED: com.telegram-skill-bot.ag.dmq-consolidate" "$GUARD_ERR" \
  && ok "maintainer headline falls back to the label" \
  || fail "maintainer headline anonymous: $(grep 'ROUTINE FAILED' "$GUARD_ERR")"
rc=0; "$INST12/routines/cron-guard.sh" --label "com.telegram-skill-bot.ag.dmq-classify" \
  -- /bin/bash -c "exit 1" 2>/dev/null || rc=$?
[[ "$(cat "$PUSH_HEAD")" == "⚠️ 김도가의 기억 분류 작업이 방금 실패했어요."* ]] \
  && ok "dmq-classify label names the classify-inbox job" \
  || fail "dmq-classify unnamed: $(cat "$PUSH_HEAD")"
echo ""

# ---- test 13: DGN-1582 (1) behind exit (77 + marker this run) ----
# The 2026-09-19 night: consolidate finished inside its budget, committed,
# and was "behind" for the 3rd night -- it reached the owner as "the job
# failed". rc 77 with a marker written by THIS run is a behind night under
# its own dedup key; the failure alarm never fires for it. dec-236: behind
# night 1 is a log line only -- nobody is messaged.
echo "--- test 13: behind exit -> never the failure alarm; night 1 silent ---"
HOME13="$STUB_DIR/home13"
mkdir -p "$HOME13"
LABEL13="com.telegram-skill-bot.mt.dmq-consolidate"
BEHIND_JOB='printf "{\"runs\": 3, \"rows_left\": 123, \"chunks_done\": 25, \"chunks_total\": 28, \"behind_night\": 1, \"budget_raised\": false, \"at\": \"x\"}" > "$DOGANY_BACKLOG_MARKER"; exit 77'
BEHIND_JOB_RAISED='printf "{\"runs\": 4, \"rows_left\": 123, \"chunks_done\": 25, \"chunks_total\": 28, \"behind_night\": 2, \"budget_raised\": true, \"budget_next_secs\": 1500, \"at\": \"x\"}" > "$DOGANY_BACKLOG_MARKER"; exit 77'
BEHIND_JOB_CAP='printf "{\"runs\": 4, \"rows_left\": 123, \"chunks_done\": 25, \"chunks_total\": 28, \"behind_night\": 2, \"budget_raised\": false, \"budget_next_secs\": 2100, \"at\": \"x\"}" > "$DOGANY_BACKLOG_MARKER"; exit 77'
: > "$PUSH_AUD_LOG"
before="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
rc=0; HOME="$HOME13" "$INST12/routines/cron-guard.sh" --label "$LABEL13" \
  -- /bin/bash -c "$BEHIND_JOB" 2>"$GUARD_ERR" || rc=$?
assert_eq "exit code == 77 (preserved)" "$rc" "77"
[[ -f "$HOME13/.dogany/backlog-behind/$LABEL13.json" ]] \
  && ok "wrapped job received DOGANY_BACKLOG_MARKER" || fail "marker path not exported"
assert_eq "behind night 1: nothing sent to anyone" "$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')" "$before"
assert_eq "behind night 1: no push attempted" "$(wc -c < "$PUSH_AUD_LOG" | tr -d ' ')" "0"
grep -q "log only, operator told from night 2" "$GUARD_ERR" && ok "behind night 1: log line says why it is silent" \
  || fail "night 1 log line missing: $(cat "$GUARD_ERR")"
[[ ! -f "$DEDUP_DIR/${LABEL13}.${TODAY}" ]] \
  && ok "failure dedup slot left free for a real failure today" || fail "behind consumed the failure dedup"
! grep -q "ROUTINE FAILED" "$GUARD_ERR" && ok "maintainer log does not say FAILED" \
  || fail "ROUTINE FAILED printed for a behind night"
# 2-factor: 77 with a marker left over from an earlier run is a failure.
touch -t 202001010000 "$HOME13/.dogany/backlog-behind/$LABEL13.json"
rc=0; HOME="$HOME13" "$INST12/routines/cron-guard.sh" --label "$LABEL13" \
  -- /bin/bash -c "exit 77" 2>/dev/null || rc=$?
assert_eq "stale marker: exit 77 preserved" "$rc" "77"
[[ "$(cat "$PUSH_HEAD")" == *"작업이 방금 실패했어요."* ]] \
  && ok "77 with a stale marker stays on the failure path" || fail "stale-marker 77 silenced: $(cat "$PUSH_HEAD")"
# 2-factor: a bare 77 with no marker at all is a failure.
rc=0; HOME="$STUB_DIR/home13b" "$INST12/routines/cron-guard.sh" --label "com.test.cg.bare77" \
  -- /bin/bash -c "exit 77" 2>/dev/null || rc=$?
[[ "$(tail -1 "$PUSH_CALL_LOG")" == *"실패했어요"* ]] \
  && ok "bare 77 without a marker stays on the failure path" || fail "bare 77 silenced"
echo ""

# ---- test 14: dec-236 behind routing -- operator only, never the owner ----
# From the 2nd consecutive behind night the operator hears once per streak
# (memory.py raised the next night's budget); the owner is never told, with
# or without an operator. A clean night resets the streak.
echo "--- test 14: behind night 2 -> operator once (budget raised), owner never ---"
HOME14="$STUB_DIR/home14"
mkdir -p "$HOME14"
LABEL14="com.telegram-skill-bot.pub.dmq-consolidate"
STREAK14="$HOME14/.dogany/backlog-behind/$LABEL14.streak"
# Korean instance with the real i18n seam: the owner-confirmed ko copy.
INST14="$STUB_DIR/inst14"
mkdir -p "$INST14/routines/lib" "$INST14/identity" "$INST14/config/i18n"
cp "$INST12/routines/lib/"* "$INST14/routines/lib/"
cp "$ROUTINES_DIR/lib/agentlib.sh" "$INST14/routines/lib/"
cp "$ROUTINES_DIR/../config/i18n/ko.json" "$ROUTINES_DIR/../config/i18n/en.json" "$INST14/config/i18n/"
printf 'AGENT_LANG=ko\n' > "$INST14/config/agent.conf"
cp "$INST12/identity/hot.custom.agent.md" "$INST14/identity/"
cp "$INST12/routines/cron-guard.sh" "$INST14/routines/cron-guard.sh"
# A new night: the per-day behind dedup and the streak's day stamp roll over.
next_night14() {
  rm -f "$DEDUP_DIR/${LABEL14}.behind."*
  if [[ -f "$STREAK14" ]]; then
    read -r _n _d _x < "$STREAK14"
    printf '%s %s %s\n' "$_n" "19990101" "$_x" > "$STREAK14"
  fi
}
behind14() {
  local _rc=0
  HOME="$HOME14" "$INST14/routines/cron-guard.sh" --label "$LABEL14" \
    -- /bin/bash -c "$1" 2>"$GUARD_ERR" || _rc=$?
  echo "$_rc"
}
sends14() { grep -cx "$1" "$PUSH_AUD_LOG" | tr -d ' '; }
: > "$PUSH_AUD_LOG"
rc="$(behind14 "$BEHIND_JOB")"
assert_eq "night 1: exit 77 preserved" "$rc" "77"
assert_eq "night 1: nothing sent" "$(wc -c < "$PUSH_AUD_LOG" | tr -d ' ')" "0"
next_night14
before="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
rc="$(behind14 "$BEHIND_JOB_RAISED")"
assert_eq "night 2: exactly one push" "$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')" "$((before + 1))"
assert_eq "night 2: it went to the operator" "$(sends14 operator)" "1"
assert_eq "night 2: owner never" "$(sends14 owner)" "0"
assert_eq "night 2: owner-confirmed ko copy, filled" "$(cat "$PUSH_FULL")" \
  "⏳ 김도가의 기억 정리가 이틀째 다 끝나지 못했어요. 123건이 남아서 다음 밤 작업 시간을 늘렸어요."
python3 - "$ROUTINES_DIR/.." "$(cat "$PUSH_FULL")" <<'PY_EOF' && ok "push.sh identifier guard passes the operator behind notice" || fail "operator behind notice would be refused by push.sh"
import sys
sys.path.insert(0, sys.argv[1])
from bridge import machine_gate
sys.exit(1 if machine_gate.find_execution_identifiers(sys.argv[2]) else 0)
PY_EOF
next_night14
rc="$(behind14 "$BEHIND_JOB_RAISED")"
assert_eq "night 3: no repeat" "$(sends14 operator)" "1"
assert_eq "night 3: owner never" "$(sends14 owner)" "0"
grep -q "operator already told this streak" "$GUARD_ERR" && ok "night 3: log line says already told" \
  || fail "night 3 log line missing: $(cat "$GUARD_ERR")"
rc=0; HOME="$HOME14" "$INST14/routines/cron-guard.sh" --label "$LABEL14" -- "$TRUE_CMD" 2>/dev/null || rc=$?
[[ ! -f "$STREAK14" ]] && ok "recovery: a clean night clears the streak" || fail "streak survived recovery: $(cat "$STREAK14")"
next_night14
rc="$(behind14 "$BEHIND_JOB")"
assert_eq "after recovery, night 1: silent again" "$(sends14 operator)" "1"
next_night14
rc="$(behind14 "$BEHIND_JOB_CAP")"
assert_eq "after recovery, night 2: one new operator notice" "$(sends14 operator)" "2"
[[ "$(cat "$PUSH_FULL")" != *"늘렸어요"* && "$(cat "$PUSH_FULL")" == *"already at its cap"* ]] \
  && ok "budget at its cap: the 'more time' copy is not sent" || fail "cap copy: $(cat "$PUSH_FULL")"
# No operator (public install): log only, the owner still never hears.
rm -f "$STREAK14"
touch "$NO_OPERATOR"
: > "$PUSH_AUD_LOG"
next_night14
rc="$(behind14 "$BEHIND_JOB")"
next_night14
rc="$(behind14 "$BEHIND_JOB_RAISED")"
assert_eq "no operator, night 2: operator route tried, unresolved" "$(cat "$PUSH_AUD_LOG")" "operator-unresolved"
assert_eq "no operator, night 2: owner never" "$(sends14 owner)" "0"
grep -q "no operator -- log only, the owner is never told" "$GUARD_ERR" && ok "no operator: log line" \
  || fail "no-operator log line missing: $(cat "$GUARD_ERR")"
next_night14
rc="$(behind14 "$BEHIND_JOB_RAISED")"
assert_eq "no operator, night 3: not retried" "$(cat "$PUSH_AUD_LOG")" "operator-unresolved"
rm -f "$NO_OPERATOR"
echo ""

# ---- test 15: DGN-1873 retry scheduled -- operator only, never the owner ----
echo "--- test 15: retry-scheduled exit -> operator only, no button ---"
HOME15="$STUB_DIR/home15"
mkdir -p "$HOME15"
LABEL15="com.telegram-skill-bot.ag15.dmq-classify"
RETRY_JOB='printf "{\"job\":\"classify-inbox\",\"what\":\"reindex\"}" > "$DOGANY_RETRY_MARKER"; exit 78'
retry15() {
  local _rc=0
  HOME="$HOME15" "$INST12/routines/cron-guard.sh" --label "${2:-$LABEL15}" \
    -- /bin/bash -c "$1" 2>"$GUARD_ERR" || _rc=$?
  echo "$_rc"
}
: > "$PUSH_AUD_LOG"
: > "$PUSH_BTN"
before="$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')"
rc="$(retry15 "$RETRY_JOB")"
assert_eq "exit code == 78 (preserved)" "$rc" "78"
assert_eq "one push" "$(wc -l < "$PUSH_CALL_LOG" | tr -d ' ')" "$((before + 1))"
assert_eq "it went to the operator, owner never" "$(cat "$PUSH_AUD_LOG")" "operator"
assert_eq "no button on a retry-scheduled notice" "$(cat "$PUSH_BTN")" ""
[[ "$(cat "$PUSH_FULL")" == *"자동으로 다시 해요"* && "$(cat "$PUSH_FULL")" != *"?"* ]] \
  && ok "copy says it retries by itself, asks nothing" || fail "retry copy: $(cat "$PUSH_FULL")"
[[ "$(cat "$PUSH_FULL")" == *"> 단계: reindex"* ]] && ok "marker 'what' rides the folded detail" \
  || fail "what missing: $(cat "$PUSH_FULL")"
[[ ! -f "$DEDUP_DIR/${LABEL15}.$(date +%Y%m%d)" ]] \
  && ok "failure dedup slot left free for the retry's own failure" || fail "retry consumed the failure dedup"
python3 - "$ROUTINES_DIR/.." "$(cat "$PUSH_FULL")" <<'PY_EOF' && ok "push.sh identifier guard passes the retry notice" || fail "retry notice would be refused by push.sh"
import sys
sys.path.insert(0, sys.argv[1])
from bridge import machine_gate
sys.exit(1 if machine_gate.find_execution_identifiers(sys.argv[2]) else 0)
PY_EOF
rc="$(retry15 "$RETRY_JOB")"
assert_eq "same day: deduped (still one operator push)" "$(cat "$PUSH_AUD_LOG")" "operator"
touch "$NO_OPERATOR"
: > "$PUSH_AUD_LOG"
rc="$(retry15 "$RETRY_JOB" "com.telegram-skill-bot.ag.classify-inbox-0500")"
assert_eq "no operator: unresolved, nothing to the owner" "$(cat "$PUSH_AUD_LOG")" "operator-unresolved"
grep -q "no operator -- log only, the owner is never told" "$GUARD_ERR" && ok "no operator: log line" \
  || fail "no-operator log line missing: $(cat "$GUARD_ERR")"
rm -f "$NO_OPERATOR"
# 2-factor: a marker older than this run, or none at all, is a failure.
touch -t 202001010000 "$HOME15/.dogany/retry-scheduled/$LABEL15.json"
rc="$(retry15 "exit 78")"
[[ "$(cat "$PUSH_HEAD")" == *"작업이 방금 실패했어요."* ]] \
  && ok "78 with a stale marker stays on the failure path" || fail "stale-marker 78 silenced"
rc="$(retry15 "exit 78" "com.telegram-skill-bot.ag.bare78")"
[[ "$(cat "$PUSH_HEAD")" == *"작업이 방금 실패했어요."* ]] \
  && ok "bare 78 without a marker stays on the failure path" || fail "bare 78 silenced"
echo ""

# ---- test 16: DGN-1873 --unattended -- dec-252 confirmed copy ----
echo "--- test 16: --unattended failure alarm -> no button, no question, reason + retry time ---"
cp "$ROUTINES_DIR/lib/dawn_alarm.py" "$INST12/routines/lib/"
AGENTS16="$STUB_DIR/launchagents16"
mkdir -p "$AGENTS16"
# The dawn queue's own plist: dmq-* steps run on its schedule (04:00 daily).
cat > "$AGENTS16/com.telegram-skill-bot.host.dawn-memory-queue.plist" <<'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.telegram-skill-bot.host.dawn-memory-queue</string>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
</dict></plist>
PLIST_EOF
# Fixed clock at 04:35 today: the next 04:00 fire is "tomorrow".
NOW16="$(python3 -c 'import datetime as d; n=d.datetime.now(); print(int(n.replace(hour=4, minute=35, second=0, microsecond=0).timestamp()))')"
unattended16() { # <label> <cmd> [extra guard args...]
  local label="$1" cmd="$2" _rc=0
  shift 2
  DOGANY_LAUNCHAGENTS_DIR="$AGENTS16" DOGANY_NOW_EPOCH="$NOW16" \
    DOGANY_BOOT_EPOCH=1 DOGANY_WAKE_EPOCH=0 \
    "$INST12/routines/cron-guard.sh" --label "$label" "$@" --unattended \
    -- /bin/bash -c "$cmd" 2>/dev/null || _rc=$?
  echo "$_rc"
}
: > "$PUSH_BTN"
rc="$(unattended16 "com.telegram-skill-bot.ag16.dmq-consolidate" "exit 1")"
assert_eq "exit code == 1 (preserved)" "$rc" "1"
assert_eq "no ask button at dawn" "$(cat "$PUSH_BTN")" ""
assert_eq "L1 + L2 (no class -> the fallback reason line)" "$(cat "$PUSH_HEAD")" "⚠️ 새벽에 김도가의 기억 정리 작업이 실패했어요.
원인을 아직 가리지 못했어요. 기록은 남겨 뒀어요."
[[ "$(sed -n 3p "$PUSH_FULL")" == "내일 새벽 4시쯤 다시 시도할게요." ]] \
  && ok "L3: the retry time comes from the dawn queue's next fire" \
  || fail "L3: $(sed -n 3p "$PUSH_FULL")"
[[ "$(sed -n 4p "$PUSH_FULL")" == ">! 기술 상세" ]] && ok "folded detail block follows L3" \
  || fail "fold: $(cat "$PUSH_FULL")"
[[ "$(cat "$PUSH_FULL")" != *"?"* && "$(cat "$PUSH_FULL")" != *"실행 기록은 남"* ]] \
  && ok "no question, no dropped 'record kept' line" || fail "copy: $(cat "$PUSH_FULL")"
# L2 is classified from THIS run's own log bytes only: an earlier step's
# lock error already in the shared log does not colour this failure.
LOG16="$STUB_DIR/instance16.log"
printf 'sqlite3.OperationalError: database is locked\n' > "$LOG16"
rc="$(unattended16 "com.telegram-skill-bot.ag16.dmq-classify" "echo plain-failure >> '$LOG16'; exit 1" --log "$LOG16")"
[[ "$(sed -n 2p "$PUSH_FULL")" == "원인을 아직 가리지 못했어요. 기록은 남겨 뒀어요." ]] \
  && ok "an earlier step's log lines are not this run's evidence" || fail "L2: $(sed -n 2p "$PUSH_FULL")"
rc="$(unattended16 "com.telegram-skill-bot.ag16b.dmq-classify" "echo 'sqlite3.OperationalError: database is locked' >> '$LOG16'; exit 1" --log "$LOG16")"
[[ "$(sed -n 2p "$PUSH_FULL")" == "기억 저장소를 다른 작업이 쓰고 있어서 끝까지 정리하지 못했어요." ]] \
  && ok "this run's 'database is locked' -> reason line 1" || fail "L2: $(sed -n 2p "$PUSH_FULL")"
# No readable schedule (a job with no plist): no invented time.
rc="$(unattended16 "com.telegram-skill-bot.ag16.no-plist-job" "exit 1")"
[[ "$(sed -n 3p "$PUSH_FULL")" == "다음 정기 실행 때 다시 시도할게요." ]] \
  && ok "no plist -> the draft no-time retry line" || fail "L3: $(sed -n 3p "$PUSH_FULL")"
python3 - "$ROUTINES_DIR/.." "$(cat "$PUSH_FULL")" <<'PY_EOF' && ok "push.sh identifier guard passes the unattended alarm" || fail "unattended alarm would be refused by push.sh"
import sys
sys.path.insert(0, sys.argv[1])
from bridge import machine_gate
sys.exit(1 if machine_gate.find_execution_identifiers(sys.argv[2]) else 0)
PY_EOF
echo ""

# ---- summary ----
echo "==========================="
echo "Results: $PASS passed, $FAIL failed"
echo "==========================="
echo ""

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0

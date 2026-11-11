#!/bin/bash
# test-lane: fast
# test-dgn1582-dmq-exit-contract.sh -- DGN-1582 dawn-memory-queue seams
#
#   Q1. cron-guard's --log is the instance log the step actually writes
#       (not the legacy .telegram_bot/logs/*.stdout.log a dmq run never
#       touches -- that is where the 6-week-old "evidence" came from).
#   Q2. consolidate rc 77 (behind: work committed, backlog stuck) counts as
#       behind=, never fail=, and the queue exits 0.
#   Q3. any other non-zero consolidate rc still counts as fail= (exit 1).
#   Q4. r3 (dec-236): a behind instance's budget boost record is exported
#       as DOGANY_CONSOLIDATE_BUDGET_SECS with the kill timeout kept 300s
#       above it, clamped at the cap; no record -> default budget/timeout.
#   Q5. DGN-1873: steps run through cron-guard --unattended (no ask button
#       at dawn) -- but only when the instance's cron-guard parses it; an
#       older copy gets the legacy argv (an unknown flag would fail the step).
#   Q6. DGN-1873: a step exiting 78 with a fresh retry marker (automatic
#       retry scheduled) counts as retry=, not fail=; a bare 78 is a failure.
#
# Run: bash routines/tests/test-dgn1582-dmq-exit-contract.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DMQ="$(cd "$SCRIPT_DIR/.." && pwd)/dawn-memory-queue.sh"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
assert_eq() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 -- got='$2' want='$3'"; fi
}

BASE="$(mktemp -d /tmp/dgn1582-dmq.XXXXXX)"
trap 'rm -rf "$BASE"' EXIT

# new_queue NAME CONS_RC -> QH (home), one instance "a" whose consolidate
# exits CONS_RC; the stub cron-guard records the --log it was handed.
new_queue() {
  QH="$BASE/$1/home"
  INST="$BASE/$1/inst-a"
  mkdir -p "$QH/.dogany/memory-queue/instances.d" "$INST/routines" "$INST/.telegram_bot/logs"
  touch "$INST/.instance.conf" "$INST/.telegram_bot/.env"
  cat > "$INST/routines/cron-guard.sh" <<EOF
#!/bin/bash
while [[ \$# -gt 0 && "\$1" != "--" ]]; do
  [[ "\$1" == "--log" ]] && echo "\$2" >> "$BASE/$1/logargs"
  [[ "\$1" == "--unattended" ]] && echo unattended >> "$BASE/$1/flagargs"
  [[ "\$1" == "--label" ]] && export DOGANY_RETRY_MARKER="\$HOME/.dogany/retry-scheduled/\$2.json"
  shift
done
mkdir -p "\$HOME/.dogany/retry-scheduled"
shift
exec "\$@"
EOF
  printf '#!/bin/bash\necho consolidate-ran\nexit %s\n' "$2" > "$INST/routines/consolidate-0430.sh"
  printf '#!/bin/bash\necho classify-ran\nexit 0\n' > "$INST/routines/classify-inbox-check.sh"
  chmod +x "$INST/routines/"*.sh
  printf 'ROOT=%s\nENABLED=1\nPRIORITY=10\n' "$INST" > "$QH/.dogany/memory-queue/instances.d/a.conf"
}
run_dmq() {
  HOME="$QH" DMQ_CLI_UPDATE=0 DMQ_ALERT_ROOT="" /bin/bash "$DMQ" > "$BASE/dmq.out" 2>&1
  echo $?
}
qlog() { cat "$QH"/.dogany/logs/dawn-memory-queue/queue-*.log; }

echo "Q1: cron-guard tails the log this run writes"
new_queue q1 0
rc="$(run_dmq)"
assert_eq "Q1 queue exit 0" "$rc" "0"
inst_log="$(ls "$QH"/.dogany/logs/dawn-memory-queue/instance-a-*.log)"
logargs="$(sort -u "$BASE/q1/logargs")"
assert_eq "Q1 every step's --log is the instance log" "$logargs" "$inst_log"
grep -q consolidate-ran "$inst_log" && ok "Q1 the step's output really lands there" \
  || fail "Q1 instance log missing step output"
[[ -z "$(ls -A "$INST/.telegram_bot/logs")" ]] && ok "Q1 legacy routine log dir untouched" \
  || fail "Q1 something wrote the legacy log dir"

echo "Q2: consolidate behind (rc 77) is not a failure"
new_queue q2 77
rc="$(run_dmq)"
assert_eq "Q2 queue exit 0" "$rc" "0"
q="$(qlog)"
[[ "$q" == *"verdict=OK instances=1 ok=1 fail=0"* ]] && ok "Q2 verdict OK, ok=1, fail=0" \
  || fail "Q2 summary: $(grep 'dmq complete' <<<"$q")"
[[ "$q" == *"behind=1"* ]] && ok "Q2 behind=1 on the summary" || fail "Q2 behind count missing"
[[ "$q" == *"[a] step A behind (rc=77"* ]] && ok "Q2 step A logged as behind" || fail "Q2 behind log line"
[[ "$q" != *"step A exit rc=77"* ]] && ok "Q2 not logged as a failed step" || fail "Q2 logged as failure"

echo "Q3: any other non-zero consolidate rc stays a failure"
new_queue q3 1
rc="$(run_dmq)"
assert_eq "Q3 queue exit 1" "$rc" "1"
q="$(qlog)"
[[ "$q" == *"verdict=DEGRADED instances=1 ok=0 fail=1"* && "$q" == *"behind=0"* ]] \
  && ok "Q3 DEGRADED fail=1 behind=0" || fail "Q3 summary: $(grep 'dmq complete' <<<"$q")"

echo "Q4: a recorded budget boost reaches consolidate, bounded"
new_queue q4 0
printf '#!/bin/bash\necho "budget=${DOGANY_CONSOLIDATE_BUDGET_SECS:-unset}"\nexit 0\n' > "$INST/routines/consolidate-0430.sh"
rc="$(run_dmq)"
inst_log="$(ls "$QH"/.dogany/logs/dawn-memory-queue/instance-a-*.log)"
grep -q "budget=unset" "$inst_log" && ok "Q4 no record: default budget (env unset)" \
  || fail "Q4 no record: $(cat "$inst_log")"
grep -q "consolidate (timeout 1500s)" <<<"$(qlog)" && ok "Q4 no record: default timeout 1500s" \
  || fail "Q4 default timeout: $(grep 'step A' <<<"$(qlog)")"
mkdir -p "$INST/memory-engine"
printf '{"budget_secs": 1500, "from": 1200}' > "$INST/memory-engine/.consolidate-budget-boost.json"
rm -f "$QH"/.dogany/logs/dawn-memory-queue/*.log
rc="$(run_dmq)"
inst_log="$(ls "$QH"/.dogany/logs/dawn-memory-queue/instance-a-*.log)"
grep -q "budget=1500" "$inst_log" && ok "Q4 record: raised budget exported" || fail "Q4 record: $(cat "$inst_log")"
grep -q "consolidate (timeout 1800s)" <<<"$(qlog)" && ok "Q4 record: timeout widened to budget+300" \
  || fail "Q4 widened timeout: $(grep 'step A' <<<"$(qlog)")"
printf '{"budget_secs": 99999}' > "$INST/memory-engine/.consolidate-budget-boost.json"
rm -f "$QH"/.dogany/logs/dawn-memory-queue/*.log
rc="$(run_dmq)"
inst_log="$(ls "$QH"/.dogany/logs/dawn-memory-queue/instance-a-*.log)"
grep -q "budget=2100" "$inst_log" && grep -q "consolidate (timeout 2400s)" <<<"$(qlog)" \
  && ok "Q4 record past the cap: clamped to 2100s / 2400s" || fail "Q4 cap: $(cat "$inst_log")"

echo "Q5: --unattended only for an instance cron-guard that parses it"
new_queue q5 0
rc="$(run_dmq)"
[[ ! -f "$BASE/q5/flagargs" ]] && ok "Q5 legacy cron-guard: flag not passed" \
  || fail "Q5 flag passed to a cron-guard that cannot parse it"
grep -q "predates --unattended" <<<"$(qlog)" && ok "Q5 legacy cron-guard: logged" \
  || fail "Q5 no log line for the legacy cron-guard"
new_queue q5b 0
printf '# case: --unattended) UNATTENDED=1\n' >> "$INST/routines/cron-guard.sh"
rc="$(run_dmq)"
assert_eq "Q5 queue exit 0" "$rc" "0"
[[ -f "$BASE/q5b/flagargs" ]] && ok "Q5 current cron-guard: the steps get --unattended" \
  || fail "Q5 --unattended not passed"

echo "Q6: retry scheduled (78 + fresh marker) is not a failure"
new_queue q6 0
printf '#!/bin/bash\necho {} > "$DOGANY_RETRY_MARKER"\nexit 78\n' > "$INST/routines/consolidate-0430.sh"
rc="$(run_dmq)"
assert_eq "Q6 queue exit 0" "$rc" "0"
q="$(qlog)"
[[ "$q" == *"fail=0"* && "$q" == *"retry=1"* ]] && ok "Q6 fail=0 retry=1" \
  || fail "Q6 summary: $(grep 'dmq complete' <<<"$q")"
[[ "$q" == *"automatic retry scheduled (rc=78"* ]] && ok "Q6 step logged as retry scheduled" \
  || fail "Q6 retry log line"
new_queue q6b 0
printf '#!/bin/bash\nexit 78\n' > "$INST/routines/consolidate-0430.sh"
rc="$(run_dmq)"
assert_eq "Q6 bare 78 (no marker): queue exit 1" "$rc" "1"
[[ "$(qlog)" == *"fail=1"*"retry=0"* ]] && ok "Q6 bare 78 counts as fail" \
  || fail "Q6 bare 78 summary: $(grep 'dmq complete' <<<"$(qlog)")"

echo ""
echo "RESULT: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

#!/usr/bin/env bash
# test-lane: slow    # measured 74.9s (2026-09-04 full sweep, DGN-1267)
unset DOGANY_BRIDGE_SESSION  # DGN-1688: hermetic -- a run from inside a bridge session must not look like a self-cancel
# test-dgn991-dispatch-detached.sh -- dispatch-detached.sh wrapper coverage.
#
# DGN-991 rev3 phase 1: session-detached background dispatch.
#   - Gates: prompt 100KB cap, fable-exhausted -> opus demotion, active-count
#     cap 3 (separate ledger from loop budget), usage-gate fail-CLOSED.
#   - Spawn E2E (fake claude): setsid detach (PPID=1, own pgid), ledger
#     running row in loop-compatible note format (status-footer model= regex),
#     report-type end state = deferred --flag dsp-report, push issued by the
#     wrapper shell (stub), active-count returns to 0.
#   - Worktree mode E2E: dsp/<slug> branch, commits -> pending-review,
#     worktree removed / branch preserved, --notify silent skips push.
#   - Observe-mode wiring (DGN-1191): no --worktree -> worker argv carries the
#     open-Bash tool policy + --session-id, observe.sb is rendered into RUNDIR,
#     NO bypassPermissions; worktree mode carries bypass. (Sandbox semantics
#     themselves are covered by test-dispatch-observe.sh.)
#   - Cancel: pid-reuse guard refuses on identity mismatch.
#   - --worktree-repo (DGN-1012): worktree cut from a TARGET repo, commits land
#     there, artifacts (rundir/ledger/inbox) stay workspace-anchored, bare
#     --worktree stays on the workspace repo (no regression), path validation.
#
# NOT covered here (slow; proven manually in DGN-991 evidence): 60s+ timeout
# postprocess, real-claude read-only denial.
#
# Run: bash routines/tests/test-dgn991-dispatch-detached.sh
# Exit: 0 all pass, nonzero any fail.
#
# Safe/offline: fake claude stub via PATH, push/usage-gate stubs, scratch git
# repo under mktemp. No real spawn, no telegram, no live ledger.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn991-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() {
  # Reap any stray fake-claude workers spawned from this scratch dir.
  pkill -f "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Scratch workspace: wrapper + real ledger-update.sh + stubs + git repo
# ---------------------------------------------------------------------------
mkdir -p "$WS/routines/lib" "$WS/product" "$WS/.claude" "$WS/.telegram_bot" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
cp "$REPO/routines/lib/ledger-update.sh" "$WS/routines/lib/"
# DGN-1218: the bottleneck gate consults this data file (fail-closed when absent).
cp "$REPO/routines/lib/dispatch-tier-bindings.tsv" "$WS/routines/lib/"
# DGN-1770: omitted --effort resolves from this data file (CLI default when absent).
cp "$REPO/routines/lib/dispatch-effort-bindings.tsv" "$WS/routines/lib/"
cp "$REPO/routines/lib/model_display.py" "$WS/routines/lib/"
# DGN-1444: concrete runtime slugs and owner display labels share this DATA.
cp "$REPO/routines/lib/dispatch-model-display.tsv" "$WS/routines/lib/"
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo "USAGE_GATE: pass five_hour=10 seven_day=10"
exit "${GATE_STUB_RC:-0}"
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
echo "PUSH-STUB $*" >> "${PUSH_STUB_LOG:?}"
exit "${PUSH_STUB_RC:-0}"
EOF
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
echo "FAKE-CLAUDE argv: $*"
echo "FAKE-CLAUDE pwd: $(pwd)"
# DGN-1701: worker-side env dump (what the real runtime would inherit).
if [ -n "${FAKE_CLAUDE_ENV_DUMP:-}" ]; then env > "$FAKE_CLAUDE_ENV_DUMP"; fi
if [ -n "${FAKE_CLAUDE_MODEL_ID:-}" ]; then
  # DGN-1227: the model id the (real) CLI reports on its stream.
  echo "{\"type\":\"assistant\",\"message\":{\"model\":\"$FAKE_CLAUDE_MODEL_ID\"}}"
fi
if [ -n "${FAKE_CLAUDE_PLAN_FILE:-}" ]; then
  # DGN-1458: a planner delivers its plan to the path its prompt names.
  plan="$(printf '%s\n' "$@" | grep -o '/[^ ]*/plan/plan.json' | head -1)"
  [ -n "$plan" ] && cp "$FAKE_CLAUDE_PLAN_FILE" "$plan"
fi
if [ -n "${FAKE_CLAUDE_COMMIT:-}" ]; then
  echo "artifact" > fake-artifact.txt
  git add fake-artifact.txt && git commit -qm "fake work"
fi
sleep "${FAKE_CLAUDE_SLEEP:-1}"
echo "FAKE-CLAUDE-DONE"
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/claude"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| auto/seed | seed | merged | 1 | - | 2026-01-01 00:00 KST | - | seed |
EOF
git -C "$WS" init -q -b main >/dev/null
git -C "$WS" config user.email t@t
git -C "$WS" config user.name t
git -C "$WS" add -A >/dev/null
git -C "$WS" commit -qm seed >/dev/null

export PATH="$TDIR/bin:$PATH"
# DGN-1504: a claude-code worktree run needs sandbox-exec; hosts without
# the macOS binaries get stand-ins (fixtures/sandbox-standins.sh).
source "$HARNESS_DIR/fixtures/sandbox-standins.sh"
install_sandbox_standins "$TDIR/bin"
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
export PUSH_STUB_LOG="$TDIR/push-calls.log"
DD="$WS/routines/dispatch-detached.sh"

wait_finalized() {
  # $1 = rundir, $2 = max seconds
  local i
  for i in $(seq 1 "$2"); do
    # .finalized is created FIRST in finalize_run; status.json lands after
    # the close-time measurements (DGN-1504 checkout snapshot included).
    [[ -d "$1/.finalized" && -f "$1/status.json" ]] && return 0
    sleep 1
  done
  return 1
}

runid_of() { sed -n 's/.*runid=\(dsp-[0-9-]*\).*/\1/p' <<< "$1"; }

echo "== 1. gates (function level, DISPATCH_TEST sourcing) =="
(
  cd "$TDIR"
  export DISPATCH_TEST=1
  # shellcheck disable=SC1090
  source "$DD"

  python3 -c "open('$TDIR/big.md','w').write('x'*102401)"
  TASK_FILE="$TDIR/big.md"
  check_prompt_size 2>/dev/null && echo "T1A-FAIL" || echo "T1A-OK"

  printf 'small' > "$TDIR/small.md"
  TASK_FILE="$TDIR/small.md"
  check_prompt_size >/dev/null && echo "T1B-OK" || echo "T1B-FAIL"

  python3 -c "import json,datetime as d; json.dump({'expiry':(d.datetime.now(d.timezone.utc)+d.timedelta(days=1)).isoformat()}, open('$WS/.claude/.fable-exhausted','w'))"
  fable_exhausted && echo "T1C-OK" || echo "T1C-FAIL"
  python3 -c "import json,datetime as d; json.dump({'expiry':(d.datetime.now(d.timezone.utc)-d.timedelta(days=1)).isoformat()}, open('$WS/.claude/.fable-exhausted','w'))"
  fable_exhausted && echo "T1D-FAIL" || echo "T1D-OK"

  python3 -c "import json,datetime as d; json.dump({'expiry':(d.datetime.now(d.timezone.utc)+d.timedelta(days=1)).isoformat()}, open('$WS/.claude/.fable-exhausted','w'))"
  MODEL=fable
  run_gates >/dev/null 2>&1
  [[ "$MODEL" == "opus" ]] && echo "T1E-OK" || echo "T1E-FAIL"
  active_count_decr
  rm -f "$WS/.claude/.fable-exhausted"

  rm -f "$WS/.telegram_bot/dispatch/active-count"
  active_count_incr; active_count_incr; active_count_incr
  active_count_incr 2>/dev/null && echo "T1F-FAIL" || echo "T1F-OK"
  active_count_decr; active_count_decr; active_count_decr

  MODEL=haiku
  GATE_STUB_RC=2 run_gates >/dev/null 2>&1 && echo "T1G-FAIL" || echo "T1G-OK"
) > "$TDIR/t1.out" 2>&1
grep -q "T1A-OK" "$TDIR/t1.out" && ok "prompt >100KB refused" || bad "prompt >100KB refused"
grep -q "T1B-OK" "$TDIR/t1.out" && ok "small prompt accepted" || bad "small prompt accepted"
grep -q "T1C-OK" "$TDIR/t1.out" && ok "fable flag (future expiry) detected" || bad "fable flag (future expiry) detected"
grep -q "T1D-OK" "$TDIR/t1.out" && ok "fable flag (past expiry) ignored" || bad "fable flag (past expiry) ignored"
grep -q "T1E-OK" "$TDIR/t1.out" && ok "fable -> opus demotion in gates" || bad "fable -> opus demotion in gates"
grep -q "T1F-OK" "$TDIR/t1.out" && ok "active-count 4th slot refused (cap 3)" || bad "active-count 4th slot refused (cap 3)"
grep -q "T1G-OK" "$TDIR/t1.out" && ok "usage-gate fail-CLOSED refusal" || bad "usage-gate fail-CLOSED refusal"

echo "== 2. report-type E2E (no worktree): detach + ledger + push + read-only argv =="
rm -f "$PUSH_STUB_LOG"
printf 'report task' > "$TDIR/task-r.md"
export FAKE_CLAUDE_SLEEP=2
OUT="$(FAKE_CLAUDE_SLEEP=2 FAKE_CLAUDE_MODEL_ID=claude-haiku-4-5-20251001 "$DD" --read-only --task "$TDIR/task-r.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg report" --ticket DGN-991 --timeout 2 --notify push)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if [[ -n "$RUNID" && -f "$RUNDIR/pid" ]]; then
  ok "spawn returned runid + pid file"
else
  bad "spawn returned runid + pid file"
fi
PID="$(cat "$RUNDIR/pid" 2>/dev/null || echo '')"
PROCLINE="$(ps -o ppid=,pgid=,sess= -p "$PID" 2>/dev/null | tr -s ' ')"
if [[ "$PROCLINE" =~ ^\ ?1\  ]]; then
  ok "worker detached (PPID=1, own session)"
else
  bad "worker detached (PPID=1, own session) -- got '$PROCLINE'"
fi
ROW="$(grep "dsp/$RUNID" "$WS/product/auto-loop-ledger.md" | head -1)"
grep -q "running" <<< "$ROW" && ok "ledger running row written" || bad "ledger running row written"
grep -qE "model=haiku" <<< "$ROW" && ok "note carries model= (footer-parseable)" || bad "note carries model= (footer-parseable)"
grep -qE "runtime=claude-code" <<< "$ROW" && ok "note carries runtime= (footer-parseable)" || bad "note carries runtime= (footer-parseable)"
grep -q "model:  Claude Haiku 5.5 " <<< "$OUT" && ok "launch banner shows the bare name (no version before the run reports one)" || bad "launch banner shows the bare name"
# status-footer parser must extract the model from this exact note text
NOTE_MODEL="$(python3 - "$ROW" <<'PY'
import re, sys
m = re.compile(r"model=([A-Za-z0-9_.\-]+)").search(sys.argv[1])
print(m.group(1) if m else "NONE")
PY
)"
[[ "$NOTE_MODEL" == "haiku" ]] && ok "status-footer model regex parses note" || bad "status-footer model regex parses note"
if wait_finalized "$RUNDIR" 30; then
  ok "worker finalized"
else
  bad "worker finalized (timed out waiting)"
fi
sleep 1
grep "dsp/$RUNID" "$WS/product/auto-loop-ledger.md" | grep -q "deferred" \
  && ok "report-type end state = deferred" || bad "report-type end state = deferred"
grep "dsp/$RUNID" "$WS/product/auto-loop-ledger.md" | grep -q "dsp-report" \
  && ok "dsp-report flag set" || bad "dsp-report flag set"
# This wrapper now hands the stub TWO kinds of owner push: the COMPLETION push
# (governed by --notify, asserted here and below) and the batched START notice
# (owner ruling 2026-09-11; paired with the END under --notify since DGN-1904,
# so a silent run sends neither -- see START NOTICE in dispatch-detached.sh and
# routines/tests/test-dispatch-start-notice.sh).
# The start notice is filtered out by its own status label so both cases keep
# asserting exactly what they always asserted about the completion push.
completion_pushes() {
  # Both spellings filtered: the pre-block one-liner said 작업시작, the
  # designed block title says "<term> 작업 시작" (DGN-1365 follow-up).
  grep "PUSH-STUB --text" "$PUSH_STUB_LOG" 2>/dev/null | grep -vE '작업 ?시작'
}
completion_pushes | grep -q . \
  && ok "push issued by wrapper shell" || bad "push issued by wrapper shell"
# END block (identity Response workflow, "Completion signal"): title line
# `✅ <background-term> 작업 완료` (DGN-1769; neutral default here: no conf in this
# scratch WS), then `- <Model>: <작업명> <HH:MM> (<소요시간>)`. The stub run
# sleeps 2s, so the elapsed slot is the under-a-minute draft form.
completion_pushes | grep -qF '✅ 백그라운드 작업 완료' \
  && ok "completion push renders the designed END block title" \
  || bad "completion push renders the designed END block title ($(completion_pushes | head -1))"
# DGN-1585: the push wrapper now takes a trailing --audience flag, appended
# on the wire AFTER the (multi-line) --text value. The PUSH-STUB above logs
# raw `$*`, so that flag lands glued onto this item line's own last line with
# no newline before it (confirmed byte-for-byte via od -c on a preserved
# PUSH_STUB_LOG, DGN-1592 rebase) -- a stub-concatenation artifact, not a
# wording defect, hence the optional trailing group rather than loosening `$`.
grep -A1 '✅ 백그라운드 작업 완료' "$PUSH_STUB_LOG" \
  | grep -qE '^- Claude Haiku 4.5: rg report [0-9]{2}:[0-9]{2} \(1분 미만\)( --audience owner)?$' \
  && ok "END item line = '- <Model> <ver>: <작업명> <HH:MM> (<소요시간>)', version from the observed id" \
  || bad "END item line with observed-id model + time + elapsed"
# DGN-1191: observe mode = kernel sandbox (seatbelt) + open Bash; the old
# Read,Grep,Glob allowlist is gone on purpose (it could not see option-level
# writes such as git log --output). DGN-1897: WebSearch/WebFetch join Bash.
# Sandbox-mode details: test-dispatch-observe.sh.
grep -q -- "--permission-mode default --allowedTools Bash,WebSearch,WebFetch --disallowedTools Task,Agent,Write,Edit,NotebookEdit --session-id" "$RUNDIR/out.log" \
  && ok "observe-mode tool policy in argv (no --worktree)" || bad "observe-mode tool policy in argv (no --worktree)"
[[ -f "$RUNDIR/observe.sb" ]] && grep -q "observe: kernel read-only (seatbelt $RUNDIR/observe.sb)" <<< "$OUT" \
  && ok "observe profile rendered into RUNDIR and announced on the spawn line" || bad "observe profile rendered into RUNDIR and announced on the spawn line"
grep -q "bypassPermissions" "$RUNDIR/out.log" \
  && bad "no bypassPermissions in read-only mode" || ok "no bypassPermissions in read-only mode"
COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo 'missing')"
[[ "$COUNT" == "0" ]] && ok "active-count back to 0" || bad "active-count back to 0 (got $COUNT)"

echo "== 2b. --effort (DGN-1666): pass-through, refusal, unset -> no flag =="
printf 'effort task' > "$TDIR/task-e.md"
ARGS_E=(--read-only --task "$TDIR/task-e.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --timeout 2 --notify silent)
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" "${ARGS_E[@]}" --label "rg effort" --effort medium)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
wait_finalized "$RUNDIR" 30 || bad "effort run finalize"
grep -q -- "--model haiku --effort medium " "$RUNDIR/out.log" \
  && ok "--effort medium passed to the claude worker" || bad "--effort medium passed to the claude worker"
grep -q "effort: medium" <<< "$OUT" && ok "spawn banner shows the effort" || bad "spawn banner shows the effort"
grep -q '^EFFORT=medium$' "$RUNDIR/run.env" && ok "run.env records EFFORT" || bad "run.env records EFFORT"
python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["effort"] == "medium" else 1)' "$RUNDIR/status.json" \
  && ok "status.json records effort" || bad "status.json records effort"
"$DD" "${ARGS_E[@]}" --label "rg effort bad" --effort turbo >/dev/null 2>"$TDIR/effort-bad.err"
[[ $? -ne 0 ]] && grep -q "invalid --effort 'turbo'" "$TDIR/effort-bad.err" \
  && ok "invalid --effort refused before spawn" || bad "invalid --effort refused before spawn"
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" "${ARGS_E[@]}" --label "rg effort unset")"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
wait_finalized "$RUNDIR" 30 || bad "no-effort run finalize"
grep -q -- "--effort" "$RUNDIR/out.log" \
  && bad "unset --effort must not pass a flag" || ok "unset --effort -> no --effort flag in argv"
python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["effort"] == "" else 1)' "$RUNDIR/status.json" \
  && ok "unset effort recorded as empty" || bad "unset effort recorded as empty"

echo "== 3. worktree mode E2E: commits -> pending-review, silent skips push =="
rm -f "$PUSH_STUB_LOG"
printf 'build task' > "$TDIR/task-b.md"
OUT="$(FAKE_CLAUDE_SLEEP=1 FAKE_CLAUDE_COMMIT=1 "$DD" --task "$TDIR/task-b.md" --model sonnet --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "rg build" --worktree rgbuild --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "worktree run finalize"; fi
grep "dsp/rgbuild" "$WS/product/auto-loop-ledger.md" | grep -q "pending-review" \
  && ok "commits -> pending-review" || bad "commits -> pending-review"
git -C "$WS" show-ref --verify --quiet refs/heads/dsp/rgbuild \
  && ok "dsp/rgbuild branch preserved" || bad "dsp/rgbuild branch preserved"
[[ -z "$(ls -A "$TDIR/wt" 2>/dev/null)" ]] \
  && ok "worktree removed after run" || bad "worktree removed after run"
grep -q "bypassPermissions" "$RUNDIR/out.log" \
  && ok "worktree mode uses bypassPermissions" || bad "worktree mode uses bypassPermissions"
# DGN-1504: kernel write boundary rendered, announced, recorded; the work
# location reaches the worker's system prompt.
[[ -f "$RUNDIR/write.sb" ]] && grep -q "OWN WORKTREE LAST" "$RUNDIR/write.sb" \
  && ok "DGN-1504 write profile rendered into RUNDIR" || bad "DGN-1504 write profile rendered into RUNDIR"
grep -q "isolation: kernel write boundary = own worktree only" <<< "$OUT" \
  && ok "DGN-1504 spawn line announces the write boundary" || bad "DGN-1504 spawn line announces the write boundary"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["write_profile"].endswith("/write.sb") and d["checkout_drift"]=="" else 1)' "$RUNDIR/status.json" \
  && ok "DGN-1504 status.json write_profile set, no checkout drift" || bad "DGN-1504 status.json write_profile set, no checkout drift"
grep -q "your ONLY writable checkout is $TDIR/wt/" "$RUNDIR/out.log" \
  && ok "DGN-1504 work location injected into the worker prompt" || bad "DGN-1504 work location injected into the worker prompt"
[[ -f "$RUNDIR/checkouts-start.tsv" && -f "$RUNDIR/checkouts-end.tsv" ]] \
  && ok "DGN-1504 shared-checkout snapshots at spawn and finalize" || bad "DGN-1504 shared-checkout snapshots at spawn and finalize"
if [[ "$SB_STUB" == 1 ]]; then
  mv "$TDIR/bin/sandbox-exec" "$TDIR/bin/sandbox-exec.off"; hash -r
  OUT_NOSB="$("$DD" --task "$TDIR/task-b.md" --model sonnet --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "rg nosb" --worktree rgnosb --timeout 2 --notify silent 2>&1)"; RC_NOSB=$?
  mv "$TDIR/bin/sandbox-exec.off" "$TDIR/bin/sandbox-exec"; hash -r
  [[ $RC_NOSB -eq 2 ]] && grep -q "write mode needs sandbox-exec" <<< "$OUT_NOSB" \
    && ok "DGN-1504 worktree spawn REFUSED without sandbox-exec (rc 2)" || bad "DGN-1504 worktree spawn REFUSED without sandbox-exec (rc=$RC_NOSB)"
  git -C "$WS" show-ref --verify --quiet refs/heads/dsp/rgnosb \
    && bad "DGN-1504 refused spawn provisioned nothing" || ok "DGN-1504 refused spawn provisioned nothing"
fi
completion_pushes | grep -q . \
  && bad "--notify silent skips push ($(completion_pushes | head -1))" \
  || ok "--notify silent skips push"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["notify"]=="skipped-silent" else 1)' "$RUNDIR/status.json" \
  && ok "status.json notify=skipped-silent" || bad "status.json notify=skipped-silent"

echo "== 4. cancel pid-reuse guard + self-cancel quietness =="
printf 'victim task' > "$TDIR/task-v.md"
rm -f "$PUSH_STUB_LOG"
OUT="$(FAKE_CLAUDE_SLEEP=120 "$DD" --read-only --task "$TDIR/task-v.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg victim" --timeout 5 --notify push)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
sleep 2
PID="$(cat "$RUNDIR/pid")"
cp "$RUNDIR/proc.txt" "$TDIR/proc-backup.txt"
echo "bogus lstart bogus command" > "$RUNDIR/proc.txt"
"$DD" --cancel "$RUNID" >/dev/null 2>&1 && bad "tampered identity refused" || ok "tampered identity refused"
kill -0 "$PID" 2>/dev/null && ok "victim untouched after refusal" || bad "victim untouched after refusal"
cp "$TDIR/proc-backup.txt" "$RUNDIR/proc.txt"
"$DD" --cancel "$RUNID" >/dev/null 2>&1
sleep 2
kill -0 "$PID" 2>/dev/null && bad "cancel kills verified target" || ok "cancel kills verified target"
grep "dsp/$RUNID" "$WS/product/auto-loop-ledger.md" | grep -q "failed" \
  && ok "canceled run -> ledger failed" || bad "canceled run -> ledger failed"
completion_pushes | grep -q '⚠️ rg victim 작업이 멈췄습니다.' \
  && ok "external cancel keeps the owner stop notice" || bad "external cancel keeps the owner stop notice"

# A cancel from the owner bridge session is intentional: it leaves an
# auditable marker and the session recovery record, but no END owner push.
rm -f "$PUSH_STUB_LOG"
OUT="$(FAKE_CLAUDE_SLEEP=120 "$DD" --read-only --task "$TDIR/task-v.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg self victim" --timeout 5 --notify push)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
sleep 2
DOGANY_BRIDGE_SESSION=1 "$DD" --cancel "$RUNID" >/dev/null 2>&1
wait_finalized "$RUNDIR" 10 || bad "self-canceled worker finalized"
sleep 1
python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["cancel_by"] == "self" else 1)' "$RUNDIR/status.json" \
  && ok "self-cancel persists cancel_by=self in status.json" || bad "self-cancel status records cancel_by=self"
grep -q '^CANCEL_BY=self$' "$RUNDIR/run.env" \
  && ok "self-cancel persists cancel_by=self in run.env" || bad "self-cancel run.env records cancel_by=self"
grep -q -- '^- cancel_by: self$' "$WS/.telegram_bot/session-inbox/dispatch-$RUNID.md" \
  && ok "self-cancel return record carries quiet-recovery marker" || bad "self-cancel return record marker"
completion_pushes | grep -q '작업 완료' \
  && bad "self-cancel emits a done (작업 완료) push" || ok "self-cancel emits no done (작업 완료) push"
grep -q -- '작업이 멈췄습니다' "$PUSH_STUB_LOG" 2>/dev/null \
  && ok "DGN-1904: self-cancel of a push run sends the canceled END notice (start/end pair)" \
  || bad "DGN-1904: self-cancel of a push run sends the canceled END notice"

echo "== 5. fresh-instance ledger bootstrap (no product/ dir, template mint path) =="
WS2="$TDIR/ws2"
mkdir -p "$WS2/routines/lib"
cp "$WRAPPER" "$WS2/routines/"
cp "$REPO/routines/lib/ledger-update.sh" "$WS2/routines/lib/"
chmod +x "$WS2/routines/dispatch-detached.sh" "$WS2/routines/lib/ledger-update.sh"
(
  export DISPATCH_TEST=1
  # shellcheck disable=SC1091
  source "$WS2/routines/dispatch-detached.sh"
  ensure_ledger && [[ -f "$WS2/product/auto-loop-ledger.md" ]] \
    && echo "T5A-OK" || echo "T5A-FAIL"
  "$WS2/routines/lib/ledger-update.sh" "dsp/boot" running "bootstrap probe" --item probe \
    >/dev/null 2>&1 || echo "T5B-WRITE-FAIL"
  grep -q "| dsp/boot | probe | running |" "$WS2/product/auto-loop-ledger.md" \
    && echo "T5B-OK" || echo "T5B-FAIL"
  ensure_ledger  # second call must be a no-op, not a clobber
  grep -q "| dsp/boot |" "$WS2/product/auto-loop-ledger.md" \
    && echo "T5C-OK" || echo "T5C-FAIL"
) > "$TDIR/t5.out" 2>&1
grep -q "T5A-OK" "$TDIR/t5.out" && ok "absent ledger seeded on first use" || bad "absent ledger seeded on first use"
grep -q "T5B-OK" "$TDIR/t5.out" && ok "ledger-update row lands in seeded ledger (not dropped)" || bad "ledger-update row lands in seeded ledger (not dropped)"
grep -q "T5C-OK" "$TDIR/t5.out" && ok "re-bootstrap is a no-op (no clobber)" || bad "re-bootstrap is a no-op (no clobber)"

echo "== 6. --repo (DGN-1012): read-only cwd redirect, artifacts stay home =="
printf 'repo task' > "$TDIR/task-repo.md"
# T6A: mutually exclusive with --worktree -> usage error exit 1 with reason.
if "$DD" --task "$TDIR/task-repo.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg repo" \
     --worktree rgslug --repo "$TDIR" >/dev/null 2>"$TDIR/t6a.err"; then
  bad "--repo + --worktree refused"
else
  grep -q "mutually exclusive" "$TDIR/t6a.err" \
    && ok "--repo + --worktree refused with reason" || bad "--repo + --worktree refused with reason"
fi
# T6B: nonexistent / non-directory path -> exit 1.
"$DD" --read-only --task "$TDIR/task-repo.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg repo" \
  --repo "$TDIR/no-such-dir" >/dev/null 2>&1 \
  && bad "--repo nonexistent path refused" || ok "--repo nonexistent path refused"
# T6C: non-git dir allowed with a warning line.
mkdir -p "$TDIR/plain-dir"
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --read-only --task "$TDIR/task-repo.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" \
  --label "rg repo plain" --timeout 2 --notify silent --repo "$TDIR/plain-dir" --local-why "fixture repo has no cloud origin")"
grep -q "not a git repository" <<< "$OUT" \
  && ok "--repo non-git dir warns" || bad "--repo non-git dir warns"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
wait_finalized "$RUNDIR" 30 || bad "non-git --repo run finalize"
# T6D: E2E on a second git repo -- worker cwd lands there, artifacts stay in WS.
REPO2="$TDIR/repo2"
mkdir -p "$REPO2"
git -C "$REPO2" init -q -b main
git -C "$REPO2" config user.email t@t
git -C "$REPO2" config user.name t
echo r2 > "$REPO2/marker.txt"
git -C "$REPO2" add -A && git -C "$REPO2" commit -qm r2seed
REPO2_ABS="$(cd "$REPO2" && pwd)"
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --read-only --task "$TDIR/task-repo.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" \
  --label "rg repo e2e" --timeout 2 --notify silent --repo "$REPO2" --local-why "fixture repo has no cloud origin")"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "--repo run finalize"; fi
grep -q "FAKE-CLAUDE pwd: $REPO2_ABS" "$RUNDIR/out.log" \
  && ok "--repo worker cwd = target repo" || bad "--repo worker cwd = target repo"
grep -q -- "--permission-mode default --allowedTools Bash,WebSearch,WebFetch --disallowedTools Task,Agent,Write,Edit,NotebookEdit" "$RUNDIR/out.log" \
  && ok "--repo keeps observe-mode tool policy" || bad "--repo keeps observe-mode tool policy"
grep -q "subpath \"$REPO2_ABS\"" "$RUNDIR/observe.sb" \
  && ok "--repo target joins the sandbox deny list (WORKDIR)" || bad "--repo target joins the sandbox deny list (WORKDIR)"
grep -q "bypassPermissions" "$RUNDIR/out.log" \
  && bad "--repo has no bypassPermissions" || ok "--repo has no bypassPermissions"
[[ -f "$RUNDIR/status.json" ]] \
  && ok "--repo status.json lands in WS rundir" || bad "--repo status.json lands in WS rundir"
[[ -f "$WS/.telegram_bot/session-inbox/dispatch-$RUNID.md" ]] \
  && ok "--repo inbox drop lands in WS session-inbox" || bad "--repo inbox drop lands in WS session-inbox"
[[ -z "$(find "$REPO2_ABS" -name 'status.json' -o -name 'out.log' -o -name 'dispatch-*' 2>/dev/null)" ]] \
  && ok "--repo target repo untouched by artifacts" || bad "--repo target repo untouched by artifacts"
COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo 'missing')"
[[ "$COUNT" == "0" ]] && ok "--repo active-count back to 0" || bad "--repo active-count back to 0 (got $COUNT)"

echo "== 7. --worktree-repo (DGN-1012): write-capable worktree in a TARGET repo =="
printf 'target build task' > "$TDIR/task-tgt.md"
# Target repo: separate git repo with its own main.
REPO3="$TDIR/repo3"
mkdir -p "$REPO3"
git -C "$REPO3" init -q -b main
git -C "$REPO3" config user.email t@t
git -C "$REPO3" config user.name t
echo r3 > "$REPO3/marker.txt"
git -C "$REPO3" add -A && git -C "$REPO3" commit -qm r3seed
REPO3_ABS="$(cd "$REPO3" && pwd)"

# T7A: --worktree-repo without --worktree -> usage error (exit 1) with reason.
if "$DD" --read-only --task "$TDIR/task-tgt.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg tgt" \
     --worktree-repo "$REPO3" >/dev/null 2>"$TDIR/t7a.err"; then
  bad "--worktree-repo without --worktree refused"
else
  grep -q "requires --worktree" "$TDIR/t7a.err" \
    && ok "--worktree-repo without --worktree refused with reason" \
    || bad "--worktree-repo without --worktree refused with reason"
fi
# T7B: nonexistent path -> exit 1.
"$DD" --task "$TDIR/task-tgt.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg tgt" \
  --worktree tgtbad --worktree-repo "$TDIR/no-such-repo" >/dev/null 2>&1 \
  && bad "--worktree-repo nonexistent path refused" || ok "--worktree-repo nonexistent path refused"
# T7C: non-git directory -> HARD refusal (unlike --repo, which only warns).
mkdir -p "$TDIR/plain-dir2"
if "$DD" --task "$TDIR/task-tgt.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg tgt" \
     --worktree tgtbad --worktree-repo "$TDIR/plain-dir2" >/dev/null 2>"$TDIR/t7c.err"; then
  bad "--worktree-repo non-git dir refused"
else
  grep -q "not a git repository" "$TDIR/t7c.err" \
    && ok "--worktree-repo non-git dir refused (hard)" || bad "--worktree-repo non-git dir refused (hard)"
fi
git -C "$WS" show-ref --verify --quiet refs/heads/dsp/tgtbad \
  && bad "refused run left no dsp/tgtbad branch" || ok "refused run left no dsp/tgtbad branch"

# T7D: E2E -- worktree cut from REPO3, worker commits THERE.
rm -f "$PUSH_STUB_LOG"
OUT="$(FAKE_CLAUDE_SLEEP=2 FAKE_CLAUDE_COMMIT=1 "$DD" --task "$TDIR/task-tgt.md" \
  --model sonnet --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "rg tgt build" --worktree tgtbuild \
  --worktree-repo "$REPO3" --local-why "fixture repo has no cloud origin" --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
# DGN-1742: the stated local reason rides the ledger running row (read now,
# before finalize rewrites the row).
grep "dsp/tgtbuild" "$WS/product/auto-loop-ledger.md" | grep "running" \
  | grep -qF "local_why=fixture repo has no cloud origin)" \
  && ok "DGN-1742 ledger running row carries local_why=<reason>" \
  || bad "DGN-1742 ledger running row carries local_why=<reason>"
grep -qF "LOCAL_WHY=fixture\\ repo\\ has\\ no\\ cloud\\ origin" "$RUNDIR/run.env" \
  && ok "DGN-1742 run.env records LOCAL_WHY verbatim" || bad "DGN-1742 run.env records LOCAL_WHY verbatim"
grep -q "repo $REPO3_ABS" <<< "$OUT" \
  && ok "spawn line names the target repo" || bad "spawn line names the target repo"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "target-repo worktree run finalize"; fi
git -C "$REPO3" show-ref --verify --quiet refs/heads/dsp/tgtbuild \
  && ok "dsp/tgtbuild branch created in TARGET repo" || bad "dsp/tgtbuild branch created in TARGET repo"
git -C "$WS" show-ref --verify --quiet refs/heads/dsp/tgtbuild \
  && bad "workspace repo has no dsp/tgtbuild branch" || ok "workspace repo has no dsp/tgtbuild branch"
[[ "$(git -C "$REPO3" rev-list --count main..dsp/tgtbuild 2>/dev/null || echo 0)" == "1" ]] \
  && ok "worker commit landed on target-repo branch" || bad "worker commit landed on target-repo branch"
git -C "$REPO3" show dsp/tgtbuild:fake-artifact.txt >/dev/null 2>&1 \
  && ok "committed artifact readable in target repo" || bad "committed artifact readable in target repo"
grep -q "bypassPermissions" "$RUNDIR/out.log" \
  && ok "target-repo worktree keeps write perms (bypassPermissions)" \
  || bad "target-repo worktree keeps write perms (bypassPermissions)"
# Artifacts stay WORKSPACE-anchored -- the return path depends on it.
[[ -f "$RUNDIR/status.json" ]] \
  && ok "--worktree-repo status.json lands in WS rundir" || bad "--worktree-repo status.json lands in WS rundir"
[[ -f "$WS/.telegram_bot/session-inbox/dispatch-$RUNID.md" ]] \
  && ok "--worktree-repo inbox drop lands in WS session-inbox" \
  || bad "--worktree-repo inbox drop lands in WS session-inbox"
[[ ! -e "$REPO3_ABS/.telegram_bot" ]] \
  && ok "target repo carries no dispatch artifacts" || bad "target repo carries no dispatch artifacts"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["local_why"] == "fixture repo has no cloud origin"' "$RUNDIR/status.json" 2>/dev/null \
  && ok "DGN-1742 status.json records local_why verbatim" || bad "DGN-1742 status.json records local_why verbatim"
grep "dsp/tgtbuild" "$WS/product/auto-loop-ledger.md" | grep -q "pending-review" \
  && ok "--worktree-repo commits -> pending-review in WS ledger" \
  || bad "--worktree-repo commits -> pending-review in WS ledger"
# Cleanup: worktree removed from the TARGET repo registration too.
if git -C "$REPO3" worktree list --porcelain | grep -q "$TDIR/wt/$RUNID"; then
  bad "target-repo worktree deregistered after run"
else
  ok "target-repo worktree deregistered after run"
fi
[[ ! -d "$TDIR/wt/$RUNID-tgtbuild" ]] \
  && ok "target-repo worktree dir removed after run" || bad "target-repo worktree dir removed after run"
COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo 'missing')"
[[ "$COUNT" == "0" ]] && ok "--worktree-repo active-count back to 0" || bad "--worktree-repo active-count back to 0 (got $COUNT)"

# T7E: no-regression -- bare --worktree still cuts from the WORKSPACE repo.
OUT="$(FAKE_CLAUDE_SLEEP=1 FAKE_CLAUDE_COMMIT=1 "$DD" --task "$TDIR/task-tgt.md" \
  --model sonnet --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "rg ws build" --worktree wsbuild --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "bare --worktree run finalize"; fi
git -C "$WS" show-ref --verify --quiet refs/heads/dsp/wsbuild \
  && ok "bare --worktree still lands in WORKSPACE repo" || bad "bare --worktree still lands in WORKSPACE repo"
git -C "$REPO3" show-ref --verify --quiet refs/heads/dsp/wsbuild \
  && bad "bare --worktree does not touch target repo" || ok "bare --worktree does not touch target repo"

echo "== 8. base branch (DGN-1184 (A)): canonical marker + dev -> cut from dev; else main =="
printf 'base task' > "$TDIR/task-base.md"
# REPO4: canonical-shaped target (.dogany-canonical marker) with dev AHEAD of main.
REPO4="$TDIR/repo4"
mkdir -p "$REPO4"
git -C "$REPO4" init -q -b main
git -C "$REPO4" config user.email t@t
git -C "$REPO4" config user.name t
echo r4 > "$REPO4/marker.txt"
printf 'canonical marker (test)\n' > "$REPO4/.dogany-canonical"
git -C "$REPO4" add -A && git -C "$REPO4" commit -qm r4seed
git -C "$REPO4" switch -qc dev
echo ahead > "$REPO4/dev-only.txt"
git -C "$REPO4" add -A && git -C "$REPO4" commit -qm "dev ahead of main"
git -C "$REPO4" switch -q main
# T8A: marker + dev -> base dev (printed), branch descends from dev, count vs dev.
OUT="$(FAKE_CLAUDE_SLEEP=1 FAKE_CLAUDE_COMMIT=1 "$DD" --task "$TDIR/task-base.md" \
  --model sonnet --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "rg devbase" --worktree devbase \
  --worktree-repo "$REPO4" --local-why "fixture repo has no cloud origin" --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
grep -q "base dev" <<< "$OUT" \
  && ok "T8A spawn line prints base dev (canonical marker + dev)" || bad "T8A spawn line prints base dev"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "T8A devbase run finalize"; fi
git -C "$REPO4" merge-base --is-ancestor dev dsp/devbase \
  && ok "T8A dsp/devbase cut from dev (dev is an ancestor)" || bad "T8A dsp/devbase not cut from dev"
[[ "$(git -C "$REPO4" rev-list --count dev..dsp/devbase 2>/dev/null || echo 0)" == "1" ]] \
  && ok "T8A commit count measured against dev (1 worker commit)" || bad "T8A commit count vs dev"
grep "dsp/devbase" "$WS/product/auto-loop-ledger.md" | grep -q "pending-review" \
  && ok "T8A commits -> pending-review (base dev)" || bad "T8A commits -> pending-review (base dev)"
grep -q "^WT_BASE=dev$" "$RUNDIR/run.env" \
  && ok "T8A run.env persists WT_BASE=dev" || bad "T8A run.env persists WT_BASE=dev"
# T8B: dev exists but NO marker (instance-shaped repo) -> base stays main.
REPO5="$TDIR/repo5"
mkdir -p "$REPO5"
git -C "$REPO5" init -q -b main
git -C "$REPO5" config user.email t@t
git -C "$REPO5" config user.name t
echo r5 > "$REPO5/marker.txt"
git -C "$REPO5" add -A && git -C "$REPO5" commit -qm r5seed
git -C "$REPO5" switch -qc dev
echo ahead > "$REPO5/dev-only.txt"
git -C "$REPO5" add -A && git -C "$REPO5" commit -qm "unrelated dev branch"
git -C "$REPO5" switch -q main
OUT="$(FAKE_CLAUDE_SLEEP=1 FAKE_CLAUDE_COMMIT=1 "$DD" --task "$TDIR/task-base.md" \
  --model sonnet --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "rg mainbase" --worktree mainbase \
  --worktree-repo "$REPO5" --local-why "fixture repo has no cloud origin" --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
grep -q "base main" <<< "$OUT" \
  && ok "T8B spawn line prints base main (no marker)" || bad "T8B spawn line prints base main"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "T8B mainbase run finalize"; fi
git -C "$REPO5" merge-base --is-ancestor dev dsp/mainbase \
  && bad "T8B without marker dev must be ignored" || ok "T8B without marker dev is ignored (cut from main)"
# T8C: explicit --base overrides resolution.
OUT="$(FAKE_CLAUDE_SLEEP=1 FAKE_CLAUDE_COMMIT=1 "$DD" --task "$TDIR/task-base.md" \
  --model sonnet --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "rg explbase" --worktree explbase \
  --worktree-repo "$REPO5" --local-why "fixture repo has no cloud origin" --base dev --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
grep -q "base dev" <<< "$OUT" \
  && ok "T8C --base dev printed" || bad "T8C --base dev printed"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "T8C explbase run finalize"; fi
git -C "$REPO5" merge-base --is-ancestor dev dsp/explbase \
  && ok "T8C --base dev honoured (cut from dev)" || bad "T8C --base dev honoured"
# T8D: --base without --worktree -> usage error with reason.
if "$DD" --read-only --task "$TDIR/task-base.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg base" \
     --base dev >/dev/null 2>"$TDIR/t8d.err"; then
  bad "T8D --base without --worktree refused"
else
  grep -q "requires --worktree" "$TDIR/t8d.err" \
    && ok "T8D --base without --worktree refused with reason" \
    || bad "T8D --base without --worktree refused with reason"
fi
# T8E: --base naming a missing branch -> exit 3, no stray branch.
"$DD" --task "$TDIR/task-base.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg base" \
  --worktree nobase --worktree-repo "$REPO5" --local-why "fixture repo has no cloud origin" --base no-such-branch >/dev/null 2>"$TDIR/t8e.err"
[[ $? -eq 3 ]] && grep -q "has no 'no-such-branch' branch" "$TDIR/t8e.err" \
  && ok "T8E missing --base branch refused (exit 3)" || bad "T8E missing --base branch refused (exit 3)"
git -C "$REPO5" show-ref --verify --quiet refs/heads/dsp/nobase \
  && bad "T8E refused run left no dsp/nobase branch" || ok "T8E refused run left no dsp/nobase branch"
COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo 'missing')"
[[ "$COUNT" == "0" ]] && ok "T8 active-count back to 0" || bad "T8 active-count back to 0 (got $COUNT)"

echo "== 9. DGN-1158 fable-exhausted marker writer (4 flag-state cases) =="
(
  cd "$TDIR"
  export DISPATCH_TEST=1
  # shellcheck disable=SC1090
  source "$DD"
  MODEL=fable

  # is_fable_credits_death: detects the real-world death signature (out.log
  # is rendered from stream+stderr, worklog/DGN-1158 evidence, verbatim);
  # ignores non-fable dispatches and non-matching output.
  printf 'Fable 5 requires usage credits. Switch to another model, ...' > "$TDIR/death.log"
  is_fable_credits_death "$TDIR/death.log" && echo "T9SIG-OK" || echo "T9SIG-FAIL"
  MODEL=opus
  is_fable_credits_death "$TDIR/death.log" && echo "T9SIGM-FAIL" || echo "T9SIGM-OK"
  MODEL=fable

  # DGN-1350: the harness REWORDS this message. The 2026-09-08 wording did not
  # match the old narrow pattern, so two detached fable dispatches died with no
  # marker written. Pin every wording seen so far, verbatim.
  printf "You've reached your Fable 5 limit. Switch to another model, or manage usage credits at claude.ai/settings/usage?from=cc_cli_limit_message, to continue." > "$TDIR/death2.log"
  is_fable_credits_death "$TDIR/death2.log" && echo "T9SIG2-OK" || echo "T9SIG2-FAIL"

  # DGN-1350 lockstep: the sibling python writer (fable-death-catch.py RE_DEATH)
  # and this bash pattern must accept the SAME corpus. Divergence is the defect.
  for _c in "$TDIR/death.log" "$TDIR/death2.log"; do
    if python3 - "$REPO/routines/fable-death-catch.py" "$_c" <<'PY'
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"RE_DEATH = re\.compile\(\s*(.*?)\s*,\s*re\.IGNORECASE", src, re.S)
pat = "".join(re.findall(r'r"([^"]*)"', m.group(1)))
sys.exit(0 if re.search(pat, open(sys.argv[2], encoding="utf-8").read(), re.I) else 1)
PY
    then :; else echo "T9LOCK-FAIL ($_c)"; fi
  done
  echo "T9LOCK-DONE"
  printf 'Task complete: refactored 3 files, all tests pass.' > "$TDIR/ok.log"
  is_fable_credits_death "$TDIR/ok.log" && echo "T9SIGN-FAIL" || echo "T9SIGN-OK"

  FLAG="$WS/.claude/.fable-exhausted"

  # Case 1: 없음 (no flag file present) -> writer creates a valid flag.
  rm -f "$FLAG"
  write_fable_exhausted_marker && fable_exhausted && echo "T9A-OK" || echo "T9A-FAIL"

  # Case 2: 유효 (an already-valid future-expiry flag) -> writer overwrites,
  # stays valid.
  python3 -c "import json,datetime as d; json.dump({'expiry':(d.datetime.now(d.timezone.utc)+d.timedelta(hours=1)).isoformat()}, open('$FLAG','w'))"
  write_fable_exhausted_marker && fable_exhausted && echo "T9B-OK" || echo "T9B-FAIL"

  # Case 3: 만료 (an expired flag) -> writer overwrites, becomes valid again.
  python3 -c "import json,datetime as d; json.dump({'expiry':(d.datetime.now(d.timezone.utc)-d.timedelta(days=1)).isoformat()}, open('$FLAG','w'))"
  write_fable_exhausted_marker && fable_exhausted && echo "T9C-OK" || echo "T9C-FAIL"

  # Case 4: 손상 (corrupted / unparseable JSON) -> writer overwrites, becomes
  # valid (never trips on a pre-existing bad file).
  printf 'not json{{{' > "$FLAG"
  write_fable_exhausted_marker && fable_exhausted && echo "T9D-OK" || echo "T9D-FAIL"

  rm -f "$FLAG"
) > "$TDIR/t9.out" 2>&1
grep -q "T9SIG-OK" "$TDIR/t9.out" && ok "T9 death signature detected (model=fable)" || bad "T9 death signature detected (model=fable)"
grep -q "T9SIGM-OK" "$TDIR/t9.out" && ok "T9 death signature ignored for non-fable model" || bad "T9 death signature ignored for non-fable model"
grep -q "T9SIGN-OK" "$TDIR/t9.out" && ok "T9 non-matching out.log ignored" || bad "T9 non-matching out.log ignored"
grep -q "T9SIG2-OK" "$TDIR/t9.out" && ok "T9 DGN-1350: 2026-09-08 reworded cap message detected" || bad "T9 DGN-1350: 2026-09-08 reworded cap message detected"
grep -q "T9LOCK-FAIL" "$TDIR/t9.out" && bad "T9 DGN-1350: bash/python death patterns in lockstep" || ok "T9 DGN-1350: bash/python death patterns in lockstep"
grep -q "T9A-OK" "$TDIR/t9.out" && ok "T9A writer: no flag -> valid flag written" || bad "T9A writer: no flag -> valid flag written"
grep -q "T9B-OK" "$TDIR/t9.out" && ok "T9B writer: valid flag -> overwritten, stays valid" || bad "T9B writer: valid flag -> overwritten, stays valid"
grep -q "T9C-OK" "$TDIR/t9.out" && ok "T9C writer: expired flag -> overwritten, becomes valid" || bad "T9C writer: expired flag -> overwritten, becomes valid"
grep -q "T9D-OK" "$TDIR/t9.out" && ok "T9D writer: corrupted flag -> overwritten, becomes valid" || bad "T9D writer: corrupted flag -> overwritten, becomes valid"

echo "== 11. DGN-1701: bridge secrets stripped from the worker env =="
# Spawn path: parent env carries TELEGRAM_BOT_TOKEN (+ siblings); the fake
# claude dumps ITS env; nothing bridge-secret may be there, PATH/HOME must be.
printf 'env strip task' > "$TDIR/task-env.md"
ENVDUMP="$TDIR/claude-env.dump"; rm -f "$ENVDUMP"
OUT="$(TELEGRAM_BOT_TOKEN=TEST-ONLY-x TELEGRAM_CHAT_ID=TEST-ONLY-chat ALLOWED_USER_IDS=12345 FAKE_CLAUDE_ENV_DUMP="$ENVDUMP" FAKE_CLAUDE_SLEEP=1 "$DD" --read-only --task "$TDIR/task-env.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" --label "rg envstrip" --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
wait_finalized "$RUNDIR" 30 || bad "DGN-1701 worker finalized (timed out waiting)"
[[ -s "$ENVDUMP" ]] && ok "DGN-1701 worker-side env dump captured" || bad "DGN-1701 worker-side env dump captured"
[[ -s "$ENVDUMP" ]] && ! grep -q 'TEST-ONLY' "$ENVDUMP" && ok "DGN-1701 no TEST-ONLY secret value in worker env" || bad "DGN-1701 no TEST-ONLY secret value in worker env"
[[ -s "$ENVDUMP" ]] && ! grep -qE '^TELEGRAM_BOT_TOKEN=' "$ENVDUMP" && ok "DGN-1701 TELEGRAM_BOT_TOKEN absent from worker env" || bad "DGN-1701 TELEGRAM_BOT_TOKEN absent from worker env"
[[ -s "$ENVDUMP" ]] && ! grep -qE '^(TELEGRAM_CHAT_ID|ALLOWED_USER_IDS)=' "$ENVDUMP" && ok "DGN-1701 sibling bridge vars absent" || bad "DGN-1701 sibling bridge vars absent"
grep -qE '^PATH=' "$ENVDUMP" && grep -qE '^HOME=' "$ENVDUMP" && ok "DGN-1701 PATH/HOME preserved (not env -i)" || bad "DGN-1701 PATH/HOME preserved (not env -i)"

# Function level: the same strip array feeds the codex spawn site.
(
  export DISPATCH_TEST=1
  # shellcheck disable=SC1090
  source "$DD"
  export TELEGRAM_BOT_TOKEN=TEST-ONLY-x TELEGRAM_FUTURE_SECRET=TEST-ONLY-y OPENAI_API_KEY=TEST-ONLY-keep-me
  worker_env_strip_args
  env -u CLAUDECODE "${WORKER_ENV_UNSET[@]}" env > "$TDIR/codex-env.dump"
  grep -q '^TELEGRAM_' "$TDIR/codex-env.dump" && echo "T11A-FAIL" || echo "T11A-OK"
  grep -q '^OPENAI_API_KEY=TEST-ONLY-keep-me' "$TDIR/codex-env.dump" && echo "T11B-OK" || echo "T11B-FAIL"
) > "$TDIR/t11.out" 2>&1
grep -q "T11A-OK" "$TDIR/t11.out" && ok "DGN-1701 strip array removes TELEGRAM_* (incl. unlisted future names)" || bad "DGN-1701 strip array removes TELEGRAM_*"
grep -q "T11B-OK" "$TDIR/t11.out" && ok "DGN-1701 runtime auth vars (OPENAI_API_KEY) untouched" || bad "DGN-1701 runtime auth vars untouched"

echo
echo "== DGN-1458 plan lane E2E: planner -> plan verdict -> dry run -> apply =="
cp "$REPO/routines/plan-apply.py" "$WS/routines/"
LIVE="$TDIR/live-instance"; mkdir -p "$LIVE"; echo "A=1" > "$LIVE/agent.conf"
OLD_SHA="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$LIVE/agent.conf")"
printf '{"version":1,"summary":"t","ops":[{"op":"write","path":"%s","content":"A=2\\n","expect_sha256":"%s"}]}' \
  "$LIVE/agent.conf" "$OLD_SHA" > "$TDIR/plan-fixture.json"
printf 'plan task' > "$TDIR/task-p.md"
"$DD" --task "$TDIR/task-p.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" \
  --why "dgn991 test stub run" --label "rg plan wt" --worktree rgplan --live-target "$LIVE" >/dev/null 2>"$TDIR/plan-wt.err"
[[ $? -eq 1 ]] && grep -q -- "--live-target requires --read-only" "$TDIR/plan-wt.err" \
  && ok "DGN-1458 --live-target with --worktree refused" || bad "DGN-1458 --live-target with --worktree refused"
OUT="$(FAKE_CLAUDE_SLEEP=1 FAKE_CLAUDE_PLAN_FILE="$TDIR/plan-fixture.json" "$DD" --read-only --task "$TDIR/task-p.md" --model haiku \
  --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn991 test stub run" \
  --label "rg plan" --live-target "$LIVE" --timeout 2 --notify silent 2>&1)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
LIVE_REAL="$(cd "$LIVE" && pwd -P)"  # macOS: /tmp -> /private/tmp; the spawn line prints the resolved path
grep -q -e "plan lane: live targets $LIVE" -e "plan lane: live targets $LIVE_REAL" <<< "$OUT" && ok "DGN-1458 spawn line announces the plan lane" || bad "DGN-1458 spawn line announces the plan lane"
wait_finalized "$RUNDIR" 30 || bad "DGN-1458 planner finalize"
grep -q "PLAN LANE" "$RUNDIR/observe.sb" 2>/dev/null && ok "DGN-1458 planner profile carries the plan-dir rule" || bad "DGN-1458 planner profile carries the plan-dir rule"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["plan_state"]=="ready: 1 op(s)" and d["live_targets"]==[sys.argv[2]] else 1)' "$RUNDIR/status.json" "$(cd "$LIVE" && pwd -P)" \
  && ok "DGN-1458 status.json plan_state=ready + live_targets" || bad "DGN-1458 status.json plan_state=ready + live_targets"
grep -q -- "- plan: ready: 1 op(s)" "$WS/.telegram_bot/session-inbox/dispatch-$RUNID.md" \
  && grep -q -- "--apply-plan $RUNID --apply" "$WS/.telegram_bot/session-inbox/dispatch-$RUNID.md" \
  && ok "DGN-1458 dispatch-return names the plan and the apply command" || bad "DGN-1458 dispatch-return names the plan and the apply command"
[[ "$(cat "$LIVE/agent.conf")" == "A=1" ]] && ok "DGN-1458 the run itself wrote nothing live" || bad "DGN-1458 the run itself wrote nothing live"
"$DD" --apply-plan "$RUNID" > "$TDIR/plan-dry.out" 2>&1
grep -q "DRY RUN" "$TDIR/plan-dry.out" && [[ "$(cat "$LIVE/agent.conf")" == "A=1" ]] \
  && ok "DGN-1458 --apply-plan without --apply is a dry run" || bad "DGN-1458 --apply-plan dry run ($(cat "$TDIR/plan-dry.out"))"
"$DD" --apply-plan "$RUNID" --apply > "$TDIR/plan-apply.out" 2>&1
[[ $? -eq 0 && "$(cat "$LIVE/agent.conf")" == "A=2" && -f "$RUNDIR/plan-receipt.json" ]] \
  && ok "DGN-1458 --apply-plan --apply applies once, receipt written" || bad "DGN-1458 --apply-plan --apply ($(cat "$TDIR/plan-apply.out"))"

echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0

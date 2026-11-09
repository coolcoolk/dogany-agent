#!/usr/bin/env bash
# test-lane: slow    # spawns detached script jobs and waits for finalize / drain
# test-component: dispatch-detached script-job kind (--script)
# test-tier: 2
# test-prereq: subprocess, tmp-files, fixture-repo (scratch git workspace, stub push, scratch DOGANY_SHARED_HOME)
# test-new-component: --script is a new dispatch kind with its own worker loop, lane and return record; the model-worker suites (test-dgn991-dispatch-detached.sh, test-dispatch-*.sh) drive a stub claude and cannot own a no-model job
unset DOGANY_BRIDGE_SESSION  # hermetic: a run from inside a bridge session must not look like a self-cancel
# test-dispatch-script-job.sh -- dispatch-detached.sh --script (DGN-2115).
#
# A script job is a non-model dispatch kind: `--script -- <argv...>` runs a
# command detached in its own session with the SAME ledger row (workbench),
# START/END notices, run dir, host queue and --cancel as a model worker.
# Covers:
#   - launch: detached (PPID=1), ledger running row carries the owner-facing
#     name, out.log = job output, exit 0 -> completed (ledger deferred), END
#     push names the job, usage lines folded into status.json, counter back to 0
#   - argv passes through verbatim (incl. words that look like dispatch flags)
#   - refusals: no argv, model flags, missing command, relative/missing workdir
#   - cancel: --cancel stops the job's whole process group, ledger failed
#   - restart survival: the launching shell AND its process group are
#     SIGKILLed right after launch (a bridge restart); the job still completes
#   - queue: a full script lane queues the next job (exit 0, ticket) and the
#     drain starts it when the first finishes; --no-queue starts at once over
#     the cap and still writes an admission line (job log + host decision log)
#   - timeout -> timed-out; nonzero exit -> error (ledger failed)
#   - session return (inbox drop) reports the exit code and the usage file and
#     never the model-run "left no product -- suspect a no-op" directive
#   - unit gate: a real `--script -- sleep 30` run shows queue admission, the
#     ledger row, the END notice and an rc-bearing return
#
# Run via the machine test budget:
#   bash git-hooks/run-tests.sh --gate bash routines/tests/test-dispatch-script-job.sh
# Safe/offline: stub push, scratch workspace under mktemp, host state in a
# scratch DOGANY_SHARED_HOME with a pinned machine reading. No model, no network.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
check() { # <desc> <command...>
  local d="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi
}

TDIR="$(mktemp -d "${TMPDIR:-/tmp}/dispatch-script-test.XXXXXX")"
source "$HARNESS_DIR/fixtures/dispatch-env.sh"
isolate_dispatch_env "$TDIR"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  chflags -R nouappend,nouchg "$TDIR" 2>/dev/null || true
  chmod -R u+w "$TDIR" 2>/dev/null || true
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines" "$WS/product" "$WS/.claude" "$WS/.telegram_bot" "$TDIR/bin" "$TDIR/home"
cp "$WRAPPER" "$WS/routines/"
bash "$HARNESS_DIR/fixtures/install-dispatch-policy.sh" "$WS"
cp -R "$REPO/routines/lib" "$WS/routines/lib"
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/"
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
echo "PUSH-STUB $*" >> "${PUSH_STUB_LOG:?}"
exit 0
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh"
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

export HOME="$TDIR/home"
export DOGANY_SHARED_HOME="$TDIR/shared"
export DOGANY_DISPATCH_MACHINE="0.1 16 64000 0"
export DOGANY_SCRIPT_JOB_MAX_ACTIVE=2
export PUSH_STUB_LOG="$TDIR/push-calls.log"
DD="$WS/routines/dispatch-detached.sh"
DISPATCH="$WS/.telegram_bot/dispatch"
LEDGER="$WS/product/auto-loop-ledger.md"
INBOX="$WS/.telegram_bot/session-inbox"

wait_finalized() { # <rundir> <secs>
  local i
  for i in $(seq 1 "$2"); do
    [[ -d "$1/.finalized" && -f "$1/status.json" ]] && return 0
    sleep 1
  done
  return 1
}
runid_of() { sed -n 's/.*runid=\(dsp-[0-9-]*\).*/\1/p' <<< "$1" | head -1; }
status_field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2], ""))' "$1/status.json" "$2"; }
alive() { kill -0 "$1" 2>/dev/null; }

echo "== 1. launch: detached, ledger row, output, usage, END notice =="
cat > "$TDIR/job1.sh" <<'EOF'
#!/bin/bash
echo "JOB1 pwd=$(pwd) args=$#"
for a in "$@"; do echo "ARG[$a]"; done
python3 "$DOGANY_SCRIPT_RUNDIR/../../../routines/lib/script_usage.py" append --model claude-opus-5-5 --input-tokens 1000 --output-tokens 200 --cost-usd 0.25
python3 "$DOGANY_SCRIPT_RUNDIR/../../../routines/lib/script_usage.py" append --model claude-opus-5-5 --input-tokens 500 --cost-usd 0.05
echo 'not json' >> "$DOGANY_SCRIPT_USAGE_FILE"
echo "env job=$DOGANY_SCRIPT_JOB runid=$DOGANY_SCRIPT_RUNID"
echo "scratch=$TMPDIR"
echo probe > "$TMPDIR/probe"
sleep 2
EOF
chmod +x "$TDIR/job1.sh"
mkdir -p "$TDIR/wd"
OUT="$("$DD" --script --label bench-one --display-name "bench one" --ticket DGN-2115 --workdir "$TDIR/wd" -- "$TDIR/job1.sh" --task x --model y -- "two words")"
RUNID="$(runid_of "$OUT")"
RUNDIR="$DISPATCH/$RUNID"
[[ -n "$RUNID" && -f "$RUNDIR/pid" ]] && ok "spawn returned runid + pid file" || bad "spawn returned runid + pid file"
PID="$(cat "$RUNDIR/pid" 2>/dev/null)"
PROCLINE="$(ps -o ppid=,sess= -p "$PID" 2>/dev/null | tr -s ' ')"
[[ "$PROCLINE" =~ ^\ ?1\  ]] && ok "worker detached (PPID=1, own session)" || bad "worker detached -- got '$PROCLINE'"
ROW="$(grep "dsp/$RUNID" "$LEDGER" | head -1)"
grep -q running <<< "$ROW" && ok "ledger running row written" || bad "ledger running row written"
grep -q "\[DGN-2115\] bench-one" <<< "$ROW" && ok "workbench item column = ticket + name" || bad "workbench item column -- $ROW"
grep -q "runtime=script" <<< "$ROW" && ok "ledger note marks runtime=script" || bad "ledger note marks runtime=script"
grep -q "cancel: .*--cancel $RUNID" <<< "$OUT" && ok "banner prints the cancel command" || bad "banner prints the cancel command"
if wait_finalized "$RUNDIR" 30; then ok "job finalized"; else bad "job finalized (timeout)"; fi
sleep 1
[[ "$(status_field "$RUNDIR" result)" == completed ]] && ok "exit 0 -> completed" || bad "exit 0 -> completed ($(status_field "$RUNDIR" result))"
[[ ! -e "$RUNDIR/tmp" ]] && ok "completed job TMPDIR removed" || bad "completed job TMPDIR left behind"
grep -q "scratch=$RUNDIR/tmp" "$RUNDIR/out.log" && ok "script inherits owned TMPDIR" || bad "script TMPDIR"
[[ "$(status_field "$RUNDIR" kind)" == script ]] && ok "status.json kind=script" || bad "status.json kind=script"
grep -q "JOB1 pwd=.*wd args=6" "$RUNDIR/out.log" && ok "out.log has the job output; ran in --workdir with 6 argv" || bad "out.log/workdir/argv: $(head -3 "$RUNDIR/out.log")"
grep -qF 'ARG[--task]' "$RUNDIR/out.log" && grep -qF 'ARG[--model]' "$RUNDIR/out.log" && grep -qF 'ARG[two words]' "$RUNDIR/out.log" \
  && ok "dispatch-looking words after -- reach the job verbatim" || bad "argv passthrough"
grep -q "env job=1 runid=$RUNID" "$RUNDIR/out.log" && ok "job sees DOGANY_SCRIPT_* env" || bad "job env"
USAGE="$(python3 -c 'import json,sys; u=json.load(open(sys.argv[1]))["script_usage"]; m=u["models"]["claude-opus-5-5"]; print(m["input_tokens"], m["output_tokens"], m["cost_usd"], u["bad_lines"], u["total"]["input_tokens"])' "$RUNDIR/status.json")"
[[ "$USAGE" == "1500 200 0.3 1 1500" ]] && ok "usage lines folded into status.json (1 bad line counted)" || bad "usage fold -- got '$USAGE'"
grep "dsp/$RUNID" "$LEDGER" | grep -q "deferred" && ok "completed -> ledger deferred (report-type end)" || bad "ledger end state"
grep -q "admission=granted" "$DISPATCH/script-admissions.log" && ok "admission line written" || bad "admission line written"
grep -A1 "작업 완료" "$PUSH_STUB_LOG" | grep -q "bench-one" \
  && ok "END notice pushed from the shell, names the job" || bad "END notice -- $(cat "$PUSH_STUB_LOG" 2>/dev/null | head -3)"
for _ in $(seq 1 30); do grep -qE '작업 ?시작' "$PUSH_STUB_LOG" 2>/dev/null && break; sleep 1; done
grep -A2 -E '작업 ?시작' "$PUSH_STUB_LOG" | grep -q "bench-one" && ok "START notice pushed for the script job" || bad "START notice -- $(head -5 "$PUSH_STUB_LOG" 2>/dev/null)"
[[ "$(cat "$DISPATCH/active-count.script" 2>/dev/null | tr -d ' \n')" == 0 ]] && ok "script counter back to 0" || bad "script counter ($(cat "$DISPATCH/active-count.script" 2>/dev/null))"
DROP="$INBOX/dispatch-$RUNID.md"
[[ -f "$DROP" ]] && ok "session return dropped" || bad "session return dropped"
grep -q "^- exit_code: 0$" "$DROP" && ok "return reports the exit code" || bad "return exit code -- $(cat "$DROP" 2>/dev/null)"
grep -qF -- "- usage: $RUNDIR/usage.jsonl -- 2 line(s), models=claude-opus-5-5" "$DROP" && ok "return names the usage file and its summary" || bad "return usage -- $(grep usage "$DROP")"
grep -q "script job finished with exit code 0" "$DROP" && ok "return directive is the script-job one" || bad "return directive -- $(tail -2 "$DROP")"
! grep -qiE "no product|suspect a no-op|^- product:" "$DROP" && ok "no 'no product / suspect a no-op' directive" || bad "model-run product directive leaked into a script return"
LIST_OUT="$("$DD" --list 2>&1)"; grep -q "$RUNID" <<< "$LIST_OUT" && ok "--list shows the run" || bad "--list shows the run"

echo "== 2. refusals =="
check "no argv refused" bash -c '! "$0" --script --label a --display-name a 2>/dev/null' "$DD"
check "--model refused" bash -c '! "$0" --script --label a --display-name a --model opus -- true 2>/dev/null' "$DD"
check "--task refused" bash -c '! "$0" --script --label a --display-name a --task /dev/null -- true 2>/dev/null' "$DD"
check "missing label refused" bash -c '! "$0" --script --display-name a -- true 2>/dev/null' "$DD"
check "unknown command refused" bash -c '! "$0" --script --label a --display-name a -- no-such-cmd-2115 2>/dev/null' "$DD"
check "relative workdir refused" bash -c '! "$0" --script --label a --display-name a --workdir rel -- true 2>/dev/null' "$DD"
check "missing workdir refused" bash -c '! "$0" --script --label a --display-name a --workdir /no/such/dir/2115 -- true 2>/dev/null' "$DD"
[[ "$(ls "$DISPATCH" | grep -c '^dsp-')" == 1 ]] && ok "refusals created no run dir" || bad "refusals created a run dir"

echo "== 3. cancel =="
cat > "$TDIR/long.sh" <<'EOF'
#!/bin/bash
echo "LONG started"
sleep 120 &
wait
EOF
chmod +x "$TDIR/long.sh"
OUT="$("$DD" --script --label long-one --display-name "long one" --notify silent -- "$TDIR/long.sh")"
RID="$(runid_of "$OUT")"; RD="$DISPATCH/$RID"
sleep 2
CPID="$(cat "$RD/claude.pid" 2>/dev/null)"
[[ -n "$CPID" ]] && alive "$CPID" && ok "job running in its own process group (claude.pid)" || bad "job process group recorded"
CC_OUT="$("$DD" --cancel "$RID" 2>&1)"
wait_finalized "$RD" 15 && ok "cancel finalized the run" || bad "cancel finalized ($CC_OUT)"
[[ "$(status_field "$RD" result)" == canceled ]] && ok "result canceled" || bad "result canceled ($(status_field "$RD" result))"
sleep 1
! alive "$CPID" && ok "job process group is gone" || bad "job still alive after cancel"
! pgrep -f "sleep 120" >/dev/null 2>&1 || { [[ -z "$(ps -o pid= -g "$CPID" 2>/dev/null)" ]] && ok "no process left in the job's group" || bad "group not empty"; }
grep "dsp/$RID" "$LEDGER" | grep -q failed && ok "ledger failed (canceled)" || bad "ledger failed (canceled)"
! grep -q "long one\|long-one" "$PUSH_STUB_LOG" 2>/dev/null && ok "--notify silent: no push for the silent job" || bad "silent job pushed"

echo "== 4. restart survival: launcher shell + its process group SIGKILLed =="
cat > "$TDIR/survive.sh" <<'EOF'
#!/bin/bash
sleep 4
echo "SURVIVED"
EOF
chmod +x "$TDIR/survive.sh"
# The launcher runs as its own job-control group; once it printed the spawn
# line we SIGKILL the whole group (what a bridge restart does to its children).
set -m
( "$DD" --script --label survive-one --display-name "survive one" -- "$TDIR/survive.sh" > "$TDIR/survive.launch" 2>&1; sleep 30 ) &
LPID=$!
set +m
for _ in $(seq 1 20); do grep -q "spawned: runid=" "$TDIR/survive.launch" 2>/dev/null && break; sleep 0.5; done
RID="$(runid_of "$(cat "$TDIR/survive.launch")")"; RD="$DISPATCH/$RID"
kill -9 -- "-$LPID" 2>/dev/null
wait "$LPID" 2>/dev/null
WPID="$(cat "$RD/pid" 2>/dev/null)"
alive "$WPID" && ok "worker alive after the launcher group was killed" || bad "worker died with the launcher"
wait_finalized "$RD" 30 && ok "job ran to its end" || bad "job did not finalize"
grep -q SURVIVED "$RD/out.log" && [[ "$(status_field "$RD" result)" == completed ]] && ok "completed after the restart" || bad "result after restart ($(status_field "$RD" result))"

echo "== 5. queue vs --no-queue (script lane cap 1) =="
export DOGANY_SCRIPT_JOB_MAX_ACTIVE=1
cat > "$TDIR/hold.sh" <<'EOF'
#!/bin/bash
sleep "${1:-6}"
echo "HOLD-DONE $1"
EOF
chmod +x "$TDIR/hold.sh"
OUT_A="$("$DD" --script --label hold-a --display-name "hold a" --notify silent -- "$TDIR/hold.sh" 6)"
RA="$(runid_of "$OUT_A")"
OUT_B="$("$DD" --script --label hold-b --display-name "hold b" --notify silent -- "$TDIR/hold.sh" 1)"; RC_B=$?
[[ $RC_B -eq 0 ]] && grep -q "queued #1 (ticket T" <<< "$OUT_B" && ok "default: full lane queues the job (exit 0, ticket)" || bad "queue -- rc=$RC_B $OUT_B"
[[ -z "$(runid_of "$OUT_B")" ]] && ok "queued job has no run dir yet" || bad "queued job already ran"
# DGN-2138 x DGN-2115: a script job runs locally (no worktree), so the host
# charges it as a build run, not an llm one.
grep -l '"hold-b"' "$DOGANY_SHARED_HOME"/dispatcher/queue/*.json 2>/dev/null | xargs -r grep -q '"kind": "build"' \
  && ok "queued script job is charged as kind build" || bad "script job run kind ($(cat "$DOGANY_SHARED_HOME"/dispatcher/queue/*.json 2>/dev/null | tr -d '\n' | head -c 300))"
OUT_C="$("$DD" --script --no-queue --label hold-c --display-name "hold c" --notify silent -- "$TDIR/hold.sh" 1)"
RCC="$(runid_of "$OUT_C")"
[[ -n "$RCC" ]] && ok "--no-queue starts at once over the cap" || bad "--no-queue -- $OUT_C"
grep -q "no-queue" <<< "$OUT_C" && ok "banner states the no-queue admission" || bad "banner admission"
grep "runid=$RCC" "$DISPATCH/script-admissions.log" | grep -q "admission=no-queue" && ok "no-queue admission line in the job log" || bad "no-queue admission line"
grep -q '"via": "no-queue"' "$DOGANY_SHARED_HOME/dispatcher/decisions.jsonl" 2>/dev/null && ok "no-queue admission line in the host decision log" || bad "host decision log"
wait_finalized "$DISPATCH/$RCC" 20 && ok "no-queue job finished" || bad "no-queue job finished"
wait_finalized "$DISPATCH/$RA" 30 && ok "first job finished" || bad "first job finished"
# The release drains the queue: job B starts on its own as a fresh run.
RB=""
for _ in $(seq 1 40); do
  RB="$(grep -l 'hold-b' "$DISPATCH"/dsp-*/run.env 2>/dev/null | head -1 | xargs -r dirname)"
  [[ -n "$RB" && -d "$RB/.finalized" && -f "$RB/status.json" ]] && break
  sleep 1
done
[[ -n "$RB" && -f "$RB/status.json" ]] && [[ "$(status_field "$RB" result)" == completed ]] && ok "queued job was drained and ran to completion" || bad "queued job drain (rb='$RB')"
sleep 1
[[ "$(cat "$DISPATCH/active-count.script" 2>/dev/null | tr -d ' \n')" == 0 ]] && ok "counter back to 0 after the burst" || bad "counter after burst ($(cat "$DISPATCH/active-count.script"))"
export DOGANY_SCRIPT_JOB_MAX_ACTIVE=2

echo "== 6. timeout and failing exit =="
OUT="$("$DD" --script --label fail-one --display-name "fail one" --notify silent -- bash -c 'echo boom; echo failure > "$TMPDIR/probe"; exit 7')"
RID="$(runid_of "$OUT")"; RD="$DISPATCH/$RID"
wait_finalized "$RD" 20 && [[ "$(status_field "$RD" result)" == error && "$(status_field "$RD" exit_code)" == 7 ]] \
  && ok "nonzero exit -> error with the exit code" || bad "error path ($(status_field "$RD" result))"
[[ -f "$RD/reclaim/scratch/probe" && -f "$RD/reclaim.json" ]] && ok "failed job scratch retired" || bad "failed job reclaim missing"
grep "dsp/$RID" "$LEDGER" | grep -q "failed" && ok "error -> ledger failed" || bad "error -> ledger failed"
grep -q "^- exit_code: 7$" "$INBOX/dispatch-$RID.md" && grep -q "script job ended with error (exit code 7)" "$INBOX/dispatch-$RID.md" \
  && ! grep -qi "no product" "$INBOX/dispatch-$RID.md" && ok "error return carries rc 7, no 'no product'" || bad "error return -- $(tail -3 "$INBOX/dispatch-$RID.md")"
# a 1-minute timeout is the floor; shorten via the run.env deadline instead of waiting 60s
OUT="$("$DD" --script --label slow-one --display-name "slow one" --notify silent --timeout 1 -- sleep 100)"
RID="$(runid_of "$OUT")"; RD="$DISPATCH/$RID"
wait_finalized "$RD" 90 && [[ "$(status_field "$RD" result)" == timed-out ]] && ok "deadline -> timed-out" || bad "timeout ($(status_field "$RD" result))"

echo "== 7. unit gate: --script -- sleep 30 =="
: > "$PUSH_STUB_LOG"
OUT="$("$DD" --script --label sleep-gate --display-name "sleep gate" --ticket DGN-2115 -- sleep 30)"
RID="$(runid_of "$OUT")"; RD="$DISPATCH/$RID"
[[ -n "$RID" ]] && grep "runid=$RID" "$DISPATCH/script-admissions.log" | grep -q "admission=granted" \
  && ok "gate: queue admission line (granted)" || bad "gate admission -- $OUT"
grep "dsp/$RID" "$LEDGER" | grep -q running && ok "gate: ledger running row while it sleeps" || bad "gate ledger row"
wait_finalized "$RD" 60 && [[ "$(status_field "$RD" result)" == completed ]] && ok "gate: completed after 30 s" || bad "gate result ($(status_field "$RD" result))"
sleep 1
grep -A1 "작업 완료" "$PUSH_STUB_LOG" | grep -q "sleep-gate" && ok "gate: END notice pushed" || bad "gate END -- $(cat "$PUSH_STUB_LOG")"
grep -q "^- exit_code: 0$" "$INBOX/dispatch-$RID.md" && ! grep -qi "no product" "$INBOX/dispatch-$RID.md" \
  && ok "gate: return carries rc, no 'no product' directive" || bad "gate return -- $(cat "$INBOX/dispatch-$RID.md")"
echo "--- gate return record ---"; cat "$INBOX/dispatch-$RID.md"; echo "---"

echo "== 8. model dispatch path untouched =="
check "model worker still refuses without --task" bash -c '! "$0" --label a --bottleneck accuracy 2>/dev/null' "$DD"

echo
echo "dispatch script job: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

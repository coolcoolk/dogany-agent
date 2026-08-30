#!/usr/bin/env bash
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
#   - Read-only wiring: no --worktree -> argv carries --allowedTools
#     whitelist and NO bypassPermissions; worktree mode carries bypass.
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
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
export PUSH_STUB_LOG="$TDIR/push-calls.log"
DD="$WS/routines/dispatch-detached.sh"

wait_finalized() {
  # $1 = rundir, $2 = max seconds
  local i
  for i in $(seq 1 "$2"); do
    [[ -d "$1/.finalized" ]] && return 0
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
OUT="$(FAKE_CLAUDE_SLEEP=2 "$DD" --task "$TDIR/task-r.md" --model haiku --label "rg report" --ticket DGN-991 --timeout 2 --notify push)"
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
grep -q "PUSH-STUB --text" "$PUSH_STUB_LOG" 2>/dev/null \
  && ok "push issued by wrapper shell" || bad "push issued by wrapper shell"
grep -q -- "--allowedTools Read,Grep,Glob" "$RUNDIR/out.log" \
  && ok "read-only whitelist in argv (no --worktree)" || bad "read-only whitelist in argv (no --worktree)"
grep -q "bypassPermissions" "$RUNDIR/out.log" \
  && bad "no bypassPermissions in read-only mode" || ok "no bypassPermissions in read-only mode"
COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo 'missing')"
[[ "$COUNT" == "0" ]] && ok "active-count back to 0" || bad "active-count back to 0 (got $COUNT)"

echo "== 3. worktree mode E2E: commits -> pending-review, silent skips push =="
rm -f "$PUSH_STUB_LOG"
printf 'build task' > "$TDIR/task-b.md"
OUT="$(FAKE_CLAUDE_SLEEP=1 FAKE_CLAUDE_COMMIT=1 "$DD" --task "$TDIR/task-b.md" --model sonnet --label "rg build" --worktree rgbuild --timeout 2 --notify silent)"
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
[[ ! -s "$PUSH_STUB_LOG" ]] && ok "--notify silent skips push" || bad "--notify silent skips push"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d["notify"]=="skipped-silent" else 1)' "$RUNDIR/status.json" \
  && ok "status.json notify=skipped-silent" || bad "status.json notify=skipped-silent"

echo "== 4. cancel pid-reuse guard =="
printf 'victim task' > "$TDIR/task-v.md"
OUT="$(FAKE_CLAUDE_SLEEP=120 "$DD" --task "$TDIR/task-v.md" --model haiku --label "rg victim" --timeout 5 --notify silent)"
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
if "$DD" --task "$TDIR/task-repo.md" --model haiku --label "rg repo" \
     --worktree rgslug --repo "$TDIR" >/dev/null 2>"$TDIR/t6a.err"; then
  bad "--repo + --worktree refused"
else
  grep -q "mutually exclusive" "$TDIR/t6a.err" \
    && ok "--repo + --worktree refused with reason" || bad "--repo + --worktree refused with reason"
fi
# T6B: nonexistent / non-directory path -> exit 1.
"$DD" --task "$TDIR/task-repo.md" --model haiku --label "rg repo" \
  --repo "$TDIR/no-such-dir" >/dev/null 2>&1 \
  && bad "--repo nonexistent path refused" || ok "--repo nonexistent path refused"
# T6C: non-git dir allowed with a warning line.
mkdir -p "$TDIR/plain-dir"
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task-repo.md" --model haiku \
  --label "rg repo plain" --timeout 2 --notify silent --repo "$TDIR/plain-dir")"
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
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task-repo.md" --model haiku \
  --label "rg repo e2e" --timeout 2 --notify silent --repo "$REPO2")"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "--repo run finalize"; fi
grep -q "FAKE-CLAUDE pwd: $REPO2_ABS" "$RUNDIR/out.log" \
  && ok "--repo worker cwd = target repo" || bad "--repo worker cwd = target repo"
grep -q -- "--allowedTools Read,Grep,Glob" "$RUNDIR/out.log" \
  && ok "--repo keeps read-only whitelist" || bad "--repo keeps read-only whitelist"
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
if "$DD" --task "$TDIR/task-tgt.md" --model haiku --label "rg tgt" \
     --worktree-repo "$REPO3" >/dev/null 2>"$TDIR/t7a.err"; then
  bad "--worktree-repo without --worktree refused"
else
  grep -q "requires --worktree" "$TDIR/t7a.err" \
    && ok "--worktree-repo without --worktree refused with reason" \
    || bad "--worktree-repo without --worktree refused with reason"
fi
# T7B: nonexistent path -> exit 1.
"$DD" --task "$TDIR/task-tgt.md" --model haiku --label "rg tgt" \
  --worktree tgtbad --worktree-repo "$TDIR/no-such-repo" >/dev/null 2>&1 \
  && bad "--worktree-repo nonexistent path refused" || ok "--worktree-repo nonexistent path refused"
# T7C: non-git directory -> HARD refusal (unlike --repo, which only warns).
mkdir -p "$TDIR/plain-dir2"
if "$DD" --task "$TDIR/task-tgt.md" --model haiku --label "rg tgt" \
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
OUT="$(FAKE_CLAUDE_SLEEP=1 FAKE_CLAUDE_COMMIT=1 "$DD" --task "$TDIR/task-tgt.md" \
  --model sonnet --label "rg tgt build" --worktree tgtbuild \
  --worktree-repo "$REPO3" --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
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
  --model sonnet --label "rg ws build" --worktree wsbuild --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if wait_finalized "$RUNDIR" 30; then sleep 1; else bad "bare --worktree run finalize"; fi
git -C "$WS" show-ref --verify --quiet refs/heads/dsp/wsbuild \
  && ok "bare --worktree still lands in WORKSPACE repo" || bad "bare --worktree still lands in WORKSPACE repo"
git -C "$REPO3" show-ref --verify --quiet refs/heads/dsp/wsbuild \
  && bad "bare --worktree does not touch target repo" || ok "bare --worktree does not touch target repo"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0

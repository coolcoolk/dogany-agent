#!/usr/bin/env bash
# test-lane: slow    # one real timed-out E2E run (~65s) dominates the wall time
# test-dgn1433-preserve-product.sh -- dispatch teardown must never destroy
# uncommitted worker product (DGN-1433).
#
# Dirty checkouts retain their original HEAD, index and files for review.
# Runs the real timeout runner with a fake worker in scratch repositories.
set -uo pipefail

# The fixture supplies a fake Claude binary, regardless of parent runtime.
export DOGANY_DISPATCH_RUNTIME=claude-code

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="${DISPATCH_WRAPPER_UNDER_TEST:-$REPO/routines/dispatch-detached.sh}"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1433-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  # The injection test leaves a read-only objects dir; unlock before rm.
  chflags -R nouappend,nouchg "$TDIR" 2>/dev/null   # terminal-state ledger is uappend
  chmod -R u+w "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Scratch workspace (same shape as test-dgn991): wrapper + ledger + stubs
# ---------------------------------------------------------------------------
mkdir -p "$WS/routines/lib" "$WS/product" "$WS/.claude" "$WS/.telegram_bot" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
cp "$REPO/routines/lib/ledger-update.sh" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-tier-bindings.tsv" "$WS/routines/lib/"
cp "$REPO/routines/lib/model_display.py" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-model-display.tsv" "$WS/routines/lib/"
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/" 2>/dev/null || true
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo "USAGE_GATE: pass five_hour=10 seven_day=10"
exit 0
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
echo "PUSH-STUB $*" >> "${PUSH_STUB_LOG:?}"
exit 0
EOF
# Fake claude: dirties the WORKTREE it runs in (cwd), per FAKE_MODE.
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
echo "FAKE-CLAUDE pwd: $(pwd)"
case "${FAKE_MODE:-none}" in
  commit)
    echo "artifact" > fake-artifact.txt
    git add fake-artifact.txt && git -c user.name=t -c user.email=t@t commit -qm "fake work" ;;
  tracked)
    echo "uncommitted tracked change" >> target.txt ;;
  untracked)
    echo "orphan product" > orphan-product.txt ;;
  none) : ;;
esac
sleep "${FAKE_CLAUDE_SLEEP:-1}"
echo "FAKE-CLAUDE-DONE"
exit "${FAKE_RC:-0}"
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/claude"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| auto/seed | seed | merged | 1 | - | 2026-01-01 00:00 KST | - | seed |
EOF
echo "seed content" > "$WS/target.txt"
git -C "$WS" init -q -b main >/dev/null
git -C "$WS" config user.email t@t
git -C "$WS" config user.name t
git -C "$WS" add -A >/dev/null
git -C "$WS" commit -qm seed >/dev/null

export PATH="$TDIR/bin:$PATH"
# DGN-1504: worktree runs need sandbox-exec (stand-ins off macOS).
source "$HARNESS_DIR/fixtures/sandbox-standins.sh"
install_sandbox_standins "$TDIR/bin"
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
export PUSH_STUB_LOG="$TDIR/push-calls.log"
DD="$WS/routines/dispatch-detached.sh"
INBOX="$WS/.telegram_bot/session-inbox"

wait_finalized() {
  local i
  for i in $(seq 1 "$2"); do
    [[ -d "$1/.finalized" ]] && return 0
    sleep 1
  done
  return 1
}
runid_of() { sed -n 's/.*runid=\(dsp-[0-9-]*\).*/\1/p' <<< "$1"; }
st_field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$1" "$2"; }

# ---------------------------------------------------------------------------
# Function-level: drive finalize_run directly against scratch repos.
# Each case creates an isolated repo, worktree and run directory.
# No real model, live ledger or notification sender is invoked.
# ---------------------------------------------------------------------------
run_finalize_case() {
  # args: name result exit_code dirty_mode inject
  # Modes cover clean, tracked, untracked, mixed, staged, ignored, pytest and detached.
  # inject: status-error (read failure) or lock-objects (legacy fixture).
  local name="$1" result="$2" exit_code="$3" dirty_mode="$4" inject="${5:-}"
  (
    export DISPATCH_TEST=1
    # Clear the function's positional params: a sourced script would otherwise
    # read them as its own argv and die in the option parser.
    set --
    # shellcheck disable=SC1090
    source "$DD" || { echo "SOURCE-FAIL"; exit 1; }

    FX_REPO="$TDIR/fx-$name"
    mkdir -p "$FX_REPO"
    git -C "$FX_REPO" init -q -b main
    git -C "$FX_REPO" config user.email t@t
    git -C "$FX_REPO" config user.name t
    echo base > "$FX_REPO/tracked.txt"
    printf 'ignored.bin\n__pycache__/\n*.pyc\n' > "$FX_REPO/.gitignore"
    mkdir -p "$FX_REPO/routines/tests"
    printf 'def test_residue():\n    assert True\n' > "$FX_REPO/routines/tests/test_residue.py"
    git -C "$FX_REPO" add -A && git -C "$FX_REPO" commit -qm seed
    FX_WT="$TDIR/wt-fx/$name"
    mkdir -p "$TDIR/wt-fx"
    git -C "$FX_REPO" worktree add "$FX_WT" -b "dsp/$name" main >/dev/null 2>&1 \
      || { echo "WT-ADD-FAIL"; exit 1; }

    case "$dirty_mode" in
      clean) : ;;
      tracked)   echo "tracked delta" >> "$FX_WT/tracked.txt" ;;
      untracked) echo "orphan" > "$FX_WT/orphan.txt" ;;
      both)      echo "tracked delta" >> "$FX_WT/tracked.txt"
                 echo "orphan" > "$FX_WT/orphan.txt" ;;
      staged)
        echo staged > "$FX_WT/tracked.txt"
        git -C "$FX_WT" add tracked.txt
        echo unstaged >> "$FX_WT/tracked.txt" ;;
      ignored) echo "tracked delta" >> "$FX_WT/tracked.txt"
               printf '\000binary' > "$FX_WT/ignored.bin" ;;
      pytest)
        # Override platform cache relocation so bytecode lives in this checkout.
        (cd "$FX_WT" && PYTHONDONTWRITEBYTECODE= PYTEST_ADDOPTS= \
          python3 -X pycache_prefix= -m pytest -q routines/tests/test_residue.py) > "$TDIR/pytest-$name.log" 2>&1 \
          || { cat "$TDIR/pytest-$name.log"; exit 1; } ;;
      detached-commit)
        git -C "$FX_WT" checkout -q --detach
        echo "stray" > "$FX_WT/stray.txt"
        git -C "$FX_WT" add -A
        git -C "$FX_WT" -c user.name=t -c user.email=t@t commit -qm "stray commit off-branch" ;;
    esac
    if [[ "$inject" == "lock-objects" ]]; then
      chmod -R a-w "$FX_REPO/.git/objects"
    fi

    RUNID="dsp-fx-$name"
    RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
    mkdir -p "$RUNDIR"
    WT_PATH="$FX_WT"; WT_REPO="$FX_REPO"; WT_BASE="main"
    BRANCH="dsp/$name"; LEDGER_BRANCH="dsp/$name"
    LABEL="fx $name"; DISPLAY_NAME=""; TICKET="DGN-1433"
    MODEL="haiku"; RUNTIME="claude-code"; BOTTLENECK="accuracy"; TIER_WHY=""
    CODEX_QUOTA_NOTE=""; CODEX_HOOK_NOTE=""; CODEX_UNGUARDED_WHY=""
    TIMEOUT_MIN=1; NOTIFY="silent"; STARTED_AT="2026-09-15T10:00:00+0900"

    git -C "$FX_WT" rev-parse HEAD > "$RUNDIR/before-head"
    git -C "$FX_WT" diff --cached --binary > "$RUNDIR/before-index"
    git -C "$FX_WT" diff --binary > "$RUNDIR/before-worktree"
    git -C "$FX_WT" status --porcelain --untracked-files=all > "$RUNDIR/before-status"
    git -C "$FX_WT" ls-files --others --ignored --exclude-standard > "$RUNDIR/before-ignored"
    if [[ "$inject" == "status-error" ]]; then
      git() {
        if [[ "${3:-}" == "status" ]]; then return 1; fi
        command git "$@"
      }
    fi
    printf '2\n' > "$ACTIVE_COUNT_FILE"
    finalize_run "$result" "$exit_code" 2>"$RUNDIR/finalize.err"
    finalize_run "$result" "$exit_code" 2>>"$RUNDIR/finalize.err"
    cp "$ACTIVE_COUNT_FILE" "$RUNDIR/count-after"
    echo "FINALIZE-RC=$?"
    if [[ "$inject" == "lock-objects" ]]; then
      chmod -R u+w "$FX_REPO/.git/objects"
    fi
  )
}

# Exercise all terminal results without assigning authorship to the changes.
for spec in 'tracked timed-out' 'untracked timed-out' 'both error' 'staged canceled' 'ignored completed'; do
  read -r mode result <<< "$spec"
  OUT="$(run_finalize_case "$mode" "$result" 124 "$mode")"
  RD="$WS/.telegram_bot/dispatch/dsp-fx-$mode"
  WT="$TDIR/wt-fx/$mode"
  [[ "$(cat "$RD/count-after")" == 1 ]] && ok "$mode decremented once" || bad "$mode decremented once"
  [[ -d "$WT" ]] && ok "$mode worktree retained" || bad "$mode worktree retained"
  [[ "$(st_field "$RD/status.json" preserve)" == kept-worktree ]] && ok "$mode verdict" || bad "$mode verdict"
  [[ "$(st_field "$RD/status.json" worktree_kept)" == "$WT" ]] && ok "$mode recovery path" || bad "$mode recovery path"
  [[ "$(git -C "$TDIR/fx-$mode" rev-parse "dsp/$mode")" == "$(cat "$RD/before-head")" ]] && ok "$mode HEAD unchanged" || bad "$mode HEAD unchanged"
  git -C "$WT" diff --cached --binary > "$RD/after-index" 2>/dev/null
  git -C "$WT" diff --binary > "$RD/after-worktree" 2>/dev/null
  cmp -s "$RD/before-index" "$RD/after-index" && ok "$mode index unchanged" || bad "$mode index unchanged"
  cmp -s "$RD/before-worktree" "$RD/after-worktree" && ok "$mode unstaged unchanged" || bad "$mode unstaged unchanged"
  grep -q -- "$WT" "$INBOX/dispatch-dsp-fx-$mode.md" && ok "$mode inbox recovery" || bad "$mode inbox recovery"
  grep -q 'product: present' "$INBOX/dispatch-dsp-fx-$mode.md" && ok "$mode product detected without stream" || bad "$mode product detected without stream"
done
[[ "$(cat "$TDIR/wt-fx/untracked/orphan.txt" 2>/dev/null)" == orphan ]] && ok 'untracked bytes' || bad 'untracked bytes'
[[ "$(od -An -tx1 "$TDIR/wt-fx/ignored/ignored.bin" 2>/dev/null | tr -d ' \n')" == 0062696e617279 ]] && ok 'ignored binary bytes' || bad 'ignored binary bytes'
for spec in 'pytest pytest none' 'clean clean none' 'detached detached-commit none' 'unreadable clean status-error'; do
  read -r name mode inject <<< "$spec"
  if [[ "$name" == pytest ]]; then
    OUT="$(run_finalize_case "$name" completed 0 "$mode" "$inject")"
  else
    OUT="$(run_finalize_case "$name" timed-out 124 "$mode" "$inject")"
  fi
  WT="$TDIR/wt-fx/$name"
  if [[ "$name" == pytest ]]; then
    RD="$WS/.telegram_bot/dispatch/dsp-fx-$name"
    [[ ! -s "$RD/before-status" ]] && ok 'pytest no real changes' || bad 'pytest no real changes'
    grep -q '.pytest_cache/' "$RD/before-ignored" && ok 'pytest cache reproduced' || bad 'pytest cache reproduced'
    grep -q '__pycache__/.*pyc' "$RD/before-ignored" && ok 'pytest bytecode reproduced' || bad 'pytest bytecode reproduced'
    [[ ! -d "$WT" ]] && ok 'pytest checkout removed' || bad 'pytest checkout removed'
    [[ -z "$(st_field "$RD/status.json" preserve)" ]] && ok 'pytest no preservation trigger' || bad 'pytest no preservation trigger'
    [[ -z "$(st_field "$RD/status.json" worktree_kept)" ]] && ok 'pytest no recovery path' || bad 'pytest no recovery path'
    [[ "$(cat "$RD/count-after")" == 1 ]] && ok 'pytest decremented once' || bad 'pytest decremented once'
  elif [[ "$name" == clean ]]; then
    [[ ! -d "$WT" ]] && ok 'clean checkout removed' || bad 'clean checkout removed'
  else
    [[ -d "$WT" ]] && ok "$name retained" || bad "$name retained"
  fi
done

# ---------------------------------------------------------------------------
# E2E: real spawn through the wrapper (fake claude), real worker finalize.
# ---------------------------------------------------------------------------
printf '0\n' > "$WS/.telegram_bot/dispatch/active-count"
echo "== E1 (T1): completed + commits, clean tree -> teardown unchanged =="
printf 'build task' > "$TDIR/task-e1.md"
OUT="$(FAKE_MODE=commit FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task-e1.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn1433 test" --label "pv build" --worktree pvbuild --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
wait_finalized "$RUNDIR" 30 || bad "E1 finalize (timed out waiting)"
sleep 1
grep "dsp/pvbuild" "$WS/product/auto-loop-ledger.md" | grep -q "pending-review" \
  && ok "E1 commits -> pending-review (unchanged)" || bad "E1 commits -> pending-review (unchanged)"
git -C "$WS" show-ref --verify --quiet refs/heads/dsp/pvbuild \
  && ok "E1 branch preserved (unchanged)" || bad "E1 branch preserved (unchanged)"
[[ ! -d "$TDIR/wt/$RUNID-pvbuild" ]] \
  && ok "E1 worktree removed (unchanged)" || bad "E1 worktree removed (unchanged)"
N="$(git -C "$WS" rev-list --count main..dsp/pvbuild)"
[[ "$N" == "1" ]] && ok "E1 exactly the worker's own commit, no extra wip" || bad "E1 exactly the worker's own commit, no extra wip (got $N)"
[[ -z "$(st_field "$RUNDIR/status.json" preserve)" ]] \
  && ok "E1 status.json preserve empty on clean completion" || bad "E1 status.json preserve empty on clean completion"

echo "== E2 (T4): failed run with tracked uncommitted change -> retained =="
printf 'fail task' > "$TDIR/task-e2.md"
OUT="$(FAKE_MODE=tracked FAKE_RC=3 FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task-e2.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn1433 test" --label "pv fail" --worktree pvfail --timeout 2 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
wait_finalized "$RUNDIR" 30 || bad "E2 finalize (timed out waiting)"
sleep 1
[[ "$(st_field "$RUNDIR/status.json" result)" == "error" ]] \
  && ok "E2 lifecycle recorded as error" || bad "E2 lifecycle recorded as error (got '$(st_field "$RUNDIR/status.json" result)')"
[[ "$(st_field "$RUNDIR/status.json" preserve)" == "kept-worktree" ]] \
  && ok "E2 failed run retained" || bad "E2 failed run retained"
grep -q "uncommitted tracked change" "$TDIR/wt/$RUNID-pvfail/target.txt" \
  && ok "E2 tracked change recoverable from worktree" || bad "E2 tracked change recoverable from worktree"
[[ -d "$TDIR/wt/$RUNID-pvfail" ]] \
  && ok "E2 worktree retained after preserve" || bad "E2 worktree retained after preserve"

echo "== E3 (T3): REAL timed-out run + untracked only -> retained (~65s) =="
printf 'timeout task' > "$TDIR/task-e3.md"
OUT="$(FAKE_MODE=untracked FAKE_CLAUDE_SLEEP=300 "$DD" --task "$TDIR/task-e3.md" --model haiku --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --why "dgn1433 test" --label "pv timeout" --worktree pvtimeout --timeout 1 --notify silent)"
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if wait_finalized "$RUNDIR" 110; then
  ok "E3 timed-out run finalized"
else
  bad "E3 timed-out run finalized (timed out waiting)"
fi
sleep 1
[[ "$(st_field "$RUNDIR/status.json" result)" == "timed-out" ]] \
  && ok "E3 lifecycle recorded as timed-out" || bad "E3 lifecycle recorded as timed-out (got '$(st_field "$RUNDIR/status.json" result)')"
[[ "$(st_field "$RUNDIR/status.json" preserve)" == "kept-worktree" ]] \
  && ok "E3 untracked-only product retained on timeout" || bad "E3 untracked-only product retained on timeout"
grep -q "orphan product" "$TDIR/wt/$RUNID-pvtimeout/orphan-product.txt" \
  && ok "E3 untracked product content recoverable from worktree" || bad "E3 untracked product content recoverable from worktree"
[[ -d "$TDIR/wt/$RUNID-pvtimeout" ]] \
  && ok "E3 worktree retained after preserve" || bad "E3 worktree retained after preserve"
grep -q -- "$TDIR/wt/$RUNID-pvtimeout" "$INBOX/dispatch-$RUNID.md" 2>/dev/null \
  && ok "E3 inbox drop reports the harness" || bad "E3 inbox drop reports the harness"
COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo 'missing')"
[[ "$COUNT" == "0" ]] && ok "E3 active-count back to 0" || bad "E3 active-count back to 0 (got $COUNT)"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]

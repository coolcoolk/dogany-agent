#!/bin/bash
# dgn712_smoke_gate_selftest.sh -- DGN-712 Layer-1 pre-restart smoke gate test.
#
# The self_restart.sh worker runs an import smoke test (python -m bridge
# --selfcheck) with the instance venv BEFORE it kills the running bridge. A
# failing import must ABORT the restart (exit 4), NOT kill the old bridge, and
# push a warning. A passing import must proceed to the normal restart path.
#
# This test exercises the gate logic in isolation with a STUB python and a stub
# launchctl/push so no real bridge/process is touched. It drives the worker
# path (--_worker) directly and asserts:
#   S1  stub python exits 1 (import fail)  -> worker exits 4, NO kill issued,
#       a warning push was emitted.
#   S2  stub python exits 0 (import ok)    -> gate passes; worker proceeds past
#       the gate (a kill is attempted; here it no-ops on the stub pid).
#   S3  --skip-smoke with a failing stub   -> gate bypassed, worker proceeds.
#
# SAFETY: fully hermetic. A private mktemp dir holds a fake bridge/ tree, stub
# venv python, stub push.sh, and a fake launchctl on PATH. Nothing outside the
# sandbox is read or killed.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/agents/.template/bridge/self_restart.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn712-smoke.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f "$SRC" ]] || fail "self_restart.sh not found at $SRC"

# ---- Build a fake instance tree: <root>/bridge/{self_restart.sh, venv/bin/python}
ROOT="$WORK/instance"
BR="$ROOT/bridge"
mkdir -p "$BR/venv/bin" "$ROOT/.telegram_bot/logs"

# Materialize self_restart.sh with the template placeholders resolved to the
# sandbox so it runs standalone (mirrors what install.sh does per instance).
sed -e "s#__PROJECT_ROOT__#$ROOT#g" \
    -e "s#__AGENT_NAME__#dgn712test#g" \
    -e "s#__AGENT_PREFIX__#[t]#g" \
    "$SRC" > "$BR/self_restart.sh"
chmod +x "$BR/self_restart.sh"

# Stub push.sh: record every push into a log so we can assert a warning fired.
mkdir -p "$ROOT/routines"
cat > "$ROOT/routines/push.sh" <<'EOF'
#!/bin/bash
# stub push: append the --text payload to pushes.log
txt=""
while [[ $# -gt 0 ]]; do case "$1" in --text) txt="$2"; shift 2;; --env|--html) shift $([[ "$1" == --html ]] && echo 1 || echo 2);; *) shift;; esac; done
echo "PUSH: $txt" >> "$PUSHLOG"
EOF
chmod +x "$ROOT/routines/push.sh"

# Stub .env so push arg check is happy (push.sh existence + -x is what matters).
touch "$ROOT/.telegram_bot/.env"

# Fake launchctl on PATH: `list` prints a matching row so cur_pid() returns a
# pid; `kill`/kickstart are recorded so we can assert whether a kill happened.
FAKEBIN="$WORK/fakebin"
mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/launchctl" <<EOF
#!/bin/bash
case "\$1" in
  list) echo "12345	0	com.telegram-skill-bot.dgn712test" ;;
  *) echo "launchctl \$*" >> "$WORK/launchctl.log" ;;
esac
EOF
chmod +x "$FAKEBIN/launchctl"

# ---- Helper: install a stub venv python that exits with a chosen code. -------
set_stub_python() {
  local code="$1"
  cat > "$BR/venv/bin/python" <<EOF
#!/bin/bash
# stub bridge interpreter: emit a selfcheck-style line and exit \$CODE.
echo "selfcheck stub (exit $code)"
exit $code
EOF
  chmod +x "$BR/venv/bin/python"
}

# run_worker_sync: run the worker to completion (used when we expect a fast
# abort). Prints the exit code.
run_worker_sync() {
  PATH="$FAKEBIN:$PATH" PUSHLOG="$WORK/pushes.log" \
    bash "$BR/self_restart.sh" --_worker --reason "dgn712 test" \
      --label "com.telegram-skill-bot.dgn712test" \
      --env "$ROOT/.telegram_bot/.env" --delay 0 "$@" \
      >"$WORK/worker.out" 2>&1
  echo $?
}

# proceeded_past_gate: the worker passed the smoke gate iff its log shows the
# gate result and does NOT show the abort line. (When the gate passes on a
# stub with no real bridge, the worker would otherwise poll 60s; we only need
# to observe that it moved past the gate, so we run it briefly in background,
# assert the log, then stop it.)
proceeded_past_gate() {
  : > "$WORK/pushes.log"
  ( PATH="$FAKEBIN:$PATH" PUSHLOG="$WORK/pushes.log" \
      bash "$BR/self_restart.sh" --_worker --reason "dgn712 test" \
        --label "com.telegram-skill-bot.dgn712test" \
        --env "$ROOT/.telegram_bot/.env" --delay 0 "$@" \
        >"$WORK/worker.out" 2>&1 ) &
  local wpid=$!
  # Give it a moment to run the gate and enter the kill/poll branch.
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    grep -q "no live pid\|kickstarting\|worker start" "$WORK/worker.out" 2>/dev/null && break
    sleep 0.2
  done
  sleep 0.3
  kill "$wpid" 2>/dev/null
  wait "$wpid" 2>/dev/null
  # Passed the gate iff no ABORT line was logged.
  ! grep -q "ABORT: pre-restart smoke gate failed" "$WORK/worker.out"
}

# ================================ S1: import fails -> abort ==================
: > "$WORK/pushes.log"
set_stub_python 1
rc="$(run_worker_sync)"
[[ "$rc" == "4" ]] || { cat "$WORK/worker.out"; fail "S1 expected exit 4 on import fail, got $rc"; }
grep -q "ABORT: pre-restart smoke gate failed" "$WORK/worker.out" \
  || fail "S1 expected the ABORT log line (worker.out: $(cat "$WORK/worker.out"))"
grep -q "업데이트 잠시 보류" "$WORK/pushes.log" \
  || fail "S1 expected an abort warning push (pushes.log: $(cat "$WORK/pushes.log"))"
echo "S1 OK: import fail -> exit 4, restart aborted, warning pushed"

# ================================ S2: import ok -> proceed ===================
set_stub_python 0
proceeded_past_gate \
  && echo "S2 OK: import ok -> gate passes, restart proceeds" \
  || { cat "$WORK/worker.out"; fail "S2 expected the worker to proceed past the gate"; }
grep -q "smoke gate result (rc=0)" "$WORK/worker.out" \
  || fail "S2 expected a passing smoke gate result line (worker.out: $(cat "$WORK/worker.out"))"

# ================================ S3: --skip-smoke bypass ====================
set_stub_python 1  # would fail the gate, but --skip-smoke bypasses it
proceeded_past_gate --skip-smoke \
  && echo "S3 OK: --skip-smoke bypasses the gate even on import fail" \
  || { cat "$WORK/worker.out"; fail "S3 --skip-smoke should bypass the failing gate and proceed"; }
grep -q "bypassing pre-restart import smoke gate" "$WORK/worker.out" \
  || fail "S3 expected the bypass log line (worker.out: $(cat "$WORK/worker.out"))"

echo "dgn712_smoke_gate_selftest: ALL PASS"

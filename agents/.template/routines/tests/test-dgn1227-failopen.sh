#!/usr/bin/env bash
# test-lane: fast    # measured 9s (2026-09-24, 5 lib states x 4 terminal paths)
# test-dgn1227-failopen.sh -- the model display layer FAILS OPEN (DGN-1227
# follow-up).
#
# Observed 2026-09-24 17:07: in a sandbox without routines/lib/model_display.py
# the timed-out run's dispatch-return drop vanished -- the drop's python did an
# unguarded `from model_display import ...`, died on ImportError, the drop was
# never written and the terminal-state ledger row was never closed. A
# presentation layer must never cost a terminal signal.
#
# Matrix: every terminal path (completed / failed / timed-out / canceled)
#   x every lib state (present / absent / raises at import / raises at call /
#     answers empty). For each cell finalize_run must still produce
#   * the session-inbox drop, with the raw alias as the model when the lib
#     failed ("- model: opus"),
#   * the owner END notice (push stub), completed carrying "- opus:",
#   * the terminal-state ledger close row.
# Plus: the spawn/START label (owner_model_display), the stream observation,
# and the status-footer workbench row under the same failures.
#
# Run:  bash routines/tests/test-dgn1227-failopen.sh
# Exit: 0 all pass, 1 any fail. Offline: scratch workspace, stub push.sh.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
LIB="$REPO/routines/lib"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1227-failopen.XXXXXX)"
cleanup() { rc=$?; chflags -R nouappend,nouchg "$TDIR" 2>/dev/null; chmod -R u+w "$TDIR" 2>/dev/null; rm -rf "$TDIR" 2>/dev/null || true; return $rc; }   # ledgers are uappend
trap cleanup EXIT

# make_ws <dir> <mode> -- a minimal dispatch tree whose display lib is in <mode>.
make_ws() {
  local ws="$1" mode="$2"
  mkdir -p "$ws/routines/lib" "$ws/product" "$ws/.telegram_bot/session-inbox"
  cp "$REPO/routines/dispatch-detached.sh" "$REPO/routines/terminal-state-ledger.py" "$ws/routines/"
  cp "$LIB/ledger-update.sh" "$LIB/dispatch-model-display.tsv" "$ws/routines/lib/"
  case "$mode" in
    present) cp "$LIB/model_display.py" "$ws/routines/lib/" ;;
    absent)  : ;;
    raise-import)
      printf 'raise RuntimeError("display lib broken at import")\n' > "$ws/routines/lib/model_display.py" ;;
    raise-call)
      cat > "$ws/routines/lib/model_display.py" <<'PY'
import sys
def model_display(*a, **k):
    raise RuntimeError("display lib broken at call")
def observe_from_stream(*a, **k):
    raise RuntimeError("display lib broken at call")
if __name__ == "__main__":
    raise SystemExit(3)
PY
      ;;
    empty)
      cat > "$ws/routines/lib/model_display.py" <<'PY'
def model_display(*a, **k):
    return ""
def observe_from_stream(*a, **k):
    return []
PY
      ;;
  esac
  cat > "$ws/routines/push.sh" <<'EOF'
#!/bin/bash
printf 'PUSH %s\n' "$*" >> "$PUSH_STUB_LOG"
exit 0
EOF
  chmod +x "$ws/routines"/*.sh "$ws/routines/lib/ledger-update.sh"
  cat > "$ws/product/auto-loop-ledger.md" <<'EOF'
| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| dsp/fo | fail-open fixture | running | 1 | - | 2026-09-24 17:00 KST | - | dispatch spawn (model=opus runtime=claude-code) |
EOF
}

# run_finalize <ws> <result> <exit> <runid> -- finalize_run in a subshell so
# every cell starts from a clean sourced state.
run_finalize() {
  local ws="$1" result="$2" exit_code="$3" runid="$4"
  local rundir="$ws/.telegram_bot/dispatch/$runid"
  mkdir -p "$rundir"; : > "$rundir/out.log"
  # A real claude stream, so the observed id path is exercised too.
  printf '%s\n' '{"type":"assistant","message":{"model":"claude-opus-5-5"}}' > "$rundir/stream.jsonl"
  python3 "$ws/routines/terminal-state-ledger.py" open --surface dispatch --id "$runid" \
    --ttl 5400 --notify session --note fixture --evidence "$rundir/out.log" >/dev/null 2>&1
  (
    export DISPATCH_TEST=1
    set --   # a sourced script inherits the caller's positional args
    # shellcheck disable=SC1090
    source "$ws/routines/dispatch-detached.sh"
    set +e
    SCRIPT_DIR="$ws/routines"; WORKSPACE="$ws"
    MODEL_DISPLAY_BINDINGS="$ws/routines/lib/dispatch-model-display.tsv"
    LEDGER_UPDATE="$ws/routines/lib/ledger-update.sh"
    LEDGER_FILE="$ws/product/auto-loop-ledger.md"; LEDGER_BRANCH="dsp/fo"
    SESSION_INBOX_DIR="$ws/.telegram_bot/session-inbox"
    PUSH_SH="$ws/routines/push.sh"
    RUNID="$runid"; RUNDIR="$rundir"; LABEL="fail-open fixture"; TICKET="DGN-1227"
    MODEL="opus"; RUNTIME="claude-code"; BRANCH=""; TIMEOUT_MIN=90; NOTIFY="owner"
    DISPLAY_NAME="fail-open fixture"
    WT_PATH=""; WT_REPO="$ws"; STARTED_AT="$(date +%Y-%m-%dT%H:%M:%S%z)"
    finalize_run "$result" "$exit_code"
  ) >"$TDIR/$runid.out" 2>"$TDIR/$runid.err"
}

export DOGANY_TSL_DEDUP_DIR="$TDIR/dedup"

for mode in present absent raise-import raise-call empty; do
  echo "== lib: $mode =="
  ws="$TDIR/ws-$mode"
  make_ws "$ws" "$mode"
  export DOGANY_TSL_LEDGER="$TDIR/tsl-$mode.jsonl"
  export DOGANY_TSL_INBOX="$ws/.telegram_bot/session-inbox"
  export DOGANY_TSL_PUSH="$ws/routines/push.sh"
  if [[ "$mode" == present ]]; then want_model="Claude Opus 5.5"; else want_model="opus"; fi

  for pair in completed:0 failed:1 timed-out:124 canceled:143; do
    result="${pair%%:*}"; code="${pair##*:}"
    runid="dsp-fo-$mode-$result"
    export PUSH_STUB_LOG="$TDIR/push-$runid.log"; : > "$PUSH_STUB_LOG"
    run_finalize "$ws" "$result" "$code" "$runid"
    drop="$ws/.telegram_bot/session-inbox/dispatch-$runid.md"
    if [[ -f "$drop" ]] && grep -qxF -- "- model: $want_model" "$drop"; then
      ok "$mode/$result: drop written, model '$want_model'"
    else
      bad "$mode/$result: drop missing or model not '$want_model' ($(grep -- '- model:' "$drop" 2>/dev/null || echo no-drop); err: $(tail -1 "$TDIR/$runid.err"))"
    fi
    if grep -q '^PUSH ' "$PUSH_STUB_LOG"; then
      ok "$mode/$result: owner END notice sent"
    else
      bad "$mode/$result: owner END notice missing"
    fi
    if [[ "$result" == completed ]]; then
      if grep -qF -- "- $want_model: fail-open fixture" "$PUSH_STUB_LOG"; then
        ok "$mode/$result: END block carries '$want_model'"
      else
        bad "$mode/$result: END block lacks '$want_model' ($(tr '\n' ' ' < "$PUSH_STUB_LOG"))"
      fi
    fi
    if grep -q "\"event\": \"close\".*\"$runid\"\|\"id\": \"$runid\".*\"event\": \"close\"\|\"event\": \"close\", \"id\": \"$runid\"" "$DOGANY_TSL_LEDGER" 2>/dev/null; then
      ok "$mode/$result: terminal-state ledger closed"
    else
      bad "$mode/$result: terminal-state ledger NOT closed"
    fi
  done

  # START notice / spawn banner label and the stream observation.
  label="$(DISPATCH_TEST=1 bash -c 'w="$1"; set --; source "$w/routines/dispatch-detached.sh"; SCRIPT_DIR="$w/routines"
      MODEL_DISPLAY_BINDINGS="$w/routines/lib/dispatch-model-display.tsv"; RUNTIME=claude-code; MODEL=opus
      owner_model_display ""' _ "$ws" 2>/dev/null)"
  if [[ "$mode" == present ]]; then want_start="Claude Opus 5.5"; else want_start="opus"; fi
  if [[ "$label" == "$want_start" ]]; then
    ok "$mode: START label '$want_start'"
  else
    bad "$mode: START label '$label' (want '$want_start')"
  fi
  obs="$(DISPATCH_TEST=1 bash -c 'w="$1" f="$2"; set --; source "$w/routines/dispatch-detached.sh"; SCRIPT_DIR="$w/routines"
      observe_model_from_stream "$f"' _ "$ws" "$TDIR/ws-$mode/.telegram_bot/dispatch/dsp-fo-$mode-completed/stream.jsonl" 2>/dev/null)"
  if [[ "$mode" == present ]]; then want_obs='["claude-opus-5-5"]'; else want_obs='[]'; fi
  if [[ "$obs" == "$want_obs" ]]; then
    ok "$mode: stream observation '$want_obs'"
  else
    bad "$mode: stream observation '$obs' (want '$want_obs')"
  fi
done

echo "== status-footer workbench row =="
footer_out="$(python3 - "$REPO/routines" <<'PY'
import importlib.util, os, sys
routines = sys.argv[1]
spec = importlib.util.spec_from_file_location("sf", os.path.join(routines, "status-footer.py"))
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)
note = "dispatch spawn (model=opus runtime=claude-code)"
def boom(*a, **k):
    raise RuntimeError("broken")
res = []
sf._shared_model_display, sf._observe_from_stream = None, None
res.append("absent=" + sf._model_display_from_note(note))
sf._shared_model_display, sf._observe_from_stream = boom, boom
res.append("raise=" + sf._model_display_from_note(note, "dsp/x"))
sf._shared_model_display = lambda *a, **k: ""
res.append("empty=" + sf._model_display_from_note(note))
print("|".join(res))
PY
)"
if [[ "$footer_out" == "absent=opus|raise=opus|empty=opus" ]]; then
  ok "footer row falls back to the raw alias (absent / raising / empty)"
else
  bad "footer row fallback: '$footer_out'"
fi

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]

#!/usr/bin/env bash
# test-lane: fast
# test-dgn1434-dynamic-cap.sh -- dispatch-detached.sh's concurrency cap follows
# LIVE remaining quota instead of standing in as a fixed "3" (DGN-1434).
#
# Covers:
#   - DGN-1835: quota_workers (the headroom formula) + dispatch_worker_burn
#     (measured per-model burn; unmeasured -> worst measured), and the machine
#     half (load / memory room) -- T1, T2, T14-T19. The DGN-1434 step tables
#     these replaced are gone.
#   - claude_quota_remaining / codex_quota_remaining: success parsing + read
#     failure (rc 1, never a guessed number).
#   - compute_dynamic_cap: healthy quota raises the cap above 3 (today's bug:
#     a nearly-empty window still refused a 4th dispatch), tight quota lowers
#     it below 3 down to 0, read failure falls back to the FIXED 3 for either
#     runtime (never unlimited), manual override is announced every time.
#   - the 2026-08-26 regression shape: a healthy 5h window must NOT mask a
#     critical 7d window -- the more-constrained window governs.
#   - per-runtime active-count independence: claude-code and codex count on
#     SEPARATE files and neither cap check touches the other's slot.
#   - the refusal message carries runtime + cap basis (numbers, not a bare
#     "cap reached").
#
# Run: bash routines/tests/test-dgn1434-dynamic-cap.sh
# Exit: 0 all pass, nonzero any fail.
#
# Safe/offline: stub claude-usage.sh / codex-ratelimit.py under a scratch
# workspace, function-level sourcing (DISPATCH_TEST=1, same harness pattern as
# test-dgn991-dispatch-detached.sh). No real spawn, no network, no live ledger.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1434-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() { rm -rf "$TDIR"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Scratch workspace: wrapper + stub quota readers. No real claude/codex binary
# needed -- every test here is function-level (source with DISPATCH_TEST=1).
# ---------------------------------------------------------------------------
mkdir -p "$WS/routines/lib" "$WS/product" "$WS/.claude" "$WS/.telegram_bot"
cp "$WRAPPER" "$WS/routines/"
# claude_quota_remaining reads usage via usage-gate.py get_usage() (DGN-1512);
# without it the read fails and every cap silently falls back to the fixed 3.
cp "$REPO/routines/usage-gate.py" "$WS/routines/"
cp "$REPO/routines/lib/conf_reader.py" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-tier-bindings.tsv" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-model-display.tsv" "$WS/routines/lib/"

# Stub claude-usage.sh --json: prints five_hour/seven_day utilization from env,
# or fails outright when CLAUDE_USAGE_STUB_FAIL is set (read-failure case).
cat > "$WS/routines/claude-usage.sh" <<'EOF'
#!/bin/bash
if [[ "${CLAUDE_USAGE_STUB_FAIL:-}" == "1" ]]; then
  echo "stub: simulated read failure" >&2
  exit 1
fi
printf '{"five_hour": {"utilization": %s}, "seven_day": {"utilization": %s}}\n' \
  "${STUB_FIVE_HOUR_UTIL:-10}" "${STUB_SEVEN_DAY_UTIL:-10}"
exit 0
EOF

# Stub codex-ratelimit.py --json: prints used_percent from env, or fails
# outright when CODEX_RATELIMIT_STUB_FAIL is set.
cat > "$WS/routines/codex-ratelimit.py" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
if os.environ.get("CODEX_RATELIMIT_STUB_FAIL") == "1":
    sys.stderr.write("stub: simulated read failure\n")
    sys.exit(2)
print(json.dumps({"ok": True, "used_percent": int(os.environ.get("STUB_CODEX_USED", "10"))}))
sys.exit(0)
EOF

cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo "USAGE_GATE: pass five_hour=10 seven_day=10"
exit "${GATE_STUB_RC:-0}"
EOF

chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/claude-usage.sh" \
  "$WS/routines/codex-ratelimit.py" "$WS/routines/usage-gate.sh"

DD="$WS/routines/dispatch-detached.sh"

# Clean baseline in THIS script's own environment (not inside run_source --
# unsetting there would also wipe out a VAR=val prefix meant for that one
# call). Individual tests override via `VAR=val run_source '...'`, which
# shadows for that single invocation only and reverts right after.
unset DOGANY_DISPATCH_MAX_ACTIVE CLAUDE_USAGE_STUB_FAIL CODEX_RATELIMIT_STUB_FAIL 2>/dev/null

# ---------------------------------------------------------------------------
# T1: quota_workers -- the DGN-1835 formula, by hand-checked numbers.
#   floor((rem - reserve - live*H) / (burn*H)), H = min(hours left, horizon).
# ---------------------------------------------------------------------------
run_source() {
  # $1 = shell snippet to eval after sourcing, in a clean subshell.
  # Captured into a local BEFORE `set --`: a bare `source "$DD"` inherits the
  # CALLING scope's own positional params as the sourced script's $@ -- left
  # as $1, that would hand the snippet itself to dispatch-detached.sh's own
  # arg parser as an "unknown arg" and exit 1 before eval ever ran.
  local snippet="$1"
  # usage-gate.py get_usage() keeps a 300s TTL cache under $WS/.claude; drop it
  # so each call re-reads the stub with that call's own STUB_* env.
  rm -f "$WS/.claude/.usage-cache.json"
  (
    cd "$TDIR"
    export DISPATCH_TEST=1
    set --
    # shellcheck disable=SC1090
    source "$DD"
    # DGN-1835: the machine half of the cap reads live load/memory; pin it so
    # results do not depend on the test host. STUB_MACHINE="fail" = read failure.
    machine_snapshot() {
      [[ "${STUB_MACHINE:-}" == "fail" ]] && return 1
      echo "${STUB_MACHINE:-0.5 8 16000}"
    }
    eval "$snippet"
  )
}

OUT="$(run_source '
  echo "T1A:$(quota_workers 92 10 7.1 6.3 "?" 0.4)"     # (82-2.84)/2.52 = 31.4
  echo "T1B:$(quota_workers 92 10 7.1 6.3 0.1 0.4)"     # H=0.1: (82-0.71)/0.63 = 129
  echo "T1C:$(quota_workers 12 10 7.1 6.3 "?" 0.4)"     # room < 0 -> 0, never negative
  echo "T1D:$(quota_workers 98 2 0 15 "?" 0.4)"         # codex: 96/6 = 16
')"
grep -q "T1A:31 0.40" <<< "$OUT" && ok "T1A formula, full horizon" || bad "T1A formula ($OUT)"
grep -q "T1B:129 0.10" <<< "$OUT" && ok "T1B near reset the horizon shrinks to the hours left" || bad "T1B ($OUT)"
grep -q "T1C:0 " <<< "$OUT" && ok "T1C remaining inside the reserve -> 0" || bad "T1C ($OUT)"
grep -q "T1D:16 " <<< "$OUT" && ok "T1D codex formula" || bad "T1D ($OUT)"

# ---------------------------------------------------------------------------
# T2: per-model burn table -- measured models priced by measurement, an
# unmeasured model priced as the worst measured one (never as free).
# ---------------------------------------------------------------------------
OUT="$(run_source '
  for m in opus sonnet fable haiku ""; do echo "T2-$m:$(dispatch_worker_burn "$m")"; done
')"
grep -q "T2-opus:6.3 1.1 measured" <<< "$OUT" && ok "T2 opus measured" || bad "T2 opus ($OUT)"
grep -q "T2-haiku:33.6 4.4 unmeasured" <<< "$OUT" && ok "T2 haiku unmeasured -> worst measured burn" || bad "T2 haiku ($OUT)"
grep -q "T2-:33.6 4.4 unmeasured" <<< "$OUT" && ok "T2 unset model -> worst measured burn" || bad "T2 unset ($OUT)"

# ---------------------------------------------------------------------------
# T3/T4: claude_quota_remaining -- success parsing + read failure.
# ---------------------------------------------------------------------------
OUT="$(STUB_FIVE_HOUR_UTIL=8 STUB_SEVEN_DAY_UTIL=15 run_source '
  out="$(claude_quota_remaining)" && echo "T3-OK:$out" || echo "T3-FAIL"
')"
grep -q "T3-OK:92 85" <<< "$OUT" && ok "T3 claude_quota_remaining parses five_hour/seven_day" \
  || bad "T3 claude_quota_remaining parses five_hour/seven_day (got: $OUT)"

OUT="$(CLAUDE_USAGE_STUB_FAIL=1 run_source '
  claude_quota_remaining >/dev/null 2>&1 && echo "T4-FAIL" || echo "T4-OK"
')"
grep -q "T4-OK" <<< "$OUT" && ok "T4 claude_quota_remaining read failure -> rc 1" \
  || bad "T4 claude_quota_remaining read failure -> rc 1 (got: $OUT)"

# ---------------------------------------------------------------------------
# T5/T5B: codex_quota_remaining -- via cached CODEX_USED_PERCENT, and its own
# live read when nothing cached yet.
# ---------------------------------------------------------------------------
OUT="$(run_source '
  CODEX_USED_PERCENT=18
  out="$(codex_quota_remaining)" && echo "T5-OK:$out" || echo "T5-FAIL"
')"
grep -q "T5-OK:82" <<< "$OUT" && ok "T5 codex_quota_remaining uses cached CODEX_USED_PERCENT" \
  || bad "T5 codex_quota_remaining uses cached CODEX_USED_PERCENT (got: $OUT)"

OUT="$(STUB_CODEX_USED=30 run_source '
  out="$(codex_quota_remaining)" && echo "T5B-OK:$out" || echo "T5B-FAIL"
')"
grep -q "T5B-OK:70" <<< "$OUT" && ok "T5B codex_quota_remaining falls back to a live read" \
  || bad "T5B codex_quota_remaining falls back to a live read (got: $OUT)"

# ---------------------------------------------------------------------------
# T6: HEALTHY quota raises the cap above 3 -- today's reported bug (2026-09-14:
# claude 5h 3% used / codex weekly 18% used, both refused at fixed cap 3).
# ---------------------------------------------------------------------------
OUT="$(STUB_FIVE_HOUR_UTIL=3 STUB_SEVEN_DAY_UTIL=4 run_source '
  compute_dynamic_cap claude-code
  echo "T6-CAP:$DYNAMIC_CAP_VALUE"
  echo "T6-NOTE:$DYNAMIC_CAP_NOTE"
')"
CAP="$(sed -n 's/^T6-CAP://p' <<< "$OUT")"
[[ "$CAP" -gt 3 ]] && ok "T6 healthy claude quota (3%/4% used) raises cap above 3 (got $CAP)" \
  || bad "T6 healthy claude quota raises cap above 3 (got $CAP)"
grep -q "T6-NOTE:claude model=.*5h remaining=97%" <<< "$OUT" && ok "T6 cap basis note names the windows" \
  || bad "T6 cap basis note names the windows (got: $(grep T6-NOTE <<< "$OUT"))"

OUT="$(STUB_CODEX_USED=18 run_source '
  compute_dynamic_cap codex
  echo "T6B-CAP:$DYNAMIC_CAP_VALUE"
')"
CAP="$(sed -n 's/^T6B-CAP://p' <<< "$OUT")"
[[ "$CAP" -gt 3 ]] && ok "T6B healthy codex quota (18% used) raises cap above 3 (got $CAP)" \
  || bad "T6B healthy codex quota raises cap above 3 (got $CAP)"

# ---------------------------------------------------------------------------
# T7: TIGHT quota lowers the cap below 3, down to 0.
# ---------------------------------------------------------------------------
OUT="$(STUB_FIVE_HOUR_UTIL=98 STUB_SEVEN_DAY_UTIL=98 run_source '
  compute_dynamic_cap claude-code
  echo "T7-CAP:$DYNAMIC_CAP_VALUE"
')"
CAP="$(sed -n 's/^T7-CAP://p' <<< "$OUT")"
[[ "$CAP" -lt 3 ]] && ok "T7 tight claude quota (98%/98% used) lowers cap below 3 (got $CAP)" \
  || bad "T7 tight claude quota lowers cap below 3 (got $CAP)"
[[ "$CAP" -eq 0 ]] && ok "T7B near-total exhaustion (98%/98% used) drives cap to 0" \
  || bad "T7B near-total exhaustion drives cap to 0 (got $CAP)"

# ---------------------------------------------------------------------------
# T8: 2026-08-26 REGRESSION -- a healthy 5h window must not mask a critical 7d
# window. That incident was cap 3 -> 5 (manual) while the 7d window was about
# to blow through 97%. The dynamic mapping must keep the cap low (<< 5) in
# that exact shape even though the 5h side alone looks fine.
# ---------------------------------------------------------------------------
OUT="$(STUB_FIVE_HOUR_UTIL=10 STUB_SEVEN_DAY_UTIL=97 run_source '
  compute_dynamic_cap claude-code
  echo "T8-CAP:$DYNAMIC_CAP_VALUE"
  echo "T8-NOTE:$DYNAMIC_CAP_NOTE"
')"
CAP="$(sed -n 's/^T8-CAP://p' <<< "$OUT")"
[[ "$CAP" -le 1 ]] && ok "T8 08-26 regression shape (5h healthy, 7d at 97% used) keeps cap <= 1 (got $CAP)" \
  || bad "T8 08-26 regression shape keeps cap <= 1 (got $CAP) -- REGRESSION: this is the exact incident shape"
grep -q "T8-NOTE:claude .*5h remaining=90%.*-> [1-9][0-9]*; 7d remaining=3%.*-> 0;.*=> cap=0" <<< "$OUT" \
  && ok "T8 cap basis shows the MORE constrained window (7d) governed, not the healthier one" \
  || bad "T8 cap basis shows the constrained window governed (got: $(grep T8-NOTE <<< "$OUT"))"

# ---------------------------------------------------------------------------
# T9/T10: read failure -> fixed 3, loudly, for EITHER runtime.
# ---------------------------------------------------------------------------
OUT="$(CLAUDE_USAGE_STUB_FAIL=1 run_source '
  compute_dynamic_cap claude-code
  echo "T9-CAP:$DYNAMIC_CAP_VALUE"
  echo "T9-NOTE:$DYNAMIC_CAP_NOTE"
')"
grep -q "T9-CAP:3" <<< "$OUT" && ok "T9 claude read failure falls back to fixed cap 3" \
  || bad "T9 claude read failure falls back to fixed cap 3 (got: $(grep T9-CAP <<< "$OUT"))"
grep -q "T9-NOTE:claude usage read FAILED" <<< "$OUT" && ok "T9 read-failure fallback is announced, not silent" \
  || bad "T9 read-failure fallback is announced (got: $(grep T9-NOTE <<< "$OUT"))"

OUT="$(CODEX_RATELIMIT_STUB_FAIL=1 run_source '
  compute_dynamic_cap codex
  echo "T10-CAP:$DYNAMIC_CAP_VALUE"
  echo "T10-NOTE:$DYNAMIC_CAP_NOTE"
')"
grep -q "T10-CAP:3" <<< "$OUT" && ok "T10 codex read failure falls back to fixed cap 3" \
  || bad "T10 codex read failure falls back to fixed cap 3 (got: $(grep T10-CAP <<< "$OUT"))"
grep -q "T10-NOTE:codex quota read FAILED" <<< "$OUT" && ok "T10 codex read-failure fallback is announced" \
  || bad "T10 codex read-failure fallback is announced (got: $(grep T10-NOTE <<< "$OUT"))"

# ---------------------------------------------------------------------------
# T11: manual override is ALWAYS announced, and ignores live quota entirely
# (even a critical quota state does not block an explicit override -- it is
# an escape hatch, per spec -- but it must never look like an ordinary
# computed decision).
# ---------------------------------------------------------------------------
OUT="$(DOGANY_DISPATCH_MAX_ACTIVE=9 STUB_FIVE_HOUR_UTIL=99 STUB_SEVEN_DAY_UTIL=99 run_source '
  compute_dynamic_cap claude-code
  echo "T11-CAP:$DYNAMIC_CAP_VALUE"
  echo "T11-NOTE:$DYNAMIC_CAP_NOTE"
')"
grep -q "T11-CAP:9" <<< "$OUT" && ok "T11 manual override wins over a critical quota reading" \
  || bad "T11 manual override wins over a critical quota reading (got: $(grep T11-CAP <<< "$OUT"))"
grep -q "T11-NOTE:OVERRIDE:" <<< "$OUT" && ok "T11 manual override is announced (never silent)" \
  || bad "T11 manual override is announced (got: $(grep T11-NOTE <<< "$OUT"))"

# ---------------------------------------------------------------------------
# T12: per-runtime counting is INDEPENDENT -- claude-code and codex fill their
# own counters on separate files; filling one never blocks the other.
# ---------------------------------------------------------------------------
OUT="$(DOGANY_DISPATCH_MAX_ACTIVE=2 run_source '
  rm -f "$WS/.telegram_bot/dispatch/active-count" "$WS/.telegram_bot/dispatch/active-count.codex"
  active_count_incr claude-code; active_count_incr claude-code
  active_count_incr claude-code 2>/dev/null && echo "T12A-FAIL" || echo "T12A-OK"
  # codex must still have room -- its counter is a SEPARATE file.
  active_count_incr codex 2>/dev/null && echo "T12B-OK" || echo "T12B-FAIL"
  active_count_incr codex 2>/dev/null && echo "T12C-OK" || echo "T12C-FAIL"
  active_count_incr codex 2>/dev/null && echo "T12D-FAIL" || echo "T12D-OK"
  echo "T12-CLAUDE-FILE:$(cat "$WS/.telegram_bot/dispatch/active-count")"
  echo "T12-CODEX-FILE:$(cat "$WS/.telegram_bot/dispatch/active-count.codex")"
')"
grep -q "T12A-OK" <<< "$OUT" && ok "T12A claude-code slot 3 refused at cap 2" || bad "T12A claude-code slot 3 refused at cap 2"
grep -q "T12B-OK" <<< "$OUT" && ok "T12B codex slot 1 accepted while claude-code is full" || bad "T12B codex slot 1 accepted while claude-code is full"
grep -q "T12C-OK" <<< "$OUT" && ok "T12C codex slot 2 accepted" || bad "T12C codex slot 2 accepted"
grep -q "T12D-OK" <<< "$OUT" && ok "T12D codex slot 3 refused at its own cap 2" || bad "T12D codex slot 3 refused at its own cap 2"
grep -q "T12-CLAUDE-FILE:2" <<< "$OUT" && ok "T12 claude-code counter file independently at 2" || bad "T12 claude-code counter file independently at 2 (got: $(grep T12-CLAUDE-FILE <<< "$OUT"))"
grep -q "T12-CODEX-FILE:2" <<< "$OUT" && ok "T12 codex counter file independently at 2" || bad "T12 codex counter file independently at 2 (got: $(grep T12-CODEX-FILE <<< "$OUT"))"

# ---------------------------------------------------------------------------
# T13: refusal via run_gates() carries runtime + cap basis, not a bare number.
# ---------------------------------------------------------------------------
OUT="$(DOGANY_DISPATCH_MAX_ACTIVE=1 run_source '
  rm -f "$WS/.telegram_bot/dispatch/active-count"
  MODEL=sonnet
  BOTTLENECK=coverage
  RUNTIME=claude-code
  run_gates >/dev/null 2>&1   # first spawn takes the only slot
  run_gates >"$TDIR/t13.err" 2>&1 && echo "T13-FAIL" || echo "T13-OK"
  cat "$TDIR/t13.err"
')"
grep -q "T13-OK" <<< "$OUT" && ok "T13 second spawn refused once the (overridden) cap of 1 is held" \
  || bad "T13 second spawn refused once the cap of 1 is held"
grep -q "refused: active dispatch cap reached for runtime=claude-code" <<< "$OUT" \
  && ok "T13 refusal names the runtime" || bad "T13 refusal names the runtime (got: $OUT)"
grep -q "cap basis: OVERRIDE:" <<< "$OUT" \
  && ok "T13 refusal carries the cap basis (the numbers behind the decision)" \
  || bad "T13 refusal carries the cap basis (got: $OUT)"

# ---------------------------------------------------------------------------
# T14-T19 (DGN-1835): the cap follows real headroom -- quota AND machine.
# ---------------------------------------------------------------------------
cap_of() { sed -n "s/^$1-CAP://p" <<< "$2"; }
note_of() { sed -n "s/^$1-NOTE://p" <<< "$2"; }

# T14: high remaining + idle machine -> well above the old table's 4/5.
OUT="$(STUB_FIVE_HOUR_UTIL=5 STUB_SEVEN_DAY_UTIL=10 STUB_MACHINE="0.5 8 16000" run_source '
  MODEL=opus; compute_dynamic_cap claude-code
  echo "T14-CAP:$DYNAMIC_CAP_VALUE"; echo "T14-NOTE:$DYNAMIC_CAP_NOTE"
')"
CAP="$(cap_of T14 "$OUT")"; NOTE="$(note_of T14 "$OUT")"
[[ "$CAP" -ge 8 ]] && ok "T14 opus, 95%/90% remaining, idle machine -> cap well above 4 (got $CAP)" \
  || bad "T14 high remaining + idle machine -> cap >= 8 (got $CAP; $NOTE)"
for want in "model=opus (measured)" "5h remaining=95%" "reserve=10%" "live=7.1%/h" "worker=6.3%/h" \
            "H=0.40h" "7d remaining=90%" "load5=0.5/12" "mem_avail=16000MB" "=> cap=$CAP"; do
  grep -qF "$want" <<< "$NOTE" && ok "T14 note carries '$want'" || bad "T14 note carries '$want' ($NOTE)"
done

# T15: low remaining -> small cap even on an idle machine.
OUT="$(STUB_FIVE_HOUR_UTIL=80 STUB_SEVEN_DAY_UTIL=10 run_source '
  MODEL=opus; compute_dynamic_cap claude-code; echo "T15-CAP:$DYNAMIC_CAP_VALUE"
')"
CAP="$(cap_of T15 "$OUT")"
[[ "$CAP" -ge 1 && "$CAP" -le 2 ]] && ok "T15 opus, 5h 20% remaining -> small cap 1-2 (got $CAP)" \
  || bad "T15 low remaining -> small cap (got $CAP)"
OUT="$(STUB_FIVE_HOUR_UTIL=50 STUB_SEVEN_DAY_UTIL=10 run_source '
  MODEL=fable; compute_dynamic_cap claude-code; echo "T15B-CAP:$DYNAMIC_CAP_VALUE"
')"
CAP="$(cap_of T15B "$OUT")"
[[ "$CAP" -le 3 ]] && ok "T15B fable (measured 5x opus burn) at 50% remaining stays small (got $CAP)" \
  || bad "T15B fable at 50% remaining stays small (got $CAP)"

# T16: loaded machine -> capped by load, on top of this runtime's own active count.
OUT="$(STUB_FIVE_HOUR_UTIL=5 STUB_SEVEN_DAY_UTIL=10 STUB_MACHINE="10.6 8 16000" run_source '
  printf "2\n" > "$WS/.telegram_bot/dispatch/active-count"
  MODEL=opus; compute_dynamic_cap claude-code
  echo "T16-CAP:$DYNAMIC_CAP_VALUE"; echo "T16-NOTE:$DYNAMIC_CAP_NOTE"
  rm -f "$WS/.telegram_bot/dispatch/active-count"
')"
CAP="$(cap_of T16 "$OUT")"; NOTE="$(note_of T16 "$OUT")"
[[ "$CAP" -eq 3 ]] && ok "T16 load5 10.6 of 12 -> room 1 on top of 2 active -> cap 3" \
  || bad "T16 loaded machine caps by load (got $CAP; $NOTE)"
grep -qF "load5=10.6/12 (+1)" <<< "$NOTE" && ok "T16 note shows the load room" || bad "T16 note shows load room ($NOTE)"

# T17: memory-starved machine -> capped by memory.
OUT="$(STUB_FIVE_HOUR_UTIL=5 STUB_SEVEN_DAY_UTIL=10 STUB_MACHINE="0.5 8 1800" run_source '
  MODEL=opus; compute_dynamic_cap claude-code; echo "T17-CAP:$DYNAMIC_CAP_VALUE"
')"
[[ "$(cap_of T17 "$OUT")" -eq 1 ]] && ok "T17 1800MB available -> memory room 1 -> cap 1" \
  || bad "T17 memory caps (got $(cap_of T17 "$OUT"))"

# T18: unmeasured model -> conservative fallback (worst measured burn), announced.
OUT="$(STUB_FIVE_HOUR_UTIL=5 STUB_SEVEN_DAY_UTIL=10 run_source '
  MODEL=haiku; compute_dynamic_cap claude-code
  echo "T18-CAP:$DYNAMIC_CAP_VALUE"; echo "T18-NOTE:$DYNAMIC_CAP_NOTE"
  MODEL=fable; compute_dynamic_cap claude-code; echo "T18F-CAP:$DYNAMIC_CAP_VALUE"
')"
[[ "$(cap_of T18 "$OUT")" -eq "$(cap_of T18F "$OUT")" ]] \
  && ok "T18 unmeasured model gets the worst measured model's cap ($(cap_of T18 "$OUT"))" \
  || bad "T18 unmeasured -> worst measured cap (haiku $(cap_of T18 "$OUT") vs fable $(cap_of T18F "$OUT"))"
grep -qF "model=haiku (unmeasured, priced as fable" <<< "$(note_of T18 "$OUT")" \
  && ok "T18 unmeasured fallback is announced" || bad "T18 unmeasured announced ($(note_of T18 "$OUT"))"

# T19: machine read failure -> quota cap only, announced (never silent).
OUT="$(STUB_FIVE_HOUR_UTIL=5 STUB_SEVEN_DAY_UTIL=10 STUB_MACHINE=fail run_source '
  MODEL=opus; compute_dynamic_cap claude-code
  echo "T19-CAP:$DYNAMIC_CAP_VALUE"; echo "T19-NOTE:$DYNAMIC_CAP_NOTE"
')"
grep -qF "machine read FAILED" <<< "$(note_of T19 "$OUT")" && ok "T19 machine read failure announced" \
  || bad "T19 machine read failure announced ($OUT)"
[[ "$(cap_of T19 "$OUT")" -ge 8 ]] && ok "T19 machine read failure leaves the quota cap" \
  || bad "T19 quota cap kept (got $(cap_of T19 "$OUT"))"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]

#!/bin/bash
# test-consolidate-usage-defer.sh -- consolidate wrapper usage-defer tests
# (DGN-726 protocol, upgraded by DGN-835: recovery moved to cron-guard).
#
# Exercises consolidate-0430.sh WITHOUT touching the real engine or launchd:
#   1. deferred run (memory.py exits 75 + fresh marker) -> wrapper exits 75,
#      does NOT schedule anything itself (cron-guard owns recovery now),
#      marker retained.
#   2. clean success (no marker)          -> rc0, scheduler never invoked.
#   3. stale legacy marker + clean success -> marker cleared (hygiene kept).
#   4. --from-reset-retry invocation      -> legacy one-shot self-clean fires
#      (kept for ONE release after DGN-835).
#
# Strategy: run the wrapper against a temp memory-engine dir with a stub
# `memory.py`, and shadow `launchctl`/`systemd-run` via a temp PATH dir to
# prove they are NEVER called by the wrapper anymore (except legacy cleanup).
#
# Run: bash routines/tests/test-consolidate-usage-defer.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER="$ROUTINES_DIR/consolidate-0430.sh"

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

TMP="$(mktemp -d /tmp/consolidate-defer-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# --- fake instance layout: routines/ (copy of wrapper) + memory-engine/ ---
mkdir -p "$TMP/routines" "$TMP/memory-engine"
cp "$WRAPPER" "$TMP/routines/consolidate-0430.sh"
chmod +x "$TMP/routines/consolidate-0430.sh"
MARKER="$TMP/memory-engine/.consolidate-usage-defer.json"

# --- stub scheduler + tools on a shadow PATH ---
BIN="$TMP/bin"
mkdir -p "$BIN"
SCHED_LOG="$TMP/sched.log"
: > "$SCHED_LOG"

cat > "$BIN/launchctl" <<EOF
#!/bin/bash
echo "launchctl \$*" >> "$SCHED_LOG"
exit 0
EOF
cat > "$BIN/systemd-run" <<EOF
#!/bin/bash
echo "systemd-run \$*" >> "$SCHED_LOG"
exit 0
EOF
cat > "$BIN/plutil" <<'EOF'
#!/bin/bash
exit 0
EOF
for t in systemctl loginctl; do
  cat > "$BIN/$t" <<'EOF'
#!/bin/bash
exit 0
EOF
done
chmod +x "$BIN"/*

# stub memory.py: `index` no-op; `consolidate` either defers (writes a fresh
# LEGACY marker + exits 75; env-marker behavior is covered by the memory.py
# pytest suite and the cron-guard test) or succeeds cleanly (exit 0).
write_stub_memory() {
  local defer_mode="$1" reset_iso="$2" deferred_at="$3"
  cat > "$TMP/memory-engine/memory.py" <<PYEOF
import sys, json, os
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "consolidate" and "$defer_mode" == "1":
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".consolidate-usage-defer.json")
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"window": "five_hour", "reset_at": "$reset_iso",
                   "deferred_at": "$deferred_at", "reason": "usage-exhaustion"}, f)
    sys.exit(75)
sys.exit(0)
PYEOF
}

run_wrapper() {
  PATH="$BIN:$PATH" bash "$TMP/routines/consolidate-0430.sh" "$@" 2>&1
  echo "WRAPPER_RC=$?"
}

iso_offset() {
  python3 -c "import sys; from datetime import datetime, timezone, timedelta; print((datetime.now(timezone.utc)+timedelta(seconds=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"
}

echo "== consolidate usage-defer wrapper (DGN-726/DGN-835) =="

# --- 1. deferred run -> rc75 passthrough, NO self-scheduling, marker kept ---
: > "$SCHED_LOG"
FUTURE="$(iso_offset 7200)"
NOWISO="$(iso_offset 0)"
write_stub_memory 1 "$FUTURE" "$NOWISO"
OUT="$(run_wrapper)"
assert_contains "defer: rc75 passthrough" "$OUT" "WRAPPER_RC=75"
assert_contains "defer: recovery-via-cron-guard log line" "$OUT" "usage-defer (rc75)"
if [[ -s "$SCHED_LOG" ]]; then fail "defer: wrapper wrongly scheduled its own retry"; else ok "defer: wrapper does NOT self-schedule (cron-guard owns recovery)"; fi
[[ -f "$MARKER" ]] && ok "defer: marker retained (fresh defer)" || fail "defer: marker missing"

# --- 2. clean success (no marker) -> rc0, no scheduler ---
: > "$SCHED_LOG"
rm -f "$MARKER"
write_stub_memory 0 "" ""
OUT="$(run_wrapper)"
assert_contains "clean run: rc0" "$OUT" "WRAPPER_RC=0"
if [[ -s "$SCHED_LOG" ]]; then fail "clean run: scheduler wrongly invoked"; else ok "clean run: scheduler not invoked"; fi

# --- 3. stale legacy marker + clean success -> marker cleared ---
: > "$SCHED_LOG"
STALE_AT="$(iso_offset -3600)"
PAST="$(iso_offset -3600)"
python3 -c "import json,sys; json.dump({'reset_at':'$PAST','deferred_at':'$STALE_AT','reason':'usage-exhaustion'}, open(sys.argv[1],'w'))" "$MARKER"
write_stub_memory 0 "" ""
OUT="$(run_wrapper)"
assert_contains "stale marker: cleared msg" "$OUT" "prior usage-defer resolved"
[[ -f "$MARKER" ]] && fail "stale marker: NOT cleared" || ok "stale marker: cleared"
if [[ -s "$SCHED_LOG" ]]; then fail "stale marker: scheduler wrongly invoked"; else ok "stale marker: scheduler not invoked"; fi

# --- 4. legacy --from-reset-retry -> one-shot self-clean (1-release keep) ---
: > "$SCHED_LOG"
rm -f "$MARKER"
write_stub_memory 0 "" ""
LEGACY_PLIST="$TMP/legacy-retry.plist"
touch "$LEGACY_PLIST"
OUT="$(run_wrapper --from-reset-retry "com.test.legacy.retry" "$LEGACY_PLIST")"
assert_contains "legacy retry: rc0" "$OUT" "WRAPPER_RC=0"
[[ -f "$LEGACY_PLIST" ]] && fail "legacy retry: plist not cleaned" || ok "legacy retry: plist cleaned"
SCHED_LINE="$(cat "$SCHED_LOG" 2>/dev/null || true)"
assert_contains "legacy retry: launchctl remove fired" "$SCHED_LINE" "launchctl remove com.test.legacy.retry"

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]

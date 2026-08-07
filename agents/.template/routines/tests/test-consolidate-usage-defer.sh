#!/bin/bash
# test-consolidate-usage-defer.sh -- DGN-726 wrapper scheduling tests.
#
# Exercises consolidate-0430.sh's defer-marker handling WITHOUT touching the real
# engine, launchd, or systemd:
#   1. defer marker with a FUTURE reset  -> scheduler invoked, "retry scheduled".
#   2. defer marker with a PAST reset     -> no scheduler, "next daily run" message.
#   3. no marker (clean success)          -> scheduler never invoked.
#   4. stale marker + clean success       -> marker cleared, scheduler not invoked.
#
# Strategy: run the wrapper against a temp memory-engine dir with a stub `memory.py`
# so `python3 memory.py consolidate` is a no-op (or drops a fresh marker), and stub
# the OS scheduler by shadowing `launchctl` / `systemd-run` via a temp PATH dir. We
# also stub `plutil` (macOS) so plist lint always passes without real validation
# quirks, and force the platform branch through the real `uname`.
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
# systemctl/loginctl no-ops (Linux branch prerequisites)
for t in systemctl loginctl; do
  cat > "$BIN/$t" <<'EOF'
#!/bin/bash
exit 0
EOF
done
chmod +x "$BIN"/*

# stub memory.py: `index` no-op; `consolidate` either writes a fresh marker
# (DEFER_MODE=1) or does nothing (clean success). Uses the real python3 for the
# wrapper's inline json parsing, so we keep python3 real and only shadow the
# scheduler tools.
write_stub_memory() {
  local defer_mode="$1" reset_iso="$2" deferred_at="$3"
  cat > "$TMP/memory-engine/memory.py" <<PYEOF
import sys, json, os
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "consolidate" and "$defer_mode" == "1":
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".consolidate-usage-defer.json")
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"reset_at": "$reset_iso", "deferred_at": "$deferred_at", "reason": "usage-exhaustion"}, f)
sys.exit(0)
PYEOF
}

run_wrapper() {
  # Run wrapper with shadow PATH first. Keep real python3/date/grep available.
  PATH="$BIN:$PATH" bash "$TMP/routines/consolidate-0430.sh" 2>&1
}

iso_offset() {
  # ISO-8601 UTC string offset from now by N seconds (portable via python3).
  python3 -c "import sys; from datetime import datetime, timezone, timedelta; print((datetime.now(timezone.utc)+timedelta(seconds=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"
}

echo "== DGN-726 consolidate usage-defer wrapper =="

# --- 1. future reset -> scheduler invoked ---
: > "$SCHED_LOG"
FUTURE="$(iso_offset 7200)"      # +2h
NOWISO="$(iso_offset 0)"
write_stub_memory 1 "$FUTURE" "$NOWISO"
OUT="$(run_wrapper)"
assert_contains "future reset: retry scheduled msg" "$OUT" "reset-aligned retry scheduled"
if [[ -s "$SCHED_LOG" ]]; then ok "future reset: scheduler invoked"; else fail "future reset: scheduler NOT invoked"; fi
[[ -f "$MARKER" ]] && ok "future reset: marker retained (fresh defer)" || fail "future reset: marker missing"

# --- 2. past reset -> no scheduler, next-daily message ---
: > "$SCHED_LOG"
PAST="$(iso_offset -3600)"       # -1h
write_stub_memory 1 "$PAST" "$NOWISO"
OUT="$(run_wrapper)"
assert_contains "past reset: next-daily msg" "$OUT" "next daily run handles it"
if [[ -s "$SCHED_LOG" ]]; then fail "past reset: scheduler wrongly invoked"; else ok "past reset: scheduler not invoked"; fi

# --- 3. clean success (no marker) -> scheduler never invoked ---
: > "$SCHED_LOG"
rm -f "$MARKER"
write_stub_memory 0 "" ""
OUT="$(run_wrapper)"
if [[ -s "$SCHED_LOG" ]]; then fail "clean run: scheduler wrongly invoked"; else ok "clean run: scheduler not invoked"; fi
assert_not_contains "clean run: no retry-scheduled msg" "$OUT" "reset-aligned retry scheduled"

# --- 4. stale marker + clean success -> marker cleared, no scheduler ---
: > "$SCHED_LOG"
# stale = deferred_at well in the past (> 10 min) so wrapper treats it as resolved.
STALE_AT="$(iso_offset -3600)"
python3 -c "import json,sys; json.dump({'reset_at':'$PAST','deferred_at':'$STALE_AT','reason':'usage-exhaustion'}, open(sys.argv[1],'w'))" "$MARKER"
write_stub_memory 0 "" ""     # clean success this run
OUT="$(run_wrapper)"
assert_contains "stale marker: cleared msg" "$OUT" "prior usage-defer resolved"
[[ -f "$MARKER" ]] && fail "stale marker: NOT cleared" || ok "stale marker: cleared"
if [[ -s "$SCHED_LOG" ]]; then fail "stale marker: scheduler wrongly invoked"; else ok "stale marker: scheduler not invoked"; fi

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]

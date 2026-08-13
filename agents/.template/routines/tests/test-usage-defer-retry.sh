#!/bin/bash
# test-usage-defer-retry.sh -- DGN-835 one-shot retry scheduler tests.
#
# Exercises usage-defer-retry.sh WITHOUT touching real launchd/systemd:
#   1. schedule (macOS branch): plist written under $HOME/Library/LaunchAgents,
#      argv serialized ARGUMENT-BY-ARGUMENT (space/quote/XML chars survive),
#      launchctl load invoked, StartCalendarInterval = reset epoch + 120s pad,
#      WorkingDirectory + Standard{Out,Error}Path present.
#   2. dedup per label: re-schedule replaces the previous plist (unload+rewrite).
#   3. past/immediate reset epoch -> exit 1, nothing scheduled (fail-open).
#   4. invalid label charset -> exit 1.
#   5. fire mode ORDER (DGN-835 MAJOR-A): the command runs TO COMPLETION with
#      the unit still loaded, THEN plist rm + launchctl remove; the replay's
#      exit code propagates. No-command fire still self-cleans.
#
# Strategy: HOME redirected into a sandbox; launchctl/plutil stubbed on PATH.
# Platform: asserts the Darwin branch (this repo's primary target); on Linux
# the script exercises the systemd branch which these stubs do not cover --
# skip there.
#
# Run: bash routines/tests/test-usage-defer-retry.sh
# Exit: 0 all pass (or skipped on non-Darwin), nonzero any fail.

set -uo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "SKIP: Darwin-branch test (uname=$(uname -s))"
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
assert_contains() {
  local desc="$1" hay="$2" needle="$3"
  if [[ "$hay" == *"$needle"* ]]; then ok "$desc"; else fail "$desc -- missing '$needle' in: $hay"; fi
}

TMP="$(mktemp -d /tmp/usage-defer-retry-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

FAKE_HOME="$TMP/home"
mkdir -p "$FAKE_HOME"

RT="$TMP/routines"
mkdir -p "$RT"
cp "$ROUTINES_DIR/usage-defer-retry.sh" "$RT/usage-defer-retry.sh"
chmod +x "$RT/usage-defer-retry.sh"

BIN="$TMP/bin"
mkdir -p "$BIN"
SCHED_LOG="$TMP/sched.log"
: > "$SCHED_LOG"
cat > "$BIN/launchctl" <<EOF
#!/bin/bash
echo "launchctl \$*" >> "$SCHED_LOG"
exit 0
EOF
cat > "$BIN/plutil" <<'EOF'
#!/bin/bash
exec /usr/bin/plutil "$@"
EOF
chmod +x "$BIN"/*

LABEL="com.test.dgn835.job"
ONESHOT="$LABEL.usage-retry"
PLIST="$FAKE_HOME/Library/LaunchAgents/$ONESHOT.plist"
FUTURE="$(python3 -c 'import time; print(int(time.time()) + 7200)')"

run_sched() {
  HOME="$FAKE_HOME" PATH="$BIN:$PATH" \
    bash "$RT/usage-defer-retry.sh" "$@" 2>&1
  echo "RC=$?"
}

echo "== DGN-835 usage-defer-retry scheduler =="

# --- 1. schedule: plist + argv fidelity ---
OUT="$(run_sched --label "$LABEL" --reset-epoch "$FUTURE" \
  -- /bin/bash /some/cron-guard.sh --label "$LABEL" -- /bin/echo "arg with space" 'a<b&c')"
assert_contains "schedule: reports success" "$OUT" "one-shot scheduled: $ONESHOT"
assert_contains "schedule: rc0" "$OUT" "RC=0"
[[ -f "$PLIST" ]] && ok "schedule: plist written" || fail "schedule: plist missing ($PLIST)"
SCHED_LINE="$(cat "$SCHED_LOG")"
assert_contains "schedule: launchctl load invoked" "$SCHED_LINE" "launchctl load $PLIST"
PLIST_BODY="$(cat "$PLIST" 2>/dev/null || true)"
assert_contains "plist: fire-mode re-entry" "$PLIST_BODY" "<string>--fire</string>"
assert_contains "plist: oneshot label arg" "$PLIST_BODY" "<string>$ONESHOT</string>"
assert_contains "plist: space arg survives as ONE string" "$PLIST_BODY" "<string>arg with space</string>"
assert_contains "plist: XML chars escaped" "$PLIST_BODY" "<string>a&lt;b&amp;c</string>"
if /usr/bin/plutil -lint "$PLIST" >/dev/null 2>&1; then
  ok "plist: passes plutil lint"
else
  fail "plist: plutil lint failed"
fi
# calendar fields match the epoch + 120s pad (minute-resolution early-fire guard)
CAL_OK="$(python3 -c "
import plistlib, sys
from datetime import datetime
with open('$PLIST', 'rb') as f:
    p = plistlib.load(f)
dt = datetime.fromtimestamp($FUTURE + 120)
c = p['StartCalendarInterval']
print('1' if (c['Month'], c['Day'], c['Hour'], c['Minute']) == (dt.month, dt.day, dt.hour, dt.minute) else '0')
")"
[[ "$CAL_OK" == "1" ]] && ok "plist: StartCalendarInterval = reset epoch + 120s pad" || fail "plist: calendar mismatch (pad)"
# WorkingDirectory + log paths ride the one-shot (DGN-835 follow-up)
assert_contains "plist: WorkingDirectory key present" "$PLIST_BODY" "<key>WorkingDirectory</key>"
assert_contains "plist: StandardOutPath -> instance log dir" "$PLIST_BODY" "/.telegram_bot/logs/$ONESHOT.stdout.log"
assert_contains "plist: StandardErrorPath -> instance log dir" "$PLIST_BODY" "/.telegram_bot/logs/$ONESHOT.stderr.log"

# --- 2. dedup per label: reschedule replaces ---
: > "$SCHED_LOG"
OUT="$(run_sched --label "$LABEL" --reset-epoch "$FUTURE" -- /bin/echo second)"
assert_contains "dedup: reschedule succeeds" "$OUT" "RC=0"
SCHED_LINE="$(cat "$SCHED_LOG")"
assert_contains "dedup: previous unit unloaded first" "$SCHED_LINE" "launchctl unload $PLIST"
PLIST_BODY="$(cat "$PLIST")"
assert_contains "dedup: plist now carries the new command" "$PLIST_BODY" "<string>second</string>"
if [[ "$PLIST_BODY" == *"arg with space"* ]]; then
  fail "dedup: old command lingers in plist"
else
  ok "dedup: old command replaced"
fi

# --- 3. past reset -> exit 1, nothing scheduled ---
: > "$SCHED_LOG"
rm -f "$PLIST"
PAST="$(python3 -c 'import time; print(int(time.time()) - 60)')"
OUT="$(run_sched --label "$LABEL" --reset-epoch "$PAST" -- /bin/echo x)"
assert_contains "past reset: rc1" "$OUT" "RC=1"
[[ -f "$PLIST" ]] && fail "past reset: plist wrongly written" || ok "past reset: nothing scheduled"

# --- 4. invalid label charset -> exit 1 ---
OUT="$(run_sched --label "bad/label" --reset-epoch "$FUTURE" -- /bin/echo x)"
assert_contains "bad label: rc1" "$OUT" "RC=1"
assert_contains "bad label: rejected" "$OUT" "invalid --label"

# --- 5. fire mode: work runs TO COMPLETION first, cleanup AFTER (MAJOR-A) ---
# The replayed command asserts the plist is STILL PRESENT while it runs
# (cleanup must not precede the work -- `launchctl remove` SIGTERMs the
# running job, so the old cleanup-then-exec order killed the replay mid-run)
# and exits 7 to prove the replay's exit code propagates.
: > "$SCHED_LOG"
FIRE_PLIST="$TMP/fired.plist"
touch "$FIRE_PLIST"
OUT="$(HOME="$FAKE_HOME" PATH="$BIN:$PATH" \
  bash "$RT/usage-defer-retry.sh" --fire "$ONESHOT" "$FIRE_PLIST" -- \
    /bin/bash -c "[[ -f \"$FIRE_PLIST\" ]] && echo plist-alive-during-run; echo fired ok; exit 7" 2>&1
  echo "RC=$?")"
assert_contains "fire: command executed" "$OUT" "fired ok"
assert_contains "fire: unit NOT cleaned before the work (order restored)" "$OUT" "plist-alive-during-run"
assert_contains "fire: replay exit code propagated" "$OUT" "RC=7"
[[ -f "$FIRE_PLIST" ]] && fail "fire: plist not cleaned after work" || ok "fire: plist cleaned after work"
SCHED_LINE="$(cat "$SCHED_LOG")"
assert_contains "fire: launchctl remove invoked (after work)" "$SCHED_LINE" "launchctl remove $ONESHOT"

# --- 6. fire mode without command -> rc1, unit still self-cleans ---
: > "$SCHED_LOG"
touch "$FIRE_PLIST"
OUT="$(HOME="$FAKE_HOME" PATH="$BIN:$PATH" \
  bash "$RT/usage-defer-retry.sh" --fire "$ONESHOT" "$FIRE_PLIST" -- 2>&1; echo "RC=$?")"
assert_contains "fire w/o command: rc1" "$OUT" "RC=1"
[[ -f "$FIRE_PLIST" ]] && fail "fire w/o command: plist lingers" || ok "fire w/o command: unit cleaned anyway"

echo "-------------------------------------------"
echo "PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]]

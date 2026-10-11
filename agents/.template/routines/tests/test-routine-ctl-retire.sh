#!/bin/bash
# test-lane: fast    # measured 0.55s (2026-09-22)
# test-host: macos launchd bootout + LaunchAgents plist
# test-routine-ctl-retire.sh -- DGN-1610: routine-ctl.sh disable must tell
# the health observer AFTER the unit is down, never before.
#
# THE DEFECT UNDER TEST: mac_disable() did launchctl bootout + plist removal
# and nothing else. The observer never learned the removal was intentional,
# so a real `routine-ctl.sh disable` produced a live false "job vanished"
# alert on an approved teardown (worklog DGN-1610, 2026-09-20 20:08 and its
# 2026-09-21 07:29 recurrence -- same defect, different day, different
# executor). Without the fix, disable never calls the observer at all, so
# no retired marker ever appears -- this test fails on marker absence.
#
# Ordering note (worklog DGN-1610, 2026-09-21 21:36/21:43): the marker is
# written HERE, after bootout, on purpose. The observer pops a retired
# marker it still sees loaded (re-mint hygiene, health-observer.py's own
# poll()) -- writing it before the unit is down would erase itself on the
# very next poll and ship a fix that silently does nothing.
#
# launchctl is stubbed through the library's own ROUTINE_CTL_LAUNCHCTL seam
# (mint_teardown.sh's MINT_TEARDOWN_LAUNCHCTL precedent) -- no real unit is
# touched.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Relative to this test's own routines/tests/ dir, not a hardcoded canonical
# path: routines/lib/routine-ctl.sh sits at the same place whether routines/
# is nested under agents/.template (canonical checkout) or is the instance's
# own top-level dir (minted instance) -- a $REPO_ROOT/agents/.template/...
# path only resolves in the former and rc=127's in the latter.
ROUTINE_CTL="$SCRIPT_DIR/../lib/routine-ctl.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $1"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export HOME="$WORK/home"
mkdir -p "$HOME"
export ROUTINE_CTL_LA_DIR="$WORK/LaunchAgents"
mkdir -p "$ROUTINE_CTL_LA_DIR"
STATE_DIR="$WORK/observer-state"
mkdir -p "$STATE_DIR"
export DOGANY_HEALTH_OBSERVER_STATE_DIR="$STATE_DIR"

NAME="dgn1610-probe"
# Replicate routine-ctl.sh's own AGENT_DIR/AGENT_NAME resolution exactly
# (lib/routine-ctl.sh: AGENT_DIR = two levels up from itself, AGENT_NAME =
# .instance.conf's DOGANY_AGENT_NAME else basename(AGENT_DIR)) instead of
# guessing a name -- canonical (agents/.template, no .instance.conf) and a
# minted instance (has one) resolve to different names, and both must match
# what routine-ctl.sh itself will use for the label below.
AGENT_DIR="$(cd "$(dirname "$ROUTINE_CTL")/../.." && pwd)"
AGENT_NAME="$(grep -E '^DOGANY_AGENT_NAME=' "$AGENT_DIR/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
AGENT_NAME="${AGENT_NAME:-$(basename "$AGENT_DIR")}"
LABEL="com.telegram-skill-bot.${AGENT_NAME}.${NAME}"

# Seed the observer's known-jobs set so the retire predicate (label known)
# passes without a live launchctl round trip from the observer itself.
python3 - "$STATE_DIR/jobs.json" "$LABEL" <<'PY'
import json, sys
path, label = sys.argv[1], sys.argv[2]
json.dump({"jobs": {label: {"first_seen_at": "t0"}}}, open(path, "w"))
PY

# launchctl stub: logs every call, reports nothing registered.
LOG="$WORK/launchctl.log"
cat > "$WORK/launchctl" <<STUB
#!/bin/bash
echo "\$@" >> "$LOG"
exit 1
STUB
chmod +x "$WORK/launchctl"
export ROUTINE_CTL_LAUNCHCTL="$WORK/launchctl"

# a plist on disk so mac_disable has something to remove
printf '<plist/>\n' > "$ROUTINE_CTL_LA_DIR/${LABEL}.plist"

OUT="$WORK/out"; ERR="$WORK/err"
bash "$ROUTINE_CTL" disable "$NAME" >"$OUT" 2>"$ERR"
rc=$?

[ "$rc" -eq 0 ] && ok "disable exits 0" || bad "disable exit rc=$rc (stderr: $(cat "$ERR"))"
grep -q '^bootout gui/' "$LOG" 2>/dev/null && ok "bootout was called" \
  || bad "bootout not called (log: $(cat "$LOG" 2>/dev/null))"
[ ! -f "$ROUTINE_CTL_LA_DIR/${LABEL}.plist" ] && ok "plist removed" || bad "plist still present"

RETIRED_WHY="$(python3 - "$STATE_DIR/jobs.json" "$LABEL" <<'PY'
import json, sys
path, label = sys.argv[1], sys.argv[2]
data = json.load(open(path))
entry = data.get("jobs", {}).get(label, {})
print(entry.get("retired", {}).get("why", ""))
PY
)"
[ -n "$RETIRED_WHY" ] && ok "observer marker written: $RETIRED_WHY" \
  || bad "no retired marker for $LABEL in jobs.json"

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]

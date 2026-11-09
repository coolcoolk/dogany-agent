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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/routine-retire.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Keep deletion deterministic and inside the fixture; never use macOS Trash.
mkdir -p "$WORK/bin"
printf '#!/bin/bash\nrm -f -- "$@"\n' > "$WORK/bin/trash"
chmod +x "$WORK/bin/trash"
export PATH="$WORK/bin:$PATH"

# Exercise both entry points in an isolated instance, never the checkout.
SOURCE_ROUTINES="$SCRIPT_DIR/.."
mkdir -p "$WORK/instance/routines/lib"
for f in lib/routine-ctl.sh lib/routine.plist.tpl lib/scheduled_job_names.py lib/update_defer.py health-observer.py routine-ctl.sh; do
  cp "$SOURCE_ROUTINES/$f" "$WORK/instance/routines/$f"
done
printf 'DOGANY_AGENT_NAME=retire-test\n' > "$WORK/instance/.instance.conf"
ROUTINE_CTL="$WORK/instance/routines/lib/routine-ctl.sh"
SHIM="$WORK/instance/routines/routine-ctl.sh"
export ROUTINE_CTL_OS=Darwin
export HOME="$WORK/home"
mkdir -p "$HOME"
export ROUTINE_CTL_LA_DIR="$WORK/LaunchAgents"
# The retirement record defaults to the checkout; keep it in scratch.
export ROUTINE_CTL_RETIRED_FILE="$WORK/retired-routines.tsv"
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

grep -qF "$LABEL" "$ROUTINE_CTL_RETIRED_FILE" \
  && ok "disable records durable retirement" || bad "retirement ledger missing label"

# Replay the agent-facing raw bootstrap attempt through the real hook adapter.
python3 - "$SOURCE_ROUTINES/bash-safety-gate.py" "$LABEL" "$WORK/instance" <<'PYGATE'
import json, os, subprocess, sys
hook, label, root = sys.argv[1:]
env = dict(os.environ, DOGANY_TEST_MODE="1", BASH_GATE_ROOT=root,
           DOGANY_BOT_DATA_DIR=root + "/.telegram_bot", BASH_GATE_MODE="detect")
command = "launchctl bootstrap gui/501 ~/Library/LaunchAgents/" + label + ".plist"
payload = {"tool_name": "Bash", "cwd": root,
           "tool_input": {"command": command}}
proc = subprocess.run([sys.executable, "-B", hook], input=json.dumps(payload),
                      text=True, capture_output=True, env=env, timeout=30)
assert proc.returncode == 0, proc.stderr
out = json.loads(proc.stdout)["hookSpecificOutput"]
assert out["permissionDecision"] == "deny", out
assert "bash routines/lib/routine-ctl.sh enable|disable|load <name>" in out["permissionDecisionReason"]
PYGATE
[ "$?" -eq 0 ] && ok "raw bootstrap refused with lifecycle hint" || bad "raw bootstrap gate failed"
grep -qF "$LABEL" "$ROUTINE_CTL_RETIRED_FILE" \
  && ok "raw refusal preserves retirement" || bad "raw refusal lost retirement"

# A stub models successful bootstrap and print; no real service is changed.
cat > "$WORK/launchctl" <<STUB
#!/bin/bash
echo "\$@" >> "$LOG"
case "\$1" in
  bootstrap|load) touch "$WORK/loaded"; exit 0 ;;
  bootout) rm -f "$WORK/loaded"; exit 0 ;;
  print) [ -f "$WORK/loaded" ]; exit \$? ;;
esac
exit 1
STUB
printf '#!/bin/bash\nexit 0\n' > "$WORK/instance/routines/probe.sh"
bash "$SHIM" enable "$NAME" routines/probe.sh 08:45 >"$OUT" 2>"$ERR"
rc=$?
[ "$rc" -eq 0 ] && ok "shim enable exits 0" || bad "shim enable rc=$rc: $(cat "$ERR")"
[ -f "$WORK/loaded" ] && ok "enable bootstraps internally" || bad "enable did not load"
if grep -qF "$LABEL" "$ROUTINE_CTL_RETIRED_FILE"; then
  bad "enable left durable retirement behind"
else
  ok "enable clears durable retirement"
fi
bash "$SHIM" status "$NAME" >"$OUT" 2>"$ERR"
[ "$?" -eq 0 ] && ok "shim forwards status" || bad "shim status failed"
cp "$ROUTINE_CTL_LA_DIR/${LABEL}.plist" "$AGENT_DIR/routines/${LABEL}.plist"
bash "$SHIM" disable "$NAME" >"$OUT" 2>"$ERR"
[ "$?" -eq 0 ] && ok "shim forwards disable" || bad "shim disable failed"
[ ! -f "$WORK/loaded" ] && ok "disable stops stubbed unit" || bad "disable left unit loaded"
bash "$SHIM" status "$NAME" >"$OUT" 2>"$ERR"
[ "$?" -eq 3 ] && ok "shim preserves unscheduled status rc" || bad "shim lost status rc"

bash "$SHIM" load "$NAME" >"$OUT" 2>"$ERR"
[ "$?" -eq 0 ] && ok "shim forwards shipped-unit load" || bad "shim load failed"
[ -f "$WORK/loaded" ] && ok "load bootstraps internally" || bad "load did not bootstrap"
if grep -qF "$LABEL" "$ROUTINE_CTL_RETIRED_FILE"; then
  bad "load left durable retirement behind"
else
  ok "load clears durable retirement"
fi

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
